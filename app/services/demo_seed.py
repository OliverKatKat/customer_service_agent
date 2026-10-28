"""Small, explicit synthetic fixtures. Existing demo edits are never reset."""

from datetime import datetime, timedelta
from decimal import Decimal
import json
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_password_hash, verify_password
from app.models.tenant import Tenant
from app.models.user import User
from app.models.order import Order
from app.models.ticket import Ticket
from app.models.review import ReviewRequest, ReviewAuditEvent
from app.models.workbench import (
    ServiceCase,
    CaseTicketBinding,
    CaseWorkItem,
    CaseNote,
    CaseEvent,
)

FIXTURE = Path(__file__).resolve().parents[2] / "demo/workbench.json"


def load_fixture():
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    if (
        fixture.get("synthetic") is not True
        or fixture.get("version") != "earphones-demo-v1"
    ):
        raise ValueError("Unknown demo fixture")
    return fixture


async def seed_demo(db: AsyncSession, password: str):
    if len(password) < 12:
        raise ValueError("Demo password must contain at least 12 characters")
    fixture = load_fixture()
    counts = {"tenants": 0, "users": 0, "orders": 0, "cases": 0, "contacts": 0}
    anchor = datetime.fromisoformat(fixture["anchor_utc"])
    for row in fixture["tenants"]:
        tenant = await db.get(Tenant, row["id"])
        if tenant is not None and tenant.name != row["name"]:
            raise ValueError("Demo tenant name collides with existing data")
        if tenant is None:
            db.add(Tenant(id=row["id"], name=row["name"], is_active=True))
            counts["tenants"] += 1
    await db.flush()
    for row in fixture["users"]:
        user = await db.scalar(select(User).where(User.username == row["username"]))
        if user is not None:
            if (
                user.tenant_id != row["tenant_id"]
                or user.role != row["role"]
                or not verify_password(password, user.hashed_password)
            ):
                raise ValueError(
                    "Existing demo account differs; refusing to overwrite it"
                )
        else:
            db.add(
                User(**row, hashed_password=get_password_hash(password), is_active=True)
            )
            counts["users"] += 1
    await db.flush()
    order_rows = {row["order_id"]: row for row in fixture["orders"]}
    for row in fixture["orders"]:
        order = await db.scalar(select(Order).where(Order.order_id == row["order_id"]))
        if order is not None:
            if (
                order.tenant_id != row["tenant_id"]
                or order.customer_id != row["customer_id"]
            ):
                raise ValueError("Demo order identifier collides with existing data")
        else:
            db.add(
                Order(
                    tenant_id=row["tenant_id"],
                    customer_id=row["customer_id"],
                    order_id=row["order_id"],
                    status=row["status"],
                    amount=float(Decimal(row["amount"])),
                    created_at=(anchor - timedelta(days=2)).replace(tzinfo=None),
                )
            )
            counts["orders"] += 1
    await db.flush()
    for index, row in enumerate(fixture["cases"]):
        order = order_rows[row["order_id"]]
        existing = await db.get(ServiceCase, (row["tenant_id"], row["case_id"]))
        if existing is not None:
            if (
                existing.source != "synthetic_demo"
                or existing.customer_id != order["customer_id"]
                or existing.order_id != row["order_id"]
            ):
                raise ValueError("Demo case identifier collides with existing data")
            continue
        opened = anchor - timedelta(hours=index)
        terminal = row["status"] in {"resolved", "closed"}
        case = ServiceCase(
            tenant_id=row["tenant_id"],
            case_id=row["case_id"],
            customer_id=order["customer_id"],
            order_id=row["order_id"],
            case_type=row["case_type"],
            reason_code=row["reason_code"],
            status=row["status"],
            opened_at=opened,
            resolved_at=opened + timedelta(minutes=30) if terminal else None,
            source="synthetic_demo",
        )
        db.add(case)
        await db.flush()
        contacts = []
        for offset, contact in enumerate(row["contacts"]):
            ticket = Ticket(
                tenant_id=case.tenant_id,
                customer_id=case.customer_id,
                order_id=case.order_id,
                title=contact["title"],
                description=contact["description"],
                status=contact["status"],
                created_at=(opened + timedelta(minutes=offset)).replace(tzinfo=None),
                resolution=contact["description"]
                if contact["status"] in {"resolved", "closed", "rejected"}
                else None,
            )
            db.add(ticket)
            await db.flush()
            contacts.append(ticket)
            db.add(
                CaseTicketBinding(
                    tenant_id=case.tenant_id, case_id=case.case_id, ticket_id=ticket.id
                )
            )
            counts["contacts"] += 1
        owner = (
            "demo_echo_staff" if case.tenant_id == "demo_echo" else "demo_nova_admin"
        )
        db.add(
            CaseWorkItem(
                tenant_id=case.tenant_id,
                case_id=case.case_id,
                kind="intake",
                status="done" if terminal else "open",
                assigned_to=owner,
                due_at=opened + timedelta(hours=24),
                completed_at=case.resolved_at if terminal else None,
            )
        )
        db.add(
            CaseNote(
                tenant_id=case.tenant_id,
                case_id=case.case_id,
                author_id=owner,
                content=f"演示商品：{order['product']}。此案件为固定合成样本；历史状态是预置数据，不是已测得的模型结果。",
                created_at=opened,
            )
        )
        db.add(
            CaseEvent(
                tenant_id=case.tenant_id,
                case_id=case.case_id,
                actor_id="system:demo-seed",
                action="created",
                payload={"fixture": fixture["version"], "synthetic": True},
                created_at=opened,
            )
        )
        if row["review_status"]:
            state = row["review_status"]
            review = ReviewRequest(
                tenant_id=case.tenant_id,
                ticket_id=contacts[0].id,
                customer_id=case.customer_id,
                order_id=case.order_id,
                amount=Decimal(order["amount"]),
                reason="合成演示申请：请核对订单状态和商家政策",
                status=state,
                created_at=opened + timedelta(minutes=5),
                decided_at=opened + timedelta(minutes=15)
                if state != "pending"
                else None,
                decision_actor_id="demo_echo_admin" if state != "pending" else None,
                decision_reason="合成样本预置人工决定" if state != "pending" else None,
            )
            db.add(review)
            await db.flush()
            db.add(
                ReviewAuditEvent(
                    tenant_id=case.tenant_id,
                    review_id=review.id,
                    actor_id="system:demo-seed",
                    action="requested",
                    reason="合成历史记录",
                    created_at=review.created_at,
                )
            )
            if state != "pending":
                db.add(
                    ReviewAuditEvent(
                        tenant_id=case.tenant_id,
                        review_id=review.id,
                        actor_id="demo_echo_admin",
                        action=state,
                        reason="合成历史记录",
                        created_at=review.decided_at,
                    )
                )
            if state == "approved":
                db.add(
                    ReviewAuditEvent(
                        tenant_id=case.tenant_id,
                        review_id=review.id,
                        actor_id="system:demo-seed",
                        action="refund_applied",
                        reason="合成历史记录，非实际支付",
                        created_at=opened + timedelta(minutes=20),
                    )
                )
        counts["cases"] += 1
    await db.flush()
    return counts
