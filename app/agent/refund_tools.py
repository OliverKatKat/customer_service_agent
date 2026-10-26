"""Read-only facts scoped to the trusted merchant, customer, and ticket."""

import json
import logging
from math import isfinite

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from pydantic import Field
from sqlalchemy import select

from app.core.db import AsyncSessionLocal
from app.models.order import Order
from app.models.ticket import Ticket
from app.models.user import User


logger = logging.getLogger("agent.refund")


def _result(status: str, **details: object) -> str:
    return json.dumps({"status": status, **details}, ensure_ascii=False)


@tool
async def inspect_refund_request(
    order_id: str = Field(..., description="当前工单绑定的订单号"),
    config: RunnableConfig = None,
) -> str:
    """Read the authenticated customer's refund facts without exposing another order."""
    configurable = (config or {}).get("configurable", {})
    tenant_id = configurable.get("tenant_id")
    customer_id = configurable.get("customer_id")
    ticket_id = configurable.get("ticket_id")
    if not isinstance(tenant_id, str) or not tenant_id.strip():
        return _result("missing_context")
    if not isinstance(customer_id, str) or not customer_id.strip():
        return _result("missing_context")
    if not isinstance(order_id, str) or not order_id.strip():
        return _result("missing_order_id")
    try:
        ticket_pk = int(ticket_id)
    except (TypeError, ValueError):
        return _result("missing_context")

    try:
        async with AsyncSessionLocal() as db:
            user = (
                await db.execute(
                    select(User).where(
                        User.tenant_id == tenant_id, User.username == customer_id
                    )
                )
            ).scalars().first()
            if user is None or not user.is_active:
                return _result("account_inactive")

            ticket = (
                await db.execute(
                    select(Ticket).where(
                        Ticket.id == ticket_pk,
                        Ticket.tenant_id == tenant_id,
                        Ticket.customer_id == customer_id,
                    )
                )
            ).scalars().first()
            if ticket is None:
                return _result("ticket_unavailable")
            if not ticket.order_id:
                return _result("missing_order_binding")
            if ticket.order_id != order_id:
                return _result("order_unavailable")

            order = (
                await db.execute(
                    select(Order).where(
                        Order.tenant_id == tenant_id,
                        Order.order_id == order_id,
                        Order.customer_id == customer_id,
                    )
                )
            ).scalars().first()
            if order is None:
                return _result("order_unavailable")
            amount = float(order.amount)
            if not isfinite(amount) or amount < 0:
                return _result("invalid_order_data")

            return _result(
                "ok",
                order_id=order.order_id,
                order_status=order.status,
                amount=round(amount, 2),
                ticket_status=ticket.status,
            )
    except Exception:
        logger.exception("Failed to inspect refund facts for ticket %s", ticket_pk)
        return _result("unavailable")
