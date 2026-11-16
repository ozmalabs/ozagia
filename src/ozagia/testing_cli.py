"""``python -m ozagia.testing_cli``: inspect test policy and plan selections.

Read-only. The plan is deterministic, never runs a test and never calls a
model or the network. Exit status is 0 for a plan (including a refusal
report), and 2 for a usage, configuration, IO or git error.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from ozagia.config import repo_root
from ozagia.core import testing as T


def _root(value: str | None) -> Path:
    return Path(value).resolve() if value else repo_root()


def _print_json(value: object) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, default=str))


def _format_policy(policy: T.TestPolicy) -> str:
    sources = policy.sources
    lines = [
        f"test policy: mode={policy.mode} fallback={policy.fallback} "
        f"max_tests={policy.max_tests} timeout_s={policy.timeout_s} "
        f"captured={policy.captured}",
        f"  mode source: {sources.get('mode', 'default')}",
    ]
    for name in ("tasks", "paths"):
        table = getattr(policy, name)
        if table:
            lines.append(f"  [testing.{name}]")
            for key, value in table.items():
                lines.append(f"    {key} = {value}")
    return "\n".join(lines)


def _format_plan(plan: dict[str, Any]) -> str:
    graph = plan.get("graph") or {}
    counts = plan.get("counts") or {}
    lines = [
        f"test plan: mode={plan.get('mode')} "
        f"requested={plan.get('requested_mode')} "
        f"changed={counts.get('changed_files', 0)} "
        f"selected={counts.get('selected_files', 0)} "
        f"omitted={counts.get('omitted_files', 0)} "
        f"max_tests={counts.get('max_tests')}",
        f"  graph: {graph.get('freshness')} ({graph.get('path')})"
        + (f" - {graph.get('reason')}" if graph.get("reason") else ""),
    ]
    if plan.get("mode_reason"):
        lines.append(f"  reason: {plan['mode_reason']}")
    if plan.get("fallback_used"):
        lines.append(
            f"  fallback: {plan.get('fallback')} - "
            f"{plan.get('fallback_reason')}")
    if plan.get("refused"):
        lines.append("  refused: the test policy refused to select tests")
    for item in plan.get("selected_tests") or []:
        node_ids = item.get("node_ids") or []
        suffix = f" ({len(node_ids)} node id(s))" if node_ids else ""
        lines.append(f"  selected: {item['file']}{suffix}"
                     f" [{', '.join(item.get('sources') or [])}]")
    if not plan.get("selected_tests") and not plan.get("refused"):
        lines.append("  selected: none")
    for item in plan.get("omitted") or []:
        lines.append(f"  omitted: {item['file']} - {item['reason']}")
    if plan.get("omitted_truncated"):
        lines.append("  omitted: list truncated")
    for warning in plan.get("warnings") or []:
        lines.append(f"  warning: {warning}")
    lines.append("truth_assessed: false")
    lines.append("executed: false")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ozagia-testing",
        description="inspect the test policy and plan affected tests")
    sub = parser.add_subparsers(dest="command", required=True)

    policy = sub.add_parser(
        "policy", help="print the resolved test policy")
    policy.add_argument("--task", default=None, metavar="CLASS",
                        help="task class for [testing.tasks] overrides")
    policy.add_argument("--path", action="append", default=None,
                        metavar="PATH",
                        help="repository path for [testing.paths] overrides "
                             "(repeatable)")
    policy.add_argument("--captured", action="store_true",
                        help="resolve the interactive captured-pytest default")
    policy.add_argument("--root", default=None)
    policy.add_argument("--json", action="store_true")

    plan = sub.add_parser("plan", help="plan the tests for a change")
    plan.add_argument("--base", default="HEAD")
    plan.add_argument("--head", default=None)
    plan.add_argument("--task", default=None, metavar="CLASS")
    plan.add_argument("--changed", action="append", default=None,
                      metavar="PATH", help="explicit changed path (repeatable)")
    plan.add_argument("--max-tests", type=int, default=None, metavar="N")
    plan.add_argument("--graph", default=None, metavar="PATH")
    plan.add_argument("--root", default=None)
    plan.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        root = _root(getattr(args, "root", None))
        if args.command == "policy":
            policy = T.load_policy(
                root, task_class=args.task, paths=args.path,
                captured=args.captured)
            if args.json:
                _print_json({
                    "schema": T.SCHEMA,
                    "root": str(root),
                    **policy.to_dict(),
                    "policy": policy.to_dict(),
                })
            else:
                print(_format_policy(policy))
            return 0

        change = T.plan(
            root, base=args.base, head=args.head, changed=args.changed,
            task_class=args.task, graph_path=args.graph,
            max_tests=args.max_tests)
        if args.json:
            _print_json(change)
        else:
            print(_format_plan(change))
        return 0
    except (T.TestPolicyError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
