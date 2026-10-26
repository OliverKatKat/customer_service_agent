# 故障排查记录（Troubleshooting Log）

> 本文件专门记录本项目踩过的坑。每次记录包含：**错误出现的场景**、**排查过程**、**根因**、**修复**，以及**解决时做的 trade-off**。以后遇到类似问题可先翻这里。

---

## 2026-10-25 · 客服 Agent 卡死在「处理中」—— SSE 长连接事务阻塞 `CREATE INDEX CONCURRENTLY`

### 一、错误出现的场景

- **环境**：Windows + Python 3.14（`pythoncore-3.14-64`）+ VS Code + Docker Desktop。
- **项目**：售后客服 Agent（FastAPI + LangGraph + Celery + PostgreSQL/Redis/Qdrant）。
- **操作**：本地隔离环境（`agent_eval_v3`）启动 API（`58080`）、Celery worker（`--pool=solo`）、Celery beat，然后在 `/chat` 聊天页以客户身份提交一条退款工单。
- **现象**：
  - 前端一直卡在「客服 Agent 处理中」，进度步骤不再增长；
  - worker 日志停在 `Celery Agent task started` 之后就不再输出；
  - 工单状态一直是 `pending`，没有生成退款审核记录（`/api/v1/tickets/<id>/review` 返回 404）；
  - 整个过程不报错、不超时，就是「永远卡住」。

### 二、我是怎么找到的（排查过程）

按「由外到内」逐层排除，最后用数据库锁视图一锤定音：

1. **排除基础设施**：确认三个 Docker 容器 `healthy`、端口通、Qdrant 集合、DB 迁移和 seed 数据都补齐。
2. **排除模型/网络**：写脚本分别测 DeepSeek（chat）和 Dashscope（embedding），同步 / 异步 / 带 `thinking:disabled` 的 `ChatOpenAI` 全部正常返回（0.7~3 秒）。→ 不是 LLM 或网络问题。
3. **看任务事件流**（Redis）：
   ```bash
   docker exec agent_eval_v3-redis-1 redis-cli XRANGE "agent:task_events:<task_id>" - +
   ```
   结果只有 `queued` 和 `started` 两条事件，**没有任何节点进度事件**（`checking_intent` / `planning` …）。→ 说明 LangGraph 一个节点都没执行。
4. **看 checkpoints 表**：
   ```sql
   SELECT count(*) FROM checkpoints;   -- 0 行
   ```
   0 行意味着 graph 从未创建过 checkpoint → 卡点在 graph 启动（checkpointer）阶段，而不是某个节点。
5. **写最小复现脚本**（每个步骤包 `asyncio.wait_for(..., timeout=15)` 逐个定位）：
   ```
   [pool.open] OK
   [SELECT 1] OK
   [saver.setup] TIMEOUT   ← 卡在这
   [saver.aget_tuple] OK
   ```
   定位到 **`AsyncPostgresSaver.setup()` 卡住**，而连接池本身正常。
6. **读 `setup()` 源码**（langgraph-checkpoint-postgres 3.0.5）：它内部会执行迁移，其中包含 `CREATE INDEX CONCURRENTLY IF NOT EXISTS checkpoints_thread_id_idx`。
7. **关键一步 —— 查 PostgreSQL 锁视图**：
   ```sql
   SELECT pid, state, wait_event_type, wait_event, left(query,80), now()-query_start
   FROM pg_stat_activity
   WHERE datname='agent_eval_v3' AND state <> 'idle';
   ```
   发现三件关键事：
   - 一个 `idle in transaction` 的会话（14 分钟，查询是 `SELECT tenants...`，来自登录鉴权）；
   - 两个 `CREATE INDEX CONCURRENTLY checkpoints_thread_id_idx` 卡在等锁（一个来自 worker，一个来自我的复现脚本）；
   - `pg_locks` 显示它们在互相等锁。

### 三、根因

一个**环状等待**：

1. `/chat` 发消息后，前端用 **SSE 长连接**（`/api/v1/task/{task_id}/events`）实时拉进度。这个端点的鉴权依赖 `get_current_user` 会执行 `db.get(Tenant)` 打开一个**读事务**；因为是长连接，FastAPI 的 `get_db` 会话要等流结束才关闭 → 这个读事务被挂起数分钟，状态变成 `idle in transaction`。
2. 与此同时，worker 第一次跑 LangGraph 时，checkpointer 的 `setup()` 要执行 `CREATE INDEX CONCURRENTLY`；PostgreSQL 规定它**必须等所有早于它的旧事务结束**才能继续。
3. 于是：**SSE 等 worker 出事件 → worker 的 `setup()` 等 SSE 的事务关闭 → 谁都不动**，形成死锁式卡顿。

### 四、修复

- **立即（解卡）**：杀掉挂起的会话，让索引能建成、worker 恢复。
  ```sql
  SELECT pg_terminate_backend(1097);   -- idle in transaction（根因会话）
  SELECT pg_terminate_backend(1967);   -- 复现脚本残留的 CREATE INDEX
  ```
  杀掉后 `checkpoints_thread_id_idx` 立刻建成，Agent 恢复正常并跑完了整条链路。

- **永久（治本）**：在 `app/api/v1/endpoints.py` 的 `get_current_user` 末尾加一行，让读事务立刻释放：
  ```python
  # Release the read transaction now so long-lived streaming responses (SSE)
  # don't keep an open transaction that blocks CREATE INDEX CONCURRENTLY.
  await db.commit()
  return {"sub": user.username, "role": user.role, "tenant_id": user.tenant_id}
  ```

### 五、Trade-off（解决时做的权衡）

1. **立即解卡：选择「杀会话」而不是「重建容器」**
   - 另一条路是 `docker compose down -v` 重建（能清掉卡住的会话，但要清空已经灌好的 seed 数据，还要重新跑迁移/灌数/建集合）。
   - **权衡**：`pg_terminate_backend` 略「侵入」（当时不确定那个会话是谁开的），但**快、且保留数据**。最终选了它，并事后确认那个会话只是 SSE 鉴权泄漏的，杀掉无副作用。

2. **治本：选择在共享依赖 `get_current_user` 里 `commit()`，而不是只给 SSE 端点做定点改造**
   - 定点改造（比如让 SSE 端点鉴权后手动关会话）更「精准」，但要改多处、逻辑分散。
   - **权衡**：`commit()` 作用在所有走鉴权的接口上，改动面更大；但代码最简单、且**覆盖了未来所有长连接型接口**（SSE / WebSocket），避免同类问题。已确认 `commit()` 对下游写接口（如 `/submit`、`approve` 里的 `SELECT ... FOR UPDATE` + 提交）无影响，因为它们会在新事务里继续。

3. **没有走的路：不换 checkpointer 后端、不改事件循环**
   - 早期一度怀疑是 Windows 事件循环 / psycopg 兼容性问题。先写脚本验证了 **psycopg 在 `SelectorEventLoop` 下正常工作**（`pool.open` + `SELECT 1` 都 OK），排除了这个方向，避免了一笔大改动。

### 六、预防 / 以后再遇到怎么办

- **依赖注入里的读事务，返回前一定要 `commit()` 或 `rollback()`**，尤其是可能被 SSE / WebSocket 等长连接复用的端点。
- LangGraph checkpointer 卡住时，别先怀疑模型/网络，**先查 `pg_stat_activity` 看有没有 `idle in transaction` 和 `CREATE INDEX CONCURRENTLY` 等锁**，能省很多时间。

---

## 2026-10-26 · 重启后 Redis/Qdrant 起不来 —— Windows 端口被 WinNAT 保留

### 一、错误出现的场景

- **环境**：Windows + Docker Desktop（重启电脑 / 关闭 Docker Desktop 之后再次打开）。
- **操作**：启动隔离环境 `agent_eval_v3`（Postgres/Redis/Qdrant），然后跑 API/worker/脚本。
- **现象**：
  - 跑脚本时 Qdrant 报 `[WinError 10061] No connection could be made because the target machine actively refused it`；
  - `docker compose up -d` 报 `listen tcp4 127.0.0.1:56379: bind: An attempt was made to access a socket in a way forbidden by its access permissions`；
  - `docker ps -a` 里 `agent_eval_v3-redis-1` 和 `agent_eval_v3-qdrant-1` 都是 `Exited`，只有 db 是 `Up`。

### 二、我是怎么找到的（排查过程）

1. `docker ps -a` → Redis/Qdrant Exited、db Up。
2. `netstat -ano | findstr ":56333 :56379"` → 无监听。
3. `docker compose up -d` → 报「socket forbidden by access permissions」。
4. `netsh int ipv4 show excludedportrange protocol=tcp` → 一眼看到 **`56333-56432` 被 Windows 划成保留端口**，正好覆盖 Qdrant(56333) 和 Redis(56379)。
5. 试 `net stop winnat` → 普通权限报 "Access denied"（需要管理员）。

### 三、根因

Windows 重启后，**Hyper-V / WinNAT 动态保留了一段端口**（本例是 56333–56432），把 eval 环境的 Qdrant(56333) 和 Redis(56379) 都盖住了；Postgres(55432) 不在保留范围内所以幸免。这是 Docker Desktop on Windows 的老毛病。

### 四、修复

**管理员 PowerShell** 里按顺序执行（先停 WinNAT 释放端口 → 趁空闲立刻起容器占住 → 再重启 WinNAT）：

```powershell
cd <项目根目录>
net stop winnat
docker compose --env-file infra/eval-v3/.env -f infra/eval-v3/compose.yaml -p agent_eval_v3 up -d
net start winnat
```

验证：`docker ps --filter "name=agent_eval_v3"` 三个容器都 `Up`。

### 五、Trade-off（解决时做的权衡）

1. **选「重启 WinNAT 释放端口」，而不是「改端口」**
   - 重启 WinNAT：不改任何配置、是标准解法，但需要管理员权限，且**下次重启 Windows 可能复发**；
   - 改端口：能直接改（compose.yaml / .env / run.ps1 / init_eval_qdrant.py 四处硬编码），确定性高、不用管理员，但要动配置、跟主项目文档不一致，且新端口仍可能在下一次重启时被再次保留。
   - 最终先走了「重启 WinNAT」这条不改配置的路。
2. **没有在普通权限下反复试 `net stop winnat`**：知道它会 "Access denied"，直接让用户去管理员终端，省了来回。

### 六、预防 / 以后再遇到怎么办

- **重启 Windows 后，先 `docker ps -a`** 看容器状态；如果 Redis/Qdrant Exited，优先怀疑端口被保留。
- 快速确认：`netsh int ipv4 show excludedportrange protocol=tcp`，看目标端口是否落在保留段。
- 如果频繁复发，可考虑把 eval 端口挪到更低、更不容易被动态保留的段（<49152），或写个启动脚本自动 `net stop winnat → up → net start winnat`。
