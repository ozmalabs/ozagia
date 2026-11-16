"""``python -m ozagia.security_cli``: offline security/antipattern scan.

Commands::

    scan --root R [--tool NAME ...] [--allow-network]
         [--fail-on blocking|high|medium|low|never] [--json] [--out FILE]
    tools
    explain FINDING_ID [--report FILE] [--root R] [--json]

``scan`` exits 0 after a completed report (findings are data), 1 when a
finding meets the selected ``--fail-on`` threshold, and 2 for usage or IO
errors. The only write is an explicit ``--out`` report file.
"""
from __future__ import annotations

import argparse
import json
import sys

from ozagia.core import security


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m ozagia.security_cli",
        description="advisory, offline security and antipattern scan")
    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser(
        "scan", help="scan an existing graph, source tree and manifests")
    scan.add_argument("--root", default=".",
                      help="repository root (default: the current directory)")
    scan.add_argument("--graph", default=None,
                      help="graph snapshot (default: <root>/.ozagia/graph.json)")
    scan.add_argument("--tool", action="append", default=None,
                      choices=sorted(security.TOOL_REGISTRY),
                      metavar="NAME",
                      help="run one explicit tool from the fixed registry "
                           "(repeatable); nothing is installed")
    scan.add_argument("--allow-network", action="store_true",
                      help="allow tools that refresh a remote database; "
                           "offline by default")
    scan.add_argument("--fail-on", default=None, choices=security.FAIL_ON,
                      help="exit 1 at or above this severity "
                           "(default: [security].fail_on or high)")
    secret_source = scan.add_mutually_exclusive_group()
    secret_source.add_argument(
        "--staged", action="store_true",
        help="also scan staged content for secrets (the commit gate)")
    secret_source.add_argument(
        "--diff", default=None, metavar="BASE..HEAD",
        help="also scan added lines in a committed range for secrets")
    scan.add_argument("--json", action="store_true",
                      help="emit the report as deterministic JSON")
    scan.add_argument("--out", default=None,
                      help="write the same output atomically to this path")

    sub.add_parser("tools",
                   help="list the fixed tool registry and local availability")

    secrets = sub.add_parser(
        "secrets", help="scan staged content or a diff range for secrets")
    secrets.add_argument("--root", default=".",
                         help="repository root (default: the current directory)")
    source = secrets.add_mutually_exclusive_group(required=True)
    source.add_argument("--staged", action="store_true",
                        help="scan staged content (the commit gate)")
    source.add_argument("--diff", default=None, metavar="BASE..HEAD",
                        help="scan added lines in a committed range")
    secrets.add_argument("--allow-secret", action="append", default=None,
                         metavar="sha256:FINGERPRINT",
                         help="allow one finding once by fingerprint; the "
                              "decision is recorded")
    secrets.add_argument("--json", action="store_true",
                         help="emit the report as deterministic JSON")
    secrets.add_argument("--out", default=None,
                         help="write the same output atomically to this path")

    explain = sub.add_parser(
        "explain", help="show one finding from a fresh scan or an existing "
                        "report")
    explain.add_argument("finding_id", metavar="FINDING_ID")
    explain.add_argument("--root", default=".",
                         help="repository root for a fresh scan")
    explain.add_argument("--graph", default=None,
                         help="graph snapshot for a fresh scan")
    explain.add_argument("--report", default=None,
                         help="read the finding from this saved JSON report "
                              "instead of scanning again")
    explain.add_argument("--tool", action="append", default=None,
                         choices=sorted(security.TOOL_REGISTRY),
                         metavar="NAME",
                         help="run one explicit tool while re-scanning")
    explain.add_argument("--allow-network", action="store_true",
                         help="allow network-requiring tools while re-scanning")
    explain.add_argument("--json", action="store_true",
                         help="emit the finding as JSON")
    return parser


def _emit(text: str, out: str | None) -> int:
    text = security.redact_text(text)
    if out:
        try:
            security.atomic_write(out, text)
        except security.SecurityError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    print(text, end="")
    return 0


def _apply_secret_overrides(report: dict, tokens: list[str] | None) -> None:
    wanted = [token for token in tokens or []
              if token.startswith("sha256:")]
    if not wanted:
        return
    kept = []
    overrides = []
    for finding in report.get("findings") or []:
        fingerprint = str(finding.get("secret_fingerprint") or "")
        token = next((item for item in wanted
                      if fingerprint == item or
                      (fingerprint and fingerprint.startswith(item))), None)
        if token is None:
            kept.append(finding)
            continue
        try:
            record = security.record_secret_decision(
                report.get("root") or ".", kind="secrets", finding=finding,
                decision="allowed_once", mode=str(report.get("mode")))
        except security.SecurityError as exc:
            print(f"error: {exc}", file=sys.stderr)
            raise
        overrides.append(record)
    report["findings"] = kept
    report["overrides"] = overrides
    summary = dict(report.get("summary") or {})
    summary["total"] = len(kept)
    summary["candidates"] = max(
        0, int(summary.get("candidates", 0)) - len(overrides))
    by_rule: dict[str, int] = {}
    for finding in kept:
        rule = str(finding.get("secret_rule") or "secret")
        by_rule[rule] = by_rule.get(rule, 0) + 1
    summary["by_rule"] = dict(sorted(by_rule.items()))
    report["summary"] = summary


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "tools":
        print(security.format_tools(), end="")
        return 0

    if args.command == "secrets":
        try:
            report = security.scan_secrets(
                args.root, staged=bool(args.staged), diff=args.diff)
            _apply_secret_overrides(report, args.allow_secret)
        except (security.SecurityError, OSError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        text = (security.report_json(report) if args.json
                else security.format_secrets_report(report))
        code = _emit(text, args.out)
        if code:
            return code
        return security.secrets_exit_code(report)

    if args.command == "explain":
        try:
            if args.report:
                report = security.load_report(args.report)
            else:
                report = security.scan(
                    args.root, graph_path=args.graph,
                    tools=args.tool, allow_network=args.allow_network)
        except (security.SecurityError, OSError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        finding = next(
            (item for item in report.get("findings") or []
             if item.get("id") == args.finding_id), None)
        if finding is None:
            print(f"error: no finding {args.finding_id!r} in "
                  f"{args.report or 'the fresh scan'}", file=sys.stderr)
            if not args.report:
                print("hint: use --report FILE for a tool-only finding, or "
                      "pass the same --tool flags", file=sys.stderr)
            return 2
        text = (security.redact_text(json.dumps(finding, indent=2,
                                                sort_keys=True)) + "\n"
                if args.json else security.format_finding(finding))
        print(text, end="")
        return 0

    try:
        report = security.scan(
            args.root, graph_path=args.graph, tools=args.tool,
            allow_network=(True if args.allow_network else None),
            fail_on=args.fail_on, staged=args.staged, diff=args.diff)
    except (security.SecurityError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    text = (security.report_json(report) if args.json
            else security.format_report(report))
    code = _emit(text, args.out)
    if code:
        return code
    return security.exit_code(report)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
