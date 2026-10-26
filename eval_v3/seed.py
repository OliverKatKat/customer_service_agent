"""Seed only the isolated agent_eval_v3 PostgreSQL database.

The default mode is a preview. --apply is required to write anything.
No project .env, app Settings, live engine, old eval script, or Qdrant client is imported.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from datetime import datetime
from pathlib import Path

import bcrypt
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

FIXTURE_PATH = Path(__file__).with_name("fixtures.json")
EXPECTED_DATABASE = "agent_eval_v3"
EXPECTED_HOSTS = {"127.0.0.1", "localhost"}
EXPECTED_PORT = 55432
EXPECTED_USER = "eval_v3"


def load_fixture() -> dict:
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8-sig"))
    if fixture.get("fixture_version") != "3.0.0":
        raise ValueError("Unsupported eval fixture version")
    tenant_ids = {item["id"] for item in fixture["tenants"]}
    users = {item["username"]: item for item in fixture["users"]}
    orders = {item["order_id"]: item for item in fixture["orders"]}
    tickets = {item["id"]: item for item in fixture["tickets"]}

    for table in ("users", "orders", "tickets"):
        rows = fixture[table]
        if len({row["id"] for row in rows}) != len(rows):
            raise ValueError(f"Duplicate primary key in {table}")
    if len(users) != len(fixture["users"]) or len(orders) != len(fixture["orders"]):
        raise ValueError("Duplicate username or order number in fixture")
    for user in fixture["users"]:
        if user["tenant_id"] not in tenant_ids:
            raise ValueError(f"Unknown tenant for {user['username']}")
    for order in fixture["orders"]:
        user = users.get(order["customer_id"])
        if user is None or user["tenant_id"] != order["tenant_id"]:
            raise ValueError(f"Invalid order owner: {order['order_id']}")
    for ticket in fixture["tickets"]:
        user = users.get(ticket["customer_id"])
        if user is None or user["tenant_id"] != ticket["tenant_id"]:
            raise ValueError(f"Invalid ticket owner: {ticket['id']}")
        if ticket["order_id"]:
            order = orders.get(ticket["order_id"])
            if order is None or (order["tenant_id"], order["customer_id"]) != (
                ticket["tenant_id"],
                ticket["customer_id"],
            ):
                raise ValueError(f"Invalid ticket order: {ticket['id']}")
    for fact in fixture["memory_facts_for_m1"]:
        ticket = tickets.get(fact["source_ticket_id"])
        if ticket is None or (ticket["tenant_id"], ticket["customer_id"]) != (
            fact["tenant_id"],
            fact["customer_id"],
        ):
            raise ValueError(f"Invalid memory source: {fact['source_ticket_id']}")
    return fixture


def checked_url(raw: str):
    if not raw:
        raise ValueError("Set EVAL_V3_DATABASE_URL or pass --database-url explicitly")
    url = make_url(raw)
    if (
        url.drivername != "postgresql+asyncpg"
        or url.database != EXPECTED_DATABASE
        or url.host not in EXPECTED_HOSTS
        or url.port != EXPECTED_PORT
        or url.username != EXPECTED_USER
        or not url.password
    ):
        raise ValueError(
            "Refusing database URL: expected postgresql+asyncpg://eval_v3:"
            "***@127.0.0.1:55432/agent_eval_v3"
        )
    return url


async def require_schema(conn) -> None:
    database = await conn.scalar(text("SELECT current_database()"))
    if database != EXPECTED_DATABASE:
        raise RuntimeError(f"Connected to unexpected database: {database}")
    required = {
        "tenants": {"id", "name", "is_active", "created_at"},
        "users": {
            "id", "tenant_id", "username", "hashed_password", "role",
            "is_active", "created_at",
        },
        "orders": {
            "id", "tenant_id", "order_id", "customer_id",
            "status", "amount", "created_at",
        },
        "tickets": {
            "id", "tenant_id", "customer_id", "order_id", "title",
            "description", "status", "resolution", "created_at",
            "updated_at", "version",
        },
    }
    for table, columns in required.items():
        result = await conn.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = :table"
            ),
            {"table": table},
        )
        missing = columns - set(result.scalars())
        if missing:
            raise RuntimeError(
                f"Schema is not ready for eval v3: {table} lacks {sorted(missing)}. "
                "Run migrations against the isolated eval database first."
            )


async def require_clean_fixture_scope(conn, fixture: dict) -> None:
    """Allow first seed or exact fixture re-seed; never overwrite unrelated rows."""
    expected = {
        "tenants": {
            row["id"]: (row["id"],) for row in fixture["tenants"]
        },
        "users": {
            row["id"]: (row["id"], row["tenant_id"], row["username"])
            for row in fixture["users"]
        },
        "orders": {
            row["id"]: (
                row["id"], row["tenant_id"], row["customer_id"], row["order_id"]
            )
            for row in fixture["orders"]
        },
        "tickets": {
            row["id"]: (
                row["id"], row["tenant_id"], row["customer_id"], row["order_id"]
            )
            for row in fixture["tickets"]
        },
    }
    statements = {
        "tenants": "SELECT id FROM tenants",
        "users": "SELECT id, tenant_id, username FROM users",
        "orders": "SELECT id, tenant_id, customer_id, order_id FROM orders",
        "tickets": "SELECT id, tenant_id, customer_id, order_id FROM tickets",
    }
    for table, query in statements.items():
        existing = (await conn.execute(text(query))).all()
        for row in existing:
            values = tuple(row)
            if table == "tenants" and values == ("merchant_default",):
                continue
            if expected[table].get(values[0]) != values:
                raise RuntimeError(
                    f"Unrecognized {table} row with id={values[0]}; "
                    "use a fresh isolated eval database instead of cleaning it in place"
                )

    # A later run can leave Agent work, checkpoints, or reviews behind.
    # Re-seeding that database would not reproduce the same starting state.
    for table in (
        "tasks", "checkpoints", "checkpoint_writes", "checkpoint_blobs",
        "review_requests", "review_audit_events", "notification_outbox",
    ):
        exists = await conn.scalar(text("SELECT to_regclass(:table_name)"), {
            "table_name": f"public.{table}",
        })
        if exists is not None:
            count = await conn.scalar(text(f"SELECT count(*) FROM {table}"))
            if count:
                raise RuntimeError(
                    f"{table} already has {count} rows; use a fresh isolated eval database"
                )


async def advance_sequence(conn, table: str, max_id: int) -> None:
    sequence = await conn.scalar(
        text("SELECT pg_get_serial_sequence(:table_name, 'id')"),
        {"table_name": f"public.{table}"},
    )
    if sequence is None:
        raise RuntimeError(f"{table}.id has no generated sequence")
    await conn.execute(
        text("SELECT setval(CAST(:sequence AS regclass), :value, true)"),
        {"sequence": sequence, "value": max_id},
    )


async def apply_fixture(url, fixture: dict, password: str) -> None:
    password_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("ascii")
    fixed_time = datetime.fromisoformat(fixture["fixed_time_utc"])
    engine = create_async_engine(url, echo=False)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("SELECT pg_advisory_xact_lock(hashtext('agent_eval_v3_fixture_seed'))")
            )
            await require_schema(conn)
            await require_clean_fixture_scope(conn, fixture)

            for row in fixture["tenants"]:
                await conn.execute(
                    text(
                        "INSERT INTO tenants (id, name, is_active, created_at) "
                        "VALUES (:id, :name, :is_active, :created_at) "
                        "ON CONFLICT (id) DO UPDATE SET "
                        "name = EXCLUDED.name, is_active = EXCLUDED.is_active, "
                        "created_at = EXCLUDED.created_at"
                    ),
                    {**row, "created_at": fixed_time},
                )
            for row in fixture["users"]:
                await conn.execute(
                    text(
                        "INSERT INTO users "
                        "(id, tenant_id, username, hashed_password, role, is_active, created_at) "
                        "VALUES "
                        "(:id, :tenant_id, :username, :hashed_password, :role, "
                        ":is_active, :created_at) "
                        "ON CONFLICT (id) DO UPDATE SET "
                        "tenant_id = EXCLUDED.tenant_id, username = EXCLUDED.username, "
                        "hashed_password = EXCLUDED.hashed_password, role = EXCLUDED.role, "
                        "is_active = EXCLUDED.is_active, created_at = EXCLUDED.created_at"
                    ),
                    {**row, "hashed_password": password_hash, "created_at": fixed_time},
                )
            for row in fixture["orders"]:
                await conn.execute(
                    text(
                        "INSERT INTO orders "
                        "(id, tenant_id, order_id, customer_id, status, amount, created_at) "
                        "VALUES "
                        "(:id, :tenant_id, :order_id, :customer_id, :status, :amount, "
                        ":created_at) "
                        "ON CONFLICT (id) DO UPDATE SET "
                        "tenant_id = EXCLUDED.tenant_id, order_id = EXCLUDED.order_id, "
                        "customer_id = EXCLUDED.customer_id, status = EXCLUDED.status, "
                        "amount = EXCLUDED.amount, created_at = EXCLUDED.created_at"
                    ),
                    {**row, "created_at": fixed_time},
                )
            for row in fixture["tickets"]:
                await conn.execute(
                    text(
                        "INSERT INTO tickets "
                        "(id, tenant_id, customer_id, order_id, title, description, "
                        "status, resolution, created_at, updated_at, version) "
                        "VALUES "
                        "(:id, :tenant_id, :customer_id, :order_id, :title, "
                        ":description, :status, NULL, :created_at, :updated_at, 1) "
                        "ON CONFLICT (id) DO UPDATE SET "
                        "tenant_id = EXCLUDED.tenant_id, customer_id = EXCLUDED.customer_id, "
                        "order_id = EXCLUDED.order_id, title = EXCLUDED.title, "
                        "description = EXCLUDED.description, status = EXCLUDED.status, "
                        "resolution = NULL, created_at = EXCLUDED.created_at, "
                        "updated_at = EXCLUDED.updated_at, version = 1"
                    ),
                    {**row, "created_at": fixed_time, "updated_at": fixed_time},
                )
            for table in ("users", "orders", "tickets"):
                await advance_sequence(
                    conn, table, max(row["id"] for row in fixture[table])
                )
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database-url",
        default=os.environ.get("EVAL_V3_DATABASE_URL"),
        help="Explicit isolated PostgreSQL async URL; never reads the project .env",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write fixtures after URL, schema, and clean-scope checks",
    )
    args = parser.parse_args()
    fixture = load_fixture()
    url = checked_url(args.database_url)
    checksum = hashlib.sha256(FIXTURE_PATH.read_bytes()).hexdigest()[:12]
    print(
        f"eval v3 fixture {fixture['fixture_version']} ({checksum}): "
        f"{len(fixture['tenants'])} tenants, {len(fixture['users'])} users, "
        f"{len(fixture['orders'])} orders, {len(fixture['tickets'])} tickets"
    )
    print(f"Target: {url.host}:{url.port}/{url.database}")
    print(
        f"M1 memory facts: {len(fixture['memory_facts_for_m1'])} "
        "(manifest only; not inserted into Qdrant)"
    )
    if not args.apply:
        print("Preview only. Pass --apply and EVAL_V3_PASSWORD to seed.")
        return
    password = os.environ.get("EVAL_V3_PASSWORD", "")
    if len(password) < 12:
        raise ValueError("Set EVAL_V3_PASSWORD to at least 12 characters")
    asyncio.run(apply_fixture(url, fixture, password))
    print("Seeded isolated eval v3 PostgreSQL fixture.")


if __name__ == "__main__":
    main()

