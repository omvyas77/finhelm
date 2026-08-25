"""The VectorStore protocol.

Defining this before any implementation is what keeps FAISS, pgvector and Pinecone a
config change rather than a rewrite. Every backend takes the same arguments and returns
the same `Hit` shape, so `retrieve/dense.py` never learns which one it is talking to.

Metadata filtering is part of the protocol on purpose. It is the capability that
separates a real vector store from a flat index, and pre-filtered retrieval
("only JPM, only 2024 onward") is a genuine production requirement rather than a nicety.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import numpy as np


@dataclass(frozen=True)
class Hit:
    chunk_id: str
    score: float
    metadata: dict

    @property
    def text(self) -> str:
        return self.metadata.get("text", "")


@runtime_checkable
class VectorStore(Protocol):
    def upsert(self, ids: list[str], vectors: np.ndarray, metadata: list[dict]) -> None:
        """Insert or replace. Must be idempotent on chunk_id."""
        ...

    def search(self, vector: np.ndarray, k: int, filters: dict | None = None) -> list[Hit]:
        """Top-k by cosine similarity, optionally restricted by exact-match metadata.

        `filters` maps a metadata field to either a scalar (equality) or a list
        (membership). Backends that cannot filter natively must still honour it.
        """
        ...

    def count(self) -> int:
        ...


def matches(metadata: dict, filters: dict | None) -> bool:
    """Shared filter semantics, so every backend agrees on what a filter means.

    Three forms, all declarative so that a backend which filters in its own query
    language can express them: a scalar is equality, a list/tuple/set is membership, and
    `{"prefix": "..."}` is a string prefix test.

    The prefix form exists because `date` is stored as an ISO day ("2024-02-23") and the
    question that needs filtering names a year. Encoding that as a predicate or a callable
    would have been shorter here and unimplementable in SQL or Pinecone; a prefix is
    `LIKE '2024%'` everywhere.
    """
    if not filters:
        return True
    for field, want in filters.items():
        got = metadata.get(field)
        if isinstance(want, dict) and "prefix" in want:
            if not isinstance(got, str) or not got.startswith(want["prefix"]):
                return False
        elif isinstance(want, (list, tuple, set)):
            if got not in want:
                return False
        elif got != want:
            return False
    return True
