# RPD 检索评测基线（M2 · before 基线）

> 本页是**检索算子（Rerank / Query Rewriting）改造前**的基线，供后续 before/after 对比使用。

## 1. 结果速览

| 模式 | hit@1 | recall@3 | MRR@3 | 延迟 p50 | 延迟 p95 |
|---|---|---|---|---|---|
| bm25 | 1.0000 | 1.0000 | 1.0000 | 0.36 ms | 0.54 ms |
| dense | 0.9333 | 1.0000 | 0.9556 | 382.59 ms | 605.24 ms |
| hybrid（当前生产配置） | 0.9333 | 1.0000 | 0.9667 | 380.98 ms | 442.71 ms |

- **当前生产基线 = `hybrid`**（`retrieve_company_knowledge` 默认 `mode="hybrid"`，BM25 0.6 + 向量 0.4 融合）。
- 报告原文：`eval_agent/results/retrieval_20261026_103647Z.json`。

## 2. 复现信息

| 项 | 值 |
|---|---|
| 数据集 | `eval_agent/retrieval_cases.json`，`dataset_version=retrieval-1.0`，15 条合成标注查询 |
| 租户 / 集合 | `merchant_eval_agent` / `company_knowledge_eval_v3__merchant_eval_agent` |
| 合成手册 | `company_knowledge/tenants/merchant_eval_agent/handbook.txt`（10 段） |
| handbook sha256 | `4a2152f98644b49a980867045dfb8ebd83512138c6d8e274bc4d06457ee47ad1` |
| 隔离服务 | `agent_eval_v3`（Postgres 55432 / Redis 56379 / Qdrant 56333） |
| 运行时刻 | `2026-10-26T10:36:35Z` |

## 3. 复现步骤

在项目根目录（`D:\Agent构建与评估 - benchmark`）：

```powershell
# 1. 拉起隔离服务
docker compose --env-file infra/eval-v3/.env -f infra/eval-v3/compose.yaml -p agent_eval_v3 up -d

# 2. 建集合 + 索引合成手册（真实调 Dashscope embedding）
./infra/eval-v3/run.ps1 -Exe python -Arguments @('scripts/init_eval_qdrant.py')
./infra/eval-v3/run.ps1 -Exe python -Arguments @('eval_agent/seed_knowledge.py', '--apply', '--allow-external-embedding')

# 3. 跑检索评测（3 模式）
./infra/eval-v3/run.ps1 -Exe python -Arguments @('eval_agent/benchmark_retrieval.py', '--run', '--allow-external-embedding')
```

> 注：`run.ps1` 设了 `$ErrorActionPreference='Stop'`，qdrant-client 1.18 的「Failed to obtain server version」warning 走 stderr 会被当成异常中断。绕过方式：手工注入 `infra/eval-v3/.env` 的隔离环境变量后直接 `python ...`，warning 无害可忽略。

## 4. 关键发现

- **唯一 hit@1 失分是 `x1-pair-led`**（「X1 重新配对时白灯应该怎么闪？」→ 期望第 0 段）：
  - `dense` 排第 3（第 7、4 段之后），`hybrid` 排第 2（第 7 段之后）；`bm25` 排第 1。
  - 原因是第 7 段是 **S1 音箱**配对，与第 0 段 **X1 耳机**配对 LED 存在跨型号语义相近，向量召回把 S1 段拉了上来。这正是后续 Rerank 算子要解决的「top-k 精确度不足」。
- **bm25 全对**：当前 15 条合成查询以词面/型号术语为主，BM25 天然占优；不能据此断言 BM25 优于向量，只能说明标注集偏词面。
- dense/hybrid 延迟（p95 约 0.4–0.6 s）主要来自外部 embedding 调用，BM25 纯本地几乎为 0。

## 5. 口径与限制

- 本结果为**合成租户 + 15 条标注查询**的对照基线，不代表线上知识库质量。
- 三个模式（bm25/dense/hybrid）均为「无 Rerank、无 Query Rewriting」的 before 状态；after（+Rerank、+Query Rewriting）为后续算子改造方向。
- 命中标签 `expected_paragraph_index` 采用 0-based 段落编号，与 handbook 段落顺序一致。

## 6. 下一步

1. 落地 Rerank 与 Query Rewriting，复跑同一脚本产出 after 对比数字。
2. 扩充标注集（更多主题 + 干扰文档），降低 bm25 的「词面优势」偏差，量化更真实的召回提升。
