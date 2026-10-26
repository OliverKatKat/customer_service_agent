import asyncio
import json
import time
import logging
import random
from functools import wraps
from langchain_core.tools import tool
from pydantic import Field
from sqlalchemy import select,desc
from app.core.db import AsyncSessionLocal
from app.models.task import Task
from app.core.cache import get_session, set_session, invalidate_cache
from sqlalchemy.orm.exc import StaleDataError #乐观锁
from langchain_core.runnables import RunnableConfig
from app.agent.customer_memory import read_scoped_preferences
from redis.asyncio import Redis
from sqlalchemy import update
from app.core.db import AsyncSessionLocal
from app.models.ticket import Ticket
from app.core.config import settings
from app.models.order import Order
from app.models.user import User
from app.models.tenant import Tenant
from app.models.review import ReviewRequest, ReviewAuditEvent, NotificationOutbox
from decimal import Decimal
from math import isfinite
from opentelemetry.propagate import inject

# ==========================================
# 🚨 为 Celery 环境准备同步数据库引擎
# ==========================================
from sqlalchemy import create_engine as sync_create_engine
from sqlalchemy.orm import sessionmaker as sync_sessionmaker

# 使用 psycopg 驱动（psycopg 3），而不是 psycopg2
SYNC_DB_URL = settings.DATABASE_URL.replace(
    "postgresql+asyncpg://", 
    "postgresql+psycopg://"  # 👈 明确指定使用 psycopg 驱动
)

sync_engine = sync_create_engine(
    SYNC_DB_URL,
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=10
)
SyncSessionLocal = sync_sessionmaker(bind=sync_engine, autocommit=False, autoflush=False)
#==========================================================

# 设置日志格式
logger = logging.getLogger("agent.tools")

from app.agent.tool_runtime import tool_wrapper
from app.agent.authorization import is_valid_refund_authorization

# ==========================================
# 1. 核心逻辑：写库 + 清理缓存
# ==========================================
@tool
async def search_user_memory(query: str, config: RunnableConfig) -> str:
    """Retrieve verified local preferences for the current merchant customer."""
    configurable = (config or {}).get("configurable", {})
    tenant_id = configurable.get("tenant_id")
    customer_id = configurable.get("customer_id")
    ticket_id = configurable.get("ticket_id")
    if (
        not isinstance(tenant_id, str) or not tenant_id.strip()
        or not isinstance(customer_id, str) or not customer_id.strip()
        or not isinstance(ticket_id, (str, int))
        or not str(ticket_id).isdecimal()
        or not isinstance(query, str) or not query.strip()
    ):
        return json.dumps({"status": "no_memory_scope", "memories": []}, ensure_ascii=False)

    ticket_pk = int(ticket_id)
    try:
        async with AsyncSessionLocal() as db:
            tenant = await db.get(Tenant, tenant_id)
            user = (
                await db.execute(
                    select(User.id).where(
                        User.tenant_id == tenant_id,
                        User.username == customer_id,
                        User.is_active.is_(True),
                    )
                )
            ).scalar_one_or_none()
            ticket = (
                await db.execute(
                    select(Ticket.id).where(
                        Ticket.id == ticket_pk,
                        Ticket.tenant_id == tenant_id,
                        Ticket.customer_id == customer_id,
                    )
                )
            ).scalar_one_or_none()
            if tenant is None or not tenant.is_active or user is None or ticket is None:
                return json.dumps(
                    {"status": "no_memory_scope", "memories": []},
                    ensure_ascii=False,
                )

        documents = await asyncio.to_thread(
            read_scoped_preferences, tenant_id, customer_id
        )
        source_ids = {
            int(metadata["source_ticket_id"])
            for document in documents
            if (
                (metadata := document["metadata"]).get("tenant_id") == tenant_id
                and metadata.get("customer_id") == customer_id
                and str(metadata.get("source_ticket_id", "")).isdecimal()
            )
        }
        if not source_ids:
            return json.dumps({"status": "no_match", "memories": []}, ensure_ascii=False)

        async with AsyncSessionLocal() as db:
            owned_sources = set(
                (
                    await db.execute(
                        select(Ticket.id).where(
                            Ticket.id.in_(source_ids),
                            Ticket.tenant_id == tenant_id,
                            Ticket.customer_id == customer_id,
                        )
                    )
                ).scalars().all()
            )

        memories = []
        for document in documents:
            metadata = document["metadata"]
            source_id = metadata.get("source_ticket_id")
            if (
                metadata.get("tenant_id") == tenant_id
                and metadata.get("customer_id") == customer_id
                and metadata.get("type") == "user_episodic_memory"
                and str(source_id).isdecimal()
                and int(source_id) in owned_sources
            ):
                memories.append(
                    {
                        "timestamp": str(metadata.get("timestamp", "")),
                        "source_ticket_id": str(source_id),
                        "text": document["text"],
                    }
                )
        memories.sort(key=lambda item: item["timestamp"], reverse=True)
        return json.dumps(
            {"status": "found" if memories else "no_match", "memories": memories[:5]},
            ensure_ascii=False,
        )
    except Exception:
        logger.exception("Tenant-scoped customer memory retrieval failed")
        return json.dumps({"status": "unavailable", "memories": []}, ensure_ascii=False)

# ==========================================# 4. 🧰 异步耗时微服务工具 (查订单/日志)
# ==========================================
@tool
async def submit_async_support_task(
    action: str = Field(..., description="仅支持 query_logs；退款必须经过 process_large_refund 人工审批流程"),
    order_id: str = Field(default="", description="相关的订单号（如果有的话）"),
    user_id: str = Field(default="", description="相关的客户ID（如果有的话）")
) -> str:
    """将日志分析排入后台任务；该工具不执行退款。"""
    if action != "query_logs":
        return (
            "❌ 此异步支持任务仅支持日志查询。所有符合退款条件的退款申请，"
            "无论金额大小，都必须通过 process_large_refund 进入人工审批流程。"
        )

    from app.workers.tasks import async_support_task

    params = {}
    if order_id:
        params["order_id"] = order_id
    if user_id:
        params["user_id"] = user_id

    task = async_support_task.delay(action, params)
    return (
        f"日志分析任务已排入后台队列，Task ID 为: {task.id}。"
        "请稍后使用 check_async_task_status 查询结果。"
    )


@tool
async def check_async_task_status(task_id: str) -> str:
    """
    当客户提供流水号询问进度，或者你刚刚排入了异步任务需要等待几秒查询结果时，调用此工具。
    """
    print(f"🔍 [查询工具] 正在去 Redis 查找流水号: {task_id} 的结果...")
    
    redis_client = Redis.from_url(settings.REDIS_URL)
    # 🚨 注意：这里的 Key 前缀 "async_result:" 必须与 Step 2 对应！
    result = await redis_client.get(f"async_result:{task_id}")
    
    # 🌟 核心升级：内部主动等待机制 (最大等待 15 秒)
    # 替大模型扛下等待的时间，避免大模型因为没耐心而崩溃，还能省下巨量 Token 费用！
    for attempt in range(1, 8):
        # 注意：这里的 key 前缀一定要和 tasks.py 里保持一致
        result = await redis_client.get(f"async_result:{task_id}") 
        
        if result is not None:
            # 取到了！立刻返回给大模型
            return f"✅ 查询结果：{result.decode('utf-8')}"
            
        print(f"⏳ [查询工具] 结果未就绪 (第 {attempt} 次尝试)，挂起 2 秒...")
        await asyncio.sleep(2) # 让 Python 挂起，不阻塞其他并发任务
        
    # 如果等了 14 秒还没好，再让大模型去安抚客户
    return "⚠️ 查询结果：后台微服务依然在拼命计算中。请告诉客户由于系统繁忙，还需要再耐心等一会儿。"

# ==========================================
# 5. 🧰 退款人工审批工具 (HITL)
# ==========================================
@tool
async def process_large_refund(
    order_id: str = Field(..., description="需要退款的订单号，必须属于当前工单"),
    amount: float = Field(..., description="退款金额，必须与数据库中的订单金额一致"),
    config: RunnableConfig = None,
):
    """Create a durable human review, then execute only a recorded approval."""
    configurable = (config or {}).get("configurable", {})
    tenant_id = configurable.get("tenant_id")
    customer_id = configurable.get("customer_id")
    ticket_id_value = configurable.get("ticket_id")
    if (
        not isinstance(tenant_id, str) or not tenant_id.strip()
        or not isinstance(customer_id, str) or not customer_id.strip()
    ):
        return "【系统物理拦截】缺少可信租户或客户上下文，无法处理退款。"
    try:
        ticket_id = int(ticket_id_value)
        if ticket_id <= 0:
            raise ValueError("Invalid ticket ID")
    except (TypeError, ValueError):
        return "【系统物理拦截】缺少有效工单上下文，无法处理退款。"

    authorization = configurable.get("authorization")
    has_authorization = is_valid_refund_authorization(
        authorization,
        ticket_id=ticket_id,
        tenant_id=tenant_id,
        order_id=order_id,
        amount=amount,
    )

    try:
        with SyncSessionLocal() as db_session:
            ticket = (
                db_session.execute(
                    select(Ticket).where(
                        Ticket.id == ticket_id,
                        Ticket.tenant_id == tenant_id,
                        Ticket.customer_id == customer_id,
                    ).with_for_update()
                )
                .scalars()
                .first()
            )
            if ticket is None:
                return "【系统物理拦截】当前工单不可用，无法执行退款。"
            if not ticket.order_id or ticket.order_id != order_id:
                return "【系统物理拦截】订单与当前工单不匹配，已拒绝执行。"

            order = (
                db_session.execute(
                    select(Order).where(
                        Order.tenant_id == tenant_id,
                        Order.order_id == order_id,
                        Order.customer_id == customer_id,
                    ).with_for_update()
                )
                .scalars()
                .first()
            )
            if order is None or ticket.customer_id != order.customer_id:
                return "【系统物理拦截】数据库中查不到当前客户的关联订单。"
            customer = (
                db_session.execute(
                    select(User).where(
                        User.tenant_id == tenant_id,
                        User.username == customer_id,
                    )
                )
                .scalars()
                .first()
            )
            if customer is None or not customer.is_active:
                return "【系统物理拦截】该账号不可用，禁止执行退款。"
            if order.status == "shipped":
                return "【系统物理拦截】已发货禁止直接退款，请执行物流拦截 SOP。"
            if order.status == "refunded":
                return f"订单 {order_id} 已经退款，无需重复执行。"
            if order.status == "pending":
                return "【系统物理拦截】订单记录为待付款，不能发起退款；如客户称已扣款，请人工核实支付记录。"
            if order.status not in {"paid", "out_of_stock"}:
                return "【系统物理拦截】订单状态不符合当前退款资格。"
            if (
                not isfinite(float(order.amount))
                or not isfinite(float(amount))
                or round(float(order.amount), 2) != round(float(amount), 2)
                or float(amount) < 0
            ):
                return "【系统物理拦截】退款金额与数据库订单金额不一致，已拒绝执行。"

            review = (
                db_session.execute(
                    select(ReviewRequest).where(
                        ReviewRequest.ticket_id == ticket_id,
                        ReviewRequest.tenant_id == tenant_id,
                    ).with_for_update()
                )
                .scalars()
                .first()
            )
            if review is not None and (
                review.customer_id != customer_id
                or review.order_id != order_id
                or round(float(review.amount), 2) != round(float(order.amount), 2)
            ):
                return "【系统物理拦截】审核请求与工单或订单归属不一致。"

            if not has_authorization:
                if review is not None:
                    if review.status == "rejected":
                        return "该退款申请已由人工驳回；如需复核，请重新联系人工客服。"
                    if review.status == "approved":
                        return "人工审核已批准，正在等待系统继续处理。"
                    return "该退款申请已进入人工审核，请等待处理。"
                if ticket.status not in {"pending", "processing", "escalated"}:
                    return "【系统物理拦截】当前工单状态不允许创建退款审核。"
                review = ReviewRequest(
                    tenant_id=tenant_id,
                    ticket_id=ticket_id,
                    customer_id=customer_id,
                    order_id=order_id,
                    amount=Decimal(str(order.amount)).quantize(Decimal("0.01")),
                    reason="客户申请退款，订单资格已通过受限工具校验",
                    status="pending",
                )
                db_session.add(review)
                db_session.flush()
                db_session.add(ReviewAuditEvent(
                    review_id=review.id,
                    tenant_id=tenant_id,
                    actor_id=customer_id,
                    action="requested",
                    reason=None,
                ))
                trace_headers = {}
                inject(trace_headers)
                outbox = NotificationOutbox(
                    review_id=review.id,
                    tenant_id=tenant_id,
                    event_key=f"review:{review.id}:requested",
                    event_type="review_requested",
                    payload={"review_id": review.id, "ticket_id": ticket_id,
                             "trace_headers": trace_headers},
                    status="pending",
                    attempts=0,
                )
                db_session.add(outbox)
                db_session.flush()
                outbox_id = outbox.id
                ticket.status = "escalated"
                ticket.resolution = "等待高级客服人工审批退款"
                db_session.commit()
                try:
                    from app.workers.tasks import dispatch_review_outbox
                    dispatch_review_outbox.apply_async(kwargs={"outbox_id": outbox_id})
                except Exception:
                    logger.exception("Review notification enqueue failed for ticket %s", ticket_id)
                return "该退款申请已转交人工审核；批准前不会更新退款状态。"

            if (
                review is None
                or review.status != "approved"
                or review.id != authorization["review_id"]
                or review.decision_actor_id != authorization["actor_id"]
                or ticket.status != "escalated"
            ):
                return "【系统物理拦截】缺少匹配的持久化批准记录，已拒绝执行。"

            order.status = "refunded"
            ticket.status = "resolved"
            ticket.resolution = (
                f"订单 {order_id} 的退款审批已通过；原型数据库状态已更新为 refunded "
                f"（{float(order.amount):.2f} 元，实际支付未接入）。"
            )
            db_session.add(ReviewAuditEvent(
                review_id=review.id,
                tenant_id=tenant_id,
                actor_id=review.decision_actor_id,
                action="refund_applied",
                reason=None,
            ))
            trace_headers = {}
            inject(trace_headers)
            outbox = NotificationOutbox(
                review_id=review.id,
                tenant_id=tenant_id,
                event_key=f"review:{review.id}:refund_completed",
                event_type="refund_completed",
                payload={"review_id": review.id, "ticket_id": ticket_id,
                             "trace_headers": trace_headers},
                status="pending",
                attempts=0,
            )
            db_session.add(outbox)
            db_session.flush()
            outbox_id = outbox.id
            db_session.commit()
            try:
                from app.workers.tasks import dispatch_review_outbox
                dispatch_review_outbox.apply_async(kwargs={"outbox_id": outbox_id})
            except Exception:
                logger.exception("Refund completion notification enqueue failed for ticket %s", ticket_id)
    except Exception as exc:
        logger.exception("Refund workflow failed for ticket %s", ticket_id)
        return f"退款处理失败：{type(exc).__name__}"

    return (
        f"工单审批已验证，订单 {order_id} 的原型数据库状态已更新为 refunded；"
        "实际支付网关尚未接入。"
    )

import hashlib

@tool
async def execute_mcp_database_query(
    sql_query: str = Field(..., description="需要执行的只读 PostgreSQL 查询语句。严禁使用 UPDATE/DELETE/DROP。")
) -> str:
    """
    当需要排查复杂的订单状态、核对金额或查询用户历史时，调用此工具。
    """
    # 🚨 🆕 新增物理防线：硬拦截写操作，防止浪费队列资源
    forbidden_keywords = ["update ", "delete ", "drop ", "insert ", "alter ", "truncate ", "grant ", "revoke "]
    lower_sql = sql_query.lower()
    if any(keyword in lower_sql for keyword in forbidden_keywords):
        return "❌ 严重警告：物理防线已触发！当前连接的是只读副本，系统严禁执行任何写入或修改操作。请修正你的 SQL 为纯 SELECT 语句。"

    print(f"🚀 [Agent 调度] 拦截到大模型的 SQL 语句:\n{sql_query}")
    
    redis_client = Redis.from_url(settings.REDIS_URL)
    
    # 🌟 1. 生成 SQL 的唯一指纹 (MD5 Hash)
    sql_hash = hashlib.md5(sql_query.strip().encode('utf-8')).hexdigest()
    idempotency_key = f"mcp_idemp:{sql_hash}"
    
    # 🌟 2. 查验 Redis：看看这个 SQL 是不是刚刚才发过？
    existing_task_id = await redis_client.get(idempotency_key)
    
    if existing_task_id:
        task_id_str = existing_task_id.decode('utf-8')
        print(f"🛡️ [防重拦截] 发现重复提交的 SQL！复用旧的任务 ID: {task_id_str}")
        return f"✅ SQL 查询已在后台执行中（触发防重复提交保护），复用旧的 Task ID: {task_id_str}。请使用 check_async_task_status 查询结果。"

    # 3. 如果是全新的 SQL，正常派发给 Celery
    from app.workers.tasks import async_mcp_runner 
    task = async_mcp_runner.delay(
        tool_name="query", 
        tool_args={"sql": sql_query} 
    )
    
    # 🌟 4. 把这个 SQL 指纹存入 Redis，锁定 60 秒
    # 意味着 60 秒内，大模型再发一模一样的 SQL，都会被上面的逻辑拦截
    await redis_client.setex(idempotency_key, 60, task.id)
    
    return f"✅ SQL 查询任务已全新排入后台队列，Task ID 为: {task.id}。请稍后使用 check_async_task_status 查询结果。"
