"""``ozagia supply-chain``: offline supply-chain attack checks.

Commands::

    check [--root R] [--json] [--policy P] [--fail-on SEVERITY]
          [--budget-s N] [--max-findings N]
    rules [--json]
    explain RULE

``check`` reads manifests, lockfiles, registry configuration and package
metadata. It never fetches, installs, imports or executes project code. Exit 1
only when ``--fail-on`` is supplied and a finding reaches that severity; the
absence of a finding is never reported as a clean verdict.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from ozagia.core import supply_chain as SC

PROG = "ozagia supply-chain"
_RANK = {"none": -1, "note": 0, "low": 1, "medium": 2, "high": 3,
         "critical": 4}


def _root(value: str | None) -> Path:
    from ozagia.config import repo_root
    return Path(value).resolve() if value else repo_root()


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))


def _format_rules(rows: list[SC.Rule]) -> str:
    lines = [f"[ozagia] supply-chain rules ({len(rows)})"]
    for rule in rows:
        lines.append(f"  {rule.id} [{rule.severity}]: {rule.title}")
        lines.append(f"    {rule.recommendation}")
    return "\n".join(lines)


def _format_check(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    lines = ["[ozagia] supply-chain scan (offline, advisory)",
             f"root: {payload.get('root', '')}",
             f"state: {summary.get('state')}  "
             f"policy: {summary.get('policy_hash')}"]
    dependency = payload.get("dependency") or {}
    lines.append(f"dependency inventory: {dependency.get('manifests', 0)} "
                 f"manifest(s), {dependency.get('lockfiles', 0)} lockfile(s)")
    counts = summary.get("by_severity") or {}
    lines.append("findings: {} ({})".format(
        summary.get("findings", 0),
        ", ".join(f"{name} {counts.get(name, 0)}"
                  for name in ("critical", "high", "medium", "low", "note"))))
    if not payload["findings"]:
        lines.append("no supply-chain candidates found; coverage is not a clean "
                     "verdict")
    for finding in payload["findings"]:
        location = (finding.get("locations") or [{}])[0]
        where = location.get("path") or finding.get("file") or "(repository)"
        line = location.get("line")
        lines.append(f"[{finding['severity']}] {finding['type']} "
                     f"{where}{':' + str(line) if line else ''}")
        lines.append(f"  {finding['summary']}")
        lines.append(f"  review: {finding['recommendation']}")
        if finding.get("extra", {}).get("occurrences", 1) > 1:
            lines.append(f"  occurrences: {finding['extra']['occurrences']}")
    not_checked = payload.get("not_checked") or []
    if not_checked:
        lines.append("not checked:")
        for item in not_checked[:10]:
            lines.append(f"  - {item.get('reason')}")
        if len(not_checked) > 10:
            lines.append(f"  - +{len(not_checked) - 10} more")
    lines.append("not assessed: " + "; ".join(SC.NOT_ASSESSED))
    lines.append("truth_assessed: false")
    return "\n".join(lines)


def add_parser(sub: Any) -> None:
    parser = sub.add_parser(
        "supply-chain",
        help="offline supply-chain attack detection for dependency inputs")
    children = parser.add_subparsers(dest="action", required=True)

    check = children.add_parser(
        "check", help="inventory dependencies and run the offline rules")
    check.add_argument("--root", default=None,
                       help="checkout to inspect (default: the repo root)")
    check.add_argument("--json", action="store_true", help="print JSON")
    check.add_argument("--policy", default=None,
                       help="explicit supply-chain policy JSON path")
    check.add_argument("--budget-s", type=float, default=30.0,
                       help="total time budget in seconds (default 30)")
    check.add_argument("--max-findings", type=int, default=SC.MAX_FINDINGS,
                       help=f"finding cap (default {SC.MAX_FINDINGS})")
    check.add_argument("--fail-on", choices=tuple(_RANK), default="none",
                       help="exit 1 when a finding reaches this severity")

    rules = children.add_parser("rules", help="list the rule catalogue")
    rules.add_argument("--json", action="store_true", help="print JSON")

    explain = children.add_parser("explain", help="explain one rule")
    explain.add_argument("rule", metavar="RULE")
    explain.add_argument("--json", action="store_true", help="print JSON")


def run(args: argparse.Namespace) -> int:
    action = str(getattr(args, "action", "") or "")
    as_json = bool(getattr(args, "json", False))
    if action == "rules":
        rows = [SC.RULES[key] for key in sorted(SC.RULES)]
        if as_json:
            _print_json([rule.__dict__ for rule in rows])
        else:
            print(_format_rules(rows))
        return 0
    if action == "explain":
        rule = SC.RULES.get(str(args.rule))
        if rule is None:
            print(f"{PROG}: unknown rule {args.rule!r}", file=sys.stderr)
            return 2
        payload = {**rule.__dict__, "not_assessed": list(SC.NOT_ASSESSED)}
        _print_json(payload) if as_json else print(
            f"{rule.id} [{rule.severity}, confidence {rule.confidence}]\n"
            f"{rule.title}\n{rule.recommendation}\n"
            f"CWE: {', '.join(rule.cwe)}\n"
            "not assessed: " + "; ".join(SC.NOT_ASSESSED))
        return 0
    if action != "check":
        print(f"{PROG}: unknown action {action!r}", file=sys.stderr)
        return 2

    root = _root(getattr(args, "root", None))
    try:
        payload = SC.check(
            root, policy=getattr(args, "policy", None),
            budget_s=float(getattr(args, "budget_s", 30.0)),
            max_findings=int(getattr(args, "max_findings", SC.MAX_FINDINGS)),
        )
    except (OSError, ValueError) as exc:
        print(f"{PROG}: {exc}", file=sys.stderr)
        return 2
    payload["root"] = str(root)
    if as_json:
        _print_json(payload)
    else:
        print(_format_check(payload))
    if payload["summary"].get("state") == "invalid":
        return 2
    threshold = _RANK[str(getattr(args, "fail_on", "none"))]
    if threshold >= 0:
        for finding in payload["findings"]:
            if _RANK.get(finding["severity"], 0) >= threshold:
                return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="offline supply-chain attack detection (never fetches, "
                    "installs or executes project code)")
    children = parser.add_subparsers(dest="action", required=True)
    # Reuse the same options without the parent command wrapper.
    check = children.add_parser("check")
    check.add_argument("--root", default=None)
    check.add_argument("--json", action="store_true")
    check.add_argument("--policy", default=None)
    check.add_argument("--budget-s", type=float, default=30.0)
    check.add_argument("--max-findings", type=int, default=SC.MAX_FINDINGS)
    check.add_argument("--fail-on", choices=tuple(_RANK), default="none")
    rules = children.add_parser("rules")
    rules.add_argument("--json", action="store_true")
    explain = children.add_parser("explain")
    explain.add_argument("rule", metavar="RULE")
    explain.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    return run(build_parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
