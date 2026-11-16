"""Preview/apply tests for the process and connection action layer.

Everything here uses fake managers, fake daemon clients and fake relays. No
real service, tunnel, daemon or network operation is started.
"""
from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from ozagia.actions_cli import main as cli_main
from ozagia.core import actions
from ozagia.core.actions import (
    ActionContext,
    ActionError,
    ActionRefused,
    apply,
    preview,
)
from ozagia.daemon.client import DaemonUnavailable

SECRET = "hunter2-hidden-value"


def _write_config(root: Path, command: list[str] | None = None) -> None:
    argv = command or ["python", "-m", "app", "--flag", SECRET]
    quoted = ", ".join(json.dumps(part) for part in argv)
    (root / ".ozagia.toml").write_text(
        f"""
[relays.local]
specs = [{{ kind = "local" }}]
allowed_kinds = ["local"]
max_timeout_s = 20

[runtime.services.api]
command = [{quoted}]
workdir = "services/api"
description = "API service"

[runtime.services.migrate]
command = ["python", "foreground"]
mode = "foreground"
description = "Database migration"

[runtime.tunnels.database]
relay = "local"
target = "db.example.test:5432"
local_port = 0
description = "Payments database"
""",
        encoding="utf-8",
    )
    (root / "services" / "api").mkdir(parents=True, exist_ok=True)


def _apply_previewed(root: Path, action: str, context: ActionContext,
                     **kwargs: Any):
    request = preview(root, action, context=context, **kwargs)
    assert request.confirmation
    return apply(root, action, request.confirmation, context=context, **kwargs)


class FakeManager:
    def __init__(self, state: str = "configured"):
        self.state = state
        self.calls: list[tuple[str, Any]] = []
        self.logs_text = "bounded log\n"

    def _row(self, name: str) -> dict[str, Any]:
        return {
            "name": name, "kind": "service", "state": self.state,
            "configured": True, "running": self.state == "running",
            "pid": 4242 if self.state == "running" else None,
        }

    def status(self, name: str) -> dict[str, Any]:
        self.calls.append(("status", name))
        return self._row(name)

    def list(self) -> list[dict[str, Any]]:
        self.calls.append(("list", None))
        return [self._row("api")]

    def start(self, name: str) -> dict[str, Any]:
        self.calls.append(("start", name))
        self.state = "running"
        return self._row(name)

    def run(self, name: str) -> dict[str, Any]:
        self.calls.append(("run", name))
        self.state = "exited"
        row = self._row(name)
        row["output"] = "foreground output"
        return row

    def stop(self, name: str) -> dict[str, Any]:
        self.calls.append(("stop", name))
        self.state = "stopped"
        return self._row(name)

    def restart(self, name: str) -> dict[str, Any]:
        self.calls.append(("restart", name))
        self.state = "running"
        return self._row(name)

    def logs(self, name: str, *, max_bytes: int = 4_096) -> str:
        self.calls.append(("logs", (name, max_bytes)))
        return self.logs_text


class FakeTunnelManager:
    def __init__(self, state: str = "configured"):
        self.state = state
        self.calls: list[tuple[str, Any]] = []

    def status(self, name: str) -> dict[str, Any]:
        self.calls.append(("status", name))
        return {"name": name, "kind": "tunnel", "state": self.state,
                "configured": True, "running": self.state == "running"}

    def list(self) -> list[dict[str, Any]]:
        self.calls.append(("list", None))
        return [self.status("database")]

    def start(self, name: str, *, allow_live: bool,
              allow_network: bool) -> dict[str, Any]:
        self.calls.append(("start", (name, allow_live, allow_network)))
        self.state = "running"
        return {"name": name, "kind": "tunnel", "state": "running",
                "configured": True, "running": True, "local_port": 43123}


class FakeLease:
    local_host = "127.0.0.1"
    local_port = 43123
    dry_run = True

    def __init__(self, argv: tuple[str, ...]):
        self.planned_argv = argv
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakeRelay:
    def __init__(self, argv: tuple[str, ...]):
        self.argv = argv
        self.leases: list[FakeLease] = []
        self.closed = False

    def open_tunnel(self, target: Any, **kwargs: Any) -> FakeLease:
        assert kwargs.get("dry_run") is True
        assert target.host == "db.example.test"
        lease = FakeLease(self.argv)
        self.leases.append(lease)
        return lease

    def close(self) -> None:
        self.closed = True


class DaemonState:
    def __init__(self, state: str = "absent"):
        self.state = state
        self.pings = 0
        self.shutdowns = 0


class FakeDaemonClient:
    def __init__(self, state: DaemonState):
        self.state = state

    def __enter__(self) -> "FakeDaemonClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def ping(self, *, timeout: float | None = None) -> dict[str, Any]:
        self.state.pings += 1
        if self.state.state == "absent":
            raise DaemonUnavailable("no daemon")
        return {"ok": True, "pid": 9090, "version": "test", "root": "/repo"}

    def shutdown(self) -> dict[str, Any]:
        self.state.shutdowns += 1
        self.state.state = "absent"
        return {"ok": True}


def _daemon_context(state: DaemonState) -> tuple[ActionContext, list[dict[str, Any]]]:
    ensured: list[dict[str, Any]] = []

    def factory(sock: str, *, timeout: float) -> FakeDaemonClient:
        assert sock
        assert timeout > 0
        return FakeDaemonClient(state)

    def ensure(root: str, **kwargs: Any) -> dict[str, Any]:
        ensured.append({"root": root, **kwargs})
        state.state = "running"
        return {"ok": True, "started": True, "sock": "/tmp/fake.sock", "pid": 9191}

    return ActionContext(daemon_client_factory=factory, daemon_ensure=ensure), ensured


def test_preview_is_read_only_and_returns_exact_operation_and_token(tmp_path):
    _write_config(tmp_path)
    ctx = ActionContext(
        manager_factory=lambda *_a, **_k: pytest.fail("preview must not build a manager"))

    result = preview(tmp_path, "service.start", context=ctx, name="api")

    assert result.mode == "preview"
    assert result.state == "preview"
    assert result.ok is True
    assert result.applicable is True
    assert result.current_state == "configured"
    assert result.argv == ("python", "-m", "app", "--flag", SECRET)
    assert "ServiceManager.start('api')" in (result.manager_call or "")
    assert result.confirmation and len(result.confirmation) >= 20
    assert not (tmp_path / ".ozagia" / "runtime").exists()
    assert not (tmp_path / ".ozagia" / "actions" / "audit.jsonl").exists()
    token_file = (tmp_path / ".ozagia" / "actions" / "pending"
                  / f"{result.confirmation}.json")
    assert token_file.is_file()
    assert token_file.stat().st_mode & 0o777 == 0o600


def test_apply_requires_a_one_time_token_and_refuses_bad_or_expired_tokens(tmp_path):
    _write_config(tmp_path)
    manager = FakeManager()
    ctx = ActionContext(manager_factory=lambda *_a, **_k: manager)

    with pytest.raises(ActionError, match="required"):
        apply(tmp_path, "service.status", "", context=ctx, name="api")
    with pytest.raises(ActionRefused, match="malformed"):
        apply(tmp_path, "service.status", "not a token", context=ctx, name="api")
    with pytest.raises(ActionRefused, match="unknown"):
        apply(tmp_path, "service.status", "a" * 24, context=ctx, name="api")

    request = preview(tmp_path, "service.status", context=ctx, name="api")
    with pytest.raises(ActionRefused, match="different arguments"):
        apply(tmp_path, "service.status", request.confirmation, context=ctx,
              name="migrate")

    first = apply(tmp_path, "service.status", request.confirmation, context=ctx,
                  name="api")
    assert first.ok is True and first.state == "applied"
    with pytest.raises(ActionRefused, match="already used"):
        apply(tmp_path, "service.status", request.confirmation, context=ctx,
              name="api")

    old = ActionContext(manager_factory=lambda *_a, **_k: manager,
                        clock=lambda: datetime(2000, 1, 1, tzinfo=UTC))
    expired = preview(tmp_path, "service.status", context=old, name="api")
    with pytest.raises(ActionRefused, match="expired"):
        apply(tmp_path, "service.status", expired.confirmation, context=ctx,
              name="api")
    assert not (tmp_path / ".ozagia" / "actions" / "pending"
                / f"{expired.confirmation}.json").exists()


def test_service_lifecycle_uses_the_fake_manager_and_missing_is_absent(tmp_path):
    _write_config(tmp_path)
    for action, expected_call in (
        ("service.start", "start"),
        ("service.run", "run"),
        ("service.stop", "stop"),
        ("service.restart", "restart"),
    ):
        fake = FakeManager(state="running" if action in {"service.stop", "service.restart"}
                           else "configured")
        ctx = ActionContext(manager_factory=lambda *_a, _fake=fake, **_k: _fake)
        request = preview(tmp_path, action, context=ctx, name="api")
        assert request.confirmation
        result = apply(tmp_path, action, request.confirmation, context=ctx,
                       name="api")
        assert result.ok is True, (action, result)
        assert expected_call in [call[0] for call in fake.calls]
        if action == "service.run":
            assert result.result["output"] == "foreground output"

    fake = FakeManager()
    ctx = ActionContext(manager_factory=lambda *_a, **_k: fake)
    request = preview(tmp_path, "service.start", context=ctx, name="missing")
    assert request.current_state == "absent"
    assert request.applicable is False
    result = apply(tmp_path, "service.start", request.confirmation, context=ctx,
                   name="missing")
    assert result.ok is False and result.state == "absent"
    assert fake.calls == []

    stopped = _apply_previewed(tmp_path, "service.stop", ctx, name="missing")
    assert stopped.ok is True and stopped.state == "absent"

    logs = _apply_previewed(tmp_path, "service.logs", ctx, name="api", max_bytes=12)
    assert logs.result == {"name": "api", "max_bytes": 12, "logs": "bounded log\n"}


def test_service_stale_or_unknown_is_refused_without_signalling(tmp_path):
    _write_config(tmp_path)
    fake = FakeManager(state="stale")
    ctx = ActionContext(manager_factory=lambda *_a, **_k: fake)
    result = _apply_previewed(tmp_path, "service.stop", ctx, name="api")
    assert result.ok is False and result.state == "refused"
    assert ("stop", "api") not in fake.calls


def test_daemon_absent_running_and_restart_cases_use_the_fake_client(tmp_path):
    state = DaemonState("absent")
    ctx, ensured = _daemon_context(state)

    status = preview(tmp_path, "daemon.status", context=ctx)
    assert status.current_state == "absent" and status.ok is True
    applied_status = apply(tmp_path, "daemon.status", status.confirmation, context=ctx)
    assert applied_status.ok is True and applied_status.current_state == "absent"

    ensure_preview = preview(tmp_path, "daemon.ensure", context=ctx)
    assert ensure_preview.applicable is True and ensure_preview.current_state == "absent"
    ensured_result = apply(tmp_path, "daemon.ensure", ensure_preview.confirmation,
                           context=ctx)
    assert ensured_result.ok is True and ensured_result.current_state == "running"
    assert len(ensured) == 1

    again = _apply_previewed(tmp_path, "daemon.ensure", ctx)
    assert again.ok is True and again.state == "applied"
    assert again.current_state == "running"
    assert len(ensured) == 1

    stop_preview = preview(tmp_path, "daemon.stop", context=ctx)
    assert stop_preview.applicable is True
    stopped = apply(tmp_path, "daemon.stop", stop_preview.confirmation, context=ctx)
    assert stopped.ok is True and state.shutdowns == 1

    absent_stop = _apply_previewed(tmp_path, "daemon.stop", ctx)
    assert absent_stop.ok is True and absent_stop.state == "absent"
    assert state.shutdowns == 1

    state.state = "running"
    restart = _apply_previewed(tmp_path, "daemon.restart", ctx)
    assert restart.ok is True and state.shutdowns == 2
    assert len(ensured) == 2


def test_tunnel_start_previews_the_gated_command_and_apply_requires_both_gates(tmp_path):
    _write_config(tmp_path)
    state = DaemonState("absent")
    daemon_ctx, _ensured = _daemon_context(state)
    fake_tunnel = FakeTunnelManager()
    relays: list[FakeRelay] = []

    def relay_factory(_config: Any, _policy: Any) -> FakeRelay:
        relay = FakeRelay(("ssh", "-N", "-L", "127.0.0.1:0:db.example.test:5432"))
        relays.append(relay)
        return relay

    ctx = ActionContext(
        daemon_client_factory=daemon_ctx.daemon_client_factory,
        tunnel_manager_factory=lambda *_a, **_k: fake_tunnel,
        relay_factory=relay_factory,
    )

    unapproved = preview(tmp_path, "tunnel.start", context=ctx, name="database")
    assert unapproved.gated is True and unapproved.live is True
    assert unapproved.applicable is False
    assert unapproved.argv == ("ssh", "-N", "-L", "127.0.0.1:0:db.example.test:5432")
    refused = apply(tmp_path, "tunnel.start", unapproved.confirmation,
                    context=ctx, name="database")
    assert refused.ok is False and refused.state == "refused"
    assert "allow-live" in (refused.error or "")
    assert fake_tunnel.calls == []

    approved = preview(tmp_path, "tunnel.start", context=ctx, name="database",
                       live=True, network=True)
    assert approved.applicable is True
    assert approved.argv == ("ssh", "-N", "-L", "127.0.0.1:0:db.example.test:5432")
    assert relays and relays[0].leases[0].closed and relays[0].closed
    started = apply(tmp_path, "tunnel.start", approved.confirmation, context=ctx,
                    name="database", live=True, network=True)
    assert started.ok is True and started.current_state == "running"
    assert fake_tunnel.calls == [("status", "database"),
                                 ("start", ("database", True, True))]


def test_audit_is_bounded_and_never_stores_argv_or_secrets(tmp_path, monkeypatch):
    _write_config(tmp_path)
    fake = FakeManager()
    ctx = ActionContext(manager_factory=lambda *_a, **_k: fake)

    result = _apply_previewed(tmp_path, "service.start", ctx, name="api")
    assert result.ok is True
    audit = tmp_path / ".ozagia" / "actions" / "audit.jsonl"
    assert audit.is_file()
    text = audit.read_text(encoding="utf-8")
    assert SECRET not in text
    assert "python" not in text
    assert "argv" not in text
    record = json.loads(text.splitlines()[-1])
    assert record["action"] == "service.start"
    assert record["result"]["state"] == "applied"
    assert record["result"]["ok"] is True
    assert record["target"] == "api"
    assert audit.stat().st_mode & 0o777 == 0o600

    monkeypatch.setattr(actions, "MAX_AUDIT_RECORDS", 2)
    for _ in range(4):
        _apply_previewed(tmp_path, "integration.status", ctx, name="opencode")
    lines = audit.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert all("argv" not in line for line in lines)


def test_unsupported_action_and_invalid_arguments_are_validation_errors(tmp_path):
    _write_config(tmp_path)
    with pytest.raises(ActionError, match="unsupported action"):
        preview(tmp_path, "service.kill", name="api")
    with pytest.raises(ActionError, match="unsupported argument"):
        preview(tmp_path, "service.start", name="api", force=True)
    with pytest.raises(ActionError, match="name is required"):
        preview(tmp_path, "service.status")
    with pytest.raises(ActionError, match="live"):
        preview(tmp_path, "tunnel.start", name="database", live="yes")


def test_integration_status_is_read_only_and_reports_paths(tmp_path):
    _write_config(tmp_path)
    result = preview(tmp_path, "integration.status", name="opencode")
    row = result.current["integrations"][0]
    assert row["name"] == "opencode"
    assert row["state"] == "absent"
    assert row["components"][0]["path"].startswith(str(tmp_path))
    assert not (tmp_path / ".opencode").exists()

    server = tmp_path / ".opencode" / "plugins" / "ozagia.js"
    brand = tmp_path / ".opencode" / "plugins" / "ozagia" / "tui.js"
    server.parent.mkdir(parents=True)
    brand.parent.mkdir(parents=True)
    server.write_text("// server\n", encoding="utf-8")
    brand.write_text("// brand\n", encoding="utf-8")
    applied = _apply_previewed(tmp_path, "integration.status", ActionContext(),
                               name="opencode")
    assert applied.ok is True
    assert applied.result["integrations"][0]["state"] == "installed"


def test_cli_exit_codes_and_json_preview(tmp_path, capsys):
    _write_config(tmp_path)
    assert cli_main(["list", "--json"]) == 0
    listing = json.loads(capsys.readouterr().out)
    names = {row["name"] for row in listing}
    assert {"daemon.ensure", "service.start", "tunnel.start",
            "integration.status"} <= names
    tunnel = next(row for row in listing if row["name"] == "tunnel.start")
    assert tunnel["live_gated"] is True and tunnel["mutating"] is True

    assert cli_main(["preview", "service.start", "--root", str(tmp_path),
                     "--name", "api", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["action"] == "service.start"
    assert payload["confirmation"]

    assert cli_main(["preview", "service.kill", "--root", str(tmp_path)]) == 2
    assert cli_main(["apply", "service.start", "--root", str(tmp_path),
                     "--name", "api", "--confirm", "bad"]) == 1


def test_no_real_process_or_network_was_created(tmp_path):
    _write_config(tmp_path)
    fake = FakeManager()
    ctx = ActionContext(manager_factory=lambda *_a, **_k: fake)
    result = _apply_previewed(tmp_path, "service.start", ctx, name="api")
    assert result.ok is True
    assert fake.calls == [("status", "api"), ("start", "api")]
    assert not (tmp_path / ".ozagia" / "runtime").exists()
    assert os.environ.get("OZAGIA_STARTED_REAL_SERVICE") is None
