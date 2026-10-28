"""First delivery: fixed case-registry statistics, not the full business mart."""

from datetime import datetime, timedelta, timezone as dt_timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import require_analyst
from app.api.envelopes import ContractRoute, envelope
from app.core.analytics_db import get_analytics_db
from app.models.workbench import ServiceCase

router = APIRouter(prefix="/analytics", route_class=ContractRoute)


@router.get("/overview")
async def overview(
    request: Request,
    start: datetime | None = None,
    end: datetime | None = None,
    timezone: str = "Asia/Shanghai",
    actor: dict = Depends(require_analyst),
    db: AsyncSession = Depends(get_analytics_db),
):
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise HTTPException(422, "业务时区不合法") from None
    snapshot = await db.scalar(select(func.transaction_timestamp()))
    end = end or snapshot
    start = start or end - timedelta(days=30)
    if any(value.tzinfo is None or value.utcoffset() is None for value in [start, end]):
        raise HTTPException(422, "查询时间必须带时区")
    start, end = start.astimezone(dt_timezone.utc), end.astimezone(dt_timezone.utc)
    if not timedelta(0) < end - start <= timedelta(days=180):
        raise HTTPException(422, "查询窗口应大于零且不超过一百八十天")
    tenant = ServiceCase.tenant_id == actor["tenant_id"]
    period = (tenant, ServiceCase.opened_at >= start, ServiceCase.opened_at < end)
    total = await db.scalar(
        select(func.count()).select_from(ServiceCase).where(*period)
    )
    classified = await db.scalar(
        select(func.count())
        .select_from(ServiceCase)
        .where(*period, ServiceCase.case_type.in_(["refund", "return", "complaint"]))
    )
    current = await db.scalar(
        select(func.count())
        .select_from(ServiceCase)
        .where(tenant, ServiceCase.status.in_(["open", "in_progress"]))
    )
    types = (
        await db.execute(
            select(ServiceCase.case_type, func.count())
            .where(*period)
            .group_by(ServiceCase.case_type)
            .order_by(ServiceCase.case_type)
        )
    ).all()
    reasons = (
        await db.execute(
            select(ServiceCase.reason_code, func.count())
            .where(*period)
            .group_by(ServiceCase.reason_code)
            .order_by(ServiceCase.reason_code)
        )
    ).all()
    return envelope(
        request,
        {
            "cases_opened_all": total,
            "after_sales_opened_classified": classified,
            "open_cases_current": current,
            "by_case_type": [{"value": name, "count": count} for name, count in types],
            "by_reason": [
                {"value": name or "unknown", "count": count} for name, count in reasons
            ],
        },
        metric_version="workbench-operational-v1",
        snapshot_at=snapshot.isoformat(),
        period={
            "start": start.isoformat(),
            "end": end.isoformat(),
            "timezone": timezone,
            "bounds": "[start,end)",
        },
        source="public.case_registry",
        scope="operational_registry",
        limitations=[
            "当前未结束案件是当前存量，不是历史积压回放",
            "未分类及普通咨询不计入已确认售后新增量",
            "尚未实现商品维度、批准金额、经营事实投影及自然语言查数",
        ],
    )
