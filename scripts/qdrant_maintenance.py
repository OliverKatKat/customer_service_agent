"""Inspect Qdrant collections, create scoped payload indexes, and take snapshots."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def required_indexes(kind: str):
    from qdrant_client import models

    tenant = models.KeywordIndexParams(type=models.KeywordIndexType.KEYWORD, is_tenant=True)
    keyword = models.PayloadSchemaType.KEYWORD
    if kind == "memory":
        return {
            "metadata.tenant_id": tenant,
            "metadata.customer_id": keyword,
            "metadata.type": keyword,
            "metadata.category": keyword,
        }
    return {
        "metadata.tenant_id": tenant,
        "metadata.source": keyword,
        "metadata.document_version": keyword,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--collection", required=True)
    parser.add_argument("--kind", choices=["memory", "knowledge"], required=True)
    parser.add_argument("--apply", action="store_true", help="Create missing indexes")
    parser.add_argument("--snapshot", action="store_true", help="Create a collection snapshot; requires --apply")
    args = parser.parse_args()
    if args.snapshot and not args.apply:
        parser.error("--snapshot requires --apply")

    from qdrant_client import QdrantClient
    from app.core.config import settings

    client = QdrantClient(url=settings.QDRANT_URL, trust_env=False, timeout=30)
    try:
        if not client.collection_exists(args.collection):
            raise RuntimeError(f"Collection does not exist: {args.collection}")
        info = client.get_collection(args.collection)
        existing = set(info.payload_schema or {})
        missing = {
            field: schema for field, schema in required_indexes(args.kind).items()
            if field not in existing
        }
        result = {
            "collection": args.collection,
            "kind": args.kind,
            "points": info.points_count,
            "existing_payload_indexes": sorted(existing),
            "missing_payload_indexes": sorted(missing),
            "snapshot": None,
        }
        if args.apply:
            for field, schema in missing.items():
                client.create_payload_index(
                    collection_name=args.collection,
                    field_name=field,
                    field_schema=schema,
                    wait=True,
                )
            if args.snapshot:
                snapshot = client.create_snapshot(collection_name=args.collection, wait=True)
                result["snapshot"] = snapshot.name if snapshot else None
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        client.close()


if __name__ == "__main__":
    main()
