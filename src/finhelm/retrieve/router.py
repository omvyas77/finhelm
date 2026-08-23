"""Decide which collection(s) a query should hit.

Keyword heuristic first, LLM only when the heuristic is ambiguous. Most finance queries
are lexically obvious ("CET1 ratio" is never a complaint; "debt collector called me at
work" is never a 10-K), so paying a network round trip on every request to learn that
would be a poor trade.

The returned `Route` carries *why* it decided, not just what. That field becomes a trace
span attribute on Day 3 and the debugging surface when an answer is wrong — "the answer
was bad" and "the router never looked at the right collection" are different bugs and
you cannot tell them apart after the fact without this.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..llm import ROUTER_MODEL, claude

FILINGS_TERMS = {
    "10-k", "10k", "10-q", "10q", "8-k", "8k", "filing", "filings", "annual report",
    "quarterly", "fiscal", "sec", "item 1a", "risk factor", "risk factors", "md&a",
    "cet1", "tier 1", "capital ratio", "basel", "liquidity coverage", "lcr",
    "net interest margin", "nim", "net interest income", "provision", "charge-off",
    "charge-offs", "allowance", "acl", "cecl", "book value", "tangible", "buyback",
    "repurchase", "dividend", "eps", "earnings per share", "revenue", "guidance",
    "segment", "cre", "commercial real estate", "loan book", "deposit", "deposits",
    "stress test", "ccar", "fomc", "federal funds", "federal open market", "fed funds",
    "monetary policy", "rate hike", "rate cut", "balance sheet runoff", "qt",
    "dot plot", "inflation target", "policy statement",
}

COMPLAINTS_TERMS = {
    "complaint", "complain", "complained", "complaining", "cfpb",
    "dispute", "disputed", "late fee", "overdraft", "credit report", "credit bureau",
    "collector", "debt collection", "harass", "harassed", "harassment",
    "servicer", "servicing", "escrow", "foreclosure", "chargeback",
    "unauthorized", "fraud alert", "identity theft", "billing error", "customer service",
    "grievance",
}


@dataclass(frozen=True)
class Route:
    collections: list[str]  # subset of ["filings", "complaints"]
    method: str  # heuristic | llm | llm_failed
    reason: str


def _compile(terms: set[str]) -> re.Pattern:
    """Whole-token matching, with an optional plural. A plain substring test is wrong
    here: the three-letter finance abbreviations ("cre", "sec", "eps") are all substrings
    of common words, so `"cre" in "credit card"` silently routes complaint questions into
    filings. Whole-token alone is too strict in the other direction — real questions say
    "late fees", not "late fee" — hence the trailing `s?`."""
    alternation = "|".join(re.escape(t) for t in sorted(terms, key=len, reverse=True))
    return re.compile(rf"(?<!\w)(?:{alternation})s?(?!\w)")


_FILINGS_RE = _compile(FILINGS_TERMS)
_COMPLAINTS_RE = _compile(COMPLAINTS_TERMS)


def _score(query: str, pattern: re.Pattern) -> int:
    return len(set(pattern.findall(query.lower())))


# A comparison needs both sides of the corpus even when only one side is named. "Do banks
# discuss late fees differently than consumers complain about them?" matches the complaints
# vocabulary twice and the filings vocabulary zero times, so keyword counting routes it to
# complaints — and the model then answers "how banks discuss late fees" while citing only
# consumer narratives. Confidently answering half a question is worse than being slow, so
# comparative phrasing overrides a one-sided keyword verdict.
# Inflections matter here and the first version dropped two of them. `compared? (?:to|with)`
# never matched "Comparing X and Y" — the participle is not covered by the optional "d",
# and the phrasing carries no "to"/"with" for it to anchor on. `contrast` failed the same
# way: the trailing (?!\w) boundary rejects "contrasting" because the next character is a
# word character. Both are among the commonest ways to phrase a comparison, and a missed
# match here routes a two-sided question at one collection, which is the exact failure this
# pattern exists to prevent.
_COMPARATIVE = re.compile(
    r"(?<!\w)(?:versus|vs\.?|compar(?:e|es|ed|ing|ison|isons)(?:\s+(?:to|with))?|"
    r"differ(?:ently|ence)?s?|contrast(?:s|ed|ing)?|both sides|discrepan(?:cy|cies)|"
    r"align|match(?:es)? up|consistent with|same (?:as|way))(?!\w)"
)


def _heuristic(query: str) -> Route | None:
    filings = _score(query, _FILINGS_RE)
    complaints = _score(query, _COMPLAINTS_RE)

    if bool(filings) != bool(complaints) and _COMPARATIVE.search(query.lower()):
        return None  # one-sided keywords but comparative intent — let the model decide

    if filings and complaints:
        return Route(["filings", "complaints"], "heuristic",
                     f"both vocabularies present (filings={filings}, complaints={complaints})")
    if filings:
        return Route(["filings"], "heuristic", f"{filings} filings term(s), 0 complaints")
    if complaints:
        return Route(["complaints"], "heuristic", f"{complaints} complaints term(s), 0 filings")
    return None  # no signal either way — hand it to the model


_PROMPT = """Classify this question about US banking into exactly one label.

filings    - answerable from SEC 10-K/10-Q/8-K filings or FOMC policy statements
             (financials, risk disclosures, capital, strategy, monetary policy)
complaints - answerable from CFPB consumer complaint narratives
             (individual consumers' experiences with a financial product)
both       - genuinely needs evidence from each

Question: {query}

Reply with one word: filings, complaints, or both."""

_LABELS = {
    "filings": ["filings"],
    "complaints": ["complaints"],
    "both": ["filings", "complaints"],
}


def route(query: str, use_llm: bool = True) -> Route:
    decided = _heuristic(query)
    if decided:
        return decided
    if not use_llm:
        return Route(["filings", "complaints"], "heuristic", "no keyword signal; fanned out")

    try:
        answer = claude(_PROMPT.format(query=query), ROUTER_MODEL, max_tokens=8).strip().lower()
    except Exception as e:
        # A router outage must degrade to a slower answer, not to no answer.
        return Route(["filings", "complaints"], "llm_failed", f"{type(e).__name__}: {e}")

    for label, collections in _LABELS.items():
        if label in answer:
            return Route(collections, "llm", f"model said {label!r}")
    return Route(["filings", "complaints"], "llm_failed", f"unparseable reply {answer!r}")
