"""Preview or seed only the isolated earphones demo database."""

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


async def run(apply):
    from sqlalchemy.engine import make_url
    from app.core.config import settings
    from app.core.db import AsyncSessionLocal, engine
    from app.services.demo_seed import load_fixture, seed_demo

    fixture = load_fixture()
    if not apply:
        print(
            json.dumps(
                {
                    "mode": "preview",
                    "orders": len(fixture["orders"]),
                    "cases": len(fixture["cases"]),
                    "synthetic": True,
                }
            )
        )
        return
    url = make_url(settings.DATABASE_URL)
    if (url.host, url.port, url.database, url.username) != (
        "127.0.0.1",
        55459,
        "workbench_demo",
        "wb_demo",
    ):
        raise SystemExit(
            "Demo seeding only permits wb_demo@127.0.0.1:55459/workbench_demo"
        )
    try:
        async with AsyncSessionLocal() as db:
            counts = await seed_demo(db, os.environ.get("DEMO_PASSWORD", ""))
            from app.services.logistics_seed import seed_logistics
            logistics_count = await seed_logistics(db)
            await db.commit()
            print(json.dumps({"mode": "apply", "created": counts, "logistics_created": logistics_count, "synthetic": True}))
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    asyncio.run(run(parser.parse_args().apply))
