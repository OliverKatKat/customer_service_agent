"""Add merchant tenant scope to users, orders, and tickets.

Revision ID: a94e39b8d9c1
Revises: df73c92fac1b
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a94e39b8d9c1"
down_revision: Union[str, Sequence[str], None] = "df73c92fac1b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

DEFAULT_TENANT_ID = "merchant_default"


def upgrade() -> None:
    op.create_table(
        "tenants",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.execute(
        "INSERT INTO tenants (id, name, is_active, created_at) "
        "VALUES ('merchant_default', 'Default merchant', TRUE, CURRENT_TIMESTAMP)"
    )

    for table in ("users", "orders", "tickets"):
        op.add_column(table, sa.Column("tenant_id", sa.String(length=64), nullable=True))
        op.execute(f"UPDATE {table} SET tenant_id = '{DEFAULT_TENANT_ID}' WHERE tenant_id IS NULL")
        op.alter_column(table, "tenant_id", existing_type=sa.String(length=64), nullable=False)
        op.create_foreign_key(
            f"fk_{table}_tenant_id_tenants", table, "tenants", ["tenant_id"], ["id"], ondelete="RESTRICT"
        )

    op.create_index("ix_users_tenant_username", "users", ["tenant_id", "username"])
    op.create_index("ix_orders_tenant_order_id", "orders", ["tenant_id", "order_id"])
    op.create_index("ix_orders_tenant_customer_id", "orders", ["tenant_id", "customer_id"])
    op.create_index("ix_tickets_tenant_customer_id", "tickets", ["tenant_id", "customer_id"])
    op.create_index("ix_tickets_tenant_order_id", "tickets", ["tenant_id", "order_id"])
    op.create_index("ix_tickets_tenant_status", "tickets", ["tenant_id", "status"])


def downgrade() -> None:
    op.drop_index("ix_tickets_tenant_status", table_name="tickets")
    op.drop_index("ix_tickets_tenant_order_id", table_name="tickets")
    op.drop_index("ix_tickets_tenant_customer_id", table_name="tickets")
    op.drop_index("ix_orders_tenant_customer_id", table_name="orders")
    op.drop_index("ix_orders_tenant_order_id", table_name="orders")
    op.drop_index("ix_users_tenant_username", table_name="users")

    for table in ("tickets", "orders", "users"):
        op.drop_constraint(f"fk_{table}_tenant_id_tenants", table, type_="foreignkey")
        op.drop_column(table, "tenant_id")

    op.drop_table("tenants")
