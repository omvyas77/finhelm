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


def interleave(pools: list[list[Hit]], k: int) -> list[Hit]:
    """Round-robin across pools, taking rank 1 from each, then rank 2, and so on.

    The alternative to RRF when the pools are *different questions* rather than different
    retrievers over the same question. Summing reciprocal ranks across sub-question pools
    scores a chunk by how many pools it appears in, and with rrf_k=60 on 20-item lists the
    within-list spread is only 1.31x while each additional pool adds a full increment:

        rank  1 in one pool    0.016393
        rank 20 in two pools   0.025000   <- wins

    A chunk ranked last in two pools therefore outranks a chunk ranked first in one. For a
    comparison that is exactly backwards. The passage answering the AXP half is rank 1 in
    the AXP sub-question's pool and absent from the JPM one; a generic passage mediocre in
    both is present twice and wins. Traced on six multi-span questions, every one of the
    fused top-8 appeared in more than one pool, and gold chunks sitting at rank 6, 15 and
    17 in a single pool came out at 36, 42 and 52 after fusion.

    Interleaving keeps each pool's own ordering and guarantees every sub-question
    contributes before any pool contributes twice, so a passage that decisively answers
    one half of a comparison cannot be outvoted by one that vaguely answers both.

    Deduplicates on chunk_id, keeping the earliest occurrence: a chunk that several pools
    agree on still surfaces early, it simply cannot accumulate a score by repetition.
    """
    if not pools:
        return []
    out: list[Hit] = []
    seen: set[str] = set()
    for rank in range(max(len(p) for p in pools)):
        for pool in pools:
            if rank >= len(pool):
                continue
            hit = pool[rank]
            if hit.chunk_id in seen:
                continue
            seen.add(hit.chunk_id)
            out.append(hit)
            if len(out) >= k:
                return out
    return out
