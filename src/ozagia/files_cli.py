"""``ozagia-files``: created-file ledger, Markdown routing and cleanup/trash."""
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

    cleanup = sub.add_parser("cleanup", help="dry-run cleanup, or apply a token")
    cleanup.add_argument("--older-than-days", type=int, default=30)
    cleanup.add_argument("--classification", action="append", default=None)
    cleanup.add_argument("--apply", action="store_true",
                         help="move planned files to trash (never delete)")
    cleanup.add_argument("--confirm", default=None,
                         help="one-time token from the dry-run plan")
    cleanup.add_argument("--force", action="store_true",
                         help="record a forced move of a referenced/unknown file")
    cleanup.add_argument("--include-referenced", action="store_true",
                         help="dry-run also lists referenced files (apply needs --force)")
    cleanup.add_argument("--root", default=None)
    cleanup.add_argument("--json", action="store_true")

    trash = sub.add_parser("trash", help="list trash runs (nothing is purged)")
    trash_sub = trash.add_subparsers(dest="trash_command", required=True)
    trash_list = trash_sub.add_parser("list", help="list trash runs and files")
    trash_list.add_argument("--limit", type=int, default=F.MAX_TRASH_RUNS)
    trash_list.add_argument("--root", default=None)
    trash_list.add_argument("--json", action="store_true")

    restore = sub.add_parser("restore", help="restore files from a trash run")
    restore.add_argument("trash_id", help="trash id, directory or manifest path")
    restore.add_argument("paths", nargs="*", default=None,
                         help="only these repository-relative paths")
    restore.add_argument("--root", default=None)
    restore.add_argument("--json", action="store_true")

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
                if not args.confirm:
                    print("error: --apply requires --confirm TOKEN from a "
                          "dry-run plan", file=sys.stderr)
                    return 2
                result = F.cleanup_apply(
                    root, confirmation=args.confirm, force=args.force)
                if args.json:
                    _print_json(result)
                else:
                    print(F.format_cleanup_apply(result))
                return 0 if result.get("ok") else 1
            plan = F.cleanup_plan(
                root, older_than_days=args.older_than_days,
                classifications=args.classification,
                include_referenced=args.include_referenced)
            if args.json:
                _print_json(plan)
            else:
                print(F.format_cleanup_plan(plan))
            return 0
        if args.command == "trash" and args.trash_command == "list":
            listing = F.list_trash(root, limit=args.limit)
            if args.json:
                _print_json(listing)
            else:
                print(F.format_trash(listing))
            return 0
        if args.command == "restore":
            result = F.restore(root, args.trash_id,
                               paths=args.paths or None)
            if args.json:
                _print_json(result)
            else:
                print(F.format_restore(result))
            return 0 if result.get("ok") else 1
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
