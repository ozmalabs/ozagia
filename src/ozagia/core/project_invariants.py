"""Read project-declared behavioral invariants for retrieval context.

Declarations live in ``[invariants.<id>]`` tables in either project config
file. Each declaration points to source and check paths; prose alone is never
presented as evidence that the behavior is implemented or tested.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ozagia.config import load_config_section

MAX_INVARIANTS = 64
MAX_PATHS = 12
MAX_TEXT = 500
_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")
_WORD = re.compile(r"[a-z0-9]+")
_STOPWORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "been", "being",
    "but", "by", "check", "checks", "ensure", "for", "from", "has",
    "have", "if", "in", "into", "is", "it", "its", "keep", "make",
    "must", "of", "on", "or", "preserve", "remain", "should", "that",
    "the", "their", "them", "then", "there", "these", "this", "those",
    "to", "use", "used", "using", "valid", "validate", "works",
})


def meaningful_terms(text: str) -> set[str]:
    """Words suitable for matching a short task against a declaration."""
    return set(_WORD.findall((text or "").lower())) - _STOPWORDS


def _path_list(value: Any, *, field: str, where: str) -> list[str]:
    if not isinstance(value, list) or len(value) > MAX_PATHS:
        raise ValueError(f"{where}: {field} must be a list of at most {MAX_PATHS} paths")
    result: list[str] = []
    for path in value:
        if not isinstance(path, str) or not path.strip() or len(path) > 512:
            raise ValueError(f"{where}: {field} entries must be non-empty paths")
        normalized = path.strip().replace("\\", "/")
        parts = normalized.split("/")
        if normalized.startswith("/") or any(part in ("", ".", "..") for part in parts):
            raise ValueError(f"{where}: {field} paths must be repository-relative")
        result.append(normalized)
    return list(dict.fromkeys(result))


def _inside_file(root: Path, relative: str) -> bool:
    path = root / relative
    try:
        path.resolve().relative_to(root)
    except (OSError, ValueError):
        return False
    return path.is_file()


def declarations(root: str | Path, task: str | None = None, *,
                 limit: int = 8) -> dict[str, Any]:
    """Return bounded, validated invariants, optionally ranked for *task*.

    Source/check paths remain declarations. ``exists`` fields report only a
    direct filesystem observation and do not imply that a check passed.
    Invalid rows are returned as errors instead of silently disappearing.
    """
    root_path = Path(root).expanduser().resolve()
    section = load_config_section(root_path, "invariants")
    errors = [f"{item.path}: {item.message}" for item in section.errors]
    rows: list[dict[str, Any]] = []
    for ident, raw in sorted(section.values.items()):
        where = f"[invariants.{ident}]"
        try:
            if not _ID.fullmatch(ident):
                raise ValueError(f"{where}: invalid invariant id")
            if not isinstance(raw, dict):
                raise ValueError(f"{where}: invariant must be a table")
            unknown = sorted(set(raw) - {"statement", "sources", "checks"})
            if unknown:
                raise ValueError(f"{where}: unknown key(s): {', '.join(unknown)}")
            statement = raw.get("statement")
            if (not isinstance(statement, str) or not statement.strip()
                    or len(statement.strip()) > MAX_TEXT
                    or any(ord(ch) < 32 for ch in statement.strip())):
                raise ValueError(f"{where}: statement must be 1..{MAX_TEXT} printable characters")
            sources = _path_list(raw.get("sources"), field="sources", where=where)
            checks = _path_list(raw.get("checks", []), field="checks", where=where)
            if not sources:
                raise ValueError(f"{where}: at least one source path is required")
            row = {
                "id": ident,
                "statement": statement.strip(),
                "sources": sources,
                "checks": checks,
                "source_exists": [_inside_file(root_path, path) for path in sources],
                "check_exists": [_inside_file(root_path, path) for path in checks],
                "evidence": "declared",
            }
            rows.append(row)
        except ValueError as exc:
            errors.append(str(exc))
    if len(rows) > MAX_INVARIANTS:
        errors.append(f"[invariants]: limit is {MAX_INVARIANTS} declarations")
        rows = rows[:MAX_INVARIANTS]
    if task:
        query = meaningful_terms(task)
        if not query:
            return {"ok": not errors, "invariants": [], "errors": errors,
                    "evidence": "declared", "task": task}

        def score(row: dict[str, Any]) -> tuple[int, str]:
            # Rank on the claim itself; shared source/test paths should not
            # make unrelated declarations look relevant to every task.
            text = (row["id"] + " " + row["statement"]).lower()
            terms = meaningful_terms(text)
            return (-len(query & terms), row["id"])

        rows.sort(key=score)
        rows = [row for row in rows if score(row)[0] < 0][:max(1, min(limit, 32))]
    return {"ok": not errors, "invariants": rows, "errors": errors,
            "evidence": "declared", "task": task}
