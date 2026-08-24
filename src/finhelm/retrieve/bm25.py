"""Lexical retrieval over the same chunks the dense index was built from.

BM25 exists here to catch what dense retrieval reliably fumbles: exact tickers, CIK and
statute references, "Item 1A", and dollar figures. A 384-dim embedding smears "$1.2
billion" and "$1.5 billion" onto nearly the same point; an inverted index does not.

Tokenisation keeps `$`, `%`, `.` and `-` inside tokens rather than using a bare
`.split()`, so "$1.2bn", "10-K" and "1a" survive as single terms — the exact tokens this
retriever is here to match.
"""

from __future__ import annotations

import functools
import re
from pathlib import Path

import pandas as pd

from ..chunking import DEFAULT_CHUNK_TOKENS, chunks_name
from rank_bm25 import BM25Okapi

from ..stores.base import Hit, matches

ROOT = Path(__file__).resolve().parents[3]
PROCESSED = ROOT / "data" / "processed"

_TOKEN = re.compile(r"[a-z0-9$][a-z0-9.$%-]*")

# BM25 cannot filter during scoring either, so filtered queries over-fetch and post-filter.
FILTER_OVERFETCH = 20


def tokenize(text: str) -> list[str]:
    return _TOKEN.findall(text.lower())


class Bm25Index:
    def __init__(self, texts: list[str], metadata: list[dict], ids: list[str]):
        self._bm25 = BM25Okapi([tokenize(t) for t in texts])
        self._meta = metadata
        self._ids = ids

    def search(self, query: str, k: int, filters: dict | None = None) -> list[Hit]:
        scores = self._bm25.get_scores(tokenize(query))
        fetch = min(len(scores), k * FILTER_OVERFETCH if filters else k)
        # argsort over ~13k floats is cheaper than a heap here and keeps ties stable.
        order = scores.argsort()[::-1][:fetch]

        hits: list[Hit] = []
        for pos in order:
            meta = self._meta[pos]
            if matches(meta, filters):
                hits.append(Hit(self._ids[pos], float(scores[pos]), meta))
            if len(hits) == k:
                break
        return hits

    def count(self) -> int:
        return len(self._ids)


@functools.lru_cache(maxsize=4)
def load_index(collection: str, strategy: str,
               chunk_tokens: int = DEFAULT_CHUNK_TOKENS) -> Bm25Index:
    """Build from the chunk parquet. ~13k docs indexes in a couple of seconds, so this is
    cached per process rather than persisted — one less artifact to keep in sync."""
    df = pd.read_parquet(
        PROCESSED / f"{chunks_name(collection, strategy, chunk_tokens)}.parquet")
    return Bm25Index(
        df["text"].tolist(),
        df.to_dict("records"),
        df["chunk_id"].tolist(),
    )
