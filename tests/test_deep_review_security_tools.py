"""Explicit, opt-in security-scanner support in deep review.

These tests never install or run a real network scanner. The pip-audit test
places a harmless fake executable on PATH and asserts that the refused path
does not execute it.
"""
import hashlib
import json
import sys


def _review_packet():
    return {
        "findings": [],
        "sections": {"security": {
            "checked": ["security.sensitive_files"],
            "not_checked": [],
        }},
        "graph": {"freshness": "unknown"},
    }


def _isolate_collect(monkeypatch):
    from ozagia.core.deep_review import collect as collect_mod

    monkeypatch.setattr(
        collect_mod.repo_review, "review_repo",
        lambda *args, **kwargs: _review_packet())
    monkeypatch.setattr(
        collect_mod, "_bounded_trust_scan",
        lambda *args, **kwargs: {
            "findings": [], "checked": 0, "not_checked": []})
    return collect_mod


def _compose(packet, root, out, monkeypatch):
    import ozagia.core.deep_review.orchestrator as orchestrator

    monkeypatch.setattr(
        orchestrator.collect, "collect", lambda *args, **kwargs: packet)
    return orchestrator.build_deep_review(
        root, out, profile="security", budget_s=10)


def test_default_security_scan_requests_no_tools_and_no_network(
        tmp_path, monkeypatch):
    collect_mod = _isolate_collect(monkeypatch)
    calls = []

    def scan(root, timeout, **kwargs):
        calls.append(kwargs)
        return {
            "findings": [], "not_assessed": [],
            "inputs": {"files_scanned": 0}, "summary": {}, "graph": {},
            "tools": {"requested": [], "network_allowed": False,
                      "results": []},
        }

    monkeypatch.setattr(collect_mod, "_bounded_security_scan", scan)
    packet = collect_mod.collect(tmp_path, profile="security", budget_s=5)

    assert calls == [{"tools": (), "allow_network": False}]
    assert packet["sections"]["security"]["scanner_tools"] == {
        "requested": [], "network_allowed": False, "results": []}


def test_bounded_security_scan_passes_tool_names_and_network_as_argv(
        monkeypatch):
    from ozagia.core.deep_review import collect as collect_mod

    calls = []

    class Result:
        returncode = 0
        stdout = '{"findings": []}'

    monkeypatch.setattr(
        collect_mod.subprocess, "run",
        lambda argv, **kwargs: (calls.append(argv), Result())[1])
    injected = "bandit'; raise SystemExit('injected')"
    collect_mod._bounded_security_scan(
        "/tmp/review-root", 5, tools=[injected], allow_network=True)

    argv = calls[0]
    assert argv[1:3] == ["-I", "-B"]
    assert injected not in argv[4]  # the child's Python source
    assert argv[5] == "/tmp/review-root"
    assert json.loads(argv[6]) == [injected]
    assert argv[7] == "1"


def test_isolated_worker_flags_prevent_import_bytecode_writes(tmp_path):
    import subprocess

    package = tmp_path / "worker_probe"
    package.mkdir()
    (package / "probe.py").write_text("VALUE = 1\n", encoding="utf-8")
    code = "import sys; sys.path.insert(0, sys.argv[1]); import probe"
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", code, str(package)],
        capture_output=True, text=True, check=False)

    assert result.returncode == 0, result.stderr
    assert not (package / "__pycache__").exists()


def test_allowlisted_scanner_records_version_and_report_hash(tmp_path):
    from ozagia.core import security

    report = '{"results": []}'
    spec = security.TOOL_REGISTRY["bandit"]
    result, findings = security._run_tool(
        spec, tmp_path,
        runner=lambda argv, **kwargs: {
            "returncode": 0, "stdout": report, "version": "Bandit 1.8.0"},
        warnings=[])

    assert result["status"] == "ok"
    assert result["version"] == "Bandit 1.8.0"
    assert result["report_hash"] == "sha256:" + hashlib.sha256(
        report.encode()).hexdigest()
    assert findings == []


def test_scanner_error_text_is_redacted_before_report_storage(tmp_path):
    from ozagia.core import security

    secret = "sk-proj-123456789012345678901234"
    spec = security.TOOL_REGISTRY["bandit"]

    def failing_runner(argv, **kwargs):
        raise OSError(f"scanner failed with token: {secret}")

    result, findings = security._run_tool(
        spec, tmp_path, runner=failing_runner, warnings=[])

    assert result["status"] == "error"
    assert secret not in result["error"]
    assert "<redacted:" in result["error"]
    assert findings == []


def test_security_manifest_provenance_redacts_untrusted_error_text():
    from ozagia.core.deep_review.orchestrator import _security_provenance

    secret = "sk-proj-123456789012345678901234"
    result = _security_provenance({"scanner_tools": {
        "requested": ["bandit"], "results": [{
            "name": "bandit", "status": "error",
            "error": f"token: {secret}", "findings": 0}]}})

    assert secret not in result["results"][0]["error"]
    assert "<redacted:" in result["results"][0]["error"]


def test_unavailable_offline_tool_is_recorded_and_report_composes(
        tmp_path, monkeypatch):
    collect_mod = _isolate_collect(monkeypatch)
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    monkeypatch.setenv("PATH", str(empty_bin))
    (tmp_path / "app.py").write_text("print('ok')\n", encoding="utf-8")

    packet = collect_mod.collect(
        tmp_path, profile="security", budget_s=10, security_tools=["bandit"])
    section = packet["sections"]["security"]
    assert section["scanner_tools"]["requested"] == ["bandit"]
    assert section["scanner_tools"]["network_allowed"] is False
    result = section["scanner_tools"]["results"][0]
    assert {key: result[key] for key in (
        "name", "kind", "status", "version", "error", "findings")} == {
        "name": "bandit", "kind": "sast", "status": "unavailable",
        "version": None, "error": "bandit is not installed", "findings": 0,
    }
    assert result.get("report_hash") is None
    assert any("bandit results: unavailable" in item["reason"]
               for item in section["not_checked"])

    out = tmp_path / "report"
    composed = _compose(packet, tmp_path, out, monkeypatch)
    assert composed["manifest"]["subjects"]["security"]["status"] == "partial"
    assert (out / "01-security.md").is_file()


def test_network_tool_is_refused_without_explicit_network(
        tmp_path, monkeypatch):
    collect_mod = _isolate_collect(monkeypatch)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    marker = tmp_path / "pip-audit-ran"
    fake = bin_dir / "pip-audit"
    fake.write_text(
        f"#!{sys.executable}\n"
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('ran')\n"
        "print('{}')\n",
        encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    (tmp_path / "requirements.txt").write_text("requests==2.0.0\n",
                                               encoding="utf-8")

    packet = collect_mod.collect(
        tmp_path, profile="security", budget_s=10,
        security_tools=["pip-audit"], security_network=False)
    section = packet["sections"]["security"]
    result = section["scanner_tools"]["results"][0]
    assert result["name"] == "pip-audit"
    assert result["kind"] == "dependency"
    assert result["status"] == "refused"
    assert result["findings"] == 0
    assert "requires network access" in result["error"]
    assert section["scanner_tools"]["network_allowed"] is False
    assert not marker.exists()


def test_unknown_tool_is_not_checked_and_report_composes(tmp_path, monkeypatch):
    collect_mod = _isolate_collect(monkeypatch)
    (tmp_path / "app.py").write_text("print('ok')\n", encoding="utf-8")

    packet = collect_mod.collect(
        tmp_path, profile="security", budget_s=10,
        security_tools=["definitely-not-a-scanner"])
    section = packet["sections"]["security"]
    assert section["scanner_tools"]["requested"] == [
        "definitely-not-a-scanner"]
    result = section["scanner_tools"]["results"][0]
    assert result["name"] == "definitely-not-a-scanner"
    assert result["status"] == "not_checked"
    assert result["kind"] is None and result["version"] is None
    assert result["findings"] == 0
    assert "unknown tool" in result["error"]
    assert any("definitely-not-a-scanner" in item["reason"]
               and "unknown" in item["reason"]
               for item in section["not_checked"])

    out = tmp_path / "report"
    composed = _compose(packet, tmp_path, out, monkeypatch)
    assert composed["manifest"]["subjects"]["security"]["status"] == "partial"
    assert (out / "01-security.md").is_file()


def test_deep_review_cli_passes_security_flags_to_builder(
        tmp_path, monkeypatch):
    from ozagia import cli
    from ozagia.core import deep_review

    calls = []

    def fake_build(root, out, **kwargs):
        calls.append(kwargs)
        out.mkdir(parents=True, exist_ok=True)
        manifest = {"schema": "ozagia.deep_review.manifest.v1"}
        (out / "manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8")
        return {"manifest": manifest, "findings": [], "out": str(out),
                "failed": False}

    monkeypatch.setattr(deep_review, "build_deep_review", fake_build)
    out = tmp_path.parent / (tmp_path.name + "-security-report")
    code = cli.main([
        "deep-review", "--root", str(tmp_path), "--out", str(out),
        "--security-tool", "bandit", "--security-tool", "pip-audit",
        "--security-network"])
    assert code == 0
    assert calls[0]["security_tools"] == ["bandit", "pip-audit"]
    assert calls[0]["security_network"] is True


def test_deep_review_cli_security_defaults_are_off():
    from ozagia import cli

    args = cli.build_parser().parse_args(["deep-review"])
    assert args.security_tool is None
    assert args.security_network is False
