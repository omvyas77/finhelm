"""The judge's 429 handling, tested without touching the network.

Two different quotas arrive as the same HTTP status with the same "Please retry in Ns"
hint, and the correct response to them is opposite: pace-and-retry for the per-minute
cap, give up immediately for the per-day one. Getting that backwards is expensive in a
way that does not look like a bug — a scoring pass spends four minutes retrying into a
window that will not reopen until midnight, then reports a per-minute problem.

These are string-matching tests because the discrimination *is* string matching: the
google-genai client surfaces the quota id inside the exception text rather than as a
structured field, so the parser is the contract worth pinning.
"""

from __future__ import annotations

import pytest

from src.finhelm.judge import (
    _DAILY_MESSAGE,
    DailyQuotaExhausted,
    _is_daily_quota,
    _is_rate_limit,
    _retry_after,
)

# Trimmed from real 429 bodies. Both carry a retry hint; only one of them means it.
PER_DAY = (
    "429 RESOURCE_EXHAUSTED. {'error': {'code': 429, 'message': 'You exceeded your "
    "current quota... * Quota exceeded for metric: generativelanguage.googleapis.com/"
    "generate_content_free_tier_requests, limit: 500, model: gemini-3.1-flash-lite "
    "Please retry in 43.831872857s.', 'status': 'RESOURCE_EXHAUSTED', 'details': "
    "[{'quotaId': 'GenerateRequestsPerDayPerProjectPerModel-FreeTier'}]}}"
)
PER_MINUTE = (
    "429 RESOURCE_EXHAUSTED. {'error': {'code': 429, 'message': 'Quota exceeded... "
    "Please retry in 12.0s.', 'details': [{'quotaId': "
    "'GenerateRequestsPerMinutePerProjectPerModel-FreeTier'}]}}"
)


def test_both_quotas_are_recognised_as_rate_limits():
    """Neither should escape as an unhandled exception."""
    assert _is_rate_limit(Exception(PER_DAY))
    assert _is_rate_limit(Exception(PER_MINUTE))


def test_only_the_daily_quota_is_classified_as_daily():
    assert _is_daily_quota(Exception(PER_DAY))
    assert not _is_daily_quota(Exception(PER_MINUTE))


def test_non_429_errors_are_not_swallowed():
    """A connection error must propagate, not be retried as if it were throttling."""
    boom = Exception("ConnectionError: connection reset by peer")
    assert not _is_rate_limit(boom)
    assert not _is_daily_quota(boom)


def test_retry_delay_is_parsed_from_the_error_body():
    """Google states the wait it wants; a guessed backoff is wasteful or too fast."""
    # +1.0s of slack, because the server window and ours are not aligned.
    assert _retry_after(Exception(PER_MINUTE), attempt=0) == pytest.approx(13.0)


def test_retry_delay_falls_back_to_capped_exponential():
    """No hint in the body — back off exponentially, but never longer than a minute."""
    assert _retry_after(Exception("429 no hint here"), attempt=3) == 8.0
    assert _retry_after(Exception("429 no hint here"), attempt=20) == 60.0


def test_daily_quota_message_does_not_blame_rpm():
    """The failure text is the whole point of the class.

    Whoever hits this at 60% through a scoring pass needs to know that lowering
    concurrency will not help and that the cache makes a later resume cheap.
    """
    with pytest.raises(DailyQuotaExhausted) as excinfo:
        raise DailyQuotaExhausted(_DAILY_MESSAGE.format(model="gemini-3.1-flash-lite"))

    message = str(excinfo.value)
    assert "gemini-3.1-flash-lite" in message      # which model ran out
    assert ".cache/ragas" in message               # how to resume cheaply
    assert "Retrying will not help" in message     # what not to try


def test_daily_quota_is_a_runtime_error():
    """Callers that only catch RuntimeError (the pre-existing behaviour) still work."""
    assert issubclass(DailyQuotaExhausted, RuntimeError)


def test_the_ragas_path_also_fails_fast_on_the_daily_quota(monkeypatch):
    """The gap that let the daily cap look like a slow judge for two scoring passes.

    `ragas_runner` builds a LangChain chat model, not a DeepEval one, so none of the
    protection above reached it. A per-day 429 went to tenacity, tenacity honoured the
    misleading "retry in ~40s", and the job blew its 300s timeout mid-backoff and landed
    as NaN. This asserts the daily error escapes as itself, immediately.
    """
    from langchain_google_genai import ChatGoogleGenerativeAI

    from src.finhelm.judge import rate_limited_langchain_gemini

    def boom(self, *args, **kwargs):
        raise Exception(PER_DAY)

    monkeypatch.setattr(ChatGoogleGenerativeAI, "_generate", boom, raising=False)

    chat = rate_limited_langchain_gemini("gemini-3.1-flash-lite", "not-a-real-key")
    with pytest.raises(DailyQuotaExhausted):
        chat.invoke("anything")


def test_the_ragas_path_still_retries_a_per_minute_quota(monkeypatch):
    """Failing fast must not swallow the quota that pacing *can* recover from."""
    from langchain_google_genai import ChatGoogleGenerativeAI

    from src.finhelm import judge

    calls = []

    def flaky(self, *args, **kwargs):
        calls.append(1)
        raise Exception(PER_MINUTE)

    monkeypatch.setattr(ChatGoogleGenerativeAI, "_generate", flaky, raising=False)
    monkeypatch.setattr(judge, "_retry_after", lambda exc, attempt: 0.0)

    chat = judge.rate_limited_langchain_gemini("gemini-3.1-flash-lite", "not-a-real-key")
    with pytest.raises(RuntimeError) as excinfo:
        chat.invoke("anything")

    assert not isinstance(excinfo.value, DailyQuotaExhausted)
    assert len(calls) == judge.MAX_ATTEMPTS, "a per-minute 429 must be retried, not abandoned"
