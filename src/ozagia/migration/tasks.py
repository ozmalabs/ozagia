"""Turn unported or diverged symbols into a bounded checklist.

The checklist is data, not a task run: it creates no goal, starts no task and
executes nothing. Each item uses the existing evidence words (``observed`` for
an inventory fact, ``verified`` for re-read facts) so a caller can hand it to
the goal/task machinery explicitly.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from ozagia.migration.report import build_report

SCHEMA = "ozagia.migration.tasks"
VERSION = 1
DEFAULT_LIMIT = 50
MAX_LIMIT = 500
_TASK_STATUSES = ("planned", "in_progress", "diverged")


def _task_id(python: str | None, rust: str | None) -> str:
    value = f"{python or ''}\0{rust or ''}".encode("utf-8")
    return "migration:" + hashlib.sha256(value).hexdigest()[:16]


def _criteria(row: dict[str, Any]) -> list[dict[str, str]]:
    counterpart = row.get("rust") or "a Rust counterpart"
    criteria = [
        {
            "criterion": "counterpart_exists",
            "evidence": "observed",
            "check": f"{counterpart} exists in the Rust inventory with a "
                     "stable path",
        },
        {
            "criterion": "parity_flags_clear",
            "evidence": "verified",
            "check": "arity, async, visibility, doc and signature flags are "
                     "clear for the pair",
        },
        {
            "criterion": "mapped_tests_pass",
            "evidence": "verified",
            "check": "the tests mapped to both symbols pass (never inferred "
                     "from a green outer status)",
        },
    ]
    if row.get("status") == "diverged":
        criteria.insert(0, {
            "criterion": "divergence_explained",
            "evidence": "observed",
            "check": "each current flag is resolved or recorded as an "
                     "intentional difference",
        })
    return criteria


def _objective(row: dict[str, Any]) -> str:
    python = row.get("python") or "the Python symbol"
    rust = row.get("rust") or "a new Rust symbol"
    if row.get("status") == "diverged":
        return f"Resolve the {python} ↔ {rust} migration divergence"
    if row.get("status") == "in_progress":
        return f"Continue the {python} → {rust} port and clear parity flags"
    return f"Port {python} to {rust}"


def tasks_from_report(report: dict[str, Any], *, limit: int = DEFAULT_LIMIT,
                      ) -> dict[str, Any]:
    """Build the checklist from a status report (bounded and deterministic)."""
    if limit < 1 or limit > MAX_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_LIMIT}")
    configured = bool(report.get("configured"))
    rows = report.get("rows") or []
    eligible = [
        row for row in rows
        if row.get("python") and row.get("status") in _TASK_STATUSES
    ]
    eligible.sort(key=lambda row: (row.get("python") or "",
                                   row.get("rust") or ""))
    selected = eligible[:limit]
    tasks = [{
        "id": _task_id(row.get("python"), row.get("rust")),
        "python": row.get("python"),
        "rust": row.get("rust"),
        "status": row.get("status"),
        "priority": "diverged" if row.get("status") == "diverged" else "gap",
        "objective": _objective(row),
        "current_flags": list(row.get("flags") or []),
        "success_criteria": _criteria(row),
    } for row in selected]
    return {
        "schema": SCHEMA,
        "version": VERSION,
        "root": report.get("root"),
        "configured": configured,
        "reason": report.get("reason") if not configured else None,
        "counts": {
            "eligible": len(eligible),
            "returned": len(tasks),
            "truncated": len(eligible) > len(tasks),
        },
        "tasks": tasks,
        "created_goals": False,
        "executed": False,
        "notes": [
            "this is a hand-off checklist; no goal, task run or test was "
            "created or started",
            "counterpart_exists is an inventory fact; parity and tests still "
            "need verification",
        ],
    }


def build_tasks(root: str | Path, *, limit: int = DEFAULT_LIMIT,
                report: dict[str, Any] | None = None,
                write: bool = False) -> dict[str, Any]:
    """Build the checklist, reading the trees unless `report` is supplied."""
    if report is None:
        report = build_report(root, write=write)
    return tasks_from_report(report, limit=limit)


def markdown_tasks(payload: dict[str, Any]) -> str:
    """A bounded Markdown checklist view."""
    lines = ["# Python→Rust migration tasks", ""]
    if not payload.get("configured"):
        lines.append(f"Not configured: {payload.get('reason', 'unknown')}")
        return "\n".join(lines)
    counts = payload.get("counts") or {}
    lines.append(
        f"{counts.get('eligible', 0)} unported/diverged symbol(s); "
        f"{counts.get('returned', 0)} shown.")
    if counts.get("truncated"):
        lines.append("The list is truncated; raise `--limit` to see more.")
    lines.append("")
    for task in payload.get("tasks") or []:
        counterpart = task.get("rust") or "no Rust counterpart yet"
        lines.append(f"- [ ] **{task['python']}** → `{counterpart}` "
                     f"({task['status']})")
        lines.append(f"  - objective: {task['objective']}")
        for criterion in task.get("success_criteria") or []:
            lines.append(f"  - success: `{criterion['criterion']}` "
                         f"({criterion['evidence']}) — {criterion['check']}")
    if not payload.get("tasks"):
        lines.append("Nothing to do: no planned, in-progress or diverged "
                     "Python pair.")
    lines.extend(["", "created_goals: false; executed: false"])
    return "\n".join(lines)


__all__ = [
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "SCHEMA",
    "VERSION",
    "build_tasks",
    "markdown_tasks",
    "tasks_from_report",
]
