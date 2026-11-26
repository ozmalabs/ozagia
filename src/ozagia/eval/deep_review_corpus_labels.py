"""Label schema, validation and sealing for the realistic deep-review corpus.

Two human reviewers label each subject independently before either sees an
Ozagia report; a third human adjudicates disagreements.  This module checks the
files, not the people: it cannot prove a label came from a human, it only
records provenance and refuses to let synthetic fixtures masquerade as human
evidence in the realistic-corpus scorer.

A sealed label file carries ``label_file_sha256`` over its canonical JSON with
that field removed.  That makes an accidental edit detectable without pretending
to be a signature.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from ozagia.eval.deep_review_corpus import (
    DEFAULT_LABELS_DIR,
    DEFAULT_MANIFEST,
    _slot_by_key,
    canonical_sha256,
    load_manifest,
)

LABEL_SCHEMA_VERSION = 1
PROVENANCE_VALUES = frozenset({"human", "synthetic_test_fixture"})
ROLES = frozenset({"reviewer", "adjudicator"})
COVERAGE_STATUSES = frozenset({"complete", "partial", "unknown"})
SEVERITIES = frozenset({"blocking", "major", "minor", "note", "unknown"})
VERDICTS = frozenset({"true_positive", "false_positive", "undetermined"})
SCOPES = frozenset({"exact", "file", "symbol", "repository", "unknown"})
RESOLUTIONS = frozenset({"agree", "reviewer_a", "reviewer_b", "new", "excluded"})
EVIDENCE_KINDS = frozenset({
    "diff", "file", "line", "symbol", "test", "issue", "command_output", "manual",
})
DEFAULT_TEMPLATES_DIR = Path(__file__).resolve().parents[3] / "evaluation" / \
    "deep_review_corpus" / "templates"
_SHA256 = frozenset("0123456789abcdef")


class LabelError(ValueError):
    """A label file or label set is invalid."""


def _text(value: Any, field: str, *, required: bool = True,
          allow_template: bool = False) -> str | None:
    if value is None:
        if required:
            raise LabelError(f"{field} must be a non-empty string")
        return None
    if not isinstance(value, str) or not value.strip():
        raise LabelError(f"{field} must be a non-empty string")
    text = value.strip()
    if allow_template and text.startswith("<") and text.endswith(">"):
        return text
    return text


def _optional_text(value: Any, field: str, *, allow_template: bool = False) -> str | None:
    return _text(value, field, required=False, allow_template=allow_template)


def _bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise LabelError(f"{field} must be a boolean")
    return value


def _bool_or_none(value: Any, field: str) -> bool | None:
    if value is None:
        return None
    return _bool(value, field)


def _sha256_or_none(value: Any, field: str) -> str | None:
    text = _optional_text(value, field)
    if text is None or (text.startswith("<") and text.endswith(">")):
        return text
    if len(text) != 64 or any(character not in _SHA256 for character in text):
        raise LabelError(f"{field} must be a lowercase SHA-256 digest")
    return text


def _relative_path(value: Any, field: str, *, allow_template: bool = False) -> str:
    text = _text(value, field, allow_template=allow_template)
    assert text is not None
    if allow_template and text.startswith("<") and text.endswith(">"):
        return text
    path = Path(text)
    if path.is_absolute() or ".." in path.parts or text in {".", ""}:
        raise LabelError(f"{field} must be a relative path: {text}")
    return path.as_posix()


def _string_list(value: Any, field: str, *, allow_template: bool = False) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise LabelError(f"{field} must be a list of strings")
    result: list[str] = []
    for index, item in enumerate(value):
        text = _text(item, f"{field}[{index}]", allow_template=allow_template)
        assert text is not None
        if text not in result:
            result.append(text)
    return result


def _timestamp(value: Any, field: str) -> str | None:
    text = _optional_text(value, field)
    if text is None:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise LabelError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise LabelError(f"{field} must include a timezone")
    return text


def label_payload(envelope: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in envelope.items() if key != "label_file_sha256"}


def label_file_digest(envelope: Mapping[str, Any]) -> str:
    payload = json.dumps(label_payload(envelope), sort_keys=True,
                         separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def seal_label_file(envelope: Mapping[str, Any]) -> dict[str, Any]:
    sealed = json.loads(json.dumps(envelope))
    sealed["label_file_sha256"] = label_file_digest(sealed)
    return sealed


def _reviewer(envelope: Mapping[str, Any], kind: str,
              allow_template: bool) -> dict[str, Any]:
    reviewer = envelope.get("reviewer")
    if not isinstance(reviewer, Mapping):
        raise LabelError("label file.reviewer must be an object")
    reviewer_id = _text(reviewer.get("id"), "reviewer.id", allow_template=allow_template)
    assert reviewer_id is not None
    role = _text(reviewer.get("role"), "reviewer.role")
    assert role is not None
    if role not in ROLES:
        raise LabelError("reviewer.role must be reviewer or adjudicator")
    if role != kind:
        raise LabelError("reviewer.role must match the file kind")
    independent = _bool(reviewer.get("independent_pass"), "reviewer.independent_pass")
    started = _timestamp(reviewer.get("started_at"), "reviewer.started_at")
    completed = _timestamp(reviewer.get("completed_at"), "reviewer.completed_at")
    if started and completed:
        started_dt = datetime.fromisoformat(started.replace("Z", "+00:00"))
        completed_dt = datetime.fromisoformat(completed.replace("Z", "+00:00"))
        if completed_dt < started_dt:
            raise LabelError("reviewer.completed_at is before started_at")
    return {"id": reviewer_id, "role": role, "independent_pass": independent,
            "started_at": started, "completed_at": completed}


def _declared_scope(envelope: Mapping[str, Any], *, allow_template: bool,
                    require_complete: bool) -> dict[str, Any]:
    scope = envelope.get("declared_scope")
    if not isinstance(scope, Mapping):
        raise LabelError("label file.declared_scope must be an object")
    status = _text(scope.get("status"), "declared_scope.status")
    assert status is not None
    if status not in COVERAGE_STATUSES:
        raise LabelError("declared_scope.status must be complete, partial or unknown")
    description = _optional_text(scope.get("description"),
                                 "declared_scope.description",
                                 allow_template=allow_template)
    subjects = _string_list(scope.get("subjects"), "declared_scope.subjects")
    paths = _string_list(scope.get("paths"), "declared_scope.paths")
    normalized_paths = [
        _relative_path(path, "declared_scope.paths[]", allow_template=allow_template)
        for path in paths
    ]
    if require_complete and status == "complete":
        if not description:
            raise LabelError("complete declared_scope needs a description of its boundary")
        if not subjects:
            raise LabelError("complete declared_scope needs at least one subject")
    return {"status": status, "description": description,
            "subjects": subjects, "paths": normalized_paths}


def _location(value: Any, field: str, *, scope: str,
              allow_template: bool) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise LabelError(f"{field} must be an object")
    path = _relative_path(value.get("path"), f"{field}.path",
                          allow_template=allow_template)
    start = value.get("start_line")
    end = value.get("end_line")
    if start is not None and (isinstance(start, bool) or not isinstance(start, int)
                              or start < 1):
        raise LabelError(f"{field}.start_line must be a positive integer or null")
    if end is not None and (isinstance(end, bool) or not isinstance(end, int)
                            or (start is not None and end < start)):
        raise LabelError(f"{field}.end_line must be null or at least start_line")
    symbol = _optional_text(value.get("symbol"), f"{field}.symbol",
                            allow_template=allow_template)
    if scope == "exact" and start is None and not allow_template:
        raise LabelError(f"{field}.start_line is required for an exact scope")
    if scope == "symbol" and not symbol and not allow_template:
        raise LabelError(f"{field}.symbol is required for a symbol scope")
    return {"path": path, "start_line": start, "end_line": end, "symbol": symbol}


def _evidence(value: Any, field: str) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise LabelError(f"{field} must be a list")
    rows: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        item_field = f"{field}[{index}]"
        if not isinstance(item, Mapping):
            raise LabelError(f"{item_field} must be an object")
        kind = _text(item.get("kind"), f"{item_field}.kind")
        assert kind is not None
        if kind not in EVIDENCE_KINDS:
            raise LabelError(f"{item_field}.kind is not a known evidence kind")
        reference = _text(item.get("reference"), f"{item_field}.reference")
        digest = _sha256_or_none(item.get("digest"), f"{item_field}.digest")
        note = _optional_text(item.get("note"), f"{item_field}.note")
        rows.append({"kind": kind, "reference": reference, "digest": digest,
                     "note": note})
    return rows


def _actionable(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise LabelError(f"{field} must be an object")
    is_actionable = _bool_or_none(value.get("is_actionable"),
                                  f"{field}.is_actionable")
    wording = _optional_text(value.get("wording"), f"{field}.wording")
    return {"is_actionable": is_actionable, "wording": wording}


def _label_entry(item: Any, field: str, *, allow_template: bool,
                 require_complete: bool) -> dict[str, Any]:
    if not isinstance(item, Mapping):
        raise LabelError(f"{field} must be an object")
    label_id = _text(item.get("id"), f"{field}.id", allow_template=allow_template)
    assert label_id is not None
    subject = _text(item.get("subject"), f"{field}.subject")
    assert subject is not None
    ftype = _text(item.get("type"), f"{field}.type")
    assert ftype is not None
    severity = _text(item.get("severity"), f"{field}.severity")
    assert severity is not None
    if severity not in SEVERITIES:
        raise LabelError(f"{field}.severity must be one of "
                         f"{', '.join(sorted(SEVERITIES))}")
    scope = _text(item.get("scope"), f"{field}.scope")
    assert scope is not None
    if scope not in SCOPES:
        raise LabelError(f"{field}.scope must be one of {', '.join(sorted(SCOPES))}")
    verdict = _text(item.get("verdict"), f"{field}.verdict")
    assert verdict is not None
    if verdict not in VERDICTS:
        raise LabelError(f"{field}.verdict must be one of {', '.join(sorted(VERDICTS))}")
    location = _location(item.get("location"), f"{field}.location", scope=scope,
                         allow_template=allow_template)
    evidence = _evidence(item.get("evidence"), f"{field}.evidence")
    claim = _text(item.get("claim"), f"{field}.claim")
    actionable = _actionable(item.get("actionable"), f"{field}.actionable")
    severity_appropriate = _bool_or_none(item.get("severity_appropriate"),
                                         f"{field}.severity_appropriate")
    source_digest = _sha256_or_none(item.get("source_file_digest"),
                                    f"{field}.source_file_digest")
    notes = _optional_text(item.get("notes"), f"{field}.notes")
    if require_complete and verdict == "true_positive":
        if not evidence:
            raise LabelError(f"{field} needs supporting evidence")
        if not actionable["wording"]:
            raise LabelError(f"{field} needs actionable wording")
        if source_digest is None or not isinstance(source_digest, str) \
                or source_digest.startswith("<"):
            raise LabelError(f"{field}.source_file_digest is required")
    return {
        "id": label_id,
        "subject": subject,
        "type": ftype,
        "severity": severity,
        "scope": scope,
        "verdict": verdict,
        "location": location,
        "evidence": evidence,
        "claim": claim,
        "actionable": actionable,
        "severity_appropriate": severity_appropriate,
        "source_file_digest": source_digest,
        "notes": notes,
    }


def _decision(item: Any, field: str, *, allow_template: bool,
              require_complete: bool) -> dict[str, Any]:
    decision = _label_entry(item, field, allow_template=allow_template,
                            require_complete=require_complete)
    reviewer_label_ids = item.get("reviewer_label_ids")
    if not isinstance(reviewer_label_ids, Mapping):
        raise LabelError(f"{field}.reviewer_label_ids must be an object")
    references: dict[str, list[str]] = {}
    for reviewer_id, label_ids in reviewer_label_ids.items():
        reviewer_text = _text(reviewer_id, f"{field}.reviewer_label_ids key")
        assert reviewer_text is not None
        references[reviewer_text] = _string_list(
            label_ids, f"{field}.reviewer_label_ids[{reviewer_text}]",
            allow_template=allow_template)
    resolution = _text(item.get("resolution"), f"{field}.resolution")
    assert resolution is not None
    if resolution not in RESOLUTIONS:
        raise LabelError(f"{field}.resolution must be one of "
                         f"{', '.join(sorted(RESOLUTIONS))}")
    rationale = _text(item.get("rationale"), f"{field}.rationale")
    decision.update({"reviewer_label_ids": references,
                     "resolution": resolution, "rationale": rationale})
    return decision


def validate_label_file(envelope: Any, *, manifest: Mapping[str, Any] | None = None,
                        allow_template: bool = False,
                        require_complete: bool = False,
                        require_sealed: bool = False,
                        require_human: bool = False) -> dict[str, Any]:
    """Validate one reviewer or adjudication file and return a normalized copy."""
    if not isinstance(envelope, Mapping):
        raise LabelError("label file must be an object")
    schema = envelope.get("label_schema_version")
    if isinstance(schema, bool) or not isinstance(schema, int):
        raise LabelError("label file.label_schema_version must be an integer")
    if schema != LABEL_SCHEMA_VERSION:
        raise LabelError(f"unsupported label_schema_version {schema}; "
                         f"supported: {LABEL_SCHEMA_VERSION}")
    corpus_id = _text(envelope.get("corpus_id"), "label file.corpus_id",
                      allow_template=allow_template)
    repository_id = _text(envelope.get("repository_id"), "label file.repository_id",
                         allow_template=allow_template)
    subject_key = _text(envelope.get("subject_key"), "label file.subject_key",
                        allow_template=allow_template)
    kind = _text(envelope.get("kind"), "label file.kind")
    assert kind is not None
    if kind not in ROLES:
        raise LabelError("label file.kind must be reviewer or adjudicator")
    provenance = _text(envelope.get("provenance"), "label file.provenance")
    assert provenance is not None
    if provenance not in PROVENANCE_VALUES:
        raise LabelError("label file.provenance must be human or synthetic_test_fixture")
    if require_human and provenance != "human":
        raise LabelError("realistic-corpus evidence requires human provenance")
    reviewer = _reviewer(envelope, kind, allow_template)
    if require_complete:
        if reviewer["started_at"] is None or reviewer["completed_at"] is None:
            raise LabelError("reviewer timestamps are required for a completed file")
    declared_scope = _declared_scope(envelope, allow_template=allow_template,
                                     require_complete=require_complete)
    if manifest is not None and not allow_template:
        assert repository_id is not None
        assert subject_key is not None
        repositories = {item["id"] for item in manifest.get("repositories") or []}
        if repository_id not in repositories:
            raise LabelError(f"repository_id is not in the manifest: {repository_id}")
        _slot_by_key(manifest, repository_id, subject_key)
    entries_key = "labels" if kind == "reviewer" else "decisions"
    entries = envelope.get(entries_key)
    if not isinstance(entries, list):
        raise LabelError(f"label file.{entries_key} must be a list")
    parsed: list[dict[str, Any]] = []
    ids: set[str] = set()
    for index, item in enumerate(entries):
        field = f"{entries_key}[{index}]"
        row = (_label_entry(item, field, allow_template=allow_template,
                            require_complete=require_complete)
               if kind == "reviewer"
               else _decision(item, field, allow_template=allow_template,
                              require_complete=require_complete))
        if row["id"] in ids:
            raise LabelError(f"duplicate {entries_key} id: {row['id']}")
        ids.add(row["id"])
        parsed.append(row)
    if require_complete and kind == "adjudicator" and not parsed:
        raise LabelError("an adjudication file needs at least one decision")
    digest = envelope.get("label_file_sha256")
    computed = label_file_digest(envelope)
    if digest is not None:
        if not isinstance(digest, str) or digest != computed:
            raise LabelError(
                "label_file_sha256 does not match the file contents; re-seal after editing")
    elif require_sealed:
        raise LabelError("label file is not sealed; run the seal command")
    normalized = {
        "label_schema_version": schema,
        "corpus_id": corpus_id,
        "repository_id": repository_id,
        "subject_key": subject_key,
        "kind": kind,
        "provenance": provenance,
        "reviewer": reviewer,
        "declared_scope": declared_scope,
        entries_key: parsed,
        "label_file_sha256": computed if digest is not None else None,
    }
    return normalized


def load_label_file(path: str | Path, **kwargs: Any) -> dict[str, Any]:
    source = Path(path)
    try:
        envelope = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise LabelError(f"invalid label JSON in {source}: {exc}") from exc
    return validate_label_file(envelope, **kwargs)


def load_template_file(path: str | Path) -> dict[str, Any]:
    return load_label_file(path, allow_template=True)


def _group_files(labels_dir: Path) -> dict[tuple[str, str], list[Path]]:
    groups: dict[tuple[str, str], list[Path]] = {}
    for path in sorted(labels_dir.rglob("*.json")):
        if not path.is_file():
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise LabelError(f"invalid label JSON in {path}: {exc}") from exc
        if not isinstance(raw, Mapping):
            raise LabelError(f"label file must be an object: {path}")
        repository_id = _text(raw.get("repository_id"), f"{path}.repository_id")
        subject_key = _text(raw.get("subject_key"), f"{path}.subject_key")
        assert repository_id is not None and subject_key is not None
        groups.setdefault((repository_id, subject_key), []).append(path)
    return groups


def _load_label_set(labels_dir: str | Path, manifest: Mapping[str, Any], *,
                    require_complete: bool = False,
                    require_human: bool = False,
                    require_sealed: bool | None = None) -> tuple[dict[str, Any],
                                                                 dict[str, dict[str, Any]]]:
    base = Path(labels_dir)
    if not base.is_dir():
        raise LabelError(f"labels directory missing: {base}")
    policy = manifest.get("review_policy") or {}
    if require_sealed is None:
        require_sealed = bool(policy.get("require_sealed_label_files", True))
    errors: list[str] = []
    warnings: list[str] = []
    subjects: list[dict[str, Any]] = []
    envelopes: dict[str, dict[str, Any]] = {}
    try:
        groups = _group_files(base)
    except LabelError as exc:
        return ({"schema_version": LABEL_SCHEMA_VERSION, "corpus_id": manifest.get("corpus_id"),
                 "labels_dir": str(base), "status": "invalid", "errors": [str(exc)],
                 "warnings": [], "subjects": [], "counts": {}, "digest": None}, {})
    for (repository_id, subject_key), paths in sorted(groups.items()):
        reviewers: dict[str, dict[str, Any]] = {}
        adjudication: dict[str, Any] | None = None
        files: list[dict[str, Any]] = []
        provenance: set[str] = set()
        for path in paths:
            relative = path.relative_to(base).as_posix()
            try:
                loaded = load_label_file(
                    path, manifest=manifest, require_complete=require_complete,
                    require_sealed=require_sealed, require_human=require_human)
            except (LabelError, ValueError) as exc:
                errors.append(f"{relative}: {exc}")
                continue
            provenance.add(loaded["provenance"])
            files.append({
                "path": relative,
                "kind": loaded["kind"],
                "reviewer_id": loaded["reviewer"]["id"],
                "sha256": label_file_digest(loaded),
            })
            if loaded["kind"] == "reviewer":
                reviewer_id = loaded["reviewer"]["id"]
                if reviewer_id in reviewers:
                    errors.append(f"{relative}: duplicate reviewer id {reviewer_id}")
                reviewers[reviewer_id] = loaded
            else:
                if adjudication is not None:
                    errors.append(f"{relative}: duplicate adjudication file")
                adjudication = loaded
        scope = subject_key
        group_complete = False
        resolutions: dict[str, int] = {}
        if len(reviewers) != 2:
            message = f"{repository_id}/{scope}: expected 2 reviewer files, found {len(reviewers)}"
            (errors if require_complete else warnings).append(message)
        if adjudication is None:
            message = f"{repository_id}/{scope}: missing adjudication file"
            (errors if require_complete else warnings).append(message)
        if adjudication is not None and reviewers:
            known_reviewer_ids = set(reviewers)
            referenced: dict[str, set[str]] = {key: set() for key in known_reviewer_ids}
            for decision in adjudication["decisions"]:
                resolutions[decision["resolution"]] = resolutions.get(
                    decision["resolution"], 0) + 1
                for reviewer_id, label_ids in decision["reviewer_label_ids"].items():
                    if reviewer_id not in known_reviewer_ids:
                        errors.append(
                            f"{repository_id}/{scope}: adjudication references unknown "
                            f"reviewer {reviewer_id}")
                        continue
                    referenced[reviewer_id].update(label_ids)
                if decision["resolution"] == "new" and decision["reviewer_label_ids"]:
                    warnings.append(
                        f"{repository_id}/{scope}: decision {decision['id']} is new "
                        "but references reviewer labels")
            for reviewer_id, reviewer_file in reviewers.items():
                actual = {label["id"] for label in reviewer_file["labels"]}
                missing = sorted(actual - referenced[reviewer_id])
                unknown = sorted(referenced[reviewer_id] - actual)
                if missing:
                    errors.append(
                        f"{repository_id}/{scope}: reviewer {reviewer_id} labels are "
                        f"not referenced by adjudication: {', '.join(missing)}")
                if unknown:
                    errors.append(
                        f"{repository_id}/{scope}: adjudication references unknown labels "
                        f"for {reviewer_id}: {', '.join(unknown)}")
            scope_statuses = {item["declared_scope"]["status"] for item in reviewers.values()}
            scope_statuses.add(adjudication["declared_scope"]["status"])
            group_complete = scope_statuses == {"complete"}
            if require_complete and not group_complete:
                errors.append(
                    f"{repository_id}/{scope}: declared scope is not complete "
                    f"({', '.join(sorted(scope_statuses))})")
            if not group_complete and not require_complete:
                warnings.append(
                    f"{repository_id}/{scope}: scope is "
                    f"{', '.join(sorted(scope_statuses))}")
        label_counts = {reviewer_id: len(item["labels"])
                        for reviewer_id, item in sorted(reviewers.items())}
        subject_row = {
            "repository_id": repository_id,
            "subject_key": subject_key,
            "subject_kind": ("whole-repo" if subject_key == "whole-repo" else "change-range"),
            "complete": group_complete,
            "provenance": ("human" if provenance == {"human"}
                           else "synthetic_test_fixture"
                           if provenance == {"synthetic_test_fixture"}
                           else "mixed" if provenance else "unknown"),
            "reviewer_ids": sorted(reviewers),
            "reviewer_label_counts": label_counts,
            "adjudication_decisions": (len(adjudication["decisions"])
                                       if adjudication is not None else 0),
            "has_adjudication": adjudication is not None,
            "resolution_counts": dict(sorted(resolutions.items())),
            "files": files,
        }
        subjects.append(subject_row)
        key = f"{repository_id}/{subject_key}"
        envelopes[key] = {"reviewers": reviewers, "adjudication": adjudication,
                          "summary": subject_row}
    complete_count = sum(1 for item in subjects if item["complete"])
    summary = {
        "schema_version": LABEL_SCHEMA_VERSION,
        "corpus_id": manifest.get("corpus_id"),
        "labels_dir": str(base),
        "status": ("invalid" if errors else "incomplete" if warnings else "valid"),
        "errors": errors,
        "warnings": warnings,
        "subjects": subjects,
        "counts": {
            "subjects": len(subjects),
            "complete_subjects": complete_count,
            "partial_or_unknown_subjects": len(subjects) - complete_count,
            "reviewer_files": sum(len(item["reviewer_ids"]) for item in subjects),
            "adjudication_files": sum(1 for item in subjects if item["has_adjudication"]),
        },
        "digest": canonical_sha256(sorted(
            (item["path"], item["sha256"])
            for subject in subjects for item in subject["files"])),
    }
    return summary, envelopes


def load_label_set(labels_dir: str | Path, manifest: Mapping[str, Any], *,
                   require_complete: bool = False,
                   require_human: bool = False,
                   require_sealed: bool | None = None) -> dict[str, Any]:
    summary, envelopes = _load_label_set(
        labels_dir, manifest, require_complete=require_complete,
        require_human=require_human, require_sealed=require_sealed)
    return {"summary": summary, "subjects": envelopes}


def validate_labels_dir(labels_dir: str | Path, manifest: Mapping[str, Any], *,
                        require_complete: bool = False,
                        require_human: bool = False,
                        require_sealed: bool | None = None) -> dict[str, Any]:
    summary, _envelopes = _load_label_set(
        labels_dir, manifest, require_complete=require_complete,
        require_human=require_human, require_sealed=require_sealed)
    return summary


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
    sub = parser.add_subparsers(dest="command", required=True)
    validate = sub.add_parser("validate", help="validate a label directory offline")
    validate.add_argument("--labels", type=Path, default=DEFAULT_LABELS_DIR)
    validate.add_argument("--require-complete", action="store_true")
    validate.add_argument("--require-human", action="store_true")
    validate.add_argument("--out", type=Path)
    seal = sub.add_parser("seal", help="compute and write a label file digest")
    seal.add_argument("--path", type=Path, required=True)
    seal.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    try:
        manifest = load_manifest(args.manifest)
        exit_code = 0
        if args.command == "validate":
            result = validate_labels_dir(
                args.labels, manifest, require_complete=args.require_complete,
                require_human=args.require_human)
            if result.get("status") == "invalid":
                exit_code = 2
        else:
            raw = json.loads(args.path.read_text(encoding="utf-8"))
            if not isinstance(raw, Mapping):
                raise LabelError("label file must be an object")
            draft = {key: value for key, value in raw.items()
                     if key != "label_file_sha256"}
            validate_label_file(draft, allow_template=True)
            result = seal_label_file(draft)
        _write_json(result, args.out)
    except (LabelError, ValueError, OSError) as exc:
        print(json.dumps({"schema_version": LABEL_SCHEMA_VERSION, "status": "error",
                          "error": str(exc)}, indent=2, sort_keys=True), file=sys.stderr)
        return 2
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
