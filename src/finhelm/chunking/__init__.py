"""One interface over three chunking strategies, so Day 2 can ablate them.

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
from nltk.tokenize import sent_tokenize

_ENC = tiktoken.get_encoding("cl100k_base")


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
