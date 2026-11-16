"""``ozagia-resources``: read-only host/resource observation and admission."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from ozagia.config import repo_root
from ozagia.core import resources as R


def _root(value: str | None) -> Path:
    return Path(value).resolve() if value else repo_root()


def _print_json(value: object) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, default=str))


def _freeing(values: list[str] | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for text in values or []:
        try:
            memory, eta = text.split(":", 1)
            out.append({"memory_mb": float(memory), "disk_mb": 0.0,
                        "eta_s": float(eta)})
        except ValueError as exc:
            raise ValueError(f"--freeing must be MEM_MB:ETA_S, got {text!r}") from exc
    return out


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ozagia-resources",
        description="read-only host resources, history and admission checks")
    sub = parser.add_subparsers(dest="command", required=True)

    status = sub.add_parser("status", help="host memory/disk/load and top groups")
    status.add_argument("--root", default=None)
    status.add_argument("--path", default=None, help="also show this filesystem")
    status.add_argument("--top", type=int, default=10)
    status.add_argument("--json", action="store_true")

    check = sub.add_parser("check", help="would this job/task/worktree fit?")
    check.add_argument("--for", dest="kind", choices=("job", "task", "worktree"),
                       default="job")
    check.add_argument("--memory-mb", type=float, default=None)
    check.add_argument("--disk-mb", type=float, default=None)
    check.add_argument("--path", default=None)
    check.add_argument("--key", default=None,
                       help="history key (e.g. a command key or task class)")
    check.add_argument("--freeing", action="append", default=None,
                       metavar="MEM_MB:ETA_S",
                       help="an ozagia job expected to free memory; repeatable")
    check.add_argument("--root", default=None)
    check.add_argument("--json", action="store_true")

    history = sub.add_parser("history", help="bounded observed-run history")
    history.add_argument("--key", default=None)
    history.add_argument("--limit", type=int, default=2000)
    history.add_argument("--root", default=None)
    history.add_argument("--json", action="store_true")

    explain = sub.add_parser("explain", help="show the admission checks as text")
    explain.add_argument("--for", dest="kind", choices=("job", "task", "worktree"),
                         default="job")
    explain.add_argument("--memory-mb", type=float, default=None)
    explain.add_argument("--disk-mb", type=float, default=None)
    explain.add_argument("--path", default=None)
    explain.add_argument("--key", default=None)
    explain.add_argument("--root", default=None)
    explain.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = _root(args.root)
    try:
        if args.command == "status":
            report = R.status(root, path=args.path, top=args.top)
            if args.json:
                _print_json(report)
            else:
                print(R.format_status(report))
            return 0
        if args.command == "history":
            report = R.history_summary(root, key=args.key, limit=args.limit)
            if args.json:
                _print_json(report)
            else:
                if not report:
                    print("no history")
                for key, item in report.items():
                    memory = (item.get("memory_mb") or {}).get("p90")
                    print(f"{key}: samples={item.get('samples')} "
                          f"confidence={item.get('confidence')} "
                          f"p90_mb={memory}")
            return 0
        policy = R.load_policy(root)
        report = R.check_admission(
            root, kind=args.kind, memory_mb=args.memory_mb,
            disk_mb=args.disk_mb, path=args.path, key=args.key,
            freeing=_freeing(getattr(args, "freeing", None)), policy=policy,
        )
        if args.json:
            _print_json(report)
        else:
            print(R.format_check(report))
        return 0 if report.get("ok") else 1
    except (ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
