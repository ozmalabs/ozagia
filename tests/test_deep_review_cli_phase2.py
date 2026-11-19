"""Collector integration for the Phase 2 analyzer outputs.

The public report contract (orchestrator/reports) is owned elsewhere; these
tests assert only what collection must place in the packet and that collection
does not perform the orchestrator's writes.
"""
from __future__ import annotations

import json


def _base_packet(sections=None, findings=None):
    return {"findings": list(findings or []),
            "sections": dict(sections or {}),
            "graph": {"freshness": "fresh"}}


def test_dependencies_collect_exposes_sbom_cve_and_conflicts(tmp_path, monkeypatch):
    import ozagia.core.deep_review.collect as collector

    (tmp_path / "requirements.txt").write_text("requests>=2\n", encoding="utf-8")
    monkeypatch.setattr(collector.repo_review, "review_repo",
                        lambda *a, **k: _base_packet())

    packet = collector.collect(tmp_path, profile="dependencies", budget_s=5)
    section = packet["sections"]["dependencies"]

    assert section["cve_state"] == "unknown"
    assert section["sbom"]["bomFormat"] == "CycloneDX"
    assert section["version_conflicts"] == section["inventory"]["version_conflicts"]
    assert "commits" not in section["sbom"]
    # Collection is in-memory; the orchestrator owns the sbom.json artifact.
    assert not (tmp_path / "sbom.json").exists()


def test_local_advisory_match_flows_into_subject_report_and_manifest(
        tmp_path, monkeypatch):
    from ozagia.core.deep_review import build_deep_review
    import ozagia.core.deep_review.collect as collector
    from ozagia.core.deep_review.analyzers.local_advisories import SCHEMA

    (tmp_path / "pyproject.toml").write_text(
        '[project]\ndependencies = ["requests>=2"]\n', encoding="utf-8")
    (tmp_path / "uv.lock").write_text(
        'version = 1\n\n[[package]]\nname = "requests"\nversion = "2.31.0"\n',
        encoding="utf-8")
    monkeypatch.setattr(collector.repo_review, "review_repo",
                        lambda *a, **k: _base_packet())
    advisory_data = {"schema": SCHEMA, "advisories": [{
        "id": "CVE-TEST-1", "ecosystem": "python", "package": "requests",
        "affected_versions": ["2.31.0"], "severity": "major"}]}
    out = tmp_path.parent / (tmp_path.name + "-advisory-report")

    result = build_deep_review(tmp_path, out, profile="dependencies",
                               budget_s=5, local_advisories=advisory_data)

    report = result["manifest"]["subjects"]["dependencies"]
    finding = json.loads((out / "findings.json").read_text(encoding="utf-8"))[0]
    assert finding["type"] == "local_advisory_exact_version_match"
    assert finding["severity"] == "major"
    assert finding["locations"] == [{"path": "uv.lock"}]
    assert finding["evidence"][0]["source"].endswith("local_advisories")
    assert finding["evidence"][0]["method"] == "exact_lock_pin_match"
    assert finding["evidence"][0]["advisory_id"] == "CVE-TEST-1"
    assert finding["evidence"][0]["advisory_source_hash"].startswith("sha256:")
    assert report["inventory"]["local_advisories"]["state"] == "matches_found"
    assert report["cve_state"] == "unknown"


def test_docs_collect_stores_documentation_coverage(tmp_path, monkeypatch):
    import ozagia.core.deep_review.collect as collector

    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "api.py").write_text(
        "def current():\n    pass\n", encoding="utf-8")
    (tmp_path / "README.md").write_text(
        "from pkg.api import removed\n", encoding="utf-8")
    monkeypatch.setattr(collector.repo_review, "review_repo",
                        lambda *a, **k: _base_packet(
                            {"docs": {"checked": ["doc_links"],
                                      "not_checked": [{"reason": "history unavailable"}]}}))

    packet = collector.collect(tmp_path, profile="docs", budget_s=5)
    section = packet["sections"]["docs"]

    assert [item["type"] for item in packet["findings"]] == ["stale_documented_api"]
    coverage = section["doc_coverage"]
    assert coverage["docs_found"] == 1
    assert coverage["docs_checked"] == ["README.md"]
    assert coverage["removed_or_renamed_symbols"] == 1
    assert any("literal" in item.lower() for item in coverage["limitations"])
    # Exact existing coverage entries are preserved, not replaced.
    assert section["not_checked"] == [{"reason": "history unavailable"}]
    assert "api_coverage_gap" not in section


def test_docs_collect_reports_missing_coverage_api(tmp_path, monkeypatch):
    import ozagia.core.deep_review.collect as collector

    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "api.py").write_text("def present():\n    pass\n",
                                             encoding="utf-8")
    (tmp_path / "README.md").write_text(
        "from pkg.api import present\n", encoding="utf-8")
    monkeypatch.setattr(collector.repo_review, "review_repo",
                        lambda *a, **k: _base_packet())
    monkeypatch.setattr(collector.docs_api, "analyze_with_coverage", None)
    monkeypatch.setattr(
        collector.analyzers, "analyze_docs_api",
        lambda root, *, budget_s: ([], ["README.md"], []))
    monkeypatch.setattr(
        collector, "_parallel_analyzers",
        lambda tasks, **kwargs: {key: (True, function(*args, budget_s=5))
                                 for key, function, args, _options in tasks})

    packet = collector.collect(tmp_path, profile="docs", budget_s=5)
    section = packet["sections"]["docs"]

    # The legacy triple-return API still supplies findings and checked docs.
    assert section["api_docs_files_checked"] == ["README.md"]
    assert "doc_coverage" not in section
    assert "analyze_with_coverage is unavailable" in section["api_coverage_gap"]


def test_operations_coverage_merges_into_ops_and_routes_findings(
        tmp_path, monkeypatch):
    from ozagia.core.deep_review import build_deep_review
    import ozagia.core.deep_review.collect as collector

    (tmp_path / "Dockerfile").write_text("FROM alpine\n", encoding="utf-8")
    monkeypatch.setattr(collector.repo_review, "review_repo",
                        lambda *a, **k: _base_packet(
                            {"ops": {"checked": ["ops.tracked_sources"],
                                     "not_checked": [{"reason":
                                                      "deployment graph unavailable"}]}}))

    packet = collector.collect(tmp_path, profile="operations", budget_s=5)
    section = packet["sections"]["ops"]

    assert section["checked"] == ["ops.tracked_sources",
                                  "operational_file_inventory",
                                  "observability_declarations",
                                  "static_iac_config_candidates"]
    assert section["not_checked"] == [
        {"reason": "deployment graph unavailable"},
        {"reason": "IaC/config checks inspect selected JSON Kubernetes, optional Kubernetes/Compose YAML, literal Terraform ingress blocks, and selected lexical patterns; full semantic validation was not run"},
        {"reason": "no recognized runbook documents found; runbook coverage is unknown"},
    ]
    assert section["inventory"]["configuration_files"] == ["Dockerfile"]
    # The analyzer's finding section stays unchanged for the orchestrator.
    assert packet["findings"][0]["section"] == "operations"

    out = tmp_path / "report"
    result = build_deep_review(tmp_path, out, profile="operations", budget_s=5)
    assert result["manifest"]["subjects"]["operations"]["findings"]
    assert all(item["subject"] == "operations" for item in result["findings"])
    assert json.loads((out / "findings.json").read_text(encoding="utf-8"))


def test_static_iac_rule_ids_and_limitations_reach_final_report(
        tmp_path, monkeypatch):
    from ozagia.core.deep_review import build_deep_review
    import ozagia.core.deep_review.collect as collector

    (tmp_path / "main.tf").write_text(
        'resource "aws_security_group_rule" "ssh" {\n'
        '  type = "ingress"\n  from_port = 22\n  to_port = 22\n'
        '  cidr_blocks = ["0.0.0.0/0"]\n}\n', encoding="utf-8")
    monkeypatch.setattr(collector.repo_review, "review_repo",
                        lambda *a, **k: _base_packet())
    out = tmp_path.parent / (tmp_path.name + "-iac-report")

    result = build_deep_review(tmp_path, out, profile="operations", budget_s=5)

    report = result["manifest"]["subjects"]["operations"]
    finding = next(item for item in result["findings"]
                   if item["type"] == "public_admin_ingress")
    assert "rule:iac.terraform.public_admin_ingress" in finding["tags"]
    assert finding["evidence"][0]["rule_id"] == "iac.terraform.public_admin_ingress"
    assert finding["evidence"][0]["method"] == "static_config_rule"
    assert finding["locations"] == [{"path": "main.tf", "line": 5}]
    assert "static_iac_config_candidates" in report["checks"]
    assert any("full semantic validation was not run" in row["reason"]
               for row in report["omitted"])
