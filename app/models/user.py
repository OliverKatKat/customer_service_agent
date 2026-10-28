from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy import String, Boolean, DateTime, ForeignKey, Index
from datetime import datetime
from app.core.db import Base

class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        Index("ix_users_tenant_username", "tenant_id", "username"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    tenant_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
    )
    username: Mapped[str] = mapped_column(String(50), unique=True, index=True, nullable=False)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False) # 存 bcrypt 哈希值
    
    # Existing admin/user; workbench additionally recognizes staff and analyst.
    role: Mapped[str] = mapped_column(String(20), default="user", index=True) 
    
    # 账号状态：方便未来做“一键封禁”恶意用户的功能
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
