"""Reviewed-label loading and deterministic corpus scoring for deep review.

The scoring in this module is deliberately narrow.  A label is a reviewed
``(path, finding type)`` expectation; a prediction is one reported
``(path, finding type, subject)`` pair.  Findings that carry no location cannot
be matched and are counted separately.  When the caller says a subject is
completely labelled, an unmatched prediction in that subject is a false
positive.  When it does not, the prediction is ``unlabeled`` instead: the
report may be noisy, but the corpus has no basis for calling it wrong.

Nothing here invokes deep review, git, a model or the network.  The output is a
JSON-safe value with explicit metric definitions and ``None`` for a metric
whose denominator is absent.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Mapping

LABEL_SCHEMA_VERSION = 2

METRIC_DEFINITIONS = {
    "precision": (
        "true positives / predictions whose subject has complete reviewed "
        "labels; predictions outside a completely labelled subject are not "
        "counted"
    ),
    "recall": "matched reviewed labels / reviewed labels",
    "false_positive_rate": (
        "false positives / all actual negatives; unavailable because the "
        "reviewed labels do not enumerate a negative universe"
    ),
    "false_discovery_rate": (
        "false positives / predictions in completely labelled subjects"
    ),
    "report_noise_ratio": (
        "reported (path, type, subject) pairs with no reviewed label / all "
        "reported pairs; unlabeled subjects contribute to noise but not to "
        "false positives"
    ),
}


def _text(value: Any, field: str, *, required: bool = True) -> str | None:
    if value is None:
        if required:
            raise ValueError(f"{field} must be a non-empty string")
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _relative_path(value: Any, field: str) -> str:
    text = _text(value, field)
    assert text is not None
    path = Path(text)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"{field} must stay inside the fixture: {text}")
    return path.as_posix()


def _string_list(value: Any, field: str) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a list of strings")
    result: list[str] = []
    for index, item in enumerate(value):
        text = _text(item, f"{field}[{index}]")
        assert text is not None
        if text not in result:
            result.append(text)
    return result


def _optional_string_list(value: Any, field: str) -> list[str]:
    if value is None:
        return []
    return _string_list(value, field)


def _coverage_fields(item: Mapping[str, Any], field: str) -> dict[str, Any]:
    checks = item.get("required_checks")
    if checks is not None:
        checks = _string_list(checks, f"{field}.required_checks")
        if not checks:
            raise ValueError(f"{field}.required_checks must not be empty")
    requires_graph = item.get("requires_graph", False)
    if not isinstance(requires_graph, bool):
        raise ValueError(f"{field}.requires_graph must be a boolean")
    baseline_checks = item.get("baseline_required_checks")
    if baseline_checks is not None:
        baseline_checks = _string_list(
            baseline_checks, f"{field}.baseline_required_checks")
        if not baseline_checks:
            raise ValueError(f"{field}.baseline_required_checks must not be empty")
    baseline_graph = item.get("baseline_requires_graph", False)
    if not isinstance(baseline_graph, bool):
        raise ValueError(f"{field}.baseline_requires_graph must be a boolean")
    return {"required_checks": checks, "requires_graph": requires_graph,
            "baseline_required_checks": baseline_checks,
            "baseline_requires_graph": baseline_graph}


def _expectation_rows(value: Any, field: str) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a list")
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str | None]] = set()
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise ValueError(f"{field}[{index}] must be an object")
        path = _relative_path(item.get("file"), f"{field}[{index}].file")
        ftype = _text(item.get("type"), f"{field}[{index}].type")
        assert ftype is not None
        subject = _text(item.get("subject"), f"{field}[{index}].subject",
                        required=False)
        coverage = _coverage_fields(item, f"{field}[{index}]")
        key = (path, ftype, subject)
        if key in seen:
            raise ValueError(f"duplicate reviewed label: {path} {ftype} {subject}")
        seen.add(key)
        label_id = _text(item.get("id"), f"{field}[{index}].id", required=False)
        rows.append({"id": label_id or f"{field}-{index}", "path": path,
                     "type": ftype, "subject": subject, **coverage})
    return rows


def _range_changes(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("labels.range.changes must be a list")
    changes: list[dict[str, Any]] = []
    paths: set[str] = set()
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise ValueError(f"labels.range.changes[{index}] must be an object")
        path = _relative_path(item.get("file"),
                              f"labels.range.changes[{index}].file")
        if path in paths:
            raise ValueError(f"duplicate range change file: {path}")
        paths.add(path)
        has_append = "append" in item
        has_content = "content" in item
        if has_append == has_content:
            raise ValueError(
                f"labels.range.changes[{index}] needs exactly one of append or content")
        text = item["append"] if has_append else item["content"]
        if not isinstance(text, str):
            raise ValueError(
                f"labels.range.changes[{index}].{'append' if has_append else 'content'} "
                "must be a string")
        change = {"file": path, "append" if has_append else "content": text}
        changes.append(change)
    return changes


def load_labels(path: str | Path) -> dict[str, Any]:
    """Load and validate a deep-review labels file.

    Missing files, invalid JSON and malformed rows raise ``ValueError``; a
    caller should report that instead of scoring an empty corpus.
    """
    labels_path = Path(path)
    if labels_path.is_dir():
        labels_path = labels_path / "labels.json"
    if not labels_path.is_file():
        raise ValueError(f"deep-review labels missing: {labels_path}")
    try:
        raw = json.loads(labels_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid deep-review labels JSON in {labels_path}: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise ValueError(f"deep-review labels must be an object: {labels_path}")

    schema = raw.get("schema_version", 1)
    if isinstance(schema, bool) or not isinstance(schema, int):
        raise ValueError("labels.schema_version must be an integer")
    cases_value = raw.get("cases")
    if not isinstance(cases_value, list):
        raise ValueError("labels.cases must be a list")
    cases: list[dict[str, Any]] = []
    case_ids: set[str] = set()
    for index, case in enumerate(cases_value):
        if not isinstance(case, Mapping):
            raise ValueError(f"labels.cases[{index}] must be an object")
        case_id = _text(case.get("id"), f"labels.cases[{index}].id")
        assert case_id is not None
        if case_id in case_ids:
            raise ValueError(f"duplicate labels case id: {case_id}")
        case_ids.add(case_id)
        path = _relative_path(case.get("file"), f"labels.cases[{index}].file")
        subject = _text(case.get("subject"), f"labels.cases[{index}].subject",
                        required=False)
        expected = _string_list(case.get("expected"),
                                f"labels.cases[{index}].expected")
        negatives = _optional_string_list(
            case.get("negatives"), f"labels.cases[{index}].negatives")
        coverage = _coverage_fields(case, f"labels.cases[{index}]")
        cases.append({"id": case_id, "file": path, "subject": subject,
                      "expected": expected, "negatives": negatives,
                      **coverage})

    report_findings = _expectation_rows(raw.get("report_findings"),
                                        "labels.report_findings")
    complete_subjects = _optional_string_list(
        raw.get("complete_subjects"), "labels.complete_subjects")

    range_value = raw.get("range")
    range_data = None
    if range_value is not None:
        if not isinstance(range_value, Mapping):
            raise ValueError("labels.range must be an object")
        changes = _range_changes(range_value.get("changes"))
        expected = _expectation_rows(range_value.get("expected"),
                                     "labels.range.expected")
        range_subjects = _optional_string_list(
            range_value.get("complete_subjects"),
            "labels.range.complete_subjects")
        if not changes:
            raise ValueError("labels.range.changes must contain at least one change")
        if not expected:
            raise ValueError("labels.range.expected must contain at least one label")
        range_data = {"description": range_value.get("description"),
                      "changes": changes, "expected": expected,
                      "complete_subjects": range_subjects}

    return {
        "schema_version": schema,
        "description": raw.get("description"),
        "cases": cases,
        "report_findings": report_findings,
        "complete_subjects": complete_subjects,
        "range": range_data,
    }


def label_metadata(manifest: Mapping[str, Any], path: str | Path | None = None) -> dict[str, Any]:
    range_data = manifest.get("range")
    return {
        "file": str(path) if path is not None else None,
        "schema_version": manifest.get("schema_version"),
        "cases": len(manifest.get("cases") or []),
        "report_findings": len(manifest.get("report_findings") or []),
        "range_findings": len((range_data or {}).get("expected") or []),
        "complete_subjects": sorted(manifest.get("complete_subjects") or []),
    }


def expected_findings(manifest: Mapping[str, Any], *, include_cases: bool = True,
                      include_report: bool = True,
                      include_range: bool = False) -> list[dict[str, Any]]:
    """Return the reviewed labels as ``path``, ``type`` and ``subject`` rows."""
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str | None]] = set()

    def add(path: str, ftype: str, subject: str | None, source: str,
            coverage: Mapping[str, Any] | None = None) -> None:
        key = (path, ftype, subject)
        if key in seen:
            raise ValueError(f"duplicate reviewed label across sections: {path} {ftype} {subject}")
        seen.add(key)
        rows.append({"path": path, "type": ftype, "subject": subject,
                     "source": source,
                     **(dict(coverage) if coverage else {
                         "required_checks": None, "requires_graph": False})})

    if include_cases:
        for case in manifest.get("cases") or []:
            for ftype in case.get("expected") or []:
                add(case["file"], ftype, case.get("subject"), f"case:{case['id']}",
                    case)
    if include_report:
        for item in manifest.get("report_findings") or []:
            add(item["path"], item["type"], item.get("subject"),
                f"report:{item['id']}", item)
    if include_range:
        range_data = manifest.get("range") or {}
        for item in range_data.get("expected") or []:
            add(item["path"], item["type"], item.get("subject"),
                f"range:{item['id']}", item)
    return rows


def _finding_rows(findings: Iterable[Mapping[str, Any]]) -> tuple[dict[tuple[str, str, str], dict[str, Any]], list[dict[str, Any]]]:
    rows: dict[tuple[str, str, str], dict[str, Any]] = {}
    locationless: list[dict[str, Any]] = []
    for finding in findings:
        if not isinstance(finding, Mapping):
            continue
        subject = str(finding.get("subject") or "unknown")
        ftype = str(finding.get("type") or "unknown")
        paths = sorted({
            str(location.get("path"))
            for location in finding.get("locations") or []
            if isinstance(location, Mapping) and location.get("path")
        })
        if not paths:
            locationless.append({"id": finding.get("id"), "type": ftype,
                                 "subject": subject})
            continue
        for path in paths:
            key = (path, ftype, subject)
            row = rows.setdefault(key, {"path": path, "type": ftype,
                                        "subject": subject, "ids": []})
            finding_id = finding.get("id")
            if isinstance(finding_id, str) and finding_id not in row["ids"]:
                row["ids"].append(finding_id)
    return rows, locationless


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def score_findings(findings: Iterable[Mapping[str, Any]],
                   expected: Iterable[Mapping[str, Any]], *,
                   complete_subjects: Iterable[str] | None = None,
                   all_subjects: Iterable[str] | None = None) -> dict[str, Any]:
    """Score reported findings as unique ``(path, type, subject)`` pairs.

    ``complete_subjects`` lists subjects whose reviewed labels are exhaustive
    for the corpus.  Unmatched predictions in those subjects become false
    positives.  Elsewhere they are ``unlabeled`` and only contribute to noise.
    ``all_subjects`` adds report subjects with no labels or predictions so their
    metrics are still shown as unknown rather than omitted.
    """
    expected_rows = [dict(row) for row in expected]
    complete = {str(subject) for subject in (complete_subjects or [])}
    actual_rows, locationless = _finding_rows(findings)
    matched_expected: set[int] = set()
    matched_actual: set[tuple[str, str, str]] = set()
    ambiguous_complete: set[str] = set()
    ambiguous_subjects: set[str] = set()
    checked_wildcards: set[tuple[str, str]] = set()

    # Reserve exact-subject predictions before matching subjectless labels.
    # Otherwise label order can consume the sole prediction available to a
    # more specific label even when a complete one-to-one matching exists.
    order = sorted(range(len(expected_rows)),
                   key=lambda index: expected_rows[index].get("subject") is None)
    for index in order:
        label = expected_rows[index]
        wanted_subject = label.get("subject")
        group = (label["path"], label["type"])
        if wanted_subject is None and group not in checked_wildcards:
            checked_wildcards.add(group)
            available = [key for key in actual_rows
                         if key[:2] == group and key not in matched_actual]
            wildcards = sum(1 for row in expected_rows
                            if row.get("subject") is None
                            and (row["path"], row["type"]) == group)
            # If some but not all of these interchangeable predictions can
            # match, subject attribution is not established by the labels.
            if 0 < wildcards < len(available) and len({key[2] for key in available}) > 1:
                ambiguous_subjects.update(key[2] for key in available)
                if any(key[2] not in complete for key in available):
                    ambiguous_complete.update(key[2] for key in available
                                              if key[2] in complete)
        candidates = sorted(
            key for key in actual_rows
            if key[0] == label["path"] and key[1] == label["type"]
            and (wanted_subject is None or key[2] == wanted_subject)
        )
        for key in candidates:
            if key not in matched_actual:
                matched_actual.add(key)
                matched_expected.add(index)
                break

    declared_complete = set(complete)
    complete -= ambiguous_complete

    def subject_row(subject: str) -> dict[str, Any]:
        subject_expected = [
            index for index, label in enumerate(expected_rows)
            if (label.get("subject") or "unknown") == subject
        ]
        subject_actual = [key for key in actual_rows if key[2] == subject]
        matched_labels = sum(1 for index in subject_expected if index in matched_expected)
        # Subjectless labels can match a finding in any subject. Attribute a
        # precision true positive to the finding's subject, not to "unknown".
        tp = sum(1 for key in subject_actual if key in matched_actual)
        fn = len(subject_expected) - matched_labels
        ambiguous = subject in ambiguous_subjects
        is_complete = subject in complete and not ambiguous
        unmatched = [key for key in subject_actual if key not in matched_actual]
        fp = len(unmatched) if is_complete else None
        predicted = len(subject_actual)
        precision = _ratio(tp, predicted) if is_complete else None
        recall = (_ratio(matched_labels, len(subject_expected))
                  if subject_expected else None)
        fdr = _ratio(fp or 0, predicted) if is_complete and predicted else None
        noise = _ratio(len(unmatched), predicted) if predicted and not ambiguous else None
        notes: list[str] = []
        if not subject_expected:
            notes.append("no reviewed labels for this subject")
        if ambiguous:
            notes.append("subjectless labels match predictions in multiple subjects")
        elif not is_complete:
            notes.append("subject is not marked as completely labelled")
        if not predicted:
            notes.append("no predictions in this subject")
        return {
            "complete": is_complete,
            "expected": len(subject_expected),
            "predicted": predicted,
            "true_positive": None if ambiguous else tp,
            "false_positive": fp,
            "false_negative": fn,
            "unmatched": None if ambiguous else len(unmatched),
            "precision": precision,
            "recall": recall,
            "false_positive_rate": None,
            "false_discovery_rate": fdr,
            "report_noise_ratio": noise,
            "notes": notes,
        }

    subjects = sorted(
        complete
        | {str(label.get("subject") or "unknown") for label in expected_rows}
        | {key[2] for key in actual_rows}
        | {str(subject) for subject in (all_subjects or [])}
    )
    subject_rows = {subject: subject_row(subject) for subject in subjects}

    tp_total = len(matched_expected)
    expected_total = len(expected_rows)
    predicted_total = len(actual_rows)
    complete_tp_total = sum(1 for key in matched_actual if key[2] in complete)
    fp_total = sum(
        1 for key in actual_rows
        if key not in matched_actual and key[2] in complete
    )
    unlabeled_total = sum(
        1 for key in actual_rows
        if key not in matched_actual and key[2] not in complete
    )
    unmatched_total = fp_total + unlabeled_total
    unmatched_preview = [
        {"path": key[0], "type": key[1], "subject": key[2],
         "ids": sorted(actual_rows[key]["ids"])}
        for key in sorted(actual_rows) if key not in matched_actual
    ]

    return {
        "expected": expected_total,
        "predicted": predicted_total,
        "true_positive": tp_total,
        "false_positive": fp_total if complete else None,
        "false_negative": expected_total - tp_total,
        "unlabeled": unlabeled_total,
        "locationless": len(locationless),
        "locationless_findings": locationless,
        "unmatched": unmatched_total,
        "unmatched_pairs": unmatched_preview,
        "precision": _ratio(complete_tp_total, complete_tp_total + fp_total)
        if complete_tp_total + fp_total else None,
        "recall": _ratio(tp_total, expected_total) if expected_total else None,
        "false_positive_rate": None,
        "false_discovery_rate": _ratio(fp_total, complete_tp_total + fp_total)
        if complete_tp_total + fp_total else None,
        "report_noise_ratio": _ratio(unmatched_total, predicted_total)
        if predicted_total else None,
        "complete_subjects": sorted(complete),
        "declared_complete_subjects": sorted(declared_complete),
        "ambiguous_precision_subjects": sorted(ambiguous_complete),
        "ambiguous_subject_attribution": sorted(ambiguous_subjects),
        "subjects": subject_rows,
        "metric_definitions": dict(METRIC_DEFINITIONS),
    }


def score_report_findings(findings: Iterable[Mapping[str, Any]],
                          expected: Iterable[Mapping[str, Any]], *,
                          complete_subjects: Iterable[str],
                          report_manifest: Mapping[str, Any],
                          all_subjects: Iterable[str] | None = None
                          ) -> dict[str, Any]:
    """Score only label subjects whose report section has usable coverage.

    Corpus labels can be exhaustive while one review run skips that subject.
    Such a run must not turn uncollected labels into false negatives or count
    its predictions as verified precision evidence.
    """
    expected_rows = [dict(item) for item in expected]
    complete = {str(subject) for subject in complete_subjects}
    relevant_subjects = complete | {
        str(item.get("subject") or "unknown") for item in expected_rows
    }
    sections = report_manifest.get("subjects")
    sections = sections if isinstance(sections, Mapping) else {}
    unavailable: dict[str, str] = {}
    truncation = report_manifest.get("truncation")
    truncation = truncation if isinstance(truncation, Mapping) else {}
    normalization_omitted = truncation.get("normalization_findings_omitted", 0)
    max_finding_truncation = truncation.get("max_findings")
    global_coverage_failure = None
    if isinstance(normalization_omitted, int) and normalization_omitted > 0:
        global_coverage_failure = "finding normalization omitted rows"
    elif (isinstance(max_finding_truncation, Mapping)
          and max_finding_truncation.get("truncated") is True):
        global_coverage_failure = "returned findings were limited by max_findings"

    def graph_is_fresh() -> bool:
        graph = report_manifest.get("prioritization_graph")
        if isinstance(graph, Mapping) and graph.get("freshness") == "fresh":
            return True
        range_review = report_manifest.get("range_review")
        graph = range_review.get("graph") if isinstance(range_review, Mapping) else None
        return isinstance(graph, Mapping) and graph.get("freshness") == "fresh"

    def omission_rows(row: Mapping[str, Any]) -> list[Mapping[str, Any]]:
        value = row.get("omitted") or []
        return [item for item in value if isinstance(item, Mapping)] if isinstance(value, list) else []

    def has_interruption_omission(row: Mapping[str, Any]) -> bool:
        value = row.get("omitted") or []
        values = value if isinstance(value, list) else [value]
        for item in values:
            reason = (item.get("reason", "") if isinstance(item, Mapping) else item)
            reason = str(reason).lower()
            if any(marker in reason for marker in
                   ("budget exhausted", "timed out", "time limit",
                    "resource admission refused", "file limit reached",
                    "read limit reached", "remaining files not checked",
                    "remaining documentation not checked",
                    "review budget", "no review budget remained",
                    "collector failed", "collector crashed", "analysis failed")):
                return True
        return False

    def label_path_omitted(label: Mapping[str, Any], row: Mapping[str, Any]) -> bool:
        path = label.get("path")
        if not isinstance(path, str) or not path:
            return False
        for field in ("omitted", "skipped", "unknown"):
            value = row.get(field) or []
            values = value if isinstance(value, list) else [value]
            if any(isinstance(item, Mapping)
                   and (item.get("path") == path or item.get("file") == path)
                   for item in values):
                return True
        return False

    def unavailable_reason(subject: str, label: Mapping[str, Any] | None) -> str | None:
        if global_coverage_failure:
            return global_coverage_failure
        row = sections.get(subject)
        if not isinstance(row, Mapping):
            return "subject report is missing"
        checks = row.get("checks")
        if row.get("status") == "unknown" or not isinstance(checks, list) or not checks:
            return "subject has no executed checks"
        required = (label or {}).get("required_checks")
        if required is not None:
            if not isinstance(required, list) or not required:
                return "label has invalid check coverage metadata"
            missing = sorted(set(required) - set(checks))
            if missing:
                return "required checks did not run: " + ", ".join(missing)
            if label_path_omitted(label or {}, row):
                return "labeled source path was omitted"
            if (label or {}).get("requires_graph") and not graph_is_fresh():
                return "label requires a fresh graph"
            if has_interruption_omission(row):
                return "required coverage was interrupted"
            for item in omission_rows(row):
                if item.get("check") in required:
                    return "a required check was omitted"
            for field in ("skipped", "unknown"):
                values = row.get(field) or []
                if not isinstance(values, list):
                    return f"{field} coverage is malformed"
                for item in values:
                    if isinstance(item, Mapping) and item.get("check") in required:
                        return "a required check was skipped or unknown"
                    if not isinstance(item, Mapping):
                        return "a required check was skipped or unknown"
            return None

        # Legacy labels without check-level requirements fail closed when the
        # report has any omission or no fresh graph. This avoids treating an
        # ambiguous graph-dependent negative result as a false negative.
        if bool(row.get("skipped")) or bool(row.get("unknown")):
            return "subject contains skipped or unknown checks"
        if row.get("omitted"):
            return "subject contains omissions without label-specific coverage metadata"
        if not graph_is_fresh():
            return "label has no check metadata and graph freshness is unavailable"
        return None

    labels_by_subject: dict[str, list[Mapping[str, Any]]] = {}
    for label in expected_rows:
        labels_by_subject.setdefault(str(label.get("subject") or "unknown"), []).append(label)
    for subject in relevant_subjects:
        labels = labels_by_subject.get(subject, [])
        if labels:
            reasons = [unavailable_reason(subject, label) for label in labels]
            reasons = [reason for reason in reasons if reason]
            if reasons:
                unavailable[subject] = reasons[0]
        else:
            reason = unavailable_reason(subject, None)
            if reason:
                unavailable[subject] = reason

    # A label without a subject may match any report section. If any labelled
    # section is unavailable, its negative result cannot be attributed safely.
    exclude_unscoped = any(item.get("subject") is None for item in expected_rows) and bool(unavailable)
    scored_expected = [item for item in expected_rows
                       if (str(item.get("subject") or "unknown") not in unavailable
                           and not (exclude_unscoped and item.get("subject") is None))]
    # An excluded subjectless label could match a finding in any subject. We
    # cannot classify unmatched predictions as false positives until its
    # coverage is known, even if their subject was otherwise fully labelled.
    scored_complete = set() if exclude_unscoped else complete - set(unavailable)
    score = score_findings(
        findings, scored_expected,
        complete_subjects=scored_complete,
        all_subjects=all_subjects)
    score["unavailable_subjects"] = sorted(unavailable)
    score["coverage_reasons"] = dict(sorted(unavailable.items()))
    score["excluded_expected"] = len(expected_rows) - len(scored_expected)
    if exclude_unscoped:
        score["precision_coverage_reason"] = (
            "subjectless labels were excluded; their prediction subjects are unknown")
    return score
