"""Tenant-scoped hybrid retrieval for merchant-owned knowledge."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from langchain_classic.retrievers import EnsembleRetriever
from langchain_community.retrievers import BM25Retriever
from langchain_core.documents import Document
from langchain_qdrant import QdrantVectorStore
from qdrant_client import models

from app.core.config import settings
from app.core.vector_db import embeddings, qdrant_client, qdrant_company


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_HANDBOOK = ROOT / "company_handbook.txt"
TENANT_HANDBOOK_ROOT = ROOT / "company_knowledge" / "tenants"
_SAFE_TENANT = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_SECTION_HEADING = re.compile(r"^第[一二三四五六七八九十百]+部分[：:].+")
_TOKEN_PATTERN = re.compile(r"[\u4e00-\u9fff]+|[a-zA-Z0-9_]+")


@dataclass(frozen=True)
class KnowledgeScope:
    tenant_id: str
    handbook: Path
    collection: str
    source: str


def resolve_knowledge_scope(tenant_id: str) -> KnowledgeScope | None:
    """Return only an explicitly present tenant corpus and its own collection."""
    if not isinstance(tenant_id, str) or not _SAFE_TENANT.fullmatch(tenant_id):
        return None
    if tenant_id == settings.DEFAULT_TENANT_ID:
        if not DEFAULT_HANDBOOK.is_file():
            return None
        return KnowledgeScope(
            tenant_id, DEFAULT_HANDBOOK,
            settings.COMPANY_KNOWLEDGE_COLLECTION, DEFAULT_HANDBOOK.name,
        )
    handbook = TENANT_HANDBOOK_ROOT / tenant_id / "handbook.txt"
    if not handbook.is_file():
        return None
    return KnowledgeScope(
        tenant_id, handbook,
        f"{settings.COMPANY_KNOWLEDGE_COLLECTION}__{tenant_id}",
        f"{tenant_id}/handbook.txt",
    )


def _tokenize_chinese(text: str) -> list[str]:
    tokens: list[str] = []
    for token in _TOKEN_PATTERN.findall(text.lower()):
        if "\u4e00" <= token[0] <= "\u9fff":
            tokens.extend(token[index : index + 2] for index in range(len(token) - 1))
        else:
            tokens.append(token)
    return tokens


def _load_handbook_index(scope: KnowledgeScope) -> tuple[BM25Retriever | None, dict[int, str], str]:
    text = scope.handbook.read_text(encoding="utf-8")
    version = hashlib.sha256(text.encode("utf-8")).hexdigest()
    bm25, sections = _build_handbook_index(scope, version, text)
    return bm25, sections, version


@lru_cache(maxsize=64)
def _build_handbook_index(
    scope: KnowledgeScope, version: str, text: str,
) -> tuple[BM25Retriever | None, dict[int, str]]:
    paragraphs = [part.strip() for part in text.split("\n\n") if part.strip()]
    current_section = "未分类"
    section_by_paragraph: dict[int, str] = {}
    documents: list[Document] = []
    for paragraph_index, paragraph in enumerate(paragraphs):
        first_line = paragraph.splitlines()[0].strip()
        if _SECTION_HEADING.match(first_line):
            current_section = first_line
        section_by_paragraph[paragraph_index] = current_section
        documents.append(Document(
            page_content=paragraph,
            metadata={
                "tenant_id": scope.tenant_id,
                "source": scope.source,
                "document_version": version,
                "section": current_section,
                "paragraph_index": paragraph_index,
            },
        ))
    if not documents:
        return None, section_by_paragraph
    retriever = BM25Retriever.from_documents(documents, preprocess_func=_tokenize_chinese)
    retriever.k = 3
    return retriever, section_by_paragraph


@lru_cache(maxsize=64)
def _vector_store(collection: str) -> QdrantVectorStore:
    if collection == settings.COMPANY_KNOWLEDGE_COLLECTION:
        return qdrant_company
    return QdrantVectorStore(
        client=qdrant_client,
        collection_name=collection,
        embedding=embeddings,
    )


async def retrieve_company_knowledge(
    query: str, tenant_id: str, limit: int = 5, mode: str = "hybrid",
) -> list[Document]:
    if mode not in {"bm25", "dense", "hybrid"}:
        raise ValueError("Unknown retrieval mode")
    scope = resolve_knowledge_scope(tenant_id)
    if scope is None:
        return []
    bm25, section_by_paragraph, version = _load_handbook_index(scope)
    if mode == "bm25":
        raw_documents = await bm25.ainvoke(query) if bm25 is not None else []
    else:
        scoped_filter = models.Filter(must=[models.FieldCondition(
            key="metadata.tenant_id", match=models.MatchValue(value=tenant_id),
        )])
        vector_retriever = _vector_store(scope.collection).as_retriever(search_kwargs={
            "k": limit, "filter": scoped_filter,
        })
        if mode == "dense" or bm25 is None:
            raw_documents = await vector_retriever.ainvoke(query)
        else:
            raw_documents = await EnsembleRetriever(
                retrievers=[bm25, vector_retriever], weights=[0.6, 0.4],
            ).ainvoke(query)

    documents: list[Document] = []
    seen: set[str] = set()
    for document in raw_documents:
        metadata = dict(document.metadata or {})
        if metadata.get("tenant_id") != tenant_id:
            continue
        if metadata.get("source") == scope.source and metadata.get("document_version") != version:
            continue
        content = document.page_content.strip()
        if not content or content in seen:
            continue
        seen.add(content)
        if metadata.get("source") == scope.source:
            metadata.setdefault("document_version", version)
            paragraph_index = metadata.get("paragraph_index")
            if isinstance(paragraph_index, int):
                metadata.setdefault("section", section_by_paragraph.get(paragraph_index, "未标注"))
        elif metadata.get("source") == "legacy_company_knowledge":
            metadata.setdefault("section", "旧知识库条目")
        document.metadata = metadata
        documents.append(document)
        if len(documents) >= limit:
            break
    return documents
