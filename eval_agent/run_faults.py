"""Run one deterministic M4 fault case through the isolated Celery worker."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx
import redis
from celery import Celery
from sqlalchemy import create_engine, text

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.core.task_events import task_event_key, task_owner_identity, task_owner_key  # noqa: E402
from app.agent.reply_guard import claims_refund_completion, discloses_pending_review  # noqa: E402
from eval_v3.seed import checked_url  # noqa: E402


ROOT = Path(__file__).resolve().parent
DATASET = ROOT / "fault_cases.json"
REDIS_URL = "redis://127.0.0.1:56379/0"
BASE_URL = "http://127.0.0.1:58080"
TENANT_ID = "merchant_eval_agent"


def require_eval_environment():
    raw_url = os.environ.get("EVAL_V3_DATABASE_URL", "")
    url = checked_url(raw_url)
    if os.environ.get("DATABASE_URL") != raw_url:
        raise RuntimeError("DATABASE_URL must be the guarded eval v3 database")
    if os.environ.get("REDIS_URL") != REDIS_URL:
        raise RuntimeError("REDIS_URL must be the isolated eval v3 Redis")
    if os.environ.get("QDRANT_URL") != "http://127.0.0.1:56333":
        raise RuntimeError("QDRANT_URL must be the isolated eval v3 Qdrant")
    if len(os.environ.get("EVAL_V3_PASSWORD", "")) < 12:
        raise RuntimeError("EVAL_V3_PASSWORD is required")
    return url.set(drivername="postgresql+psycopg")


def run_case(case: dict) -> dict:
    engine = create_engine(require_eval_environment(), pool_pre_ping=True)
    redis_client = redis.Redis.from_url(REDIS_URL, decode_responses=True)
    celery = Celery("m4_fault_reader", broker=REDIS_URL, backend=REDIS_URL)
    task_id = str(uuid.uuid4())
    request_id = str(uuid.uuid4())
    actor = case["actor"]
    if actor != "eval_agent_alice":
        raise ValueError("M4 fault worker accepts only the synthetic Alice account")

    try:
        with httpx.Client(base_url=BASE_URL, timeout=20, trust_env=False) as client:
            login = client.post(
                "/api/v1/auth/login",
                data={"username": actor, "password": os.environ["EVAL_V3_PASSWORD"]},
            )
            login.raise_for_status()
            token = login.json()["access_token"]
            if login.json()["tenant_id"] != TENANT_ID:
                raise RuntimeError("Synthetic user belongs to the wrong tenant")
            headers = {"Authorization": f"Bearer {token}"}

            topic = f"[{case['case_id']}] {case['topic']}"
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            with engine.begin() as conn:
                user_exists = conn.scalar(text(
                    "SELECT 1 FROM users WHERE tenant_id=:tenant AND username=:actor AND is_active=true"
                ), {"tenant": TENANT_ID, "actor": actor})
                if not user_exists:
                    raise RuntimeError("Synthetic user is missing")
                order_before = None
                if case["order_id"]:
                    order_before = conn.scalar(text(
                        "SELECT status FROM orders WHERE tenant_id=:tenant "
                        "AND customer_id=:actor AND order_id=:order_id"
                    ), {"tenant": TENANT_ID, "actor": actor, "order_id": case["order_id"]})
                    if order_before != "paid":
                        raise RuntimeError("Fault case requires an unchanged paid synthetic order")
                ticket_id = conn.scalar(text(
                    "INSERT INTO tickets "
                    "(tenant_id, customer_id, order_id, title, status, version, created_at, updated_at) "
                    "VALUES (:tenant, :actor, :order_id, :title, 'pending', 1, :now, :now) "
                    "RETURNING id"
                ), {"tenant": TENANT_ID, "actor": actor, "order_id": case["order_id"],
                    "title": topic, "now": now})

            redis_client.set(
                task_owner_key(task_id), task_owner_identity(TENANT_ID, actor), ex=86400
            )
            celery.send_task(
                "process_agent_task",
                task_id=task_id,
                queue="eval_faults",
                kwargs={"topic": topic, "ticket_id": str(ticket_id),
                        "user_id": actor, "tenant_id": TENANT_ID, "memory_facts": []},
                headers={"x-request-id": request_id},
            )

            result = celery.AsyncResult(task_id)
            deadline = time.monotonic() + 60
            while result.state not in {"SUCCESS", "FAILURE", "REVOKED"}:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"Fault worker did not finish task {task_id}")
                time.sleep(0.5)
            raw = result.result if isinstance(result.result, dict) else {}
            for _ in range(4):
                task_response = client.get(f"/api/v1/task/{task_id}", headers=headers)
                if task_response.status_code != 429:
                    break
                retry_after = min(float(task_response.headers.get("Retry-After", "1")), 65)
                time.sleep(max(retry_after, 1))
            task_response.raise_for_status()
            task_api = task_response.json()
            events = [
                {"event": fields["event"], **json.loads(fields["payload"])}
                for _, fields in redis_client.xrange(task_event_key(task_id))
            ]

            with engine.connect() as conn:
                ticket_status = conn.scalar(text(
                    "SELECT status FROM tickets WHERE id=:id AND tenant_id=:tenant"
                ), {"id": ticket_id, "tenant": TENANT_ID})
                review_count = conn.scalar(text(
                    "SELECT count(*) FROM review_requests WHERE ticket_id=:id AND tenant_id=:tenant"
                ), {"id": ticket_id, "tenant": TENANT_ID})
                review_status = conn.scalar(text(
                    "SELECT status FROM review_requests WHERE ticket_id=:id AND tenant_id=:tenant"
                ), {"id": ticket_id, "tenant": TENANT_ID})
                order_after = None
                if case["order_id"]:
                    order_after = conn.scalar(text(
                        "SELECT status FROM orders WHERE tenant_id=:tenant "
                        "AND customer_id=:actor AND order_id=:order_id"
                    ), {"tenant": TENANT_ID, "actor": actor, "order_id": case["order_id"]})

            if case.get("setup_review"):
                reply = str(raw.get("output", ""))
                checks = {
                    "celery_finished": result.state == "SUCCESS",
                    "worker_success": raw.get("status") == "success",
                    "expected_guard": case["expected_action"] in raw.get("postflight_actions", []),
                    "task_api_completed": task_api.get("status") == "completed",
                    "completed_event": any(event.get("event") == "completed" for event in events),
                    "ticket_escalated": ticket_status == "escalated",
                    "review_pending": review_status == "pending",
                    "order_unchanged": order_after == order_before,
                    "false_claim_removed": not claims_refund_completion(reply),
                    "pending_review_disclosed": discloses_pending_review(reply),
                }
            else:
                checks = {
                    "celery_finished": result.state == "SUCCESS",
                    "worker_error": raw.get("status") == "error",
                    "expected_guard": case["expected_action"] in raw.get("postflight_actions", []),
                    "task_api_failed": task_api.get("status") == "failed",
                    "failed_event": any(event.get("event") == "failed" for event in events),
                    "ticket_unchanged": ticket_status == "pending",
                    "review_absent": review_count == 0,
                    "order_unchanged": order_after == order_before,
                    "false_claim_removed": case["forced_reply"] not in str(raw.get("output", "")),
                }
            return {
                "dataset_version": "m4-fault-1",
                "execution_scope": "isolated eval worker with deterministic reply; no external LLM",
                "case_id": case["case_id"], "actor": actor,
                "order_id": case["order_id"], "input_topic": topic,
                "expected_action": case["expected_action"],
                "ticket_id": ticket_id,
                "task_id": task_id, "request_id": request_id,
                "forced_reply": case["forced_reply"],
                "worker": {"celery_state": result.state, "result": raw},
                "task_api": task_api, "events": events,
                "database": {"ticket_status": ticket_status, "review_count": review_count, "review_status": review_status,
                             "order_before": order_before, "order_after": order_after},
                "checks": checks, "passed": all(checks.values()),
            }
    finally:
        redis_client.close()
        engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    dataset = json.loads(DATASET.read_text(encoding="utf-8-sig"))
    if dataset.get("dataset_version") != "m4-fault-1":
        raise ValueError("Unsupported M4 fault dataset")
    selected = [case for case in dataset["cases"] if case["case_id"] == args.case]
    if len(selected) != 1:
        raise ValueError(f"Unknown or duplicate M4 case: {args.case}")
    print(f"M4 fault preview: {args.case}; actor={selected[0]['actor']}; queue=eval_faults")
    if not args.apply:
        print("Preview only. Pass --apply to create a synthetic ticket and dispatch the task.")
        return
    report = run_case(selected[0])
    output_dir = ROOT / "results"
    output_dir.mkdir(exist_ok=True)
    path = output_dir / (
        f"m4_fault_{args.case.lower()}_"
        f"{datetime.now(timezone.utc):%Y%m%d_%H%M%S_%fZ}.json"
    )
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"M4 fault {'PASS' if report['passed'] else 'FAIL'}: {path}")
    if not report["passed"]:
        print(json.dumps(report["checks"], ensure_ascii=False, indent=2))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
