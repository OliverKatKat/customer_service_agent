"""Compare BM25, dense, and hybrid retrieval on labeled synthetic handbook queries."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
CASES_PATH = ROOT / "eval_agent" / "retrieval_cases.json"


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, max(0, int((len(ordered) - 1) * fraction + 0.5)))
    return round(ordered[index], 3)


async def run(dataset: dict, modes: list[str]) -> dict:
    from eval_agent.run import require_eval_environment

    require_eval_environment()
    from app.agent.knowledge_retrieval import retrieve_company_knowledge, resolve_knowledge_scope
    from app.core.vector_db import qdrant_client
    from qdrant_client import models

    tenant_id = dataset["tenant_id"]
    scope = resolve_knowledge_scope(tenant_id)
    if scope is None:
        raise RuntimeError("Synthetic handbook is unavailable")
    text = scope.handbook.read_text(encoding="utf-8")
    paragraphs = [part.strip() for part in text.split("\n\n") if part.strip()]
    cases = dataset["cases"]
    if any(not 0 <= case["expected_paragraph_index"] < len(paragraphs) for case in cases):
        raise ValueError("Dataset paragraph labels do not match handbook")
    version = hashlib.sha256(text.encode("utf-8")).hexdigest()
    if any(mode != "bm25" for mode in modes):
        current = qdrant_client.count(
            collection_name=scope.collection,
            count_filter=models.Filter(must=[
                models.FieldCondition(key="metadata.tenant_id", match=models.MatchValue(value=tenant_id)),
                models.FieldCondition(key="metadata.source", match=models.MatchValue(value=scope.source)),
                models.FieldCondition(key="metadata.document_version", match=models.MatchValue(value=version)),
            ]),
            exact=True,
        ).count
        if current < len(paragraphs):
            raise RuntimeError("Qdrant has not indexed the current synthetic handbook; rerun seed_knowledge.py")

    report = {
        "dataset_version": dataset["dataset_version"],
        "handbook_sha256": version,
        "tenant_id": tenant_id,
        "run_at_utc": datetime.now(timezone.utc).isoformat(),
        "case_count": len(cases),
        "modes": {},
    }
    for mode in modes:
        rows = []
        for case in cases:
            started = time.perf_counter()
            documents = await retrieve_company_knowledge(case["query"], tenant_id, limit=3, mode=mode)
            latency_ms = (time.perf_counter() - started) * 1000
            indices = [doc.metadata.get("paragraph_index") for doc in documents]
            try:
                rank = indices.index(case["expected_paragraph_index"]) + 1
            except ValueError:
                rank = None
            rows.append({
                "id": case["id"],
                "expected_paragraph_index": case["expected_paragraph_index"],
                "retrieved_paragraph_indices": indices,
                "rank": rank,
                "latency_ms": round(latency_ms, 3),
            })
        latencies = [row["latency_ms"] for row in rows]
        report["modes"][mode] = {
            "hit_at_1": round(sum(row["rank"] == 1 for row in rows) / len(rows), 4),
            "recall_at_3": round(sum(row["rank"] is not None for row in rows) / len(rows), 4),
            "mrr_at_3": round(statistics.mean(1 / row["rank"] if row["rank"] else 0 for row in rows), 4),
            "latency_p50_ms": percentile(latencies, 0.5),
            "latency_p95_ms": percentile(latencies, 0.95),
            "cases": rows,
        }
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="Query isolated Qdrant")
    parser.add_argument("--allow-external-embedding", action="store_true")
    parser.add_argument("--mode", choices=["bm25", "dense", "hybrid", "all"], default="all")
    args = parser.parse_args()
    dataset = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    if dataset.get("dataset_version") != "retrieval-1.0" or not dataset.get("cases"):
        raise ValueError("Unsupported or empty retrieval dataset")
    modes = ["bm25", "dense", "hybrid"] if args.mode == "all" else [args.mode]
    if not args.run:
        print(f"Preview: {len(dataset['cases'])} synthetic queries; modes={','.join(modes)}")
        return
    if any(mode != "bm25" for mode in modes) and not args.allow_external_embedding:
        parser.error("Dense and hybrid modes send synthetic queries to the embedding API")
    report = asyncio.run(run(dataset, modes))
    output = ROOT / "eval_agent" / "results" / f"retrieval_{datetime.now(timezone.utc):%Y%m%d_%H%M%SZ}.json"
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(output)
    for mode, result in report["modes"].items():
        print(f"{mode}: hit@1={result['hit_at_1']}, recall@3={result['recall_at_3']}, mrr@3={result['mrr_at_3']}, p95={result['latency_p95_ms']} ms")


if __name__ == "__main__":
    main()
