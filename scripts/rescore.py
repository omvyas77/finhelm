"""Re-score a frozen run against the current golden-set labels.

Model outputs are left untouched. Only the labels change: each record's type, gold spans,
ground truth and expected source are replaced with the current values from
evals/golden_set.jsonl, and the summary is recomputed with evals.metrics.aggregate. No API
calls, and deterministic, because the bootstrap intervals are seeded.

The run file, its history.jsonl row and evals/baseline.json are all rewritten, and the run
file gains a `rescored` block recording when, why and which questions changed.

    python scripts/rescore.py semantic-hybrid-rr-ctx-ag-final \
        --reason "q055 relabelled: JPM 8-K 2026-01-22 discloses the figure"
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from evals import metrics as M  # noqa: E402

GOLDEN = ROOT / "evals" / "golden_set.jsonl"
RESULTS = ROOT / "evals" / "results"
HISTORY = ROOT / "evals" / "history.jsonl"
LABEL_FIELDS = ("type", "gold_spans", "ground_truth", "expected_source")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("run_name")
    ap.add_argument("--reason", required=True)
    ap.add_argument("--k", type=int, default=16)
    args = ap.parse_args()

    path = RESULTS / f"{args.run_name}.json"
    run = json.loads(path.read_text())
    golden = {json.loads(line)["id"]: json.loads(line)
              for line in GOLDEN.read_text().splitlines() if line.strip()}

    changed = []
    for record in run["records"]:
        label = golden.get(record["id"])
        if label is None:
            continue
        if any(record.get(f) != label.get(f) for f in LABEL_FIELDS):
            changed.append(record["id"])
            for field in LABEL_FIELDS:
                record[field] = label.get(field)

    before = dict(run["summary"])
    # Label-dependent metrics are recomputed; latency and cost are properties of the
    # outputs, which have not changed, so they carry over.
    run["summary"] = {**before, **M.aggregate(run["records"], k=args.k)}

    history = run.setdefault("rescored", [])
    if isinstance(history, dict):
        history = run["rescored"] = [history]
    history.append({
        "date": dt.date.today().isoformat(),
        "reason": args.reason,
        "relabelled": changed,
        "records_regenerated": False,
    })
    path.write_text(json.dumps(run, indent=2) + "\n")

    rows = [json.loads(line) for line in HISTORY.read_text().splitlines() if line.strip()]
    hits = [i for i, row in enumerate(rows) if row.get("run_name") == args.run_name]
    if len(hits) != 1:
        raise SystemExit(f"expected one history row for {args.run_name}, found {len(hits)}")
    row = rows[hits[0]]
    for key in list(row):
        if key in before:
            del row[key]
    row.update({k: v for k, v in run["summary"].items() if v is not None})
    row["rescored"] = history
    HISTORY.write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    subprocess.run([sys.executable, str(ROOT / "evals" / "compare_to_baseline.py"),
                    "--update", "--run-name", args.run_name], check=True,
                   stdout=subprocess.DEVNULL)

    print(f"relabelled: {changed or 'none'}")
    for key in ("n_positive", "n_negative", "n_gold_spans", f"recall_at_{args.k}",
                f"recall_at_{args.k}_micro", f"recall_at_{args.k}_single_span",
                f"recall_at_{args.k}_multi_span", "mrr", "abstention_recall",
                "over_refusal_rate"):
        old, new = before.get(key), run["summary"].get(key)
        if old is None and new is None:
            continue
        print(f"  {key:<28} {old!s:>20} -> {new}")


if __name__ == "__main__":
    main()
