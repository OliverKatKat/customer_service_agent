"""Real PostgreSQL integration tests. Refuses all non-disposable database URLs.

Run migrations first; see docs/workbench-phase1.md. No LLM, Redis, or Qdrant calls.
"""

import asyncio
import json
from datetime import datetime, timezone
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from sqlalchemy.engine import make_url

TEST_URL = os.environ.get("WORKBENCH_TEST_DATABASE_URL", "")
url = make_url(TEST_URL) if TEST_URL else None
if (
    url is None
    or (url.host, url.port, url.database, url.username)
    != ("127.0.0.1", 55458, "workbench_test", "wb_test")
    or os.environ.get("DATABASE_URL") != TEST_URL
):
    raise RuntimeError(
        "Tests require explicit WORKBENCH_TEST_DATABASE_URL=wb_test@127.0.0.1:55458/workbench_test and matching DATABASE_URL"
    )

import bcrypt
import httpx
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.main import app
from app.api.v1 import endpoints
from app.core.config import settings
from app.core.db import engine
from app.core.analytics_db import close_analytics_engines
from app.core.security import create_access_token
from app.models.tenant import Tenant
from app.models.user import User
from app.models.order import Order
from app.models.ticket import Ticket
from app.models.review import ReviewRequest
from app.models.workbench import (
    ServiceCase,
    CaseTicketBinding,
    CaseEvent,
    CaseNote,
    CaseWorkItem,
)

ROOT = Path(__file__).resolve().parents[1]
PASSWORD = "workbench-fixture-password"
HASH = bcrypt.hashpw(PASSWORD.encode(), bcrypt.gensalt(rounds=4)).decode()
READER_URL = (
    "postgresql+asyncpg://wb_reader:wb_reader_local_only@127.0.0.1:55458/workbench_test"
)


class WorkbenchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.admin = create_async_engine(TEST_URL, poolclass=NullPool)
        self.factory = async_sessionmaker(self.admin, expire_on_commit=False)
        async with self.admin.begin() as connection:
            await connection.execute(
                text(
                    "TRUNCATE tenants,users,orders,tickets,tasks RESTART IDENTITY CASCADE"
                )
            )
            exists = await connection.scalar(
                text("SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='wb_reader')")
            )
            if not exists:
                await connection.execute(
                    text("CREATE ROLE wb_reader LOGIN PASSWORD 'wb_reader_local_only'")
                )
            await connection.execute(text("GRANT USAGE ON SCHEMA public TO wb_reader"))
            await connection.execute(text("GRANT SELECT ON case_registry TO wb_reader"))
        async with self.factory() as db:
            db.add_all(
                [
                    Tenant(id="alpha", name="Synthetic alpha", is_active=True),
                    Tenant(id="beta", name="Synthetic beta", is_active=True),
                ]
            )
            await db.flush()
            for tenant, username, role in [
                ("alpha", "alice", "user"),
                ("alpha", "bob", "user"),
                ("alpha", "staff_a", "staff"),
                ("alpha", "analyst_a", "analyst"),
                ("alpha", "admin_a", "admin"),
                ("beta", "eve", "user"),
                ("beta", "staff_b", "staff"),
                ("beta", "analyst_b", "analyst"),
            ]:
                db.add(
                    User(
                        tenant_id=tenant,
                        username=username,
                        role=role,
                        hashed_password=HASH,
                        is_active=True,
                    )
                )
            db.add_all(
                [
                    Order(
                        tenant_id="alpha",
                        order_id="A-1",
                        customer_id="alice",
                        status="paid",
                        amount=199,
                    ),
                    Order(
                        tenant_id="alpha",
                        order_id="B-1",
                        customer_id="bob",
                        status="paid",
                        amount=99,
                    ),
                    Order(
                        tenant_id="beta",
                        order_id="E-1",
                        customer_id="eve",
                        status="paid",
                        amount=499,
                    ),
                ]
            )
            await db.commit()
        settings.ANALYTICS_READONLY_DATABASE_URL = READER_URL
        self.tokens = {}
        self.patchers = [
            patch.object(
                endpoints.rate_limiter,
                "is_allowed",
                new=AsyncMock(return_value=(True, 0)),
            ),
            patch.object(endpoints, "record_task_owner", new=AsyncMock()),
            patch.object(endpoints, "publish_task_event", new=AsyncMock()),
            patch.object(
                endpoints.process_agent_task,
                "apply_async",
                side_effect=lambda **kwargs: SimpleNamespace(id=kwargs["task_id"]),
            ),
            patch.object(
                endpoints.dispatch_review_outbox,
                "apply_async",
                return_value=SimpleNamespace(id="test-delivery"),
            ),
        ]
        for p in self.patchers:
            p.start()
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://workbench.local"
        )

    async def asyncTearDown(self):
        await self.client.aclose()
        for p in reversed(self.patchers):
            p.stop()
        await close_analytics_engines()
        await engine.dispose()
        await self.admin.dispose()

    async def headers(self, user):
        if user not in self.tokens:
            response = await self.client.post(
                "/api/v1/auth/login", data={"username": user, "password": PASSWORD}
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.tokens[user] = response.json()["access_token"]
        return {"Authorization": "Bearer " + self.tokens[user]}

    async def request(self, method, path, user="staff_a", **kwargs):
        return await self.client.request(
            method, "/api/v1" + path, headers=await self.headers(user), **kwargs
        )

    async def submit(self, user="alice", **overrides):
        payload = {
            "topic": "耳机单侧无声，希望申请售后",
            "order_id": {"alice": "A-1", "bob": "B-1", "eve": "E-1"}[user],
            **overrides,
        }
        response = await self.request("POST", "/submit", user, json=payload)
        self.assertEqual(response.status_code, 202, response.text)
        return response.json()

    async def scalar(self, query):
        async with self.admin.connect() as connection:
            return await connection.scalar(query)

    async def test_intake_creates_case_binding_todo_and_audit_atomically(self):
        result = await self.submit()
        self.assertEqual(
            await self.scalar(select(func_count()).select_from(ServiceCase)), 1
        )
        self.assertEqual(
            await self.scalar(select(func_count()).select_from(CaseTicketBinding)), 1
        )
        self.assertEqual(
            await self.scalar(select(func_count()).select_from(CaseWorkItem)), 1
        )
        self.assertEqual(await self.scalar(select(CaseEvent.action)), "created")
        detail = await self.request("GET", "/workbench/cases/" + result["case_id"])
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(
            detail.json()["data"]["tickets"][0]["ticket_id"], result["ticket_id"]
        )
        self.assertEqual(
            detail.json()["data"]["order"]["amount_source"], "legacy_float_unmigrated"
        )

    async def test_explicit_repeat_links_contacts_but_same_order_does_not_auto_merge(
        self,
    ):
        first = await self.submit()
        second = await self.submit(case_id=first["case_id"])
        third = await self.submit()
        self.assertEqual(first["case_id"], second["case_id"])
        self.assertNotEqual(first["case_id"], third["case_id"])
        self.assertEqual(
            await self.scalar(select(func_count()).select_from(ServiceCase)), 2
        )

    async def test_other_customer_cannot_attach_contact_and_failed_intake_rolls_back(
        self,
    ):
        first = await self.submit()
        response = await self.request(
            "POST",
            "/submit",
            "bob",
            json={"topic": "继续咨询", "order_id": "B-1", "case_id": first["case_id"]},
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(await self.scalar(select(func_count()).select_from(Ticket)), 1)
        self.assertEqual(
            await self.scalar(select(func_count()).select_from(CaseTicketBinding)), 1
        )

    async def test_cross_tenant_detail_note_and_work_item_all_fail_closed(self):
        first = await self.submit()
        item = await self.scalar(select(CaseWorkItem.id))
        for method, path, payload in [
            ("GET", "/workbench/cases/" + first["case_id"], None),
            (
                "POST",
                "/workbench/cases/" + first["case_id"] + "/notes",
                {"content": "不能写入"},
            ),
            (
                "PATCH",
                f"/workbench/work-items/{item}",
                {"expected_version": 1, "status": "done"},
            ),
        ]:
            response = await self.request(
                method, path, "staff_b", **({"json": payload} if payload else {})
            )
            self.assertEqual(response.status_code, 404, response.text)
            self.assertEqual(response.json()["error"]["code"], "RESOURCE_NOT_FOUND")
        self.assertEqual(
            await self.scalar(select(func_count()).select_from(CaseNote)), 0
        )

    async def test_role_matrix_and_forged_token_role(self):
        for user in ["alice", "analyst_a"]:
            self.assertEqual(
                (await self.request("GET", "/workbench/todos", user)).status_code, 403
            )
        for user in ["alice", "staff_a"]:
            self.assertEqual(
                (await self.request("GET", "/analytics/overview", user)).status_code,
                403,
            )
        forged = create_access_token(
            {"sub": "alice", "tenant_id": "alpha", "role": "admin"}
        )
        response = await self.client.get(
            "/api/v1/workbench/todos", headers={"Authorization": "Bearer " + forged}
        )
        self.assertEqual(response.status_code, 403)

    async def test_staff_and_analyst_cannot_approve_legacy_refunds(self):
        for user in ["staff_a", "analyst_a"]:
            response = await self.request(
                "POST",
                "/reviews/1/approve",
                user,
                json={
                    "expected_version": 1,
                    "approval_code": settings.ADMIN_APPROVAL_CODE,
                    "reason": "测试审核",
                },
            )
            self.assertEqual(response.status_code, 403)

    async def test_active_role_is_rechecked_after_login(self):
        headers = await self.headers("staff_a")
        async with self.admin.begin() as db:
            await db.execute(
                update(User).where(User.username == "staff_a").values(is_active=False)
            )
        response = await self.client.get("/api/v1/workbench/todos", headers=headers)
        self.assertEqual(response.status_code, 401)

    async def test_note_records_real_actor_and_rejects_tenant_injection(self):
        case = (await self.submit())["case_id"]
        response = await self.request(
            "POST",
            f"/workbench/cases/{case}/notes",
            json={"content": "  已电话核对型号  "},
        )
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(await self.scalar(select(CaseNote.author_id)), "staff_a")
        self.assertEqual(await self.scalar(select(CaseNote.content)), "已电话核对型号")
        response = await self.request(
            "POST",
            f"/workbench/cases/{case}/notes",
            json={"content": "伪造", "tenant_id": "beta"},
        )
        self.assertEqual(response.status_code, 422)
        response = await self.request(
            "POST", f"/workbench/cases/{case}/notes", json={"content": "   "}
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(
            await self.scalar(select(func_count()).select_from(CaseNote)), 1
        )

    async def test_concurrent_case_edits_only_one_version_wins(self):
        case = (await self.submit())["case_id"]
        await self.headers("staff_a")
        responses = await asyncio.gather(
            *[
                self.request(
                    "PATCH",
                    f"/workbench/cases/{case}",
                    json={"expected_version": 1, "case_type": kind},
                )
                for kind in ["refund", "complaint"]
            ]
        )
        self.assertEqual(sorted(r.status_code for r in responses), [200, 409])
        self.assertEqual(
            await self.scalar(
                select(func_count())
                .select_from(CaseEvent)
                .where(CaseEvent.action == "case_updated")
            ),
            1,
        )
        self.assertEqual(await self.scalar(select(ServiceCase.version)), 2)

    async def test_pending_contacts_cannot_be_marked_resolved(self):
        case = (await self.submit())["case_id"]
        response = await self.request(
            "PATCH",
            f"/workbench/cases/{case}",
            json={"expected_version": 1, "status": "resolved", "reason": "已处理完成"},
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(await self.scalar(select(ServiceCase.status)), "open")
        self.assertEqual(await self.scalar(select(CaseWorkItem.status)), "open")

    async def test_approved_but_not_executed_cannot_be_resolved(self):
        first = await self.submit()
        async with self.factory() as db:
            await db.execute(update(Ticket).values(status="resolved"))
            db.add(
                ReviewRequest(
                    tenant_id="alpha",
                    ticket_id=int(first["ticket_id"]),
                    customer_id="alice",
                    order_id="A-1",
                    amount=199,
                    reason="合成审核",
                    status="approved",
                )
            )
            await db.commit()
        response = await self.request(
            "PATCH",
            f"/workbench/cases/{first['case_id']}",
            json={"expected_version": 1, "status": "resolved", "reason": "尝试关闭"},
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            await self.scalar(select(Order.status).where(Order.order_id == "A-1")),
            "paid",
        )

    async def test_verified_execution_can_be_resolved_and_todos_finish(self):
        first = await self.submit()
        async with self.factory() as db:
            await db.execute(update(Ticket).values(status="resolved"))
            await db.execute(
                update(Order).where(Order.order_id == "A-1").values(status="refunded")
            )
            db.add(
                ReviewRequest(
                    tenant_id="alpha",
                    ticket_id=int(first["ticket_id"]),
                    customer_id="alice",
                    order_id="A-1",
                    amount=199,
                    reason="合成审核",
                    status="approved",
                )
            )
            await db.commit()
        response = await self.request(
            "PATCH",
            f"/workbench/cases/{first['case_id']}",
            json={
                "expected_version": 1,
                "status": "resolved",
                "reason": "已核对模拟执行",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsNotNone(response.json()["data"]["resolved_at"])
        self.assertEqual(await self.scalar(select(CaseWorkItem.status)), "done")
        rejected = await self.request(
            "POST",
            "/submit",
            "alice",
            json={"topic": "重复请求", "order_id": "A-1", "case_id": first["case_id"]},
        )
        self.assertEqual(rejected.status_code, 409)
        self.assertEqual(await self.scalar(select(func_count()).select_from(Ticket)), 1)

    async def test_work_item_assignment_and_version_boundaries(self):
        await self.submit()
        item = await self.scalar(select(CaseWorkItem.id))
        response = await self.request(
            "PATCH",
            f"/workbench/work-items/{item}",
            json={"expected_version": 1, "assigned_to": "staff_b"},
        )
        self.assertEqual(response.status_code, 422)
        response = await self.request(
            "PATCH",
            f"/workbench/work-items/{item}",
            json={"expected_version": 1, "assigned_to": "staff_a"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        response = await self.request(
            "PATCH",
            f"/workbench/work-items/{item}",
            json={"expected_version": 1, "status": "done"},
        )
        self.assertEqual(response.status_code, 409)
        response = await self.request(
            "PATCH",
            f"/workbench/work-items/{item}",
            json={"expected_version": 2, "due_at": "2026-10-28T12:00:00"},
        )
        self.assertEqual(response.status_code, 422)

    async def test_cursor_is_bound_to_tenant_and_filters(self):
        await self.submit()
        await self.submit()
        await self.submit()
        first = await self.request("GET", "/workbench/todos?limit=1")
        cursor = first.json()["data"]["next_cursor"]
        second = await self.request(
            "GET", "/workbench/todos", params={"limit": 1, "cursor": cursor}
        )
        self.assertEqual(second.status_code, 200)
        self.assertNotEqual(
            first.json()["data"]["items"][0]["id"],
            second.json()["data"]["items"][0]["id"],
        )
        for user, params in [
            ("staff_b", {"cursor": cursor}),
            ("staff_a", {"cursor": cursor, "state": "done"}),
            ("staff_a", {"cursor": cursor + "bad"}),
        ]:
            self.assertEqual(
                (
                    await self.request("GET", "/workbench/todos", user, params=params)
                ).status_code,
                422,
            )

    async def test_analytics_counts_only_this_tenant_and_retains_unknown(self):
        case = (await self.submit())["case_id"]
        await self.submit("eve")
        await self.submit("eve")
        response = await self.request("GET", "/analytics/overview", "analyst_a")
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()["data"]
        self.assertEqual(data["cases_opened_all"], 1)
        self.assertEqual(data["after_sales_opened_classified"], 0)
        self.assertEqual(data["by_reason"], [{"value": "unknown", "count": 1}])
        await self.request(
            "PATCH",
            f"/workbench/cases/{case}",
            json={"expected_version": 1, "case_type": "refund"},
        )
        data = (await self.request("GET", "/analytics/overview", "analyst_a")).json()[
            "data"
        ]
        self.assertEqual(data["after_sales_opened_classified"], 1)
        self.assertEqual(
            (await self.request("GET", "/analytics/overview", "analyst_b")).json()[
                "data"
            ]["cases_opened_all"],
            2,
        )

    async def test_analytics_does_not_fall_back_and_rejects_write_role(self):
        settings.ANALYTICS_READONLY_DATABASE_URL = ""
        response = await self.request("GET", "/analytics/overview", "analyst_a")
        self.assertEqual(response.status_code, 503)
        settings.ANALYTICS_READONLY_DATABASE_URL = TEST_URL
        response = await self.request("GET", "/analytics/overview", "analyst_a")
        self.assertEqual(response.status_code, 503, response.text)
        self.assertNotIn("wb_local_test_only", response.text)

    async def test_reader_database_role_really_cannot_write(self):
        reader = create_async_engine(READER_URL, poolclass=NullPool)
        try:
            with self.assertRaises(DBAPIError):
                async with reader.begin() as connection:
                    await connection.execute(
                        text("UPDATE case_registry SET status='closed'")
                    )
        finally:
            await reader.dispose()

    async def test_analytics_window_validation(self):
        for params in [
            {"timezone": "not/a-zone"},
            {"start": "2020-01-01T00:00:00+00:00", "end": "2026-01-01T00:00:00+00:00"},
            {"start": "2026-01-01T00:00:00", "end": "2026-01-02T00:00:00"},
        ]:
            self.assertEqual(
                (
                    await self.request(
                        "GET", "/analytics/overview", "analyst_a", params=params
                    )
                ).status_code,
                422,
            )

    async def test_legacy_admin_approval_still_uses_version_and_outbox(self):
        first = await self.submit()
        async with self.factory() as db:
            await db.execute(update(Ticket).values(status="escalated"))
            review = ReviewRequest(
                tenant_id="alpha",
                ticket_id=int(first["ticket_id"]),
                customer_id="alice",
                order_id="A-1",
                amount=199,
                reason="合成申请",
                status="pending",
            )
            db.add(review)
            await db.commit()
            rid = review.id
        payload = {
            "expected_version": 1,
            "approval_code": settings.ADMIN_APPROVAL_CODE,
            "reason": "测试主管审核",
        }
        response = await self.request(
            "POST", f"/reviews/{rid}/approve", "admin_a", json=payload
        )
        self.assertEqual(response.status_code, 202, response.text)
        self.assertTrue(response.json()["prototype_only"])
        again = await self.request(
            "POST", f"/reviews/{rid}/approve", "admin_a", json=payload
        )
        self.assertEqual(again.status_code, 409)
        self.assertEqual(
            await self.scalar(text("SELECT count(*) FROM notification_outbox")), 1
        )
        self.assertEqual(
            await self.scalar(select(Order.status).where(Order.order_id == "A-1")),
            "paid",
        )

    async def test_backfill_is_preview_first_and_repeatable(self):
        async with self.factory() as db:
            db.add(
                Ticket(
                    tenant_id="alpha",
                    customer_id="alice",
                    order_id="A-1",
                    title="旧工单",
                    status="resolved",
                    created_at=datetime(2026, 1, 1),
                )
            )
            await db.commit()

        def run(*args):
            return subprocess.run(
                [sys.executable, "scripts/backfill_workbench_cases.py", *args],
                cwd=ROOT,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )

        preview = await asyncio.to_thread(run)
        self.assertEqual(preview.returncode, 0, preview.stderr)
        self.assertEqual(
            await self.scalar(select(func_count()).select_from(ServiceCase)), 0
        )
        first = await asyncio.to_thread(run, "--apply", "--source-timezone", "UTC")
        self.assertEqual(first.returncode, 0, first.stderr)
        again = await asyncio.to_thread(run, "--apply", "--source-timezone", "UTC")
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual(
            await self.scalar(select(func_count()).select_from(ServiceCase)), 1
        )
        self.assertEqual(
            await self.scalar(select(ServiceCase.opened_at)),
            datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        self.assertEqual(await self.scalar(select(ServiceCase.status)), "open")

    async def test_migration_round_trip_on_empty_tables(self):
        def run(*args):
            return subprocess.run(
                [sys.executable, "-m", "alembic", *args],
                cwd=ROOT,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )

        down = await asyncio.to_thread(run, "downgrade", "b67d21f0c4e8")
        self.assertEqual(down.returncode, 0, down.stderr)
        up = await asyncio.to_thread(run, "upgrade", "head")
        self.assertEqual(up.returncode, 0, up.stderr)
        self.assertEqual(
            await self.scalar(text("SELECT version_num FROM alembic_version")),
            "d34b9c2e7810",
        )

    async def test_migration_refuses_to_drop_live_case_history(self):
        await self.submit()
        result = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-m", "alembic", "downgrade", "b67d21f0c4e8"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(
            await self.scalar(select(func_count()).select_from(ServiceCase)), 1
        )

    async def test_composite_foreign_key_blocks_cross_tenant_binding(self):
        first = await self.submit()
        second = await self.submit("eve")
        with self.assertRaises(IntegrityError):
            async with self.factory() as db:
                db.add(
                    CaseTicketBinding(
                        tenant_id="alpha",
                        case_id=first["case_id"],
                        ticket_id=int(second["ticket_id"]),
                    )
                )
                await db.commit()

    async def test_public_workbench_and_existing_pages_are_static_only(self):
        for path in ["/workbench", "/chat", "/review", "/demo"]:
            response = await self.client.get(path)
            self.assertEqual(response.status_code, 200, path)
            self.assertNotIn(settings.ADMIN_APPROVAL_CODE, response.text)

    async def seed_small_demo(self):
        from app.services.demo_seed import seed_demo

        async with self.factory() as db:
            counts = await seed_demo(db, PASSWORD)
            await db.commit()
            return counts

    async def test_demo_seed_is_small_repeatable_and_preserves_edits(self):
        counts = await self.seed_small_demo()
        self.assertEqual(
            counts,
            {"tenants": 2, "users": 8, "orders": 28, "cases": 14, "contacts": 15},
        )
        async with self.admin.begin() as db:
            await db.execute(
                update(ServiceCase)
                .where(ServiceCase.case_id == "CASE-DEMO-REFUND")
                .values(reason_code="product_fault")
            )
            await db.execute(
                update(Order)
                .where(Order.order_id == "DEMO-ECHO-001")
                .values(status="refunded")
            )
        again = await self.seed_small_demo()
        self.assertTrue(all(value == 0 for value in again.values()))
        self.assertEqual(
            await self.scalar(
                select(ServiceCase.reason_code).where(
                    ServiceCase.case_id == "CASE-DEMO-REFUND"
                )
            ),
            "product_fault",
        )
        self.assertEqual(
            await self.scalar(
                select(Order.status).where(Order.order_id == "DEMO-ECHO-001")
            ),
            "refunded",
        )
        self.assertEqual(
            await self.scalar(
                select(func_count())
                .select_from(CaseTicketBinding)
                .where(CaseTicketBinding.case_id == "CASE-DEMO-REPEAT")
            ),
            2,
        )

    async def test_demo_seed_refuses_colliding_tenant(self):
        from app.services.demo_seed import seed_demo

        async with self.factory() as db:
            db.add(Tenant(id="demo_echo", name="Not a demo tenant", is_active=True))
            await db.commit()
        with self.assertRaises(ValueError):
            async with self.factory() as db:
                await seed_demo(db, PASSWORD)
                await db.commit()
        self.assertEqual(
            await self.scalar(
                select(func_count())
                .select_from(User)
                .where(User.username.like("demo_%"))
            ),
            0,
        )

    async def test_material_facts_are_scoped_and_do_not_approve(self):
        await self.seed_small_demo()
        response = await self.request(
            "POST",
            "/workbench/cases/CASE-DEMO-REFUND/material",
            "demo_echo_staff",
            json={"use_model": False},
        )
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()["data"]
        self.assertEqual(data["mode"], "facts_only")
        self.assertEqual(data["facts"]["order"]["order_id"], "DEMO-ECHO-001")
        self.assertTrue(data["policy_evidence"])
        evidence_text = " ".join(row["text"] for row in data["policy_evidence"])
        self.assertIn("所有退款都需要管理员批准", evidence_text)
        self.assertIn("不能直接执行退款", evidence_text)
        self.assertNotIn("NOVA_ONLY_REFERENCE", response.text)
        self.assertEqual(
            await self.scalar(
                select(Order.status).where(Order.order_id == "DEMO-ECHO-001")
            ),
            "paid",
        )
        self.assertEqual(data["facts"]["reviews"][0]["status"], "pending")
        self.assertEqual(
            (
                await self.request(
                    "POST",
                    "/workbench/cases/CASE-DEMO-REFUND/material",
                    "staff_b",
                    json={},
                )
            ).status_code,
            404,
        )
        self.assertEqual(
            (
                await self.request(
                    "POST",
                    "/workbench/cases/CASE-DEMO-REFUND/material",
                    "demo_echo_alice",
                    json={},
                )
            ).status_code,
            403,
        )

    async def test_invalid_model_policy_citation_uses_honest_fallback(self):
        from app.services.case_material import Draft

        await self.seed_small_demo()
        candidate = Draft(
            customer_summary="客户申请售后",
            attention_points=[],
            suggested_next_step="人工核对",
            policy_citations=["INVENTED-POLICY"],
        )
        with patch(
            "app.services.case_material.ask_model",
            new=AsyncMock(return_value=candidate),
        ):
            response = await self.request(
                "POST",
                "/workbench/cases/CASE-DEMO-REFUND/material",
                "demo_echo_staff",
                json={"use_model": True},
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["data"]["mode"], "template_fallback")
        self.assertNotIn("INVENTED-POLICY", response.text)

    async def test_valid_model_draft_cannot_replace_database_facts(self):
        from app.services.case_material import Draft

        await self.seed_small_demo()
        candidate = Draft(
            customer_summary="客户买错型号，希望取消订单。",
            attention_points=["请核对现有审核请求。"],
            suggested_next_step="查看已有审核",
            policy_citations=["P1"],
        )
        with patch(
            "app.services.case_material.ask_model",
            new=AsyncMock(return_value=candidate),
        ) as model:
            response = await self.request(
                "POST",
                "/workbench/cases/CASE-DEMO-REFUND/material",
                "demo_echo_staff",
                json={"use_model": True},
            )
        self.assertEqual(response.status_code, 200, response.text)
        model.assert_awaited_once()
        data = response.json()["data"]
        self.assertEqual(data["mode"], "ai_draft")
        self.assertEqual(data["facts"]["order"]["status"], "paid")
        self.assertTrue(data["draft_only"])

    async def test_data_only_demo_never_dispatches_or_calls_model(self):
        await self.seed_small_demo()
        settings.DEMO_DATA_ONLY = True
        try:
            with patch(
                "app.services.case_material.ask_model", new=AsyncMock()
            ) as model:
                response = await self.request(
                    "POST",
                    "/workbench/cases/CASE-DEMO-REFUND/material",
                    "demo_echo_staff",
                    json={"use_model": True},
                )
                self.assertEqual(response.json()["data"]["mode"], "facts_only")
                model.assert_not_awaited()
            response = await self.request(
                "POST",
                "/submit",
                "demo_echo_alice",
                json={
                    "topic": "继续申请",
                    "order_id": "DEMO-ECHO-003",
                    "case_id": "CASE-DEMO-REPEAT",
                },
            )
            self.assertEqual(response.status_code, 503)
            endpoints.process_agent_task.apply_async.assert_not_called()
        finally:
            settings.DEMO_DATA_ONLY = False

    async def test_bm25_merchant_policy_does_not_require_vector_collection(self):
        from app.agent import knowledge_agent as knowledge

        await self.seed_small_demo()
        ticket = await self.scalar(
            select(CaseTicketBinding.ticket_id).where(
                CaseTicketBinding.case_id == "CASE-DEMO-REFUND"
            )
        )
        previous = settings.KNOWLEDGE_RETRIEVAL_MODE
        settings.KNOWLEDGE_RETRIEVAL_MODE = "bm25"
        try:
            with (
                patch.object(
                    knowledge.qdrant_client,
                    "collection_exists",
                    side_effect=AssertionError("No vector call expected"),
                ),
                patch.object(
                    knowledge.knowledge_agent_graph,
                    "ainvoke",
                    new=AsyncMock(
                        return_value={
                            "status": "ok",
                            "answer": "合成商家政策",
                            "evidence": [],
                        }
                    ),
                ),
            ):
                result = await knowledge.consult_company_knowledge.ainvoke(
                    {"query": "未发货退款"},
                    config={
                        "configurable": {
                            "tenant_id": "demo_echo",
                            "customer_id": "demo_echo_alice",
                            "ticket_id": str(ticket),
                        }
                    },
                )
                self.assertEqual(json.loads(result)["status"], "ok")
        finally:
            settings.KNOWLEDGE_RETRIEVAL_MODE = previous

    async def test_shipped_demo_order_cannot_be_refunded_by_tool(self):
        from app.agent.tools import process_large_refund

        await self.seed_small_demo()
        ticket = await self.scalar(
            select(CaseTicketBinding.ticket_id).where(
                CaseTicketBinding.case_id == "CASE-DEMO-SHIPPED"
            )
        )
        result = await process_large_refund.ainvoke(
            {"order_id": "DEMO-ECHO-002", "amount": 399.0},
            config={
                "configurable": {
                    "tenant_id": "demo_echo",
                    "customer_id": "demo_echo_alice",
                    "ticket_id": str(ticket),
                }
            },
        )
        self.assertIn("已发货禁止直接退款", result)
        self.assertEqual(
            await self.scalar(
                select(Order.status).where(Order.order_id == "DEMO-ECHO-002")
            ),
            "shipped",
        )
        self.assertEqual(
            await self.scalar(
                select(func_count())
                .select_from(ReviewRequest)
                .where(ReviewRequest.order_id == "DEMO-ECHO-002")
            ),
            0,
        )

    async def prepare_logistics(self):
        from app.services.logistics_seed import seed_logistics
        await self.seed_small_demo()
        async with self.factory() as db:
            await seed_logistics(db)
            exists=await db.scalar(text("SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='wb_logistics_test')"))
            if not exists:
                await db.execute(text("CREATE ROLE wb_logistics_test LOGIN PASSWORD 'logistics_test_only' NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS"))
            await db.execute(text('GRANT USAGE ON SCHEMA logistics TO wb_logistics_test'))
            await db.execute(text('GRANT SELECT ON logistics.shipments,logistics.shipment_events TO wb_logistics_test'))
            await db.commit()
        return 'postgresql://wb_logistics_test:logistics_test_only@127.0.0.1:55458/workbench_test'

    async def logistics_config(self):
        ticket=await self.scalar(select(CaseTicketBinding.ticket_id).where(CaseTicketBinding.case_id=='CASE-DEMO-SHIPPED'))
        return {'configurable':{'tenant_id':'demo_echo','customer_id':'demo_echo_alice','ticket_id':str(ticket)}}

    async def test_logistics_real_mcp_protocol_and_split_packages(self):
        from app.integrations.logistics import query_logistics
        dsn=await self.prepare_logistics()
        data=await query_logistics(dsn,'demo_echo','demo_echo_alice','DEMO-ECHO-002')
        self.assertEqual(data['status'],'ok')
        self.assertEqual(data['provider'],'logistics_mcp')
        self.assertEqual(len(data['shipments']),2)
        self.assertEqual({s['status'] for s in data['shipments']},{'in_transit','delivered'})
        self.assertTrue(all(s['source']=='synthetic' and len(s['events'])==2 for s in data['shipments']))
        self.assertNotEqual(data['queried_at'],data['shipments'][0]['last_event_at'])

    async def test_logistics_provider_scopes_sql_and_parameters(self):
        from app.integrations.logistics import query_logistics
        dsn=await self.prepare_logistics()
        for tenant,customer,order in [('demo_echo','demo_echo_carol','DEMO-ECHO-002'),('demo_nova','demo_nova_alice','DEMO-ECHO-002'),('demo_echo','demo_echo_alice',"' OR 1=1 --")]:
            data=await query_logistics(dsn,tenant,customer,order)
            self.assertEqual(data,{'status':'not_found'})
        self.assertEqual(await self.scalar(text('SELECT count(*) FROM logistics.shipments')),4)

    async def test_logistics_gateway_rejects_forged_context_before_mcp(self):
        from app.agent.logistics_tools import get_logistics_status
        await self.prepare_logistics()
        good=await self.logistics_config()
        with patch('app.agent.logistics_tools.query_logistics',new=AsyncMock()) as provider:
            for config,order in [({},'DEMO-ECHO-002'),({'configurable':{**good['configurable'],'customer_id':'demo_echo_carol'}},'DEMO-ECHO-002'),(good,'DEMO-ECHO-006')]:
                result=json.loads(await get_logistics_status.ainvoke({'order_id':order},config=config))
                self.assertIn(result['status'],{'missing_context','order_unavailable'})
            provider.assert_not_awaited()
        async with self.factory() as db:
            await db.execute(update(User).where(User.username=='demo_echo_alice').values(is_active=False));await db.commit()
        result=json.loads(await get_logistics_status.ainvoke({'order_id':'DEMO-ECHO-002'},config=good))
        self.assertEqual(result['status'],'account_inactive')

    async def test_logistics_executor_refund_allowlist_uses_actual_mcp(self):
        from app.agent.executor import executor_node
        from app.agent.skills.registry import REFUND_TOOL_NAMES
        from langchain_core.messages import AIMessage
        dsn=await self.prepare_logistics()
        old=settings.LOGISTICS_DATABASE_URL;settings.LOGISTICS_DATABASE_URL=dsn
        try:
            self.assertIn('get_logistics_status',REFUND_TOOL_NAMES)
            response=await executor_node({'selected_skill':'refund','messages':[AIMessage(content='',tool_calls=[{'id':'real-mcp-call','name':'get_logistics_status','args':{'order_id':'DEMO-ECHO-002'}}])]},await self.logistics_config())
            data=json.loads(response['messages'][0].content)
            self.assertEqual(data['status'],'ok')
            self.assertEqual(data['provider'],'logistics_mcp')
            self.assertEqual(await self.scalar(select(Order.status).where(Order.order_id=='DEMO-ECHO-002')),'shipped')
            self.assertEqual(await self.scalar(select(func_count()).select_from(ReviewRequest).where(ReviewRequest.order_id=='DEMO-ECHO-002')),0)
        finally:
            settings.LOGISTICS_DATABASE_URL=old

    async def test_logistics_reader_cannot_write_or_read_business_tables(self):
        dsn=await self.prepare_logistics()
        import psycopg
        def check():
            for sql in ['DELETE FROM logistics.shipments','SELECT * FROM public.orders']:
                with psycopg.connect(dsn) as db:
                    with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                        db.execute(sql)
        await asyncio.to_thread(check)

    async def test_logistics_provider_refuses_privileged_database(self):
        from app.integrations.logistics import query_logistics
        await self.prepare_logistics()
        data=await query_logistics(TEST_URL.replace('postgresql+asyncpg://','postgresql://'),'demo_echo','demo_echo_alice','DEMO-ECHO-002')
        self.assertEqual(data,{'status':'unavailable'})

    async def test_logistics_unconfigured_and_timeout_do_not_invent_results(self):
        from app.agent.logistics_tools import get_logistics_status
        dsn=await self.prepare_logistics();config=await self.logistics_config()
        old=settings.LOGISTICS_DATABASE_URL
        try:
            settings.LOGISTICS_DATABASE_URL=''
            with patch('app.agent.logistics_tools.query_logistics',new=AsyncMock()) as provider:
                self.assertEqual(json.loads(await get_logistics_status.ainvoke({'order_id':'DEMO-ECHO-002'},config=config)),{'status':'unavailable'})
                provider.assert_not_awaited()
            settings.LOGISTICS_DATABASE_URL=dsn
            with patch('app.agent.logistics_tools.query_logistics',new=AsyncMock(side_effect=TimeoutError())):
                self.assertEqual(json.loads(await get_logistics_status.ainvoke({'order_id':'DEMO-ECHO-002'},config=config)),{'status':'unavailable'})
        finally: settings.LOGISTICS_DATABASE_URL=old

    async def test_logistics_seed_preserves_updates_and_migration_guard(self):
        from app.services.logistics_seed import seed_logistics
        await self.prepare_logistics()
        async with self.factory() as db:
            await db.execute(text("UPDATE logistics.shipment_events SET description='人工核实后的样本' WHERE tenant_id='demo_echo' AND shipment_id='ECHO-PACK-002-A' AND event_id='002'"))
            self.assertEqual(await seed_logistics(db),0)
            await db.commit()
        self.assertEqual(await self.scalar(text("SELECT description FROM logistics.shipment_events WHERE tenant_id='demo_echo' AND shipment_id='ECHO-PACK-002-A' AND event_id='002'")),'人工核实后的样本')
        result=await asyncio.to_thread(subprocess.run,[sys.executable,'-m','alembic','downgrade','-1'],cwd=ROOT,capture_output=True)
        self.assertNotEqual(result.returncode,0)
        self.assertEqual(await self.scalar(text('SELECT count(*) FROM logistics.shipments')),4)


def func_count():
    from sqlalchemy import func

    return func.count()


if __name__ == "__main__":
    unittest.main(verbosity=2)
