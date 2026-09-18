# crew

An agile engineering organization run as CrewAI agents, orchestrated by a
GitHub Projects v2 board.

The board is the orchestrator. Crews are stateless workers that claim a card,
perform exactly one state transition, and write the result back to the issue.
The process is a reconciliation loop: idempotent, restartable, and auditable by
reading the board.

## Roles

The human is **Product Sponsor** and nothing else: sets goals, approves epics,
accepts the increment at sprint review. Refinement, estimation, sprint
mechanics, implementation, review, QA, merge, and release notes are executed by
agents.

## Operating rules

Read [`docs/ways-of-working.md`](docs/ways-of-working.md) — the constitution
every agent follows. Process constants live in
[`src/crew_org/config/org.yaml`](src/crew_org/config/org.yaml).

## Usage

```
crew tick             # drain every actionable card until the board is stable
crew sprint start
crew sprint close
```

A tick runs to quiescence. The human controls when the process runs, not the
individual transitions between states.

## Inference

Primary backend is a local DGX Spark serving Qwen3.8-27B under SGLang, fronted
by a LiteLLM proxy. Escalation for genuinely hard problems runs through headless
Claude Code (`claude -p`) on an existing subscription — **no `ANTHROPIC_API_KEY`
is configured, by design**, so escalation cannot incur metered API charges.

Escalation is budgeted and classified: schema and scope failures may never
escalate. See §9 of the constitution.
