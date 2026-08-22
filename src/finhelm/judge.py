"""A judge model that survives a free-tier rate limit.

DeepEval metrics do not make one call per test. FaithfulnessMetric extracts the claims
from an answer and then issues a verdict call per claim, so a single question can be five
to ten requests, and a twelve-question gate is comfortably over a hundred. The Gemini free
tier allows fifteen requests per minute. The gate therefore cannot be made reliable by
tuning concurrency — `async_mode=False` does not help, because the limit is per minute,
not per instant.

What made this expensive to diagnose is how it presented. Every test passed when run
alone and half of them failed when run together, which looks exactly like flaky judging.
The 429 arrived as `google.genai.errors.ClientError` from inside DeepEval, surfaced as a
test error rather than a skip, and in the Ragas path the same limit appeared as
`TimeoutError` and then as a `NaN` metric. Three different symptoms, one cause.

So this does two things the underlying client does not:

  - paces requests to stay under the documented limit, rather than discovering it by
    being rejected;
  - honours the `retryDelay` Google returns in the 429 body, instead of guessing a
    backoff that is usually either wasteful or still too fast.

The pacing is deliberately global rather than per-instance. The quota is enforced per
project per model, so two judge objects sharing a process share one budget, and a limiter
attached to an instance would let a metric that constructs its own model quietly double
the request rate.
"""

from __future__ import annotations

import re
import threading
import time

# Free-tier requests per minute, per project, per model. Kept slightly under the real
# limit: the server's window and ours are not aligned, so pacing exactly at 15 still
# collides at the boundary.
DEFAULT_RPM = 12

MAX_ATTEMPTS = 6

# The other free-tier quota, and the one pacing cannot help with: 500 generate_content
# requests per project per day. A full Ragas pass (~128 judge jobs) plus three runs of the
# DeepEval gate (~100 calls each) is enough to reach it, so budget the day's runs rather
# than discovering this at 60% through a scoring pass.
DEFAULT_RPD = 500

_DAILY_MESSAGE = (
    "judge daily quota exhausted for {model} "
    f"(free tier: {DEFAULT_RPD} requests/project/day).\n"
    "Retrying will not help — the window resets at midnight Pacific. Options:\n"
    "  - re-run later; Ragas' disk cache at .cache/ragas replays already-scored rows "
    "free, so a resumed pass only pays for what failed\n"
    "  - use a paid key, or a different judge model with its own quota\n"
    "Note the 429 body suggests 'retry in ~40s'. That advice is for the per-minute "
    "quota and is wrong here."
)

# Google returns the wait it wants in the error body ("Please retry in 33.42s"). Parsing
# it is worth the regex — a fixed backoff either sleeps far longer than required or
# retries into the same closed window.
_RETRY_DELAY = re.compile(r"retry in (\d+(?:\.\d+)?)s")

_lock = threading.Lock()
_next_allowed = 0.0


def _pace(rpm: int) -> None:
    """Block until the next request is allowed under the global rate budget."""
    global _next_allowed
    interval = 60.0 / rpm
    with _lock:
        now = time.monotonic()
        wait = max(0.0, _next_allowed - now)
        _next_allowed = max(now, _next_allowed) + interval
    if wait:
        time.sleep(wait)


def _retry_after(exc: Exception, attempt: int) -> float:
    match = _RETRY_DELAY.search(str(exc))
    if match:
        return float(match.group(1)) + 1.0
    return min(2.0 ** attempt, 60.0)


def _is_rate_limit(exc: Exception) -> bool:
    text = str(exc)
    return "429" in text or "RESOURCE_EXHAUSTED" in text


# The free tier enforces *two* quotas and only one of them is worth retrying. Pacing
# solves requests-per-minute; nothing solves requests-per-day except waiting for the
# reset or paying. Google returns both as a 429 carrying "Please retry in 43.8s", which
# is actively misleading for the daily cap — honouring it burns every attempt in four
# minutes and then reports a per-minute problem that isn't the real one.
_PER_DAY = re.compile(r"PerDay|per day|_requests_per_day", re.IGNORECASE)


def _is_daily_quota(exc: Exception) -> bool:
    return bool(_PER_DAY.search(str(exc)))


class DailyQuotaExhausted(RuntimeError):
    """Raised immediately on a per-day 429, because retrying cannot help today."""


def rate_limited_gemini(model: str, api_key: str, rpm: int = DEFAULT_RPM):
    """A DeepEval GeminiModel that paces itself and retries on 429.

    Returns an instance of a subclass rather than a wrapper object because DeepEval
    type-checks the model it is handed; duck-typing it fails at metric construction.
    """
    from deepeval.models import GeminiModel

    class RateLimitedGeminiModel(GeminiModel):
        def _call(self, fn, *args, **kwargs):
            last: Exception | None = None
            for attempt in range(MAX_ATTEMPTS):
                _pace(rpm)
                try:
                    return fn(*args, **kwargs)
                except Exception as exc:  # noqa: BLE001 - re-raised below if not a 429
                    if not _is_rate_limit(exc):
                        raise
                    if _is_daily_quota(exc):
                        raise DailyQuotaExhausted(_DAILY_MESSAGE.format(model=model)) from exc
                    last = exc
                    time.sleep(_retry_after(exc, attempt))
            raise RuntimeError(
                f"judge rate limited after {MAX_ATTEMPTS} attempts; "
                f"lower rpm (currently {rpm}) or use a paid key"
            ) from last

        def generate(self, *args, **kwargs):
            return self._call(super().generate, *args, **kwargs)

        async def a_generate(self, *args, **kwargs):
            # Paced synchronously on purpose. The point is to hold the whole process
            # under one shared budget; an async-aware limiter here would let concurrent
            # coroutines each acquire their own slot and reintroduce the burst.
            last: Exception | None = None
            for attempt in range(MAX_ATTEMPTS):
                _pace(rpm)
                try:
                    return await super().a_generate(*args, **kwargs)
                except Exception as exc:  # noqa: BLE001
                    if not _is_rate_limit(exc):
                        raise
                    if _is_daily_quota(exc):
                        raise DailyQuotaExhausted(_DAILY_MESSAGE.format(model=model)) from exc
                    last = exc
                    time.sleep(_retry_after(exc, attempt))
            raise RuntimeError(
                f"judge rate limited after {MAX_ATTEMPTS} attempts; "
                f"lower rpm (currently {rpm}) or use a paid key"
            ) from last

    return RateLimitedGeminiModel(model=model, api_key=api_key, temperature=0)
