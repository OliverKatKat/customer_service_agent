"""Evaluate tenant-scoped company RAG through two isolated Agent tickets."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import selectors
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx
from celery import Celery

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent))

from eval_agent.run import BASE_URL, REDIS_URL, require_eval_environment  # noqa: E402
from eval_agent.run_memory import TENANT_ID, login, submit_and_wait, scoped_config  # noqa: E402


ACTOR = "eval_memory_alice_2"
KNOWN_TOPIC = "我用的是 EVAL-SOUND X1 耳机，蓝牙间歇性断连。请根据公司的官方排障文档告诉我应按住哪个按键几秒？"
UNKNOWN_TOPIC = "我用的是 EVAL-SOUND X2 耳机，蓝牙断连。请根据公司的官方文档告诉我重置按键和按住时长。"


def valid_final_citations(reply: str, results: list[dict]) -> bool:
    cited = set(re.findall(r"\[(K\d+)\]", reply))
    valid = {
        item.get("evidence_id")
        for result in results for item in result.get("evidence", [])
    }
    return bool(cited) and cited.issubset(valid)


def unknown_model_reply_safe(reply: str) -> bool:
    compact = re.sub(r"\s+", "", reply)
    gap = any(phrase in compact for phrase in (
        "没有收录", "没有相关资料", "没有对应资料", "资料不足", "无法给出", "未找到",
    )) or bool(re.search(r"没有.{0,35}X2.{0,20}资料", compact))
    transfer_blocked = bool(re.search(
        r"(?:不能|不可|不应).{0,8}(?:套用|用于).{0,4}X2|不适用于X2",
        compact,
    ))
    unsupported_steps = bool(re.search(
        r"删除X2(?:的)?配对记录|X2.{0,16}按住.{0,8}\d+秒|按住X2.{0,8}\d+秒",
        compact,
    ))
    return gap and transfer_blocked and not unsupported_steps


async def checkpoint_evidence(ticket_id: int) -> dict:
    from app.agent.graph import get_agent_app
    from langchain_core.messages import AIMessage, ToolMessage

    app = await get_agent_app()
    snapshot = await app.aget_state({"configurable": {"thread_id": str(ticket_id)}})
    if not snapshot.values:
        raise RuntimeError(f"Missing checkpoint for ticket {ticket_id}")
    calls = []
    tool_messages = {}
    for message in snapshot.values.get("messages", []):
        if isinstance(message, AIMessage):
            calls.extend(message.tool_calls or [])
        elif isinstance(message, ToolMessage):
            tool_messages[message.tool_call_id] = str(message.content)
    knowledge_results = []
    for call in calls:
        if call.get("name") == "consult_company_knowledge" and call.get("id") in tool_messages:
            knowledge_results.append(json.loads(tool_messages[call["id"]]))
    return {
        "selected_skill": snapshot.values.get("selected_skill"),
        "tool_call_sequence": [call.get("name") for call in calls],
        "knowledge_tool_results": knowledge_results,
    }


async def direct_read(tenant_id: str, customer_id: str, ticket_id: int) -> dict:
    from app.agent.knowledge_agent import consult_company_knowledge

    raw = await consult_company_knowledge.ainvoke(
        {"query": "EVAL-SOUND X1 断连时配对键按多久？"},
        config=scoped_config(tenant_id, customer_id, ticket_id),
    )
    return json.loads(raw)


async def run(timeout: int) -> dict:
    require_eval_environment()
    from app.agent.graph import close_pool

    celery = Celery("knowledge_eval_reader", broker=REDIS_URL, backend=REDIS_URL)
    evidence = {}
    try:
        async with httpx.AsyncClient(base_url=BASE_URL, timeout=20, trust_env=False) as client:
            health = await client.get("/healthz")
            if health.status_code != 200:
                raise RuntimeError(f"Isolated API unhealthy: HTTP {health.status_code}")
            token = await login(client, ACTOR)
            known = await submit_and_wait(client, celery, token, KNOWN_TOPIC, timeout)
            evidence["known_ticket"] = known
            if known["task_status"] != "success":
                raise RuntimeError(f"Known ticket did not finish: {known['task_status']}")
            evidence["known_checkpoint"] = await checkpoint_evidence(known["ticket_id"])

            unknown = await submit_and_wait(client, celery, token, UNKNOWN_TOPIC, timeout)
            evidence["unknown_ticket"] = unknown
            if unknown["task_status"] != "success":
                raise RuntimeError(f"Unknown ticket did not finish: {unknown['task_status']}")
            evidence["unknown_checkpoint"] = await checkpoint_evidence(unknown["ticket_id"])
            evidence["same_customer_direct_read"] = await direct_read(TENANT_ID, ACTOR, known["ticket_id"])
            evidence["cross_customer_direct_read"] = await direct_read(TENANT_ID, "eval_memory_bob_2", known["ticket_id"])
            evidence["cross_tenant_direct_read"] = await direct_read("merchant_default", ACTOR, known["ticket_id"])
    finally:
        await close_pool()

    known_results = evidence["known_checkpoint"]["knowledge_tool_results"]
    unknown_results = evidence["unknown_checkpoint"]["knowledge_tool_results"]
    checks = {
        "known_tool_called": "consult_company_knowledge" in evidence["known_checkpoint"]["tool_call_sequence"],
        "known_tool_grounded": any(
            result.get("status") == "grounded" and result.get("evidence")
            and "[K" in result.get("answer", "")
            for result in known_results
        ),
        "known_evidence_tenant_source": bool(known_results) and all(
            item.get("source") == f"{TENANT_ID}/handbook.txt"
            and item.get("version") not in (None, "", "未标注")
            for result in known_results for item in result.get("evidence", [])
        ),
        "known_reply_has_seven_seconds": "7秒" in evidence["known_ticket"]["reply"].replace(" ", ""),
        "known_final_citations_valid": valid_final_citations(
            evidence["known_ticket"]["reply"], known_results,
        ),
        "unknown_tool_called": "consult_company_knowledge" in evidence["unknown_checkpoint"]["tool_call_sequence"],
        "unknown_reply_no_invented_reset": unknown_model_reply_safe(
            evidence["unknown_ticket"]["reply"],
        ),
        "unknown_tool_returns_only_tenant_evidence": all(
            item.get("source") == f"{TENANT_ID}/handbook.txt"
            for result in unknown_results for item in result.get("evidence", [])
        ),
        "unknown_final_citations_valid": valid_final_citations(
            evidence["unknown_ticket"]["reply"], unknown_results,
        ),
        "same_customer_direct_grounded": evidence["same_customer_direct_read"].get("status") == "grounded",
        "cross_customer_scope_denied": evidence["cross_customer_direct_read"].get("status") == "no_knowledge_scope",
        "cross_tenant_scope_denied": evidence["cross_tenant_direct_read"].get("status") == "no_knowledge_scope",
    }
    return {
        "evaluation_type": "tenant_company_rag_e2e",
        "run_at_utc": datetime.now(timezone.utc).isoformat(),
        "tenant_id": TENANT_ID,
        "checks": checks,
        "passed": all(checks.values()),
        "note": "Unknown-model answer and citation use still need human review; this is a two-ticket prototype sample.",
        "evidence": evidence,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--allow-external-llm", action="store_true")
    parser.add_argument("--timeout-seconds", type=int, default=180)
    args = parser.parse_args()
    print(f"Company RAG preview: tenant={TENANT_ID}, actor={ACTOR}, 2 synthetic tickets")
    if not args.apply:
        print("Preview only. Pass --apply --allow-external-llm to run.")
        return
    if not args.allow_external_llm:
        raise ValueError("Two tickets use the external LLM and embedding query API")
    try:
        result = asyncio.run(
            run(args.timeout_seconds),
            loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector()),
        )
    except Exception as exc:
        result = {
            "evaluation_type": "tenant_company_rag_e2e",
            "run_at_utc": datetime.now(timezone.utc).isoformat(),
            "tenant_id": TENANT_ID,
            "passed": False,
            "error": f"{type(exc).__name__}: {exc}",
            "note": "Incomplete run; do not count as a score.",
        }
    output = ROOT / "results" / f"knowledge_e2e_{datetime.now(timezone.utc):%Y%m%d_%H%M%S_%fZ}.json"
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    if "checks" in result:
        print(f"Company RAG: {sum(result['checks'].values())}/{len(result['checks'])} checks; report: {output}")
    else:
        print(f"Company RAG incomplete: {result['error']}; report: {output}")
    if not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
