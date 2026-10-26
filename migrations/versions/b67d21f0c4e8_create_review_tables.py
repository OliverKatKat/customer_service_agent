"""Create durable human-review and outbox tables.

Revision ID: b67d21f0c4e8
Revises: a94e39b8d9c1
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "b67d21f0c4e8"
down_revision: Union[str, Sequence[str], None] = "a94e39b8d9c1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "review_requests",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(64), sa.ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("ticket_id", sa.Integer(), sa.ForeignKey("tickets.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("customer_id", sa.String(100), nullable=False),
        sa.Column("order_id", sa.String(100), nullable=False),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("decision_actor_id", sa.String(100), nullable=True),
        sa.Column("decision_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.UniqueConstraint("ticket_id", name="uq_review_requests_ticket_id"),
        sa.CheckConstraint("status IN ('pending', 'approved', 'rejected')", name="ck_review_requests_status"),
    )
    op.create_index(
        "ix_review_requests_tenant_status_created",
        "review_requests",
        ["tenant_id", "status", "created_at"],
    )
    op.create_table(
        "review_audit_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("review_id", sa.Integer(), sa.ForeignKey("review_requests.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("tenant_id", sa.String(64), sa.ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("actor_id", sa.String(100), nullable=False),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_review_audit_events_tenant_review",
        "review_audit_events",
        ["tenant_id", "review_id"],
    )
    op.create_table(
        "notification_outbox",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("review_id", sa.Integer(), sa.ForeignKey("review_requests.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("tenant_id", sa.String(64), sa.ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("event_key", sa.String(160), nullable=False),
        sa.Column("event_type", sa.String(40), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("event_key", name="uq_notification_outbox_event_key"),
        sa.CheckConstraint("status IN ('pending', 'processing', 'sent', 'failed')", name="ck_notification_outbox_status"),
    )
    op.create_index(
        "ix_notification_outbox_status_created",
        "notification_outbox",
        ["status", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_notification_outbox_status_created", table_name="notification_outbox")
    op.drop_table("notification_outbox")
    op.drop_index("ix_review_audit_events_tenant_review", table_name="review_audit_events")
    op.drop_table("review_audit_events")
    op.drop_index("ix_review_requests_tenant_status_created", table_name="review_requests")
    op.drop_table("review_requests")