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


__all__ = ["decompose"]
