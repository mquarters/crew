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
crew doctor --host <spark>     # prove the inference substrate before trusting it
crew auth                      # verify the crew's credential, and what it must NOT do
crew tick                      # goals become epics; approved epics become stories
crew sprint start              # fill the sprint from approved epics
crew deliver                   # implement a story and show the diff
crew deliver --land            # ...and actually open the pull request
crew review                    # review every open pull request
crew qa                        # verify delivered work against its criteria
crew sprint close              # merge what you approved, and report
```

A tick runs to quiescence. The human controls when the process runs, not the
individual transitions between states.

`crew deliver` is **dry by default**: the work is implemented and verified in a
sandbox, and the diff is written to `var/diffs/` rather than landed. Passing
`--land` is a deliberate act.

## The two gates

Everything else is the crew's. The Sponsor:

1. **Approves epics** — each proposed epic is a card in `Inbox (Goals)` labelled
   `needs:human`. Move it to `Needs Refinement` to approve, close it to reject.
   Approving an epic *is* the sprint scope decision; nothing asks again.
2. **Reviews the increment** at sprint close — which is also where pull
   requests get approved. The crew cannot approve its own work, so reviewing
   the increment *is* approving the pull requests that make it up: one pass at
   the end, rather than a decision per story.

Stories, estimates, sprint contents, implementation, review and merge are not
Sponsor decisions. A manager reading eight stories to understand a sprint has
been put back into the work.

## Running generated code

Testing what the crew writes means executing code an LLM wrote, so it runs in a
container with no network during tests, a non-root user, a read-only root
filesystem, dropped capabilities and hard resource limits. **When no container
engine is available the check fails rather than falling back to the host** — a
silent fallback looks protected while running arbitrary code as you. See §14 of
the constitution.

## Inference

Primary backend is a local DGX Spark serving Qwen3.8-27B under SGLang, fronted
by a LiteLLM proxy. Escalation for genuinely hard problems runs through headless
Claude Code (`claude -p`) on an existing subscription — **no `ANTHROPIC_API_KEY`
is configured, by design**, so escalation cannot incur metered API charges.

Escalation is budgeted and classified: schema and scope failures may never
escalate. See §9 of the constitution.
