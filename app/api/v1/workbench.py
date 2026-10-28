"""Staff-facing case workspace. All object lookups bind the verified tenant."""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import require_staff
from app.api.envelopes import ContractRoute, envelope, make_cursor, read_cursor
from app.api.v1.workbench_schemas import (
    CasePatch,
    CaseStatus,
    CaseType,
    NoteCreate,
    WorkItemPatch,
)
from app.core.db import get_db
from app.core.identity import customer_ref
from app.models.order import Order
from app.models.review import ReviewRequest, ReviewAuditEvent
from app.models.ticket import Ticket
from app.models.user import User
from app.models.workbench import (
    CaseEvent,
    CaseNote,
    CaseTicketBinding,
    CaseWorkItem,
    ServiceCase,
    utc_now,
)
from app.services.cases import ensure_can_finish, get_case
from app.services.case_material import MaterialRequest, build_material

router = APIRouter(prefix="/workbench", route_class=ContractRoute)


@router.post("/cases/{case_id}/material")
async def case_material(case_id: str, payload: MaterialRequest, request: Request,
                        actor: dict=Depends(require_staff), db: AsyncSession=Depends(get_db)):
    material=await build_material(db,actor["tenant_id"],case_id,payload.use_model)
    return envelope(request,material)


def case_card(case):
    return {
        "case_id": case.case_id,
        "customer_ref": customer_ref(case.tenant_id, case.customer_id),
        "order_id": case.order_id,
        "case_type": case.case_type,
        "reason_code": case.reason_code,
        "status": case.status,
        "opened_at": case.opened_at.isoformat(),
        "resolved_at": case.resolved_at.isoformat() if case.resolved_at else None,
        "version": case.version,
        "source": case.source,
    }


def work_item_card(item):
    return {
        "id": str(item.id),
        "case_id": item.case_id,
        "kind": item.kind,
        "status": item.status,
        "assigned_to": item.assigned_to,
        "due_at": item.due_at.isoformat(),
        "overdue": item.status == "open" and item.due_at < utc_now(),
        "completed_at": item.completed_at.isoformat() if item.completed_at else None,
        "version": item.version,
    }


@router.get("/cases")
async def list_cases(
    request: Request,
    state: CaseStatus | None = None,
    case_type: CaseType | None = None,
    cursor: str | None = Query(default=None, max_length=2048),
    limit: int = Query(default=20, ge=1, le=100),
    actor: dict = Depends(require_staff),
    db: AsyncSession = Depends(get_db),
):
    scope = f"cases:{actor['tenant_id']}:{actor['sub']}:{state}:{case_type}"
    last = read_cursor(cursor, scope, str)
    query = select(ServiceCase).where(
        ServiceCase.tenant_id == actor["tenant_id"], ServiceCase.case_id > last
    )
    if state:
        query = query.where(ServiceCase.status == state)
    if case_type:
        query = query.where(ServiceCase.case_type == case_type)
    rows = (
        (await db.execute(query.order_by(ServiceCase.case_id).limit(limit + 1)))
        .scalars()
        .all()
    )
    return envelope(
        request,
        {
            "cases": [case_card(c) for c in rows[:limit]],
            "next_cursor": make_cursor(rows[limit - 1].case_id, scope)
            if len(rows) > limit
            else None,
        },
    )


@router.get("/todos")
async def list_todos(
    request: Request,
    state: Literal["open", "done"] = "open",
    overdue_only: bool = False,
    cursor: str | None = Query(default=None, max_length=2048),
    limit: int = Query(default=20, ge=1, le=100),
    actor: dict = Depends(require_staff),
    db: AsyncSession = Depends(get_db),
):
    scope = f"todos:{actor['tenant_id']}:{actor['sub']}:{state}:{overdue_only}"
    last = read_cursor(cursor, scope)
    query = select(CaseWorkItem).where(
        CaseWorkItem.tenant_id == actor["tenant_id"],
        CaseWorkItem.status == state,
        CaseWorkItem.id > last,
    )
    if overdue_only:
        query = query.where(CaseWorkItem.due_at < utc_now())
    rows = (
        (await db.execute(query.order_by(CaseWorkItem.id).limit(limit + 1)))
        .scalars()
        .all()
    )
    return envelope(
        request,
        {
            "items": [work_item_card(row) for row in rows[:limit]],
            "next_cursor": make_cursor(rows[limit - 1].id, scope)
            if len(rows) > limit
            else None,
        },
        due_policy="internal_intake_target_24h",
        sort="id_ascending",
    )


@router.get("/cases/{case_id}")
async def case_detail(
    case_id: str,
    request: Request,
    actor: dict = Depends(require_staff),
    db: AsyncSession = Depends(get_db),
):
    case = await get_case(db, actor["tenant_id"], case_id)
    tenant = case.tenant_id
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
                    CaseTicketBinding.tenant_id == tenant,
                    CaseTicketBinding.case_id == case_id,
                )
                .order_by(Ticket.id)
                .limit(101)
            )
        )
        .scalars()
        .all()
    )
    ticket_ids = [t.id for t in tickets[:100]]
    reviews = (
        (
            await db.execute(
                select(ReviewRequest)
                .where(
                    ReviewRequest.tenant_id == tenant,
                    ReviewRequest.ticket_id.in_(ticket_ids),
                    ReviewRequest.customer_id == case.customer_id,
                )
                .order_by(ReviewRequest.id)
            )
        )
        .scalars()
        .all()
        if ticket_ids
        else []
    )
    audit = (
        (
            await db.execute(
                select(ReviewAuditEvent)
                .where(
                    ReviewAuditEvent.tenant_id == tenant,
                    ReviewAuditEvent.review_id.in_([r.id for r in reviews]),
                )
                .order_by(ReviewAuditEvent.id)
                .limit(201)
            )
        )
        .scalars()
        .all()
        if reviews
        else []
    )
    order = (
        (
            await db.execute(
                select(Order).where(
                    Order.tenant_id == tenant,
                    Order.order_id == case.order_id,
                    Order.customer_id == case.customer_id,
                )
            )
        ).scalar_one_or_none()
        if case.order_id
        else None
    )
    notes = (
        (
            await db.execute(
                select(CaseNote)
                .where(CaseNote.tenant_id == tenant, CaseNote.case_id == case_id)
                .order_by(CaseNote.id.desc())
                .limit(51)
            )
        )
        .scalars()
        .all()
    )
    events = (
        (
            await db.execute(
                select(CaseEvent)
                .where(CaseEvent.tenant_id == tenant, CaseEvent.case_id == case_id)
                .order_by(CaseEvent.id.desc())
                .limit(51)
            )
        )
        .scalars()
        .all()
    )
    items = (
        (
            await db.execute(
                select(CaseWorkItem)
                .where(
                    CaseWorkItem.tenant_id == tenant, CaseWorkItem.case_id == case_id
                )
                .order_by(CaseWorkItem.id)
            )
        )
        .scalars()
        .all()
    )
    by_ticket = {t.id: t for t in tickets}
    review_cards = []
    for r in reviews:
        completed = (
            r.status == "approved"
            and by_ticket[r.ticket_id].status == "resolved"
            and order is not None
            and order.status == "refunded"
        )
        review_cards.append(
            {
                "review_id": str(r.id),
                "ticket_id": str(r.ticket_id),
                "status": r.status,
                "version": r.version,
                "approved_amount": format(r.amount, ".2f")
                if r.status == "approved"
                else None,
                "requested_amount": format(r.amount, ".2f"),
                "reason": r.reason,
                "decision_reason": r.decision_reason,
                "execution_status": "succeeded"
                if completed
                else "processing"
                if r.status == "approved"
                else "not_started",
                "execution_mode": "simulation",
            }
        )
    data = case_card(case)
    data.update(
        {
            "tickets": [
                {
                    "ticket_id": str(t.id),
                    "title": t.title,
                    "description": t.description,
                    "status": t.status,
                    "resolution": t.resolution,
                }
                for t in tickets[:100]
            ],
            "order": {
                "order_id": order.order_id,
                "status": order.status,
                "legacy_amount": str(order.amount),
                "amount_source": "legacy_float_unmigrated",
            }
            if order
            else None,
            "reviews": review_cards,
            "review_audit": [
                {
                    "action": a.action,
                    "reason": a.reason,
                    "created_at": a.created_at.isoformat(),
                }
                for a in audit[:200]
            ],
            "notes": [
                {
                    "id": str(n.id),
                    "author_ref": customer_ref(tenant, n.author_id),
                    "content": n.content,
                    "created_at": n.created_at.isoformat(),
                }
                for n in reversed(notes[:50])
            ],
            "events": [
                {
                    "action": e.action,
                    "actor_ref": customer_ref(tenant, e.actor_id),
                    "payload": e.payload,
                    "created_at": e.created_at.isoformat(),
                }
                for e in reversed(events[:50])
            ],
            "work_items": [work_item_card(i) for i in items],
            "truncated": {
                "tickets": len(tickets) > 100,
                "notes": len(notes) > 50,
                "events": len(events) > 50,
                "review_audit": len(audit) > 200,
            },
        }
    )
    return envelope(
        request,
        data,
        execution_mode="simulation",
        amount_migration="pending",
        summary_generation="not_implemented",
    )


@router.post("/cases/{case_id}/notes", status_code=201)
async def add_note(
    case_id: str,
    payload: NoteCreate,
    request: Request,
    actor: dict = Depends(require_staff),
    db: AsyncSession = Depends(get_db),
):
    case = await get_case(db, actor["tenant_id"], case_id)
    note = CaseNote(
        tenant_id=case.tenant_id,
        case_id=case_id,
        author_id=actor["sub"],
        content=payload.content,
    )
    db.add(note)
    await db.flush()
    db.add(
        CaseEvent(
            tenant_id=case.tenant_id,
            case_id=case_id,
            actor_id=actor["sub"],
            action="note_added",
            payload={"note_id": str(note.id)},
        )
    )
    await db.commit()
    return envelope(
        request,
        {
            "id": str(note.id),
            "content": note.content,
            "created_at": note.created_at.isoformat(),
        },
    )


@router.patch("/cases/{case_id}")
async def patch_case(
    case_id: str,
    payload: CasePatch,
    request: Request,
    actor: dict = Depends(require_staff),
    db: AsyncSession = Depends(get_db),
):
    case = await get_case(db, actor["tenant_id"], case_id, lock=True)
    if case.version != payload.expected_version:
        raise HTTPException(409, "案件版本已变化，请刷新后重试")
    changes = payload.model_dump(exclude_unset=True)
    changes.pop("expected_version")
    changes.pop("reason", None)
    if case.status == "closed" or (
        case.status == "resolved" and payload.status != "closed"
    ):
        raise HTTPException(409, "已结束的案件不接受此修改")
    if payload.status == "open" and case.status != "open":
        raise HTTPException(409, "本期不支持重新打开案件")
    if payload.status in {"resolved", "closed"}:
        await ensure_can_finish(db, case)
        if case.resolved_at is None:
            case.resolved_at = utc_now()
        await db.execute(
            update(CaseWorkItem)
            .where(
                CaseWorkItem.tenant_id == case.tenant_id,
                CaseWorkItem.case_id == case_id,
                CaseWorkItem.status == "open",
            )
            .values(
                status="done", completed_at=utc_now(), version=CaseWorkItem.version + 1
            )
        )
    for key, value in changes.items():
        setattr(case, key, value)
    case.version += 1
    db.add(
        CaseEvent(
            tenant_id=case.tenant_id,
            case_id=case_id,
            actor_id=actor["sub"],
            action="case_updated",
            payload={"changes": changes, "reason": payload.reason},
        )
    )
    await db.commit()
    return envelope(request, case_card(case))


@router.patch("/work-items/{item_id}")
async def patch_work_item(
    item_id: int,
    payload: WorkItemPatch,
    request: Request,
    actor: dict = Depends(require_staff),
    db: AsyncSession = Depends(get_db),
):
    tenant = actor["tenant_id"]
    identity = (
        await db.execute(
            select(CaseWorkItem.case_id).where(
                CaseWorkItem.tenant_id == tenant, CaseWorkItem.id == item_id
            )
        )
    ).scalar_one_or_none()
    if identity is None:
        raise HTTPException(404, "资源不存在或不可访问")
    case = await get_case(db, tenant, identity, lock=True)
    item = (
        await db.execute(
            select(CaseWorkItem)
            .where(CaseWorkItem.tenant_id == tenant, CaseWorkItem.id == item_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    if item.version != payload.expected_version:
        raise HTTPException(409, "待办版本已变化，请刷新后重试")
    if case.status in {"resolved", "closed"}:
        raise HTTPException(409, "案件已结束")
    changes = payload.model_dump(exclude_unset=True)
    changes.pop("expected_version")
    if payload.assigned_to is not None:
        assignee = (
            await db.execute(
                select(User.id).where(
                    User.tenant_id == tenant,
                    User.username == payload.assigned_to,
                    User.is_active.is_(True),
                    User.role.in_(["admin", "staff"]),
                )
            )
        ).scalar_one_or_none()
        if assignee is None:
            raise HTTPException(422, "负责人必须是本商户有效的售后工作人员")
    for key, value in changes.items():
        setattr(item, key, value)
    if "status" in changes:
        item.completed_at = utc_now() if item.status == "done" else None
    item.version += 1
    event_changes = {
        key: (value.isoformat() if hasattr(value, "isoformat") else value)
        for key, value in changes.items()
    }
    db.add(
        CaseEvent(
            tenant_id=tenant,
            case_id=item.case_id,
            actor_id=actor["sub"],
            action="work_item_updated",
            payload={"item_id": str(item.id), "changes": event_changes},
        )
    )
    await db.commit()
    return envelope(request, work_item_card(item))
