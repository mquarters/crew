# Inference setup

Primary backend is the DGX Spark serving **Qwen3.8-27B under SGLang**, fronted
by a LiteLLM proxy. Escalation runs through headless Claude Code on an existing
subscription — there is deliberately **no `ANTHROPIC_API_KEY`** anywhere in this
project, so escalation cannot incur metered API charges.

## Verified configuration (2026-09-18)

| | |
|---|---|
| Endpoint | `http://gx10-3703.local:8888/v1` — **not** SGLang's default 30000 |
| Served name | `qwen3.8-27b-sglang` |
| Context | 262,144 tokens |
| Tool calling | working — emits well-formed `tool_calls` |
| Constrained JSON | working — 5/5 schema-valid |
| Reasoning | on by default; `chat_template_kwargs: {"enable_thinking": false}` disables it |

### Qwen3.8 is a reasoning model, and it changes how you budget tokens

Reasoning is emitted into a **separate `reasoning_content` field**, which is why
tool calling and constrained JSON both work — SGLang's reasoning parser is
correctly configured and thinking never contaminates the payload.

But the token budget is shared. At `max_tokens=16` a trivial prompt returned
**empty `content`** with `finish_reason="length"`: all sixteen tokens went to
thinking. The answer never arrived.

This is the failure mode to recognise, because it does not look like what it
is — it looks like the model returning nothing for no reason. Give every agent
headroom above its thinking; `crew doctor` now fails rather than passes on empty
content, so the gate catches it.

Thinking costs 22 tokens to answer "reply with the word ready". It is worth
paying on judgment-heavy roles (Architect, Reviewer, story splitting) and pure
waste on mechanical ones (routing, labelling) — hence the `crew-mechanical`
alias.

## 1. Launch SGLang on the Spark

Two flags decide whether this whole architecture works. Neither is on by
default, and both fail *silently* — agents just start behaving erratically.

```bash
python -m sglang.launch_server \
  --model-path <path-or-hf-id-for-Qwen3.8-27B> \
  --served-model-name qwen3.8-27b \
  --host 0.0.0.0 \
  --port 30000 \
  --tool-call-parser <qwen-parser> \
  --grammar-backend xgrammar \
  --context-length 32768
```

| Flag | Why it matters |
|---|---|
| `--tool-call-parser` | Without it the model replies in prose instead of emitting `tool_calls`, and **no agent can use a tool reliably**. The correct value depends on the model's chat template. |
| `--grammar-backend` | Enables constrained decoding. The entire `output_pydantic` typed-state design rests on this. Without it, every task degrades to the `SCHEMA` repair path. |
| `--context-length` | Agent prompts carry the constitution plus issue history. Too small and cards fail in ways that look like model stupidity. |

**Verify the parser name against your build** — do not copy a value blindly:

```bash
python -m sglang.launch_server --help | grep -A3 tool-call-parser
python -m sglang.launch_server --help | grep -A3 grammar-backend
```

### Qwen3 is a reasoning model

If this build emits thinking traces, they will contaminate tool-call and JSON
output unless parsed out. Check whether your SGLang version offers a
`--reasoning-parser` and set it to the Qwen variant. Symptom if wrong: probes
fail with "arguments unparseable" or JSON wrapped in prose.

## 2. Prove it before building on it

```bash
crew doctor --host <spark-hostname>       # discovers the port
crew doctor --base-url http://<host>:30000/v1
```

This is the Phase 0 gate. It checks, in order: endpoint reachable, chat
round-trip, **tool calling**, and **constrained JSON decoding** — the last run
repeatedly, because one lucky pass proves nothing.

Do not proceed past a `FAIL`. A substrate failure here reappears later as
unexplained agent failures that are far more expensive to diagnose.

## 3. Start the LiteLLM proxy

```bash
cd deploy/litellm
SGLANG_BASE_URL=http://<spark-host>:30000/v1 docker compose up -d
curl -s http://localhost:4000/v1/models | jq
```

The proxy centralizes model aliases, request logging, and rate policy. Note the
ordering: `crew doctor` talks to SGLang **directly**, so if the proxy is the
thing that is broken, the direct probe still passes and the fault is
attributable.

## 4. Escalation path

Escalation shells out to `claude -p` inside an isolated git worktree. It uses
your Claude subscription's OAuth credentials, so it cannot bill beyond what you
already pay. Hitting a usage limit parks the card as `Blocked` with
`escalation:rate-limited` and ends the tick gracefully — it never retries in a
loop.

Escalation is budgeted and classified; `SCHEMA` and `SCOPE` failures may never
escalate. See §9 of [the constitution](ways-of-working.md).
