import json

from ozagia.core.deep_review.contract import finding_id
from ozagia.core.deep_review.orchestrator import build_deep_review


def test_finding_id_ignores_line_numbers():
    a = {"type": "dangerous_construct", "keys": ["file:src/x.py"], "line": 2}
    b = {**a, "line": 40}
    assert finding_id("security", a) == finding_id("security", b)


def test_finding_id_ignores_collector_key_order():
    a = {"type": "dangerous_construct", "keys": ["file:b.py", "file:a.py"]}
    b = {"type": "dangerous_construct", "keys": ["file:a.py", "file:b.py"]}
    assert finding_id("security", a) == finding_id("security", b)


def test_finding_id_combines_rule_key_with_location():
    a = {"type": "unsafe_call", "rule_key": "rule-1", "keys": ["file:a.py"]}
    b = {"type": "unsafe_call", "rule_key": "rule-1", "keys": ["file:b.py"]}
    assert finding_id("security", a) != finding_id("security", b)
    assert finding_id("security", {**a, "keys": ["file:a.py", "file:b.py"]}) == \
        finding_id("security", {**a, "keys": ["file:b.py", "file:a.py"]})


def test_explicit_stable_key_remains_authoritative():
    a = {"type": "unsafe_call", "stable_key": "persistent:call-7",
         "rule_key": "rule-1", "keys": ["file:a.py"]}
    b = {**a, "rule_key": "rule-2", "keys": ["file:b.py"]}
    assert finding_id("security", a) == finding_id("security", b)


def test_finding_id_uses_location_symbol_without_line_numbers():
    a = {"type": "dangerous_construct", "locations": [
        {"path": "src/x.py", "symbol": "run", "line": 3}]}
    b = {"type": "dangerous_construct", "locations": [
        {"path": "src/x.py", "symbol": "run", "line": 90}]}
    c = {"type": "dangerous_construct", "locations": [
        {"path": "src/x.py", "symbol": "parse", "line": 3}]}
    assert finding_id("security", a) == finding_id("security", b)
    assert finding_id("security", a) != finding_id("security", c)


def test_subject_report_escapes_untrusted_table_values():
    from ozagia.core.deep_review.reports import render_subject
    rendered = render_subject("security", [{"id": "x|y", "severity": "major",
        "type": "<script>", "locations": [{"path": "a|b\nnext"}]}],
        status="partial", omissions=[])
    assert "x\\|y" in rendered
    assert "&lt;script&gt;" in rendered
    assert "a\\|b next" in rendered


def test_subject_report_escapes_markdown_in_untrusted_prose():
    from ozagia.core.deep_review.reports import render_subject
    rendered = render_subject("security", [], status="partial", omissions=[],
                              scope="[click](https://example.invalid) `code`",
                              checks=["**bold**"])
    assert r"\[click\]\(https://example.invalid\)" in rendered
    assert r"\`code\`" in rendered
    assert r"\*\*bold\*\*" in rendered


def test_subject_report_renders_scope_and_coverage_fields():
    from ozagia.core.deep_review.reports import render_subject
    rendered = render_subject("security", [], status="partial", omissions=["graph stale"],
                              scope="Offline scan.", checks=["source scan"],
                              skipped=["external tools disabled"],
                              unknown=["runtime reachability"])
    assert "Offline scan." in rendered
    assert "source scan" in rendered
    assert "external tools disabled" in rendered
    assert "runtime reachability" in rendered
    assert "graph stale" in rendered


def test_subject_report_renders_scope_and_coverage_details():
    from ozagia.core.deep_review.reports import render_subject
    rendered = render_subject("security", [], status="partial", omissions=["no auth checks"],
                              checks=["secret scan"], skipped=["optional tool missing"],
                              unknown=["CVE state unavailable"])
    assert "Checks: secret scan" in rendered
    assert "Skipped: optional tool missing" in rendered
    assert "Unknown: CVE state unavailable" in rendered
    assert "Omissions: no auth checks" in rendered


def test_reports_are_deterministic_and_mark_unimplemented_subjects(tmp_path, monkeypatch):
    import ozagia.core.deep_review.orchestrator as orchestrator
    packet = {"findings": [{"section": "architecture", "type": "import_cycle",
                            "severity": "major", "keys": ["file:a.py"],
                            "summary": "cycle"},
                           {"section": "security", "type": "secret_candidate",
                            "severity": "blocking", "keys": ["file:b.py"],
                            "summary": "secret candidate"}],
              "sections": {"architecture": {}}, "graph": {"fresh": True}}
    monkeypatch.setattr(orchestrator.collect, "collect", lambda *a, **k: packet)
    out = tmp_path / "report"
    build_deep_review(tmp_path, out)
    first = {p.name: p.read_bytes() for p in out.iterdir()}
    build_deep_review(tmp_path, out)
    second = {p.name: p.read_bytes() for p in out.iterdir()}
    assert {name: data for name, data in first.items() if name != "manifest.json"} == {
        name: data for name, data in second.items() if name != "manifest.json"}
    assert {item["type"] for item in json.loads(
        (out / "findings.json").read_text())} == {"import_cycle", "secret_candidate"}
    index = (out / "findings-index.md").read_text()
    assert index.index("## Blocking") < index.index("## Major")
    assert index.index("### Security") < index.index("### Architecture")
    assert "01-security.md#finding-" in index
    assert "06-architecture.md#finding-" in index
    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["subjects"]["performance"]["status"] == "unknown"
    assert manifest["revision"] is None or len(manifest["revision"]) == 40
    assert manifest["tool_versions"]["python"]
    assert manifest["budgets"]["total_s"] == 60
    assert manifest["timings"]["collect_s"] >= 0
    assert manifest["output_hashes"] == json.loads(
        second["manifest.json"])["output_hashes"]


def test_opt_in_bench_is_separate_and_does_not_change_findings_or_gate(
        tmp_path, monkeypatch):
    import hashlib
    import time
    import ozagia.core.deep_review.orchestrator as orchestrator

    packet = {"findings": [{"section": "security", "type": "candidate",
                            "severity": "major", "keys": ["file:src/a.py"]}],
              "sections": {"security": {"checked": ["scan"]}},
              "graph": {"freshness": "fresh"}}
    monkeypatch.setattr(orchestrator.collect, "collect", lambda *a, **k: packet)
    monkeypatch.setattr(orchestrator, "_verified_priority_graph",
                        lambda *a, **k: (None, {"freshness": "missing"}))
    calls = []

    def fake_bench(root, commands, **kwargs):
        calls.append((commands, kwargs))
        time.sleep(0.03)
        return {
            "budget_s": kwargs["budget_s"], "elapsed_s": 0.01,
            "output_limit_bytes_per_stream": 100,
            "limitations": ["single run"],
            "commands": [{"label": "command-1", "status": "completed",
                          "returncode": 0, "duration_ms": 9,
                          "stdout": "secret-output", "stderr": "secret-error",
                          "stdout_truncated": False, "stderr_truncated": False}],
        }

    monkeypatch.setattr(orchestrator.bench_runner, "run_bench", fake_bench)
    argv = ["python", "-c", "print('argv-secret')"]
    result = build_deep_review(
        tmp_path, tmp_path / "with-bench", fail_on="major",
        bench_commands=[argv], bench_timeout_s=4)

    artifact_path = tmp_path / "with-bench" / "bench-results.json"
    artifact_text = artifact_path.read_text(encoding="utf-8")
    artifact = json.loads(artifact_text)
    row = artifact["commands"][0]
    assert calls[0][0] == [argv]
    assert calls[0][1]["enabled"] is True
    assert calls[0][1]["timeout_s"] == 4
    assert row["command_sha256"] == "sha256:" + hashlib.sha256(
        (json.dumps(argv, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()).hexdigest()
    assert row["status"] == "completed"
    assert "stdout" not in row and "stderr" not in row
    assert "argv-secret" not in artifact_text
    assert "secret-output" not in artifact_text
    assert "secret-error" not in artifact_text
    assert result["failed"] is True
    assert result["manifest"]["budgets"]["elapsed_s"] >= 0.03
    assert result["manifest"]["budgets"]["remaining_s"] == max(
        0.0, result["manifest"]["budgets"]["total_s"]
        - result["manifest"]["budgets"]["elapsed_s"])
    assert result["manifest"]["bench"]["report"] == "bench-results.json"
    assert "bench-results.json" in result["manifest"]["output_hashes"]
    plain = build_deep_review(tmp_path, tmp_path / "without-bench", fail_on="major")
    assert result["findings"] == plain["findings"]
    assert result["failed"] == plain["failed"]


def test_baseline_and_fail_threshold(tmp_path, monkeypatch):
    import ozagia.core.deep_review.orchestrator as orchestrator
    packet = {"findings": [{"section": "security", "type": "dangerous_construct",
                            "severity": "major", "keys": ["file:x.py"]}],
              "sections": {"security": {"checked": ["patterns"], "not_checked": []}},
              "graph": {"freshness": "fresh"}}
    monkeypatch.setattr(orchestrator.collect, "collect", lambda *a, **k: packet)
    baseline = tmp_path / "baseline"
    build_deep_review(tmp_path, baseline)
    result = build_deep_review(tmp_path, tmp_path / "next", baseline=baseline,
                               fail_on="major")
    assert result["failed"] is True
    assert result["manifest"]["comparison"]["unchanged"]
    assert (tmp_path / "next" / "00-method-and-scope.md").is_file()


def test_phase_one_static_patterns_and_subject_reports(tmp_path, monkeypatch):
    from ozagia.core.deep_review import analyzers
    source = '''\
def quadratic(items):
    for x in items:
        for y in items:
            pass

def swallowed():
    try:
        work()
    except Exception:
        pass

def shared(value=[]):
    return value

def materialize(n):
    return list(range(n))
'''
    (tmp_path / "sample.py").write_text(source, encoding="utf-8")
    findings, checked, skipped = analyzers.analyze_static(tmp_path)
    assert checked == ["sample.py"]
    assert skipped == []
    assert {item["section"] for item in findings} == {
        "performance", "failure-modes", "data-structures", "scalability"}
    monkeypatch.setattr("ozagia.core.deep_review.collect.repo_review.review_repo",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("unexpected base review")))
    result = build_deep_review(tmp_path, tmp_path / "report", profile="performance")
    assert result["manifest"]["subjects"]["performance"]["status"] == "partial"
    got = json.loads((tmp_path / "report" / "findings.json").read_text())
    assert [item["type"] for item in got] == ["repeated_nested_iteration"]


def test_static_analyzer_flags_only_explicitly_unbounded_function_caches(tmp_path):
    source = '''\
from functools import cache as memo, lru_cache
import functools
import custom

@memo
def cached(value):
    return value

@lru_cache(maxsize=None)
def also_cached(value):
    return value

@functools.lru_cache(None)
def positional_none(value):
    return value

@functools.cache
def qualified_cache(value):
    return value

@lru_cache(maxsize=32)
def bounded(value):
    return value

@functools.lru_cache()
def default_bounded(value):
    return value

@custom.cache
def custom_cache(value):
    return value

@cache
def unimported_cache(value):
    return value

def helper():
    from functools import cache

@cache
def name_from_local_import(value):
    return value
'''
    (tmp_path / "cache.py").write_text(source, encoding="utf-8")
    from ozagia.core.deep_review.analyzers.static import analyze

    findings, checked, skipped = analyze(tmp_path)
    cache_findings = [item for item in findings
                      if item["type"] == "unbounded_function_cache"]
    assert checked == ["cache.py"]
    assert skipped == []
    assert [item["locations"][0]["symbol"] for item in cache_findings] == [
        "cached", "also_cached", "positional_none", "qualified_cache"]
    assert all(item["section"] == "scalability" for item in cache_findings)
    assert all(item["confidence"] == "low" for item in cache_findings)


def test_invalid_max_findings_fails_before_collection(tmp_path, monkeypatch):
    import pytest
    import ozagia.core.deep_review.orchestrator as orchestrator
    monkeypatch.setattr(orchestrator.collect, "collect",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("collected")))
    with pytest.raises(ValueError, match="max_findings"):
        build_deep_review(tmp_path, tmp_path / "out", max_findings="bad")


def test_dependency_analyzer_is_collected_with_inventory(tmp_path, monkeypatch):
    from ozagia.core.deep_review.collect import collect
    (tmp_path / "requirements.txt").write_text("requests>=2\n", encoding="utf-8")
    review_options = {}

    def review_repo(*_args, **kwargs):
        review_options.update(kwargs)
        return {"findings": [], "sections": {},
                "graph": {"freshness": "fresh"}}

    monkeypatch.setattr("ozagia.core.deep_review.collect.repo_review.review_repo",
                        review_repo)
    packet = collect(tmp_path, profile="dependencies", budget_s=2)
    assert packet["findings"][0]["type"] == "missing_lock_file"
    assert packet["sections"]["dependencies"]["inventory"]["manifests"][0]["file"] == "requirements.txt"
    assert review_options["probe_interpreters"] is False


def test_dependency_collection_never_probes_target_interpreters(tmp_path, monkeypatch):
    from ozagia.core.deep_review.collect import collect
    from ozagia.core import environments

    (tmp_path / "app.py").write_text("import requests\n", encoding="utf-8")

    def unexpected_probe(*_args, **_kwargs):
        raise AssertionError("target interpreter probing must remain disabled")

    monkeypatch.setattr(environments, "discover", unexpected_probe)
    monkeypatch.setattr(environments, "probe_one", unexpected_probe)
    collect(tmp_path, profile="full", budget_s=3)


def test_dependency_analyzer_reports_unparseable_manifest_coverage(tmp_path):
    from ozagia.core.deep_review.analyzers.dependencies import analyze
    (tmp_path / "package.json").write_text("{broken", encoding="utf-8")
    result = analyze(tmp_path, budget_s=2)
    assert result["checked"] is False
    assert result["not_checked"] == [
        {"reason": "package.json: manifest is invalid or unsupported"}]


def test_security_adapter_is_offline_bounded_and_preserves_evidence(tmp_path, monkeypatch):
    from ozagia.core.deep_review.collect import collect
    calls = []
    def scan(root, *, timeout, **kwargs):
        calls.append(timeout)
        return {
            "findings": [{"file": "src/app.py", "line": 12,
                          "source": {"rule": "unsafe_shell"},
                          "severity": "high", "confidence": "high",
                          "message": "Shell input may be unsafe.",
                          "remediation": "Validate the input.",
                          "evidence": {"kind": "line", "snippet": "secret=never-copy"},
                          "truth_assessed": False}],
            "not_assessed": ["runtime reachability"],
            "inputs": {"files_scanned": 3}, "summary": {"omitted": 0},
            "graph": {"freshness": "missing"}}
    monkeypatch.setattr("ozagia.core.deep_review.collect.repo_review.review_repo",
                        lambda *a, **k: {"findings": [], "sections": {},
                                         "graph": {"freshness": "unknown"}})
    monkeypatch.setattr("ozagia.core.deep_review.collect._bounded_security_scan", scan)
    monkeypatch.setattr("ozagia.core.deep_review.collect._bounded_trust_scan",
                        lambda *a, **k: {"findings": [], "checked": 0,
                                         "not_checked": []})
    packet = collect(tmp_path, profile="security", budget_s=5)
    assert len(calls) == 1 and 4.9 < calls[0] <= 5
    assert packet["findings"][0]["section"] == "security"
    assert packet["findings"][0]["line"] == 12
    assert "secret=never-copy" not in str(packet)
    assert packet["sections"]["security"]["checked"] == [
        "offline_security_scan", "python_trust_boundary_candidates"]
    assert packet["sections"]["security"]["not_checked"] == [
        {"reason": "runtime reachability"}]


def test_security_adapter_preserves_repo_review_coverage(tmp_path, monkeypatch):
    from ozagia.core.deep_review.collect import collect
    monkeypatch.setattr("ozagia.core.deep_review.collect.repo_review.review_repo",
        lambda *a, **k: {"findings": [], "sections": {"security": {
            "checked": ["security.sensitive_files"],
            "not_checked": [{"reason": "tracked file list incomplete"}] }},
            "graph": {"freshness": "unknown"}})
    monkeypatch.setattr("ozagia.core.deep_review.collect._bounded_security_scan",
        lambda *a, **k: {"findings": [], "not_assessed": [],
                         "inputs": {"files_scanned": 2}, "summary": {}, "graph": {}})
    monkeypatch.setattr("ozagia.core.deep_review.collect._bounded_trust_scan",
        lambda *a, **k: {"findings": [], "checked": 0, "not_checked": []})
    section = collect(tmp_path, profile="security", budget_s=5)["sections"]["security"]
    assert section["checked"] == ["security.sensitive_files", "offline_security_scan",
                                  "python_trust_boundary_candidates"]
    assert section["not_checked"] == [{"reason": "tracked file list incomplete"}]


def test_security_scan_failure_preserves_existing_coverage(tmp_path, monkeypatch):
    from ozagia.core.deep_review.collect import collect
    monkeypatch.setattr("ozagia.core.deep_review.collect.repo_review.review_repo",
        lambda *a, **k: {"findings": [], "sections": {"security": {
            "checked": ["security.sensitive_files"], "not_checked": []}},
            "graph": {"freshness": "unknown"}})
    def unavailable(*args, **kwargs):
        raise TimeoutError("timed out")
    monkeypatch.setattr("ozagia.core.deep_review.collect._bounded_security_scan", unavailable)
    section = collect(tmp_path, profile="security", budget_s=5)["sections"]["security"]
    assert section["checked"] == ["security.sensitive_files"]
    assert "timed out" in section["not_checked"][0]["reason"]


def test_trust_boundary_adapter_adds_safe_provenance_and_preserves_coverage(
        tmp_path, monkeypatch):
    from ozagia.core.deep_review.collect import collect
    monkeypatch.setattr("ozagia.core.deep_review.collect.repo_review.review_repo",
        lambda *a, **k: {"findings": [], "sections": {"security": {
            "checked": ["security.sensitive_files"], "not_checked": []}},
            "graph": {"freshness": "unknown"}})
    monkeypatch.setattr("ozagia.core.deep_review.collect._bounded_security_scan",
        lambda *a, **k: {"findings": [], "not_assessed": [],
                         "inputs": {"files_scanned": 1}, "summary": {}, "graph": {}})
    monkeypatch.setattr("ozagia.core.deep_review.collect._bounded_trust_scan",
        lambda *a, **k: {"findings": [{"rule": "trust-flow-command",
            "category": "command", "file": "src/app.py", "line": 9,
            "source_line": 7, "message": "external input may reach command sink run()",
            "cwe": "cwe:78"}], "checked": 2, "not_checked": []})
    packet = collect(tmp_path, profile="security", budget_s=5)
    finding = packet["findings"][0]
    assert finding["type"] == "trust-flow-command"
    assert finding["severity"] == "minor" and finding["confidence"] == "low"
    assert finding["security_evidence"]["source_line"] == 7
    assert finding["truth_assessed"] is False
    section = packet["sections"]["security"]
    assert "security.sensitive_files" in section["checked"]
    assert section["trust_boundary_files_checked"] == 2


def test_trust_boundary_adapter_deduplicates_existing_sink_rule(tmp_path, monkeypatch):
    from ozagia.core.deep_review.collect import collect
    monkeypatch.setattr("ozagia.core.deep_review.collect.repo_review.review_repo",
        lambda *a, **k: {"findings": [], "sections": {},
                         "graph": {"freshness": "unknown"}})
    monkeypatch.setattr("ozagia.core.deep_review.collect._bounded_security_scan",
        lambda *a, **k: {"findings": [{"file": "src/app.py", "line": 9,
            "source": {"rule": "shell-true"}, "severity": "high",
            "confidence": "high", "message": "shell enabled", "remediation": "use argv",
            "evidence": {}}], "not_assessed": [], "inputs": {}, "summary": {}, "graph": {}})
    monkeypatch.setattr("ozagia.core.deep_review.collect._bounded_trust_scan",
        lambda *a, **k: {"findings": [{"rule": "trust-flow-command",
            "category": "command", "file": "src/app.py", "line": 9,
            "source_line": 7, "message": "external input may reach command sink run()"}],
            "checked": 1, "not_checked": []})
    packet = collect(tmp_path, profile="security", budget_s=5)
    assert len(packet["findings"]) == 1
    assert packet["findings"][0]["type"] == "shell-true"


def test_finding_message_populates_title_and_impact(tmp_path, monkeypatch):
    import ozagia.core.deep_review.orchestrator as orchestrator
    packet = {"findings": [{"section": "architecture", "type": "cycle",
                            "severity": "minor", "keys": ["file:a.py"],
                            "message": "Cycle may prevent clean layering."}],
              "sections": {}, "graph": {"freshness": "fresh"}}
    monkeypatch.setattr(orchestrator.collect, "collect", lambda *a, **k: packet)
    result = build_deep_review(tmp_path, tmp_path / "report")
    finding = result["findings"][0]
    assert finding["title"] == finding["impact"] == "Cycle may prevent clean layering."


def test_findings_json_marks_every_deterministic_finding_unassessed(tmp_path, monkeypatch):
    import ozagia.core.deep_review.orchestrator as orchestrator
    packet = {"findings": [
        {"section": "security", "type": "unsafe_shell", "severity": "major",
         "keys": ["file:src/app.py"], "summary": "Input reaches a shell."},
        {"section": "performance", "type": "slow_loop", "severity": "minor",
         "keys": ["file:src/work.py"], "summary": "Loop does repeated I/O."},
    ], "sections": {}, "graph": {"freshness": "fresh"}}
    monkeypatch.setattr(orchestrator.collect, "collect", lambda *a, **k: packet)
    out = tmp_path / "report"
    result = build_deep_review(tmp_path, out)
    findings = json.loads((out / "findings.json").read_text(encoding="utf-8"))
    assert findings and all(item["truth_assessed"] is False for item in findings)
    assert result["findings"] and all(
        item["truth_assessed"] is False for item in result["findings"])


def test_unverified_collector_verified_claims_are_downgraded_and_reported(
        tmp_path, monkeypatch):
    import ozagia.core.deep_review.orchestrator as orchestrator
    packet = {
        "findings": [{
            "section": "security", "type": "scanner_candidate",
            "severity": "major", "keys": ["file:src/app.py"],
            "evidence_kind": "verified", "summary": "Scanner candidate.",
        }, {
            "section": "security", "type": "trust_candidate",
            "severity": "minor", "keys": ["file:src/input.py"],
            "evidence_kind": "declared",
            "security_evidence": {"kind": "verified"},
            "summary": "Trust candidate.",
        }],
        "sections": {"security": {"checked": ["offline scan"]}},
        "graph": {"freshness": "missing"},
    }
    monkeypatch.setattr(orchestrator.collect, "collect", lambda *a, **k: packet)

    result = build_deep_review(tmp_path, tmp_path / "report")

    findings = {item["type"]: item for item in result["findings"]}
    for finding in findings.values():
        assert finding["evidence_kind"] == "observed"
        assert finding["truth_assessed"] is False
        assert finding["evidence"][0]["verification_claim_downgraded"] is True
        assert "independently checkable verification result" in (
            finding["evidence"][0]["verification_reason"])
    omissions = result["manifest"]["subjects"]["security"]["omitted"]
    assert any("downgraded to observed" in row["reason"] for row in omissions)


def test_plan_hash_is_stable_across_output_paths_and_tracks_input_changes(
        tmp_path, monkeypatch):
    import ozagia.core.deep_review.orchestrator as orchestrator
    root = tmp_path / "repo"
    root.mkdir()
    source = root / "app.py"
    source.write_text("value = 1\n", encoding="utf-8")
    secret = "private-token-value"
    (root / ".env").write_text(f"TOKEN={secret}\n", encoding="utf-8")
    packet = {"findings": [],
              "sections": {"security": {"checked": ["offline scan"]}},
              "graph": {"freshness": "missing", "reason": "test fixture"}}
    monkeypatch.setattr(orchestrator.collect, "collect", lambda *a, **k: packet)

    first = build_deep_review(root, root / "report-one", profile="security")
    second = build_deep_review(root, root / "report-two", profile="security")

    assert first["manifest"]["plan_hash"].startswith("sha256:")
    assert first["manifest"]["plan_hash"] == second["manifest"]["plan_hash"]
    plan = first["manifest"]["plan"]
    assert plan["schema"] == "ozagia.deep_review.plan.v1"
    assert plan["inputs"]["workspace"]["state"] == "complete"
    assert str(root / "report-one") not in json.dumps(plan)
    assert secret not in json.dumps(plan)

    source.write_text("value = 2\n", encoding="utf-8")
    changed = build_deep_review(root, root / "report-three", profile="security")
    assert changed["manifest"]["plan_hash"] != first["manifest"]["plan_hash"]

    changed_option = build_deep_review(
        root, root / "report-four", profile="security", max_findings=0)
    assert changed_option["manifest"]["plan_hash"] != changed["manifest"]["plan_hash"]


def test_plan_hash_rejects_untrusted_digest_shapes_and_marks_unprocessed_inputs(
        tmp_path):
    import time
    import ozagia.core.deep_review.orchestrator as orchestrator

    digest, plan = orchestrator._plan_hash(
        root=tmp_path, out=tmp_path / "report", budget_available=False,
        revision=None, graph_hash=None, profile="full", range_spec=None,
        budget_s=10, index=False, fail_on="high", max_findings=None,
        security_tools=[], security_network=False,
        local_advisories="advisories.json", license_policy="policy.json",
        baseline=None, baseline_findings=[], profile_reports=["report.json"],
        bench_commands=None, bench_timeout_s=0, tool_versions={},
        sections={"performance": {"profile_report_hashes": [
            "attacker-controlled-secret"]},
            "dependencies": {"inventory": {"local_advisories": {
                "source_hash": "not-a-hash"}, "license_policy": {
                "policy_hash": "also-not-a-hash"}}}},
        findings=[{"profile_evidence": {"report_hash": "bad"}}],
        security_provenance=None, deadline=time.monotonic() + 10)

    assert digest.startswith("sha256:")
    assert plan["inputs"]["profile_reports"] == {
        "state": "partial_hashes_unavailable", "supplied": 1, "sha256": []}
    assert plan["inputs"]["local_advisories"] == {
        "state": "supplied_unprocessed", "sha256": None}
    assert plan["inputs"]["license_policy"] == {
        "state": "supplied_unprocessed", "sha256": None}
    assert "attacker-controlled-secret" not in json.dumps(plan)


def test_workspace_plan_digest_marks_unreadable_walks_partial(tmp_path, monkeypatch):
    import time
    import ozagia.core.deep_review.orchestrator as orchestrator

    def walk(root, *, followlinks, onerror):
        onerror(PermissionError("permission denied"))
        yield str(root), [], []

    monkeypatch.setattr(orchestrator.os, "walk", walk)

    digest = orchestrator._workspace_input_digest(
        tmp_path, tmp_path / "report", deadline=time.monotonic() + 5)

    assert digest["state"] == "partial_walk_error"
    assert digest["walk_errors"] == 1


def test_prior_report_recognition_is_bounded_and_checks_artifact_hashes(
        tmp_path, monkeypatch):
    import time
    import ozagia.core.deep_review.orchestrator as orchestrator

    root = tmp_path / "repo"
    root.mkdir()
    (root / "app.py").write_text("value = 1\n", encoding="utf-8")
    packet = {"findings": [], "sections": {},
              "graph": {"freshness": "missing", "reason": "test fixture"}}
    monkeypatch.setattr(orchestrator.collect, "collect", lambda *a, **k: packet)
    report = root / "report-old"
    build_deep_review(root, report, profile="security")

    manifest_path = report / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    # The report remains genuine if irrelevant JSON metadata makes its manifest
    # larger than the old 64 KiB recognition cap.
    manifest["padding"] = "x" * (70 * 1024)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    assert orchestrator._is_deep_review_output(
        root, report, deadline=time.monotonic() + 5)

    # A schema-shaped manifest without matching artifact hashes must not cause
    # a user directory to disappear from the workspace fingerprint.
    forged = root / "report-forged"
    forged.mkdir()
    fake_hashes = {name: "sha256:" + "0" * 64 for name in (
        "00-method-and-scope.md", "findings.json", "findings-index.md",
        "README.md")}
    (forged / "manifest.json").write_text(json.dumps({
        "schema": "ozagia.deep_review.manifest.v1",
        "plan_hash_schema": "ozagia.deep_review.plan.v1",
        "plan_hash": "sha256:" + "1" * 64,
        "output_hashes": fake_hashes,
    }), encoding="utf-8")
    for name in fake_hashes:
        (forged / name).write_text("user data\n", encoding="utf-8")
    assert not orchestrator._is_deep_review_output(
        root, forged, deadline=time.monotonic() + 5)

    # Even a once-valid report stops being excluded when a core artifact is
    # changed after the manifest was written.
    with (report / "findings.json").open("a", encoding="utf-8") as handle:
        handle.write("\n")
    assert not orchestrator._is_deep_review_output(
        root, report, deadline=time.monotonic() + 5)



def test_duplicate_findings_merge_evidence_and_manifest_hashes(tmp_path, monkeypatch):
    import hashlib
    import ozagia.core.deep_review.orchestrator as orchestrator
    (tmp_path / "x.py").write_text("pass\n", encoding="utf-8")
    raw = {"section": "architecture", "type": "cycle", "severity": "major",
           "keys": ["file:x.py"], "file": "x.py", "summary": "cycle"}
    packet = {"findings": [raw, {**raw, "recommendation": "break the cycle"}],
              "sections": {"architecture": {"checked": ["imports"]}},
              "graph": {"freshness": "fresh"}}
    monkeypatch.setattr(orchestrator.collect, "collect", lambda *a, **k: packet)
    out = tmp_path / "report"
    result = build_deep_review(tmp_path, out)
    findings = json.loads((out / "findings.json").read_text())
    assert len(findings) == 1
    assert findings[0]["recommendation"] == "break the cycle"
    assert findings[0]["evidence"][0]["hash"].startswith("sha256:")
    manifest = result["manifest"]
    assert manifest["output_hashes"]["findings.json"] == "sha256:" + hashlib.sha256(
        (out / "findings.json").read_bytes()).hexdigest()
    assert "## Scorecard" in (out / "README.md").read_text()
    assert manifest["status"] == "partial"


def test_output_artifact_hashing_streams_large_files(tmp_path):
    import hashlib
    import ozagia.core.deep_review.orchestrator as orchestrator

    artifact = tmp_path / "large-report.json"
    contents = b"x" * (2 * 1024 * 1024 + 17)
    artifact.write_bytes(contents)

    assert orchestrator._hash_output_artifact(artifact) == (
        "sha256:" + hashlib.sha256(contents).hexdigest())


def test_output_artifact_hashing_rejects_fifo_without_blocking(tmp_path):
    import os
    import subprocess
    import sys

    fifo = tmp_path / "report.json"
    os.mkfifo(fifo)
    result = subprocess.run(
        [sys.executable, "-c", (
            "from pathlib import Path; "
            "from ozagia.core.deep_review.orchestrator import _hash_output_artifact; "
            "_hash_output_artifact(Path(__import__('sys').argv[1]))"), str(fifo)],
        capture_output=True, timeout=5, check=False)
    assert result.returncode != 0
    assert b"not a regular file" in result.stderr


def test_output_artifact_hashing_rejects_symlink(tmp_path):
    import pytest
    from ozagia.core.deep_review.orchestrator import _hash_output_artifact

    target = tmp_path / "target"
    target.write_text("content")
    link = tmp_path / "report.json"
    link.symlink_to(target)
    with pytest.raises(OSError):
        _hash_output_artifact(link)


def test_identity_keys_are_not_fabricated_as_file_locations(tmp_path, monkeypatch):
    import ozagia.core.deep_review.orchestrator as orchestrator
    packet = {
        "findings": [{
            "section": "dependencies", "type": "conflicting_dependency_versions",
            "keys": ["dependency:maven:org.example:widget"],
            "summary": "Dependency versions conflict.",
            "scope": "Conflict is reported across inventory records.",
        }, {
            "section": "dependencies", "type": "duplicate_dependency_declaration",
            "keys": ["dependency:python:requests", "file:requirements.txt"],
            "summary": "Dependency is declared more than once.",
        }],
        "sections": {"dependencies": {"checked": ["offline inventory"]}},
        "graph": {"freshness": "missing"},
    }
    monkeypatch.setattr(orchestrator.collect, "collect", lambda *a, **k: packet)

    result = build_deep_review(tmp_path, tmp_path / "report")

    by_type = {item["type"]: item for item in result["findings"]}
    conflict = by_type["conflicting_dependency_versions"]
    assert conflict["locations"] == []
    assert conflict["scope"] == "Conflict is reported across inventory records."
    duplicate = by_type["duplicate_dependency_declaration"]
    assert duplicate["locations"] == [{"path": "requirements.txt"}]


def test_duplicate_findings_keep_one_severity_confidence_assessment():
    from ozagia.core.deep_review.orchestrator import _deduplicate

    base = {"id": "dr-same", "severity": "note", "confidence": "low",
            "evidence_kind": "inferred", "locations": [], "evidence": [],
            "tags": [], "related_ids": []}
    stronger = {**base, "severity": "blocking", "confidence": "high"}
    for rows in ([base, stronger], [stronger, base]):
        finding = _deduplicate(rows)[0]
        assert finding["severity"] == "blocking"
        assert finding["confidence"] == "high"

    # Do not synthesize a high-confidence blocker from two different claims.
    low_blocker = {**base, "severity": "blocking", "confidence": "low"}
    high_note = {**base, "severity": "note", "confidence": "high"}
    for rows in ([low_blocker, high_note], [high_note, low_blocker]):
        finding = _deduplicate(rows)[0]
        assert (finding["severity"], finding["confidence"]) == ("blocking", "low")


def test_cross_subject_duplicates_have_one_canonical_finding_and_subject_views(
        tmp_path, monkeypatch):
    import ozagia.core.deep_review.orchestrator as orchestrator
    shared = {"type": "unsafe_call", "rule_key": "shell-input",
              "keys": ["file:src/app.py"], "summary": "Input reaches shell."}
    packet = {"findings": [
        {**shared, "section": "architecture"},
        {**shared, "section": "security"},
        {**shared, "section": "performance", "type": "slow_call"},
        {**shared, "section": "failure-modes", "keys": ["file:src/other.py"]},
    ], "sections": {}, "graph": {"freshness": "fresh"}}
    monkeypatch.setattr(orchestrator.collect, "collect", lambda *a, **k: packet)
    out = tmp_path / "report"
    result = build_deep_review(tmp_path, out)
    findings = json.loads((out / "findings.json").read_text())
    canonical = [item for item in findings if item["type"] == "unsafe_call"
                 and any(location["path"] == "src/app.py"
                         for location in item["locations"])]
    assert len(canonical) == 1
    finding = canonical[0]
    assert finding["subject"] == "security"
    assert finding["subjects"] == ["security", "architecture"]
    assert {item["subject"] for item in finding["evidence"]} == {
        "security", "architecture"}
    assert finding["subject_observations"] == [
        {"subject": "security", "type": "unsafe_call",
         "title": "Input reaches shell.", "impact": "Input reaches shell.",
         "recommendation": "", "severity": "note", "confidence": "medium",
         "evidence_kind": "observed"},
        {"subject": "architecture", "type": "unsafe_call",
         "title": "Input reaches shell.", "impact": "Input reaches shell.",
         "recommendation": "", "severity": "note", "confidence": "medium",
         "evidence_kind": "observed"},
    ]
    assert finding["related_ids"] == []
    for subject, report in (("security", "01-security.md"),
                            ("architecture", "06-architecture.md")):
        assert finding["id"] in result["manifest"]["subjects"][subject]["findings"]
        assert finding["id"] in (out / report).read_text()
    assert result["manifest"]["identity_policy"] == \
        "canonical_subject_exact_finding_id_v1"


def test_cross_subject_merge_preserves_one_observed_assessment_pair(
        tmp_path, monkeypatch):
    import ozagia.core.deep_review.orchestrator as orchestrator
    shared = {"type": "unsafe_call", "rule_key": "shell-input",
              "keys": ["file:src/app.py"], "summary": "Input reaches shell."}
    packet = {"findings": [
        {**shared, "section": "security", "severity": "note",
         "confidence": "high"},
        {**shared, "section": "architecture", "severity": "blocking",
         "confidence": "low"},
    ], "sections": {}, "graph": {"freshness": "fresh"}}
    monkeypatch.setattr(orchestrator.collect, "collect", lambda *a, **k: packet)

    build_deep_review(tmp_path, tmp_path / "report")
    findings = json.loads((tmp_path / "report" / "findings.json").read_text())
    finding = next(item for item in findings if item["type"] == "unsafe_call")

    assert (finding["severity"], finding["confidence"]) == ("blocking", "low")
    assert {(row["severity"], row["confidence"])
            for row in finding["subject_observations"]} == {
                ("note", "high"), ("blocking", "low")}


def test_cross_subject_stable_key_does_not_merge_different_finding_types(
        tmp_path, monkeypatch):
    import ozagia.core.deep_review.orchestrator as orchestrator
    shared_key = "same-location"
    packet = {"findings": [
        {"section": "security", "type": "unsafe_call",
         "stable_key": shared_key, "keys": ["file:src/app.py"],
         "summary": "Shell construction."},
        {"section": "performance", "type": "slow_call",
         "stable_key": shared_key, "keys": ["file:src/app.py"],
         "summary": "Expensive call."},
    ], "sections": {}, "graph": {"freshness": "fresh"}}
    monkeypatch.setattr(orchestrator.collect, "collect", lambda *a, **k: packet)

    build_deep_review(tmp_path, tmp_path / "report")
    findings = json.loads((tmp_path / "report" / "findings.json").read_text())

    assert {item["type"] for item in findings} == {"unsafe_call", "slow_call"}
    assert len(findings) == 2
    assert all("subjects" not in item for item in findings)


def test_legacy_cross_subject_baseline_reports_secondary_id_as_resolved(
        tmp_path, monkeypatch):
    import ozagia.core.deep_review.orchestrator as orchestrator
    shared = {"type": "unsafe_call", "rule_key": "shell-input",
              "keys": ["file:src/app.py"], "summary": "Input reaches shell."}
    packet = {"findings": [{**shared, "section": subject}
                           for subject in ("security", "architecture")],
              "sections": {}, "graph": {"freshness": "fresh"}}
    monkeypatch.setattr(orchestrator.collect, "collect", lambda *a, **k: packet)
    canonical_id = finding_id("security", shared)
    legacy_duplicate_id = finding_id("architecture", shared)
    baseline = tmp_path / "legacy-findings.json"
    baseline.write_text(json.dumps([
        {"id": canonical_id, "subject": "security"},
        {"id": legacy_duplicate_id, "subject": "architecture"},
    ]), encoding="utf-8")
    result = build_deep_review(tmp_path, tmp_path / "current", baseline=baseline)
    comparison = result["manifest"]["comparison"]
    assert comparison["unchanged"] == [canonical_id]
    assert comparison["resolved"] == []
    assert comparison["unverified_absent"] == [legacy_duplicate_id]


def test_evidence_hash_is_bounded_and_skips_symlink(tmp_path, monkeypatch):
    import ozagia.core.deep_review.orchestrator as orchestrator
    (tmp_path / "large.py").write_bytes(b"x" * (1024 * 1024 + 1))
    (tmp_path / "real.py").write_text("pass\n", encoding="utf-8")
    (tmp_path / "link.py").symlink_to(tmp_path / "real.py")
    packet = {"findings": [
        {"section": "architecture", "type": "large", "keys": ["file:large.py"],
         "file": "large.py"},
        {"section": "architecture", "type": "linked", "keys": ["file:link.py"],
         "file": "link.py"},
    ], "sections": {}, "graph": {"freshness": "unknown"}}
    monkeypatch.setattr(orchestrator.collect, "collect", lambda *a, **k: packet)
    result = build_deep_review(tmp_path, tmp_path / "report")
    by_type = {f["type"]: f for f in result["findings"]}
    assert "hash_omitted" in by_type["large"]["evidence"][0]
    assert "hash" not in by_type["linked"]["evidence"][0]


def test_excerpt_reader_refuses_fifo_without_waiting(tmp_path):
    import os
    import pytest
    import ozagia.core.deep_review.orchestrator as orchestrator

    if not hasattr(os, "mkfifo") or not hasattr(os, "O_NONBLOCK"):
        pytest.skip("non-blocking FIFO unavailable")
    os.mkfifo(tmp_path / "pipe")
    descriptor, omission = orchestrator._open_excerpt_source(tmp_path, "pipe")
    assert descriptor is None
    assert omission == "source file is not a regular file"


def test_evidence_hash_refuses_swapped_ancestor(tmp_path, monkeypatch):
    import os
    import ozagia.core.deep_review.orchestrator as orchestrator

    root = tmp_path / "repo"
    root.mkdir()
    (root / "pkg").mkdir()
    (root / "pkg" / "file.py").write_text("safe", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "file.py").write_text("private", encoding="utf-8")
    original_open = os.open
    swapped = False

    def swap_open(path, flags, *args, **kwargs):
        nonlocal swapped
        if path == "pkg" and kwargs.get("dir_fd") is not None and not swapped:
            swapped = True
            (root / "pkg").rename(root / "old-pkg")
            (root / "pkg").symlink_to(outside, target_is_directory=True)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", swap_open)
    descriptor, omission = orchestrator._open_excerpt_source(root, "pkg/file.py")
    assert swapped
    assert descriptor is None
    assert omission is not None


def test_evidence_file_hash_is_read_once_per_path(tmp_path, monkeypatch):
    import ozagia.core.deep_review.orchestrator as orchestrator
    target = tmp_path / "same.py"
    target.write_text("pass\n", encoding="utf-8")
    original_open = orchestrator._open_excerpt_source
    reads = []

    def counted_open(root, path, *args, **kwargs):
        if path == "same.py":
            reads.append(path)
        return original_open(root, path, *args, **kwargs)

    monkeypatch.setattr(orchestrator, "_open_excerpt_source", counted_open)
    raw = [{"section": "architecture", "type": f"finding_{n}",
            "keys": [f"rule:r{n}:same.py"], "file": "same.py"}
           for n in range(3)]
    monkeypatch.setattr(orchestrator.collect, "collect", lambda *a, **k: {
        "findings": raw, "sections": {}, "graph": {"freshness": "unknown"}})
    result = build_deep_review(tmp_path, tmp_path / "report")
    # One read fingerprints the plan input and one hashes finding evidence.
    assert len(reads) == 2
    assert all(item["evidence"][0]["hash"].startswith("sha256:")
               for item in result["findings"])


def test_elapsed_budget_overrun_marks_manifest_partial(tmp_path, monkeypatch):
    import ozagia.core.deep_review.orchestrator as orchestrator
    monkeypatch.setattr(orchestrator.collect, "collect", lambda *a, **k: {
        "findings": [], "sections": {}, "graph": {"freshness": "fresh"}})
    monkeypatch.setattr(orchestrator, "_run_status", lambda _sections: "complete")
    ticks = iter(range(100))
    monkeypatch.setattr(orchestrator.time, "monotonic", lambda: next(ticks) / 10)
    result = build_deep_review(tmp_path, tmp_path / "report", budget_s=0)
    assert result["manifest"]["budgets"]["exceeded"] is True
    assert result["manifest"]["status"] == "partial"
    assert result["manifest"]["complete"] is False


def test_zero_budget_writes_consistent_partial_report_and_counts_omissions(
        tmp_path, monkeypatch):
    import hashlib
    import ozagia.core.deep_review.orchestrator as orchestrator
    monkeypatch.setattr(orchestrator.collect, "collect", lambda *a, **k: {
        "findings": [{"section": "security", "type": "candidate",
                      "keys": ["file:a.py"]}],
        "sections": {"security": {"checked": ["patterns"]}},
        "graph": {"freshness": "fresh"}})
    out = tmp_path / "report"
    result = build_deep_review(tmp_path, out, budget_s=0)
    manifest = result["manifest"]
    assert manifest["status"] == "partial"
    assert manifest["truncation"]["normalization_findings_omitted"] == 1
    assert json.loads((out / "findings.json").read_text()) == []
    for name, digest in manifest["output_hashes"].items():
        assert digest == "sha256:" + hashlib.sha256((out / name).read_bytes()).hexdigest()


def test_manifest_preserves_data_inventory(tmp_path, monkeypatch):
    import ozagia.core.deep_review.orchestrator as orchestrator
    inventory = {"entities": 4, "relationships": 2, "provenance": "static"}
    monkeypatch.setattr(orchestrator.collect, "collect", lambda *a, **k: {
        "findings": [], "sections": {"data-structures": {
            "checked": ["inventory"], "data_inventory": inventory}},
        "graph": {"freshness": "fresh"}})
    result = build_deep_review(tmp_path, tmp_path / "report")
    assert result["manifest"]["subjects"]["data-structures"]["data_inventory"] == inventory


def test_unavailable_collector_reason_is_preserved_in_coverage(tmp_path, monkeypatch):
    import ozagia.core.deep_review.orchestrator as orchestrator
    monkeypatch.setattr(orchestrator.collect, "collect", lambda *a, **k: {
        "findings": [], "sections": {"security": {
            "checked": [],
            "not_checked": [{"reason": "security scan unavailable: subprocess failed"}],
        }}, "graph": {"freshness": "unknown"}})

    out = tmp_path / "report"
    result = build_deep_review(tmp_path, out, profile="security")

    rendered = (out / "01-security.md").read_text(encoding="utf-8")
    assert "security scan unavailable: subprocess failed" in rendered
    assert "No dedicated collector" not in rendered
    assert result["manifest"]["subjects"]["security"]["omitted"] == [
        {"reason": "security scan unavailable: subprocess failed"}]


def test_optional_model_pass_is_bounded_nonblocking_and_uses_fake_runner():
    from ozagia.core.deep_review.model_pass import run_model_pass

    calls = []
    def fake_runner(model, messages):
        calls.append((model, messages))
        packet_text = messages[-1]["content"]
        assert "password" not in packet_text
        assert "secret-value" not in packet_text
        assert "raw finding title" not in packet_text
        return {"summary": "Candidate only.", "findings": [{
            "wording": "Check whether these components share an invariant.",
            "severity": "blocking", "issue_type": "possible_invariant_gap",
            "citation_keys": ["dr-existing"], "recommendation": "Verify statically.",
        }]}

    result = run_model_pass([{
        "id": "dr-existing", "subject": "architecture", "type": "cycle",
        "severity": "major", "title": "raw finding title password=secret-value",
        "locations": [{"path": "src/a.py", "line": 4}],
    }], model_runner=fake_runner, models="fake/model", roles=("architecture",))
    assert len(calls) == 1
    candidate = result["findings"][0]
    assert candidate["severity"] == "note"
    assert candidate["candidate_severity"] == "blocking"
    assert candidate["evidence"][0]["kind"] == "model"
    assert candidate["truth_assessed"] is False
    assert result["truth_assessed"] is False


def test_optional_model_pass_does_not_consume_unbounded_findings_iterable():
    from ozagia.core.deep_review.model_pass import run_model_pass

    consumed = []
    def findings():
        for index in range(100_000):
            consumed.append(index)
            yield {"id": f"f{index}", "subject": "tests", "type": "candidate"}

    result = run_model_pass(findings(), model_runner=lambda *a, **k: {"findings": []},
                            models="fake/model", roles=("tests",), max_findings=2)
    assert consumed == [0, 1, 2]
    assert result["packet_findings"] == 2
    assert result["packet_omitted"] is None
    assert result["packet_omitted_unknown"] is True


def test_optional_model_pass_provider_failure_returns_partial_result():
    from ozagia.core.deep_review.model_pass import run_model_pass

    def broken_runner(model, messages):
        raise RuntimeError("provider unavailable")

    result = run_model_pass([], model_runner=broken_runner,
                            models="fake/model", roles=("security",))
    assert result["status"] == "unavailable"
    assert result["findings"] == []
    assert result["errors"][0]["stage"] == "model"
    assert result["errors"][0]["error"] == "RuntimeError: model review failed"
    assert "provider unavailable" not in str(result["errors"])


def test_optional_model_pass_redacts_provider_exception_details():
    from ozagia.core.deep_review.model_pass import run_model_pass

    def broken_runner(model, messages):
        raise RuntimeError("api_key=secret-token at https://private.invalid")

    result = run_model_pass([], model_runner=broken_runner,
                            models="fake/model", roles=("security",))
    assert result["status"] == "unavailable"
    assert result["errors"] == [{
        "role": "security", "model": "fake/model", "stage": "model",
        "error_type": "RuntimeError",
        "error": "RuntimeError: model review failed",
    }]
    assert "secret-token" not in str(result)


def test_optional_model_pass_redacts_candidate_text():
    from ozagia.core.deep_review.model_pass import run_model_pass

    def runner(model, messages):
        return {"summary": "Candidate only.", "findings": [{
            "issue_type": "candidate",
            "wording": "password=secret-value", "severity": "minor",
            "citation_keys": ["f1"], "recommendation": "api_key=another-secret",
        }]}

    result = run_model_pass([{"id": "f1"}], model_runner=runner,
                            models="fake/model", roles=("security",))
    assert "secret-value" not in str(result)
    assert "another-secret" not in str(result)
    assert "private.invalid" not in str(result)


def test_optional_model_pass_redacts_provider_metadata(monkeypatch):
    from ozagia.core.deep_review import model_pass

    from ozagia.harness import review
    monkeypatch.setattr(review, "run_review_matrix", lambda *a, **k: {
        "runs": [{"role": "security", "model": "fake/model",
                  "reviewer_id": "reviewer password=private-metadata",
                  "provider": "api_key=provider-secret", "usage": {"total": 2},
                  "packet_sha256": "abc", "output": {"summary": "ok",
                  "findings": [{
                      "wording": "Check boundary", "issue_type": "check",
                      "severity": "minor",
                      "citation_keys": [],
                  }]}}], "errors": [],
    })
    result = model_pass.run_model_pass([], model_runner=lambda *a, **k: {},
                                      models="fake/model", roles=("security",))
    serialized = str(result)
    assert "private-metadata" not in serialized
    assert "provider-secret" not in serialized
    assert "fake/model" in serialized
    assert len(result["findings"]) == 1


def _valid_model_output(wording="Check the boundary.", severity="major",
                        citations=None):
    return {"summary": "One checkable candidate.", "findings": [{
        "wording": wording,
        "issue_type": "boundary_check",
        "severity": severity,
        "citation_keys": list(citations or []),
    }]}


def test_model_output_validation_is_strict_and_counts_findings():
    from ozagia.core.deep_review.model_pass import validate_model_output

    accepted = validate_model_output(_valid_model_output(citations=["f1"]))
    assert accepted["valid"] is True
    assert accepted["accepted"] == 1 and accepted["rejected"] == 0

    assert validate_model_output([])["valid"] is False
    missing_summary = validate_model_output({"findings": []})
    assert missing_summary["valid"] is False
    assert any("summary" in error for error in missing_summary["errors"])
    bad_items = _valid_model_output()
    bad_items["findings"][0]["severity"] = "critical"
    bad_items["findings"][0]["citation_keys"] = [7]
    result = validate_model_output(bad_items)
    assert result["valid"] is False
    assert result["accepted"] == 0 and result["rejected"] == 1
    assert any("severity" in error for error in result["errors"])
    assert any("citation_keys" in error for error in result["errors"])

    missing_fields = {"summary": "ok", "findings": [{
        "severity": "minor", "citation_keys": [],
    }]}
    result = validate_model_output(missing_fields)
    assert result["valid"] is False
    assert any("wording" in error for error in result["errors"])
    assert any("issue_type" in error for error in result["errors"])

    oversized = _valid_model_output()
    oversized["findings"][0]["wording"] = "x" * 1001
    assert validate_model_output(oversized)["valid"] is False
    assert validate_model_output({"summary": "ok", "findings": {},
                                  "truth_assessed": True})["valid"] is False


def test_model_output_assertion_contract_is_strict_and_bounded():
    from ozagia.core.deep_review.model_pass import validate_model_output

    assertion = {"schema": "ozagia.deep_review.assertion.v1",
                 "kind": "python.mutable_default", "path": "src/a.py",
                 "symbol": "Thing.method", "source_sha256": "a" * 64}
    payload = _valid_model_output()
    payload["findings"][0]["assertions"] = [assertion]
    assert validate_model_output(payload)["valid"] is True

    for bad in ({**assertion, "extra": "field"},
                {**assertion, "path": "../outside.py"},
                {**assertion, "source_sha256": "A" * 64}):
        payload["findings"][0]["assertions"] = [bad]
        assert validate_model_output(payload)["valid"] is False
    payload["findings"][0]["assertions"] = [assertion] * 5
    assert validate_model_output(payload)["valid"] is False


def test_model_pass_rejects_invalid_response_and_counts_rejections():
    from ozagia.core.deep_review.model_pass import run_model_pass

    canary = "password=" + "deep-review-validation-canary"

    def runner(model, messages):
        return {"summary": "Candidate only.", "findings": [
            {"wording": "One", "issue_type": "check", "severity": canary,
             "citation_keys": []},
            {"wording": "Two", "issue_type": "check", "severity": "minor",
             "citation_keys": [3]},
        ]}

    result = run_model_pass([], model_runner=runner, models="fake/model",
                            roles=("security",))
    assert result["status"] == "unavailable"
    assert result["findings"] == []
    assert result["validation"]["accepted"] == 0
    assert result["validation"]["rejected"] == 2
    assert result["errors"][0]["stage"] == "validation"
    assert "validation failed" in result["errors"][0]["error"]
    assert "deep-review-validation-canary" not in str(result)


def test_model_pass_recovers_fenced_json_with_prose():
    from ozagia.core.deep_review.model_pass import run_model_pass

    text = ("Review follows.\n```json\n"
            + json.dumps(_valid_model_output())
            + "\n```\n")

    def runner(model, messages):
        return {"output": text}

    result = run_model_pass([], model_runner=runner, models="fake/model",
                            roles=("architecture",))
    assert result["status"] == "complete"
    assert result["validation"]["accepted"] == 1
    assert result["findings"][0]["subject"] == "architecture"


def test_model_pass_counts_unknown_citations_without_silently_dropping_them():
    from ozagia.core.deep_review.model_pass import run_model_pass

    payload = _valid_model_output(citations=["f1", "dr-not-in-packet"])

    result = run_model_pass(
        [{"id": "f1", "subject": "security", "type": "pattern",
          "severity": "note", "locations": []}],
        model_runner=lambda *a, **k: payload, models="fake/model",
        roles=("security",))
    candidate = result["findings"][0]
    assert result["validation"]["unknown_citations"] == 1
    assert candidate["related_ids"] == ["f1"]
    assert candidate["evidence"][0]["unknown_citation_count"] == 1
    assert "dr-not-in-packet" not in json.dumps(candidate["related_ids"])


def test_model_pass_maps_reviewer_roles_to_report_subjects():
    from ozagia.core.deep_review.model_pass import run_model_pass

    expected = {
        "security": "security",
        "performance": "performance",
        "failures": "failure-modes",
        "data": "data-structures",
        "scalability": "scalability",
        "architecture": "architecture",
        "tests": "tests-and-coverage",
    }
    for role, subject in expected.items():
        def runner(model, messages, role=role):
            return _valid_model_output(wording=f"Candidate from {role}.")

        result = run_model_pass([], model_runner=runner, models="fake/model",
                                roles=(role,))
        assert result["findings"][0]["subject"] == subject


def test_model_pass_packets_hash_for_assertion_and_preserves_payload(tmp_path):
    import hashlib
    from ozagia.core.deep_review.model_pass import run_model_pass

    source = b"def f(items=[]):\n    return items\n"
    (tmp_path / "a.py").write_bytes(source)
    digest = hashlib.sha256(source).hexdigest()

    def runner(model, messages):
        packet_text = json.dumps(messages)
        assert digest in packet_text
        return {"summary": "Checkable assertion.", "findings": [{
            "wording": "Candidate.", "issue_type": "candidate",
            "severity": "minor", "citation_keys": [],
            "assertions": [{
                "schema": "ozagia.deep_review.assertion.v1",
                "kind": "python.mutable_default", "path": "a.py",
                "symbol": "f", "source_sha256": digest,
            }],
        }]}

    result = run_model_pass([{"id": "f1", "subject": "security",
                             "type": "x", "locations": [{"path": "a.py"}]}],
                           model_runner=runner, models="fake/model",
                           roles=("security",), root=tmp_path)
    assert result["findings"][0]["assertions"][0]["source_sha256"] == digest


def test_model_pass_dedupes_roles_deterministically_with_provenance():
    from ozagia.core.deep_review.model_pass import run_model_pass

    payload = _valid_model_output(citations=["f1"])

    def runner(model, messages):
        return payload

    first = run_model_pass([{"id": "f1"}], model_runner=runner,
                           models="fake/model",
                           roles=("security", "performance"))
    second = run_model_pass([{"id": "f1"}], model_runner=runner,
                            models="fake/model",
                            roles=("performance", "security"))
    assert len(first["findings"]) == len(second["findings"]) == 1
    candidate = first["findings"][0]
    assert candidate["id"] == second["findings"][0]["id"]
    assert candidate["severity"] == "note"
    assert candidate["candidate_severity"] == "major"
    assert first["validation"]["deduplicated"] == 1
    provenance = candidate["evidence"][0]["provenance"]
    assert [row["role"] for row in provenance] == ["performance", "security"]
    assert set(candidate["tags"]) >= {"model-review",
                                      "model-role:performance",
                                      "model-role:security"}


def test_model_pass_request_cap_returns_partial_results():
    from ozagia.core.deep_review.model_pass import run_model_pass

    calls = []

    def runner(model, messages):
        calls.append(model)
        return _valid_model_output()

    result = run_model_pass([], model_runner=runner, models="fake/model",
                            roles=("security", "architecture"),
                            deadline_s=60, max_requests=1)
    assert len(calls) == 1
    assert result["budgets"]["requests"] == 1
    assert result["budgets"]["exceeded"] is True
    assert result["status"] == "partial"
    assert any(error["stage"] == "budget" and "request cap" in error["error"]
               for error in result["errors"])


def test_model_pass_deadline_preempts_requests():
    from ozagia.core.deep_review.model_pass import run_model_pass

    calls = []

    def runner(model, messages):
        calls.append(model)
        return _valid_model_output()

    result = run_model_pass([], model_runner=runner, models="fake/model",
                            roles=("security",), deadline_s=0)
    assert calls == []
    assert result["status"] == "unavailable"
    assert result["budgets"]["requests"] == 0
    assert result["budgets"]["exceeded"] is True
    assert result["errors"][0]["stage"] == "budget"


def test_model_pass_token_cap_stops_after_reported_usage():
    from ozagia.core.deep_review.model_pass import run_model_pass

    calls = []

    def runner(model, messages):
        calls.append(model)
        return {"output": _valid_model_output(),
                "usage": {"total_tokens": 100}}

    result = run_model_pass([], model_runner=runner, models="fake/model",
                            roles=("security", "architecture"),
                            deadline_s=60, max_tokens=100)
    assert len(calls) == 1
    assert result["budgets"]["tokens_used"] == 100
    assert result["budgets"]["usage_observed"] is True
    assert result["status"] == "partial"
    assert any(error["stage"] == "budget" and "token budget" in error["error"]
               for error in result["errors"])


def test_model_pass_cost_cap_stops_after_reported_usage():
    from ozagia.core.deep_review.model_pass import run_model_pass

    calls = []

    def runner(model, messages):
        calls.append(model)
        return {"output": _valid_model_output(),
                "usage": {"total_tokens": 10, "cost_usd": 1.5}}

    result = run_model_pass([], model_runner=runner, models="fake/model",
                            roles=("security", "architecture"),
                            deadline_s=60, max_cost_usd=1.0)
    assert len(calls) == 1
    assert result["budgets"]["cost_usd"] == 1.5
    assert result["status"] == "partial"
    assert any(error["stage"] == "budget" and "cost budget" in error["error"]
               for error in result["errors"])


def test_model_pass_redacts_packet_and_run_secret_canaries():
    from ozagia.core.deep_review.model_pass import run_model_pass

    canary = "password=" + "deep-review-packet-canary"
    seen = []

    def runner(model, messages):
        seen.append(messages)
        return _valid_model_output()

    finding = {"id": "f1", "subject": canary, "type": canary,
               "severity": "note",
               "locations": [{"path": "src/" + canary + ".py", "line": 2}]}
    result = run_model_pass([finding], model_runner=runner,
                            models="fake/" + canary, roles=("security",))
    assert "deep-review-packet-canary" not in json.dumps(seen)
    assert "deep-review-packet-canary" not in str(result)
    assert "deep-review-packet-canary" not in json.dumps(result["runs"])


def test_model_pass_redacts_candidate_usage_and_error_canaries():
    from ozagia.core.deep_review.model_pass import run_model_pass

    canary = "password=" + "deep-review-output-canary"

    def runner(model, messages):
        return {"summary": canary, "findings": [{
            "wording": canary, "issue_type": canary, "severity": "major",
            "citation_keys": [], "recommendation": canary,
        }], "usage": {"note": canary, "total_tokens": 3},
            "provider": canary}

    result = run_model_pass([], model_runner=runner, models="fake/model",
                            roles=("security",))
    assert len(result["findings"]) == 1
    assert "deep-review-output-canary" not in str(result)

    def broken(model, messages):
        raise RuntimeError(canary)

    failed = run_model_pass([], model_runner=broken, models="fake/model",
                            roles=("security",))
    assert "deep-review-output-canary" not in str(failed)
    assert failed["errors"][0]["error"] == "RuntimeError: model review failed"


def test_model_metadata_list_redaction_is_bounded():
    from ozagia.core.deep_review.model_pass import (
        MAX_REDACTION_ITEMS, _redact_value,
    )

    result = _redact_value([str(index) for index in range(MAX_REDACTION_ITEMS + 100)])

    assert result[:MAX_REDACTION_ITEMS] == [str(index)
                                             for index in range(MAX_REDACTION_ITEMS)]
    assert result[MAX_REDACTION_ITEMS:] == ["<redacted:items>"]


def test_model_pass_ocelot_is_off_by_default():
    from ozagia.core.deep_review.model_pass import run_model_pass

    result = run_model_pass([], model_runner=lambda *a, **k: _valid_model_output(),
                            models="fake/model", roles=("security",))
    assert result["ocelot"]["requested"] is False
    assert result["ocelot"]["state"] == "not_requested"
    assert len(result["findings"]) == 1


def test_model_pass_ocelot_absent_falls_back_exactly():
    from ozagia.core.deep_review.model_pass import run_model_pass

    calls = []

    def fake_clusterer(findings, **kwargs):
        calls.append([item["id"] for item in findings])
        return {"available": False, "state": "unavailable", "engine": "exact",
                "clusters": [], "cluster_count": 0,
                "error": "Ocelot checkout not found",
                "note": "exact clusters only"}

    result = run_model_pass([], model_runner=lambda *a, **k: _valid_model_output(),
                            models="fake/model", roles=("security",),
                            ocelot=True, ocelot_clusterer=fake_clusterer)
    candidate_id = result["findings"][0]["id"]
    assert calls == [[candidate_id]]
    assert result["ocelot"]["requested"] is True
    assert result["ocelot"]["available"] is False
    assert result["ocelot"]["state"] == "unavailable"
    assert "checkout not found" in result["ocelot"]["error"]
    assert result["findings"][0]["severity"] == "note"


def test_model_pass_ocelot_failure_falls_back_exactly():
    from ozagia.core.deep_review.model_pass import run_model_pass

    canary = "password=" + "deep-review-ocelot-canary"

    def fake_clusterer(findings, **kwargs):
        raise RuntimeError(canary)

    result = run_model_pass([], model_runner=lambda *a, **k: _valid_model_output(),
                            models="fake/model", roles=("security",),
                            ocelot=True, ocelot_clusterer=fake_clusterer)
    assert result["status"] == "complete"
    assert result["ocelot"]["available"] is False
    assert result["ocelot"]["state"] == "error"
    assert "RuntimeError" in result["ocelot"]["error"]
    assert "exact clusters" in result["ocelot"]["note"]
    assert "not failed" in result["ocelot"]["note"]
    assert "deep-review-ocelot-canary" not in str(result)
    assert len(result["findings"]) == 1


def test_model_pass_ocelot_present_clusters_candidate_ids():
    from ozagia.core.deep_review.model_pass import run_model_pass

    def fake_clusterer(findings, **kwargs):
        ids = sorted(item["id"] for item in findings)
        return {"available": True, "state": "ready", "engine": "fake-ocelot",
                "clusters": [{"findings": ids, "size": len(ids),
                              "origin": "exact"}],
                "cluster_count": 1,
                "note": "candidate signal only, not truth"}

    result = run_model_pass([], model_runner=lambda *a, **k: _valid_model_output(),
                            models="fake/model", roles=("security",),
                            ocelot=fake_clusterer)
    candidate_id = result["findings"][0]["id"]
    assert result["ocelot"]["available"] is True
    assert result["ocelot"]["state"] == "ready"
    assert result["ocelot"]["clusters"][0]["findings"] == [candidate_id]
    assert result["ocelot"]["truth_assessed"] is False
