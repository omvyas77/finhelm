"""Splice a retrieved sentence back into its ±window of neighbours.

sentence_window indexes one sentence at a time so the embedding is not diluted by
neighbouring topics. That is the whole point of the strategy — and it is only half of it.
A lone sentence averages 213 characters here, which is not enough context to answer from,
and not enough text for a gold span to match against either. The neighbours have to come
back before the hits leave retrieval.

`chunking/sentence_window.py` said so from the start ("at generation time the ±window
neighbours are spliced back in") and shipped an `expand()` to do it, but nothing ever
called it. The cost was invisible in the usual way: the ablation still produced numbers,
and the numbers were merely low. Measured on the four sentence_window cells, expansion
recovers 12 hits and loses none — recall@5 for sentence_window-hybrid-rr goes 0.269 ->
0.315. Without it the row was competing at ~1/9th the text width of the fixed row.

*Why here, and not earlier.* Expansion runs once, after reranking, on the hits that
actually made it into the context window. The bi-encoder and the cross-encoder both score
the bare sentence, which is the precision the strategy is bought for; handing the
cross-encoder a 1,900-character window would give back exactly what the sentence index was
built to avoid. Expanding last also means ~8 lookups per query instead of ~20 per
collection.

*Why keyed per hit rather than per run.* A single result set can mix strategies:
`complaints` has no sentence_window chunks, so it falls back to `fixed` (see
_resolve_strategy) while `filings` does not. A chunk_id that is not in the window map is
left alone, so the mixed case needs no special casing.
"""

from __future__ import annotations

import functools
from dataclasses import replace

import pandas as pd

from ..stores.base import Hit
from .bm25 import PROCESSED

STRATEGY = "sentence_window"


@functools.lru_cache(maxsize=2)
def _windows(collection: str) -> tuple[dict, dict]:
    """(sentences by section, window span by chunk_id) for one collection.

    Cached per process like the BM25 index, and for the same reason: this is derived from
    an artifact already on disk, so rebuilding it is cheaper than keeping a third copy in
    sync.
    """
    df = pd.read_parquet(
        PROCESSED / f"chunks_{collection}_{STRATEGY}.parquet",
        columns=["chunk_id", "doc_id", "section", "text", "window_start", "window_end"],
    )

    # The trailing integer of a chunk_id is the sentence's position in the list that
    # window_start/window_end index into, and chunk_doc() is called per (document,
    # section) — so the key has to be both. Grouping on doc_id alone silently mis-orders
    # the 15 of 460 filings that carry more than one section, which does not raise: it
    # yields a window of real sentences from the wrong part of the filing.
    df["i"] = df["chunk_id"].str.rsplit("_", n=1).str[-1].astype(int)
    df["key"] = list(zip(df["doc_id"], df["section"]))

    sentences = {key: group.sort_values("i")["text"].tolist()
                 for key, group in df.groupby("key")}
    spans = {chunk_id: (key, int(start), int(end)) for chunk_id, key, start, end
             in zip(df["chunk_id"], df["key"], df["window_start"], df["window_end"])}
    return sentences, spans


def expand(hits: list[Hit], used: list[tuple[str, str]]) -> list[Hit]:
    """Replace each sentence hit's text with its ±window, leaving other hits untouched.

    `used` is the (collection, resolved strategy) pairs this query actually read, so a
    sweep that is not running sentence_window pays nothing — not even a parquet load.
    """
    collections = [c for c, strategy in used if strategy == STRATEGY]
    if not collections:
        return hits

    sentences: dict = {}
    spans: dict = {}
    for collection in collections:
        collection_sentences, collection_spans = _windows(collection)
        sentences.update(collection_sentences)
        spans.update(collection_spans)

    expanded = []
    for hit in hits:
        span = spans.get(hit.chunk_id)
        if span is None:
            expanded.append(hit)
            continue
        key, start, end = span
        text = " ".join(sentences[key][start:end])
        # A new metadata dict, never a mutation: BM25 hands out the cached parquet
        # records themselves, so writing through `hit.metadata` would rewrite the index's
        # own copy and every later query in the process would retrieve pre-expanded text.
        expanded.append(replace(hit, metadata={**hit.metadata, "text": text}))
    return expanded
