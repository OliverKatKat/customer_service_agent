"""Add isolated shipment facts for the logistics MCP provider."""
from alembic import op
import sqlalchemy as sa

revision = 'd34b9c2e7810'
down_revision = 'c12a8e4d5f90'
branch_labels = None
depends_on = None


def upgrade():
    op.execute('CREATE SCHEMA logistics')
    op.execute('REVOKE ALL ON SCHEMA logistics FROM PUBLIC')
    op.create_table('shipments',
        sa.Column('tenant_id', sa.String(64), primary_key=True),
        sa.Column('shipment_id', sa.String(64), primary_key=True),
        sa.Column('order_id', sa.String(100), nullable=False),
        sa.Column('customer_id', sa.String(100), nullable=False),
        sa.Column('carrier', sa.String(100), nullable=False),
        sa.Column('tracking_number', sa.String(100), nullable=False),
        sa.Column('source', sa.String(32), nullable=False),
        sa.ForeignKeyConstraint(['tenant_id','order_id'], ['public.orders.tenant_id','public.orders.order_id'], ondelete='RESTRICT'),
        sa.CheckConstraint("source IN ('synthetic','external')",name='ck_shipment_source'),
        schema='logistics')
    op.create_index('ix_shipments_scope','shipments',['tenant_id','customer_id','order_id'],schema='logistics')
    op.create_table('shipment_events',
        sa.Column('tenant_id',sa.String(64),primary_key=True),
        sa.Column('shipment_id',sa.String(64),primary_key=True),
        sa.Column('event_id',sa.String(64),primary_key=True),
        sa.Column('status',sa.String(32),nullable=False),
        sa.Column('description',sa.String(400),nullable=False),
        sa.Column('occurred_at',sa.DateTime(timezone=True),nullable=False),
        sa.ForeignKeyConstraint(['tenant_id','shipment_id'],['logistics.shipments.tenant_id','logistics.shipments.shipment_id'],ondelete='CASCADE'),
        sa.CheckConstraint("status IN ('awaiting_pickup','picked_up','in_transit','delivered','exception')",name='ck_shipment_event_status'),schema='logistics')
    op.create_index('ix_shipment_event_time','shipment_events',['tenant_id','shipment_id','occurred_at'],schema='logistics')


def downgrade():
    connection=op.get_bind()
    if connection.scalar(sa.text('SELECT EXISTS(SELECT 1 FROM logistics.shipments)')):
        raise RuntimeError('Refusing to remove populated logistics tables')
    op.drop_table('shipment_events',schema='logistics')
    op.drop_table('shipments',schema='logistics')
    op.execute('DROP SCHEMA logistics')
