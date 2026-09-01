"""Score an existing eval run with Ragas' LLM-judged metrics.

This reads a result file that `run_eval.py` already produced rather than re-running the
pipeline. Retrieval is deterministic and already measured; re-running it to attach judge
scores would pay for generation twice and, worse, would score a different set of answers
than the ones in the result file being annotated.

Three decisions worth stating, because each one is a way this is commonly got wrong:

*Negatives are excluded.* Faithfulness asks "is the answer supported by the context" and
context recall asks "was the reference recoverable from the context". For the 21
unanswerable/out_of_scope questions the correct answer is a refusal and the reference is
empty, so both questions are malformed rather than merely hard. Those rows are scored by
the abstention pair in evals/metrics.py instead. Averaging a meaningless faithfulness
score into the headline number is how a system that refuses everything posts good Ragas
figures.

*The judge is cached on disk, in a directory named for the judge model.* Ragas' own
DiskCacheBackend keys on the actual prompt sent to the model, so a change to a metric's
prompt correctly misses the cache — better than hand-rolling a key from (metric, question,
answer, contexts), which would serve stale scores in exactly that case.

But the key does *not* include the model. `ragas.cache._generate_cache_key` starts with
`if inspect.ismethod(func): args = args[1:]`, which strips `self` — and `self` is the
wrapper carrying the model. Two different judges asked the same question therefore collide
on one cache entry (verified in tests/test_ragas_cache.py). Left alone, switching
`judge_model` and re-scoring would replay the *previous* judge's verdicts while the output
file recorded the new judge's name: a mislabelled artifact, which is worse than a slow one
because nothing about it looks wrong.

Partitioning the cache directory by model makes that switch a guaranteed miss and keeps
each judge's entries independently reusable. This is not hypothetical — three judge models
were tried and rejected before this one (see config.py), so the switch is a thing that
actually happens here.

*Embeddings are the local bge-small, not an API model.* ResponseRelevancy embeds
generated questions to compare against the original. Using the same encoder the retriever
uses keeps the similarity scale consistent with the rest of the harness, and costs
nothing.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from evals.metrics import NEGATIVE_TYPES, refused  # noqa: E402
from finhelm import llm  # noqa: E402
from finhelm.config import Config  # noqa: E402

RESULTS = ROOT / "evals" / "results"
CACHE_ROOT = ROOT / ".cache" / "ragas"


def cache_dir(judge_model: str) -> Path:
    """Cache location for one judge, kept apart from every other judge's entries.

    Partitioned because the cache key itself cannot tell two models apart — see the
    module docstring. A directory per model is cruder than fixing the key, but it works
    against the library as shipped rather than depending on ragas internals staying put.
    """
    return CACHE_ROOT / judge_model.replace("/", "_")


def build_rows(records: list[dict]) -> tuple[list[dict], list[str]]:
    """Convert result records into Ragas' input schema, reporting what was dropped.

    Rows are skipped rather than passed through with empty fields because Ragas returns
    NaN for a malformed row, and a NaN silently lowers the mean of whichever metric it
    lands in. A skipped row that is counted and reported is recoverable; a NaN averaged
    into a headline number is not.
    """
    rows, skipped = [], []
    for record in records:
        if record["type"] in NEGATIVE_TYPES:
            continue

        contexts = [c["text"] for c in record.get("retrieved", []) if c.get("text")]
        answer = (record.get("answer") or "").strip()
        reference = (record.get("ground_truth") or "").strip()

        if not answer:
            skipped.append(f"{record['id']}: no answer (retrieve-only run?)")
            continue
        # A refusal on a positive question is a retrieval failure, and it is already
        # counted as one by over_refusal_rate. It cannot be judged here: faithfulness
        # asks whether the claims in the answer follow from the context, and a refusal
        # asserts nothing. Whatever the judge returns for it would be noise added to a
        # metric about groundedness.
        if refused(record):
            skipped.append(f"{record['id']}: abstained (counted by over_refusal_rate)")
            continue
        if not contexts:
            skipped.append(f"{record['id']}: no retrieved contexts")
            continue
        if not reference:
            skipped.append(f"{record['id']}: no ground_truth")
            continue

        rows.append({
            "user_input": record["question"],
            "response": answer,
            "retrieved_contexts": contexts,
            "reference": reference,
        })
    return rows, skipped


def judge(cfg: Config):
    """The judge LLM and the embedding model, both wired to the same disk cache."""
    from langchain_huggingface import HuggingFaceEmbeddings
    from ragas.cache import DiskCacheBackend
    from ragas.embeddings import LangchainEmbeddingsWrapper
    from ragas.llms import LangchainLLMWrapper

    from finhelm.judge import rate_limited_langchain_gemini

    path = cache_dir(cfg.judge_model)
    path.mkdir(parents=True, exist_ok=True)
    cache = DiskCacheBackend(cache_dir=str(path))

    # Paced, and it stops dead on a per-day 429 instead of retrying into a window that
    # will not reopen until midnight. A plain ChatGoogleGenerativeAI here is what let the
    # daily cap masquerade as a slow judge across two separate scoring passes.
    chat = rate_limited_langchain_gemini(cfg.judge_model, llm.env("GOOGLE_API_KEY"))
    embeddings = HuggingFaceEmbeddings(model_name=cfg.embed_model)
    return (LangchainLLMWrapper(chat, cache=cache),
            LangchainEmbeddingsWrapper(embeddings, cache=cache))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("run", help="run name, e.g. fixed-hybrid (reads evals/results/<run>.json)")
    ap.add_argument("--limit", type=int, default=None, help="first N scoreable rows")
    ap.add_argument("--workers", type=int, default=4,
                    help="concurrent judge calls (default 4; 16 rate-limits)")
    ap.add_argument("--timeout", type=int, default=300, help="per-judge-call seconds")
    args = ap.parse_args()

    path = RESULTS / f"{args.run}.json"
    if not path.exists():
        sys.exit(f"no such result file: {path}")

    payload = json.loads(path.read_text())
    # The judge is a property of the harness, not of the run being scored, so it comes
    # from the current Config. The retrieval config is read back from the file so the
    # output records what was actually scored.
    cfg = Config()

    rows, skipped = build_rows(payload["records"])
    for note in skipped:
        print(f"  skipped {note}")
    if not rows:
        sys.exit("nothing scoreable — run without --retrieve-only to produce answers")
    if args.limit:
        rows = rows[: args.limit]

    from ragas import EvaluationDataset, evaluate
    from ragas.metrics import (
        Faithfulness,
        LLMContextPrecisionWithReference,
        LLMContextRecall,
        ResponseRelevancy,
    )

    from ragas.run_config import RunConfig

    judge_llm, judge_embeddings = judge(cfg)
    metrics = [Faithfulness(), ResponseRelevancy(),
               LLMContextPrecisionWithReference(), LLMContextRecall()]
    print(f"scoring {len(rows)} rows from {args.run} with {cfg.judge_model}")

    result = evaluate(
        EvaluationDataset.from_list(rows),
        metrics=metrics,
        llm=judge_llm,
        embeddings=judge_embeddings,
        # Defaults are max_workers=16 / timeout=180, which on a rate-limited key turns
        # into 429s, tenacity backoff, and then jobs timing out inside the backoff. The
        # failure looks like a slow judge but is really self-inflicted concurrency, and
        # it surfaces as NaN rather than as an error.
        run_config=RunConfig(max_workers=args.workers, timeout=args.timeout),
    )

    # Aggregate per metric over the rows that actually returned a number. Ragas emits NaN
    # for a judge call that failed, and numpy's mean of a column containing NaN is NaN —
    # so the headline figure silently becomes unreadable when one call out of hundreds
    # times out. Counting the failures separately keeps a mostly-successful run usable and
    # makes a badly-degraded one obvious instead of merely blank.
    frame = result.to_pandas()
    scores: dict[str, dict] = {}
    for name in [m.name for m in metrics]:
        if name not in frame:
            continue
        column = frame[name]
        ok = column.dropna()
        scores[name] = {
            "mean": round(float(ok.mean()), 4) if len(ok) else None,
            "n_scored": int(len(ok)),
            "n_failed": int(column.isna().sum()),
        }

    out = path.with_suffix(".ragas.json")
    out.write_text(json.dumps({
        "run_name": args.run,
        "judge_model": cfg.judge_model,
        "n_rows": len(rows),
        "n_skipped": len(skipped),
        "skipped": skipped,
        "scores": scores,
    }, indent=2))

    print(f"\n{args.run} (judge: {cfg.judge_model}, {len(rows)} rows)")
    for name, s in scores.items():
        value = "-" if s["mean"] is None else f"{s['mean']:.4f}"
        note = f"   ({s['n_failed']} judge call(s) failed)" if s["n_failed"] else ""
        print(f"  {name:<38} {value:>7}  n={s['n_scored']}{note}")
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
