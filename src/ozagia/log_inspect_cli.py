"""``python -m ozagia.log_inspect_cli``: bounded, redacted local log review.

Only the paths passed with ``--log`` are read. The command writes locally; it
does not scan a home directory, execute a task or upload anything.
"""
from __future__ import annotations

import argparse
import sys

from ozagia.core import log_inspect as L


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m ozagia.log_inspect_cli",
        description="bounded, redacted inspection of explicitly listed local "
                    "logs (OpenCode, daemon journal, eval results, JSONL/text)")
    parser.add_argument("--log", action="append", required=True,
                        metavar="PATH",
                        help="explicit input file; repeatable (.gz supported)")
    parser.add_argument("--root", default=None,
                        help="resolve relative --log paths against this "
                             "directory; it is not scanned")
    parser.add_argument("--since", default=None, metavar="ISO",
                        help="drop timestamped records older than this ISO "
                             "time (untimestamped records are kept)")
    parser.add_argument("--limit", type=int, default=20,
                        help="maximum findings shown (default 20, max 100)")
    parser.add_argument("--json", action="store_true",
                        help="emit the report as JSON")
    parser.add_argument("--out", default=None,
                        help="also write the text or JSON report to this path")
    parser.add_argument("--task-spec", default=None, metavar="FILE",
                        help="write a bounded, read-only task specification "
                             "describing this inspection; it is not run")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = L.inspect_logs(
            args.log, root=args.root, since=args.since, limit=args.limit)
    except (L.LogInspectError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    text = L.report_json(report) if args.json else L.format_report(report)
    try:
        if args.out:
            L.atomic_write(args.out, text)
        if args.task_spec:
            L.atomic_write(args.task_spec, L.task_spec_json(report))
    except L.LogInspectError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(text, end="")
    if report.get("errors"):
        print(
            f"error: {len(report['errors'])} input(s) unreadable",
            file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
