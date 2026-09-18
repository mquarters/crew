# Agent credentials

The crew must not act as you. Two separate problems make this necessary, and
they need different fixes.

## Problem 1 — blast radius

A token that can administer a repository can change branch protection, transfer
it, or delete it. An agent holding that is one bad tool call away from something
irreversible. The crew needs to write code, issues, pull requests and board
fields, and nothing else.

## Problem 2 — GitHub forbids approving your own pull request

This is the sharper one, and it is not obvious until PRs start flowing.

If the Developer agent opens a PR **as you** and the Code Reviewer agent
approves it **as you**, GitHub rejects the approval. The PR can then only merge
by admin bypass — which makes the review gate theatre.

So the crew needs an identity that is *not* yours. One fine-grained token does
not solve this; it needs a second account.

---

## The constraint that shapes everything below

**Fine-grained tokens cannot access Projects v2 owned by a user account.**
GitHub documents this as a known gap: the `Projects` permission exists only
under *organization* permissions, and there is no account-level equivalent.

Since the board *is* the orchestrator, this is not a detail the crew can work
around. It forces a choice.

| | Board owner | Token | Blast radius |
|---|---|---|---|
| **A** | Organization | Fine-grained, per-repo | Exactly the two repos, exactly four permissions |
| **B** | Your account | Classic, `project` + `public_repo` | Every public repo you own, now and in future |

### Option A — move the board to a free organization (recommended)

A free GitHub organization costs nothing and fixes several things at once:

- fine-grained tokens gain `Projects: Read and write` under organization
  permissions, so the crew's credential can finally be scoped properly;
- organizations support **project templates**, so future boards clone from a
  configured one rather than being rebuilt field by field;
- a machine account becomes an ordinary org member with the **Write** role,
  which is the natural way to solve the self-approval problem below;
- repository and project ownership stop being tangled up with your personal
  account.

Migration is mostly scriptable: repositories transfer via the API (old URLs
redirect), and `copyProjectV2` accepts an `ownerId`, so the board copies across
with its fields intact. Only creating the organization itself is manual —
GitHub has no API for it.

### Option B — a classic token, kept as narrow as possible

Works today with no migration. If you take it, grant **only**:

- `project` — the board
- `public_repo` — write access to public repositories

**Not** full `repo`. That scope reaches every private repository you can see,
which is the blast radius this whole exercise exists to avoid.

The residual risk is real but bounded while you own few public repos: the token
can write to all of them, and that set grows silently as you create more.

---

## Fine-grained token permissions (Option A)

Create at **https://github.com/settings/personal-access-tokens/new**

| Setting | Value |
|---|---|
| Token name | `crew-agents` |
| Expiration | 90 days (calendar a rotation) |
| Resource owner | **the organization** |
| Repository access | Only select repositories → `crew`, `sprint-metrics` |

**Repository permissions:**

| Permission | Level | Why |
|---|---|---|
| Contents | Read and write | branches, commits, worktrees |
| Issues | Read and write | cards, comments, the audit trail |
| Pull requests | Read and write | opening and reviewing PRs |
| Metadata | Read-only | mandatory, granted automatically |

**Organization permissions:**

| Permission | Level | Why |
|---|---|---|
| Projects | Read and write | the board is the orchestrator |

**Do not grant Administration.** That is the permission that would let an agent
disable branch protection.

Add the token to `.env` (gitignored; the pre-commit hook blocks tokens anyway):

```
GITHUB_TOKEN=github_pat_...
```

Verify with `crew auth`. It checks the positives — repo access, push, issues,
the board — and the negative that actually matters: that the token is
**refused** when it tries to administer the repository.

### What this does and does not fix

It fixes blast radius. It does **not** make branch protection bind you: bypass
is decided by your repository role, not by the token's permissions. That is why
`enforce_admins` is switched on for `sprint-metrics` — that, not the token, is
what makes "agents never push to main" true there.

## Step 2 — a machine account (before Phase 3)

Needed to solve problem 2. Create a second GitHub account (e.g. `mquarters-crew`),
invite it as a collaborator with **Write** — never Admin — on both repos, and
issue the fine-grained token above from *that* account instead.

Then:

- the crew authors commits and PRs as the machine account,
- **you** supply the approving review, satisfying branch protection honestly,
- the audit trail attributes the work to the crew rather than to you,
- revoking the crew's access is one click and does not touch your own.

`crew auth` warns rather than fails when the token's login differs from the
board owner, because at that point the mismatch is the intended state.

## Rotation

Fine-grained tokens expire. When one does, every agent action fails with 401 at
once — which looks like a total crew outage. `crew auth` distinguishes the two:
a rejected token reports `token rejected (401)` rather than a permission error.
