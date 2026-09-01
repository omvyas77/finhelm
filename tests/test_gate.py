"""Tests for the CI gate itself.

A gate is the one piece of a project that nothing else checks. Every other test fails
loudly when the code under it breaks; a broken gate fails by passing, silently, forever,
while the workflow file continues to read exactly like a gate. This project has already
shipped one of those — the spec's `--fail-under recall_at_5=0.75` names a metric this
system does not produce, and a lenient implementation would have gone green on every push
for the life of the repo.

So the two things worth testing hardest here are not the happy paths: an unknown metric
name must fail, and a regression in a metric where *lower is better* must be recognised
as a regression rather than as an improvement.
"""

from __future__ import annotations

import json

import pytest

from evals.compare_to_baseline import compare
from evals.run_eval import enforce

SUMMARY = {
    "recall_at_16": 0.7403,
    "recall_at_16_micro": 0.7016,
    "citation_validity": 1.0,
    "over_refusal_rate": 0.1160,
    "mrr": 0.4628,
}


# --------------------------------------------------------------- --fail-under

def test_a_metric_the_run_never_produced_is_a_failure():
    """The single most important assertion in this file. `recall_at_5` is what the build
    guide's workflow gates on and this system serves top-k=16, so the key is absent from
    every summary it writes."""
    failures = enforce(SUMMARY, ["recall_at_5=0.75"])
    assert len(failures) == 1
    assert "never fail" in failures[0]
    # And it says what *is* available, because the next question is always "then what?"
    assert "recall_at_16" in failures[0]


def test_a_metric_below_its_floor_fails():
    failures = enforce(SUMMARY, ["recall_at_16=0.80"])
    assert failures and "0.7403" in failures[0] and "0.8000" in failures[0]


def test_a_metric_above_its_floor_passes():
    assert enforce(SUMMARY, ["recall_at_16=0.70", "citation_validity=0.95"]) == []


def test_an_unmeasured_metric_fails_rather_than_passing():
    """citation_validity is None on a retrieve-only run. Treating None as "no evidence of
    failure" would let the cheap tier claim it had checked generation quality."""
    failures = enforce({"citation_validity": None}, ["citation_validity=0.95"])
    assert failures and "not measured" in failures[0]


@pytest.mark.parametrize("spec", ["recall_at_16", "recall_at_16=", "recall_at_16=high"])
def test_a_malformed_threshold_fails(spec):
    assert enforce(SUMMARY, [spec])


def test_thresholds_are_independent():
    failures = enforce(SUMMARY, ["recall_at_16=0.99", "mrr=0.99"])
    assert len(failures) == 2


# ------------------------------------------------------- compare_to_baseline

BASE = {"recall_at_16": 0.7403, "over_refusal_rate": 0.1160, "citation_validity": 1.0}


def test_a_drop_in_recall_is_a_regression():
    failures, _ = compare({**BASE, "recall_at_16": 0.68}, BASE, 0.03)
    assert failures and "recall_at_16 regressed" in failures[0]


def test_a_rise_in_over_refusal_is_a_regression():
    """Direction is per metric. Refusing more questions makes the abstention numbers look
    better and the system less useful; a gate that read every metric as higher-is-better
    would reward it."""
    failures, _ = compare({**BASE, "over_refusal_rate": 0.30}, BASE, 0.03)
    assert failures and "over_refusal_rate regressed" in failures[0]


def test_a_fall_in_over_refusal_is_not_a_regression():
    failures, _ = compare({**BASE, "over_refusal_rate": 0.02}, BASE, 0.03)
    assert failures == []


def test_movement_inside_the_tolerance_passes():
    failures, _ = compare({**BASE, "recall_at_16": 0.7403 - 0.029}, BASE, 0.03)
    assert failures == []


def test_a_metric_the_baseline_tracks_and_the_run_drops_is_a_failure():
    """Otherwise a rename silently shrinks what the gate covers."""
    run = {k: v for k, v in BASE.items() if k != "recall_at_16"}
    failures, _ = compare(run, BASE, 0.03)
    assert failures and "this run does not" in failures[0]


def test_the_committed_baseline_is_readable_and_covers_the_headline_metric():
    from evals.compare_to_baseline import BASELINE

    assert BASELINE.exists(), "evals/baseline.json is not committed"
    payload = json.loads(BASELINE.read_text())
    assert payload["recall_at_16"] > 0
    assert payload["run_name"]
