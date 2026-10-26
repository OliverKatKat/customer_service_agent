"""Read observability data for v1.5 synthetic Agent tickets from LangGraph checkpoints."""
from __future__ import annotations

import asyncio
import json
import os
import selectors
import sys
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

PROJECT_ROOT = Path.cwd()
sys.path.insert(0, str(PROJECT_ROOT))
from app.agent.graph import get_agent_app, close_pool  # noqa: E402
from eval_v3.seed import checked_url  # noqa: E402

ROOT = PROJECT_ROOT / "eval_agent"
RESULTS = ROOT / "results"


async def main() -> None:
    url = os.environ["EVAL_V3_DATABASE_URL"]
    checked_url(url)
    if os.environ.get("DATABASE_URL") != url:
        raise RuntimeError("Database is not isolated eval v3")
    dataset = json.loads((ROOT / "cases.json").read_text(encoding="utf-8-sig"))
    fixture = json.loads((ROOT / "fixtures.json").read_text(encoding="utf-8-sig"))
    if dataset["dataset_version"] != "1.5.0" or fixture["tenant"]["id"] != "merchant_eval_agent":
        raise RuntimeError("Unexpected dataset or tenant")
    cases = {case["case_id"]: case for case in dataset["cases"]}

    targets = []
    for path in sorted(RESULTS.glob("agent_*.json")):
        report = json.loads(path.read_text(encoding="utf-8-sig"))
        if report.get("dataset_version") != "1.5.0" or "results" not in report:
            continue
        for result in report["results"]:
            observed = result["observed"]
            if observed.get("submit_status") != 202:
                continue
            case = cases[result["case_id"]]
            if observed["actor"] != case["actor"] or observed["order_id"] != case["order_id"]:
                raise RuntimeError("Observed case identity mismatch")
            targets.append((path.name, result))

    if len(targets) != 48 or len({r["observed"]["ticket_id"] for _, r in targets}) != 48:
        raise RuntimeError(f"Expected 48 unique accepted synthetic tickets, got {len(targets)}")

    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            for _, result in targets:
                observed = result["observed"]
                row = (await conn.execute(text(
                    "SELECT tenant_id, customer_id, order_id FROM tickets WHERE id=:id"
                ), {"id": observed["ticket_id"]})).one_or_none()
                if row is None or row[0] != fixture["tenant"]["id"] or row[1] != observed["actor"] or (
                    (row[2] or None) != (observed["order_id"] or None)
                ):
                    raise RuntimeError(f"Ticket identity mismatch: {observed['ticket_id']}")

        app = await get_agent_app()
        entries = []
        for report_name, result in targets:
            observed = result["observed"]
            state = await app.aget_state({
                "configurable": {"thread_id": str(observed["ticket_id"])}
            })
            if not state.values:
                raise RuntimeError(f"Missing checkpoint: {observed['ticket_id']}")
            calls = []
            usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "cache_read_tokens": 0}
            model_calls = 0
            raw_reply = None
            for message in state.values.get("messages", []):
                if type(message).__name__ != "AIMessage":
                    continue
                for call in getattr(message, "tool_calls", []) or []:
                    calls.append(call.get("name"))
                metadata = getattr(message, "usage_metadata", None) or {}
                if metadata:
                    model_calls += 1
                    for key in ("input_tokens", "output_tokens", "total_tokens"):
                        usage[key] += int(metadata.get(key) or 0)
                    usage["cache_read_tokens"] += int(
                        (metadata.get("input_token_details") or {}).get("cache_read") or 0
                    )
                if not getattr(message, "tool_calls", None):
                    raw_reply = str(message.content)
            entries.append({
                "report": report_name,
                "case_id": result["case_id"],
                "ticket_id": observed["ticket_id"],
                "task_id": observed["task_id"],
                "tool_call_sequence": calls,
                "model_calls": model_calls,
                "usage": usage,
                "raw_model_reply_before_postflight": raw_reply,
                "postflight_reply_changed": raw_reply is not None and raw_reply != observed.get("reply"),
            })
    finally:
        await close_pool()
        await engine.dispose()

    summary = {
        "dataset_version": "1.5.0",
        "scope": "Read-only extraction from isolated synthetic LangGraph checkpoints; no new model calls",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "accepted_tickets": len(entries),
        "model_calls": sum(e["model_calls"] for e in entries),
        "input_tokens": sum(e["usage"]["input_tokens"] for e in entries),
        "output_tokens": sum(e["usage"]["output_tokens"] for e in entries),
        "total_tokens": sum(e["usage"]["total_tokens"] for e in entries),
        "cache_read_tokens": sum(e["usage"]["cache_read_tokens"] for e in entries),
        "postflight_reply_changed": sum(e["postflight_reply_changed"] for e in entries),
        "entries": entries,
    }
    output = RESULTS / f"checkpoint_observability_v1_5_{datetime.now(timezone.utc):%Y%m%d_%H%M%S_%fZ}.json"
    output.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Report: {output}")
    print(f"Accepted tickets: {summary['accepted_tickets']}; model calls: {summary['model_calls']}; total tokens: {summary['total_tokens']}")
    print(f"Replies changed after checkpoint: {summary['postflight_reply_changed']}")


asyncio.run(main(), loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector()))
