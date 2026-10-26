from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy import String, Text, DateTime, Column,Integer
from datetime import datetime
from app.core.db import Base # 我们稍后在下一步创建 Base

class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    title: Mapped[str] = mapped_column(String(255), index=True) # 调研主题
    status: Mapped[str] = mapped_column(String(50), default="pending") # 状态: pending, running, completed, failed
    content: Mapped[str] = mapped_column(Text, nullable=True) # 最终生成的报告内容
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    #版本号字段
    version = Column(Integer, default=1,nullable=False)
    #开启SQLAlchemy的自动乐观锁防护
    #每次修改数据，SQLAlchemy就会自动做两件事
    #1. 自动在SQL语句后加上 WHERE version = 当前版本
    #2. 自动把版本号 +1
    __mapper_args__ = {
        "version_id_col" : version
    }