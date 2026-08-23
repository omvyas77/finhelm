"""Deterministic metrics — no LLM calls, no network, no cost.

These gate every push. The LLM-judged metrics (Ragas, DeepEval) are better at catching
subtle unfaithfulness but cost money and take minutes, so they run less often; everything
in this file runs in milliseconds and catches most regressions.

--------------------------------------------------------------------------------------
Why ground truth is anchored on (doc_id, snippet) and NOT on chunk_id
--------------------------------------------------------------------------------------
The build guide's golden-set schema stores `expected_chunk_ids`. That does not survive
contact with the chunking ablation, and it fails *silently*, which is worse than failing.

chunk_id is `{doc_id}_{section}_{NNN}` where NNN is the chunk's position within its
strategy. The same id therefore exists under all three strategies and points at
completely different text:

    JPM_10K_2026-02-13_008131_item_1a_risk_factors_000
      fixed            4,566 chars  ". The following discussion sets forth the material..."
      semantic             1 char   "."
      sentence_window      1 char   "."

Scoring recall@5 by chunk_id equality across a fixed/semantic/sentence_window sweep would
compare a 4.5 KB passage against a lone period, report a plausible-looking number, and
never raise an error. Every strategy other than the one the golden set was authored
against would be scored against text it never contained.

doc_id, by contrast, is identical across all three strategies (verified: 460 documents,
set-equal). So ground truth records *which document* and *what text within it* answers the
question, and a retrieved chunk counts as a hit when it overlaps that text. This measures
the thing we actually care about — did retrieval surface the passage containing the
answer — and it means one golden set scores every strategy fairly.
"""

from __future__ import annotations

import math
import random
import re
from typing import Iterable, Sequence

# Matching is done on contiguous word n-grams, not on bag-of-words overlap.
#
# Bag-of-words was the first implementation and it was measurably wrong. Financial prose
# is formulaic — MD&A sections are full of "<line item> increased N percent, primarily
# driven by <driver>" — so unrelated sentences from the same filing share most of their
# vocabulary. Scoring a real gold span against one document's sentence_window chunks,
# 18 chunks cleared an overlap-coefficient threshold of 0.6 and only 6 of them actually
# contained the passage. Among the false positives were the *same sentence for a
# different quarter* ("increased 18 percent" vs the gold "increased 21 percent"), which
# is not merely a near-miss but a different fact.
#
# The bias is what makes this fatal rather than noisy: sentence_window produces ~15x more
# chunks per document than fixed, so it accumulates disproportionately many false
# positives, and the chunking ablation would have crowned it the winner on a measurement
# artifact. A contiguous run of N words cannot be assembled from formulaic vocabulary;
# it has to actually be there.
_WORD = re.compile(r"[a-z0-9]+")

# 10 words is long enough that boilerplate cannot reach it by coincidence (the false
# positives above topped out at 9-word runs) and short enough to survive a chunk boundary
# cutting the gold span in half.
NGRAM = 10


def _words(text: str) -> list[str]:
    return _WORD.findall(text.lower())


def _ngrams(words: list[str], n: int) -> set[tuple[str, ...]]:
    if len(words) <= n:
        return {tuple(words)} if words else set()
    return {tuple(words[i : i + n]) for i in range(len(words) - n + 1)}


def overlap(a: str, b: str) -> float:
    """Fraction of b's n-grams present in a, i.e. how much of the gold span the chunk holds.

    Reported for diagnostics and threshold tuning; `is_hit` only needs to know whether it
    is above zero. Asymmetric on purpose — the question is always "how much of the gold
    passage did this chunk capture", never the reverse.
    """
    wa, wb = _words(a), _words(b)
    if not wa or not wb:
        return 0.0
    n = min(NGRAM, len(wb))
    gold_grams = _ngrams(wb, n)
    if not gold_grams:
        return 0.0
    return len(gold_grams & _ngrams(wa, n)) / len(gold_grams)


_NUMBER = re.compile(r"\d+(?:\.\d+)?")


def _numbers(text: str) -> set[str]:
    # Commas are thousands separators in filings; stripping them makes "1,234" and "1234"
    # the same figure. Trailing ".0" is normalised so "5" and "5.0" do not look different.
    return {n.rstrip("0").rstrip(".") if "." in n else n
            for n in _NUMBER.findall(text.replace(",", ""))}


def _contradicts(chunk_text: str, snippet: str) -> bool:
    """True when both texts state figures and share none of them.

    An n-gram match alone cannot separate "Net card fees increased 21 percent, primarily
    driven by growth in our premium card portfolios" from the identical sentence for a
    different quarter reading 18 percent: the differing figure sits outside the matched
    window, so a 10-word run still lines up. In filings the number *is* the fact, so two
    passages that both quantify and agree on nothing are about different periods.

    A chunk with no figures at all is not treated as contradicting — that is the shape of
    a chunk boundary splitting the gold span, where the prose landed in one chunk and the
    figures in the next, and it should still count as partial evidence.
    """
    chunk_nums, gold_nums = _numbers(chunk_text), _numbers(snippet)
    return bool(chunk_nums) and bool(gold_nums) and not (chunk_nums & gold_nums)


def is_hit(chunk: dict, gold: dict, threshold: float = 0.0) -> bool:
    """Does this retrieved chunk contain the gold passage?

    Three conditions, each closing a false-positive route found empirically:

      1. doc_id matches exactly — ~2% of filing chunks are byte-identical footnote
         boilerplate repeated across filers, so text matching alone would manufacture
         hits from the wrong company;
      2. a contiguous run of NGRAM words is shared (or the whole snippet, when it is
         shorter) — formulaic MD&A vocabulary cannot reach that by coincidence;
      3. the figures do not contradict — see _contradicts.

    `threshold` is the fraction of the gold span's n-grams the chunk must hold. The
    default 0.0 means "any contiguous run counts", which is what recall should measure
    when a chunk boundary legitimately splits the span across two retrieved chunks.
    """
    if chunk.get("doc_id") != gold["doc_id"]:
        return False
    text = chunk.get("text", "")
    if _contradicts(text, gold["snippet"]):
        return False
    score = overlap(text, gold["snippet"])
    return score > threshold if threshold == 0.0 else score >= threshold


def recall_at_k(retrieved: Sequence[dict], gold_spans: Sequence[dict], k: int,
                threshold: float = 0.0) -> float | None:
    """Fraction of gold spans found in the top k.

    Multi-hop questions carry 2+ spans, usually in different documents, and a system that
    retrieves both halves of a comparison is meaningfully better than one that retrieves
    a single side confidently. Averaging over spans rather than scoring 0/1 per question
    is what makes that visible.
    """
    if not gold_spans:
        return None
    top = retrieved[:k]
    found = sum(1 for g in gold_spans if any(is_hit(c, g, threshold) for c in top))
    return found / len(gold_spans)


def mrr(retrieved: Sequence[dict], gold_spans: Sequence[dict],
        threshold: float = 0.0) -> float | None:
    """Reciprocal rank of the first gold span found.

    Reported alongside recall because they fail differently: recall says whether the
    evidence was in the context window at all, MRR says whether it was near the top.
    A reranker that reorders without adding anything moves MRR and leaves recall flat,
    which is exactly the signal 2.8 is trying to isolate.
    """
    if not gold_spans:
        return None
    for rank, chunk in enumerate(retrieved, start=1):
        if any(is_hit(chunk, g, threshold) for g in gold_spans):
            return 1.0 / rank
    return 0.0


_CITATION = re.compile(r"\[S(\d+)\]")

# A "claim" is a sentence long enough to assert something. Markdown headings, list
# bullets and the short connective lines the model writes between sections are not
# claims, and counting them as uncited claims understates citation density on exactly
# the well-structured answers we want to reward. (Day 1 shipped without this filter and
# every heading in a well-formatted answer scored as an unsourced assertion.)
_HEADING = re.compile(r"^\s*(?:#{1,6}\s|\*{1,2}[^*]+\*{1,2}\s*:?\s*$|[-*+]\s*$|\d+[.)]\s*$)")


def _claim_sentences(answer: str) -> list[str]:
    out = []
    for raw in re.split(r"(?<=[.!?])\s+|\n+", answer):
        s = raw.strip()
        if len(s.split()) > 4 and not _HEADING.match(s):
            out.append(s)
    return out


def citation_validity(answer: str, n_sources: int) -> float | None:
    """Fraction of [S#] markers pointing at a source that actually existed.

    Catches fabricated citations — the failure mode users never notice, because an
    invented [S7] looks exactly as authoritative as a real one.
    """
    cited = set(_CITATION.findall(answer))
    if not cited:
        return None
    return sum(1 for c in cited if 1 <= int(c) <= n_sources) / len(cited)


def citation_density(answer: str) -> float:
    """Citations per claim sentence. Low density = confident unsourced assertions."""
    claims = _claim_sentences(answer)
    return len(_CITATION.findall(answer)) / max(len(claims), 1)


def uncited_claims(answer: str) -> int:
    """Count of claim sentences carrying no citation marker at all."""
    return sum(1 for s in _claim_sentences(answer) if not _CITATION.search(s))


# ---------------------------------------------------------------------------------
# Uncertainty
# ---------------------------------------------------------------------------------
# Day 2 ranked 18 configurations on a golden set holding 74 gold spans and reported
# differences as small as 0.01 as if they were results. At p ~ 0.39 the standard error
# on recall is sqrt(.39*.61/74) ~ 0.057, so the 95% interval on the headline number is
# roughly +/- 0.11 — wide enough to contain the top six rows of that table. The ranking
# was mostly noise with an ordering printed on it.
#
# Two things fix that, and both are reported rather than left to the reader:
#
#   * a Wilson interval on every rate, which unlike the normal approximation stays inside
#     [0, 1] and behaves at the small counts this set actually has;
#   * a *paired* bootstrap for comparing two runs. Independent intervals overlapping is
#     not evidence of no difference when both configurations answered the identical
#     questions; pairing removes the question-difficulty variance that dominates here and
#     is a far more sensitive test.
#
# Gold spans are also correlated within a question — a multi-hop question contributes two
# spans that succeed or fail together more often than chance — so the bootstrap resamples
# *questions*, not spans. Resampling spans would understate the interval.


def wilson(successes: float, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson score interval for a rate. Returns (low, high)."""
    if n == 0:
        return (0.0, 0.0)
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def bootstrap_paired(a: Sequence[float], b: Sequence[float], rounds: int = 10000,
                     seed: int = 0) -> dict:
    """Paired bootstrap over per-question scores from two runs.

    `a` and `b` must be aligned: element i is the same question under both configs.
    Returns the observed difference, its 95% interval, and the fraction of resamples in
    which b beat a. An interval containing 0 means the runs are indistinguishable on this
    golden set — which is a finding about the golden set as much as about the configs.
    """
    if len(a) != len(b):
        raise ValueError(f"unpaired inputs: {len(a)} vs {len(b)}")
    pairs = [(x, y) for x, y in zip(a, b) if x is not None and y is not None]
    if not pairs:
        return {"delta": None, "low": None, "high": None, "p_better": None, "n": 0}

    rng = random.Random(seed)
    n = len(pairs)
    observed = sum(y - x for x, y in pairs) / n
    deltas = []
    for _ in range(rounds):
        sample = [pairs[rng.randrange(n)] for _ in range(n)]
        deltas.append(sum(y - x for x, y in sample) / n)
    deltas.sort()
    return {
        "delta": observed,
        "low": deltas[int(0.025 * rounds)],
        "high": deltas[int(0.975 * rounds)],
        "p_better": sum(1 for d in deltas if d > 0) / rounds,
        "n": n,
    }


def per_question_recall(records: Sequence[dict], k: int = 5,
                        threshold: float = 0.0) -> dict[str, float]:
    """Recall per question id — the aligned input bootstrap_paired needs."""
    return {r["id"]: recall_at_k(r["retrieved"], r.get("gold_spans", []), k, threshold)
            for r in records if r.get("gold_spans")}


NEGATIVE_TYPES = ("unanswerable", "out_of_scope")
ABSTAIN_PREFIX = "INSUFFICIENT_CONTEXT"


def refused(record: dict) -> bool:
    return record.get("answer", "").strip().startswith(ABSTAIN_PREFIX)


def abstention(records: Iterable[dict]) -> dict[str, float]:
    """Report BOTH directions.

    A system that refuses everything scores 1.0 on abstention_recall and catastrophically
    on over_refusal_rate. Reporting either number alone is how RAG demos claim to have
    "solved hallucination". The pair is the actual result, and choosing where to sit on
    that tradeoff is a product decision, not a tuning artifact.
    """
    records = list(records)
    neg = [r for r in records if r.get("type") in NEGATIVE_TYPES]
    pos = [r for r in records if r.get("type") not in NEGATIVE_TYPES]
    return {
        "abstention_recall": sum(map(refused, neg)) / max(len(neg), 1),
        "over_refusal_rate": sum(map(refused, pos)) / max(len(pos), 1),
        "n_negative": float(len(neg)),
        "n_positive": float(len(pos)),
    }


def _as_set(value) -> set:
    return set(value) if isinstance(value, (list, tuple, set)) else {value}


def route_accuracy(records: Iterable[dict]) -> float | None:
    """Did the router pick the right collection(s)?

    Compared as sets: for a comparative question the correct answer is both collections,
    and a router that picks one of the two is wrong even though it overlaps the target.
    That specific failure — answering half a comparison confidently while citing only one
    side — is the worst one this system produced on Day 1, so it must not earn partial
    credit here.
    """
    scored = [r for r in records if r.get("expected_source")]
    if not scored:
        return None
    hits = sum(1 for r in scored if _as_set(r["route"]) == _as_set(r["expected_source"]))
    return hits / len(scored)


def aggregate(records: Sequence[dict], k: int = 5, threshold: float = 0.0) -> dict:
    """Roll per-question records up into the metric row logged to MLflow.

    Negatives are excluded from retrieval metrics by construction: `gold_spans` is empty
    for them, recall_at_k/mrr return None, and None is dropped from the mean. An
    unanswerable question has no passage to retrieve, so scoring retrieval on it would
    punish the system for correctly finding nothing.
    """
    def mean(values):
        vals = [v for v in values if v is not None]
        return sum(vals) / len(vals) if vals else None

    # Span-level counts for the interval: recall is a rate over gold spans, so the
    # interval must be computed on spans found / spans total, not on the mean of
    # per-question means (which weights a 2-span question the same as a 1-span one).
    spans = [(g, r) for r in records for g in r.get("gold_spans", [])]
    found = sum(1 for g, r in spans if any(is_hit(c, g, threshold) for c in r["retrieved"][:k]))
    low, high = wilson(found, len(spans))

    return {
        f"recall_at_{k}_ci_low": low,
        f"recall_at_{k}_ci_high": high,
        "n_gold_spans": float(len(spans)),
        f"recall_at_{k}": mean(
            recall_at_k(r["retrieved"], r.get("gold_spans", []), k, threshold)
            for r in records
        ),
        "mrr": mean(mrr(r["retrieved"], r.get("gold_spans", []), threshold) for r in records),
        "citation_validity": mean(
            citation_validity(r["answer"], len(r["retrieved"])) for r in records
        ),
        "citation_density": mean(citation_density(r["answer"]) for r in records),
        "uncited_claims": mean(float(uncited_claims(r["answer"])) for r in records),
        "route_accuracy": route_accuracy(records),
        **abstention(records),
    }
