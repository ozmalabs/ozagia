"""``python -m ozagia.tasks_cli``: validate, preview and run bounded tasks.

``run`` is a preview unless ``--allow-exec`` is passed. The CLI never calls a
model and never supplies a tool executor: a test or host integration must
inject one through :func:`main` (``executor=...``). Without one, an
``--allow-exec`` run is refused and records the refusal.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from ozagia.config import repo_root
from ozagia.core import tasks as T


def _root(value: str | None) -> Path:
    return Path(value).resolve() if value else repo_root()


def _print_json(value: object) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, default=str))


def _inputs(values: list[str] | None) -> dict[str, str]:
    result: dict[str, str] = {}
    for text in values or []:
        name, sep, value = text.partition("=")
        if not sep or not name:
            raise ValueError(f"--input must be NAME=VALUE, got {text!r}")
        if name in result:
            raise ValueError(f"--input {name!r} is given more than once")
        result[name] = value
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ozagia-tasks",
        description="validate, preview and run bounded tasks")
    sub = parser.add_subparsers(dest="command", required=True)

    validate = sub.add_parser("validate", help="validate a task specification")
    validate.add_argument("spec")
    validate.add_argument("--json", action="store_true")

    run = sub.add_parser("run", help="preview or run a bounded task")
    run.add_argument("spec")
    run.add_argument("--input", action="append", default=None,
                     metavar="K=V", help="an input value; repeatable")
    run.add_argument("--dry-run", action="store_true",
                     help="validate and preview even with --allow-exec")
    run.add_argument("--allow-exec", action="store_true",
                     help="execute steps through the injected executor")
    run.add_argument("--approve", action="store_true",
                     help="approve mutating steps whose approval is 'ask'")
    run.add_argument("--root", default=None)
    run.add_argument("--json", action="store_true")

    listing = sub.add_parser("list", help="list recorded task runs")
    listing.add_argument("--root", default=None)
    listing.add_argument("--limit", type=int, default=50)
    listing.add_argument("--json", action="store_true")

    show = sub.add_parser("show", help="show one recorded task run")
    show.add_argument("run", nargs="?", default=None)
    show.add_argument("--root", default=None)
    show.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None, *,
         executor: Any = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "validate":
            report = T.validate_spec(args.spec)
            if args.json:
                _print_json(report)
            else:
                print(T.format_validate(report))
            return 0 if report.get("ok") else 1
        if args.command == "run":
            spec = T.load_spec(args.spec)
            root = _root(args.root)
            runner = T.TaskRunner(spec, root, executor=executor)
            execute = bool(args.allow_exec and not args.dry_run)
            report = runner.run(
                _inputs(args.input), allow_exec=execute,
                approve=bool(args.approve), executor=executor)
            if args.json:
                _print_json(report)
            elif report.get("executed"):
                print(T.format_run(report))
            else:
                print(T.format_preview(report))
            return 0 if report.get("ok") else 1
        if args.command == "list":
            root = _root(args.root)
            runs = T.list_runs(root, limit=args.limit)
            if args.json:
                _print_json({"schema": T.SCHEMA, "root": str(root),
                             "runs": runs})
            else:
                print(T.format_runs(runs))
            return 0
        if args.command == "show":
            root = _root(args.root)
            run_id = args.run
            if run_id is None:
                runs = T.list_runs(root, limit=1)
                if not runs:
                    raise T.TaskRunError(f"no task runs under {root}")
                run_id = runs[0]["run_id"]
            report = T.show_run(root, run_id)
            if args.json:
                _print_json(report)
            else:
                print(T.format_show(report))
            return 0
    except T.TaskSpecError as exc:
        for error in exc.errors:
            print(f"error: {error}", file=sys.stderr)
        for warning in exc.warnings:
            print(f"warning: {warning}", file=sys.stderr)
        return 1
    except (T.TaskError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
