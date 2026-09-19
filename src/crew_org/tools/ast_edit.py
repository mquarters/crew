"""Editing code by name rather than by position.

The Developer used to return whole files, which made every story a
transcription exercise: regenerate three hundred lines, change four, and leave
the rest byte-identical. It could not do that, and the failures were not
random — it redesigned what it was asked to extend, deleted tests, and drifted
in code nobody had asked it to touch.

Targeting by name removes the transcription entirely. The model names a
definition and supplies its new source; nothing else is reproduced, so nothing
else can be damaged. Deleting becomes an operation it has to choose rather than
an accident of regeneration.

Two findings shaped this. Aider measured a 30-50% rise in editing errors when
models were pushed toward surgical line edits instead of whole functions, and a
9x rise without permissive parsing — so edits are whole definitions and the
applier forgives imperfect input. A published comparison of edit formats found
name-addressed AST edits had zero format failures across four models, where
search/replace had eleven and unified diff thirty-one, because both of those
require reproducing existing text exactly.

Python only. The principle generalises, the parser does not.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from enum import StrEnum


class EditError(ValueError):
    """An edit that cannot be applied, described so the model can fix it."""


class Operation(StrEnum):
    REPLACE = "replace"
    """Replace an existing top-level definition or method, by name."""

    ADD = "add"
    """Append a new top-level definition."""

    ADD_METHOD = "add_method"
    """Add a method to an existing class."""

    ADD_IMPORT = "add_import"
    """Add an import. Separate because a spliced function often needs one."""

    DELETE = "delete"
    """Remove a definition. Deliberate, never incidental."""


@dataclass(frozen=True)
class Edit:
    operation: Operation
    target: str
    source: str = ""


def _definitions(tree: ast.Module) -> dict[str, ast.stmt]:
    """Every addressable definition, by qualified dotted name."""
    found: dict[str, ast.stmt] = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            found[node.name] = node
            if isinstance(node, ast.ClassDef):
                for child in node.body:
                    if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                        found[f"{node.name}.{child.name}"] = child
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    found[target.id] = node
    return found


def qualified_names(source: str) -> set[str]:
    try:
        return set(_definitions(ast.parse(source)))
    except SyntaxError:
        return set()


def _span(node: ast.stmt) -> tuple[int, int]:
    """The line range a definition occupies, decorators included."""
    decorators = getattr(node, "decorator_list", None)
    start = (decorators[0].lineno if decorators else node.lineno) - 1
    return start, (node.end_lineno or node.lineno)


def _indent_of(line: str) -> str:
    return line[: len(line) - len(line.lstrip())]


def _reindent(source: str, indent: str) -> str:
    """Put a definition at the indentation its destination needs.

    A model asked for a method will usually emit it unindented, or indented by
    a guess. Correcting that here is cheaper than rejecting the edit.
    """
    lines = source.strip("\n").splitlines()
    if not lines:
        return ""
    base = _indent_of(lines[0])
    out = []
    for line in lines:
        stripped = line[len(base) :] if line.startswith(base) else line.lstrip()
        out.append(indent + stripped if stripped.strip() else "")
    return "\n".join(out)


def _import_insertion_point(tree: ast.Module, lines: list[str]) -> int:
    """After the last import, or after the module docstring, or the top."""
    last = 0
    for node in tree.body:
        if isinstance(node, ast.Import | ast.ImportFrom):
            last = node.end_lineno or node.lineno
        elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and last == 0:
            last = node.end_lineno or node.lineno
    return last


def apply_edit(source: str, edit: Edit) -> str:
    """Apply one edit. The file is re-parsed per edit, so spans never go stale."""
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise EditError(f"the file does not parse: {exc}") from exc

    lines = source.splitlines()
    definitions = _definitions(tree)

    if edit.operation is Operation.ADD_IMPORT:
        statement = edit.source.strip()
        if not statement:
            raise EditError("add_import needs the import statement as its source")
        if any(line.strip() == statement for line in lines):
            return source  # already there; adding it twice is not an error
        at = _import_insertion_point(tree, lines)
        return "\n".join(lines[:at] + [statement] + lines[at:]) + "\n"

    if edit.operation is Operation.ADD:
        if edit.target in definitions:
            raise EditError(f"{edit.target!r} already exists — use replace, or choose another name")
        body = edit.source.strip("\n")
        return source.rstrip("\n") + "\n\n\n" + body + "\n"

    if edit.operation is Operation.ADD_METHOD:
        class_name = edit.target.split(".")[0]
        node = definitions.get(class_name)
        if not isinstance(node, ast.ClassDef):
            raise EditError(f"no class named {class_name!r} to add a method to")
        _start, end = _span(node)
        indent = _indent_of(lines[node.body[0].lineno - 1]) if node.body else "    "
        body = _reindent(edit.source, indent)
        return "\n".join(lines[:end] + [""] + body.splitlines() + lines[end:]) + "\n"

    node = definitions.get(edit.target)
    if node is None:
        known = ", ".join(sorted(definitions)[:12]) or "nothing"
        raise EditError(f"no definition named {edit.target!r}. This file defines: {known}")

    start, end = _span(node)

    if edit.operation is Operation.DELETE:
        return "\n".join(lines[:start] + lines[end:]) + "\n"

    indent = _indent_of(lines[start])
    body = _reindent(edit.source, indent)
    if not body.strip():
        raise EditError(f"replacing {edit.target!r} with nothing — use delete if that is meant")
    return "\n".join(lines[:start] + body.splitlines() + lines[end:]) + "\n"


def apply_edits(source: str, edits: list[Edit]) -> str:
    """Apply edits in order, re-parsing between each."""
    for edit in edits:
        source = apply_edit(source, edit)
    return source
