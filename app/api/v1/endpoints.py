# app/api/v1/endpoints.py
import uuid
import logging
import secrets
import asyncio
import json
from math import isfinite
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.db import get_db
from app.models.ticket import Ticket
from app.models.workbench import CaseTicketBinding
from app.models.order import Order
from app.models.user import User
from app.models.tenant import Tenant
from app.models.review import ReviewRequest, ReviewAuditEvent, NotificationOutbox, utc_now
from fastapi import APIRouter, Depends, HTTPException, status, Request
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials, OAuth2PasswordBearer
from redis.asyncio import Redis # 👈 引入异步 Redis

from app.api.v1.schemas import TicketCreate, TicketApprove, ReviewApprove, ReviewReject
from app.api.dependencies import get_current_user, verify_admin_role
from app.services.cases import register_ticket
from app.agent.customer_memory import extract_local_preferences
from app.workers.tasks import (
    process_agent_task, dispatch_review_outbox, RECOVERY_MAX_ATTEMPTS,
)
from app.core.rate_limiter import RedisRateLimiter # 👈 引入你刚才写的限流器

from celery.result import AsyncResult
from app.workers.celery_app import celery_app
from fastapi import APIRouter, Depends, status
from opentelemetry import trace
from opentelemetry.propagate import inject
from app.core.config import settings
from app.core.identity import customer_ref
from app.core.review_events import approved_task_id
from app.core.task_events import (
    decode_redis_value,
    publish_task_event,
    record_task_owner,
    task_event_key,
    task_owner_key,
    task_owner_identity,
)
from sqlalchemy import select, func


router = APIRouter()
logger = logging.getLogger("agent")

# ==========================================
# 🚨 1. 初始化限流基础设施
# ==========================================
security = HTTPBearer()
# 初始化 Redis 客户端和限流器实例
redis_client = Redis.from_url(settings.REDIS_URL)
rate_limiter = RedisRateLimiter(redis_client)

# 🚨 升级：指定 Swagger UI 登录时红锁弹窗要请求的真实后端 URL
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login")

# ==========================================
# 🚨 2. 保安逻辑定义：鉴权
# ==========================================
async def verify_rate_limit(user_info: dict = Depends(get_current_user)):
    """
    依赖于鉴权：先拿到 user_id，再对该 user_id 进行精准限流
    """
    user_id = task_owner_identity(user_info["tenant_id"], user_info["sub"])
    
    # 限流规则：每个用户每 60 秒只能提交 5 次大模型任务 (可按需调整)
    LIMIT = 3
    WINDOW_SECONDS = 60
    
    is_allowed, retry_after = await rate_limiter.is_allowed(
        user_id=user_id, 
        limit=LIMIT, 
        window=WINDOW_SECONDS
    )
    
    if not is_allowed:
        logger.warning("Rate limit hit for customer_ref=%s; retry_after=%s", customer_ref(user_info["tenant_id"], user_info["sub"]), retry_after)
        # 精准抛出 429 状态码，并附带 Retry-After 响应头
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"请求过于频繁，请在 {retry_after} 秒后重试。",
            headers={"Retry-After": str(retry_after)}
        )
    
    # 如果放行，顺便把 user_info 传递给下游路由
    return user_info
# ==========================================
# 🚨 新增保安逻辑：高级权限查验 (RBAC)
# ==========================================
@router.post("/submit", status_code=status.HTTP_202_ACCEPTED)
async def submit_ticket(
    payload: TicketCreate,
    user_info: dict = Depends(verify_rate_limit),
    db: AsyncSession = Depends(get_db)  # 👈 注入 HTTP 生命周期的数据库 Session
):
    if settings.DEMO_DATA_ONLY:
        raise HTTPException(status_code=503, detail="当前是演示数据浏览模式。配置模型并重启演示后可发送对话；现有样本和审核材料仍可查看。")
    # 用户与租户来自已重新核验的登录身份，不接受请求体指定。
    user_id = user_info["sub"]
    tenant_id = user_info["tenant_id"]
    if payload.order_id:
        order = (await db.execute(
            select(Order).where(
                Order.tenant_id == tenant_id,
                Order.customer_id == user_id,
                Order.order_id == payload.order_id,
            )
        )).scalars().first()
        if order is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="关联订单不存在")

    # ------------------------------------------
    # 💾 步骤 A：严格按照 Ticket 模型图纸建档
    # ------------------------------------------
    new_ticket = Ticket(
        # 绝不手动传 id，让 PostgreSQL 自动生成整数序列
        tenant_id=tenant_id,
        customer_id=user_id,
        order_id=payload.order_id,
        title=payload.topic[:255], # 截取前255字作为标题，防止超长
        status="pending"           # 默认初始状态
    )
    db.add(new_ticket)
    await db.flush()
    service_case = await register_ticket(db, new_ticket, user_id, payload.case_id)
    await db.commit()
    await db.refresh(new_ticket)   # 刷新对象，拿到数据库生成的真实 ID
    
    # 将真实的自增 ID (如 1, 2, 3) 转为字符串，作为整个链路的流水号
    db_id_str = str(new_ticket.id)

    # ------------------------------------------
    # 🧠 步骤 B：上下文注入 (补全大模型盲区)
    # ------------------------------------------
    enhanced_topic = payload.topic
    if payload.order_id:
        enhanced_topic += f"\n\n[系统附加信息] 客户当前所在的订单详情页关联订单号为：{payload.order_id}"

    logger.info(
        "Received ticket %s",
        db_id_str,
        extra={"tenant_id": tenant_id, "customer_ref": customer_ref(tenant_id, user_id), "ticket_id": db_id_str},
    )
    
    # 使用真正的 OpenTelemetry trace ID；工单 ID 单独保留。
    otel_headers = {}
    request_id = str(uuid.uuid4())
    current_span = trace.get_current_span()
    current_span.set_attribute("app.request_id", request_id)
    current_span.set_attribute("app.tenant_id", tenant_id)
    current_span.set_attribute("app.customer_ref", customer_ref(tenant_id, user_id))
    current_span.set_attribute("app.ticket_id", db_id_str)
    otel_trace_id = f"{current_span.get_span_context().trace_id:032x}"
    inject(otel_headers)
    otel_headers["x-request-id"] = request_id

    # ------------------------------------------
    # 🚀 步骤 C：异步派发给大模型大脑
    # ------------------------------------------
    # Allocate the id first so the ownership record and initial SSE event exist
    # before a fast Celery worker can publish its first progress update.
    task_id = str(uuid.uuid4())
    await record_task_owner(redis_client, task_id, tenant_id, user_id)
    await publish_task_event(redis_client, task_id, "queued", "工单已受理，客服 Agent 即将处理")
    task = process_agent_task.apply_async(
        task_id=task_id,
        kwargs={
            "topic": enhanced_topic,
            "memory_facts": extract_local_preferences([{"type": "human", "content": payload.topic}]),
            "ticket_id": db_id_str,
            "user_id": user_id,
            "tenant_id": tenant_id
        },
        headers=otel_headers
    )

    return {
        "status": "pending",
        "trace_id": db_id_str,  # 兼容旧客户端；此字段实际是工单 ID。
        "ticket_id": db_id_str,
        "case_id": service_case.case_id,
        "otel_trace_id": otel_trace_id,
        "request_id": request_id,
        "task_id": task.id,
        "message": "工单已受理，智能客服正在处理..."
    }
#====================================================
#                 FastAPI取回结果接口 (修复升级版)
#====================================================
@router.get("/task/{task_id}", status_code=status.HTTP_200_OK)
async def get_task_result(
    task_id: str,
    user_info: dict = Depends(verify_rate_limit) 
):
    """前端轮询接口：统一查询任意 Celery 异步任务 (大脑任务/MCP工具任务) 的结果"""
    
    # Missing or expired ownership must fail closed.
    owner = await redis_client.get(task_owner_key(task_id))
    expected_owner = task_owner_identity(user_info["tenant_id"], user_info["sub"])
    if owner is None or decode_redis_value(owner) != expected_owner:
        raise HTTPException(status_code=404, detail="任务不存在")

    # 🌟 核心升级：直接使用 Celery 原生的 AsyncResult 去读取任务状态
    task_result = AsyncResult(task_id, app=celery_app)
    
    if task_result.state == 'PENDING' or task_result.state == 'STARTED':
        return {"status": "processing", "message": "任务后台执行中，请稍候..."}
        
    elif task_result.state == 'SUCCESS':
        # 任务执行完了，我们把 worker 里 return 的内容拿出来
        data = task_result.result
        
        # 兼容处理：因为大脑任务返回的是字典，而 MCP 任务返回的是字符串
        if isinstance(data, dict):
            # 大脑任务的返回结构 (包含了我们在 task 里写的 output)
            task_status = "failed" if data.get("status") in {"error", "failed"} else "completed"
            return {"status": task_status, "result": data.get("output", str(data))}
        else:
            # MCP 纯文本工具的返回结构
            return {"status": "completed", "result": data}
            
    elif task_result.state == 'FAILURE':
        return {"status": "failed", "result": "任务在后台发生严重崩溃"}
        
    else:
        return {"status": task_result.state, "result": "未知状态"}
    


@router.get("/task/{task_id}/events", status_code=status.HTTP_200_OK)
async def stream_task_events(
    task_id: str,
    request: Request,
    user_info: dict = Depends(get_current_user),
):
    """Stream queued/running/completed task events over Server-Sent Events."""
    owner = await redis_client.get(task_owner_key(task_id))
    expected_owner = task_owner_identity(user_info["tenant_id"], user_info["sub"])
    if owner is None or decode_redis_value(owner) != expected_owner:
        # Do not reveal whether another user's task id exists.
        raise HTTPException(status_code=404, detail="任务不存在")

    stream_key = task_event_key(task_id)
    cursor = request.headers.get("last-event-id", "0-0")
    try:
        major, minor = cursor.split("-", 1)
        int(major)
        int(minor)
    except (ValueError, AttributeError):
        cursor = "0-0"

    async def event_generator():
        nonlocal cursor
        while not await request.is_disconnected():
            try:
                batches = await redis_client.xread(
                    {stream_key: cursor},
                    count=100,
                    block=15000,
                )
            except Exception:
                logger.exception("SSE read failed for task %s", task_id)
                payload = json.dumps(
                    {"status": "failed", "message": "进度连接暂时不可用"},
                    ensure_ascii=False,
                )
                yield f"event: failed\ndata: {payload}\n\n"
                return

            if not batches:
                # The worker may have been terminated outside the Agent's error
                # handlers. Check Celery's result backend without blocking the API loop.
                task_result = AsyncResult(task_id, app=celery_app)
                state = await asyncio.to_thread(lambda: task_result.state)
                if state in {"FAILURE", "REVOKED"}:
                    payload = json.dumps(
                        {"status": "failed", "message": "后台任务未能完成"},
                        ensure_ascii=False,
                    )
                    yield f"event: failed\ndata: {payload}\n\n"
                    return
                if state == "SUCCESS":
                    data = await asyncio.to_thread(lambda: task_result.result)
                    if isinstance(data, dict):
                        result_text = data.get("output", str(data))
                        task_status = data.get("status", "success")
                    else:
                        result_text = data
                        task_status = "success"
                    terminal_event = "failed" if task_status == "error" else "completed"
                    payload = json.dumps(
                        {"status": task_status, "message": "处理完成" if terminal_event == "completed" else "处理未成功", "result": result_text},
                        ensure_ascii=False,
                    )
                    yield f"event: {terminal_event}\ndata: {payload}\n\n"
                    return
                yield ": keep-alive\n\n"
                continue

            for _, entries in batches:
                for event_id, fields in entries:
                    cursor = decode_redis_value(event_id)
                    event_name = decode_redis_value(fields.get(b"event", fields.get("event", b"progress")))
                    payload = decode_redis_value(fields.get(b"payload", fields.get("payload", b"{}")))
                    yield f"id: {cursor}\nevent: {event_name}\ndata: {payload}\n\n"
                    if event_name in {"completed", "failed"}:
                        return

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ==========================================
# 5. Tenant-scoped durable human review API
# ==========================================
def _check_review_code(code: str) -> None:
    configured = settings.ADMIN_APPROVAL_CODE
    if len(configured) < 16:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="审批功能尚未配置安全凭据",
        )
    if not secrets.compare_digest(code.encode("utf-8"), configured.encode("utf-8")):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="审批凭据无效",
        )


def _review_payload(review: ReviewRequest) -> dict:
    return {
        "review_id": review.id,
        "tenant_id": review.tenant_id,
        "ticket_id": review.ticket_id,
        "customer_id": review.customer_id,
        "order_id": review.order_id,
        "amount": float(review.amount),
        "reason": review.reason,
        "status": review.status,
        "decision_actor_id": review.decision_actor_id,
        "decision_reason": review.decision_reason,
        "created_at": review.created_at.isoformat(),
        "decided_at": review.decided_at.isoformat() if review.decided_at else None,
        "version": review.version,
        "prototype_only": True,
    }


@router.get("/reviews")
async def list_reviews(
    state: str = "pending",
    limit: int = 50,
    admin_info: dict = Depends(verify_admin_role),
    db: AsyncSession = Depends(get_db),
):
    if state not in {"pending", "approved", "rejected"} or not 1 <= limit <= 100:
        raise HTTPException(status_code=400, detail="无效的审核状态或条数")
    reviews = (
        await db.execute(
            select(ReviewRequest)
            .where(
                ReviewRequest.tenant_id == admin_info["tenant_id"],
                ReviewRequest.status == state,
            )
            .order_by(ReviewRequest.created_at.desc(), ReviewRequest.id.desc())
            .limit(limit)
        )
    ).scalars().all()
    return {"reviews": [_review_payload(item) for item in reviews]}


@router.get("/reviews/{review_id}")
async def review_detail(
    review_id: int,
    admin_info: dict = Depends(verify_admin_role),
    db: AsyncSession = Depends(get_db),
):
    review = (
        await db.execute(
            select(ReviewRequest).where(
                ReviewRequest.id == review_id,
                ReviewRequest.tenant_id == admin_info["tenant_id"],
            )
        )
    ).scalars().first()
    if review is None:
        raise HTTPException(status_code=404, detail="审核请求不存在")
    audit = (
        await db.execute(
            select(ReviewAuditEvent)
            .where(
                ReviewAuditEvent.review_id == review.id,
                ReviewAuditEvent.tenant_id == admin_info["tenant_id"],
            )
            .order_by(ReviewAuditEvent.created_at, ReviewAuditEvent.id)
        )
    ).scalars().all()
    ticket = (
        await db.execute(select(Ticket).where(
            Ticket.id == review.ticket_id,
            Ticket.tenant_id == admin_info["tenant_id"],
            Ticket.customer_id == review.customer_id,
        ))
    ).scalars().first()
    order = (
        await db.execute(select(Order).where(
            Order.order_id == review.order_id,
            Order.tenant_id == admin_info["tenant_id"],
            Order.customer_id == review.customer_id,
        ))
    ).scalars().first()
    recovery_attempts = sum(event.action == "recovery_attempt" for event in audit)
    completed = (ticket is not None and order is not None
                 and ticket.status == "resolved" and order.status == "refunded")
    processing_state = (
        "completed" if review.status == "approved" and completed
        else "needs_attention" if review.status == "approved" and recovery_attempts >= RECOVERY_MAX_ATTEMPTS
        else "processing" if review.status == "approved"
        else review.status
    )
    case_id = await db.scalar(select(CaseTicketBinding.case_id).where(
        CaseTicketBinding.tenant_id == admin_info["tenant_id"],
        CaseTicketBinding.ticket_id == review.ticket_id,
    ))
    return {
        "case_id": case_id,
        "review": _review_payload(review),
        "processing_state": processing_state,
        "recovery_attempts": recovery_attempts,
        "evidence": {
            "ticket_title": ticket.title if ticket else None,
            "ticket_description": ticket.description if ticket else None,
            "ticket_status": ticket.status if ticket else None,
            "order_status": order.status if order else None,
            "order_amount": float(order.amount) if order else None,
        },
        "audit": [
            {
                "action": event.action,
                "actor_id": event.actor_id,
                "reason": event.reason,
                "created_at": event.created_at.isoformat(),
            }
            for event in audit
        ],
    }


@router.get("/tickets/{ticket_id}/review")
async def customer_review_status(
    ticket_id: int,
    user_info: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Customer-facing state for a ticket owned by the authenticated user."""
    ticket = (
        await db.execute(
            select(Ticket).where(
                Ticket.id == ticket_id,
                Ticket.tenant_id == user_info["tenant_id"],
                Ticket.customer_id == user_info["sub"],
            )
        )
    ).scalars().first()
    if ticket is None:
        raise HTTPException(status_code=404, detail="工单不存在")
    review = (
        await db.execute(
            select(ReviewRequest).where(
                ReviewRequest.ticket_id == ticket_id,
                ReviewRequest.tenant_id == user_info["tenant_id"],
                ReviewRequest.customer_id == user_info["sub"],
            )
        )
    ).scalars().first()
    if review is None:
        raise HTTPException(status_code=404, detail="该工单尚无退款审核")
    recovery_attempts = 0
    if review.status == "approved" and ticket.status != "resolved":
        recovery_attempts = (await db.execute(
            select(func.count(ReviewAuditEvent.id)).where(
                ReviewAuditEvent.review_id == review.id,
                ReviewAuditEvent.tenant_id == user_info["tenant_id"],
                ReviewAuditEvent.action == "recovery_attempt",
            )
        )).scalar_one()
    if review.status == "rejected":
        message = "人工审核已驳回，请联系人工客服。"
    elif ticket.status == "resolved":
        message = "人工审核已批准，原型订单状态已更新；未接入实际支付退款。"
    elif review.status == "approved" and recovery_attempts >= RECOVERY_MAX_ATTEMPTS:
        message = "人工审核已批准，但系统处理未完成；请联系人工客服核查。"
    elif review.status == "approved":
        message = "人工审核已批准，系统正在继续处理。"
    else:
        message = "退款申请正在等待人工审核。"
    return {
        "ticket_id": ticket_id,
        "review_id": review.id,
        "review_status": review.status,
        "ticket_status": ticket.status,
        "message": message,
        "prototype_only": True,
    }

async def _decide_review(
    review_id: int,
    action: str,
    approval_code: str,
    reason: str,
    admin_info: dict,
    db: AsyncSession,
    expected_version: int | None = None,
) -> dict:
    if settings.DEMO_DATA_ONLY:
        raise HTTPException(status_code=503, detail="数据浏览模式不执行审批。配置模型并重启演示后可演示审批与模拟执行。")
    _check_review_code(approval_code)
    tenant_id = admin_info["tenant_id"]
    actor_id = admin_info["sub"]
    span = trace.get_current_span()
    span.set_attribute("app.tenant_id", tenant_id)
    span.set_attribute("app.review_id", review_id)
    span.set_attribute("app.actor_ref", customer_ref(tenant_id, actor_id))
    reason = reason.strip()
    if action == "rejected" and len(reason) < 3:
        raise HTTPException(status_code=422, detail="驳回原因至少 3 个字符")

    # Lock in the same order as the refund tool: ticket, order, review.
    identity = (
        await db.execute(
            select(
                ReviewRequest.ticket_id,
                ReviewRequest.order_id,
                ReviewRequest.customer_id,
            ).where(
                ReviewRequest.id == review_id,
                ReviewRequest.tenant_id == tenant_id,
            )
        )
    ).first()
    if identity is None:
        raise HTTPException(status_code=404, detail="审核请求不存在")
    ticket_id, order_id, customer_id = identity

    ticket = (
        await db.execute(
            select(Ticket).where(
                Ticket.id == ticket_id,
                Ticket.tenant_id == tenant_id,
                Ticket.customer_id == customer_id,
            ).with_for_update()
        )
    ).scalars().first()
    order = (
        await db.execute(
            select(Order).where(
                Order.order_id == order_id,
                Order.tenant_id == tenant_id,
                Order.customer_id == customer_id,
            ).with_for_update()
        )
    ).scalars().first()
    review = (
        await db.execute(
            select(ReviewRequest).where(
                ReviewRequest.id == review_id,
                ReviewRequest.tenant_id == tenant_id,
            ).with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalars().first()
    if review is None or ticket is None or order is None:
        raise HTTPException(status_code=409, detail="审核请求关联的工单或订单不可用")
    if review.status != "pending":
        raise HTTPException(status_code=409, detail="审核请求已处理")
    if expected_version is not None and review.version != expected_version:
        raise HTTPException(status_code=409, detail="审核请求版本已变化，请刷新后重试")
    if (
        ticket.status != "escalated"
        or ticket.order_id != review.order_id
        or ticket.customer_id != review.customer_id
        or review.ticket_id != ticket.id
        or review.tenant_id != tenant_id
        or review.order_id != order.order_id
        or review.customer_id != order.customer_id
        or not isfinite(float(order.amount))
        or round(float(review.amount), 2) != round(float(order.amount), 2)
        or order.status not in {"pending", "paid", "out_of_stock"}
    ):
        raise HTTPException(status_code=409, detail="工单或订单状态与审核请求不匹配")

    review.status = action
    review.decision_actor_id = actor_id
    review.decision_reason = reason or None
    review.decided_at = utc_now()
    if action == "rejected":
        ticket.status = "rejected"
        ticket.resolution = "人工审核已驳回退款申请；原型订单状态未更改。"
    db.add(ReviewAuditEvent(
        review_id=review.id,
        tenant_id=tenant_id,
        actor_id=actor_id,
        action=action,
        reason=reason or None,
    ))
    trace_headers = {}
    inject(trace_headers)
    outbox = NotificationOutbox(
        review_id=review.id,
        tenant_id=tenant_id,
        event_key=f"review:{review.id}:{action}",
        event_type=f"review_{action}",
        payload={"review_id": review.id, "ticket_id": ticket.id,
                 "trace_headers": trace_headers},
        status="pending",
        attempts=0,
    )
    db.add(outbox)
    await db.flush()
    outbox_id = outbox.id
    await db.commit()

    delivery_pending = True
    try:
        dispatch_review_outbox.apply_async(kwargs={"outbox_id": outbox_id})
    except Exception:
        logger.exception("Review outbox enqueue failed for review %s", review_id)
    return {
        "status": action,
        "review_id": review_id,
        "ticket_id": str(ticket_id),
        "task_id": approved_task_id(review_id) if action == "approved" else None,
        "delivery_pending": delivery_pending,
        "prototype_only": True,
    }


@router.post("/reviews/{review_id}/approve", status_code=status.HTTP_202_ACCEPTED)
async def approve_review(
    review_id: int,
    payload: ReviewApprove,
    admin_info: dict = Depends(verify_admin_role),
    db: AsyncSession = Depends(get_db),
):
    return await _decide_review(
        review_id, "approved", payload.approval_code, payload.reason, admin_info, db,
        expected_version=payload.expected_version
    )


@router.post("/reviews/{review_id}/reject", status_code=status.HTTP_200_OK)
async def reject_review(
    review_id: int,
    payload: ReviewReject,
    admin_info: dict = Depends(verify_admin_role),
    db: AsyncSession = Depends(get_db),
):
    return await _decide_review(
        review_id, "rejected", payload.approval_code, payload.reason, admin_info, db,
        expected_version=payload.expected_version
    )


@router.post("/ticket/approve", status_code=status.HTTP_202_ACCEPTED)
async def approve_ticket(
    payload: TicketApprove,
    admin_info: dict = Depends(verify_admin_role),
    db: AsyncSession = Depends(get_db),
):
    """Legacy route; all decisions use the durable review state machine."""
    try:
        ticket_id = int(payload.trace_id)
    except (TypeError, ValueError):
        raise HTTPException(status_code=404, detail="工单不存在")
    review_id = (
        await db.execute(
            select(ReviewRequest.id).where(
                ReviewRequest.ticket_id == ticket_id,
                ReviewRequest.tenant_id == admin_info["tenant_id"],
            )
        )
    ).scalar_one_or_none()
    if review_id is None:
        raise HTTPException(status_code=404, detail="审核请求不存在")
    return await _decide_review(
        review_id, "approved", payload.approval_code,
        "通过兼容审批入口批准", admin_info, db,
    )
#=========================在 Swagger UI 中透出“当前登录账号”接口================
@router.get("/users/me", tags=["Authentication"])
async def read_current_user(current_user: dict = Depends(get_current_user)):
    """
    在 SwaggerUI 中点击 Try it out 执行，能直接返回当前登录用户的账号和角色
    """
    return {
        "username": current_user.get("sub"),
        "role": current_user.get("role"),
        "tenant_id": current_user.get("tenant_id"),
        "status": "authenticated"
    }
