"""Tests for the uncertainty and stratification helpers.

These exist because Day 2 reported an 18-cell ranking whose gaps were smaller than its
own confidence interval, and because the first attempt to measure decomposition read a
field that was never recorded and concluded the feature had not run.
"""

from __future__ import annotations

import pytest

from evals.metrics import bootstrap_paired, recall_by_span_count, wilson


def test_wilson_stays_inside_the_unit_interval_at_the_extremes():
    """The normal approximation gives negative lower bounds at p=0; Wilson must not."""
    low, high = wilson(0, 10)
    assert low == 0.0 and 0.0 < high < 1.0
    low, high = wilson(10, 10)
    assert 0.0 < low < 1.0 and high == 1.0


def test_wilson_narrows_as_n_grows():
    """The whole argument for a larger golden set is that this width shrinks."""
    widths = [wilson(int(0.42 * n), n)[1] - wilson(int(0.42 * n), n)[0]
              for n in (74, 150, 300, 600)]
    assert widths == sorted(widths, reverse=True)
    assert widths[0] > 0.2, "n=74 should be visibly too wide to rank configs on"


def test_bootstrap_paired_rejects_unaligned_inputs():
    """Silently zipping mismatched runs would compare question i against question j."""
    with pytest.raises(ValueError):
        bootstrap_paired([0.1, 0.2], [0.1])


def test_bootstrap_paired_finds_a_real_difference_and_ignores_a_null_one():
    real = bootstrap_paired([0.0] * 30, [1.0] * 30, rounds=2000)
    assert real["low"] > 0 and real["p_better"] > 0.95

    null = bootstrap_paired([0.5] * 30, [0.5] * 30, rounds=2000)
    assert null["low"] == 0.0 and null["high"] == 0.0


def test_bootstrap_paired_drops_unscorable_pairs_rather_than_crashing():
    """Negatives have no gold spans, so recall_at_k returns None for them."""
    out = bootstrap_paired([None, 0.0, 0.0], [None, 1.0, 1.0], rounds=500)
    assert out["n"] == 2 and out["delta"] == pytest.approx(1.0)


def _record(spans: int, hits: int) -> dict:
    """A record needing `spans` gold spans of which `hits` are retrieved."""
    gold = [{"doc_id": f"D{i}", "snippet": f"alpha bravo charlie delta echo foxtrot golf "
                                          f"hotel india juliet {i}"} for i in range(spans)]
    retrieved = [{"doc_id": g["doc_id"], "text": g["snippet"]} for g in gold[:hits]]
    # `answer` is required by aggregate() — the citation metrics read it — and run_eval
    # always sets it, so an empty string is the faithful stand-in for a retrieve-only run.
    return {"gold_spans": gold, "retrieved": retrieved, "answer": ""}


def test_recall_by_span_count_separates_the_two_tiers():
    """The pooled number hid a 0.559 vs 0.175 split; this is what surfaces it."""
    records = [_record(1, 1), _record(1, 0), _record(2, 1), _record(2, 0)]
    out = recall_by_span_count(records)
    assert out["recall_at_5_single_span"] == pytest.approx(0.5)
    assert out["n_single_span"] == 2.0
    assert out["recall_at_5_multi_span"] == pytest.approx(0.25)
    assert out["n_multi_span"] == 2.0


def test_recall_by_span_count_buckets_three_spans_with_the_multi_tier():
    """Questions needing 3+ spans are rare; pooling them with 2 keeps the tier readable
    rather than producing a tier of one."""
    out = recall_by_span_count([_record(3, 3), _record(2, 1)])
    assert out["n_multi_span"] == 2.0
    assert "recall_at_5_3_span" not in out


def test_recall_by_span_count_ignores_questions_with_no_gold_spans():
    """Negatives must not be scored as zero-recall retrieval failures."""
    out = recall_by_span_count([{"gold_spans": [], "retrieved": []}, _record(1, 1)])
    assert out["n_single_span"] == 1.0 and "n_multi_span" not in out


def test_bootstrap_ci_brackets_the_mean_it_describes():
    """The regression this guards: the macro recall was reported next to a Wilson interval
    computed on gold spans, and on the expanded golden set the point estimate (0.406) fell
    outside its own interval (0.287-0.404). A number outside its own confidence interval
    is worse than no interval, because it still looks authoritative."""
    from evals.metrics import bootstrap_ci

    values = [1.0] * 40 + [0.5] * 20 + [0.0] * 40
    mean = sum(values) / len(values)
    low, high = bootstrap_ci(values, rounds=2000)
    assert low <= mean <= high


def test_aggregate_reports_macro_and_micro_separately():
    """They answer different questions and diverge whenever span counts are unequal:
    macro is 'how does the system do on an average question', micro is 'what fraction of
    all the evidence is retrieved'."""
    from evals.metrics import aggregate

    records = [_record(1, 1), _record(2, 0)]
    out = aggregate(records)
    # macro: (1.0 + 0.0) / 2 = 0.5.  micro: 1 of 3 spans found = 0.333.
    assert out["recall_at_5"] == pytest.approx(0.5)
    assert out["recall_at_5_micro"] == pytest.approx(1 / 3)
    assert out["recall_at_5_ci_low"] <= out["recall_at_5"] <= out["recall_at_5_ci_high"]


def test_an_interval_touching_zero_is_not_resolved():
    """The boundary case that produced a false positive: comparing the query prefix on
    bge-base gave [-0.0166, +0.0000] from a difference vector that was zero for 180 of 181
    questions. A caller testing `low < 0 < high` reads an upper bound of exactly zero as
    excluding zero, and reports a resolved effect where there is none."""
    from evals.metrics import bootstrap_paired

    a = [0.0] * 180 + [1.0]
    b = [0.0] * 181                      # one question worse, all others identical
    out = bootstrap_paired(a, b, rounds=2000)
    assert out["high"] == pytest.approx(0.0, abs=1e-9)
    assert out["resolved"] is False


def test_a_genuine_effect_still_resolves():
    """The guard above must not make the test unable to detect anything."""
    from evals.metrics import bootstrap_paired

    out = bootstrap_paired([0.0] * 90 + [1.0] * 91, [1.0] * 181, rounds=2000)
    assert out["resolved"] is True
    assert out["low"] > 0


def test_passage_windows_cover_the_whole_passage():
    """The regression this guards: a cross-encoder truncates a long pair from the end, so a
    gold span past the 512-token budget is invisible to the reranker no matter how well it
    scores. Measured on this corpus, that hid 24% of pooled multi-span gold spans."""
    from finhelm.retrieve.rerank import _passage_windows

    text = " ".join(f"word{i}" for i in range(2000))
    windows = _passage_windows(text, "BAAI/bge-reranker-base", budget=200)
    assert len(windows) > 1, "a passage far over budget must be split"
    # The tail has to appear somewhere, which is the entire point.
    assert "word1999" in windows[-1]
    # And a short passage must pass through untouched rather than being re-tokenised.
    short = "a short passage"
    assert _passage_windows(short, "BAAI/bge-reranker-base", budget=200) == [short]
