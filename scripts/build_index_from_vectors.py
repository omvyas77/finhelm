"""Build a FAISS index from vectors embedded elsewhere.

    python scripts/build_index_from_vectors.py \
        --collection filings --strategy semantic --contextual \
        --embed-model BAAI/bge-large-en-v1.5 --vectors ~/Downloads/vectors.npz

The counterpart to export_for_colab.py. Metadata is rebuilt from the local chunk parquet —
it never left this machine — and the vectors are matched back on chunk_id rather than on
row order, because a notebook that shuffles, batches or drops a row would otherwise
produce an index whose vectors belong to the wrong chunks. That failure is silent: every
query still returns results, they are simply the wrong ones.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from finhelm.stores import index_name  # noqa: E402
from finhelm.stores.faiss_store import FaissStore  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
# From finhelm.paths, not rebuilt here, so FINHELM_DATA_DIR actually reaches this
# script. It did not: these two lines used to be their own copy of the default, and a
# run pointed at the small CI fixture silently embedded the real 24,650-chunk corpus
# instead and was on its way to overwriting the real index when it was caught.
from finhelm.paths import INDEX_DIR, PROCESSED  # noqa: E402

META_FIELDS = ["chunk_id", "doc_id", "source", "ticker", "form", "date", "section", "url",
               "text", "window_start", "window_end"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--collection", required=True, choices=["filings", "complaints"])
    ap.add_argument("--strategy", required=True,
                    choices=["fixed", "semantic", "sentence_window"])
    ap.add_argument("--embed-model", required=True)
    ap.add_argument("--vectors", required=True, help="npz with 'chunk_ids' and 'vectors'")
    ap.add_argument("--contextual", action="store_true")
    args = ap.parse_args()

    blob = np.load(Path(args.vectors).expanduser(), allow_pickle=True)
    ids, vectors = list(blob["chunk_ids"]), blob["vectors"].astype("float32")
    if len(ids) != len(vectors):
        raise SystemExit(f"{len(ids)} ids but {len(vectors)} vectors")

    df = pd.read_parquet(PROCESSED / f"chunks_{args.collection}_{args.strategy}.parquet")
    by_id = {r["chunk_id"]: r for r in df.to_dict("records")}

    missing = [c for c in ids if c not in by_id]
    if missing:
        raise SystemExit(f"{len(missing)} chunk_ids are not in the local parquet, "
                         f"e.g. {missing[:3]} — the export and this corpus disagree")
    if len(ids) != len(df):
        print(f"  ! {len(df) - len(ids)} local chunks have no vector and will be omitted")

    metadata = [{k: by_id[c][k] for k in META_FIELDS if k in by_id[c]} for c in ids]
    store = FaissStore(dim=vectors.shape[1])
    store.upsert(ids, vectors, metadata)

    out = INDEX_DIR / index_name(args.collection, args.strategy, args.contextual,
                                 args.embed_model)
    store.save(out)
    size = sum(f.stat().st_size for f in out.iterdir()) / 1e6
    print(f"{store.count()} vectors (dim {vectors.shape[1]}) -> {out} ({size:.1f} MB)")


if __name__ == "__main__":
    main()
