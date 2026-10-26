# 独立评估环境

compose.yaml 只启动本机隔离的 PostgreSQL、Redis、Qdrant，项目名为 agent_eval_v3。先把 env.example 复制为 .env 并替换示例密码；另把 app/.env.example 复制为 app/.env，填写应用所需密钥。两个 .env 都不应入库。

在项目根目录依次执行：

    docker compose --env-file infra/eval-v3/.env -f infra/eval-v3/compose.yaml -p agent_eval_v3 up -d
    ./infra/eval-v3/run.ps1 -Exe python -Arguments @('-m', 'alembic', 'upgrade', 'head')
    ./infra/eval-v3/run.ps1 -Exe python -Arguments @('eval_v3/seed.py')
    ./infra/eval-v3/run.ps1 -Exe python -Arguments @('eval_v3/seed.py', '--apply')
    ./infra/eval-v3/run.ps1 -Exe python -Arguments @('scripts/init_eval_qdrant.py')

run.ps1 检查数据库与服务地址，并只为子进程覆盖连接环境变量。Agent 端到端评估另需运行 eval_agent/seed.py，知识库评估需按 docs/m5-memory-rag-eval-20261009.md 建立隔离集合。真实模型和嵌入调用应使用评估脚本的显式参数。审核页面演示见 docs/review-demo.md。