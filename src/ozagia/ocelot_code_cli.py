"""``python -m ozagia.ocelot_code_cli``: optional code-finding clustering.

Commands::

    cluster --root R --findings FILE [--ocelot-root PATH]
            [--mode auto|inprocess|subprocess] [--timeout S] [--json]
    impact  --root R (--diff BASE..HEAD | --changed PATH) [--test-plan]
            [--graph PATH] [--ocelot-root PATH] [--mode ...] [--json]
    patterns (--report FILE | --root R) [--graph PATH]
            [--ocelot-root PATH] [--mode ...] [--json]
    status  [--root R] [--dest PATH] [--json]

``cluster`` reads code-review findings or repo-scan opportunities and adds
optional Ocelot structural clusters. ``impact`` builds change shapes and
groups similar shapes into candidate test sets; ``patterns`` groups scan
opportunities or graph file shapes into candidate architecture patterns.
Ocelot is never installed or downloaded. An unavailable checkout is a
reported state with exact fallback, so these commands exit 0; usage, IO and
JSON errors exit 2. ``status`` reports the installer's resolution (path,
version, source and licence) without a network call.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping

from ozagia.core import ocelot_code as OC
from ozagia.integrations import ocelot as ocelot_installer


def _print_json(value: object) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, default=str))


def _load_findings(path: str) -> list[dict[str, Any]]:
    try:
        raw = Path(path).expanduser().read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"cannot read findings file {path}: {exc}") from exc
    try:
        data = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"findings file {path} is not valid JSON: {exc}") from exc
    if isinstance(data, Mapping):
        for key in ("findings", "opportunities", "items", "rows"):
            value = data.get(key)
            if isinstance(value, list):
                data = value
                break
        else:
            raise ValueError(
                "findings JSON object has no findings/opportunities list")
    if not isinstance(data, list):
        raise ValueError("findings JSON must be a list or an object with a list")
    return [dict(item) for item in data if isinstance(item, Mapping)]


def _load_report(path: str) -> tuple[list[dict[str, Any]], str | None]:
    """Load a scan report or a list-shaped JSON file and its recorded root."""
    try:
        raw = Path(path).expanduser().read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"cannot read report {path}: {exc}") from exc
    try:
        data = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"report {path} is not valid JSON: {exc}") from exc
    root: str | None = None
    if isinstance(data, Mapping):
        recorded = data.get("root")
        root = recorded if isinstance(recorded, str) else None
        for key in ("opportunities", "items", "rows", "findings", "files",
                    "shapes"):
            value = data.get(key)
            if isinstance(value, list):
                data = value
                break
        else:
            raise ValueError(
                "report JSON has no opportunities/items/files list")
    if not isinstance(data, list):
        raise ValueError("report JSON must be a list or an object with a list")
    return [dict(item) for item in data if isinstance(item, Mapping)], root


def _format_cluster(result: Mapping[str, Any]) -> str:
    ocelot = result.get("ocelot") or {}
    mode = result.get("mode")
    actual = result.get("actual_mode")
    clusters = result.get("clusters") or []
    multi = [cluster for cluster in clusters
             if isinstance(cluster, Mapping)
             and len(cluster.get("findings") or []) > 1]
    lines = [
        "Ocelot code clustering",
        f"findings: {result.get('finding_count', 0)}",
        f"state: {result.get('state')}",
        f"available: {'yes' if result.get('available') else 'no'}",
        f"mode: {mode}" + (f" ({actual})" if actual else ""),
        f"engine: {result.get('engine')}",
        f"clusters: {len(clusters)} ({len(multi)} with more than one finding)",
        f"analogies: {len(result.get('analogies') or [])}",
    ]
    if isinstance(ocelot, Mapping) and ocelot.get("path"):
        lines.append(
            f"ocelot: {ocelot.get('path')} "
            f"({ocelot.get('version') or 'unknown version'}, "
            f"{ocelot.get('source') or 'unknown source'})")
    if result.get("error"):
        lines.append(f"error: {result['error']}")
    if result.get("note"):
        lines.append(f"note: {result['note']}")
    for cluster in multi:
        key = cluster.get("key") or {}
        target = key.get("target_id") or ""
        lines.append(
            f"  [{key.get('family')}] {key.get('target_type')} {target}: "
            + ", ".join(cluster.get("findings") or []))
    lines.append("truth_assessed: false")
    return "\n".join(lines) + "\n"


def _format_impact(result: Mapping[str, Any]) -> str:
    ocelot = result.get("ocelot") or {}
    clusters = result.get("clusters") or []
    lines = [
        "Ocelot test-impact clusters (candidate test sets)",
        f"shapes: {result.get('shape_count', 0)}",
        f"state: {result.get('state')}",
        f"available: {'yes' if result.get('available') else 'no'}",
        f"mode: {result.get('mode')}"
        + (f" ({result.get('actual_mode')})"
           if result.get("actual_mode") else ""),
        f"engine: {result.get('engine')}",
        f"clusters: {len(clusters)}",
        f"analogies: {len(result.get('analogies') or [])}",
    ]
    if isinstance(ocelot, Mapping) and ocelot.get("path"):
        lines.append(
            f"ocelot: {ocelot.get('path')} "
            f"({ocelot.get('version') or 'unknown version'}, "
            f"{ocelot.get('source') or 'unknown source'})")
    if result.get("error"):
        lines.append(f"error: {result['error']}")
    if result.get("note"):
        lines.append(f"note: {result['note']}")
    for cluster in clusters:
        if not isinstance(cluster, Mapping):
            continue
        tests = cluster.get("candidate_tests") or []
        planned = cluster.get("planned_tests") or []
        lines.append(
            f"  {cluster.get('cluster_id')} [{cluster.get('family')}] "
            f"{len(cluster.get('shape_ids') or [])} shape(s), origin "
            f"{cluster.get('origin')}, confidence {cluster.get('confidence')}")
        lines.append(
            "    candidate tests: "
            + (", ".join(tests[:10]) if tests else "none identified"))
        if planned:
            lines.append("    planned tests (not run): " + ", ".join(planned[:10]))
    lines.append("test_set_complete: false")
    lines.append("truth_assessed: false")
    return "\n".join(lines) + "\n"


def _format_patterns(result: Mapping[str, Any]) -> str:
    ocelot = result.get("ocelot") or {}
    patterns = result.get("patterns") or []
    lines = [
        "Ocelot architecture patterns (candidates)",
        f"shapes: {result.get('shape_count', 0)}",
        f"state: {result.get('state')}",
        f"available: {'yes' if result.get('available') else 'no'}",
        f"mode: {result.get('mode')}"
        + (f" ({result.get('actual_mode')})"
           if result.get("actual_mode") else ""),
        f"engine: {result.get('engine')}",
        f"patterns: {len(patterns)}",
        f"analogies: {len(result.get('analogies') or [])}",
    ]
    if isinstance(ocelot, Mapping) and ocelot.get("path"):
        lines.append(
            f"ocelot: {ocelot.get('path')} "
            f"({ocelot.get('version') or 'unknown version'}, "
            f"{ocelot.get('source') or 'unknown source'})")
    if result.get("error"):
        lines.append(f"error: {result['error']}")
    if result.get("note"):
        lines.append(f"note: {result['note']}")
    for pattern in patterns:
        if not isinstance(pattern, Mapping):
            continue
        lines.append(
            f"  {pattern.get('pattern_id')} [{pattern.get('family')}] "
            f"{pattern.get('file_count', 0)} file(s), origin "
            f"{pattern.get('origin')}, confidence {pattern.get('confidence')}")
        files = pattern.get("files") or []
        if files:
            lines.append("    files: " + ", ".join(files[:8]))
    lines.append("candidate: true")
    lines.append("truth_assessed: false")
    return "\n".join(lines) + "\n"


def _format_status(result: Mapping[str, Any]) -> str:
    lines = [
        f"state: {result.get('state')}",
        f"installed: {'yes' if result.get('installed') else 'no'}",
        f"path: {result.get('path') or '-'}",
        f"version: {result.get('version') or 'unknown'}",
        f"source: {result.get('source') or '-'}",
        f"install root: {result.get('install_root')}",
    ]
    if result.get("manifest_path"):
        lines.append(f"manifest: {result.get('manifest_path')}")
    lines.append("licence: MIT (Ocelot is MIT-licensed; ozagia does not bundle it)")
    lines.append("reconciliation: optional")
    if result.get("note"):
        lines.append(f"note: {result.get('note')}")
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m ozagia.ocelot_code_cli",
        description="optional Ocelot structural clustering for code findings; "
                    "Ocelot is never installed or downloaded")
    sub = parser.add_subparsers(dest="command", required=True)

    cluster = sub.add_parser(
        "cluster", help="cluster findings from a JSON file")
    cluster.add_argument("--root", required=True,
                         help="repository root used for sibling resolution")
    cluster.add_argument("--findings", required=True,
                         help="JSON list, review packet or scan report")
    cluster.add_argument("--ocelot-root", default=None,
                         help="explicit Ocelot checkout (default: resolve one)")
    cluster.add_argument("--mode", default="auto", choices=OC.MODES,
                         help="worker transport (default: auto)")
    cluster.add_argument("--timeout", type=int, default=OC.DEFAULT_TIMEOUT,
                         help=f"worker timeout in seconds "
                              f"(default: {OC.DEFAULT_TIMEOUT})")
    cluster.add_argument("--json", action="store_true",
                         help="emit the result as sorted JSON")

    impact = sub.add_parser(
        "impact", help="cluster similar change shapes into candidate tests")
    impact.add_argument("--root", required=True,
                        help="repository root")
    impact.add_argument("--diff", action="append", default=None,
                        metavar="BASE..HEAD",
                        help="change range to shape (repeatable)")
    impact.add_argument("--changed", action="append", default=None,
                        metavar="PATH",
                        help="explicit repository-relative changed path "
                             "(repeatable)")
    impact.add_argument("--graph", default=None,
                        help="graph snapshot (default: <root>/.ozagia/graph.json)")
    impact.add_argument("--test-plan", action="store_true",
                        help="attach core.testing.plan selections; never runs")
    impact.add_argument("--ocelot-root", default=None,
                        help="explicit Ocelot checkout (default: resolve one)")
    impact.add_argument("--mode", default="auto", choices=OC.MODES,
                        help="worker transport (default: auto)")
    impact.add_argument("--timeout", type=int, default=OC.DEFAULT_TIMEOUT,
                        help=f"worker timeout in seconds "
                             f"(default: {OC.DEFAULT_TIMEOUT})")
    impact.add_argument("--json", action="store_true",
                        help="emit the result as sorted JSON")

    patterns = sub.add_parser(
        "patterns",
        help="cluster opportunities or graph file shapes into candidate "
             "architecture patterns")
    source = patterns.add_mutually_exclusive_group(required=True)
    source.add_argument("--report", default=None,
                        help="scan report or list-shaped JSON with "
                             "opportunities")
    source.add_argument("--root", default=None,
                        help="repository root; uses graph file shapes with a "
                             "file-list fallback")
    patterns.add_argument("--graph", default=None,
                          help="graph snapshot (default: <root>/.ozagia/graph.json)")
    patterns.add_argument("--ocelot-root", default=None,
                          help="explicit Ocelot checkout (default: resolve one)")
    patterns.add_argument("--mode", default="auto", choices=OC.MODES,
                          help="worker transport (default: auto)")
    patterns.add_argument("--timeout", type=int, default=OC.DEFAULT_TIMEOUT,
                          help=f"worker timeout in seconds "
                               f"(default: {OC.DEFAULT_TIMEOUT})")
    patterns.add_argument("--json", action="store_true",
                          help="emit the result as sorted JSON")

    state = sub.add_parser(
        "status", help="show the installer's resolved Ocelot status")
    state.add_argument("--root", default=None,
                       help="repository whose sibling checkout is considered")
    state.add_argument("--dest", default=None,
                       help="install root to search (default: user data dir)")
    state.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "cluster":
            if args.timeout < 1:
                print("error: --timeout must be a positive integer",
                      file=sys.stderr)
                return 2
            findings = _load_findings(args.findings)
            result = OC.cluster_findings(
                findings, root=args.root, ocelot_root=args.ocelot_root,
                mode=args.mode, timeout=args.timeout)
            if args.json:
                _print_json(result)
            else:
                print(_format_cluster(result), end="")
            return 0
        if args.command == "impact":
            if args.timeout < 1:
                print("error: --timeout must be a positive integer",
                      file=sys.stderr)
                return 2
            if not args.diff and not args.changed:
                print("error: impact needs --diff BASE..HEAD or --changed PATH",
                      file=sys.stderr)
                return 2
            shapes = [
                OC.change_shape(
                    args.root, diff=spec, graph_path=args.graph,
                    with_test_plan=args.test_plan)
                for spec in (args.diff or [])
            ]
            if args.changed:
                shapes.append(OC.change_shape(
                    args.root, changed=args.changed, graph_path=args.graph,
                    with_test_plan=args.test_plan))
            result = OC.test_impact_clusters(
                shapes, ocelot_root=args.ocelot_root, mode=args.mode,
                timeout=args.timeout, root=args.root)
            if args.json:
                _print_json(result)
            else:
                print(_format_impact(result), end="")
            return 0
        if args.command == "patterns":
            if args.timeout < 1:
                print("error: --timeout must be a positive integer",
                      file=sys.stderr)
                return 2
            root = args.root
            items: list[dict[str, Any]] | None = None
            if args.report:
                items, recorded_root = _load_report(args.report)
                root = root or recorded_root
            result = OC.architecture_patterns(
                items, root=root, graph_path=args.graph,
                ocelot_root=args.ocelot_root, mode=args.mode,
                timeout=args.timeout)
            if args.json:
                _print_json(result)
            else:
                print(_format_patterns(result), end="")
            return 0
        if args.command == "status":
            result = ocelot_installer.status(args.root, dest=args.dest)
            if args.json:
                _print_json(result)
            else:
                print(_format_status(result), end="")
            return 0
    except (OC.OcelotCodeError, ocelot_installer.OcelotError,
            OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"error: unknown command {args.command!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
