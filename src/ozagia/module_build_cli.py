"""``python -m ozagia.module_build_cli``: preview-first module scaffolding.

Commands::

    preview --root R [--report PATH] [--opportunity ID] [--dest DIR] [--json]
    apply   --root R [--report PATH] [--opportunity ID] [--dest DIR] \
            --confirm TOKEN [--force] [--json]
    list    --root R [--json]

``preview`` is the default and writes nothing. ``apply`` rebuilds the same
plan, verifies its one-time token against the root and plan digest, re-checks
every path, writes the files atomically and records an audit under
``.ozagia/modules/``.

Exit codes: 0 for a preview, a successful apply or a list; 1 for a refused
token, path, collision or replay; 2 for a usage, report or IO error.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from ozagia.core import module_build as core


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m ozagia.module_build_cli",
        description="preview-first module scaffolding from a repo-scan report")
    sub = parser.add_subparsers(dest="command", required=True)

    for name, help_text in (
            ("preview", "build and print a scaffold plan; write nothing"),
            ("apply", "apply a previewed plan with its one-time token")):
        item = sub.add_parser(name, help=help_text)
        item.add_argument(
            "--root", default=".",
            help="repository root (default: the current directory)")
        item.add_argument(
            "--report", default=None,
            help="repo-scan report JSON (default: a conventional path under "
                 + ", ".join(core.DEFAULT_REPORT_PATHS) + ")")
        item.add_argument(
            "--opportunity", default=None, metavar="ID",
            help="opportunity id to scaffold (required when the report has "
                 "more than one)")
        item.add_argument(
            "--dest", default=None,
            help="destination relative to the root (default: modules/<id>)")
        item.add_argument(
            "--json", action="store_true", help="emit structured JSON")
        if name == "apply":
            item.add_argument(
                "--confirm", required=True, metavar="TOKEN",
                help="one-time token printed by preview")
            item.add_argument(
                "--force", action="store_true",
                help="replace existing target files (the token must still "
                     "match and is still one-time)")

    listing = sub.add_parser("list", help="list applied scaffolds from the audit")
    listing.add_argument("--root", default=".")
    listing.add_argument("--json", action="store_true")
    return parser


def _plan_from_args(args: argparse.Namespace) -> dict[str, Any]:
    report = args.report
    if report is None and args.opportunity is None:
        default = core.default_report_path(args.root)
        if default is None:
            raise core.ModuleBuildError(
                "no report given and no conventional report under "
                + ", ".join(core.DEFAULT_REPORT_PATHS))
        report = default
    return core.build_plan(
        args.root, report_path=report, opportunity_id=args.opportunity,
        dest=args.dest)


def _print_json(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, allow_nan=False))


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "list":
        try:
            records = core.list_applied(args.root)
            root = str(Path(args.root).expanduser().resolve())
        except (core.ModuleBuildError, OSError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        if args.json:
            _print_json({
                "schema": core.AUDIT_SCHEMA,
                "root": root,
                "records": records,
            })
        else:
            print(core.format_applied_list(records), end="")
        return 0

    try:
        plan = _plan_from_args(args)
    except core.ModuleBuildRefused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    except (core.ModuleBuildError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.command == "preview":
        print(core.plan_json(plan) if args.json else core.format_plan(plan),
              end="")
        return 0

    try:
        result = core.apply(args.root, plan, args.confirm, force=args.force)
    except core.ModuleBuildRefused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    except (core.ModuleBuildError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.json:
        _print_json(result)
    else:
        print(core.format_applied(result), end="")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
