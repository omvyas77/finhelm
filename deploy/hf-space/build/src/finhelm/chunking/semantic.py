"""Semantic chunking: split where the topic actually shifts.

Embed each sentence, measure cosine distance between consecutive sentences, and start a
new chunk wherever that distance exceeds the document's 90th percentile. The threshold
is per-document on purpose — a fixed global threshold behaves differently on terse FOMC
statements than on sprawling Risk Factors sections.

A token cap is still enforced, because a section that never changes topic would
otherwise produce one enormous chunk that retrieves for everything and grounds nothing.
"""

from __future__ import annotations

import numpy as np

from ..embeddings import encode
from . import Chunk, build, n_tokens, sentences

_PERCENTILE = 90
# Boundary detection needs relative distances between neighbouring sentences, not
# maximal embedding fidelity. Truncating to 128 tokens is ~1.7x faster on the same
# hardware and barely moves the split points.
_BOUNDARY_SEQ_LEN = 128


def _split_points(embeddings: np.ndarray) -> set[int]:
    """Indices where a new chunk should start, from per-document distance spikes."""
    if len(embeddings) < 3:
        return set()
    sims = np.sum(embeddings[:-1] * embeddings[1:], axis=1)  # unit vectors -> cosine
    distances = 1.0 - sims
    threshold = np.percentile(distances, _PERCENTILE)
    return {i + 1 for i, d in enumerate(distances) if d > threshold}


def chunk_doc(doc: dict, cfg) -> list[Chunk]:
    sents = sentences(doc["text"])
    if not sents:
        return []
    if len(sents) == 1:
        return [build(doc, 0, sents[0])]

    breaks = _split_points(encode(sents, cfg.embed_model, max_seq_length=_BOUNDARY_SEQ_LEN))

    costs = [n_tokens(s) for s in sents]
    chunks: list[Chunk] = []
    buf: list[str] = []
    budget = 0

    for i, sent in enumerate(sents):
        starts_new = i in breaks or budget + costs[i] > cfg.chunk_tokens
        if buf and starts_new:
            chunks.append(build(doc, len(chunks), " ".join(buf)))
            buf, budget = [], 0
        buf.append(sent)
        budget += costs[i]

    if buf:
        chunks.append(build(doc, len(chunks), " ".join(buf)))
    return chunks
