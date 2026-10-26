# 退款人工审核本地演示（2026-10-06）

## 当前可直接查看

本机隔离演示入口：`http://127.0.0.1:58080/review`。eval v3 数据库中的合成请求 **审核 #48 / 工单 #930344 / 订单 E3-DEMO-A88BC31B0D76** 已由管理员批准并完成原型状态更新。这条记录由 `eval_v3/create_demo_review.py` 调用真实退款工具创建；`review_requested`、`review_approved`、`refund_completed` 三个 outbox 事件均已送达。金额为 1599 元，未连接实际支付网关。

用 `eval_alpha_admin` 登录管理员工作台。登录密码在本机被 Git 忽略的 `infra/eval-v3/.env` 的 `EVAL_V3_PASSWORD`；点击“批准放行”或“驳回”还需输入同文件的 `EVAL_V3_APPROVAL_CODE`。不要把两个凭据提交到仓库或截图分享。客户视角可用 `eval_alpha_alice` 登录，输入工单 `930344` 查看审核状态。页面的登录令牌和二次审批凭据仅在当前页面内存中使用，不写入浏览器本地存储。

## 启动与重启

当前已启动独立 `agent_eval_v3` Docker 服务，以及本机 API、Celery worker、Celery beat。Docker 使用 `127.0.0.1:55432/56379/56333`，本机 API 使用 `127.0.0.1:58080`。重启 Windows 或关闭会话后，在项目根目录分别运行以下命令。`run.ps1` 会校验 eval 地址，覆盖子进程的数据连接和单独的审批凭据；不会迁移旧库。

```powershell
$python = 'python'
$env:PYTHONIOENCODING = 'utf-8'
docker compose --env-file infra/eval-v3/.env -f infra/eval-v3/compose.yaml -p agent_eval_v3 up -d
./infra/eval-v3/run.ps1 -Exe $python -Arguments @('scripts/run_api.py','--host','127.0.0.1','--port','58080')
```

另开两个终端运行 worker 和 beat：

```powershell
$python = 'python'
$env:PYTHONIOENCODING = 'utf-8'
./infra/eval-v3/run.ps1 -Exe $python -Arguments @('-m','celery','-A','app.workers.celery_app:celery_app','worker','--pool=solo','--loglevel=INFO')
```

```powershell
$python = 'python'
$env:PYTHONIOENCODING = 'utf-8'
./infra/eval-v3/run.ps1 -Exe $python -Arguments @('-m','celery','-A','app.workers.celery_app:celery_app','beat','--loglevel=INFO','--schedule','infra/eval-v3/celerybeat-schedule')
```

若 #48 已处理，可运行下列命令再创建一条独立合成订单和待审核请求。它会**增加** eval v3 数据，旧数据不会被清空；不调用外部 LLM。`run_m2.py` 只使用固定夹具工单 #930305，演示记录不影响该评估。

```powershell
./infra/eval-v3/run.ps1 -Exe $python -Arguments @('eval_v3/create_demo_review.py','--apply')
```

## 操作链路

1. 退款工具核对商户、客户、工单、订单、金额、状态，在一个事务中写入 `review_requests.pending`、创建审计和 outbox、将工单设为 `escalated`。
2. 管理员列表按登录账号所属商户查询。详情展示工单描述、订单状态、金额和审计记录；按钮提交审核版本、原因和二次审批凭据。重复点击或旧版本返回 409。
3. 批准写入 `approved` 与 outbox；worker 重新核对审核、商户、客户、订单和金额，再调度 Agent。Agent 工具在更新原型订单状态前再次读取持久化批准记录。驳回将工单设为 `rejected`，不派发退款 Agent。
4. worker 收到 `review_requested`、`review_rejected`、`refund_completed` 时写入商户级 Redis 站内事件流；页面每 8 秒短轮询数据库。beat 每 15 秒补发 pending/failed outbox。客户只能查询自己工单的审核状态。

## 评估与边界

`eval_v3/run_m2.py` 在隔离库运行 8/8：跨商户审核、重复批准、驳回、普通用户/RBAC/版本/凭据、待审核创建幂等、通知失败重试、批准后工具只执行一次、页面与客户查询租户边界。最新报告：`eval_v3/results/m2_20261006_070639Z.json`。评估中 Agent 派发由桩函数替代。另外，用户在浏览器手动批准合成审核 #48 后，真实 worker 调用外部模型并执行 `process_large_refund`，约 2 秒内将订单更新为 `refunded`、工单更新为 `resolved`；这是 1 次端到端成功样本，不能推出整体完成率。

本机实际启动检查：`/review` HTTP 200，`/healthz` 报告 DB 与 Redis 健康；管理员登录与待审核列表 HTTP 200；#48 的三个 outbox 均为 `sent`，订单为 `refunded`、工单为 `resolved`；正确凭据加过期版本的预先验证返回 409，未更改当时的 pending 状态。浏览器自动化组件在当前 Windows 会话启动失败，尚未做自动化视觉点击检查。

旧数据库还在 `df73c92fac1b`，未迁移到新表；旧 PostgreSQL/Qdrant/Redis 数据卷未清空。正式环境启用前需要单独执行旧库迁移、设置至少 16 字符的正式 `ADMIN_APPROVAL_CODE`，并重新验证实际 Agent 完成率、失败补偿与权限边界。对外企业微信/飞书/邮件通知尚未接入；当前是站内队列与短轮询。
## 批准后详情刷新修正

用户首次点击批准后，截图显示审核 approved，但详情中的订单仍 paid、工单仍 escalated；数据库核对表明这是 Agent 完成前的短暂状态。页面已改为在已批准详情打开时每 8 秒重新读取详情，并显示“批准后处理中”或“原型退款已完成”。旧页面会话可以点击 #48 条目立即重新读取；刷新页面后需重新登录以加载新脚本。

真实 Agent 的旧回复曾提到“到账时间以支付渠道为准”，与未接支付网关的事实不一致。worker 已改为在批准任务结束后回查持久化审核、订单与工单，再生成固定回复，明确“未发起真实退款或打款”。这项回复修正通过已完成的 #48 状态做了只读检查，后续新审批任务会使用重启后的 worker。
## 批准后未完成时的恢复（待验证）

后台在审核批准 60 秒后检查未完成工单，通过原有受限退款工具尝试恢复，最多 3 次。每次尝试写审计；三次未完成时管理端显示“自动恢复未完成，请人工核查”，客户查询显示联系人工客服。恢复只更新隔离原型数据库中的订单状态，未接入支付网关。此功能目前通过 AST 语法检查，Docker Desktop 的 Secrets Engine 启动故障阻止了数据库回归，不能视为已完成验收。

## 2026-10-06 恢复评估更新

Docker Desktop 已恢复，隔离 API `/healthz` 为 200，worker 和 beat 运行。原 M2/M3 评估 8/8；新恢复评估 3/3，报告分别为 `eval_v3/results/m2_20261006_124720Z.json` 和 `eval_v3/results/recovery_20261006_124839Z.json`。早先“待验证”的描述以此节为准。新评估使用回滚事务和桩派发，不等同于真实 Agent 故障注入；真实退款仍未接支付网关。

管理端与客户查询在三次恢复未完成时的 API 验证已补入 `eval_v3/run_m2.py`，最新 9/9 报告：`eval_v3/results/m2_20261006_125114Z.json`。本机 `/review` 返回 HTTP 200 且加载了新进度提示脚本，脚本通过 Node 语法检查。
