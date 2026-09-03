"""The HTTP surface, with the pipeline stubbed out.

an earlier stage shipped the API with no tests at all, which is how the `agentic` field went
missing: the Streamlit sidebar offered a toggle, the request body had nowhere to put it,
and the service used its pinned config regardless. Nothing failed — the toggle just did
nothing, in the only mode a deployment ever runs in.

`answer` is monkeypatched throughout. These assert the contract of the HTTP layer —
what it accepts, what it forwards, what it puts in the response — and deliberately not
the quality of the retrieval underneath it, which is what evals/ is for. That keeps them
free, offline, and eligible for the fast CI tier.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from finhelm import api
from finhelm.generate import Answer
from finhelm.stores.base import Hit


@pytest.fixture
def client():
    return TestClient(api.app, raise_server_exceptions=False)


@pytest.fixture
def captured(monkeypatch):
    """Record the config `answer` was called with, and return a fixed Answer."""
    seen = {}

    def fake_answer(question, cfg=None, filters=None, collections=None):
        seen["question"] = question
        seen["cfg"] = cfg
        seen["filters"] = filters
        seen["collections"] = collections
        return Answer(
            answer="Net charge-offs rose to 5.9% [S1].",
            cited_ids=["S1"],
            retrieved=[Hit("COF_10K_2025_item7_004", 0.71, {
                "doc_id": "COF_10K_2025", "ticker": "COF", "date": "2025-02-20",
                "section": "item_7_mda", "url": "https://example.invalid/f.htm",
                "text": "Net charge-offs rose ...",
            })],
            route="filings", abstained=False, invalid_citations=[],
            uncited_sentences=0, retrieval_ms=120, generation_ms=900,
            trace_id="abc123", config=api.CONFIG.run_name(),
            route_reason="ticker mentioned", sub_questions=[],
        )

    monkeypatch.setattr(api, "answer", fake_answer)
    return seen


def test_ask_forwards_the_agentic_override(client, captured):
    client.post("/ask", json={"question": "compare COF and SYF on credit",
                              "agentic": False})
    assert captured["cfg"].agentic is False


def test_ask_without_an_override_uses_the_pinned_config(client, captured):
    client.post("/ask", json={"question": "what did COF report"})
    assert captured["cfg"] is api.CONFIG


def test_an_override_does_not_mutate_the_pinned_config(client, captured):
    """CONFIG is frozen and module-level. A request that changed it would leak into every
    subsequent request in the process."""
    before = api.CONFIG.agentic
    client.post("/ask", json={"question": "anything at all", "agentic": not before})
    assert api.CONFIG.agentic is before


def test_ask_forwards_filters_and_collections(client, captured):
    client.post("/ask", json={"question": "credit normalization at COF",
                              "filters": {"ticker": "COF"},
                              "collections": ["filings"]})
    assert captured["filters"] == {"ticker": "COF"}
    assert captured["collections"] == ["filings"]


def test_only_cited_sources_are_returned_as_citations(client, captured, monkeypatch):
    """A source the model never cited is not a citation. Returning all sixteen retrieved
    passages as `citations` would make citation counts meaningless in the UI."""
    body = client.post("/ask", json={"question": "what did COF report"}).json()
    assert [c["marker"] for c in body["citations"]] == ["S1"]
    assert body["citations"][0]["ticker"] == "COF"


def test_question_length_is_validated(client):
    assert client.post("/ask", json={"question": "hi"}).status_code == 422
    assert client.post("/ask", json={"question": "x" * 2001}).status_code == 422


def test_request_id_is_adopted_when_supplied(client, captured):
    response = client.post("/ask", json={"question": "what did COF report"},
                           headers={"X-Request-ID": "caller-supplied-id"})
    assert response.headers["x-request-id"] == "caller-supplied-id"


def test_request_id_is_minted_when_absent(client, captured):
    response = client.post("/ask", json={"question": "what did COF report"})
    assert response.headers["x-request-id"]
    assert "x-response-time-ms" in response.headers


def test_health_reports_whether_the_index_is_present(client):
    body = client.get("/health").json()
    assert body["status"] == ("ok" if body["index_present"] else "degraded")
    # The name, not just a boolean: "which index did you expect" is the first question
    # when a container comes up degraded.
    assert body["index"].startswith("filings_")


def test_config_endpoint_reports_the_served_config(client):
    body = client.get("/config").json()
    assert body["run_name"] == api.CONFIG.run_name()
    assert body["config"]["embed_model"] == api.CONFIG.embed_model


def test_eval_report_says_whether_it_matched_the_live_config(client):
    body = client.get("/eval-report").json()
    if "detail" in body:
        pytest.skip("no evaluation history on disk")
    # The flag is the point: falling back to the newest run on disk while implying it
    # describes the running config is how a service reports someone else's numbers.
    assert isinstance(body["matched_live_config"], bool)
    if body["matched_live_config"]:
        assert body["run_name"].startswith(api.CONFIG.run_name())
