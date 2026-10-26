"""Create the empty knowledge collection required to import the app in a fresh eval environment."""
from __future__ import annotations

import os

from qdrant_client import QdrantClient, models

url = os.environ.get("QDRANT_URL")
collection = os.environ.get("COMPANY_KNOWLEDGE_COLLECTION")
if url != "http://127.0.0.1:56333" or collection != "company_knowledge_eval_v3":
    raise RuntimeError("Refusing to initialize a non-evaluation Qdrant target")

client = QdrantClient(url=url, trust_env=False)
try:
    if not client.collection_exists(collection):
        client.create_collection(
            collection_name=collection,
            vectors_config=models.VectorParams(size=1024, distance=models.Distance.COSINE),
        )
    print(f"Evaluation knowledge collection ready: {collection}")
finally:
    client.close()