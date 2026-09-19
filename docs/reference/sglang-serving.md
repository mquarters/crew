# SGLang serving — reference

The Spark's serving stack lives in its own repository:

- **Upstream:** https://github.com/MiaAI-Lab/Qwen3.8-27B-SGLang-DGX-Spark
- **On the box:** `mquarters@sparky:/home/mquarters/Qwen3.8-27B-SGLang-DGX-Spark`
- **Pinned at:** `9fb18ed` (2026-09-18)
- **Launched with:** `./start-dflash.sh` → delegates to `start.sh`

That repo is not vendored here. What *is* recorded here is the **resolved runtime
configuration** — the flags the server actually ends up running with after
`start-dflash.sh` appends `EXTRA_ARGS` and `.env` supplies `DF_EXTRA`. Since
argparse takes the last occurrence, several flags appear more than once and the
final value is not obvious from reading any single file.

## Effective configuration (verified 2026-09-18)

Captured from the running process, not from the scripts.

| Flag | Effective value | Why the crew cares |
|---|---|---|
| `--served-model-name` | `qwen3.8-27b-sglang` | The model string in `deploy/litellm/config.yaml` |
| `--port` | `8888` | Not SGLang's default 30000 |
| `--context-length` | `262144` | Constitution + issue history fit comfortably |
| `--reasoning-parser` | `qwen3` | **Load-bearing.** Puts thinking in `reasoning_content` so it never contaminates tool calls or JSON |
| `--tool-call-parser` | `qwen3_coder` | **Load-bearing.** Without it, agents get prose instead of `tool_calls` |
| `--grammar-backend` | *(not set — default)* | **Load-bearing by default.** Constrained JSON passes 5/5 on the default backend; an explicit override could regress it |
| `--max-running-requests` | `8` | The crew's real concurrency ceiling — see below |
| `--speculative-algorithm` | `DFLASH` | Overrides `EAGLE` from `start.sh` |
| `--speculative-draft-model-path` | `z-lab/Qwen3.8-27B-DFlash2` @ `50307d4c` | Block-diffusion draft |
| `--model-path` | `RadixArk/Qwen3.8-27B-NVFP4-BF16-LMHead` | NVFP4 target, BF16 lm_head |
| `--mem-fraction-static` | `0.80` | Appears three times (`0.95` → `0.90` → `0.80`); `.env`'s `DF_EXTRA` wins |
| `--mamba-radix-cache-strategy` | `extra_buffer` | Overrides `extra_buffer_lazy` |
| `--kv-cache-dtype` | `fp8_e4m3` | |
| `--max-mamba-cache-size` | `40` | From `DF_EXTRA` |

`.env` on the box supplies:

```
CONTEXT_LENGTH=262144
MAX_CONCURRENT_REQUESTS=8
DF_EXTRA="--mem-fraction-static 0.80 --max-mamba-cache-size 40"
```

### On `--mem-fraction-static`

`start-dflash.sh` warns that `0.95` hard-rebooted the GB10 once during draft
graph capture, and that the cookbook pins `0.80` because `0.85` trips DGX OS
earlyoom. The running value is `0.80`. **If serving is re-tuned, this is the
flag most likely to take the box down** — the crew has no way to detect or
recover from a host reboot mid-tick.

## What the crew depends on

If serving is iterated to better suit CrewAI, three things must not regress.
`crew doctor` checks all three, so run it after any change:

1. **`--tool-call-parser qwen3_coder`** — agents cannot use tools without it.
2. **Constrained JSON decoding** — the entire `output_pydantic` typed-state
   design rests on it. It currently works on the default grammar backend.
3. **`--reasoning-parser qwen3`** — keeps thinking out of the payload.

## Concurrency: the one real mismatch

`--max-running-requests 8` is the ceiling. The board's WIP limits currently
allow more agents than that to be active at once:

| Column | WIP limit |
|---|---|
| In Progress | 3 |
| Awaiting QA | 3 |
| QA | 3 |

Nine cards in flight, each with an agent, plus refinement work, exceeds eight
concurrent requests. SGLang **queues** rather than rejecting, so nothing breaks
— but tick latency degrades non-obviously, and a slow tick looks like a hung
agent rather than a saturated server.

Two ways to resolve it, neither urgent until the crew actually runs hot:

- Raise `--max-running-requests` (costs KV cache headroom), or
- Cap the crew's own concurrency below 8 in `config/org.yaml`.

The second is safer on a box whose memory settings already sit near a known
crash boundary.

## Reasoning: off for implementation, on for refinement

This model reasons **circularly on long structured generations**. The reasoning
compounds rather than converging, exhausts the token budget, and the answer
never arrives.

Measured on the same story, same schema, same prompt:

| | thinking ON | thinking OFF |
|---|---|---|
| Wall clock | 27 minutes | **55 seconds** |
| Reasoning tokens | thousands | 0 |
| Valid output | none | a complete implementation |

So the Developer runs on `crew-code` with `enable_thinking: false`. Refinement
stays on `crew-local` with thinking on: those generations are short enough to
survive the pathology, and the epics and stories it produces are measurably
better for it.

This is the single largest performance decision in the system, and it is
model-specific. Re-measure it before changing the backend rather than carrying
the setting forward as though it were a general truth.

### Zombie requests

A killed client does **not** stop generation. Observed: the crew process gone,
and SGLang still generating at 24 tok/s with 13k tokens consumed, five minutes
later. LiteLLM holds the upstream connection, so the server never learns the
client left.

Shorter generations make this mostly self-correcting — a 55-second job drains
before it matters, where a 27-minute one does not. Watch `num_running_reqs`
against whether any crew process is alive; a request with no client is a
recognisable and specific failure shape.

## Reasoning cost

Qwen3.8 thinks before answering — 22 reasoning tokens to answer "reply with the
word ready". Thinking is worth paying for on judgment-heavy roles and is pure
waste on mechanical ones. `chat_template_kwargs: {"enable_thinking": false}` is
honoured (verified: 0 reasoning tokens), which is what the `crew-mechanical`
LiteLLM alias exists to exploit.

The failure mode to recognise: too small a `max_tokens` returns **empty
`content`** with `finish_reason="length"`, because the budget went to thinking.
It looks like the model returning nothing for no reason.
