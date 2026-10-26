import json
import logging

logger = logging.getLogger("agent.task_events")
TASK_EVENT_TTL_SECONDS = 24 * 60 * 60
TASK_EVENT_MAXLEN = 200


def task_event_key(task_id: str) -> str:
    return f"agent:task_events:{task_id}"


def task_owner_key(task_id: str) -> str:
    return f"agent:task_owner:{task_id}"


def decode_redis_value(value):
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return value


def task_owner_identity(tenant_id: str, user_id: str) -> str:
    """Keep task ownership distinct even when merchants reuse a username."""
    return json.dumps([tenant_id, user_id], ensure_ascii=False, separators=(",", ":"))


async def record_task_owner(redis_client, task_id: str, tenant_id: str, user_id: str) -> None:
    await redis_client.set(
        task_owner_key(task_id),
        task_owner_identity(tenant_id, user_id),
        ex=TASK_EVENT_TTL_SECONDS,
    )


async def publish_task_event(
    redis_client,
    task_id: str,
    status: str,
    message: str,
    *,
    result: str | None = None,
) -> str | None:
    """Append a safe, user-facing lifecycle/progress event to the task stream."""
    event_name = "completed" if status in {"completed", "degraded"} else (
        "failed" if status == "failed" else "progress"
    )
    payload = {"status": status, "message": message}
    if result is not None:
        payload["result"] = result

    key = task_event_key(task_id)
    try:
        pipe = redis_client.pipeline(transaction=False)
        pipe.xadd(
            key,
            {
                "event": event_name,
                "payload": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            },
            maxlen=TASK_EVENT_MAXLEN,
            approximate=True,
        )
        pipe.expire(key, TASK_EVENT_TTL_SECONDS)
        results = await pipe.execute()
        return decode_redis_value(results[0])
    except Exception:
        # Progress delivery must never bring down the Agent task itself.
        logger.warning("Unable to publish task progress event for %s", task_id, exc_info=True)
        return None
