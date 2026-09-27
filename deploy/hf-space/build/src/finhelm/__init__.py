"""finhelm: finance RAG over US bank filings, with an evaluation harness.

KMP_DUPLICATE_LIB_OK is set here, before anything can import faiss or torch.

On macOS, torch, faiss-cpu and scikit-learn each ship their own copy of libomp.dylib.
Loading two into one process aborts the OpenMP runtime:

    OMP: Error #15: Initializing libomp.dylib, but found libomp.dylib already initialized

which surfaces as a bare SIGSEGV (exit 139) as soon as a query touches both the embedding
model and the index. The override is documented as possibly producing incorrect results,
so tests/test_faiss_correctness.py checks FAISS top-k against a brute-force numpy cosine
over the same vectors. Linux wheels do not carry the conflict.
"""

import os

os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
