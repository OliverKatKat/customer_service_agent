"""Idempotent synthetic logistics, used only by the guarded demo seed command."""
import json
from datetime import datetime
from pathlib import Path
from sqlalchemy import text


async def seed_logistics(db):
    fixture=json.loads((Path(__file__).resolve().parents[2]/'demo/logistics.json').read_text(encoding='utf-8'))
    if fixture.get('synthetic') is not True:
        raise ValueError('Synthetic fixture required')
    count=0
    for row in fixture['shipments']:
        params={k:v for k,v in row.items() if k!='events'}
        owner=await db.scalar(text('SELECT customer_id FROM orders WHERE tenant_id=:tenant_id AND order_id=:order_id'),params)
        if owner!=row['customer_id']:
            raise ValueError('Logistics order ownership mismatch')
        existing=(await db.execute(text('SELECT order_id,customer_id,source FROM logistics.shipments WHERE tenant_id=:tenant_id AND shipment_id=:shipment_id'),params)).mappings().first()
        if existing and dict(existing)!={k:row[k] for k in ['order_id','customer_id','source']}:
            raise ValueError('Logistics fixture identity collision')
        inserted=await db.scalar(text('''INSERT INTO logistics.shipments
            (tenant_id,shipment_id,order_id,customer_id,carrier,tracking_number,source)
            VALUES (:tenant_id,:shipment_id,:order_id,:customer_id,:carrier,:tracking_number,:source)
            ON CONFLICT DO NOTHING RETURNING shipment_id'''),params)
        count+=bool(inserted)
        for event in row['events']:
            values={**params,**event,'occurred_at':datetime.fromisoformat(event['occurred_at'])}
            await db.execute(text('''INSERT INTO logistics.shipment_events
                (tenant_id,shipment_id,event_id,status,description,occurred_at)
                VALUES (:tenant_id,:shipment_id,:event_id,:status,:description,:occurred_at)
                ON CONFLICT DO NOTHING'''),values)
    return count
