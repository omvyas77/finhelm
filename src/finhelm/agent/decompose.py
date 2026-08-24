"""Split a multi-hop question into independently retrievable sub-questions.

A comparison — "how does JPMorgan's CET1 ratio compare with Citigroup's?" — is a single
string that names two facts living in two different filings. Embedded as one vector it
lands somewhere between them and retrieves neither side cleanly. That is not a ranking
problem a reranker can repair: the query itself is the wrong shape.

Measured on the Day 2 golden set, multi_hop was the worst-performing question type, and
7 of the 15 gold spans that never reached the candidate pool at all belonged to one.

Two design constraints, both learned from the failures this project already logged:

  * Decomposition is *additive*. Sub-question hits are fused with hits for the original
    question rather than replacing them, because a decomposition that drops a facet is
    otherwise unrecoverable — and an LLM splitting a question is exactly the step most
    likely to drop one. Fusion means a bad split costs ranking, not evidence.
  * It fails open. Any error, timeout, or unparseable reply returns the original question
    unchanged. A retrieval pipeline that stops answering because a helper model returned
    prose instead of JSON is strictly worse than one that answers the un-decomposed
    question, which is what Day 2 already did successfully 41% of the time.
"""

from __future__ import annotations

import json
import re

from ..chunking.context import ISSUERS
from ..llm import ROUTER_MODEL, claude

SYSTEM = """You split financial research questions into independent sub-questions.

Rules:
1. Split ONLY if the question genuinely asks for two or more separately-retrievable
   facts (a comparison, a multi-entity question, or a change across two periods).
2. Each sub-question must stand alone: name the company, metric and period explicitly.
   "How did it change?" is useless as a retrieval query.
3. If the question asks for exactly one fact, return it unchanged as a single item.
4. Never invent entities or periods that the question did not mention.

Reply with JSON only: {"sub_questions": ["...", "..."]}"""

# A deterministic pre-check, run before the model is asked to split anything.
#
# Left ungated, decompose splits 53 of 54 golden-set questions — including ones asking for
# a single fact from a single filing, which it "splits" into near-paraphrases. That costs a
# model call, ~2.9x retrieval latency and ~7x cost per query to help the roughly one
# question in five that genuinely spans two documents. Worse, the split is not free
# accuracy: a budget divided across sub-questions that were never needed measurably hurts
# single-span questions (0.471 vs 0.559 when allocation was tried).
#
# So the question has to look genuinely multi-part before a model is consulted. The three
# signals are the ones that actually distinguish the two-span questions in the golden set:
# two issuers named, comparative phrasing, or two distinct periods.
#
# It fails toward *not* splitting. A missed split falls back to the un-decomposed query,
# which is the Day 2 behaviour and answers correctly 41% of the time; an unnecessary split
# costs latency on every question forever. On the golden set this fires on 18 of the 20
# two-span questions and skips 21 of the 34 single-span ones.

# Aliases are listed explicitly rather than derived from the company names, because
# deriving them produces generic finance vocabulary that appears in almost every question:
# "Capital One" yields "capital", "Synchrony Financial" yields "financial", "American
# Express" yields "american" and "express". Matching on those makes "what are the bank's
# capital requirements?" look like a question naming an issuer, which then trips the
# two-issuer branch of the gate and splits a single-fact question.
_ALIASES = {
    "JPM": ["jpmorgan", "jp morgan", "j.p. morgan"],
    "BAC": ["bank of america"],
    "C": ["citigroup", "citibank", "citi"],
    "WFC": ["wells fargo"],
    "GS": ["goldman sachs", "goldman"],
    "COF": ["capital one"],
    "SYF": ["synchrony"],
    "DFS": ["discover financial", "discover card"],
    "AXP": ["american express", "amex"],
    "USB": ["u.s. bancorp", "us bancorp", "bancorp"],
}
assert set(_ALIASES) == set(ISSUERS), "alias table drifted from the issuer list"


def _issuers(question: str) -> set[str]:
    """Tickers named, by symbol or by an unambiguous alias.

    Tickers are matched against the original text, case-sensitively. "C" is Citigroup and
    also the most common letter to appear alone in a lowercased financial question; "GS"
    and "USB" have the same problem in miniature. Requiring the uppercase symbol is what
    separates the ticker from the letter.
    """
    lowered = question.lower()
    found = set()
    for ticker, aliases in _ALIASES.items():
        if re.search(rf"(?<![A-Za-z]){re.escape(ticker)}(?![A-Za-z])", question):
            found.add(ticker)
            continue
        if any(re.search(rf"(?<!\w){re.escape(a)}(?!\w)", lowered) for a in aliases):
            found.add(ticker)
    return found


_YEAR = re.compile(r"\b(?:19|20)\d{2}\b")
_QUARTER = re.compile(r"\bq[1-4]\b|\b(?:first|second|third|fourth) quarter\b", re.I)


def _periods(question: str) -> int:
    years = set(_YEAR.findall(question))
    quarters = {m.group(0).lower() for m in _QUARTER.finditer(question)}
    return len(years) + len(quarters)


def worth_splitting(question: str) -> bool:
    """Does this question look like it spans more than one document?"""
    # Imported here rather than at module scope to break a cycle: retrieve/__init__ imports
    # decompose, so decompose cannot import from the retrieve package at import time. The
    # comparative pattern belongs to the router — it is the same question ("does this need
    # both sides?") asked of collections rather than of documents — and duplicating it
    # would let the two definitions drift apart, which is worse than a deferred import.
    from ..retrieve.router import _COMPARATIVE

    return (
        len(_issuers(question)) >= 2
        or bool(_COMPARATIVE.search(question.lower()))
        or _periods(question) >= 2
    )


_JSON = re.compile(r"\{.*\}", re.S)


def _parse(reply: str, limit: int) -> list[str]:
    match = _JSON.search(reply)
    if not match:
        return []
    try:
        payload = json.loads(match.group(0))
    except json.JSONDecodeError:
        return []
    subs = payload.get("sub_questions")
    if not isinstance(subs, list):
        return []
    # Deduplicate while preserving order: a model asked for two sides of a comparison
    # occasionally returns the same side twice, and issuing that query twice would double
    # its weight under rank fusion.
    seen, out = set(), []
    for item in subs:
        if isinstance(item, str) and item.strip() and item.strip() not in seen:
            seen.add(item.strip())
            out.append(item.strip())
    return out[:limit]


def decompose(question: str, max_sub_questions: int = 4,
              model: str = ROUTER_MODEL) -> list[str]:
    """Return sub-questions, or [question] when splitting is unnecessary or fails.

    The return value always includes something retrievable, so callers never need to
    handle an empty list.
    """
    if not worth_splitting(question):
        return [question]

    try:
        reply = claude(question, model, system=SYSTEM, max_tokens=512, temperature=0.0)
    except Exception:
        return [question]

    subs = _parse(reply, max_sub_questions)
    # A single sub-question means the model judged the question atomic; returning the
    # original rather than the paraphrase keeps retrieval on the user's exact wording,
    # which BM25 in particular is sensitive to.
    if len(subs) <= 1:
        return [question]
    return subs


def filters_for(question: str) -> dict | None:
    """Metadata filter implied by a question, or None when it implies nothing safe.

    Decomposition produces sub-questions that each name exactly one issuer ("What was
    American Express's ICS segment revenue in 2024?"), which makes a ticker filter both
    available and precise: it removes roughly nine tenths of the corpus as distractors
    before ranking begins, so the sub-question competes against its own issuer's filings
    rather than against every large bank's near-identical prose.

    Two restrictions, both correctness rather than caution:

    * Exactly one issuer, or no filter. Zero means nothing to filter on; two or more means
      the question spans both, and filtering to either one guarantees missing the other
      half — which is the failure this is meant to fix, not cause. The original compound
      question therefore filters to nothing by construction, which is correct: it is the
      sub-questions that are single-issuer.

    * No date filter, despite `date` being indexed and every sub-question naming a period.
      Filings routinely report prior-period figures — a 2023 number is usually stated in
      the 2024 10-K as the comparative column — so filtering to the year a question asks
      about would exclude the document that actually answers it. The gold spans confirm
      this: several questions about one year are answered by a filing dated the next.
    """
    issuers = _issuers(question)
    if len(issuers) != 1:
        return None
    return {"ticker": issuers.pop()}


__all__ = ["decompose", "worth_splitting", "filters_for"]
