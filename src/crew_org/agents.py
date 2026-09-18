"""Construction of CrewAI agents from configuration.

Roles are data, not code: agents.yaml holds the role, goal, backstory, the
LiteLLM alias, the capability allow-list, and the constitution sections the role
is bound by. Changing how a role behaves is a config change reviewed like any
other, rather than a prompt edited in passing.
"""

from __future__ import annotations

from typing import Any

from crewai import Agent

from crew_org.constitution import excerpt
from crew_org.llm import build_llm
from crew_org.permissions import load_agents


def build_agent(key: str, spec: dict[str, Any] | None = None, **overrides: Any) -> Agent:
    """Build one agent by its key in agents.yaml."""
    spec = spec or load_agents().get(key)
    if spec is None:
        raise KeyError(f"no agent named {key!r} in agents.yaml")

    rules = excerpt(*spec.get("constitution", []))
    backstory = f"{spec['backstory'].strip()}\n\n{rules}"

    params: dict[str, Any] = {
        "role": spec["role"],
        "goal": spec["goal"].strip(),
        "backstory": backstory,
        "llm": build_llm(spec.get("llm", "crew-local")),
        # A refined role does one thing. Letting agents delegate re-introduces
        # the fuzzy hand-offs the board exists to replace.
        "allow_delegation": False,
        "verbose": False,
    }
    params.update(overrides)
    return Agent(**params)


def build_agents(*keys: str, **overrides: Any) -> dict[str, Agent]:
    specs = load_agents()
    return {key: build_agent(key, specs.get(key), **overrides) for key in keys}
