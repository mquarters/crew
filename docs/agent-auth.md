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

## Step 1 — a fine-grained token (do this now)

Create at **https://github.com/settings/personal-access-tokens/new**

| Setting | Value |
|---|---|
| Token name | `crew-agents` |
| Expiration | 90 days (calendar a rotation) |
| Repository access | **Only select repositories** → `crew`, `sprint-metrics` |

**Repository permissions** — grant exactly these, and nothing else:

| Permission | Level | Why |
|---|---|---|
| Contents | Read and write | branches, commits, worktrees |
| Issues | Read and write | cards, comments, the audit trail |
| Pull requests | Read and write | opening and reviewing PRs |
| Metadata | Read-only | mandatory, granted automatically |

**Account permissions** — one only:

| Permission | Level | Why |
|---|---|---|
| Projects | Read and write | the board is the orchestrator; this is *not* part of repository permissions |

**Do not grant Administration.** That is the permission that would let an agent
disable branch protection.

Then add it to `.env` (which is gitignored, and the pre-commit hook blocks
tokens anyway):

```
GITHUB_TOKEN=github_pat_...
```

Verify:

```
crew auth
```

It checks the positives — repo access, push, issues, the project board — and
the negative that actually matters: that the token is **refused** when it tries
to administer the repository. An over-privileged token is indistinguishable from
a correct one until the day an agent uses it.

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
