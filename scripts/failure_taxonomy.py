"""Classify every failure in a result file, with counts.

This is the answer to "what is still wrong with it?", and it is a script rather than a
paragraph in notes/failures.md because the point of the taxonomy is to be re-run: Day 3
exists to move these counts, and a hand-tallied table would be stale the first time
retrieval changes.

--------------------------------------------------------------------------------------
Why the categories are assigned in a fixed order
--------------------------------------------------------------------------------------
The spec's five categories overlap in practice — a question can be routed to the wrong
collection *and* retrieve nothing *and* then refuse — so counting them independently
produces a table whose column sums exceed the number of failures and which cannot be used
to decide what to fix. Each failure is therefore charged to its *earliest* cause in the
pipeline, since fixing an upstream cause can dissolve everything downstream of it:

    routing -> retrieval -> abstention decision -> synthesis

The independent (overlapping) tallies are printed underneath, because the precedence
order answers "what should I fix first" while the raw counts answer "how often does this
happen at all", and conflating those is its own mistake.

--------------------------------------------------------------------------------------
The synthesis check is deterministic on purpose, and it is a floor not a verdict
--------------------------------------------------------------------------------------
"Right chunk, wrong synthesis" ideally needs a judge. This uses figure agreement instead:
when the gold span quantifies something, the answer is expected to contain at least one of
its figures. In filings the number usually *is* the fact, which is what makes the check
meaningful, and it costs nothing so it cannot be rate-limited into unavailability.

It is a floor: it catches an answer that contradicts or omits the gold figures, and misses
an answer that quotes the right number inside wrong prose. Every count it produces is
therefore a lower bound on synthesis failure, and is labelled as such in the output. Rows
whose gold span contains no figures are reported separately rather than silently scored,
because for those the check has nothing to test and counting them as passes would inflate
the result.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from evals.metrics import NEGATIVE_TYPES, _numbers, is_hit, refused  # noqa: E402

RESULTS = ROOT / "evals" / "results"


def gold_retrieved(record: dict, k: int) -> bool:
    return any(is_hit(chunk, span)
               for span in record["gold_spans"]
               for chunk in record["retrieved"][:k])


def misrouted(record: dict) -> bool:
    """True when none of the collections the question needs were searched.

    `expected_source` is a list and so is `route`; comparing them with `in` silently
    compares a list against list *members* and reports almost everything as an error.
    That mistake produced a "67 of 75 misrouted" figure that flatly contradicted the
    harness's own route_accuracy of 0.955.
    """
    expected = record.get("expected_source")
    if not expected:  # out_of_scope questions have no correct collection
        return False
    return not (set(expected) & set(record.get("route") or []))


def gold_figures(record: dict) -> set[str]:
    figures: set[str] = set()
    for span in record["gold_spans"]:
        figures |= _numbers(span["snippet"])
    return figures


def classify(record: dict, k: int) -> str:
    """The single earliest cause this failure should be charged to."""
    if misrouted(record):
        return "routing error"

    if not gold_retrieved(record, k):
        # Both are retrieval failures, but they fail differently downstream and want
        # different fixes, so they are not merged. Abstaining with no evidence is the
        # system behaving correctly on a bad input; answering anyway is the dangerous one.
        return ("retrieval miss (abstained)" if refused(record)
                else "retrieval miss (answered anyway)")

    if refused(record):
        return "over-refusal"

    figures = gold_figures(record)
    if figures and not (figures & _numbers(record.get("answer") or "")):
        return "wrong synthesis (>= floor)"
    return "correct"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("run", help="run name, e.g. semantic-hybrid-rr-final")
    ap.add_argument("--k", type=int, default=5)
    args = ap.parse_args()

    path = RESULTS / f"{args.run}.json"
    if not path.exists():
        sys.exit(f"no such result file: {path}")
    records = json.loads(path.read_text())["records"]

    positives = [r for r in records if r["type"] not in NEGATIVE_TYPES]
    negatives = [r for r in records if r["type"] in NEGATIVE_TYPES]
    if not any((r.get("answer") or "").strip() for r in positives):
        sys.exit("this is a retrieve-only run — no answers to classify")

    charged = Counter()
    by_type: dict[str, Counter] = {}
    for record in positives:
        label = classify(record, args.k)
        charged[label] += 1
        by_type.setdefault(label, Counter())[record["type"]] += 1

    total = len(positives)
    print(f"\n{args.run}: {total} answerable questions, charged to earliest cause\n")
    for label, count in charged.most_common():
        spread = ", ".join(f"{t} {n}" for t, n in sorted(by_type[label].items()))
        print(f"  {label:<32} {count:>3}  ({count / total:.0%})   {spread}")

    # Negatives are a separate population: the correct answer is a refusal, so the only
    # failure available to them is answering. Folding them into the table above would let
    # a system that refuses everything look good.
    answered = [r for r in negatives if not refused(r)]
    print(f"\n  {'hallucinated on a negative':<32} {len(answered):>3}  "
          f"({len(answered) / max(len(negatives), 1):.0%} of {len(negatives)} negatives)"
          f"   {', '.join(r['id'] for r in answered)}")

    print("\nindependent counts (these overlap, and will not sum to the table above):")
    print(f"  misrouted                        {sum(misrouted(r) for r in positives):>3}")
    print(f"  gold span not in top-{args.k}            "
          f"{sum(not gold_retrieved(r, args.k) for r in positives):>3}")
    print(f"  abstained                        {sum(refused(r) for r in positives):>3}")

    # The abstention policy's real error rate. Refusing when retrieval returned nothing is
    # not an over-refusal — it is the system declining to invent an answer, which is the
    # behaviour the golden set's negatives exist to reward. Only a refusal made while
    # holding the evidence is a policy failure, and separating the two is what stops Day 3
    # from tuning the threshold when the actual problem is recall.
    with_evidence = [r for r in positives if gold_retrieved(r, args.k)]
    print(f"  abstained despite having gold    "
          f"{sum(refused(r) for r in with_evidence):>3}  of {len(with_evidence)} that had it")

    scoreable = [r for r in with_evidence if gold_figures(r) and not refused(r)]
    print(f"  synthesis checkable (gold has figures, answered): {len(scoreable):>3}")

    temporal = [r for r in positives if r["type"] == "temporal"]
    failed_temporal = [r for r in temporal if classify(r, args.k) != "correct"]
    print(f"\ntemporal confusion: {len(failed_temporal)} of {len(temporal)} temporal "
          f"questions failed  ({', '.join(r['id'] for r in failed_temporal) or 'none'})")


if __name__ == "__main__":
    main()
