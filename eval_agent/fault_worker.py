"""Celery entry point for deterministic M4 fault cases only."""
import json
import os
from pathlib import Path

from langchain_core.messages import AIMessage

from app.workers.celery_app import celery_app
import app.workers.tasks as tasks


if (
    not os.getenv("EVAL_V3_DATABASE_URL")
    or os.getenv("DATABASE_URL") != os.getenv("EVAL_V3_DATABASE_URL")
    or os.getenv("REDIS_URL") != "redis://127.0.0.1:56379/0"
):
    raise RuntimeError("M4 fault worker requires the isolated eval v3 environment")

dataset = json.loads(
    Path(__file__).with_name("fault_cases.json").read_text(encoding="utf-8-sig")
)
cases = {case["case_id"]: case for case in dataset["cases"]}


class FaultAgent:
    async def ainvoke(self, inputs, config=None):
        context = (config or {}).get("configurable", {})
        if (
            context.get("tenant_id") != "merchant_eval_agent"
            or context.get("customer_id") != "eval_agent_alice"
        ):
            raise PermissionError("M4 fault case requires the synthetic actor")

        topic = str(inputs["messages"][0].content)
        matches = [
            case for case_id, case in cases.items()
            if topic.startswith(f"[{case_id}] ")
        ]
        if len(matches) != 1:
            raise ValueError("Unknown M4 fault case")

        matched_case = matches[0]
        if matched_case.get("setup_review"):
            from app.agent.tools import process_large_refund
            await process_large_refund.ainvoke(
                {"order_id": matched_case["order_id"], "amount": matched_case["amount"]},
                config=config,
            )
        return {
            "messages": [
                *inputs["messages"],
                AIMessage(content=matched_case["forced_reply"]),
            ]
        }


async def get_fault_agent():
    return FaultAgent()


tasks.get_agent_app = get_fault_agent