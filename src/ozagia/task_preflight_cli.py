"""``ozagia task-preflight``: a read-only task preflight for one goal.

    ozagia task-preflight GOAL [--json] [--limit N] [--cursor C] [--root R]
                            [--checkout DIR ...] [--scope ENTRY ...]
                            [--base REV] [--timeout-s S]

The command composes the goal's criteria state, graph freshness, capability
readiness, interpreter candidates, resource admission and affected-test plan.
It never runs a test, refreshes the graph, starts a daemon or writes to a
goal/task/session/graph store.

Exit status: ``0`` only when the assessment is green (every required
dimension current and true); ``1`` for unknown or failed; ``2`` for an
invalid request or a refusal (including an absent goal).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from ozagia.core import contracts as C
from ozagia.core import task_preflight as TP

PROG = "ozagia task-preflight"


def add_parser(sub: Any) -> None:
    parser = sub.add_parser(
        "task-preflight",
        help="read-only goal/task preflight (assessment, criteria, evidence)")
    parser.add_argument("goal", help="goal id (g1 or goal:g1)")
    parser.add_argument("--json", action="store_true", help="print JSON")
    parser.add_argument("--limit", type=int, default=TP.DEFAULT_LIMIT,
                        help=f"criteria page size (default "
                             f"{TP.DEFAULT_LIMIT}, max {TP.MAX_LIMIT})")
    parser.add_argument("--cursor", default=None,
                        help="opaque criteria page cursor from a previous call")
    parser.add_argument("--root", default=None, help="repository root")
    parser.add_argument("--checkout", action="append", default=None,
                        metavar="DIR",
                        help="additional checkout to include (repeatable)")
    parser.add_argument("--scope", action="append", default=None,
                        metavar="ENTRY",
                        help="scope entry (repeatable; default: the goal's)")
    parser.add_argument("--base", default="HEAD",
                        help="revision for changed-file discovery (default HEAD)")
    parser.add_argument("--timeout-s", type=int, default=30,
                        help="caller-owned preflight timeout in seconds "
                             "(default 30, max 600)")


def _root(value: str | None) -> Path:
    if value:
        return Path(value).expanduser().resolve()
    from ozagia.config import repo_root
    return repo_root()


def payload(args: argparse.Namespace) -> dict[str, Any]:
    """Build the report for one parsed command."""
    if args.timeout_s < 0 or args.timeout_s * 1000 > TP.MAX_TIMEOUT_MS:
        raise TP.PreflightError(
            f"--timeout-s must be between 0 and "
            f"{TP.MAX_TIMEOUT_MS // 1000}")
    root = _root(args.root)
    providers = TP.default_providers()
    return TP.build_preflight(
        root, args.goal,
        scope=args.scope,
        checkouts=args.checkout,
        base=args.base,
        providers=providers,
        timeout_ms=args.timeout_s * 1000,
        limit=args.limit,
        cursor=args.cursor,
    )


def _print(payload_value: Any) -> None:
    print(json.dumps(payload_value, indent=2, sort_keys=True,
                     ensure_ascii=False, allow_nan=False))


def _error(message: str, *, as_json: bool, code: str = "invalid_request") -> int:
    if as_json:
        _print({"ok": False, "code": code, "error": message})
    else:
        print(f"{PROG}: {message}", file=sys.stderr)
    return 2


def _format(report: dict[str, Any]) -> str:
    assessment = report["assessment"]
    goal = report["goal"]
    lines = [f"goal {goal['local_id']}: {assessment['status']}"]
    lines.append("dimensions:")
    for name in TP.DIMENSIONS:
        row = report["dimensions"][name]
        readiness = row["readiness"]
        marker = ("true" if readiness is True else
                  "false" if readiness is False else "unknown")
        lines.append(f"  {name:13} {marker:7}  {row['reason']}")
    if assessment["reasons"]:
        lines.append("reasons:")
        for row in assessment["reasons"]:
            lines.append(f"  {row['dimension']}: {row['reason']}")
    criteria = report["criteria"]
    lines.append(
        f"criteria: {criteria['total']} total, "
        f"{criteria['counts'].get('passed', 0)} passed, "
        f"{criteria['counts'].get('failed', 0)} failed, "
        f"{criteria['counts'].get('unknown', 0)} unknown, "
        f"{criteria['counts'].get('not_assessed', 0)} not assessed")
    for item in criteria["items"]:
        test = item.get("test") or {}
        detail = f" ({test.get('outcome')})" if test.get("outcome") else ""
        lines.append(
            f"  {item['id']} {item['kind']} -> {item['result']} "
            f"[{item['evidence']}/{item['freshness']}]{detail}"
            + (f": {item['reason']}" if item.get("reason") else ""))
    truncation = criteria.get("truncated") or {}
    if truncation.get("truncated"):
        lines.append(
            f"  truncated: {truncation['omitted']} more; next --cursor "
            f"{truncation['next_cursor']}")
    if report["not_assessed"]:
        lines.append("not assessed:")
        lines.extend(f"  - {item}" for item in report["not_assessed"])
    return "\n".join(lines)


def run(args: argparse.Namespace) -> int:
    """Run one parsed ``ozagia task-preflight`` command."""
    try:
        report = payload(args)
    except TP.PreflightError as exc:
        return _error(str(exc), as_json=bool(args.json), code=exc.code)
    except C.ContractError as exc:
        return _error(str(exc), as_json=bool(args.json), code=exc.code)
    except OSError as exc:
        return _error(f"{type(exc).__name__}: {exc}", as_json=bool(args.json))
    if args.json:
        _print(report)
    else:
        print(_format(report))
    return 0 if report["assessment"]["status"] == "green" else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=PROG)
    parser.add_argument("goal")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--limit", type=int, default=TP.DEFAULT_LIMIT)
    parser.add_argument("--cursor", default=None)
    parser.add_argument("--root", default=None)
    parser.add_argument("--checkout", action="append", default=None)
    parser.add_argument("--scope", action="append", default=None)
    parser.add_argument("--base", default="HEAD")
    parser.add_argument("--timeout-s", type=int, default=30)
    return run(parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
