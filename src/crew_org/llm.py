"""LLM construction for agents.

Agents name a LiteLLM alias (`crew-local`, `crew-draft`, `crew-mechanical`),
never a model. The proxy owns the mapping to a backend, so re-pointing the crew
at different hardware is a config change rather than a code change.

Thinking is a cost lever, not a fixed tax: Qwen3.8 spends reasoning tokens
before answering, which is worth paying on judgment-heavy roles and is waste on
mechanical ones. `crew-mechanical` disables it — verified through the proxy at
2 completion tokens against 25.
"""

from __future__ import annotations

import os
from typing import Any

from crewai import LLM

DEFAULT_BASE_URL = "http://localhost:4000/v1"

# The proxy requires a value; the backend ignores it. This is not a credential.
PLACEHOLDER_KEY = "sk-not-used"

# Reasoning models need headroom ABOVE their thinking, and real refinement work
# thinks hard: a single "propose epics for this goal" task was measured spending
# 4,097 reasoning tokens and emitting 0 text tokens against a 4,096 ceiling —
# the whole budget consumed before the answer began. CrewAI retries, so it
# surfaces only as a slow task and a logged parse error, which is a genuinely
# confusing way to discover a token budget problem.
#
# The context window is 262,144, so headroom is cheap. Spend it.
DEFAULT_MAX_TOKENS = 16384


def base_url() -> str:
    return os.environ.get("CREW_LLM_BASE_URL", DEFAULT_BASE_URL)


def build_llm(alias: str = "crew-local", **overrides: Any) -> LLM:
    """Build a CrewAI LLM bound to a proxy alias.

    The `openai/` prefix tells LiteLLM the backend speaks the OpenAI protocol;
    it is not a statement about the provider.
    """
    params: dict[str, Any] = {
        "model": f"openai/{alias}",
        "base_url": base_url(),
        "api_key": os.environ.get("CREW_LLM_API_KEY", PLACEHOLDER_KEY),
        "max_tokens": DEFAULT_MAX_TOKENS,
    }
    params.update(overrides)
    return LLM(**params)


def health() -> tuple[bool, str]:
    """Is the proxy up?

    The proxy is deliberately project-scoped — it runs in Docker for the crew
    and is not general infrastructure — so it will not always be running. A tick
    that fails on a dead proxy should say so plainly rather than surfacing a
    connection error from somewhere deep inside an agent.
    """
    import httpx  # noqa: PLC0415

    url = base_url()
    try:
        r = httpx.get(f"{url}/models", timeout=5.0)
        r.raise_for_status()
    except Exception:  # noqa: BLE001
        return False, (
            f"LiteLLM proxy is not answering at {url}.\n"
            "  Start it with:  cd deploy/litellm && "
            "SGLANG_BASE_URL=http://gx10-3703.local:8888/v1 docker compose up -d"
        )
    aliases = [m["id"] for m in r.json().get("data", [])]
    if "crew-local" not in aliases:
        return False, f"Proxy is up but has no 'crew-local' alias. Serving: {aliases}"
    return True, f"proxy up — {', '.join(aliases)}"
