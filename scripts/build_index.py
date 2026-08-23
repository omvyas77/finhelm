"""Embed a chunk parquet and persist a FAISS index.

    python scripts/build_index.py --collection filings --strategy fixed

Indexes live at data/index/{collection}_{strategy}/ so the Day 2 ablation can hold
several strategies side by side and swap between them by config alone.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.finhelm.chunking.context import contextualize  # noqa: E402
from src.finhelm.config import Config  # noqa: E402
from src.finhelm.embeddings import encode, pick_device  # noqa: E402
from src.finhelm.stores import index_name  # noqa: E402
from src.finhelm.stores.faiss_store import FaissStore  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
PROCESSED = ROOT / "data" / "processed"
INDEX_DIR = ROOT / "data" / "index"

META_FIELDS = [
    "chunk_id", "doc_id", "source", "ticker", "form", "date", "section", "url",
    "text", "window_start", "window_end",
]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--collection", required=True, choices=["filings", "complaints"])
    ap.add_argument("--strategy", required=True,
                    choices=["fixed", "semantic", "sentence_window"])
    ap.add_argument("--batch-size", type=int, default=128)
    ap.add_argument("--contextual", action="store_true",
                    help="prepend issuer/form/period/section before embedding")
    args = ap.parse_args()

    cfg = Config()
    src = PROCESSED / f"chunks_{args.collection}_{args.strategy}.parquet"
    df = pd.read_parquet(src)
    print(f"{src.name}: {len(df)} chunks | device={pick_device()}")

    # Only the embedded text carries the header; df["text"] (and therefore the stored
    # metadata, BM25, generation and is_hit) stays exactly as chunked. See context.py.
    if args.contextual:
        texts = [contextualize(r["text"], r) for r in df.to_dict("records")]
        print(f"  contextual headers on, e.g.: {texts[0].splitlines()[0]}")
    else:
        texts = df["text"].tolist()

    started = time.monotonic()
    vectors = encode(texts, cfg.embed_model, batch_size=args.batch_size, progress=True)
    elapsed = time.monotonic() - started
    print(f"embedded in {elapsed:.0f}s ({len(df) / elapsed:.0f} chunks/s) -> {vectors.shape}")

    store = FaissStore(dim=vectors.shape[1])
    metadata = df[[c for c in META_FIELDS if c in df.columns]].to_dict("records")
    store.upsert(df["chunk_id"].tolist(), vectors, metadata)

    out = INDEX_DIR / index_name(args.collection, args.strategy, args.contextual)
    store.save(out)
    size_mb = sum(f.stat().st_size for f in out.iterdir()) / 1e6
    print(f"saved {store.count()} vectors -> {out} ({size_mb:.1f} MB)")


if __name__ == "__main__":
    main()
