"""``python -m ozagia.repo_scan_cli``: advisory repository scan.

Commands::

    scan --root R [--since N] [--top N] [--json] [--out FILE]
    list-categories

``scan`` exits 0 when the graph is missing or stale: an unavailable graph is a
reported state, not a tool failure. Usage and IO errors exit 2. The command is
read-only apart from an explicit ``--out`` report file.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ozagia.core import repo_scan as scan_core


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m ozagia.repo_scan_cli",
        description="advisory, read-only repository scan")
    sub = parser.add_subparsers(dest="command", required=True)
    scan = sub.add_parser(
        "scan", help="rank development opportunities from the existing graph")
    scan.add_argument("--root", default=".",
                      help="repository root (default: the current directory)")
    scan.add_argument("--since", type=int, default=scan_core.DEFAULT_SINCE_DAYS,
                      metavar="N", help="churn/stale window in days "
                                        f"(default: {scan_core.DEFAULT_SINCE_DAYS})")
    scan.add_argument("--top", type=int, default=scan_core.DEFAULT_TOP,
                      metavar="N", help="maximum opportunities to show "
                                        f"(default: {scan_core.DEFAULT_TOP})")
    scan.add_argument("--json", action="store_true",
                      help="emit the report as deterministic JSON")
    scan.add_argument("--out", default=None,
                      help="write the same output atomically to this path")
    sub.add_parser("list-categories",
                   help="list scan categories and their descriptions")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "list-categories":
        print(scan_core.format_categories(), end="")
        return 0
    if args.top < 1:
        print("error: --top must be a positive integer", file=sys.stderr)
        return 2
    if args.since < 1:
        print("error: --since must be a positive integer", file=sys.stderr)
        return 2
    try:
        report = scan_core.scan(
            Path(args.root), since_days=args.since, top=args.top)
    except (scan_core.RepoScanError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    text = (scan_core.report_json(report) if args.json
            else scan_core.format_report(report))
    if args.out:
        try:
            scan_core.atomic_write(args.out, text)
        except scan_core.RepoScanError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    print(text, end="")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
