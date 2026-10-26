"""Rename order user_id to customer_id.

Revision ID: 66f72c183fcb
Revises: f503018399c7
Create Date: 2026-06-08 22:45:45.084262

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '66f72c183fcb'
down_revision: Union[str, Sequence[str], None] = 'f503018399c7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Preserve existing orders while adopting the customer_id column name."""
    op.drop_index(op.f('ix_orders_user_id'), table_name='orders')
    op.alter_column(
        'orders',
        'user_id',
        new_column_name='customer_id',
        existing_type=sa.String(length=100),
        existing_nullable=False,
    )
    op.create_index(op.f('ix_orders_customer_id'), 'orders', ['customer_id'], unique=False)


def downgrade() -> None:
    """Restore the schema created by f503018399c7 without dropping orders."""
    op.drop_index(op.f('ix_orders_customer_id'), table_name='orders')
    op.alter_column(
        'orders',
        'customer_id',
        new_column_name='user_id',
        existing_type=sa.String(length=100),
        existing_nullable=False,
    )
    op.create_index(op.f('ix_orders_user_id'), 'orders', ['user_id'], unique=False)
