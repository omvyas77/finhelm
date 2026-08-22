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
