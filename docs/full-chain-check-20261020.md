# 全链路复测记录（2026-10-20，中国时间）

## 环境

- 从原 Agent 目录复制 app/.env 和 infra/eval-v3/.env；两份文件均经 SHA-256 比对一致，未在报告中记录密钥。
- 原 agent_eval_v3 容器已停止，原数据卷保留。当前运行的是 agent_eval_v3_clean_20261020，使用独立的新 PostgreSQL、Redis、Qdrant 数据卷。
- 新库迁移到 Alembic head，并装入 eval_v3 与 eval_agent 固定合成夹具。
- 在隔离 Qdrant 建立空的 company_knowledge_eval_v3 集合，另将三段 EVAL-SOUND 合成手册索引到 merchant_eval_agent 专属集合。未发送默认公司手册到外部 Embedding 服务。
- API 绑定 127.0.0.1:58080；Celery worker 和 beat 使用同一隔离服务。/healthz 返回 API、数据库、Redis 均健康。
- 精简版 infra/eval-v3/run.ps1 为 Python 子进程设置 UTF-8，以适配 Windows 控制台日志。

## 本次结果

| 范围 | 结果 | 报告 |
| --- | ---: | --- |
| M0 身份与订单边界 | 12/12 | ../eval_v3/results/m0_20261019_163356Z.json |
| M1 客户记忆边界 | 10/10 | ../eval_v3/results/m1_20261019_163417Z.json |
| M2/M3 审核、退款工具与页面边界 | 9/9 | ../eval_v3/results/m2_20261019_163437Z.json |
| Agent 端到端合成案例 | 20/20 | ../eval_agent/results/agent_20261019_164943_826689Z.json |
| 客户记忆端到端 | 13/13 | ../eval_agent/results/memory_e2e_20261019_165106_748156Z.json |
| 租户知识 RAG 端到端 | 12/12 | ../eval_agent/results/knowledge_e2e_20261019_165154_576250Z.json |
| 记忆网关 | 7/7 | ../eval_agent/results/gateway_20261019_165213_269923Z.json |

Agent 单条退款工单另有 1/1 预检查报告：../eval_agent/results/agent_20261019_163748_271620Z.json。知识库专项首次运行因其依赖的 eval_memory_alice_2 用户尚未创建而返回 HTTP 401；先运行客户记忆专项后重跑得到表中的 12/12。该首次不完整报告未计入成绩。

## 重启顺序

在本目录中，先用 infra/eval-v3/.env 启动 Compose 项目 agent_eval_v3_clean_20261020。其后分别通过 infra/eval-v3/run.ps1 启动 scripts/run_api.py（127.0.0.1:58080）、Celery worker（solo pool）和 beat。具体命令见 docs/review-demo.md；其中旧项目名 agent_eval_v3 应换为 agent_eval_v3_clean_20261020。

如重新建全新评估卷，先运行迁移、eval_v3/seed.py --apply、eval_agent/seed.py --apply、scripts/init_eval_qdrant.py，再索引合成手册。知识库专项须在 eval_agent/run_memory.py --run-number 2 之后运行。测试会生成新的合成工单和报告。

这些结果属于本机隔离原型与合成数据；退款只更新原型订单状态，没有真实支付网关。外部 LLM 和 Embedding 服务用于本轮合成样例，未来重复运行仍可能因模型输出波动得到不同评分。
## 人工批准闭环补测

使用 eval_v3/create_demo_review.py 在隔离库创建合成订单 E3-DEMO-73792AD55240、工单 #930331 和待审核请求 #15。通过真实登录及 POST /api/v1/reviews/15/approve 接口提交批准，HTTP 202；随后从审核详情接口读到 review.status=approved、processing_state=completed、order.status=refunded、ticket.status=resolved。notification_outbox 中 review_requested、review_approved、refund_completed 三个事件均为 sent。这里的 refunded 仅是原型数据库状态，未调用真实支付网关。