"""Unified read-only status snapshot."""
from __future__ import annotations

import json
import os
import subprocess
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
    "workspace", "resources", "files", "runtime", "daemon", "doctor",
    "config",
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
            return {"id": 2, "ok": True, "stats": {
                "pid": 4321, "uptime_s": 12.5, "idle_timeout_s": 300}}

        def close(self) -> None:
            return None

    return FakeDaemon


@pytest.fixture
def components(tmp_path: Path, monkeypatch) -> SimpleNamespace:
    root = tmp_path / "repo"
    root.mkdir()
    cfg_path = root / ".ozagia.toml"
    cfg_path.write_text("# test project\n", encoding="utf-8")
    state: dict[str, Any] = {"snapshot": "fresh", "ping": None,
                             "stats": None}
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
    assert report["doctor"]["state"] == "ok"
    assert report["doctor"]["report"]["snapshot"]["state"] == "fresh"
    assert report["config"]["state"] == "configured"
    assert report["config"]["path"] == str(components.cfg_path)
    assert report["config"]["report"]["runtime_services"] == ["api"]
    assert components.calls == [
        (str(status_module.protocol.socket_path(components.root)), 0.5)]


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
    assert payload["runtime"]["report"]["items"][1]["state"] == "stale"

    assert status_cli.main(["--root", str(components.root)]) == 0
    text = capsys.readouterr().out
    for label in ("workspace:", "resources:", "files:", "daemon:",
                  "runtime:", "snapshot:", "config:"):
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
    assert not (root / ".ozagia").exists()
