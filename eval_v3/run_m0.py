"""Run deterministic M0 tenant-boundary cases without an LLM or Celery worker.

HTTP calls use the real FastAPI routes with a database outer transaction that
rolls back after evaluation. The Celery dispatch and rate limiter are replaced
only within this process so no external model or background job is triggered.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from seed import checked_url
from contract import load_dataset

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent))
CASES = ROOT / "cases.json"
FIXTURE = ROOT / "fixtures.json"


def require_eval_environment() -> None:
    eval_url = os.environ.get("EVAL_V3_DATABASE_URL", "")
    checked_url(eval_url)
    if os.environ.get("DATABASE_URL") != eval_url:
        raise RuntimeError("DATABASE_URL must equal the guarded eval v3 URL")
    if os.environ.get("REDIS_URL") != "redis://127.0.0.1:56379/0":
        raise RuntimeError("REDIS_URL must target isolated eval v3 Redis")
    if os.environ.get("QDRANT_URL") != "http://127.0.0.1:56333":
        raise RuntimeError("QDRANT_URL must target isolated eval v3 Qdrant")
    if len(os.environ.get("EVAL_V3_PASSWORD", "")) < 12:
        raise RuntimeError("EVAL_V3_PASSWORD is required")


async def run() -> dict:
    require_eval_environment()
    from app.main import app
    from app.core.db import engine, get_db
    from app.api.v1 import endpoints
    from app.agent.refund_tools import inspect_refund_request
    from app.core.task_events import task_event_key, task_owner_key

    payload = load_dataset(CASES)
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8-sig"))
    cases = [case for case in payload["cases"] if case["milestone"] == "M0"]
    results = []
    tokens: dict[str, str] = {}
    dependencies: dict[str, str] = {}
    created_tasks: list[str] = []
    password = os.environ["EVAL_V3_PASSWORD"]

    async with engine.connect() as connection:
        outer = await connection.begin()
        # Validate fixture-owned rows, not global table sizes. Demo runs may add
        # unrelated orders and tickets to the same isolated eval database.
        identity_columns = {
            "users": ("tenant_id", "username", "role", "is_active"),
            "orders": ("tenant_id", "customer_id", "order_id", "status", "amount"),
            "tickets": ("tenant_id", "customer_id", "order_id"),
        }
        for table, columns in identity_columns.items():
            projection = ", ".join(columns)
            for fixture_row in fixture[table]:
                row = (await connection.execute(
                    text(f"SELECT {projection} FROM {table} WHERE id=:id"),
                    {"id": fixture_row["id"]},
                )).mappings().one_or_none()
                if row is None or any(row[key] != fixture_row[key] for key in columns):
                    await outer.rollback()
                    raise RuntimeError(
                        f"Eval v3 fixture row changed or missing: {table} id={fixture_row['id']}"
                    )

        async def eval_db():
            async with AsyncSession(
                bind=connection,
                expire_on_commit=False,
                join_transaction_mode="create_savepoint",
            ) as session:
                yield session

        async def allow_eval_requests(*_args, **_kwargs):
            return True, 0

        def fake_dispatch(*_args, **kwargs):
            return SimpleNamespace(id=kwargs["task_id"])

        old_rate_limit = endpoints.rate_limiter.is_allowed
        old_dispatch = endpoints.process_agent_task.apply_async
        app.dependency_overrides[get_db] = eval_db
        endpoints.rate_limiter.is_allowed = allow_eval_requests
        endpoints.process_agent_task.apply_async = fake_dispatch

        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://eval-v3.local"
            ) as client:
                async def token_for(username: str) -> str:
                    if username not in tokens:
                        response = await client.post(
                            "/api/v1/auth/login",
                            data={"username": username, "password": password},
                        )
                        if response.status_code != 200:
                            raise AssertionError(
                                f"Login for {username}: HTTP {response.status_code}"
                            )
                        tokens[username] = response.json()["access_token"]
                    return tokens[username]

                for case in cases:
                    case_id = case["case_id"]
                    try:
                        expected = case["expect"]
                        surface = case["surface"]
                        if surface == "http":
                            request = case["request"]
                            if case_id == "M0-AUTH-INACTIVE":
                                response = await client.post(
                                    request["path"],
                                    data={
                                        "username": request["form_username"],
                                        "password": password,
                                    },
                                )
                                assert response.status_code == expected["http_status"]
                                assert (
                                    "access_token" in response.json()
                                ) == expected["token_issued"]
                            elif request["method"] == "POST":
                                before = await connection.scalar(
                                    text("SELECT count(*) FROM tickets")
                                )
                                actor = case["actor"]
                                response = await client.post(
                                    request["path"],
                                    json=request["json"],
                                    headers={
                                        "Authorization": f"Bearer {await token_for(actor)}"
                                    },
                                )
                                assert response.status_code == expected["http_status"], (
                                    f"HTTP {response.status_code}: {response.text[:200]}"
                                )
                                after = await connection.scalar(
                                    text("SELECT count(*) FROM tickets")
                                )
                                if "ticket_delta" in expected:
                                    assert after - before == expected["ticket_delta"]
                                if "new_ticket" in expected:
                                    body = response.json()
                                    ticket_id = int(body["ticket_id"])
                                    row = (
                                        await connection.execute(
                                            text(
                                                "SELECT tenant_id, customer_id, order_id "
                                                "FROM tickets WHERE id=:id"
                                            ),
                                            {"id": ticket_id},
                                        )
                                    ).mappings().one()
                                    for key, value in expected["new_ticket"].items():
                                        assert row[key] == value, f"{key}: {row[key]}"
                                    dependencies[case_id] = body["task_id"]
                                    created_tasks.append(body["task_id"])
                                order_status = expected.get(
                                    "order_status_before_approval",
                                    expected.get("order_status"),
                                )
                                if order_status:
                                    actual = await connection.scalar(
                                        text(
                                            "SELECT status FROM orders "
                                            "WHERE order_id=:order_id"
                                        ),
                                        {"order_id": request["json"]["order_id"]},
                                    )
                                    assert actual == order_status
                            elif request["method"] == "GET":
                                task_id = dependencies[case["depends_on"]]
                                path = request["path_template"].replace(
                                    "{task_id_from_dependency}", task_id
                                )
                                response = await client.get(
                                    path,
                                    headers={
                                        "Authorization": (
                                            f"Bearer {await token_for(case['actor'])}"
                                        )
                                    },
                                )
                                assert response.status_code == expected["http_status"]
                                if expected.get("no_result_leak"):
                                    body = response.json()
                                    assert "result" not in body
                                    # A renamed response field must not expose
                                    # another customer's order or identity.
                                    serialized = json.dumps(body, ensure_ascii=False)
                                    assert not any(
                                        value in serialized
                                        for value in expected["forbidden_values"]
                                    )
                            else:
                                raise AssertionError("Unsupported M0 HTTP request")
                        elif surface == "tool":
                            raw = await inspect_refund_request.ainvoke(
                                case["args"],
                                config={"configurable": case["trusted_context"]},
                            )
                            actual = json.loads(raw)
                            for key, value in expected.items():
                                if key == "forbidden_values":
                                    for forbidden in value:
                                        assert forbidden not in raw
                                else:
                                    assert actual.get(key) == value, (
                                        f"{key}: {actual.get(key)} != {value}"
                                    )
                        else:
                            raise AssertionError(f"Unsupported M0 surface: {surface}")
                        results.append({"case_id": case_id, "passed": True})
                    except Exception as exc:
                        results.append(
                            {
                                "case_id": case_id,
                                "passed": False,
                                "reason": f"{type(exc).__name__}: {exc}",
                            }
                        )
        finally:
            endpoints.process_agent_task.apply_async = old_dispatch
            endpoints.rate_limiter.is_allowed = old_rate_limit
            app.dependency_overrides.pop(get_db, None)
            await outer.rollback()
            for task_id in created_tasks:
                await endpoints.redis_client.delete(
                    task_owner_key(task_id), task_event_key(task_id)
                )
            await endpoints.redis_client.aclose()
    return {
        "dataset_version": payload["dataset_version"],
        "run_at_utc": datetime.now(timezone.utc).isoformat(),
        "scope": "M0 API and tool tenant boundaries only; Celery and LLM stubbed",
        "evaluation_type": "boundary_regression",
        "declared_case_count": len(cases),
        "supplemental_check_count": 0,
        "total": len(results),
        "passed": sum(item["passed"] for item in results),
        "results": results,
    }


def main() -> None:
    report = asyncio.run(run())
    output_dir = ROOT / "results"
    output_dir.mkdir(exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%SZ")
    path = output_dir / f"m0_{timestamp}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"M0 boundary eval: {report['passed']}/{report['total']} passed")
    for item in report["results"]:
        if not item["passed"]:
            print(f"FAIL {item['case_id']}: {item['reason']}")
    print(f"Report: {path}")
    if report["passed"] != report["total"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
