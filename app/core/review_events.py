"""Stable identifiers and local event keys for the review workflow."""
from __future__ import annotations

import uuid


REVIEW_EVENT_TTL_SECONDS = 7 * 24 * 60 * 60


def approved_task_id(review_id: int) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"agent-review:{review_id}:approved"))


def review_event_key(tenant_id: str) -> str:
    return f"agent:review_events:{tenant_id}"