"""Run the golden set against a configuration and record the result.

Produces three artifacts per run, deliberately kept separate:

  - an MLflow run (params + metrics + per-question table) for interactive comparison;
  - `evals/results/<run_name>.json`, the full per-question detail for failure analysis;
  - one line appended to `evals/history.jsonl`, which is what CI reads.

MLflow is for exploration and history.jsonl is the machine-readable contract. Keeping them
separate means a broken or missing MLflow server never blocks the PR gate.

Questions are evaluated sequentially, on purpose. Running them concurrently would roughly
halve wall-clock time but makes the p50/p95 latency numbers meaningless — they would
measure contention between eval workers rather than the latency a user would see. Latency
is a headline number in the ablation table, so it has to be measured under the conditions
it claims to describe.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from evals import metrics as M  # noqa: E402
from finhelm import llm  # noqa: E402
from finhelm.config import Config  # noqa: E402
from finhelm.generate import answer  # noqa: E402
from finhelm.retrieve import STRATEGY_FALLBACKS, retrieve  # noqa: E402

GOLDEN = ROOT / "evals" / "golden_set.jsonl"
RESULTS = ROOT / "evals" / "results"
HISTORY = ROOT / "evals" / "history.jsonl"


def load_golden(path: Path = GOLDEN) -> list[dict]:
    with path.open() as fh:
        return [json.loads(line) for line in fh if line.strip()]


def hit_to_dict(hit) -> dict:
    """Flatten a Hit into what the metrics need.

    doc_id and text come from metadata; chunk_id and score are kept for failure analysis
    (which chunk won, and by how much) but are never used for scoring — see the header of
    evals/metrics.py for why chunk_id cannot be ground truth.
    """
    return {
        "chunk_id": hit.chunk_id,
        "score": round(float(hit.score), 4),
        "doc_id": hit.metadata.get("doc_id"),
        "ticker": hit.metadata.get("ticker"),
        "date": hit.metadata.get("date"),
        "section": hit.metadata.get("section"),
        "text": hit.metadata.get("text", ""),
    }


def evaluate_one(question: dict, cfg: Config, retrieve_only: bool) -> dict:
    usage_from = len(llm.USAGE)
    started = time.monotonic()

    if retrieve_only:
        found = retrieve(question["question"], cfg)
        record = {
            "answer": "",
            "route": found.route.collections,
            "route_reason": found.route.reason,
            "retrieved": [hit_to_dict(h) for h in found.hits],
            "abstained": False,
            "retrieval_ms": int((time.monotonic() - started) * 1000),
            "generation_ms": 0,
            # Recorded because "did decomposition actually fire?" is otherwise
            # unanswerable from the artifact. The first attempt to measure agentic mode
            # read this field, found it absent, and concluded decomposition never ran —
            # while cost per query had risen 7x proving it had.
            "sub_questions": list(found.sub_questions),
            # The pre-rerank candidate pool, in rank order, as ids only — carrying its
            # text would add ~9 MB per result file.
            #
            # chunk_ids and not just doc_ids: whether the right *document* reached the
            # pool is a different and much weaker question than whether the chunk holding
            # the gold span did, since a filing contributes many chunks and only some
            # contain the answer. Scoring the pool on doc_id alone overstates how close
            # retrieval came. The audit resolves these ids back to text via the chunk
            # parquet and runs the same is_hit used for the headline metric.
            "pool_chunk_ids": [h.chunk_id for h in found.candidates],
            "pool_doc_ids": [h.metadata.get("doc_id") for h in found.candidates],
            "pool_size": len(found.candidates),
        }
    else:
        result = answer(question["question"], cfg)
        record = {
            "answer": result.answer,
            # generate.py joins collections with "+" for logging; the metric compares
            # sets, so split it back apart rather than teaching the metric about the
            # display format.
            "route": result.route.split("+") if result.route else [],
            "route_reason": result.route_reason,
            "retrieved": [hit_to_dict(h) for h in result.retrieved],
            "abstained": result.abstained,
            "retrieval_ms": result.retrieval_ms,
            "generation_ms": result.generation_ms,
            # Same field the retrieve-only branch records. Its absence here meant a
            # generating run could not answer "did decomposition fire, and on what?".
            "sub_questions": list(result.sub_questions),
        }

    record["latency_ms"] = record["retrieval_ms"] + record["generation_ms"]
    record["cost_usd"] = llm.cost_usd(llm.USAGE[usage_from:])
    record.update({
        "id": question["id"],
        "question": question["question"],
        "type": question["type"],
        "ground_truth": question.get("ground_truth", ""),
        "gold_spans": question.get("gold_spans", []),
        "expected_source": question.get("expected_source"),
    })
    return record


def _percentiles(values: list[float], prefix: str) -> dict:
    return {
        f"p50_{prefix}_ms": statistics.median(values),
        # quantiles() needs at least two points, and n=100 gives the 99 cut points of
        # which index 94 is p95.
        f"p95_{prefix}_ms": (statistics.quantiles(values, n=100)[94]
                             if len(values) > 1 else float(values[0])),
    }


def enforce(summary: dict, specs: list[str]) -> list[str]:
    """Check `METRIC=VALUE` thresholds against a finished summary.

    An unknown metric name is a failure rather than a silent pass, and that is the whole
    point of this function. The spec's gate is `--fail-under recall_at_5=0.75`; this
    system serves and measures recall@16, so `recall_at_5` is simply not a key in the
    summary. A dict lookup with a default would have made that gate green forever while
    reading, in the workflow file, exactly like a gate. A threshold you cannot fail is
    indistinguishable from no threshold at all.

    Likewise a metric present but None — recall on a run with no gold spans, citation
    validity on a retrieve-only run — is a failure. It means the gate did not measure
    what it claims to measure.
    """
    failures = []
    for spec in specs:
        name, _, raw = spec.partition("=")
        name = name.strip()
        if not _ or not raw.strip():
            failures.append(f"{spec!r} is not METRIC=VALUE")
            continue
        try:
            floor = float(raw)
        except ValueError:
            failures.append(f"{spec!r}: {raw!r} is not a number")
            continue

        if name not in summary:
            available = ", ".join(sorted(k for k, v in summary.items()
                                         if isinstance(v, (int, float))))
            failures.append(
                f"{name} is not a metric this run produced, so the threshold could "
                f"never fail.\n      available: {available}")
            continue

        value = summary[name]
        if value is None:
            failures.append(f"{name} was not measured on this run (None), "
                            f"so >= {floor} cannot be checked")
        elif value < floor:
            failures.append(f"{name} = {value:.4f} is below the floor of {floor:.4f}")
    return failures


def summarize(records: list[dict], cfg: Config, k: int) -> dict:
    summary = M.aggregate(records, k=k)
    summary.update(_percentiles([r["latency_ms"] for r in records], "latency"))
    # Reported separately from end-to-end latency because the two are not comparable
    # across runs, and the ablation table needs the retrieval half specifically.
    #
    # `latency_ms` is retrieval + generation, so a --retrieve-only run and a generating
    # run of the *same retrieval config* report wildly different p50s — measured here at
    # 1751ms vs 7963ms. Both numbers are correct; putting them in one column is not.
    # Without this split, a generation run silently supersedes its retrieve-only twin in
    # the ablation table and the winning cell reads as 4.5x slower than its neighbours,
    # which is an argument against the best config that the data does not support.
    summary.update(_percentiles([r["retrieval_ms"] for r in records], "retrieval"))
    summary.update({
        "cost_usd_per_query": sum(r["cost_usd"] for r in records) / max(len(records), 1),
        "n_questions": float(len(records)),
    })
    return summary


def log_mlflow(cfg: Config, summary: dict, records: list[dict], path: Path,
               run_name: str) -> None:
    """Log to MLflow, but never let a tracking-server problem fail the run.

    The eval result is already safely on disk by the time this is called. Treating an
    unreachable MLflow as fatal would mean losing a paid eval run to an infrastructure
    hiccup.
    """
    try:
        import mlflow
    except ImportError:
        print("  (mlflow not installed — skipping tracking)")
        return

    try:
        mlflow.set_experiment("finhelm-retrieval")
        # The tagged name, matching the result file. Using cfg.run_name() here instead
        # meant a `--tag smoke` run over 5 questions was logged under the same name as
        # the real 75-question cell, silently sitting next to it in the experiment.
        with mlflow.start_run(run_name=run_name):
            mlflow.log_params(asdict(cfg))
            mlflow.log_metrics({k: v for k, v in summary.items() if v is not None})
            mlflow.log_artifact(str(path))
            mlflow.log_table(
                {
                    "id": [r["id"] for r in records],
                    "type": [r["type"] for r in records],
                    "question": [r["question"] for r in records],
                    "answer": [r["answer"] for r in records],
                    "route": ["+".join(r["route"]) for r in records],
                    "latency_ms": [r["latency_ms"] for r in records],
                    "recall": [M.recall_at_k(r["retrieved"], r["gold_spans"], 5)
                               for r in records],
                },
                "per_question.json",
            )
    except Exception as exc:  # noqa: BLE001 - tracking must never break the run
        # Loud, because quiet cost the entire an earlier stage experiment record. MLFLOW_TRACKING_URI
        # pointed at http://localhost:5000 with no server behind it, and on macOS port
        # 5000 is held by Control Center's AirPlay receiver — which answers, with 403.
        # So this did not fail like an unreachable host, it failed like a live server
        # refusing us, one dim parenthetical per run, for all eighteen ablation cells.
        # Every one of them was logged nowhere and nobody noticed for two days.
        print(f"\n  !! MLFLOW LOGGING FAILED — this run is NOT tracked: {exc}")
        print(f"  !! tracking uri: {mlflow.get_tracking_uri()}")
        print("  !! the result file is still safe; re-attach it with scripts/backfill_mlflow.py\n")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--chunking", default="fixed",
                    choices=["fixed", "semantic", "sentence_window"])
    ap.add_argument("--retriever", default="dense", choices=["dense", "bm25", "hybrid"])
    ap.add_argument("--rerank", action="store_true")
    # Defaults to whatever the generator is actually given (cfg.top_k_context), not a
    # fixed 5. Reporting recall@5 while feeding the model 8 chunks understates the system
    # for no reason — measured at 0.4558 against 0.4917 for the same run — and the gap
    # would grow silently with any change to top_k_context.
    ap.add_argument("--k", type=int, default=None,
                    help="k for recall@k (default: cfg.top_k_context)")
    ap.add_argument("--top-k-context", type=int, default=None)
    # The pool width the reranker gets to choose from. Never swept during an earlier stage because it
    # had no flag, which left it the one retrieval knob fixed at its default through all
    # 18 cells.
    ap.add_argument("--top-k-retrieve", type=int, default=None)
    ap.add_argument("--embed-model", default=None)
    ap.add_argument("--no-query-prefix", action="store_true",
                    help="control arm: embed the query bare, as an earlier stage did")
    ap.add_argument("--cross-query-fusion", choices=["rrf", "interleave"], default=None)
    ap.add_argument("--no-filter-by-issuer", action="store_true",
                    help="control arm: search every issuer's filings, as before")
    ap.add_argument("--rerank-per-subquestion", action="store_true")
    # The one component never varied. Everything else in the pipeline — embedder, chunk
    # text, fusion, query shape, issuer filter — has been swapped at least once.
    ap.add_argument("--rerank-model", default=None)
    ap.add_argument("--chunk-tokens", type=int, default=None)
    # Pinned to the pool width it is tuned for: rrf_k=20 beats 60 at every per-query width
    # <= 50, converges at 80 and inverts at 120, so the constant is not universally better
    # and must not travel without its width.
    ap.add_argument("--rrf-k", type=int, default=None)
    ap.add_argument("--fresh", action="store_true",
                    help="ignore any checkpoint and re-answer every question")
    ap.add_argument("--no-rerank-windows", action="store_true",
                    help="control arm: score only the prefix that fits the 512-token window")
    ap.add_argument("--contextual", action="store_true",
                    help="use the contextual-header index (requires *_ctx built)")
    ap.add_argument("--agentic-filters-only", action="store_true",
                    help="decompose to extract metadata filters, then retrieve with the "
                         "single original query — isolates the filter half of "
                         "decomposition from the query half")
    ap.add_argument("--agentic", action="store_true",
                    help="decompose multi-hop questions before retrieving")
    ap.add_argument("--retrieve-only", action="store_true",
                    help="skip generation; retrieval metrics only, no LLM cost")
    ap.add_argument("--limit", type=int, default=None, help="first N questions (smoke test)")
    ap.add_argument("--golden", default=str(GOLDEN))
    ap.add_argument("--tag", default="", help="suffix for the run name and result file")
    ap.add_argument("--no-llm-router", action="store_true",
                    help="never call the model to route; the keyword heuristic fans out "
                         "to both collections when it finds no signal")
    ap.add_argument("--deterministic-only", action="store_true",
                    help="no model call anywhere in the run: retrieve-only, heuristic "
                         "router, decomposition off. Verified afterwards against "
                         "llm.USAGE rather than assumed. What the every-push CI tier runs.")
    ap.add_argument("--fail-under", action="append", default=[], metavar="METRIC=VALUE",
                    help="exit 1 if METRIC is below VALUE. Repeatable. An unknown metric "
                         "name is itself a failure, not a pass.")
    ap.add_argument("--fail-on-fallback", action="store_true",
                    help="exit 1 if any collection ran under a chunking strategy other "
                         "than the one requested")
    ap.add_argument("--allow-fallback", action="append", default=[], metavar="COLLECTION",
                    help="a collection whose fallback is intended and should not fail "
                         "--fail-on-fallback. Repeatable.")
    args = ap.parse_args()

    overrides = {"chunking": args.chunking, "retriever": args.retriever, "rerank": args.rerank}
    if args.top_k_context is not None:
        overrides["top_k_context"] = args.top_k_context
    if args.top_k_retrieve is not None:
        overrides["top_k_retrieve"] = args.top_k_retrieve
    if args.contextual:
        overrides["contextual_headers"] = True
    if args.embed_model:
        overrides["embed_model"] = args.embed_model
    if args.cross_query_fusion:
        overrides["cross_query_fusion"] = args.cross_query_fusion
    if args.no_filter_by_issuer:
        overrides["filter_by_issuer"] = False
    if args.rerank_model:
        overrides["rerank_model"] = args.rerank_model
    if args.chunk_tokens:
        overrides["chunk_tokens"] = args.chunk_tokens
    if args.rrf_k is not None:
        overrides["rrf_k"] = args.rrf_k
    if args.no_rerank_windows:
        overrides["rerank_windows"] = False
    if args.rerank_per_subquestion:
        overrides["rerank_per_subquestion"] = True
    if args.no_query_prefix:
        overrides["query_prefix"] = False
    if args.agentic:
        overrides["agentic"] = True
    if args.agentic_filters_only:
        overrides["agentic_filters_only"] = True
    # --deterministic-only is the CI name for the combination that makes no model call at
    # all. Three separate switches, because there are three separate call sites and
    # missing one degrades quietly rather than failing:
    #
    #   generation  - skipped by retrieve_only
    #   routing     - the heuristic fans out to both collections instead of asking
    #   decomposition - agent.decompose calls the model and catches every exception,
    #                 returning [question]. Without a key it therefore does not error; it
    #                 silently stops splitting multi-hop questions, and the gate measures
    #                 a system with decomposition switched off while reporting a number
    #                 that looks like the real one.
    #
    # The consequence is stated rather than hidden: this tier does not exercise
    # decomposition. Its job is to notice a change to chunking, fusion, filtering or
    # reranking, on every push, for free. The judged tier covers the agentic path.
    if args.deterministic_only:
        args.retrieve_only = True
        args.no_llm_router = True
        overrides["agentic"] = False
    if args.no_llm_router:
        overrides["llm_router"] = False
    cfg = Config(**overrides)
    # The generator is handed cfg.top_k_context chunks, so that is the k recall must be
    # measured at unless the caller says otherwise.
    k = args.k if args.k is not None else cfg.top_k_context

    questions = load_golden(Path(args.golden))
    if args.limit:
        questions = questions[: args.limit]

    run_name = cfg.run_name() + (f"-{args.tag}" if args.tag else "")
    print(f"{run_name}: {len(questions)} questions"
          f"{' (retrieve-only)' if args.retrieve_only else ''}")

    # Checkpoint file, written one JSON line per question as the run proceeds.
    #
    # A generating run over the full golden set is ~35 minutes and ~$3.70, and an
    # interruption used to lose all of it: the previous attempt died at question 113 of 202
    # on an API credit error, wrote no result file, and the 113 answered questions had to be
    # paid for again. Appending as we go means a rerun resumes instead of restarting, and
    # `--fresh` forces a clean start when the config changed in a way the name does not
    # capture.
    RESULTS.mkdir(parents=True, exist_ok=True)
    partial_path = RESULTS / f"{run_name}.partial.jsonl"

    done: dict[str, dict] = {}
    if partial_path.exists() and not args.fresh:
        with partial_path.open() as fh:
            for line in fh:
                if line.strip():
                    row = json.loads(line)
                    done[row["id"]] = row
        if done:
            print(f"  resuming: {len(done)} questions already answered in {partial_path.name}")

    records = []
    with partial_path.open("a") as checkpoint:
        for i, question in enumerate(questions, start=1):
            cached = done.get(question["id"])
            if cached is not None:
                records.append(cached)
                continue
            record = evaluate_one(question, cfg, args.retrieve_only)
            records.append(record)
            checkpoint.write(json.dumps(record) + "\n")
            checkpoint.flush()
            recall = M.recall_at_k(record["retrieved"], record["gold_spans"], k)
            flag = "-" if recall is None else f"{recall:.2f}"
            print(f"  [{i:>3}/{len(questions)}] {record['id']} {record['type']:<12} "
                  f"recall@{k}={flag} {record['latency_ms']:>6}ms", flush=True)

    summary = summarize(records, cfg, k)

    # What the run actually did, which is not always what was asked for: not every
    # collection was chunked under every strategy. Carried into the result file and
    # history so a row of the ablation table can be read back to the corpus it ran on.
    fallbacks = [{"collection": collection, "requested": requested, "used": used}
                 for (collection, requested), used in STRATEGY_FALLBACKS.items()]

    path = RESULTS / f"{run_name}.json"
    path.write_text(json.dumps(
        {"run_name": run_name, "config": asdict(cfg), "summary": summary,
         "retrieve_only": args.retrieve_only,
         "strategy_fallbacks": fallbacks, "records": records}, indent=2))

    # The complete result supersedes the checkpoint. Removed only after the real file is
    # on disk, so an interruption during the write still leaves a resumable run.
    partial_path.unlink(missing_ok=True)

    HISTORY.parent.mkdir(parents=True, exist_ok=True)
    with HISTORY.open("a") as fh:
        fh.write(json.dumps({
            "run_name": run_name,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "config": asdict(cfg),
            # Recorded explicitly rather than inferred downstream from a near-zero
            # cost_usd_per_query. Retrieve-only runs are not free — the router still makes
            # an LLM call — so "cost is zero" was never the right test, and a threshold
            # over cost would need re-tuning every time the router or pricing changed.
            "retrieve_only": args.retrieve_only,
            "strategy_fallbacks": fallbacks,
            **{k: v for k, v in summary.items() if v is not None},
        }) + "\n")

    log_mlflow(cfg, summary, records, path, run_name)

    print(f"\n{run_name}")
    for key, value in summary.items():
        print(f"  {key:<22} {'-' if value is None else f'{value:.4f}'}")

    for fallback in fallbacks:
        print(f"  ! {fallback['collection']} has no '{fallback['requested']}' index — "
              f"ran as '{fallback['used']}'")

    print(f"\nwrote {path}")

    # After the result file, history line and MLflow run are all written, never before:
    # a failing gate is exactly when someone needs the artifact to see *why* it failed,
    # and CI uploads evals/results/ on failure.
    failures = enforce(summary, args.fail_under)

    # The claim "this tier is free" checked against what actually happened, not against
    # the flags that were meant to arrange it. Every model call in this codebase appends
    # to llm.USAGE, so one assertion covers routing, decomposition, generation and
    # anything added later — a new call site on the retrieval path fails the cheap tier
    # loudly instead of quietly putting API spend on every push.
    if args.deterministic_only and llm.USAGE:
        models = sorted({record.get("model", "?") for record in llm.USAGE})
        failures.append(
            f"--deterministic-only made {len(llm.USAGE)} model call(s) to {models}; "
            f"the run cost ${llm.cost_usd(llm.USAGE):.4f} and is neither free nor "
            f"reproducible")

    # A missing index is not an error anywhere else in this codebase: _resolve_strategy
    # substitutes `fixed` and records the substitution, which is the right behaviour for
    # an ablation over a corpus that was only ever chunked one way. It is the wrong
    # behaviour for a gate. Point the CI config at an index that is not there — change the
    # embedding model, rename a strategy — and the run silently measures a different
    # system, clears its threshold, and reports green.
    # `complaints` is the standing exception and has to be named, not assumed. It exists
    # only as `fixed` on purpose — complaint narratives are a few hundred words and are
    # already close to one chunk, so re-chunking them tests nothing — which means a bare
    # --fail-on-fallback can never pass. Requiring the exception to be spelled out keeps
    # the check sharp for the case it is actually for: filings falling back, which means
    # the index the config names is missing and the gate is measuring something else.
    if args.fail_on_fallback:
        for fallback in fallbacks:
            if fallback["collection"] in args.allow_fallback:
                continue
            failures.append(
                f"{fallback['collection']} has no '{fallback['requested']}' index and ran "
                f"as '{fallback['used']}'; the gate would be measuring a different system")

    if failures:
        print("\nGATE FAILED")
        for failure in failures:
            print(f"  x {failure}")
        raise SystemExit(1)
    if args.fail_under:
        print(f"\ngate passed: {len(args.fail_under)} threshold(s) met")


if __name__ == "__main__":
    main()
