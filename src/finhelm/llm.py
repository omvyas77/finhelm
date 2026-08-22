"""Thin LLM client wrappers.

One place that loads `.env` and constructs clients, so the router, the generator and
(from Day 4) the eval judge do not each grow their own copy of key handling and retry
policy. Clients are cached because constructing one per request adds a TLS handshake to
every call for no benefit.
"""

from __future__ import annotations

import functools
import os
from pathlib import Path

from dotenv import dotenv_values, load_dotenv

ENV_PATH = Path(__file__).resolve().parents[2] / ".env"
load_dotenv(ENV_PATH)


def env(name: str) -> str:
    """Read a secret, treating an empty environment variable as absent.

    `load_dotenv` will not overwrite a name that already exists in the environment, and
    some shells export API keys as the empty string. The result is a var that is present,
    falsy, and silently shadows the real value in .env — which reads as "key not set"
    while the key sits right there in the file. Real values still win over .env, so CI
    secrets are unaffected.
    """
    value = os.environ.get(name) or dotenv_values(ENV_PATH).get(name)
    if not value:
        raise RuntimeError(f"{name} is not set; add it to {ENV_PATH}")
    return value

# Router classification is a single token of real work; paying Opus rates for it would
# dominate the latency budget of the thing it is meant to make cheaper.
ROUTER_MODEL = "claude-haiku-4-5-20251001"


@functools.lru_cache(maxsize=1)
def _anthropic():
    import anthropic

    return anthropic.Anthropic(api_key=env("ANTHROPIC_API_KEY"))


def claude(
    prompt: str,
    model: str,
    system: str | None = None,
    max_tokens: int = 1024,
    temperature: float = 0.0,
) -> str:
    kwargs = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
        # anthropic SDK 1.0.0 dropped `temperature` from the typed signature for the 4.6
        # models (sampling moved to output_config.effort), but the HTTP API still honours
        # it. Passing it as a typed kwarg raises TypeError; extra_body is the way through.
        # Temperature 0 is not cosmetic here — the ablation compares runs against each
        # other, and a sampling model would put noise in every delta.
        "extra_body": {"temperature": temperature},
    }
    if system:
        kwargs["system"] = system
    response = _anthropic().messages.create(**kwargs)
    return "".join(block.text for block in response.content if block.type == "text")
