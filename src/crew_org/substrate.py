"""Phase 0: prove the inference substrate before anything is built on it.

Two capabilities decide whether the typed-state architecture holds, and both are
launch-flag dependent under SGLang:

  * tool calling      — needs a Qwen-matched --tool-call-parser
  * constrained JSON  — needs a grammar backend (xgrammar / outlines)

These probes talk raw OpenAI-compatible HTTP rather than going through CrewAI or
LiteLLM, so a failure is attributable to the server and not to a layer above it.
"""

from __future__ import annotations

import json
from enum import StrEnum

import httpx
from pydantic import BaseModel

# SGLang's own default is 30000 and vLLM's is 8000, but a served instance can
# be anywhere — this Spark runs on 8888. Assume nothing; probe.
CANDIDATE_PORTS = (8888, 30000, 8000, 8080, 40000)
STRUCTURED_TRIALS = 5
TIMEOUT = 30.0


class Status(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    WARN = "warn"
    SKIP = "skip"


class ProbeResult(BaseModel):
    check: str
    status: Status
    detail: str
    hint: str | None = None


def discover(host: str, ports: tuple[int, ...] = CANDIDATE_PORTS) -> str | None:
    """Find an OpenAI-compatible endpoint on `host`. Returns a base URL or None."""
    for port in ports:
        url = f"http://{host}:{port}/v1"
        try:
            r = httpx.get(f"{url}/models", timeout=3.0)
            if r.status_code == 200:
                return url
        except httpx.HTTPError:
            continue
    return None


def probe_models(base_url: str) -> tuple[ProbeResult, str | None]:
    """0a/0b — endpoint reachable, and what is it actually serving?"""
    try:
        r = httpx.get(f"{base_url}/models", timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json().get("data", [])
    except httpx.HTTPError as exc:
        return (
            ProbeResult(
                check="endpoint reachable",
                status=Status.FAIL,
                detail=f"{type(exc).__name__}: {exc}",
                hint="Is SGLang running, and is the host/port right? Try --host to auto-discover.",
            ),
            None,
        )
    if not data:
        return (
            ProbeResult(
                check="endpoint reachable",
                status=Status.FAIL,
                detail="/v1/models returned no models",
                hint="The server is up but serving nothing.",
            ),
            None,
        )
    served = data[0].get("id")
    return (
        ProbeResult(
            check="endpoint reachable",
            status=Status.PASS,
            detail=f"serving {served!r}" + (f" (+{len(data) - 1} more)" if len(data) > 1 else ""),
        ),
        served,
    )


def _chat(base_url: str, model: str, **body) -> dict:
    r = httpx.post(
        f"{base_url}/chat/completions",
        json={"model": model, **body},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    return r.json()


def _reasoning_tokens(response: dict) -> int:
    """Reasoning token count, wherever this hop happens to put it.

    SGLang reports it at the top of `usage`; LiteLLM nests it under
    `completion_tokens_details`.
    """
    usage = response.get("usage") or {}
    details = usage.get("completion_tokens_details") or {}
    return usage.get("reasoning_tokens") or details.get("reasoning_tokens") or 0


def probe_chat(base_url: str, model: str) -> ProbeResult:
    """0b — a basic completion round-trips *with content*.

    Reasoning models spend the token budget thinking before they answer, so a
    small `max_tokens` returns an empty `content` and a `length` finish. That
    is a failure, not a pass — checking only for the absence of an exception
    would wave it through.
    """
    try:
        out = _chat(
            base_url,
            model,
            messages=[{"role": "user", "content": "Reply with the single word: ready"}],
            max_tokens=512,
            temperature=0.0,
        )
        choice = out["choices"][0]
        text = choice["message"].get("content") or ""
        finish = choice.get("finish_reason")
        reasoning = _reasoning_tokens(out)
    except Exception as exc:  # noqa: BLE001
        return ProbeResult(
            check="chat completion", status=Status.FAIL, detail=f"{type(exc).__name__}: {exc}"
        )

    if not text.strip():
        return ProbeResult(
            check="chat completion",
            status=Status.FAIL,
            detail=f"empty content (finish_reason={finish!r}, {reasoning} reasoning tokens)",
            hint="The model answered with nothing. If it spent the budget reasoning, raise "
            "max_tokens; agents need headroom above their thinking.",
        )

    note = f" after {reasoning} reasoning tokens" if reasoning else ""
    return ProbeResult(
        check="chat completion",
        status=Status.PASS,
        detail=f"responded {text.strip()[:30]!r}{note}",
    )


def probe_context(base_url: str, model: str) -> ProbeResult:
    """0e — how much context is there for the constitution plus issue history?"""
    try:
        r = httpx.get(f"{base_url}/models", timeout=TIMEOUT)
        r.raise_for_status()
        entry = next(m for m in r.json()["data"] if m.get("id") == model)
        length = entry.get("max_model_len")
    except Exception as exc:  # noqa: BLE001
        return ProbeResult(
            check="context length", status=Status.WARN, detail=f"could not determine: {exc}"
        )
    if not length:
        return ProbeResult(
            check="context length",
            status=Status.WARN,
            detail="not reported by this endpoint",
            hint="Expected when probing through a proxy — LiteLLM does not surface the "
            "backend's window. Probe the backend directly to confirm it.",
        )
    if length < 16384:
        return ProbeResult(
            check="context length",
            status=Status.WARN,
            detail=f"{length:,} tokens",
            hint="Tight for a constitution plus issue history. Expect truncation failures "
            "that look like the model being stupid.",
        )
    return ProbeResult(check="context length", status=Status.PASS, detail=f"{length:,} tokens")


def probe_thinking_control(base_url: str, model: str) -> ProbeResult:
    """Can reasoning be switched off per request?

    Not a gate — a cost lever. Thinking is worth paying for on judgment-heavy
    roles and waste on mechanical ones, but only if it can be controlled.
    """
    try:
        out = _chat(
            base_url,
            model,
            messages=[{"role": "user", "content": "Reply with the single word: ready"}],
            max_tokens=512,
            temperature=0.0,
            chat_template_kwargs={"enable_thinking": False},
        )
        reasoning = _reasoning_tokens(out)
        text = out["choices"][0]["message"].get("content") or ""
    except Exception as exc:  # noqa: BLE001
        return ProbeResult(
            check="thinking control",
            status=Status.WARN,
            detail=f"request rejected: {type(exc).__name__}",
            hint="Reasoning cannot be disabled per request; every role pays for thinking.",
        )

    if reasoning == 0 and text.strip():
        return ProbeResult(
            check="thinking control",
            status=Status.PASS,
            detail="enable_thinking=false honoured (0 reasoning tokens)",
        )
    return ProbeResult(
        check="thinking control",
        status=Status.WARN,
        detail=f"still spent {reasoning} reasoning tokens",
        hint="Mechanical roles will pay for thinking they do not need.",
    )


TOOL_SPEC = [
    {
        "type": "function",
        "function": {
            "name": "set_card_status",
            "description": "Move a board card to a new status column.",
            "parameters": {
                "type": "object",
                "properties": {
                    "card": {"type": "integer", "description": "Issue number"},
                    "status": {"type": "string", "description": "Target column"},
                },
                "required": ["card", "status"],
            },
        },
    }
]


def probe_tool_calling(base_url: str, model: str) -> ProbeResult:
    """0c — does the server emit well-formed tool_calls?

    Without this, CrewAI agents cannot reliably use tools and the delivery tier
    is not viable.
    """
    try:
        out = _chat(
            base_url,
            model,
            messages=[{"role": "user", "content": "Move card 42 to the Awaiting QA column."}],
            tools=TOOL_SPEC,
            tool_choice="auto",
            max_tokens=256,
            temperature=0.0,
        )
        message = out["choices"][0]["message"]
        calls = message.get("tool_calls")
    except Exception as exc:  # noqa: BLE001
        return ProbeResult(
            check="tool calling",
            status=Status.FAIL,
            detail=f"{type(exc).__name__}: {exc}",
            hint="Server rejected the request. Launch SGLang with a Qwen tool-call parser.",
        )

    if not calls:
        return ProbeResult(
            check="tool calling",
            status=Status.FAIL,
            detail=f"no tool_calls; model replied in prose: {str(message.get('content'))[:60]!r}",
            hint="Launch SGLang with --tool-call-parser qwen25 (or the parser matching this "
            "model). Until then, agents cannot use tools reliably.",
        )

    try:
        args = json.loads(calls[0]["function"]["arguments"])
    except (KeyError, json.JSONDecodeError) as exc:
        return ProbeResult(
            check="tool calling",
            status=Status.FAIL,
            detail=f"tool_calls present but arguments unparseable: {exc}",
            hint="The tool-call parser is mismatched to the model's output format.",
        )

    if "card" not in args or "status" not in args:
        return ProbeResult(
            check="tool calling",
            status=Status.WARN,
            detail=f"called with incomplete arguments: {args}",
            hint="Parsing works but the model omits required fields; tighten tool descriptions.",
        )
    name = calls[0]["function"]["name"]
    return ProbeResult(check="tool calling", status=Status.PASS, detail=f"called {name}{args}")


STORY_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "points": {"type": "integer"},
        "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["title", "points", "acceptance_criteria"],
    "additionalProperties": False,
}


def probe_structured_output(
    base_url: str, model: str, trials: int = STRUCTURED_TRIALS
) -> ProbeResult:
    """0d — does constrained decoding return schema-valid JSON, repeatably?

    The whole output_pydantic typed-state design rests on this. One lucky pass
    proves nothing, so it is run repeatedly.
    """
    ok = 0
    last_error = ""
    for _ in range(trials):
        try:
            out = _chat(
                base_url,
                model,
                messages=[
                    {
                        "role": "user",
                        "content": "Write one user story about reporting sprint cycle time.",
                    }
                ],
                response_format={
                    "type": "json_schema",
                    "json_schema": {"name": "story", "schema": STORY_SCHEMA, "strict": True},
                },
                max_tokens=512,
                temperature=0.0,
            )
            payload = json.loads(out["choices"][0]["message"]["content"])
            if all(k in payload for k in STORY_SCHEMA["required"]):
                ok += 1
            else:
                last_error = f"missing keys in {sorted(payload)}"
        except Exception as exc:  # noqa: BLE001
            last_error = f"{type(exc).__name__}: {exc}"

    if ok == trials:
        return ProbeResult(
            check="constrained JSON", status=Status.PASS, detail=f"{ok}/{trials} schema-valid"
        )
    if ok == 0:
        return ProbeResult(
            check="constrained JSON",
            status=Status.FAIL,
            detail=f"0/{trials} schema-valid — {last_error}",
            hint="Launch SGLang with a grammar backend (--grammar-backend xgrammar). "
            "Without it, every task degrades to the SCHEMA repair path.",
        )
    return ProbeResult(
        check="constrained JSON",
        status=Status.WARN,
        detail=f"only {ok}/{trials} schema-valid — {last_error}",
        hint="Unreliable structured output. Expect frequent SCHEMA retries; consider a "
        "grammar backend or tighter schemas.",
    )


def probe_crew(base_url: str, model: str) -> ProbeResult:
    """The whole stack: CrewAI -> proxy -> server -> a validated Pydantic model.

    Every earlier probe can pass while this fails, because CrewAI adds its own
    prompt scaffolding and output parsing on top. Costs real tokens and a minute
    or so, so it is opt-in.
    """
    try:
        from crewai import Agent, Crew, Process, Task  # noqa: PLC0415
        from pydantic import BaseModel as _BaseModel  # noqa: PLC0415

        from crew_org.llm import build_llm  # noqa: PLC0415

        class _Probe(_BaseModel):
            title: str
            stories: list[str]

        alias = model.removeprefix("openai/")
        agent = Agent(
            role="Product Owner",
            goal="Turn a goal statement into one coherent epic.",
            backstory="You decompose by outcome, never by architectural layer.",
            llm=build_llm(alias, base_url=base_url),
            verbose=False,
        )
        task = Task(
            description=(
                "Goal: report how the crew is performing.\n"
                "Propose exactly ONE epic that delivers part of it."
            ),
            expected_output="An epic title and candidate story titles.",
            agent=agent,
            output_pydantic=_Probe,
        )
        result = Crew(
            agents=[agent], tasks=[task], process=Process.sequential, verbose=False
        ).kickoff()
    except Exception as exc:  # noqa: BLE001
        return ProbeResult(
            check="crewai round-trip",
            status=Status.FAIL,
            detail=f"{type(exc).__name__}: {exc}"[:160],
            hint="The substrate is sound but CrewAI cannot drive it. Check the alias "
            "exists on the proxy and that max_tokens leaves room above reasoning.",
        )

    epic = getattr(result, "pydantic", None)
    if epic is None:
        return ProbeResult(
            check="crewai round-trip",
            status=Status.FAIL,
            detail="crew ran but returned no typed output",
            hint="output_pydantic did not parse. Expect the SCHEMA repair path to carry "
            "the load, and tighten task descriptions.",
        )
    usage = getattr(result, "token_usage", None)
    cost = f", {usage.total_tokens} tokens" if usage else ""
    return ProbeResult(
        check="crewai round-trip",
        status=Status.PASS,
        detail=f"typed output: {epic.title!r}, {len(epic.stories)} stories{cost}",
    )


def run_all(base_url: str, model: str | None = None, *, deep: bool = False) -> list[ProbeResult]:
    """Run the Phase 0 gate in order. Later checks are skipped if earlier ones fail."""
    reach, served = probe_models(base_url)
    results = [reach]
    if reach.status is Status.FAIL:
        for check in (
            "chat completion",
            "tool calling",
            "constrained JSON",
            "context length",
            "thinking control",
        ):
            results.append(
                ProbeResult(check=check, status=Status.SKIP, detail="endpoint unreachable")
            )
        return results

    target = model or served
    assert target is not None
    chat = probe_chat(base_url, target)
    results.append(chat)
    if chat.status is Status.FAIL:
        for check in ("tool calling", "constrained JSON", "context length", "thinking control"):
            results.append(
                ProbeResult(check=check, status=Status.SKIP, detail="chat completion failed")
            )
        return results

    results.append(probe_tool_calling(base_url, target))
    results.append(probe_structured_output(base_url, target))
    results.append(probe_context(base_url, target))
    results.append(probe_thinking_control(base_url, target))
    if deep:
        results.append(probe_crew(base_url, target))
    return results
