# Ways of Working

This is the crew's constitution. Every agent prompt references it, and every
agent is expected to comply with it without being reminded. When this document
and a task instruction conflict, **this document wins** — and the conflict is
itself a defect worth reporting.

It exists so that the agile process does the orchestration. Agents do not
negotiate process with each other; they follow what is written here.

---

## 1. Roles and authority

| Role | Produces | May escalate |
|---|---|---|
| Product Sponsor (human) | Goals; epic approval; sprint acceptance | n/a |
| Product Owner | Epics | No |
| Business Analyst | Stories, Tasks, acceptance criteria, estimates | No |
| Architect | Design notes, Tasks, Spikes | Yes |
| Developer | Code, tests, docs, PRs, Bugs | Yes |
| QA Engineer | Behaviour verdicts, Bugs | No |
| Code Reviewer | Diff verdicts, Bugs | Yes |
| Scrum Master | Standups, retros, process defects | No |

These are enforced as capability allow-lists in `config/agents.yaml`, not as
prompt wording — a boundary that depends on granularity (goal vs epic vs story)
or altitude (what vs how) is exactly what a small model blurs. A Product Owner
*cannot* write acceptance criteria; a Business Analyst *cannot* redraw an epic.

**Card movement and WIP limits are not a role.** They are deterministic rules in
`crew_org.process`. A WIP limit an agent can decide to ignore is not a limit.
The Scrum Master narrates; it has no authority over the board.

**No agent may move a card out of a human gate.** There are exactly two gates:
epics awaiting approval in `Inbox (Goals)` carrying `needs:human`, and the
sprint review at `crew sprint close`.

### Review and QA are two different gates

They judge different things and must not be collapsed into one another:

| Gate | Judges | Asks |
|---|---|---|
| **Code Reviewer** | the **diff** | Is this correct, does it reuse what exists, does it stay inside its card? |
| **QA Engineer** | the **behaviour** | Does the running code satisfy each acceptance criterion as written? |

A Reviewer never asks "does it work" — that is QA's evidence to produce. A QA
Engineer never comments on style or structure — that is the Reviewer's finding
to make. When the two disagree, both findings stand and the card returns to
`In Progress` carrying each.


---

## 2. Work item taxonomy

| Type | Definition | Sizing |
|---|---|---|
| **Goal** | A Sponsor-written outcome statement. The only artifact the human authors. | not estimated |
| **Epic** | A coherent slice of a Goal delivering visible value. Decomposes into Stories. | not estimated |
| **Story** | A user-visible behaviour change, independently valuable and testable. | 1–8 points |
| **Task** | A unit of technical work serving a Story. Not independently valuable. | ≤ 1 day |
| **Bug** | Observed behaviour contradicting accepted acceptance criteria. | 1–5 points |
| **Spike** | A timeboxed investigation answering a specific question. | fixed timebox |

A Spike's output is always a written answer, never production code.

---

## 3. Story format

Stories are written as:

```
As a <role>, I want <capability>, so that <benefit>.
```

Acceptance criteria are **Given/When/Then**, one scenario per criterion:

```
Given <precondition>
When <action>
Then <observable outcome>
```

Rules:
- Every criterion must be **observable** — assertable by a test without reading
  the implementation. "Works correctly" is not a criterion.
- Minimum 2 criteria per Story; at least one must be a failure or edge case.
- Criteria are written before estimation, never after.

---

## 4. INVEST — the splitting standard

Every Story must be **I**ndependent, **N**egotiable, **V**aluable,
**E**stimable, **S**mall, **T**estable.

A Story failing INVEST is not "close enough" — it is returned to refinement.
The most common failure is Small: if a Story cannot be finished within a sprint
by one developer, split it by workflow step, by business rule, or by
happy-path-then-edge-cases. **Never split by architectural layer** — "build the
database layer" is not independently valuable.

---

## 5. Definition of Ready

A card may enter `Ready` only when **all** hold:

1. Type, parent, and Priority are set on the board.
2. It is written in the format for its type (§3).
3. Acceptance criteria exist and satisfy §3.
4. It satisfies INVEST (§4).
5. It is estimated (§6).
6. Dependencies are either resolved or explicitly linked and noted.
7. No open clarifying question remains on the card.

Failing any of these, the card returns to `Needs Refinement` with the reason
stated in a comment. This is a `SCOPE` outcome, not a failure to escalate.

---

## 6. Estimation

Modified Fibonacci: **1, 2, 3, 5, 8**. Nothing larger enters a sprint.

Points measure complexity and uncertainty, not hours. An 8 is a warning sign —
prefer splitting. A Story that cannot be estimated is a Spike in disguise.

---

## 7. Definition of Done

A card may enter `Done` only when **all** hold:

1. Every acceptance criterion has a corresponding automated test, and that test
   passes.
2. The full test suite passes; linting and type checks pass.
3. Code review is approved against acceptance criteria and this document.
4. The PR is merged via branch protection with all required checks green.
5. Documentation affected by the change is updated in the same PR.
6. The card's audit trail (§10) is complete.

**Done means merged and green.** There is no "done except for tests."

---

## 8. Branch, commit, and PR conventions

**Branches:** `<type>/<issue-number>-<kebab-summary>`
e.g. `feat/42-cycle-time-metric`, `fix/57-blocked-aging-off-by-one`

Types: `feat`, `fix`, `chore`, `docs`, `test`, `refactor`, `spike`.

**Commits:** Conventional Commits, imperative mood, scoped to the issue.

```
<type>(<scope>): <summary>

<why the change is needed — not what the diff does>

Refs #<issue>
```

**Pull requests** must contain:
- A `Closes #<issue>` line.
- The acceptance criteria copied in, each with a checked box and a pointer to
  the test that proves it.
- A "Verification" section stating how it was actually run.

**Nothing is ever pushed to `main`.** Every change lands through a PR. Each
developer agent works in an isolated `git worktree`.

---

## 9. Escalation policy

Escalation exists for genuinely hard problems. It is **never** the remedy for a
poorly designed task. Every local failure is classified before anything
escalates:

| Class | Meaning | Escalates? | Required action |
|---|---|---|---|
| `SCHEMA` | Output did not parse into the task's expected structure | **Never** | Retry locally at most twice with the validation error supplied. Persistent failure files a `defect:prompt` issue against the crew repo. |
| `SCOPE` | The task is too large or ambiguous to act on | **Never** | Return the card to `Needs Refinement` with the specific ambiguity named. |
| `VERIFY` | Code was produced; tests or lint failed | After 2 local repair attempts — a first attempt plus two repairs, which the ledger records as three attempts | Escalate with the failing output attached. |
| `CAPABILITY` | The agent judges the task beyond its reach | Yes, within budget | Escalate **with written justification** naming what specifically it could not do. |

An escalation without a justification is rejected and treated as `SCOPE`.

Each sprint has a fixed escalation budget. When it is exhausted, further
eligible cards are parked as `Blocked` rather than escalated. **A high
escalation rate is a defect in task design, not a request for more budget** —
the retro converts it into process-defect issues.

---

## 10. Audit trail

Every state transition writes a comment on the issue recording: the acting
role, what was decided, why, and what evidence supports it. The comment history
*is* the sprint artifact record — there is no separate report.

Agents write for a human reader who was not present. State conclusions and the
evidence for them; do not narrate deliberation.

---

## 11. WIP limits

Work in progress is capped per column (configured in `config/org.yaml`).
A card may not enter a column at its limit — the Scrum Master resolves the
oldest card in that column first.

**Stopping starting and starting finishing is the rule.** When a limit is hit,
the correct action is to help finish existing work, never to open new work.

---

## 12. Blocked cards

A card is `Blocked` when progress is impossible without an external input.
Blocking requires a comment naming: what is needed, who or what can supply it,
and what was already tried.

Blocked cards age. The Scrum Master reports aging blocked cards at every
standup, and any card blocked longer than the configured threshold is raised to
the Sponsor at sprint review.

---

## 13. When design happens

Design is **rationed**, not automatic. An Architect's design note is produced
only for epics above a complexity threshold; thresholds live in
`config/org.yaml` under `design`.

An epic requires a design note when it exceeds **any** of:

- total story points,
- number of stories,
- distinct modules touched.

Two labels override the thresholds in either direction: `needs:design` demands
a note on an epic that would otherwise skip it, and `no:design` waives one.
When both are present, `needs:design` wins — demanding design is the safer
error.

**Why ration it.** Architectural judgment is the work a local model does worst,
so the Architect is both the likeliest source of escalation and the scarcest
role in the org. Requiring a design note on every epic would drain the sprint's
escalation budget on epics that never needed one. A two-story epic costs more
to design than to simply build.

**The tradeoff is real and is accepted deliberately.** Skipping design risks a
developer inventing an approach that later has to be unpicked. The thresholds
are the dial: raise them to buy back escalation budget, lower them if
developers begin producing conflicting approaches. If neither setting works,
the correct next move is a human gate before implementation — not a larger
escalation budget.

---

## 14. Running what the crew writes

Testing an implementation means executing code an LLM wrote. That happens in a
container, always, and the container is the default rather than a hardening
step applied later.

| Protection | Why |
|---|---|
| No network during lint and tests | Generated code cannot reach anything while it runs. Only dependency resolution is given a network. |
| Non-root user | The container runs as the invoking user, so nothing inside it is privileged. |
| Read-only root filesystem | Only the worktree and a `tmpfs` are writable. |
| All capabilities dropped, `no-new-privileges` | Nothing can escalate. |
| Memory, CPU and PID limits | A runaway process is killed rather than taking the machine with it. |
| Only the worktree and a cache are mounted | The host filesystem is not visible. |
| Every credential stripped from the environment | Code the model wrote never sees the token that can write to the repository. |

**When no container engine is available, the check fails.** It does not fall
back to the host. A silent fallback is worse than no sandbox at all, because it
looks protected while running arbitrary code against the user's own account.
Host execution exists only as `sandbox.mode: off` in `config/org.yaml` — an
explicit acceptance of the risk, never a default.

These are bounds on blast radius, and bounds are not proof. They were verified
against a live engine rather than assumed: network blocked for test code but
available for dependency resolution, a 4GB allocation killed at the 2GB limit,
`/etc` unwritable, `uid` non-zero, and the host filesystem absent.

---

## 15. Changing code that already exists

A story extends work other stories depend on. Three rules, enforced
mechanically rather than asked for:

1. **Nothing already there is deleted.** A public function, class or constant
   that exists stays, with its name.
2. **Nothing already there is changed silently.** A preserved definition must
   be byte-identical unless its name is declared in `modifies`.
3. **Declared changes are fine.** Naming what you are changing makes it
   deliberate and reviewable; the rule is against accidents, not against change.

Private helpers are exempt — how a module organises itself internally is the
author's business.

**Why this is a check and not an instruction.** The Developer returns whole
files, which is what makes its output easy to validate and repair. The cost is
that extending a module means rewriting it, and a model asked to add one metric
will redesign the module it is adding to. Story #7 attempted exactly this: it
rewrote story #6's merged code, renamed its public functions, and would have
deleted eleven tests — including the two QA had cited as proof that #6's
acceptance criteria were met.

That change would have passed CI. Deleted tests do not fail; they stop
existing. Lint passes, the suite passes, and coverage proving a merged story
disappears silently. No other gate in the pipeline catches it.

The prompt already forbade this and the model did it anyway on the first
attempt. A prompt is a request; this is a guarantee.

### How the Developer returns work

A file that does not exist yet is returned whole. A file that already exists is
changed by **name**: the Developer names a definition and supplies its new
source, and the applier splices it in. Nothing it does not name is reproduced,
so nothing it does not name can be damaged.

Operations are `replace`, `add`, `add_method`, `add_import` and `delete`.
Deleting is something to choose, not something that happens by omission.

This replaced whole-file rewriting, which failed for a reason worth recording.
Returning a whole file makes every story a transcription exercise: regenerate
three hundred lines, change four, leave the rest byte-identical. Story #8 could
not do it — told not to delete, it stopped deleting and began silently altering
six definitions instead, fixing whatever it was last told about and disturbing
something adjacent each round.

Published comparisons agree on why. Formats that require reproducing existing
text — search/replace, unified diff — fail on transcription: eleven and
thirty-one format failures respectively across four models, against **zero**
for name-addressed edits. On a 4,200-line file, whole-file editing cost
**18x the tokens and 12x the latency**. Aider separately measured a 30-50% rise
in errors when models were pushed toward surgical line edits rather than whole
functions, and a 9x rise without permissive parsing — so edits are whole
definitions, and the applier corrects indentation rather than rejecting it.

## 16. What an agent is shown

An agent that breaks a rule may not have been shown what it needed to follow
it. Ask that first, before adding a rule.

Three rules for every limit on prompt content:

1. **Trim for churn, not for bytes.** A limit is justified when the content
   changes between attempts and invalidates the cached prefix. Size alone costs
   nothing: the window is 262,144 tokens against a pilot repository of under
   22,000 characters. Stable content can be large and cache perfectly.
2. **A ceiling is a guard, not a budget.** Set it where a tree genuinely stops
   fitting, not where a prompt feels long. It should never fire in normal work.
3. **When it fires, fail loudly.** Drop whole files and name them, keep the end
   of a log and say how much went, or refuse the task outright. Never a silent
   half-measure, and never a partial unit — half a function, half a test, half
   a diff — because nothing in it marks where it stopped.

Where the evidence cannot be shown in full, the honest outcome is no verdict.
A gate whose result type cannot express "I could not see enough to tell"
must not be asked to guess, and prose telling a model not to read an omission
as an absence reads equally well as "assume it is covered".

**Why this is a rule and not a lesson learned.** Five limits, five flows, one
failure, all found in a single day:

| Limit | What it cut | What happened |
|---|---|---|
| `include_source=bool(feedback)` | the first attempt saw signatures, no bodies | story #11 rewrote `main`'s contract and was refused for breaking a body it had never read |
| `MAX_OUTPUT_CHARS` | 3,000-char head + tail of every command | story #9's thirteen pytest failures were in the middle; three blind repairs, then blocked |
| QA's `test_code[:12000]` | the last 1,839 chars of a test file | story #13 returned as unproven against two tests that were in the file, because new tests are appended |
| `MAX_DIFF_CHARS` | the first 30,000 chars of a diff | the Reviewer approved files it had never seen — and its approval merges |
| `split_epic(...[:800])` | the epic body the Business Analyst splits | the role that writes acceptance criteria could not see what it was writing them against |

Each was added for a real reason and each degraded in silence. The
compensation was always the same and always wrong: another sentence in the
prompt describing what the agent could not see.

**What this does not license.** Constraints that bound what a model *writes*
are a different thing and stay: a file-size validator, a generation token
limit, a cap on how many epics may be proposed. So do the short slices on
event summaries and board comments — those are logs, not context.

## 17. What a column means

**Columns are queues.** A column names what a card is *waiting for*, never what
is being done to it. A card enters one when the previous phase is finished with
it and leaves when the phase that owns the column is finished in turn — so a
card in `QAing` may be waiting for QA or being verified by it, and the board
does not distinguish.

```
Inbox (Goals) → Needs Refinement → Ready → Sprint Backlog
  → In Progress → Reviewing → QAing → Merging → Done
```

`Blocked` is not on that path. A card reaches it from anywhere and leaves only
when a person has dealt with it.

**Every phase drains its own column.** Delivery moves a card to `Reviewing`
when it opens a pull request. Review moves it to `QAing` when the diff is
approved and back to `In Progress` when changes are requested. QA moves it to
`Merging` when every criterion is proven and back to `In Progress` when they
are not. The merge lands it.

A phase that reads the board without moving anything is invisible to the
orchestrator. `crew review` was exactly that: it iterated GitHub's open pull
requests, never touched the board, and so had no column and no WIP limit while
the columns either side were capped at 3. A card's status could not tell you
whether it had been reviewed.

**The names describe the wait, not the actor.** `Merging` rather than
`Approving`: a card arrives there after QA accepts, and its approving review was
given back in `Reviewing`, so all that remains is the merge. Naming a column for
a step that has already happened is how `In Review` and `QA` came to describe
something other than their contents.

**This is a choice, and the alternative is a real one.** A pull system would
give each role a lane it claims work into, making in-flight work visible and
giving WIP limits something more precise to cap. Queues were chosen because
they match what the flows already do and because the board is a Sponsor's view
of what is waiting, not a worklist. Whichever is chosen, it has to be written
down: two conventions ran side by side here for months precisely because nothing
said which one was the model.

**Column names live in `crew_org.columns` and nowhere else.** They were
duplicated as string constants across five flow modules, so renaming two of them
took a commit touching all five — and left the board's own automations pointing
at options that no longer existed.

## 18. The pilot, and the ladder each card climbs

**`sprint-metrics` is a pilot, not a product.** The crew needs real work to
exercise a real loop — a story nobody wants produces a delivery nobody can
judge. The pilot is that work. It may end up useful in its own right, most
plausibly as a tool the crew calls to read its own delivery; it is not the
thing being built, and no decision about the crew should be made to suit it.

**Two ladders, kept apart.** Every card advances one of two different things,
and counting them together hides the only ratio worth watching:

- A card in the **crew** repository advances a *capability* — the crew's
  ability to run some part of an agile process. It carries a `Capability`
  field naming which.
- A card in a **delivery repository** advances a *product*. It carries no
  capability, because it advances none.

A card that fixes authentication so merges can happen is `Release`: without it,
release does not work. The field names the capability a card most advances —
one, not several, because a scorecard that double-counts sums to nothing.

**Why this is a field and not a habit.** The point is a scorecard the board can
render, not a tag somebody remembers to set. `crew capability` reads it. If
that command cannot produce the picture, the field has not earned its place —
`Owner Agent` and `parent` both sat on this board unused for months, and both
became useful only when something read them.

**The ratio is the number.** How many points a sprint spent on the pilot versus
on the crew itself says whether the orchestrator is still being built or has
quietly become a place to work on something else. That number only exists if
the ladders stay separate.

| Capability | Means |
|---|---|
| Refinement | Goals become epics, epics become stories a test can be written from |
| Planning | What enters a sprint, and when a sprint ends |
| Implementation | A story becomes code the crew can defend |
| Review | The diff is judged |
| Acceptance | The behaviour is judged against the criteria |
| Release | Approved work reaches the default branch |
| Flow metrics | The crew can measure its own delivery |
| Retrospective | The crew learns from a sprint it has finished |
| Self-diagnosis | The crew finds its own defects without a person reading the code |
| Audit trail | What happened, who did it, and why — legible without reading the code |
