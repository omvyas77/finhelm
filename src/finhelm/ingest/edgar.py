"""SEC EDGAR ingestion — 10-K / 10-Q / 8-K for ten financial-sector companies.

Rate limits: SEC blocks IPs that exceed 10 req/s for ~10 minutes, and returns 403
for a missing or generic User-Agent. Both are handled here; do not bypass either.

Every fetch is cached to disk keyed by accession number, so re-runs while debugging
the chunker cost zero requests.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv
from selectolax.parser import HTMLParser

load_dotenv()

TICKERS = ["JPM", "BAC", "C", "WFC", "GS", "COF", "SYF", "DFS", "AXP", "USB"]

# Tickers that have left SEC's live company_tickers.json but whose historical
# filings we still want. DFS was acquired by Capital One in May 2025; its
# pre-merger filings are the other half of several comparative questions.
DELISTED_CIKS = {"DFS": "0001393612"}

THROTTLE = 0.15  # ~6.7 req/s, under the 10/s ceiling
HEADERS = {
    "User-Agent": os.environ["SEC_USER_AGENT"],
    "Accept-Encoding": "gzip, deflate",
}

ROOT = Path(__file__).resolve().parents[3]
CACHE_DIR = ROOT / "data" / "cache" / "edgar"
OUT_PATH = ROOT / "data" / "raw" / "edgar.jsonl"

# How far back to go per form type.
N_10K = 3  # most recent 3 annual reports
N_10Q = 6  # most recent 6 quarterly reports
DAYS_8K = 548  # 18 months of current reports

_last_request = 0.0


def _get(url: str) -> requests.Response:
    """Throttled GET. Retries once on 429/503 with a long backoff."""
    global _last_request
    for attempt in range(2):
        elapsed = time.monotonic() - _last_request
        if elapsed < THROTTLE:
            time.sleep(THROTTLE - elapsed)
        _last_request = time.monotonic()
        r = requests.get(url, headers=HEADERS, timeout=30)
        if r.status_code in (429, 503) and attempt == 0:
            time.sleep(10)
            continue
        r.raise_for_status()
        return r
    r.raise_for_status()
    return r


def cik_map() -> dict[str, str]:
    """ticker -> zero-padded 10-digit CIK."""
    data = _get("https://www.sec.gov/files/company_tickers.json").json()
    return {v["ticker"]: f'{v["cik_str"]:010d}' for v in data.values()}


@dataclass(frozen=True)
class Filing:
    ticker: str
    cik: str
    form: str
    date: str
    accession: str
    primary_doc: str

    @property
    def url(self) -> str:
        return (
            f"https://www.sec.gov/Archives/edgar/data/{int(self.cik)}/"
            f"{self.accession.replace('-', '')}/{self.primary_doc}"
        )

    @property
    def doc_id(self) -> str:
        # The accession sequence is required for uniqueness: companies file several
        # 8-Ks on the same date, so ticker+form+date collides.
        seq = self.accession.split("-")[-1]
        return f"{self.ticker}_{self.form.replace('-', '')}_{self.date}_{seq}"


def _submission_pages(cik: str) -> list[dict]:
    """`filings.recent` plus any overflow pages.

    `recent` caps at ~1000 filings. Banks file 8-Ks constantly, so for the
    high-volume tickers three years of 10-Ks can fall off the end of it.
    """
    data = _get(f"https://data.sec.gov/submissions/CIK{cik}.json").json()
    pages = [data["filings"]["recent"]]
    for extra in data["filings"].get("files", []):
        pages.append(_get(f"https://data.sec.gov/submissions/{extra['name']}").json())
    return pages


def list_filings(ticker: str, cik: str) -> list[Filing]:
    """All in-scope filings for one company, newest first."""
    rows: list[Filing] = []
    for page in _submission_pages(cik):
        for form, acc, doc, filed in zip(
            page["form"], page["accessionNumber"], page["primaryDocument"], page["filingDate"]
        ):
            if form in ("10-K", "10-Q", "8-K") and doc:
                rows.append(Filing(ticker, cik, form, filed, acc, doc))

    rows.sort(key=lambda f: f.date, reverse=True)
    cutoff_8k = (date.today() - timedelta(days=DAYS_8K)).isoformat()

    tenk = [f for f in rows if f.form == "10-K"][:N_10K]
    tenq = [f for f in rows if f.form == "10-Q"][:N_10Q]
    eightk = [f for f in rows if f.form == "8-K" and f.date >= cutoff_8k]
    return tenk + tenq + eightk


def _fetch(url: str, cache_key: str) -> str:
    """Fetch a URL, caching to disk under `cache_key`."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cached = CACHE_DIR / f"{cache_key}.html"
    if cached.exists():
        return cached.read_text(encoding="utf-8", errors="ignore")
    html = _get(url).text
    cached.write_text(html, encoding="utf-8")
    return html


def fetch_html(f: Filing) -> str:
    """Fetch the primary document, caching to disk by accession number."""
    return _fetch(f.url, f.accession)


def exhibit_13_urls(f: Filing) -> list[str]:
    """URLs of any EX-13 (annual report) exhibits attached to this filing.

    WFC and USB file a thin 10-K wrapper whose items say only "information in
    response to this item can be found in the annual report." The substance lives
    in Exhibit 13, so without this the corpus holds a ~74K-char stub in place of a
    ~1M-char annual disclosure.
    """
    base = f"https://www.sec.gov/Archives/edgar/data/{int(f.cik)}/{f.accession.replace('-', '')}"
    tree = HTMLParser(_fetch(f"{base}/{f.accession}-index.htm", f"{f.accession}_index"))
    urls = []
    for row in tree.css("tr"):
        cells = [c.text().strip() for c in row.css("td")]
        link = row.css_first("a")
        if len(cells) >= 4 and cells[3].upper().startswith("EX-13") and link:
            href = link.attributes.get("href", "")
            href = href.split("doc=", 1)[-1]  # strip the /ix?doc= inline-XBRL viewer prefix
            if href:
                urls.append(f"https://www.sec.gov{href}")
    return urls


def fetch_10k_text(f: Filing) -> str:
    """Primary document text, plus any EX-13 annual report appended."""
    text = to_text(fetch_html(f))
    for i, url in enumerate(exhibit_13_urls(f)):
        try:
            text += " " + to_text(_fetch(url, f"{f.accession}_ex13_{i}"))
        except requests.HTTPError as e:
            print(f"  ex-13 fetch failed for {f.accession}: {e}", flush=True)
    return text


_IX_HIDDEN = re.compile(r"(?is)<ix:hidden\b.*?</ix:hidden>")


def to_text(html: str) -> str:
    """Strip markup. Dense numeric tables become sludge in plain text, so drop them."""
    # Inline-XBRL filings carry a hidden block of tagged facts — CIK, axis members,
    # repeated dates, the company name. It renders as nothing but survives text
    # extraction as a short, keyword-dense chunk, which BM25's length normalisation then
    # ranks *above* real prose for any query naming a company.
    #
    # Filers hide it two different ways and only one is reachable from CSS: selectolax
    # cannot select namespaced tags (`ix\:hidden` silently matches zero nodes), so the
    # <ix:hidden> form has to come out of the raw markup before parsing.
    html = _IX_HIDDEN.sub("", html)

    tree = HTMLParser(html)
    for tag in tree.css("script, style"):
        tag.decompose()
    for hidden in tree.css('[style*="display:none"], [style*="display: none"]'):
        hidden.decompose()

    for tbl in tree.css("table"):
        if sum(c.isdigit() for c in tbl.text()) > 200:
            tbl.decompose()

    # separator=" " is load-bearing: selectolax joins block-level text with no delimiter,
    # so "UNITED STATES</div><div>SECURITIES" extracts as "STATESSECURITIES" — a token no
    # query will match and no tokenizer can split back apart.
    return _tighten(" ".join(tree.text(separator=" ").split()))


# Filers put the currency symbol, the digits, and the closing paren of a negative in
# separate inline elements, so the separator that fixes glued words also splits every
# money figure: "$1.2" becomes "$ 1.2" and "(1,234)" becomes "( 1,234 )". Left alone this
# costs BM25 the exact dollar amounts it is in the pipeline to match.
_TIGHTEN = [
    (re.compile(r"([$(])\s+(?=[\d.(])"), r"\1"),  # "$ 1.2" / "( 1,234" / "$ (9)"
    (re.compile(r"(?<=[\d.])\s+([%)])"), r"\1"),  # "5.25 %" / "1,234 )"
]


def _tighten(text: str) -> str:
    for pattern, repl in _TIGHTEN:
        text = pattern.sub(repl, text)
    return text


PATTERNS = {
    "item_1a_risk_factors": r"item\s*1a[\.\s\-–—:]*risk\s*factors(.*?)item\s*1b",
    "item_7_mda": r"item\s*7[\.\s\-–—:]*management.{0,10}s\s*discussion(.*?)item\s*7a",
    "item_7a_market_risk": r"item\s*7a[\.\s\-–—:]*quantitative(.*?)item\s*8",
}


MIN_SECTION_CHARS = 5000


def sections(text: str) -> dict[str, str]:
    """Split a 10-K into named items; fall back to the whole document.

    Every item heading appears at least twice: once in the table of contents and
    once at the real section. The TOC match comes first and captures only a page
    range, so we scan all matches and keep the longest — the body always dwarfs
    the TOC entry.
    """
    low, out = text.lower(), {}
    for name, pat in PATTERNS.items():
        best = max(
            (m for m in re.finditer(pat, low, re.S)),
            key=lambda m: len(m.group(1)),
            default=None,
        )
        if best and len(best.group(1)) > MIN_SECTION_CHARS:
            out[name] = text[best.start(1) : best.end(1)]
    return out or {"full_document": text}


def main() -> None:
    ciks = cik_map()
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    n_docs = 0
    tenk_total = tenk_parsed = 0
    form_counts: dict[str, int] = {}

    with OUT_PATH.open("w", encoding="utf-8") as out:
        for ticker in TICKERS:
            cik = ciks.get(ticker) or DELISTED_CIKS.get(ticker)
            if cik is None:
                print(f"{ticker}: not in SEC ticker map and no CIK override — skipped", flush=True)
                continue
            filings = list_filings(ticker, cik)
            print(f"{ticker}: {len(filings)} filings", flush=True)

            for f in filings:
                try:
                    text = fetch_10k_text(f) if f.form == "10-K" else to_text(fetch_html(f))
                except requests.HTTPError as e:
                    print(f"  skip {f.accession} ({f.form}): {e}", flush=True)
                    continue
                if len(text) < 500:
                    continue

                if f.form == "10-K":
                    tenk_total += 1
                    parts = sections(text)
                    if "full_document" not in parts:
                        tenk_parsed += 1
                else:
                    parts = {"full_document": text}

                for section, body in parts.items():
                    out.write(
                        json.dumps(
                            {
                                "doc_id": f.doc_id,
                                "source": "edgar",
                                "ticker": f.ticker,
                                "form": f.form,
                                "date": f.date,
                                "section": section,
                                "url": f.url,
                                "text": body,
                            }
                        )
                        + "\n"
                    )
                    n_docs += 1
                form_counts[f.form] = form_counts.get(f.form, 0) + 1

    rate = tenk_parsed / tenk_total if tenk_total else 0.0
    print(f"\nwrote {n_docs} documents -> {OUT_PATH}")
    print(f"filings by form: {form_counts}")
    print(
        f"10-K section extraction: {tenk_parsed}/{tenk_total} ({rate:.0%})"
        " — rest fall back to full_document"
    )


if __name__ == "__main__":
    main()
