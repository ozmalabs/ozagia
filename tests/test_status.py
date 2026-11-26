"""Unified read-only status snapshot."""
from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ozagia import status_cli
from ozagia.config import ConfigError
from ozagia.core import status as status_module
from ozagia.core.files import FileLedgerError
from ozagia.core.worktree import WorktreeError
from ozagia.daemon.client import (
    DaemonError,
    DaemonTimeout,
    DaemonUnavailable,
)

KEYS = {
    "schema", "checked_at", "root", "ok", "errors", "degraded",
    "workspace", "resources", "files", "runtime", "daemon", "jobs",
    "admissions", "providers", "doctor", "integrations", "capabilities",
    "snapshot", "sessions", "ownership", "config", "reviews", "mesh",
    "flight",
}


def _raise(exc: BaseException):
    def fail(*args: Any, **kwargs: Any) -> Any:
        raise exc
    return fail


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env.update({
        "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.com",
        "GIT_CONFIG_NOSYSTEM": "1",
    })
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          text=True, check=True, env=env)


def _workspace_report() -> dict[str, Any]:
    return {
        "schema": 1,
        "root": "/repo",
        "base": "main",
        "checked_at": "2026-11-16T00:00:00+00:00",
        "ok": True,
        "worktrees": [{
            "path": "/repo",
            "branch": "main",
            "head": "abc123",
            "dirty": {"staged": 0, "unstaged": 0, "untracked": 1,
                      "conflicted": 0},
            "base": {"branch": "main", "ahead": 0, "behind": 0,
                     "merge_base": "abc123"},
        }],
        "findings": [],
    }


def _resources_report() -> dict[str, Any]:
    return {
        "schema": 1,
        "checked_at": "2026-11-16T00:00:00+00:00",
        "host": {
            "platform": "linux",
            "cpu_count": 8,
            "load": {"1m": 0.5, "5m": 0.4, "15m": 0.3},
            "memory": {"source": "/proc/meminfo", "total_mb": 16_000.0,
                       "available_mb": 8_000.0, "used_pct": 50.0},
            "pressure": {},
        },
        "disks": [{
            "path": "/repo", "mount": "/", "fstype": "ext4",
            "free_gb": 100.0, "free_pct": 50.0, "is_tmpfs": False,
        }],
        "top_groups": [],
        "policy": {"errors": []},
        "history": {},
        "enforcement": {"available": False},
    }


def _runtime_report() -> dict[str, Any]:
    service = {
        "name": "api", "kind": "service", "state": "configured",
        "configured": True, "running": False, "relay": None, "target": None,
        "mode": "background", "pid": None, "description": "API",
        "workdir": "services/api",
    }
    tunnel = {
        "name": "db", "kind": "tunnel", "state": "stale", "configured": True,
        "running": False, "relay": "prod", "target": "db.internal:5432",
        "description": None,
    }
    return {
        "configured": True, "live": False,
        "services": [service], "tunnels": [tunnel], "processes": [],
        "items": [service, tunnel],
        "context": "",
        "relays": [],
        "note": "configuration is declarative",
    }


def _doctor_report(root: Path, snapshot_state: str) -> dict[str, Any]:
    return {
        "root": str(root),
        "root_exists": True,
        "config": {"path": None, "present": False, "valid": True,
                   "adapters_configured": []},
        "snapshot": {
            "path": str(root / ".ozagia" / "graph.json"),
            "state": snapshot_state,
            "nodes": 12,
            "edges": 34,
        },
        "parsers": {},
        "language_servers": {},
        "external_tools": {},
    }


def _daemon_class(state: dict[str, str | None], calls: list[tuple[str, float]]):
    class FakeDaemon:
        def __init__(self, sock: str | Path, *, timeout: float = 30.0) -> None:
            self.sock = str(sock)
            self.timeout = timeout
            calls.append((self.sock, timeout))

        def __enter__(self) -> "FakeDaemon":
            return self

        def __exit__(self, *exc: Any) -> None:
            return None

        def ping(self, *, timeout: float | None = None) -> dict[str, Any]:
            failure = state.get("ping")
            if failure == "absent":
                raise DaemonUnavailable("no daemon")
            if failure == "timeout":
                raise DaemonTimeout("timed out")
            if failure == "error":
                raise DaemonError("bad reply")
            return {"id": 1, "ok": True, "version": 1, "root": "/repo",
                    "pid": 4321}

        def stats(self) -> dict[str, Any]:
            if state.get("stats") == "timeout":
                raise DaemonTimeout("stats timed out")
            if state.get("stats") == "error":
                raise DaemonError("stats failed")
            stats = {
                "pid": 4321, "uptime_s": 12.5, "idle_timeout_s": 300}
            if state.get("job_stats") is not None:
                stats["jobs"] = state["job_stats"]
            return {"id": 2, "ok": True, "stats": stats}

        def job_list(self) -> dict[str, Any]:
            failure = state.get("jobs")
            if failure == "timeout":
                raise DaemonTimeout("job table timed out")
            if failure == "error":
                raise DaemonError("job table failed")
            rows = state.get("job_rows") or []
            return {"id": 3, "ok": True, "jobs": rows,
                    "running": sum(1 for job in rows
                                   if job.get("status") in
                                   ("running", "detached"))}

        def secret_providers(self) -> dict[str, Any]:
            failure = state.get("providers")
            if failure == "timeout":
                raise DaemonTimeout("providers timed out")
            if failure == "error":
                raise DaemonError("providers failed")
            return {"id": 4, "ok": True,
                    "providers": state.get("provider_rows") or [],
                    "storage": "memory-only"}

        def close(self) -> None:
            return None

    return FakeDaemon


@pytest.fixture
def components(tmp_path: Path, monkeypatch) -> SimpleNamespace:
    root = tmp_path / "repo"
    root.mkdir()
    state_root = root / ".ozagia"
    state_root.mkdir()
    (state_root / "graph.json").write_text("{}", encoding="utf-8")
    cfg_path = root / ".ozagia.toml"
    cfg_path.write_text("# test project\n", encoding="utf-8")
    state: dict[str, Any] = {
        "snapshot": "fresh", "ping": None, "stats": None,
        "jobs": None, "job_rows": [], "providers": None,
        "provider_rows": [],
        "job_stats": {
            "jobs_refused": 0, "resource_refusals": 0,
            "jobs_resource_killed": 0, "resource_events": 0,
            "cgroup_oom_kills": 0, "stopped_on_load": 0,
            "resource_peak_mb": 0.0, "resource_peak_disk_write_mb": 0.0,
        },
    }
    calls: list[tuple[str, float]] = []
    config = SimpleNamespace(
        source=cfg_path,
        adapters=("adapter:register",),
        source_dirs=("src",),
        test_dirs=("tests",),
        jobs_enabled=False,
        runtime_services=(SimpleNamespace(name="api"),),
        runtime_tunnels=(SimpleNamespace(name="db"),),
    )

    monkeypatch.setattr(status_module, "config_path", lambda value: cfg_path)
    monkeypatch.setattr(status_module, "load_project_config",
                        lambda value: config)
    monkeypatch.setattr(status_module, "worktree_check",
                        lambda value: _workspace_report())
    monkeypatch.setattr(
        status_module, "resources_status",
        lambda value, *, path=None, top=10: _resources_report())
    monkeypatch.setattr(status_module, "ledger_path",
                        lambda value: root / ".ozagia" / "files" / "ledger.jsonl")
    monkeypatch.setattr(
        status_module, "list_files",
        lambda value, *, limit=200: [{"path": "notes.md", "size": 12}])
    monkeypatch.setattr(
        status_module, "orphans", lambda value: [{"path": "notes.md"}])
    monkeypatch.setattr(
        status_module, "new_file_count",
        lambda value: {"count": 2, "since_hours": 24, "session": None})
    monkeypatch.setattr(status_module, "runtime_status",
                        lambda value: _runtime_report())
    monkeypatch.setattr(
        status_module, "diagnose",
        lambda value: _doctor_report(root, state["snapshot"]))
    monkeypatch.setattr(status_module, "DaemonClient",
                        _daemon_class(state, calls))
    for name in ("OZAGIA_MESH", "OZAGIA_MESH_ID", "OZAGIA_MESH_HARNESS",
                 "OZAGIA_MESH_SESSION"):
        monkeypatch.delenv(name, raising=False)
    return SimpleNamespace(root=root, cfg_path=cfg_path, state=state,
                           calls=calls, config=config)


def test_status_all_present(components: SimpleNamespace) -> None:
    report = status_module.status(components.root, timeout=0.5)

    assert set(report) == KEYS
    assert report["schema"] == status_module.SCHEMA
    assert report["root"] == str(components.root)
    assert report["ok"] is True
    assert report["errors"] == []
    assert report["degraded"] == []
    assert report["workspace"]["state"] == "ok"
    assert report["workspace"]["report"] == _workspace_report()
    assert report["resources"]["state"] == "ok"
    assert report["resources"]["report"] == _resources_report()
    assert report["files"]["state"] == "ok"
    assert report["files"]["report"]["counts"] == {
        "records": 1, "orphans": 1, "new": 2}
    assert report["files"]["report"]["ledger"]["present"] is False
    assert report["runtime"]["state"] == "ok"
    assert report["runtime"]["report"]["items"][0]["state"] == "configured"
    assert report["runtime"]["report"]["items"][1]["state"] == "stale"
    assert report["daemon"]["state"] == "running"
    assert report["daemon"]["report"]["pid"] == 4321
    assert report["daemon"]["report"]["stats"]["uptime_s"] == 12.5
    assert report["daemon"]["report"]["timeout_s"] == 0.5
    assert report["jobs"]["state"] == "ok"
    assert report["jobs"]["report"]["counts"]["known"] == 0
    assert report["admissions"]["state"] == "ok"
    assert report["admissions"]["report"]["counts"]["jobs_refused"] == 0
    assert report["providers"]["state"] == "ok"
    assert report["providers"]["report"]["counts"]["known"] == 0
    assert report["doctor"]["state"] == "ok"
    assert report["doctor"]["report"]["snapshot"]["state"] == "fresh"
    assert report["snapshot"]["state"] == "ok"
    assert report["snapshot"]["report"]["nodes"] == 12
    assert report["snapshot"]["report"]["edges"] == 34
    assert report["snapshot"]["report"]["counts_read"] is True
    assert report["snapshot"]["report"]["size_bytes"] == 2
    assert report["sessions"]["state"] == "absent"
    assert report["ownership"]["state"] == "absent"
    assert report["integrations"]["state"] == "unknown"
    assert report["capabilities"]["state"] == "unknown"
    assert report["reviews"]["state"] == "absent"
    assert report["mesh"] == {"state": "absent", "error": None, "report": None}
    assert not (components.root / ".ozagia" / "mesh").exists()
    assert report["config"]["state"] == "configured"
    assert report["config"]["path"] == str(components.cfg_path)
    assert report["config"]["report"]["runtime_services"] == ["api"]
    assert components.calls == [
        (str(status_module.protocol.socket_path(components.root)), 0.5)] * 3


def test_status_carries_bounded_integration_and_capability_summaries(
    components: SimpleNamespace, monkeypatch,
) -> None:
    doctor_report = _doctor_report(components.root, "fresh")
    doctor_report["integrations"] = {
        "note": "presence only",
        "hosts": {
            "opencode": {
                "label": "OpenCode TUI", "state": "verified",
                "installed": True, "version": "2.4.1",
                "path": "/bin/opencode", "tested_range": "2.3.0, 2.4.1",
                "verification": "host-launch without model", "error": None,
            },
        },
        "config": {"opencode": {"state": "present",
                                "found": ["/repo/.opencode/plugins/ozagia.js"],
                                "errors": []}},
        "warnings": [],
    }
    doctor_report["capabilities"] = {
        "state": "current",
        "catalogue": {"version": "1.0", "hash": "abc", "capabilities": 102,
                      "domains": {"graph": 31}, "surfaces": 156},
        "audit": {
            "state": "current",
            "counts": {"capabilities": 102, "declared_surfaces": 156,
                       "findings": 1},
            "findings_by_kind": {"observed_surface_undeclared": 1},
            "findings_by_severity": {"coverage": 1},
            "findings": [{"subject": "must-not-be-copied"}],
        },
        "error": None,
        "warnings": [{"component": "capabilities",
                      "code": "capabilities.findings",
                      "message": "1 finding"}],
    }
    monkeypatch.setattr(status_module, "diagnose",
                        lambda value: doctor_report)

    report = status_module.status(components.root)

    assert report["integrations"]["state"] == "ok"
    host = report["integrations"]["report"]["hosts"]["opencode"]
    assert host["state"] == "verified"
    assert host["version"] == "2.4.1"
    assert report["integrations"]["report"]["config"]["opencode"]["state"] == (
        "present")
    assert report["capabilities"]["state"] == "ok"
    audit = report["capabilities"]["report"]["audit"]
    assert audit["counts"]["findings"] == 1
    assert "findings" not in audit
    assert "must-not-be-copied" not in json.dumps(report["capabilities"])


def test_status_missing_daemon_is_absent_not_empty(
    components: SimpleNamespace,
) -> None:
    components.state["ping"] = "absent"

    report = status_module.status(components.root)

    assert report["ok"] is True
    assert report["daemon"]["state"] == "absent"
    assert report["daemon"]["error"] == "no daemon"
    assert report["daemon"]["report"]["socket"]
    assert report["daemon"]["report"]["ping"] is None
    assert report["daemon"]["report"]["stats"] is None
    assert any(item["code"] == "daemon.absent"
               for item in report["degraded"])


def test_status_uses_explicit_session_socket_over_another_root_socket(
    components: SimpleNamespace, monkeypatch, tmp_path: Path,
) -> None:
    session_sock = tmp_path / "session-missing.sock"
    other_root_sock = tmp_path / "other-root.sock"
    other_root_sock.touch()
    monkeypatch.setenv("OZAGIA_DAEMON_SOCK", str(session_sock))
    components.state["ping"] = "absent"

    report = status_module.status(components.root)

    assert report["daemon"]["state"] == "absent"
    assert report["daemon"]["report"]["socket"] == str(session_sock)
    assert components.calls[0][0] == str(session_sock)


def test_status_unresponsive_daemon_is_unknown(components: SimpleNamespace) -> None:
    components.state["ping"] = "timeout"

    report = status_module.status(components.root)

    assert report["ok"] is True
    assert report["daemon"]["state"] == "unknown"
    assert any(item["code"] == "daemon.unknown"
               for item in report["degraded"])


def test_status_daemon_stats_failure_keeps_running(
    components: SimpleNamespace,
) -> None:
    components.state["stats"] = "timeout"

    report = status_module.status(components.root)

    assert report["daemon"]["state"] == "running"
    assert report["daemon"]["report"]["stats"] is None
    assert "stats timed out" in report["daemon"]["report"]["stats_error"]
    assert any(item["code"] == "daemon.stats"
               for item in report["degraded"])


def test_status_worktree_error_is_degraded(
    components: SimpleNamespace, monkeypatch,
) -> None:
    monkeypatch.setattr(status_module, "worktree_check",
                        _raise(WorktreeError("git is missing")))

    report = status_module.status(components.root)

    assert report["ok"] is True
    assert report["workspace"] == {
        "state": "unavailable", "error": "git is missing", "report": None}
    assert any(item["code"] == "workspace.unavailable"
               for item in report["degraded"])


def test_status_resources_error_is_degraded(
    components: SimpleNamespace, monkeypatch,
) -> None:
    monkeypatch.setattr(status_module, "resources_status",
                        _raise(OSError("no procfs")))

    report = status_module.status(components.root)

    assert report["ok"] is True
    assert report["resources"]["state"] == "unavailable"
    assert report["resources"]["error"] == "no procfs"
    assert any(item["code"] == "resources.unavailable"
               for item in report["degraded"])


def test_status_file_ledger_error_is_degraded(
    components: SimpleNamespace, monkeypatch,
) -> None:
    monkeypatch.setattr(status_module, "list_files",
                        _raise(FileLedgerError("ledger is unreadable")))

    report = status_module.status(components.root)

    assert report["ok"] is True
    assert report["files"]["state"] == "unavailable"
    assert report["files"]["report"] is None
    assert any(item["code"] == "files.unavailable"
               for item in report["degraded"])


@pytest.mark.parametrize("snapshot_state", ["stale", "missing"])
def test_status_snapshot_state_is_explicit(
    components: SimpleNamespace, snapshot_state: str,
) -> None:
    components.state["snapshot"] = snapshot_state

    report = status_module.status(components.root)

    assert report["ok"] is True
    assert report["doctor"]["state"] == "degraded"
    assert report["doctor"]["report"]["snapshot"]["state"] == snapshot_state
    assert any(item["code"] == f"snapshot.{snapshot_state}"
               for item in report["degraded"])


def test_status_reports_merged_runtime_names_from_both_configs(
    tmp_path: Path, monkeypatch,
) -> None:
    for name in ("OZAGIA_CONFIG", "OZAGIA_ROOT", "OZAGIA_STATE_DIR"):
        monkeypatch.delenv(name, raising=False)
    root = tmp_path / "repo"
    root.mkdir()
    legacy = root / ".ozagia.toml"
    legacy.write_text(
        '[relays.local]\nspecs = [{ kind = "local" }]\n\n'
        '[runtime.services.api]\ncommand = ["python", "-m", "app"]\n',
        encoding="utf-8")
    repo = root / "ozagia" / "config.toml"
    repo.parent.mkdir()
    repo.write_text(
        '[runtime.services.worker]\ncommand = ["python", "-m", "worker"]\n\n'
        '[runtime.tunnels.database]\n'
        'relay = "local"\ntarget = "db.example:5432"\n',
        encoding="utf-8")

    config_section, project = status_module._config_status(root)
    assert config_section["state"] == "configured"
    assert config_section["report"]["source"] == str(legacy.resolve())
    assert config_section["report"]["runtime_services"] == ["api", "worker"]
    assert config_section["report"]["runtime_tunnels"] == ["database"]

    degraded: list[dict[str, str]] = []
    runtime_section = status_module._runtime_status(root, project, degraded)
    assert degraded == []
    assert {(item["kind"], item["name"])
            for item in runtime_section["report"]["items"]} == {
        ("service", "api"), ("service", "worker"), ("tunnel", "database"),
    }


def test_status_invalid_config_is_a_hard_error(
    components: SimpleNamespace, monkeypatch, capsys,
) -> None:
    monkeypatch.setattr(status_module, "load_project_config",
                        _raise(ConfigError("bad toml")))

    report = status_module.status(components.root)

    assert report["ok"] is False
    assert report["config"]["state"] == "invalid"
    assert report["config"]["error"] == "bad toml"
    assert [item["code"] for item in report["errors"]] == ["config.invalid"]

    code = status_cli.main(["--root", str(components.root), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 1
    assert payload["ok"] is False
    assert payload["config"]["state"] == "invalid"


def test_status_cli_json_and_text(
    components: SimpleNamespace, monkeypatch, capsys,
) -> None:
    report = status_module.status(components.root)
    monkeypatch.setattr(status_cli, "status",
                        lambda value, **kwargs: report)

    assert status_cli.main(["--root", str(components.root), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == status_module.SCHEMA
    assert payload["daemon"]["state"] == "running"
    assert payload["jobs"]["state"] == "ok"
    assert payload["admissions"]["state"] == "ok"
    assert payload["providers"]["state"] == "ok"
    assert payload["snapshot"]["state"] == "ok"
    assert payload["sessions"]["state"] == "absent"
    assert payload["ownership"]["state"] == "absent"
    assert payload["runtime"]["report"]["items"][1]["state"] == "stale"

    assert status_cli.main(["--root", str(components.root)]) == 0
    text = capsys.readouterr().out
    for label in ("workspace:", "resources:", "files:", "daemon:", "jobs:",
                  "admissions:", "providers:", "runtime:", "snapshot:",
                  "snapshot metrics:", "sessions:", "ownership:", "config:"):
        assert label in text
    assert "service api: configured, not running" in text
    assert "tunnel db: stale" in text


def test_status_real_repository_is_read_only(
    tmp_path: Path, monkeypatch,
) -> None:
    for name in ("OZAGIA_CONFIG", "OZAGIA_ROOT", "OZAGIA_STATE_DIR",
                 "OZAGIA_DAEMON_SOCK"):
        monkeypatch.delenv(name, raising=False)
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "symbolic-ref", "HEAD", "refs/heads/main")
    (root / "README.md").write_text("# Test\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(root, "commit", "-q", "-m", "initial")

    report = status_module.status(root, timeout=0.1)

    assert report["ok"] is True
    assert report["workspace"]["state"] == "ok"
    worktree = report["workspace"]["report"]["worktrees"][0]
    assert worktree["branch"] == "main"
    assert report["resources"]["state"] == "ok"
    assert report["files"]["state"] == "ok"
    assert report["files"]["report"]["ledger"]["present"] is False
    assert report["runtime"]["state"] == "ok"
    assert report["daemon"]["state"] == "absent"
    assert report["doctor"]["report"]["snapshot"]["state"] == "missing"
    assert any(item["code"] == "snapshot.missing"
               for item in report["degraded"])
    assert report["snapshot"]["state"] == "absent"
    assert report["sessions"]["state"] == "absent"
    assert report["ownership"]["state"] == "absent"
    assert report["jobs"]["state"] == "absent"
    assert report["admissions"]["state"] == "absent"
    assert report["providers"]["state"] == "absent"
    assert not (root / ".ozagia").exists()


def test_status_jobs_are_bounded_and_content_free(
    components: SimpleNamespace,
) -> None:
    components.state["job_rows"] = [{
        "id": f"j{index}", "status": "running", "pid": 1000 + index,
        "command": "tool --password hunter2", "argv": ["--token", "hunter2"],
        "cwd": "/private/checkout", "log": "/state/jobs/j1.log",
        "env_keys": ["PASSWORD"], "started": "2026-11-25T10:00:00Z",
        "ended": None, "exit_code": None, "signal": None,
        "harness": "opencode", "session": "session-with-a-long-id",
        "enforcement": "rlimit+watch", "peak_memory_mb": 12,
        "peak_disk_write_mb": 3, "log_bytes": 99, "elapsed_s": 5,
        "leader_exited": False, "group_alive": True,
    } for index in range(status_module.JOB_LIMIT + 2)]

    report = status_module.status(components.root)

    jobs = report["jobs"]
    assert jobs["state"] == "ok"
    assert jobs["report"]["counts"]["known"] == status_module.JOB_LIMIT + 2
    assert jobs["report"]["counts"]["shown"] == status_module.JOB_LIMIT
    assert jobs["report"]["truncated"] is True
    row = jobs["report"]["jobs"][0]
    assert row["id"] == "j0"
    assert row["state"] == "running"
    assert row["pid"] == 1000
    assert row["argv_count"] == 2
    assert row["has_command"] is True
    assert row["peak_memory_mb"] == 12
    assert row["peak_disk_write_mb"] == 3
    for key in ("command", "argv", "cwd", "log", "env_keys", "session"):
        assert key not in row
    serialized = json.dumps(report["jobs"])
    assert "hunter2" not in serialized
    assert "tool --password" not in serialized
    assert "/private/checkout" not in serialized
    assert "/state/jobs" not in serialized


def test_status_jobs_absent_and_unknown_states(
    components: SimpleNamespace,
) -> None:
    components.state["ping"] = "absent"
    report = status_module.status(components.root)
    assert report["jobs"]["state"] == "absent"
    assert report["providers"]["state"] == "absent"
    assert report["admissions"]["state"] == "absent"

    components.state["ping"] = None
    components.state["jobs"] = "timeout"
    components.state["providers"] = "error"
    report = status_module.status(components.root)
    assert report["jobs"]["state"] == "unknown"
    assert report["providers"]["state"] == "unknown"
    assert any(item["code"] == "jobs.unknown"
               for item in report["degraded"])
    assert any(item["code"] == "providers.unknown"
               for item in report["degraded"])


def test_status_admissions_come_from_daemon_job_counters(
    components: SimpleNamespace,
) -> None:
    components.state["job_stats"] = {
        "jobs_refused": 2, "resource_refusals": 3,
        "jobs_resource_killed": 1, "resource_events": 4,
        "cgroup_oom_kills": 1, "stopped_on_load": 0,
        "resource_peak_mb": 64.0, "resource_peak_disk_write_mb": 2.0,
    }

    report = status_module.status(components.root)

    assert report["admissions"]["state"] == "ok"
    counts = report["admissions"]["report"]["counts"]
    assert counts["jobs_refused"] == 2
    assert counts["resource_refusals"] == 3
    assert counts["jobs_resource_killed"] == 1
    assert counts["resource_events"] == 4

    components.state["job_stats"] = None
    report = status_module.status(components.root)
    assert report["admissions"]["state"] == "unknown"


def test_status_providers_use_the_daemon_reply_and_are_bounded(
    components: SimpleNamespace,
) -> None:
    components.state["provider_rows"] = [{
        "name": f"provider-{index}", "enabled": True, "available": True,
        "ready": index == 0, "requires": ["optional package"],
        "notes": "no value is returned",
    } for index in range(status_module.PROVIDER_LIMIT + 2)]

    report = status_module.status(components.root)

    providers = report["providers"]
    assert providers["state"] == "ok"
    assert providers["report"]["kind"] == "secret_providers"
    assert "not a connection" in providers["report"]["note"]
    assert providers["report"]["counts"]["known"] == (
        status_module.PROVIDER_LIMIT + 2)
    assert providers["report"]["counts"]["ready"] == 1
    assert providers["report"]["counts"]["shown"] == (
        status_module.PROVIDER_LIMIT)
    assert providers["report"]["truncated"] is True
    assert len(providers["report"]["providers"]) == status_module.PROVIDER_LIMIT
    assert "hunter2" not in json.dumps(providers)


def _write_profile(
    root: Path, name: str, envelope: dict[str, Any],
) -> Path:
    directory = root / ".ozagia" / "session_profiles"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(json.dumps(envelope), encoding="utf-8")
    return path


def test_status_sessions_are_metadata_only_and_empty_is_not_missing(
    components: SimpleNamespace,
) -> None:
    directory = components.root / ".ozagia" / "session_profiles"
    directory.mkdir()
    report = status_module.status(components.root)
    assert report["sessions"]["state"] == "ok"
    assert report["sessions"]["report"]["profiles"] == 0
    assert report["sessions"]["report"]["profiles_read"] == 0

    now = datetime.now(timezone.utc).isoformat()
    _write_profile(components.root, "one.json", {
        "session_id": "session-super-secret-id",
        "harness": "opencode",
        "observed_at": now,
        "host_profile": {
            "source": "opencode_server_events",
            "model_calls": 2,
            "models": ["provider/model-a", "provider/model-b"],
            "tokens": {"input": 20, "output": 7, "reasoning": 3},
            "timing_ms": {"first_token": 100, "model_turn": 800},
            "coverage": {"host_tool_calls": 4, "assistant_turns": 2},
            "tools": {"read": {"completed": {"count": 4}}},
            "capture_diagnostics": [{
                "capture_id": "0d6b8d4e-2c7f-4b0b-8e3a-1b2c3d4e5f60",
                "counters": {"events_received": 9, "step_events": 2,
                             "tool_lifecycle_terminal": 4},
            }],
            "prompt": "must not be copied",
        },
        "daemon_profile": {
            "tool_calls_observed": 4,
            "tools_by_name": {"read": 4},
        },
    })

    report = status_module.status(components.root)

    sessions = report["sessions"]
    assert sessions["state"] == "ok"
    assert sessions["report"]["profiles"] == 1
    assert sessions["report"]["profiles_read"] == 1
    assert sessions["report"]["models"] == [
        "provider/model-a", "provider/model-b"]
    totals = sessions["report"]["totals"]
    assert totals["model_calls"] == 2
    assert totals["tokens"]["input"] == 20
    assert totals["tokens"]["output"] == 7
    assert totals["tool_calls"] == 4
    assert totals["timing_ms"]["model_turn"] == 800
    diagnostics = sessions["report"]["capture_diagnostics"]
    assert diagnostics["captures"] == 1
    assert diagnostics["counters"]["events_received"] == 9
    assert diagnostics["counters"]["tool_lifecycle_terminal"] == 4
    serialized = json.dumps(sessions)
    assert "must not be copied" not in serialized
    assert "session-super-secret-id" not in serialized
    assert "tools_by_name" not in serialized


def test_status_sessions_malformed_profile_is_unknown_with_partial_report(
    components: SimpleNamespace,
) -> None:
    _write_profile(components.root, "good.json", {
        "session_id": "good",
        "harness": "opencode",
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "host_profile": {"models": ["m"], "model_calls": 1},
    })
    (components.root / ".ozagia" / "session_profiles" / "bad.json").write_text(
        "{not json", encoding="utf-8")

    report = status_module.status(components.root)

    sessions = report["sessions"]
    assert sessions["state"] == "unknown"
    assert sessions["report"]["malformed"] == 1
    assert sessions["report"]["profiles_read"] == 1
    assert any(item["code"] == "sessions.malformed"
               for item in report["degraded"])


def test_status_snapshot_without_counts_says_they_were_not_read(
    components: SimpleNamespace, monkeypatch,
) -> None:
    doctor_report = _doctor_report(components.root, "fresh")
    del doctor_report["snapshot"]["nodes"]
    del doctor_report["snapshot"]["edges"]
    monkeypatch.setattr(status_module, "diagnose",
                        lambda value: doctor_report)

    report = status_module.status(components.root)

    assert report["snapshot"]["state"] == "ok"
    assert report["snapshot"]["report"]["counts_read"] is False
    assert report["snapshot"]["report"]["nodes"] is None
    assert report["snapshot"]["report"]["size_bytes"] == 2
    assert "not reported" in report["snapshot"]["report"]["counts_note"]


def test_status_ownership_reports_dirty_uncommitted_journaled_edits(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "symbolic-ref", "HEAD", "refs/heads/main")
    target = root / "a.txt"
    target.write_text("one\n", encoding="utf-8")
    _git(root, "add", "a.txt")
    _git(root, "commit", "-q", "-m", "initial")
    head = _git(root, "rev-parse", "HEAD").stdout.strip()
    target.write_text("two\n", encoding="utf-8")
    state = root / ".ozagia"
    state.mkdir()
    journal = state / "journal.jsonl"
    journal.write_text(json.dumps({
        "ts": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "epoch": time.time(),
        "kind": "edit",
        "session": {"harness": "opencode", "id": "session-abcdef123456"},
        "files": ["a.txt"],
        "head": head,
    }) + "\n", encoding="utf-8")

    degraded: list[dict[str, str]] = []
    section = status_module._ownership_status(root, degraded)

    assert section["state"] == "ok"
    assert degraded == []
    assert section["report"]["counts"]["files"] == 1
    assert section["report"]["counts"]["sessions"] == 1
    row = section["report"]["rows"][0]
    assert row["path"] == "a.txt"
    assert row["label"] == "opencode session-"
    assert row["live"] is False
    assert row["committed"] is False
    assert "session-abcdef123456" not in json.dumps(section)


def test_status_ownership_unreadable_journal_is_unknown(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".ozagia").mkdir()
    (root / ".ozagia" / "journal.jsonl").mkdir()

    degraded: list[dict[str, str]] = []
    section = status_module._ownership_status(root, degraded)

    assert section["state"] == "unknown"
    assert any(item["code"] == "ownership.unreadable" for item in degraded)


def test_status_sessions_absent_does_not_create_state(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()

    degraded: list[dict[str, str]] = []
    section = status_module._sessions_status(root, degraded)

    assert section["state"] == "absent"
    assert not (root / ".ozagia").exists()


# --------------------------------------------------------------------------- #
# session mesh (read-only)
# --------------------------------------------------------------------------- #
def test_status_mesh_is_absent_and_creates_nothing(components) -> None:
    report = status_module.status(components.root, timeout=0.5)

    assert report["mesh"]["state"] == "absent"
    assert report["mesh"]["report"] is None
    assert "mesh: off" in status_module.format_status(report)
    assert not (components.root / ".ozagia" / "mesh").exists()


def test_status_mesh_ok_is_metadata_only(components, monkeypatch) -> None:
    from ozagia.core import mesh as mesh_module

    root = components.root
    mesh_module.register(root, "opencode", "a", pid=os.getpid(),
                        exposure="coordinate")
    second = mesh_module.register(root, "opencode", "b", pid=os.getpid(),
                                  exposure="read")
    mesh_module.send(root, "opencode", "a", to="opencode:b", kind="note",
                     subject="SUBJECT CANARY", body="BODY CANARY")
    monkeypatch.setenv("OZAGIA_MESH_HARNESS", "opencode")
    monkeypatch.setenv("OZAGIA_MESH_SESSION", "b")

    report = status_module.status(root, timeout=0.5)
    mesh = report["mesh"]

    assert mesh["state"] == "ok"
    assert mesh["report"]["self"]["key"] == second["key"]
    assert mesh["report"]["self"]["role"] == "peer"
    assert mesh["report"]["unread"] == 1
    assert mesh["report"]["counts"]["sessions"]["live"] == 2
    assert [row["session"] for row in mesh["report"]["peers"]] == ["a"]
    assert "SUBJECT CANARY" not in json.dumps(mesh)
    assert "BODY CANARY" not in json.dumps(mesh)
    assert "mesh: default" in status_module.format_status(report)


def test_status_mesh_read_error_is_unknown_without_creating_state(
        components, monkeypatch) -> None:
    from ozagia.core import mesh as mesh_module

    monkeypatch.setattr(mesh_module, "mesh_configured", lambda root: True)
    monkeypatch.setattr(
        mesh_module, "status",
        _raise(mesh_module.MeshIOError("mesh directory is unreadable")))

    report = status_module.status(components.root, timeout=0.5)
    mesh = report["mesh"]

    assert mesh["state"] == "unknown"
    assert mesh["report"] is None
    assert "unreadable" in mesh["error"]
    assert not (components.root / ".ozagia" / "mesh").exists()
