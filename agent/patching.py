"""Applying LLM patches safely (PLAN §B4 guardrails).

A patch is a list of {file, search, replace} edits. Before anything is written:
- every file must be on the allow-list (app/, requirements*.txt, Dockerfile, config files)
- tests/, .github/ (and the agent itself) can never be edited
- the patch must stay small (size cap)
- every `search` must match exactly once
"""

from __future__ import annotations

import difflib
import re
from pathlib import Path, PurePosixPath

from agent.llm import Edit

ALLOWED_ROOT_FILES = re.compile(r"Dockerfile|\.dockerignore|requirements[\w-]*\.txt|render\.ya?ml|\.env\.example")
FORBIDDEN_PREFIXES = ("tests/", ".github/", "agent/", "scenarios/")
MAX_EDITS = 10
MAX_CHANGED_LINES = 120  # hard cap; anything above 30 lines is already CORE (classify.patch_kind)


class PatchRejected(ValueError):
    """The patch breaks a guardrail (forbidden file, too big)."""


class PatchError(ValueError):
    """The patch can't be applied (search text not found / ambiguous)."""


def path_problem(path: str) -> str | None:
    """Why this path may not be edited, or None if it's allowed."""
    p = PurePosixPath(path.replace("\\", "/"))
    if p.is_absolute() or ".." in p.parts or not p.parts:
        return f"{path}: path must be relative and inside the repo"
    rel = p.as_posix()
    if rel.startswith(FORBIDDEN_PREFIXES) or "/tests/" in f"/{rel}" or p.name.startswith(("test_", "conftest")):
        return f"{rel}: editing tests, workflows or the agent is not allowed"
    if rel.startswith("app/") or ALLOWED_ROOT_FILES.fullmatch(rel):
        return None
    return f"{rel}: not on the allow-list (app/, requirements*.txt, Dockerfile, config files)"


def check_guardrails(edits: list[Edit]) -> list[str]:
    problems = [p for e in edits if (p := path_problem(e.file))]
    if not edits:
        problems.append("the patch is empty")
    if len(edits) > MAX_EDITS:
        problems.append(f"too many edits ({len(edits)} > {MAX_EDITS})")
    return problems


def changed_lines(old: str, new: str) -> int:
    return sum(1 for line in difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm="", n=0)
               if line[:1] in "+-" and not line.startswith(("+++", "---")))


def compute_changes(root: str | Path, edits: list[Edit]) -> dict[str, tuple[str, str]]:
    """Apply the edits in memory: {path: (old content, new content)}. Raises PatchRejected/PatchError."""
    problems = check_guardrails(edits)
    if problems:
        raise PatchRejected("; ".join(problems))
    root = Path(root)
    changes: dict[str, tuple[str, str]] = {}
    for edit in edits:
        path = PurePosixPath(edit.file.replace("\\", "/")).as_posix()
        file = root / path
        if path in changes:
            old, current = changes[path]
        else:
            old = file.read_text(encoding="utf-8").replace("\r\n", "\n") if file.is_file() else None
            current = old
        search = edit.search.replace("\r\n", "\n")
        if not search:
            if current is not None:
                raise PatchError(f"{path}: empty search is only allowed for new files")
            current = edit.replace
        elif current is None:
            raise PatchError(f"{path}: file does not exist")
        else:
            count = current.count(search)
            if count == 0:
                raise PatchError(f"{path}: search text not found: {search[:80]!r}")
            if count > 1:
                raise PatchError(f"{path}: search text is ambiguous ({count} matches): {search[:80]!r}")
            current = current.replace(search, edit.replace, 1)
        changes[path] = (old if old is not None else "", current)

    total = sum(changed_lines(old, new) for old, new in changes.values())
    if total > MAX_CHANGED_LINES:
        raise PatchRejected(f"patch too big ({total} changed lines > {MAX_CHANGED_LINES})")
    return changes


def write_changes(root: str | Path, changes: dict[str, tuple[str, str]]) -> None:
    for path, (_, new) in changes.items():
        target = Path(root) / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(new, encoding="utf-8", newline="\n")


def apply_edits(root: str | Path, edits: list[Edit]) -> dict[str, tuple[str, str]]:
    """Check + apply to disk. Nothing is written unless every edit is valid."""
    changes = compute_changes(root, edits)
    write_changes(root, changes)
    return changes


def unified_diff(changes: dict[str, tuple[str, str]]) -> str:
    out = []
    for path, (old, new) in changes.items():
        out += difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True),
                                    fromfile=f"a/{path}", tofile=f"b/{path}")
    return "".join(out)
