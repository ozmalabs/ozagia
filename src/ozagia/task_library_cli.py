"""``python -m ozagia.task_library_cli``: browse and copy task classes.

The library is read-only. ``copy`` previews by default and writes only with
``--apply``; the destination must stay inside ``<root>/ozagia/tasks``. The
command never calls a model, reaches the network or runs a task.

Exit status is ``0`` on success, ``1`` on a library/spec validation error and
``2`` on a usage, lookup or IO error.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from ozagia.config import repo_root
from ozagia.core import task_library as L


def _root(value: str | None) -> Path:
    return Path(value).resolve() if value else repo_root()


def _print_json(value: object) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


def _format_catalog(value: dict[str, Any]) -> str:
    counts = value["counts"]
    lines = [
        f"Task classes: {counts['total']} "
        f"({counts['builtin']} built-in, {counts['repo']} repo overrides)",
        "",
    ]
    tasks = value["tasks"]
    for domain in value["domains"]:
        lines.append(f"{domain['id']} ({domain['count']})")
        for task in tasks:
            if task["domain"] != domain["id"]:
                continue
            lines.append(
                f"  {task['id']}  v{task['version']}  {task['title']}")
            lines.append(f"      {task['description']}")
        lines.append("")
    return "\n".join(lines).rstrip()


def _format_task(value: dict[str, Any]) -> str:
    entry = value["entry"]
    spec = value["spec"]
    defaults = entry["defaults"]
    lines = [
        f"id: {entry['id']}",
        f"version: {entry['version']}",
        f"domain: {entry['domain']}",
        f"title: {entry['title']}",
        f"source: {entry['source']}",
        f"path: {entry['path']}",
        f"digest: {entry['digest']}",
        f"objective: {entry['description']}",
        "tools: " + ", ".join(defaults["tools"]),
        "write_scope: " + (", ".join(defaults["write_scope"]) or "(none)"),
        f"requires_graph: {'true' if defaults['requires_graph'] else 'false'}",
        f"output: {defaults['output']}",
        "budget: " + ", ".join(
            f"{key}={value}" for key, value in defaults["budget"].items()),
        "resources: " + ", ".join(
            f"{key}={value}" for key, value in defaults["resources"].items()),
        "gates.before: " + (", ".join(defaults["gates"]["before"]) or "(none)"),
        "gates.after: " + (", ".join(defaults["gates"]["after"]) or "(none)"),
        "approval: " + ", ".join(
            f"{key}={value}" for key, value in defaults["approval"].items()),
        "steps:",
    ]
    for index, step in enumerate(spec["steps"], 1):
        lines.append(f"  {index}. {step['id']} ({step['tool']}) — {step['goal']}")
    if entry["warnings"]:
        lines.append("warnings:")
        lines.extend(f"  - {warning}" for warning in entry["warnings"])
    return "\n".join(lines)


def _format_validation(value: dict[str, Any]) -> str:
    counts = value["counts"]
    status = "ok" if value["ok"] else "invalid"
    lines = [
        f"{status}: {counts['total']} task classes "
        f"({counts['builtin']} built-in, {counts['repo']} repo overrides)",
    ]
    if value["errors"]:
        lines.append("errors:")
        lines.extend(f"  - {error}" for error in value["errors"])
    if value["warnings"]:
        lines.append("warnings:")
        lines.extend(f"  - {warning}" for warning in value["warnings"])
    return "\n".join(lines)


def _format_copy(value: dict[str, Any]) -> str:
    destination = value["relative_destination"]
    if value["unchanged"]:
        action = f"unchanged: {destination} already matches {value['task_id']}"
    elif value["written"]:
        action = f"copied {value['task_id']} v{value['version']} to {destination}"
    else:
        action = (f"dry-run: would copy {value['task_id']} v{value['version']} "
                  f"to {destination}")
    lines = [f"{action} ({value['bytes']} bytes)"]
    if value.get("fallback"):
        lines.append(
            "warning: the library is invalid; copied the built-in source")
    if value["dry_run"] and value["diff"]:
        lines.extend(["", value["diff"].rstrip()])
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ozagia-task-library",
        description="browse, validate and copy built-in task classes")
    sub = parser.add_subparsers(dest="command", required=True)

    listing = sub.add_parser("list", help="list available task classes")
    listing.add_argument("--domain", default=None,
                         help="filter by domain: research, coding or ops")
    listing.add_argument("--root", default=None)
    listing.add_argument("--json", action="store_true")

    show = sub.add_parser("show", help="show one task class and its spec")
    show.add_argument("task_id")
    show.add_argument("--root", default=None)
    show.add_argument("--json", action="store_true")

    validate = sub.add_parser(
        "validate", help="validate built-ins and repository overrides")
    validate.add_argument("--root", default=None)
    validate.add_argument("--json", action="store_true")

    catalogue = sub.add_parser(
        "catalog", help="print the catalog metadata as one object")
    catalogue.add_argument("--root", default=None)
    catalogue.add_argument("--json", action="store_true")

    copy = sub.add_parser(
        "copy", help="copy a task class into ozagia/tasks (preview default)")
    copy.add_argument("task_id")
    copy.add_argument("--to", required=True, metavar="PATH",
                      help="destination .toml file or directory inside "
                           "ozagia/tasks")
    mode = copy.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true",
                      help="preview only; this is the default")
    mode.add_argument("--apply", action="store_true",
                      help="write the file; explicit confirmation")
    copy.add_argument("--force", action="store_true",
                      help="overwrite an existing file with different content")
    copy.add_argument("--root", default=None)
    copy.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "validate":
            root = _root(args.root)
            report = L.validate_library(root)
            if args.json:
                _print_json(report)
            else:
                print(_format_validation(report))
            return 0 if report["ok"] else 1

        if args.command == "list":
            tasks = L.list_tasks(_root(args.root), domain=args.domain)
            if args.json:
                _print_json({
                    "schema": L.SCHEMA,
                    "domain": args.domain,
                    "count": len(tasks),
                    "tasks": tasks,
                })
            else:
                print(_format_catalog({
                    "counts": {
                        "builtin": sum(1 for task in tasks
                                       if task["source"] == "builtin"),
                        "repo": sum(1 for task in tasks
                                    if task["source"] == "repo"),
                        "total": len(tasks),
                    },
                    "domains": [
                        {"id": domain,
                         "count": sum(1 for task in tasks
                                      if task["domain"] == domain)}
                        for domain in (L.DOMAINS if args.domain is None
                                       else (args.domain,))
                    ],
                    "tasks": tasks,
                }))
            return 0

        if args.command == "catalog":
            report = L.catalog(_root(args.root))
            if args.json:
                _print_json(report)
            else:
                print(_format_catalog(report))
            return 0

        if args.command == "show":
            task = L.get_task(args.task_id, root=_root(args.root))
            value = task.to_dict()
            if args.json:
                _print_json(value)
            else:
                print(_format_task(value))
            return 0

        if args.command == "copy":
            result = L.copy_task(
                _root(args.root), args.task_id, args.to,
                dry_run=not args.apply,
                confirm=bool(args.apply),
                force=bool(args.force),
            )
            if args.json:
                _print_json(result)
            else:
                print(_format_copy(result))
            return 0
    except L.TaskLibraryError as exc:
        if exc.errors:
            for message in exc.errors:
                print(f"error: {message}", file=sys.stderr)
            return 1
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
