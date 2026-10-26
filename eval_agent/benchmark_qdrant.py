"""Measure scoped Qdrant queries on disposable synthetic vectors."""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
COLLECTION = "jd_vector_benchmark_20261020"
DIMENSIONS = 1024
TENANTS = 100


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int((len(ordered) - 1) * fraction + 0.5)))
    return round(ordered[index], 3)


def run(points_count: int, queries: int, workers: int) -> dict:
    if os.environ.get("QDRANT_URL") != "http://127.0.0.1:56333":
        raise RuntimeError("Benchmark is restricted to isolated eval Qdrant")
    from qdrant_client import QdrantClient, models

    client = QdrantClient(url=os.environ["QDRANT_URL"], trust_env=False, timeout=60)
    report = {
        "run_at_utc": datetime.now(timezone.utc).isoformat(),
        "collection": COLLECTION,
        "vector_dimensions": DIMENSIONS,
        "tenant_count": TENANTS,
        "point_count_requested": points_count,
        "queries_per_profile": queries,
        "profiles": {},
        "passed": False,
    }
    created = False
    try:
        if client.collection_exists(COLLECTION):
            raise RuntimeError("Benchmark collection already exists; refusing to overwrite it")
        client.create_collection(
            collection_name=COLLECTION,
            vectors_config=models.VectorParams(size=DIMENSIONS, distance=models.Distance.COSINE),
        )
        created = True
        for field, schema in {
            "tenant_id": models.KeywordIndexParams(
                type=models.KeywordIndexType.KEYWORD, is_tenant=True,
            ),
            "customer_id": models.PayloadSchemaType.KEYWORD,
        }.items():
            client.create_payload_index(
                collection_name=COLLECTION, field_name=field,
                field_schema=schema, wait=True,
            )

        rng = random.Random(20261020)
        batch = []
        target_vector = None
        ingest_started = time.perf_counter()
        for index in range(points_count):
            vector = [rng.uniform(-1, 1) for _ in range(DIMENSIONS)]
            if index == 42:
                target_vector = vector
            batch.append(models.PointStruct(
                id=index + 1,
                vector=vector,
                payload={
                    "tenant_id": f"tenant_{index % TENANTS:03d}",
                    "customer_id": f"customer_{(index // TENANTS) % 10:02d}",
                },
            ))
            if len(batch) == 128:
                client.upsert(collection_name=COLLECTION, points=batch, wait=True)
                batch = []
        if batch:
            client.upsert(collection_name=COLLECTION, points=batch, wait=True)
        report["ingest_seconds"] = round(time.perf_counter() - ingest_started, 3)
        report["point_count_observed"] = client.count(COLLECTION, exact=True).count
        if target_vector is None or report["point_count_observed"] != points_count:
            raise RuntimeError("Synthetic point count or target vector mismatch")

        scope = models.Filter(must=[
            models.FieldCondition(key="tenant_id", match=models.MatchValue(value="tenant_042")),
            models.FieldCondition(key="customer_id", match=models.MatchValue(value="customer_00")),
        ])

        def query_once(_):
            started = time.perf_counter()
            response = client.query_points(
                collection_name=COLLECTION,
                query=target_vector,
                query_filter=scope,
                limit=5,
                with_payload=True,
            )
            elapsed_ms = (time.perf_counter() - started) * 1000
            matches = response.points
            scoped = bool(matches) and all(
                point.payload.get("tenant_id") == "tenant_042"
                and point.payload.get("customer_id") == "customer_00"
                for point in matches
            )
            exact_top = bool(matches) and matches[0].id == 43
            return elapsed_ms, scoped and exact_top

        for concurrency in (1, workers):
            started = time.perf_counter()
            with ThreadPoolExecutor(max_workers=concurrency) as pool:
                samples = list(pool.map(query_once, range(queries)))
            wall_seconds = time.perf_counter() - started
            latencies = [sample[0] for sample in samples]
            report["profiles"][str(concurrency)] = {
                "queries": queries,
                "scope_and_top1_correct": sum(sample[1] for sample in samples),
                "p50_ms": percentile(latencies, 0.5),
                "p95_ms": percentile(latencies, 0.95),
                "throughput_qps": round(queries / wall_seconds, 2),
            }
        report["passed"] = all(
            item["scope_and_top1_correct"] == queries
            for item in report["profiles"].values()
        )
    except Exception as error:
        report["error"] = f"{type(error).__name__}: {error}"
    finally:
        if created:
            client.delete_collection(COLLECTION)
            report["collection_removed"] = not client.collection_exists(COLLECTION)
        client.close()
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--points", type=int, default=2000)
    parser.add_argument("--queries", type=int, default=100)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if not 100 <= args.points <= 20000 or not 10 <= args.queries <= 1000 or not 2 <= args.workers <= 16:
        parser.error("Require 100-20000 points, 10-1000 queries, and 2-16 workers")
    print(f"Synthetic benchmark: {args.points} x {DIMENSIONS} vectors, {TENANTS} tenants, {args.queries} queries/profile")
    if not args.apply:
        return
    report = run(args.points, args.queries, args.workers)
    output = ROOT / "eval_agent" / "results" / f"qdrant_benchmark_{args.points}_{datetime.now(timezone.utc):%Y%m%d_%H%M%SZ}.json"
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(output)
    for concurrency, metrics in report["profiles"].items():
        print(f"workers={concurrency}: p95={metrics['p95_ms']} ms, throughput={metrics['throughput_qps']} qps, scope={metrics['scope_and_top1_correct']}/{args.queries}")
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
