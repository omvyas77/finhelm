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


def encode(texts: list[str], model_name: str, batch_size: int = 128,
           progress: bool = False, max_seq_length: int | None = None) -> np.ndarray:
    """Normalized embeddings, float32. Normalized so inner product == cosine.

    `max_seq_length` trades accuracy for speed. Leave it None for indexing, where the
    full 512 tokens matter. Semantic chunking passes 128 because it only needs the
    relative distance between adjacent sentences to locate a topic boundary, and
    truncation moves those distances far less than it costs in throughput.
    """
    model = get_model(model_name)
    previous = model.max_seq_length
    if max_seq_length:
        model.max_seq_length = max_seq_length
    try:
        vectors = model.encode(
            texts,
            batch_size=batch_size,
            normalize_embeddings=True,
            show_progress_bar=progress,
            convert_to_numpy=True,
        )
    finally:
        model.max_seq_length = previous
    return np.asarray(vectors, dtype="float32")
