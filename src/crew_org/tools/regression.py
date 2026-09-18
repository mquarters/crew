"""Detecting when an implementation destroys work that already exists.

The Developer returns whole files, which is what makes its output easy to
validate and repair. The cost is that touching an existing file means rewriting
it, and a model asked to "add throughput" will happily redesign the module it
is adding to — discarding functions that other stories, and other tests, depend
on.

Observed on story #7: it rewrote story #6's merged module from scratch, renamed
its public functions, and implemented three future stories along the way. The
existing tests then failed to import, which reads as a coding error rather than
as the regression it is.

This is checked mechanically because it is mechanically checkable, and because
a prompt asking the model not to do it is a request rather than a guarantee.
"""

from __future__ import annotations

import ast
from pathlib import Path


def public_names(source: str) -> set[str]:
    """Top-level names a caller could import. Private names are the author's business."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()

    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            if not node.name.startswith("_"):
                names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and not target.id.startswith("_"):
                    names.add(target.id)
    return names


def removed_public_names(before: str, after: str) -> set[str]:
    """Public names the rewrite drops. An empty set means nothing was lost."""
    return public_names(before) - public_names(after)


def find_regressions(worktree: Path, files: list) -> dict[str, set[str]]:
    """Public names each proposed file would delete from what is already there.

    Only Python files are considered: removing a heading from a README is not a
    regression, and treating every deletion as one would make the check noise.
    """
    found: dict[str, set[str]] = {}
    for item in files:
        if not item.path.endswith(".py"):
            continue
        existing = worktree / item.path
        if not existing.exists():
            continue
        removed = removed_public_names(existing.read_text(encoding="utf-8"), item.content)
        if removed:
            found[item.path] = removed
    return found


def describe(regressions: dict[str, set[str]]) -> str:
    """What the Developer is told, written so the repair is obvious."""
    lines = [
        "This would delete public names that already exist. Other stories and "
        "their tests depend on them, so removing them is a regression rather "
        "than a refactor.",
        "",
    ]
    for path, names in sorted(regressions.items()):
        lines.append(f"`{path}` would lose: {', '.join(sorted(names))}")
    lines += [
        "",
        "Keep every existing public function and class, with the same name and "
        "signature, and add what this story needs alongside them. If something "
        "genuinely must change shape, that is a separate story.",
    ]
    return "\n".join(lines)
