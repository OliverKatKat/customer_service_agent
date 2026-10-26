"""RAGAS eval for the company-knowledge subagent.

Generates (question, retrieved_contexts, response) triplets through the
knowledge_agent_graph, then scores faithfulness / answer_relevancy /
context_precision (RAGAS 0.2.x's nearest replacement for the former context_relevancy, not strictly equivalent).

Usage:
  python eval_external/ragas_eval.py                     # preview only
  python eval_external/ragas_eval.py --apply --allow-external-llm
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TENANT_ID = "merchant_eval_agent"
QUESTIONS_PATH = ROOT / "eval_external" / "ragas_questions.json"
RESULTS_DIR = ROOT / "eval_external" / "results"

# faithfulness / answer_relevancy / context relevance. RAGAS 0.2.x removed the old
# `context_relevancy`; LLMContextPrecisionWithoutReference is used as an approximate
# substitute (same notion, not strictly equivalent).
RAGAS_METRIC_NAMES = (
    "faithfulness",
    "answer_relevancy",
    "llm_context_precision_without_reference",
)


def _sanitize(obj):
    """Convert non-finite floats (NaN/Inf) to None so JSON stays valid."""
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize(v) for v in obj]
    if isinstance(obj, float) and not math.isfinite(obj):
        return None
    return obj


def _dump_json(obj) -> str:
    """Serialize to valid JSON; allow_nan=False makes any residual NaN raise."""
    return json.dumps(_sanitize(obj), ensure_ascii=False, indent=2, allow_nan=False)


def load_questions() -> list[dict]:
    dataset = json.loads(QUESTIONS_PATH.read_text(encoding="utf-8"))
    if dataset.get("dataset_version") != "ragas-1.0" or not dataset.get("questions"):
        raise ValueError("Unsupported or empty RAGAS question dataset")
    return dataset["questions"]


async def build_triplets(questions: list[dict]) -> list[dict]:
    from app.agent.knowledge_agent import knowledge_agent_graph

    triplets: list[dict] = []
    for item in questions:
        result = await knowledge_agent_graph.ainvoke(
            {"query": item["question"], "tenant_id": TENANT_ID}
        )
        triplets.append({
            "id": item["id"],
            "question": item["question"],
            "paragraph_index": item.get("paragraph_index"),
            "status": result.get("status", "unknown"),
            "retrieved_contexts": [
                evidence["text"] for evidence in result.get("evidence", [])
            ],
            "response": result.get("answer", ""),
        })
    return triplets


def run_ragas(triplets: list[dict]) -> dict:
    from ragas import evaluate
    from ragas.dataset_schema import EvaluationDataset, SingleTurnSample
    from ragas.metrics import (
        LLMContextPrecisionWithoutReference,
        answer_relevancy,
        faithfulness,
    )
    from langchain_openai import ChatOpenAI, OpenAIEmbeddings

    from app.core.config import settings

    judge_llm = ChatOpenAI(
        model=settings.QWEN_JUDGE_MODEL_NAME,
        api_key=settings.QWEN_JUDGE_API_KEY,
        base_url=settings.QWEN_JUDGE_BASE_URL,
        temperature=0,
    )
    embeddings = OpenAIEmbeddings(
        openai_api_key=settings.DASHSCOPE_API_KEY,
        openai_api_base=settings.EMBEDDING_API_BASE,
        model=settings.EMBEDDING_MODEL_NAME,
        check_embedding_ctx_length=False,
    )

    usable = [
        item for item in triplets
        if item["retrieved_contexts"] and item["response"].strip()
    ]
    skipped = [item["id"] for item in triplets if item not in usable]

    samples = [
        SingleTurnSample(
            user_input=item["question"],
            retrieved_contexts=item["retrieved_contexts"],
            response=item["response"],
        )
        for item in usable
    ]
    dataset = EvaluationDataset(samples=samples)
    from ragas.run_config import RunConfig

    result = evaluate(
        dataset,
        metrics=[faithfulness, answer_relevancy, LLMContextPrecisionWithoutReference()],
        llm=judge_llm,
        embeddings=embeddings,
        run_config=RunConfig(timeout=120),
    )
    frame = result.to_pandas()
    summary = {
        name: round(float(frame[name].mean()), 4)
        for name in RAGAS_METRIC_NAMES
        if name in frame.columns
    }
    per_sample = []
    for item, (_, row) in zip(usable, frame.iterrows()):
        per_sample.append({
            "id": item["id"],
            "question": item["question"],
            "status": item["status"],
            "retrieved_context_count": len(item["retrieved_contexts"]),
            "scores": {
                name: (round(float(row[name]), 4) if name in frame.columns else None)
                for name in RAGAS_METRIC_NAMES
            },
        })
    return {
        "metric_names": list(RAGAS_METRIC_NAMES),
        "judge_model": settings.QWEN_JUDGE_MODEL_NAME,
        "usable_count": len(usable),
        "skipped_ids": skipped,
        "summary": summary,
        "per_sample": per_sample,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Generate triplets and run RAGAS metrics")
    parser.add_argument("--allow-external-llm", action="store_true")
    parser.add_argument("--max-questions", type=int, default=50)
    args = parser.parse_args()

    questions = load_questions()
    selected = questions[: args.max_questions] if args.max_questions > 0 else questions
    print(
        f"RAGAS eval dataset ragas-1.0: {len(selected)}/{len(questions)} questions, "
        f"tenant={TENANT_ID}"
    )
    if not args.apply:
        print("Preview only. Pass --apply --allow-external-llm to generate triplets and score.")
        return
    if not args.allow_external_llm:
        raise ValueError(
            "Triplet generation and RAGAS scoring need --allow-external-llm "
            "(retrieval embedding + synthesis LLM + judge LLM)"
        )

    from eval_agent.run import require_eval_environment

    require_eval_environment()

    triplets = asyncio.run(build_triplets(selected))
    report = run_ragas(triplets)

    RESULTS_DIR.mkdir(exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%SZ")
    triplets_path = RESULTS_DIR / f"ragas_triplets_{timestamp}.json"
    report_path = RESULTS_DIR / f"ragas_{timestamp}.json"

    triplets_path.write_text(
        _dump_json({
            "dataset_version": "ragas-1.0",
            "tenant_id": TENANT_ID,
            "run_at_utc": datetime.now(timezone.utc).isoformat(),
            "question_count": len(triplets),
            "triplets": triplets,
        }),
        encoding="utf-8",
    )
    report_path.write_text(
        _dump_json({
            "dataset_version": "ragas-1.0",
            "tenant_id": TENANT_ID,
            "run_at_utc": datetime.now(timezone.utc).isoformat(),
            **report,
        }),
        encoding="utf-8",
    )
    print(f"triplets: {triplets_path}")
    print(f"report:   {report_path}")
    print(f"summary:  {report['summary']} (usable={report['usable_count']}, skipped={report['skipped_ids']})")


if __name__ == "__main__":
    main()
