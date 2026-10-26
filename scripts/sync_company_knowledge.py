from __future__ import annotations

import argparse
import hashlib
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from langchain_openai import OpenAIEmbeddings
from qdrant_client import QdrantClient, models

from app.core.config import settings


HANDBOOK_PATH = ROOT / "company_handbook.txt"
SOURCE_COLLECTION = "company_knowledge"
TARGET_COLLECTION = settings.COMPANY_KNOWLEDGE_COLLECTION
LEGACY_POLICY_MARKERS = (
    "[内部授权系统注入]",
    "approval_code 字段留空",
    "INTERNAL_AUTH_PREFIX",
)
LEGACY_NAMESPACE = uuid.UUID("98111565-8f10-49bc-976f-122a8436738a")
HANDBOOK_NAMESPACE = uuid.UUID("ba674f7c-9af2-4b4a-b3a6-384cc3fa0f09")


def chunk_text(text: str, max_chars: int = 1600, overlap: int = 160) -> list[str]:
    text = text.strip()
    if len(text) <= max_chars:
        return [text] if text else []

    chunks: list[str] = []
    start = 0
    separators = ("。", "！", "？", "；", "\n", ". ", "; ", " ")
    while start < len(text):
        end = min(start + max_chars, len(text))
        if end < len(text):
            floor = start + max_chars // 2
            candidates = [
                text.rfind(separator, floor, end) + len(separator)
                for separator in separators
            ]
            candidates = [point for point in candidates if point > floor]
            if candidates:
                end = max(candidates)
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= len(text):
            break
        start = max(start + 1, end - overlap)
    return chunks


def scroll_all(client: QdrantClient, collection: str, *, with_vectors: bool):
    offset = None
    while True:
        points, offset = client.scroll(
            collection_name=collection,
            offset=offset,
            limit=128,
            with_payload=True,
            with_vectors=with_vectors,
        )
        yield from points
        if offset is None:
            break


def payload_metadata(payload: dict) -> dict:
    metadata = payload.get("metadata")
    return dict(metadata) if isinstance(metadata, dict) else {}


def ensure_target_collection(client: QdrantClient) -> None:
    if client.collection_exists(TARGET_COLLECTION):
        if client.collection_exists(SOURCE_COLLECTION):
            source_vectors = client.get_collection(
                SOURCE_COLLECTION
            ).config.params.vectors
            target_vectors = client.get_collection(
                TARGET_COLLECTION
            ).config.params.vectors
            if source_vectors != target_vectors:
                raise RuntimeError(
                    f"Vector configuration differs between {SOURCE_COLLECTION} "
                    f"and {TARGET_COLLECTION}; refusing to mix incompatible vectors."
                )
        return

    if not client.collection_exists(SOURCE_COLLECTION):
        raise RuntimeError(
            f"Neither {SOURCE_COLLECTION} nor {TARGET_COLLECTION} exists; "
            "create the source collection before syncing."
        )

    source_info = client.get_collection(SOURCE_COLLECTION)
    client.create_collection(
        collection_name=TARGET_COLLECTION,
        vectors_config=source_info.config.params.vectors,
    )


def migrate_legacy_points(client: QdrantClient) -> tuple[int, int]:
    if not client.collection_exists(SOURCE_COLLECTION):
        return 0, 0

    migrated = 0
    skipped = 0
    batch: list[models.PointStruct] = []

    for point in scroll_all(client, SOURCE_COLLECTION, with_vectors=True):
        payload = dict(point.payload or {})
        text = payload.get("page_content", "")
        if not isinstance(text, str) or not text.strip():
            skipped += 1
            continue
        if any(marker in text for marker in LEGACY_POLICY_MARKERS):
            skipped += 1
            continue
        if point.vector is None:
            skipped += 1
            continue

        metadata = payload_metadata(payload)
        metadata.update(
            {
                "tenant_id": settings.DEFAULT_TENANT_ID,
                "source": "legacy_company_knowledge",
                "legacy_id": str(point.id),
            }
        )
        payload["metadata"] = metadata
        migrated_id = str(
            uuid.uuid5(LEGACY_NAMESPACE, f"{SOURCE_COLLECTION}:{point.id}")
        )
        batch.append(
            models.PointStruct(
                id=migrated_id,
                vector=point.vector,
                payload=payload,
            )
        )
        migrated += 1
        if len(batch) >= 64:
            client.upsert(collection_name=TARGET_COLLECTION, points=batch)
            batch = []

    if batch:
        client.upsert(collection_name=TARGET_COLLECTION, points=batch)
    return migrated, skipped


def index_handbook(client: QdrantClient) -> tuple[str, int, int]:
    text = HANDBOOK_PATH.read_text(encoding="utf-8")
    version = hashlib.sha256(text.encode("utf-8")).hexdigest()
    paragraphs = [part.strip() for part in text.split("\n\n") if part.strip()]

    chunks: list[tuple[int, int, str]] = []
    for paragraph_index, paragraph in enumerate(paragraphs):
        for chunk_index, chunk in enumerate(chunk_text(paragraph)):
            chunks.append((paragraph_index, chunk_index, chunk))

    embeddings = OpenAIEmbeddings(
        openai_api_key=settings.DASHSCOPE_API_KEY,
        openai_api_base=settings.EMBEDDING_API_BASE,
        model=settings.EMBEDDING_MODEL_NAME,
        check_embedding_ctx_length=False,
    )

    indexed = 0
    # DashScope's embeddings endpoint accepts at most 10 inputs per request.
    for start in range(0, len(chunks), 10):
        batch = chunks[start : start + 10]
        vectors = embeddings.embed_documents([chunk for _, _, chunk in batch])
        if len(vectors) != len(batch):
            raise RuntimeError("Embedding API returned an incomplete batch.")

        points = []
        for (paragraph_index, chunk_index, chunk), vector in zip(
            batch, vectors, strict=True
        ):
            point_id = str(
                uuid.uuid5(
                    HANDBOOK_NAMESPACE,
                    f"{version}:{paragraph_index}:{chunk_index}",
                )
            )
            points.append(
                models.PointStruct(
                    id=point_id,
                    vector=vector,
                    payload={
                        "page_content": chunk,
                        "metadata": {
                            "tenant_id": settings.DEFAULT_TENANT_ID,
                            "source": "company_handbook.txt",
                            "document_version": version,
                            "paragraph_index": paragraph_index,
                            "chunk_index": chunk_index,
                        },
                    },
                )
            )
        client.upsert(collection_name=TARGET_COLLECTION, points=points)
        indexed += len(points)
        print(f"Indexed handbook batch: {indexed}/{len(chunks)}")

    stale_ids = []
    for point in scroll_all(client, TARGET_COLLECTION, with_vectors=False):
        metadata = payload_metadata(point.payload or {})
        if (
            metadata.get("source") == "company_handbook.txt"
            and metadata.get("document_version") != version
        ):
            stale_ids.append(point.id)

    for start in range(0, len(stale_ids), 128):
        client.delete(
            collection_name=TARGET_COLLECTION,
            points_selector=models.PointIdsList(
                points=stale_ids[start : start + 128]
            ),
        )

    return version, indexed, len(stale_ids)


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync company knowledge into Qdrant.")
    parser.add_argument(
        "--allow-external-embedding",
        action="store_true",
        help="Allow sending handbook chunks to the configured embedding API.",
    )
    args = parser.parse_args()
    if not args.allow_external_embedding:
        parser.error(
            "syncing the handbook calls the configured external embedding API; "
            "pass --allow-external-embedding only after approving that data transfer"
        )

    client = QdrantClient(url=settings.QDRANT_URL, trust_env=False, timeout=30)
    ensure_target_collection(client)

    migrated, skipped = migrate_legacy_points(client)
    version, indexed, pruned = index_handbook(client)
    total = client.count(TARGET_COLLECTION, exact=True).count

    print(f"Target collection: {TARGET_COLLECTION}")
    print(f"Current handbook version: {version}")
    print(f"Handbook chunks indexed: {indexed}; old managed chunks pruned: {pruned}")
    print(f"Legacy points preserved: {migrated}; stale/empty points skipped: {skipped}")
    print(f"Target point count: {total}")
    print(f"Original collection preserved: {SOURCE_COLLECTION}")


if __name__ == "__main__":
    main()
