"""Offline end-to-end deep-review fixture run, label scoring and comparison.

The fixture is copied to a temporary Git checkout. Reports and all state stay
inside that temporary directory; this evaluator never indexes, runs tests, or
invokes a model.  A second report is compared with the first by stable finding
identity.  Reviewed ``(path, type, subject)`` labels are scored against the
reported findings; subjects without reviewed labels are reported as unknown
rather than treated as perfect.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
import tracemalloc
from collections import Counter
from pathlib import Path
from typing import Any, Callable

from ozagia.core.deep_review.orchestrator import build_deep_review
from ozagia.core.repo_review import PreflightError
from ozagia.eval.deep_review_labels import (
    expected_findings,
    label_metadata,
    load_labels,
    score_report_findings,
)

DEFAULT_FIXTURE = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "deep_review_service"


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True,
                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                   stderr=subprocess.PIPE, text=True, timeout=20)


def _preflight_failure(exc: PreflightError) -> dict[str, Any]:
    report = exc.report
    return {
        "status": "preflight_refused",
        "reason": str(exc),
        "check_failures": [
            {key: check.get(key) for key in ("id", "level", "message", "shortfall", "suggestions")
             if key in check}
            for check in report.get("checks", []) if check.get("level") == "error"
        ],
    }


def _measure(call: Callable[[], dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Run one report build and retain wall time plus a Python allocation peak."""
    was_tracing = tracemalloc.is_tracing()
    peak_reason = None
    if not was_tracing:
        try:
            tracemalloc.start()
        except (RuntimeError, ValueError) as exc:  # pragma: no cover - stdlib failure
            peak_reason = f"tracemalloc could not start: {exc}"
    if tracemalloc.is_tracing():
        tracemalloc.reset_peak()
    started = time.perf_counter()
    try:
        result = call()
    finally:
        duration = time.perf_counter() - started
        peak = None
        if tracemalloc.is_tracing():
            _current, peak = tracemalloc.get_traced_memory()
            if not was_tracing:
                tracemalloc.stop()
    return result, {"duration_s": duration, "python_peak_bytes": peak,
                    "peak_reason": peak_reason,
                    "was_already_tracing": was_tracing}


def _process_peak_rss() -> dict[str, Any]:
    """Return the process RSS high-water mark when the platform exposes it."""
    try:
        import resource
    except ImportError:  # pragma: no cover - not available on Windows
        return {"bytes": None, "available": False,
                "reason": "resource.getrusage is not available on this platform"}
    try:
        usage = resource.getrusage(resource.RUSAGE_SELF)
        raw = int(usage.ru_maxrss)
    except (OSError, ValueError) as exc:  # pragma: no cover - platform failure
        return {"bytes": None, "available": False,
                "reason": f"resource.getrusage failed: {exc}"}
    # Linux reports KiB; macOS reports bytes.
    value = raw if sys.platform == "darwin" else raw * 1024
    return {
        "bytes": value,
        "available": True,
        "source": "resource.getrusage(RUSAGE_SELF).ru_maxrss",
        "scope": "process lifetime high-water mark; not isolated to one report",
    }


def _report_size(out: Path) -> dict[str, Any]:
    if not out.is_dir():
        return {"bytes": None, "files": 0,
                "reason": "report directory was not written"}
    try:
        files = [path for path in out.rglob("*") if path.is_file()]
        return {"bytes": sum(path.stat().st_size for path in files),
                "files": len(files), "reason": None}
    except OSError as exc:
        return {"bytes": None, "files": 0,
                "reason": f"report directory could not be measured: {exc}"}


def _counts(result: dict[str, Any]) -> dict[str, Any]:
    findings = result.get("findings")
    findings = findings if isinstance(findings, list) else []
    by_subject = Counter(str(item.get("subject") or "unknown")
                         for item in findings if isinstance(item, dict))
    by_severity = Counter(str(item.get("severity") or "unknown")
                          for item in findings if isinstance(item, dict))
    manifest = result.get("manifest")
    manifest = manifest if isinstance(manifest, dict) else {}
    statuses = Counter(str(row.get("status") or "unknown")
                       for row in (manifest.get("subjects") or {}).values()
                       if isinstance(row, dict))
    return {
        "findings": len(findings),
        "by_subject": dict(sorted(by_subject.items())),
        "by_severity": dict(sorted(by_severity.items())),
        "subject_statuses": dict(sorted(statuses.items())),
        "reported_subjects": len(manifest.get("subjects") or {}),
    }


def _score(findings: list[dict[str, Any]], expected: list[dict[str, Any]],
           complete: list[str], report_manifest: dict[str, Any],
           all_subjects: list[str] | None = None) -> dict[str, Any]:
    return score_report_findings(
        findings, expected, complete_subjects=complete,
        report_manifest=report_manifest, all_subjects=all_subjects)


def evaluate(fixture: str | Path = DEFAULT_FIXTURE, *, budget_s: float = 60,
             labels: str | Path | None = None) -> dict[str, Any]:
    """Run full report generation twice, compare identities and score labels."""
    source = Path(fixture).resolve()
    labels_path = Path(labels) if labels is not None else source / "labels.json"
    manifest = load_labels(labels_path)
    expected = expected_findings(manifest, include_report=True)
    complete = list(manifest.get("complete_subjects") or [])
    metadata = label_metadata(manifest, labels_path)

    with tempfile.TemporaryDirectory(prefix="ozagia-deep-review-eval-") as tmp:
        workspace = Path(tmp)
        root = workspace / "repo"
        shutil.copytree(source, root, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        _git(root, "init", "-q")
        _git(root, "add", "--all")
        _git(root, "-c", "user.name=Fixture Evaluation", "-c",
             "user.email=fixture@example.invalid", "commit", "-qm", "fixture")
        reports = [workspace / "report-1", workspace / "report-2"]
        started = time.perf_counter()
        try:
            first, first_measure = _measure(
                lambda: build_deep_review(root, reports[0], budget_s=budget_s))
            second, second_measure = _measure(
                lambda: build_deep_review(
                    root, reports[1], budget_s=budget_s,
                    baseline=reports[0] / "findings.json"))
        except PreflightError as exc:
            failure = _preflight_failure(exc)
            return {
                "schema_version": 2,
                "scope": "full offline report pipeline on a temporary synthetic checkout",
                "fixture": source.name,
                "labels": metadata,
                "environment": failure,
                "comparison": None,
                "metrics": None,
                "duration": {"total_s": time.perf_counter() - started,
                             "single_host_sample": True},
                "limitations": [
                    "Host resource admission can refuse report generation before collectors run.",
                    "No model, network, target import, index refresh, or test execution.",
                    "Synthetic labels and fixture findings are not production accuracy evidence.",
                ],
            }

        first_score = _score(first.get("findings") or [], expected, complete,
                             first.get("manifest") or {},
                             sorted((first["manifest"].get("subjects") or {})))
        second_score = _score(second.get("findings") or [], expected, complete,
                              second.get("manifest") or {},
                              sorted((second["manifest"].get("subjects") or {})))
        unavailable_subjects = sorted(set(first_score["unavailable_subjects"])
                                      | set(second_score["unavailable_subjects"]))
        complete_run = (first["manifest"].get("complete")
                        and second["manifest"].get("complete")
                        and not unavailable_subjects)
        first_ids = {item["id"] for item in first.get("findings") or []
                     if isinstance(item, dict) and item.get("id")}
        second_ids = {item["id"] for item in second.get("findings") or []
                      if isinstance(item, dict) and item.get("id")}
        comparison = second["manifest"].get("comparison") or {}
        first_size = _report_size(reports[0])
        second_size = _report_size(reports[1])
        rss = _process_peak_rss()
        first_counts = _counts(first)
        second_counts = _counts(second)
        total_seconds = time.perf_counter() - started
        return {
            "schema_version": 2,
            "scope": "full offline report pipeline on a temporary synthetic checkout",
            "fixture": source.name,
            "labels": metadata,
            "status": "complete" if complete_run else "partial",
            "reports": 2,
            "first_report": {
                "status": first["manifest"].get("status"),
                "findings": len(first_ids),
                "subjects": {key: row.get("status") for key, row in
                             first["manifest"].get("subjects", {}).items()},
                "duration_s": first_measure["duration_s"],
                "python_peak_bytes": first_measure["python_peak_bytes"],
                "report_bytes": first_size["bytes"],
                "report_files": first_size["files"],
                "counts": first_counts,
            },
            "second_report": {
                "status": second["manifest"].get("status"),
                "findings": len(second_ids),
                "subjects": {key: row.get("status") for key, row in
                             second["manifest"].get("subjects", {}).items()},
                "duration_s": second_measure["duration_s"],
                "python_peak_bytes": second_measure["python_peak_bytes"],
                "report_bytes": second_size["bytes"],
                "report_files": second_size["files"],
                "counts": second_counts,
            },
            "metrics": {
                "first": first_score,
                "second": second_score,
                "stable": first_score == second_score,
            },
            "comparison": {
                "new": len(comparison.get("new", [])),
                "resolved": len(comparison.get("resolved", [])),
                "unchanged": len(comparison.get("unchanged", [])),
                "stable_ids": first_ids == second_ids,
            },
            "duration": {
                "total_s": total_seconds,
                "first_report_s": first_measure["duration_s"],
                "second_report_s": second_measure["duration_s"],
                "single_host_sample": True,
            },
            "peak_memory": {
                "python_tracemalloc_bytes": {
                    "first_report": first_measure["python_peak_bytes"],
                    "second_report": second_measure["python_peak_bytes"],
                },
                "method": ("tracemalloc for Python allocations; native "
                           "allocations are excluded"),
                "isolated_per_report": False,
                "process_rss": rss,
            },
            "report_bytes": {
                "first": first_size,
                "second": second_size,
                "total_bytes": (first_size["bytes"] + second_size["bytes"]
                                if first_size["bytes"] is not None and
                                second_size["bytes"] is not None else None),
            },
            "counts": {"first": first_counts, "second": second_counts},
            "environment": {
                "status": "admitted" if complete_run else "partial",
                "preflight": "passed",
                "unavailable_label_subjects": unavailable_subjects,
            },
            "limitations": [
                "The second report compares the same frozen fixture against the first report.",
                "Stable finding IDs do not validate finding truth or severity.",
                "Duration and peak memory are single local samples; tracemalloc excludes native allocations and process RSS is not run-isolated.",
                "Only complete_subjects in the labels are scored for precision and false positives; other subjects remain unlabeled noise.",
                "No model, network, target import, index refresh, or test execution.",
            ],
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--labels", type=Path,
                        help="labels JSON; defaults to FIXTURE/labels.json")
    parser.add_argument("--budget", type=float, default=60)
    parser.add_argument("--out", type=Path, help="write JSON to this path; stdout by default")
    args = parser.parse_args(argv)
    if args.budget < 0:
        parser.error("--budget must be >= 0")
    result = json.dumps(evaluate(args.fixture, budget_s=args.budget,
                                 labels=args.labels),
                        indent=2, sort_keys=True) + "\n"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(result, encoding="utf-8")
    else:
        print(result, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
