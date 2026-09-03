"""Cross-encoder reranking.

Bi-encoders (what the FAISS index holds) embed the query and the document independently,
so the score is a dot product between two vectors that never saw each other. That is what
makes the index possible — documents are embedded once, offline — and it is also its
ceiling: the model cannot condition its reading of the passage on the question.

A cross-encoder runs query and passage through the network together and scores the pair
directly. It is far more accurate and completely unindexable: scoring N passages means N
forward passes at query time. So it is only usable as a second stage over a candidate list
the bi-encoder has already narrowed to ~20.

The latency this costs is the point of measuring it, not an incidental detail. Reranking
is the single largest quality-per-millisecond decision in the pipeline, and "it depends on
your latency budget" is only a real answer if you have the number.
"""

from __future__ import annotations

import functools
import time

from dataclasses import replace

from ..stores.base import Hit


@functools.lru_cache(maxsize=2)
def _model(name: str):
    """Loaded lazily and cached.

    The import is inside the function so that `--retriever dense` runs never pay the
    ~1.5s cost of importing sentence_transformers, and so a machine that only serves
    dense retrieval does not need the reranker weights on disk at all.
    """
    from sentence_transformers import CrossEncoder

    return CrossEncoder(name)


# Sliding-window scoring.
#
# bge-reranker-base has a 512-token window and the cross-encoder truncates the pair from the
# end, so a passage longer than the budget is scored on its prefix alone. Measured on this
# corpus that is not an edge case: 44% of query+passage pairs exceed 512 (passages p50 356
# tokens, p95 922, max 974), and **24% of pooled multi-span gold spans sit past the cut** —
# 14% for single-span. The reranker is not misjudging those passages, it is scoring text
# that does not contain the answer.
#
# Windowing splits an over-long passage into overlapping spans and keeps the best score, so
# a gold span anywhere in the passage can win. Overlap is half a window because a span
# landing exactly on a boundary would otherwise be split across two windows and score poorly
# in both — the failure the whole mechanism exists to remove.
WINDOW_OVERLAP = 0.5


@functools.lru_cache(maxsize=4)
def _tokenizer(model_name: str):
    from transformers import AutoTokenizer

    return AutoTokenizer.from_pretrained(model_name)


def _passage_windows(text: str, model_name: str, budget: int) -> list[str]:
    """`text` split into overlapping spans that each fit the cross-encoder's budget."""
    tok = _tokenizer(model_name)
    ids = tok(text, add_special_tokens=False)["input_ids"]
    if len(ids) <= budget:
        return [text]
    stride = max(1, int(budget * (1 - WINDOW_OVERLAP)))
    out = []
    for start in range(0, len(ids), stride):
        piece = ids[start : start + budget]
        if not piece:
            break
        out.append(tok.decode(piece))
        if start + budget >= len(ids):
            break
    return out


def rerank_windowed(query: str, hits: list[Hit], k: int,
                    model_name: str) -> tuple[list[Hit], int]:
    """Rerank scoring every window of an over-long passage and keeping its best.

    Same contract as `rerank`. Costs one forward pass per window rather than per passage;
    with these passage lengths that is roughly 1.5x the pairs.
    """
    if not hits:
        return [], 0

    started = time.monotonic()
    tok = _tokenizer(model_name)
    budget = 512 - len(tok(query)["input_ids"]) - 3

    pairs, owner = [], []
    for i, hit in enumerate(hits):
        for window in _passage_windows(hit.text, model_name, budget):
            pairs.append((query, window))
            owner.append(i)

    scores = _model(model_name).predict(pairs, show_progress_bar=False)
    best = [float("-inf")] * len(hits)
    for idx, score in zip(owner, scores):
        best[idx] = max(best[idx], float(score))

    ranked = sorted(zip(hits, best), key=lambda pair: pair[1], reverse=True)
    elapsed_ms = int((time.monotonic() - started) * 1000)
    return [Hit(h.chunk_id, float(s), h.metadata) for h, s in ranked[:k]], elapsed_ms


def rerank(query: str, hits: list[Hit], k: int, model_name: str) -> tuple[list[Hit], int]:
    """Rescore `hits` against the query and return the top k, plus elapsed milliseconds.

    The returned Hit carries the cross-encoder score, not the original cosine or RRF
    score. This is deliberate: downstream code and the results file should report the
    score that actually determined the ordering. The bi-encoder score is not preserved
    because keeping two scores on one object invites reading the wrong one.
    """
    if not hits:
        return [], 0

    started = time.monotonic()
    # Single batched call — one forward pass over all pairs is several times faster than
    # looping, and the candidate list is small enough to fit in one batch comfortably.
    scores = _model(model_name).predict(
        [(query, hit.text) for hit in hits],
        show_progress_bar=False,
    )
    ranked = sorted(zip(hits, scores), key=lambda pair: float(pair[1]), reverse=True)
    elapsed_ms = int((time.monotonic() - started) * 1000)

    return [Hit(hit.chunk_id, float(score), hit.metadata) for hit, score in ranked[:k]], elapsed_ms


def rerank_per_query(pools: list[list[Hit]], queries: list[str], k: int,
                     model_name: str) -> tuple[list[Hit], int]:
    """Rerank each pool against the query that produced it, then take a quota from each.

    The single stage that loses the most evidence on multi-document questions. Reranking
    the merged pool against the *original* question asks the cross-encoder which passages
    best answer a string naming two facts, and the honest answer is the passages vaguely
    about both — never the one that decisively answers half of it. Measured on the 67
    multi-span questions: 33% of gold spans sat in the candidate pool and were dropped
    here, against 37% that never reached the pool at all.

    Scoring each pool against its own sub-question removes the mismatch, and a quota per
    pool makes both halves representable by construction rather than by luck.

    Quotas are handed out round-robin from each pool's reranked order rather than as a
    fixed ceil(k/n) slice, so a pool with fewer candidates than its share gives the
    remainder back instead of leaving the budget short.

    An earlier attempt at allocation lost on every tier, and the difference matters: it
    split the *context* budget while still scoring against the compound original, so it
    diluted single-span questions without fixing the mismatch. Decomposition is now gated
    by worth_splitting, so questions needing one fact never reach this path at all.
    """
    started = time.monotonic()
    ranked = []
    for pool, query in zip(pools, queries):
        if not pool:
            ranked.append([])
            continue
        scores = _model(model_name).predict([(query, h.text) for h in pool],
                                            show_progress_bar=False)
        order = sorted(zip(pool, scores), key=lambda p: float(p[1]), reverse=True)
        ranked.append([replace(h, score=float(sc)) for h, sc in order])

    out: list[Hit] = []
    seen: set[str] = set()
    for depth in range(max((len(r) for r in ranked), default=0)):
        for pool in ranked:
            if depth >= len(pool):
                continue
            hit = pool[depth]
            if hit.chunk_id in seen:
                continue
            seen.add(hit.chunk_id)
            out.append(hit)
            if len(out) >= k:
                return out, int((time.monotonic() - started) * 1000)
    return out, int((time.monotonic() - started) * 1000)
