"""Operational case registry; tickets/reviews remain authoritative for execution."""

from datetime import datetime, timezone

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


def utc_now():
    return datetime.now(timezone.utc)


class ServiceCase(Base):
    __tablename__ = "case_registry"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "order_id"],
            ["orders.tenant_id", "orders.order_id"],
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "status IN ('open','in_progress','resolved','closed')",
            name="ck_case_registry_status",
        ),
        CheckConstraint(
            "case_type IN ('refund','return','complaint','consultation','other')",
            name="ck_case_registry_type",
        ),
        CheckConstraint(
            "resolved_at IS NULL OR resolved_at >= opened_at",
            name="ck_case_registry_resolved",
        ),
        Index("ix_case_registry_tenant_opened", "tenant_id", "opened_at", "case_id"),
        Index("ix_case_registry_tenant_status", "tenant_id", "status"),
    )
    tenant_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("tenants.id", ondelete="RESTRICT"), primary_key=True
    )
    case_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    customer_id: Mapped[str] = mapped_column(String(100), nullable=False)
    order_id: Mapped[str | None] = mapped_column(String(100))
    case_type: Mapped[str] = mapped_column(String(16), default="other", nullable=False)
    reason_code: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="open", nullable=False)
    opened_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source: Mapped[str] = mapped_column(String(32), default="online", nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    __mapper_args__ = {"version_id_col": version}


class CaseTicketBinding(Base):
    __tablename__ = "case_ticket_bindings"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "case_id"],
            ["case_registry.tenant_id", "case_registry.case_id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "ticket_id"],
            ["tickets.tenant_id", "tickets.id"],
            ondelete="RESTRICT",
        ),
        Index("ix_case_ticket_bindings_case", "tenant_id", "case_id"),
    )
    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    ticket_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    case_id: Mapped[str] = mapped_column(String(64), nullable=False)


class CaseNote(Base):
    __tablename__ = "case_notes"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "case_id"],
            ["case_registry.tenant_id", "case_registry.case_id"],
            ondelete="RESTRICT",
        ),
        Index("ix_case_notes_case", "tenant_id", "case_id", "id"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    case_id: Mapped[str] = mapped_column(String(64), nullable=False)
    author_id: Mapped[str] = mapped_column(String(100), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class CaseWorkItem(Base):
    __tablename__ = "case_work_items"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "case_id"],
            ["case_registry.tenant_id", "case_registry.case_id"],
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "tenant_id", "case_id", "kind", name="uq_case_work_items_kind"
        ),
        CheckConstraint("status IN ('open','done')", name="ck_case_work_items_status"),
        Index("ix_case_work_items_tenant_status", "tenant_id", "status", "id"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    case_id: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), default="intake", nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="open", nullable=False)
    assigned_to: Mapped[str | None] = mapped_column(String(100))
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    __mapper_args__ = {"version_id_col": version}


class CaseEvent(Base):
    __tablename__ = "case_events"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "case_id"],
            ["case_registry.tenant_id", "case_registry.case_id"],
            ondelete="RESTRICT",
        ),
        Index("ix_case_events_case", "tenant_id", "case_id", "id"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    case_id: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(100), nullable=False)
    action: Mapped[str] = mapped_column(String(40), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
