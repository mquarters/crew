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

# Reasoning models need headroom above their thinking. Too small a ceiling
# returns empty content with finish_reason="length", which looks like the model
# failing for no reason rather than like a budget problem.
DEFAULT_MAX_TOKENS = 4096


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
