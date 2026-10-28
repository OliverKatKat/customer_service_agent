"""Case intake and human transitions, with no model or queue side effects."""

import uuid
from datetime import timedelta

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ticket import Ticket
from app.models.review import ReviewRequest
from app.models.workbench import (
    ServiceCase,
    CaseTicketBinding,
    CaseWorkItem,
    CaseEvent,
    utc_now,
)


async def get_case(
    db: AsyncSession, tenant_id: str, case_id: str, *, lock=False
) -> ServiceCase:
    query = select(ServiceCase).where(
        ServiceCase.tenant_id == tenant_id, ServiceCase.case_id == case_id
    )
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    case = (await db.execute(query)).scalar_one_or_none()
    if case is None:
        raise HTTPException(404, "资源不存在或不可访问")
    return case


async def register_ticket(
    db: AsyncSession,
    ticket: Ticket,
    actor_id: str,
    case_id: str | None = None,
    *,
    opened_at=None,
    source="online",
):
    """Caller must flush the ticket first, then commit the entire intake once."""
    if case_id:
        case = await get_case(db, ticket.tenant_id, case_id, lock=True)
        if case.customer_id != ticket.customer_id or case.order_id != (
            ticket.order_id or None
        ):
            raise HTTPException(404, "资源不存在或不可访问")
        if case.status in {"resolved", "closed"}:
            raise HTTPException(409, "案件已结束，请创建新的售后申请")
        case.version += 1
        action = "contact_linked"
    else:
        case = ServiceCase(
            tenant_id=ticket.tenant_id,
            case_id="CASE-" + uuid.uuid4().hex,
            customer_id=ticket.customer_id,
            order_id=ticket.order_id or None,
            opened_at=opened_at or utc_now(),
            source=source,
        )
        db.add(case)
        await db.flush()
        db.add(
            CaseWorkItem(
                tenant_id=case.tenant_id,
                case_id=case.case_id,
                kind="intake",
                due_at=case.opened_at + timedelta(hours=24),
            )
        )
        action = "created"
    db.add(
        CaseTicketBinding(
            tenant_id=ticket.tenant_id, ticket_id=ticket.id, case_id=case.case_id
        )
    )
    db.add(
        CaseEvent(
            tenant_id=ticket.tenant_id,
            case_id=case.case_id,
            actor_id=actor_id,
            action=action,
            payload={"ticket_id": str(ticket.id)},
        )
    )
    await db.flush()
    return case


async def ensure_can_finish(db: AsyncSession, case: ServiceCase):
    """Lock contacts before reviews, matching the legacy refund/review lock order."""
    tickets = (
        (
            await db.execute(
                select(Ticket)
                .join(
                    CaseTicketBinding,
                    (CaseTicketBinding.tenant_id == Ticket.tenant_id)
                    & (CaseTicketBinding.ticket_id == Ticket.id),
                )
                .where(
                    CaseTicketBinding.tenant_id == case.tenant_id,
                    CaseTicketBinding.case_id == case.case_id,
                )
                .order_by(Ticket.id)
                .with_for_update(of=Ticket)
            )
        )
        .scalars()
        .all()
    )
    if not tickets or any(
        t.status not in {"resolved", "closed", "rejected"} for t in tickets
    ):
        raise HTTPException(409, "仍有未处理完成的联系，请先核对执行结果")
    reviews = (
        (
            await db.execute(
                select(ReviewRequest)
                .where(
                    ReviewRequest.tenant_id == case.tenant_id,
                    ReviewRequest.ticket_id.in_([t.id for t in tickets]),
                )
                .order_by(ReviewRequest.id)
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    if any(r.status == "pending" for r in reviews):
        raise HTTPException(409, "仍有待审核申请")
    by_ticket = {t.id: t for t in tickets}
    for review in reviews:
        # Verify the same two business facts as the legacy review detail route.
        if review.status == "approved":
            from app.models.order import Order

            order = (
                await db.execute(
                    select(Order).where(
                        Order.tenant_id == case.tenant_id,
                        Order.order_id == review.order_id,
                        Order.customer_id == case.customer_id,
                    )
                )
            ).scalar_one_or_none()
            if (
                by_ticket[review.ticket_id].status != "resolved"
                or order is None
                or order.status != "refunded"
            ):
                raise HTTPException(409, "批准申请尚未完成模拟执行")
