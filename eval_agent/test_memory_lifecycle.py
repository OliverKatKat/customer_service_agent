"""Local preference lifecycle checks; no Qdrant, database, or model calls."""
from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://unused:unused@127.0.0.1/unused")
os.environ.setdefault("DEEPSEEK_API_KEY", "unused")
os.environ.setdefault("DASHSCOPE_API_KEY", "unused")
os.environ.setdefault("ADMIN_APPROVAL_CODE", "unused")

from app.agent.customer_memory import (  # noqa: E402
    _canonical_id, extract_local_preferences, read_scoped_preferences, store_preferences,
)


class MemoryClient:
    def __init__(self):
        self.points = {}

    def retrieve(self, *, ids, **kwargs):
        return [self.points[point_id] for point_id in ids if point_id in self.points]

    def upsert(self, *, points, **kwargs):
        for point in points:
            self.points[str(point.id)] = SimpleNamespace(id=point.id, payload=point.payload)

    def scroll(self, *, scroll_filter, **kwargs):
        expected = {
            condition.key: condition.match.value for condition in scroll_filter.must
        }
        records = [
            point for point in self.points.values()
            if all(
                point.payload["metadata"].get(key.removeprefix("metadata.")) == value
                for key, value in expected.items()
            )
        ]
        return records, None

    def close(self):
        pass


class MemoryLifecycleTests(unittest.TestCase):
    def test_latest_explicit_change_and_revoke_extraction(self):
        messages = [
            {"type": "human", "content": "以后请通过邮件联系我。"},
            {"type": "human", "content": "以后改用电话联系我。"},
        ]
        self.assertEqual(
            extract_local_preferences(messages),
            [("contact_preference", "联系偏好：电话联系")],
        )
        messages.append({"type": "human", "content": "请删除我的联系偏好。"})
        self.assertEqual(extract_local_preferences(messages), [("contact_preference", "")])

    def test_change_revoke_stale_job_and_scope(self):
        client = MemoryClient()
        with patch("app.agent.customer_memory.local_client", return_value=client):
            self.assertEqual(store_preferences("merchant", "alice", "10", [
                ("contact_preference", "联系偏好：邮件联系"),
            ]), 1)
            self.assertEqual(store_preferences("merchant", "alice", "11", [
                ("contact_preference", "联系偏好：电话联系"),
            ]), 1)
            self.assertEqual([x["text"] for x in read_scoped_preferences("merchant", "alice")], [
                "联系偏好：电话联系",
            ])
            current = next(iter(client.points.values()))
            current.payload["metadata"]["expires_at"] = "2000-01-01T00:00:00+00:00"
            self.assertEqual(read_scoped_preferences("merchant", "alice"), [])
            current.payload["metadata"]["expires_at"] = None
            self.assertEqual(read_scoped_preferences("merchant", "bob"), [])
            self.assertEqual(read_scoped_preferences("other", "alice"), [])
            self.assertEqual(store_preferences("merchant", "alice", "12", [
                ("contact_preference", ""),
            ]), 1)
            self.assertEqual(store_preferences("merchant", "alice", "11", [
                ("contact_preference", "联系偏好：电话联系"),
            ]), 0)
            self.assertEqual(read_scoped_preferences("merchant", "alice"), [])

    def test_canonical_read_rejects_wrong_type_category_and_missing_scope(self):
        client = MemoryClient()
        point_id = _canonical_id("merchant", "alice", "contact_preference")
        metadata = {
            "tenant_id": "merchant", "customer_id": "alice",
            "type": "unrelated_memory", "category": "contact_preference",
            "source_ticket_id": "10", "status": "active",
        }
        client.points[point_id] = SimpleNamespace(
            id=point_id,
            payload={"page_content": "联系偏好：邮件联系", "metadata": metadata},
        )
        with patch("app.agent.customer_memory.local_client", return_value=client):
            self.assertEqual(read_scoped_preferences("merchant", "alice"), [])
            metadata["type"] = "user_episodic_memory"
            metadata["category"] = "communication_preference"
            self.assertEqual(read_scoped_preferences("merchant", "alice"), [])
            self.assertEqual(read_scoped_preferences("", "alice"), [])
            self.assertEqual(read_scoped_preferences("merchant", " "), [])
            self.assertEqual(read_scoped_preferences("other", "alice"), [])
            metadata["category"] = "contact_preference"
            metadata["tenant_id"] = "other"
            self.assertEqual(read_scoped_preferences("merchant", "alice"), [])
            metadata["tenant_id"] = "merchant"
            metadata["customer_id"] = "bob"
            self.assertEqual(read_scoped_preferences("merchant", "alice"), [])


if __name__ == "__main__":
    unittest.main()
