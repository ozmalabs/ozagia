"""``python -m ozagia.status_cli``: one read-only repository status snapshot."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ozagia.config import repo_root
from ozagia.core.status import format_status, status


def _root(value: str | None) -> Path:
    return Path(value).resolve() if value else repo_root()


def _print_json(value: object) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, default=str))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m ozagia.status_cli",
        description="read-only workspace, resource, file, runtime, daemon and "
                    "snapshot status")
    parser.add_argument("--root", default=None)
    parser.add_argument("--path", default=None,
                        help="also observe this filesystem's resources")
    parser.add_argument("--top", type=int, default=10,
                        help="process groups to report by memory (default 10)")
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = _root(args.root)
    try:
        report = status(root, path=args.path, top=args.top)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.json:
        _print_json(report)
    else:
        print(format_status(report))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
