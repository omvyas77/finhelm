"""Tests for the disparity screen.

The statistics here are easy to get subtly wrong in ways that produce plausible tables, so
the assertions target the three choices that decide whether a flag means anything: the
baseline excluding the cell under test, the minimum cell size applying before the
correction, and Benjamini-Hochberg running over the whole family at once.
"""

from __future__ import annotations

import pandas as pd
import pytest

from analytics.complaint_disparity import (DEFAULT_SOURCE, MIN_CELL, adjust,
                                           load, screen)


def _frame(rows):
    df = pd.DataFrame(rows)
    df["relief"] = df["company_response"].isin(
        {"Closed with monetary relief", "Closed with non-monetary relief"})
    df["on_time"] = df["timely"].eq("Yes")
    return df


def _cell(company, product, n, relief_n):
    return [{"company": company, "product": product,
             "company_response": ("Closed with monetary relief" if i < relief_n
                                  else "Closed with explanation"),
             "timely": "Yes"} for i in range(n)]


def test_the_baseline_excludes_the_cell_under_test():
    """A large issuer compared against a baseline containing its own complaints is being
    tested against a number it helped produce, which biases every such test toward finding
    nothing. With one dominant company the effect is stark: including it, the company *is*
    the baseline and the difference collapses to zero."""
    rows = _cell("BIG", "Card", 400, 40) + _cell("SMALL", "Card", 100, 50)
    out = screen(_frame(rows), "relief", min_cell=50)

    big = out[out.company == "BIG"].iloc[0]
    assert big["rate"] == pytest.approx(0.10)
    # Baseline is SMALL only, not SMALL+BIG.
    assert big["baseline_rate"] == pytest.approx(0.50)
    assert big["baseline_n"] == 100
    assert big["difference"] == pytest.approx(-0.40)


def test_cells_below_the_minimum_are_never_tested():
    """Filtering after computing p-values would still let tiny cells inflate the
    multiple-testing correction and suppress real signal elsewhere."""
    rows = _cell("TINY", "Card", 10, 9) + _cell("BIG", "Card", 300, 60)
    out = screen(_frame(rows), "relief", min_cell=50)
    assert "TINY" not in set(out.company)


def test_a_cell_whose_baseline_is_too_small_is_skipped():
    rows = _cell("A", "Card", 100, 50) + _cell("B", "Card", 10, 5)
    out = screen(_frame(rows), "relief", min_cell=50)
    # A's baseline is B, which is under the minimum, so A cannot be tested either.
    assert out.empty


def test_benjamini_hochberg_drops_a_raw_hit_when_the_family_is_mostly_null():
    """Benjamini-Hochberg is a *step-up* procedure, and getting that wrong is why this
    test was written incorrectly twice.

    It finds the largest rank i where p_(i) <= (i/m) * alpha and rejects every hypothesis
    at or below it. Two consequences that intuition gets backwards:

      * If every p-value in the family is below alpha, BH removes **nothing** — the
        largest p already satisfies the rank-m threshold of (m/m) * alpha = alpha. A
        family of twenty p-values all at 0.04 is rejected in full.
      * BH only drops a raw hit when large p-values elsewhere in the family pull the
        cutoff down. The correction's work is done by the nulls, not by the hits.

    That is exactly what the real screen shows: on the timely-response outcome, 19 raw
    hits become 6 after correction, because most of the 80 cells are null.
    """
    p_values = [0.001, 0.04] + [0.5] * 18
    out = adjust(pd.DataFrame({"p_value": p_values}), alpha=0.05)

    assert (out["q_value"] >= out["p_value"]).all(), "q must never fall below p"
    raw = sum(p < 0.05 for p in p_values)
    assert raw == 2
    assert out["flagged"].sum() == 1, "the 0.04 must not survive a mostly-null family"

    # And the property that makes the above true, asserted directly.
    all_significant = adjust(pd.DataFrame({"p_value": [0.04] * 20}), alpha=0.05)
    assert all_significant["flagged"].all(), \
        "step-up: when every p is below alpha, BH rejects the whole family"


def test_an_obvious_disparity_is_flagged_and_matching_cells_are_not():
    """The outlier is kept small relative to the pool on purpose.

    The first version used one outlier against two normal companies, and every company got
    flagged — because with a three-company pool the outlier is a third of everyone else's
    baseline and drags it far from the normal rate. That is not a bug; it is the module's
    headline limitation reproducing in miniature, and it is why the peer group matters more
    than the statistics. Here the outlier is 60 complaints against 2,000, so it moves the
    baseline by about two points and the normal companies stay quiet.
    """
    rows = _cell("OUTLIER", "Card", 60, 54)
    for i in range(10):
        rows += _cell(f"NORMAL{i}", "Card", 200, 40)

    out = adjust(screen(_frame(rows), "relief", min_cell=50))
    flagged = set(out.loc[out.flagged, "company"])
    assert "OUTLIER" in flagged
    assert not {c for c in flagged if c.startswith("NORMAL")}


# data/raw/ is gitignored — the CFPB extract is a rebuildable artifact, like the FAISS
# index — so a runner has no copy of it. The first version of these three tests called
# load() directly and passed locally while failing in CI on a missing file: the same
# "the local filesystem papers over it" failure as the PR gate that scored the wrong
# config for the life of its file.
#
# The fix is not to skip all three. Two of them assert things about load()'s *behaviour*
# and can be checked against a synthetic extract anywhere; only the third asserts
# something about the real data and genuinely needs it.

def _write_extract(path, rows):
    pd.DataFrame(rows).to_parquet(path, index=False)
    return path


def test_in_progress_complaints_leave_the_denominator(tmp_path):
    """Counting an unresolved complaint as "no relief" would understate relief for
    whichever company happens to have open cases on the extract date."""
    src = _write_extract(tmp_path / "x.parquet", [
        {"company": "A", "product": "Card", "company_response": "In progress",
         "timely": "Yes", "zip_code": "12345"},
        {"company": "A", "product": "Card", "company_response": "Closed with explanation",
         "timely": "Yes", "zip_code": "12345"},
    ])
    df = load(src)
    assert "In progress" not in set(df["company_response"])
    assert len(df) == 1


def test_load_derives_the_outcome_columns_and_a_three_digit_zip(tmp_path):
    src = _write_extract(tmp_path / "x.parquet", [
        {"company": "A", "product": "Card",
         "company_response": "Closed with monetary relief", "timely": "Yes",
         "zip_code": "12345"},
        {"company": "B", "product": "Card", "company_response": "Closed with explanation",
         "timely": "No", "zip_code": "12345"},
    ])
    df = load(src)
    assert {"relief", "on_time", "zip3"} <= set(df.columns)
    assert list(df["relief"]) == [True, False]
    assert list(df["on_time"]) == [True, False]
    assert df["zip3"].dropna().str.len().eq(3).all()


@pytest.mark.skipif(not DEFAULT_SOURCE.exists(),
                    reason=f"CFPB extract not present ({DEFAULT_SOURCE.name} is gitignored)")
def test_the_dispute_rate_is_absent_from_the_source_rather_than_dropped():
    """The build guide asks for a consumer-dispute rate. CFPB stopped publishing the field
    in April 2017; this asserts the reason it is missing is the data, not an oversight.

    Unlike the two above, this is a claim about the real extract, so it can only be
    checked where the real extract is.
    """
    assert "consumer_disputed" not in load().columns
