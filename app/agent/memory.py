"""Approximate context budgeting with whole customer turns."""
from __future__ import annotations

import json
from functools import lru_cache

import tiktoken
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage


@lru_cache(maxsize=8)
def _encoder(model_name: str):
    try:
        return tiktoken.encoding_for_model(model_name)
    except KeyError:
        return tiktoken.get_encoding("cl100k_base")


def count_tokens(text: str, model_name: str = "deepseek-v4-flash") -> int:
    """Approximate tokens; cl100k_base is a fallback for unknown model names."""
    if not text:
        return 0
    return len(_encoder(model_name).encode(text, disallowed_special=()))


def count_message_tokens(message: BaseMessage) -> int:
    """Include tool-call payload and a small message envelope allowance."""
    content = message.content if isinstance(message.content, str) else json.dumps(
        message.content, ensure_ascii=False, default=str,
    )
    cost = count_tokens(content) + 4
    tool_calls = getattr(message, "tool_calls", None)
    if tool_calls:
        cost += count_tokens(json.dumps(tool_calls, ensure_ascii=False, default=str))
    return cost


def trim_messages(messages: list[BaseMessage], max_tokens: int = 3000) -> list[BaseMessage]:
    """Keep system messages and as many complete recent customer turns as fit."""
    if not messages:
        return []
    if max_tokens <= 0:
        raise ValueError("max_tokens must be positive")
    system_messages = [message for message in messages if isinstance(message, SystemMessage)]
    chat_history = [message for message in messages if not isinstance(message, SystemMessage)]
    used = sum(count_message_tokens(message) for message in system_messages)

    turns: list[list[BaseMessage]] = []
    current: list[BaseMessage] = []
    for message in chat_history:
        if isinstance(message, HumanMessage) and current:
            turns.append(current)
            current = []
        current.append(message)
    if current:
        turns.append(current)

    retained_turns: list[list[BaseMessage]] = []
    for turn in reversed(turns):
        turn_cost = sum(count_message_tokens(message) for message in turn)
        if used + turn_cost > max_tokens:
            break
        retained_turns.insert(0, turn)
        used += turn_cost
    return system_messages + [message for turn in retained_turns for message in turn]
