"""Draft golden-set questions from sampled corpus chunks.

Provenance: LLM-drafted, human-verified, with hand-written negatives. This script does the
drafting half only. `evals/golden_set.jsonl` is not valid until a human has read every row
and the negatives have been written by hand — LLMs are bad at inventing plausible-but-absent
facts, which is exactly what the 21 negatives need to be.

Two properties of the output are enforced here rather than left to review:

1. **The snippet must be verbatim from the sampled chunk.** Ground truth is scored by text
   overlap against the source document, so a snippet the model paraphrased would mark the
   correct passage as a miss and silently depress recall for every configuration equally —
   a bias invisible in the ablation table because it moves every row together.

2. **Questions are drafted from `fixed` chunks but grounded on (doc_id, snippet).** The
   chunk the question came from is recorded for review only. Scoring never uses chunk_id,
   because the same id means different text under each chunking strategy (see
   evals/metrics.py).
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from evals.metrics import NGRAM, _WORD  # noqa: E402
from finhelm.llm import claude  # noqa: E402

PROCESSED = ROOT / "data" / "processed"

# Drafting uses the generation model's family by default, which is a known and accepted
# weakness: a model tends to write questions phrased the way it likes to be asked. The
# human verification pass is the mitigation, and --model lets the whole set be redrafted
# by a different family to check how much it matters.
DRAFT_MODEL = "claude-opus-4-6"

# Rounds of re-pairing when sampling cross-entity pairs; see sample_pairs.
ROUNDS = 4

SYSTEM = """You write evaluation questions for a financial-document retrieval system.

You are given ONE passage from a real SEC filing, FOMC document, or consumer complaint.
Write a question that this passage answers, and extract the exact sentence(s) that answer it.

Hard requirements:
- The question must be answerable from this passage ALONE.
- The question must NOT quote the passage or use phrasing so distinctive that finding it
  is trivial string matching. Ask what a financial analyst would actually ask.
- The question must name the company/entity and period where relevant, so it is
  well-posed without the passage in front of you.
- `snippet` must be copied VERBATIM from the passage, character for character. Do not
  paraphrase, do not fix typos, do not add ellipses. 1-3 sentences.
- `ground_truth` is a one-to-two sentence answer in your own words.

Return ONLY a JSON object:
{"question": "...", "ground_truth": "...", "snippet": "..."}"""

PAIR_SYSTEM = """You write multi-hop evaluation questions for a financial retrieval system.

You are given TWO passages from different documents. Write ONE question that requires
BOTH passages to answer — a comparison, a contrast, or a change over time. A question
answerable from either passage alone is useless and must not be produced.

Hard requirements:
- Name both entities (or the entity and both periods) explicitly.
- `snippet_a` must be copied VERBATIM from PASSAGE A, `snippet_b` VERBATIM from PASSAGE B.
  1-3 sentences each. No paraphrasing.
- `ground_truth` states the actual comparison in two to three sentences.

Return ONLY a JSON object:
{"question": "...", "ground_truth": "...", "snippet_a": "...", "snippet_b": "..."}"""


def load(collection: str) -> pd.DataFrame:
    return pd.read_parquet(PROCESSED / f"chunks_{collection}_fixed.parquet")


def _norm(text: str) -> str:
    """Collapse whitespace. Stored snippets keep their punctuation for human review."""
    return re.sub(r"\s+", " ", text).strip()


def verbatim_ok(snippet: str, passage: str) -> bool:
    """Is the snippet a contiguous run of the passage's words?

    Checked on word tokens rather than characters, because the acceptance rule here must
    match the scoring rule in evals/metrics.py exactly. A character-level check was
    rejecting valid snippets over punctuation spacing — filings render figures as
    "$118,481" and "(8,512)", the model reproduces the digits faithfully and the spacing
    approximately, and a snippet that differs by one space around a parenthesis is still
    perfectly findable by an n-gram matcher.

    The floor of NGRAM tokens is not cosmetic either: a snippet shorter than the metric's
    n-gram width can never be matched at full width, so accepting one would write a gold
    span that scores as a miss against the document it was copied from.
    """
    snippet_words = _WORD.findall(snippet.lower())
    if len(snippet_words) < NGRAM:
        return False
    passage_words = _WORD.findall(passage.lower())
    n = len(snippet_words)
    return any(
        passage_words[i : i + n] == snippet_words
        for i in range(len(passage_words) - n + 1)
    )


def parse_json(raw: str) -> dict | None:
    """Pull the JSON object out of a model response.

    Asking for "ONLY JSON" is a request, not a guarantee; the model still occasionally
    wraps it in a fenced block or a sentence of preamble.
    """
    match = re.search(r"\{.*\}", raw, re.S)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


def draft_single(row: pd.Series, qtype: str, source: str, model: str) -> dict | None:
    passage = row["text"][:6000]
    raw = claude(
        f"PASSAGE (from {row['ticker'] or row['source']} {row['form']} dated {row['date']}, "
        f"section {row['section']}):\n\n{passage}",
        model=model,
        system=SYSTEM,
        max_tokens=800,
    )
    data = parse_json(raw)
    if not data or not all(key in data for key in ("question", "ground_truth", "snippet")):
        return None
    if not verbatim_ok(data["snippet"], passage):
        return None
    return {
        "question": data["question"].strip(),
        "ground_truth": data["ground_truth"].strip(),
        "gold_spans": [{"doc_id": row["doc_id"], "snippet": _norm(data["snippet"])}],
        "type": qtype,
        "expected_source": [source],
        "provenance": "llm_drafted_pending_review",
        "drafted_from_chunk": row["chunk_id"],
    }


def draft_pair(a: pd.Series, b: pd.Series, qtype: str, source: str, model: str) -> dict | None:
    pa, pb = a["text"][:5000], b["text"][:5000]
    raw = claude(
        f"PASSAGE A (from {a['ticker'] or a['source']} {a['form']} dated {a['date']}):\n\n{pa}"
        f"\n\n---\n\nPASSAGE B (from {b['ticker'] or b['source']} {b['form']} dated {b['date']}):"
        f"\n\n{pb}",
        model=model,
        system=PAIR_SYSTEM,
        max_tokens=1000,
    )
    data = parse_json(raw)
    keys = ("question", "ground_truth", "snippet_a", "snippet_b")
    if not data or not all(key in data for key in keys):
        return None
    if not (verbatim_ok(data["snippet_a"], pa) and verbatim_ok(data["snippet_b"], pb)):
        return None
    return {
        "question": data["question"].strip(),
        "ground_truth": data["ground_truth"].strip(),
        "gold_spans": [
            {"doc_id": a["doc_id"], "snippet": _norm(data["snippet_a"])},
            {"doc_id": b["doc_id"], "snippet": _norm(data["snippet_b"])},
        ],
        "type": qtype,
        "expected_source": [source],
        "provenance": "llm_drafted_pending_review",
        "drafted_from_chunk": f"{a['chunk_id']}|{b['chunk_id']}",
    }


def sample_singles(df: pd.DataFrame, n: int, rng: random.Random) -> list[pd.Series]:
    """Stratify by (ticker, section) so the set is not 24 questions about Citigroup.

    Round-robin over the strata rather than sampling proportionally: `full_document` is
    85% of the corpus, and a proportional sample would bury the MD&A and risk-factor
    sections that carry the analytically interesting prose.
    """
    df = df[df["text"].str.len() > 1200]
    strata: dict[tuple, list] = {}
    for _, row in df.iterrows():
        strata.setdefault((row["ticker"], row["section"]), []).append(row)
    keys = sorted(strata, key=lambda k: (str(k[0]), str(k[1])))
    rng.shuffle(keys)
    for key in keys:
        rng.shuffle(strata[key])

    out, i = [], 0
    while len(out) < n and any(strata.values()):
        key = keys[i % len(keys)]
        if strata[key]:
            out.append(strata[key].pop())
        i += 1
    return out


def sample_pairs(df: pd.DataFrame, n: int, rng: random.Random, mode: str) -> list[tuple]:
    """mode='cross_entity' pairs two tickers on the same section (comparison questions);
    mode='temporal' pairs one ticker's same section across two different years."""
    df = df[(df["text"].str.len() > 1200) & df["ticker"].notna() & (df["ticker"] != "")]
    pairs = []
    if mode == "cross_entity":
        # One shuffled pass over 10 tickers yields at most 5 pairs per section, so a
        # single pass across 4 sections caps out around 20 candidates — not enough slack
        # once some drafts are rejected. Repeated shuffles draw different pairings each
        # round, which also stops the same two banks being compared in every question.
        for _ in range(ROUNDS):
            for _section, group in df.groupby("section"):
                tickers = sorted(group["ticker"].unique())
                rng.shuffle(tickers)
                for ta, tb in zip(tickers[::2], tickers[1::2]):
                    ra = group[group.ticker == ta].sample(1, random_state=rng.randint(0, 10**6))
                    rb = group[group.ticker == tb].sample(1, random_state=rng.randint(0, 10**6))
                    pairs.append((ra.iloc[0], rb.iloc[0]))
    else:
        df = df.assign(year=df["date"].str[:4])
        for (ticker, section), group in df.groupby(["ticker", "section"]):
            years = sorted(group["year"].unique())
            if len(years) < 2:
                continue
            early, late = years[0], years[-1]
            ra = group[group.year == early].sample(1, random_state=rng.randint(0, 10**6))
            rb = group[group.year == late].sample(1, random_state=rng.randint(0, 10**6))
            pairs.append((ra.iloc[0], rb.iloc[0]))
    rng.shuffle(pairs)
    return pairs[: n * 4]  # over-sample; some drafts fail the verbatim check


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="evals/drafts.jsonl")
    ap.add_argument("--model", default=DRAFT_MODEL)
    ap.add_argument("--seed", type=int, default=20260822)
    ap.add_argument("--n-filings", type=int, default=24)
    ap.add_argument("--n-other", type=int, default=10)
    ap.add_argument("--n-multihop", type=int, default=12)
    ap.add_argument("--n-temporal", type=int, default=8)
    args = ap.parse_args()

    rng = random.Random(args.seed)
    filings = load("filings")
    complaints = load("complaints")
    edgar = filings[filings.source == "edgar"]
    fomc = filings[filings.source == "fomc"]

    out_path = ROOT / args.out
    out_path.parent.mkdir(parents=True, exist_ok=True)
    written = rejected = 0

    with out_path.open("w") as fh:
        def emit(record):
            nonlocal written, rejected
            if record is None:
                rejected += 1
                print("  reject (bad json or non-verbatim snippet)", flush=True)
                return False
            record["id"] = f"q{written + 1:03d}"
            fh.write(json.dumps(record) + "\n")
            fh.flush()
            written += 1
            print(f"  q{written:03d} [{record['type']}] {record['question'][:88]}", flush=True)
            return True

        print(f"single-hop filings (target {args.n_filings})", flush=True)
        pool = sample_singles(edgar, args.n_filings * 2, rng)
        made = 0
        for row in pool:
            if made >= args.n_filings:
                break
            if emit(draft_single(row, "single_hop", "filings", args.model)):
                made += 1

        # FOMC sits in the `filings` collection, so its expected route is `filings` even
        # though the guide groups it with complaints as "other sources".
        print(f"\nsingle-hop complaints + FOMC (target {args.n_other})", flush=True)
        n_fomc = args.n_other // 2
        plan = [(row, "complaints") for row in sample_singles(
                    complaints, (args.n_other - n_fomc) * 2, rng)]
        plan += [(row, "filings") for row in sample_singles(fomc, n_fomc * 2, rng)]
        rng.shuffle(plan)
        made = 0
        for row, source in plan:
            if made >= args.n_other:
                break
            if emit(draft_single(row, "single_hop", source, args.model)):
                made += 1

        print(f"\nmulti-hop comparative (target {args.n_multihop})", flush=True)
        made = 0
        for a, b in sample_pairs(edgar, args.n_multihop, rng, "cross_entity"):
            if made >= args.n_multihop:
                break
            if emit(draft_pair(a, b, "multi_hop", "filings", args.model)):
                made += 1

        print(f"\ntemporal (target {args.n_temporal})", flush=True)
        made = 0
        for a, b in sample_pairs(edgar, args.n_temporal, rng, "temporal"):
            if made >= args.n_temporal:
                break
            if emit(draft_pair(a, b, "temporal", "filings", args.model)):
                made += 1

    print(f"\nwrote {written} drafts to {out_path} ({rejected} rejected)")
    print("NEXT: human review every row, then add the 21 hand-written negatives.")


if __name__ == "__main__":
    main()
