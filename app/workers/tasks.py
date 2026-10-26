# app/workers/tasks.py
import sys
import asyncio
import logging
import os
from tenacity import AsyncRetrying, stop_after_attempt, wait_exponential, retry_if_exception_type
from langgraph.errors import GraphRecursionError
import redis as sync_redis # 引入同步库，取个别名防止和异步 redis 冲突
from app.core.config import settings
from app.core.task_events import publish_task_event
import time

# 🚨 架构师修正：由于 Python 3.14+ 默认的 Windows 异步引擎已极其稳健，
# 官方宣布在 3.16 将彻底移除旧版 WindowsSelectorEventLoopPolicy 切换补丁，故在此彻底卸载。

# ==========================================
# 🚨 针对 Windows 环境的异步底层补丁
# 必须放在文件顶部，确保 Celery Worker 启动时优先加载
# ==========================================
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from app.workers.celery_app import celery_app 
from langchain_core.messages import HumanMessage
from app.agent.graph import get_agent_app, close_pool
from app.agent.authorization import is_valid_refund_authorization
from app.agent.reply_guard import claims_human_handoff, claims_review_submission, claims_refund_completion, ground_pending_refund_reply
from app.agent.state import AgentAuthorization, AgentContext
from app.core.identity import customer_ref
from app.core.logging import JSONFormatter, bind_log_context, reset_log_context
from app.core.tracing import setup_tracing
from app.models.ticket import Ticket
from app.models.tenant import Tenant
from app.models.user import User
from app.workers.progress import AgentProgressCallback
from opentelemetry import propagate, trace
from opentelemetry.context import attach, detach

# 🚨 使用最新版官方异步 Redis 客户端！
from redis.asyncio import Redis

#MCP相关import
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
import json
from datetime import datetime, timezone, timedelta

from app.core.db import AsyncSessionLocal, engine as async_db_engine
from app.models.order import Order
from app.models.review import ReviewRequest, ReviewAuditEvent, NotificationOutbox, utc_now
from app.core.review_events import approved_task_id, review_event_key, REVIEW_EVENT_TTL_SECONDS
from app.core.task_events import task_owner_key, task_owner_identity, TASK_EVENT_TTL_SECONDS
from sqlalchemy import select, or_, func, text

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


logger = logging.getLogger("celery_worker")
logger.setLevel(logging.INFO)
if not any(getattr(handler, "_agent_json_handler", False) for handler in logger.handlers):
    handler = logging.StreamHandler()
    handler.setFormatter(JSONFormatter())
    handler._agent_json_handler = True
    logger.addHandler(handler)
logger.propagate = False
setup_tracing()
tracer = trace.get_tracer(__name__)
DB_URL = settings.DATABASE_URL
sync_redis_client = sync_redis.from_url(settings.REDIS_URL, decode_responses=True)

# ==========================================
# 🚨 核心修复 1：为 Celery 创建独立的【同步】数据库引擎
# ==========================================
# 必须把 asyncpg 协议替换为 psycopg 协议 (SQLAlchemy 2.0 推荐的同步驱动)
# 这样 Celery 就会使用自己独立的同步连接池，彻底告别 Event Loop 冲突！
SYNC_DB_URL = settings.DATABASE_URL.replace("postgresql+asyncpg://", "postgresql+psycopg://")

sync_engine = create_engine(
    SYNC_DB_URL,
    pool_pre_ping=True,  # 每次使用前 ping 一下，防止拿到断开的死连接
    pool_size=5,         # 同步连接池大小
    max_overflow=10      # 允许溢出的连接数
)

# 创建同步 Session 工厂
SyncSessionLocal = sessionmaker(bind=sync_engine, autocommit=False, autoflush=False)

try:
    from openai import OpenAIError
    RETRY_EXCEPTIONS = (asyncio.TimeoutError, ConnectionError, OpenAIError)
except ImportError:
    RETRY_EXCEPTIONS = (asyncio.TimeoutError, ConnectionError, Exception)

# ==========================================
# 🚨 3. 自研工业级：分布式异步熔断器 (完美替代 aiobreaker)
class CircuitBreakerOpenError(Exception):
    """专属熔断异常，用来穿透 Tenacity 的防线"""
    pass

class AsyncRedisCircuitBreaker:
    def __init__(self, redis_conn, fail_max=5, reset_timeout=60):
        self.redis = redis_conn
        self.fail_max = fail_max
        self.reset_timeout = reset_timeout
        self.fails_key = "circuit_breaker:fails"
        self.open_key = "circuit_breaker:open"

    async def call(self, func, *args, **kwargs):
        # 探路：如果 Redis 里存在 open_key，说明处于熔断期，瞬间拦截！
        if await self.redis.get(self.open_key):
            raise CircuitBreakerOpenError("Circuit breaker is OPEN")

        try:
            # 真正去执行 Agent
            result = await func(*args, **kwargs)
            # 成功则清零失败计数
            await self.redis.delete(self.fails_key)
            return result
        except CircuitBreakerOpenError:
            raise # 熔断拦截直接抛出，Tenacity 看到它不在白名单，瞬间放行！
        except Exception as e:
            # 遇到真实报错，Redis 计数器 +1
            fails = await self.redis.incr(self.fails_key)
            if fails >= self.fail_max:
                # 满 5 次，写下一个存活 60 秒的熔断标志！
                await self.redis.setex(self.open_key, self.reset_timeout, "1")
                await self.redis.delete(self.fails_key) # 清空计数器为下一轮准备
                logger.critical("🚨 [熔断器] circuit_state: closed -> open")
            
            # 向外抛出交由 Tenacity 处理重试
            raise e
# ==========================================


# ==========================================
# 🚨 4. Tenacity 安全重试层
retry_strategy = AsyncRetrying(
    stop=stop_after_attempt(3), 
    wait=wait_exponential(multiplier=1, min=2, max=10), 
    retry=retry_if_exception_type(RETRY_EXCEPTIONS), # 只重试白名单异常
    reraise=True
)
# ==========================================


# ==========================================
# 🚨 5. Celery Worker 主入口
def approved_refund_result(
    authorization: dict,
    *,
    tenant_id: str,
    customer_id: str,
    ticket_id: int,
) -> tuple[str, bool]:
    """Derive the customer message from committed state, not the LLM wording."""
    with SyncSessionLocal() as db:
        review = db.get(ReviewRequest, authorization["review_id"])
        ticket = db.get(Ticket, ticket_id)
        order = db.execute(select(Order).where(
            Order.tenant_id == tenant_id,
            Order.customer_id == customer_id,
            Order.order_id == authorization["order_id"],
        )).scalars().first()
        completed = (
            review is not None
            and review.tenant_id == tenant_id
            and review.customer_id == customer_id
            and review.ticket_id == ticket_id
            and review.order_id == authorization["order_id"]
            and review.status == "approved"
            and ticket is not None
            and ticket.tenant_id == tenant_id
            and ticket.customer_id == customer_id
            and ticket.status == "resolved"
            and order is not None
            and order.status == "refunded"
        )
    if completed:
        return (
            f"人工审核已通过，订单 {authorization['order_id']} 的原型数据库状态已更新为 refunded。"
            "系统未接入支付网关，未发起真实退款或打款。",
            True,
        )
    return (
        "人工审核已批准，但原型订单处理尚未完成，请人工复核。"
        "系统未接入支付网关，未发起真实退款或打款。",
        False,
    )


def review_submission_recorded(*, tenant_id: str, customer_id: str, ticket_id: int) -> bool:
    """Check the committed, scoped review before confirming submission to a customer."""
    with SyncSessionLocal() as db:
        ticket = db.execute(select(Ticket).where(
            Ticket.id == ticket_id,
            Ticket.tenant_id == tenant_id,
            Ticket.customer_id == customer_id,
        )).scalars().first()
        if ticket is None or not ticket.order_id:
            return False
        review = db.execute(select(ReviewRequest).where(
            ReviewRequest.ticket_id == ticket_id,
            ReviewRequest.tenant_id == tenant_id,
            ReviewRequest.customer_id == customer_id,
            ReviewRequest.order_id == ticket.order_id,
        )).scalars().first()
        return review is not None


def pending_refund_is_unsettled(*, tenant_id: str, customer_id: str, ticket_id: int) -> bool:
    """Return true only for a scoped pending review whose order is not refunded."""
    with SyncSessionLocal() as db:
        ticket = db.execute(select(Ticket).where(
            Ticket.id == ticket_id,
            Ticket.tenant_id == tenant_id,
            Ticket.customer_id == customer_id,
        )).scalars().first()
        if ticket is None or not ticket.order_id:
            return False
        review = db.execute(select(ReviewRequest).where(
            ReviewRequest.ticket_id == ticket_id,
            ReviewRequest.tenant_id == tenant_id,
            ReviewRequest.customer_id == customer_id,
            ReviewRequest.order_id == ticket.order_id,
        )).scalars().first()
        order = db.execute(select(Order).where(
            Order.tenant_id == tenant_id,
            Order.customer_id == customer_id,
            Order.order_id == ticket.order_id,
        )).scalars().first()
        return (
            review is not None
            and review.status == "pending"
            and order is not None
            and order.status in {"paid", "out_of_stock"}
        )


def human_handoff_recorded(*, tenant_id: str, customer_id: str, ticket_id: int) -> bool:
    """An ordinary pending ticket has not been queued for human handling."""
    with SyncSessionLocal() as db:
        ticket = db.execute(select(Ticket).where(
            Ticket.id == ticket_id,
            Ticket.tenant_id == tenant_id,
            Ticket.customer_id == customer_id,
        )).scalars().first()
        return ticket is not None and ticket.status in {"escalated", "resolved"}


def verified_pending_cancel_reply(
    *, tenant_id: str, customer_id: str, ticket_id: int, topic: str,
) -> str | None:
    """Use a factual reply for unpaid cancellation, which has no execution tool."""
    if "取消" not in topic and "cancel" not in topic.lower():
        return None
    with SyncSessionLocal() as db:
        ticket = db.execute(select(Ticket).where(
            Ticket.id == ticket_id,
            Ticket.tenant_id == tenant_id,
            Ticket.customer_id == customer_id,
        )).scalars().first()
        if ticket is None or not ticket.order_id:
            return None
        order = db.execute(select(Order).where(
            Order.tenant_id == tenant_id,
            Order.customer_id == customer_id,
            Order.order_id == ticket.order_id,
        )).scalars().first()
        if order is None or order.status != "pending":
            return None
        return (
            f"订单 {order.order_id} 当前记录为待付款。"
            "我没有取消订单的执行工具，不能直接为您取消；本次也没有发起退款。"
            "请联系人工客服核实取消事宜。如果您已经被扣款，也请人工一并核对支付记录。"
        )

@celery_app.task(bind=True, name="process_agent_task")
def process_agent_task(
    self,
    topic: str,
    ticket_id: str,
    user_id: str,
    tenant_id: str,
    authorization: AgentAuthorization | None = None,
    memory_facts: list | None = None,
):
    headers = self.request.headers or {}
    ctx = propagate.extract(headers)
    context_token = attach(ctx)
    log_token = None

    try:
        with tracer.start_as_current_span("celery_worker_execution") as span:
            if (
                not isinstance(ticket_id, str)
                or not ticket_id.isdecimal()
                or int(ticket_id) <= 0
                or not isinstance(user_id, str)
                or not user_id.strip()
                or not isinstance(tenant_id, str)
                or not tenant_id.strip()
            ):
                raise ValueError("Missing or invalid task identity")
            ticket_pk = int(ticket_id)
            task_id = str(self.request.id or "")
            request_id = str(headers.get("x-request-id") or task_id)

            # Queue arguments are correlation data, not authorization. Recheck
            # the tenant and ticket before exposing any Agent tool.
            with SyncSessionLocal() as identity_db:
                tenant = identity_db.get(Tenant, tenant_id)
                ticket = identity_db.get(Ticket, ticket_pk)
                customer = identity_db.execute(
                    select(User).where(
                        User.tenant_id == tenant_id,
                        User.username == user_id,
                    )
                ).scalars().first()
                if (
                    tenant is None
                    or not tenant.is_active
                    or ticket is None
                    or ticket.tenant_id != tenant_id
                    or ticket.customer_id != user_id
                    or customer is None
                    or not customer.is_active
                ):
                    raise PermissionError("Task identity does not match active tenant, customer and ticket ownership")

            customer_ref_value = customer_ref(tenant_id, user_id)
            span.set_attribute("app.tenant_id", tenant_id)
            span.set_attribute("app.customer_ref", customer_ref_value)
            span.set_attribute("app.ticket_id", ticket_id)
            span.set_attribute("app.request_id", request_id)
            span.set_attribute("app.task_id", task_id)
            log_token = bind_log_context(
                tenant_id=tenant_id,
                customer_ref=customer_ref_value,
                ticket_id=ticket_id,
                request_id=request_id,
                task_id=task_id,
            )
            logger.info("Celery Agent task started")

            async def run_agent():
                # ==========================================
                # 💡 修复点 1：在当前 Event Loop 内实例化 Redis 和熔断器
                # ==========================================
                local_redis = Redis.from_url(settings.REDIS_URL)
                await publish_task_event(local_redis, task_id, "started", "客服 Agent 已开始处理工单")
                progress_callback = AgentProgressCallback(local_redis, task_id)
                local_breaker = AsyncRedisCircuitBreaker(local_redis, fail_max=5, reset_timeout=60)
                
                # 将安全调用包裹在局部函数里
                async def safe_ainvoke(agent_app, inputs, config):
                    async for attempt in retry_strategy:
                        with attempt:
                            return await local_breaker.call(agent_app.ainvoke, inputs, config=config)

                try:
                    agent_app = await get_agent_app()
                    request_context: AgentContext = {
                        "request_id": request_id,
                        "ticket_id": ticket_id,
                        # Checkpoint remains ticket-scoped; long-term memory is tenant/customer-scoped.
                        "conversation_id": ticket_id,
                        "memory_scope_id": ticket_id,
                        "customer_id": user_id,
                        "tenant_id": tenant_id,
                    }
                    if authorization is not None:
                        if is_valid_refund_authorization(
                            authorization,
                            ticket_id=ticket_id,
                            tenant_id=tenant_id,
                        ):
                            request_context["authorization"] = authorization
                        else:
                            logger.warning(
                                "Discarding invalid refund authorization context, ticket_id: %s",
                                ticket_id,
                            )
                    inputs = {
                        "messages": [HumanMessage(content=topic)],
                        "context": request_context,
                    }
                    
                    config = {
                        "configurable": {
                            "thread_id": request_context["conversation_id"],
                            **request_context,
                        },
                        "recursion_limit": 25,
                        "callbacks": [progress_callback],
                    }
                    
                    final_state = await safe_ainvoke(agent_app, inputs, config)
                    last_msg = final_state["messages"][-1].content
                    task_status = "success"
                    postflight_actions = []
                    if "authorization" not in request_context:
                        pending_reply = await asyncio.to_thread(
                            verified_pending_cancel_reply,
                            tenant_id=tenant_id,
                            customer_id=user_id,
                            ticket_id=ticket_pk,
                            topic=topic,
                        )
                        if pending_reply is not None:
                            last_msg = pending_reply
                            postflight_actions.append("pending_cancel_verified_reply")
                    if "authorization" in request_context:
                        last_msg, completed = await asyncio.to_thread(
                            approved_refund_result,
                            request_context["authorization"],
                            tenant_id=tenant_id,
                            customer_id=user_id,
                            ticket_id=ticket_pk,
                        )
                        task_status = "success" if completed else "error"
                    elif claims_review_submission(str(last_msg)):
                        recorded = await asyncio.to_thread(
                            review_submission_recorded,
                            tenant_id=tenant_id,
                            customer_id=user_id,
                            ticket_id=ticket_pk,
                        )
                        if not recorded:
                            logger.error("Agent claimed an unrecorded review for ticket %s", ticket_id)
                            postflight_actions.append("unrecorded_review_claim_blocked")
                            last_msg = (
                                "抱歉，本次退款申请尚未提交人工审核。"
                                "请由人工客服核实后继续处理；目前不能确认退款已办理。"
                            )
                            task_status = "error"
                    if (
                        "authorization" not in request_context
                        and task_status == "success"
                        and claims_refund_completion(str(last_msg))
                    ):
                        pending_unsettled = await asyncio.to_thread(
                            pending_refund_is_unsettled,
                            tenant_id=tenant_id,
                            customer_id=user_id,
                            ticket_id=ticket_pk,
                        )
                        if pending_unsettled:
                            logger.warning(
                                "Replacing ambiguous refund-completion wording for ticket %s",
                                ticket_id,
                            )
                            postflight_actions.append("pending_review_status_grounded_reply")
                            last_msg = ground_pending_refund_reply(str(last_msg))
                    if (
                        "authorization" not in request_context
                        and task_status == "success"
                        and claims_human_handoff(str(last_msg))
                    ):
                        recorded = await asyncio.to_thread(
                            human_handoff_recorded,
                            tenant_id=tenant_id,
                            customer_id=user_id,
                            ticket_id=ticket_pk,
                        )
                        if not recorded:
                            logger.error("Agent promised an unrecorded handoff for ticket %s", ticket_id)
                            postflight_actions.append("unrecorded_handoff_claim_blocked")
                            last_msg = (
                                "抱歉，当前工单尚未转交人工。"
                                "这项请求需要人工客服核实，请通过客服渠道联系人工客服。"
                            )
                            task_status = "error"
                    memory_write_task_id = None
                    if authorization is None and memory_facts:
                        try:
                            memory_headers = {}
                            propagate.inject(memory_headers)
                            memory_headers["x-request-id"] = request_id
                            memory_job = extract_and_store_memory.apply_async(
                                kwargs={
                                    "tenant_id": tenant_id,
                                    "customer_id": user_id,
                                    "source_ticket_id": ticket_id,
                                    "facts": memory_facts,
                                },
                                headers=memory_headers,
                            )
                            memory_write_task_id = memory_job.id
                        except Exception:
                            logger.exception("Failed to enqueue local customer memory")
                    event_name = "completed" if task_status == "success" else "failed"
                    await publish_task_event(
                        local_redis, task_id, event_name,
                        "客服 Agent 已完成处理" if task_status == "success" else "客服 Agent 处理未完成",
                        result=last_msg,
                    )
                    return {
                        "status": task_status,
                        "output": last_msg,
                        "postflight_actions": postflight_actions,
                        "memory_write_task_id": memory_write_task_id,
                    }

                except CircuitBreakerOpenError:
                    logger.warning(f"🛡️ [服务降级] LLM 熔断器已开启，直接返回兜底结果, ticket_id: {ticket_id}")
                    fallback = "【系统降级】当前 AI 思考引擎负载过高，请稍后再试。"
                    await publish_task_event(
                        local_redis, task_id, "degraded", "AI 服务繁忙，已返回降级提示", result=fallback
                    )
                    return {"status": "success", "output": fallback}
                    
                except GraphRecursionError:
                    logger.error(f"❌ [Celery] Agent 思考陷入死循环，已被强行掐断！ticket_id: {ticket_id}")
                    message = "❌ 抱歉，排障过程过于复杂，系统已停止推理。"
                    await publish_task_event(
                        local_redis, task_id, "failed", "Agent 处理步骤过多，系统已停止本次任务", result=message
                    )
                    return {"status": "error", "output": message}
                    
                except Exception as e:
                    logger.error("Celery Agent execution failed: %s", type(e).__name__)
                    message = f"内环异常: {type(e).__name__}"
                    await publish_task_event(
                        local_redis, task_id, "failed", "Agent 处理失败，请稍后重试", result=message
                    )
                    return {"status": "error", "output": message}
                    
                finally:
                    try:
                        await close_pool()
                    finally:
                        try:
                            # asyncio.run creates a new event loop for each Celery
                            # task. Do not reuse asyncpg pooled connections from
                            # the loop that is about to close.
                            await async_db_engine.dispose()
                        finally:
                            await local_redis.aclose()

            return asyncio.run(run_agent())

    finally:
        if log_token is not None:
            reset_log_context(log_token)
        detach(context_token)


# ==========================================
# 🚨 6. 长期记忆异步归档任务 (Episodic Memory)
# ==========================================
from app.agent.customer_memory import store_preferences

@celery_app.task(bind=True, name="app.workers.tasks.extract_and_store_memory")
def extract_and_store_memory(
    self,
    tenant_id: str,
    customer_id: str,
    source_ticket_id: str,
    facts: list,
):
    """Persist locally normalized preferences with verified identity and trace context."""
    headers = self.request.headers or {}
    context_token = attach(propagate.extract(headers))
    log_token = None
    try:
        with tracer.start_as_current_span("customer_memory_store") as span:
            if (
                not isinstance(tenant_id, str) or not tenant_id.strip()
                or not isinstance(customer_id, str) or not customer_id.strip()
                or not isinstance(source_ticket_id, str) or not source_ticket_id.isdecimal()
                or not isinstance(facts, list)
            ):
                raise ValueError("Missing or invalid memory identity")
            request_id = str(headers.get("x-request-id") or self.request.id or "")
            log_token = bind_log_context(
                tenant_id=tenant_id,
                customer_ref=customer_ref(tenant_id, customer_id),
                ticket_id=source_ticket_id,
                request_id=request_id,
                task_id=str(self.request.id or ""),
            )
            span.set_attribute("app.tenant_id", tenant_id)
            span.set_attribute("app.customer_ref", customer_ref(tenant_id, customer_id))
            span.set_attribute("app.ticket_id", source_ticket_id)
            span.set_attribute("app.request_id", request_id)

            normalized = []
            for fact in facts[:3]:
                if (
                    not isinstance(fact, (list, tuple))
                    or len(fact) != 2
                    or not all(isinstance(value, str) for value in fact)
                ):
                    raise ValueError(
                        "Memory fact must be a normalized category and text pair"
                    )
                normalized.append(tuple(fact))

            with SyncSessionLocal() as db:
                tenant = db.get(Tenant, tenant_id)
                ticket = db.get(Ticket, int(source_ticket_id))
                customer = db.execute(
                    select(User.id).where(
                        User.tenant_id == tenant_id,
                        User.username == customer_id,
                        User.is_active.is_(True),
                    )
                ).scalar_one_or_none()
                if (
                    tenant is None or not tenant.is_active
                    or customer is None or ticket is None
                    or ticket.tenant_id != tenant_id
                    or ticket.customer_id != customer_id
                ):
                    raise PermissionError(
                        "Memory identity does not match active tenant, customer and ticket"
                    )

                # Serialize writes for one customer. Qdrant's upsert is unconditional;
                # without this lock, a delayed old job can race with a revocation.
                db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:scope, 0))"), {
                    "scope": f"{tenant_id}|{customer_id}",
                })
                count = store_preferences(
                    tenant_id, customer_id, source_ticket_id, normalized
                )
                db.commit()
            logger.info("Stored %s verified local customer preferences", count)
            return {"stored": count}
    finally:
        if log_token is not None:
            reset_log_context(log_token)
        detach(context_token)


RECOVERY_INITIAL_DELAY = timedelta(seconds=60)
RECOVERY_MAX_ATTEMPTS = 3


def _recovery_due(attempts: int, last_attempt, now) -> bool:
    if attempts >= RECOVERY_MAX_ATTEMPTS:
        return False
    if last_attempt is None:
        return True
    return last_attempt <= now - timedelta(seconds=60 * (2 ** max(attempts - 1, 0)))


@celery_app.task(name="app.workers.tasks.recover_approved_refund")
def recover_approved_refund(review_id: int):
    """Apply a stalled approved prototype refund without depending on LLM output."""
    now = utc_now()
    with SyncSessionLocal() as db:
        review = db.execute(
            select(ReviewRequest).where(ReviewRequest.id == review_id).with_for_update()
        ).scalars().first()
        if review is None or review.status != "approved":
            return {"status": "not_approved"}
        ticket = db.get(Ticket, review.ticket_id)
        tenant = db.get(Tenant, review.tenant_id)
        if (
            ticket is None or ticket.tenant_id != review.tenant_id
            or ticket.customer_id != review.customer_id
            or ticket.status != "escalated"
            or tenant is None or not tenant.is_active
            or review.decided_at is None
            or review.decided_at > now - RECOVERY_INITIAL_DELAY
        ):
            return {"status": "not_stalled"}
        attempts, last_attempt = db.execute(
            select(func.count(ReviewAuditEvent.id), func.max(ReviewAuditEvent.created_at))
            .where(
                ReviewAuditEvent.review_id == review.id,
                ReviewAuditEvent.tenant_id == review.tenant_id,
                ReviewAuditEvent.action == "recovery_attempt",
            )
        ).one()
        if not _recovery_due(attempts, last_attempt, now):
            return {"status": "manual_review" if attempts >= RECOVERY_MAX_ATTEMPTS else "backoff"}
        authorization = {
            "source": "admin_ticket_approval",
            "operation": "large_refund",
            "actor_id": review.decision_actor_id,
            "review_id": review.id,
            "tenant_id": review.tenant_id,
            "ticket_id": str(review.ticket_id),
            "order_id": review.order_id,
            "amount": float(review.amount),
        }
        identity = {
            "tenant_id": review.tenant_id,
            "customer_id": review.customer_id,
            "ticket_id": str(review.ticket_id),
        }
        valid_authorization = is_valid_refund_authorization(
            authorization, ticket_id=review.ticket_id,
            tenant_id=review.tenant_id, order_id=review.order_id,
            amount=float(review.amount),
        )
        db.add(ReviewAuditEvent(
            review_id=review.id, tenant_id=review.tenant_id,
            actor_id="system:recovery", action="recovery_attempt",
            reason=f"Attempt {attempts + 1}: approved refund remains incomplete",
        ))
        if not valid_authorization:
            db.add(ReviewAuditEvent(
                review_id=review.id, tenant_id=review.tenant_id,
                actor_id="system:recovery", action="recovery_failed",
                reason="Stored approval authorization is invalid",
            ))
        db.commit()
        if not valid_authorization:
            return {"status": "invalid_authorization"}

    try:
        from app.agent.tools import process_large_refund
        asyncio.run(process_large_refund.ainvoke(
            {"order_id": authorization["order_id"], "amount": authorization["amount"]},
            config={"configurable": {**identity, "authorization": authorization}},
        ))
        _, completed = approved_refund_result(
            authorization,
            tenant_id=identity["tenant_id"],
            customer_id=identity["customer_id"],
            ticket_id=int(identity["ticket_id"]),
        )
    except Exception:
        logger.exception("Approved refund recovery failed for review %s", review_id)
        completed = False
    if not completed:
        with SyncSessionLocal() as db:
            db.add(ReviewAuditEvent(
                review_id=review_id, tenant_id=identity["tenant_id"],
                actor_id="system:recovery", action="recovery_failed",
                reason="Approved refund tool did not complete the prototype state update",
            ))
            db.commit()
    return {"status": "recovered" if completed else "failed", "review_id": review_id}


@celery_app.task(name="app.workers.tasks.schedule_stalled_approved_reviews")
def schedule_stalled_approved_reviews():
    """Queue bounded recovery for approved reviews still waiting after one minute."""
    now = utc_now()
    attempt_count = (
        select(func.count(ReviewAuditEvent.id))
        .where(
            ReviewAuditEvent.review_id == ReviewRequest.id,
            ReviewAuditEvent.tenant_id == ReviewRequest.tenant_id,
            ReviewAuditEvent.action == "recovery_attempt",
        )
        .correlate(ReviewRequest)
        .scalar_subquery()
    )
    with SyncSessionLocal() as db:
        reviews = db.execute(
            select(ReviewRequest.id)
            .join(Ticket, Ticket.id == ReviewRequest.ticket_id)
            .where(
                ReviewRequest.status == "approved",
                ReviewRequest.decided_at <= now - RECOVERY_INITIAL_DELAY,
                Ticket.tenant_id == ReviewRequest.tenant_id,
                Ticket.status == "escalated",
                attempt_count < RECOVERY_MAX_ATTEMPTS,
            )
            .order_by(ReviewRequest.decided_at, ReviewRequest.id)
            .limit(100)
        ).scalars().all()
        due = []
        for review_id in reviews:
            attempts, last_attempt = db.execute(
                select(func.count(ReviewAuditEvent.id), func.max(ReviewAuditEvent.created_at))
                .where(
                    ReviewAuditEvent.review_id == review_id,
                    ReviewAuditEvent.action == "recovery_attempt",
                )
            ).one()
            if _recovery_due(attempts, last_attempt, now):
                due.append(review_id)
    queued = 0
    for review_id in due:
        key = f"review:recovery:queued:{review_id}"
        if sync_redis_client.set(key, "1", nx=True, ex=90):
            try:
                recover_approved_refund.apply_async(kwargs={"review_id": review_id})
                queued += 1
            except Exception:
                sync_redis_client.delete(key)
                logger.exception("Could not queue recovery for review %s", review_id)
    return {"queued": queued}

@celery_app.task(bind=True, name="app.workers.tasks.dispatch_review_outbox")
def dispatch_review_outbox(self, outbox_id: int):
    """Deliver one committed review event; retries cannot repeat a refund effect."""
    now = utc_now()
    with SyncSessionLocal() as db:
        event = (
            db.execute(
                select(NotificationOutbox)
                .where(NotificationOutbox.id == outbox_id)
                .with_for_update()
            )
        ).scalars().first()
        if event is None:
            return {"status": "missing"}
        if event.status == "sent":
            return {"status": "already_sent"}
        if event.status == "processing" and event.updated_at > now - timedelta(seconds=60):
            return {"status": "already_processing"}
        event.status = "processing"
        event.attempts += 1
        event.updated_at = now
        db.commit()

    try:
        with SyncSessionLocal() as db:
            event = db.get(NotificationOutbox, outbox_id)
            trace_headers = event.payload.get("trace_headers", {})
            if not isinstance(trace_headers, dict):
                trace_headers = {}
        with tracer.start_as_current_span(
            "review_outbox_dispatch", context=propagate.extract(trace_headers)
        ) as span:
            with SyncSessionLocal() as db:
                event = db.get(NotificationOutbox, outbox_id)
                review = db.get(ReviewRequest, event.review_id)
                if (
                    review is None or event.tenant_id != review.tenant_id
                    or event.payload.get("review_id") != review.id
                    or event.payload.get("ticket_id") != review.ticket_id
                ):
                    raise PermissionError("Outbox event does not match its review")
                span.set_attribute("app.tenant_id", review.tenant_id)
                span.set_attribute("app.customer_ref", customer_ref(review.tenant_id, review.customer_id))
                span.set_attribute("app.ticket_id", str(review.ticket_id))
                span.set_attribute("app.review_id", review.id)

                if event.event_type == "review_approved":
                    ticket = db.get(Ticket, review.ticket_id)
                    order = (
                        db.execute(
                            select(Order).where(
                                Order.tenant_id == review.tenant_id,
                                Order.order_id == review.order_id,
                                Order.customer_id == review.customer_id,
                            )
                        )
                    ).scalars().first()
                    if (
                        review.status != "approved"
                        or ticket is None or ticket.tenant_id != review.tenant_id
                        or ticket.customer_id != review.customer_id
                        or order is None
                        or round(float(order.amount), 2) != round(float(review.amount), 2)
                        or not review.decision_actor_id
                    ):
                        raise PermissionError("Approved review cannot be dispatched")
                    already_completed = ticket.status == "resolved" and order.status == "refunded"
                    if not already_completed and ticket.status != "escalated":
                        raise PermissionError("Approved review is no longer actionable")
                    if not already_completed:
                        task_id = approved_task_id(review.id)
                        sync_redis_client.set(
                            task_owner_key(task_id),
                            task_owner_identity(review.tenant_id, review.decision_actor_id),
                            ex=TASK_EVENT_TTL_SECONDS,
                        )
                        authorization = {
                            "source": "admin_ticket_approval",
                            "operation": "large_refund",
                            "actor_id": review.decision_actor_id,
                            "review_id": review.id,
                            "tenant_id": review.tenant_id,
                            "ticket_id": str(review.ticket_id),
                            "order_id": review.order_id,
                            "amount": float(review.amount),
                        }
                        headers = {"x-request-id": event.event_key}
                        propagate.inject(headers)
                        process_agent_task.apply_async(
                            task_id=task_id,
                            kwargs={
                                "topic": (
                                    f"工单 {review.ticket_id} 中订单 {review.order_id} 的退款"
                                    "已由管理员审批，请继续完成该工单的退款处理。"
                                ),
                                "ticket_id": str(review.ticket_id),
                                "user_id": review.customer_id,
                                "tenant_id": review.tenant_id,
                                "authorization": authorization,
                            },
                            headers=headers,
                        )
                elif event.event_type in {
                    "review_requested", "review_rejected", "refund_completed"
                }:
                    key = review_event_key(review.tenant_id)
                    sync_redis_client.xadd(
                        key,
                        {
                            "event": event.event_type,
                            "payload": json.dumps(
                                event.payload, ensure_ascii=False, separators=(",", ":")
                            ),
                        },
                        maxlen=500,
                        approximate=True,
                    )
                    sync_redis_client.expire(key, REVIEW_EVENT_TTL_SECONDS)
                else:
                    raise ValueError("Unknown review outbox event type")

            with SyncSessionLocal() as db:
                event = (
                    db.execute(
                        select(NotificationOutbox)
                        .where(NotificationOutbox.id == outbox_id)
                        .with_for_update()
                    )
                ).scalars().one()
                if event.status == "processing":
                    event.status = "sent"
                    event.delivered_at = utc_now()
                    db.commit()
            return {"status": "sent"}
    except Exception:
        with SyncSessionLocal() as db:
            event = (
                db.execute(
                    select(NotificationOutbox)
                    .where(NotificationOutbox.id == outbox_id)
                    .with_for_update()
                )
            ).scalars().first()
            if event is not None and event.status == "processing":
                event.status = "failed"
                db.commit()
        logger.exception("Review outbox dispatch failed for event %s", outbox_id)
        raise


@celery_app.task(name="app.workers.tasks.dispatch_pending_review_outbox")
def dispatch_pending_review_outbox():
    """Periodic retry for committed events missed by immediate enqueue."""
    cutoff = utc_now() - timedelta(seconds=60)
    with SyncSessionLocal() as db:
        ids = (
            db.execute(
                select(NotificationOutbox.id)
                .where(
                    or_(
                        NotificationOutbox.status.in_(("pending", "failed")),
                        (
                            (NotificationOutbox.status == "processing")
                            & (NotificationOutbox.updated_at < cutoff)
                        ),
                    )
                )
                .order_by(NotificationOutbox.created_at, NotificationOutbox.id)
                .limit(50)
            )
        ).scalars().all()
    for outbox_id in ids:
        dispatch_review_outbox.apply_async(kwargs={"outbox_id": outbox_id})
    return {"queued": len(ids)}

# ==========================================
# 🚨 7. 工业级防御：耗时 MCP 工具的后台异步执行器
# ==========================================
@celery_app.task(bind=True, name="app.workers.tasks.async_mcp_runner")
def async_mcp_runner(self, tool_name: str, tool_args: dict):
    """
    后台专属：专门执行耗时的 MCP 工具，现已升级为 PostgreSQL 标准查询接口。
    """
    task_id = self.request.id
    logger.info(f"⚙️ [Celery MCP] 接收到耗时任务: {tool_name}, ID: {task_id}")
    
    # 🚨 升级外挂：将文件系统替换为 PostgreSQL 数据库连接器
    # 这样大模型就可以通过后台安全地去数据库里执行 SELECT 语句查死锁、查慢查询了
    
     # 🎯 核心优化：优先使用只读副本 URL，若未配置则回退到主库，并转为标准 postgresql:// 协议
    target_url = settings.READONLY_DATABASE_URL or settings.DATABASE_URL
    mcp_db_url = target_url.replace("postgresql+asyncpg://", "postgresql://")
    npx_command = "npx.cmd" if sys.platform == "win32" else "npx"

    # 🚀 使用只读副本的连接串启动 MCP Server
    server_params = StdioServerParameters(
        command=npx_command,
        args=["-y", "@modelcontextprotocol/server-postgres", mcp_db_url],
        env=os.environ.copy() 
    )
    
    
    async def run_mcp_tool():
        try:
            # 🛡️ 防御 1：建立连接时的全局超时控制 (30秒)
            async with asyncio.timeout(30.0):
                async with stdio_client(server_params) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        
                        
                        '''
                        # 🛡️ 防御 2：调用工具时的捕获
                        try:
                            mcp_result = await session.call_tool(tool_name, tool_args)
                            
                            if mcp_result.isError:
                                # 捕获工具自身的业务报错（比如找不到文件）
                                return f"❌ 工具执行失败: {mcp_result.content}"
                            else:
                                return mcp_result.content[0].text if mcp_result.content else "执行成功，无返回值"
                                
                        except Exception as tool_e:
                            # 捕获调用过程中的协议报错（比如大模型传错了参数结构）
                            logger.error(f"❌ [Celery MCP] 工具调用级异常: {str(tool_e)}")
                            return f"❌ 调用工具时发生异常: {str(tool_e)}。请大模型检查传入的参数是否符合 JSON Schema 要求。" '''
                        # 🎯 核心调用：MCP Server 会自动暴露如 'execute_sql' 或 'get_schema' 等工具
                        try:
                            mcp_result = await session.call_tool(tool_name, tool_args)
                            
                            if mcp_result.isError:
                                return f"❌ SQL 执行失败: {mcp_result.content}"
                            else:
                                # 提取返回的文本结果
                                return mcp_result.content[0].text if mcp_result.content else "查询成功，无返回数据"
                                
                        except Exception as tool_e:
                            logger.error(f"❌ [Celery MCP] 工具调用级异常: {str(tool_e)}")
                            return f"❌ 调用 MCP 工具时发生异常: {str(tool_e)}。请检查参数是否符合 Schema。"

        except asyncio.TimeoutError:
            # 🛡️ 防御 3：超时降级
            logger.error(f"❌ [Celery MCP] 致命错误：连接或执行超时！")
            return "❌ 致命错误：底层系统处理超时 (Timeout)。请安抚用户稍后再试。"
            
        except Exception as e:
            # 🛡️ 防御 4：兜底捕获（比如 Node.js 没装好、环境坏了）
            logger.critical(f"🚨 [Celery MCP] 进程级崩溃: {str(e)}")
            return f"🚨 系统严重错误：底层服务崩溃。错误信息: {type(e).__name__}。请向用户致歉。"

    # 执行并获取结果
    final_result = asyncio.run(run_mcp_tool())
    
    # 写入 Redis
    redis_sync_client = Redis.from_url(settings.REDIS_URL, decode_responses = True)
    asyncio.run(redis_sync_client.setex(f"async_result:{task_id}", 3600, final_result))
    
    logger.info(f"✅ [Celery MCP] 任务结束，最终状态已写入 Redis！")
    return final_result


# ==========================================
# 🚨 核心修复 2：重写 Celery Task (全同步化)
# ==========================================
@celery_app.task(bind=True, name="app.workers.tasks.async_support_task")
def async_support_task(self, action: str, params: dict):
    task_id = self.request.id
    logger.info(f"⚙️ [Celery 后台] 接收到耗时支持任务: {action}, 参数: {params}, ID: {task_id}")
    
    # 👇 注意：这里变成了普通的同步函数，不再有 async 关键字！
    def run_real_api():
        try:
            # Defense in depth: this worker is limited to log analysis.
            if action != "query_logs":
                logger.warning("Rejected unsupported async support action: %s", action)
                return (
                    "拒绝执行：该异步 worker 仅支持日志查询。所有符合退款条件的退款申请 "
                    "必须经 process_large_refund 和人工审批流程处理。"
                )

            # 🛡️ 防御 1：模拟真实微服务网络延迟 (设定为 1.5 秒)
            logger.info(f"⏳ [Celery 后台] 正在请求内部订单系统 API，请耐心等待...")
            time.sleep(1.5)  # 👈 改用同步的 time.sleep
            
            '''
            # 💡 业务路由：根据大模型传入的 action 执行真实逻辑
            if action == "query_order":
                order_id = params.get("order_id", "")
                if not order_id:
                    return "❌ 错误：未提供订单号，无法查询。"
                
                # 🎯 核心：使用【同步 Session】查询数据库
                with SyncSessionLocal() as db:
                    result = db.execute(
                        select(Order).where(Order.order_id == order_id)
                    )
                    order = result.scalars().first()
                    
                    if not order:
                        return f"❌ 核查失败：数据库中未找到订单号为 '{order_id}' 的记录。请向客户核对订单号是否输入正确。"
                    
                    # 根据真实的数据库状态返回不同的业务话术
                    if order.status == "pending":
                        return f"⚠️ 订单 {order_id} 深度查询结果：当前状态为『待支付 (pending)』，暂不支持退款操作。"
                    elif order.status == "paid":
                        return f"✅ 订单 {order_id} 深度查询成功：状态为『已支付但未发货 (paid)』，金额 {order.amount} 元，符合全额退款条件。"
                    elif order.status == "shipped":
                        return f"⚠️ 订单 {order_id} 深度查询结果：状态为『已发货 (shipped)』，需要先联系物流拦截后才能申请退款。"
                    elif order.status == "out_of_stock":
                    # 👈 新增这个专属分支，把 amount 告诉大模型
                        return f"✅ 订单 {order_id} 深度查询成功：由于仓库『缺货 (out_of_stock)』，金额 {order.amount} 元，符合全额退款条件。"
                    elif order.status == "refunded":
                        return f"ℹ️ 订单 {order_id} 查询结果：该订单已经退款完毕，请勿重复操作。"
                    else:
                        return f"ℹ️ 订单 {order_id} 当前状态为：{order.status}，订单金额为 {order.amount} 元"
                        '''
            
            if action == "query_logs":
                user_id = params.get("user_id", "未知用户")
                return f"✅ 日志分析完成：用户 {user_id} 尝试支付 3 次，均因『上游支付网关 504 超时』失败，并非用户账户异常。"

            return f"⚠️ 警告：系统不支持的操作类型 '{action}'。"

        except Exception as e: 
            # 同步代码不会抛出 asyncio.TimeoutError，直接捕获所有 Exception 即可
            logger.critical(f"🚨 [Celery 后台] 进程级崩溃: {str(e)}")
            return f"🚨 系统严重错误：底层微服务崩溃。错误信息: {type(e).__name__}。请向客户致歉并立刻通知研发。"

    # 🎯 核心修复 3：直接同步调用，彻底抛弃 asyncio.run()！
    final_result = run_real_api()
    
    # 🎯 核心交接：使用全局同步 Redis 客户端，直接写入
    sync_redis_client.setex(f"async_result:{task_id}", 3600, final_result)
    
    logger.info(f"✅ [Celery 后台] 任务 {task_id} 结束，结果已写入 Redis 并交棒给 Agent！")
    return final_result
