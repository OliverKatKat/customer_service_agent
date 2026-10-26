"""Run selected natural-language Agent cases against the isolated local API."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from celery import Celery
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
from eval_v3.seed import checked_url  # noqa: E402
from app.agent.reply_guard import claims_human_handoff, claims_review_submission, claims_refund_completion, discloses_pending_review  # noqa: E402

ROOT = Path(__file__).resolve().parent
DATASET_PATH = ROOT / "cases.json"
FIXTURE_PATH = ROOT / "fixtures.json"
BASE_URL = "http://127.0.0.1:58080"
REDIS_URL = "redis://127.0.0.1:56379/0"
QDRANT_URL = "http://127.0.0.1:56333"
TERMINAL_STATES = {"SUCCESS", "FAILURE", "REVOKED"}


def load_cases() -> tuple[dict, dict]:
    dataset = json.loads(DATASET_PATH.read_text(encoding="utf-8-sig"))
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8-sig"))
    if dataset.get("dataset_version") != "1.5.0" or fixture.get("fixture_version") != "1.0.0":
        raise ValueError("Unsupported Agent E2E dataset or fixture version")
    users = {user["username"] for user in fixture["users"]}
    orders = {order["order_id"] for order in fixture["orders"]}
    seen = set()
    for case in dataset["cases"]:
        case_id = case["case_id"]
        if case_id in seen or case["actor"] not in users:
            raise ValueError(f"Duplicate case or unknown actor: {case_id}")
        seen.add(case_id)
        if not isinstance(case["topic"], str) or not 1 <= len(case["topic"]) <= 100:
            raise ValueError(f"Invalid API topic length: {case_id}")
        if case["order_id"] not in orders | {None, "E3B-ALICE-PAID"}:
            raise ValueError(f"Unknown synthetic order: {case_id}")
        expected = case["expect"]
        allowed = {
            "submit_status", "task_status", "ticket_delta", "order_status",
            "review_status", "ticket_status", "postflight_actions", "direct_tool_contains",
            "reply_any", "reply_forbidden",
        }
        if set(expected) - allowed:
            raise ValueError(f"Unknown expectations in {case_id}")
        required = {"submit_status", "order_status"} if case["order_id"] else {"submit_status"}
        if required - set(expected):
            raise ValueError(f"Missing expectations in {case_id}")
        if expected["submit_status"] == 202 and "task_status" not in expected:
            raise ValueError(f"Missing task outcome in {case_id}")
        if expected["submit_status"] == 404 and expected.get("ticket_delta") != 0:
            raise ValueError(f"Rejected case needs zero ticket delta: {case_id}")
    return dataset, fixture


def require_eval_environment() -> str:
    database_url = os.environ.get("EVAL_V3_DATABASE_URL", "")
    checked_url(database_url)
    if os.environ.get("DATABASE_URL") != database_url:
        raise RuntimeError("DATABASE_URL must equal guarded eval v3 URL")
    if os.environ.get("REDIS_URL") != REDIS_URL:
        raise RuntimeError("REDIS_URL must target isolated eval v3 Redis")
    if os.environ.get("QDRANT_URL") != QDRANT_URL:
        raise RuntimeError("QDRANT_URL must target isolated eval v3 Qdrant")
    if len(os.environ.get("EVAL_V3_PASSWORD", "")) < 12:
        raise RuntimeError("EVAL_V3_PASSWORD is required")
    return database_url


async def count_tickets(engine, tenant_id: str, customer_id: str) -> int:
    async with engine.connect() as conn:
        return int(await conn.scalar(text(
            "SELECT count(*) FROM tickets WHERE tenant_id=:tenant AND customer_id=:customer"
        ), {"tenant": tenant_id, "customer": customer_id}))


async def read_state(engine, ticket_id: int | None, order_id: str | None) -> dict:
    observed = {}
    async with engine.connect() as conn:
        if order_id:
            observed["order_status"] = await conn.scalar(text(
                "SELECT status FROM orders WHERE order_id=:order_id"
            ), {"order_id": order_id})
        if ticket_id is not None:
            observed["ticket_status"] = await conn.scalar(text(
                "SELECT status FROM tickets WHERE id=:ticket_id"
            ), {"ticket_id": ticket_id})
            observed["review_status"] = await conn.scalar(text(
                "SELECT status FROM review_requests WHERE ticket_id=:ticket_id"
            ), {"ticket_id": ticket_id})
    return observed


async def await_task(celery_app: Celery, task_id: str, timeout_seconds: int) -> dict:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        task = celery_app.AsyncResult(task_id)
        if task.state in TERMINAL_STATES:
            payload = task.result
            if isinstance(payload, dict):
                return {
                    "celery_state": task.state,
                    "task_status": payload.get("status"),
                    "reply": str(payload.get("output", "")),
                    "postflight_actions": payload.get("postflight_actions", []),
                    "memory_write_task_id": payload.get("memory_write_task_id"),
                }
            return {
                "celery_state": task.state,
                "task_status": task.state.lower(),
                "reply": str(payload)[:1000],
            }
        await asyncio.sleep(2)
    return {"celery_state": "TIMEOUT", "task_status": "timeout", "reply": ""}


def score(expected: dict, observed: dict) -> dict[str, bool]:
    checks = {}
    for key in (
        "submit_status", "task_status", "ticket_delta", "order_status",
        "review_status", "ticket_status", "postflight_actions",
    ):
        if key in expected:
            checks[key] = observed.get(key) == expected[key]
    if "reply_any" in expected:
        checks["reply_any"] = any(term in observed.get("reply", "") for term in expected["reply_any"])
    if "direct_tool_contains" in expected:
        checks["direct_tool_contains"] = (
            expected["direct_tool_contains"] in observed.get("direct_tool_result", "")
        )
    if "reply_forbidden" in expected:
        checks["reply_forbidden"] = not any(
            term in observed.get("reply", "") for term in expected["reply_forbidden"]
        )
    if observed.get("submit_status") == 202:
        checks["task_api_status"] = observed.get("task_api_status") == (
            "completed" if observed.get("task_status") == "success" else "failed"
        )
        checks["review_claim_grounded"] = (
            not claims_review_submission(observed.get("reply", ""))
            or observed.get("review_status") in {"pending", "approved", "rejected"}
        )
        checks["handoff_claim_grounded"] = (
            not claims_human_handoff(observed.get("reply", ""))
            or observed.get("ticket_status") in {"escalated", "resolved"}
        )
    return checks


async def run(selected: list[dict], dataset: dict, fixture: dict, timeout_seconds: int) -> dict:
    database_url = require_eval_environment()
    engine = create_async_engine(database_url, echo=False)
    celery_app = Celery("agent_eval_reader", broker=REDIS_URL, backend=REDIS_URL)
    results = []
    tenant_id = fixture["tenant"]["id"]
    tokens = {}
    try:
        async with httpx.AsyncClient(base_url=BASE_URL, timeout=20, trust_env=False) as client:
            health = await client.get("/healthz")
            if health.status_code != 200:
                raise RuntimeError(f"Local eval API is unhealthy: HTTP {health.status_code}")
            for case in selected:
                started = time.monotonic()
                case_id = case["case_id"]
                actor = case["actor"]
                observed = {"actor": actor, "order_id": case["order_id"], "topic": case["topic"]}
                try:
                    if actor not in tokens:
                        login = await client.post(
                            "/api/v1/auth/login",
                            data={"username": actor, "password": os.environ["EVAL_V3_PASSWORD"]},
                        )
                        if login.status_code != 200:
                            raise RuntimeError(f"Login failed: HTTP {login.status_code}")
                        if login.json()["tenant_id"] != tenant_id:
                            raise RuntimeError("Synthetic actor belongs to wrong tenant")
                        tokens[actor] = login.json()["access_token"]
                    before = await count_tickets(engine, tenant_id, actor)
                    response = await client.post(
                        "/api/v1/submit",
                        json={"topic": case["topic"], "order_id": case["order_id"] or ""},
                        headers={"Authorization": f"Bearer {tokens[actor]}"},
                    )
                    observed["submit_status"] = response.status_code
                    if response.status_code == 429:
                        retry_after = min(int(response.headers.get("Retry-After", "1")), 65)
                        await asyncio.sleep(max(retry_after, 1))
                        response = await client.post(
                            "/api/v1/submit",
                            json={"topic": case["topic"], "order_id": case["order_id"] or ""},
                            headers={"Authorization": f"Bearer {tokens[actor]}"},
                        )
                        observed["submit_status"] = response.status_code
                    ticket_id = None
                    if response.status_code == 202:
                        body = response.json()
                        ticket_id = int(body["ticket_id"])
                        task_id = str(body["task_id"])
                        observed.update({
                            "ticket_id": ticket_id,
                            "task_id": task_id,
                            "request_id": body.get("request_id"),
                        })
                        observed.update(await await_task(celery_app, task_id, timeout_seconds))
                        for _ in range(3):
                            task_response = await client.get(
                                f"/api/v1/task/{task_id}",
                                headers={"Authorization": f"Bearer {tokens[actor]}"},
                            )
                            if task_response.status_code != 429:
                                break
                            await asyncio.sleep(min(int(task_response.headers.get("Retry-After", "1")), 65))
                        observed["task_api_status"] = (
                            task_response.json().get("status")
                            if task_response.status_code == 200 else f"HTTP {task_response.status_code}"
                        )
                    else:
                        observed["submit_detail"] = str(response.text)[:250]
                    observed["ticket_delta"] = (
                        await count_tickets(engine, tenant_id, actor)
                    ) - before
                    if ticket_id is not None and "direct_tool_contains" in case["expect"]:
                        from app.agent.tools import process_large_refund
                        fixture_order = next(
                            order for order in fixture["orders"]
                            if order["order_id"] == case["order_id"]
                        )
                        observed["direct_tool_result"] = await process_large_refund.ainvoke(
                            {"order_id": case["order_id"], "amount": fixture_order["amount"]},
                            config={"configurable": {
                                "tenant_id": tenant_id,
                                "customer_id": actor,
                                "ticket_id": str(ticket_id),
                            }},
                        )
                    observed.update(await read_state(engine, ticket_id, case["order_id"]))
                    checks = score(case["expect"], observed)
                    reply_quality_checks = {}
                    if observed.get("review_status") == "pending":
                        reply_quality_checks = {
                            "pending_review_disclosed": discloses_pending_review(observed.get("reply", "")),
                            "no_refund_completion_claim": not claims_refund_completion(observed.get("reply", "")),
                            "order_not_refunded": observed.get("order_status") in {"paid", "out_of_stock"},
                        }
                    results.append({
                        "case_id": case_id, "expected": case["expect"], "passed": all(checks.values()),
                        "checks": checks, "reply_quality_checks": reply_quality_checks,
                        "observed": observed,
                        "elapsed_seconds": round(time.monotonic() - started, 2),
                    })
                except Exception as exc:
                    results.append({
                        "case_id": case_id, "expected": case["expect"], "passed": False, "observed": observed,
                        "error": f"{type(exc).__name__}: {exc}",
                        "elapsed_seconds": round(time.monotonic() - started, 2),
                    })
    finally:
        await engine.dispose()
    quality_rows = [
        item["reply_quality_checks"] for item in results
        if item.get("reply_quality_checks")
    ]
    reply_quality_metrics = {
        "pending_review_outputs": len(quality_rows),
        "pending_review_disclosed": sum(row.get("pending_review_disclosed") is True for row in quality_rows),
        "no_refund_completion_claim": sum(row.get("no_refund_completion_claim") is True for row in quality_rows),
        "order_not_refunded": sum(row.get("order_not_refunded") is True for row in quality_rows),
        "state_consistent_outputs": sum(all(row.values()) for row in quality_rows),
    }
    return {
        "dataset_version": dataset["dataset_version"],
        "evaluator_version": "1.6.0-reply-consistency",
        "evaluation_type": dataset["evaluation_type"],
        "reply_quality_metrics": reply_quality_metrics,
        "run_at_utc": datetime.now(timezone.utc).isoformat(),
        "selected_count": len(selected),
        "dataset_count": len(dataset["cases"]),
        "passed": sum(item["passed"] for item in results),
        "total": len(results),
        "scope": ("Real local API and accepted-submit worker path; external LLM configured; synthetic eval tenant only" if any(item.get("observed", {}).get("submit_status") == 202 for item in results) else "Real local API rejection before worker/model; synthetic eval tenant only"),
        "results": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Submit selected cases")
    parser.add_argument("--allow-external-llm", action="store_true")
    parser.add_argument("--case", action="append", default=[], help="Case ID; repeatable")
    parser.add_argument("--max-cases", type=int, default=1)
    parser.add_argument("--timeout-seconds", type=int, default=180)
    args = parser.parse_args()
    dataset, fixture = load_cases()
    if args.max_cases < 1 or args.timeout_seconds < 10:
        raise ValueError("max-cases and timeout-seconds must be positive")
    known = {case["case_id"] for case in dataset["cases"]}
    if set(args.case) - known:
        raise ValueError(f"Unknown case IDs: {sorted(set(args.case) - known)}")
    selected = [
        case for case in dataset["cases"] if not args.case or case["case_id"] in args.case
    ][:args.max_cases]
    print(f"Agent E2E dataset {dataset['dataset_version']}: selected {len(selected)}/{len(dataset['cases'])} cases")
    for case in selected:
        print(f"  {case['case_id']}: {case['order_id'] or '<unbound>'}")
    if not args.apply:
        print("Preview only. Pass --apply after starting local API and worker.")
        return
    if any(case["expect"]["submit_status"] == 202 for case in selected):
        if not args.allow_external_llm:
            raise ValueError("Selected cases need --allow-external-llm")
    report = asyncio.run(run(selected, dataset, fixture, args.timeout_seconds))
    output_dir = ROOT / "results"
    output_dir.mkdir(exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%fZ")
    path = output_dir / f"agent_{timestamp}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Agent E2E: {report['passed']}/{report['total']} passed; report: {path}")
    for item in report["results"]:
        if not item["passed"]:
            print(f"FAIL {item['case_id']}: {item.get('error') or item.get('checks')}")
    if report["passed"] != report["total"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

