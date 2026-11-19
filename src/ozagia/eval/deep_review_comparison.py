"""Offline comparison harnesses for deep review.

Three comparisons are provided, all on temporary synthetic checkouts:

* ``evaluate_range``: whole-repo report versus the same report with
  ``range_spec=...``, using two deterministic commits.
* ``evaluate_model_pass``: no model pass versus an injected, scripted fake
  runner.  No provider, network or live model is involved.
* ``evaluate_graph``: absent graph versus ``index=True`` on identical copies of
  the fixture.

Labels are loaded from the fixture's ``labels.json`` for the whole-repo,
model-pass and graph comparisons.  The range comparison uses
``tests/fixtures/deep_review_range/labels.json`` by default so its scripted
change text is not part of the checkout under test.  Every result names the
scope it measured; missing denominators are ``None``, never fabricated.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
import tracemalloc
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from ozagia.core.deep_review.model_pass import run_model_pass
from ozagia.core.deep_review.orchestrator import build_deep_review
from ozagia.core.repo_review import PreflightError
from ozagia.eval._safe_git import command as safe_git_command
from ozagia.eval.deep_review_labels import (
    METRIC_DEFINITIONS,
    expected_findings,
    label_metadata,
    load_labels,
    score_report_findings,
)

DEFAULT_FIXTURE = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "deep_review_service"
DEFAULT_RANGE_LABELS = (Path(__file__).resolve().parents[3] / "tests" /
                        "fixtures" / "deep_review_range" / "labels.json")
_FAKE_MODEL = "fake/offline"
_FAKE_ROLES = ("performance", "failure-modes")
_FIXED_GIT_ENV = {
    "GIT_AUTHOR_NAME": "Fixture Evaluation",
    "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00+0000",
    "GIT_COMMITTER_NAME": "Fixture Evaluation",
    "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
    "GIT_COMMITTER_DATE": "2026-01-01T00:00:00+0000",
}


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    command, env = safe_git_command(root, *args)
    return subprocess.run(
        command, check=check, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=20,
        env={**env, **_FIXED_GIT_ENV},
    )


def _revision(root: Path) -> str:
    return _git(root, "rev-parse", "HEAD").stdout.strip()


def _init_checkout(source: Path, workspace: Path, name: str) -> tuple[Path, str]:
    root = workspace / name
    shutil.copytree(source, root, ignore=shutil.ignore_patterns(
        "__pycache__", "*.pyc", ".git"))
    _git(root, "init", "-q", "-b", "main")
    _git(root, "add", "--all")
    _git(root, "commit", "-qm", "fixture-base")
    return root, _revision(root)


def _commit_all(root: Path, message: str) -> str:
    _git(root, "add", "--all")
    _git(root, "commit", "-qm", message)
    return _revision(root)


def _safe_relative(value: str) -> Path:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"fixture path escapes the checkout: {value}")
    return path


def _apply_changes(root: Path, changes: Iterable[Mapping[str, Any]]) -> None:
    for change in changes:
        path = root / _safe_relative(str(change["file"]))
        path.parent.mkdir(parents=True, exist_ok=True)
        if "append" in change:
            current = path.read_text(encoding="utf-8") if path.is_file() else ""
            path.write_text(current + str(change["append"]), encoding="utf-8")
        else:
            path.write_text(str(change["content"]), encoding="utf-8")


def _measure(call: Callable[[], dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
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
                    "peak_reason": peak_reason}


def _process_peak_rss() -> dict[str, Any]:
    try:
        import resource
    except ImportError:  # pragma: no cover - not available on Windows
        return {"bytes": None, "available": False,
                "reason": "resource.getrusage is not available on this platform"}
    try:
        raw = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    except (OSError, ValueError) as exc:  # pragma: no cover - platform failure
        return {"bytes": None, "available": False,
                "reason": f"resource.getrusage failed: {exc}"}
    return {"bytes": raw if sys.platform == "darwin" else raw * 1024,
            "available": True,
            "source": "resource.getrusage(RUSAGE_SELF).ru_maxrss",
            "scope": "process lifetime high-water mark; not isolated to one report"}


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


def _labels_input(fixture: str | Path, labels: str | Path | None) -> tuple[Path, Path, dict[str, Any]]:
    source = Path(fixture).resolve()
    labels_path = Path(labels) if labels is not None else source / "labels.json"
    return source, labels_path, load_labels(labels_path)


def _report_view(result: Mapping[str, Any], out: Path,
                 measure: Mapping[str, Any]) -> dict[str, Any]:
    manifest = result.get("manifest") or {}
    size = _report_size(out)
    return {
        "status": manifest.get("status"),
        "findings": len(result.get("findings") or []),
        "subjects": {key: row.get("status") for key, row in
                     (manifest.get("subjects") or {}).items()},
        "duration_s": measure["duration_s"],
        "python_peak_bytes": measure["python_peak_bytes"],
        "report_bytes": size["bytes"],
        "report_files": size["files"],
    }


def _range_review_state(result: Mapping[str, Any]) -> tuple[bool, dict[str, Any]]:
    """Return whether range evidence was collected and its environment state."""
    manifest = result.get("manifest") or {}
    review = manifest.get("range_review")
    if isinstance(review, Mapping) and review.get("status") == "available":
        return True, {"status": "admitted", "preflight": "passed",
                      "range_review": "available"}
    reason = (review.get("error") if isinstance(review, Mapping) else None)
    if not reason:
        reason = "range review did not report an available result"
    return False, {"status": "partial", "preflight": "passed",
                   "range_review": "unavailable", "reason": str(reason)}


def _finding_diff(first: Iterable[Mapping[str, Any]],
                  second: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    first_ids = {str(item["id"]) for item in first
                 if isinstance(item, Mapping) and item.get("id")}
    second_ids = {str(item["id"]) for item in second
                  if isinstance(item, Mapping) and item.get("id")}
    return {
        "added": sorted(second_ids - first_ids),
        "removed": sorted(first_ids - second_ids),
        "shared": sorted(first_ids & second_ids),
    }


def _signals(findings: Iterable[Mapping[str, Any]], paths: set[str]) -> list[dict[str, Any]]:
    rows = []
    for finding in findings:
        matched = sorted({
            str(location.get("path"))
            for location in finding.get("locations") or []
            if isinstance(location, Mapping) and location.get("path") in paths
        })
        if not matched:
            continue
        rows.append({"subject": finding.get("subject"),
                     "type": finding.get("type"), "paths": matched})
    return rows


def _comparison_counts(result: Mapping[str, Any]) -> dict[str, int]:
    manifest = result.get("manifest") or {}
    comparison = manifest.get("comparison") or {}
    return {
        "new": len(comparison.get("new") or []),
        "resolved": len(comparison.get("resolved") or []),
        "unchanged": len(comparison.get("unchanged") or []),
    }


# --------------------------------------------------------------------------- #
# whole repo vs range
# --------------------------------------------------------------------------- #
def evaluate_range(fixture: str | Path = DEFAULT_FIXTURE, *, budget_s: float = 60,
                   labels: str | Path | None = None) -> dict[str, Any]:
    """Compare a whole-repo report with a range report on two fixed commits.

    ``labels`` points at the range-label file (by default
    ``tests/fixtures/deep_review_range/labels.json``), not the whole-repo
    fixture labels.  Keeping it separate prevents its scripted change text from
    being scanned as part of the checkout under test.
    """
    source = Path(fixture).resolve()
    labels_path = Path(labels) if labels is not None else DEFAULT_RANGE_LABELS
    manifest = load_labels(labels_path)
    range_data = manifest.get("range")
    if not isinstance(range_data, Mapping):
        raise ValueError(
            f"labels range comparison missing: {labels_path} has no range section")
    expected = expected_findings(manifest, include_cases=False,
                                 include_report=False, include_range=True)
    complete = list(range_data.get("complete_subjects") or
                    sorted({row["subject"] for row in expected if row.get("subject")}))
    changes = list(range_data.get("changes") or [])
    changed_paths = {str(change["file"]) for change in changes}

    with tempfile.TemporaryDirectory(prefix="ozagia-deep-review-range-") as tmp:
        workspace = Path(tmp)
        root, base = _init_checkout(source, workspace, "repo")
        _apply_changes(root, changes)
        head = _commit_all(root, "fixture-change")
        full_out, range_out = workspace / "full", workspace / "range"
        started = time.perf_counter()
        try:
            full, full_measure = _measure(
                lambda: build_deep_review(root, full_out, budget_s=budget_s))
            ranged, range_measure = _measure(
                lambda: build_deep_review(
                    root, range_out, budget_s=budget_s,
                    range_spec=f"{base}..{head}",
                    baseline=full_out / "findings.json"))
        except PreflightError as exc:
            return {
                "schema_version": 1,
                "scope": "whole-repo versus range report on a temporary synthetic checkout",
                "fixture": source.name,
                "labels": label_metadata(manifest, labels_path),
                "commits": {"base": base, "head": head},
                "environment": _preflight_failure(exc),
                "comparison": None,
                "duration": {"total_s": time.perf_counter() - started,
                             "single_host_sample": True},
                "limitations": [
                    "Host resource admission can refuse either report before collectors run.",
                    "No model, network, target import, index refresh, or test execution.",
                ],
            }

        range_ids = {str(item["id"]) for item in ranged.get("findings") or []}
        full_ids = {str(item["id"]) for item in full.get("findings") or []}
        comparison = ranged.get("manifest", {}).get("comparison") or {}
        new_ids = {str(item) for item in comparison.get("new") or []}
        expected_types = {row["type"] for row in expected}
        # Range review maps findings to their subject sections. The public
        # baseline comparison identifies findings that the range added, so use
        # that instead of relying on a particular subject name.
        full_change = [
            item for item in full.get("findings") or []
            if str(item.get("type")) in expected_types
            and any(isinstance(location, Mapping) and
                    location.get("path") in changed_paths
                    for location in item.get("locations") or [])
        ]
        range_change = [
            item for item in ranged.get("findings") or []
            if str(item.get("id")) in new_ids
        ]
        full_expected = []
        for label in expected:
            row = dict(label)
            row["required_checks"] = label.get("baseline_required_checks")
            row["requires_graph"] = label.get("baseline_requires_graph", False)
            full_expected.append(row)
        full_score = score_report_findings(
            full_change, full_expected, complete_subjects=complete,
            report_manifest=full.get("manifest") or {})
        range_review = ranged.get("manifest", {}).get("range_review") or {}
        range_available, environment = _range_review_state(ranged)
        range_score = (score_report_findings(
            range_change, expected, complete_subjects=complete,
            report_manifest=ranged.get("manifest") or {})
                       if range_available else None)
        counts = _comparison_counts(ranged) if range_available else None
        range_graph = range_review.get("graph") or {}
        total_seconds = time.perf_counter() - started
        return {
            "schema_version": 1,
            "scope": "whole-repo versus range report on a temporary synthetic checkout",
            "fixture": source.name,
            "labels": label_metadata(manifest, labels_path),
            "status": "complete" if (range_available and
                                     full.get("manifest", {}).get("complete") and
                                     ranged.get("manifest", {}).get("complete"))
                      else "partial",
            "commits": {"base": base, "head": head},
            "changed_files": sorted(changed_paths),
            "environment": environment,
            "reports": {
                "full": _report_view(full, full_out, full_measure),
                "range": _report_view(ranged, range_out, range_measure),
            },
            "change_set": {
                "scope": "range-added findings only",
                "metric_definitions": dict(METRIC_DEFINITIONS),
                "expected": len(expected),
                "full": {
                    "expected": full_score["expected"],
                    "excluded_expected": full_score["excluded_expected"],
                    "unavailable_subjects": full_score["unavailable_subjects"],
                    "predicted": full_score["predicted"],
                    "true_positive": full_score["true_positive"],
                    "false_positive": full_score["false_positive"],
                    "false_negative": full_score["false_negative"],
                    "unlabeled": full_score["unlabeled"],
                    "recall": full_score["recall"],
                    "precision": full_score["precision"],
                    "false_positive_rate": full_score["false_positive_rate"],
                    "false_discovery_rate": full_score["false_discovery_rate"],
                    "report_noise_ratio": full_score["report_noise_ratio"],
                },
                "range": {
                    "expected": range_score["expected"] if range_score else None,
                    "excluded_expected": (range_score["excluded_expected"]
                                          if range_score else len(expected)),
                    "unavailable_subjects": (
                        range_score["unavailable_subjects"] if range_score else None),
                    "predicted": range_score["predicted"] if range_score else None,
                    "true_positive": range_score["true_positive"] if range_score else None,
                    "false_positive": range_score["false_positive"] if range_score else None,
                    "false_negative": range_score["false_negative"] if range_score else None,
                    "unlabeled": range_score["unlabeled"] if range_score else None,
                    "recall": range_score["recall"] if range_score else None,
                    "precision": range_score["precision"] if range_score else None,
                    "false_positive_rate": (range_score["false_positive_rate"]
                                             if range_score else None),
                    "false_discovery_rate": (range_score["false_discovery_rate"]
                                              if range_score else None),
                    "report_noise_ratio": (range_score["report_noise_ratio"]
                                           if range_score else None),
                },
                "full_signals_on_changed_files": _signals(
                    full.get("findings") or [], changed_paths),
                "range_signals_on_changed_files": (_signals(
                    ranged.get("findings") or [], changed_paths)
                    if range_available else None),
            },
            "comparison": counts,
            "finding_ids": {
                "added_by_range": len(range_ids - full_ids),
                "only_in_full": len(full_ids - range_ids),
                "shared": len(full_ids & range_ids),
            } if range_available else None,
            "range_graph": {
                "freshness": range_graph.get("freshness"),
                "available": range_graph.get("available"),
                "usable": range_graph.get("usable"),
                "reason": range_graph.get("reason"),
            },
            "duration": {
                "total_s": total_seconds,
                "full_report_s": full_measure["duration_s"],
                "range_report_s": range_measure["duration_s"],
                "single_host_sample": True,
            },
            "peak_memory": {
                "python_tracemalloc_bytes": {
                    "full_report": full_measure["python_peak_bytes"],
                    "range_report": range_measure["python_peak_bytes"],
                },
                "method": ("tracemalloc for Python allocations; native "
                           "allocations are excluded"),
                "isolated_per_report": False,
                "process_rss": _process_peak_rss(),
            },
            "limitations": [
                "The fixture and labels are synthetic; change-set recall is not a production estimate.",
                "Range-added findings are identified through the public baseline comparison; the whole-repo report cannot emit range-only rule identities.",
                "Range review used no fresh graph in this comparison; graph-dependent caller and test evidence was omitted.",
                "Duration and memory are single local samples; tracemalloc excludes native allocations.",
                "No model, network, target import, index refresh, or test execution.",
            ],
        }


# --------------------------------------------------------------------------- #
# optional model pass vs none
# --------------------------------------------------------------------------- #
def _packet_and_role(messages: Iterable[Mapping[str, Any]]) -> tuple[dict[str, Any], str]:
    content = ""
    for message in messages:
        if message.get("role") == "user":
            content = str(message.get("content") or "")
    match = re.search(r"^Review role: (.+)$", content, re.MULTILINE)
    role = match.group(1).strip() if match else "unknown"
    marker = "Review packet (deterministic JSON):\n"
    if marker not in content:
        return {}, role
    start = content.index(marker) + len(marker)
    end = content.find("\n\nReturn exactly one JSON object", start)
    if end < 0:
        return {}, role
    try:
        packet = json.loads(content[start:end])
    except (TypeError, ValueError):
        return {}, role
    return (packet if isinstance(packet, dict) else {}), role


def _fake_runner(model: str, messages: list[dict[str, Any]], **_kwargs: Any) -> dict[str, Any]:
    """Return scripted candidates with usage; never calls a provider."""
    packet, role = _packet_and_role(messages)
    rows = packet.get("findings") if isinstance(packet.get("findings"), list) else []
    target = next((row for row in rows
                   if isinstance(row, Mapping) and row.get("subject") == role), None)
    findings: list[dict[str, Any]] = []
    if isinstance(target, Mapping) and target.get("id"):
        wording = f"Review the {role} evidence before accepting it."
        for _ in range(2):
            findings.append({
                "wording": wording,
                "severity": "minor",
                "issue_type": f"{role}_review",
                "citation_keys": [target["id"]],
                "recommendation": "Check the cited deterministic finding.",
            })
    findings.append({
        "wording": "Unsupported cross-subject observation.",
        "severity": "note",
        "issue_type": "unsupported",
        "citation_keys": ["dr-not-in-packet"],
        "recommendation": "Discard unless deterministic evidence is added.",
    })
    return {
        "output": {"summary": "Scripted offline candidates.", "findings": findings},
        "usage": {"prompt_tokens": 100, "completion_tokens": 20,
                  "total_tokens": 120},
    }


def _run_fake_model_pass(findings: list[Mapping[str, Any]]) -> dict[str, Any]:
    return run_model_pass(
        findings, model_runner=_fake_runner, models=_FAKE_MODEL,
        roles=_FAKE_ROLES,
    )


def _candidate_citations(candidate: Mapping[str, Any]) -> list[str]:
    evidence = candidate.get("evidence")
    if isinstance(evidence, list) and evidence and isinstance(evidence[0], Mapping):
        cited = evidence[0].get("citation_ids")
        if isinstance(cited, list):
            return sorted({str(item) for item in cited if item})
    related = candidate.get("related_ids")
    if isinstance(related, list):
        return sorted({str(item) for item in related if item})
    return []


def _candidate_key(candidate: Mapping[str, Any]) -> tuple[str, tuple[str, ...]]:
    wording = " ".join(str(candidate.get("title") or candidate.get("impact") or "")
                       .casefold().split())
    return wording, tuple(_candidate_citations(candidate))


def _expected_key(row: Mapping[str, Any]) -> tuple[str, str, str | None]:
    return (str(row["path"]), str(row["type"]), row.get("subject"))


def score_model_candidates(candidates: Iterable[Mapping[str, Any]],
                           findings: Iterable[Mapping[str, Any]],
                           expected: Iterable[Mapping[str, Any]], *,
                           accepted: int | None = None,
                           deduplicated: int = 0) -> dict[str, Any]:
    """Score scripted model candidates against reviewed labels via citations.

    A candidate is supported when one of its accepted citation IDs points to a
    deterministic finding whose location and type match a reviewed label.
    Candidate recall is the fraction of reviewed labels covered that way.
    Duplicate candidates are identical after wording normalisation and citation
    sorting; only the first of each duplicate group is scored.  ``accepted`` and
    ``deduplicated`` carry the model-pass validation counts when the pass
    collapses duplicate candidates before returning them.
    """
    expected_rows = [dict(row) for row in expected]
    label_by_finding: dict[str, set[tuple[str, str, str | None]]] = {}
    for finding in findings:
        if not isinstance(finding, Mapping) or not finding.get("id"):
            continue
        labels: set[tuple[str, str, str | None]] = set()
        for location in finding.get("locations") or []:
            if not isinstance(location, Mapping) or not location.get("path"):
                continue
            for label in expected_rows:
                if (str(location["path"]) == label["path"] and
                        str(finding.get("type")) == label["type"] and
                        (label.get("subject") is None or
                         label.get("subject") == finding.get("subject"))):
                    labels.add(_expected_key(label))
        if labels:
            label_by_finding[str(finding["id"])] = labels

    rows = []
    seen: set[tuple[str, tuple[str, ...]]] = set()
    observed_duplicates = 0
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            continue
        key = _candidate_key(candidate)
        labels: set[tuple[str, str, str | None]] = set()
        for citation in _candidate_citations(candidate):
            labels.update(label_by_finding.get(citation, set()))
        duplicate = key in seen
        if duplicate:
            observed_duplicates += 1
        else:
            seen.add(key)
        rows.append({"key": key, "labels": labels, "duplicate": duplicate})

    unique = [row for row in rows if not row["duplicate"]]
    supported = [row for row in unique if row["labels"]]
    covered = set().union(*(row["labels"] for row in supported)) if supported else set()
    total = len(rows)
    unique_count = len(unique)
    supported_count = len(supported)
    noise = unique_count - supported_count
    duplicate_count = max(observed_duplicates, int(deduplicated))
    accepted_count = accepted if accepted is not None else total
    reason = ("no model candidates were produced" if not unique_count else
              "no reviewed labels are available for this run" if not expected_rows else None)
    return {
        "candidates": total,
        "accepted_candidates": accepted_count,
        "unique_candidates": unique_count,
        "supported_candidates": supported_count,
        "added_noise_candidates": noise,
        "duplicate_candidates": duplicate_count,
        "covered_labels": len(covered),
        "expected_labels": len(expected_rows),
        "candidate_precision": (supported_count / unique_count
                                if unique_count and expected_rows else None),
        "candidate_recall": (len(covered) / len(expected_rows)
                             if expected_rows else None),
        "added_noise_rate": noise / unique_count if unique_count else None,
        "duplicate_rate": (duplicate_count / accepted_count
                           if accepted_count else None),
        "duplicate_rate_basis": "duplicate accepted candidates / accepted model candidates",
        "reason": reason,
    }


def _usage_counts(runs: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    run_rows = [run for run in runs if isinstance(run, Mapping)]
    prompt = completion = total = 0
    missing = 0
    for run in run_rows:
        usage = run.get("usage")
        if not isinstance(usage, Mapping) or not usage:
            missing += 1
            continue
        values = []
        for keys in (("prompt_tokens", "input_tokens", "input"),
                     ("completion_tokens", "output_tokens", "output")):
            value = next((usage[key] for key in keys
                          if isinstance(usage.get(key), (int, float)) and
                          not isinstance(usage.get(key), bool)), None)
            values.append(int(value) if value is not None else None)
        total_value = usage.get("total_tokens")
        total_value = (int(total_value) if isinstance(total_value, (int, float)) and
                       not isinstance(total_value, bool) else None)
        if values[0] is None and values[1] is None and total_value is None:
            missing += 1
            continue
        prompt += values[0] or 0
        completion += values[1] or 0
        if total_value is None:
            total_value = (values[0] or 0) + (values[1] or 0)
        total += total_value
    available = bool(run_rows) and missing == 0
    return {
        "available": available,
        "partial": missing > 0 and missing < len(run_rows),
        "runs": len(run_rows),
        "missing_runs": missing,
        "prompt_tokens": prompt if available else None,
        "completion_tokens": completion if available else None,
        "total_tokens": total if available else None,
        "reason": (None if available else
                   "no model run reported usage" if not run_rows else
                   f"{missing} of {len(run_rows)} model runs reported no usage"),
    }


def _deterministic_signature(result: Mapping[str, Any]) -> str:
    rows = []
    for candidate in result.get("findings") or []:
        rows.append({
            "title": candidate.get("title"),
            "citations": _candidate_citations(candidate),
            "severity": candidate.get("candidate_severity"),
            "type": candidate.get("type"),
        })
    rows.sort(key=lambda row: json.dumps(row, sort_keys=True))
    return json.dumps(rows, sort_keys=True, separators=(",", ":"))


def evaluate_model_pass(fixture: str | Path = DEFAULT_FIXTURE, *,
                        budget_s: float = 60,
                        labels: str | Path | None = None) -> dict[str, Any]:
    """Compare deterministic findings with an injected fake model pass."""
    source, labels_path, manifest = _labels_input(fixture, labels)
    expected = expected_findings(manifest, include_report=True)
    complete = list(manifest.get("complete_subjects") or [])
    metadata = label_metadata(manifest, labels_path)

    with tempfile.TemporaryDirectory(prefix="ozagia-deep-review-model-") as tmp:
        workspace = Path(tmp)
        root, _base = _init_checkout(source, workspace, "repo")
        out = workspace / "report"
        started = time.perf_counter()
        try:
            report, measure = _measure(
                lambda: build_deep_review(root, out, budget_s=budget_s))
        except PreflightError as exc:
            return {
                "schema_version": 1,
                "scope": "deterministic report versus an injected fake model pass",
                "fixture": source.name,
                "labels": metadata,
                "environment": _preflight_failure(exc),
                "baseline": None,
                "with_model_pass": None,
                "duration": {"total_s": time.perf_counter() - started,
                             "single_host_sample": True},
                "limitations": [
                    "Host resource admission can refuse report generation before collectors run.",
                    "No model, network, target import, index refresh, or test execution.",
                ],
            }

        findings = [item for item in report.get("findings") or []
                    if isinstance(item, dict)]
        baseline_score = score_report_findings(
            findings, expected, complete_subjects=complete,
            report_manifest=report.get("manifest") or {})
        unavailable_subjects = set(baseline_score["unavailable_subjects"])
        candidate_expected = [row for row in expected
                              if (row.get("subject") not in unavailable_subjects
                                  and not (row.get("subject") is None
                                           and unavailable_subjects))]
        before_pass = json.dumps(findings, sort_keys=True)
        first_pass = _run_fake_model_pass(findings)
        second_pass = _run_fake_model_pass(findings)
        validation = first_pass.get("validation")
        validation = validation if isinstance(validation, Mapping) else {}
        candidate_metrics = score_model_candidates(
            first_pass.get("findings") or [], findings, candidate_expected,
            accepted=validation.get("accepted"),
            deduplicated=int(validation.get("deduplicated") or 0),
        )
        usage = _usage_counts(first_pass.get("runs") or [])
        deterministic_unchanged = before_pass == json.dumps(findings, sort_keys=True)
        repeat_stable = (_deterministic_signature(first_pass) ==
                         _deterministic_signature(second_pass))
        total_seconds = time.perf_counter() - started
        unavailable_subjects = baseline_score["unavailable_subjects"]
        model_run_complete = (report.get("manifest", {}).get("complete")
                              and not unavailable_subjects)
        return {
            "schema_version": 1,
            "scope": "deterministic report versus an injected fake model pass",
            "fixture": source.name,
            "labels": metadata,
            "status": "complete" if model_run_complete else "partial",
            "environment": {
                "status": "admitted",
                "preflight": "passed",
                "unavailable_label_subjects": unavailable_subjects,
            },
            "report": _report_view(report, out, measure),
            "baseline": {
                "model_pass": False,
                "model_runs": 0,
                "model_candidates": 0,
                "deterministic_findings": len(findings),
                "deterministic_score": baseline_score,
                "usage": {"available": False, "reason": "no model pass requested",
                          "runs": 0, "prompt_tokens": None,
                          "completion_tokens": None, "total_tokens": None},
            },
            "with_model_pass": {
                "model_pass": True,
                "status": first_pass.get("status"),
                "model_runs": len(first_pass.get("runs") or []),
                "errors": first_pass.get("errors") or [],
                "model_candidates": candidate_metrics["candidates"],
                "deterministic_findings": len(findings),
                "deterministic_unchanged": deterministic_unchanged,
                "candidate_metrics": candidate_metrics,
                "validation": validation,
                "budgets": first_pass.get("budgets"),
                "packet_findings": first_pass.get("packet_findings"),
                "usage": usage,
                "truth_assessed": first_pass.get("truth_assessed"),
            },
            "delta": {
                "deterministic_findings": 0,
                "model_candidates_added": candidate_metrics["unique_candidates"],
                "noise_candidates_added": candidate_metrics["added_noise_candidates"],
                "tokens_added": usage.get("total_tokens"),
            },
            "deterministic_repeat": repeat_stable,
            "truth_assessed": False,
            "live_provider_calls": 0,
            "network": False,
            "duration": {
                "total_s": total_seconds,
                "report_s": measure["duration_s"],
                "single_host_sample": True,
            },
            "peak_memory": {
                "python_tracemalloc_bytes": measure["python_peak_bytes"],
                "method": ("tracemalloc for Python allocations; native "
                           "allocations are excluded"),
                "isolated_per_report": False,
                "process_rss": _process_peak_rss(),
            },
            "limitations": [
                "The runner is scripted and offline; its candidates measure the harness and scoring seam, not model quality.",
                "Candidate precision and recall depend on the injected runner and on citation-to-label mapping.",
                "No live provider, network, target import, index refresh, or test execution.",
                "Labels are synthetic and only complete_subjects are scored for false positives.",
            ],
        }


# --------------------------------------------------------------------------- #
# graph-aware vs graph-less
# --------------------------------------------------------------------------- #
def _priority_summary(result: Mapping[str, Any]) -> dict[str, Any]:
    manifest = result.get("manifest") or {}
    info = manifest.get("prioritization_graph") or {}
    findings = [item for item in result.get("findings") or []
                if isinstance(item, Mapping)]
    priorities = [item.get("priority") for item in findings
                  if isinstance(item.get("priority"), Mapping)]
    scores = [row.get("score") for row in priorities
              if isinstance(row.get("score"), (int, float))]
    return {
        "freshness": info.get("freshness"),
        "reason": info.get("reason"),
        "nodes": info.get("nodes"),
        "edges": info.get("edges"),
        "findings": len(findings),
        "findings_with_direct_symbols": sum(
            1 for row in priorities
            if isinstance(row.get("blast_radius"), Mapping) and
            row["blast_radius"].get("direct_symbols")),
        "findings_with_reachable_callers": sum(
            1 for row in priorities
            if isinstance(row.get("blast_radius"), Mapping) and
            row["blast_radius"].get("reachable_callers")),
        "findings_without_graph_omission": sum(
            1 for row in priorities if row.get("omission") is None),
        "mean_priority_score": (sum(scores) / len(scores) if scores else None),
        "max_priority_score": max(scores) if scores else None,
    }


def _quality_row(score: Mapping[str, Any]) -> dict[str, Any]:
    predicted = score["predicted"]
    return {
        "expected": score["expected"],
        "predicted": predicted,
        "true_positive": score["true_positive"],
        "false_positive": score["false_positive"],
        "false_negative": score["false_negative"],
        "unlabeled": score["unlabeled"],
        "precision": score["precision"],
        "recall": score["recall"],
        "false_positive_rate": score["false_positive_rate"],
        "false_discovery_rate": score["false_discovery_rate"],
        "report_noise_ratio": score["report_noise_ratio"],
        "precision_all_predictions": (score["true_positive"] / predicted
                                      if predicted else None),
    }


def evaluate_graph(fixture: str | Path = DEFAULT_FIXTURE, *, budget_s: float = 60,
                   labels: str | Path | None = None) -> dict[str, Any]:
    """Compare an absent graph with a fresh graph built through ``index=True``."""
    source, labels_path, manifest = _labels_input(fixture, labels)
    expected = expected_findings(manifest, include_report=True)
    complete = list(manifest.get("complete_subjects") or [])
    metadata = label_metadata(manifest, labels_path)

    with tempfile.TemporaryDirectory(prefix="ozagia-deep-review-graph-") as tmp:
        workspace = Path(tmp)
        less_root, less_rev = _init_checkout(source, workspace, "graph-less")
        graph_root, graph_rev = _init_checkout(source, workspace, "graph-aware")
        less_out, graph_out = workspace / "graph-less-report", workspace / "graph-aware-report"
        started = time.perf_counter()
        try:
            less, less_measure = _measure(
                lambda: build_deep_review(less_root, less_out, budget_s=budget_s))
            aware, aware_measure = _measure(
                lambda: build_deep_review(graph_root, graph_out,
                                          budget_s=budget_s, index=True))
        except PreflightError as exc:
            return {
                "schema_version": 1,
                "scope": "absent graph versus a fresh graph on identical synthetic checkouts",
                "fixture": source.name,
                "labels": metadata,
                "environment": _preflight_failure(exc),
                "quality": None,
                "duration": {"total_s": time.perf_counter() - started,
                             "single_host_sample": True},
                "limitations": [
                    "Host resource admission can refuse either report before collectors run.",
                    "No model, network, target import, or test execution.",
                ],
            }

        less_score = score_report_findings(
            less.get("findings") or [], expected, complete_subjects=complete,
            report_manifest=less.get("manifest") or {})
        aware_score = score_report_findings(
            aware.get("findings") or [], expected, complete_subjects=complete,
            report_manifest=aware.get("manifest") or {})
        diff = _finding_diff(less.get("findings") or [], aware.get("findings") or [])
        unavailable_subjects = sorted(set(less_score["unavailable_subjects"])
                                      | set(aware_score["unavailable_subjects"]))
        graph_run_complete = (less.get("manifest", {}).get("complete")
                              and aware.get("manifest", {}).get("complete")
                              and not unavailable_subjects)
        total_seconds = time.perf_counter() - started
        return {
            "schema_version": 1,
            "scope": "absent graph versus a fresh graph on identical synthetic checkouts",
            "fixture": source.name,
            "labels": metadata,
            "status": "complete" if graph_run_complete else "partial",
            "commits": {"graph_less": less_rev, "graph_aware": graph_rev,
                        "same": less_rev == graph_rev},
            "environment": {
                "status": "admitted",
                "preflight": "passed",
                "unavailable_label_subjects": unavailable_subjects,
            },
            "reports": {
                "graph_less": _report_view(less, less_out, less_measure),
                "graph_aware": _report_view(aware, graph_out, aware_measure),
            },
            "quality": {
                "scope": "reviewed labels are complete only for the listed complete_subjects",
                "metric_definitions": {
                    **METRIC_DEFINITIONS,
                    "precision_all_predictions": (
                        "true positives / every reported (path, type, subject) "
                        "pair; unlabeled graph-added pairs count as misses"
                    ),
                    "report_noise_ratio": (
                        "unmatched reported pairs / every reported pair"
                    ),
                },
                "graph_less": _quality_row(less_score),
                "graph_aware": _quality_row(aware_score),
            },
            "priority_evidence": {
                "graph_less": _priority_summary(less),
                "graph_aware": _priority_summary(aware),
            },
            "finding_ids": {
                "added_by_graph": len(diff["added"]),
                "removed_by_graph": len(diff["removed"]),
                "shared": len(diff["shared"]),
                "added_ids": diff["added"],
                "removed_ids": diff["removed"],
            },
            "duration": {
                "total_s": total_seconds,
                "graph_less_s": less_measure["duration_s"],
                "graph_aware_s": aware_measure["duration_s"],
                "single_host_sample": True,
            },
            "peak_memory": {
                "python_tracemalloc_bytes": {
                    "graph_less": less_measure["python_peak_bytes"],
                    "graph_aware": aware_measure["python_peak_bytes"],
                },
                "method": ("tracemalloc for Python allocations; native "
                           "allocations are excluded"),
                "isolated_per_report": False,
                "process_rss": _process_peak_rss(),
            },
            "limitations": [
                "The fixture has no call edges between its functions, so graph-aware unreferenced-symbol candidates are partly a fixture artifact.",
                "Graph-aware adds candidates and report noise on this fixture; that does not show that graph evidence improves review quality.",
                "Labels are synthetic and only complete_subjects are scored for false positives.",
                "Duration and memory are single local samples; tracemalloc excludes native allocations.",
                "No model, network, target import, index refresh outside the temporary checkout, or test execution.",
            ],
        }


COMPARISONS = {
    "range": evaluate_range,
    "model-pass": evaluate_model_pass,
    "graph": evaluate_graph,
}


def evaluate_all(fixture: str | Path = DEFAULT_FIXTURE, *, budget_s: float = 60,
                 labels: str | Path | None = None,
                 range_labels: str | Path | None = None) -> dict[str, Any]:
    source = Path(fixture).resolve()
    return {
        "schema_version": 1,
        "fixture": source.name,
        "scope": "offline deep-review comparison suite on synthetic checkouts",
        "comparisons": {
            "range": evaluate_range(fixture, budget_s=budget_s,
                                    labels=range_labels),
            "model-pass": evaluate_model_pass(fixture, budget_s=budget_s,
                                              labels=labels),
            "graph": evaluate_graph(fixture, budget_s=budget_s, labels=labels),
        },
        "limitations": [
            "Synthetic labels and fixtures are regression evidence, not production accuracy.",
            "No live provider, network, target import, index refresh outside temporary checkouts, or test execution.",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--comparison", choices=(*COMPARISONS, "all"), default="all")
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--labels", type=Path,
                        help="whole-repo labels JSON; defaults to FIXTURE/labels.json")
    parser.add_argument("--range-labels", type=Path,
                        help="range labels JSON; defaults to tests/fixtures/deep_review_range/labels.json")
    parser.add_argument("--budget", type=float, default=60)
    parser.add_argument("--out", type=Path,
                        help="write JSON to this path; stdout by default")
    args = parser.parse_args(argv)
    if args.budget < 0:
        parser.error("--budget must be >= 0")
    if args.comparison == "all":
        result = evaluate_all(args.fixture, budget_s=args.budget,
                              labels=args.labels, range_labels=args.range_labels)
    elif args.comparison == "range":
        result = evaluate_range(args.fixture, budget_s=args.budget,
                                labels=args.range_labels)
    else:
        result = COMPARISONS[args.comparison](
            args.fixture, budget_s=args.budget, labels=args.labels)
    text = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
    else:
        print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
