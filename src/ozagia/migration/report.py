"""Build and render the migration status report.

The report is deterministic: no timestamps, sorted rows and bounded lists. It
compares its own input fingerprints with the last report under the state
directory (`.ozagia/migration/status.json` by default) to say whether the
status is fresh relative to both trees and the effective configuration.

Reports are the only files this module writes, and only under the state
directory. ``status`` and ``tasks`` never write a source tree.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from ozagia.config import (
    ConfigError,
    MIGRATION_STATUSES,
    MigrationConfig,
    load_project_config,
    state_path,
)
from ozagia.migration.inventory import (
    TreeInventory,
    inventory_python,
    inventory_rust,
)
from ozagia.migration.match import MatchResult, MigrationRow, match

SCHEMA = "ozagia.migration.status"
VERSION = 1
MAX_ROWS = 500
MAX_GAPS = 10
MAX_DIVERGENCES = 20
MAX_CANDIDATES = 50
MAX_OMISSIONS = 100
REPORT_DIRNAME = "migration"
STATUS_JSON = "status.json"
STATUS_MARKDOWN = "status.md"


def _digest(value: Any) -> str:
    text = json.dumps(value, sort_keys=True, separators=(",", ":"),
                      default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _previous_report(root: Path) -> tuple[dict[str, Any] | None, Path]:
    path = state_path(root, REPORT_DIRNAME, STATUS_JSON)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None, path
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        return None, path
    return data, path


def _freshness(root: Path, config_digest: str,
               python: TreeInventory, rust: TreeInventory,
               ) -> dict[str, Any]:
    previous, path = _previous_report(root)
    if previous is None:
        return {
            "state": "unknown",
            "python": "unknown",
            "rust": "unknown",
            "config": "unknown",
            "previous": str(path),
            "reason": "no previous migration report was recorded",
        }
    inputs = previous.get("inputs") or {}
    old_python = str(inputs.get("python") or "")
    old_rust = str(inputs.get("rust") or "")
    old_config = str(inputs.get("config") or "")
    py_state = "unknown" if not old_python else (
        "fresh" if old_python == python.fingerprint else "changed")
    rs_state = "unknown" if not old_rust else (
        "fresh" if old_rust == rust.fingerprint else "changed")
    cfg_state = "unknown" if not old_config else (
        "fresh" if old_config == config_digest else "changed")
    states = (py_state, rs_state, cfg_state)
    if any(state == "changed" for state in states):
        overall = "stale"
    elif all(state == "fresh" for state in states):
        overall = "fresh"
    else:
        overall = "unknown"
    return {
        "state": overall,
        "python": py_state,
        "rust": rs_state,
        "config": cfg_state,
        "previous": str(path),
    }


def _row_sort(row: MigrationRow) -> tuple[str, str]:
    return (row.python or row.rust or "", row.rust or "")


def _gaps(result: MatchResult) -> tuple[list[dict[str, Any]], int]:
    rows = [row for row in result.rows if row.status == "planned"]
    rows.sort(key=_row_sort)
    return [row.to_dict() for row in rows[:MAX_GAPS]], len(rows)


def _divergences(result: MatchResult) -> tuple[list[dict[str, Any]], int]:
    rows = [row for row in result.rows
            if "missing_counterpart" not in row.flags and row.flags]
    rows.sort(key=lambda row: (row.status != "diverged", _row_sort(row)))
    return [row.to_dict() for row in rows[:MAX_DIVERGENCES]], len(rows)


def _omissions(python: TreeInventory, rust: TreeInventory,
               result: MatchResult) -> tuple[list[dict[str, Any]], int]:
    items: list[dict[str, Any]] = []
    for tree, inventory in (("python", python), ("rust", rust)):
        for item in inventory.omitted:
            items.append({"tree": tree, **item})
    for item in result.unresolved_pairs:
        items.append({
            "tree": "config",
            "kind": "pair",
            "path": f"{item['python']} -> {item['rust']}",
            "reason": "; ".join(
                reason for reason in (item.get("python_reason"),
                                      item.get("rust_reason")) if reason),
        })
    items.sort(key=lambda item: (item["tree"], item["path"], item["reason"]))
    return items[:MAX_OMISSIONS], len(items)


def _counts(python: TreeInventory, rust: TreeInventory,
            result: MatchResult) -> dict[str, Any]:
    counts = result.counts()
    return {
        "python": {
            "files": python.files,
            "symbols": len(python.symbols),
            "api": len(python.api),
            "bytes": python.bytes_read,
        },
        "rust": {
            "files": rust.files,
            "symbols": len(rust.symbols),
            "api": len(rust.api),
            "bytes": rust.bytes_read,
        },
        **counts,
        "statuses": result.status_counts(),
    }


def _unconfigured(root: Path, config: MigrationConfig) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "version": VERSION,
        "root": str(root),
        "configured": False,
        "reason": ("no [migration] section with python and rust trees is "
                   "configured"),
        "config": config.to_dict(),
        "counts": {
            "python": {"files": 0, "symbols": 0, "api": 0, "bytes": 0},
            "rust": {"files": 0, "symbols": 0, "api": 0, "bytes": 0},
            "rows": 0, "matched": 0, "python_only": 0, "rust_only": 0,
            "candidates": 0,
            "statuses": {status: 0 for status in MIGRATION_STATUSES},
        },
        "status_counts": {status: 0 for status in MIGRATION_STATUSES},
        "rows": [],
        "rows_truncated": False,
        "gaps": [],
        "gaps_count": 0,
        "divergences": [],
        "divergences_count": 0,
        "candidates": [],
        "candidates_count": 0,
        "candidates_truncated": False,
        "unresolved_pairs": [],
        "omissions": [],
        "omissions_count": 0,
        "omissions_truncated": False,
        "freshness": {"state": "unknown", "reason": "not configured"},
        "limits": {},
        "notes": [
            "add [migration] with python and rust trees to .ozagia.toml",
            "nothing was scanned, written or executed",
        ],
        "written": {"json": None, "markdown": None},
        "executed": False,
        "source_modified": False,
    }


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.",
                                suffix=".tmp")
    tmp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def build_report(root: str | Path, *, write: bool = True,
                 config: MigrationConfig | None = None,
                 python: TreeInventory | None = None,
                 rust: TreeInventory | None = None) -> dict[str, Any]:
    """Inventory, match and return the status report.

    ``write=False`` never touches the state directory. Injected inventories
    and config are for callers that already loaded them (tests and MCP); a
    normal call reads ``.ozagia.toml`` and both configured trees.
    """
    root_p = Path(root).resolve()
    sources: list[str] = []
    if config is None:
        try:
            loaded = load_project_config(root_p)
            config = loaded.migration
            sources = [str(path) for path in loaded.sources]
        except (ConfigError, OSError) as exc:
            return {
                **_unconfigured(root_p, MigrationConfig()),
                "reason": f"configuration could not be read: {exc}",
            }
    if not config.configured:
        return _unconfigured(root_p, config)
    if python is None:
        python = inventory_python(root_p, config)
    if rust is None:
        rust = inventory_rust(root_p, config)
    result = match(python, rust, config)
    config_payload = config.to_dict()
    config_digest = _digest(config_payload)
    freshness = _freshness(root_p, config_digest, python, rust)
    rows = [row.to_dict() for row in result.rows[:MAX_ROWS]]
    candidates = [item.to_dict()
                  for item in result.candidates[:MAX_CANDIDATES]]
    gaps, gaps_count = _gaps(result)
    divergences, divergences_count = _divergences(result)
    omissions, omissions_count = _omissions(python, rust, result)
    report: dict[str, Any] = {
        "schema": SCHEMA,
        "version": VERSION,
        "root": str(root_p),
        "configured": True,
        "config": {
            **config_payload,
            "sources": sources,
        },
        "inputs": {
            "python": python.fingerprint,
            "rust": rust.fingerprint,
            "config": config_digest,
            "inventory_version": 1,
        },
        "trees": {"python": python.to_dict(), "rust": rust.to_dict()},
        "counts": _counts(python, rust, result),
        "status_counts": result.status_counts(),
        "rows": rows,
        "rows_truncated": len(result.rows) > MAX_ROWS,
        "gaps": gaps,
        "gaps_count": gaps_count,
        "divergences": divergences,
        "divergences_count": divergences_count,
        "candidates": candidates,
        "candidates_count": len(result.candidates),
        "candidates_truncated": (len(result.candidates) > MAX_CANDIDATES
                                 or result.truncated),
        "unresolved_pairs": list(result.unresolved_pairs),
        "omissions": omissions,
        "omissions_count": omissions_count,
        "omissions_truncated": omissions_count > MAX_OMISSIONS,
        "freshness": freshness,
        "limits": python.limits,
        "notes": [
            "parity flags are facts about names, fields and presence; "
            "cross-language type equality is not checked",
            "inferred candidates are never accepted and never change a status",
            "status and tasks read both trees; they never execute the project",
            "the source trees are not modified by this report",
            *(["inference candidates were bounded; not every name/module "
               "pair was scored"] if result.truncated else []),
        ],
        "written": {"json": None, "markdown": None},
        "executed": False,
        "source_modified": False,
    }
    if write:
        json_path = state_path(root_p, REPORT_DIRNAME, STATUS_JSON)
        markdown_path = state_path(root_p, REPORT_DIRNAME, STATUS_MARKDOWN)
        report["written"] = {"json": str(json_path),
                             "markdown": str(markdown_path)}
        _atomic_write(json_path, json.dumps(report, indent=2, sort_keys=True,
                                            default=str) + "\n")
        _atomic_write(markdown_path, markdown_report(report) + "\n")
    return report


def _format_flags(flags: list[str] | tuple[str, ...]) -> str:
    return ", ".join(flags) if flags else "none"


def markdown_report(report: dict[str, Any]) -> str:
    """A bounded human-readable view of the JSON report."""
    lines = ["# Python→Rust migration status", ""]
    if not report.get("configured"):
        lines.append(f"Not configured: {report.get('reason', 'unknown')}")
        return "\n".join(lines)
    counts = report.get("counts") or {}
    python = counts.get("python") or {}
    rust = counts.get("rust") or {}
    config = report.get("config") or {}
    lines.extend([
        f"- Python tree: `{config.get('python')}` — "
        f"{python.get('files', 0)} file(s), {python.get('api', 0)} public "
        f"symbol(s)",
        f"- Rust tree: `{config.get('rust')}` — {rust.get('files', 0)} "
        f"file(s), {rust.get('api', 0)} public symbol(s)",
        f"- Explicit pairs: {len(config.get('pairs') or [])}; "
        f"inference: {'on' if config.get('infer') else 'off'}",
        f"- Rows: {counts.get('rows', 0)}; matched: {counts.get('matched', 0)}; "
        f"candidates: {counts.get('candidates', 0)}",
        "",
        "## Status totals",
        "",
        "| status | count |",
        "|---|---|",
    ])
    for status in MIGRATION_STATUSES:
        lines.append(f"| {status} | {report.get('status_counts', {}).get(status, 0)} |")
    lines.extend(["", "## Top gaps (planned)", ""])
    gaps = report.get("gaps") or []
    if not gaps:
        lines.append("None.")
    for row in gaps:
        lines.append(f"- `{row['python']}` — no Rust counterpart "
                     f"({row.get('python_kind') or 'symbol'})")
    if report.get("gaps_count", 0) > len(gaps):
        lines.append(f"- … {report['gaps_count'] - len(gaps)} more")
    lines.extend(["", "## Top divergences", ""])
    divergences = report.get("divergences") or []
    if not divergences:
        lines.append("None.")
    for row in divergences:
        counterpart = row.get("rust") or "no counterpart"
        lines.append(f"- `{row.get('python') or '—'}` ↔ `{counterpart}` — "
                     f"{row.get('status')}: {_format_flags(row.get('flags'))}")
    if report.get("divergences_count", 0) > len(divergences):
        lines.append(f"- … {report['divergences_count'] - len(divergences)} more")
    lines.extend(["", "## Inferred candidates (never accepted)", ""])
    candidates = report.get("candidates") or []
    if not candidates:
        lines.append("None.")
    for item in candidates:
        lines.append(f"- `{item['python']}` ↔ `{item['rust']}` — "
                     f"confidence {item['confidence']:.2f} "
                     f"({item['reason']}); accepted: false")
    if report.get("candidates_truncated"):
        lines.append("- … list truncated")
    lines.extend(["", "## Freshness", ""])
    freshness = report.get("freshness") or {}
    lines.append(f"- state: {freshness.get('state', 'unknown')}")
    for key in ("python", "rust", "config"):
        if key in freshness:
            lines.append(f"- {key}: {freshness[key]}")
    if freshness.get("reason"):
        lines.append(f"- note: {freshness['reason']}")
    lines.extend(["", "## Explicit omissions", ""])
    omissions = report.get("omissions") or []
    if not omissions:
        lines.append("None.")
    for item in omissions:
        lines.append(f"- [{item['tree']}] `{item['path']}` — {item['reason']}")
    if report.get("omissions_truncated"):
        lines.append("- … list truncated")
    lines.extend(["", "## Limits and trust", ""])
    limits = report.get("limits") or {}
    if limits:
        lines.append("- scan limits: " + ", ".join(
            f"{key}={value}" for key, value in sorted(limits.items())))
    for note in report.get("notes") or []:
        lines.append(f"- {note}")
    lines.append("- executed: false; source_modified: false")
    return "\n".join(lines)


__all__ = [
    "MAX_CANDIDATES",
    "MAX_DIVERGENCES",
    "MAX_GAPS",
    "MAX_OMISSIONS",
    "MAX_ROWS",
    "SCHEMA",
    "STATUS_JSON",
    "STATUS_MARKDOWN",
    "VERSION",
    "build_report",
    "markdown_report",
]
