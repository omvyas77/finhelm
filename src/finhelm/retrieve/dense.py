"""Dense retrieval: embed the query, ask the store for top-k.

Deliberately thin. All backend-specific behaviour lives behind the VectorStore protocol,
so this function never learns whether it is talking to FAISS or pgvector — which is what
makes the an earlier stage swap a config change rather than a rewrite.
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
    query_prefix: bool = True,
) -> list[Hit]:
    # is_query applies the model's retrieval instruction prefix; passages are embedded
    # bare at index time. See embeddings.QUERY_PREFIXES.
    vector = encode([query], embed_model, is_query=query_prefix)[0]
    return store.search(vector, k, filters)
