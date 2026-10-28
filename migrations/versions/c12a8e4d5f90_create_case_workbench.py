"""Add operational cases without changing legacy ticket/review semantics.

Revision ID: c12a8e4d5f90
Revises: b67d21f0c4e8
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "c12a8e4d5f90"
down_revision = "b67d21f0c4e8"
branch_labels = None
depends_on = None


def upgrade():
    op.create_unique_constraint(
        "uq_orders_tenant_order_workbench", "orders", ["tenant_id", "order_id"]
    )
    op.create_unique_constraint(
        "uq_tickets_tenant_id_workbench", "tickets", ["tenant_id", "id"]
    )
    op.create_table(
        "case_registry",
        sa.Column(
            "tenant_id",
            sa.String(64),
            sa.ForeignKey("tenants.id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column("case_id", sa.String(64), primary_key=True),
        sa.Column("customer_id", sa.String(100), nullable=False),
        sa.Column("order_id", sa.String(100)),
        sa.Column("case_type", sa.String(16), nullable=False),
        sa.Column("reason_code", sa.String(64)),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "order_id"],
            ["orders.tenant_id", "orders.order_id"],
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "status IN ('open','in_progress','resolved','closed')",
            name="ck_case_registry_status",
        ),
        sa.CheckConstraint(
            "case_type IN ('refund','return','complaint','consultation','other')",
            name="ck_case_registry_type",
        ),
        sa.CheckConstraint(
            "resolved_at IS NULL OR resolved_at >= opened_at",
            name="ck_case_registry_resolved",
        ),
    )
    op.create_index(
        "ix_case_registry_tenant_opened",
        "case_registry",
        ["tenant_id", "opened_at", "case_id"],
    )
    op.create_index(
        "ix_case_registry_tenant_status", "case_registry", ["tenant_id", "status"]
    )
    op.create_table(
        "case_ticket_bindings",
        sa.Column("tenant_id", sa.String(64), primary_key=True),
        sa.Column("ticket_id", sa.Integer(), primary_key=True),
        sa.Column("case_id", sa.String(64), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "case_id"],
            ["case_registry.tenant_id", "case_registry.case_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "ticket_id"],
            ["tickets.tenant_id", "tickets.id"],
            ondelete="RESTRICT",
        ),
    )
    op.create_index(
        "ix_case_ticket_bindings_case", "case_ticket_bindings", ["tenant_id", "case_id"]
    )
    op.create_table(
        "case_notes",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("case_id", sa.String(64), nullable=False),
        sa.Column("author_id", sa.String(100), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "case_id"],
            ["case_registry.tenant_id", "case_registry.case_id"],
            ondelete="RESTRICT",
        ),
    )
    op.create_index("ix_case_notes_case", "case_notes", ["tenant_id", "case_id", "id"])
    op.create_table(
        "case_work_items",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("case_id", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("assigned_to", sa.String(100)),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "case_id"],
            ["case_registry.tenant_id", "case_registry.case_id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "tenant_id", "case_id", "kind", name="uq_case_work_items_kind"
        ),
        sa.CheckConstraint(
            "status IN ('open','done')", name="ck_case_work_items_status"
        ),
    )
    op.create_index(
        "ix_case_work_items_tenant_status",
        "case_work_items",
        ["tenant_id", "status", "id"],
    )
    op.create_table(
        "case_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("case_id", sa.String(64), nullable=False),
        sa.Column("actor_id", sa.String(100), nullable=False),
        sa.Column("action", sa.String(40), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "case_id"],
            ["case_registry.tenant_id", "case_registry.case_id"],
            ondelete="RESTRICT",
        ),
    )
    op.create_index(
        "ix_case_events_case", "case_events", ["tenant_id", "case_id", "id"]
    )


def downgrade():
    # Deliberately refuse to drop live operational history.
    connection = op.get_bind()
    for table in [
        "case_events",
        "case_notes",
        "case_work_items",
        "case_ticket_bindings",
        "case_registry",
    ]:
        if connection.scalar(sa.text(f"SELECT EXISTS (SELECT 1 FROM {table})")):
            raise RuntimeError(
                "Workbench contains business records; export/restore them before downgrading"
            )
    for table in [
        "case_events",
        "case_work_items",
        "case_notes",
        "case_ticket_bindings",
        "case_registry",
    ]:
        op.drop_table(table)
    op.drop_constraint("uq_tickets_tenant_id_workbench", "tickets", type_="unique")
    op.drop_constraint("uq_orders_tenant_order_workbench", "orders", type_="unique")
