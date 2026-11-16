"""``ozagia hooks``: opt-in Git hooks for the worktree commit/push policy.

    ozagia hooks install --pre-commit --pre-push [--root R] [--force]
                         [--python P] [--json]
    ozagia hooks uninstall [--pre-commit] [--pre-push] [--all] [--root R]
                           [--json]
    ozagia hooks status [--pre-commit] [--pre-push] [--root R] [--json]

Nothing is installed implicitly. ``uninstall`` with no kind removes both
hooks. An existing hook without the ozagia marker is kept unless ``install
--force`` is explicit. Exit status is 0 on success, 1 when a requested hook
was kept because it was not ours, and 2 for usage, Git or IO errors.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ozagia.config import repo_root
from ozagia.core import hooks as H


def _root(value: str | None) -> Path:
    return Path(value).resolve() if value else repo_root()


def _print_json(value: object) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, default=str))


def _add_kind_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--pre-commit", action="store_true",
                        help="the pre-commit hook (commit policy)")
    parser.add_argument("--pre-push", action="store_true",
                        help="the pre-push hook (push policy)")
    parser.add_argument("--all", action="store_true",
                        help="both hooks")


def _kinds(args: argparse.Namespace, *, required: bool) -> list[str] | None:
    selected = []
    if args.all:
        selected = ["commit", "push"]
    else:
        if args.pre_commit:
            selected.append("commit")
        if args.pre_push:
            selected.append("push")
    if not selected:
        if required:
            raise ValueError(
                "choose at least one hook: --pre-commit, --pre-push or --all")
        return None
    return selected


def build_parser(prog: str = "ozagia-hooks") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog,
        description="opt-in Git hooks for the worktree commit/push policy")
    sub = parser.add_subparsers(dest="command", required=True)

    install = sub.add_parser(
        "install", help="install the hooks (never automatic)")
    _add_kind_flags(install)
    install.add_argument("--force", action="store_true",
                         help="replace a hook that was not installed by ozagia")
    install.add_argument("--python", default=None,
                         help="interpreter for the hook (default: the "
                              "interpreter running ozagia, or "
                              "$OZAGIA_HOOK_PYTHON)")
    install.add_argument("--root", default=None)
    install.add_argument("--json", action="store_true")

    uninstall = sub.add_parser(
        "uninstall", help="remove only hooks installed by ozagia")
    _add_kind_flags(uninstall)
    uninstall.add_argument("--root", default=None)
    uninstall.add_argument("--json", action="store_true")

    status = sub.add_parser("status", help="show hook presence and ownership")
    _add_kind_flags(status)
    status.add_argument("--root", default=None)
    status.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None, *,
         prog: str = "ozagia-hooks") -> int:
    parser = build_parser(prog)
    args = parser.parse_args(argv)
    root = _root(args.root)
    try:
        if args.command == "install":
            kinds = _kinds(args, required=True)
            report = H.install(root, kinds, force=args.force,
                               python=args.python)
            if args.json:
                _print_json(report)
            else:
                print(H.format_install(report))
            return 0 if report.get("ok") else 1
        if args.command == "uninstall":
            kinds = _kinds(args, required=False)
            report = H.uninstall(root, kinds)
            if args.json:
                _print_json(report)
            else:
                print(H.format_uninstall(report))
            return 0 if report.get("ok") else 1
        if args.command == "status":
            kinds = _kinds(args, required=False)
            report = H.status(root, kinds)
            if args.json:
                _print_json(report)
            else:
                print(H.format_status(report))
            return 0
    except (H.HooksError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
