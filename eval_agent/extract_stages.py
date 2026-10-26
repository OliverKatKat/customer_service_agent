"""Read actual LangGraph stage coverage for frozen synthetic M4 tickets."""
from __future__ import annotations

import asyncio
import json
import os
import selectors
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path.cwd()
sys.path.insert(0, str(ROOT))
from app.agent.graph import get_agent_app, close_pool  # noqa: E402
from eval_v3.seed import checked_url  # noqa: E402

RESULTS = ROOT / "eval_agent" / "results"


async def main() -> None:
    url = os.environ["EVAL_V3_DATABASE_URL"]
    checked_url(url)
    if os.environ.get("DATABASE_URL") != url:
        raise RuntimeError("Not the isolated eval v3 database")
    summary_path = RESULTS / "agent_summary_v1_5_20261008_155531_185195Z.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8-sig"))
    checkpoint = json.loads((RESULTS / summary["checkpoint_observability_report"]).read_text(encoding="utf-8-sig"))
    if summary["total_attempts"] != 60 or len(checkpoint["entries"]) != 48:
        raise RuntimeError("Unexpected frozen M4 report")
    app = await get_agent_app()
    entries = []
    try:
        for source in checkpoint["entries"]:
            ticket = source["ticket_id"]
            config = {"configurable": {"thread_id": str(ticket)}}
            state = await app.aget_state(config)
            scheduled = []
            async for snapshot in app.aget_state_history(config):
                scheduled.extend(snapshot.next)
            nodes = sorted(set(scheduled) - {"__start__"})
            route = state.values.get("routing_decision") or {}
            if "intent_guard" not in nodes or route.get("outcome") not in {"allow", "block"}:
                raise RuntimeError(f"Missing intent evidence for ticket {ticket}")
            if route["outcome"] == "block" and any(n in nodes for n in ("memory_gateway", "planner", "tools")):
                raise RuntimeError(f"Blocked ticket reached downstream node {ticket}")
            if route["outcome"] == "allow" and ("memory_gateway" not in nodes or "planner" not in nodes):
                raise RuntimeError(f"Allowed ticket missing planner path {ticket}")
            entries.append({
                "case_id": source["case_id"],
                "ticket_id": ticket,
                "routing_decision": route,
                "visited_nodes": nodes,
                "selected_skill": state.values.get("selected_skill"),
                "tool_call_sequence": source["tool_call_sequence"],
            })
    finally:
        await close_pool()
    counts = Counter(node for entry in entries for node in entry["visited_nodes"])
    tools = Counter(name for entry in entries for name in entry["tool_call_sequence"])
    result = {
        "dataset_version": "1.5.0",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "scope": "Read-only LangGraph checkpoint history for 48 accepted synthetic tickets",
        "total_submissions": summary["total_attempts"],
        "api_rejected_before_graph": summary["rejected_before_worker_attempts"],
        "graph_tickets": len(entries),
        "intent_classified_tickets": counts["intent_guard"],
        "intent_blocked_tickets": sum(e["routing_decision"]["outcome"] == "block" for e in entries),
        "memory_gateway_tickets": counts["memory_gateway"],
        "planner_tickets": counts["planner"],
        "executor_tickets": counts["tools"],
        "tool_calls": dict(tools),
        "knowledge_rag_tool_tickets": sum("consult_company_knowledge" in e["tool_call_sequence"] for e in entries),
        "customer_memory_retrieval_tickets": sum("search_user_memory" in e["tool_call_sequence"] for e in entries),
        "note": "memory_gateway trims/checkpoints context; visiting it does not prove Qdrant retrieval. Intent classifier LLM calls are not included in checkpoint AIMessage token usage.",
        "entries": entries,
    }
    path = RESULTS / f"stage_coverage_v1_5_{datetime.now(timezone.utc):%Y%m%d_%H%M%S_%fZ}.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(path)
    print({k: result[k] for k in ("total_submissions", "api_rejected_before_graph", "intent_classified_tickets", "intent_blocked_tickets", "memory_gateway_tickets", "planner_tickets", "executor_tickets", "knowledge_rag_tool_tickets", "customer_memory_retrieval_tickets")})


asyncio.run(main(), loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector()))
