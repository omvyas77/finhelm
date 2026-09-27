"""Reciprocal rank fusion of dense and lexical results.

RRF fuses on rank, not score. Cosine similarity lives in [-1, 1] while BM25 is unbounded
and corpus-dependent, so combining scores would need per-corpus normalisation that goes
stale as the corpus grows. Ranks are comparable as they are.

    score(d) = sum over lists of 1 / (rrf_k + rank(d))

`rrf_k` (60, from the original TREC work) damps the top of each list, so a document
ranked first by one retriever cannot outvote a document ranked well by both.
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
    # Carry the fused score, not the original: an RRF score and a raw cosine are not
    # comparable quantities.
    return [Hit(cid, score, seen[cid].metadata) for cid, score in ranked]


def interleave(pools: list[list[Hit]], k: int) -> list[Hit]:
    """Round-robin across pools, taking rank 1 from each, then rank 2, and so on.

    Used when the pools are different *questions* rather than different retrievers over
    one question. Summing reciprocal ranks across sub-question pools scores a chunk by how
    many pools it appears in: with rrf_k=60 on 20-item lists the within-list spread is
    1.31x, while each extra pool adds a full increment.

        rank  1 in one pool    0.016393
        rank 20 in two pools   0.025000   <- wins

    So a chunk ranked last in two pools outranks one ranked first in a single pool, which
    is backwards for a comparison: the passage answering the AXP half is rank 1 in the AXP
    pool and absent from the JPM one, while a generic passage appears in both. Traced on
    six multi-span questions, every fused top-8 chunk appeared in more than one pool, and
    gold chunks at ranks 6, 15 and 17 in a single pool fell to 36, 42 and 52 after fusion.

    Interleaving preserves each pool's ordering and lets every sub-question contribute
    before any pool contributes twice. Deduplicates on chunk_id keeping the earliest
    occurrence, so agreement across pools still surfaces early but cannot accumulate.
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
