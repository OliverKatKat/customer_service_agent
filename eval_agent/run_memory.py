"""Evaluate customer preference memory through the isolated API and worker."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import selectors
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from celery import Celery

ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent
sys.path.insert(0, str(PROJECT_ROOT))

from eval_agent.run import (  # noqa: E402
    BASE_URL,
    REDIS_URL,
    await_task,
    require_eval_environment,
)
from eval_agent.seed import apply_fixture  # noqa: E402


TENANT_ID = "merchant_eval_agent"
ALICE = "eval_memory_alice"
BOB = "eval_memory_bob"
FIXTURE = {
    "tenant": {"id": TENANT_ID, "name": "Agent E2E Evaluation Merchant"},
    "users": [
        {"id": 941101, "username": ALICE, "role": "user"},
        {"id": 941102, "username": BOB, "role": "user"},
    ],
    "orders": [],
}
SOURCE_TOPIC = "我的耳机蓝牙连接不稳定，请给排查建议。我希望以后请通过邮件联系我。"
RECALL_TOPIC = "我的耳机蓝牙连接仍不稳定。你记得我之前希望用什么方式联系吗？"
NO_MATCH_TOPIC = "我的耳机蓝牙连接不稳定。你记得我之前希望用什么方式联系吗？"
EXPECTED_MEMORY = "联系偏好：邮件联系"
FALSE_REGISTRATION_CLAIM = re.compile(
    r"(?:没有|缺少).{0,24}(?:登记|记录).{0,12}(?:偏好|联系方式).{0,5}工具"
    r"|(?:无法|不能).{0,16}(?:正式)?登记.{0,12}(?:偏好|联系方式)"
    r"|(?:人工客服).{0,20}(?:确认并登记|确认后才能正式记录|才能正式记录)"
    r"|建议.{0,15}人工客服.{0,15}确认并登记"
)


def configure_identity(run_number: int) -> None:
    """Use fresh synthetic customers for each run, preserving earlier evidence."""
    global ALICE, BOB, FIXTURE
    if not 0 <= run_number <= 1000:
        raise ValueError("run-number must be between 0 and 1000")
    if run_number:
        ALICE = f"eval_memory_alice_{run_number}"
        BOB = f"eval_memory_bob_{run_number}"
    FIXTURE = {
        "tenant": {"id": TENANT_ID, "name": "Agent E2E Evaluation Merchant"},
        "users": [
            {"id": 941101 + 2 * run_number, "username": ALICE, "role": "user"},
            {"id": 941102 + 2 * run_number, "username": BOB, "role": "user"},
        ],
        "orders": [],
    }


async def login(client: httpx.AsyncClient, username: str) -> str:
    response = await client.post(
        "/api/v1/auth/login",
        data={"username": username, "password": os.environ["EVAL_V3_PASSWORD"]},
    )
    if response.status_code != 200 or response.json().get("tenant_id") != TENANT_ID:
        raise RuntimeError(f"Synthetic login failed or tenant mismatch: {username}, HTTP {response.status_code}")
    return response.json()["access_token"]


async def submit_and_wait(
    client: httpx.AsyncClient, celery: Celery, token: str, topic: str, timeout: int
) -> dict:
    response = await client.post(
        "/api/v1/submit",
        json={"topic": topic, "order_id": ""},
        headers={"Authorization": f"Bearer {token}"},
    )
    if response.status_code == 429:
        await asyncio.sleep(min(int(response.headers.get("Retry-After", "1")), 65))
        response = await client.post(
            "/api/v1/submit",
            json={"topic": topic, "order_id": ""},
            headers={"Authorization": f"Bearer {token}"},
        )
    if response.status_code != 202:
        raise RuntimeError(f"Synthetic submit failed: HTTP {response.status_code}: {response.text[:180]}")
    body = response.json()
    task = await await_task(celery, str(body["task_id"]), timeout)
    return {
        "ticket_id": int(body["ticket_id"]),
        "task_id": str(body["task_id"]),
        "request_id": body.get("request_id"),
        **task,
    }


async def wait_for_source(ticket_id: int, timeout: int) -> dict | None:
    from app.agent.customer_memory import read_scoped_preferences

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        documents = await asyncio.to_thread(read_scoped_preferences, TENANT_ID, ALICE)
        for document in documents:
            metadata = document["metadata"]
            if (
                document["text"] == EXPECTED_MEMORY
                and str(metadata.get("source_ticket_id")) == str(ticket_id)
            ):
                return document
        await asyncio.sleep(2)
    return None


async def wait_for_memory_job(celery: Celery, task_id: str | None, timeout: int) -> dict:
    if not task_id:
        return {"state": "MISSING", "stored": None}
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = celery.AsyncResult(task_id)
        if result.state in {"SUCCESS", "FAILURE", "REVOKED"}:
            payload = result.result
            return {
                "state": result.state,
                "stored": payload.get("stored") if isinstance(payload, dict) else None,
            }
        await asyncio.sleep(1)
    return {"state": "TIMEOUT", "stored": None}


def scoped_config(tenant_id: str, customer_id: str, ticket_id: int) -> dict:
    return {"configurable": {
        "tenant_id": tenant_id,
        "customer_id": customer_id,
        "ticket_id": str(ticket_id),
    }}


async def direct_read(tenant_id: str, customer_id: str, ticket_id: int) -> dict:
    from app.agent.tools import search_user_memory

    raw = await search_user_memory.ainvoke(
        {"query": "之前希望怎么联系？"},
        config=scoped_config(tenant_id, customer_id, ticket_id),
    )
    return json.loads(raw)


async def checkpoint_evidence(ticket_id: int) -> dict:
    from app.agent.graph import get_agent_app
    from langchain_core.messages import AIMessage, ToolMessage

    app = await get_agent_app()
    snapshot = await app.aget_state({"configurable": {"thread_id": str(ticket_id)}})
    if not snapshot.values:
        raise RuntimeError(f"Missing LangGraph checkpoint for ticket {ticket_id}")
    calls = []
    tool_messages = {}
    for message in snapshot.values.get("messages", []):
        if isinstance(message, AIMessage):
            for call in message.tool_calls or []:
                calls.append({"id": call.get("id"), "name": call.get("name")})
        elif isinstance(message, ToolMessage):
            tool_messages[message.tool_call_id] = str(message.content)
    memory_results = []
    for call in calls:
        if call["name"] == "search_user_memory":
            raw = tool_messages.get(call["id"])
            if raw:
                memory_results.append(json.loads(raw))
    return {
        "selected_skill": snapshot.values.get("selected_skill"),
        "tool_call_sequence": [call["name"] for call in calls],
        "memory_tool_results": memory_results,
    }


async def run(timeout: int) -> dict:
    require_eval_environment()
    from app.agent.graph import close_pool
    from app.agent.customer_memory import read_scoped_preferences

    await apply_fixture(FIXTURE, os.environ["EVAL_V3_PASSWORD"])
    celery = Celery("memory_eval_reader", broker=REDIS_URL, backend=REDIS_URL)
    evidence = {}
    try:
        async with httpx.AsyncClient(base_url=BASE_URL, timeout=20, trust_env=False) as client:
            health = await client.get("/healthz")
            if health.status_code != 200:
                raise RuntimeError(f"Isolated API unhealthy: HTTP {health.status_code}")
            alice_token = await login(client, ALICE)
            bob_token = await login(client, BOB)

            for customer_id in (ALICE, BOB):
                existing = await asyncio.to_thread(read_scoped_preferences, TENANT_ID, customer_id)
                if existing:
                    raise RuntimeError(
                        f"{customer_id} already has memory; use fresh synthetic identities before rerunning"
                    )

            source = await submit_and_wait(client, celery, alice_token, SOURCE_TOPIC, timeout)
            evidence["source_ticket"] = source
            if source["task_status"] != "success":
                raise RuntimeError(f"Source ticket did not finish: {source['task_status']}")
            evidence["memory_write_job"] = await wait_for_memory_job(
                celery, source.get("memory_write_task_id"), timeout,
            )
            stored = await wait_for_source(source["ticket_id"], timeout)
            evidence["stored_preference"] = stored
            if stored is None:
                raise RuntimeError("Source preference was not persisted to isolated Qdrant")

            recall = await submit_and_wait(client, celery, alice_token, RECALL_TOPIC, timeout)
            evidence["same_customer_ticket"] = recall
            if recall["task_status"] != "success":
                raise RuntimeError(f"Recall ticket did not finish: {recall['task_status']}")
            evidence["same_customer_checkpoint"] = await checkpoint_evidence(recall["ticket_id"])
            evidence["same_customer_direct_read"] = await direct_read(TENANT_ID, ALICE, recall["ticket_id"])

            no_match = await submit_and_wait(client, celery, bob_token, NO_MATCH_TOPIC, timeout)
            evidence["other_customer_ticket"] = no_match
            if no_match["task_status"] != "success":
                raise RuntimeError(f"Other customer ticket did not finish: {no_match['task_status']}")
            evidence["other_customer_checkpoint"] = await checkpoint_evidence(no_match["ticket_id"])
            evidence["other_customer_direct_read"] = await direct_read(TENANT_ID, BOB, no_match["ticket_id"])
            evidence["cross_customer_direct_read"] = await direct_read(TENANT_ID, ALICE, no_match["ticket_id"])
            evidence["cross_tenant_direct_read"] = await direct_read("merchant_default", ALICE, recall["ticket_id"])
    finally:
        await close_pool()

    same_checkpoint = evidence["same_customer_checkpoint"]
    other_checkpoint = evidence["other_customer_checkpoint"]
    expected_source = str(evidence["source_ticket"]["ticket_id"])
    checks = {
        "background_write_confirmed": evidence["memory_write_job"] == {"state": "SUCCESS", "stored": 1},
        "source_written_with_provenance": evidence["stored_preference"] is not None,
        "same_customer_tool_called": "search_user_memory" in same_checkpoint["tool_call_sequence"],
        "same_customer_tool_grounded": any(
            result.get("status") == "found" and any(
                item.get("text") == EXPECTED_MEMORY
                and item.get("source_ticket_id") == expected_source
                for item in result.get("memories", [])
            ) for result in same_checkpoint["memory_tool_results"]
        ),
        "same_customer_direct_grounded": evidence["same_customer_direct_read"].get("status") == "found"
        and any(
            item.get("text") == EXPECTED_MEMORY and item.get("source_ticket_id") == expected_source
            for item in evidence["same_customer_direct_read"].get("memories", [])
        ),
        "same_customer_reply_uses_preference": "邮件" in evidence["same_customer_ticket"]["reply"],
        "source_reply_does_not_deny_storage": not FALSE_REGISTRATION_CLAIM.search(
            evidence["source_ticket"]["reply"]
        ),
        "same_customer_reply_no_contact_promise": not re.search(
            r"(?:我|我们)(?:会|将).{0,24}(?:邮件|这个方式|这种方式).{0,24}(?:联系|跟进)"
            r"|(?:我|我们)(?:会|将).{0,12}(?:联系|跟进).{0,12}(?:邮件|这个方式|这种方式)",
            evidence["same_customer_ticket"]["reply"],
        ),
        "other_customer_tool_called": "search_user_memory" in other_checkpoint["tool_call_sequence"],
        "other_customer_tool_no_match": any(
            result.get("status") == "no_match" and not result.get("memories")
            for result in other_checkpoint["memory_tool_results"]
        ),
        "other_customer_reply_no_false_registration_claim": not FALSE_REGISTRATION_CLAIM.search(
            evidence["other_customer_ticket"]["reply"]
        ),
        "other_customer_direct_no_match": evidence["other_customer_direct_read"].get("status") == "no_match",
        "cross_customer_scope_denied": evidence["cross_customer_direct_read"].get("status") == "no_memory_scope",
        "cross_tenant_scope_denied": evidence["cross_tenant_direct_read"].get("status") == "no_memory_scope",
    }
    return {
        "evaluation_type": "customer_memory_e2e",
        "run_at_utc": datetime.now(timezone.utc).isoformat(),
        "tenant_id": TENANT_ID,
        "checks": checks,
        "passed": all(checks.values()),
        "note": "Reply meaning and unsupported claims still require human review; token/LLM costs are not scored.",
        "evidence": evidence,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Create synthetic users and submit three tickets")
    parser.add_argument("--allow-external-llm", action="store_true", help="Acknowledge API worker model calls")
    parser.add_argument("--timeout-seconds", type=int, default=180)
    parser.add_argument("--run-number", type=int, default=0, help="Fresh synthetic identities for a rerun")
    args = parser.parse_args()
    configure_identity(args.run_number)
    if args.timeout_seconds < 10:
        raise ValueError("Timeout must be at least 10 seconds")
    print(f"Memory E2E preview: tenant={TENANT_ID}, users={ALICE}/{BOB}, 3 synthetic tickets")
    if not args.apply:
        print("Preview only. Pass --apply --allow-external-llm to run against isolated eval v3.")
        return
    if not args.allow_external_llm:
        raise ValueError("The three worker cases call an external LLM; pass --allow-external-llm")
    try:
        result = asyncio.run(
            run(args.timeout_seconds),
            loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector()),
        )
    except Exception as exc:
        result = {
            "evaluation_type": "customer_memory_e2e",
            "run_at_utc": datetime.now(timezone.utc).isoformat(),
            "tenant_id": TENANT_ID,
            "passed": False,
            "error": f"{type(exc).__name__}: {exc}",
            "note": "Stopped before all evidence could be collected; do not count this as a completed score.",
        }
    output_dir = ROOT / "results"
    output_dir.mkdir(exist_ok=True)
    path = output_dir / f"memory_e2e_{datetime.now(timezone.utc):%Y%m%d_%H%M%S_%fZ}.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    if "checks" in result:
        print(f"Memory E2E: {sum(result['checks'].values())}/{len(result['checks'])} checks; report: {path}")
    else:
        print(f"Memory E2E incomplete: {result['error']}; report: {path}")
    if not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
