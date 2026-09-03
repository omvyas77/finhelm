"""Decomposition must fail open and never silently drop a facet."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from finhelm.agent import decompose as D  # noqa: E402

Q = "How does JPMorgan's CET1 ratio compare with Citigroup's?"


def test_unparseable_reply_returns_the_original_question(monkeypatch):
    """A helper model answering in prose must cost ranking, never the answer. an earlier stage
    answered 41% of questions correctly with no decomposition at all, so falling back to
    that is strictly better than raising."""
    monkeypatch.setattr(D, "claude", lambda *a, **k: "Sure! Here are some thoughts.")
    assert D.decompose(Q) == [Q]


def test_api_failure_returns_the_original_question(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("rate limited")
    monkeypatch.setattr(D, "claude", boom)
    assert D.decompose(Q) == [Q]


def test_a_single_sub_question_keeps_the_users_exact_wording(monkeypatch):
    """When the model judges a question atomic it tends to echo a paraphrase. Retrieval
    should stay on the original string — BM25 in particular scores it differently."""
    monkeypatch.setattr(D, "claude",
                        lambda *a, **k: '{"sub_questions": ["What is JPM CET1?"]}')
    assert D.decompose(Q) == [Q]


def test_a_real_split_is_returned(monkeypatch):
    monkeypatch.setattr(D, "claude", lambda *a, **k:
                        '{"sub_questions": ["JPM CET1 ratio", "Citigroup CET1 ratio"]}')
    assert D.decompose(Q) == ["JPM CET1 ratio", "Citigroup CET1 ratio"]


def test_duplicate_sub_questions_are_collapsed(monkeypatch):
    """Issuing the same query twice would double its weight under rank fusion, which
    quietly biases the pool toward one side of a comparison."""
    monkeypatch.setattr(D, "claude", lambda *a, **k:
                        '{"sub_questions": ["JPM CET1", "JPM CET1", "C CET1"]}')
    assert D.decompose(Q) == ["JPM CET1", "C CET1"]


def test_sub_questions_are_capped(monkeypatch):
    monkeypatch.setattr(D, "claude", lambda *a, **k:
                        '{"sub_questions": ["a", "b", "c", "d", "e", "f"]}')
    assert len(D.decompose(Q, max_sub_questions=3)) == 3


def test_json_embedded_in_prose_is_still_parsed(monkeypatch):
    monkeypatch.setattr(D, "claude", lambda *a, **k:
                        'Here you go:\n{"sub_questions": ["x", "y"]}\nHope that helps.')
    assert D.decompose(Q) == ["x", "y"]
