"""The sentence-window splice, and the two ways it silently produces wrong text.

`expand()` had a subtler failure mode than "does not run": every bug available to it
returns real sentences from the real filing, just the wrong ones, so nothing raises and
the only symptom is a recall number that looks unremarkable. These tests pin the two that
actually bit:

  1. keying the sentence list on doc_id instead of (doc_id, section), which interleaves
     the sections of the 15 multi-section filings and slices a window out of the wrong one;
  2. writing the expanded text through `hit.metadata`, which is the BM25 index's own
     cached parquet record — one query would then leave every later query in the process
     retrieving pre-expanded text.

The last test is the end-to-end one: expansion must never lose a hit. Since the window
contains its own sentence, any gold span the sentence matched the window must still match,
and `is_hit` must stay monotonic for that to hold.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from evals.metrics import is_hit  # noqa: E402
from finhelm.retrieve import window  # noqa: E402
from finhelm.stores.base import Hit  # noqa: E402

CHUNKS = window.PROCESSED / f"chunks_filings_{window.STRATEGY}.parquet"
needs_chunks = pytest.mark.skipif(not CHUNKS.exists(), reason="sentence_window chunks not built")


@pytest.fixture(scope="module")
def maps():
    return window._windows("filings")


@needs_chunks
def test_sentence_lists_are_keyed_by_section_not_just_document(maps):
    """The mis-ordering that made expansion look like it *lowered* recall.

    chunk_doc() runs per (document, section), so the trailing index of a chunk_id is only
    unique within a section. If the key were doc_id alone, a multi-section filing's
    sentences would collide and the list would be built from two interleaved sections.
    """
    sentences, _ = maps
    keys = list(sentences)
    assert all(isinstance(k, tuple) and len(k) == 2 for k in keys)

    multi = {doc for doc, _ in keys if sum(1 for d, _ in keys if d == doc) > 1}
    assert multi, "expected some filing to carry more than one section"


@needs_chunks
def test_expanded_text_starts_from_the_indexed_sentence(maps):
    """Reconstruction check: the chunk's own text must appear in its window."""
    import pandas as pd

    frame = pd.read_parquet(CHUNKS, columns=["chunk_id", "text"]).iloc[::7919]
    sentences, spans = maps
    for chunk_id, text in zip(frame["chunk_id"], frame["text"]):
        key, start, end = spans[chunk_id]
        assert text in " ".join(sentences[key][start:end]), chunk_id


@needs_chunks
def test_expand_does_not_mutate_the_bm25_cached_metadata(maps):
    """BM25 hands out the parquet records themselves; expansion must copy, not write."""
    _, spans = maps
    chunk_id = next(iter(spans))
    metadata = {"text": "the indexed sentence.", "doc_id": "d", "section": "s"}
    hit = Hit(chunk_id, 1.0, metadata)

    (expanded,) = window.expand([hit], [("filings", window.STRATEGY)])

    assert metadata["text"] == "the indexed sentence."
    assert expanded.metadata is not metadata
    assert len(expanded.text) > len(hit.text)


def test_hits_from_other_strategies_are_returned_untouched():
    """A fixed-chunking result set must not pay for, or be altered by, the window map."""
    hit = Hit("some_fixed_chunk_000", 1.0, {"text": "unchanged"})
    assert window.expand([hit], [("complaints", "fixed")]) == [hit]


@needs_chunks
def test_unknown_chunk_ids_survive_a_mixed_strategy_result_set(maps):
    """filings=sentence_window + complaints=fixed is the routed-to-both case."""
    foreign = Hit("complaints_fixed_000", 1.0, {"text": "a complaint narrative"})
    out = window.expand([foreign], [("filings", window.STRATEGY), ("complaints", "fixed")])
    assert out == [foreign]


@needs_chunks
def test_expansion_never_turns_a_hit_into_a_miss(maps):
    """is_hit must be monotonic in chunk text, or expansion could lose recall.

    It is not obviously monotonic — `_contradicts` rejects a chunk that states figures and
    shares none with the gold span, and a window drags in neighbouring figures the lone
    sentence did not have. This asserts the direction the measured runs showed (12 hits
    gained, 0 lost); if a future change to _contradicts breaks it, the sentence_window
    column silently loses recall again.
    """
    import pandas as pd

    # Real sentences, and specifically ones carrying figures — a sentence with no numbers
    # cannot exercise _contradicts, which is the only non-monotonic path in is_hit.
    frame = pd.read_parquet(CHUNKS, columns=["chunk_id", "text"])
    numeric = frame[frame["text"].str.contains(r"\d", regex=True)].iloc[::400][:200]
    assert len(numeric) > 50, "expected a usable sample of sentences containing figures"

    sentences, spans = maps
    regressions = []
    for chunk_id, sentence in zip(numeric["chunk_id"], numeric["text"]):
        key, start, end = spans[chunk_id]
        gold = {"doc_id": "d", "snippet": sentence}
        bare = {"doc_id": "d", "text": sentence}
        full = {"doc_id": "d", "text": " ".join(sentences[key][start:end])}
        if is_hit(bare, gold) and not is_hit(full, gold):
            regressions.append(chunk_id)

    assert not regressions, (
        f"{len(regressions)} sentence(s) matched their own gold span but their window did "
        f"not — is_hit is no longer monotonic, e.g. {regressions[:3]}"
    )
