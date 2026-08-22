"""Why the Ragas judge cache is partitioned by model.

`ragas.cache._generate_cache_key` builds its key from the function qualname, the
positional args and the kwargs — after doing `if inspect.ismethod(func): args = args[1:]`
to drop `self`. For a bound method on an LLM wrapper, `self` is the only thing carrying
the model identity, so two different judges asked an identical question hash to the same
entry.

That is fine for the case the cache was designed for (re-scoring the same run with the
same judge) and silently wrong for the case this project actually hits: three judge models
were tried and rejected before the current one, and a re-score after such a switch would
replay the old judge's verdicts under the new judge's name in the output file.

These tests pin both halves — the collision that forces the workaround, and the
partitioning that contains it. The first one exists so that if a future ragas release puts
the model into the key, this fails and tells us the directory split is now redundant,
rather than leaving a workaround nobody dares remove.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from evals.ragas_runner import CACHE_ROOT, cache_dir  # noqa: E402


class _FakeLLM:
    """Stands in for LangchainLLMWrapper: the model lives on the instance."""

    def __init__(self, model: str) -> None:
        self.model = model

    def generate(self, prompt: str) -> str:
        return f"{self.model} verdict"


def test_cache_key_cannot_distinguish_two_judge_models():
    """The collision this whole workaround exists for.

    If this ever fails, ragas has started including the model in the key and
    `cache_dir()` can be simplified back to a single directory.
    """
    from ragas.cache import _generate_cache_key

    a, b = _FakeLLM("gemini-3.1-flash-lite"), _FakeLLM("gemini-3.5-flash")
    prompt = "Is this claim supported by the context?"

    key_a = _generate_cache_key(a.generate, (a, prompt), {})
    key_b = _generate_cache_key(b.generate, (b, prompt), {})

    assert key_a == key_b, (
        "ragas now distinguishes judge models in its cache key — the per-model cache "
        "directory in evals/ragas_runner.py is no longer needed"
    )


def test_cache_key_still_distinguishes_prompts():
    """The property the cache is actually for must survive the workaround."""
    from ragas.cache import _generate_cache_key

    llm = _FakeLLM("gemini-3.1-flash-lite")
    key_faith = _generate_cache_key(llm.generate, (llm, "Is this claim supported?"), {})
    key_relev = _generate_cache_key(llm.generate, (llm, "Is this context relevant?"), {})

    assert key_faith != key_relev


def test_each_judge_gets_its_own_directory():
    assert cache_dir("gemini-3.1-flash-lite") != cache_dir("gemini-3.5-flash")
    assert cache_dir("gemini-3.1-flash-lite").parent == CACHE_ROOT


def test_cache_dir_is_stable_for_one_model():
    """A resumed pass must land on the same directory or it re-pays for every row."""
    assert cache_dir("gemini-3.1-flash-lite") == cache_dir("gemini-3.1-flash-lite")


def test_model_names_with_slashes_stay_inside_the_cache_root():
    """`models/gemini-x` must not escape into `.cache/ragas/models/`."""
    path = cache_dir("models/gemini-x")
    assert path.parent == CACHE_ROOT
    assert "/" not in path.name
