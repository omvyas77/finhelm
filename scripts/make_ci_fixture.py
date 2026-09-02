"""Carve a small, committable corpus out of the real one so CI can run a real eval.

The problem this solves: a retrieval gate needs an index, the index is 961 MB of
gitignored build artifact, and building it from the raw corpus takes 85 minutes on CPU.
Without something like this, the CI "eval gate" can only re-read numbers somebody else
recorded — which gates nothing about the code in the pull request.

So CI gets a corpus small enough to commit and to index in about a minute: every chunk
holding a gold span for a stratified subset of the golden set, plus a sample of
distractors drawn from the same corpus.

**The number this produces is a tripwire, not a quality measure.** Recall against ~2,000
chunks is not comparable to recall against 24,650 — fewer distractors is an easier
retrieval problem, and the CI figure will read higher than the headline 0.7403. It is
useful for one thing: noticing that a change to chunking, fusion, filtering or reranking
moved retrieval, on every push, for free. The floor in the workflow is calibrated against
this corpus and means nothing against any other.

    python scripts/make_ci_fixture.py
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "evals"))

import metrics as M  # noqa: E402

PROCESSED = ROOT / "data" / "processed"
# Laid out as a data dir in its own right — processed/ beside index/ — so
# FINHELM_DATA_DIR=data/ci works with no copying and no staging step.
OUT = ROOT / "data" / "ci" / "processed"
GOLDEN = ROOT / "evals" / "golden_set.jsonl"
SUBSET = ROOT / "evals" / "ci_subset.jsonl"

# Stratified so the gate keeps covering what the eval covers: the negatives especially,
# since abstention is the behaviour a retrieval change is most likely to break quietly.
QUOTA = {"single_hop": 14, "multi_hop": 10, "temporal": 6,
         "unanswerable": 6, "out_of_scope": 4}

COLLECTIONS = {
    "filings": "chunks_filings_semantic.parquet",
    "complaints": "chunks_complaints_fixed.parquet",
}


def _smoke_ids() -> set[str]:
    """Questions the DeepEval judged gate answers.

    They must be in the fixture. The judged tier inherits FINHELM_DATA_DIR from the
    workflow, so it retrieves from this corpus — and when the fixture was built from the
    stratified subset alone, 7 of the 12 smoke questions had their gold spans excluded by
    construction. The suite then failed 8 of 13 in CI while failing 4 of 13 locally, and
    the difference was not answer quality: the system was being asked questions whose
    evidence had been deliberately removed from the corpus it was given.
    """
    path = ROOT / "tests" / "smoke_set.jsonl"
    if path.exists():
        return {json.loads(l)["id"] for l in path.read_text().splitlines() if l.strip()}
    import sys as _sys
    _sys.path.insert(0, str(ROOT / "tests"))
    from test_smoke_deepeval import load_smoke_set  # noqa: E402

    return {q["id"] for q in load_smoke_set()}


def pick_questions(rows: list[dict], seed: int) -> list[dict]:
    rng = random.Random(seed)
    by_id = {r["id"]: r for r in rows}

    # The smoke set first and unconditionally, then the stratified sample fills the rest.
    required = _smoke_ids() & set(by_id)
    chosen: list[dict] = [by_id[i] for i in sorted(required)]

    for kind, quota in QUOTA.items():
        have = sum(1 for r in chosen if r["type"] == kind)
        pool = [r for r in rows if r["type"] == kind and r["id"] not in required]
        rng.shuffle(pool)
        chosen.extend(pool[: max(0, quota - have)])

    chosen.sort(key=lambda r: r["id"])
    return chosen


def gold_chunks(questions: list[dict], frames: dict[str, pd.DataFrame]) -> set[str]:
    """Chunk ids that satisfy is_hit for some gold span.

    Selected with the very function the metric uses, not with a lookalike. A fixture
    assembled by a near-copy of the matching rule would hand CI a corpus where the gold
    span is present by one definition and unreachable by the other, and the gate would
    fail for a reason that has nothing to do with retrieval.
    """
    keep: set[str] = set()
    missing = []
    for question in questions:
        for gold in question.get("gold_spans", []):
            found = False
            for frame in frames.values():
                candidates = frame[frame["doc_id"] == gold["doc_id"]]
                for row in candidates.itertuples():
                    chunk = {"chunk_id": row.chunk_id, "doc_id": row.doc_id,
                             "text": row.text}
                    if M.is_hit(chunk, gold):
                        keep.add(row.chunk_id)
                        found = True
            if not found:
                missing.append((question["id"], gold["doc_id"]))
    if missing:
        # Retrievability on the full corpus is 1.0000, so this cannot happen unless the
        # fixture logic is wrong. Failing here beats shipping a corpus whose ceiling is
        # below 1 and then calibrating a floor against it.
        raise SystemExit(f"gold spans unreachable in the source corpus: {missing[:5]}")
    return keep


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--distractors", type=int, default=1800,
                    help="chunks sampled from the rest of the corpus, split across "
                         "collections in proportion to their size")
    ap.add_argument("--seed", type=int, default=20260901)
    args = ap.parse_args()

    rows = [json.loads(l) for l in GOLDEN.read_text().splitlines() if l.strip()]
    questions = pick_questions(rows, args.seed)

    frames = {}
    for collection, name in COLLECTIONS.items():
        path = PROCESSED / name
        if not path.exists():
            raise SystemExit(f"{path} missing; build the corpus before the fixture")
        frames[collection] = pd.read_parquet(path)

    keep = gold_chunks(questions, frames)
    total = sum(len(f) for f in frames.values())
    rng = random.Random(args.seed)

    OUT.mkdir(parents=True, exist_ok=True)
    written = {}
    for collection, frame in frames.items():
        share = int(args.distractors * len(frame) / total)
        pool = [c for c in frame["chunk_id"].tolist() if c not in keep]
        rng.shuffle(pool)
        selected = keep.intersection(frame["chunk_id"]) | set(pool[:share])
        subset = frame[frame["chunk_id"].isin(selected)].reset_index(drop=True)
        path = OUT / COLLECTIONS[collection]
        subset.to_parquet(path, index=False)
        written[collection] = (len(subset), path.stat().st_size / 1048576)

    SUBSET.write_text("\n".join(json.dumps(q) for q in questions) + "\n")

    print(f"{len(questions)} questions -> {SUBSET.relative_to(ROOT)}")
    for kind, quota in QUOTA.items():
        got = sum(1 for q in questions if q["type"] == kind)
        print(f"    {kind:<14} {got}/{quota}")
    print(f"{len(keep)} gold-bearing chunks + {args.distractors} distractors")
    for collection, (n, mb) in written.items():
        print(f"    {collection:<12} {n:>5} chunks  {mb:.2f} MB")


if __name__ == "__main__":
    main()
