"""What a repository looks like, for any agent that needs to know.

Delivery was the first to need it and for a while the only one, so it lived in
the delivery flow. Refinement needs it too: a Business Analyst that cannot see
the code writes acceptance criteria against a product it is imagining. crew#6
was split into three stories addressed to "the audit trail entry", with no way
to know that the board already had an Owner Agent field sitting empty.
"""

from __future__ import annotations

from pathlib import Path

# Files worth showing an agent so its work fits in with what is there.
CONTEXT_FILES = ("pyproject.toml", "README.md")

# The whole repository, on every attempt. The window is 262,144 tokens and the
# pilot repo is 17,195 characters — under 2% of it. Showing a developer the
# names of three functions and asking it to honour behaviour it has never read
# is not a context budget, it is a blindfold, and every rule since #8 has been
# an attempt to describe in prose what one `cat` would have shown.
#
# The ceiling is a guard against a tree that genuinely does not fit, not a
# budget. It drops whole files and names them: a file cut mid-function is worse
# than a file left out, because nothing in it marks where it stopped.
#
# 200,000 was set when the only repository anyone read was the 22,000-character
# pilot. The crew's own repository is 244,159 characters, so the first time
# refinement read it the guard fired in ordinary work and dropped
# `github_project.py` — the file defining the `Owner Agent` field the epic
# being refined was about. A guard that fires in normal work is a budget.
#
# The arithmetic: 600,000 characters is roughly 150,000 tokens, leaving about
# 100,000 of the 262,144-token window for the story, the standing instructions,
# a failure report of up to MAX_FAILURE_REPORT_CHARS, and the generation.
CONTEXT_CHAR_CEILING = 600_000
# Tool droppings. They tell the Developer nothing and they are not free: the
# file listing is capped, so eleven cache entries are eleven real files the
# model never gets shown.
IGNORED_DIRS = frozenset({".git", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache"})


def repository_context(worktree: Path, *, editing: bool = True) -> str:
    """What the repository looks like, and what each file defines.

    Editing works by name, so the names are the context that matters. Listing
    them is a few hundred tokens that stay identical between attempts, where
    dumping file bodies was thousands that changed every time — which both
    poisoned the prefix cache and left the model guessing at targets it had
    never been shown. Both EDIT failures on story #8 were invented names.

    The names carry their signatures, because a name alone does not say that
    `is_completed` is a property or that `format_performance_table` takes
    `(cards, wip_limits)`. Story #9 changed both and broke thirteen tests, and
    it had never been shown either contract — it was refused for violating a
    rule nobody had told it. Signatures cost a few tokens more and are just as
    stable between attempts, so the cacheable prefix is unaffected.

    The bodies follow, in full, on every attempt. They used to appear only on a
    repair, on a prefix-cache argument: bodies churn between attempts where
    names do not. That optimised the wrong thing. Story #11 rewrote `main` —
    dropped its return type, its docstring and its stdin default — and was
    refused for breaking a contract that lives in a body it had never been
    shown. The signature line it did get, `main(argv: Sequence[str] | None =
    None) -> int`, carries none of that. Cache hits are cheaper than a story
    that never lands.

    `editing=False` leaves out the rules about how to address a definition and
    what may not be reshaped. Refinement reads this to decide what work should
    exist; it is not editing anything, and instructions for a job it is not
    doing are noise competing with the ones it must follow.
    """
    from crew_org.tools.regression import signatures_for_context  # noqa: PLC0415

    paths = sorted(
        p for p in worktree.rglob("*") if p.is_file() and not (IGNORED_DIRS & set(p.parts))
    )

    lines = ["### Files and what they define", ""]
    for path in paths:
        rel = path.relative_to(worktree)
        if path.suffix == ".py":
            signatures = signatures_for_context(path.read_text(encoding="utf-8", errors="ignore"))
            defined = (
                ", ".join(f"{name}{sig}" for name, sig in sorted(signatures.items()))
                if signatures
                else "nothing at top level"
            )
            lines.append(f"- `{rel}` — {defined}")
        else:
            lines.append(f"- `{rel}`")

    if editing:
        lines += [
            "",
            "Target an existing definition by the names above. `Class.method` for a "
            "method. A file is not a definition: to change what a package exports, "
            "edit `__all__`, not `__init__`.",
            "",
            "The signatures above are what merged code already calls. Changing a "
            "public one is refused — not as a matter of taste, but because callers "
            "you are not editing would break. Everything else is yours: bodies, "
            "private helpers, module internals, and new definitions of whatever "
            "shape the story needs. Design it the way it should be designed.",
        ]

    for name in CONTEXT_FILES:
        target = worktree / name
        if target.exists():
            lines += ["", f"### {name}", "", "```", target.read_text().strip(), "```"]

    lines += ["", "### Current source", ""]
    budget = CONTEXT_CHAR_CEILING
    omitted: list[str] = []
    for target in sorted(worktree.glob("src/**/*.py")) + sorted(worktree.glob("tests/**/*.py")):
        if IGNORED_DIRS & set(target.parts):
            continue
        rel = target.relative_to(worktree)
        body = target.read_text(encoding="utf-8", errors="ignore")
        if len(body) > budget:
            omitted.append(str(rel))
            continue
        budget -= len(body)
        lines += [f"`{rel}`", "", "```python", body.strip(), "```", ""]
    if omitted:
        lines += [
            "These files exist and are not shown, because the tree did not fit: "
            + ", ".join(f"`{name}`" for name in omitted)
            + ". Treat anything they define as code you cannot see.",
            "",
        ]

    return "\n".join(lines)
