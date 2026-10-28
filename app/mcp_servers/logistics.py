"""Private stdio provider. Only the trusted tool adapter may launch this process."""
import os
import logging
from datetime import datetime, timezone
import psycopg
from psycopg.rows import dict_row
from mcp.server.fastmcp import FastMCP

mcp = FastMCP('Scoped logistics')
logger = logging.getLogger('logistics.provider')


@mcp.tool()
def get_logistics_status(order_id: str) -> dict:
    """Read package status and recent tracking events for the bound order only."""
    tenant,customer,bound=(os.environ.get(k,'') for k in
                           ['LOGISTICS_TENANT_ID','LOGISTICS_CUSTOMER_ID','LOGISTICS_ORDER_ID'])
    dsn=os.environ.get('LOGISTICS_DATABASE_URL','')
    if not dsn or not all((tenant,customer,bound)):
        return {'status':'unavailable'}
    if not isinstance(order_id,str) or not 1<=len(order_id)<=100 or order_id!=bound:
        return {'status':'not_found'}
    try:
        with psycopg.connect(dsn,connect_timeout=3,row_factory=dict_row) as db:
            db.execute('SET TRANSACTION READ ONLY')
            db.execute("SET LOCAL statement_timeout = '2000ms'")
            unsafe=db.execute("""SELECT rolsuper OR rolbypassrls OR rolcreaterole
                OR has_table_privilege(current_user,'logistics.shipments','INSERT,UPDATE,DELETE,TRUNCATE')
                OR has_table_privilege(current_user,'public.orders','SELECT') AS unsafe
                FROM pg_roles WHERE rolname=current_user""").fetchone()
            if unsafe is None or unsafe['unsafe']:
                return {'status':'unavailable'}
            shipments=db.execute('''SELECT shipment_id,carrier,tracking_number,source
                FROM logistics.shipments WHERE tenant_id=%s AND customer_id=%s AND order_id=%s
                ORDER BY shipment_id LIMIT 11''',(tenant,customer,order_id)).fetchall()
            if not shipments:
                return {'status':'not_found'}
            for shipment in shipments[:10]:
                events=db.execute('''SELECT event_id,status,description,occurred_at
                    FROM logistics.shipment_events WHERE tenant_id=%s AND shipment_id=%s
                    ORDER BY occurred_at DESC,event_id DESC LIMIT 21''',
                    (tenant,shipment['shipment_id'])).fetchall()
                shipment['events_truncated']=len(events)>20
                shipment['events']=[{**e,'occurred_at':e['occurred_at'].isoformat()} for e in events[:20]]
                shipment['status']=events[0]['status'] if events else 'unknown'
                shipment['last_event_at']=events[0]['occurred_at'].isoformat() if events else None
            return {'status':'ok','order_id':order_id,'provider':'logistics_mcp',
                    'queried_at':datetime.now(timezone.utc).isoformat(),
                    'shipments_truncated':len(shipments)>10,'shipments':shipments[:10]}
    except Exception as exc:
        logger.warning('Logistics query unavailable: %s',type(exc).__name__)
        return {'status':'unavailable'}


if __name__=='__main__':
    mcp.run(transport='stdio')
