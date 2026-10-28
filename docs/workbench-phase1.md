# 第一批售后工作台交付记录

日期为2026年10月28日，目标目录为 `D:\Agent构建与评估 - 进化`。

## 已交付

- 新增案件、工单归属、人工备注、待办与事件五张业务表，迁移版本为 `c12a8e4d5f90`。
- `/submit` 在同一事务保存工单和案件登记；返回兼容字段并增加 `case_id`。传入本人相同订单且未结束的 `case_id` 时关联重复联系。不传时创建新申请，不按订单号自动合并。
- 新增 `staff` 和 `analyst` 角色授权；抽出共享鉴权，原审核接口仍限 `admin`。角色、账号和租户有效性每次从数据库重新核验。
- 工作台支持案件列表和详情、人工确认分类、备注、待办领取与完成、版本冲突处理。结束案件前核对全部关联工单和模拟执行事实。
- 新增 `/workbench` 页面，包含待办、案件处理和台账统计视图。工作人员与分析员按角色显示入口；审核操作仍前往原 `/review`。
- 新增专用只读台账统计连接，缺配置或账号有写权限均返回503，不回退主库。统计保留未确认原因。
- API 导入阶段不再访问 Qdrant 或调用嵌入服务。长期记忆集合初始化移到 Agent 首次使用，人工工作台不依赖向量库启动。

## 接口

新增接口前缀为 `/api/v1`：`GET /workbench/cases`、`GET /workbench/cases/{case_id}`、`PATCH /workbench/cases/{case_id}`、`POST /workbench/cases/{case_id}/notes`、`GET /workbench/todos`、`PATCH /workbench/work-items/{id}` 和 `GET /analytics/overview`。详细字段可查看 FastAPI OpenAPI。

案件和待办列表使用带签名且绑定身份与筛选条件的游标。详情最多展示100条联系、最近50条备注与事件、200条审核事件，并返回截断标记。完整历史分页与导出尚待补齐。

台账统计的三个计数为区间内登记申请、区间内已分类售后及当前未结束案件。前两项按 `[start,end)` 查询，售后分类为退款、退货、投诉。当前未结束案件是当前存量，不是历史时点积压。没有商品、金额、处理时长、比较期或自然语言查数能力，不能将本批计数包装为完整经营分析。

## 启动和迁移

先使用项目专用 Python 环境并安装 requirements。此机全局 OpenTelemetry API 1.41.1 与 SDK 1.45.1 不兼容；项目要求 API 与 SDK 都为1.41.1，FastAPI instrumentation 为0.62b1。不要通过修改全局环境修复。

```powershell
Set-Location -LiteralPath 'D:\Agent构建与评估 - 进化'
# 确认 app/.env 指向预期的本地数据库并备份后，再应用业务迁移。
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe scripts/run_api.py --host 127.0.0.1 --port 8000
```

Windows 使用已有 run_api.py 的 Selector 事件循环，避免 psycopg 与默认 Proactor 事件循环冲突。本批验证仅迁移临时测试库，没有自动迁移现有 agent_db 或回填其中的业务数据。

```powershell
# 默认仅预览未关联工单，不写数据。
.\.venv\Scripts\python.exe scripts/backfill_workbench_cases.py
# 只有确认旧工单确实以 UTC 保存时，才按该来源时区回填。
.\.venv\Scripts\python.exe scripts/backfill_workbench_cases.py --apply --source-timezone UTC
```

回填每批默认100条，跳过找不到同商户同客户订单的记录；重复运行不重复建立关联。不根据旧 updated_at 猜测解决时间，旧已解决工单仍保留为待人工核对的台账案件。

新账号通过 `scripts/create_workbench_user.py --tenant 商户ID --username 新账号 --role staff` 预览，增加 `--apply` 后创建；密码从 `WORKBENCH_ACCOUNT_PASSWORD` 环境变量读取，至少12个字符。`analyst` 同理。命令不修改已有账号，不提供公开提权接口。

## 只读分析配置

新增环境变量为 `ANALYTICS_READONLY_DATABASE_URL`，要求 `postgresql+asyncpg` URL。数据库管理员需创建独立登录角色，授予目标数据库 CONNECT、public schema USAGE 和 case_registry SELECT。角色不得拥有业务表，不得有 SUPERUSER、BYPASSRLS 或任何 public 业务表的写权限。使用数据库管理工具安全设置密码，不将真实 URL 提交 Git。

未配置该变量时，只有台账统计返回503；人工案件与审批仍可使用。读取处同时设置只读、可重复读事务和三秒语句超时。角色配置完成后，以读账号实际尝试写入必须被数据库拒绝。

## 验证与复现

```powershell
# 单独的临时 PostgreSQL，绑定127.0.0.1:55458，数据在 tmpfs 中。
docker compose -p workbench-tests -f infra/workbench-tests/compose.yaml up -d --wait
.\.venv\Scripts\python.exe scripts/run_workbench_tests.py
docker compose -p workbench-tests -f infra/workbench-tests/compose.yaml down
```

测试脚本覆盖自己的环境变量，只连接 `wb_test@127.0.0.1:55458/workbench_test`，测试文件再次校验 URL，拒绝连接其他数据库。测试会清理该临时库的合成数据。模型地址被定向到不可用的本机端口，队列派发在进程内替换，不消耗模型调用。自动报告写入 `eval_workbench/results`。

已通过24项 PostgreSQL 与 HTTP 集成检查：原审批版本与 Outbox 契约、跨租户读写拒绝、数据库复合外键、角色拒绝与实时失效、并发编辑、重复联系、备注归属、待办负责人、游标、只读缺配置和越权配置、真实写权限拒绝、回填幂等、空库升降级以及有案件时阻止破坏性降级。并未重跑全部原有 Agent、RAGAS 或 tau-bench 模型评估。

无头 Chrome 已验证真实登录、案件详情、备注持久化、HTML 文本安全展示、分类修改、退出清理、分析员视图、只读统计及390px手机布局；未出现页面 JavaScript 错误。此结果不代表性能或真实业务提效已验收。

## 后续事项

订单金额与旧时间迁移、在线分析事实投影、审核异常队列、结构化材料草稿、在新页面直接审批、客户聊天页显式继续同一申请、完整历史分页和经营分析为后续交付。当前客户页面仍兼容原协议；API 已支持 case_id，页面自动继续申请尚未接入。源代码改造不包含真实支付操作。
