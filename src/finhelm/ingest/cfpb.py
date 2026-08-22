"""CFPB consumer complaint narratives — free API, no key required.

Narratives are published only with the consumer's consent and are PII-scrubbed by CFPB
before release.

Two payloads come out of one pass:
  * `cfpb_narratives.jsonl` — text for retrieval
  * `cfpb_structured.parquet` — the full record for Day 4's disparity module

API quirks worth knowing (all discovered empirically, see notes/failures.md):
  * `format=json` returns 404. The endpoint already returns JSON.
  * Offset paging (`frm`/`from`/`offset`) is silently ignored — every page comes back
    identical. Deep paging requires the `search_after` cursor instead.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import requests

BASE = "https://www.consumerfinance.gov/data-research/consumer-complaints/search/api/v1/"
DETAIL = "https://www.consumerfinance.gov/data-research/consumer-complaints/search/detail"

PRODUCTS = [
    "Credit card",
    "Mortgage",
    "Debt collection",
    "Checking or savings account",
]
PER_PRODUCT = 5000
DATE_MIN = "2023-01-01"
DATE_MAX = "2026-06-30"  # last complete quarter

ROOT = Path(__file__).resolve().parents[3]
CACHE_DIR = ROOT / "data" / "cache" / "cfpb"
NARRATIVE_PATH = ROOT / "data" / "raw" / "cfpb_narratives.jsonl"
STRUCTURED_PATH = ROOT / "data" / "raw" / "cfpb_structured.parquet"

KEEP_FIELDS = [
    "complaint_id", "date_received", "product", "sub_product", "issue", "sub_issue",
    "company", "state", "zip_code", "company_response", "company_public_response",
    "timely", "submitted_via", "tags",
]


def quarters(start: str, end: str) -> list[tuple[str, str]]:
    """Inclusive (min, max) date strings, one per calendar quarter."""
    bounds = pd.date_range(start=start, end=end, freq="QS").tolist()
    out = []
    for q_start in bounds:
        q_end = q_start + pd.offsets.QuarterEnd(1)
        out.append((q_start.strftime("%Y-%m-%d"), q_end.strftime("%Y-%m-%d")))
    return out


def _page(product: str, date_min: str, date_max: str, size: int) -> list[dict]:
    params = {
        "field": "all",
        "has_narrative": "true",
        "date_received_min": date_min,
        "date_received_max": date_max,
        "no_aggs": "true",
        "product": product,
        "size": size,
        "sort": "created_date_desc",
    }
    r = requests.get(BASE, params=params, timeout=180)
    r.raise_for_status()
    return r.json()["hits"]["hits"]


def fetch_product(product: str) -> list[dict]:
    """PER_PRODUCT records spread evenly across quarters, cached to disk.

    Sampling has to be stratified by time. The API sorts newest-first and ignores
    offset paging, so a flat "take the first 5,000" collapses the window to the
    most recent few months regardless of `date_received_min` — which silently
    destroys every temporal question in the golden set.
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cached = CACHE_DIR / f"{product.replace(' ', '_').replace('/', '_')}.json"
    if cached.exists():
        return json.loads(cached.read_text())

    windows = quarters(DATE_MIN, DATE_MAX)
    per_window = PER_PRODUCT // len(windows) + 1

    out: list[dict] = []
    for date_min, date_max in windows:
        hits = _page(product, date_min, date_max, per_window)
        out.extend(h["_source"] for h in hits)
        print(f"  {product} {date_min[:7]}: +{len(hits)} (total {len(out)})", flush=True)

    cached.write_text(json.dumps(out))
    return out


def main() -> None:
    NARRATIVE_PATH.parent.mkdir(parents=True, exist_ok=True)

    records: dict[str, dict] = {}
    for product in PRODUCTS:
        for src in fetch_product(product):
            cid = src.get("complaint_id")
            if cid:
                records[str(cid)] = src  # dedupe across products by complaint id

    n_written = 0
    with NARRATIVE_PATH.open("w", encoding="utf-8") as out:
        for cid, src in records.items():
            text = (src.get("complaint_what_happened") or "").strip()
            if len(text) < 200:  # one-liners carry no retrievable content
                continue
            date = (src.get("date_received") or "")[:10]
            out.write(
                json.dumps(
                    {
                        "doc_id": f"CFPB_{date}_{cid}",
                        "source": "cfpb",
                        "ticker": None,
                        "form": "complaint",
                        "date": date,
                        "section": src.get("product") or "unknown",
                        "url": f"{DETAIL}/{cid}",
                        "text": text,
                    }
                )
                + "\n"
            )
            n_written += 1

    df = pd.DataFrame(
        [{k: src.get(k) for k in KEEP_FIELDS} for src in records.values()]
    )
    df["date_received"] = pd.to_datetime(df["date_received"], errors="coerce", utc=True)
    df.to_parquet(STRUCTURED_PATH, index=False)

    print(f"\nnarratives: {n_written} -> {NARRATIVE_PATH}")
    print(f"structured: {len(df)} rows -> {STRUCTURED_PATH}")
    print(f"dropped {len(records) - n_written} records with <200 chars of narrative")
    print("\nby product:")
    print(df["product"].value_counts().to_string())


if __name__ == "__main__":
    main()
