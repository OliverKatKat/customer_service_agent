from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy import String, Text, DateTime, Column, Integer, ForeignKey, Index, UniqueConstraint
from datetime import datetime
from app.core.db import Base

class Ticket(Base):
    __tablename__ = "tickets"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_tickets_tenant_id_workbench"),
        Index("ix_tickets_tenant_customer_id", "tenant_id", "customer_id"),
        Index("ix_tickets_tenant_order_id", "tenant_id", "order_id"),
        Index("ix_tickets_tenant_status", "tenant_id", "status"),
    )

    # 1. 基础主键
    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    tenant_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
    )
    
    # 2. 业务关联字段 (售后支持必备)
    customer_id: Mapped[str] = mapped_column(String(100), index=True, default="unknown_user")
    order_id: Mapped[str] = mapped_column(String(100), nullable=True, index=True) # 可能为空，比如只是单纯的技术咨询
    
    # 3. 工单内容
    title: Mapped[str] = mapped_column(String(255), index=True) # 用户提交的简要问题
    description: Mapped[str] = mapped_column(Text, nullable=True) # 详细描述/报错日志
    
    # 4. 状态流转控制
    # 状态机：pending(待接单) -> processing(Agent处理中) -> escalated(触发HITL转人工) -> resolved(已解决) -> closed(已关闭)
    status: Mapped[str] = mapped_column(String(50), default="pending", index=True)
    
    # 5. 最终处理结果
    resolution: Mapped[str] = mapped_column(Text, nullable=True) # 记录大模型或人工给出的最终方案
    
    # 6. 时间戳
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # 7. 并发防护（乐观锁）
    # 当 Agent 异步查完订单准备更新状态，刚好人类客服也在后台点“关闭工单”时，防止数据相互覆盖
    version = Column(Integer, default=1, nullable=False)

    __mapper_args__ = {
        "version_id_col": version
    }
