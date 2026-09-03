"""FOMC statements and minutes from federalreserve.gov, 2022–present.

Public domain (17 U.S.C. §105). Statements are short and dense; minutes are long and
well structured. Transcripts are skipped — they carry a ~5-year release lag, so they
are useless for the recency this corpus is built around.

The meeting date is kept as the document date even though minutes publish ~3 weeks
later, because that is the date questions refer to.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

import requests
from dotenv import load_dotenv
from selectolax.parser import HTMLParser

load_dotenv()

BASE = "https://www.federalreserve.gov"
CALENDAR = f"{BASE}/monetarypolicy/fomccalendars.htm"
SINCE = "2022-01-01"
THROTTLE = 0.2

HEADERS = {"User-Agent": os.environ["SEC_USER_AGENT"]}

ROOT = Path(__file__).resolve().parents[3]
CACHE_DIR = ROOT / "data" / "cache" / "fomc"
OUT_PATH = ROOT / "data" / "raw" / "fomc.jsonl"

STATEMENT_RE = re.compile(r"monetary(\d{8})a\.htm")
MINUTES_RE = re.compile(r"fomcminutes(\d{8})\.htm")


def _get(url: str) -> requests.Response:
    time.sleep(THROTTLE)
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    if "charset" not in r.headers.get("content-type", "").lower():
        # federalreserve.gov serves UTF-8 without declaring it; requests then falls
        # back to ISO-8859-1 and en dashes arrive as mojibake ("9 â 3 vote").
        r.encoding = "utf-8"
    return r


def _fetch(url: str, cache_key: str) -> str:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cached = CACHE_DIR / f"{cache_key}.html"
    if cached.exists():
        return cached.read_text(encoding="utf-8", errors="ignore")
    html = _get(url).text
    cached.write_text(html, encoding="utf-8")
    return html


def to_text(html: str) -> str:
    """Article body only. The full page is ~80% nav chrome and boilerplate."""
    tree = HTMLParser(html)
    for tag in tree.css("script, style"):
        tag.decompose()
    node = tree.css_first("div#article") or tree.css_first("#content") or tree.body
    # separator=" " — without it selectolax glues adjacent block elements together, so a
    # paragraph break becomes a word merge ("...to 4-1/4 percent.The Committee...").
    return " ".join(node.text(separator=" ").split())


def discover() -> list[tuple[str, str, str]]:
    """(doc_type, iso_date, url) for every statement and minutes since SINCE."""
    hrefs = {a.attributes.get("href", "") for a in HTMLParser(_get(CALENDAR).text).css("a")}
    found = []
    for href in hrefs:
        for kind, pattern in (("statement", STATEMENT_RE), ("minutes", MINUTES_RE)):
            m = pattern.search(href or "")
            if m:
                raw = m.group(1)
                iso = f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
                if iso >= SINCE:
                    found.append((kind, iso, href if href.startswith("http") else BASE + href))
    return sorted(set(found), key=lambda x: (x[1], x[0]))


def main() -> None:
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    items = discover()
    counts = {"statement": 0, "minutes": 0}
    skipped = 0

    with OUT_PATH.open("w", encoding="utf-8") as out:
        for kind, iso, url in items:
            try:
                text = to_text(_fetch(url, f"{kind}_{iso}"))
            except requests.HTTPError as e:
                print(f"  skip {kind} {iso}: {e}", flush=True)
                skipped += 1
                continue
            if len(text) < 500:
                skipped += 1
                continue

            out.write(
                json.dumps(
                    {
                        "doc_id": f"FOMC_{iso}_{kind}",
                        "source": "fomc",
                        "ticker": None,
                        "form": kind,
                        "date": iso,
                        "section": "full_document",
                        "url": url,
                        "text": text,
                    }
                )
                + "\n"
            )
            counts[kind] += 1

    print(f"wrote {sum(counts.values())} documents -> {OUT_PATH}")
    print(f"  statements: {counts['statement']}  minutes: {counts['minutes']}  skipped: {skipped}")
    if counts["statement"]:
        dates = [i[1] for i in items]
        print(f"  date range: {min(dates)} -> {max(dates)}")


if __name__ == "__main__":
    main()
