"""Store selection by name, so `cfg.store` is the only thing that changes on Day 3."""

from __future__ import annotations

import functools
from pathlib import Path

from .base import Hit, VectorStore, matches

ROOT = Path(__file__).resolve().parents[3]
INDEX_DIR = ROOT / "data" / "index"


def index_name(collection: str, strategy: str, contextual: bool = False) -> str:
    """Directory name for an index.

    Contextual-header indexes get their own directory rather than overwriting the plain
    one. The two are not interchangeable — a query embedded for one is being compared
    against vectors built under different text — and keeping both on disk is what makes
    the comparison an A/B rather than a before-and-after with no way back.
    """
    return f"{collection}_{strategy}" + ("_ctx" if contextual else "")


@functools.lru_cache(maxsize=6)
def load_store(collection: str, strategy: str, backend: str = "faiss",
               contextual: bool = False) -> VectorStore:
    """Cached: loading a store re-parses meta.jsonl, which is 62 MB for complaints.
    Uncached, a two-collection query spent ~70s on disk I/O before embedding anything.
    maxsize=4 holds both collections for the strategy under test plus room to A/B a
    second strategy without thrashing."""
    if backend == "faiss":
        from .faiss_store import FaissStore

        return FaissStore.load(INDEX_DIR / index_name(collection, strategy, contextual))
    if backend == "pgvector":
        from .pgvector_store import PgVectorStore

        return PgVectorStore(collection=index_name(collection, strategy, contextual))
    raise ValueError(f"unknown store backend: {backend}")


__all__ = ["Hit", "VectorStore", "matches", "load_store", "index_name", "INDEX_DIR"]
