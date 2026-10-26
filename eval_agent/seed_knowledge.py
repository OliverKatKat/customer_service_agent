"""Index only the synthetic merchant_eval_agent handbook in isolated Qdrant."""
from __future__ import annotations

import argparse
import hashlib
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from eval_agent.run import require_eval_environment  # noqa: E402

TENANT_ID = "merchant_eval_agent"
POINT_NAMESPACE = uuid.UUID("31011d11-cd0b-4f09-a02f-17480302412b")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Create/update only this tenant's isolated collection")
    parser.add_argument("--allow-external-embedding", action="store_true")
    args = parser.parse_args()

    handbook = ROOT / "company_knowledge" / "tenants" / TENANT_ID / "handbook.txt"
    text = handbook.read_text(encoding="utf-8")
    version = hashlib.sha256(text.encode("utf-8")).hexdigest()
    paragraphs = [part.strip() for part in text.split("\n\n") if part.strip()]
    if not paragraphs:
        raise RuntimeError("Synthetic handbook is empty")
    from app.core.config import settings

    collection = f"{settings.COMPANY_KNOWLEDGE_COLLECTION}__{TENANT_ID}"
    print(f"Synthetic handbook: {TENANT_ID}, {len(paragraphs)} paragraphs, version {version[:12]}")
    print(f"Target collection: {collection}")
    if not args.apply:
        print("Preview only. Pass --apply --allow-external-embedding to index synthetic text.")
        return
    if not args.allow_external_embedding:
        raise ValueError("Indexing sends the synthetic handbook paragraphs to the configured embedding API")
    require_eval_environment()

    from qdrant_client import QdrantClient, models
    from app.core.vector_db import embeddings

    vectors = embeddings.embed_documents(paragraphs)
    if len(vectors) != len(paragraphs) or any(len(vector) != 1024 for vector in vectors):
        raise RuntimeError("Embedding API returned unexpected vector dimensions/count")
    client = QdrantClient(url=settings.QDRANT_URL, trust_env=False, timeout=30)
    try:
        if not client.collection_exists(collection):
            client.create_collection(
                collection_name=collection,
                vectors_config=models.VectorParams(size=1024, distance=models.Distance.COSINE),
            )
        points = []
        for index, (paragraph, vector) in enumerate(zip(paragraphs, vectors, strict=True)):
            section = paragraph.splitlines()[0].strip()
            points.append(models.PointStruct(
                id=str(uuid.uuid5(POINT_NAMESPACE, f"{TENANT_ID}:{version}:{index}")),
                vector=vector,
                payload={
                    "page_content": paragraph,
                    "metadata": {
                        "tenant_id": TENANT_ID,
                        "source": f"{TENANT_ID}/handbook.txt",
                        "document_version": version,
                        "section": section,
                        "paragraph_index": index,
                    },
                },
            ))
        client.upsert(collection_name=collection, points=points, wait=True)
        stale = []
        offset = None
        while True:
            batch, offset = client.scroll(
                collection_name=collection, offset=offset, limit=100,
                with_payload=True, with_vectors=False,
            )
            for point in batch:
                metadata = (point.payload or {}).get("metadata") or {}
                if metadata.get("tenant_id") != TENANT_ID:
                    raise RuntimeError("Unexpected tenant metadata in dedicated eval collection")
                if metadata.get("source") == f"{TENANT_ID}/handbook.txt" and metadata.get("document_version") != version:
                    stale.append(point.id)
            if offset is None:
                break
        if stale:
            client.delete(collection_name=collection, points_selector=models.PointIdsList(points=stale), wait=True)
        total = client.count(collection_name=collection, exact=True).count
    finally:
        client.close()
    print(f"Indexed {len(points)} synthetic paragraphs; pruned {len(stale)} stale points; collection count {total}")


if __name__ == "__main__":
    main()
