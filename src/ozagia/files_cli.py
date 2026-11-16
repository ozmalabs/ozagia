"""``ozagia-files``: created-file ledger, Markdown routing and dry-run cleanup."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ozagia.config import repo_root
from ozagia.core import files as F


def _root(value: str | None) -> Path:
    return Path(value).resolve() if value else repo_root()


def _print_json(value: object) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, default=str))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ozagia-files",
        description="track created files and route Markdown without deleting")
    sub = parser.add_subparsers(dest="command", required=True)

    listing = sub.add_parser("list", help="list ledger records")
    listing.add_argument("--classification", default=None)
    listing.add_argument("--status", default="active")
    listing.add_argument("--limit", type=int, default=200)
    listing.add_argument("--root", default=None)
    listing.add_argument("--json", action="store_true")

    scan = sub.add_parser("scan", help="record current untracked/added files")
    scan.add_argument("--session", default=None)
    scan.add_argument("--task", default=None)
    scan.add_argument("--reason", default="unknown")
    scan.add_argument("--tool", default="scan")
    scan.add_argument("--root", default=None)
    scan.add_argument("--json", action="store_true")

    explain = sub.add_parser("explain", help="why does this file exist?")
    explain.add_argument("path")
    explain.add_argument("--root", default=None)
    explain.add_argument("--json", action="store_true")

    orphans = sub.add_parser("orphans", help="unreferenced notes/scratch/unknown files")
    orphans.add_argument("--root", default=None)
    orphans.add_argument("--json", action="store_true")

    cleanup = sub.add_parser("cleanup", help="dry-run cleanup candidates")
    cleanup.add_argument("--older-than-days", type=int, default=30)
    cleanup.add_argument("--classification", action="append", default=None)
    cleanup.add_argument("--apply", action="store_true",
                         help="not enabled: this build is dry-run only")
    cleanup.add_argument("--root", default=None)
    cleanup.add_argument("--json", action="store_true")

    route = sub.add_parser("route", help="classify a Markdown path")
    route.add_argument("path")
    route.add_argument("--content-file", default=None)
    route.add_argument("--session", default=None)
    route.add_argument("--root", default=None)
    route.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = _root(args.root)
    try:
        if args.command == "list":
            records = F.list_files(root, classification=args.classification,
                                   status=args.status, limit=args.limit)
            if args.json:
                _print_json({"schema": F.SCHEMA, "files": records,
                             "count": len(records)})
            else:
                print(F.format_list(records))
            return 0
        if args.command == "scan":
            records = F.scan_new_files(root, session=args.session,
                                       task=args.task, reason=args.reason,
                                       tool=args.tool)
            if args.json:
                _print_json({"schema": F.SCHEMA, "recorded": records,
                             "count": len(records)})
            else:
                print(F.format_list(records) if records else "no new files")
            return 0
        if args.command == "explain":
            report = F.explain(root, args.path)
            if args.json:
                _print_json(report)
            else:
                print(F.format_explain(report))
            return 0
        if args.command == "orphans":
            records = F.orphans(root)
            if args.json:
                _print_json({"schema": F.SCHEMA, "files": records,
                             "count": len(records)})
            else:
                print(F.format_list(records))
            return 0
        if args.command == "cleanup":
            if args.apply:
                print("error: applying cleanup is not enabled in this build; "
                      "dry-run only", file=sys.stderr)
                return 2
            records = F.cleanup_candidates(
                root, older_than_days=args.older_than_days,
                classifications=args.classification)
            if args.json:
                _print_json({"schema": F.SCHEMA, "dry_run": True,
                             "candidates": records, "count": len(records)})
            else:
                print(F.format_cleanup(records))
            return 0
        if args.command == "route":
            content = ""
            if args.content_file:
                content = Path(args.content_file).read_text(
                    encoding="utf-8", errors="replace")
            report = F.markdown_route(
                args.path, content, root=root,
                context={"session": args.session})
            if args.json:
                _print_json(report)
            else:
                print(f"{report.get('kind')}: {report.get('path')} -> "
                      f"{report.get('suggested_path')} "
                      f"(allowed={report.get('allowed')})")
                print(f"  {report.get('reason')}")
                if report.get("duplicate_of"):
                    print(f"  duplicate_of: {report['duplicate_of']}")
            return 0 if report.get("allowed") else 1
    except (F.FileLedgerError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
