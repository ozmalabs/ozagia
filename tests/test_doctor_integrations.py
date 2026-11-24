"""Doctor integration detection and the capability summary boundary."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ozagia import doctor
from ozagia.integrations import versions


def _fake(binary_dir: Path, name: str, body: str) -> Path:
    path = binary_dir / name
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def _isolate_hosts(monkeypatch, binary_dir: Path) -> None:
    monkeypatch.setenv("PATH", str(binary_dir))

    def host_path(spec, root):
        candidate = binary_dir / spec.command
        return str(candidate) if candidate.exists() else None

    monkeypatch.setattr(doctor, "_host_path", host_path)


@pytest.mark.parametrize(
    "output,expected",
    [
        ("opencode v2.4.1", "2.4.1"),
        ("codex-cli 0.172.0", "0.172.0"),
        ("2.1.227 (Claude Code)", "2.1.227"),
        ("no version here", None),
        ("", None),
        ("1.2", None),
    ],
)
def test_version_parser(output, expected):
    assert versions.parse_version(output) == expected


def test_every_host_matrix_anchor_exists_in_the_document():
    document = Path(__file__).resolve().parents[1] / versions.MATRIX_DOC
    text = document.read_text(encoding="utf-8")
    missing = [
        spec.matrix_anchor for spec in versions.HOSTS.values()
        if f'<a id="{spec.matrix_anchor}"></a>' not in text
    ]
    assert missing == []


def test_verified_host_version(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _fake(bin_dir, "opencode", "echo 'opencode v2.4.1'")
    _isolate_hosts(monkeypatch, bin_dir)

    row = doctor._host_report(versions.HOSTS["opencode"], tmp_path)

    assert row["state"] == "verified"
    assert row["installed"] is True
    assert row["version"] == "2.4.1"
    assert row["path"] == str(bin_dir / "opencode")
    assert row["tested_range"] == "2.3.0, 2.4.1"
    assert row["error"] is None


def test_newer_host_version_is_installed_untested(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _fake(bin_dir, "codex", "echo 'codex-cli 0.172.0'")
    _isolate_hosts(monkeypatch, bin_dir)

    row = doctor._host_report(versions.HOSTS["codex"], tmp_path)
    warnings = doctor._integration_warnings({"codex": row})

    assert row["installed"] is True
    assert row["state"] == "installed_untested_version"
    assert "outside the tested versions" in warnings[0]["message"]
    assert "0.169.0" in warnings[0]["message"]


def test_older_host_version_is_installed_untested(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _fake(bin_dir, "opencode", "echo 'opencode v2.2.9'")
    _isolate_hosts(monkeypatch, bin_dir)

    row = doctor._host_report(versions.HOSTS["opencode"], tmp_path)

    assert row["state"] == "installed_untested_version"
    assert row["version"] == "2.2.9"


def test_offline_tested_version_is_not_called_verified(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _fake(bin_dir, "codex", "echo 'codex-cli 0.169.0'")
    _isolate_hosts(monkeypatch, bin_dir)

    row = doctor._host_report(versions.HOSTS["codex"], tmp_path)
    warnings = doctor._integration_warnings({"codex": row})

    assert row["state"] == "installed_untested_version"
    assert "offline-tested range" in warnings[0]["message"]
    assert "not verified" in warnings[0]["message"]


def test_absent_host_is_not_installed(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _isolate_hosts(monkeypatch, bin_dir)

    row = doctor._host_report(versions.HOSTS["kiro"], tmp_path)

    assert row["state"] == "not_installed"
    assert row["installed"] is False
    assert row["version"] is None
    assert row["path"] is None


def test_version_timeout_is_unknown(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _fake(bin_dir, "opencode", "/bin/sleep 5")
    _isolate_hosts(monkeypatch, bin_dir)
    monkeypatch.setattr(doctor, "HOST_VERSION_TIMEOUT_S", 0.1)

    row = doctor._host_report(versions.HOSTS["opencode"], tmp_path)
    warnings = doctor._integration_warnings({"opencode": row})

    assert row["installed"] is True
    assert row["state"] == "unknown"
    assert "timed out" in row["error"]
    assert warnings[0]["code"] == "integration.opencode.unknown"


def test_malformed_version_output_is_unknown(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _fake(bin_dir, "opencode", "echo 'not a version'")
    _isolate_hosts(monkeypatch, bin_dir)

    row = doctor._host_report(versions.HOSTS["opencode"], tmp_path)

    assert row["installed"] is True
    assert row["state"] == "unknown"
    assert "printed no version" in row["error"]


def test_project_venv_pyright_is_found(tmp_path, monkeypatch):
    venv = tmp_path / ".venv" / "bin"
    venv.mkdir(parents=True)
    _fake(venv, "pyright", "echo 'pyright 1.1.414'")
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))

    row = doctor._host_report(versions.HOSTS["pyright"], tmp_path)

    assert row["state"] == "verified"
    assert row["version"] == "1.1.414"
    assert row["path"] == str(venv / "pyright")


def _write_project_integrations(root: Path, home: Path) -> None:
    opencode = root / ".opencode" / "plugins"
    opencode.mkdir(parents=True)
    (opencode / "ozagia.js").write_text("// plugin\n", encoding="utf-8")
    (opencode / "ozagia").mkdir()
    (opencode / "ozagia" / "tui.js").write_text("// tui\n", encoding="utf-8")
    (opencode / "ozagia-profile").mkdir()
    (opencode / "ozagia-profile" / "tui.js").write_text(
        "// profile\n", encoding="utf-8")

    claude = root / ".claude"
    claude.mkdir()
    (claude / "settings.local.json").write_text(json.dumps({
        "hooks": {"PreToolUse": [{"hooks": [{
            "type": "command", "timeout": 20,
            "command": "/usr/bin/python -m ozagia.integrations.claude_code hook",
        }]}]},
    }), encoding="utf-8")

    codex = root / ".codex"
    codex.mkdir()
    (codex / "hooks.json").write_text(json.dumps({
        "hooks": {"PreToolUse": [{"hooks": [{
            "type": "command", "timeout": 20,
            "command": "/usr/bin/python -m ozagia.integrations.codex hook",
        }]}]},
    }), encoding="utf-8")
    (codex / "config.toml").write_text(
        "[mcp_servers.ozagia]\ncommand = '/usr/bin/python'\n",
        encoding="utf-8")
    (root / "AGENTS.md").write_text(
        "<!-- ozagia:begin -->\n## ozagia\n<!-- ozagia:end -->\n",
        encoding="utf-8")

    kiro = root / ".kiro"
    (kiro / "hooks").mkdir(parents=True)
    (kiro / "hooks" / "ozagia.json").write_text(json.dumps({
        "version": "v1",
        "hooks": [{"name": "ozagia-pre-shell", "trigger": "PreToolUse",
                   "action": {"type": "command",
                              "command": "/usr/bin/python -m ozagia.integrations.kiro hook"}}],
    }), encoding="utf-8")
    (kiro / "settings").mkdir()
    (kiro / "settings" / "mcp.json").write_text(json.dumps({
        "mcpServers": {"ozagia": {"command": "/usr/bin/python"}},
    }), encoding="utf-8")
    (kiro / "agents").mkdir()
    (kiro / "agents" / "ozagia.json").write_text(json.dumps({
        "name": "ozagia",
        "hooks": {"preToolUse": [{
            "action": {"type": "command",
                       "command": "/usr/bin/python -m ozagia.integrations.kiro hook"},
        }]},
    }), encoding="utf-8")
    (home / ".kiro").mkdir(parents=True, exist_ok=True)
    (home / ".codex").mkdir(parents=True, exist_ok=True)


def test_config_presence_reports_written_files(tmp_path, monkeypatch):
    from ozagia.integrations.presence import config_presence

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    root = tmp_path / "repo"
    root.mkdir()
    _write_project_integrations(root, home)

    presence = config_presence(root, environ={"HOME": str(home)})

    assert presence["opencode"]["state"] == "present"
    assert presence["claude"]["state"] == "present"
    assert presence["codex"]["state"] == "present"
    assert any(path.endswith(".codex/hooks.json")
               for path in presence["codex"]["found"])
    assert presence["codex"]["scopes"]["project"]["parts"]["hooks"]["state"] == (
        "present")
    assert presence["codex"]["scopes"]["project"]["parts"]["mcp"]["state"] == (
        "present")
    assert presence["kiro"]["state"] == "present"
    assert any(path.endswith(".kiro/hooks/ozagia.json")
               for path in presence["kiro"]["found"])
    assert presence["kiro"]["scopes"]["project"]["parts"]["hooks"]["state"] == (
        "present")
    assert presence["kiro"]["scopes"]["project"]["parts"]["agent"]["state"] == (
        "present")


def test_config_presence_is_absent_not_unknown_when_nothing_exists(
    tmp_path, monkeypatch,
):
    from ozagia.integrations.presence import config_presence

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    presence = config_presence(tmp_path, environ={"HOME": str(home)})

    for key in ("opencode", "claude", "codex", "kiro"):
        assert presence[key]["state"] == "absent"
        assert presence[key]["found"] == []


def test_config_presence_marks_invalid_json_unknown(tmp_path, monkeypatch):
    from ozagia.integrations.presence import config_presence

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    claude = tmp_path / ".claude"
    claude.mkdir()
    (claude / "settings.local.json").write_text("{bad json", encoding="utf-8")

    presence = config_presence(tmp_path, environ={"HOME": str(home)})

    assert presence["claude"]["state"] == "unknown"
    assert presence["claude"]["errors"]


def test_integrations_report_does_not_claim_a_live_session(
    tmp_path, monkeypatch,
):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _fake(bin_dir, "opencode", "echo 'opencode v2.4.1'")
    _isolate_hosts(monkeypatch, bin_dir)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    report = doctor._integrations_report(tmp_path)

    assert set(report["hosts"]) == set(versions.HOST_ORDER)
    assert report["hosts"]["opencode"]["state"] == "verified"
    assert report["hosts"]["kiro"]["state"] == "not_installed"
    assert "No host agent or model was started" in report["note"]
    assert report["config"]["opencode"]["state"] == "absent"


def test_capability_summary_is_bounded(tmp_path, monkeypatch):
    from ozagia.core import self_graph

    def fake_audit(*, root=None, limit=None, **kwargs):
        return {
            "state": "current",
            "catalogue": {"version": "1.0", "hash": "abc", "capabilities": 2,
                          "domains": {"graph": 2}, "surfaces": 3},
            "counts": {
                "capabilities": 2, "declared_surfaces": 3,
                "present_surfaces": 4, "observed_surfaces": 3,
                "undeclared_surfaces": 1, "duplicate_declarations": 0,
                "capabilities_without_surfaces": 0, "covered_surfaces": 2,
                "findings": 2,
                "findings_by_kind": {"observed_surface_undeclared": 2},
                "findings_by_severity": {"coverage": 2},
            },
            "truncation": {"truncated": True, "omitted": 10},
            "findings": [{"subject": "huge"} for _ in range(1000)],
        }

    monkeypatch.setattr(self_graph, "audit_self_graph", fake_audit)

    report = doctor._capabilities_report(tmp_path)

    assert report["state"] == "current"
    assert report["catalogue"] == {
        "version": "1.0", "hash": "abc", "capabilities": 2,
        "domains": {"graph": 2}, "surfaces": 3,
    }
    assert report["audit"]["counts"]["findings"] == 2
    assert "findings" not in report
    assert "huge" not in json.dumps(report)
    assert report["warnings"][0]["code"] == "capabilities.findings"


def test_capability_audit_failure_is_unknown_not_verified(tmp_path, monkeypatch):
    from ozagia.core import self_graph

    def fail(**kwargs):
        raise RuntimeError("audit broke")

    monkeypatch.setattr(self_graph, "audit_self_graph", fail)

    report = doctor._capabilities_report(tmp_path)

    assert report["state"] == "unknown"
    assert "audit broke" in report["error"]
    assert report["warnings"][0]["code"] == "capabilities.unknown"


def test_diagnose_has_integration_and_capability_sections(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _isolate_hosts(monkeypatch, bin_dir)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(home / ".codex"))
    for name in ("OZAGIA_CONFIG", "OZAGIA_STATE_DIR", "OZAGIA_ROOT"):
        monkeypatch.delenv(name, raising=False)

    from ozagia.core import self_graph

    monkeypatch.setattr(self_graph, "audit_self_graph", lambda **kwargs: {
        "state": "current",
        "catalogue": {"version": "1.0", "hash": "abc", "capabilities": 1,
                      "domains": {"status": 1}, "surfaces": 2},
        "counts": {"capabilities": 1, "declared_surfaces": 2,
                   "findings": 0, "findings_by_kind": {},
                   "findings_by_severity": {}},
        "truncation": {},
        "findings": [],
    })

    report = doctor.diagnose(tmp_path)

    assert report["integrations"]["hosts"]["opencode"]["state"] == "not_installed"
    assert report["capabilities"]["state"] == "current"
    assert isinstance(report["warnings"], list)
