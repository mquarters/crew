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

And then it needs a *third*. The rule is about the identity that opened the
pull request, not about you: once the Developer opens PRs as `crew[bot]`, the
Code Reviewer reviewing as `crew[bot]` is refused in exactly the same way. It
still posts — as `COMMENTED` — so nothing errors, `crew review` prints
"approved", and `merge_approved` sits waiting for an `APPROVED` review that can
never arrive. Every story stops in Awaiting Approval until a person clicks.

See [The reviewing app](#the-reviewing-app).

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

## Step 2 — a GitHub App (the crew's real identity)

A personal access token **always acts as the human who created it**. There is no
way to make one act as anyone else. So while the crew runs on a PAT, every issue
it files and every comment it writes is attributed to the Sponsor — the audit
trail claims the Sponsor wrote the epic proposals, which is exactly the
separation this design exists to create.

It also breaks the review gate. GitHub forbids approving your own pull request,
so if the Developer agent opens a PR as the Sponsor and the Reviewer agent
approves as the Sponsor, the approval is rejected and the PR can only merge by
admin bypass.

A **GitHub App** fixes both. It has its own `crew[bot]` identity, costs nothing
and consumes no seat, and mints short-lived installation tokens (one hour)
rather than holding a long-lived credential.

### Create it

**https://github.com/organizations/mqucifer/settings/apps/new**

| Setting | Value |
|---|---|
| Name | must be unique across GitHub — ours is `mqucifer-crew`, so the bot is `mqucifer-crew[bot]` |
| Homepage URL | the crew repo URL |
| Webhook | **uncheck Active** — the crew polls; it has no endpoint to receive hooks |
| Where can it be installed | Only on this account |

**Repository permissions:**

| Permission | Level |
|---|---|
| Contents | Read and write |
| Issues | Read and write |
| Pull requests | Read and write |
| Metadata | Read-only *(automatic)* |

**Organization permissions:**

| Permission | Level |
|---|---|
| Projects | Read and write |

Not Administration.

### Install and configure

1. Create the app, then **Install App** → the `mqucifer` organization → *Only
   select repositories* → `crew`, `sprint-metrics`.
2. On the app's settings page, **Generate a private key**. A `.pem` downloads.
3. Move it somewhere gitignored — `.secrets/crew-app.pem` in this repo is
   already covered by `.gitignore`.
4. Put the App ID (top of the app settings page) in `.env`:

```
GITHUB_APP_ID=123456
GITHUB_APP_PRIVATE_KEY=.secrets/<downloaded>.private-key.pem
GITHUB_APP_INSTALLATION_ID=        # discovered automatically if blank
```

`crew auth` and `crew tick` prefer the App whenever it is configured and fall
back to the PAT otherwise, so this can be switched on without changing
anything else. Both print the identity they are acting as — if it still says
`personal access token`, the App is not being picked up.

### What stays the Sponsor's

Git *commit* authorship comes from git config, not from the token, so agent
commits must set the author explicitly to be attributed to the bot:

```
mqucifer-crew[bot] <APP_ID+mqucifer-crew[bot]@users.noreply.github.com>
```

Pushes, issues, comments and reviews carry the bot identity automatically.

## The reviewing app

A second GitHub App, whose only job is to be somebody else.

Create it exactly as above, with a narrower grant:

| Permission | Level | Why |
|---|---|---|
| Pull requests | Write | Post the review, including an approving one |
| Organization projects | Read | Read the board to know what it is reviewing |

It needs no contents and no issues access: it reads diffs and writes verdicts.
Install it on the repositories the crew delivers into, put its private key in
`.secrets/`, and set:

```
GITHUB_REVIEW_APP_ID=
GITHUB_REVIEW_APP_PRIVATE_KEY=.secrets/<app>.private-key.pem
GITHUB_REVIEW_APP_INSTALLATION_ID=
```

`crew review` resolves this app; everything else resolves the delivery app.
Unset, it falls back to the delivery app and you are back to Problem 2's second
half — `crew auth` says so rather than leaving you to find out at merge time:

```
reviewing identity: mqucifer-crew-approver[bot] — can approve.
```

### What this means for the merge gate

With both apps configured, the crew approves and merges its own work without a
person in the loop. That is the constitution working as written — the Sponsor
approves epics and reviews at sprint end, not every swimlane — but it is worth
naming. What still stands between generated code and `main`: QA verifying each
acceptance criterion, the sandbox, the required `tests` check, and branch
protection. No agent has `administration`, so none of them can turn those off.

## Rotation

Fine-grained tokens expire. When one does, every agent action fails with 401 at
once — which looks like a total crew outage. `crew auth` distinguishes the two:
a rejected token reports `token rejected (401)` rather than a permission error.

### Verifying it

```
crew auth
  identity: mqucifer-crew[bot]
  token type     pass   GitHub App installation token (mqucifer-crew[bot]) — expires hourly
  identity       pass   acting as mqucifer-crew[bot], scoped to crew, sprint-metrics
  permissions    pass   contents:write, issues:write, metadata:read,
                        organization_projects:write, pull_requests:write
  not an admin   pass   no administration permission
```

An App is checked differently from a token. Its capability is its **grant set**,
stated once by the token-mint response, rather than something to infer
repository by repository — an installation token does not report per-repo push
rights the way a personal access token does, so probing each repo reports a
read-only App as broken.

Note the App ID is discoverable rather than something to hunt for:
`gh api orgs/<org>/installations` lists `app_id` and `installation_id`.
