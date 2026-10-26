from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy import String, Float, DateTime, ForeignKey, Index
from datetime import datetime
from app.core.db import Base

class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (
        Index("ix_orders_tenant_order_id", "tenant_id", "order_id"),
        Index("ix_orders_tenant_customer_id", "tenant_id", "customer_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
    )
    order_id: Mapped[str] = mapped_column(String(100), unique=True, index=True, nullable=False)
    customer_id: Mapped[str] = mapped_column(String(100), index=True, default="unknown_user")
    
    # 真实订单状态：如 paid(已支付), shipped(已发货), pending(排队中), out_of_stock(缺货)
    status: Mapped[str] = mapped_column(String(50), default="paid")
    amount: Mapped[float] = mapped_column(Float, default=0.0)
    
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
