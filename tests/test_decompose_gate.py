"""Tests for the deterministic pre-check in front of decomposition.

Ungated, decompose split 53 of 54 golden-set questions and paid a model call, ~2.9x
retrieval latency and ~7x cost on every one of them to help the roughly one in five that
genuinely spans two documents.
"""

from __future__ import annotations

import pytest

from finhelm.agent.decompose import _issuers, _periods, worth_splitting


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
    guessing: a missed split costs the an earlier stage behaviour, an unnecessary one costs latency
    on every question forever."""
    assert not worth_splitting("Tell me about the thing in the document")


# --------------------------------------------------------------- the timeout

def test_decompose_passes_its_timeout_to_the_model_call(monkeypatch):
    """Config.agent_timeout_s existed from an earlier stage, was logged into every history row, and
    was read by nothing. The planner therefore ran under the SDK's ten-minute default,
    which on the request path means a hung call holds an /ask open for ten minutes before
    failing open."""
    import finhelm.agent.decompose as D

    seen = {}

    def fake_claude(prompt, model, system=None, max_tokens=1024, temperature=0.0,
                    timeout=None):
        seen["timeout"] = timeout
        return '{"sub_questions": ["a about X", "b about Y"]}'

    monkeypatch.setattr(D, "claude", fake_claude)
    D.decompose("How do JPM and COF differ on commercial real estate risk?",
                timeout_s=7.5)
    assert seen["timeout"] == 7.5


def test_a_planner_timeout_falls_back_to_the_original_question(monkeypatch):
    """Failing open is the whole contract: a planner outage must cost ranking, never the
    answer. Every exception path returns something retrievable."""
    import finhelm.agent.decompose as D

    def raises(*a, **k):
        raise TimeoutError("planner took too long")

    monkeypatch.setattr(D, "claude", raises)
    question = "How do JPM and COF differ on commercial real estate risk?"
    assert D.decompose(question, timeout_s=1) == [question]


def test_retrieve_hands_the_configured_timeout_to_the_planner(monkeypatch):
    """The knob is only real if the caller actually threads it."""
    import finhelm.retrieve as R

    seen = {}

    def fake_decompose(question, max_sub_questions=4, timeout_s=None):
        seen["timeout_s"] = timeout_s
        seen["cap"] = max_sub_questions
        return [question]

    monkeypatch.setattr(R, "decompose", fake_decompose)
    monkeypatch.setattr(R, "route", lambda q, use_llm=True: R.Route(["filings"], "test", ""))
    monkeypatch.setattr(R, "_from_collection", lambda *a, **k: [])

    from finhelm.config import Config
    R.retrieve("anything at all", Config(agentic=True, agent_timeout_s=12,
                                         max_sub_questions=3))
    assert seen == {"timeout_s": 12, "cap": 3}


# ------------------------------------------------- isolating filters from queries

def test_union_filters_turns_a_disagreement_into_membership():
    """Two sub-questions naming different issuers imply "either", never "both".
    Intersecting them would empty the pool for exactly the comparison questions
    decomposition exists to serve."""
    from finhelm.retrieve import _union_filters

    assert _union_filters([{"ticker": "COF"}, {"ticker": "SYF"}]) == {
        "ticker": ["COF", "SYF"]}


def test_union_filters_keeps_a_scalar_when_they_agree():
    from finhelm.retrieve import _union_filters

    assert _union_filters([{"ticker": "COF"}, {"ticker": "COF"}]) == {"ticker": "COF"}


def test_union_filters_drops_prefix_filters():
    """{"prefix": ("2024","2025")} and {"prefix": ("2025","2026")} have no union that is
    both correct and expressible, and a wrong date filter costs recall nothing recovers."""
    from finhelm.retrieve import _union_filters

    assert _union_filters([{"ticker": "COF", "date": {"prefix": "2024"}}]) == {
        "ticker": "COF"}


def test_filters_only_arm_retrieves_one_query_but_keeps_the_split_filters(monkeypatch):
    """The whole point of the arm: better filters, single query."""
    import finhelm.retrieve as R
    from finhelm.config import Config

    monkeypatch.setattr(R, "decompose", lambda q, cap=4, timeout_s=None: [
        "What did COF report?", "What did SYF report?"])
    monkeypatch.setattr(R, "filters_for",
                        lambda q: {"ticker": "COF"} if "COF" in q else {"ticker": "SYF"})
    monkeypatch.setattr(R, "route", lambda q, use_llm=True: R.Route(["filings"], "t", ""))

    seen = []

    def fake_from_collection(q, c, cfg, merged):
        seen.append((q, merged))
        return []

    monkeypatch.setattr(R, "_from_collection", fake_from_collection)
    R.retrieve("compare COF and SYF", Config(agentic_filters_only=True,
                                             filter_by_issuer=True))
    assert len(seen) == 1, "retrieval must run on the single original query"
    assert seen[0][0] == "compare COF and SYF"
    assert seen[0][1] == {"ticker": ["COF", "SYF"]}
