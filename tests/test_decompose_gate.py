"""Tests for the deterministic pre-check in front of decomposition.

Ungated, decompose split 53 of 54 golden-set questions and paid a model call, ~2.9x
retrieval latency and ~7x cost on every one of them to help the roughly one in five that
genuinely spans two documents.
"""

from __future__ import annotations

import pytest

from src.finhelm.agent.decompose import _issuers, _periods, worth_splitting


@pytest.mark.parametrize("question", [
    "Compare JPMorgan's CET1 ratio with Citigroup's",
    "Comparing Bank of America's charge-off ratios with its ALM disclosures",
    "Contrasting Goldman Sachs and Discover on liquidity risk",
    "How do AXP and BAC regulatory risk disclosures differ?",
    "What was Wells Fargo's net interest income in 2023 versus 2024?",
])
def test_fires_on_genuinely_multi_part_questions(question):
    assert worth_splitting(question)


@pytest.mark.parametrize("question", [
    "What did JPMorgan report as its CET1 ratio in 2024?",
    "Describe the principal risk factors Capital One discloses.",
    "What is the FOMC's stated inflation target?",
])
def test_skips_single_fact_questions(question):
    assert not worth_splitting(question)


def test_comparative_inflections_all_count():
    """The original router pattern matched compare/compared but not comparing, comparison
    or contrasting — the participle fails the trailing word boundary, and the noun form
    was absent. All three are common phrasings for a two-sided question."""
    for verb in ("compare", "compares", "compared", "comparing", "comparison",
                 "comparisons", "contrast", "contrasting", "contrasts"):
        assert worth_splitting(f"A {verb} of two banks"), verb


def test_issuers_matches_ticker_and_company_name():
    assert _issuers("JPM versus Citigroup") == {"JPM", "C"}
    assert _issuers("How did JPMorgan Chase perform?") == {"JPM"}


def test_single_letter_ticker_does_not_match_inside_words():
    """C is Citigroup. A substring test would find it in 'credit', 'capital' and most
    other words in a finance question, and route every one of them as multi-issuer."""
    assert _issuers("What drove the credit card charge-off increase?") == set()
    assert _issuers("Consumer capital requirements and compliance costs") == set()


def test_short_name_fragments_are_not_used_as_issuer_evidence():
    """'Bank of America' contributes only 'america' — 'bank' and 'of' would match almost
    any filings question and manufacture a second issuer out of nothing."""
    assert _issuers("What are the bank's capital requirements?") == set()


def test_periods_counts_distinct_years_and_quarters():
    assert _periods("net income in 2023 and 2024") == 2
    assert _periods("net income in 2024") == 1
    # The same year repeated is one period, not two.
    assert _periods("2024 revenue against 2024 guidance") == 1
    assert _periods("Q1 and Q3 of 2024") == 3


def test_gate_fails_toward_not_splitting():
    """An unrecognised phrasing must fall through to the un-decomposed query rather than
    guessing: a missed split costs the Day 2 behaviour, an unnecessary one costs latency
    on every question forever."""
    assert not worth_splitting("Tell me about the thing in the document")
