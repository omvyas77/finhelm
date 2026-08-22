"""Tests for the deterministic metrics.

These are pure functions with no I/O, so there is no excuse for not testing them, and a
silently wrong metric is worse than a missing one: every ablation decision downstream
inherits the error and the table still looks reasonable.
"""

import pytest

from evals import metrics as m

GOLD_TEXT = (
    "Net charge-offs in the domestic card portfolio increased to 5.9 percent "
    "for the fourth quarter of 2025, reflecting continued credit normalization."
)
DOC = "COF_10K_2026-02-20_009991"


def chunk(text, doc_id=DOC):
    return {"doc_id": doc_id, "text": text}


def gold(snippet=GOLD_TEXT, doc_id=DOC):
    return {"doc_id": doc_id, "snippet": snippet}


# ---------------------------------------------------------------- overlap / is_hit


def test_hit_regardless_of_chunk_length():
    """A `fixed` chunk is ~30x longer than a `sentence_window` one, so both a chunk that
    swallows the gold span whole and a chunk that captures only part of it must count as
    hits. `overlap` is asymmetric — it reports how much of the gold span the chunk holds,
    which differs between the two — but both clear is_hit."""
    long_chunk = "Preamble text. " * 60 + GOLD_TEXT + " Trailing discussion. " * 40
    short_chunk = "increased to 5.9 percent for the fourth quarter of 2025"

    assert m.overlap(long_chunk, GOLD_TEXT) == pytest.approx(1.0)
    assert 0.0 < m.overlap(short_chunk, GOLD_TEXT) < 1.0
    assert m.is_hit(chunk(long_chunk), gold())
    assert m.is_hit(chunk(short_chunk), gold())


def test_formulaic_boilerplate_is_not_a_hit():
    """The bug that bag-of-words overlap shipped with. MD&A prose is templated, so a
    sentence about a different line item in the same filing shared enough vocabulary to
    clear a 0.6 overlap-coefficient threshold. It must not clear an n-gram match."""
    boilerplate = "Discount revenue increased 7 percent, primarily driven by an increase in billed business."
    gold_span = gold("Net card fees increased 21 percent, primarily driven by growth in "
                     "our premium card portfolios.")
    assert not m.is_hit(chunk(boilerplate), gold_span)


def test_same_sentence_different_period_is_not_a_hit():
    """The most dangerous false positive: identical phrasing, different number. This is a
    different fact, not a near miss, and scoring it as a hit would mean the retrieval
    metric cannot tell 2024 from 2025."""
    other_quarter = ("Net card fees increased 18 percent, primarily driven by growth in "
                     "our premium card portfolios.")
    gold_span = gold("Net card fees increased 21 percent, primarily driven by growth in "
                     "our premium card portfolios.")
    assert not m.is_hit(chunk(other_quarter), gold_span)


def test_same_text_in_a_different_document_is_not_a_hit():
    """~2% of filing chunks are byte-identical boilerplate repeated across filers, so
    text overlap alone would manufacture hits from the wrong company."""
    assert not m.is_hit(chunk(GOLD_TEXT, doc_id="JPM_10K_2026-02-13_008131"), gold())


def test_unrelated_text_in_the_right_document_is_not_a_hit():
    assert not m.is_hit(chunk("The Board declared a quarterly dividend of $0.60."), gold())


def test_overlap_handles_empty_input():
    assert m.overlap("", GOLD_TEXT) == 0.0
    assert m.overlap(GOLD_TEXT, "") == 0.0


# ---------------------------------------------------------------- recall / mrr


def test_recall_counts_fraction_of_spans_found():
    """Multi-hop: retrieving one side of a comparison is half credit, not full."""
    spans = [gold(), gold("Synchrony reported a 6.4 percent net charge-off rate.",
                          doc_id="SYF_10K_2026-02-07_007712")]
    retrieved = [chunk("noise"), chunk(GOLD_TEXT)]

    assert m.recall_at_k(retrieved, spans, k=5) == pytest.approx(0.5)
    assert m.recall_at_k(retrieved, [gold()], k=5) == pytest.approx(1.0)


def test_recall_respects_k():
    retrieved = [chunk("noise")] * 4 + [chunk(GOLD_TEXT)]
    assert m.recall_at_k(retrieved, [gold()], k=3) == 0.0
    assert m.recall_at_k(retrieved, [gold()], k=5) == pytest.approx(1.0)


def test_mrr_uses_rank_of_first_hit():
    retrieved = [chunk("noise"), chunk("noise"), chunk(GOLD_TEXT)]
    assert m.mrr(retrieved, [gold()]) == pytest.approx(1 / 3)
    assert m.mrr([chunk(GOLD_TEXT)], [gold()]) == pytest.approx(1.0)
    assert m.mrr([chunk("noise")], [gold()]) == 0.0


def test_retrieval_metrics_are_none_without_gold_spans():
    """Negatives have no passage to retrieve. None means 'not applicable' and is dropped
    from the aggregate mean — scoring them 0.0 would punish correct abstention."""
    assert m.recall_at_k([chunk("x")], [], k=5) is None
    assert m.mrr([chunk("x")], []) is None


# ---------------------------------------------------------------- citations


def test_citation_validity_flags_invented_sources():
    assert m.citation_validity("Charge-offs rose [S1][S2].", n_sources=3) == pytest.approx(1.0)
    assert m.citation_validity("Charge-offs rose [S1][S9].", n_sources=3) == pytest.approx(0.5)
    assert m.citation_validity("No citations here.", n_sources=3) is None


def test_markdown_headings_do_not_count_as_uncited_claims():
    """The Day 1 metric counted every heading as an unsourced assertion, which made
    well-structured answers look worse than unstructured ones."""
    answer = (
        "## How Banks Frame Late Fees\n"
        "Capital One describes the fee cap as a material revenue headwind [S1].\n"
        "**Consumer complaints**\n"
        "Consumers report unexpected fee increases after the cap took effect [S2].\n"
    )
    assert m.uncited_claims(answer) == 0
    assert m.citation_density(answer) == pytest.approx(1.0)


def test_uncited_claims_still_catches_real_unsourced_sentences():
    answer = (
        "## Summary\n"
        "Charge-offs increased materially during the fourth quarter [S1].\n"
        "This trend will almost certainly continue into the next fiscal year.\n"
    )
    assert m.uncited_claims(answer) == 1


# ---------------------------------------------------------------- abstention / routing


def test_abstention_reports_both_directions():
    records = [
        {"type": "unanswerable", "answer": "INSUFFICIENT_CONTEXT: not in the filings."},
        {"type": "out_of_scope", "answer": "Tesla guided to 2 million units."},
        {"type": "single_hop", "answer": "Charge-offs rose [S1]."},
        {"type": "single_hop", "answer": "INSUFFICIENT_CONTEXT: missing."},
    ]
    result = m.abstention(records)
    assert result["abstention_recall"] == pytest.approx(0.5)
    assert result["over_refusal_rate"] == pytest.approx(0.5)


def test_refuse_everything_scores_perfectly_on_one_and_terribly_on_the_other():
    """The guard against reporting abstention_recall alone."""
    records = [{"type": t, "answer": "INSUFFICIENT_CONTEXT: no."}
               for t in ("unanswerable", "single_hop", "multi_hop")]
    result = m.abstention(records)
    assert result["abstention_recall"] == pytest.approx(1.0)
    assert result["over_refusal_rate"] == pytest.approx(1.0)


def test_route_accuracy_gives_no_partial_credit_for_half_a_comparison():
    """The Day 1 failure: routed a comparative question to complaints only, then wrote a
    section on how *banks* frame the issue citing only consumer narratives."""
    records = [
        {"route": ["complaints"], "expected_source": ["filings", "complaints"]},
        {"route": ["filings", "complaints"], "expected_source": ["filings", "complaints"]},
    ]
    assert m.route_accuracy(records) == pytest.approx(0.5)


def test_route_accuracy_ignores_order():
    records = [{"route": ["complaints", "filings"], "expected_source": ["filings", "complaints"]}]
    assert m.route_accuracy(records) == pytest.approx(1.0)


def test_route_accuracy_is_none_when_nothing_is_labelled():
    assert m.route_accuracy([{"route": ["filings"]}]) is None


# ---------------------------------------------------------------- aggregate


def test_aggregate_excludes_negatives_from_retrieval_metrics():
    records = [
        {"type": "single_hop", "answer": "Rose [S1].", "retrieved": [chunk(GOLD_TEXT)],
         "gold_spans": [gold()], "route": ["filings"], "expected_source": ["filings"]},
        {"type": "unanswerable", "answer": "INSUFFICIENT_CONTEXT: absent.",
         "retrieved": [chunk("noise")], "gold_spans": [],
         "route": ["filings"], "expected_source": ["filings"]},
    ]
    out = m.aggregate(records, k=5)
    # Only the answerable question contributes; a 0.0 from the negative would halve this.
    assert out["recall_at_5"] == pytest.approx(1.0)
    assert out["mrr"] == pytest.approx(1.0)
    assert out["abstention_recall"] == pytest.approx(1.0)
    assert out["over_refusal_rate"] == pytest.approx(0.0)
    assert out["route_accuracy"] == pytest.approx(1.0)
