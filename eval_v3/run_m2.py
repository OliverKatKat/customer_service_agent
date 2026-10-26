"""M2 review workflow evaluation against the guarded isolated eval v3 stores.

Database changes are enclosed in outer transactions and rolled back. Celery and
external LLM calls are replaced in-process; the API, refund tool, and review
outbox code are exercised with the real fixture schema.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker

from seed import checked_url
from contract import load_dataset

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent))
FIXTURE = json.loads((ROOT / "fixtures.json").read_text(encoding="utf-8-sig"))
DATASET = load_dataset(ROOT / "cases.json")
DECLARED_CASES = [
    case for case in DATASET["cases"] if case["milestone"] in {"M2", "M3"}
]
CASES = [
    case for case in DECLARED_CASES if case["surface"] == "review"
]
SUPPLEMENTAL = {
    case["case_id"]: case["expect"]
    for case in DECLARED_CASES if case.get("action") == "supplemental"
}
TICKET_ID = 930305
ORDER_ID = "E3A-ALICE-PAID-LARGE"
TENANT_ID = "merchant_eval_alpha"
CUSTOMER_ID = "eval_alpha_alice"
ADMIN_CODE = "eval-v3-review-code-2026"


def require_eval_environment() -> None:
    url = os.environ.get("EVAL_V3_DATABASE_URL", "")
    checked_url(url)
    if os.environ.get("DATABASE_URL") != url:
        raise RuntimeError("DATABASE_URL must equal guarded eval v3 URL")
    if os.environ.get("REDIS_URL") != "redis://127.0.0.1:56379/0":
        raise RuntimeError("REDIS_URL must target isolated eval v3 Redis")
    if os.environ.get("QDRANT_URL") != "http://127.0.0.1:56333":
        raise RuntimeError("QDRANT_URL must target isolated eval v3 Qdrant")
    if len(os.environ.get("EVAL_V3_PASSWORD", "")) < 12:
        raise RuntimeError("EVAL_V3_PASSWORD is required")


async def run() -> dict:
    require_eval_environment()
    from app.main import app
    from app.core.db import engine, get_db
    from app.core.config import settings
    from app.api.v1 import endpoints
    from app.agent import tools as refund_module
    from app.models.review import ReviewRequest, ReviewAuditEvent, NotificationOutbox

    results: list[dict] = []
    dispatch_ids: list[int] = []
    previous_code = settings.ADMIN_APPROVAL_CODE
    previous_dispatch = endpoints.dispatch_review_outbox.apply_async
    previous_tool_session = refund_module.SyncSessionLocal
    settings.ADMIN_APPROVAL_CODE = ADMIN_CODE
    endpoints.dispatch_review_outbox.apply_async = (
        lambda *args, **kwargs: dispatch_ids.append(kwargs["kwargs"]["outbox_id"])
        or SimpleNamespace(id=str(dispatch_ids[-1]))
    )

    async with engine.connect() as connection:
        outer = await connection.begin()
        try:
            for table in ("users", "orders", "tickets"):
                ids = [item["id"] for item in FIXTURE[table]]
                actual = await connection.scalar(text(
                    f"SELECT count(*) FROM {table} WHERE id = ANY(:ids)"
                ), {"ids": ids})
                if actual != len(ids):
                    raise RuntimeError(f"Eval fixture rows missing: {table}={actual}/{len(ids)}")
            if await connection.scalar(text(
                "SELECT count(*) FROM review_requests WHERE ticket_id=:id"
            ), {"id": TICKET_ID}):
                raise RuntimeError("Fixture ticket 930305 must have no review before M2 evaluation")

            async def eval_db():
                async with AsyncSession(
                    bind=connection, expire_on_commit=False,
                    join_transaction_mode="create_savepoint",
                ) as session:
                    yield session

            app.dependency_overrides[get_db] = eval_db
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://eval-v3.local"
            ) as client:
                tokens = {}

                async def auth(username: str) -> dict:
                    if username not in tokens:
                        response = await client.post(
                            "/api/v1/auth/login",
                            data={"username": username, "password": os.environ["EVAL_V3_PASSWORD"]},
                        )
                        assert response.status_code == 200, response.text[:200]
                        tokens[username] = response.json()["access_token"]
                    return {"Authorization": f"Bearer {tokens[username]}"}

                async def setup_review() -> int:
                    async with AsyncSession(
                        bind=connection, expire_on_commit=False,
                        join_transaction_mode="create_savepoint",
                    ) as db:
                        review = ReviewRequest(
                            tenant_id=TENANT_ID, ticket_id=TICKET_ID,
                            customer_id=CUSTOMER_ID, order_id=ORDER_ID,
                            amount=1599, reason="隔离评估申请", status="pending",
                        )
                        db.add(review)
                        await db.flush()
                        db.add(ReviewAuditEvent(
                            review_id=review.id, tenant_id=TENANT_ID,
                            actor_id=CUSTOMER_ID, action="requested",
                        ))
                        db.add(NotificationOutbox(
                            review_id=review.id, tenant_id=TENANT_ID,
                            event_key=f"review:{review.id}:requested",
                            event_type="review_requested",
                            payload={"review_id": review.id, "ticket_id": TICKET_ID},
                            status="pending", attempts=0,
                        ))
                        await db.execute(
                            text("UPDATE tickets SET status='escalated' WHERE id=:id"),
                            {"id": TICKET_ID},
                        )
                        await db.commit()
                        return review.id

                async def status_snapshot(review_id: int) -> tuple[str, str, int]:
                    row = (await connection.execute(text(
                        "SELECT r.status, o.status AS order_status, r.version "
                        "FROM review_requests r JOIN orders o ON "
                        "o.tenant_id=r.tenant_id AND o.order_id=r.order_id "
                        "WHERE r.id=:id"
                    ), {"id": review_id})).one()
                    return row[0], row[1], row[2]

                for case in CASES:
                    if case["setup"]["review_for_ticket_id"] != TICKET_ID:
                        raise ValueError(
                            f"Unsupported review fixture in {case['case_id']}"
                        )
                    savepoint = await connection.begin_nested()
                    start_dispatch = len(dispatch_ids)
                    try:
                        review_id = await setup_review()
                        expected = case["expect"]
                        actor = case["actor"]
                        headers = await auth(actor)
                        if case["action"] == "approve":
                            response = await client.post(
                                f"/api/v1/reviews/{review_id}/approve",
                                json={"approval_code": ADMIN_CODE, "expected_version": 1},
                                headers=headers,
                            )
                            assert response.status_code == expected["http_status"], response.text
                            review_state, order_state, _ = await status_snapshot(review_id)
                            assert review_state == expected["review_state"]
                            assert order_state == expected["order_status"]
                            assert len(dispatch_ids) - start_dispatch == expected["dispatch_delta"]
                        elif case["action"] == "approve_twice":
                            first = await client.post(
                                f"/api/v1/reviews/{review_id}/approve",
                                json={"approval_code": ADMIN_CODE, "expected_version": 1},
                                headers=headers,
                            )
                            second = await client.post(
                                f"/api/v1/reviews/{review_id}/approve",
                                json={"approval_code": ADMIN_CODE, "expected_version": 1},
                                headers=headers,
                            )
                            assert first.status_code == 202, first.text
                            assert second.status_code == 409, second.text
                            assert len(dispatch_ids) - start_dispatch == expected["dispatch_count"]
                            decisions = await connection.scalar(text(
                                "SELECT count(*) FROM review_audit_events "
                                "WHERE review_id=:id AND action='approved' AND actor_id=:actor"
                            ), {"id": review_id, "actor": actor})
                            assert decisions == expected["effective_decisions"]
                            if expected.get("audit_has_actor"):
                                audit_actor = await connection.scalar(text(
                                    "SELECT actor_id FROM review_audit_events "
                                    "WHERE review_id=:id AND action='approved'"
                                ), {"id": review_id})
                                assert audit_actor == actor
                            review_state, order_state, version = await status_snapshot(review_id)
                            assert (review_state, order_state, version) == ("approved", "paid", 2)
                        elif case["action"] == "reject":
                            response = await client.post(
                                f"/api/v1/reviews/{review_id}/reject",
                                json={
                                    "approval_code": ADMIN_CODE,
                                    "expected_version": 1,
                                    "reason": "订单信息需要人工复核",
                                },
                                headers=headers,
                            )
                            assert response.status_code == 200, response.text
                            review_state, order_state, _ = await status_snapshot(review_id)
                            assert review_state == expected["review_state"]
                            assert order_state == expected["order_status"]
                            assert response.json()["task_id"] is None
                            assert len(dispatch_ids) - start_dispatch == expected["dispatch_count"]
                            audit_reason = await connection.scalar(text(
                                "SELECT reason FROM review_audit_events "
                                "WHERE review_id=:id AND action='rejected'"
                            ), {"id": review_id})
                            if expected.get("audit_has_reason"):
                                assert audit_reason == "订单信息需要人工复核"
                            event_type = await connection.scalar(text(
                                "SELECT event_type FROM notification_outbox "
                                "WHERE review_id=:id AND event_type='review_rejected'"
                            ), {"id": review_id})
                            assert event_type == "review_rejected"
                            approved_outbox_count = await connection.scalar(text(
                                "SELECT count(*) FROM notification_outbox "
                                "WHERE review_id=:id AND event_type='review_approved'"
                            ), {"id": review_id})
                            assert approved_outbox_count == expected["approved_outbox_count"]
                        else:
                            raise AssertionError(f"Unhandled action {case['action']}")
                        results.append({"case_id": case["case_id"], "passed": True})
                    except Exception as exc:
                        results.append({
                            "case_id": case["case_id"], "passed": False,
                            "reason": f"{type(exc).__name__}: {exc}",
                        })
                    finally:
                        await savepoint.rollback()

                # Additional API checks share a fresh rollback-only review.
                savepoint = await connection.begin_nested()
                stage = "M2-REVIEW-ROLE-VERSION-CODE"
                try:
                    expected = SUPPLEMENTAL[stage]
                    review_id = await setup_review()
                    user_headers = await auth(CUSTOMER_ID)
                    admin_headers = await auth("eval_alpha_admin")
                    denied = await client.get("/api/v1/reviews", headers=user_headers)
                    assert denied.status_code == expected["user_list_status"]
                    stale = await client.post(
                        f"/api/v1/reviews/{review_id}/approve",
                        json={"approval_code": ADMIN_CODE, "expected_version": 9},
                        headers=admin_headers,
                    )
                    assert stale.status_code == expected["stale_version_status"]
                    wrong_code = await client.post(
                        f"/api/v1/reviews/{review_id}/approve",
                        json={"approval_code": "wrong-code-123456789", "expected_version": 1},
                        headers=admin_headers,
                    )
                    assert wrong_code.status_code == expected["wrong_code_status"]
                    state, _, _ = await status_snapshot(review_id)
                    assert state == expected["review_state"]
                    results.append({"case_id": "M2-REVIEW-ROLE-VERSION-CODE", "passed": True})
                    stage = "M3-REVIEW-PAGE-AND-CUSTOMER-STATUS"
                    expected = SUPPLEMENTAL[stage]
                    own_status = await client.get(
                        f"/api/v1/tickets/{TICKET_ID}/review", headers=user_headers
                    )
                    other_headers = await auth("eval_beta_alice")
                    other_status = await client.get(
                        f"/api/v1/tickets/{TICKET_ID}/review", headers=other_headers
                    )
                    detail = await client.get(
                        f"/api/v1/reviews/{review_id}", headers=admin_headers
                    )
                    page = await client.get("/review")
                    assert own_status.status_code == expected["own_status"]
                    assert own_status.json()["review_status"] == expected["review_status"]
                    assert other_status.status_code == expected["other_status"]
                    assert detail.status_code == expected["detail_status"]
                    assert detail.json()["evidence"]["order_status"] == expected["order_status"]
                    assert page.status_code == expected["page_status"]
                    assert expected["page_contains"] in page.text
                    results.append({"case_id": "M3-REVIEW-PAGE-AND-CUSTOMER-STATUS", "passed": True})
                    stage = "M3-RECOVERY-MANUAL-STATUS"
                    expected = SUPPLEMENTAL[stage]
                    try:
                        await connection.execute(text(
                            "UPDATE review_requests SET status='approved', "
                            "decision_actor_id='eval_alpha_admin', decided_at=now() "
                            "WHERE id=:id"
                        ), {"id": review_id})
                        for _ in range(3):
                            await connection.execute(text(
                                "INSERT INTO review_audit_events "
                                "(review_id, tenant_id, actor_id, action, created_at) "
                                "VALUES (:id, :tenant, 'system:recovery', 'recovery_attempt', now())"
                            ), {"id": review_id, "tenant": TENANT_ID})
                        stalled_detail = await client.get(
                            f"/api/v1/reviews/{review_id}", headers=admin_headers
                        )
                        stalled_customer = await client.get(
                            f"/api/v1/tickets/{TICKET_ID}/review", headers=user_headers
                        )
                        assert stalled_detail.status_code == expected["detail_status"]
                        assert stalled_detail.json()["processing_state"] == expected["processing_state"]
                        assert stalled_detail.json()["recovery_attempts"] == expected["recovery_attempts"]
                        assert stalled_customer.status_code == expected["customer_status"]
                        assert expected["customer_message_contains"] in stalled_customer.json()["message"]
                        results.append({"case_id": "M3-RECOVERY-MANUAL-STATUS", "passed": True})
                    except Exception as exc:
                        results.append({
                            "case_id": "M3-RECOVERY-MANUAL-STATUS", "passed": False,
                            "reason": f"{type(exc).__name__}: {exc}",
                        })
                except Exception as exc:
                    results.append({"case_id": stage, "passed": False,
                                    "reason": f"{type(exc).__name__}: {exc}"})
                finally:
                    await savepoint.rollback()

            # The actual refund tool commits into savepoints on one synchronous
            # connection, so its state transitions can also be rolled back.
            from app.agent.tools import sync_engine, process_large_refund
            with sync_engine.connect() as sync_connection:
                sync_outer = sync_connection.begin()
                refund_module.SyncSessionLocal = sessionmaker(
                    bind=sync_connection, autoflush=False,
                    join_transaction_mode="create_savepoint",
                )
                try:
                    expected = SUPPLEMENTAL["M2-TOOL-DURABLE-PENDING"]
                    context = {"configurable": {
                        "tenant_id": TENANT_ID, "customer_id": CUSTOMER_ID,
                        "ticket_id": str(TICKET_ID),
                    }}
                    start_dispatch = len(dispatch_ids)
                    first = await process_large_refund.ainvoke(
                        {"order_id": ORDER_ID, "amount": 1599}, config=context,
                    )
                    second = await process_large_refund.ainvoke(
                        {"order_id": ORDER_ID, "amount": 1599}, config=context,
                    )
                    count = sync_connection.scalar(text(
                        "SELECT count(*) FROM review_requests WHERE ticket_id=:id"
                    ), {"id": TICKET_ID})
                    ticket_state = sync_connection.scalar(text(
                        "SELECT status FROM tickets WHERE id=:id"
                    ), {"id": TICKET_ID})
                    order_state = sync_connection.scalar(text(
                        "SELECT status FROM orders WHERE order_id=:id"
                    ), {"id": ORDER_ID})
                    assert count == expected["review_count"]
                    assert ticket_state == expected["ticket_status"]
                    assert order_state == expected["order_status"]
                    assert len(dispatch_ids) - start_dispatch == expected["dispatch_delta"]
                    assert expected["first_contains"] in first
                    assert expected["second_contains"] in second
                    results.append({"case_id": "M2-TOOL-DURABLE-PENDING", "passed": True})
                except Exception as exc:
                    results.append({"case_id": "M2-TOOL-DURABLE-PENDING", "passed": False,
                                    "reason": f"{type(exc).__name__}: {exc}"})
                finally:
                    sync_outer.rollback()
            # Simulate a failed approved-event dispatch, then retry the same
            # committed outbox row. No real Agent task is queued.
            from app.workers import tasks as worker_tasks
            from app.models.ticket import Ticket
            old_worker_session = worker_tasks.SyncSessionLocal
            old_worker_dispatch = worker_tasks.process_agent_task.apply_async
            old_worker_redis = worker_tasks.sync_redis_client
            with sync_engine.connect() as sync_connection:
                sync_outer = sync_connection.begin()
                factory = sessionmaker(
                    bind=sync_connection, autoflush=False,
                    join_transaction_mode="create_savepoint",
                )
                worker_tasks.SyncSessionLocal = factory
                sent_agent_tasks = []
                attempts = [0]

                class FakeRedis:
                    def set(self, *_args, **_kwargs):
                        return True

                def flaky_agent_dispatch(*_args, **kwargs):
                    attempts[0] += 1
                    if attempts[0] == 1:
                        raise RuntimeError("eval transient broker failure")
                    sent_agent_tasks.append(kwargs["task_id"])
                    return SimpleNamespace(id=kwargs["task_id"])

                worker_tasks.sync_redis_client = FakeRedis()
                worker_tasks.process_agent_task.apply_async = flaky_agent_dispatch
                stage = "M2-OUTBOX-RETRY"
                try:
                    expected = SUPPLEMENTAL[stage]
                    with factory() as db:
                        ticket = db.get(Ticket, TICKET_ID)
                        ticket.status = "escalated"
                        review = ReviewRequest(
                            tenant_id=TENANT_ID, ticket_id=TICKET_ID,
                            customer_id=CUSTOMER_ID, order_id=ORDER_ID,
                            amount=1599, reason="隔离评估通知重试", status="approved",
                            decision_actor_id="eval_alpha_admin",
                            decided_at=datetime.now(timezone.utc),
                        )
                        db.add(review)
                        db.flush()
                        review_id = review.id
                        event = NotificationOutbox(
                            review_id=review.id, tenant_id=TENANT_ID,
                            event_key=f"review:{review.id}:approved",
                            event_type="review_approved",
                            payload={"review_id": review.id, "ticket_id": TICKET_ID},
                            status="pending", attempts=0,
                        )
                        db.add(event)
                        db.flush()
                        event_id = event.id
                        db.commit()
                    try:
                        worker_tasks.dispatch_review_outbox.run(event_id)
                        raise AssertionError("First dispatch should fail")
                    except RuntimeError as exc:
                        assert expected["first_error_contains"] in str(exc)
                    with factory() as db:
                        assert db.get(NotificationOutbox, event_id).status == expected["failed_status"]
                    response = worker_tasks.dispatch_review_outbox.run(event_id)
                    assert response["status"] == expected["sent_status"]
                    response = worker_tasks.dispatch_review_outbox.run(event_id)
                    assert response["status"] == expected["third_status"]
                    with factory() as db:
                        final_event = db.get(NotificationOutbox, event_id)
                        assert final_event.status == expected["sent_status"]
                        assert final_event.attempts == expected["event_attempts"]
                    assert len(sent_agent_tasks) == expected["agent_dispatch_count"]
                    results.append({"case_id": "M2-OUTBOX-RETRY", "passed": True})
                    stage = "M2-APPROVED-TOOL-ONCE"
                    expected = SUPPLEMENTAL[stage]
                    refund_module.SyncSessionLocal = factory
                    auth_context = {"configurable": {
                        "tenant_id": TENANT_ID, "customer_id": CUSTOMER_ID,
                        "ticket_id": str(TICKET_ID),
                        "authorization": {
                            "source": "admin_ticket_approval",
                            "operation": "large_refund",
                            "actor_id": "eval_alpha_admin",
                            "review_id": review_id,
                            "tenant_id": TENANT_ID,
                            "ticket_id": str(TICKET_ID),
                            "order_id": ORDER_ID,
                            "amount": 1599.0,
                        },
                    }}
                    applied = await process_large_refund.ainvoke(
                        {"order_id": ORDER_ID, "amount": 1599}, config=auth_context,
                    )
                    duplicate = await process_large_refund.ainvoke(
                        {"order_id": ORDER_ID, "amount": 1599}, config=auth_context,
                    )
                    with factory() as db:
                        assert db.get(Ticket, TICKET_ID).status == expected["ticket_status"]
                        assert db.execute(text(
                            "SELECT status FROM orders WHERE order_id=:order_id"
                        ), {"order_id": ORDER_ID}).scalar_one() == expected["order_status"]
                        count = db.execute(text(
                            "SELECT count(*) FROM review_audit_events "
                            "WHERE review_id=:id AND action='refund_applied'"
                        ), {"id": review_id}).scalar_one()
                        assert count == expected["refund_audit_count"]
                    assert expected["first_contains"] in applied
                    assert expected["second_contains"] in duplicate
                    results.append({"case_id": "M2-APPROVED-TOOL-ONCE", "passed": True})
                except Exception as exc:
                    results.append({"case_id": stage, "passed": False,
                                    "reason": f"{type(exc).__name__}: {exc}"})
                finally:
                    refund_module.SyncSessionLocal = previous_tool_session
                    worker_tasks.SyncSessionLocal = old_worker_session
                    worker_tasks.process_agent_task.apply_async = old_worker_dispatch
                    worker_tasks.sync_redis_client = old_worker_redis
                    sync_outer.rollback()
        finally:
            refund_module.SyncSessionLocal = previous_tool_session
            endpoints.dispatch_review_outbox.apply_async = previous_dispatch
            settings.ADMIN_APPROVAL_CODE = previous_code
            app.dependency_overrides.pop(get_db, None)
            await outer.rollback()
            await endpoints.redis_client.aclose()

    expected_ids = {case["case_id"] for case in DECLARED_CASES}
    actual_ids = [item["case_id"] for item in results]
    if len(expected_ids) != len(DECLARED_CASES):
        raise ValueError("Duplicate M2/M3 case_id in cases.json")
    if len(actual_ids) != len(set(actual_ids)):
        raise ValueError("M2/M3 runner emitted a duplicate case_id")
    for missing_id in sorted(expected_ids - set(actual_ids)):
        results.append({
            "case_id": missing_id, "passed": False,
            "reason": "Runner did not execute declared case",
        })
    if set(actual_ids) - expected_ids:
        raise ValueError(f"Undeclared M2/M3 results: {sorted(set(actual_ids) - expected_ids)}")
    return {
        "dataset_version": DATASET["dataset_version"],
        "fixture_version": FIXTURE["fixture_version"],
        "run_at_utc": datetime.now(timezone.utc).isoformat(),
        "scope": "M2/M3 API, review page and refund-tool transitions; Celery and external LLM stubbed",
        "evaluation_type": "boundary_regression",
        "declared_case_count": len(DECLARED_CASES),
        "supplemental_check_count": len(DECLARED_CASES) - len(CASES),
        "total": len(results),
        "passed": sum(item["passed"] for item in results),
        "results": results,
    }


def main() -> None:
    report = asyncio.run(run())
    path = ROOT / "results" / (
        "m2_" + datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%SZ") + ".json"
    )
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"M2 review eval: {report['passed']}/{report['total']} passed")
    for item in report["results"]:
        if not item["passed"]:
            print(f"FAIL {item['case_id']}: {item['reason']}")
    print(f"Report: {path}")
    if report["passed"] != report["total"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
