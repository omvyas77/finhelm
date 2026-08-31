"""Postgres + pgvector behind the same VectorStore protocol as FAISS.

Exists to make the backend a configuration choice rather than a rewrite, and to have a
real answer to "what happens when the corpus outgrows a flat index". The interesting
comparison is not raw speed — FAISS wins that at this size — but metadata filtering.

FAISS has no notion of a WHERE clause. `faiss_store.search` post-filters: it over-fetches
`k * FILTER_OVERFETCH` candidates and discards the ones that do not match, which means a
narrow filter can exhaust the window and quietly return fewer than k results. Postgres
applies the predicate as part of the query. That difference is why the issuer and form
filters in this project needed a backoff path, and it is the honest argument for a real
database rather than "it scales better".
"""

from __future__ import annotations

import json
import os
from typing import Iterable

import numpy as np

from .base import Hit

DEFAULT_DSN = os.getenv("FINHELM_PG_DSN",
                        "postgresql://finhelm:finhelm@localhost:5432/finhelm")

# Metadata promoted to real columns because they are what gets filtered on. Anything else
# lives in a jsonb blob: the point of columns here is predicates, not storage.
COLUMNS = ["chunk_id", "doc_id", "source", "ticker", "form", "date", "section", "url",
           "text"]


class PgVectorStore:
    """One table per (collection, strategy, model), named like the FAISS index directory."""

    def __init__(self, collection: str, dim: int = 768, dsn: str | None = None):
        import psycopg2

        self.collection = collection
        self.table = collection.replace("-", "_")
        self.dim = dim
        self._dsn = dsn or DEFAULT_DSN
        self._conn = psycopg2.connect(self._dsn)
        self._conn.autocommit = True
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        with self._conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {self.table} (
                    chunk_id TEXT PRIMARY KEY,
                    doc_id   TEXT, source TEXT, ticker TEXT, form TEXT,
                    date     DATE, section TEXT, url TEXT, text TEXT,
                    extra    JSONB,
                    embedding vector({self.dim})
                );
            """)
            # HNSW over cosine distance, matching the FAISS index, which stores normalised
            # vectors and searches by inner product — equivalent ordering for unit vectors.
            cur.execute(f"""
                CREATE INDEX IF NOT EXISTS {self.table}_embedding_idx
                ON {self.table} USING hnsw (embedding vector_cosine_ops);
            """)
            # The filter columns get their own indexes. Without them a predicate is a
            # sequential scan and the comparison against FAISS measures the missing index
            # rather than the engine.
            for column in ("ticker", "form", "date", "doc_id"):
                cur.execute(f"CREATE INDEX IF NOT EXISTS {self.table}_{column}_idx "
                            f"ON {self.table} ({column});")

    def upsert(self, ids: list[str], vectors: np.ndarray, metadata: list[dict]) -> None:
        from psycopg2.extras import execute_values

        rows = []
        for chunk_id, vector, meta in zip(ids, vectors, metadata):
            extra = {k: v for k, v in meta.items() if k not in COLUMNS}
            rows.append((
                chunk_id, meta.get("doc_id"), meta.get("source"), meta.get("ticker"),
                meta.get("form"), meta.get("date") or None, meta.get("section"),
                meta.get("url"), meta.get("text"), json.dumps(extra, default=str),
                # pgvector's text form; psycopg2 has no native adapter for it.
                "[" + ",".join(f"{float(x):.6f}" for x in vector) + "]",
            ))
        with self._conn.cursor() as cur:
            execute_values(cur, f"""
                INSERT INTO {self.table}
                    (chunk_id, doc_id, source, ticker, form, date, section, url, text,
                     extra, embedding)
                VALUES %s
                ON CONFLICT (chunk_id) DO UPDATE SET
                    embedding = EXCLUDED.embedding, text = EXCLUDED.text,
                    extra = EXCLUDED.extra
            """, rows, page_size=500)

    def search(self, vector: np.ndarray, k: int, filters: dict | None = None) -> list[Hit]:
        """Nearest neighbours, with the filter applied as a predicate rather than after.

        The three filter shapes mirror stores.base.matches exactly — scalar equality,
        membership, and {"prefix": ...} — because a filter that means one thing against
        FAISS and another against Postgres would make the backends silently
        non-interchangeable, which is the whole point of the protocol.
        """
        clauses, params = [], []
        for field, want in (filters or {}).items():
            if field not in COLUMNS:
                continue
            if isinstance(want, dict) and "prefix" in want:
                prefixes = want["prefix"]
                prefixes = (prefixes,) if isinstance(prefixes, str) else tuple(prefixes)
                clauses.append("(" + " OR ".join(
                    [f"{field}::text LIKE %s" for _ in prefixes]) + ")")
                params.extend(f"{p}%" for p in prefixes)
            elif isinstance(want, (list, tuple, set)):
                clauses.append(f"{field} = ANY(%s)")
                params.append(list(want))
            else:
                clauses.append(f"{field} = %s")
                params.append(want)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        literal = "[" + ",".join(f"{float(x):.6f}" for x in vector) + "]"

        with self._conn.cursor() as cur:
            cur.execute(f"""
                SELECT chunk_id, doc_id, source, ticker, form, date, section, url, text,
                       1 - (embedding <=> %s::vector) AS score
                FROM {self.table}
                {where}
                ORDER BY embedding <=> %s::vector
                LIMIT %s
            """, [literal, *params, literal, k])
            rows = cur.fetchall()

        hits = []
        for row in rows:
            meta = dict(zip(COLUMNS, row[:len(COLUMNS)]))
            meta["date"] = str(meta["date"]) if meta["date"] else None
            hits.append(Hit(row[0], float(row[-1]), meta))
        return hits

    def count(self) -> int:
        with self._conn.cursor() as cur:
            cur.execute(f"SELECT count(*) FROM {self.table}")
            return int(cur.fetchone()[0])

    def close(self) -> None:
        self._conn.close()
