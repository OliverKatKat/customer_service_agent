"""Model-free memory-gateway boundary and long-context evaluation."""
from __future__ import annotations

import asyncio
import json
import sys
import types
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage, ToolMessage

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import app.agent.memory_gateway as gateway  # noqa: E402
from app.agent.memory import trim_messages as actual_trim  # noqa: E402


TENANT = "merchant_eval_agent"
CUSTOMER = "eval_memory_alice_2"
TICKET = "930475"


def config(*, tenant: str = TENANT, customer: str = CUSTOMER, ticket: str = TICKET) -> dict:
    return {"configurable": {"tenant_id": tenant, "customer_id": customer, "ticket_id": ticket}}


def state(messages: list, *, tenant: str = TENANT) -> dict:
    return {"messages": messages, "context": {
        "tenant_id": tenant, "customer_id": CUSTOMER, "ticket_id": TICKET,
        "request_id": "gateway-eval-local",
    }}


class FakeMemoryTask:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def apply_async(self, **kwargs) -> None:
        self.calls.append(kwargs)


async def run() -> dict:
    checks: dict[str, bool] = {}
    evidence: dict = {}
    fake_task = FakeMemoryTask()
    fake_module = types.ModuleType("app.workers.tasks")
    fake_module.extract_and_store_memory = fake_task
    with patch.dict(sys.modules, {"app.workers.tasks": fake_module}):
        short = await gateway.memory_gateway_node(
            state([HumanMessage(content="蓝牙连接不稳定", id="short-human")]), config()
        )
        checks["short_context_unchanged"] = not short.get("messages") and not short.get("gateway_blocked")

        mismatch = await gateway.memory_gateway_node(
            state([HumanMessage(content="查询客户资料", id="mismatch-human")]),
            config(tenant="merchant_default"),
        )
        evidence["mismatch"] = {"blocked": mismatch.get("gateway_blocked"),
                                "message_count": len(mismatch.get("messages", []))}
        checks["identity_mismatch_blocks_planner"] = mismatch.get("gateway_blocked") is True

        old_turn = [
            HumanMessage(content="我希望以后请通过邮件联系我。", id="old-human"),
            AIMessage(content="已收到", id="old-ai"),
        ]
        recent_turn = [
            HumanMessage(content="耳机蓝牙连接不稳定，请帮我排查。" * 3, id="new-human"),
            AIMessage(content="请提供型号。", id="new-ai"),
        ]
        with patch.object(gateway, "trim_messages", side_effect=lambda messages, max_tokens: actual_trim(messages, 85)):
            compacted = await gateway.memory_gateway_node(state(old_turn + recent_turn), config())
        removed = {m.id for m in compacted.get("messages", []) if isinstance(m, RemoveMessage)}
        evidence["compaction"] = {"removed_ids": sorted(removed), "enqueue_count": len(fake_task.calls)}
        checks["old_turn_removed_atomically"] = removed == {"old-human", "old-ai"}
        checks["preference_enqueue_scoped"] = bool(fake_task.calls) and (
            fake_task.calls[-1].get("kwargs", {}).get("tenant_id") == TENANT
            and fake_task.calls[-1].get("kwargs", {}).get("customer_id") == CUSTOMER
            and fake_task.calls[-1].get("kwargs", {}).get("source_ticket_id") == TICKET
            and ("contact_preference", "联系偏好：邮件联系") in fake_task.calls[-1].get("kwargs", {}).get("facts", [])
        )

        with patch.object(fake_task, "apply_async", side_effect=RuntimeError("queue unavailable")):
            with patch.object(gateway, "trim_messages", side_effect=lambda messages, max_tokens: actual_trim(messages, 85)):
                enqueue_failed = await gateway.memory_gateway_node(state(old_turn + recent_turn), config())
        evidence["enqueue_failure"] = {
            "blocked": enqueue_failed.get("gateway_blocked"),
            "reason": enqueue_failed.get("gateway_block_reason"),
            "removed_ids": [m.id for m in enqueue_failed.get("messages", []) if isinstance(m, RemoveMessage)],
        }
        checks["enqueue_failure_keeps_history"] = (
            enqueue_failed.get("gateway_blocked") is True
            and enqueue_failed.get("gateway_block_reason") == "memory_archive_enqueue_failed"
            and not evidence["enqueue_failure"]["removed_ids"]
        )

        incomplete_tools = [
            HumanMessage(content="请查询这个问题", id="tool-human"),
            AIMessage(content="", id="tool-ai", tool_calls=[
                {"name": "lookup", "args": {}, "id": "call-one"},
                {"name": "lookup", "args": {}, "id": "call-two"},
            ]),
            ToolMessage(content="result one", tool_call_id="call-one", id="tool-result-one"),
            AIMessage(content="答复", id="tool-final"),
        ]
        repaired = await gateway.memory_gateway_node(state(incomplete_tools), config())
        repaired_ids = {m.id for m in repaired.get("messages", []) if isinstance(m, RemoveMessage)}
        evidence["incomplete_tool_pair"] = {"removed_ids": sorted(repaired_ids)}
        checks["incomplete_tool_pair_removed"] = {"tool-ai", "tool-result-one"}.issubset(repaired_ids)

        oversized = [HumanMessage(content="蓝牙故障描述。" * 80, id="large-human")]
        with patch.object(gateway, "trim_messages", side_effect=lambda messages, max_tokens: actual_trim(messages, 50)):
            too_large = await gateway.memory_gateway_node(state(oversized), config())
        evidence["oversized_current_turn"] = {"blocked": too_large.get("gateway_blocked"),
                                                "message_count": len(too_large.get("messages", []))}
        checks["oversized_current_turn_blocks_planner"] = too_large.get("gateway_blocked") is True

    return {
        "evaluation_type": "memory_gateway_long_context",
        "run_at_utc": datetime.now(timezone.utc).isoformat(),
        "scope": "Model-free synthetic messages; Celery memory task replaced with a recorder; no DB writes",
        "checks": checks,
        "passed": sum(checks.values()),
        "total": len(checks),
        "evidence": evidence,
    }


def main() -> None:
    result = asyncio.run(run())
    output_dir = Path(__file__).resolve().parent / "results"
    output_dir.mkdir(exist_ok=True)
    path = output_dir / f"gateway_{datetime.now(timezone.utc):%Y%m%d_%H%M%S_%fZ}.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Memory gateway: {result['passed']}/{result['total']}; report: {path}")
    for name, passed in result["checks"].items():
        if not passed:
            print(f"FAIL {name}")
    if result["passed"] != result["total"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
