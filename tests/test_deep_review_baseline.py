import json
import os

import pytest

from ozagia.core.deep_review.baseline import (
    BaselineValidationError, load_baseline, validate_findings,
)


def _finding(identifier="dr-0123456789abcdef", **extra):
    return {"id": identifier, "subject": "security", "type": "candidate",
            **extra}


def test_loads_current_report_directory_and_legacy_findings_only(tmp_path):
    report = tmp_path / "report"
    report.mkdir()
    findings = [_finding(), _finding("dr-fedcba9876543210", severity="major")]
    (report / "findings.json").write_text(json.dumps(findings), encoding="utf-8")
    (report / "manifest.json").write_text(json.dumps({
        "schema": "ozagia.deep_review.manifest.v1", "findings": 2,
    }), encoding="utf-8")

    assert load_baseline(report) == findings
    # Historical artifacts can be a plain findings.json with no manifest.
    assert load_baseline(report / "findings.json") == findings


def test_loads_older_report_directory_without_manifest(tmp_path):
    report = tmp_path / "older-report"
    report.mkdir()
    findings = [_finding()]
    (report / "findings.json").write_text(json.dumps(findings), encoding="utf-8")

    assert load_baseline(report) == findings


@pytest.mark.parametrize("identifier", [
    "F-0123456789abcdef", "dr-0123456789abcde", "dr-0123456789abcdeG",
    "dr-0123456789ABCDEF", "unrelated-report-id",
])
def test_rejects_unrelated_or_malformed_finding_ids(identifier):
    with pytest.raises(BaselineValidationError, match="invalid deep-review ID"):
        validate_findings([_finding(identifier)])


def test_rejects_duplicate_ids_and_malformed_findings():
    with pytest.raises(BaselineValidationError, match="duplicate finding ID"):
        validate_findings([_finding(), _finding()])
    with pytest.raises(BaselineValidationError, match="must be an object"):
        validate_findings(["not a finding"])
    with pytest.raises(BaselineValidationError, match="must contain a list"):
        validate_findings({"findings": [_finding()]})


def test_rejects_directory_with_unrelated_manifest_schema(tmp_path):
    report = tmp_path / "report"
    report.mkdir()
    (report / "findings.json").write_text("[]", encoding="utf-8")
    (report / "manifest.json").write_text(json.dumps({
        "schema": "unrelated.report.v1",
    }), encoding="utf-8")

    with pytest.raises(BaselineValidationError, match="unsupported deep-review schema"):
        load_baseline(report)


def test_rejects_missing_malformed_and_oversized_artifacts(tmp_path):
    with pytest.raises(BaselineValidationError, match="missing or unreadable"):
        load_baseline(tmp_path / "missing-report")

    malformed = tmp_path / "malformed.json"
    malformed.write_text("{no", encoding="utf-8")
    with pytest.raises(BaselineValidationError, match="not valid JSON"):
        load_baseline(malformed)

    oversized = tmp_path / "large.json"
    oversized.write_bytes(b" " * (32 * 1024 * 1024 + 1))
    with pytest.raises(BaselineValidationError, match="exceeds 32 MiB"):
        load_baseline(oversized)


def test_rejects_symlinks_for_baseline_report_and_artifact_files(tmp_path):
    actual = tmp_path / "actual.json"
    actual.write_text(json.dumps([_finding()]), encoding="utf-8")
    linked_file = tmp_path / "linked.json"
    linked_file.symlink_to(actual)
    with pytest.raises(BaselineValidationError, match="must not be a symlink"):
        load_baseline(linked_file)

    report = tmp_path / "report"
    report.mkdir()
    (report / "findings.json").symlink_to(actual)
    with pytest.raises(BaselineValidationError, match="regular file"):
        load_baseline(report)

    linked_dir = tmp_path / "linked-report"
    linked_dir.symlink_to(report, target_is_directory=True)
    with pytest.raises(BaselineValidationError, match="must not be a symlink"):
        load_baseline(linked_dir)


def test_rejects_fifo_and_duplicate_json_object_keys_without_hanging(tmp_path):
    fifo = tmp_path / "findings.json"
    if not hasattr(os, "mkfifo"):
        pytest.skip("named pipes are unavailable")
    os.mkfifo(fifo)
    with pytest.raises(BaselineValidationError, match="regular file"):
        load_baseline(fifo)

    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text(
        '[{"id":"dr-0123456789abcdef",'
        '"id":"dr-fedcba9876543210"}]', encoding="utf-8")
    with pytest.raises(BaselineValidationError, match="duplicate object key"):
        load_baseline(duplicate)


def test_rejects_symlinked_manifest(tmp_path):
    report = tmp_path / "report"
    report.mkdir()
    (report / "findings.json").write_text("[]", encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({
        "schema": "ozagia.deep_review.manifest.v1",
    }), encoding="utf-8")
    (report / "manifest.json").symlink_to(manifest)

    with pytest.raises(BaselineValidationError, match="regular file"):
        load_baseline(report)
