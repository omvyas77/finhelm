"""FAISS implementation of the VectorStore protocol.

`IndexFlatIP` over normalized vectors gives exact cosine similarity. Flat is the right
choice at this corpus size — approximate indexes (HNSW/IVF) trade recall for speed that
~31k vectors do not need, and an exact baseline keeps the an earlier stage retrieval numbers
attributable to the retriever rather than to index approximation.

Filtering is the known weakness: FAISS has no metadata predicate, so filters are applied
by over-fetching and post-filtering. That is precisely the gap pgvector closes, and
measuring it is the point of the an earlier stage comparison.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from .base import Hit, matches

# How much to over-fetch when filtering, since FAISS cannot filter during search.
FILTER_OVERFETCH = 20


class FaissStore:
    def __init__(self, dim: int = 384):
        import faiss

        self.dim = dim
        self._index = faiss.IndexFlatIP(dim)
        self._ids: list[str] = []
        self._meta: list[dict] = []
        self._pos: dict[str, int] = {}

    def upsert(self, ids: list[str], vectors: np.ndarray, metadata: list[dict]) -> None:
        vectors = np.ascontiguousarray(vectors, dtype="float32")
        if vectors.shape[0] != len(ids) or len(ids) != len(metadata):
            raise ValueError("ids, vectors and metadata must be the same length")

        fresh = [i for i, cid in enumerate(ids) if cid not in self._pos]
        if len(fresh) != len(ids):
            # Flat indexes cannot overwrite in place; rebuilding is the honest fix and
            # this corpus is static, so re-ingest rather than pretend to support it.
            raise NotImplementedError(
                "FaissStore.upsert cannot replace existing ids; rebuild the index"
            )

        for cid, meta in zip(ids, metadata):
            self._pos[cid] = len(self._ids)
            self._ids.append(cid)
            self._meta.append(meta)
        self._index.add(vectors)

    def search(self, vector: np.ndarray, k: int, filters: dict | None = None) -> list[Hit]:
        if self._index.ntotal == 0:
            return []
        query = np.ascontiguousarray(vector, dtype="float32").reshape(1, -1)
        fetch = min(self._index.ntotal, k * FILTER_OVERFETCH if filters else k)
        scores, positions = self._index.search(query, fetch)

        hits: list[Hit] = []
        for score, pos in zip(scores[0], positions[0]):
            if pos < 0:
                continue
            meta = self._meta[pos]
            if matches(meta, filters):
                hits.append(Hit(self._ids[pos], float(score), meta))
            if len(hits) == k:
                break
        return hits

    def count(self) -> int:
        return self._index.ntotal

    def save(self, directory: Path) -> None:
        import faiss

        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        faiss.write_index(self._index, str(directory / "index.faiss"))
        with (directory / "meta.jsonl").open("w", encoding="utf-8") as fh:
            for cid, meta in zip(self._ids, self._meta):
                fh.write(json.dumps({"chunk_id": cid, "metadata": meta}) + "\n")

    @classmethod
    def load(cls, directory: Path) -> "FaissStore":
        import faiss

        directory = Path(directory)
        store = cls.__new__(cls)
        store._index = faiss.read_index(str(directory / "index.faiss"))
        store.dim = store._index.d
        store._ids, store._meta, store._pos = [], [], {}
        with (directory / "meta.jsonl").open(encoding="utf-8") as fh:
            for line in fh:
                row = json.loads(line)
                store._pos[row["chunk_id"]] = len(store._ids)
                store._ids.append(row["chunk_id"])
                store._meta.append(row["metadata"])
        return store
