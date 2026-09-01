"""Fail a pull request that makes the system measurably worse than the accepted run.

Separate from `run_eval.py --fail-under` and answering a different question. A floor
catches "this is now bad"; this catches "this is worse than what we agreed to ship",
which is the one that matters once the floor has slack in it. A change that drops
recall@16 from 0.74 to 0.71 clears any floor set below 0.71 and is still a regression.

Direction is per metric, not global: over_refusal_rate and the latency percentiles are
worse when they go up. Getting that wrong would build a gate that congratulates you for
refusing more questions.

**On the threshold.** 0.03 on recall@16 sits inside this golden set's noise band — the
95% CI is ±0.06 — so on two independent *generating* runs of the same config this would
fire on nothing but sampling. That is not a reason to widen it; it is a reason to compare
runs that are actually comparable. The deterministic CI run (`--deterministic-only`, no
generation and no LLM router) is reproducible to the digit, so any movement in it is a
real consequence of the diff. Against a generating run, treat a single failure here as a
prompt to re-run rather than as proof.

    python evals/compare_to_baseline.py --max-regression 0.03
    python evals/compare_to_baseline.py --update      # accept the current run
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HISTORY = ROOT / "evals" / "history.jsonl"
BASELINE = ROOT / "evals" / "baseline.json"

# What a regression means for each. Everything not listed here is descriptive — n_questions,
# the confidence bounds, cost — and comparing it would produce failures nobody can act on.
TRACKED = {
    "recall_at_16": "higher",
    "recall_at_16_micro": "higher",
    "recall_at_16_single_span": "higher",
    "recall_at_16_multi_span": "higher",
    "mrr": "higher",
    "citation_validity": "higher",
    "abstention_recall": "higher",
    "route_accuracy": "higher",
    "over_refusal_rate": "lower",
}


def latest(run_name_prefix: str | None) -> dict:
    if not HISTORY.exists():
        raise SystemExit(f"{HISTORY} does not exist; nothing to compare")
    rows = [json.loads(l) for l in HISTORY.read_text().splitlines() if l.strip()]
    if run_name_prefix:
        rows = [r for r in rows if r.get("run_name", "").startswith(run_name_prefix)]
        if not rows:
            raise SystemExit(f"no run in history starting with {run_name_prefix!r}")
    if not rows:
        raise SystemExit("history is empty")
    return rows[-1]


def compare(run: dict, base: dict, tolerance: float) -> tuple[list[str], list[str]]:
    failures, lines = [], []
    for metric, direction in TRACKED.items():
        if metric not in base:
            continue
        if metric not in run:
            # Not skipped. A metric the baseline tracks and the run does not is either a
            # renamed key or a run that measured less than it claims to, and both should
            # stop a merge rather than quietly shrink the comparison.
            failures.append(f"{metric}: baseline has it, this run does not")
            continue

        before, after = base[metric], run[metric]
        if before is None or after is None:
            failures.append(f"{metric}: not measured on both sides "
                            f"(baseline={before}, run={after})")
            continue

        delta = after - before
        regression = -delta if direction == "higher" else delta
        mark = " " if regression <= tolerance else "x"
        lines.append(f"  {mark} {metric:<28} {before:>8.4f} -> {after:>8.4f}  "
                     f"{delta:+.4f}")
        if regression > tolerance:
            failures.append(f"{metric} regressed by {regression:.4f} "
                            f"({before:.4f} -> {after:.4f}), tolerance {tolerance:.4f}")
    return failures, lines


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--max-regression", type=float, default=0.03)
    ap.add_argument("--run-name", default=None,
                    help="compare the newest history entry whose run_name starts with "
                         "this; default is the newest entry of any name")
    ap.add_argument("--baseline", default=str(BASELINE))
    ap.add_argument("--update", action="store_true",
                    help="write the current run to the baseline file and exit")
    args = ap.parse_args()

    run = latest(args.run_name)
    baseline_path = Path(args.baseline)

    if args.update:
        payload = {"run_name": run.get("run_name"),
                   "timestamp": run.get("timestamp"),
                   "n_questions": run.get("n_questions"),
                   "retrieve_only": run.get("retrieve_only"),
                   **{m: run[m] for m in TRACKED if m in run}}
        baseline_path.write_text(json.dumps(payload, indent=2) + "\n")
        print(f"baseline <- {payload['run_name']}")
        for metric in TRACKED:
            if metric in payload and payload[metric] is not None:
                print(f"  {metric:<28} {payload[metric]:.4f}")
        return

    if not baseline_path.exists():
        raise SystemExit(f"{baseline_path} does not exist; create it with --update")
    base = json.loads(baseline_path.read_text())

    print(f"baseline {base.get('run_name')} ({base.get('n_questions')} questions)")
    print(f"run      {run.get('run_name')} ({run.get('n_questions')} questions)")
    if base.get("retrieve_only") != run.get("retrieve_only"):
        # A retrieve-only run has no citation_validity and no abstention numbers, so half
        # the table would compare a measurement against nothing. Refuse rather than
        # report a comparison that is only half real.
        raise SystemExit(
            f"cannot compare: baseline retrieve_only={base.get('retrieve_only')} but "
            f"run retrieve_only={run.get('retrieve_only')}")
    print()

    failures, lines = compare(run, base, args.max_regression)
    for line in lines:
        print(line)

    if failures:
        print(f"\nREGRESSION (tolerance {args.max_regression})")
        for failure in failures:
            print(f"  x {failure}")
        raise SystemExit(1)
    print(f"\nno metric regressed by more than {args.max_regression}")


if __name__ == "__main__":
    main()
