"""The one retrieval entry point the generator and the eval harness both call.

Everything the Day 2 ablation varies — chunking strategy, retriever, reranking, routing —
is a field on Config, so the ablation loop is `for cfg in variants: retrieve(q, cfg)`
rather than a different code path per row of the results table.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import Config
from ..stores import load_store
from ..stores.base import Hit
from . import bm25, dense, hybrid
from .router import Route, route


@dataclass(frozen=True)
class Retrieved:
    hits: list[Hit]
    route: Route


def _from_collection(query: str, collection: str, cfg: Config, filters: dict | None) -> list[Hit]:
    k = cfg.top_k_retrieve

    if cfg.retriever == "bm25":
        return bm25.load_index(collection, cfg.chunking).search(query, k, filters)

    store = load_store(collection, cfg.chunking, cfg.store)
    dense_hits = dense.search(query, store, k, cfg.embed_model, filters)
    if cfg.retriever == "dense":
        return dense_hits

    lexical = bm25.load_index(collection, cfg.chunking).search(query, k, filters)
    return hybrid.fuse([dense_hits, lexical], k, cfg.rrf_k)


def retrieve(
    query: str,
    cfg: Config | None = None,
    filters: dict | None = None,
    collections: list[str] | None = None,
) -> Retrieved:
    cfg = cfg or Config()
    decision = (
        Route(collections, "explicit", "caller specified collections")
        if collections
        else route(query)
    )

    per_collection = [_from_collection(query, c, cfg, filters) for c in decision.collections]

    if len(per_collection) == 1:
        # Fusing a single list would only overwrite each score with 1/(rrf_k + rank),
        # throwing away the cosine or BM25 value for no gain. Keep the real scores —
        # Day 2 needs them to tell a confident hit from a barely-above-noise one.
        hits = per_collection[0][: cfg.top_k_context]
    else:
        # Across collections it is the opposite: `filings` and `complaints` are separate
        # indexes whose scores are not on a comparable scale, so rank fusion is the only
        # defensible way to interleave them.
        hits = hybrid.fuse(per_collection, cfg.top_k_context, cfg.rrf_k)
    return Retrieved(hits, decision)


__all__ = ["retrieve", "Retrieved", "Route", "route"]
