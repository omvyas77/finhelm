"""Dense retrieval: embed the query, ask the store for top-k.

Deliberately thin. All backend-specific behaviour lives behind the VectorStore protocol,
so this function never learns whether it is talking to FAISS or pgvector — which is what
makes the Day 3 swap a config change rather than a rewrite.
"""

from __future__ import annotations

from ..embeddings import encode
from ..stores.base import Hit, VectorStore


def search(
    query: str,
    store: VectorStore,
    k: int,
    embed_model: str,
    filters: dict | None = None,
) -> list[Hit]:
    vector = encode([query], embed_model)[0]
    return store.search(vector, k, filters)
