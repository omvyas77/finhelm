"""Contextual headers prepended to a chunk *before it is embedded*.

The largest single failure bucket measured earlier was "right document, wrong passage":
48% of missed gold spans came from a filing that retrieval had already surfaced, just not
from the paragraph holding the answer. The cause is visible in the chunks themselves. A
median semantic chunk is 196 words of the form

    "increased 21 percent, primarily driven by growth in premium card portfolios"

which carries no indication of issuer, form, period or section. Every large-bank filing
contains dozens of near-identical sentences, so the embedding has nothing to separate
JPMorgan's 2024 MD&A from Citigroup's 2023 risk factors, and neither does the query.

All four facts are already in the chunk metadata; they simply never reached the encoder.
Prepending them costs ~15 tokens and makes the vector encode *which* passage this is.

Two deliberate constraints:

  * The header is applied at index time only, to the text handed to the embedding model.
    The stored `text` stays pristine, because it is also what BM25 indexes, what the
    generator quotes, and what `is_hit` matches gold snippets against. Rewriting it would
    make every chunk in a JPM filing match the lexical query "JPMorgan" equally well,
    which is noise rather than signal, and would quietly change what the eval measures.
  * The header goes at the front. Chunks above the model's 512-token window are truncated
    from the end, so a leading header survives on exactly the long chunks that need it.
"""

from __future__ import annotations

# Ticker -> the name a question is likely to use. Questions ask about "JPMorgan", not
# about "JPM", and the encoder has no way to connect the two on its own.
ISSUERS = {
    "JPM": "JPMorgan Chase", "BAC": "Bank of America", "C": "Citigroup",
    "WFC": "Wells Fargo", "GS": "Goldman Sachs", "COF": "Capital One",
    "SYF": "Synchrony Financial", "DFS": "Discover Financial",
    "AXP": "American Express", "USB": "U.S. Bancorp",
}

# Section slugs are grep-safe, not readable. "item_1a_risk_factors" shares no useful
# token with the way anyone phrases a question about risk factors.
SECTIONS = {
    "item_1a_risk_factors": "Item 1A, Risk Factors",
    "item_7_mda": "Item 7, Management's Discussion and Analysis",
    "item_7a_market_risk": "Item 7A, Quantitative and Qualitative Disclosures About Market Risk",
    "full_document": "",
}

FORMS = {
    "complaint": "consumer complaint",
    "10-K": "annual report (10-K)",
    "10-Q": "quarterly report (10-Q)",
    "8-K": "current report (8-K)",
    "minutes": "meeting minutes",
    "statement": "policy statement",
}


def header(row: dict) -> str:
    """One line naming who, what, when and where within the document."""
    form = FORMS.get(row.get("form"), row.get("form") or "filing")
    date = row.get("date") or "undated"

    if row.get("source") == "cfpb":
        # Complaints carry no issuer at all — the CFPB public set strips the company — but
        # `section` holds the product category ("Credit card", "Mortgage"), which is the
        # single most useful discriminator this collection has and the thing a question
        # about overdraft fees needs to match on.
        product = (row.get("section") or "").strip().lower()
        about = f" about a {product}" if product else ""
        return f"Consumer complaint to the CFPB{about}, {date}."

    if row.get("source") == "fomc":
        who = "Federal Open Market Committee"
    else:
        ticker = row.get("ticker")
        name = ISSUERS.get(ticker)
        # An unmapped ticker still beats nothing: fall back to the symbol rather than
        # dropping the issuer entirely, so a new filer added later degrades quietly
        # instead of silently losing its identity from every one of its vectors.
        who = f"{name} ({ticker})" if name else (ticker or "Unknown issuer")

    section = SECTIONS.get(row.get("section"), (row.get("section") or "").replace("_", " "))
    where = f", {section}" if section else ""
    return f"{who} {form}, {date}{where}."


def contextualize(text: str, row: dict) -> str:
    return f"{header(row)}\n{text}"
