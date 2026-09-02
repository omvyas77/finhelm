"""The PR gate: a 12-question stratified slice of the golden set, run as pytest.

This is deliberately not a miniature version of `run_eval.py`. The full eval answers 75
questions and reports numbers for a human to interpret; this asks a narrower question
that a machine can answer without judgement — *did this change break the pipeline?* — on
a slice small enough to run on every pull request.

**Which metric applies to which question type is the whole design here.**

For the ten positives, the assertion is about groundedness: the answer must be supported
by the retrieved context (faithfulness) and the context must be relevant to the question
(contextual relevancy).

For the two negatives, both of those metrics are undefined. The correct answer to "what
was Jamie Dimon's 2023 total compensation" — which is in the proxy, not in our corpus —
is a refusal, and a refusal makes no claims that could be faithful or unfaithful to
anything. Sending it to FaithfulnessMetric scores noise. So the negatives assert the
thing that actually matters for them: that the system abstained instead of inventing a
figure. A gate that ran faithfulness over all twelve would pass a system that confidently
fabricated Dimon's pay, because the fabrication is perfectly consistent with whatever
context got retrieved.

Marked `slow` because it costs real generation and judge calls; `-m "not slow"` keeps the
deterministic unit tests fast for local iteration.
"""

from __future__ import annotations

import functools
import json
import os

# DeepEval cancels a test case after DEEPEVAL_PER_ATTEMPT_TIMEOUT_SECONDS_OVERRIDE
# (207s by default) and this suite cannot finish one inside that, for a reason that is
# pure arithmetic rather than bad luck.
#
# judge.py paces the judge at 12 RPM to stay inside the Gemini free tier — one call every
# five seconds, globally, because the quota is per project per model. The served config
# hands the generator 16 passages, and a faithfulness check scores claims against every
# one of them, so a single test case needs well over forty paced calls: past 200 seconds
# before any judging is slow, merely spaced out.
#
# The failure that produces is maximally misleading: four `test_answer_is_grounded`
# failures with asyncio CancelledError tracebacks and no metric score anywhere, which
# reads exactly like the answers being unfaithful. They were not scored at all.
#
# Set to a day, which is "no limit" in every sense that matters here, because the setting
# cannot actually be switched off. DeepEval types it as Optional[float] with gt=0, and its
# default of None means *no override* — so leaving it unset restores the 207s that caused
# the cancellations. "None" and "" are both rejected by pydantic before any test runs.
#
# The first version of this line set the string "None" and was never executed locally
# before being pushed: the local run that motivated it predates the line. CI validated it
# on import and failed 11 of 13 with a ValidationError that named the setting, which is at
# least a loud failure rather than a quiet one.
#
# The real bounds are pytest's own and the 60-minute CI job ceiling, both of which fail
# loudly and say what they are. That ceiling is load-bearing and marked as such in the
# workflow.
os.environ.setdefault("DEEPEVAL_PER_ATTEMPT_TIMEOUT_SECONDS_OVERRIDE", "86400")
from pathlib import Path

import pytest

# Module scope, because RELEVANCY_THRESHOLD below is derived from it. This is the
# served config — the same object the service answers requests with — which is what
# keeps the gate scoring the system that ships.
from finhelm.api import CONFIG  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "evals" / "golden_set.jsonl"

# Stratified so a regression in any one question type trips the gate. Multi-hop and
# temporal are over-represented relative to their share of the golden set on purpose:
# they are where retrieval changes break first, and a gate weighted by population would
# spend most of its budget re-confirming the easy single-hop cases.
SLICE = {"single_hop": 4, "multi_hop": 3, "temporal": 3, "unanswerable": 1, "out_of_scope": 1}

# Calibrated against measured baseline rather than aspiration. A gate pinned above what
# the system currently does is red on every PR, and a permanently red gate gets ignored
# within a week — which is strictly worse than no gate, because it also hides the real
# regression when one arrives. Raise both as the pipeline improves.
FAITHFULNESS_THRESHOLD = 0.65

# Deliberately far below the 0.70 a quickstart would suggest, because contextual
# relevancy is *diluted by top_k_context* and therefore not portable across context
# sizes. The metric scores the fraction of supplied context that bears on the question;
# we supply 8 chunks and a typical question is answered by one of them, which caps the
# achievable score near 1/8 = 0.125 no matter how good retrieval is. Observed failures
# sat at 0.125, 0.12 and 0.16 — at or above that ceiling, i.e. retrieval doing as well as
# the metric permits while the threshold called it a failure.
#
# So this number is only meaningful alongside top_k_context=8, and comparing it against a
# run with a different context size would be comparing two different metrics. It is set
# to catch "almost nothing relevant came back", not to grade precision.
# Derived from the served context size rather than hardcoded, because the whole point of
# the paragraph above is that this number is not portable across context sizes — and it
# went stale exactly as predicted. 0.10 was calibrated when the service supplied 8 chunks.
# The service now supplies 16, which halves the achievable ceiling to about 1/16 = 0.0625,
# so the fixed threshold sat *above* what retrieval could reach and CI failed a question
# scoring 0.087 — a score that is in fact better than one relevant chunk in sixteen.
#
# 0.8/k keeps the same relationship to the ceiling that 0.10 had at k=8, and moves with
# top_k_context so a future budget change cannot silently reintroduce the same failure.
RELEVANCY_THRESHOLD = round(0.8 / CONFIG.top_k_context, 4)


def load_smoke_set() -> list[dict]:
    if not GOLDEN.exists():
        pytest.skip(f"golden set not built: {GOLDEN}")
    with GOLDEN.open() as fh:
        records = [json.loads(line) for line in fh if line.strip()]

    chosen: list[dict] = []
    for qtype, n in SLICE.items():
        # First N by id, which is stable across runs: the gate must test the same twelve
        # questions every time or a red build is ambiguous between "code regressed" and
        # "this run drew harder questions".
        chosen.extend([r for r in records if r["type"] == qtype][:n])
    return chosen


@functools.lru_cache(maxsize=1)
def judge_model():
    """Shared across tests so every judge call draws on one rate budget.

    Constructing a model per test would give each its own limiter and defeat the pacing —
    see src/finhelm/judge.py for why the budget has to be process-global.
    """
    from finhelm import llm
    from finhelm.api import CONFIG
    from finhelm.judge import rate_limited_gemini

    return rate_limited_gemini(CONFIG.judge_model, llm.env("GOOGLE_API_KEY"))


@pytest.fixture(scope="module")
def answers() -> dict[str, object]:
    """Answer all twelve questions once and share across tests.

    Per-test generation would re-answer the same question for each metric, tripling both
    the cost and the wall-clock time of the gate for no additional coverage.
    """
    from finhelm.api import CONFIG
    from finhelm.generate import answer

    # The *served* config, not a fresh Config().
    #
    # A bare Config() is chunking=fixed, retriever=dense, no reranking, bge-small and
    # top_k_context=8 — none of which this project ships. The service is pinned to
    # semantic + hybrid + rerank + contextual headers + bge-base at k=16, and for the life
    # of this file the PR quality gate was scoring a system nobody runs. It surfaced only
    # in CI, where the fixture has no `filings_fixed` index and FAISS said so; on a
    # developer machine that index exists, so the suite passed while measuring the wrong
    # thing.
    return {q["id"]: answer(q["question"], CONFIG) for q in load_smoke_set()}


# Twelve tests share two module-scoped fixtures, so an absent key produced twelve
# identical FAILED-fixture tracebacks with the real cause buried in each. This states it
# once. It is a skip rather than a failure because forked pull requests do not receive
# secrets by design — but the workflow asserts the secrets exist for same-repo PRs, so an
# unset key on a branch that should have one still fails, just not here.
def _absent(name: str) -> bool:
    """Resolve a key the way the application does, not just from os.environ.

    llm.env() falls back to .env, so a developer machine has keys that os.getenv alone
    cannot see. Checking only the environment would skip the entire judged suite locally
    while reporting nothing — the precise failure this guard exists to prevent.
    """
    from finhelm import llm

    try:
        return not llm.env(name)
    except RuntimeError:
        return True


_MISSING = [n for n in ("ANTHROPIC_API_KEY", "GOOGLE_API_KEY") if _absent(n)]
pytestmark = pytest.mark.skipif(
    bool(_MISSING), reason=f"judged gate needs {', '.join(_MISSING)}")

SMOKE = load_smoke_set()
POSITIVES = [q for q in SMOKE if q["type"] not in ("unanswerable", "out_of_scope")]
NEGATIVES = [q for q in SMOKE if q["type"] in ("unanswerable", "out_of_scope")]


@pytest.mark.slow
@pytest.mark.parametrize("question", POSITIVES, ids=lambda q: q["id"])
def test_answer_is_grounded(question, answers):
    """Positives that produced an answer must be supported by their context.

    An abstention is skipped rather than failed here, and counted by
    `test_abstention_within_budget` instead. The two failures are different bugs with
    different owners: an ungrounded answer is the generator inventing things, while an
    abstention is retrieval not finding evidence that exists. Collapsing them into one
    red test means the failure message never tells you which one happened, and at the
    current recall@5 (~0.3) it would also mean roughly half this gate is red for a reason
    no PR caused.
    """
    from deepeval import assert_test
    from deepeval.metrics import ContextualRelevancyMetric, FaithfulnessMetric
    from deepeval.test_case import LLMTestCase

    result = answers[question["id"]]
    if result.abstained:
        pytest.skip("abstained — counted by test_abstention_within_budget")

    contexts = [h.metadata.get("text", "") for h in result.retrieved]
    model = judge_model()
    assert_test(
        LLMTestCase(input=question["question"],
                    actual_output=result.answer,
                    retrieval_context=contexts),
        # async_mode=False on purpose. DeepEval defaults to firing a metric's sub-calls
        # concurrently; each metric here fans out into claim extraction plus one verdict
        # call per claim, so twelve parametrised tests burst well past the judge's rate
        # limit. The symptom was maddening: every test passed in isolation and half of
        # them failed together, which reads like flaky quality rather than throttling.
        [FaithfulnessMetric(threshold=FAITHFULNESS_THRESHOLD, model=model,
                            async_mode=False),
         ContextualRelevancyMetric(threshold=RELEVANCY_THRESHOLD, model=model,
                                   async_mode=False)],
    )


# Measured floor, not an aspiration: with the current retriever roughly half the positive
# slice abstains. The gate's job is to catch a change that makes this worse, so the budget
# sits just above where the baseline sits. Tightening it is the point of Day 3.
MAX_ABSTENTIONS = 6


@pytest.mark.slow
def test_abstention_within_budget(answers):
    """Retrieval regression check, expressed as a budget over the whole slice.

    Per-question assertions cannot express "retrieval got worse overall", which is the
    shape this regression actually takes.
    """
    abstained = [q["id"] for q in POSITIVES if answers[q["id"]].abstained]
    assert len(abstained) <= MAX_ABSTENTIONS, (
        f"{len(abstained)}/{len(POSITIVES)} answerable questions abstained "
        f"(budget {MAX_ABSTENTIONS}): {abstained}. Retrieval found no usable evidence."
    )


# Known-failing, tracked in notes/failures.md: q055 asks for Jamie Dimon's compensation
# and the system fabricates an itemised breakdown citing an 8-K cover page. Marked xfail
# rather than deleted or loosened, and strict=True so that fixing the generator turns this
# into a loud XPASS telling you to remove the marker. A red-on-every-PR gate gets ignored
# within a week; a tracked known failure does not.
KNOWN_HALLUCINATION = {"q055"}

# ...but only against the real corpus, and that distinction is load-bearing.
#
# The hallucination is corpus-dependent. q055 is an `unanswerable` question with no gold
# spans, and the fabrication is built out of a plausible-but-irrelevant passage the
# retriever surfaces from the full 24,650-chunk index. The CI fixture holds 1,903 chunks
# and does not contain that passage, so on the fixture the system abstains correctly and
# the strict xfail reports XPASS — which reads as "the bug is fixed, remove the marker".
#
# It is not fixed. The Day 4.4 final run against the real corpus still answers q055 with
# "$43,000,000 total compensation for fiscal year 2025" and does not abstain. Deleting the
# marker on the strength of a fixture run would retire the project's only tracked instance
# of the exact failure this system exists to prevent.
#
# So the expectation applies where it can be evaluated. On the fixture the question still
# runs and still has to pass; it is simply not *expected* to fail.
RUNNING_ON_FIXTURE = bool(os.getenv("FINHELM_DATA_DIR"))


@pytest.mark.slow
@pytest.mark.parametrize(
    "question",
    [pytest.param(q, marks=pytest.mark.xfail(
        strict=True, reason="known hallucination — see notes/failures.md (Day 2)"))
     if (q["id"] in KNOWN_HALLUCINATION and not RUNNING_ON_FIXTURE) else q
     for q in NEGATIVES],
    ids=lambda q: q["id"],
)
def test_refuses_when_answer_is_absent(question, answers):
    """Negatives: the only correct behaviour is to abstain.

    Asserted directly rather than via a judge. Whether the system said
    INSUFFICIENT_CONTEXT is a fact about the output, not a matter of opinion, and paying
    an LLM to have an opinion about it would add cost, latency and flakiness to the one
    assertion in this file that cannot be wrong.
    """
    result = answers[question["id"]]
    assert result.abstained, (
        f"{question['id']} ({question['type']}) produced an answer instead of abstaining. "
        f"The corpus does not contain this. Got: {result.answer[:200]!r}"
    )
