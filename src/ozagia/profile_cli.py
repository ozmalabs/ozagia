"""``python -m ozagia.profile_cli``: ingest profiler/telemetry artifacts.

Read-only over the target repository: the command reads one artifact, joins
active frames to the tag cache and writes a report only when ``--out`` is
given. A missing tag cache is built under the state directory. It does not
import the target, start a profiler, call a model or use the network.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ozagia.config import repo_root, state_path
from ozagia.core import profile as P
from ozagia.core.profile import analyze, build_report_from_profile
from ozagia.core.profile.graph import GraphIndex
from ozagia.core.profile.ingest import ingest_file

_FORMAT_HELP = {
    "cprofile": "Python cProfile/pstats dump (.prof, .pstats)",
    "speedscope": "Speedscope sampled/evented JSON (py-spy, pyinstrument export)",
    "chrome_cpuprofile": "Chrome/Node CPU profile JSON (.cpuprofile)",
    "collapsed": "folded/collapsed stack text (perf, py-spy raw, stackcollapse)",
    "otlp_json": "OpenTelemetry OTLP JSON traces",
    "otlp_metrics": "OpenTelemetry OTLP JSON metrics",
    "prometheus_text": "Prometheus/OpenMetrics text exposition",
}


def _root(value: str | None) -> Path:
    return Path(value).resolve() if value else repo_root()


def _print_json(value: object) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, default=str))


def build_parser(prog: str = "ozagia-profile") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog,
        description="Join profiler/telemetry artifacts to the repository graph")
    sub = parser.add_subparsers(dest="command", required=True)

    ingest = sub.add_parser(
        "ingest", help="read one artifact and report hotspots",
        description="Read one profiler or OTLP JSON artifact; never runs it.")
    ingest.add_argument("path", help="profile artifact")
    ingest.add_argument("--root", default=None, help="repository root")
    ingest.add_argument("--format", choices=P.FORMATS, default="auto",
                        help="artifact format (default: detect from content)")
    ingest.add_argument("--weight-unit", choices=P.WEIGHT_UNITS, default="auto",
                        help="collapsed-stack weight unit")
    ingest.add_argument("--interval-us", type=int, default=None,
                        help="microseconds per sample when the artifact uses counts")
    ingest.add_argument("--baseline", default=None,
                        help="earlier artifact to compare against")
    ingest.add_argument("--top", type=int, default=25,
                        help="hotspots to report (1..200; default 25)")
    ingest.add_argument("--sort", choices=P.SORTS, default="self",
                        help="hotspot order (default: self)")
    ingest.add_argument("--no-graph", action="store_true",
                        help="skip the tag-cache/symbol-graph join")
    ingest.add_argument("--refresh", action="store_true",
                        help="refresh the tag cache before joining")
    ingest.add_argument("--include-profile", action="store_true",
                        help="include the normalized profile in JSON output")
    ingest.add_argument("--out", default=None,
                        help="write profile.json and profile.md to this directory")
    ingest.add_argument("--json", action="store_true", help="emit JSON")

    run = sub.add_parser(
        "run", help="run an explicitly allowed profiler command",
        description=("Execute one caller-supplied profiler command under the "
                     "resource policy, then ingest its artifact. Requires "
                     "--allow-exec."))
    run.add_argument("--root", default=None, help="repository root")
    run.add_argument("--tool", choices=P.TOOLS, required=True,
                     help="cprofile, py-spy or node")
    run.add_argument("--allow-exec", action="store_true",
                     help="approve executing the command")
    run.add_argument("--out", default=None,
                     help="artifact/report directory (default: state profiles/run-<stamp>)")
    run.add_argument("--timeout-s", type=int, default=60,
                     help="wall-clock timeout in seconds (1..3600)")
    run.add_argument("--memory-mb", type=int, default=None,
                     help="override the policy memory limit")
    run.add_argument("--disk-mb", type=int, default=None,
                     help="override the policy disk admission estimate")
    run.add_argument("--cwd", default=None,
                     help="working directory inside the repository")
    run.add_argument("--tool-path", default=None,
                     help="explicit profiler/interpreter path")
    run.add_argument("--rate", type=int, default=None,
                     help="py-spy sampling rate in Hz")
    run.add_argument("--format", choices=P.FORMATS, default="auto",
                     help="artifact format for the collected output")
    run.add_argument("--top", type=int, default=25,
                     help="hotspots to report (1..200)")
    run.add_argument("--sort", choices=P.SORTS, default="self")
    run.add_argument("--no-graph", action="store_true")
    run.add_argument("--refresh", action="store_true")
    run.add_argument("--include-profile", action="store_true")
    run.add_argument("--json", action="store_true", help="emit JSON")
    run.add_argument("profiler_command", nargs=argparse.REMAINDER,
                     help="profiler command after --")

    compare = sub.add_parser(
        "compare", help="compare two profile artifacts",
        description="Ingest two artifacts and report measured self-time changes.")
    compare.add_argument("baseline", help="earlier artifact")
    compare.add_argument("current", help="later artifact")
    compare.add_argument("--root", default=None, help="repository root")
    compare.add_argument("--format", choices=P.FORMATS, default="auto")
    compare.add_argument("--weight-unit", choices=P.WEIGHT_UNITS, default="auto")
    compare.add_argument("--interval-us", type=int, default=None)
    compare.add_argument("--top", type=int, default=10,
                         help="regressions/improvements to show (default 10)")
    compare.add_argument("--no-graph", action="store_true")
    compare.add_argument("--refresh", action="store_true")
    compare.add_argument("--json", action="store_true")

    formats = sub.add_parser("formats", help="list supported artifacts")
    formats.add_argument("--json", action="store_true")
    return parser


def _ingest_options(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "format": args.format,
        "interval_us": args.interval_us,
        "weight_unit": args.weight_unit,
    }


def _graph(args: argparse.Namespace, root: Path) -> GraphIndex | None:
    if args.no_graph:
        return None
    return GraphIndex.open(root, refresh=args.refresh)


def _cmd_ingest(args: argparse.Namespace, root: Path) -> int:
    options = _ingest_options(args)
    graph = _graph(args, root)
    profile = ingest_file(args.path, root=root, **options)
    baseline = None
    if args.baseline:
        base_profile = ingest_file(args.baseline, root=root, **options)
        baseline = analyze(base_profile, graph,
                           top=min(200, max(int(args.top), 50)), sort="self")
    report = build_report_from_profile(
        profile, root, graph=graph, top=args.top, sort=args.sort,
        baseline=baseline, include_profile=args.include_profile)
    if args.out:
        paths = P.write_report(report, args.out)
        if not args.json:
            print(f"wrote {paths['json']}")
            print(f"wrote {paths['markdown']}")
            return 0
    if args.json:
        print(P.report_json(report), end="")
    else:
        print(P.format_report(report))
    return 0


def _cmd_run(args: argparse.Namespace, root: Path) -> int:
    command = list(args.profiler_command or [])
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        print("error: a command is required after --", file=sys.stderr)
        return 2
    out_dir = (Path(args.out) if args.out else
               state_path(root, "profiles",
                          "run-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")))
    profile, run_record = P.run_collection(
        root, tool=args.tool, command=command, out_dir=out_dir,
        allow_exec=args.allow_exec, timeout_s=args.timeout_s,
        memory_mb=args.memory_mb, disk_mb=args.disk_mb, cwd=args.cwd,
        tool_path=args.tool_path, rate=args.rate, format=args.format)
    report = build_report_from_profile(
        profile, root, graph=_graph(args, root), top=args.top, sort=args.sort,
        include_profile=args.include_profile, collection=run_record)
    paths = P.write_report(report, out_dir)
    if args.json:
        print(P.report_json(report), end="")
    else:
        print(f"wrote {paths['json']}")
        print(f"wrote {paths['markdown']}")
        print(P.format_report(report))
    failed = bool(run_record.get("timed_out")) or run_record.get("returncode") not in (0, None)
    return 1 if failed else 0


def _format_comparison(value: dict[str, Any]) -> str:
    lines = ["# Profile comparison", ""]
    if not value.get("compatible"):
        lines.append(f"Incompatible: {value.get('reason')}")
        return "\n".join(lines)
    lines.append(f"Unit: {value.get('unit')}; total self delta: "
                 f"{value.get('self_delta')}.")
    lines.append(f"Matched {value.get('matched', 0)} frame(s), "
                 f"{value.get('base_only', 0)} base-only, "
                 f"{value.get('current_only', 0)} current-only.")
    for title, key in (("Regressions", "regressions"),
                       ("Improvements", "improvements")):
        rows = value.get(key) or []
        lines.append("")
        lines.append(f"## {title}")
        lines.append("")
        if not rows:
            lines.append("None in the compared rows.")
            continue
        for row in rows:
            lines.append(f"- {row.get('symbol') or row.get('key')}: "
                         f"{row.get('delta_self')} {value.get('unit')}")
    lines.append("")
    return "\n".join(lines)


def _cmd_compare(args: argparse.Namespace, root: Path) -> int:
    options = _ingest_options(args)
    graph = _graph(args, root)
    base_profile = ingest_file(args.baseline, root=root, **options)
    current_profile = ingest_file(args.current, root=root, **options)
    base_report = build_report_from_profile(
        base_profile, root, graph=graph, top=200, sort="self")
    current_report = build_report_from_profile(
        current_profile, root, graph=graph, top=200, sort="self")
    result = P.compare_reports(base_report, current_report, top=args.top)
    if args.json:
        _print_json(result)
    else:
        print(_format_comparison(result))
    return 0


def _cmd_formats(args: argparse.Namespace) -> int:
    if args.json:
        _print_json({"formats": [
            {"id": name, "description": text}
            for name, text in _FORMAT_HELP.items()]})
        return 0
    print("Supported profile artifacts:")
    for name, text in _FORMAT_HELP.items():
        print(f"  {name:<18} {text}")
    print()
    print("Examples:")
    print("  python -m cProfile -o app.prof app.py")
    print("  py-spy record -f speedscope -o app.speedscope -- python app.py")
    print("  node --cpu-prof app.js")
    print("  perf script | stackcollapse-perf.pl > app.folded")
    print("  otel-collector JSON exporter or `otel-cli` trace/metrics export")
    print("  curl -s http://localhost:9090/metrics > app.prom")
    print()
    print("Guided collection (requires --allow-exec):")
    print("  ozagia profile run --tool cprofile -- python app.py")
    print("  ozagia profile run --tool pyspy -- python app.py")
    print("  ozagia profile run --tool node -- node app.js")
    return 0


def main(argv: list[str] | None = None, *,
         prog: str = "ozagia-profile") -> int:
    args = build_parser(prog).parse_args(argv)
    if args.command == "formats":
        return _cmd_formats(args)
    try:
        root = _root(getattr(args, "root", None))
        if args.command == "ingest":
            return _cmd_ingest(args, root)
        if args.command == "run":
            return _cmd_run(args, root)
        return _cmd_compare(args, root)
    except P.CollectionRefused as exc:
        if getattr(args, "json", False):
            _print_json({"ok": False, "refused": True, "error": str(exc),
                         "admission": exc.report})
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 2
    except (P.ProfileError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
