"""Corpus and index locations, overridable with FINHELM_DATA_DIR.

CI cannot use the real ones: the chunk parquet is 192 MB and the indexes 961 MB, and both
are gitignored build artifacts. FINHELM_DATA_DIR points a run at the committed fixture in
data/ci instead.

Read once at import. These are module-level constants elsewhere in the codebase, so
resolving them per call would let a store loaded before the variable was set and one
loaded after disagree about which corpus they are on.
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

DATA_DIR = Path(os.getenv("FINHELM_DATA_DIR") or (ROOT / "data"))
PROCESSED = DATA_DIR / "processed"
INDEX_DIR = DATA_DIR / "index"

__all__ = ["ROOT", "DATA_DIR", "PROCESSED", "INDEX_DIR"]
