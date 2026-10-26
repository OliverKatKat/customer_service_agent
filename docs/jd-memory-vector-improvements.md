# Agent 记忆与向量检索改进（2026-10-20）

## 本次改动

1. `requirements.txt` 显式列出 Qdrant、LangChain 检索组件、BM25、tiktoken 和 MCP；Docker 镜像包含两套知识手册。原项目环境依赖和手册遗漏会妨碍全新环境复现。
2. 客户偏好仍由本地规则从客户原话中提取，只存标准化结果；每个商户、客户、偏好类别使用固定 Qdrant point ID。新工单可替换旧值，明确要求删除联系偏好时写入撤回标记。活跃偏好默认 365 天后逻辑过期，可用 `CUSTOMER_MEMORY_TTL_DAYS` 配置。后台任务在 PostgreSQL 事务锁下串行写同一客户，并跳过晚到的旧工单任务。旧 point 仍留在集合中用于迁移兼容；读取时以新 point 为准。撤回标记和逻辑过期不能当作历史审计记录或物理删除完成证明。
3. Agent 任务结果包含 `memory_write_task_id`。客户记忆端到端评估现在核对该 Celery 任务的成功状态和写入数；原有的 Qdrant 读取检查继续保留。普通客户回复仍只承诺“尝试记录”，不会把入队当成写入成功。
4. 公司知识向量检索在 Qdrant 查询中带 `tenant_id` 过滤，返回时仍二次核验；默认商户不再接受无租户标签的旧向量。重新索引脚本会补上租户标签。BM25 缓存以手册内容与版本为键，手册更新后自动重建。手册同来源、非当前版本的向量不会进入答案证据。
5. 合成商户手册扩展到 10 个主题段落，增加 15 条有段落标注的检索问题。评估脚本分别运行 BM25、向量、混合模式，输出 hit@1、recall@3、MRR@3 和 p50/p95。该脚本评估检索阶段；最终答复、拒答和引用还需运行现有 `run_knowledge.py` 并扩展其问题集。
6. `scripts/qdrant_maintenance.py` 提供集合状态检查、缺失 payload 索引创建和集合快照创建。脚本默认只读，变更需 `--apply`。快照恢复应在独立实例演练，不能覆盖当前评估集合。

## 隔离环境运行顺序

使用 `infra/eval-v3/run.ps1` 注入隔离 PostgreSQL、Redis、Qdrant 地址，并让应用获得本机密钥。以下命令在项目根目录执行；包装器参数形式见其 README。

1. `python -m unittest eval_agent.test_memory_lifecycle eval_agent.test_reply_guard`
2. `python eval_agent/seed_knowledge.py` 预览新手册；然后显式使用 `--apply --allow-external-embedding` 为合成商户重新索引。
3. `python eval_agent/benchmark_retrieval.py` 预览问题集；然后在隔离包装器中用 `--run --allow-external-embedding` 跑对照报告。
4. 用新 `--run-number` 跑 `eval_agent/run_memory.py --apply --allow-external-llm`，避免与历史合成客户记忆混合。
   另用不同的 `--run-number` 跑 `eval_agent/run_memory_lifecycle.py --apply --allow-external-llm`，验证偏好变更、撤回和旧任务重放。
5. `python scripts/qdrant_maintenance.py --collection user_longterm_memory --kind memory` 查看索引计划；在隔离环境确认后加 `--apply`。知识集合使用 `--kind knowledge` 和实际集合名。创建快照再加 `--snapshot`。`eval_agent/verify_qdrant_restore.py` 在隔离实例把合成知识快照恢复到临时集合，核对后删除临时集合。
6. `eval_agent/benchmark_qdrant.py --points 2000 --queries 100 --workers 4` 预览配置；通过隔离环境包装器加 `--apply` 运行。可将 `--points` 改为 10000。脚本只连接 `http://127.0.0.1:56333`，使用独立临时集合、100 个合成租户和 1024 维随机向量，验证过滤范围及精确 top1，结束时删除集合。

## 已知边界

- 当前客户偏好记忆使用共享 Qdrant 集合中的逻辑隔离：可信商户/客户身份、双字段检索过滤、返回项复核，以及工具层的来源工单归属校验。旧无归属记录继续隔离。独立集合/实例等物理隔离尚未实现，不能把逻辑隔离描述为存储层强隔离。引入自由文本语义记忆之前，应先沿所有新读写路径复用并扩展这些边界，尤其是记忆类型、来源和撤回状态。
- 客户偏好向量仍是类别占位向量，读取依赖作用域过滤和结构化记录，不应称为语义记忆。自由文本语义记忆需要单独设计写入准入、来源、冲突、保留期限和检索评估。
- 当前撤回仅让偏好不再用于回答，并在 Qdrant 留下撤回标记；彻底删除、审计策略、存量 point 清理和用户自助管理尚未完成。
- 后台任务 ID 使写入可核验，但尚无面向用户的记忆状态查询接口和失败补偿队列。
- 默认商户旧向量若没有 `tenant_id` 元数据，现在不会进入向量检索结果；本地 BM25 仍可用。默认手册重新索引前，需先核对资料是否允许发送给外部 Embedding 服务。
- 15 条检索问题及合成文档只能提供小样本方案对照；不能代表真实业务分布。向量存储负载测试只覆盖 2000/10000 个随机点、单机本地访问和一个固定过滤查询，不等于生产容量规划。

## 2026-10-20 隔离环境验证

- 本地 11 个单元测试通过；`compileall` 通过。代码与原目录同步后逐文件校验一致。
- 合成商户手册重新索引为 10 段，3 个旧版本点被清理。两套集合的预期 payload 索引都已创建并复查无缺项；各创建了一份集合快照。`../eval_agent/results/qdrant_restore_20261020_034647Z.json` 记录了在同一隔离 Qdrant 中恢复到独立临时集合、核对 10/10 点并清理探针集合；异机恢复仍未演练。
- 检索对照报告：`../eval_agent/results/retrieval_20261020_033412Z.json`。15 条标注查询中，BM25 hit@1 为 15/15，向量与混合检索各为 14/15；三者 recall@3 都是 15/15。p95 分别约 0.44 ms、738 ms、854 ms，包含本地检索与外部查询 Embedding 的不同开销。这批小型合成数据不支持宣称某模式在线上更优。
- 客户记忆端到端报告：`../eval_agent/results/memory_e2e_20261020_033621_985773Z.json`，14/14，包含 Celery 写入完成状态核对。生命周期报告：`../eval_agent/results/memory_lifecycle_20261020_033801Z.json`，5/5，覆盖邮件改电话、撤回和旧工单重放。
- 知识问答首次扩充手册回归 `../eval_agent/results/knowledge_e2e_20261020_033851_310149Z.json` 为 11/12，X2 无资料时 Agent 仍给出删除 X2 配对记录的无依据建议。加严知识子 Agent 与 Planner 的未知型号规则后，`knowledge_e2e_20261020_034205_040385Z.json` 的 X2 答复已安全拒答，但旧评分词表将“没有任何官方资料”误判为未拒答，仍报 11/12。评分器增加同义拒答并禁止 X2 具体步骤后，`knowledge_e2e_20261020_034352_400958Z.json` 为 12/12；原始报告均保留。最后一轮 X2 答复已人工核对，没有给出 X2 操作步骤。
- 上述端到端运行使用本机独立 `agent_eval_v3_clean_20261020` PostgreSQL、Redis、Qdrant 数据卷和合成客户；外部 LLM/Embedding 调用已发生。样本规模仍小，不能推断生产稳定性、真实召回率或费用。
- 向量存储合成负载报告：`../eval_agent/results/qdrant_benchmark_2000_20261020_130450Z.json` 和 `../eval_agent/results/qdrant_benchmark_10000_20261020_130516Z.json`。两次都建立了 100 个租户的 1024 维随机向量；各自的单线程与 4 线程档均完成 100/100 次租户/客户过滤和精确 top1 校验，临时集合已删除。2,000 点时单线程/4 线程 p95 为 24.345/8.377 ms，吞吐约 203/745 QPS；10,000 点时为 24.98/19.344 ms、约 151/613 QPS。数字来自本机隔离 Docker 的短时固定查询，包含客户端与本机 HTTP 开销；查询、缓存、硬件和数据分布均不足以外推线上性能。

## 后续隔离核对（2026-10-20）

复查发现固定 ID 的偏好读取虽核对了商户和客户，却没有同时核对记忆类型与类别；扫描路径也可能把固定 ID 与不匹配的类别组合当成旧记录。现已让缺少身份的直接读取返回空，并在固定 ID 和扫描读取时核对类型、类别及 ID；新增畸形 payload 的离线回归用例。此处修复的是偏好读取边界，不代表自由文本语义记忆已具备隔离能力。
