import hashlib
import re

import pytest

from ozagia.core.deep_review import orchestrator, range_review
from ozagia.core.deep_review.contract import canonical_json


def _stub_build(monkeypatch, packet, *, graph=(None, {
        "freshness": "unknown", "reason": "test graph omitted"})):
    monkeypatch.setattr(orchestrator.collect, "collect", lambda *a, **k: packet)
    monkeypatch.setattr(orchestrator, "_verified_priority_graph",
                        lambda root, **kwargs: graph)


def _packet(findings, sections=None, graph=None):
    return {"findings": findings, "sections": sections or {},
            "graph": graph or {"freshness": "unknown"}}


def test_evidence_excerpt_is_bounded_linked_and_hashed(tmp_path, monkeypatch):
    (tmp_path / "src").mkdir()
    source = tmp_path / "src" / "app.py"
    source.write_text("line one\nline two\nline three\nline four\n",
                      encoding="utf-8")
    packet = _packet([{
        "section": "architecture", "type": "cycle", "severity": "major",
        "keys": ["file:src/app.py"], "file": "src/app.py", "line": 3,
        "summary": "Import cycle."}])
    _stub_build(monkeypatch, packet)
    out = tmp_path / "report"
    result = orchestrator.build_deep_review(tmp_path, out)

    finding = result["findings"][0]
    expected = f"evidence/{finding['id']}-L3.txt"
    assert finding["excerpt_ref"] == expected
    excerpt = (out / expected).read_text(encoding="utf-8")
    assert "line three" in excerpt
    assert "line one" in excerpt and "line four" in excerpt
    digest = "sha256:" + hashlib.sha256(excerpt.encode("utf-8")).hexdigest()
    assert finding["excerpt_hash"] == digest
    assert finding["evidence"][0]["excerpt_ref"] == expected
    assert finding["evidence"][0]["excerpt_hash"] == digest
    manifest = result["manifest"]
    assert manifest["output_hashes"][expected] == (
        "sha256:" + hashlib.sha256((out / expected).read_bytes()).hexdigest())
    assert manifest["evidence"]["files"] == 1
    assert manifest["evidence"]["omitted"] == 0

    first = (out / expected).read_bytes()
    orchestrator.build_deep_review(tmp_path, out)
    assert (out / expected).read_bytes() == first


def test_reused_output_directory_does_not_retain_stale_report_artifacts(
        tmp_path, monkeypatch):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('ok')\n", encoding="utf-8")
    out = tmp_path / "report"
    first_packet = _packet([{
        "section": "security", "type": "candidate", "severity": "major",
        "keys": ["file:src/app.py"], "file": "src/app.py", "line": 1,
        "summary": "Candidate."}], sections={"security": {"checked": ["scan"]}})
    _stub_build(monkeypatch, first_packet)
    first = orchestrator.build_deep_review(tmp_path, out)
    assert first["manifest"]["evidence"]["files"] == 1
    assert list((out / "evidence").iterdir())
    stale_excerpt = next((out / "evidence").iterdir())

    # Simulate optional artifacts added by a prior CLI invocation.
    (out / "model-review.json").write_text("{}\n", encoding="utf-8")
    (out / "sbom.json").write_text("{}\n", encoding="utf-8")
    (out / "bench-results.json").write_text("{}\n", encoding="utf-8")
    unrelated = out / "notes.txt"
    unrelated.write_text("preserve caller file\n", encoding="utf-8")
    evidence_note = out / "evidence" / "notes.txt"
    evidence_note.write_text("preserve caller evidence\n", encoding="utf-8")
    _stub_build(monkeypatch, _packet([], sections={"security": {"checked": ["scan"]}}))

    second = orchestrator.build_deep_review(tmp_path, out, profile="security")

    assert second["manifest"]["evidence"]["files"] == 0
    assert not stale_excerpt.exists()
    assert evidence_note.read_text(encoding="utf-8") == "preserve caller evidence\n"
    assert not (out / "model-review.json").exists()
    assert not (out / "sbom.json").exists()
    assert not (out / "bench-results.json").exists()
    assert unrelated.read_text(encoding="utf-8") == "preserve caller file\n"


def test_reused_output_with_conflicting_directory_is_refused_before_cleanup(
        tmp_path, monkeypatch):
    out = tmp_path / "report"
    out.mkdir()
    (out / "README.md").write_text("existing caller content\n", encoding="utf-8")
    blocker = out / "findings.json"
    blocker.mkdir()
    (blocker / "keep.txt").write_text("keep\n", encoding="utf-8")
    _stub_build(monkeypatch, _packet([]))

    with pytest.raises(orchestrator.DeepReviewError,
                       match="non-report directories or special files"):
        orchestrator.build_deep_review(tmp_path, out)

    assert (out / "README.md").read_text(encoding="utf-8") == "existing caller content\n"
    assert (blocker / "keep.txt").read_text(encoding="utf-8") == "keep\n"
    assert not (out / "manifest.json").exists()


def test_evidence_excerpt_omitted_when_context_contains_secret(tmp_path, monkeypatch):
    source = tmp_path / "settings.py"
    source.write_text('API_KEY = "sk_live_1234567890abcdef"\n', encoding="utf-8")
    packet = _packet([{
        "section": "security", "type": "secret", "severity": "major",
        "keys": ["file:settings.py"], "file": "settings.py", "line": 1,
        "summary": "Potential credential."}])
    _stub_build(monkeypatch, packet)

    result = orchestrator.build_deep_review(tmp_path, tmp_path / "report")
    finding = result["findings"][0]
    assert "excerpt_ref" not in finding
    assert "secret-like content" in finding["excerpt_omitted"]
    assert not (tmp_path / "report" / "evidence").exists()


def test_evidence_excerpt_redacts_short_unquoted_secret_assignment(tmp_path,
                                                                   monkeypatch):
    source = tmp_path / "settings.py"
    source.write_text("API_KEY=small123\n", encoding="utf-8")
    packet = _packet([{
        "section": "security", "type": "secret", "severity": "major",
        "keys": ["file:settings.py"], "file": "settings.py", "line": 1,
        "summary": "Potential credential."}])
    _stub_build(monkeypatch, packet)

    result = orchestrator.build_deep_review(tmp_path, tmp_path / "report")
    finding = result["findings"][0]
    excerpt = (tmp_path / "report" / finding["excerpt_ref"]).read_text(
        encoding="utf-8")
    assert "small123" not in excerpt
    assert "API_KEY=<redacted:secret>" in excerpt


def test_evidence_excerpt_omitted_for_secret_in_adjacent_context_line(
        tmp_path, monkeypatch):
    source = tmp_path / "settings.py"
    source.write_text(
        "ordinary = True\nAPI_KEY = sk_live_1234567890abcdef\n",
        encoding="utf-8")
    packet = _packet([{
        "section": "security", "type": "config", "severity": "minor",
        "keys": ["file:settings.py"], "file": "settings.py", "line": 1,
        "summary": "Configuration candidate."}])
    _stub_build(monkeypatch, packet)

    result = orchestrator.build_deep_review(tmp_path, tmp_path / "report")
    finding = result["findings"][0]
    assert "excerpt_ref" not in finding
    assert "secret-like content" in finding["excerpt_omitted"]
    assert not (tmp_path / "report" / "evidence").exists()


def test_evidence_excerpt_reports_missing_symlink_and_oversized_files(
        tmp_path, monkeypatch):
    (tmp_path / "real.py").write_text("real\n", encoding="utf-8")
    (tmp_path / "link.py").symlink_to(tmp_path / "real.py")
    (tmp_path / "large.py").write_bytes(b"x" * (1024 * 1024 + 1))
    packet = _packet([
        {"section": "architecture", "type": "missing", "severity": "minor",
         "keys": ["file:missing.py"], "file": "missing.py", "line": 1},
        {"section": "architecture", "type": "linked", "severity": "minor",
         "keys": ["file:link.py"], "file": "link.py", "line": 1},
        {"section": "architecture", "type": "large", "severity": "minor",
         "keys": ["file:large.py"], "file": "large.py", "line": 1},
    ])
    _stub_build(monkeypatch, packet)
    out = tmp_path / "report"
    result = orchestrator.build_deep_review(tmp_path, out)
    by_type = {item["type"]: item for item in result["findings"]}

    assert by_type["missing"]["excerpt_omitted"] == "source file is missing"
    assert "symlink" in by_type["linked"]["excerpt_omitted"]
    assert "1 MiB" in by_type["large"]["excerpt_omitted"]
    assert all("excerpt_ref" not in item for item in by_type.values())
    assert result["manifest"]["evidence"]["files"] == 0
    assert result["manifest"]["evidence"]["omitted"] == 3
    assert not (out / "evidence").exists()


def test_evidence_excerpt_truncates_long_source_lines(tmp_path, monkeypatch):
    source = tmp_path / "long.py"
    source.write_text("short\n" + ("x" * 9000) + "\nshort again\n",
                      encoding="utf-8")
    packet = _packet([{
        "section": "architecture", "type": "long_line", "severity": "minor",
        "keys": ["file:long.py"], "file": "long.py", "line": 2,
        "summary": "Long source line."}])
    _stub_build(monkeypatch, packet)
    out = tmp_path / "report"
    finding = orchestrator.build_deep_review(
        tmp_path, out)["findings"][0]

    excerpt_path = out / finding["excerpt_ref"]
    assert excerpt_path.stat().st_size <= 8 * 1024
    assert excerpt_path.read_text(encoding="utf-8").count("\n") <= 40
    assert finding["evidence"][0]["excerpt_truncated"] is True


def test_manifest_graph_content_hash_and_unknown_reason(tmp_path, monkeypatch):
    packet = _packet([])
    _stub_build(monkeypatch, packet, graph=(
        {"nodes": [], "edges": []},
        {"freshness": "fresh", "reason": "test verified snapshot"}))
    out = tmp_path / "report"
    result = orchestrator.build_deep_review(tmp_path, out)
    expected = "sha256:" + hashlib.sha256(
        canonical_json({"nodes": [], "edges": []}).encode("utf-8")).hexdigest()
    assert result["manifest"]["graph_hash"] == expected
    assert result["manifest"]["graph"]["content_hash"] == expected
    assert result["manifest"]["graph_content_hash"] == {
        "status": "hashed", "hash": expected, "reason": None}

    unknown = tmp_path / "unknown"
    _stub_build(monkeypatch, packet, graph=(
        None, {"freshness": "unknown", "reason": "no usable snapshot"}))
    result = orchestrator.build_deep_review(tmp_path, unknown)
    assert result["manifest"]["graph_hash"] == "unknown"
    assert result["manifest"]["graph_content_hash"]["status"] == "unknown"
    assert result["manifest"]["graph_hash_reason"] == "no usable snapshot"
    assert "no usable snapshot" in (
        unknown / "00-method-and-scope.md").read_text(encoding="utf-8")


def test_report_hashes_and_readme_links_resolve(tmp_path, monkeypatch):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "auth.py").write_text("def login():\n    pass\n",
                                               encoding="utf-8")
    packet = _packet([
        {"section": "security", "type": "weak_auth", "severity": "major",
         "keys": ["file:src/auth.py"], "file": "src/auth.py", "line": 1,
         "summary": "Authentication is weak."},
        {"section": "architecture", "type": "cycle", "severity": "note",
         "keys": ["file:src/auth.py"], "summary": "Small cycle."},
    ], sections={"security": {"checked": ["patterns"]},
                 "architecture": {"checked": ["imports"]}})
    _stub_build(monkeypatch, packet)
    out = tmp_path / "report"
    result = orchestrator.build_deep_review(tmp_path, out)
    manifest = result["manifest"]
    for subject, row in manifest["subjects"].items():
        report = out / row["report"]
        digest = hashlib.sha256(report.read_bytes()).hexdigest()
        assert row["report_hash"] == digest
        assert manifest["output_hashes"][row["report"]] == "sha256:" + digest

    readme = (out / "README.md").read_text(encoding="utf-8")
    assert "## Top risks" in readme
    assert "[Findings index](./findings-index.md)" in readme
    assert "See the [findings index](./findings-index.md)" in readme
    links = re.findall(r"\]\(([^)]+)\)", readme)
    assert links
    for link in links:
        assert not link.startswith(("http://", "https://", "mailto:"))
        path, _, fragment = link.partition("#")
        target = (out / path.removeprefix("./")).resolve()
        assert target.is_file(), link
        if fragment:
            assert f'<a id="{fragment}"></a>' in target.read_text(
                encoding="utf-8"), link


def test_method_document_carries_versions_budgets_profiles_and_omissions(
        tmp_path, monkeypatch):
    packet = _packet([], sections={
        "performance": {
            "checked": ["measured_profile_reports"],
            "profile_reports": ["reports/profile.json"],
            "not_checked": [],
        },
        "security": {
            "checked": ["patterns"],
            "not_checked": [{"reason": "external scanner unavailable"}],
        },
    })
    _stub_build(monkeypatch, packet)
    out = tmp_path / "report"
    result = orchestrator.build_deep_review(tmp_path, out, budget_s=12)
    method = (out / "00-method-and-scope.md").read_text(encoding="utf-8")

    assert "## Tool versions" in method
    assert f"python: {result['manifest']['tool_versions']['python']}" in method
    assert "## Budgets and timings" in method
    assert "Total budget: 12 s" in method
    assert "manifest.json" in method
    assert "collect_s" in method and "compose_s" in method
    assert "## Profile reports" in method
    assert "reports/profile.json" in method
    assert "## Explicit omissions and unknowns" in method
    assert "external scanner unavailable" in method
    assert "test graph omitted" in method


def test_range_review_merges_every_section_and_records_blast_radius(
        tmp_path, monkeypatch):
    packet = _packet([], sections={
        "security": {
            "checked": ["repo_security"],
            "not_checked": [{"reason": "repo note"}],
        },
        "architecture": {"checked": ["import_graph"]},
    })
    _stub_build(monkeypatch, packet)
    change = {
        "findings": [
            {"section": "security", "type": "range_auth",
             "severity": "major", "keys": ["file:src/auth.py"],
             "summary": "Changed auth path."},
            {"section": "correctness", "type": "range_generic",
             "severity": "minor", "keys": ["file:src/x.py"],
             "summary": "Generic change."},
        ],
        "sections": {
            "security": {
                "checked": ["git_range_review"],
                "not_checked": [{"reason": "range note"}],
            },
            "correctness": {
                "checked": ["git_range_review"],
                "not_checked": [{"reason": "range note"}],
            },
        },
        "graph": {"freshness": "unknown"},
        "blast_radius": {"status": "omitted", "omissions": ["stale graph"]},
        "range": {"base": "a", "head": "b"},
    }
    monkeypatch.setattr(range_review, "collect_range",
                        lambda *a, **k: change)
    out = tmp_path / "report"
    result = orchestrator.build_deep_review(
        tmp_path, out, range_spec="a..b")
    manifest = result["manifest"]

    assert manifest["range_review"]["sections"] == [
        "correctness", "security"]
    assert manifest["range_review"]["range_subjects"] == {
        "correctness": "correctness", "security": "security"}
    assert manifest["range_review"]["range_subject"] == (
        manifest["range_review"]["range_subjects"])
    assert manifest["range_review"]["blast_radius"] == {
        "status": "omitted", "omissions": ["stale graph"]}
    security = manifest["subjects"]["security"]
    assert security["checks"] == ["repo_security", "git_range_review"]
    assert {"reason": "repo note"} in security["omitted"]
    assert {"reason": "range note"} in security["omitted"]
    by_type = {item["type"]: item["id"] for item in result["findings"]}
    assert by_type["range_auth"] in (out / "01-security.md").read_text(
        encoding="utf-8")
    correctness = (out / "11-correctness.md").read_text(encoding="utf-8")
    assert by_type["range_generic"] in correctness
    assert by_type["range_auth"] not in correctness


@pytest.mark.parametrize("error,word", [
    (TimeoutError("range deadline elapsed"), "timed out"),
    (RuntimeError("range exploded"), "unavailable"),
])
def test_range_review_failure_is_isolated_as_partial_coverage(
        tmp_path, monkeypatch, error, word):
    packet = _packet([{
        "section": "architecture", "type": "cycle", "severity": "major",
        "keys": ["file:a.py"], "summary": "Cyclic import."}],
        sections={"architecture": {"checked": ["imports"]}},
        graph={"freshness": "fresh"})
    _stub_build(monkeypatch, packet, graph=(
        {"nodes": [], "edges": []},
        {"freshness": "fresh", "reason": "test verified snapshot"}))
    monkeypatch.setattr(range_review, "collect_range",
                        lambda *a, **k: (_ for _ in ()).throw(error))
    out = tmp_path / "report"
    result = orchestrator.build_deep_review(
        tmp_path, out, range_spec="a..b")
    manifest = result["manifest"]

    assert manifest["status"] == "partial"
    assert manifest["complete"] is False
    assert manifest["range_review"]["status"] == "unavailable"
    assert manifest["range_review"]["sections"] == ["correctness"]
    assert word in manifest["range_review"]["error"]
    correctness = manifest["subjects"]["correctness"]
    assert correctness["status"] == "partial"
    omissions = [item.get("reason", item) if isinstance(item, dict) else item
                 for item in correctness["omitted"]]
    assert any("range review" in str(item) and word in str(item)
               for item in omissions)
    rendered = (out / "11-correctness.md").read_text(encoding="utf-8")
    assert "range review" in rendered and word in rendered
    assert (out / "README.md").is_file()
    finding_id = result["findings"][0]["id"]
    assert finding_id in (out / "06-architecture.md").read_text(encoding="utf-8")


def test_manifest_copies_doc_coverage_sbom_and_cve_state(
        tmp_path, monkeypatch):
    sbom = {
        "bomFormat": "CycloneDX", "specVersion": "1.5", "version": 1,
        "components": [{"type": "library", "name": "requests"}],
        "metadata": {"properties": [
            {"name": "ozagia:cve_state", "value": "unknown"}]},
    }
    coverage = {
        "docs_checked": ["README.md"], "docs_found": 1,
        "local_imports_checked": 2, "limitations": ["literal imports only"],
    }
    conflicts = [{"dependency": "requests", "constraints": ["2", "3"]}]
    packet = _packet([], sections={
        "dependencies": {
            "checked": ["dependency_manifest_inventory"],
            "inventory": {"cve_state": "unknown", "manifests": []},
            "sbom": sbom,
            "cve_state": "unknown",
            "version_conflicts": conflicts,
        },
        "docs": {
            "checked": ["local_documented_api_references"],
            "doc_coverage": coverage,
        },
    })
    _stub_build(monkeypatch, packet)
    out = tmp_path / "report"
    result = orchestrator.build_deep_review(tmp_path, out)
    manifest = result["manifest"]

    dependencies = manifest["subjects"]["dependencies"]
    assert dependencies["sbom"] == sbom
    assert dependencies["cve_state"] == "unknown"
    assert dependencies["version_conflicts"] == conflicts
    documents = manifest["subjects"]["documents-and-api"]
    assert documents["doc_coverage"] == coverage
    assert (out / "sbom.json").read_text(encoding="utf-8") == canonical_json(sbom)
    digest = "sha256:" + hashlib.sha256((out / "sbom.json").read_bytes()).hexdigest()
    assert manifest["output_hashes"]["sbom.json"] == digest
    assert manifest["sbom"] == {
        "report": "sbom.json", "report_hash": digest,
        "components": 1, "cve_state": "unknown"}


def test_sbom_is_not_written_when_the_dependencies_section_has_none(
        tmp_path, monkeypatch):
    packet = _packet([], sections={
        "dependencies": {
            "checked": ["dependency_manifest_inventory"],
            "inventory": {"cve_state": "unknown", "manifests": []},
        },
    })
    _stub_build(monkeypatch, packet)
    out = tmp_path / "report"
    result = orchestrator.build_deep_review(tmp_path, out)

    assert not (out / "sbom.json").exists()
    assert "sbom" not in result["manifest"]["subjects"]["dependencies"]
    assert "sbom.json" not in result["manifest"]["output_hashes"]
    assert result["manifest"]["subjects"]["dependencies"]["cve_state"] == "unknown"


def test_profile_evidence_and_unassessed_truth_are_preserved(
        tmp_path, monkeypatch):
    packet = _packet([{
        "section": "performance", "type": "profile_hotspot",
        "severity": "minor", "keys": ["file:src/hot.py"],
        "file": "src/hot.py", "summary": "Measured hotspot.",
        "truth_assessed": True,
        "profile_evidence": {
            "report": "reports/profile.json", "report_hash": "sha256:abc",
            "profile_hash": "sha256:def", "unit": "ms", "clock": "monotonic",
            "self": 12.5, "inclusive": 20.0, "count": 3,
        },
    }], sections={"performance": {"checked": ["measured_profile_reports"]}})
    _stub_build(monkeypatch, packet)
    out = tmp_path / "report"
    result = orchestrator.build_deep_review(tmp_path, out)
    finding = result["findings"][0]
    evidence = finding["evidence"][0]

    assert finding["truth_assessed"] is False
    assert result["manifest"]["truth_assessed"] is False
    assert evidence["kind"] == "measured"
    for key, value in (("report", "reports/profile.json"),
                       ("report_hash", "sha256:abc"),
                       ("profile_hash", "sha256:def"), ("unit", "ms"),
                       ("clock", "monotonic"), ("self", 12.5),
                       ("inclusive", 20.0), ("count", 3)):
        assert evidence[key] == value
