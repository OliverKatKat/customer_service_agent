# app/models/__init__.py
from app.core.db import Base
from app.models.task import Task
from app.models.tenant import Tenant
from app.models.ticket import Ticket  # 👈 新增这一行，让元数据收集器能感知到新表
from app.models.order import Order  # 👈 新增这一行，让元数据收集器能感知到新表
from app.models.user import User

from app.models.review import ReviewRequest, ReviewAuditEvent, NotificationOutbox
