"""Merge the three raw sources into one corpus and deduplicate.

Retrieval and citation should not care which source a passage came from, so every
ingester already emits the same record shape. This step concatenates them, enforces
that shape, drops duplicates, and reports what it dropped.

8-Ks repeat boilerplate constantly (the same cover page, the same forward-looking
statements disclaimer), which is what the dedupe is mainly for.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
RAW_DIR = ROOT / "data" / "raw"
OUT_PATH = ROOT / "data" / "processed" / "corpus.jsonl"

SOURCES = ["edgar.jsonl", "fomc.jsonl", "cfpb_narratives.jsonl"]
FIELDS = ("doc_id", "source", "ticker", "form", "date", "section", "url", "text")

DEDUPE_PREFIX = 500
MIN_CHARS = 200
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def chunk_key(rec: dict) -> str:
    """Stable, greppable identity for a record: doc_id plus section."""
    return f"{rec['doc_id']}_{rec['section']}"


def _fingerprint(text: str) -> str:
    """Hash of the full normalized text.

    Deliberately NOT a prefix hash. Hashing the first 500 characters looks like a
    cheap way to catch 8-K boilerplate, but SEC sections open with identical
    stock language every year — "The following discussion sets forth the material
    risk factors..." — so a prefix hash silently collapses three years of Risk
    Factors into one and destroys every temporal comparison in the golden set.
    Only exact duplicates are safe to drop automatically.
    """
    return hashlib.sha256(" ".join(text.lower().split()).encode()).hexdigest()


def _prefix(text: str) -> str:
    return hashlib.sha256(
        " ".join(text.lower().split())[:DEDUPE_PREFIX].encode()
    ).hexdigest()


def load_raw() -> list[dict]:
    rows = []
    for name in SOURCES:
        path = RAW_DIR / name
        if not path.exists():
            print(f"  missing {name} — skipped")
            continue
        with path.open(encoding="utf-8") as fh:
            n = 0
            for line in fh:
                rows.append(json.loads(line))
                n += 1
        print(f"  {name}: {n}")
    return rows


def main() -> None:
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    print("loading:")
    rows = load_raw()

    seen_fp: dict[str, str] = {}
    seen_key: set[str] = set()
    near_dupe_prefixes: Counter = Counter()
    dropped = Counter()
    kept: list[dict] = []

    for rec in rows:
        if set(rec) != set(FIELDS):
            dropped["schema_mismatch"] += 1
            continue
        if not rec["text"] or len(rec["text"]) < MIN_CHARS:
            dropped["too_short"] += 1
            continue
        if not DATE_RE.match(rec["date"] or ""):
            dropped["bad_date"] += 1
            continue

        key = chunk_key(rec)
        if key in seen_key:
            dropped["duplicate_key"] += 1
            continue

        fp = _fingerprint(rec["text"])
        if fp in seen_fp:
            dropped["duplicate_text"] += 1
            continue

        seen_key.add(key)
        seen_fp[fp] = key
        near_dupe_prefixes[_prefix(rec["text"])] += 1
        kept.append(rec)

    with OUT_PATH.open("w", encoding="utf-8") as out:
        for rec in kept:
            out.write(json.dumps(rec) + "\n")

    print(f"\nkept {len(kept)} / {len(rows)} records -> {OUT_PATH}")
    print(f"dropped: {dict(dropped)}")
    print("\nby source:", dict(Counter(r["source"] for r in kept)))
    print("by form:  ", dict(Counter(r["form"] for r in kept)))
    total_chars = sum(len(r["text"]) for r in kept)
    print(f"total text: {total_chars / 1e6:.1f} MB")

    shared = sum(v - 1 for v in near_dupe_prefixes.values() if v > 1)
    print(
        f"\nkept records sharing a {DEDUPE_PREFIX}-char opening: {shared} "
        "(retained on purpose — mostly year-over-year SEC boilerplate)"
    )


if __name__ == "__main__":
    main()
