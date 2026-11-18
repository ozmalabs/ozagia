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
        "false positives / predictions in completely labelled subjects; this "
        "is a corpus-local false-discovery proportion, not a population rate"
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
        key = (path, ftype, subject)
        if key in seen:
            raise ValueError(f"duplicate reviewed label: {path} {ftype} {subject}")
        seen.add(key)
        label_id = _text(item.get("id"), f"{field}[{index}].id", required=False)
        rows.append({"id": label_id or f"{field}-{index}", "path": path,
                     "type": ftype, "subject": subject})
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
        cases.append({"id": case_id, "file": path, "subject": subject,
                      "expected": expected, "negatives": negatives})

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

    def add(path: str, ftype: str, subject: str | None, source: str) -> None:
        key = (path, ftype, subject)
        if key in seen:
            raise ValueError(f"duplicate reviewed label across sections: {path} {ftype} {subject}")
        seen.add(key)
        rows.append({"path": path, "type": ftype, "subject": subject,
                     "source": source})

    if include_cases:
        for case in manifest.get("cases") or []:
            for ftype in case.get("expected") or []:
                add(case["file"], ftype, case.get("subject"), f"case:{case['id']}")
    if include_report:
        for item in manifest.get("report_findings") or []:
            add(item["path"], item["type"], item.get("subject"),
                f"report:{item['id']}")
    if include_range:
        range_data = manifest.get("range") or {}
        for item in range_data.get("expected") or []:
            add(item["path"], item["type"], item.get("subject"),
                f"range:{item['id']}")
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

    for index, label in enumerate(expected_rows):
        wanted_subject = label.get("subject")
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

    def subject_row(subject: str) -> dict[str, Any]:
        subject_expected = [
            index for index, label in enumerate(expected_rows)
            if (label.get("subject") or "unknown") == subject
        ]
        subject_actual = [key for key in actual_rows if key[2] == subject]
        tp = sum(1 for index in subject_expected if index in matched_expected)
        fn = len(subject_expected) - tp
        is_complete = subject in complete
        unmatched = [key for key in subject_actual if key not in matched_actual]
        fp = len(unmatched) if is_complete else None
        predicted = len(subject_actual)
        precision = _ratio(tp, predicted) if is_complete else None
        recall = _ratio(tp, len(subject_expected)) if subject_expected else None
        fpr = _ratio(fp or 0, predicted) if is_complete and predicted else None
        noise = _ratio(len(unmatched), predicted) if predicted else None
        notes: list[str] = []
        if not subject_expected:
            notes.append("no reviewed labels for this subject")
        if not is_complete:
            notes.append("subject is not marked as completely labelled")
        if not predicted:
            notes.append("no predictions in this subject")
        return {
            "complete": is_complete,
            "expected": len(subject_expected),
            "predicted": predicted,
            "true_positive": tp,
            "false_positive": fp,
            "false_negative": fn,
            "unmatched": len(unmatched),
            "precision": precision,
            "recall": recall,
            "false_positive_rate": fpr,
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
        "precision": _ratio(tp_total, tp_total + fp_total)
        if complete and tp_total + fp_total else None,
        "recall": _ratio(tp_total, expected_total) if expected_total else None,
        "false_positive_rate": _ratio(fp_total, tp_total + fp_total)
        if complete and tp_total + fp_total else None,
        "report_noise_ratio": _ratio(unmatched_total, predicted_total)
        if predicted_total else None,
        "complete_subjects": sorted(complete),
        "subjects": subject_rows,
        "metric_definitions": dict(METRIC_DEFINITIONS),
    }
