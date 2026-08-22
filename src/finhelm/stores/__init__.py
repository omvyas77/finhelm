"""Store selection by name, so `cfg.store` is the only thing that changes on Day 3."""

from __future__ import annotations

from pathlib import Path

from .base import Hit, VectorStore, matches

ROOT = Path(__file__).resolve().parents[3]
INDEX_DIR = ROOT / "data" / "index"


def load_store(collection: str, strategy: str, backend: str = "faiss") -> VectorStore:
    if backend == "faiss":
        from .faiss_store import FaissStore

        return FaissStore.load(INDEX_DIR / f"{collection}_{strategy}")
    if backend == "pgvector":
        from .pgvector_store import PgVectorStore

        return PgVectorStore(collection=f"{collection}_{strategy}")
    raise ValueError(f"unknown store backend: {backend}")


__all__ = ["Hit", "VectorStore", "matches", "load_store", "INDEX_DIR"]
