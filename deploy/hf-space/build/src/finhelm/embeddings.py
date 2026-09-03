"""Embedding model access, with one place that decides which device to use.

Device is auto-detected rather than configured: MPS locally on Apple Silicon, CUDA if
present, CPU everywhere else. The Docker image and Cloud Run both land on CPU, so this
must degrade silently rather than fail.
"""

from __future__ import annotations

import functools
import os

import numpy as np


def pick_device() -> str:
    if os.getenv("FINHELM_DEVICE"):
        return os.environ["FINHELM_DEVICE"]
    import torch

    if torch.cuda.is_available():
        return "cuda"
    if torch.backends.mps.is_available():
        return "mps"
    return "cpu"


@functools.lru_cache(maxsize=4)
def get_model(name: str):
    from sentence_transformers import SentenceTransformer

    return SentenceTransformer(name, device=pick_device())


# How many texts to embed between MPS cache flushes.
#
# On MPS, throughput decays badly across a long encode as the allocator's cache grows.
# Measured on the sentence_window corpus at batch_size=128, in 5000-chunk windows:
#
#     432, 465, 459, 309, 137, 12 chunks/s     <- one uninterrupted encode
#     398, 440, 442, 426, 333, 395 chunks/s    <- torch.mps.empty_cache() between windows
#
# A 38x collapse by 30k chunks, against flat allocated memory (133 MB) when flushed. The
# filings sentence_window index is 190,858 chunks, so left alone this is the difference
# between roughly eight minutes and the several hours the build guide budgets for it.
#
# It is not a batch-size problem, which is the natural first guess: batches of 256 and 512
# measured 21x and 44x slower than 128 only because those runs happened later in the same
# process, after the cache had already grown.
# Lowered from 8192 after bge-base stalled. The window is a memory budget, not a count:
# bge-base is 768-dim against bge-small's 384 and carries ~3x the activations, so the same
# window holds roughly twice the allocator pressure and the decay this constant exists to
# prevent reappears inside a single window. Measured in isolation bge-base sustains 29.4
# chunks/s, which predicts 8192 chunks in 4.6 min; the real build passed 15 min without
# reaching that checkpoint. Smaller windows flush more often for a negligible cost.
MPS_FLUSH_EVERY = 2048


def _encode_all(model, texts: list[str], batch_size: int, progress: bool) -> np.ndarray:
    """Encode in blocks on MPS, releasing the allocator cache between them.

    CUDA and CPU do not show the decay, and blocking there would only add overhead and
    fragment the progress bar, so they take the single-call path unchanged.
    """
    on_mps = str(getattr(model, "device", "")).startswith("mps")
    if not on_mps or len(texts) <= MPS_FLUSH_EVERY:
        return model.encode(texts, batch_size=batch_size, normalize_embeddings=True,
                            show_progress_bar=progress, convert_to_numpy=True)

    import torch

    blocks = []
    for start in range(0, len(texts), MPS_FLUSH_EVERY):
        blocks.append(model.encode(
            texts[start:start + MPS_FLUSH_EVERY], batch_size=batch_size,
            normalize_embeddings=True, show_progress_bar=False, convert_to_numpy=True))
        torch.mps.empty_cache()
        if progress:
            done = min(start + MPS_FLUSH_EVERY, len(texts))
            print(f"    embedded {done:>7}/{len(texts)}", flush=True)
    return np.vstack(blocks)


# Instruction prefixes for asymmetric retrieval models.
#
# BGE v1.5 is trained with the query and the passage encoded differently: passages go in
# bare, queries carry an instruction. Embedding both the same way — which is what this
# module did through all of Day 2 — leaves the query vector in a slightly different region
# than the training distribution. Measured on the Day 2 golden set it costs about 4 points
# of recall@5, which is small but free to recover and simply wrong to leave in.
#
# Keyed by model prefix because the correct string is model-specific (E5 wants "query: ",
# BGE wants the sentence below) and guessing it for an unknown model is worse than
# skipping it, so anything unrecognised gets no prefix at all.
QUERY_PREFIXES = {
    "BAAI/bge-": "Represent this sentence for searching relevant passages: ",
    "intfloat/e5-": "query: ",
    "intfloat/multilingual-e5-": "query: ",
}


def query_prefix(model_name: str) -> str:
    for stem, prefix in QUERY_PREFIXES.items():
        if model_name.startswith(stem):
            return prefix
    return ""


def encode(texts: list[str], model_name: str, batch_size: int = 128,
           progress: bool = False, max_seq_length: int | None = None,
           is_query: bool = False) -> np.ndarray:
    """Normalized embeddings, float32. Normalized so inner product == cosine.

    `max_seq_length` trades accuracy for speed. Leave it None for indexing, where the
    full 512 tokens matter. Semantic chunking passes 128 because it only needs the
    relative distance between adjacent sentences to locate a topic boundary, and
    truncation moves those distances far less than it costs in throughput.
    """
    if is_query:
        prefix = query_prefix(model_name)
        if prefix:
            texts = [prefix + t for t in texts]

    model = get_model(model_name)
    previous = model.max_seq_length
    if max_seq_length:
        model.max_seq_length = max_seq_length
    try:
        vectors = _encode_all(model, texts, batch_size, progress)
    finally:
        model.max_seq_length = previous
    return np.asarray(vectors, dtype="float32")
