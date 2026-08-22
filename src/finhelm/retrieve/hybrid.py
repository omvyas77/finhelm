"""Reciprocal rank fusion of dense and lexical results.

RRF fuses on *rank*, not score, which is the whole reason it works here: cosine
similarity lives in [-1, 1] and BM25 is unbounded and corpus-dependent, so any
score-level combination would need per-corpus normalisation that silently rots as the
corpus grows. Rank is comparable by construction.

    score(d) = sum over lists of 1 / (rrf_k + rank(d))

`rrf_k` (60, from the original TREC work) damps the top of each list so that a document
ranked #1 by one retriever cannot alone outvote a document ranked well by both.
"""

from __future__ import annotations

from ..stores.base import Hit


def fuse(lists: list[list[Hit]], k: int, rrf_k: int = 60) -> list[Hit]:
    scores: dict[str, float] = {}
    seen: dict[str, Hit] = {}

    for hits in lists:
        for rank, hit in enumerate(hits, start=1):
            scores[hit.chunk_id] = scores.get(hit.chunk_id, 0.0) + 1.0 / (rrf_k + rank)
            seen.setdefault(hit.chunk_id, hit)

    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:k]
    # Carry the fused score, not the original one — downstream should never compare an
    # RRF score against a raw cosine and think they mean the same thing.
    return [Hit(cid, score, seen[cid].metadata) for cid, score in ranked]
