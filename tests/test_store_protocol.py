"""Every backend must satisfy the same protocol and mean the same thing by a filter.

The protocol is only worth having if a filter behaves identically across backends. FAISS
post-filters in Python via stores.base.matches; pgvector turns the same dict into SQL. If
those two drift, swapping the backend silently changes what a query returns — which is
worse than the backends being obviously incompatible.
"""

import numpy as np
import pytest

from finhelm.config import Config
from finhelm.stores.base import VectorStore, matches
from finhelm.stores.faiss_store import FaissStore
from finhelm.stores.pgvector_store import PgVectorStore
from finhelm.stores.pinecone_store import PineconeStore


def test_faiss_satisfies_the_protocol():
    assert isinstance(FaissStore(dim=8), VectorStore)


@pytest.mark.parametrize("store", [PgVectorStore, PineconeStore])
def test_backends_expose_the_protocol_surface(store):
    for method in ("upsert", "search", "count"):
        assert callable(getattr(store, method, None)), f"{store.__name__} lacks {method}"


def test_pinecone_refuses_loudly_rather_than_half_working():
    with pytest.raises(NotImplementedError, match="documented stub"):
        PineconeStore("filings")


def test_embed_dim_follows_the_model_not_a_default():
    """The regression: embed_dim was a field fixed at 384 while the shipped index is 768.
    Nothing read it, so the disagreement was invisible — until a store used it for DDL,
    where vector(384) rejects every 768-dim insert."""
    assert Config(embed_model="BAAI/bge-small-en-v1.5").embed_dim == 384
    assert Config(embed_model="BAAI/bge-base-en-v1.5").embed_dim == 768
    with pytest.raises(ValueError, match="unknown embedding width"):
        Config(embed_model="nobody/knows").embed_dim


class _Cursor:
    """Captures the SQL pgvector would run, so filter translation is testable with no DB."""
    def __init__(self): self.sql = None; self.params = None
    def execute(self, sql, params=None): self.sql, self.params = sql, params
    def fetchall(self): return []
    def fetchone(self): return (0,)
    def __enter__(self): return self
    def __exit__(self, *a): return False


def _search_sql(filters):
    store = PgVectorStore.__new__(PgVectorStore)
    store.table, store.dim = "filings", 8
    cursor = _Cursor()
    store._conn = type("C", (), {"cursor": lambda self: cursor})()
    PgVectorStore.search(store, np.zeros(8, dtype="float32"), 5, filters)
    return cursor.sql, cursor.params


def test_no_filter_emits_no_where_clause():
    sql, _ = _search_sql(None)
    assert "WHERE" not in sql


@pytest.mark.parametrize("filters,fragment", [
    ({"ticker": "JPM"}, "ticker = %s"),
    ({"form": ["10-K", "10-Q"]}, "form = ANY(%s)"),
    ({"date": {"prefix": ("2024", "2025")}}, "LIKE"),
])
def test_each_filter_shape_becomes_a_predicate(filters, fragment):
    sql, _ = _search_sql(filters)
    assert "WHERE" in sql and fragment in sql


def test_prefix_filter_matches_the_faiss_semantics():
    """Both backends must agree that {"prefix": (...)} means startswith on any of them."""
    metadata = {"date": "2025-02-07"}
    assert matches(metadata, {"date": {"prefix": ("2024", "2025")}}) is True
    assert matches(metadata, {"date": {"prefix": ("2023",)}}) is False
    _, params = _search_sql({"date": {"prefix": ("2024", "2025")}})
    assert "2024%" in params and "2025%" in params
