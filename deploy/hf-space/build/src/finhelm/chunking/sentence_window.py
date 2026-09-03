"""Sentence-window chunking: index one sentence, retrieve a window around it.

Each sentence is embedded alone, which makes retrieval precise — the embedding is not
diluted by neighbouring topics. At generation time the ±cfg.sentence_window neighbours
are spliced back in so the model still sees coherent context.

`text` holds the indexed sentence; `window_text` (rebuilt via expand()) holds what the
generator actually reads. Storing the offsets means reassembly is a slice.
"""

from __future__ import annotations

from . import Chunk, build, sentences


def chunk_doc(doc: dict, cfg) -> list[Chunk]:
    sents = sentences(doc["text"])
    if not sents:
        return []

    w = cfg.sentence_window
    chunks: list[Chunk] = []
    for i, sent in enumerate(sents):
        chunks.append(
            build(
                doc,
                i,
                sent,
                window_start=max(0, i - w),
                window_end=min(len(sents), i + w + 1),
            )
        )
    return chunks


def expand(chunk: Chunk, doc_text: str) -> str:
    """Rebuild the ±window context for a retrieved sentence chunk."""
    if chunk.window_start is None:
        return chunk.text
    sents = sentences(doc_text)
    return " ".join(sents[chunk.window_start : chunk.window_end])
