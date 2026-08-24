# Embedding on a rented GPU

Only the embedding step moves. Chunking, index construction, retrieval and eval all stay
local, so nothing about the measurement changes — the remote machine sees chunk ids and
text and returns vectors.

**Use a GPU runtime, not a TPU.** sentence-transformers is PyTorch, and PyTorch on TPU
means `torch_xla`: the encode path is not XLA-friendly, variable-length inputs have to be
padded into fixed shape buckets, and recompilation eats the time the TPU was supposed to
save. A T4 is CUDA and works unmodified. Measured locally on MPS: bge-base sustains ~23
chunks/s, so 43k chunks is ~30 min. A T4 does the same work in 2-3 min, and bge-large in
about five — which is the model that is otherwise out of reach at ~75 min locally.

## 1. Export (local)

    python scripts/export_for_colab.py --collection filings   --strategy semantic --contextual
    python scripts/export_for_colab.py --collection complaints --strategy fixed    --contextual

Writes `data/processed/embed_<collection>_<strategy>_ctx.parquet` — two columns, chunk_id
and the exact string to embed. The contextual header is applied *here* so that a remotely
built index and a locally built one embed byte-identical text; a notebook that formats the
header even slightly differently would make the A/B meaningless.

Upload both to Drive (~30 MB combined).

## 2. Embed (Colab, GPU runtime)

```python
!pip -q install sentence-transformers pandas pyarrow

import numpy as np, pandas as pd, torch
from sentence_transformers import SentenceTransformer

MODEL = "BAAI/bge-large-en-v1.5"     # or bge-base-en-v1.5, Alibaba-NLP/gte-large-en-v1.5
SRC   = "/content/drive/MyDrive/embed_filings_semantic_ctx.parquet"

assert torch.cuda.is_available(), "switch runtime to GPU"
df = pd.read_parquet(SRC)
model = SentenceTransformer(MODEL, device="cuda")
model.max_seq_length = 512          # must match local: embeddings.encode caps at 512

vectors = model.encode(
    df["text"].tolist(), batch_size=256, show_progress_bar=True,
    normalize_embeddings=True,      # local FaissStore assumes normalised vectors
    convert_to_numpy=True,
).astype("float32")

np.savez_compressed("/content/vectors_filings.npz",
                    chunk_ids=df["chunk_id"].to_numpy(), vectors=vectors)
```

Two settings must match local behaviour or the index is subtly wrong rather than broken:
`normalize_embeddings=True` (the FAISS store uses inner product and assumes unit vectors,
so unnormalised input silently ranks by magnitude) and `max_seq_length = 512`.

Passages are embedded **bare** — no query prefix. The instruction prefix is query-side
only; applying it here would put every passage in the query region of the space. See
`embeddings.QUERY_PREFIXES`.

## 3. Import (local)

    python scripts/build_index_from_vectors.py \
      --collection filings --strategy semantic --contextual \
      --embed-model BAAI/bge-large-en-v1.5 \
      --vectors ~/Downloads/vectors_filings.npz

Vectors are matched back on chunk_id, not row order — a notebook that shuffles or drops a
row would otherwise build an index whose vectors belong to the wrong chunks, which fails
silently: every query still returns results, they are simply wrong. The importer refuses
outright on unknown chunk ids and warns on missing ones.

## 4. Evaluate

    python evals/run_eval.py --chunking semantic --retriever hybrid --rerank --agentic \
      --contextual --embed-model BAAI/bge-large-en-v1.5 --retrieve-only --tag large

`index_name` encodes the embedding model, so a bge-large index cannot collide with the
bge-small one. Both stay on disk and remain directly comparable.
