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


def definition_sources(source: str) -> dict[str, str]:
    """Top-level definitions, mapped to their exact source text.

    Names alone are too weak a guarantee: a preserved name can carry a changed
    signature, a rewritten body, or behaviour nobody asked for. Comparing the
    source catches that.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {}

    lines = source.splitlines()
    out: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            name = node.name
        elif (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            name = node.targets[0].id
        else:
            continue
        if name.startswith("_"):
            continue
        # Assignments have no decorators; only definitions do.
        decorators = getattr(node, "decorator_list", None)
        start = (decorators[0].lineno - 1) if decorators else (node.lineno - 1)
        end = node.end_lineno or node.lineno
        out[name] = "\n".join(lines[start:end]).rstrip()
    return out


def undeclared_changes(before: str, after: str, declared: set[str]) -> set[str]:
    """Definitions that changed without being declared as intended changes.

    A rewrite that keeps every name can still replace every body. Requiring the
    Developer to say what it means to touch makes "I only added throughput" a
    checkable claim rather than a hope.
    """
    old, new = definition_sources(before), definition_sources(after)
    return {
        name
        for name, source in old.items()
        if name in new and new[name] != source and name not in declared
    }


def removed_public_names(before: str, after: str) -> set[str]:
    """Public names the rewrite drops. An empty set means nothing was lost."""
    return public_names(before) - public_names(after)


def overwrites_existing(worktree: Path, new_files: list) -> list[str]:
    """New files that would overwrite something already there.

    Writing an existing path as a "new file" is a whole-file rewrite by another
    name — the exact thing editing by name exists to prevent — so it is refused
    rather than merged.
    """
    return [f.path for f in new_files if (worktree / f.path).exists()]


def find_regressions(
    worktree: Path, files: list, declared: set[str] | None = None
) -> dict[str, dict[str, set[str]]]:
    """What each proposed file would destroy or silently alter.

    Returns per path: names it would delete, and names it would change without
    having declared the intention. Only Python files are considered — removing a
    heading from a README is not a regression, and treating every deletion as
    one would make the check noise.
    """
    declared = declared or set()
    found: dict[str, dict[str, set[str]]] = {}
    for item in files:
        if not item.path.endswith(".py"):
            continue
        existing = worktree / item.path
        if not existing.exists():
            continue
        before = existing.read_text(encoding="utf-8")
        removed = removed_public_names(before, item.content)
        altered = undeclared_changes(before, item.content, declared)
        if removed or altered:
            found[item.path] = {"removed": removed, "altered": altered}
    return found


def describe(regressions: dict[str, dict[str, set[str]]]) -> str:
    """What the Developer is told, written so the repair is obvious."""
    lines = [
        "This would change code you did not say you were changing. Other stories "
        "and their tests depend on it.",
        "",
    ]
    for path, kinds in sorted(regressions.items()):
        if kinds.get("removed"):
            lines.append(f"`{path}` would delete: {', '.join(sorted(kinds['removed']))}")
        if kinds.get("altered"):
            lines.append(
                f"`{path}` would rewrite, undeclared: {', '.join(sorted(kinds['altered']))}"
            )
    lines += [
        "",
        "Keep every existing definition exactly as it is — same name, same "
        "signature, same body — and add what this story needs alongside it.",
        "If a change to existing code is genuinely required, name it in "
        "`modifies` so the change is deliberate and reviewable. Anything you "
        "rewrite without declaring is treated as an accident, because that is "
        "usually what it is.",
    ]
    return "\n".join(lines)
