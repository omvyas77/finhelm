"""One interface over three chunking strategies, so an earlier stage can ablate them.

    chunk(doc, cfg) -> list[Chunk]

Note on paragraph boundaries: the ingest `to_text` collapses all whitespace, so the
corpus has no paragraph structure left to split on. Sentence boundaries are used
instead. That is a deliberate tradeoff — retaining paragraphs would mean carrying
markup-derived structure through every source, and SEC HTML is too inconsistent for
that to be reliable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import tiktoken

_ENC = tiktoken.get_encoding("cl100k_base")

# The chunk size everything before the size sweep was built at. Files produced at this
# size keep their original unsuffixed names so no existing artifact has to be rebuilt.
DEFAULT_CHUNK_TOKENS = 800


def chunks_name(collection: str, strategy: str,
                chunk_tokens: int = DEFAULT_CHUNK_TOKENS) -> str:
    """Filename stem for a chunk parquet.

    The size belongs in the name for the same reason the embedding model belongs in the
    index name: two parquets built at different sizes are not interchangeable, and without
    it `--chunk-tokens 400` overwrites the 800-token file that every existing index and
    every measured result was built from. That failure is silent — the rebuild succeeds,
    and only the next eval reveals that BM25 and the FAISS index now disagree about what
    chunk_id means.
    """
    stem = f"chunks_{collection}_{strategy}"
    return stem if chunk_tokens == DEFAULT_CHUNK_TOKENS else f"{stem}_t{chunk_tokens}"


@dataclass(frozen=True)
class Chunk:
    chunk_id: str
    doc_id: str
    source: str
    ticker: str | None
    form: str
    date: str
    section: str
    url: str
    text: str
    # sentence-window only: the sentence index range this chunk expands to at
    # generation time. Stored so reassembly is a slice, not a re-parse.
    window_start: int | None = None
    window_end: int | None = None

    def to_dict(self) -> dict:
        return {
            "chunk_id": self.chunk_id, "doc_id": self.doc_id, "source": self.source,
            "ticker": self.ticker, "form": self.form, "date": self.date,
            "section": self.section, "url": self.url, "text": self.text,
            "window_start": self.window_start, "window_end": self.window_end,
        }


def n_tokens(text: str) -> int:
    return len(_ENC.encode(text, disallowed_special=()))


def slug(value: str) -> str:
    """Grep-safe section label."""
    return re.sub(r"[^a-z0-9]+", "_", (value or "unknown").lower()).strip("_")


def sentences(text: str) -> list[str]:
    """Sentence split, with a hard cap so a runaway 'sentence' can't blow a chunk."""
    # Imported here, not at module scope, because sentence splitting is an *indexing*
    # concern and this module is on the serving path only for chunks_name(). A top-level
    # import made every serving process carry nltk and its punkt corpus for a function it
    # never calls — which is how the deployed Space died on ModuleNotFoundError after
    # being given a correctly minimal dependency set.
    from nltk.tokenize import sent_tokenize

    out: list[str] = []
    for s in sent_tokenize(text):
        s = s.strip()
        if not s:
            continue
        # SEC text occasionally loses periods when tables are stripped, producing
        # multi-thousand-token "sentences". Split those on a word budget.
        if n_tokens(s) > 400:
            buf: list[str] = []
            for w in s.split():
                buf.append(w)
                if len(buf) >= 250:
                    out.append(" ".join(buf))
                    buf = []
            if buf:
                out.append(" ".join(buf))
        else:
            out.append(s)
    return out


def make_chunk_id(doc: dict, index: int) -> str:
    return f"{doc['doc_id']}_{slug(doc['section'])}_{index:03d}"


def build(doc: dict, index: int, text: str, **extra) -> Chunk:
    return Chunk(
        chunk_id=make_chunk_id(doc, index),
        doc_id=doc["doc_id"],
        source=doc["source"],
        ticker=doc.get("ticker"),
        form=doc["form"],
        date=doc["date"],
        section=doc["section"],
        url=doc["url"],
        text=text,
        **extra,
    )


def chunk(doc: dict, cfg) -> list[Chunk]:
    """Dispatch on cfg.chunking."""
    from . import fixed, semantic, sentence_window

    strategies = {
        "fixed": fixed.chunk_doc,
        "semantic": semantic.chunk_doc,
        "sentence_window": sentence_window.chunk_doc,
    }
    if cfg.chunking not in strategies:
        raise ValueError(f"unknown chunking strategy: {cfg.chunking}")
    return strategies[cfg.chunking](doc, cfg)
