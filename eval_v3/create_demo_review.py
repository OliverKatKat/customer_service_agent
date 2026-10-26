"""Create one synthetic pending refund review in the isolated eval v3 database.

Run after the eval services are up. This writes only a newly generated demo order
and ticket, then calls the real refund tool. It never invokes the external LLM.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import uuid
from pathlib import Path

from sqlalchemy import select, text

from seed import checked_url

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent))


def guard() -> None:
    url = os.environ.get("EVAL_V3_DATABASE_URL", "")
    checked_url(url)
    if os.environ.get("DATABASE_URL") != url:
        raise RuntimeError("DATABASE_URL must point to isolated eval v3")
    if os.environ.get("REDIS_URL") != "redis://127.0.0.1:56379/0":
        raise RuntimeError("REDIS_URL must point to isolated eval v3")
    if os.environ.get("QDRANT_URL") != "http://127.0.0.1:56333":
        raise RuntimeError("QDRANT_URL must point to isolated eval v3")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Create one new synthetic review")
    args = parser.parse_args()
    guard()
    if not args.apply:
        print("Preview: create one synthetic paid order, ticket and pending review in eval v3.")
        print("Pass --apply to create it; no external LLM or payment gateway is called.")
        return

    from app.core.db import AsyncSessionLocal
    from app.models.order import Order
    from app.models.ticket import Ticket
    from app.agent.tools import process_large_refund
    from app.models.review import ReviewRequest

    tenant_id = "merchant_eval_alpha"
    customer_id = "eval_alpha_alice"
    order_id = "E3-DEMO-" + uuid.uuid4().hex[:12].upper()
    async with AsyncSessionLocal() as db:
        database = await db.scalar(text("SELECT current_database()"))
        if database != "agent_eval_v3":
            raise RuntimeError(f"Refusing unexpected database {database}")
        order = Order(
            tenant_id=tenant_id, customer_id=customer_id,
            order_id=order_id, amount=1599, status="paid",
        )
        db.add(order)
        await db.flush()
        ticket = Ticket(
            tenant_id=tenant_id, customer_id=customer_id,
            order_id=order_id, title="合成演示：大额退款人工审核",
            description="合成数据；请在管理端核对金额与订单状态后选择批准或驳回。",
            status="pending",
        )
        db.add(ticket)
        await db.flush()
        ticket_id = ticket.id
        await db.commit()

    result = await process_large_refund.ainvoke(
        {"order_id": order_id, "amount": 1599},
        config={"configurable": {
            "tenant_id": tenant_id, "customer_id": customer_id,
            "ticket_id": str(ticket_id),
        }},
    )
    async with AsyncSessionLocal() as db:
        review = (await db.execute(select(ReviewRequest).where(
            ReviewRequest.ticket_id == ticket_id,
            ReviewRequest.tenant_id == tenant_id,
        ))).scalars().first()
    if review is None or review.status != "pending":
        raise RuntimeError(f"Demo ticket {ticket_id} was created, but review failed: {result}")
    print(f"Review #{review.id} created for ticket #{ticket_id}, order {order_id}.")
    print("Open http://127.0.0.1:58080/review and log in as eval_alpha_admin.")
    print("Synthetic data only; the prototype never sends an actual payment refund.")


if __name__ == "__main__":
    asyncio.run(main())