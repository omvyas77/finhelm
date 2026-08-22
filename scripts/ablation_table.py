"""Render evals/history.jsonl as the Day 2 ablation table.

Reads the history file rather than the results/ directory because history.jsonl is the
append-only record: one line per run, never overwritten. `results/<run_name>.json` is
keyed on run_name, so re-running a configuration silently replaces the previous file and
the table would lose the fact that a cell was measured twice.

Two things this deliberately refuses to do:

  - print a number for a cell that was never run. An empty cell is honest; a zero is a
    claim that the configuration performed badly, which is a different statement;
  - print a strategy name for a run that fell back to another strategy without saying so.
    `strategy_fallbacks` is carried through from the runner and surfaced as a footnote,
    because a row labelled "semantic" that ran half its corpus as fixed is only
    interpretable if you can see that.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HISTORY = ROOT / "evals" / "history.jsonl"

CHUNKINGS = ["fixed", "semantic", "sentence_window"]
RETRIEVERS = ["bm25", "dense", "hybrid"]

# (column header, key in the history row, format spec)
#
# The latency columns are *retrieval* latency, not end-to-end. This table compares
# retrieval configurations, and a run that also generated an answer carries several
# seconds of Anthropic round-trip in its `latency_ms` that has nothing to do with the
# retriever being compared. Mixing the two puts a 7963ms cell next to a 1751ms one and
# invites the reader to blame the retriever for the generator's time.
COLUMNS = [
    ("recall@5", "recall_at_5", ".3f"),
    ("MRR", "mrr", ".3f"),
    ("route acc", "route_accuracy", ".3f"),
    ("p50 ret ms", "p50_retrieval_ms", ".0f"),
    ("p95 ret ms", "p95_retrieval_ms", ".0f"),
]

# Runs recorded before retrieval latency was broken out only carry the combined figure.
# For a --retrieve-only run the two are equal by construction (generation_ms is 0), so
# reading the combined column is exact — but only for those runs, which is why the
# fallback is gated on retrieve_only below rather than applied unconditionally.
LEGACY_LATENCY = {
    "p50_retrieval_ms": "p50_latency_ms",
    "p95_retrieval_ms": "p95_latency_ms",
}


def cell_value(row: dict, key: str):
    """Read a metric, falling back to the pre-split latency field only where it is safe."""
    if row.get(key) is not None:
        return row[key]
    legacy = LEGACY_LATENCY.get(key)
    # A generating run has no usable retrieval figure in the old schema — its combined
    # latency is dominated by generation. Return None so the cell renders blank rather
    # than reporting the generator's time as the retriever's.
    if legacy and row.get("retrieve_only", True):
        return row.get(legacy)
    return None


def load_runs() -> dict[tuple[str, str, bool], dict]:
    """Best run per (chunking, retriever, rerank) cell.

    "Best" means the widest run, not the most recent one. A `--limit 8` smoke run carries
    the same config as the full run it was smoke-testing, so plain last-write-wins would
    let a throwaway 8-question check silently replace a 75-question measurement — and the
    replacement is invisible in the rendered table, because both rows look equally real.
    Ties on question count go to the later run, which is the case where "a re-run
    supersedes the earlier one" actually holds.
    """
    runs: dict[tuple[str, str, bool], dict] = {}
    if not HISTORY.exists():
        return runs
    with HISTORY.open() as fh:
        for line in fh:
            if not line.strip():
                continue
            row = json.loads(line)
            cfg = row.get("config", {})
            key = (cfg.get("chunking"), cfg.get("retriever"), bool(cfg.get("rerank")))
            prior = runs.get(key)
            if prior is None or row.get("n_questions", 0) >= prior.get("n_questions", 0):
                runs[key] = row
    return runs


def render(runs: dict[tuple[str, str, bool], dict], rerank: bool) -> tuple[list[str], list[str]]:
    headers = ["chunking", "retriever"] + [c[0] for c in COLUMNS] + ["n"]
    widths = [max(len(h), 15 if h == "chunking" else len(h)) for h in headers]

    rows: list[list[str]] = []
    notes: list[str] = []

    for chunking in CHUNKINGS:
        for retriever in RETRIEVERS:
            row = runs.get((chunking, retriever, rerank))
            if row is None:
                rows.append([chunking, retriever] + ["" for _ in COLUMNS] + [""])
                continue

            cells = []
            for _, key, spec in COLUMNS:
                value = cell_value(row, key)
                cells.append("" if value is None else format(value, spec))

            label = chunking
            for fallback in row.get("strategy_fallbacks", []):
                marker = "*"
                label = chunking + marker
                note = (f"{marker} {row['run_name']}: {fallback['collection']} has no "
                        f"'{fallback['requested']}' index, ran as '{fallback['used']}'")
                if note not in notes:
                    notes.append(note)

            rows.append([label, retriever] + cells + [f"{row.get('n_questions', 0):.0f}"])

    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    def line(cells: list[str]) -> str:
        return "| " + " | ".join(c.ljust(w) for c, w in zip(cells, widths)) + " |"

    out = [line(headers), "|" + "|".join("-" * (w + 2) for w in widths) + "|"]
    out += [line(r) for r in rows]
    return out, notes


def main() -> None:
    runs = load_runs()
    if not runs:
        print(f"no runs in {HISTORY}")
        return

    for rerank in (False, True):
        present = [k for k in runs if k[2] is rerank]
        if not present:
            continue
        print(f"\n### {'With' if rerank else 'Without'} cross-encoder reranking\n")
        table, notes = render(runs, rerank)
        print("\n".join(table))
        for note in notes:
            print(f"\n{note}")

    measured = len(runs)
    total = len(CHUNKINGS) * len(RETRIEVERS) * 2
    print(f"\n{measured}/{total} cells measured")


if __name__ == "__main__":
    main()
