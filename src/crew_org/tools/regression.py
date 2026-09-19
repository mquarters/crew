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
import textwrap
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


# --- what a replacement may not change -----------------------------------
#
# Editing by name made the change list visible, but nothing evaluated whether
# the listed scope was proportionate to the story. Six public definitions were
# replaced for a card that asked for one column, and every replacement changed
# a signature: a frozen dataclass gained fields, properties became methods, a
# return type went from `int` to `float | None`. The private helper that
# consumed those values was never looked at, so it went on summing integers
# over a list of None.
#
# Hence the line drawn here. A body is the author's business. A signature is a
# promise other code is already relying on, and a story that says "add a
# column" is not a story about renegotiating promises.

# Decorators that change how a definition is *called*, not merely what it does.
CALLING_DECORATORS = frozenset({"property", "staticmethod", "classmethod", "cached_property"})


def _decorator_name(node: ast.expr) -> str:
    """The bare name of a decorator, however it is spelled."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Call):
        return _decorator_name(node.func)
    return ""


def _parameters(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    """Parameter names, in the order a caller must supply them."""
    a = node.args
    names = [p.arg for p in (*a.posonlyargs, *a.args)]
    if a.vararg:
        names.append(f"*{a.vararg.arg}")
    names += [f"{p.arg}=" for p in a.kwonlyargs]
    if a.kwarg:
        names.append(f"**{a.kwarg.arg}")
    return names


def signature_of(node: ast.stmt) -> str | None:
    """What a caller depends on. None for definitions with no callable contract.

    Deliberately not the full source: renaming a local, rewriting a body or
    changing a docstring are the author's business and must stay allowed, or
    the check refuses the very edits stories are made of.
    """
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
        calling = sorted(
            {
                name
                for d in node.decorator_list
                if (name := _decorator_name(d)) in CALLING_DECORATORS
            }
        )
        kind = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
        return f"{' '.join(calling)} {kind}({', '.join(_parameters(node))})".strip()

    if isinstance(node, ast.ClassDef):
        # A dataclass's fields are its constructor, so an annotated attribute is
        # as much a part of the contract as a method is.
        members: list[str] = []
        for child in node.body:
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef) and (
                not child.name.startswith("_") or child.name == "__init__"
            ):
                members.append(f"{child.name} {signature_of(child)}")
            elif (
                isinstance(child, ast.AnnAssign)
                and isinstance(child.target, ast.Name)
                and not child.target.id.startswith("_")
            ):
                members.append(child.target.id)
        bases = [_decorator_name(b) for b in node.bases]
        decorators = sorted(_decorator_name(d) for d in node.decorator_list)
        return f"class({', '.join(bases)}) {' '.join(decorators)} {{{', '.join(sorted(members))}}}"

    return None


def _first_definition(source: str) -> ast.stmt | None:
    """The definition an edit carries, parsed on its own.

    An edit's source arrives at whatever indentation it had in the file, so it
    is dedented before parsing — a method replacement is otherwise a syntax
    error rather than a definition.
    """
    try:
        tree = ast.parse(textwrap.dedent(source))
    except SyntaxError:
        return None
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            return node
    return None


def broken_contracts(worktree: Path, edits: list) -> dict[str, tuple[str, str]]:
    """Edits that would change or remove a contract other code depends on.

    Keyed by `path::target`, valued by what the signature was and what it would
    become — the two strings are the whole explanation, so the message written
    from them needs no further reasoning.

    A replacement that leaves the signature alone is not reported, however much
    of the body it rewrites. That is the point: stories are made of body edits,
    and a check that refused those would refuse everything.
    """
    from crew_org.tools.ast_edit import definitions  # noqa: PLC0415

    found: dict[str, tuple[str, str]] = {}
    cache: dict[str, dict[str, ast.stmt]] = {}

    for edit in edits:
        operation = str(getattr(edit, "operation", ""))
        if operation not in {"replace", "delete"} or not edit.path.endswith(".py"):
            continue
        # A private helper is the author's business — nothing outside the module
        # can depend on it, and refusing to let a story reshape its own internals
        # is the kind of constraint that blocks correct work. `__init__` is the
        # exception: it is private in name only, and it is the constructor.
        leaf = edit.target.rpartition(".")[2]
        if leaf.startswith("_") and leaf != "__init__":
            continue

        existing = worktree / edit.path
        if not existing.exists():
            continue
        if edit.path not in cache:
            cache[edit.path] = definitions(existing.read_text(encoding="utf-8"))

        old_node = cache[edit.path].get(edit.target)
        if old_node is None:
            # Naming something that is not there is a different failure, and
            # the edit machinery already reports it as one.
            continue
        was = signature_of(old_node)
        if was is None:
            continue

        key = f"{edit.path}::{edit.target}"
        if operation == "delete":
            found[key] = (was, "removed")
            continue

        new_node = _first_definition(edit.source)
        now = signature_of(new_node) if new_node is not None else None
        if now is not None and now != was:
            found[key] = (was, now)

    return found


def describe_contracts(broken: dict[str, tuple[str, str]]) -> str:
    """What the Developer is told. Written so the repair is obvious."""
    lines = [
        "This changes what existing code promises its callers. Other stories "
        "have already been merged against these signatures and their tests "
        "still call them the old way.",
        "",
    ]
    for key, (was, now) in sorted(broken.items()):
        path, target = key.split("::", 1)
        lines.append(f"`{target}` in `{path}`")
        lines.append(f"    was: {was}")
        lines.append(f"    now: {now}")
    lines += [
        "",
        "Keep each of these signatures exactly as it is. You may rewrite a "
        "body freely — that is invisible to callers — but a parameter list, a "
        "property that becomes a method, a dataclass field, or a removed "
        "definition is not.",
        "",
        "If this story needs behaviour the current signature cannot express, "
        "add a new definition alongside the existing one and leave the old one "
        "working. Do not renegotiate an interface to make one story easier; "
        "everything already built on it has to keep passing.",
    ]
    return "\n".join(lines)


def render_signature(node: ast.stmt) -> str | None:
    """A definition's contract, written the way a caller reads it.

    Compact on purpose: this goes in the Developer's prompt, where it has to
    earn its tokens and stay identical between attempts so the prefix stays
    cacheable. `signature_of` is the exact form used for comparison; this is
    the same information written to be read.
    """
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
        calling = [
            name
            for d in node.decorator_list
            if (name := _decorator_name(d)) in CALLING_DECORATORS
        ]
        rendered = f"({', '.join(_parameters(node))})"
        return f"{rendered} [{calling[0]}]" if calling else rendered
    if isinstance(node, ast.ClassDef):
        return ""
    return None


def signatures_for_context(source: str) -> dict[str, str]:
    """Every public definition, mapped to how it must be called.

    Shown to the Developer before it writes, rather than only quoted back at it
    after it has broken something. A constraint the model is never told is not
    a constraint it can respect, and discovering one by being refused costs a
    repair attempt that was reserved for real failures.
    """
    from crew_org.tools.ast_edit import definitions  # noqa: PLC0415

    out: dict[str, str] = {}
    for name, node in definitions(source).items():
        if name.rpartition(".")[2].startswith("_"):
            continue
        rendered = render_signature(node)
        if rendered is not None:
            out[name] = rendered
    return out
