"""Measured profile reports as deep-review evidence.

This adapter reads reports produced by :mod:`ozagia.core.profile`. It does not
run a profiler, start the target or infer a cause. A hotspot is tagged
``evidence_kind: observed`` but remains a measured cost, not a verified defect.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Mapping

from ozagia.core import profile as P
from ozagia.core import security

MAX_REPORTS = 8
MAX_REPORT_BYTES = 64 * 1024 * 1024
MAX_FINDINGS_PER_REPORT = 20
MAX_METRICS = 20


def _location(row: Mapping[str, Any]) -> str:
    file = row.get("file")
    line = row.get("line")
    if file:
        return f"{file}:{line}" if line else str(file)
    return str(row.get("name") or "(unknown)")


def _measurement(value: Any, unit: str) -> str:
    try:
        number = int(value or 0)
    except (TypeError, ValueError):
        number = 0
    if unit == "samples":
        return f"{number:,} samples"
    if unit == "microseconds":
        if number >= 1_000_000:
            return f"{number / 1_000_000:.2f} s"
        if number >= 1_000:
            return f"{number / 1_000:.2f} ms"
        return f"{number} us"
    return f"{number:,}"


def _report_path(root: Path, value: str | Path) -> Path:
    path = Path(value)
    if not path.is_absolute():
        candidate = root / path
        if candidate.is_file():
            return candidate
    return path


def _load_report(root: Path, path: Path) -> dict[str, Any]:
    try:
        # A supplied report is untrusted input. Bound the read itself instead
        # of relying on stat(), which can be stale or report zero for special
        # files such as /proc entries.
        with path.open("rb") as stream:
            raw = stream.read(MAX_REPORT_BYTES + 1)
        if len(raw) > MAX_REPORT_BYTES:
            raise ValueError(
                f"profile report exceeds {MAX_REPORT_BYTES} byte limit")
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        data = None
    if (isinstance(data, Mapping) and isinstance(data.get("schema"), Mapping)
            and data["schema"].get("namespace") == P.REPORT_NAMESPACE):
        return dict(data)
    # An artifact path is accepted as a convenience; the report is generated
    # with the read-only ingest path. Graph mapping would create or refresh
    # `.ozagia/tags.json`, which a review reading an artifact must not do.
    return P.build_report(root, path, top=25, use_graph=False, refresh=False)


def _redact(value: Any) -> str:
    try:
        return security.redact_text(str(value))
    except Exception:  # pragma: no cover - redaction must never break review
        return str(value)


def _hotspot_findings(report: Mapping[str, Any], source: str,
                      report_hash: str | None) -> list[dict[str, Any]]:
    measurement = report.get("measurement") or {}
    unit = str(measurement.get("unit") or "unknown")
    findings: list[dict[str, Any]] = []
    for row in (report.get("hotspots") or [])[:MAX_FINDINGS_PER_REPORT]:
        if not isinstance(row, Mapping):
            continue
        graph = row.get("graph") if isinstance(row.get("graph"), Mapping) else {}
        symbol = graph.get("symbol")
        identity = str(symbol or row.get("key") or row.get("name") or "hotspot")
        share = int(row.get("share_bp") or 0)
        severity = "minor" if share >= 2_000 else "note"
        self_text = _measurement(row.get("self"), unit)
        inclusive_text = _measurement(row.get("inclusive"), unit)
        location = _location(row)
        summary = (f"Measured hotspot {identity} at {location}: self {self_text}, "
                   f"inclusive {inclusive_text}, {share / 100:.1f}% of self weight.")
        findings.append({
            "section": "performance",
            "type": "measured_hotspot",
            "severity": severity,
            "confidence": "high",
            "evidence_kind": "observed",
            "stable_key": f"profile:hotspot:{identity}",
            "keys": [f"profile:hotspot:{identity}"],
            "file": row.get("file"),
            "line": row.get("line"),
            "summary": _redact(summary),
            "recommendation": _redact(
                "Inspect the mapped symbol and verify the measured hotspot with a "
                "targeted benchmark before changing code."),
            "profile_evidence": {
                "report": source,
                "report_hash": report_hash,
                "profile_hash": report.get("profile_hash"),
                "unit": unit,
                "clock": report.get("clock"),
                "self": row.get("self"),
                "inclusive": row.get("inclusive"),
                "calls": row.get("calls"),
                "hits": row.get("hits"),
                "share_bp": share,
                "graph": dict(graph),
            },
        })
    return findings


def _regression_findings(report: Mapping[str, Any], source: str,
                         report_hash: str | None) -> list[dict[str, Any]]:
    baseline = report.get("baseline")
    if not isinstance(baseline, Mapping) or not baseline.get("compatible"):
        return []
    unit = str((report.get("measurement") or {}).get("unit") or "unknown")
    findings = []
    for row in (baseline.get("regressions") or [])[:MAX_FINDINGS_PER_REPORT]:
        if not isinstance(row, Mapping):
            continue
        identity = str(row.get("symbol") or row.get("key") or row.get("name"))
        delta = int(row.get("delta_self") or 0)
        if delta <= 0:
            continue
        summary = (f"Measured regression in {identity}: self time increased by "
                   f"{_measurement(delta, unit)} between the baseline and current "
                   f"profile.")
        findings.append({
            "section": "performance",
            "type": "measured_regression",
            "severity": "minor",
            "confidence": "high",
            "evidence_kind": "observed",
            "stable_key": f"profile:regression:{identity}",
            "keys": [f"profile:regression:{identity}"],
            "file": row.get("file"),
            "summary": _redact(summary),
            "recommendation": _redact(
                "Confirm the regression with an independent run and a targeted "
                "benchmark before changing the hot path."),
            "profile_evidence": {
                "report": source,
                "report_hash": report_hash,
                "unit": unit,
                "delta_self": delta,
                "base_self": row.get("base_self"),
                "current_self": row.get("current_self"),
            },
        })
    return findings


def _telemetry_findings(report: Mapping[str, Any], source: str,
                        report_hash: str | None) -> list[dict[str, Any]]:
    telemetry = report.get("telemetry")
    if not isinstance(telemetry, Mapping):
        return []
    findings = []
    for row in (telemetry.get("operations") or [])[:MAX_FINDINGS_PER_REPORT]:
        if not isinstance(row, Mapping):
            continue
        errors = int(row.get("errors") or 0)
        if errors <= 0:
            continue
        identity = f"{row.get('service') or 'unknown'}:{row.get('operation') or 'operation'}"
        summary = (f"Telemetry operation {identity} recorded {errors} error span(s) "
                   f"across {row.get('count', 0)} span(s).")
        findings.append({
            "section": "failure-modes",
            "type": "measured_telemetry_error",
            "severity": "minor",
            "confidence": "high",
            "evidence_kind": "observed",
            "stable_key": f"profile:telemetry-error:{identity}",
            "keys": [f"profile:telemetry-error:{identity}"],
            "summary": _redact(summary),
            "recommendation": _redact(
                "Check the operation's error path and correlate the failing spans "
                "with logs before changing behavior."),
            "profile_evidence": {
                "report": source,
                "report_hash": report_hash,
                "service": row.get("service"),
                "operation": row.get("operation"),
                "count": row.get("count"),
                "errors": errors,
                "p95_us": row.get("p95_us"),
            },
        })
    return findings


def analyze(root: str | Path, paths: list[str] | None, *,
            budget_s: float = 60) -> dict[str, Any]:
    """Read bounded profile reports and return measured findings and coverage."""
    root = Path(root).resolve()
    deadline = time.monotonic() + max(0.0, float(budget_s))
    findings: list[dict[str, Any]] = []
    loaded: list[str] = []
    report_hashes: list[str] = []
    not_checked: list[dict[str, str]] = []
    metrics: dict[str, Any] | None = None
    if paths and len(paths) > MAX_REPORTS:
        not_checked.append({"reason":
                            f"profile report limit reached ({MAX_REPORTS})"})
    for raw in (paths or [])[:MAX_REPORTS]:
        if time.monotonic() >= deadline:
            not_checked.append({"reason": "time budget exhausted before all profile reports were read"})
            break
        path = _report_path(root, raw)
        try:
            report = _load_report(root, path)
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            not_checked.append({"path": str(path), "reason": f"profile report unavailable: {exc}"})
            continue
        if (not isinstance(report.get("schema"), Mapping)
                or report["schema"].get("namespace") != P.REPORT_NAMESPACE):
            not_checked.append({"path": str(path), "reason": "not an ozagia profile report"})
            continue
        source = str(path)
        report_hash = report.get("report_hash")
        if isinstance(report_hash, str) and report_hash:
            report_hashes.append(report_hash)
        loaded.append(source)
        findings.extend(_hotspot_findings(report, source, report_hash))
        findings.extend(_regression_findings(report, source, report_hash))
        findings.extend(_telemetry_findings(report, source, report_hash))
        report_metrics = report.get("metrics")
        if isinstance(report_metrics, Mapping):
            summary = {
                "series": report_metrics.get("series_count", 0),
                "points": report_metrics.get("points", 0),
                "kinds": report_metrics.get("by_kind") or {},
                "services": (report_metrics.get("services") or [])[:20],
                "mapped": report_metrics.get("mapped", 0),
            }
            if metrics is None:
                metrics = summary
            else:
                metrics["series"] += summary["series"]
                metrics["points"] += summary["points"]
                metrics["mapped"] += summary["mapped"]
                for key, value in summary["kinds"].items():
                    metrics["kinds"][key] = metrics["kinds"].get(key, 0) + value
                metrics["services"] = sorted({*metrics["services"],
                                              *summary["services"]})[:20]
    sections: dict[str, Any] = {}
    if loaded:
        sections["performance"] = {
            "checked": ["measured_profile_reports"],
            "not_checked": not_checked,
            "profile_reports": loaded[:MAX_REPORTS],
            "profile_report_hashes": report_hashes[:MAX_REPORTS],
            "profile_metrics": metrics,
            "scope": ("Measured hotspots and regressions from supplied profile "
                      "reports, plus selected static Python performance patterns."),
        }
        if metrics is not None:
            sections["scalability"] = {
                "checked": ["measured_profile_metrics"],
                "not_checked": not_checked,
                "profile_reports": loaded[:MAX_REPORTS],
                "profile_report_hashes": report_hashes[:MAX_REPORTS],
                "profile_metrics": metrics,
                "scope": ("Metric and span inventory from supplied profile reports; "
                          "no load sweep or causal scaling model."),
            }
        if any(item.get("section") == "failure-modes" for item in findings):
            sections["failure-modes"] = {
                "checked": ["measured_telemetry_errors"],
                "not_checked": not_checked,
                "profile_reports": loaded[:MAX_REPORTS],
                "profile_report_hashes": report_hashes[:MAX_REPORTS],
                "scope": ("Telemetry error spans from supplied profile reports; "
                          "absence of an error span is not proof of reliability."),
            }
    return {"findings": findings, "sections": sections,
            "checked": loaded[:MAX_REPORTS], "not_checked": not_checked,
            "metrics": metrics,
            "profile_report_hashes": report_hashes[:MAX_REPORTS]}


__all__ = ["MAX_REPORTS", "analyze"]
