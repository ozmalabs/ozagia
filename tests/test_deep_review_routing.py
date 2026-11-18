import json

import pytest

from ozagia.core.deep_review import orchestrator
from ozagia.core.deep_review.reports import (
    MAP, SOURCE_SUBJECT, rank_findings, subject_for_section,
)


def _stub_build(monkeypatch, packet):
    monkeypatch.setattr(orchestrator.collect, "collect", lambda *a, **k: packet)
    monkeypatch.setattr(
        orchestrator, "_verified_priority_graph",
        lambda root, **kwargs: (None, {
            "freshness": "unknown", "reason": "test graph omitted"}))


@pytest.mark.parametrize("source,subject", [
    ("operations", "operations"),
    ("ops", "operations"),
    ("documents-and-api", "documents-and-api"),
    ("docs", "documents-and-api"),
    ("tests", "tests-and-coverage"),
    ("tests-and-coverage", "tests-and-coverage"),
    ("health", "architecture"),
    ("churn", "tests-and-coverage"),
])
def test_subject_routing_accepts_map_keys_values_and_health_churn(source, subject):
    assert subject_for_section(source) == subject


def test_subject_routing_map_still_owns_output_sections():
    assert MAP["operations"] == "ops"
    assert MAP["documents-and-api"] == "docs"
    assert MAP["tests-and-coverage"] == "tests"
    assert SOURCE_SUBJECT["health"] == "architecture"


def test_full_report_routes_operations_and_docs_without_correctness_fallthrough(
        tmp_path, monkeypatch):
    packet = {
        "findings": [
            {"section": "operations", "type": "runbook_missing",
             "severity": "minor", "keys": ["file:ops-runbook.md"],
             "summary": "Runbook is missing."},
            {"section": "documents-and-api", "type": "stale_documented_api",
             "severity": "minor", "keys": ["file:README.md"],
             "summary": "Documented API is stale."},
            {"section": "health", "type": "large_file",
             "severity": "note", "keys": ["file:big.py"],
             "summary": "Large file."},
            {"section": "churn", "type": "churn_untested",
             "severity": "note", "keys": ["file:changed.py"],
             "summary": "Changed code is untested."},
        ],
        "sections": {
            "operations": {"checked": ["operational_file_inventory"]},
            "docs": {"checked": ["local_documented_api_references"]},
            "architecture": {"checked": ["health"]},
            "tests": {"checked": ["churn"]},
        },
        "graph": {"freshness": "unknown"},
    }
    _stub_build(monkeypatch, packet)
    out = tmp_path / "report"
    result = orchestrator.build_deep_review(tmp_path, out)

    operations = (out / "09-operations.md").read_text(encoding="utf-8")
    docs = (out / "10-documents-and-api.md").read_text(encoding="utf-8")
    correctness = (out / "11-correctness.md").read_text(encoding="utf-8")
    architecture = (out / "06-architecture.md").read_text(encoding="utf-8")
    tests = (out / "07-tests-and-coverage.md").read_text(encoding="utf-8")
    by_type = {item["type"]: item["id"] for item in result["findings"]}
    assert by_type["runbook_missing"] in operations
    assert by_type["stale_documented_api"] in docs
    assert by_type["runbook_missing"] not in correctness
    assert by_type["stale_documented_api"] not in correctness
    assert by_type["large_file"] in architecture
    assert by_type["churn_untested"] in tests
    assert {item["subject"] for item in result["findings"]} == {
        "operations", "documents-and-api", "architecture",
        "tests-and-coverage"}
    assert result["manifest"]["subjects"]["operations"]["findings"]
    assert result["manifest"]["subjects"]["documents-and-api"]["findings"]


def test_max_findings_is_a_global_top_n_view_not_subject_prefix(
        tmp_path, monkeypatch):
    packet = {
        "findings": [
            {"section": "architecture", "type": "minor_cycle",
             "severity": "note", "keys": ["file:a.py"],
             "summary": "Small architecture note."},
            {"section": "tests", "type": "coverage_gap",
             "severity": "blocking", "keys": ["file:z.py"],
             "summary": "Late subject has the top risk."},
        ],
        "sections": {
            "architecture": {"checked": ["imports"]},
            "tests": {"checked": ["coverage_record"]},
        },
        "graph": {"freshness": "unknown"},
    }
    _stub_build(monkeypatch, packet)
    out = tmp_path / "report"
    result = orchestrator.build_deep_review(tmp_path, out, max_findings=1)

    assert len(result["findings"]) == 1
    returned = result["findings"][0]
    assert returned["type"] == "coverage_gap"
    assert returned["subject"] == "tests-and-coverage"
    saved = json.loads((out / "findings.json").read_text(encoding="utf-8"))
    by_type = {item["type"]: item["id"] for item in saved}
    assert set(by_type) == {"minor_cycle", "coverage_gap"}
    assert by_type["coverage_gap"] in (out / "07-tests-and-coverage.md").read_text(
        encoding="utf-8")
    assert by_type["minor_cycle"] in (out / "06-architecture.md").read_text(
        encoding="utf-8")
    truncation = result["manifest"]["truncation"]["max_findings"]
    assert truncation == {
        "limit": 1, "total": 2, "returned": 1, "truncated": True}


def test_global_rank_uses_severity_priority_and_blast_radius():
    findings = [
        {"id": "note-high", "severity": "note",
         "priority": {"score": 99, "blast_radius": {
             "reachable_callers": 99, "direct_symbols": 1}}},
        {"id": "major-low", "severity": "major",
         "priority": {"score": 70, "blast_radius": {
             "reachable_callers": 0, "direct_symbols": 1}}},
        {"id": "major-high", "severity": "major",
         "priority": {"score": 70, "blast_radius": {
             "reachable_callers": 4, "direct_symbols": 1}}},
        {"id": "major-high-score", "severity": "major",
         "priority": {"score": 72, "blast_radius": {
             "reachable_callers": 0, "direct_symbols": 1}}},
    ]
    assert [item["id"] for item in rank_findings(findings)] == [
        "major-high-score", "major-high", "major-low", "note-high"]
