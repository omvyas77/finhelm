"""The one retrieval entry point the generator and the eval harness both call.

Everything the Day 2 ablation varies — chunking strategy, retriever, reranking, routing —
is a field on Config, so the ablation loop is `for cfg in variants: retrieve(q, cfg)`
rather than a different code path per row of the results table.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass

from ..config import Config
from ..stores import INDEX_DIR, load_store
from ..stores.base import Hit
from . import bm25, dense, hybrid
from .rerank import rerank
from .router import Route, route


@dataclass(frozen=True)
class Retrieved:
    hits: list[Hit]
    route: Route
    rerank_ms: int = 0


FALLBACK_STRATEGY = "fixed"

# Records every (collection, requested, used) substitution made by _resolve_strategy, so
# the eval runner can report which collections actually ran under which strategy instead
# of the ablation table implying a sweep that did not happen.
STRATEGY_FALLBACKS: dict[tuple[str, str], str] = {}


def _available(collection: str, strategy: str, retriever: str) -> bool:
    """Can this (collection, strategy) actually be served by this retriever?

    The two retrievers read different artifacts, and the artifacts are not in sync:
    BM25 builds from the chunk parquet at load time, while dense needs a FAISS index that
    took hours to build. `filings_sentence_window` has a parquet but no index, so
    `--retriever bm25 --chunking sentence_window` is a real, runnable ablation cell while
    the dense arm of the same row is not. Asking the narrower question — what does *this*
    retriever need — keeps that cell available instead of falling back on it needlessly.

    Hybrid needs both, so it requires both to be present.
    """
    has_chunks = (bm25.PROCESSED / f"chunks_{collection}_{strategy}.parquet").exists()
    has_index = (INDEX_DIR / f"{collection}_{strategy}").exists()
    if retriever == "bm25":
        return has_chunks
    if retriever == "dense":
        return has_index
    return has_chunks and has_index


@functools.lru_cache(maxsize=32)
def _resolve_strategy(collection: str, strategy: str, retriever: str) -> str:
    """Return the chunking strategy actually available for this collection.

    Only `filings` was chunked under all three strategies; `complaints` exists as `fixed`
    only. Without this, any question the router sends to complaints crashes the moment
    the sweep moves off `fixed` — which is how the first sweep attempt died.

    Falling back is the right behaviour rather than erroring, because the chunking
    ablation is a question about filings prose: complaint narratives are a few hundred
    words each and are already close to one chunk, so re-chunking them tests nothing. But
    the substitution is recorded rather than silent, because a table captioned
    "semantic vs fixed" that quietly ran half its corpus as fixed either way would
    overstate what was compared.
    """
    if _available(collection, strategy, retriever):
        return strategy
    STRATEGY_FALLBACKS[(collection, strategy)] = FALLBACK_STRATEGY
    return FALLBACK_STRATEGY


def _from_collection(query: str, collection: str, cfg: Config, filters: dict | None) -> list[Hit]:
    k = cfg.top_k_retrieve
    strategy = _resolve_strategy(collection, cfg.chunking, cfg.retriever)

    if cfg.retriever == "bm25":
        return bm25.load_index(collection, strategy).search(query, k, filters)

    store = load_store(collection, strategy, cfg.store)
    dense_hits = dense.search(query, store, k, cfg.embed_model, filters)
    if cfg.retriever == "dense":
        return dense_hits

    lexical = bm25.load_index(collection, strategy).search(query, k, filters)
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

    # Candidates are assembled at full width first. When reranking is on, the cross-encoder
    # needs the whole ~20-per-collection pool to work with — truncating to top_k_context
    # before reranking would hand it the bi-encoder's answer and ask it to confirm that,
    # which is exactly the mistake reranking exists to correct.
    if len(per_collection) == 1:
        # Fusing a single list would only overwrite each score with 1/(rrf_k + rank),
        # throwing away the cosine or BM25 value for no gain. Keep the real scores —
        # Day 2 needs them to tell a confident hit from a barely-above-noise one.
        candidates = per_collection[0]
    else:
        # Across collections it is the opposite: `filings` and `complaints` are separate
        # indexes whose scores are not on a comparable scale, so rank fusion is the only
        # defensible way to interleave them.
        candidates = hybrid.fuse(per_collection, cfg.top_k_retrieve, cfg.rrf_k)

    if cfg.rerank:
        hits, rerank_ms = rerank(query, candidates, cfg.top_k_context, cfg.rerank_model)
        return Retrieved(hits, decision, rerank_ms)

    return Retrieved(candidates[: cfg.top_k_context], decision)


__all__ = ["retrieve", "Retrieved", "Route", "route", "STRATEGY_FALLBACKS"]
