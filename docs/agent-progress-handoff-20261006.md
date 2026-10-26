# Agent 项目进度交接（2026-10-06）

## 已做

- M0、M1 保留上次隔离评估结果：M0 12/12，M1 8/8。旧 PostgreSQL/Qdrant/Redis 备份和数据卷未动。
- M2：退款工具与工单升级/审核请求/创建审计/outbox 同事务；管理员同商户待审核列表、详情、批准/驳回、二次审批凭据、版本冲突、审计；worker 回查持久化审核与订单，beat 每 15 秒补发失败通知。批准后的原型退款状态只更新一次。
- M3 本地原型：`/review` 浏览器工作台，管理员可看工单/订单证据和审计，点击批准放行或驳回；客户可查自己工单审核状态。页面每 8 秒短轮询。
- Windows API 启动脚本 `scripts/run_api.py` 使用 Selector loop factory，避开 psycopg 与 ProactorEventLoop 不兼容。
- `infra/eval-v3/run.ps1` 单独注入至少 16 字符的 eval 审批凭据。实际值在被 Git 忽略的 `infra/eval-v3/.env`，正式 `app/.env` 未改。
- 隔离 M2/M3 评估最新 8/8：`eval_v3/results/m2_20261006_070639Z.json`。本机 API `/review` 和 `/healthz` 正常；admin 登录/列表正常；正确凭据 + 错误版本返回 409，状态未变。worker 与 beat 已启动并实际消费待审核通知。
- 合成审核 #48 / 工单 #930344 / 订单 E3-DEMO-A88BC31B0D76 已由用户在浏览器批准；真实 Agent 约 2 秒后完成，订单 refunded、工单 resolved，三个 outbox 均 sent，审计含 refund_applied。

## 当前服务

- Docker Compose `agent_eval_v3`：PostgreSQL `127.0.0.1:55432`、Redis `127.0.0.1:56379`、Qdrant `127.0.0.1:56333`。
- 本机 API：`http://127.0.0.1:58080/review`；Celery worker 与 beat 在当前终端会话。重启命令见 `docs/review-demo.md`。
- 旧数据库仍为 Alembic `df73c92fac1b`，尚未应用租户和审核迁移。线上或旧服务端口不应指向目前新代码。

## 问题、选项与决定

1. 创建审核后从 `Session.new` 读取 outbox ID 会丢失：提交后集合为空。改为 flush 时捕获 ID，再提交后入队；beat 兜底补发。
2. Windows 默认 ProactorEventLoop 不兼容 psycopg async pool。直接 `uvicorn` 会反复报错；改为 `asyncio.run(..., loop_factory=asyncio.SelectorEventLoop)` 启动，健康检查通过。
3. 正式 `ADMIN_APPROVAL_CODE` 长度不足，放行会返回 503。可修改正式环境配置，也可为 eval 单独生成凭据；选择后者，先不改旧服务。
4. 浏览器自动化 helper 启动失败（Windows `CreateProcessWithLogonW failed: 1056`）。改用真实本机 HTTP 请求、ASGI 评估和 Node JS 语法检查；视觉点击仍需人工查看。
5. 本机 httpx 默认代理导致 `127.0.0.1` 请求 502；本机验证改用 `trust_env=False`，API 本身健康。

## 下一步

1. 用户已在 `/review` 手动批准 #48，真实 Agent 完成了这一次合成闭环。若需再演示按钮，可运行 `eval_v3/create_demo_review.py --apply` 创建新合成请求。
2. 若要正式环境启用，先在旧数据库备份基础上单独演练迁移 `df73c92fac1b -> b67d21f0c4e8`，补正式审批凭据；不要把 eval v3 夹具混入旧库。
3. 后续评估应单独测真实 Agent 批准后工具调用成功率、超时与失败补偿；确认外部企业微信/飞书/邮件目标后再接通知渠道。
## 用户批准后的跟进（2026-10-06）

- 截图中 approved + paid/escalated 是批准后的短暂中间态；数据库随后显示 refunded/resolved，审计 requested/approved/refund_applied，通知 requested/approved/refund_completed 均 sent。客户状态 API 也返回 resolved。
- 管理端已批准详情现每 8 秒自动重读并区分处理中/原型退款完成；已打开的旧脚本可点击审核条目刷新，重新加载页面后自动刷新生效。
- Agent 曾在一次真实回复中暗示可能到账；worker 已改为以已提交的数据库状态生成审批任务的确定性回复，明确未接支付网关、未发起真实退款或打款。仅做了对 #48 的只读结果检查；新逻辑在新 worker 中运行，尚未产生第二次真实审批样本。
## 批准后任务恢复（2026-10-06 晚，待运行时验证）

- `app/workers/tasks.py` 增加 `schedule_stalled_approved_reviews` 与 `recover_approved_refund`：审核批准至少 60 秒后仍处于 escalated，按持久化审批重新验证租户、客户、工单和订单，经同一个受限退款工具恢复原型状态；最多尝试 3 次，带退避与审计。恢复不依赖 LLM 再次选择工具。
- `app/workers/celery_app.py` 每 30 秒扫描一次。已完成退款的批准 outbox 在重试时直接记为 sent，避免恢复和派发之间的竞态。
- 管理员详情显示处理进度和恢复尝试数；三次仍未完成时提示人工核查。客户侧只显示自己工单的核查提示。
- 当前仅完成 AST 语法检查，**尚未做数据库/worker 回归评估**。Docker Desktop 本次启动又在 Secrets Engine 的 `engine.sock` 报错，隔离 DB 不可用；不要将此处描述为已验证闭环，也不要迁移旧库。
- 下一步：关闭 Docker 错误窗口，保留并改名失效 socket 后重启；运行隔离 M2 评估、构造批准后 Agent 未完成的夹具并确认恢复/重试上限/租户边界，再重启 API、worker、beat。

## 恢复验证结果（2026-10-06 20:50 CST）

- Docker Desktop 的两个失效 AF_UNIX socket 无法单独重命名或删除。已关闭报错进程，将专用 `docker-secrets-engine` 目录和仅含 `dockerInference` 的 `Docker\run` 目录分别改名为带时间戳的 `.stale-*`，保留原内容；Docker 自动重建目录后启动成功。没有执行 factory reset，旧数据卷未动。
- 隔离 eval v3 容器重新启动。原 M2/M3 回归 8/8：`eval_v3/results/m2_20261006_124720Z.json`。新增恢复评估 3/3：`eval_v3/results/recovery_20261006_124839Z.json`，覆盖超时后恢复、幂等、已完成 outbox 重放、三次失败转人工核查。新评估的数据库操作被外层事务回滚；LLM 与 Celery 派发由桩替代。
- 本机 API 已重启，`/healthz` HTTP 200，DB/Redis healthy；Celery worker 与 beat 已启动。实际日志显示 beat 派发 `schedule_stalled_approved_reviews`，worker 消费并返回 queued=0（目前无卡住的审核）。
- 仍需注意：这是隔离原型状态更新，不涉及真实支付网关。真实 Agent 只完成过前述 #48 一次审批样本；恢复链路在隔离评估中调用真实受限退款工具，但没有让外部模型故意失败做线上演练。
