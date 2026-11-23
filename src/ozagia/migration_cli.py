"""``ozagia migration``: status, task hand-off and scaffold preview.

Commands::

    status [--json|--markdown] [--no-record] [--root R]
    tasks [--json] [--limit N] [--root R]
    scaffold SYMBOL --target FILE [--write] [--base-sha SHA] [--json] [--root R]

``status`` scans both configured trees and records a report under the state
directory (``.ozagia/migration/`` by default) unless ``--no-record`` is given.
``tasks`` emits a checklist and never creates a goal or runs anything.
``scaffold`` is a preview unless ``--write`` is given; the write goes through
:mod:`ozagia.edit.txn` with a backup.

Exit status: 0 for a report, checklist or successful scaffold; 2 for a usage,
configuration or refusal error.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from ozagia.config import MIGRATION_STATUSES, repo_root
from ozagia.migration.report import build_report, markdown_report
from ozagia.migration.scaffold import ScaffoldError, scaffold
from ozagia.migration.tasks import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    build_tasks,
    markdown_tasks,
)

PROG = "ozagia migration"


def _root(value: str | None) -> Path:
    return Path(value).resolve() if value else repo_root()


def _print_json(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, default=str))


def _format_status(report: dict[str, Any]) -> str:
    if not report.get("configured"):
        return f"{PROG}: not configured: {report.get('reason', 'unknown')}"
    config = report.get("config") or {}
    counts = report.get("counts") or {}
    python = counts.get("python") or {}
    rust = counts.get("rust") or {}
    lines = [
        f"{PROG}: {config.get('python')} -> {config.get('rust')} "
        f"(inference {'on' if config.get('infer') else 'off'})",
        f"  python: {python.get('files', 0)} file(s), "
        f"{python.get('api', 0)} public symbol(s)",
        f"  rust: {rust.get('files', 0)} file(s), "
        f"{rust.get('api', 0)} public symbol(s)",
    ]
    totals = report.get("status_counts") or {}
    lines.append("  status: " + ", ".join(
        f"{status}={totals.get(status, 0)}" for status in MIGRATION_STATUSES))
    for row in report.get("gaps") or []:
        counterpart = (row.get("rust") if row.get("rust_resolved", True)
                       else "no counterpart")
        lines.append(f"  gap: {row['python']} -> {counterpart}")
    for row in report.get("divergences") or []:
        flags = ", ".join(row.get("flags") or [])
        lines.append(f"  diverged: {row.get('python') or '—'} <-> "
                     f"{row.get('rust') or '—'} [{flags}]")
    for item in report.get("candidates") or []:
        lines.append(f"  candidate: {item['python']} ~ {item['rust']} "
                     f"({item['confidence']:.2f}, not accepted)")
    freshness = report.get("freshness") or {}
    lines.append(f"  freshness: {freshness.get('state', 'unknown')}")
    written = report.get("written") or {}
    if written.get("json"):
        lines.append(f"  report: {written['json']}")
    lines.append("  executed: false; source_modified: false")
    return "\n".join(lines)


def _format_scaffold(result: dict[str, Any]) -> str:
    status = "written" if result.get("written") else "preview (not written)"
    lines = [
        f"{PROG} scaffold: {result['symbol']} -> {result['target']} "
        f"[{status}]",
        f"  base sha256: {result['base_sha256']}",
        f"  new sha256:  {result['new_sha256']}",
        f"  parse gate:  {result['parse_gate']['note']}",
    ]
    for note in result.get("notes") or []:
        lines.append(f"  note: {note}")
    if result.get("backup"):
        lines.append(f"  backup: {result['backup']}")
    lines.append("")
    lines.append("suggested skeleton (not compiled):")
    for line in result["candidate"].splitlines():
        lines.append(f"  {line}")
    if not result.get("written"):
        lines.append("")
        lines.append("nothing was written; pass --write to append this "
                     "skeleton through the edit transaction")
    return "\n".join(lines)


def _add_actions(children: Any) -> None:
    status = children.add_parser(
        "status", help="inventory both trees and report migration status")
    status.add_argument("--root", default=None,
                        help="repository root (default: the repo root)")
    output = status.add_mutually_exclusive_group()
    output.add_argument("--json", action="store_true", help="print JSON")
    output.add_argument("--markdown", action="store_true",
                        help="print a Markdown report")
    status.add_argument(
        "--no-record", action="store_true",
        help="do not write the report under the state directory")

    tasks = children.add_parser(
        "tasks", help="emit a checklist for unported or diverged symbols")
    tasks.add_argument("--root", default=None,
                       help="repository root (default: the repo root)")
    tasks.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                       help=f"maximum tasks (default {DEFAULT_LIMIT}, "
                            f"max {MAX_LIMIT})")
    tasks.add_argument("--json", action="store_true", help="print JSON")

    scaffold_parser = children.add_parser(
        "scaffold",
        help="render a bounded Rust skeleton for a simple Python symbol")
    scaffold_parser.add_argument("symbol", help="public Python symbol key")
    scaffold_parser.add_argument("--target", required=True,
                                 help="existing .rs file inside the Rust tree")
    scaffold_parser.add_argument(
        "--write", action="store_true",
        help="append the skeleton through the edit transaction (default: "
             "preview only)")
    scaffold_parser.add_argument(
        "--base-sha", default=None,
        help="refuse the write unless the target still has this sha256")
    scaffold_parser.add_argument("--root", default=None,
                                 help="repository root (default: the repo "
                                      "root)")
    scaffold_parser.add_argument("--json", action="store_true",
                                 help="print JSON")


def add_parser(sub: Any) -> None:
    parser = sub.add_parser(
        "migration",
        help="Python-to-Rust migration status, tasks and scaffold preview")
    children = parser.add_subparsers(dest="migration_command", required=True)
    _add_actions(children)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ozagia-migration",
        description="Python-to-Rust migration status, tasks and scaffold")
    children = parser.add_subparsers(dest="migration_command", required=True)
    _add_actions(children)
    return parser


def run(args: argparse.Namespace) -> int:
    root = _root(getattr(args, "root", None))
    if args.migration_command == "status":
        try:
            report = build_report(root, write=not args.no_record)
        except (OSError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        if (not report.get("configured")
                and str(report.get("reason", "")).startswith(
                    "configuration could not be read")):
            print(f"error: {report['reason']}", file=sys.stderr)
            return 2
        if args.json:
            _print_json(report)
        elif args.markdown:
            print(markdown_report(report))
        else:
            print(_format_status(report))
        return 0

    if args.migration_command == "tasks":
        if args.limit < 1 or args.limit > MAX_LIMIT:
            print(f"error: --limit must be between 1 and {MAX_LIMIT}",
                  file=sys.stderr)
            return 2
        try:
            payload = build_tasks(root, limit=args.limit)
        except (OSError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        if (not payload.get("configured")
                and str(payload.get("reason") or "").startswith(
                    "configuration could not be read")):
            print(f"error: {payload['reason']}", file=sys.stderr)
            return 2
        if args.json:
            _print_json(payload)
        else:
            print(markdown_tasks(payload))
        return 0

    if args.migration_command == "scaffold":
        try:
            result = scaffold(root, args.symbol, args.target,
                              write=args.write, base_sha256=args.base_sha)
        except (ScaffoldError, OSError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        if args.json:
            _print_json(result)
        else:
            print(_format_scaffold(result))
        return 0

    print(f"error: unknown migration command {args.migration_command!r}",
          file=sys.stderr)  # pragma: no cover - argparse enforces choices
    return 2


def main(argv: list[str] | None = None) -> int:
    return run(build_parser().parse_args(argv))


__all__ = ["add_parser", "build_parser", "main", "run"]


if __name__ == "__main__":
    raise SystemExit(main())
