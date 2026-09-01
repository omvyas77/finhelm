"""Store selection by name, so `cfg.store` is the only thing that changes on Day 3."""

from __future__ import annotations

import functools
from pathlib import Path

from .base import Hit, VectorStore, matches

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_EMBED_MODEL = "BAAI/bge-small-en-v1.5"
INDEX_DIR = ROOT / "data" / "index"


def index_name(collection: str, strategy: str, contextual: bool = False,
               embed_model: str | None = None, chunk_tokens: int = 800) -> str:
    """Directory name for an index.

    Contextual-header indexes get their own directory rather than overwriting the plain
    one. The two are not interchangeable — a query embedded for one is being compared
    against vectors built under different text — and keeping both on disk is what makes
    the comparison an A/B rather than a before-and-after with no way back.
    """
    name = f"{collection}_{strategy}" + ("_ctx" if contextual else "")
    # The embedding model is part of an index's identity: vectors from two models are not
    # comparable and have different dimensionality, so loading the wrong one either throws
    # or — worse, if the dims happen to match — returns confident nonsense. The default
    # model keeps the bare name so every index built before this stays loadable.
    if embed_model and embed_model != DEFAULT_EMBED_MODEL:
        name += "_" + embed_model.rsplit("/", 1)[-1].replace(".", "")
    # Chunk size likewise: an index built at 400 tokens holds different chunks under the
    # same chunk_ids as one built at 800, so sharing a directory would silently pair
    # BM25's idea of a chunk with FAISS's different one.
    if chunk_tokens != 800:
        name += f"_t{chunk_tokens}"
    return name


@functools.lru_cache(maxsize=6)
def load_store(collection: str, strategy: str, backend: str = "faiss",
               contextual: bool = False, embed_model: str | None = None,
               chunk_tokens: int = 800) -> VectorStore:
    """Cached: loading a store re-parses meta.jsonl, which is 62 MB for complaints.
    Uncached, a two-collection query spent ~70s on disk I/O before embedding anything.
    maxsize=4 holds both collections for the strategy under test plus room to A/B a
    second strategy without thrashing."""
    if backend == "faiss":
        from .faiss_store import FaissStore

        return FaissStore.load(INDEX_DIR / index_name(collection, strategy, contextual,
                                                      embed_model, chunk_tokens))
    if backend == "pgvector":
        from ..config import EMBED_DIMS
        from .pgvector_store import PgVectorStore

        # The table's vector column is sized here, before anything is embedded, so it has
        # to come from the same table Config.embed_dim reads. Taking the constructor
        # default instead would create vector(768) for a 384-dim model and reject every
        # insert — with a Postgres error far from the line that chose the model.
        model = embed_model or DEFAULT_EMBED_MODEL
        return PgVectorStore(collection=index_name(collection, strategy, contextual,
                                                   embed_model, chunk_tokens),
                             dim=EMBED_DIMS[model])
    raise ValueError(f"unknown store backend: {backend}")


__all__ = ["Hit", "VectorStore", "matches", "load_store", "index_name", "INDEX_DIR",
           "DEFAULT_EMBED_MODEL"]
