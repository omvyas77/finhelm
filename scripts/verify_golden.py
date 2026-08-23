"""Mechanical checks on drafted golden-set rows.

This is NOT human review and does not replace it. `draft_golden.py` ends by telling you to
read every row, and that instruction still stands: a machine can tell whether a question is
*answerable from the span*, but not whether it is well-posed, fair, or interesting, and not
whether the span is the only passage that answers it.

What it can catch is the set of defects that would silently corrupt the metric, which are
exactly the ones a tired human reviewer misses:

  1. **Unfindable spans.** A gold span whose document is absent, or which no chunk of that
     document contains under `is_hit`, scores as a permanent miss for every configuration.
     That does not just add noise — it lowers the ceiling, so the whole ablation is
     measured against a target no system could reach. The Day 2 set is clean on this
     (ceiling 1.000, verified across all three chunking strategies); new rows must not
     break it.

  2. **Trivial questions.** The drafting prompt forbids questions that quote the passage,
     because retrieving a span whose wording the question already contains measures string
     matching rather than retrieval. A model asked not to do this still does it sometimes,
     and each such row inflates recall for every configuration equally — invisible in a
     comparison, wrong in the headline.

  3. **Duplicates.** Re-drafting with a new seed re-samples the same corpus, so a question
     already in the golden set can be drafted again. Scoring it twice double-weights one
     passage.

Rows that pass are written with provenance `llm_drafted_machine_verified`, which is
deliberately not the same token as the reviewed set's. Nothing here promotes a row into
`golden_set.jsonl`; that stays a human decision.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from evals import metrics as M  # noqa: E402
from evals.metrics import NGRAM, _WORD  # noqa: E402

PROCESSED = ROOT / "data" / "processed"

# Fraction of the question's words that also appear in the gold snippet, above which the
# question is treated as quoting its own answer. Set from the drafted distribution rather
# than picked: legitimate questions cluster well below this because they name an entity and
# a metric, while the passage states a figure and a cause.
TRIVIAL_OVERLAP = 0.80


def load_chunks() -> dict[str, list[str]]:
    """doc_id -> every chunk text, across both collections under the `fixed` strategy.

    `fixed` alone is enough: the Day 2 ceiling check found all three strategies identical
    at 1.000, because a span that survives one chunking survives all of them — chunk
    boundaries move, but no strategy deletes text.
    """
    by_doc: dict[str, list[str]] = defaultdict(list)
    for name in ("filings_fixed", "complaints_fixed"):
        path = PROCESSED / f"chunks_{name}.parquet"
        if not path.exists():
            continue
        frame = pd.read_parquet(path, columns=["doc_id", "text"])
        for doc_id, text in zip(frame["doc_id"], frame["text"]):
            by_doc[doc_id].append(text)
    return by_doc


def question_overlap(question: str, snippet: str) -> float:
    q = set(_WORD.findall(question.lower()))
    s = set(_WORD.findall(snippet.lower()))
    return len(q & s) / len(q) if q else 0.0


def check(row: dict, by_doc: dict[str, list[str]], seen: set[str]) -> list[str]:
    problems = []
    spans = row.get("gold_spans") or []
    if not spans:
        problems.append("no gold spans")

    for span in spans:
        doc_id, snippet = span.get("doc_id"), span.get("snippet", "")
        chunks = by_doc.get(doc_id)
        if not chunks:
            problems.append(f"doc absent from corpus: {doc_id}")
            continue
        if len(_WORD.findall(snippet.lower())) < NGRAM:
            problems.append(f"snippet shorter than the {NGRAM}-word matcher")
        if not any(M.is_hit({"doc_id": doc_id, "text": t}, span) for t in chunks):
            problems.append(f"span unfindable in {doc_id} (would score as a permanent miss)")

    worst = max((question_overlap(row["question"], s.get("snippet", "")) for s in spans),
                default=0.0)
    if worst >= TRIVIAL_OVERLAP:
        problems.append(f"question quotes its own answer ({worst:.0%} word overlap)")

    key = " ".join(sorted(_WORD.findall(row["question"].lower())))
    if key in seen:
        problems.append("duplicate question")
    seen.add(key)

    return problems


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--drafts", required=True)
    ap.add_argument("--against", default="evals/golden_set.jsonl",
                    help="existing set, checked against for duplicates")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    by_doc = load_chunks()
    seen = {" ".join(sorted(_WORD.findall(json.loads(l)["question"].lower())))
            for l in (ROOT / args.against).open() if l.strip()}
    print(f"corpus: {len(by_doc)} documents | existing set: {len(seen)} questions")

    rows = [json.loads(l) for l in (ROOT / args.drafts).open() if l.strip()]
    passed, failed, reasons = [], [], Counter()
    for row in rows:
        problems = check(row, by_doc, seen)
        if problems:
            failed.append((row, problems))
            for p in problems:
                reasons[p.split(":")[0].split(" (")[0]] += 1
        else:
            passed.append({**row, "provenance": "llm_drafted_machine_verified"})

    print(f"\n{len(passed)}/{len(rows)} passed, {len(failed)} rejected")
    for reason, count in reasons.most_common():
        print(f"  {count:>3}  {reason}")
    print(f"\nby type: {dict(Counter(r['type'] for r in passed))}")
    print(f"gold spans: {sum(len(r['gold_spans']) for r in passed)}")

    if args.out:
        out = ROOT / args.out
        with out.open("w") as fh:
            for row in passed:
                fh.write(json.dumps(row) + "\n")
        print(f"\nwrote {out}")
        print("NOT promoted to golden_set.jsonl — these are machine-verified, not reviewed.")


if __name__ == "__main__":
    main()
