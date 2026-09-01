"""The one retrieval entry point the generator and the eval harness both call.

Everything the Day 2 ablation varies — chunking strategy, retriever, reranking, routing —
is a field on Config, so the ablation loop is `for cfg in variants: retrieve(q, cfg)`
rather than a different code path per row of the results table.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass, field

from ..agent.decompose import decompose, filters_for
from ..chunking import chunks_name
from ..config import Config
from ..stores import INDEX_DIR, index_name, load_store
from ..stores.base import Hit
from ..telemetry import set_attributes, span
from . import bm25, dense, hybrid, window
from .rerank import rerank, rerank_per_query, rerank_windowed
from .router import Route, route


@dataclass(frozen=True)
class Retrieved:
    hits: list[Hit]
    route: Route
    rerank_ms: int = 0
    # Sub-questions actually issued, empty when decomposition was off or declined to
    # split. Recorded so a multi-hop answer can be traced back to the queries that fed
    # it rather than to the one the user typed.
    sub_questions: list[str] = field(default_factory=list)
    # The full candidate pool as it stood before reranking and before truncation to
    # top_k_context — i.e. what retrieval actually found, as opposed to what survived
    # selection. Carried because the two failure modes underneath a recall miss want
    # opposite fixes and are indistinguishable from `hits` alone: a gold document that
    # reached the pool and was then dropped is a fusion/rerank problem, while one that
    # never reached it is a representation problem. Day 2 could not tell them apart.
    candidates: list[Hit] = field(default_factory=list)


FALLBACK_STRATEGY = "fixed"

# Records every (collection, requested, used) substitution made by _resolve_strategy, so
# the eval runner can report which collections actually ran under which strategy instead
# of the ablation table implying a sweep that did not happen.
STRATEGY_FALLBACKS: dict[tuple[str, str], str] = {}


def _available(collection: str, strategy: str, retriever: str,
               contextual: bool = False, embed_model: str | None = None,
               chunk_tokens: int = 800) -> bool:
    """Can this (collection, strategy) actually be served by this retriever?

    The two retrievers read different artifacts, and the artifacts need not be in sync:
    BM25 builds from the chunk parquet at load time, while dense needs a prebuilt FAISS
    index. `filings_sentence_window` spent most of the ablation with a parquet and no
    index, which made `--retriever bm25 --chunking sentence_window` a real, runnable cell
    while the dense arm of the same row was not. Asking the narrower question — what does
    *this* retriever need — kept that cell available instead of falling back on it
    needlessly. (The index exists now, but the asymmetry is a property of the artifacts,
    not of that one gap: `complaints` still has no index under any strategy but `fixed`.)

    Hybrid needs both, so it requires both to be present.
    """
    has_chunks = (bm25.PROCESSED /
                  f"{chunks_name(collection, strategy, chunk_tokens)}.parquet").exists()
    has_index = (INDEX_DIR / index_name(collection, strategy, contextual,
                                        embed_model, chunk_tokens)).exists()
    if retriever == "bm25":
        return has_chunks
    if retriever == "dense":
        return has_index
    return has_chunks and has_index


@functools.lru_cache(maxsize=32)
def _resolve_strategy(collection: str, strategy: str, retriever: str,
                      contextual: bool = False, embed_model: str | None = None,
                      chunk_tokens: int = 800) -> str:
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
    if _available(collection, strategy, retriever, contextual, embed_model, chunk_tokens):
        return strategy
    STRATEGY_FALLBACKS[(collection, strategy)] = FALLBACK_STRATEGY
    return FALLBACK_STRATEGY


def _search(query: str, collection: str, strategy: str, cfg: Config, k: int,
            filters: dict | None) -> list[Hit]:
    if cfg.retriever == "bm25":
        return bm25.load_index(collection, strategy, cfg.chunk_tokens).search(query, k, filters)

    store = load_store(collection, strategy, cfg.store, cfg.contextual_headers,
                       cfg.embed_model, cfg.chunk_tokens)
    dense_hits = dense.search(query, store, k, cfg.embed_model, filters, cfg.query_prefix)
    if cfg.retriever == "dense":
        return dense_hits

    lexical = bm25.load_index(collection, strategy, cfg.chunk_tokens).search(query, k, filters)
    return hybrid.fuse([dense_hits, lexical], k, cfg.rrf_k)


def _from_collection(query: str, collection: str, cfg: Config, filters: dict | None) -> list[Hit]:
    k = cfg.top_k_retrieve
    strategy = _resolve_strategy(collection, cfg.chunking, cfg.retriever,
                                 cfg.contextual_headers, cfg.embed_model, cfg.chunk_tokens)

    hits = _search(query, collection, strategy, cfg, k, filters)
    if not filters or len(hits) >= k:
        return hits

    # Backoff. A filter derived from the question can be wrong — an amendment, an exhibit,
    # a fact carried in an 8-K rather than the 10-K the question names — and a filter that
    # empties the pool costs recall that no later stage can recover. Refilling from the
    # unfiltered ranking keeps the filtered hits in front and spends only the slots the
    # filter could not fill, so a good filter loses nothing and a bad one degrades to the
    # unfiltered behaviour instead of to nothing.
    seen = {h.chunk_id for h in hits}
    for hit in _search(query, collection, strategy, cfg, k, None):
        if hit.chunk_id not in seen:
            hits.append(hit)
            seen.add(hit.chunk_id)
        if len(hits) >= k:
            break
    return hits


# Removed: per-sub-question budget allocation.
#
# The idea was that fusing every pool and reranking the result against the original
# compound question discards what decomposition found, since the original names two facts
# and the passages most similar to it are the ones moderately about both. An isolation
# test seemed to confirm it: 7 of 40 gold spans reached a shared top-5, against 14 of 40
# when each sub-question got a top-5 "of its own".
#
# That comparison was invalid. Giving each sub-question its own top-5 hands a two-way
# split 10 slots and a four-way split 20, against 5 for the baseline. It measured context
# budget, not query shape. Held to an equal 5-slot budget the allocation loses on every
# tier: multi-span 0.175 vs 0.225 fused, single-span 0.471 vs 0.559, pooled 0.361 vs
# 0.435 (paired, n=54, [-0.139, +0.009]).
#
# The single-span damage is the more useful half of the result. decompose splits 53 of 54
# questions, so quota-splitting also dilutes questions that only ever needed one fact —
# any future attempt at this has to gate on the split being warranted, not merely present.

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
        else route(query, cfg.llm_router)
    )

    set_attributes(**{"retrieve.route": "+".join(decision.collections),
                      "retrieve.route_method": decision.method,
                      "retrieve.k": cfg.top_k_retrieve,
                      "retrieve.strategy": cfg.retriever,
                      "retrieve.chunking": cfg.chunking,
                      "retrieve.rrf_k": cfg.rrf_k})

    used = [(c, _resolve_strategy(c, cfg.chunking, cfg.retriever, cfg.contextual_headers,
                                  cfg.embed_model, cfg.chunk_tokens))
            for c in decision.collections]

    # Routing happens once, on the question as asked. Sub-questions of a filings question
    # are still filings questions, and re-routing each one would spend a model call per
    # split to re-derive the same answer.
    sub_questions: list[str] = []
    if cfg.agentic:
        split = decompose(query, cfg.max_sub_questions,
                          timeout_s=cfg.agent_timeout_s)
        if split != [query]:
            sub_questions = split

    # The original query is always retrieved for, even when a split succeeded: fusing the
    # sub-questions *with* it means a decomposition that silently drops a facet costs
    # ranking rather than evidence. See agent/decompose.py.
    queries = [query, *sub_questions]

    pools = []
    for index, q in enumerate(queries):
        # One span per sub-question, not one attribute listing them all.
        #
        # A decomposed query's whole story is which half found what, and an attribute on
        # the parent cannot show that: it collapses four retrievals of very different
        # cost and yield into one bar. Separate spans put the sub-questions side by side
        # in the waterfall, which is where "this half retrieved nothing" becomes visible
        # rather than inferable.
        #
        # index 0 is always the original question — it is retrieved for alongside the
        # split so that a decomposition which drops a facet costs ranking, not evidence.
        with span("subquery", **{"subquery.index": index,
                                 "subquery.is_original": index == 0,
                                 "subquery.text": q[:200],
                                 "subquery.collections": "+".join(decision.collections)}) as sub:
            per_collection = []
            for c in decision.collections:
                # A sub-question's issuer filter applies only where the field exists. CFPB
                # complaint chunks carry no ticker, so filtering them by one matches
                # nothing and would silently empty that half of the pool rather than
                # narrowing it.
                implied = (filters_for(q)
                           if (cfg.filter_by_issuer and c == "filings") else None)
                merged = {**(filters or {}), **(implied or {})} or None
                per_collection.append(_from_collection(q, c, cfg, merged))
            # Fusing a single list would only overwrite each score with 1/(rrf_k + rank),
            # throwing away the cosine or BM25 value for no gain. Keep the real scores —
            # Day 2 needs them to tell a confident hit from a barely-above-noise one.
            # Across collections it is the opposite: `filings` and `complaints` are separate
            # indexes whose scores are not on a comparable scale, so rank fusion is the only
            # defensible way to interleave them.
            pool = (per_collection[0] if len(per_collection) == 1
                    else hybrid.fuse(per_collection, cfg.top_k_retrieve, cfg.rrf_k))
            if sub is not None:
                sub.set_attribute("subquery.hits", len(pool))
                # The score of the best thing this sub-question found. A sub-question that
                # returns twenty weak hits and one that returns twenty strong ones are the
                # same bar without it.
                sub.set_attribute("subquery.top_score",
                                  round(float(pool[0].score), 4) if pool else 0.0)
            pools.append(pool)

    # Candidates are assembled at full width first. When reranking is on, the cross-encoder
    # needs the whole pool to work with — truncating to top_k_context before reranking
    # would hand it the bi-encoder's answer and ask it to confirm that, which is exactly
    # the mistake reranking exists to correct.
    if len(pools) == 1:
        candidates = pools[0]
    else:
        # Widen the pool with the number of queries. Narrowing back to top_k_retrieve
        # would discard most of what decomposition just went and found.
        width = cfg.top_k_retrieve * len(pools)
        candidates = (hybrid.interleave(pools, width)
                      if cfg.cross_query_fusion == "interleave"
                      else hybrid.fuse(pools, width, cfg.rrf_k))

    # Sentence-window hits carry only the indexed sentence until here; window.expand
    # splices their neighbours back in. It runs last, on the selected hits only, so both
    # scorers above still see the bare sentence — see window.py.
    # Two rerank shapes, because which question a candidate is scored against decides what
    # survives. The default scores everything against the original question, which is right
    # for a single-fact query and wrong for a comparison: asked which passages best answer a
    # string naming two facts, a cross-encoder prefers the ones vaguely about both over the
    # one that decisively answers half. rerank_per_subquestion scores each pool against the
    # sub-question that built it and takes a quota from each.
    if cfg.rerank and cfg.rerank_per_subquestion and len(pools) > 1:
        with span("rerank", **{"rerank.model": cfg.rerank_model,
                               "rerank.candidates": len(candidates),
                               "rerank.per_subquestion": True}):
            hits, rerank_ms = rerank_per_query(pools, queries, cfg.top_k_context,
                                               cfg.rerank_model)
        return Retrieved(window.expand(hits, used), decision, rerank_ms,
                         sub_questions, candidates)

    if cfg.rerank:
        scorer = rerank_windowed if cfg.rerank_windows else rerank
        hits, rerank_ms = scorer(query, candidates, cfg.top_k_context, cfg.rerank_model)
        return Retrieved(window.expand(hits, used), decision, rerank_ms,
                         sub_questions, candidates)

    return Retrieved(window.expand(candidates[: cfg.top_k_context], used), decision,
                     sub_questions=sub_questions, candidates=candidates)


__all__ = ["retrieve", "Retrieved", "Route", "route", "STRATEGY_FALLBACKS"]
