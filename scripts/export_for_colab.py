"""Export contextualised chunk text for embedding on a rented GPU.

    python scripts/export_for_colab.py --collection filings --strategy semantic

Only the *embedding* step is worth moving off this machine. Chunking is cheap, FAISS index
construction is cheap, and both depend on code that lives here — but embedding 43k chunks
through a 768-dim model is ~23 minutes on local MPS and ~2 minutes on a Colab T4, and the
gap widens with model size. bge-large is 75 minutes locally, which is why it was ruled out;
on a T4 it is about five.

What crosses the wire is deliberately minimal: chunk_id and the exact string to embed. No
metadata, no gold spans, nothing that would let the remote copy drift from local state —
the returned vectors are matched back on chunk_id, and build_index_from_vectors.py rebuilds
the metadata locally from the parquet that never left.

The header is applied here rather than in the notebook so that both paths embed byte-
identical text. A contextual index built remotely and one built locally must be comparable,
and "the notebook formatted the header slightly differently" is exactly the kind of silent
difference that would make an A/B meaningless.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.finhelm.chunking.context import contextualize  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
PROCESSED = ROOT / "data" / "processed"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--collection", required=True, choices=["filings", "complaints"])
    ap.add_argument("--strategy", required=True,
                    choices=["fixed", "semantic", "sentence_window"])
    ap.add_argument("--contextual", action="store_true")
    args = ap.parse_args()

    src = PROCESSED / f"chunks_{args.collection}_{args.strategy}.parquet"
    df = pd.read_parquet(src)
    texts = ([contextualize(r["text"], r) for r in df.to_dict("records")]
             if args.contextual else df["text"].tolist())

    out_name = f"embed_{args.collection}_{args.strategy}"
    if args.contextual:
        out_name += "_ctx"
    out = PROCESSED / f"{out_name}.parquet"
    pd.DataFrame({"chunk_id": df["chunk_id"], "text": texts}).to_parquet(out, index=False)

    mb = out.stat().st_size / 1e6
    print(f"{len(df)} chunks -> {out}  ({mb:.1f} MB)")
    if args.contextual:
        print(f"  header sample: {texts[0].splitlines()[0]}")


if __name__ == "__main__":
    main()
