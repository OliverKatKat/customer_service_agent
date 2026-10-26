# Agent 端到端评估

cases.json 与 fixtures.json 定义 20 条 v1.5.0 合成商户案例。先按 ../infra/eval-v3/README.md 配置独立服务、应用密钥和夹具。

在项目根目录通过 ./infra/eval-v3/run.ps1 -Exe python -Arguments @('eval_agent/seed.py') 预览夹具；确认后加 --apply。用同一包装器运行 eval_agent/run.py 默认只预览。提交合成工单要加 --apply；调用外部模型还要加 --allow-external-llm。结果生成在 results/。

这里保留了 2026-10-19 的三轮原始报告、汇总和关键专项证据。该轮 60 次自动评分为 59/60；其中一条已退款案例未通过自动检查。2026-10-08 的人工判读结果属于另一轮评估，不应与本轮混用。评估口径和限制见 ../docs/m4-eval-analysis-20261008.md 及 ../docs/m4-reply-consistency-optimization-20261018.md。

检索对照评估：`python eval_agent/benchmark_retrieval.py` 先预览数据集；完成合成手册重新索引后，通过隔离环境包装器运行 `python eval_agent/benchmark_retrieval.py --run --allow-external-embedding`。报告保存在 `results/retrieval_*.json`。标注集目前只有一个合成租户和 15 个问题，结果只用于方案对照，不代表线上质量。基线结果与口径见 `../docs/rpd-retrieval-baseline-20261026.md`。

偏好生命周期评估：`python eval_agent/run_memory_lifecycle.py --run-number 43` 预览；在隔离环境中加 `--apply --allow-external-llm` 后验证覆盖、撤回和旧任务重放，使用新的 run number 避免重复合成客户。快照恢复探针见 `verify_qdrant_restore.py`，只接受本机隔离 Qdrant，执行后删除临时集合。

向量存储合成负载：`python eval_agent/benchmark_qdrant.py --points 2000 --queries 100 --workers 4` 预览，在隔离环境包装器中加 `--apply` 执行；也可改为 10000 点。报告位于 `results/qdrant_benchmark_*.json`。脚本仅接受本机隔离 Qdrant，运行中创建并最终删除专用集合；报告检查 `passed` 和 `collection_removed` 均为 `true`。这是固定过滤查询的短时本机测量，不能直接作为线上吞吐承诺。
