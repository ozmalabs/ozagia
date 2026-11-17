"""Read-only action readiness tests.

The readiness join reads configuration, runtime status and log metadata. These
tests use temporary repositories and fake/bogus runtime records; no service,
tunnel, daemon, network call or secret resolution is performed.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from ozagia.config import state_path
from ozagia.core import action_readiness as A
from ozagia.core import actions
from ozagia.core import runtime
from ozagia.core import secrets

CANARY = "canary-value-must-never-appear"
SECRET_ENV = "OZAGIA_ACTION_READINESS_CANARY"


def _write_config(root: Path, *, service_env: str = "",
                  service_mode: str = "background",
                  service_relay: str | None = None,
                  tunnel: bool = True) -> None:
    env_line = f"env = {{ API_TOKEN = {json.dumps(service_env)} }}" if service_env else ""
    relay_line = f'relay = "{service_relay}"' if service_relay else ""
    text = f"""
[relays.local]
specs = [{{ kind = "local" }}]
allowed_kinds = ["local"]
max_timeout_s = 20

[runtime.services.api]
command = ["python", "-m", "app"]
mode = "{service_mode}"
{relay_line}
{env_line}

[runtime.services.migrate]
command = ["python", "manage.py", "migrate"]
mode = "foreground"
"""
    if tunnel:
        text += """
[runtime.tunnels.database]
relay = "local"
target = "db.example.test:5432"
local_port = 0
"""
    (root / ".ozagia.toml").write_text(text, encoding="utf-8")


def _snapshot(root: Path) -> dict[str, tuple[int, int]]:
    return {
        path.relative_to(root).as_posix(): (path.stat().st_size,
                                            path.stat().st_mtime_ns)
        for path in root.rglob("*") if path.is_file()
    }


def _runtime_id(kind: str, name: str) -> str:
    return hashlib.sha256(f"{kind}\0{name}".encode("utf-8")).hexdigest()[:24]


def _operation(report: dict, action: str, target: str | None = None) -> dict:
    return next(row for row in report["operations"]
                if row["action"] == action and row["target"] == target)


def _target(report: dict, kind: str, name: str) -> dict:
    return next(row for row in report["targets"]
                if row["kind"] == kind and row["name"] == name)


def _forbid_mutation(monkeypatch) -> None:
    def forbidden(*_args, **_kwargs):
        raise AssertionError("readiness attempted a mutating or resolving call")

    monkeypatch.setattr(actions, "preview", forbidden)
    monkeypatch.setattr(actions, "apply", forbidden)
    monkeypatch.setattr(runtime.ServiceManager, "start", forbidden)
    monkeypatch.setattr(runtime.ServiceManager, "run", forbidden)
    monkeypatch.setattr(runtime.ServiceManager, "stop", forbidden)
    monkeypatch.setattr(runtime.ServiceManager, "restart", forbidden)
    monkeypatch.setattr(runtime.TunnelManager, "start", forbidden)
    monkeypatch.setattr(secrets.SecretBroker, "resolve", forbidden)
    monkeypatch.setattr(secrets.SecretBroker, "resolve_env", forbidden)
    monkeypatch.setattr(secrets.SecretBroker, "check", forbidden)
    monkeypatch.setattr(secrets.SecretValue, "reveal", forbidden)


def test_configured_background_service_is_available_and_writes_nothing(
        tmp_path, monkeypatch):
    _write_config(tmp_path)
    _forbid_mutation(monkeypatch)
    before = _snapshot(tmp_path)

    report = A.readiness(tmp_path, selections=[
        {"action": "service.status", "name": "api"},
        {"action": "service.start", "name": "api"},
        {"action": "service.logs", "name": "api"},
    ], observed_at=1_700_000_000_000)

    assert report["schema"] == {"namespace": "ozagia.action_readiness",
                                "version": "1.0"}
    assert report["state"] == "available"
    assert report["readiness"] == "available"
    assert report["selection"] == "explicit"
    assert report["observed_at"] == 1_700_000_000_000
    assert report["provenance"]["secrets_resolved"] is False
    assert report["provenance"]["previewed"] is False
    assert report["provenance"]["applied"] is False
    assert _operation(report, "service.start", "api")["readiness"] == "available"
    assert _operation(report, "service.status", "api")["readiness"] == "available"
    assert _operation(report, "service.logs", "api")["readiness"] == "available"
    target = _target(report, "service", "api")
    assert target["configured"] is True
    assert target["mode"] == "background"
    assert target["process"]["state"] == "configured"
    assert target["process"]["verified"] is False
    assert target["logs"]["state"] == "not_recorded"
    assert target["logs"]["content_read"] is False
    assert target["environment"]["state"] == "none"
    assert report["counts"]["operations"]["available"] == 3

    assert _snapshot(tmp_path) == before
    assert not state_path(tmp_path, "actions").exists()
    assert not state_path(tmp_path, "runtime").exists()


def test_unconfigured_service_is_absent_and_never_available(tmp_path, monkeypatch):
    _write_config(tmp_path)
    _forbid_mutation(monkeypatch)
    report = A.readiness(tmp_path, selections=[
        {"action": "service.start", "name": "missing"}])
    assert report["state"] == "absent"
    row = _operation(report, "service.start", "missing")
    assert row["readiness"] == "absent"
    assert "not configured" in row["reason"]
    target = _target(report, "service", "missing")
    assert target["configured"] is False
    assert target["readiness"] == "absent"
    assert report["counts"]["operations"]["absent"] == 1
    assert not state_path(tmp_path, "actions").exists()
    assert not state_path(tmp_path, "runtime").exists()


def test_live_gated_tunnel_is_blocked_and_reports_its_gates(
        tmp_path, monkeypatch):
    _write_config(tmp_path)
    _forbid_mutation(monkeypatch)
    report = A.readiness(tmp_path, selections=[
        {"action": "tunnel.start", "name": "database"}])
    row = _operation(report, "tunnel.start", "database")
    assert row["readiness"] == "blocked"
    assert "live-gated" in row["reason"]
    assert row["live_gated"] is True
    assert row["mutating"] is True
    assert row["gates"] == {"confirmation": True, "live": True,
                            "network": True}
    assert any("--allow-live" in item for item in row["requirements"])
    assert report["state"] == "blocked"
    target = _target(report, "tunnel", "database")
    assert target["readiness"] == "blocked"
    assert target["relay"]["configured"] is True
    assert target["lease"]["state"] == "configured"
    assert target["lease"]["held"] is False
    assert not state_path(tmp_path, "actions").exists()
    assert not state_path(tmp_path, "runtime").exists()


def test_stale_process_identity_is_blocked_and_readiness_does_not_reconcile(
        tmp_path, monkeypatch):
    _write_config(tmp_path)
    _forbid_mutation(monkeypatch)
    directory = state_path(tmp_path, "runtime")
    directory.mkdir(parents=True)
    record_id = _runtime_id("service", "api")
    state = {
        "version": 1,
        "records": {
            record_id: {
                "id": record_id, "kind": "service", "name": "api",
                "state": "running", "pid": 999_999,
                "process_group": 999_999, "process_start": "bogus",
            },
        },
    }
    (directory / "state.json").write_text(json.dumps(state), encoding="utf-8")
    before = _snapshot(tmp_path)

    report = A.readiness(tmp_path, selections=[
        {"action": "service.stop", "name": "api"},
        {"action": "service.start", "name": "api"},
    ])

    assert _operation(report, "service.stop", "api")["readiness"] == "blocked"
    assert "stale or unverified" in _operation(
        report, "service.stop", "api")["reason"]
    assert _operation(report, "service.start", "api")["readiness"] == "blocked"
    assert report["state"] == "blocked"
    target = _target(report, "service", "api")
    assert target["process"]["state"] == "stale"
    assert target["process"]["verified"] is False
    assert target["readiness"] == "blocked"
    assert _snapshot(tmp_path) == before


def test_unresolved_secret_reference_is_unknown_and_never_resolved(
        tmp_path, monkeypatch):
    _write_config(tmp_path,
                  service_env=f"secret://env/{SECRET_ENV}")
    monkeypatch.setenv(SECRET_ENV, CANARY)
    _forbid_mutation(monkeypatch)

    report = A.readiness(tmp_path, selections=[
        {"action": "service.start", "name": "api"}])

    row = _operation(report, "service.start", "api")
    assert row["readiness"] == "unknown"
    assert "not resolved" in row["reason"] or "never resolves" in row["reason"]
    target = _target(report, "service", "api")
    assert target["readiness"] == "unknown"
    assert target["readiness"] != "available"
    assert target["environment"]["state"] == "unresolved"
    assert target["environment"]["keys"] == ["API_TOKEN"]
    assert target["environment"]["references"] == [{
        "key": "API_TOKEN", "reference": f"secret://env/{SECRET_ENV}"}]
    assert target["environment"]["unresolved"] == 1
    assert target["environment"]["resolved"] is False
    assert CANARY not in json.dumps(report, sort_keys=True)
    assert report["state"] == "unknown"
    assert report["provenance"]["secrets_resolved"] is False
    assert not state_path(tmp_path, "actions").exists()
    assert not state_path(tmp_path, "runtime").exists()


def test_session_secret_reference_blocks_a_runtime_service(
        tmp_path, monkeypatch):
    _write_config(tmp_path, service_env="secret://session/opaquetoken123456")
    _forbid_mutation(monkeypatch)
    report = A.readiness(tmp_path, selections=[
        {"action": "service.run", "name": "api"}])
    row = _operation(report, "service.run", "api")
    assert row["readiness"] == "blocked"
    assert "session secret reference" in row["reason"]
    target = _target(report, "service", "api")
    assert target["environment"]["state"] == "session_reference"
    assert target["environment"]["session_references"] == [
        "secret://session/opaquetoken123456"]
    assert target["readiness"] == "blocked"


def test_foreground_service_blocks_start_but_allows_run(tmp_path, monkeypatch):
    _write_config(tmp_path, service_mode="foreground")
    _forbid_mutation(monkeypatch)
    report = A.readiness(tmp_path, selections=[
        {"action": "service.start", "name": "migrate"},
        {"action": "service.run", "name": "migrate"},
    ])
    assert _operation(report, "service.start", "migrate")["readiness"] == "blocked"
    assert "background" in _operation(
        report, "service.start", "migrate")["reason"]
    assert _operation(report, "service.run", "migrate")["readiness"] == "available"
    assert report["state"] == "blocked"


def test_relay_backed_service_lifecycle_is_blocked(tmp_path, monkeypatch):
    _write_config(tmp_path, service_relay="local")
    _forbid_mutation(monkeypatch)
    report = A.readiness(tmp_path, selections=[
        {"action": "service.start", "name": "api"},
        {"action": "service.run", "name": "api"},
        {"action": "service.restart", "name": "api"},
    ])
    for action in ("service.start", "service.run", "service.restart"):
        row = _operation(report, action, "api")
        assert row["readiness"] == "blocked"
        assert "relay-backed" in row["reason"]
    target = _target(report, "service", "api")
    assert target["relay"] == "local"
    assert report["state"] == "blocked"


def test_log_metadata_is_reported_without_reading_content(
        tmp_path, monkeypatch):
    _write_config(tmp_path)
    _forbid_mutation(monkeypatch)
    directory = state_path(tmp_path, "runtime")
    store = runtime.RuntimeStore(directory)
    record_id = _runtime_id("service", "api")
    store.put({
        "id": record_id, "kind": "service", "name": "api",
        "state": "stopped", "mode": "background",
    })
    store.append_log(record_id, f"raw log line {CANARY}\n".encode("utf-8"))
    before = _snapshot(tmp_path)

    report = A.readiness(tmp_path, selections=[
        {"action": "service.logs", "name": "api"}])

    target = _target(report, "service", "api")
    assert target["logs"]["state"] == "available"
    assert target["logs"]["bytes"] > 0
    assert target["logs"]["content_read"] is False
    assert target["logs"]["path"].endswith(f"{record_id}.log")
    assert CANARY not in json.dumps(report, sort_keys=True)
    assert _snapshot(tmp_path) == before


def test_all_configured_view_lists_declared_operations(tmp_path, monkeypatch):
    _write_config(tmp_path)
    _forbid_mutation(monkeypatch)
    report = A.readiness(tmp_path)
    actions_seen = {(row["action"], row["target"])
                    for row in report["operations"]}
    assert ("service.list", None) in actions_seen
    assert ("service.start", "api") in actions_seen
    assert ("service.run", "migrate") in actions_seen
    assert ("tunnel.list", None) in actions_seen
    assert ("tunnel.start", "database") in actions_seen
    assert _operation(report, "service.list")["readiness"] == "available"
    assert _operation(report, "tunnel.start", "database")["readiness"] == "blocked"
    # All-target view is conservative: mode/no-op blockers are explicit.
    assert report["state"] in {"blocked", "unknown"}


def test_selection_validation(tmp_path):
    _write_config(tmp_path)
    with pytest.raises(A.ActionReadinessError, match="service/tunnel"):
        A.readiness(tmp_path, selections=[
            {"action": "integration.status"}])
    with pytest.raises(A.ActionReadinessError, match="requires a bounded"):
        A.readiness(tmp_path, selections=[{"action": "service.start"}])
    with pytest.raises(A.ActionReadinessError, match="does not take"):
        A.readiness(tmp_path, selections=[
            {"action": "service.list", "name": "api"}])
    with pytest.raises(A.ActionReadinessError, match="unsupported"):
        A.readiness(tmp_path, selections=[
            {"action": "service.start", "name": "api", "force": True}])
    with pytest.raises(A.ActionReadinessError, match="duplicate"):
        A.readiness(tmp_path, selections=[
            {"action": "service.start", "name": "api"},
            {"action": "service.start", "name": "api"},
        ])
    with pytest.raises(A.ActionReadinessError, match="at most"):
        A.readiness(tmp_path, selections=[
            {"action": "service.status", "name": f"s{index}"}
            for index in range(A.MAX_SELECTIONS + 1)
        ])


def test_empty_selection_is_explicitly_unknown(tmp_path):
    _write_config(tmp_path)
    report = A.readiness(tmp_path, selections=[])
    assert report["selection"] == "explicit"
    assert report["state"] == "unknown"
    assert report["operations"] == []
    assert report["targets"] == []
    assert report["reason"] == "no service or tunnel action was explicitly selected"
    assert not state_path(tmp_path, "actions").exists()


def test_unreadable_configuration_is_unknown_not_absent(tmp_path):
    (tmp_path / ".ozagia.toml").write_text(
        "[runtime.services.api\ncommand = [\n", encoding="utf-8")
    report = A.readiness(tmp_path, selections=[
        {"action": "service.start", "name": "api"}])
    assert report["state"] == "unknown"
    assert report["error"] == "config_unreadable"
    assert _operation(report, "service.start", "api")["readiness"] == "unknown"
    assert _target(report, "service", "api")["readiness"] == "unknown"
    assert not state_path(tmp_path, "actions").exists()
    assert not state_path(tmp_path, "runtime").exists()


def test_unreadable_runtime_state_is_unknown_and_never_starts_anything(
        tmp_path, monkeypatch):
    _write_config(tmp_path)
    _forbid_mutation(monkeypatch)
    directory = state_path(tmp_path, "runtime")
    directory.mkdir(parents=True)
    (directory / "state.json").write_text("{not json", encoding="utf-8")
    before = _snapshot(tmp_path)
    report = A.readiness(tmp_path, selections=[
        {"action": "service.status", "name": "api"}])
    assert report["state"] == "unknown"
    assert report["error"] == "runtime_unreadable"
    assert _operation(report, "service.status", "api")["readiness"] == "unknown"
    assert _target(report, "service", "api")["process"]["error"] == "runtime_unreadable"
    assert _snapshot(tmp_path) == before
    assert not state_path(tmp_path, "actions").exists()
