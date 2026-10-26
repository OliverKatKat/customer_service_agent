"""Local, tenant-scoped customer preference memory without external extraction."""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from qdrant_client import QdrantClient, models

from app.core.config import settings

COLLECTION = "user_longterm_memory"
VECTOR_SIZE = 1024
CONTACT_METHODS = (
    ("站内信", "站内信联系"),
    ("邮件", "邮件联系"),
    ("email", "邮件联系"),
    ("e-mail", "邮件联系"),
    ("电话", "电话联系"),
    ("短信", "短信联系"),
    ("微信", "微信联系"),
)
ALLOWED_FACTS = {
    ("contact_preference", "联系偏好：站内信联系"),
    ("contact_preference", "联系偏好：邮件联系"),
    ("contact_preference", "联系偏好：电话联系"),
    ("contact_preference", "联系偏好：短信联系"),
    ("contact_preference", "联系偏好：微信联系"),
    ("communication_preference", "沟通偏好：中文回复"),
    ("communication_preference", "沟通偏好：英文回复"),
    ("communication_preference", "沟通偏好：简短回复"),
}
PREFERENCE_CUES = (
    "我喜欢", "我更喜欢", "我偏好", "我希望",
    "请用", "请通过", "请优先", "以后请",
    "联系我时", "改用", "改成", "i prefer", "please use",
)
REVOKE_CONTACT = re.compile(
    r"(?:忘记|删除|清除|撤回|不再记住).{0,8}(?:联系偏好|联系方式)"
    r"|(?:联系偏好|联系方式).{0,8}(?:忘记|删除|清除|撤回)"
)
REVOKED = "revoked"


def local_client() -> QdrantClient:
    parsed = urlparse(settings.QDRANT_URL)
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in {
        "127.0.0.1", "localhost", "::1"
    }:
        raise RuntimeError("Customer memory Qdrant URL must be local")
    return QdrantClient(url=settings.QDRANT_URL, trust_env=False)


def extract_local_preferences(messages: list[dict]) -> list[tuple[str, str]]:
    """Keep the latest explicit instruction per category, including a deletion request."""
    latest: dict[str, str] = {}
    for item in messages[-20:]:
        if (
            not isinstance(item, dict)
            or item.get("type") != "human"
            or not isinstance(item.get("content"), str)
        ):
            continue
        for sentence in re.split(r"[。！？!?\n;；,，]", item["content"][:1000]):
            lowered = sentence.strip().lower()
            if REVOKE_CONTACT.search(lowered):
                latest["contact_preference"] = ""
                continue
            if not any(cue in lowered for cue in PREFERENCE_CUES):
                continue
            for marker, normalized in CONTACT_METHODS:
                index = lowered.find(marker)
                if index < 0:
                    continue
                preceding = lowered[max(0, index - 5):index]
                if any(negation in preceding for negation in ("不", "别", "不要", "not ", "never ")):
                    continue
                latest["contact_preference"] = f"联系偏好：{normalized}"
                break
            if "请用中文" in lowered or "我希望用中文" in lowered:
                latest["communication_preference"] = "沟通偏好：中文回复"
            if "请用英文" in lowered or "我希望用英文" in lowered:
                latest["communication_preference"] = "沟通偏好：英文回复"
            if "请简短" in lowered or "我希望简短" in lowered:
                latest["communication_preference"] = "沟通偏好：简短回复"
    return list(latest.items())[:3]


def _canonical_id(tenant_id: str, customer_id: str, category: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{tenant_id}|{customer_id}|{category}"))


def store_preferences(
    tenant_id: str,
    customer_id: str,
    source_ticket_id: str,
    facts: list[tuple[str, str]],
) -> int:
    if (
        not isinstance(tenant_id, str) or not tenant_id.strip()
        or not isinstance(customer_id, str) or not customer_id.strip()
        or not str(source_ticket_id).isdecimal()
    ):
        raise ValueError("Memory identity and numeric source ticket are required")
    if not facts:
        return 0
    allowed = ALLOWED_FACTS | {("contact_preference", "")}
    if any(not isinstance(fact, (tuple, list)) or tuple(fact) not in allowed for fact in facts):
        raise ValueError("Customer memory accepts only locally normalized explicit preferences")
    now = datetime.now(timezone.utc).isoformat()
    expires_at = (datetime.now(timezone.utc) + timedelta(days=settings.CUSTOMER_MEMORY_TTL_DAYS)).isoformat()
    client = local_client()
    try:
        ids = [_canonical_id(tenant_id, customer_id, category) for category, _ in facts]
        existing = {str(point.id): point for point in client.retrieve(
            collection_name=COLLECTION, ids=ids, with_payload=True,
        )}
        points = []
        for category, value in facts:
            point_id = _canonical_id(tenant_id, customer_id, category)
            prior = (existing.get(point_id).payload or {}).get("metadata", {}) if point_id in existing else {}
            if prior and (prior.get("tenant_id"), prior.get("customer_id")) != (tenant_id, customer_id):
                raise ValueError("Canonical memory point belongs to another scope")
            if (
                prior.get("tenant_id") == tenant_id
                and prior.get("customer_id") == customer_id
                and str(prior.get("source_ticket_id", "")).isdecimal()
                and int(prior["source_ticket_id"]) > int(source_ticket_id)
            ):
                continue  # A delayed older Celery task must not restore a stale preference.
            vector = [0.0] * VECTOR_SIZE
            vector[0 if category == "contact_preference" else 1] = 1.0
            points.append(models.PointStruct(
                id=point_id,
                vector=vector,
                payload={
                    "page_content": value,
                    "metadata": {
                        "tenant_id": tenant_id,
                        "customer_id": customer_id,
                        "source_ticket_id": source_ticket_id,
                        "type": "user_episodic_memory",
                        "category": category,
                        "timestamp": now,
                        "expires_at": None if not value else expires_at,
                        "status": REVOKED if not value else "active",
                        "source": "local_explicit_preference_rule",
                    },
                },
            ))
        if not points:
            return 0
        client.upsert(collection_name=COLLECTION, points=points, wait=True)
    finally:
        client.close()
    return len(points)


def read_scoped_preferences(tenant_id: str, customer_id: str) -> list[dict]:
    if (
        not isinstance(tenant_id, str) or not tenant_id.strip()
        or not isinstance(customer_id, str) or not customer_id.strip()
    ):
        return []
    scoped_filter = models.Filter(
        must=[
            models.FieldCondition(
                key="metadata.tenant_id",
                match=models.MatchValue(value=tenant_id),
            ),
            models.FieldCondition(
                key="metadata.customer_id",
                match=models.MatchValue(value=customer_id),
            ),
            models.FieldCondition(
                key="metadata.type",
                match=models.MatchValue(value="user_episodic_memory"),
            ),
        ]
    )
    client = local_client()
    latest: dict[str, dict] = {}
    offset = None
    try:
        canonical_ids = [
            _canonical_id(tenant_id, customer_id, category)
            for category in ("contact_preference", "communication_preference")
        ]
        canonical = {str(point.id): point for point in client.retrieve(
            collection_name=COLLECTION, ids=canonical_ids, with_payload=True,
        )}
        scanned = 0
        while scanned < 500:
            points, offset = client.scroll(
                collection_name=COLLECTION,
                scroll_filter=scoped_filter,
                limit=100,
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )
            for point in points:
                scanned += 1
                payload = point.payload or {}
                metadata = payload.get("metadata") or {}
                text = payload.get("page_content")
                if (
                    metadata.get("tenant_id") == tenant_id
                    and metadata.get("customer_id") == customer_id
                    and metadata.get("type") == "user_episodic_memory"
                    and isinstance(text, str)
                ):
                    category = metadata.get("category")
                    if category not in {"contact_preference", "communication_preference"}:
                        continue
                    if str(point.id) in canonical_ids and str(point.id) != _canonical_id(
                        tenant_id, customer_id, category
                    ):
                        continue
                    candidate = {"text": text, "metadata": metadata, "id": str(point.id)}
                    previous = latest.get(category)
                    def rank(record: dict) -> tuple[int, str, int]:
                        meta = record["metadata"]
                        ticket = str(meta.get("source_ticket_id", ""))
                        return (
                            int(ticket) if ticket.isdecimal() else -1,
                            str(meta.get("timestamp", "")),
                            int(record["id"] == _canonical_id(tenant_id, customer_id, category)),
                        )
                    if previous is None or rank(candidate) > rank(previous):
                        latest[category] = candidate
            if offset is None:
                break
        for category in ("contact_preference", "communication_preference"):
            point = canonical.get(_canonical_id(tenant_id, customer_id, category))
            if point is None:
                continue
            payload = point.payload or {}
            metadata = payload.get("metadata") or {}
            if (
                metadata.get("tenant_id") == tenant_id
                and metadata.get("customer_id") == customer_id
                and metadata.get("type") == "user_episodic_memory"
                and metadata.get("category") == category
                and isinstance(payload.get("page_content"), str)
            ):
                latest[category] = {
                    "text": payload.get("page_content", ""),
                    "metadata": metadata,
                    "id": str(point.id),
                }
    finally:
        client.close()
    active = []
    for record in latest.values():
        metadata = record["metadata"]
        if metadata.get("status") == REVOKED or not record["text"]:
            continue
        expiry = metadata.get("expires_at")
        if expiry:
            try:
                if datetime.fromisoformat(expiry) <= datetime.now(timezone.utc):
                    continue
            except (TypeError, ValueError):
                continue
        active.append({"text": record["text"], "metadata": metadata})
    return active
