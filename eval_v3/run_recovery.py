"""Rollback-only approved-refund recovery evaluation for isolated eval v3."""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import select, text
from sqlalchemy.orm import sessionmaker

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from eval_v3.seed import checked_url


def require_eval_environment() -> None:
    url = os.environ.get("EVAL_V3_DATABASE_URL", "")
    checked_url(url)
    if os.environ.get("DATABASE_URL") != url:
        raise RuntimeError("DATABASE_URL must equal guarded eval v3 URL")
    if os.environ.get("REDIS_URL") != "redis://127.0.0.1:56379/0":
        raise RuntimeError("REDIS_URL must target isolated eval v3 Redis")
    if os.environ.get("QDRANT_URL") != "http://127.0.0.1:56333":
        raise RuntimeError("QDRANT_URL must target isolated eval v3 Qdrant")


def main() -> None:
    require_eval_environment()
    from app.agent import tools as refund_module
    from app.models.order import Order
    from app.models.review import NotificationOutbox, ReviewAuditEvent, ReviewRequest
    from app.models.ticket import Ticket
    from app.workers import tasks as worker

    old = (
        worker.SyncSessionLocal,
        refund_module.SyncSessionLocal,
        worker.sync_redis_client,
        worker.recover_approved_refund.apply_async,
        worker.dispatch_review_outbox.apply_async,
        worker.process_agent_task.apply_async,
        refund_module.process_large_refund,
    )
    report = []
    ticket_id = 930305
    tenant_id = "merchant_eval_alpha"
    customer_id = "eval_alpha_alice"
    order_id = "E3A-ALICE-PAID-LARGE"

    class FakeRedis:
        def set(self, *_args, **_kwargs):
            return True

        def delete(self, *_args, **_kwargs):
            return 1

    queued_recoveries = []
    agent_dispatches = []
    worker.sync_redis_client = FakeRedis()
    worker.recover_approved_refund.apply_async = lambda *a, **kw: queued_recoveries.append(kw)
    worker.dispatch_review_outbox.apply_async = lambda *a, **kw: None
    worker.process_agent_task.apply_async = lambda *a, **kw: agent_dispatches.append(kw)

    def setup(factory):
        with factory() as db:
            ticket = db.get(Ticket, ticket_id)
            order = db.execute(select(Order).where(
                Order.tenant_id == tenant_id, Order.order_id == order_id,
                Order.customer_id == customer_id,
            )).scalar_one()
            assert ticket.tenant_id == tenant_id and ticket.customer_id == customer_id
            assert order.status == "paid"
            assert db.execute(select(ReviewRequest.id).where(
                ReviewRequest.ticket_id == ticket_id,
            )).scalar_one_or_none() is None
            ticket.status = "escalated"
            review = ReviewRequest(
                tenant_id=tenant_id, ticket_id=ticket_id,
                customer_id=customer_id, order_id=order_id,
                amount=1599, reason="隔离恢复评估", status="approved",
                decision_actor_id="eval_alpha_admin",
                decided_at=datetime.now(timezone.utc) - timedelta(minutes=3),
            )
            db.add(review)
            db.flush()
            db.add(ReviewAuditEvent(
                review_id=review.id, tenant_id=tenant_id,
                actor_id="eval_alpha_admin", action="approved",
            ))
            event = NotificationOutbox(
                review_id=review.id, tenant_id=tenant_id,
                event_key=f"review:{review.id}:approved",
                event_type="review_approved",
                payload={"review_id": review.id, "ticket_id": ticket_id},
                status="sent", attempts=1,
            )
            db.add(event)
            db.flush()
            ids = review.id, event.id
            db.commit()
            return ids

    try:
        for scenario in ("recover", "limit"):
            with refund_module.sync_engine.connect() as connection:
                outer = connection.begin()
                factory = sessionmaker(
                    bind=connection, autoflush=False,
                    join_transaction_mode="create_savepoint",
                )
                worker.SyncSessionLocal = factory
                refund_module.SyncSessionLocal = factory
                try:
                    review_id, event_id = setup(factory)
                    if scenario == "recover":
                        scheduled = worker.schedule_stalled_approved_reviews.run()
                        assert scheduled["queued"] == 1
                        assert queued_recoveries[-1]["kwargs"]["review_id"] == review_id
                        recovered = worker.recover_approved_refund.run(review_id)
                        assert recovered["status"] == "recovered", recovered
                        repeated = worker.recover_approved_refund.run(review_id)
                        assert repeated["status"] == "not_stalled", repeated
                        with factory() as db:
                            ticket = db.get(Ticket, ticket_id)
                            order = db.execute(select(Order).where(Order.order_id == order_id)).scalar_one()
                            assert ticket.status == "resolved" and order.status == "refunded"
                            counts = dict(db.execute(text(
                                "SELECT action, count(*) FROM review_audit_events "
                                "WHERE review_id=:id GROUP BY action"
                            ), {"id": review_id}).all())
                            assert counts.get("recovery_attempt") == 1
                            assert counts.get("refund_applied") == 1
                            db.get(NotificationOutbox, event_id).status = "failed"
                            db.commit()
                        replay = worker.dispatch_review_outbox.run(event_id)
                        assert replay["status"] == "sent" and not agent_dispatches
                        report.extend([
                            {"case_id": "M2-STALLED-APPROVAL-RECOVERY", "passed": True},
                            {"case_id": "M2-COMPLETED-OUTBOX-REPLAY", "passed": True},
                        ])
                    else:
                        async def failed_tool(*_args, **_kwargs):
                            return "simulated tool failure"

                        refund_module.process_large_refund = SimpleNamespace(ainvoke=failed_tool)
                        for attempt in range(3):
                            result = worker.recover_approved_refund.run(review_id)
                            assert result["status"] == "failed", result
                            if attempt < 2:
                                with factory() as db:
                                    db.execute(text(
                                        "UPDATE review_audit_events SET created_at=now()-interval '10 minutes' "
                                        "WHERE review_id=:id AND action='recovery_attempt'"
                                    ), {"id": review_id})
                                    db.commit()
                        final = worker.recover_approved_refund.run(review_id)
                        assert final["status"] == "manual_review", final
                        assert worker.schedule_stalled_approved_reviews.run()["queued"] == 0
                        with factory() as db:
                            ticket = db.get(Ticket, ticket_id)
                            order = db.execute(select(Order).where(Order.order_id == order_id)).scalar_one()
                            assert ticket.status == "escalated" and order.status == "paid"
                            counts = dict(db.execute(text(
                                "SELECT action, count(*) FROM review_audit_events "
                                "WHERE review_id=:id GROUP BY action"
                            ), {"id": review_id}).all())
                            assert counts.get("recovery_attempt") == 3
                            assert counts.get("recovery_failed") == 3
                        report.append({"case_id": "M2-RECOVERY-BOUND-AND-MANUAL-REVIEW", "passed": True})
                except Exception as exc:
                    report.append({
                        "case_id": "M2-RECOVERY-" + scenario.upper(),
                        "passed": False,
                        "reason": f"{type(exc).__name__}: {exc}",
                    })
                finally:
                    refund_module.process_large_refund = old[6]
                    outer.rollback()
    finally:
        (
            worker.SyncSessionLocal,
            refund_module.SyncSessionLocal,
            worker.sync_redis_client,
            worker.recover_approved_refund.apply_async,
            worker.dispatch_review_outbox.apply_async,
            worker.process_agent_task.apply_async,
            refund_module.process_large_refund,
        ) = old

    output = {
        "run_at_utc": datetime.now(timezone.utc).isoformat(),
        "scope": "isolated rollback-only review recovery; LLM and Celery queue stubbed",
        "total": len(report),
        "passed": sum(item["passed"] for item in report),
        "results": report,
    }
    target = ROOT / "eval_v3" / "results" / (
        "recovery_" + datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%SZ") + ".json"
    )
    target.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Recovery eval: {output['passed']}/{output['total']} passed")
    for item in report:
        if not item["passed"]:
            print(f"FAIL {item['case_id']}: {item['reason']}")
    print(f"Report: {target}")
    if output["passed"] != output["total"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
