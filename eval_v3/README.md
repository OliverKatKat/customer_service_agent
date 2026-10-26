# Eval v3 隔离边界评估

fixtures.json 和 cases.json 定义固定合成商户、用户、订单及 M0–M3 边界断言。先使用 ../infra/eval-v3/ 中的 Compose 环境，并通过其 run.ps1 执行迁移和夹具加载。seed.py 默认只预览，--apply 才写入隔离库。

在项目根目录依次运行 ./infra/eval-v3/run.ps1 -Exe python -Arguments @('eval_v3/run_m0.py')、run_m1.py 和 run_m2.py。本目录保留的代表性结果为 M0 12/12、M1 10/10、M2/M3 9/9。它们是权限与状态边界回归，Celery 和外部 LLM 在部分 runner 中由桩替代，不能作为真实 Agent 对话成功率。