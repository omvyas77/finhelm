"""Every headline number in the README must come from a recorded run.

The build guide's rule for the final evaluation is "no hand-edited metrics anywhere,
ever", and a rule that lives only in a document is one nobody can enforce. This asserts
it: each value in the README's results table is parsed back out and compared against the
frozen run it claims to come from.

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
    alphabetical order. Two files match that pattern here and the Day 2 one
    (`semantic-hybrid-rr-final.json`, 75 questions) sorts last, so every check using that
    idiom was reading a run from two weeks before the one the README quotes. The
    hallucination guard passed against it for the wrong reason, since q055 fabricates in
    both.

    Same rule as everywhere else in this repo: identify the artifact, do not approximate it.
    """
    run = _final_run()
    path = ROOT / "evals" / "results" / f"{run['run_name']}.json"
    if not path.exists():
        pytest.skip(f"{path.name} not on disk")
    return json.loads(path.read_text())["records"]


def _results_table() -> str:
    text = README.read_text()
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
    "0.7403": "recall_at_16",
    "0.7016": "recall_at_16_micro",
    "0.9048": "abstention_recall",
    "0.1160": "over_refusal_rate",
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
