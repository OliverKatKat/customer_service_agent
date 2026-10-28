"""Preview unbound tickets; explicitly apply only after confirming legacy time zone."""

import argparse
import asyncio
import json
from datetime import timezone
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


async def run(args):
    from sqlalchemy import select
    from app.core.db import AsyncSessionLocal, engine
    from app.models.ticket import Ticket
    from app.models.order import Order
    from app.models.workbench import CaseTicketBinding
    from app.services.cases import register_ticket

    try:
        async with AsyncSessionLocal() as db:
            unbound = CaseTicketBinding.ticket_id.is_(None)
            query = (
                select(Ticket)
                .outerjoin(
                    CaseTicketBinding,
                    (CaseTicketBinding.tenant_id == Ticket.tenant_id)
                    & (CaseTicketBinding.ticket_id == Ticket.id),
                )
                .where(unbound)
                .order_by(Ticket.id)
                .limit(args.limit)
            )
            if args.apply:
                query = query.with_for_update(of=Ticket, skip_locked=True)
            tickets = (await db.execute(query)).scalars().all()
            if not args.apply:
                print(
                    json.dumps(
                        {
                            "mode": "preview",
                            "unbound_in_batch": len(tickets),
                            "limit": args.limit,
                            "changes": 0,
                        }
                    )
                )
                return
            source_zone = ZoneInfo(args.source_timezone)
            created = skipped = 0
            for ticket in tickets:
                if ticket.order_id:
                    valid = await db.scalar(
                        select(Order.id).where(
                            Order.tenant_id == ticket.tenant_id,
                            Order.order_id == ticket.order_id,
                            Order.customer_id == ticket.customer_id,
                        )
                    )
                    if valid is None:
                        skipped += 1
                        continue
                opened = ticket.created_at
                if opened.tzinfo is None:
                    opened = opened.replace(tzinfo=source_zone)
                await register_ticket(
                    db,
                    ticket,
                    "system:backfill",
                    opened_at=opened.astimezone(timezone.utc),
                    source="legacy_backfill",
                )
                created += 1
            await db.commit()
            print(
                json.dumps(
                    {
                        "mode": "apply",
                        "created": created,
                        "skipped_invalid_order": skipped,
                        "source_timezone": args.source_timezone,
                        "limit": args.limit,
                    }
                )
            )
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--source-timezone",
        help="Confirmed time zone of legacy naive ticket timestamps",
    )
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()
    if not 1 <= args.limit <= 1000:
        parser.error("limit must be between 1 and 1000")
    if args.apply and not args.source_timezone:
        parser.error("--apply requires an explicitly confirmed --source-timezone")
    if args.source_timezone:
        try:
            ZoneInfo(args.source_timezone)
        except (ValueError, KeyError):
            parser.error("unknown source time zone")
    asyncio.run(run(args))
