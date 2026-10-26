"""Retrieval boundary checks without network calls."""
from __future__ import annotations

import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://unused:unused@127.0.0.1/unused")
os.environ.setdefault("DEEPSEEK_API_KEY", "unused")
os.environ.setdefault("DASHSCOPE_API_KEY", "unused")
os.environ.setdefault("ADMIN_APPROVAL_CODE", "unused")

# Prevent vector_db's import-time Qdrant collection checks.
fake_vector_db = types.ModuleType("app.core.vector_db")
fake_vector_db.embeddings = object()
fake_vector_db.qdrant_client = object()
fake_vector_db.qdrant_company = object()
with patch.dict(sys.modules, {"app.core.vector_db": fake_vector_db}):
    from app.agent import knowledge_retrieval as retrieval  # noqa: E402

from langchain_core.documents import Document  # noqa: E402


class FakeBM25:
    def __init__(self, docs):
        self.docs = docs
        self.k = 0

    @classmethod
    def from_documents(cls, docs, **kwargs):
        return cls(docs)


class KnowledgeRetrievalTests(unittest.TestCase):
    def test_handbook_change_rebuilds_bm25_index(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            path = Path(folder) / "handbook.txt"
            scope = retrieval.KnowledgeScope("merchant", path, "collection", "merchant/handbook.txt")
            path.write_text("第一部分：旧内容\n旧规则", encoding="utf-8")
            with patch.object(retrieval, "BM25Retriever", FakeBM25):
                _, _, first_version = retrieval._load_handbook_index(scope)
                path.write_text("第一部分：新内容\n新规则", encoding="utf-8")
                index, _, second_version = retrieval._load_handbook_index(scope)
            self.assertNotEqual(first_version, second_version)
            self.assertIn("新规则", index.docs[0].page_content)

    def test_dense_query_filters_tenant_before_search_and_rejects_stale_docs(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            path = Path(folder) / "handbook.txt"
            path.write_text("第一部分：规则\n当前规则", encoding="utf-8")
            scope = retrieval.KnowledgeScope("merchant", path, "collection", "merchant/handbook.txt")
            with patch.object(retrieval, "BM25Retriever", FakeBM25):
                _, _, version = retrieval._load_handbook_index(scope)
            captured = {}

            class FakeStore:
                def __init__(self, **kwargs):
                    pass

                def as_retriever(self, *, search_kwargs):
                    captured.update(search_kwargs)
                    return self

                async def ainvoke(self, query):
                    return [
                        Document(page_content="foreign", metadata={"tenant_id": "other"}),
                        Document(page_content="stale", metadata={"tenant_id": "merchant", "source": scope.source, "document_version": "old"}),
                        Document(page_content="current", metadata={"tenant_id": "merchant", "source": scope.source, "document_version": version}),
                    ]

            with patch.object(retrieval, "resolve_knowledge_scope", return_value=scope), patch.object(
                retrieval, "QdrantVectorStore", FakeStore,
            ), patch.object(retrieval, "BM25Retriever", FakeBM25):
                coroutine = retrieval.retrieve_company_knowledge("规则", "merchant", mode="dense")
                with self.assertRaises(StopIteration) as completed:
                    coroutine.send(None)  # Fake retriever finishes without an event loop.
                documents = completed.exception.value
            self.assertEqual([doc.page_content for doc in documents], ["current"])
            self.assertEqual(captured["filter"].must[0].match.value, "merchant")


if __name__ == "__main__":
    unittest.main()
