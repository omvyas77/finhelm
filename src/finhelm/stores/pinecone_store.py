"""Managed vector store behind the same protocol. Documented stub, not wired.

Deliberately unimplemented. Pinecone earns its cost when the index outgrows a single
machine, when you need it replicated and backed up without owning that problem, or when
write and read traffic have to scale independently. None of those is true here: the
filings index is 24,650 vectors at 768 dimensions — about 76 MB — which fits in memory on
a laptop and is served by FAISS in single-digit milliseconds.

Running a managed store for this corpus would add a network hop, a bill and a vendor to
every query in exchange for capabilities the workload does not use. The class exists so
that the swap is a config change and the protocol is demonstrably backend-agnostic, and
because "we chose not to" is a better answer than "we never considered it".

The three methods below are what an implementation owes the protocol. The only part that
needs real thought is `search`: Pinecone's metadata filters use their own operator syntax
($eq, $in), so `stores.base.matches` semantics — scalar, membership, prefix — would need
translating, and **prefix has no native equivalent**. That matters here: the filing-year
filter is a prefix over a date column. It would have to become an explicit year field at
upsert time, which is a schema decision forced by the backend rather than by the data.
"""

from __future__ import annotations

import numpy as np

from .base import Hit


class PineconeStore:
    def __init__(self, collection: str, dim: int = 768, api_key: str | None = None):
        raise NotImplementedError(
            "PineconeStore is a documented stub. FAISS serves this corpus (24,650 vectors, "
            "~76 MB) from memory; a managed store would add a network hop and a bill for "
            "capabilities this workload does not use. See the module docstring."
        )

    def upsert(self, ids: list[str], vectors: np.ndarray,
               metadata: list[dict]) -> None: ...

    def search(self, vector: np.ndarray, k: int,
               filters: dict | None = None) -> list[Hit]: ...

    def count(self) -> int: ...
