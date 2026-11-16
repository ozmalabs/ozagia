"""``ozagia-worktree``: deterministic worktree, branch and policy checks."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ozagia.config import repo_root
from ozagia.core import security as SEC
from ozagia.core import worktree as W


def _root(value: str | None) -> Path:
    return Path(value).resolve() if value else repo_root()


def _print_json(value: object) -> None:
    print(SEC.redact_text(json.dumps(value, indent=2, sort_keys=True,
                                     default=str)))


def _checks(values: list[str] | None) -> dict[str, bool]:
    out: dict[str, bool] = {}
    for text in values or []:
        name, _, status = text.partition("=")
        if not name or status not in {"passed", "failed", "true", "false"}:
            raise ValueError(f"--check must be NAME=passed|failed, got {text!r}")
        out[name] = status in {"passed", "true"}
    return out


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ozagia-worktree",
        description="read-only worktree, branch, drift and policy checks")
    sub = parser.add_subparsers(dest="command", required=True)

    listing = sub.add_parser("list", help="list worktrees and branches")
    listing.add_argument("--root", default=None)
    listing.add_argument("--json", action="store_true")

    status = sub.add_parser("status", help="status for one worktree")
    status.add_argument("path", nargs="?", default=None)
    status.add_argument("--base", default="main")
    status.add_argument("--config-dir", default=None)
    status.add_argument("--root", default=None)
    status.add_argument("--json", action="store_true")

    check = sub.add_parser("check", help="check every worktree for wrong branch/drift")
    check.add_argument("--base", default="main")
    check.add_argument("--config-dir", default=None)
    check.add_argument("--root", default=None)
    check.add_argument("--json", action="store_true")

    policy = sub.add_parser("policy", help="check commit/merge/push policy")
    policy.add_argument("path", nargs="?", default=None)
    policy.add_argument("--for", dest="kind", required=True,
                        choices=("commit", "merge", "push"))
    policy.add_argument("--base", default="main")
    policy.add_argument("--target", default=None)
    policy.add_argument("--message", default=None)
    policy.add_argument("--check", action="append", default=None,
                        metavar="NAME=passed|failed")
    policy.add_argument("--force", action="store_true")
    policy.add_argument("--tags", action="store_true")
    policy.add_argument("--allow-secret", action="append", default=None,
                        metavar="sha256:FINGERPRINT",
                        help="allow one finding once by fingerprint; the "
                             "decision is recorded")
    policy.add_argument("--config-dir", default=None)
    policy.add_argument("--root", default=None)
    policy.add_argument("--json", action="store_true")

    prune = sub.add_parser("prune", help="show what git worktree prune would do")
    prune.add_argument("--dry-run", action="store_true",
                       help="required; this build never prunes")
    prune.add_argument("--root", default=None)
    prune.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = _root(args.root)
    try:
        if args.command == "list":
            worktrees = W.discover(root)
            if args.json:
                _print_json({"schema": W.SCHEMA, "root": str(root),
                             "worktrees": worktrees})
            else:
                print(W.format_list(worktrees))
            return 0
        if args.command == "check":
            report = W.check(root, base=args.base, config_dir=args.config_dir)
            if args.json:
                _print_json(report)
            else:
                print(W.format_check(report))
            return 0 if report.get("ok") else 1
        if args.command == "status":
            report = W.check(root, base=args.base, config_dir=args.config_dir)
            selected = Path(args.path).resolve() if args.path else root
            item = next((worktree for worktree in report.get("worktrees") or []
                         if Path(worktree.get("path", "")).resolve() == selected),
                        None)
            if item is None:
                print(f"error: {selected} is not a worktree of {root}",
                      file=sys.stderr)
                return 2
            if args.json:
                _print_json(item)
            else:
                print(f"{item.get('path')}  {item.get('branch')}  "
                      f"head={(item.get('head') or '')[:12]}  "
                      f"ahead={item.get('ahead')} behind={item.get('behind')}  "
                      f"dirty={sum(int((item.get('dirty') or {}).get(k, 0)) for k in ('staged', 'unstaged', 'untracked', 'conflicted'))}")
                for finding in item.get("findings") or []:
                    print(f"  {finding.get('level')}: {finding.get('message')}")
            return 0 if not any(f.get("level") == "error"
                                for f in item.get("findings") or []) else 1
        if args.command == "policy":
            path = Path(args.path).resolve() if args.path else root
            report = W.policy_check(
                path, args.kind, base=args.base, target=args.target,
                message=args.message, checks=_checks(args.check),
                force=args.force, tags=args.tags, config_dir=args.config_dir,
                allow_secrets=args.allow_secret or (),
            )
            if args.json:
                _print_json(report)
            else:
                print(SEC.redact_text(W.format_policy(report)), end="")
            return 0 if report.get("ok") else 1
        if args.command == "prune":
            if not args.dry_run:
                print("error: this build only supports `prune --dry-run`",
                      file=sys.stderr)
                return 2
            report = W.prune_dry_run(root)
            if args.json:
                _print_json(report)
            else:
                print(report.get("output") or "nothing to prune")
            return 0 if report.get("ok") else 1
    except (W.WorktreeError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
