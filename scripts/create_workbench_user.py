"""Provision staff/analyst accounts from an operator's DB session, never via public HTTP."""

import argparse
import asyncio
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


async def run(args):
    from sqlalchemy import select
    from app.core.db import AsyncSessionLocal, engine
    from app.core.security import get_password_hash
    from app.models.tenant import Tenant
    from app.models.user import User

    try:
        async with AsyncSessionLocal() as db:
            tenant = await db.get(Tenant, args.tenant)
            if tenant is None or not tenant.is_active:
                raise SystemExit("Tenant does not exist or is inactive")
            user = await db.scalar(select(User).where(User.username == args.username))
            if user is not None:
                raise SystemExit(
                    "Username already exists; this command does not change existing accounts"
                )
            if not args.apply:
                print("Preview only: a new account would be created; no changes")
                return
            password = os.environ.get("WORKBENCH_ACCOUNT_PASSWORD", "")
            if len(password) < 12:
                raise SystemExit(
                    "Set WORKBENCH_ACCOUNT_PASSWORD to at least 12 characters"
                )
            db.add(
                User(
                    tenant_id=args.tenant,
                    username=args.username,
                    role=args.role,
                    hashed_password=get_password_hash(password),
                    is_active=True,
                )
            )
            await db.commit()
            print("Created one workbench account; password is not logged")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--username", required=True)
    parser.add_argument("--role", choices=["staff", "analyst"], required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if not 1 <= len(args.username) <= 50:
        parser.error("username must contain 1 to 50 characters")
    asyncio.run(run(args))
