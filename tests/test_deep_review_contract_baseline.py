import json

import pytest

from ozagia.core.deep_review import orchestrator
from ozagia.core.deep_review.orchestrator import DeepReviewError


def _packet(findings, sections=None):
    return {"findings": findings, "sections": sections or {},
            "graph": {"freshness": "unknown"}}


def _stub(monkeypatch, packet):
    monkeypatch.setattr(orchestrator.collect, "collect", lambda *a, **k: packet)
    monkeypatch.setattr(
        orchestrator, "_verified_priority_graph",
        lambda root, **kwargs: (None, {
            "freshness": "unknown", "reason": "test graph omitted"}))


def _raw(section, kind, severity, path, summary):
    return {"section": section, "type": kind, "severity": severity,
            "keys": [f"file:{path}"], "summary": summary}


@pytest.mark.parametrize("payload", [
    "{not json",
    "{}",
    json.dumps([{"type": "missing-id"}]),
    json.dumps([{"id": 7, "type": "non-string-id"}]),
])
def test_malformed_baseline_is_rejected(tmp_path, monkeypatch, payload):
    baseline = tmp_path / "baseline.json"
    baseline.write_text(payload, encoding="utf-8")
    _stub(monkeypatch, _packet([]))
    with pytest.raises(DeepReviewError, match="invalid baseline findings"):
        orchestrator.build_deep_review(
            tmp_path, tmp_path / "out", baseline=baseline)


def test_report_directory_with_unsupported_manifest_is_rejected(
        tmp_path, monkeypatch):
    baseline = tmp_path / "baseline"
    baseline.mkdir()
    (baseline / "findings.json").write_text("[]", encoding="utf-8")
    (baseline / "manifest.json").write_text(
        json.dumps({"schema": "some.other.report.v1"}), encoding="utf-8")
    _stub(monkeypatch, _packet([]))

    with pytest.raises(DeepReviewError, match="unsupported deep-review schema"):
        orchestrator.build_deep_review(
            tmp_path, tmp_path / "out", baseline=baseline)


def test_baseline_new_resolved_unchanged_and_subject_severity_deltas(
        tmp_path, monkeypatch):
    _stub(monkeypatch, _packet([
        _raw("security", "shared", "major", "a.py", "Still present."),
        _raw("architecture", "removed", "minor", "b.py", "No longer present."),
    ]))
    first = orchestrator.build_deep_review(tmp_path, tmp_path / "baseline")
    baseline_ids = {item["type"]: item["id"] for item in first["findings"]}

    _stub(monkeypatch, _packet([
        _raw("security", "shared", "major", "a.py", "Still present."),
        _raw("performance", "new_risk", "blocking", "c.py", "New risk."),
    ]))
    second = orchestrator.build_deep_review(
        tmp_path, tmp_path / "current", baseline=tmp_path / "baseline")
    current_ids = {item["type"]: item["id"] for item in second["findings"]}
    comparison = second["manifest"]["comparison"]

    assert comparison["identity_policy"] == \
        "canonical_subject_exact_finding_id_v1"
    assert comparison["move_mapping"] == "unknown"
    assert "Moved or renamed" in comparison["move_policy"]
    assert second["manifest"]["identity_policy"] == \
        "canonical_subject_exact_finding_id_v1"
    assert second["manifest"]["move_mapping"] == "unknown"
    assert comparison["new"] == [current_ids["new_risk"]]
    assert comparison["resolved"] == []
    assert comparison["unverified_absent"] == [baseline_ids["removed"]]
    assert comparison["unchanged"] == [current_ids["shared"]]
    assert comparison["by_subject"] == {
        "architecture": {"new": 0, "resolved": 0, "unverified_absent": 1, "unchanged": 0},
        "performance": {"new": 1, "resolved": 0, "unverified_absent": 0, "unchanged": 0},
        "security": {"new": 0, "resolved": 0, "unverified_absent": 0, "unchanged": 1},
    }
    assert comparison["by_severity"] == {
        "blocking": {"new": 1, "resolved": 0, "unverified_absent": 0, "unchanged": 0},
        "major": {"new": 0, "resolved": 0, "unverified_absent": 0, "unchanged": 1},
        "minor": {"new": 0, "resolved": 0, "unverified_absent": 1, "unchanged": 0},
    }
    assert comparison["totals"] == {
        "new": 1, "resolved": 0, "unverified_absent": 1, "unchanged": 1}


def test_moved_file_follows_resolved_plus_new_policy(tmp_path, monkeypatch):
    _stub(monkeypatch, _packet([
        _raw("architecture", "cycle", "major", "src/a.py", "Cycle."),
    ]))
    first = orchestrator.build_deep_review(tmp_path, tmp_path / "baseline")
    old_id = first["findings"][0]["id"]

    _stub(monkeypatch, _packet([
        _raw("architecture", "cycle", "major", "src/b.py", "Cycle."),
    ]))
    second = orchestrator.build_deep_review(
        tmp_path, tmp_path / "current", baseline=tmp_path / "baseline")
    new_id = second["findings"][0]["id"]
    comparison = second["manifest"]["comparison"]

    assert old_id != new_id
    assert comparison["new"] == [new_id]
    assert comparison["resolved"] == []
    assert comparison["unverified_absent"] == [old_id]
    assert comparison["unchanged"] == []
    assert "moved" not in comparison
    assert comparison["move_mapping"] == "unknown"
    assert comparison["by_subject"] == {
        "architecture": {"new": 1, "resolved": 0, "unverified_absent": 1, "unchanged": 0}}
    assert comparison["totals"] == {
        "new": 1, "resolved": 0, "unverified_absent": 1, "unchanged": 0}


def test_fail_on_ignores_note_only_model_candidates(tmp_path, monkeypatch):
    _stub(monkeypatch, _packet([{
        "section": "architecture", "type": "model_candidate",
        "severity": "note", "keys": ["file:a.py"],
        "summary": "Candidate only.",
        "tags": ["model-review"],
    }]))
    result = orchestrator.build_deep_review(
        tmp_path, tmp_path / "report", fail_on="major")

    assert result["findings"][0]["severity"] == "note"
    assert result["failed"] is False
    assert result["manifest"]["truth_assessed"] is False


def test_baseline_absence_requires_comparable_current_coverage():
    old = [{"id": "dr-1234567890abcdef", "subject": "security"}]
    complete = {"status": "complete", "checks": ["scan"], "omitted": [],
                "skipped": [], "unknown": []}
    baseline = {"subjects": {"security": complete}, "profile": "full", "range": None,
                "truncation": {"normalization_findings_omitted": 0}}
    def compare(current, manifest=baseline):
        return orchestrator._baseline_comparison(
            old, [], sections={"security": current}, baseline_manifest=manifest,
            profile="full")

    for current in ({**complete, "status": "partial"},
                    {**complete, "status": "unknown"},
                    {**complete, "checks": ["unrelated"]},
                    {**complete, "omitted": ["scan unavailable"]}):
        comparison = compare(current)
        assert comparison["resolved"] == []
        assert comparison["unverified_absent"] == [old[0]["id"]]
    for manifest in (None, {**baseline, "profile": "security"},
                     {**baseline, "range": "main..HEAD"},
                     {**baseline, "subjects": {"security": {
                         **complete, "status": "partial"}}},
                     {**baseline, "subjects": {"security": {
                         **complete, "checks": ["other scan"]}}},
                     {**baseline, "subjects": {"security": {
                         **complete, "checks": []}}}):
        assert compare(complete, manifest)["unverified_absent"] == [old[0]["id"]]
    comparison = compare(complete)
    assert comparison["resolved"] == [old[0]["id"]]
    assert comparison["unverified_absent"] == []


def test_new_ids_are_partitioned_by_comparable_baseline_coverage():
    finding = {"id": "dr-1234567890abcdef", "subject": "security"}
    complete = {"status": "complete", "checks": ["scan"], "omitted": [],
                "skipped": [], "unknown": []}
    baseline = {"subjects": {"security": complete}, "profile": "full", "range": None,
                "truncation": {"normalization_findings_omitted": 0}}
    def compare(manifest, current=None):
        return orchestrator._baseline_comparison(
            [], [finding], sections={"security": current or complete},
            baseline_manifest=manifest, profile="full")

    result = compare(baseline)
    assert result["new"] == result["coverage_supported_new"] == [finding["id"]]
    assert result["unverified_new"] == []
    for manifest in (None, {**baseline, "profile": "security"},
                     {**baseline, "truncation": {"normalization_findings_omitted": 1}},
                     {**baseline, "subjects": {"security": {**complete, "status": "partial"}}}):
        result = compare(manifest)
        assert result["coverage_supported_new"] == []
        assert result["unverified_new"] == [finding["id"]]
    result = compare(baseline, {**complete, "checks": ["scan", "other"]})
    assert result["unverified_new"] == [finding["id"]]
