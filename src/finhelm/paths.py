"""Where the corpus and the indexes live.

One place, and overridable, because CI cannot use the real ones. The corpus is 192 MB of
chunk parquet and the indexes are 961 MB; both are gitignored build artifacts, so a
checkout on a runner has neither. `FINHELM_DATA_DIR` lets the gate point at the small
committed fixture in data/ci without copying files over the developer's real corpus —
which is also how this gets tested locally without destroying a corpus that takes 85
minutes to rebuild.

Read at import, not per call: these are module-level constants elsewhere in the codebase
and making them dynamic would mean a store loaded before the variable was set and one
loaded after could disagree about which corpus they are on.
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

DATA_DIR = Path(os.getenv("FINHELM_DATA_DIR") or (ROOT / "data"))
PROCESSED = DATA_DIR / "processed"
INDEX_DIR = DATA_DIR / "index"

__all__ = ["ROOT", "DATA_DIR", "PROCESSED", "INDEX_DIR"]
