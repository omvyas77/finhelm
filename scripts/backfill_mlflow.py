"""Re-attach result files to MLflow after a tracking outage.

Spec 2.5 is what turns "I tried some things" into "I ran a controlled experiment", and
for most of Day 2 it recorded nothing: MLFLOW_TRACKING_URI pointed at http://localhost:5000
with no MLflow server behind it, and on macOS that port belongs to Control Center's AirPlay
receiver, which replies 403. Eighteen ablation cells logged into a void.

Nothing was actually lost — `evals/results/<run>.json` holds the config, the summary and
every per-question record, which is a superset of what gets logged. So the experiment
record is reconstructible from disk rather than by re-running a sweep that cost real money.

Timestamps are the one thing that cannot be recovered: MLflow stamps a backfilled run with
the moment it was written, not the moment it ran. The result file's mtime is logged as
`ran_at` so the true ordering survives, and every backfilled run is tagged `backfilled=true`
so nobody later mistakes the timestamps for the real ones.

Idempotent: a run name already present in the experiment is skipped, so this can be re-run
after a partial failure without producing duplicates.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from evals import metrics as M  # noqa: E402

# Imported for the side effect of loading .env, which is where MLFLOW_TRACKING_URI lives.
# Without it this script and run_eval.py resolve *different* stores — run_eval reads the
# dotenv value, while a bare `import mlflow` here fell back to a stray
# sqlite:///mlflow.db. Two tracking stores that each look fine in isolation is a worse
# failure than no tracking at all, because the ablation would appear to be missing runs
# rather than appearing to be broken.
from src.finhelm import llm  # noqa: E402,F401

RESULTS = ROOT / "evals" / "results"
EXPERIMENT = "finhelm-retrieval"


def existing_run_names(mlflow, experiment_id: str) -> set[str]:
    frame = mlflow.search_runs(experiment_ids=[experiment_id], output_format="pandas")
    if frame.empty or "tags.mlflow.runName" not in frame:
        return set()
    return set(frame["tags.mlflow.runName"].dropna())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="list what would be logged")
    args = ap.parse_args()

    import mlflow

    uri = mlflow.get_tracking_uri()
    print(f"tracking uri: {uri}")
    if uri.startswith("http"):
        # The whole reason this script exists. Fail rather than re-enact the outage.
        import urllib.error
        import urllib.request

        try:
            urllib.request.urlopen(f"{uri}/api/2.0/mlflow/experiments/search?max_results=1",
                                   timeout=5)
        except urllib.error.HTTPError as exc:
            sys.exit(f"tracking server at {uri} answered {exc.code} — "
                     "if that is 403 on port 5000, macOS AirPlay owns the port; "
                     "set MLFLOW_TRACKING_URI=file:./mlruns")
        except Exception as exc:  # noqa: BLE001
            sys.exit(f"tracking server at {uri} unreachable: {exc}")

    mlflow.set_experiment(EXPERIMENT)
    experiment = mlflow.get_experiment_by_name(EXPERIMENT)
    already = existing_run_names(mlflow, experiment.experiment_id)

    paths = sorted(p for p in RESULTS.glob("*.json") if not p.name.endswith(".ragas.json"))
    logged, skipped = 0, 0

    for path in paths:
        payload = json.loads(path.read_text())
        run_name = payload.get("run_name") or path.stem
        if run_name in already:
            skipped += 1
            continue

        summary = payload.get("summary", {})
        config = payload.get("config", {})
        records = payload.get("records", [])
        ran_at = dt.datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds")

        if args.dry_run:
            print(f"  would log {run_name:<32} {len(records):>3} records  ran_at {ran_at}")
            logged += 1
            continue

        with mlflow.start_run(run_name=run_name):
            mlflow.set_tags({"backfilled": "true", "ran_at": ran_at,
                             "source_file": path.name})
            mlflow.log_params(config)
            mlflow.log_metrics({k: v for k, v in summary.items()
                                if isinstance(v, (int, float)) and v is not None})
            mlflow.log_artifact(str(path))
            if records:
                mlflow.log_table(
                    {
                        "id": [r["id"] for r in records],
                        "type": [r["type"] for r in records],
                        "question": [r["question"] for r in records],
                        "answer": [r.get("answer") or "" for r in records],
                        "route": ["+".join(r.get("route") or []) for r in records],
                        "latency_ms": [r.get("latency_ms") for r in records],
                        "recall": [M.recall_at_k(r["retrieved"], r["gold_spans"], 5)
                                   for r in records],
                    },
                    "per_question.json",
                )
        print(f"  logged {run_name:<32} {len(records):>3} records  ran_at {ran_at}")
        logged += 1

    verb = "would log" if args.dry_run else "logged"
    print(f"\n{verb} {logged}, skipped {skipped} already present")
    if not args.dry_run and logged:
        print(f"view with:  mlflow ui --backend-store-uri {uri}")


if __name__ == "__main__":
    main()
