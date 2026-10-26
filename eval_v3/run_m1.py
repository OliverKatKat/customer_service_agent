"""Evaluate M1 memory boundaries against the isolated eval v3 stores only."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from qdrant_client import QdrantClient, models

from seed import checked_url
from contract import load_dataset

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent))
VECTOR_SIZE = 1024


def require_eval_environment() -> None:
    eval_url = os.environ.get("EVAL_V3_DATABASE_URL", "")
    checked_url(eval_url)
    if os.environ.get("DATABASE_URL") != eval_url:
        raise RuntimeError("DATABASE_URL must equal the guarded eval v3 URL")
    if os.environ.get("QDRANT_URL") != "http://127.0.0.1:56333":
        raise RuntimeError("QDRANT_URL must target isolated eval v3 Qdrant")
    if os.environ.get("REDIS_URL") != "redis://127.0.0.1:56379/0":
        raise RuntimeError("REDIS_URL must target isolated eval v3 Redis")


def seed_memory(fixture: dict, extract_local_preferences, extract_and_store_memory) -> int:
    ticket_owners = {
        ticket["id"]: (ticket["tenant_id"], ticket["customer_id"])
        for ticket in fixture["tickets"]
    }
    stored = 0
    for fact in fixture["memory_facts_for_m1"]:
        if ticket_owners.get(fact["source_ticket_id"]) != (
            fact["tenant_id"], fact["customer_id"]
        ):
            raise RuntimeError("Fixture memory source ticket ownership is invalid")
        extracted = extract_local_preferences([
            {"type": "human", "content": fact["utterance"]}
        ])
        if extracted != [("contact_preference", fact["expected_fact"])]:
            raise AssertionError(f"Fixture utterance did not extract as declared: {fact['source_ticket_id']}")
        result = extract_and_store_memory(
            fact["tenant_id"], fact["customer_id"],
            str(fact["source_ticket_id"]), extracted,
        )
        if result["stored"] != 1:
            raise AssertionError(f"Fixture preference was not stored: {fact['source_ticket_id']}")
        stored += 1
    return stored


async def run() -> dict:
    require_eval_environment()
    from app.agent.customer_memory import (
        extract_local_preferences,
        read_scoped_preferences,
    )
    from app.agent import customer_memory as memory_module
    from app.agent.tools import search_user_memory
    from app.workers.tasks import extract_and_store_memory

    fixture = json.loads((ROOT / "fixtures.json").read_text(encoding="utf-8-sig"))
    all_cases = load_dataset(ROOT / "cases.json")
    declared_cases = [case for case in all_cases["cases"] if case["milestone"] == "M1"]
    cases = [case for case in declared_cases if case["surface"] == "memory"]
    supplemental = {
        case["case_id"]: case["expect"]
        for case in declared_cases if case.get("action") == "supplemental"
    }
    users = {user["username"]: user for user in fixture["users"]}
    client = QdrantClient(url=os.environ["QDRANT_URL"], trust_env=False)
    collection = f"user_longterm_memory_eval_run_{uuid.uuid4().hex[:12]}"
    original_collection = memory_module.COLLECTION
    collection_created = False
    results = []
    local_input = [
        {"type": "human", "content": "以后请通过邮件联系我，请简短回复。"},
        {"type": "ai", "content": "我偏好电话联系。"},
    ]
    local_facts = extract_local_preferences(local_input)
    try:
        client.create_collection(
            collection_name=collection,
            vectors_config=models.VectorParams(
                size=VECTOR_SIZE, distance=models.Distance.COSINE,
            ),
        )
        collection_created = True
        memory_module.COLLECTION = collection
        seeded = seed_memory(fixture, extract_local_preferences, extract_and_store_memory)
        if seeded != 3:
            raise RuntimeError(f"Expected 3 memory fixture facts, got {seeded}")

        for case in cases:
            context = case.get("trusted_context")
            if context is None:
                actor = users[case["actor"]]
                context = {
                    "tenant_id": actor["tenant_id"],
                    "customer_id": actor["username"],
                    "ticket_id": str(case["target_ticket_id"]),
                }
            raw = await search_user_memory.ainvoke(
                {"query": case["query"]},
                config={"configurable": context},
            )
            result = json.loads(raw)
            joined = json.dumps(result, ensure_ascii=False)
            expected = case["expect"]
            unknown_expectations = set(expected) - {
                "contains_facts", "excludes_facts", "result_count"
            }
            if unknown_expectations:
                raise ValueError(
                    f"Unimplemented M1 expectations for {case['case_id']}: "
                    f"{sorted(unknown_expectations)}"
                )
            checks = [
                fact in joined
                for fact in expected.get("contains_facts", [])
            ] + [
                fact not in joined
                for fact in expected.get("excludes_facts", [])
            ]
            if "result_count" in expected:
                checks.append(len(result.get("memories", [])) == expected["result_count"])
            if not checks:
                raise ValueError(f"M1 case has no assertions: {case['case_id']}")
            results.append({
                "case_id": case["case_id"],
                "passed": all(checks),
                "status": result.get("status"),
                "returned_count": len(result.get("memories", [])),
                "checks": checks,
            })

        facts = local_facts
        expected = supplemental["M1-LOCAL-EXTRACTION-AND-WRITE"]
        local_only = [list(fact) for fact in facts] == expected["extracted_facts"]
        written = extract_and_store_memory(
            "merchant_eval_alpha", "eval_alpha_alice", "930302", facts
        )
        scoped = read_scoped_preferences(
            "merchant_eval_alpha", "eval_alpha_alice"
        )
        other = read_scoped_preferences(
            "merchant_eval_alpha", "eval_alpha_bob"
        )
        results.append({
            "case_id": "M1-LOCAL-EXTRACTION-AND-WRITE",
            "passed": local_only and written["stored"] == expected["stored_count"]
            and any(item["text"] == expected["own_contains"] for item in scoped)
            and not any(item["text"] == expected["other_excludes"] for item in other),
            "stored": written["stored"],
            "fact_count": len(facts),
        })
        # Prove that legacy documents and falsely attributed source tickets
        # cannot be returned even if present in the temporary collection.
        legacy_canary = supplemental["M1-LEGACY-UNSCOPED-QUARANTINE"]["forbidden_value"]
        wrong_source_canary = supplemental["M1-SOURCE-TICKET-OWNERSHIP"]["forbidden_value"]
        point_ids = [
            str(uuid.uuid5(uuid.NAMESPACE_URL, "eval-v3|legacy-unscoped")),
            str(uuid.uuid5(uuid.NAMESPACE_URL, "eval-v3|wrong-source")),
        ]
        vector = [0.0] * VECTOR_SIZE
        vector[0] = 1.0
        client.upsert(
            collection_name=collection,
            points=[
                models.PointStruct(
                    id=point_ids[0],
                    vector=vector,
                    payload={
                        "page_content": legacy_canary,
                        "metadata": {
                            "session_id": "legacy-ticket-only",
                            "type": "user_episodic_memory",
                        },
                    },
                ),
                models.PointStruct(
                    id=point_ids[1],
                    vector=vector,
                    payload={
                        "page_content": wrong_source_canary,
                        "metadata": {
                            "tenant_id": "merchant_eval_alpha",
                            "customer_id": "eval_alpha_alice",
                            "source_ticket_id": "930303",
                            "type": "user_episodic_memory",
                        },
                    },
                ),
            ],
            wait=True,
        )
        try:
            raw = await search_user_memory.ainvoke(
                {"query": "请找我的联系偏好"},
                config={
                    "configurable": {
                        "tenant_id": "merchant_eval_alpha",
                        "customer_id": "eval_alpha_alice",
                        "ticket_id": "930302",
                    }
                },
            )
            joined = str(raw)
            results.append({
                "case_id": "M1-LEGACY-UNSCOPED-QUARANTINE",
                "passed": legacy_canary not in joined,
            })
            results.append({
                "case_id": "M1-SOURCE-TICKET-OWNERSHIP",
                "passed": wrong_source_canary not in joined,
            })
        finally:
            client.delete(
                collection_name=collection,
                points_selector=models.PointIdsList(points=point_ids),
                wait=True,
            )

        error_type = None
        try:
            extract_and_store_memory(
                "merchant_eval_alpha", "eval_alpha_bob", "930301", facts
            )
        except Exception as exc:
            error_type = type(exc).__name__
        results.append({
            "case_id": "M1-WRITE-WRONG-CUSTOMER",
            "passed": error_type == supplemental["M1-WRITE-WRONG-CUSTOMER"]["error_type"],
            "observed_error_type": error_type,
        })
        declared_ids = {case["case_id"] for case in declared_cases}
        actual_ids = [item["case_id"] for item in results]
        if len(actual_ids) != len(set(actual_ids)):
            raise ValueError("M1 runner emitted a duplicate case_id")
        for missing_id in sorted(declared_ids - set(actual_ids)):
            results.append({
                "case_id": missing_id, "passed": False,
                "reason": "Runner did not execute declared case",
            })
        if set(actual_ids) - declared_ids:
            raise ValueError(f"Undeclared M1 results: {sorted(set(actual_ids) - declared_ids)}")
        return {
            "dataset_version": all_cases["dataset_version"],
            "milestone": "M1",
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "environment": "isolated eval v3",
            "evaluation_type": "boundary_regression",
            "seeded_fixture_facts": seeded,
            "declared_case_count": len(declared_cases),
            "supplemental_check_count": len(supplemental),
            "cases": results,
            "passed": sum(bool(item["passed"]) for item in results),
            "total": len(results),
        }
    finally:
        try:
            memory_module.COLLECTION = original_collection
            if collection_created:
                client.delete_collection(collection_name=collection)
        finally:
            client.close()


if __name__ == "__main__":
    report = asyncio.run(run())
    output_dir = ROOT / "results"
    output_dir.mkdir(exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%SZ")
    path = output_dir / f"m1_{timestamp}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"passed": report["passed"], "total": report["total"], "report": str(path)}, ensure_ascii=False))
    if report["passed"] != report["total"]:
        raise SystemExit(1)
