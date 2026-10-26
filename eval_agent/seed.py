"""Add only the dedicated Agent E2E tenant, users and orders to eval v3 DB."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import bcrypt
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
from eval_v3.seed import checked_url, require_schema  # noqa: E402

FIXTURE_PATH = Path(__file__).with_name("fixtures.json")


def load_fixture() -> dict:
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8-sig"))
    if fixture.get("fixture_version") != "1.0.0":
        raise ValueError("Unsupported Agent E2E fixture version")
    tenant_id = fixture["tenant"]["id"]
    users = {user["username"] for user in fixture["users"]}
    if len(users) != len(fixture["users"]):
        raise ValueError("Duplicate Agent E2E usernames")
    if len({row["id"] for row in fixture["orders"]}) != len(fixture["orders"]):
        raise ValueError("Duplicate Agent E2E order ids")
    if len({row["order_id"] for row in fixture["orders"]}) != len(fixture["orders"]):
        raise ValueError("Duplicate Agent E2E order numbers")
    for order in fixture["orders"]:
        if order["customer_id"] not in users:
            raise ValueError(f"Unknown customer for {order['order_id']}")
    if not tenant_id.startswith("merchant_eval_"):
        raise ValueError("Agent E2E tenant id must be synthetic")
    return fixture


async def apply_fixture(fixture: dict, password: str) -> None:
    raw_url = os.environ.get("EVAL_V3_DATABASE_URL", "")
    url = checked_url(raw_url)
    if os.environ.get("DATABASE_URL") != raw_url:
        raise ValueError("DATABASE_URL must equal the guarded eval v3 URL")
    if len(password) < 12:
        raise ValueError("EVAL_V3_PASSWORD must contain at least 12 characters")
    # The existing created_at columns are PostgreSQL timestamps without zone.
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    engine = create_async_engine(url, echo=False)
    tenant = fixture["tenant"]
    tenant_id = tenant["id"]
    try:
        async with engine.begin() as conn:
            await conn.execute(text(
                "SELECT pg_advisory_xact_lock(hashtext('agent_eval_e2e_fixture_seed'))"
            ))
            await require_schema(conn)
            await conn.execute(text(
                "INSERT INTO tenants (id, name, is_active, created_at) "
                "VALUES (:id, :name, true, :created_at) ON CONFLICT DO NOTHING"
            ), {**tenant, "created_at": now})
            tenant_row = (await conn.execute(text(
                "SELECT id, name, is_active FROM tenants WHERE id=:id"
            ), {"id": tenant_id})).mappings().one()
            if tenant_row["name"] != tenant["name"] or not tenant_row["is_active"]:
                raise RuntimeError("Agent E2E tenant differs from the declared fixture")

            for user in fixture["users"]:
                await conn.execute(text(
                    "INSERT INTO users "
                    "(id, tenant_id, username, hashed_password, role, is_active, created_at) "
                    "VALUES (:id, :tenant_id, :username, :hashed_password, :role, true, :created_at) "
                    "ON CONFLICT DO NOTHING"
                ), {**user, "tenant_id": tenant_id, "hashed_password": password_hash,
                    "created_at": now})
                row = (await conn.execute(text(
                    "SELECT tenant_id, username, hashed_password, role, is_active "
                    "FROM users WHERE id=:id"
                ), {"id": user["id"]})).mappings().one_or_none()
                if (
                    row is None or row["tenant_id"] != tenant_id
                    or row["username"] != user["username"]
                    or row["role"] != user["role"] or not row["is_active"]
                    or not bcrypt.checkpw(password.encode(), row["hashed_password"].encode())
                ):
                    raise RuntimeError(f"Agent E2E user differs from fixture: {user['username']}")

            for order in fixture["orders"]:
                await conn.execute(text(
                    "INSERT INTO orders "
                    "(id, tenant_id, order_id, customer_id, status, amount, created_at) "
                    "VALUES (:id, :tenant_id, :order_id, :customer_id, :status, "
                    ":amount, :created_at) ON CONFLICT DO NOTHING"
                ), {**order, "tenant_id": tenant_id, "created_at": now})
                row = (await conn.execute(text(
                    "SELECT tenant_id, order_id, customer_id, status, amount "
                    "FROM orders WHERE id=:id"
                ), {"id": order["id"]})).mappings().one_or_none()
                if (
                    row is None or row["tenant_id"] != tenant_id
                    or row["order_id"] != order["order_id"]
                    or row["customer_id"] != order["customer_id"]
                    or row["status"] != order["status"]
                    or float(row["amount"]) != float(order["amount"])
                ):
                    raise RuntimeError(f"Agent E2E order differs from fixture: {order['order_id']}")
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Insert the scoped fixture")
    args = parser.parse_args()
    fixture = load_fixture()
    url = checked_url(os.environ.get("EVAL_V3_DATABASE_URL", ""))
    print(
        f"Agent E2E fixture {fixture['fixture_version']}: "
        f"{len(fixture['users'])} users, {len(fixture['orders'])} orders "
        f"at {url.host}:{url.port}/{url.database}"
    )
    if not args.apply:
        print("Preview only. Pass --apply to insert synthetic rows.")
        return
    asyncio.run(apply_fixture(fixture, os.environ.get("EVAL_V3_PASSWORD", "")))
    print("Agent E2E synthetic tenant, users and orders are ready.")


if __name__ == "__main__":
    main()
