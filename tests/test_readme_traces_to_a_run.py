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
