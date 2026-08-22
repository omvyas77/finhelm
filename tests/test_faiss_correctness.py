"""Verify the OpenMP workaround in src/finhelm/__init__.py did not corrupt search.

KMP_DUPLICATE_LIB_OK=TRUE is documented as possibly producing *silently* incorrect
results. Silent is the problem: a retrieval system that returns plausible-but-wrong
neighbours would look like a chunking or embedding failure for days. So the FAISS index
is checked against a brute-force numpy cosine over the exact same vectors.
"""

from __future__ import annotations

import numpy as np
import pytest

from src.finhelm.stores import INDEX_DIR, load_store


@pytest.mark.skipif(
    not (INDEX_DIR / "filings_fixed" / "index.faiss").exists(),
    reason="index not built; run scripts/build_index.py",
)
def test_faiss_matches_bruteforce():
    store = load_store("filings", "fixed", "faiss")
    index = store._index
    n, dim = index.ntotal, index.d

    # Reconstruct what FAISS actually holds, so the comparison cannot be fooled by the
    # index and the source parquet disagreeing.
    vectors = index.reconstruct_n(0, n)

    rng = np.random.default_rng(0)
    k = 10
    for probe in rng.choice(n, size=25, replace=False):
        query = vectors[probe : probe + 1]
        _, positions = index.search(query, k)

        scores = vectors @ query[0]
        expected = np.argsort(-scores)[:k]

        # Compare on score, not position: ~1.9% of filing chunks are repeated footnote
        # boilerplate with byte-identical text and therefore identical vectors, so order
        # among tied scores is ambiguous and asserting on position makes this test fail
        # for a reason that is not a correctness bug.
        assert np.allclose(scores[positions[0]], scores[expected], atol=1e-5)

        # A vector must still retrieve something at self-similarity 1.0 first — itself,
        # or one of its exact duplicates.
        assert scores[positions[0][0]] == pytest.approx(1.0, abs=1e-5)
