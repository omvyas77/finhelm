"""Chunk the corpus into the two collections, one parquet per strategy.

    python scripts/build_chunks.py --strategy fixed
    python scripts/build_chunks.py --strategy semantic --collection filings

Two collections, kept separate on purpose: a question about CRE exposure should never
surface a complaint about a late fee, and the split is what gives the router something
to route.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
import time
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.finhelm.chunking import chunk, chunks_name  # noqa: E402
from src.finhelm.config import Config  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "data" / "processed" / "corpus.jsonl"
OUT_DIR = ROOT / "data" / "processed"

COLLECTIONS = {
    "filings": lambda r: r["source"] in ("edgar", "fomc"),
    "complaints": lambda r: r["source"] == "cfpb",
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", required=True,
                    choices=["fixed", "semantic", "sentence_window"])
    ap.add_argument("--collection", default="both",
                    choices=["filings", "complaints", "both"])
    ap.add_argument("--chunk-tokens", type=int, default=None)
    args = ap.parse_args()

    overrides = {"chunking": args.strategy}
    if args.chunk_tokens:
        overrides["chunk_tokens"] = args.chunk_tokens
    cfg = dataclasses.replace(Config(), **overrides)

    docs = [json.loads(line) for line in CORPUS.open(encoding="utf-8")]
    targets = ["filings", "complaints"] if args.collection == "both" else [args.collection]

    for name in targets:
        subset = [d for d in docs if COLLECTIONS[name](d)]
        started = time.monotonic()
        rows, failures = [], 0

        for i, doc in enumerate(subset, 1):
            try:
                rows.extend(c.to_dict() for c in chunk(doc, cfg))
            except Exception as e:  # one malformed doc must not kill a 20-minute run
                failures += 1
                print(f"  ! {doc['doc_id']}: {type(e).__name__}: {e}", flush=True)
            if i % 25 == 0 or i == len(subset):
                print(f"  {name}: {i}/{len(subset)} docs -> {len(rows)} chunks", flush=True)

        df = pd.DataFrame(rows)
        out = OUT_DIR / f"{chunks_name(name, args.strategy, cfg.chunk_tokens)}.parquet"
        df.to_parquet(out, index=False)

        elapsed = time.monotonic() - started
        tokens = df["text"].str.split().str.len()
        print(
            f"\n{name}/{args.strategy}: {len(df)} chunks from {len(subset)} docs "
            f"in {elapsed:.0f}s ({failures} failed)"
        )
        print(f"  words per chunk: p50={tokens.median():.0f} p95={tokens.quantile(.95):.0f} "
              f"max={tokens.max():.0f}")
        print(f"  -> {out}")


if __name__ == "__main__":
    main()
