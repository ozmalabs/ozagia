"""Render and persist application-profile reports."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping

from .analyze import MAX_TOP, SORTS, analyze, compare_analyses
from .graph import GraphIndex
from .ingest import FORMATS, WEIGHT_UNITS, ingest_file
from .model import (
    REPORT_NAMESPACE, VERSION, canonical_json, sha256_text,
)

def duration(value: Any, unit: str) -> str:
    try:
        number = int(value or 0)
    except (TypeError, ValueError):
        number = 0
    if unit == "samples":
        return f"{number:,} samples"
    if unit == "calls":
        return f"{number:,} calls"
    if number < 1_000:
        return f"{number} us"
    if number < 1_000_000:
        return f"{number / 1_000:.2f} ms"
    return f"{number / 1_000_000:.2f} s"


def percent(bp: Any) -> str:
    try:
        return f"{int(bp or 0) / 100:.1f}%"
    except (TypeError, ValueError):
        return "0.0%"


def _cell(value: Any, *, limit: int = 160) -> str:
    text = str(value if value not in (None, "") else "-")
    text = text.replace("|", "\\|").replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 1] + "\u2026"


def _frame_label(row: Mapping[str, Any]) -> str:
    name = row.get("name") or "(unknown)"
    file = row.get("file")
    line = row.get("line")
    if file:
        return f"{name} ({file}{':' + str(line) if line else ''})"
    return str(name)


def _status(row: Mapping[str, Any]) -> str:
    graph = row.get("graph") or {}
    status = str(graph.get("status") or "unknown")
    if status == "mapped" and graph.get("symbol"):
        return str(graph["symbol"])
    return f"[{status}]"


def report_hash(report: Mapping[str, Any]) -> str:
    value = {key: item for key, item in report.items() if key != "report_hash"}
    return sha256_text(canonical_json(value))


def build_report_from_profile(profile: Mapping[str, Any], root: str | Path, *,
                              graph: GraphIndex | None = None,
                              top: int = 25, sort: str = "self",
                              baseline: Mapping[str, Any] | None = None,
                              include_profile: bool = False,
                              collection: Mapping[str, Any] | None = None
                              ) -> dict[str, Any]:
    root = Path(root).resolve()
    try:
        top = min(max(int(top), 1), MAX_TOP)
    except (TypeError, ValueError):
        top = 25
    # A baseline comparison needs the wider row set even when the rendered
    # hotspot table is short; trim the table after the comparison is built.
    analysis = analyze(profile, graph, top=max(top, 200) if baseline else top,
                       sort=sort, baseline=baseline)
    if baseline:
        analysis["hotspots"] = analysis["hotspots"][:top]
    report = {
        "schema": {"namespace": REPORT_NAMESPACE, "version": VERSION},
        "source": dict(profile.get("source") or {}),
        "scope": dict(profile.get("scope") or {"root": str(root)}),
        "profile_hash": profile.get("profile_hash"),
        "clock": profile.get("clock") or "unknown",
        "measurement": analysis["measurement"],
        "coverage": analysis["coverage"],
        "hotspots": analysis["hotspots"],
        "files": analysis["files"],
        "modules": analysis["modules"],
        "paths": analysis["paths"],
        "telemetry": analysis["telemetry"],
        "metrics": analysis["metrics"],
        "baseline": analysis["baseline"],
        "callouts": analysis["callouts"],
        "graph": analysis["graph"],
        "sort": analysis["sort"],
        "omissions": analysis["omissions"],
        "warnings": analysis["warnings"],
        "not_assessed": [
            "causality: a hotspot is a measured cost, not proof of a defect",
            "wall-clock savings: sampled/CPU/profiler overhead is not a task estimate",
            "unmapped code: frames outside the tag corpus are not guessed",
            "concurrency and scaling: no load sweep is synthesized from one artifact",
            "metrics: values are the artifact's aggregations; no causal or load interpretation",
        ],
    }
    if collection is not None:
        report["collection"] = dict(collection)
    if include_profile:
        report["profile"] = dict(profile)
    report["report_hash"] = report_hash(report)
    return report


def build_report(root: str | Path, path: str | Path, *,
                 format: str = "auto", interval_us: int | None = None,
                 weight_unit: str = "auto", top: int = 25,
                 sort: str = "self", use_graph: bool = True,
                 refresh: bool = False,
                 baseline: Mapping[str, Any] | None = None,
                 include_profile: bool = False) -> dict[str, Any]:
    """Ingest one artifact and build a bounded profile report."""
    root = Path(root).resolve()
    profile = ingest_file(path, root=root, format=format,
                          interval_us=interval_us, weight_unit=weight_unit)
    graph = GraphIndex.open(root, refresh=refresh) if use_graph else None
    return build_report_from_profile(
        profile, root, graph=graph, top=top, sort=sort, baseline=baseline,
        include_profile=include_profile)


def report_json(report: Mapping[str, Any], *, pretty: bool = True) -> str:
    if pretty:
        return json.dumps(report, sort_keys=True, indent=2,
                          ensure_ascii=False) + "\n"
    return canonical_json(report) + "\n"


def format_report(report: Mapping[str, Any]) -> str:
    measurement = report.get("measurement") or {}
    unit = str(measurement.get("unit") or "unknown")
    source = report.get("source") or {}
    scope = report.get("scope") or {}
    graph = report.get("graph") or {}
    lines = ["# Application profile", ""]
    lines.append(f"- **Source:** {_cell(source.get('tool'))} "
                 f"(`{_cell(source.get('kind'))}`)")
    lines.append(f"- **Artifact:** `{_cell(source.get('path'))}`")
    if source.get("sha256"):
        lines.append(f"- **SHA-256:** `{str(source['sha256'])[:16]}\u2026`")
    if report.get("profile_hash"):
        lines.append(f"- **Profile hash:** `{str(report['profile_hash'])[:16]}\u2026`")
    if report.get("report_hash"):
        lines.append(f"- **Report hash:** `{str(report['report_hash'])[:16]}\u2026`")
    lines.append(f"- **Root:** `{_cell(scope.get('root'))}`")
    lines.append(f"- **Clock:** {_cell(measurement.get('clock'))} "
                 f"({_cell(measurement.get('kind'))})")
    collection = report.get("collection")
    if collection:
        status = "timed out" if collection.get("timed_out") else (
            f"exit {collection.get('returncode')}")
        lines.append(f"- **Collection:** {_cell(collection.get('tool'))} "
                     f"({_cell(status)}, {collection.get('duration_s')} s; "
                     f"command `{_cell(collection.get('command_key'))}`)")
        lines.append(f"- **Tool version:** {_cell(collection.get('tool_version'))}")
    lines.append(f"- **Measured self:** {duration(measurement.get('total_self'), unit)}"
                 f" across {measurement.get('frames', 0)} active frame(s)")
    if graph.get("source") == "not_requested":
        graph_line = "- **Graph:** not requested (`--no-graph`)"
    else:
        graph_line = (f"- **Graph:** {_cell(graph.get('source'))}; "
                      f"{graph.get('files', 0)} file(s), "
                      f"{graph.get('definitions', 0)} definition(s)")
        if graph.get("error"):
            graph_line += f"; partial: {_cell(graph['error'])}"
    lines.append(graph_line)
    lines.append("")
    coverage = report.get("coverage") or {}
    lines.append("## Coverage")
    lines.append("")
    lines.append(f"- Frames mapped: {coverage.get('mapped', 0)} "
                 f"({percent(coverage.get('mapped_frames_bp'))}); "
                 f"unresolved {coverage.get('unresolved', 0)}, "
                 f"ambiguous {coverage.get('ambiguous', 0)}, "
                 f"external {coverage.get('external', 0)}, "
                 f"unavailable {coverage.get('unavailable', 0)}, "
                 f"not requested {coverage.get('not_requested', 0)}, "
                 f"not assessed {coverage.get('not_assessed', 0)}.")
    lines.append(f"- Self weight mapped: {percent(coverage.get('mapped_self_bp'))}; "
                 f"unmapped: {percent(coverage.get('unmapped_self_bp'))}.")
    if report.get("callouts"):
        lines.append("")
        lines.append("## Callouts")
        lines.append("")
        for callout in report["callouts"]:
            lines.append(f"- {_cell(callout, limit=300)}")
    lines.append("")
    lines.append("## Hotspots")
    lines.append("")
    lines.append("| # | Frame | Graph symbol / status | Self | Inclusive | Calls / hits | Callers | Impact |")
    lines.append("|---|---|---|---:|---:|---:|---:|---:|")
    for index, row in enumerate(report.get("hotspots") or [], 1):
        calls = row.get("calls")
        if calls is None:
            calls = row.get("hits")
        graph_row = row.get("graph") or {}
        impact = graph_row.get("impact_symbols")
        impact_text = (f"{impact} sym / {len(graph_row.get('impact_files') or [])} files"
                       if impact is not None else "-")
        lines.append(
            f"| {index} | {_cell(_frame_label(row))} | {_cell(_status(row))} "
            f"| {duration(row.get('self'), unit)} "
            f"| {duration(row.get('inclusive'), unit)} "
            f"| {calls if calls is not None else '-'} "
            f"| {graph_row.get('callers', '-')} | {impact_text} |")
    if not report.get("hotspots"):
        lines.append("| - | No active frame was found in the artifact. | - | - | - | - | - | - |")
    lines.append("")
    paths = report.get("paths") or []
    if paths:
        lines.append("## Hot paths")
        lines.append("")
        for index, path in enumerate(paths, 1):
            stack = " \u2192 ".join(path.get("stack") or [])
            omitted = path.get("omitted_frames") or 0
            suffix = f" (+{omitted} outer frame(s))" if omitted else ""
            lines.append(f"{index}. {duration(path.get('weight'), path.get('unit') or unit)} "
                         f"\u2014 {_cell(stack, limit=500)}{suffix}")
        lines.append("")
    for title, key in (("Files", "files"), ("Modules", "modules")):
        rows = report.get(key) or []
        if not rows:
            continue
        lines.append(f"## {title}")
        lines.append("")
        lines.append("| Name | Self | Inclusive | Frames | Mapped | Share |")
        lines.append("|---|---:|---:|---:|---:|---:|")
        for row in rows:
            name = row.get("file") if key == "files" else row.get("module")
            mapped = ("-" if graph.get("source") == "not_requested"
                      else row.get("mapped", 0))
            lines.append(
                f"| {_cell(name)} | {duration(row.get('self'), unit)} "
                f"| {duration(row.get('inclusive'), unit)} "
                f"| {row.get('frames', 0)} | {mapped} "
                f"| {percent(row.get('self_share_bp'))} |")
        lines.append("")
    telemetry = report.get("telemetry")
    if telemetry:
        lines.append("## Telemetry")
        lines.append("")
        lines.append(f"{telemetry.get('spans', 0)} span(s) across "
                     f"{len(telemetry.get('services') or [])} service(s); "
                     f"status {telemetry.get('status')}; "
                     f"errors {telemetry.get('error_spans', 0)}.")
        lines.append("")
        lines.append("| Service | Operation | Count | Total | p50 | p95 | Errors | Frame |")
        lines.append("|---|---|---:|---:|---:|---:|---:|---|")
        for row in telemetry.get("operations") or []:
            lines.append(
                f"| {_cell(row.get('service'))} | {_cell(row.get('operation'))} "
                f"| {row.get('count', 0)} "
                f"| {duration(row.get('total_us'), 'microseconds')} "
                f"| {duration(row.get('p50_us'), 'microseconds')} "
                f"| {duration(row.get('p95_us'), 'microseconds')} "
                f"| {row.get('errors', 0)} "
                f"| {_cell(row.get('symbol') or '(unmapped)')} |")
        if telemetry.get("operations_truncated"):
            lines.append("")
            lines.append("_Operation list truncated._")
        lines.append("")
    metrics = report.get("metrics")
    if metrics:
        lines.append("## Metrics")
        lines.append("")
        lines.append(f"{metrics.get('series_count', 0)} series / "
                     f"{metrics.get('points', 0)} point(s); "
                     f"{metrics.get('mapped', 0)} map to a code location; "
                     f"kinds {metrics.get('by_kind')}.")
        lines.append("")
        lines.append("| Name | Kind | Service | Value | p50 | p95 | p99 | Frame |")
        lines.append("|---|---|---|---:|---:|---:|---:|---|")
        for row in metrics.get("series") or []:
            value = row.get("value")
            if value is None and row.get("count") is not None:
                value = f"count {row.get('count')}"
            lines.append(
                f"| {_cell(row.get('name'))} | {_cell(row.get('kind'))} "
                f"| {_cell(row.get('service'))} | {_cell(value)} "
                f"| {_cell(row.get('p50'))} | {_cell(row.get('p95'))} "
                f"| {_cell(row.get('p99'))} "
                f"| {_cell((row.get('graph') or {}).get('symbol') or '(unmapped)')} |")
        if metrics.get("histogram_quantiles"):
            lines.append("")
            lines.append(f"_{metrics['histogram_quantiles']}._")
        lines.append("")
    baseline = report.get("baseline")
    if baseline:
        lines.append("## Baseline comparison")
        lines.append("")
        if not baseline.get("compatible"):
            lines.append(f"Incompatible: {_cell(baseline.get('reason'))}")
        else:
            lines.append(f"Total self delta: "
                         f"{duration(baseline.get('self_delta'), unit)}; "
                         f"{baseline.get('matched', 0)} matched frame(s), "
                         f"{baseline.get('base_only', 0)} base-only, "
                         f"{baseline.get('current_only', 0)} current-only.")
            for title, rows in (("Regressions", baseline.get("regressions") or []),
                                ("Improvements", baseline.get("improvements") or [])):
                if not rows:
                    continue
                lines.append("")
                lines.append(f"### {title}")
                lines.append("")
                for row in rows:
                    lines.append(f"- {_cell(row.get('symbol') or row.get('key'))}: "
                                 f"{duration(row.get('delta_self'), unit)}")
        lines.append("")
    lines.append("## Unknowns and omissions")
    lines.append("")
    omissions = report.get("omissions") or {}
    if omissions.get("truncated"):
        lines.append(f"- Input was bounded: {omissions.get('omitted', 0)} item(s) "
                     f"omitted ({', '.join(omissions.get('reasons') or [])}).")
    for warning in report.get("warnings") or []:
        lines.append(f"- {_cell(warning, limit=300)}")
    for item in report.get("not_assessed") or []:
        lines.append(f"- Not assessed: {_cell(item, limit=300)}")
    lines.append("")
    return "\n".join(lines)


def _atomic_write(path: Path, text: str) -> None:
    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp",
                                dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        try:
            os.unlink(temp)
        except OSError:
            pass


def write_report(report: Mapping[str, Any], out: str | Path) -> dict[str, str]:
    out_dir = Path(out)
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "profile.json"
    md_path = out_dir / "profile.md"
    _atomic_write(json_path, report_json(report))
    _atomic_write(md_path, format_report(report) + "\n")
    return {"json": str(json_path), "markdown": str(md_path)}


def compare_reports(base: Mapping[str, Any], current: Mapping[str, Any], *,
                    top: int = 10) -> dict[str, Any]:
    return compare_analyses(base, current, top=top)


__all__ = [
    "FORMATS", "SORTS", "WEIGHT_UNITS", "build_report",
    "build_report_from_profile", "compare_reports", "duration", "format_report",
    "percent", "report_hash", "report_json", "write_report",
]
