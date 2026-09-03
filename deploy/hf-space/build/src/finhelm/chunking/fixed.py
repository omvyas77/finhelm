"""Fixed-size chunking: ~cfg.chunk_tokens per chunk with cfg.chunk_overlap carryover.

The baseline. Packs whole sentences up to the token budget rather than cutting
mid-sentence, and carries the tail of each chunk into the next so a fact spanning a
boundary is still retrievable from one side.
"""

from __future__ import annotations

from . import Chunk, build, n_tokens, sentences


def chunk_doc(doc: dict, cfg) -> list[Chunk]:
    sents = sentences(doc["text"])
    if not sents:
        return []

    costs = [n_tokens(s) for s in sents]
    chunks: list[Chunk] = []
    start = 0

    while start < len(sents):
        total, end = 0, start
        while end < len(sents) and total + costs[end] <= cfg.chunk_tokens:
            total += costs[end]
            end += 1
        if end == start:  # single sentence exceeds the budget; take it whole
            end = start + 1

        chunks.append(build(doc, len(chunks), " ".join(sents[start:end])))

        if end >= len(sents):
            break

        # Step back far enough to carry ~chunk_overlap tokens into the next chunk.
        carry, back = 0, end
        while back > start + 1 and carry + costs[back - 1] <= cfg.chunk_overlap:
            back -= 1
            carry += costs[back]
        start = back

    return chunks
