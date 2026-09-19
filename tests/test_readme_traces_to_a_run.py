"""Every headline number in the README must come from a recorded run.

No metric in this repository is edited by hand, and a rule that lives only in a document
is one nobody can enforce. This asserts it: each value in the README and the results table
is parsed back out and compared against the frozen run it claims to come from.

It is the same standing rule this repository keeps rediscovering — anything that describes
the system must be pinned to the artifact the system actually produced, and the pinning
has to be checked somewhere a good intention cannot paper over it. A README is the most
likely place for a number to drift, because updating prose is easy and re-running an
evaluation is not.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
METHOD = ROOT / "docs" / "METHOD.md"
HISTORY = ROOT / "evals" / "history.jsonl"

# README label -> metric key in the run record.
TABLE = {
    "recall@16 (macro)": "recall_at_16",
    "recall@16 (micro)": "recall_at_16_micro",
    "single-span questions": "recall_at_16_single_span",
    "multi-span questions": "recall_at_16_multi_span",
    "MRR": "mrr",
    "citation validity": "citation_validity",
    "abstention recall": "abstention_recall",
    "over-refusal rate": "over_refusal_rate",
    "citation density": "citation_density",
    "route accuracy": "route_accuracy",
}


def _final_run() -> dict:
    rows = [json.loads(line) for line in HISTORY.read_text().splitlines() if line.strip()]
    finals = [r for r in rows if r.get("run_name", "").endswith("-final")]
    if not finals:
        pytest.skip("no run tagged -final in history")
    return finals[-1]


def _final_records() -> list[dict]:
    """The records of the run history says is final — resolved by name, never by glob.

    `sorted(glob("*-final.json"))[-1]` looks like "the newest final run" and is actually
    alphabetical order. Two files match that pattern here and the older one
    (`semantic-hybrid-rr-final.json`, the 75-question set) sorts last, so every check using
    that idiom was reading a run from two weeks before the one the README quotes.

    Same rule as everywhere else in this repo: identify the artifact, do not approximate it.
    """
    run = _final_run()
    path = ROOT / "evals" / "results" / f"{run['run_name']}.json"
    if not path.exists():
        pytest.skip(f"{path.name} not on disk")
    return json.loads(path.read_text())["records"]


def _results_table() -> str:
    """The full table now lives in docs/METHOD.md; the README carries a four-number
    summary for someone skimming. Both are checked — a figure a reader meets first is the
    one most worth pinning to the run that produced it."""
    text = METHOD.read_text()
    start = text.index("| Metric | Value")
    return text[start:text.index("\n\n", start)]


@pytest.mark.parametrize("label,key", TABLE.items())
def test_readme_value_matches_the_final_run(label, key):
    run = _final_run()
    if run.get(key) is None:
        pytest.skip(f"{key} not measured on the final run")

    row = next((r for r in _results_table().splitlines() if label in r), None)
    assert row is not None, f"README results table has no row for {label!r}"

    # First 4-decimal number in the row, ignoring any bracketed interval that follows.
    match = re.search(r"(\d\.\d{4})", row)
    assert match, f"no 4-decimal value in the {label!r} row: {row}"
    assert float(match.group(1)) == pytest.approx(run[key], abs=5e-5), (
        f"README says {match.group(1)} for {label}; "
        f"run {run['run_name']} recorded {run[key]:.4f}")


def test_the_readme_names_the_run_its_numbers_come_from():
    """A table of numbers with no run behind it cannot be checked by anyone."""
    run = _final_run()
    assert run["run_name"] in README.read_text(), (
        "the README must name the run its results table comes from")


ROUNDED = {
    "74%": ("recall_at_16", 0),
    "95%": ("abstention_recall", 0),
    "12%": ("over_refusal_rate", 0),
}


@pytest.mark.parametrize("shown,spec", ROUNDED.items())
def test_readme_summary_figures_round_from_the_run(shown, spec):
    """The README quotes rounded percentages so a skimmer can read them. Rounded is fine;
    invented is not, so each one is recomputed from the frozen run."""
    key, places = spec
    run = _final_run()
    if run.get(key) is None:
        pytest.skip(f"{key} not measured")
    expected = f"{round(run[key] * 100, places):.0f}%"
    assert expected == shown, f"README shows {shown} for {key}; run rounds to {expected}"
    assert shown in README.read_text(), f"README no longer shows {shown}"


def test_the_frozen_result_file_is_committed():
    """gitignore keeps evals/results/ out of git except for *-final.json, so the run
    behind the README survives a fresh clone."""
    run = _final_run()
    assert (ROOT / "evals" / "results" / f"{run['run_name']}.json").exists()


def test_the_baseline_tracks_the_final_run():
    """compare_to_baseline gates pull requests against evals/baseline.json. If that drifts
    from the run the README quotes, the gate is defending a number nobody published."""
    baseline = json.loads((ROOT / "evals" / "baseline.json").read_text())
    run = _final_run()
    assert baseline["run_name"] == run["run_name"]
    assert baseline["recall_at_16"] == pytest.approx(run["recall_at_16"], abs=1e-9)


# ------------------------------------------------------------------- the blog post

BLOG_CLAIMS = {
    "0.7377": "recall_at_16",
    "0.7000": "recall_at_16_micro",
    "0.9474": "abstention_recall",
    "0.1202": "over_refusal_rate",
}


@pytest.mark.parametrize("literal,key", BLOG_CLAIMS.items())
def test_blog_post_figures_come_from_the_frozen_run(literal, key):
    """The post is the most-read artifact and the least likely to be re-derived.

    It quotes measurements in prose, where nothing recomputes them, so the same rule the
    README is held to applies here: a number in the write-up must be one the frozen run
    actually produced.
    """
    post = ROOT / "blog" / "measuring-refusal.md"
    if not post.exists():
        pytest.skip("no blog post")
    run = _final_run()
    if run.get(key) is None:
        pytest.skip(f"{key} not measured")
    assert literal in post.read_text(), f"post no longer quotes {key}"
    assert float(literal) == pytest.approx(run[key], abs=5e-5), (
        f"post quotes {literal} for {key}; run {run['run_name']} recorded {run[key]:.4f}")


def test_blog_post_counts_match_the_records():
    """The post's central claim — that retrieval failures become confident answers 60% of
    the time — is a count over the run's records, not a logged metric. Recomputed here so
    prose cannot drift from the data it describes."""
    import json

    import sys
    sys.path.insert(0, str(ROOT / "evals"))
    import metrics as M

    post = ROOT / "blog" / "measuring-refusal.md"
    if not post.exists():
        pytest.skip("no blog post")
    text = post.read_text()

    records = _final_records()
    answerable = [r for r in records
                  if r["type"] not in ("unanswerable", "out_of_scope")]
    missed = [r for r in answerable
              if (M.recall_at_k(r["retrieved"], r["gold_spans"], 16) or 0) == 0]
    abstained = [r for r in missed if r["abstained"]]
    answered = len(missed) - len(abstained)

    assert f"{len(answerable)} answerable" in text
    assert f"**{len(missed)} where retrieval did not surface the evidence" in text
    assert f"abstained on {len(abstained)}" in text
    assert f"**{answered} anyway**" in text
    assert f"**{round(100 * answered / len(missed))}% of the time**" in text


# ------------------------------------------------------------ counts quoted in prose

def _negatives_and_misses():
    import sys
    sys.path.insert(0, str(ROOT / "evals"))
    import metrics as M

    records = _final_records()
    negatives = [r for r in records if r["type"] in M.NEGATIVE_TYPES]
    answerable = [r for r in records if r["type"] not in M.NEGATIVE_TYPES]
    missed = [r for r in answerable
              if (M.recall_at_k(r["retrieved"], r["gold_spans"], 16) or 0) == 0]
    return M, records, negatives, answerable, missed


def test_readme_counts_match_the_records():
    """The README gives counts beside its percentages, because 95% of 19 and 95% of 190
    are different claims. Each count is recomputed here."""
    M, records, negatives, answerable, missed = _negatives_and_misses()
    text = README.read_text()
    declined = sum(M.refused(r) for r in negatives)
    wrongly = sum(M.refused(r) for r in answerable)
    abstained = sum(M.refused(r) for r in missed)

    assert f"Measured on {len(records)} questions" in text
    assert f"({declined} of {len(negatives)})" in text
    assert f"({wrongly} of {len(answerable)})" in text
    assert f"refuses {declined} of the {len(negatives)} questions" in text
    assert f"On {len(missed)} answerable questions" in text
    assert f"It declined {abstained} of those and answered {len(missed) - abstained}" in text


def test_readme_cost_and_latency_round_from_the_run():
    run = _final_run()
    text = README.read_text()
    assert f"**${run['cost_usd_per_query']:.2f}**" in text
    assert f"median question took {round(run['p50_latency_ms'] / 1000)} seconds" in text


def test_readme_uncited_sentence_share_matches_the_records():
    """"Not every sentence is cited" is quantified in the README, so the figure is
    recomputed with the metric's own sentence splitter and citation pattern."""
    M, records, *_ = _negatives_and_misses()
    sentences = [s for r in records if not M.refused(r)
                 for s in M._claim_sentences(r["answer"])]
    uncited = sum(1 for s in sentences if not M._CITATION.search(s))
    assert f"{round(100 * uncited / len(sentences))}% of the sentences" in README.read_text()


REVIEW = ROOT / "evals" / "zero_recall_review.jsonl"
VERDICTS = {
    "correct_uncredited": "right, from a passage the scorer doesn't credit",
    "declined_in_prose": "a refusal, stated partway through the answer",
    "partial": "grounded in real passages, but answering a nearby question",
    "wrong": "wrong",
}


def test_the_hand_review_covers_exactly_the_answered_misses():
    """The 18 answered-with-zero-recall cases were classified by reading them. The
    classification is a human judgement, but which questions it covers is not: if the run
    or the labels change, the review must be redone rather than left describing a
    different set."""
    M, _, _, _, missed = _negatives_and_misses()
    answered = {r["id"] for r in missed if not M.refused(r)}
    review = [json.loads(line) for line in REVIEW.read_text().splitlines() if line.strip()]
    assert {row["id"] for row in review} == answered
    assert {row["verdict"] for row in review} <= set(VERDICTS)


@pytest.mark.parametrize("doc", [README, ROOT / "blog" / "measuring-refusal.md"],
                         ids=["readme", "blog"])
def test_review_breakdown_matches_the_review_file(doc):
    from collections import Counter

    review = [json.loads(line) for line in REVIEW.read_text().splitlines() if line.strip()]
    counts = Counter(row["verdict"] for row in review)
    text = doc.read_text()
    for verdict, label in VERDICTS.items():
        assert re.search(rf"\| {re.escape(label)} \| {counts[verdict]} \|", text), (
            f"{doc.name}: row {label!r} should show {counts[verdict]}")
