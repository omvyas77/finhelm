"""Finhelm — finance RAG with a real evaluation harness.

The env var below has to be set before anything imports faiss or torch, which is why it
lives in the package __init__ rather than in a script.

PyTorch, faiss-cpu and scikit-learn each ship their own copy of libomp.dylib in their
macOS wheels. Loading two of them into one process makes the OpenMP runtime abort:

    OMP: Error #15: Initializing libomp.dylib, but found libomp.dylib already initialized

which surfaces as a bare SIGSEGV (exit 139) the moment retrieval touches both the
embedding model and the index — i.e. on every real query.

The override is documented as unsafe and "may silently produce incorrect results", so it
is not taken on trust: tests/test_faiss_correctness.py checks FAISS top-k against a
brute-force numpy cosine over the same vectors. Linux wheels do not have the conflict, so
this is a macOS-dev-only concession that does not follow the system into Docker.
"""

import os

os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
