"""Assemble and validate evals/golden_set.jsonl from drafts + hand-written negatives.

Validation is the point of this script. A golden set with a broken row does not throw an
error at eval time — it quietly scores as a permanent miss for every configuration, which
looks like a retrieval weakness in the ablation table and is impossible to distinguish
from one by reading the numbers.

Every check below corresponds to a way this set could be silently wrong:

  - composition drift (fewer negatives than intended shifts the abstention metrics);
  - a gold span whose doc_id is not in the corpus (unscoreable, always a miss);
  - a gold span that is not findable under some chunking strategy (would penalise that
    strategy specifically, corrupting the one comparison the set exists to make);
  - a negative that carries gold spans (contradicts its own type);
  - duplicate questions (double-weights whatever they test).
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from evals.metrics import is_hit  # noqa: E402

EVALS = ROOT / "evals"
PROCESSED = ROOT / "data" / "processed"

# The composition the build guide specifies. Asserted rather than assumed, because the
# drafting step silently produces fewer rows when candidates fail the verbatim check.
TARGET = {"single_hop": 34, "multi_hop": 12, "temporal": 8, "unanswerable": 13, "out_of_scope": 8}

SOURCES = [
    ("drafts_singles.jsonl", lambda r: r["type"] in ("single_hop", "temporal")),
    ("drafts_multihop.jsonl", lambda r: r["type"] == "multi_hop"),
    ("negatives.jsonl", lambda r: True),
]


def read(name: str, keep) -> list[dict]:
    path = EVALS / name
    if not path.exists():
        sys.exit(f"missing {path}")
    with path.open() as fh:
        return [r for r in (json.loads(line) for line in fh if line.strip()) if keep(r)]


def main() -> None:
    records: list[dict] = []
    for name, keep in SOURCES:
        rows = read(name, keep)
        print(f"{name:26} {len(rows):>3} rows")
        records.extend(rows)

    # Stable ordering: positives first in composition order, then negatives. Keeps
    # --limit smoke runs representative instead of 10 questions about the same thing.
    order = {t: i for i, t in enumerate(TARGET)}
    records.sort(key=lambda r: order.get(r["type"], 99))
    for i, record in enumerate(records, start=1):
        record["id"] = f"q{i:03d}"
        record.setdefault("provenance", "llm_drafted_human_verified")

    problems: list[str] = []

    counts = Counter(r["type"] for r in records)
    for qtype, want in TARGET.items():
        if counts.get(qtype, 0) != want:
            problems.append(f"composition: {qtype} has {counts.get(qtype, 0)}, want {want}")

    questions = Counter(r["question"].strip().lower() for r in records)
    for question, n in questions.items():
        if n > 1:
            problems.append(f"duplicate question x{n}: {question[:70]}")

    for record in records:
        spans = record.get("gold_spans", [])
        if record["type"] in ("unanswerable", "out_of_scope"):
            if spans:
                problems.append(f"{record['id']}: negative carries {len(spans)} gold spans")
        elif not spans:
            problems.append(f"{record['id']}: positive has no gold spans")

    # Findability: the expensive check, and the one that actually protects the ablation.
    frames = {
        "filings_fixed": PROCESSED / "chunks_filings_fixed.parquet",
        "filings_semantic": PROCESSED / "chunks_filings_semantic.parquet",
        "filings_sentence_window": PROCESSED / "chunks_filings_sentence_window.parquet",
        "complaints_fixed": PROCESSED / "chunks_complaints_fixed.parquet",
    }
    loaded = {name: pd.read_parquet(path)[["doc_id", "text"]]
              for name, path in frames.items() if path.exists()}

    by_doc = {name: dict(tuple(df.groupby("doc_id"))) for name, df in loaded.items()}

    for record in records:
        for span in record.get("gold_spans", []):
            found_anywhere = False
            for name, groups in by_doc.items():
                group = groups.get(span["doc_id"])
                if group is None:
                    continue
                found_anywhere = True
                hits = sum(is_hit({"doc_id": d, "text": t}, span)
                           for d, t in zip(group["doc_id"], group["text"]))
                if hits == 0:
                    problems.append(
                        f"{record['id']}: span not findable in {name} "
                        f"(doc {span['doc_id']})")
            if not found_anywhere:
                problems.append(f"{record['id']}: doc_id {span['doc_id']} not in any corpus")

    print(f"\n{len(records)} questions:", dict(counts))

    if problems:
        print(f"\n{len(problems)} PROBLEM(S):")
        for problem in problems:
            print("  -", problem)
        sys.exit(1)

    out = EVALS / "golden_set.jsonl"

    # Refuse to shrink the golden set without being told to.
    #
    # This script assembles from the original hand-written sources, which is 75 questions.
    # The set on disk is 202 — the Day 2 expansion was added by a different path and lives
    # only in the file. Running this script therefore *destroys* 127 questions, and it takes
    # no arguments, so anything that executes it destroys them: a shell loop checking that
    # every script imports cleanly ran `python scripts/assemble_golden.py --help`, which is
    # not a flag it parses, and silently rewrote the file. git had it; nothing else warned.
    if out.exists():
        existing = sum(1 for line in out.read_text().splitlines() if line.strip())
        if len(records) < existing and "--force" not in sys.argv:
            sys.exit(
                f"refusing to overwrite {out.name}: it holds {existing} questions and this "
                f"run assembled {len(records)}, which would delete {existing - len(records)}."
                f"\nRe-run with --force if that is genuinely what you want.")

    with out.open("w") as fh:
        for record in records:
            fh.write(json.dumps(record) + "\n")
    print(f"\nvalidated and wrote {out}")


if __name__ == "__main__":
    main()
