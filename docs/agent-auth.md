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

## Why the board is organization-owned

**Fine-grained tokens cannot access Projects v2 owned by a user account.**
GitHub documents this as a known gap: the `Projects` permission exists only
under *organization* permissions, with no account-level equivalent.

Because the board *is* the orchestrator, a user-owned board would have forced a
classic token — coarse scopes reaching every repository the owner can see.
That is the blast radius this whole exercise exists to avoid.

So both repositories and the board live in the free organization
**`mqucifer`**. That also buys two things worth having:

- **Project templates**, which are an organization feature. Future boards clone
  from a configured one instead of being rebuilt field by field.
- **A home for the machine account** — an ordinary org member with the Write
  role, which is how the self-approval problem below gets solved.

## Fine-grained token permissions (Option A)

Create at **https://github.com/settings/personal-access-tokens/new**

| Setting | Value |
|---|---|
| Token name | `crew-agents` |
| Expiration | 90 days (calendar a rotation) |
| Resource owner | **`mqucifer`** |
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

Needed to solve problem 2. Create a second GitHub account (e.g. `mquarters-crew`), invite it to the
`mqucifer` organization with the **Write** role — never Owner — and issue the
fine-grained token above from *that* account instead.

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
