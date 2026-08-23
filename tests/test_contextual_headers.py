"""Contextual headers must add context without changing what the eval measures."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evals import metrics as M  # noqa: E402
from src.finhelm.chunking.context import contextualize, header  # noqa: E402
from src.finhelm.embeddings import query_prefix  # noqa: E402
from src.finhelm.stores import index_name  # noqa: E402

EDGAR = {"source": "edgar", "ticker": "JPM", "form": "10-K", "date": "2024-02-16",
         "section": "item_1a_risk_factors"}
FOMC = {"source": "fomc", "ticker": None, "form": "statement", "date": "2024-03-20",
        "section": "full_document"}
CFPB = {"source": "cfpb", "ticker": None, "form": "complaint", "date": "2023-09-30",
        "section": "Credit card"}


def test_header_names_issuer_period_and_section():
    got = header(EDGAR)
    assert "JPMorgan Chase" in got and "JPM" in got
    assert "2024-02-16" in got and "Risk Factors" in got


def test_fomc_and_cfpb_never_render_an_unknown_issuer():
    """Both sources have a null ticker. The generic branch would label them 'Unknown
    issuer', which is worse than no header at all — it puts the same misleading token
    into 1,339 filing vectors and all 18,498 complaint vectors."""
    for row in (FOMC, CFPB):
        assert "Unknown" not in header(row)
    assert "Federal Open Market Committee" in header(FOMC)
    assert "credit card" in header(CFPB)


def test_unmapped_ticker_falls_back_to_the_symbol():
    got = header({**EDGAR, "ticker": "ZZZ"})
    assert "ZZZ" in got and "Unknown" not in got


def test_header_is_prepended_not_appended():
    """Chunks over the model's 512-token window are truncated from the end, so a header
    at the back would be dropped from exactly the long chunks that need it most."""
    out = contextualize("body text here", EDGAR)
    assert out.startswith(header(EDGAR))
    assert out.endswith("body text here")


def test_contextualizing_never_changes_is_hit():
    """The header is applied to embedding input only. If it ever reached the stored text
    it would still match gold n-grams — so this would pass — but it would also enter the
    BM25 index and make every JPM chunk match the query 'JPMorgan' equally. This pins the
    property that matters: adding a header cannot flip a gold-span judgement either way.
    """
    golden = Path(__file__).resolve().parents[1] / "evals" / "golden_set.jsonl"
    spans = [s for line in golden.read_text().splitlines() if line.strip()
             for s in (json.loads(line).get("gold_spans") or [])]
    assert spans, "golden set has no gold spans"

    for span in spans[:40]:
        bare = {"doc_id": span["doc_id"], "text": span["snippet"]}
        withhdr = {"doc_id": span["doc_id"], "text": contextualize(span["snippet"], EDGAR)}
        assert M.is_hit(bare, span) == M.is_hit(withhdr, span)


def test_contextual_indexes_get_their_own_directory():
    """A ctx index and a plain index hold vectors built from different text. Sharing a
    directory would make the A/B a one-way door and silently mix the two."""
    assert index_name("filings", "semantic", False) == "filings_semantic"
    assert index_name("filings", "semantic", True) == "filings_semantic_ctx"


@pytest.mark.parametrize("model,expected", [
    ("BAAI/bge-small-en-v1.5", "Represent this sentence for searching relevant passages: "),
    ("intfloat/e5-base", "query: "),
    ("some/unknown-model", ""),
])
def test_query_prefix_is_model_specific(model, expected):
    """Guessing an instruction for an unknown model is worse than sending none."""
    assert query_prefix(model) == expected
