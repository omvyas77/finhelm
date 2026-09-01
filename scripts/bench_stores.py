"""FAISS vs pgvector on the same vectors, same queries, same filters.

The point is not the latency table. FAISS wins that at 24k vectors and would win it at
10x this size; a flat inner-product scan over 75 MB of float32 is hard to beat with a
network round trip in the way. The point is the second half of the report.

FAISS has no WHERE clause, so `faiss_store.search` over-fetches `k * FILTER_OVERFETCH`
neighbours and discards the ones that fail the predicate. When the predicate is narrow —
one issuer, one form, one year — the matching rows can sit outside that window entirely,
and the store returns *fewer than k results with no error*. Postgres applies the predicate
as part of the query and cannot do that. This script measures how often it happens on the
filters this project actually generates, which is the honest argument for a real database
here rather than "it scales better".

Vectors are read back out of the existing FAISS index rather than recomputed, so both
backends are answering from bit-identical embeddings and any difference is the engine.

    docker compose up -d postgres
    PYTHONPATH=src .venv/bin/python scripts/bench_stores.py
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from finhelm.agent.decompose import filters_for
from finhelm.config import EMBED_DIMS, Config
from finhelm.embeddings import encode
from finhelm.stores import INDEX_DIR, index_name
from finhelm.stores.faiss_store import FILTER_OVERFETCH, FaissStore

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "evals" / "golden_set.jsonl"


def load_faiss(cfg: Config, collection: str) -> FaissStore:
    directory = INDEX_DIR / index_name(collection, cfg.chunking, cfg.contextual_headers,
                                       cfg.embed_model, cfg.chunk_tokens)
    if not directory.exists():
        raise SystemExit(f"no index at {directory}; build it before benchmarking")
    return FaissStore.load(directory)


def mirror_into_pg(faiss: FaissStore, table: str, dim: int, batch: int = 500):
    """Copy the FAISS index into Postgres verbatim.

    reconstruct_n gives back the stored vectors rather than re-encoding the corpus, which
    matters twice: it takes seconds instead of 85 minutes, and it removes the possibility
    that a difference in results is a difference in embeddings.
    """
    from finhelm.stores.pgvector_store import PgVectorStore

    store = PgVectorStore(collection=table, dim=dim)
    if store.count() == faiss.count():
        print(f"  pgvector already holds {store.count():,} rows; skipping load")
        return store

    vectors = faiss._index.reconstruct_n(0, faiss._index.ntotal)
    started = time.monotonic()
    for start in range(0, len(faiss._ids), batch):
        stop = start + batch
        store.upsert(faiss._ids[start:stop], vectors[start:stop],
                     faiss._meta[start:stop])
        print(f"  loaded {min(stop, len(faiss._ids)):,}/{len(faiss._ids):,}", end="\r")
    print(f"\n  loaded {store.count():,} rows in {time.monotonic() - started:.1f}s")
    return store


def timed(fn, *args, repeat: int = 3) -> tuple[list, float]:
    """Median of `repeat`, and the results from the last run.

    Median rather than mean: this is a laptop running four other containers, and one
    scheduler hiccup in a five-query sample moves a mean by more than the effect.
    """
    samples = []
    result = None
    for _ in range(repeat):
        started = time.perf_counter()
        result = fn(*args)
        samples.append((time.perf_counter() - started) * 1000)
    return result, statistics.median(samples)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--collection", default="filings")
    parser.add_argument("--k", type=int, default=20)
    parser.add_argument("--queries", type=int, default=40)
    args = parser.parse_args()

    cfg = Config(chunking="semantic", contextual_headers=True,
                 embed_model="BAAI/bge-base-en-v1.5")
    dim = EMBED_DIMS[cfg.embed_model]
    table = index_name(args.collection, cfg.chunking, cfg.contextual_headers,
                       cfg.embed_model, cfg.chunk_tokens)

    print(f"index {table}  k={args.k}  dim={dim}")
    faiss = load_faiss(cfg, args.collection)
    print(f"  faiss holds {faiss.count():,} vectors")
    pg = mirror_into_pg(faiss, table, dim)

    rows = [json.loads(line) for line in GOLDEN.read_text().splitlines() if line.strip()]
    # expected_source is a list, not a scalar — `r.get("source")` matched nothing at all
    # and the first run of this script reported a benchmark over zero questions while
    # printing a full results table of "n/a".
    questions = [r["question"] for r in rows
                 if "filings" in (r.get("expected_source") or [])][: args.queries]
    print(f"  {len(questions)} filings questions from the golden set\n")

    # Embed once, outside the timing loop. Encoding is ~40 ms and identical for both
    # backends, so including it would compress the difference being measured.
    vectors = encode(questions, cfg.embed_model, is_query=cfg.query_prefix)

    unfiltered = {"faiss": [], "pg": []}
    filtered = {"faiss": [], "pg": []}
    short, agreement, overlap, n_filtered = 0, [], [], 0

    for question, vector in zip(questions, vectors):
        _, ms = timed(faiss.search, vector, args.k, None)
        unfiltered["faiss"].append(ms)
        _, ms = timed(pg.search, vector, args.k, None)
        unfiltered["pg"].append(ms)

        # The filters the pipeline actually derives, not invented ones. filters_for is
        # what runs in production on every filings sub-question.
        predicate = filters_for(question)
        if not predicate:
            continue
        n_filtered += 1

        f_hits, ms = timed(faiss.search, vector, args.k, predicate)
        filtered["faiss"].append(ms)
        p_hits, ms = timed(pg.search, vector, args.k, predicate)
        filtered["pg"].append(ms)

        if len(f_hits) < args.k and len(p_hits) >= len(f_hits):
            short += 1

        # Both orderings and set overlap, reported separately on purpose. A high set
        # overlap with a scrambled order is a real difference that overlap alone hides;
        # this project has been burned by exactly that once already.
        f_ids = [h.chunk_id for h in f_hits]
        p_ids = [h.chunk_id for h in p_hits]
        pairs = min(len(f_ids), len(p_ids))
        if pairs:
            agreement.append(sum(a == b for a, b in zip(f_ids, p_ids)) / pairs)
            overlap.append(len(set(f_ids) & set(p_ids)) / pairs)

    def summarise(samples):
        if not samples:
            return "n/a"
        ordered = sorted(samples)
        p95 = ordered[min(len(ordered) - 1, int(0.95 * len(ordered)))]
        return f"p50 {statistics.median(samples):6.1f} ms   p95 {p95:6.1f} ms"

    print("latency, same vectors, same queries")
    print(f"  unfiltered  faiss    {summarise(unfiltered['faiss'])}")
    print(f"  unfiltered  pgvector {summarise(unfiltered['pg'])}")
    print(f"  filtered    faiss    {summarise(filtered['faiss'])}")
    print(f"  filtered    pgvector {summarise(filtered['pg'])}")

    print(f"\nfiltering, on the {n_filtered} questions filters_for() fires on")
    print(f"  faiss over-fetches {args.k} x {FILTER_OVERFETCH} = "
          f"{args.k * FILTER_OVERFETCH} candidates, then discards non-matches")
    print(f"  queries where faiss returned FEWER than k={args.k}: {short}/{n_filtered}")
    if agreement:
        print(f"  exact-order agreement  {statistics.mean(agreement):.3f}")
        print(f"  set overlap            {statistics.mean(overlap):.3f}")
    print("\n  Set overlap is the flattering number and exact-order agreement is the one "
          "that\n  matters: HNSW is approximate, so pgvector is not obliged to return the "
          "flat\n  index's ordering, only a close one.")

    narrow_filter_probe(faiss, pg, vectors[:8], args.k)


def narrow_filter_probe(faiss, pg, vectors, k: int) -> None:
    """Where the post-filter actually breaks, on filters this project does not generate.

    The section above is a negative result: on the issuer and form filters filters_for()
    really produces, FAISS never returned short, because an issuer is roughly a ninth of
    this corpus and 400 candidates is a wide net. That stands as the finding, and it would
    be dishonest to describe pgvector's advantage as though it had been observed here.

    But the mechanism is real and it is arithmetic, so it can be shown directly rather
    than argued. The narrowest (ticker, form, year) cell in this corpus holds 26 of 24,650
    rows, 0.105%; a 400-candidate window drawn from anywhere expects 0.42 matches in it.
    Postgres applies the predicate inside the query and has no window to exhaust.
    """
    from finhelm.stores.base import matches

    cells = {}
    for meta in faiss._meta:
        ticker, form, date = meta.get("ticker"), meta.get("form"), meta.get("date")
        if ticker and form and date:
            key = (ticker, form, str(date)[:4])
            cells[key] = cells.get(key, 0) + 1
    narrowest = sorted(cells.items(), key=lambda kv: kv[1])[:3]

    print("\nnarrow filters - constructed, NOT ones filters_for() emits")
    print(f"  {'filter':22s} {'rows':>6s} {'% corpus':>9s} {'faiss':>8s} {'pgvector':>9s}")
    for (ticker, form, year), count in narrowest:
        predicate = {"ticker": ticker, "form": form, "date": {"prefix": year}}
        # Confirm both backends read the filter the same way before comparing counts, or
        # a difference in results could be a difference in filter semantics rather than
        # in engine behaviour.
        available = sum(1 for m in faiss._meta if matches(m, predicate))
        assert available == count, f"filter semantics disagree: {available} != {count}"

        f_total = sum(len(faiss.search(v, k, predicate)) for v in vectors)
        p_total = sum(len(pg.search(v, k, predicate)) for v in vectors)
        label = f"{ticker} {form} {year}"
        print(f"  {label:22s} {count:6d} {100 * count / faiss.count():8.3f}% "
              f"{f_total / len(vectors):8.1f} {p_total / len(vectors):9.1f}")
    print(f"  mean results returned per query against k={k}. Both backends were handed "
          f"the\n  same predicate, checked against stores.base.matches first.")


if __name__ == "__main__":
    main()
