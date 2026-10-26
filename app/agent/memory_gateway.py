"""Verify conversation scope and compact only complete, recoverable turns."""
from __future__ import annotations

from uuid import uuid4

from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    RemoveMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.runnables import RunnableConfig
from opentelemetry.propagate import inject

from app.agent.customer_memory import extract_local_preferences
from app.agent.memory import trim_messages
from app.agent.state import AgentState
from app.core.config import settings


def _block(reason: str) -> dict:
    return {
        "messages": [AIMessage(
            content="当前会话上下文暂时无法安全核验或整理，请稍后重试。",
            id=f"gateway_block_{uuid4().hex[:8]}",
        )],
        "gateway_blocked": True,
        "gateway_block_reason": reason,
    }


def _clean_tool_history(messages: list) -> tuple[list, list, bool]:
    """Remove incomplete tool-call batches and orphan tool results as one unit."""
    cleaned = []
    removals = []
    repaired = False
    index = 0
    while index < len(messages):
        message = messages[index]
        if isinstance(message, AIMessage) and message.tool_calls:
            end = index + 1
            while end < len(messages) and isinstance(messages[end], ToolMessage):
                end += 1
            replies = messages[index + 1:end]
            expected = [call.get("id") for call in message.tool_calls]
            received = [reply.tool_call_id for reply in replies]
            if expected and all(isinstance(item, str) and item for item in expected) and (
                len(expected) == len(set(expected))
                and len(received) == len(expected)
                and set(received) == set(expected)
            ):
                cleaned.extend(messages[index:end])
            else:
                removals.extend(messages[index:end])
                repaired = True
            index = end
            continue
        if isinstance(message, ToolMessage):
            removals.append(message)
            repaired = True
        else:
            cleaned.append(message)
        index += 1
    return cleaned, removals, repaired


async def memory_gateway_node(state: AgentState, config: RunnableConfig):
    messages = list(state["messages"])
    context = state.get("context") or {}
    configurable = (config or {}).get("configurable", {})
    tenant_id = context.get("tenant_id")
    customer_id = context.get("customer_id")
    ticket_id = context.get("ticket_id")
    if (
        not isinstance(tenant_id, str) or not tenant_id.strip()
        or not isinstance(customer_id, str) or not customer_id.strip()
        or not isinstance(ticket_id, (str, int)) or not str(ticket_id).isdecimal()
        or configurable.get("tenant_id") != tenant_id
        or configurable.get("customer_id") != customer_id
        or str(configurable.get("ticket_id")) != str(ticket_id)
    ):
        return _block("identity_mismatch")

    cleaned, incomplete, repaired = _clean_tool_history(messages)
    if any(not getattr(message, "id", None) for message in incomplete):
        return _block("unremovable_tool_message")
    retained = trim_messages(cleaned, max_tokens=settings.MEMORY_CONTEXT_MAX_TOKENS)
    last_human = next((message for message in reversed(cleaned) if isinstance(message, HumanMessage)), None)
    if last_human is None or not any(message is last_human for message in retained):
        return _block("current_turn_exceeds_budget")

    retained_objects = {id(message) for message in retained}
    old_messages = [message for message in cleaned if id(message) not in retained_objects]
    if any(not getattr(message, "id", None) for message in old_messages):
        return _block("unremovable_old_message")

    if old_messages:
        old_text = [
            {"type": "human" if isinstance(message, HumanMessage) else "ai", "content": message.content}
            for message in old_messages
            if isinstance(message, (HumanMessage, AIMessage)) and isinstance(message.content, str) and message.content
        ]
        facts = extract_local_preferences(old_text)
        if facts:
            try:
                from app.workers.tasks import extract_and_store_memory

                headers = {}
                inject(headers)
                if isinstance(context.get("request_id"), str):
                    headers["x-request-id"] = context["request_id"]
                extract_and_store_memory.apply_async(
                    kwargs={
                        "tenant_id": tenant_id,
                        "customer_id": customer_id,
                        "source_ticket_id": str(ticket_id),
                        "facts": facts,
                    },
                    headers=headers,
                )
            except Exception:
                return _block("memory_archive_enqueue_failed")

    removals = []
    seen_ids = set()
    for message in [*incomplete, *old_messages]:
        if message.id not in seen_ids:
            removals.append(RemoveMessage(id=message.id))
            seen_ids.add(message.id)
    if repaired:
        removals.append(SystemMessage(
            content="[系统通知] 前次工具调用的结果不完整，已移除该次调用；请重新核验后再回答。",
            id=f"gateway_repair_{uuid4().hex[:8]}",
        ))
    return {
        "messages": removals,
        "gateway_blocked": False,
        "gateway_block_reason": None,
    }
