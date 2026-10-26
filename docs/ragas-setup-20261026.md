# RAGAS 环境说明（Python 3.14 兼容性）

`eval_external/ragas_eval.py` 依赖 `ragas`。本机 Python 3.14.3，`ragas` 各版本均需额外处理，记录如下以便复现。

## 1. 版本选择

| ragas 版本 | 问题 | 结论 |
|---|---|---|
| 0.4.3（最新） | 硬依赖 `scikit-network`，Python 3.14 无预编译 wheel，需 MSVC 源码编译（本机无） | ❌ |
| 0.1.22 | 强制 `langchain<0.3`（会降级项目 langchain 1.3.10）+ `numpy 1.26`（无 cp314 wheel） | ❌ |
| **0.2.15** | 不降 langchain/numpy、不依赖 scikit-network | ✅ |

安装：`python -m pip install ragas==0.2.15`

## 2. 兼容性补丁（4 处，均在 site-packages/ragas 下）

1. **`llms/base.py`** — 顶部 `from langchain_community.chat_models.vertexai import ChatVertexAI` / `from langchain_community.llms import VertexAI` 在新版 langchain-community 已移除。改成 `try/except` 并置 `None`，多补全白名单改为过滤 `None`。
2. **`metrics/base.py`** — 三处 `asyncio.wait_for(coro, timeout)` 改成 `timeout is None` 时直接 `await coro`（`asyncio.wait_for(None)` 在 3.14 仍会进入 `asyncio.timeout` 报「Timeout should be used inside a task」）。
3. **`executor.py`** — 注释掉模块级 `nest_asyncio.apply()`。它的补丁版 `asyncio.run` 会让 `sniffio.current_async_library()` 检测不到 asyncio，导致 `openai` 报 `AsyncLibraryNotFoundError`。
4. **`run_config.py`** — `add_async_retry` 退化为直接返回 `fn`（禁用重试）。为可选，非必须。

配合 `evaluate(..., run_config=RunConfig(timeout=None))` 使用。

> **复现提示**：以上 4 处补丁针对 `ragas==0.2.15` + `Python 3.14`，是对全局 site-packages 的手工修改，版本或环境不同时可能不适用。**推荐在独立 venv 中安装并应用补丁**，避免污染全局环境；换版本前需重新核对这些补丁是否仍然需要。

## 3. 指标名映射

RPD 文档的 `context_relevancy` 在 ragas 0.2.x 已改名：

| RPD 指标 | ragas 0.2.x | 说明 |
|---|---|---|
| faithfulness | `faithfulness` | 无 reference |
| answer_relevancy | `answer_relevancy` | 无 reference（需 embeddings） |
| context_relevancy | `LLMContextPrecisionWithoutReference()` | 无 reference，**近似替代**旧 context_relevancy（非严格等价） |

`ragas.metrics.context_precision`（0.2.x）是需要 `reference` 的版本，不适用。
