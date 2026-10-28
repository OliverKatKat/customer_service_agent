"""Authorization stays in the Agent gateway; the provider only returns logistics facts."""
import json
import logging
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from pydantic import Field
from sqlalchemy import select
from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.models.order import Order
from app.models.ticket import Ticket
from app.models.user import User
from app.models.tenant import Tenant
from app.integrations.logistics import query_logistics

logger=logging.getLogger('agent.logistics')


@tool
async def get_logistics_status(
    order_id: str=Field(...,min_length=1,max_length=100,description='当前工单绑定的订单号'),
    config: RunnableConfig=None,
) -> str:
    """核对本人订单后查询包裹及物流轨迹。演示数据会标注 synthetic；不能退款、改地址或拦截包裹。"""
    def result(status,**extra):
        return json.dumps({'status':status,**extra},ensure_ascii=False)
    context=(config or {}).get('configurable',{})
    tenant,customer=context.get('tenant_id'),context.get('customer_id')
    try:
        ticket_id=int(context.get('ticket_id'))
    except (TypeError,ValueError):
        return result('missing_context')
    if not all(isinstance(v,str) and v.strip() for v in (tenant,customer)):
        return result('missing_context')
    try:
        async with AsyncSessionLocal() as db:
            account=await db.scalar(select(User).where(User.tenant_id==tenant,User.username==customer,User.is_active.is_(True)))
            merchant=await db.get(Tenant,tenant)
            if account is None or merchant is None or not merchant.is_active:
                return result('account_inactive')
            ticket=await db.scalar(select(Ticket).where(Ticket.id==ticket_id,Ticket.tenant_id==tenant,Ticket.customer_id==customer,Ticket.order_id==order_id))
            order=await db.scalar(select(Order).where(Order.tenant_id==tenant,Order.customer_id==customer,Order.order_id==order_id))
            if ticket is None or order is None:
                return result('order_unavailable')
        # Release the business transaction before starting the provider process.
        if not settings.LOGISTICS_DATABASE_URL:
            return result('unavailable')
        return json.dumps(await query_logistics(settings.LOGISTICS_DATABASE_URL,tenant,customer,order_id),ensure_ascii=False)
    except Exception as exc:
        logger.warning('Logistics lookup unavailable: %s',type(exc).__name__)
        return result('unavailable')
