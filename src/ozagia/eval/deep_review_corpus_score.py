"""Deterministic scoring for the realistic deep-review corpus.

Inputs are frozen: a versioned corpus manifest, sealed human label files with an
adjudication, and a run record pointing at raw ``findings.json`` artifacts.  The
scorer never runs deep review, git, tests, a model or the network.  It reuses
the existing ``score_findings`` pair matcher so synthetic fixture scores and
realistic-corpus scores keep the same precision/recall definitions.

Synthetic and realistic evidence are separate by construction: one invocation
scores exactly one ``evidence_class`` and refuses a corpus identity that mixes
markers.  A synthetic run can exercise the pipeline, but every quality gate is
marked not applicable and the quality claim is ``none``.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping

from ozagia.eval.deep_review_corpus import (
    DEFAULT_LABELS_DIR,
    DEFAULT_MANIFEST,
    CorpusError,
    _repo_by_id,
    _slot_by_key,
    canonical_sha256,
    checkout_path,
    load_manifest,
    manifest_digest,
)
from ozagia.eval.deep_review_corpus_labels import (
    LabelError,
    load_label_set,
)
from ozagia.eval.deep_review_labels import METRIC_DEFINITIONS, score_findings

SCORE_SCHEMA_VERSION = 1
REALISTIC = "realistic_corpus"
SYNTHETIC = "synthetic_regression"
EVIDENCE_CLASSES = frozenset({REALISTIC, SYNTHETIC})
SYNTHETIC_MARKERS = ("synthetic", "fixture", "dry-run", "dry_run")
CONDITIONS = frozenset({"whole_repo", "range", "graph_aware", "graph_less"})
RUN_STATUSES = frozenset({"complete", "partial", "failed"})
SUBJECT_STATUSES = frozenset({"complete", "partial", "unknown", "not_run"})
GRAPH_FRESHNESS = frozenset({"fresh", "stale", "missing", "unknown"})
HIGH_IMPACT_SEVERITIES = frozenset({"blocking", "major"})
BLOCKING = "blocking"
REPORT_MAX_BYTES = 64 * 1024 * 1024
MAX_RUNS = 10_000
MIN_COMPLETE_REPOSITORIES = 8
PRECISION_THRESHOLD = 0.80
RECALL_THRESHOLD = 0.70
HIGH_IMPACT_PRECISION_THRESHOLD = 0.90
SUBJECT_PRECISION_FLOOR = 0.65
SUBJECT_PREDICTION_MINIMUM = 20
NOISE_THRESHOLD = 0.20
OPERATIONAL_PASS_RATE = 0.95
HUMAN_UTILITY_THRESHOLD = 0.80

SCORE_METRIC_DEFINITIONS = dict(METRIC_DEFINITIONS)
SCORE_METRIC_DEFINITIONS.update({
    "raw_counts": (
        "precision numerator is true-positive predictions in complete scopes; "
        "its denominator adds false positives from those scopes. recall uses "
        "all reviewed positive labels included by a complete run, including "
        "partial-scope labels. noise uses all predictions, labeled or not."
    ),
    "noise_on_complete_scope": (
        "false positives / predictions in complete scopes; this is the "
        "proposed noise gate's measure. The overall noise ratio keeps "
        "unlabeled-scope predictions in its denominator and is reported "
        "separately."
    ),
    "coverage": (
        "a scope is complete only when both reviewers and the adjudicator "
        "declare complete scope and the scored run collected every declared "
        "subject; otherwise its predictions are unlabeled and its labels do "
        "not contribute precision"
    ),
})


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _text(value: Any, field: str, *, required: bool = True) -> str | None:
    if value is None:
        if required:
            raise CorpusError("invalid_argument", f"{field} must be a non-empty string")
        return None
    if not isinstance(value, str) or not value.strip():
        raise CorpusError("invalid_argument", f"{field} must be a non-empty string")
    return value.strip()


def _number(value: Any, field: str, *, required: bool = False) -> float | None:
    if value is None and not required:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise CorpusError("invalid_argument", f"{field} must be a non-negative number")
    return float(value)


def _sha_or_none(value: Any, field: str) -> str | None:
    text = _text(value, field, required=False)
    if text is None:
        return None
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise CorpusError("invalid_argument", f"{field} must be a lowercase SHA-256")
    return text


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _record_path(base: Path, value: Any, field: str) -> Path | None:
    text = _text(value, field, required=False)
    if text is None:
        return None
    path = Path(text).expanduser()
    return path if path.is_absolute() else (base / path).resolve()


def load_run_record(path: str | Path) -> dict[str, Any]:
    """Read and validate a run record.  Paths remain relative to the record."""
    source = Path(path)
    if not source.is_file():
        raise CorpusError("record_missing", f"run record missing: {source}")
    if source.stat().st_size > 4 * 1024 * 1024:
        raise CorpusError("too_large", "run record exceeds 4 MiB")
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CorpusError("bad_record", f"invalid run record JSON: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise CorpusError("bad_record", "run record must be an object")
    schema = raw.get("schema_version")
    if isinstance(schema, bool) or not isinstance(schema, int) or schema != 1:
        raise CorpusError("bad_record", "run record.schema_version must be 1")
    corpus_id = _text(raw.get("corpus_id"), "record.corpus_id")
    evidence_class = _text(raw.get("evidence_class"), "record.evidence_class")
    assert corpus_id is not None and evidence_class is not None
    if evidence_class not in EVIDENCE_CLASSES:
        raise CorpusError("bad_record", "record.evidence_class is not supported")
    lowered = corpus_id.lower()
    has_marker = any(marker in lowered for marker in SYNTHETIC_MARKERS)
    if evidence_class == REALISTIC and has_marker:
        raise CorpusError(
            "pooling_refused",
            "a realistic record cannot use a synthetic/fixture corpus identity")
    if evidence_class == SYNTHETIC and not has_marker:
        raise CorpusError(
            "pooling_refused",
            "a synthetic record must identify itself with synthetic/fixture/dry-run")
    condition = _text(raw.get("scored_condition"), "record.scored_condition")
    assert condition is not None
    if condition not in CONDITIONS:
        raise CorpusError("bad_record", "record.scored_condition is not supported")
    runs_value = raw.get("runs")
    if not isinstance(runs_value, list) or not runs_value:
        raise CorpusError("bad_record", "record.runs must be a non-empty list")
    if len(runs_value) > MAX_RUNS:
        raise CorpusError("too_large", f"record.runs exceeds {MAX_RUNS}")
    runs: list[dict[str, Any]] = []
    run_ids: set[str] = set()
    for index, item in enumerate(runs_value):
        field = f"record.runs[{index}]"
        if not isinstance(item, Mapping):
            raise CorpusError("bad_record", f"{field} must be an object")
        run_id = _text(item.get("run_id"), f"{field}.run_id")
        assert run_id is not None
        if run_id in run_ids:
            raise CorpusError("duplicate_run_id", f"duplicate run_id: {run_id}")
        run_ids.add(run_id)
        repository_id = _text(item.get("repository_id"), f"{field}.repository_id")
        subject_key = _text(item.get("subject_key"), f"{field}.subject_key")
        run_condition = _text(item.get("condition"), f"{field}.condition")
        status = _text(item.get("status"), f"{field}.status")
        assert repository_id and subject_key and run_condition and status
        if run_condition not in CONDITIONS:
            raise CorpusError("bad_record", f"{field}.condition is not supported")
        if status not in RUN_STATUSES:
            raise CorpusError("bad_record", f"{field}.status is not supported")
        report_path = _record_path(source.parent, item.get("report_path"),
                                   f"{field}.report_path")
        report_sha = _sha_or_none(item.get("report_sha256"), f"{field}.report_sha256")
        manifest_path = _record_path(source.parent, item.get("manifest_path"),
                                     f"{field}.manifest_path")
        manifest_sha = _sha_or_none(item.get("manifest_sha256"),
                                    f"{field}.manifest_sha256")
        subject_status = item.get("subject_status")
        if subject_status is not None:
            if not isinstance(subject_status, Mapping):
                raise CorpusError("bad_record", f"{field}.subject_status must be an object")
            normalized_status: dict[str, str] = {}
            for subject, value in subject_status.items():
                subject_name = _text(subject, f"{field}.subject_status key")
                assert subject_name is not None
                if value not in SUBJECT_STATUSES:
                    raise CorpusError("bad_record",
                                      f"{field}.subject_status[{subject_name}] is invalid")
                normalized_status[subject_name] = str(value)
            subject_status = normalized_status
        graph = _text(item.get("graph_freshness"), f"{field}.graph_freshness",
                      required=False) or "unknown"
        if graph not in GRAPH_FRESHNESS:
            raise CorpusError("bad_record", f"{field}.graph_freshness is invalid")
        budget = _number(item.get("budget_s"), f"{field}.budget_s")
        duration = _number(item.get("duration_s"), f"{field}.duration_s")
        exit_status = item.get("exit_status")
        if exit_status is not None and (isinstance(exit_status, bool)
                                        or not isinstance(exit_status, int)):
            raise CorpusError("bad_record", f"{field}.exit_status must be an integer")
        writes = item.get("writes_outside_allowed_paths")
        if writes is not None and not isinstance(writes, bool):
            raise CorpusError("bad_record",
                              f"{field}.writes_outside_allowed_paths must be a boolean")
        repeat_of = _text(item.get("repeat_of"), f"{field}.repeat_of", required=False)
        runs.append({
            "run_id": run_id,
            "repository_id": repository_id,
            "subject_key": subject_key,
            "condition": run_condition,
            "status": status,
            "report_path": str(report_path) if report_path is not None else None,
            "report_sha256": report_sha,
            "manifest_path": str(manifest_path) if manifest_path is not None else None,
            "manifest_sha256": manifest_sha,
            "subject_status": subject_status,
            "graph_freshness": graph,
            "budget_s": budget,
            "duration_s": duration,
            "exit_status": exit_status,
            "writes_outside_allowed_paths": writes,
            "repeat_of": repeat_of,
        })
    for run in runs:
        if run["repeat_of"] is not None and run["repeat_of"] not in run_ids:
            raise CorpusError("bad_record",
                              f"repeat_of references missing run: {run['repeat_of']}")
    record = {
        "schema_version": 1,
        "corpus_id": corpus_id,
        "evidence_class": evidence_class,
        "scored_condition": condition,
        "source_revision": raw.get("source_revision"),
        "environment": raw.get("environment"),
        "manifest_digest": _sha_or_none(raw.get("manifest_digest"),
                                        "record.manifest_digest"),
        "labels_digest": _sha_or_none(raw.get("labels_digest"),
                                      "record.labels_digest"),
        "human_utility": _human_utility(raw.get("human_utility")),
        "runs": runs,
        "record_path": str(source),
        "digest": canonical_sha256({key: value for key, value in raw.items()
                                    if key != "digest"}),
    }
    return record


def _human_utility(value: Any) -> dict[str, Any]:
    if value is None:
        return {"status": "not_measured"}
    if not isinstance(value, Mapping):
        raise CorpusError("bad_record", "record.human_utility must be an object")
    rate = value.get("actionability_rate")
    if isinstance(rate, bool) or not isinstance(rate, (int, float)) or not 0 <= rate <= 1:
        raise CorpusError("bad_record",
                          "human_utility.actionability_rate must be between 0 and 1")
    sample = value.get("sample_size")
    if isinstance(sample, bool) or not isinstance(sample, int) or sample < 1:
        raise CorpusError("bad_record", "human_utility.sample_size must be positive")
    method = _text(value.get("method"), "human_utility.method")
    return {"status": "measured", "actionability_rate": float(rate),
            "sample_size": sample, "method": method}


def _read_json_artifact(path: Path, *, max_bytes: int, what: str) -> tuple[bytes, Any]:
    if not path.is_file():
        raise CorpusError("missing_artifact", f"{what} missing: {path}")
    if path.stat().st_size > max_bytes:
        raise CorpusError("too_large", f"{what} exceeds {max_bytes} bytes: {path}")
    try:
        data = path.read_bytes()
        value = json.loads(data.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CorpusError("bad_artifact", f"invalid {what} JSON in {path}: {exc}") from exc
    return data, value


def _load_findings(run: Mapping[str, Any]) -> list[dict[str, Any]]:
    if run["status"] == "failed":
        return []
    path = run.get("report_path")
    if path is None:
        raise CorpusError("missing_artifact",
                          f"run {run['run_id']} has no report_path")
    report = Path(path)
    if report.is_dir():
        report = report / "findings.json"
    data, value = _read_json_artifact(report, max_bytes=REPORT_MAX_BYTES,
                                     what="findings report")
    digest = hashlib.sha256(data).hexdigest()
    if run.get("report_sha256") is not None and digest != run["report_sha256"]:
        raise CorpusError(
            "report_digest_mismatch",
            f"findings report digest does not match the run record for {run['run_id']}",
            details={"expected": run["report_sha256"], "actual": digest,
                     "path": str(report)})
    items = value.get("findings") if isinstance(value, Mapping) else value
    if not isinstance(items, list):
        raise CorpusError("bad_artifact",
                          f"findings report must be a list or have a findings list: {report}")
    findings: list[dict[str, Any]] = []
    seen: dict[str, tuple[Any, ...]] = {}
    for index, item in enumerate(items):
        if not isinstance(item, Mapping):
            raise CorpusError("bad_artifact", f"finding {index} is not an object")
        finding = dict(item)
        finding_id = finding.get("id")
        identity = (str(finding.get("type") or ""),
                    str(finding.get("subject") or ""),
                    tuple(sorted(str(location.get("path"))
                                 for location in finding.get("locations") or []
                                 if isinstance(location, Mapping) and location.get("path"))))
        if isinstance(finding_id, str) and finding_id:
            if finding_id in seen:
                if seen[finding_id] != identity:
                    raise CorpusError(
                        "duplicate_finding_id",
                        f"duplicate finding id with different content in {report}: "
                        f"{finding_id}")
                continue
            seen[finding_id] = identity
        finding["severity"] = str(finding.get("severity") or "unknown").lower()
        finding["subject"] = str(finding.get("subject") or "unknown")
        finding["type"] = str(finding.get("type") or "unknown")
        findings.append(finding)
    return findings


def _load_report_manifest(run: Mapping[str, Any]) -> dict[str, Any]:
    path = run.get("manifest_path")
    if path is None:
        return {}
    data, value = _read_json_artifact(Path(path), max_bytes=16 * 1024 * 1024,
                                     what="report manifest")
    digest = hashlib.sha256(data).hexdigest()
    if run.get("manifest_sha256") is not None and digest != run["manifest_sha256"]:
        raise CorpusError(
            "manifest_digest_mismatch",
            f"report manifest digest does not match the run record for {run['run_id']}",
            details={"expected": run["manifest_sha256"], "actual": digest})
    if not isinstance(value, Mapping):
        raise CorpusError("bad_artifact", "report manifest must be an object")
    return dict(value)


def _subject_status(run: Mapping[str, Any], report_manifest: Mapping[str, Any]
                    ) -> dict[str, str]:
    explicit = run.get("subject_status")
    if isinstance(explicit, Mapping):
        return {str(key): str(value) for key, value in explicit.items()}
    subjects = report_manifest.get("subjects")
    if isinstance(subjects, Mapping):
        return {
            str(name): str(row.get("status") or "unknown")
            for name, row in subjects.items() if isinstance(row, Mapping)
        }
    return {}


def _run_complete(run: Mapping[str, Any], report_manifest: Mapping[str, Any]) -> bool:
    if run["status"] != "complete":
        return False
    status = report_manifest.get("status")
    return status in (None, "complete")


def _adjudicated_rows(loaded: Mapping[str, Any], manifest: Mapping[str, Any]
                      ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    expected: list[dict[str, Any]] = []
    negatives: list[dict[str, Any]] = []
    unknown: list[dict[str, Any]] = []
    excluded: list[str] = []
    seen_positive: dict[tuple[str, str, str], str] = {}
    seen_negative: dict[tuple[str, str, str], str] = {}
    missing_adjudication: list[str] = []
    for key, group in sorted(loaded["subjects"].items()):
        repository_id, subject_key = key.split("/", 1)
        _repo_by_id(manifest, repository_id)
        _slot_by_key(manifest, repository_id, subject_key)
        adjudication = group.get("adjudication")
        if adjudication is None:
            missing_adjudication.append(key)
            continue
        for decision in adjudication["decisions"]:
            base_subject = str(decision["subject"])
            namespaced = f"{repository_id}::{subject_key}::{base_subject}"
            row = {
                "path": decision["location"]["path"],
                "type": decision["type"],
                "subject": namespaced,
                "severity": decision["severity"],
                "label_id": decision["id"],
                "repository_id": repository_id,
                "subject_key": subject_key,
                "label_subject": base_subject,
                "source_file_digest": decision["source_file_digest"],
                "evidence": decision["evidence"],
                "reviewer_label_ids": decision["reviewer_label_ids"],
                "resolution": decision["resolution"],
                "actionable": decision["actionable"],
            }
            if decision["resolution"] == "excluded":
                excluded.append(f"{key}:{decision['id']}")
                continue
            if decision["verdict"] == "true_positive":
                pair = (row["path"], row["type"], namespaced)
                if pair in seen_positive or pair in seen_negative:
                    raise CorpusError(
                        "duplicate_adjudicated_label",
                        f"adjudication has conflicting labels for {pair} in {key}")
                seen_positive[pair] = decision["id"]
                expected.append(row)
            elif decision["verdict"] == "false_positive":
                pair = (row["path"], row["type"], namespaced)
                if pair in seen_positive or pair in seen_negative:
                    raise CorpusError(
                        "duplicate_adjudicated_label",
                        f"adjudication has conflicting labels for {pair} in {key}")
                seen_negative[pair] = decision["id"]
                negatives.append(row)
            else:
                unknown.append(row)
    details = {
        "missing_adjudication": sorted(missing_adjudication),
        "excluded_decisions": sorted(excluded),
        "negative_labels": len(negatives),
        "unknown_labels": len(unknown),
    }
    return expected, {"details": details, "negatives": negatives, "unknown": unknown}


def _scope_complete(group: Mapping[str, Any]) -> bool:
    summaries = group.get("summary") or {}
    return bool(summaries.get("complete"))


def _label_scope_subjects(group: Mapping[str, Any] | None) -> set[str]:
    """Return every subject the reviewers declared inside a label scope."""
    if group is None:
        return set()
    declared: set[str] = set()
    files = [*(group.get("reviewers") or {}).values(), group.get("adjudication")]
    for item in files:
        if not isinstance(item, Mapping):
            continue
        scope = item.get("declared_scope")
        if isinstance(scope, Mapping):
            declared.update(str(subject) for subject in scope.get("subjects") or [])
    return declared


def _build_scoring_inputs(loaded: Mapping[str, Any], manifest: Mapping[str, Any],
                          record: Mapping[str, Any]) -> dict[str, Any]:
    primary: dict[tuple[str, str], dict[str, Any]] = {}
    repeats: list[dict[str, Any]] = []
    other_conditions: list[dict[str, Any]] = []
    condition = record["scored_condition"]
    for run in record["runs"]:
        key = (run["repository_id"], run["subject_key"])
        _repo_by_id(manifest, run["repository_id"])
        _slot_by_key(manifest, run["repository_id"], run["subject_key"])
        if run["condition"] != condition:
            other_conditions.append(run)
            continue
        if run["repeat_of"] is None:
            if key in primary:
                raise CorpusError(
                    "duplicate_scored_run",
                    f"more than one primary {condition} run for {key[0]}/{key[1]}")
            primary[key] = run
        else:
            repeats.append(run)
    expected, label_details = _adjudicated_rows(loaded, manifest)
    predictions: list[dict[str, Any]] = []
    findings_by_run: dict[str, list[dict[str, Any]]] = {}
    complete_subjects: set[str] = set()
    scored_expected: list[dict[str, Any]] = []
    unavailable_scopes: list[dict[str, Any]] = []
    excluded_expected: list[dict[str, Any]] = []
    scope_rows: list[dict[str, Any]] = []
    source_expected_by_scope: dict[tuple[str, str], int] = {}
    for row in expected:
        key = (row["repository_id"], row["subject_key"])
        source_expected_by_scope[key] = source_expected_by_scope.get(key, 0) + 1
    for key, run in sorted(primary.items()):
        repository_id, subject_key = key
        findings = _load_findings(run)
        findings_by_run[run["run_id"]] = findings
        report_manifest = _load_report_manifest(run)
        run_complete = _run_complete(run, report_manifest)
        statuses = _subject_status(run, report_manifest)
        group = loaded["subjects"].get(f"{repository_id}/{subject_key}")
        scope_is_complete = bool(group and _scope_complete(group))
        label_subjects = _label_scope_subjects(group)
        # Precision can only call unmatched predictions false positives when
        # the entire declared label scope was actually collected.  A run may
        # report some subjects as complete while silently omitting another
        # declared subject; treating those individual predictions as labeled
        # would make scope-level coverage look better than it was.
        run_scope_complete = (
            run_complete and scope_is_complete and bool(label_subjects)
            and label_subjects <= set(statuses)
            and all(statuses[subject] == "complete" for subject in label_subjects)
        )
        scope_predictions = 0
        for finding in findings:
            base_subject = finding["subject"]
            namespaced = f"{repository_id}::{subject_key}::{base_subject}"
            predictions.append({**finding, "subject": namespaced})
            scope_predictions += 1
        if run_scope_complete:
            complete_subjects.update(
                f"{repository_id}::{subject_key}::{subject}"
                for subject in label_subjects)
        reason = None
        if not run_complete:
            reason = f"run status {run['status']!r} did not provide complete coverage"
        elif group is None:
            reason = "no label set for this run"
        elif not scope_is_complete:
            reason = "label scope is partial or unknown"
        if reason is not None:
            unavailable_scopes.append({
                "repository_id": repository_id, "subject_key": subject_key,
                "run_id": run["run_id"], "reason": reason})
        for row in expected:
            if (row["repository_id"], row["subject_key"]) != key:
                continue
            subject_ok = (statuses.get(row["label_subject"]) == "complete"
                          if statuses else run_complete)
            if run_complete and subject_ok:
                scored_expected.append(row)
            else:
                excluded_expected.append(row)
        scope_rows.append({
            "repository_id": repository_id,
            "subject_key": subject_key,
            "run_id": run["run_id"],
            "run_status": run["status"],
            "run_complete": run_complete,
            "run_scope_complete": run_scope_complete,
            "label_scope_complete": scope_is_complete,
            "predicted": scope_predictions,
            "reviewed_positive_labels": source_expected_by_scope.get(key, 0),
            "missing_run": False,
            "subject_statuses": dict(sorted(statuses.items())),
            "label_subjects": sorted(label_subjects),
            "graph_freshness": run["graph_freshness"],
        })
    for key, group in sorted(loaded["subjects"].items()):
        repository_id, subject_key = key.split("/", 1)
        if (repository_id, subject_key) in primary:
            continue
        # Keep complete human label scopes in coverage even when the selected
        # condition has no corresponding run.  Otherwise their expected
        # findings disappear from both recall and the coverage gate, allowing
        # a score over a cherry-picked subset of the frozen corpus.
        if _scope_complete(group):
            unavailable_scopes.append({
                "repository_id": repository_id,
                "subject_key": subject_key,
                "run_id": None,
                "reason": "no scored run for this label scope",
            })
            scope_rows.append({
                "repository_id": repository_id,
                "subject_key": subject_key,
                "run_id": None,
                "run_status": "missing",
                "run_complete": False,
                "run_scope_complete": False,
                "label_scope_complete": True,
                "predicted": 0,
                "reviewed_positive_labels": source_expected_by_scope.get(
                    (repository_id, subject_key), 0),
                "missing_run": True,
                "subject_statuses": {},
                "label_subjects": sorted(_label_scope_subjects(group)),
                "graph_freshness": "unknown",
            })
    return {
        "primary": primary,
        "repeats": repeats,
        "other_conditions": other_conditions,
        "predictions": predictions,
        "findings_by_run": findings_by_run,
        "expected": scored_expected,
        "all_reviewed_expected": expected,
        "excluded_expected": excluded_expected,
        "complete_subjects": complete_subjects,
        "unavailable_scopes": unavailable_scopes,
        "scope_rows": scope_rows,
        "label_details": label_details,
    }


def _verify_source_digests(loaded: Mapping[str, Any], manifest: Mapping[str, Any],
                           checkout_root: str | Path | None) -> dict[str, Any]:
    if checkout_root is None:
        return {"status": "not_checked", "reason": "no --checkout-root supplied",
                "checked": 0, "missing": 0, "mismatched": 0, "not_recorded": 0,
                "mismatches": []}
    checked = 0
    missing = 0
    mismatched = 0
    not_recorded = 0
    mismatches: list[dict[str, Any]] = []
    cache: dict[str, Path] = {}
    for key, group in sorted(loaded["subjects"].items()):
        repository_id, _subject_key = key.split("/", 1)
        adjudication = group.get("adjudication")
        if adjudication is None:
            continue
        if repository_id not in cache:
            cache[repository_id] = checkout_path(
                manifest, repository_id, checkout_root=checkout_root)
        root = cache[repository_id]
        for decision in adjudication["decisions"]:
            digest = decision.get("source_file_digest")
            if not digest:
                not_recorded += 1
                continue
            relative = decision["location"]["path"]
            path = root / relative
            if not path.is_file():
                missing += 1
                if len(mismatches) < 100:
                    mismatches.append({"repository_id": repository_id,
                                       "label_id": decision["id"],
                                       "path": relative, "status": "missing"})
                continue
            actual = _sha256_file(path)
            if actual != digest:
                mismatched += 1
                if len(mismatches) < 100:
                    mismatches.append({"repository_id": repository_id,
                                       "label_id": decision["id"], "path": relative,
                                       "status": "mismatch", "expected": digest,
                                       "actual": actual})
            else:
                checked += 1
    if mismatched:
        status = "mismatch"
    elif missing or not_recorded:
        status = "incomplete"
    elif checked:
        status = "verified"
    else:
        status = "not_checked"
    return {"status": status, "checked": checked, "missing": missing,
            "mismatched": mismatched, "not_recorded": not_recorded,
            "mismatches": mismatches}


def _repeatability(record: Mapping[str, Any], inputs: Mapping[str, Any]) -> dict[str, Any]:
    primary = inputs["primary"]
    repeats = inputs["repeats"]
    if not repeats:
        return {"status": "not_tested", "repeats": [],
                "reason": "no repeat runs were recorded"}
    rows: list[dict[str, Any]] = []
    failed = False
    for repeat in repeats:
        key = (repeat["repository_id"], repeat["subject_key"])
        original = primary.get(key)
        if original is None:
            raise CorpusError(
                "bad_record",
                f"repeat {repeat['run_id']} has no primary run for {key[0]}/{key[1]}")
        if repeat["repeat_of"] != original["run_id"]:
            raise CorpusError(
                "bad_record",
                f"repeat {repeat['run_id']} names {repeat['repeat_of']}, "
                f"expected {original['run_id']}")
        left = inputs["findings_by_run"].get(original["run_id"])
        right = inputs["findings_by_run"].get(repeat["run_id"])
        if left is None:
            left = _load_findings(original)
        if right is None:
            right = _load_findings(repeat)
        left_ids = sorted(str(item.get("id")) for item in left if item.get("id"))
        right_ids = sorted(str(item.get("id")) for item in right if item.get("id"))
        ids_equal = left_ids == right_ids
        left_sha = hashlib.sha256(json.dumps(
            left, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        right_sha = hashlib.sha256(json.dumps(
            right, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        stable = ids_equal and left_sha == right_sha
        if not stable:
            failed = True
        rows.append({"run_id": original["run_id"], "repeat_of": repeat["run_id"],
                     "stable_finding_ids": ids_equal, "stable_content": left_sha == right_sha,
                     "stable": stable, "finding_count": len(left)})
    return {"status": "failed" if failed else "verified", "repeats": rows}


def _operational(record: Mapping[str, Any]) -> dict[str, Any]:
    runs = record["runs"]
    measured = [run for run in runs
                if run.get("budget_s") is not None and run.get("duration_s") is not None]
    within = [run for run in measured if run["duration_s"] <= run["budget_s"]]
    writes = [run["run_id"] for run in runs
              if run.get("writes_outside_allowed_paths") is True]
    failures = [run["run_id"] for run in runs
                if run.get("exit_status") not in (None, 0)]
    return {
        "status": ("not_measured" if not measured else
                   "measured" if len(measured) == len(runs) else "partially_measured"),
        "runs": len(runs),
        "runs_with_budget": len(measured),
        "within_budget": len(within),
        "within_budget_rate": _ratio(len(within), len(measured)),
        "writes_outside_allowed_paths": writes,
        "nonzero_exit_runs": failures,
    }


def _provenance(loaded: Mapping[str, Any]) -> str:
    values = {str(item.get("provenance")) for item in loaded["subjects"].values()}
    if values == {"human"}:
        return "human"
    if values == {"synthetic_test_fixture"}:
        return "synthetic_test_fixture"
    if not values:
        return "unknown"
    return "mixed"


def _coverage(loaded: Mapping[str, Any], manifest: Mapping[str, Any],
              inputs: Mapping[str, Any], summary: Mapping[str, Any]) -> dict[str, Any]:
    manifest_repos = list(manifest.get("repositories") or [])
    slots = list(manifest.get("change_ranges") or [])
    active_slots = [slot for slot in slots if slot.get("status") != "excluded"]
    group_keys = set(loaded["subjects"])
    complete_keys = {key for key, group in loaded["subjects"].items()
                     if _scope_complete(group)}
    partial_keys = {key for key, group in loaded["subjects"].items()
                    if not _scope_complete(group)}
    unlabeled_slots = sorted(
        slot["id"] for slot in active_slots
        if f"{slot['repository_id']}/{slot['id']}" not in group_keys)
    complete_repositories: list[str] = []
    incomplete_repositories: list[str] = []
    scored_complete_keys = {
        f"{row['repository_id']}/{row['subject_key']}"
        for row in inputs["scope_rows"]
        if row.get("run_scope_complete")
        and bool(row.get("label_subjects"))
    }
    for repo in manifest_repos:
        repo_id = repo["id"]
        expected_keys = {"whole-repo"} | {
            slot["id"] for slot in active_slots if slot["repository_id"] == repo_id
        }
        have = {key.split("/", 1)[1] for key in complete_keys
                if key.startswith(repo_id + "/")
                and key in scored_complete_keys}
        if expected_keys <= have:
            complete_repositories.append(repo_id)
        else:
            incomplete_repositories.append(repo_id)
    unavailable = inputs["unavailable_scopes"]
    scope_by_key = {
        f"{row['repository_id']}/{row['subject_key']}": row
        for row in inputs["scope_rows"]
    }
    missing_for_complete = []
    for key in sorted(complete_keys):
        row = scope_by_key.get(key)
        if row is None or key not in scored_complete_keys:
            missing_for_complete.append({
                "repository_id": key.split("/", 1)[0],
                "subject_key": key.split("/", 1)[1],
                "run_id": row.get("run_id") if row else None,
                "reason": ("no scored run for this label scope"
                           if row is None or row.get("missing_run") else
                           "run did not provide complete coverage"),
            })
    stale_runs = [row for row in inputs["scope_rows"]
                  if row.get("graph_freshness") == "stale"]
    return {
        "manifest_repositories": len(manifest_repos),
        "manifest_change_range_slots": len(slots),
        "label_subjects": len(loaded["subjects"]),
        "complete_label_subjects": len(complete_keys),
        "partial_or_unknown_label_subjects": len(partial_keys),
        "label_subject_keys": sorted(group_keys),
        "complete_subject_keys": sorted(complete_keys),
        "partial_or_unknown_subject_keys": sorted(partial_keys),
        "unlabeled_manifest_slots": unlabeled_slots,
        "complete_repositories": sorted(complete_repositories),
        "incomplete_repositories": sorted(incomplete_repositories),
        "complete_repository_count": len(complete_repositories),
        "target_complete_repositories": MIN_COMPLETE_REPOSITORIES,
        "target_repositories": (manifest.get("targets") or {}).get("repositories"),
        "unavailable_scopes": unavailable,
        "missing_runs_for_complete_labels": missing_for_complete,
        "stale_scored_runs": stale_runs,
        "prediction_scopes": inputs["scope_rows"],
        "label_warnings": list(summary.get("warnings") or []),
        "label_errors": list(summary.get("errors") or []),
        "source_digest_status": None,
    }


def _subject_parts(namespaced: str) -> tuple[str, str, str]:
    try:
        repository_id, subject_key, base_subject = namespaced.split("::", 2)
    except ValueError as exc:
        raise CorpusError("bad_subject_key",
                          f"namespaced subject is malformed: {namespaced}") from exc
    return repository_id, subject_key, base_subject


def _aggregate_subjects(score: Mapping[str, Any], base_key: str) -> dict[str, Any]:
    rows = score.get("subjects") or {}
    matching = [(key, row) for key, row in rows.items()
                if _subject_parts(key)[2] == base_key]
    complete_rows = [(key, row) for key, row in matching if row.get("complete")]
    tp_complete = sum(row.get("true_positive") or 0 for _, row in complete_rows)
    fp = sum(row.get("false_positive") or 0 for _, row in complete_rows)
    complete_predicted = sum(row.get("predicted") or 0
                             for _, row in complete_rows)
    tp = sum(row.get("true_positive") or 0 for _, row in matching)
    expected = sum(row.get("expected") or 0 for _, row in matching)
    predicted = sum(row.get("predicted") or 0 for _, row in matching)
    fn = sum(row.get("false_negative") or 0 for _, row in matching)
    return {
        "subject": base_key,
        "complete": bool(complete_rows),
        "repositories": sorted({_subject_parts(key)[0] for key, _ in matching}),
        "scopes": sorted({_subject_parts(key)[1] for key, _ in matching}),
        "expected": expected,
        "predicted": predicted,
        "complete_scope_predicted": complete_predicted,
        "true_positive": tp,
        "false_positive": fp,
        "false_negative": fn,
        "precision": _ratio(tp_complete, tp_complete + fp),
        "recall": _ratio(tp, expected),
        "report_noise_ratio": _ratio(predicted - tp, predicted),
        "prediction_coverage": _ratio(tp_complete + fp, predicted),
    }


def _per_repository(repository_id: str, repo_score: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "repository_id": repository_id,
        "complete_subjects": sorted(
            key for key, row in (repo_score.get("subjects") or {}).items()
            if row.get("complete")),
        "expected": repo_score.get("expected"),
        "predicted": repo_score.get("predicted"),
        "true_positive": repo_score.get("true_positive"),
        "false_positive": repo_score.get("false_positive"),
        "false_negative": repo_score.get("false_negative"),
        "unlabeled": repo_score.get("unlabeled"),
        "locationless": repo_score.get("locationless"),
        "precision": repo_score.get("precision"),
        "recall": repo_score.get("recall"),
        "report_noise_ratio": repo_score.get("report_noise_ratio"),
    }


def _row_pairs(row: Mapping[str, Any]) -> set[tuple[str, str, str]]:
    subject = str(row.get("subject") or "unknown")
    ftype = str(row.get("type") or "unknown")
    paths = {str(row["path"])} if row.get("path") else {
        str(location.get("path"))
        for location in row.get("locations") or []
        if isinstance(location, Mapping) and location.get("path")
    }
    return {(path, ftype, subject) for path in paths}


def _blocking_summary(expected: Iterable[Mapping[str, Any]],
                      predictions: Iterable[Mapping[str, Any]],
                      complete_subjects: set[str]) -> dict[str, Any]:
    blocking_expected = [row for row in expected
                         if row.get("severity") == BLOCKING]
    blocking_predictions = [row for row in predictions
                            if row.get("severity") == BLOCKING]
    expected_pairs = {pair for row in blocking_expected for pair in _row_pairs(row)}
    matched = 0
    unsupported = 0
    for row in blocking_predictions:
        pairs = _row_pairs(row)
        if pairs & expected_pairs:
            matched += 1
        elif row.get("subject") in complete_subjects:
            unsupported += 1
    evidence_ok = all(
        row.get("evidence") and len(row.get("reviewer_label_ids") or {}) >= 2
        for row in blocking_expected)
    return {
        "blocking_expected": len(blocking_expected),
        "blocking_predicted": len(blocking_predictions),
        "blocking_matched": matched,
        "unsupported_blocking": unsupported,
        "blocking_evidence_ok": evidence_ok,
    }


def evaluate_gates(metrics: Mapping[str, Any]) -> dict[str, Any]:
    """Evaluate the proposed gates, returning pass/fail/insufficient per gate."""
    evidence_class = metrics.get("evidence_class")
    provenance = metrics.get("provenance")
    if evidence_class != REALISTIC:
        return {
            "status": "not_applicable_synthetic",
            "quality_claim": "none",
            "gates": {
                key: {"status": "not_applicable_synthetic",
                      "reason": "synthetic or non-realistic evidence cannot satisfy "
                                "realistic-corpus quality gates"}
                for key in ("deterministic_report_quality", "high_impact_findings",
                            "subject_stability", "noise", "repeatability_and_coverage",
                            "operational_bounds", "human_utility")
            },
        }
    if provenance != "human":
        return {
            "status": "insufficient_data",
            "quality_claim": "none",
            "gates": {
                key: {"status": "insufficient_data",
                      "reason": "realistic-corpus gates require human-provenance labels"}
                for key in ("deterministic_report_quality", "high_impact_findings",
                            "subject_stability", "noise", "repeatability_and_coverage",
                            "operational_bounds", "human_utility")
            },
        }
    overall = metrics.get("overall") or {}
    precision = overall.get("precision")
    recall = overall.get("recall")
    coverage = metrics.get("coverage") or {}
    high = metrics.get("high_impact") or {}
    noise = overall.get("report_noise_ratio_complete_scope")
    repeatability = metrics.get("repeatability") or {}
    digest_checks = metrics.get("digest_checks") or {}
    operational = metrics.get("operational") or {}
    subject_rows = metrics.get("subject_stability") or []
    gates: dict[str, Any] = {}

    complete_repositories = int(coverage.get("complete_repository_count") or 0)
    if precision is None or recall is None:
        gates["deterministic_report_quality"] = {
            "status": "insufficient_data",
            "reason": "precision or recall has no labeled denominator"}
    else:
        passed = (precision >= PRECISION_THRESHOLD and recall >= RECALL_THRESHOLD
                  and complete_repositories >= MIN_COMPLETE_REPOSITORIES)
        gates["deterministic_report_quality"] = {
            "status": "pass" if passed else "fail",
            "threshold": {"precision": PRECISION_THRESHOLD,
                          "recall": RECALL_THRESHOLD,
                          "complete_repositories": MIN_COMPLETE_REPOSITORIES},
            "observed": {"precision": precision, "recall": recall,
                         "complete_repositories": complete_repositories},
            "raw": {"true_positive": overall.get("true_positive"),
                    "false_positive": overall.get("false_positive"),
                    "expected": overall.get("expected"),
                    "predicted": overall.get("predicted")},
        }

    high_precision = high.get("precision")
    if (high.get("unsupported_blocking", 0)
            or high.get("blocking_evidence_ok") is not True):
        gates["high_impact_findings"] = {
            "status": "fail",
            "observed": {"precision": high_precision,
                         "unsupported_blocking": high.get("unsupported_blocking"),
                         "blocking_evidence_ok": high.get("blocking_evidence_ok")},
            "reason": "an unsupported blocking finding or missing blocking evidence"}
    elif high_precision is None:
        gates["high_impact_findings"] = {
            "status": "insufficient_data",
            "reason": "no high-impact predictions in complete scopes"}
    else:
        gates["high_impact_findings"] = {
            "status": "pass" if high_precision >= HIGH_IMPACT_PRECISION_THRESHOLD
            else "fail",
            "threshold": HIGH_IMPACT_PRECISION_THRESHOLD,
            "observed": {"precision": high_precision},
            "raw": {"true_positive": high.get("true_positive"),
                    "false_positive": high.get("false_positive"),
                    "predicted": high.get("predicted")},
        }

    qualifying = [row for row in subject_rows
                  if isinstance(row.get("complete_scope_predicted"), int)
                  and not isinstance(row.get("complete_scope_predicted"), bool)
                  and row["complete_scope_predicted"] >= SUBJECT_PREDICTION_MINIMUM]
    weak = [row for row in qualifying
            if row.get("precision") is None
            or float(row["precision"]) < SUBJECT_PRECISION_FLOOR]
    if not qualifying:
        gates["subject_stability"] = {
            "status": "insufficient_data",
            "observed": {"qualifying": 0,
                         "prediction_scope": "complete label scopes only"},
            "reason": f"no subject has at least {SUBJECT_PREDICTION_MINIMUM} predictions"}
    else:
        gates["subject_stability"] = {
            "status": "pass" if not weak else "fail",
            "threshold": {"minimum_predictions": SUBJECT_PREDICTION_MINIMUM,
                          "precision_floor": SUBJECT_PRECISION_FLOOR},
            "observed": {
                "qualifying": len(qualifying),
                "weak": weak,
                "prediction_scope": "complete label scopes only",
            },
        }

    if noise is None:
        gates["noise"] = {"status": "insufficient_data",
                          "reason": "no predictions; noise is undefined"}
    elif not coverage.get("complete_label_subjects"):
        gates["noise"] = {
            "status": "insufficient_data",
            "reason": "no complete label scope; noise is not measurable on labeled scope"}
    else:
        gates["noise"] = {
            "status": "pass" if noise <= NOISE_THRESHOLD else "fail",
            "threshold": NOISE_THRESHOLD, "observed": noise,
            "raw": {"unmatched": overall.get("unmatched"),
                    "predicted": overall.get("predicted")},
        }

    repeat_status = repeatability.get("status")
    digest_status = digest_checks.get("status")
    if repeat_status != "verified" or digest_status != "verified":
        gates["repeatability_and_coverage"] = {
            "status": "insufficient_data",
            "observed": {"repeatability": repeat_status, "digests": digest_status},
            "reason": "repeat runs or pinned input/source digests are incomplete"}
    else:
        coverage_issues = (bool(coverage.get("label_errors"))
                           or bool(coverage.get("missing_runs_for_complete_labels"))
                           or bool(coverage.get("stale_scored_runs")))
        gates["repeatability_and_coverage"] = {
            "status": "fail" if coverage_issues else "pass",
            "observed": {"repeatability": repeat_status, "digests": digest_status,
                         "missing_complete_label_runs": len(
                             coverage.get("missing_runs_for_complete_labels") or []),
                         "stale_scored_runs": len(
                             coverage.get("stale_scored_runs") or [])},
        }

    if operational.get("status") != "measured":
        gates["operational_bounds"] = {
            "status": "insufficient_data",
            "observed": {"runs": operational.get("runs"),
                         "runs_with_budget": operational.get("runs_with_budget")},
            "reason": "run durations/budgets were not recorded for every run"}
    elif operational.get("writes_outside_allowed_paths"):
        gates["operational_bounds"] = {
            "status": "fail", "observed": operational,
            "reason": "a run wrote outside the allowed report/state paths"}
    elif operational.get("nonzero_exit_runs"):
        gates["operational_bounds"] = {
            "status": "fail", "observed": operational,
            "reason": "a run exited with a nonzero status"}
    else:
        rate = operational.get("within_budget_rate")
        gates["operational_bounds"] = {
            "status": "pass" if rate is not None and rate >= OPERATIONAL_PASS_RATE
            else "fail",
            "threshold": OPERATIONAL_PASS_RATE,
            "observed": {"within_budget_rate": rate,
                         "runs": operational.get("runs")},
        }

    human = metrics.get("human_utility") or {}
    if human.get("status") != "measured":
        gates["human_utility"] = {
            "status": "not_measured",
            "reason": "requires a separate blinded top-finding sample"}
    else:
        rate = human.get("actionability_rate")
        gates["human_utility"] = {
            "status": "pass" if rate is not None and rate >= HUMAN_UTILITY_THRESHOLD
            else "fail",
            "threshold": HUMAN_UTILITY_THRESHOLD,
            "observed": {"actionability_rate": rate,
                         "sample_size": human.get("sample_size")},
        }

    statuses = {gate["status"] for gate in gates.values()}
    if "fail" in statuses:
        overall_status = "no_go"
    elif statuses <= {"pass"}:
        overall_status = "gate_met"
    else:
        overall_status = "insufficient_data"
    return {
        "status": overall_status,
        "quality_claim": ("gate_met_not_universal_correctness"
                          if overall_status == "gate_met" else "none"),
        "gates": gates,
    }


def _provenance(loaded: Mapping[str, Any]) -> str:
    values = {str((item.get("summary") or {}).get("provenance") or "unknown")
              for item in (loaded.get("subjects") or {}).values()}
    if values == {"human"}:
        return "human"
    if "synthetic_test_fixture" in values:
        return "synthetic_test_fixture"
    if not values:
        return "unknown"
    return "mixed"


def score_corpus(manifest: Mapping[str, Any], labels_dir: str | Path,
                 record: Mapping[str, Any], *,
                 checkout_root: str | Path | None = None,
                 require_realistic: bool = False) -> dict[str, Any]:
    """Score one frozen run record against one label directory."""
    if require_realistic and record["evidence_class"] != REALISTIC:
        raise CorpusError("pooling_refused",
                          "--require-realistic refuses synthetic evidence")
    if record["corpus_id"] != manifest.get("corpus_id"):
        raise CorpusError(
            "corpus_mismatch",
            "run record corpus_id does not match the manifest",
            details={"record": record["corpus_id"],
                     "manifest": manifest.get("corpus_id")})
    if record["evidence_class"] != manifest.get("evidence_class"):
        raise CorpusError(
            "evidence_class_mismatch",
            "run record evidence_class does not match the manifest",
            details={"record": record["evidence_class"],
                     "manifest": manifest.get("evidence_class")})
    try:
        loaded = load_label_set(
            labels_dir, manifest,
            require_complete=False,
            require_human=record["evidence_class"] == REALISTIC,
            require_sealed=True)
    except (LabelError, ValueError) as exc:
        raise CorpusError("invalid_labels", str(exc)) from exc
    summary = loaded["summary"]
    if summary.get("status") == "invalid":
        raise CorpusError("invalid_labels", "label set is invalid",
                          details={"errors": summary.get("errors")})
    actual_manifest_digest = manifest.get("digest") or manifest_digest(manifest)
    actual_labels_digest = summary.get("digest")
    if record.get("manifest_digest") is not None \
            and record["manifest_digest"] != actual_manifest_digest:
        raise CorpusError(
            "manifest_digest_mismatch",
            "manifest digest does not match the run record",
            details={"expected": record["manifest_digest"],
                     "actual": actual_manifest_digest})
    if record.get("labels_digest") is not None \
            and record["labels_digest"] != actual_labels_digest:
        raise CorpusError(
            "labels_digest_mismatch",
            "labels digest does not match the run record",
            details={"expected": record["labels_digest"],
                     "actual": actual_labels_digest})
    inputs = _build_scoring_inputs(loaded, manifest, record)
    predictions = inputs["predictions"]
    expected = inputs["expected"]
    complete = inputs["complete_subjects"]
    overall = score_findings(predictions, expected, complete_subjects=complete)
    high_predictions = [row for row in predictions
                        if row.get("severity") in HIGH_IMPACT_SEVERITIES]
    high_expected = [row for row in expected
                     if row.get("severity") in HIGH_IMPACT_SEVERITIES]
    high = score_findings(high_predictions, high_expected,
                          complete_subjects=complete)
    blocking = _blocking_summary(expected, predictions, complete)
    high.update(blocking)
    per_subject = [
        _aggregate_subjects(overall, base_subject)
        for base_subject in sorted({_subject_parts(key)[2]
                                    for key in (overall.get("subjects") or {})})
    ]
    per_subject.sort(key=lambda row: row["subject"])
    labeled_predictions = sum(
        row.get("predicted") or 0 for row in (overall.get("subjects") or {}).values()
        if row.get("complete"))
    locationless = int(overall.get("locationless") or 0)
    unlabeled_predictions = int(overall.get("predicted") or 0) - labeled_predictions
    unlabeled_predictions += locationless
    complete_true_positive = sum(
        row.get("true_positive") or 0
        for row in (overall.get("subjects") or {}).values()
        if row.get("complete"))
    complete_false_positive = int(overall.get("false_positive") or 0)
    complete_noise = (_ratio(complete_false_positive, labeled_predictions)
                      if labeled_predictions else None)
    overall["report_noise_ratio_complete_scope"] = complete_noise
    coverage = _coverage(loaded, manifest, inputs, summary)
    primary_runs = list(inputs["primary"].values())
    pinned_reports = [run for run in primary_runs if run.get("report_sha256") is not None]
    if pinned_reports and len(pinned_reports) == len(primary_runs):
        report_status = "verified"
    elif pinned_reports:
        report_status = "incomplete"
    else:
        report_status = "not_checked"
    source_files = _verify_source_digests(loaded, manifest, checkout_root)
    digest_checks = {
        "manifest_digest": actual_manifest_digest,
        "manifest_pin": ("verified" if record.get("manifest_digest") is not None
                         else "not_pinned"),
        "labels_digest": actual_labels_digest,
        "labels_pin": ("verified" if record.get("labels_digest") is not None
                       else "not_pinned"),
        "record_digest": record.get("digest"),
        "report_digest": report_status,
        "report_pins": {"expected": len(pinned_reports),
                        "scored_runs": len(primary_runs)},
        "source_files": source_files,
    }
    statuses = [digest_checks["manifest_pin"], digest_checks["labels_pin"],
                report_status, source_files["status"]]
    if "mismatch" in statuses:
        digest_checks["status"] = "mismatch"
    elif all(value == "verified" for value in statuses):
        digest_checks["status"] = "verified"
    elif all(value in {"not_checked", "not_pinned"} for value in statuses):
        digest_checks["status"] = "not_checked"
    else:
        digest_checks["status"] = "incomplete"
    coverage["source_digest_status"] = source_files["status"]
    coverage["report_digest_status"] = report_status
    coverage["manifest_pin"] = digest_checks["manifest_pin"]
    coverage["labels_pin"] = digest_checks["labels_pin"]
    repeatability = _repeatability(record, inputs)
    operational = _operational(record)
    reviewer_summary = {
        "status": summary.get("status"),
        "counts": summary.get("counts"),
        "subjects": summary.get("subjects"),
        "warnings": summary.get("warnings"),
        "errors": summary.get("errors"),
        "provenance": _provenance(loaded),
    }
    raw_counts = {
        "reviewed_positive_labels": len(inputs["all_reviewed_expected"]),
        "scored_reviewed_positive_labels": len(expected),
        "excluded_reviewed_labels": len(inputs["excluded_expected"]),
        "predicted_pairs": overall.get("predicted"),
        "true_positive": overall.get("true_positive"),
        "true_positive_in_complete_scopes": complete_true_positive,
        "false_positive": overall.get("false_positive"),
        "false_negative": overall.get("false_negative"),
        "unlabeled_predictions": unlabeled_predictions,
        "labeled_predictions": labeled_predictions,
        "locationless_predictions": locationless,
        "negative_reviewed_labels": inputs["label_details"]["details"][
            "negative_labels"],
        "unknown_reviewed_labels": inputs["label_details"]["details"][
            "unknown_labels"],
        "precision_numerator": complete_true_positive,
        "precision_denominator": complete_true_positive
        + (overall.get("false_positive") or 0),
        "recall_denominator": overall.get("expected"),
        "noise_denominator": overall.get("predicted"),
        "noise_on_complete_scope_numerator": complete_false_positive,
        "noise_on_complete_scope_denominator": labeled_predictions,
    }
    gate_metrics = {
        "evidence_class": record["evidence_class"],
        "provenance": reviewer_summary["provenance"],
        "overall": overall,
        "high_impact": high,
        "coverage": coverage,
        "repeatability": repeatability,
        "digest_checks": digest_checks,
        "operational": operational,
        "subject_stability": per_subject,
        "human_utility": record.get("human_utility") or {"status": "not_measured"},
    }
    gates = evaluate_gates(gate_metrics)
    per_repository = []
    for repo in manifest.get("repositories") or []:
        repo_id = repo["id"]
        repo_score = score_findings(
            [row for row in predictions if row["subject"].startswith(repo_id + "::")],
            [row for row in expected if row["subject"].startswith(repo_id + "::")],
            complete_subjects={key for key in complete if key.startswith(repo_id + "::")})
        item = _per_repository(repo_id, repo_score)
        item["status"] = ("scored" if (repo_score.get("subjects")
                                       or repo_score.get("predicted")
                                       or repo_score.get("expected"))
                          else "no_predictions_or_labels")
        per_repository.append(item)
    pooling = {
        "evidence_class": record["evidence_class"],
        "synthetic_fixtures_included": record["evidence_class"] != REALISTIC,
        "separate_from_realistic": record["evidence_class"] != REALISTIC,
        "note": ("Realistic-corpus scores never pool synthetic fixture results; "
                 "synthetic runs are pipeline regressions only."),
    }
    limitations = [
        "Reviewed labels are a finite sample; recall is relative to reviewed labels, "
        "not to all defects in the repository.",
        "False-positive rate is undefined because labels do not enumerate all actual negatives.",
        "Per-subject precision is reported only for complete scopes.",
        "Synthetic/fixture runs are regression checks and cannot support a quality claim.",
    ]
    if record["evidence_class"] != REALISTIC:
        limitations.append("This record is synthetic; all quality gates are not applicable.")
    if not inputs["repeats"]:
        limitations.append("No repeat run was recorded, so deterministic repeatability is untested.")
    if checkout_root is None:
        limitations.append("Source-file digests were not verified because no checkout root was supplied.")
    return {
        "schema_version": SCORE_SCHEMA_VERSION,
        "scope": ("realistic deep-review corpus scoring" if record["evidence_class"] == REALISTIC
                  else "synthetic regression scoring; not quality evidence"),
        "status": "scored" if record["evidence_class"] == REALISTIC else "synthetic_regression",
        "corpus_id": manifest.get("corpus_id"),
        "evidence_class": record["evidence_class"],
        "scored_condition": record["scored_condition"],
        "quality_claim": gates["quality_claim"],
        "gate_status": gates["status"],
        "pooling": pooling,
        "manifest_digest": digest_checks["manifest_digest"],
        "labels_digest": digest_checks["labels_digest"],
        "record_digest": digest_checks["record_digest"],
        "counts": raw_counts,
        "overall": overall,
        "high_impact": high,
        "labeled_vs_unlabeled": {
            "labeled_predictions": labeled_predictions,
            "unlabeled_predictions": unlabeled_predictions,
            "locationless_predictions": locationless,
            "scored_reviewed_labels": len(expected),
            "excluded_reviewed_labels": len(inputs["excluded_expected"]),
            "note": ("Precision uses complete scopes only. Predictions in partial or "
                     "unlabeled scopes stay in the noise denominator and are never "
                     "silently counted as true or false positives."),
        },
        "coverage": coverage,
        "reviewers": reviewer_summary,
        "per_repository": per_repository,
        "per_subject": per_subject,
        "per_subject_key": inputs["scope_rows"],
        "digest_checks": digest_checks,
        "repeatability": repeatability,
        "operational": operational,
        "other_conditions": [
            {"run_id": run["run_id"], "condition": run["condition"],
             "repository_id": run["repository_id"], "subject_key": run["subject_key"]}
            for run in inputs["other_conditions"]
        ],
        "gates": gates,
        "limitations": limitations,
        "metric_definitions": dict(SCORE_METRIC_DEFINITIONS),
    }


def _write_json(value: Any, out: str | Path | None) -> None:
    text = json.dumps(value, indent=2, sort_keys=True) + "\n"
    if out is None:
        print(text, end="")
    else:
        path = Path(out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--labels", type=Path, default=DEFAULT_LABELS_DIR)
    parser.add_argument("--record", type=Path, required=True)
    parser.add_argument("--checkout-root", type=Path)
    parser.add_argument("--require-realistic", action="store_true")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    try:
        manifest = load_manifest(args.manifest)
        record = load_run_record(args.record)
        result = score_corpus(
            manifest, args.labels, record, checkout_root=args.checkout_root,
            require_realistic=args.require_realistic)
        _write_json(result, args.out)
    except (CorpusError, LabelError, ValueError, OSError) as exc:
        payload = (exc.to_dict() if isinstance(exc, CorpusError)
                   else {"schema_version": SCORE_SCHEMA_VERSION, "status": "error",
                         "reason": "invalid_input", "error": str(exc)})
        print(json.dumps(payload, indent=2, sort_keys=True), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
