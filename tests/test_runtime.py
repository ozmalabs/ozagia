"""Fake-backed tests for local runtime ownership and tunnel state."""
from __future__ import annotations

import io
import json
import os
import signal
import subprocess
from pathlib import Path
from typing import Any

import pytest

from ozagia.config import load_project_config
from ozagia.core.runtime import (
    RuntimeErrorBase,
    RuntimeStore,
    ServiceManager,
    TunnelManager,
    runtime_context,
    runtime_logs,
    runtime_status,
)
from ozagia.core.secrets import SecretBroker, SessionSecretStore


def _write_config(root: Path, text: str) -> None:
    (root / ".ozagia.toml").write_text(text, encoding="utf-8")


class FakeProcess:
    def __init__(self, pid: int, *, output: bytes = b"", auto_exit: bool = False):
        self.pid = pid
        self.stdout = io.BytesIO(output)
        self.active = True
        self.auto_exit = auto_exit
        self.returncode = 0

    def poll(self) -> int | None:
        return None if self.active else self.returncode

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        if self.auto_exit:
            self.active = False
        if self.active:
            raise subprocess.TimeoutExpired("fake", 0)
        return self.returncode


class FakeProcesses:
    def __init__(self):
        self.created: list[FakeProcess] = []
        self.calls: list[dict[str, Any]] = []
        self.next_pid = 10001

    def factory(self, argv: list[str], **kwargs: Any) -> FakeProcess:
        assert kwargs["shell"] is False
        assert kwargs["start_new_session"] is True
        self.calls.append({"argv": argv, "kwargs": kwargs})
        auto_exit = any(arg == "foreground" for arg in argv)
        output = b"bounded output" if auto_exit else b"background log line\n"
        process = FakeProcess(self.next_pid, output=output, auto_exit=auto_exit)
        self.next_pid += 1
        self.created.append(process)
        return process

    def identity(self, pid: int) -> tuple[str | None, int | None]:
        for process in self.created:
            if process.pid == pid and process.active:
                return f"start-{pid}", pid
        return None, None

    def signal_group(self, group: int, sig: int) -> None:
        assert sig in {signal.SIGTERM, signal.SIGKILL}
        for process in self.created:
            if process.pid == group:
                process.active = False


def _config(root: Path):
    _write_config(root, """
[relays.local]
specs = [{ kind = "local" }]
allowed_kinds = ["local"]
max_timeout_s = 20

[runtime.services.api]
command = ["python", "-m", "app"]
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
""")
    (root / "services" / "api").mkdir(parents=True)
    return load_project_config(root)


def test_store_is_versioned_private_bounded_and_omits_execution_data(tmp_path):
    store = RuntimeStore(tmp_path / "runtime", max_log_bytes=8)
    record_id = "a" * 24
    record = {
        "id": record_id, "kind": "service", "name": "api", "state": "running",
        "mode": "background", "pid": 123, "process_group": 123,
        "process_start": "42", "exit_code": None, "exit_signal": None,
        "started_at": "now", "ended_at": None, "updated_at": "now",
        "config_fingerprint": "a" * 64, "command_fingerprint": "b" * 64,
        "relay": None, "target": None, "local_host": None, "local_port": None,
        "lease_token": None, "reason": None,
    }
    store.put(record)
    store.append_log(record_id, b"123456")
    store.append_log(record_id, b"7890")
    assert store.logs(record_id) == "34567890"
    assert (tmp_path / "runtime" / "state.json").stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "runtime" / "logs" / f"{record_id}.log").stat().st_mode & 0o777 == 0o600
    data = json.loads((tmp_path / "runtime" / "state.json").read_text())
    assert data["version"] == 1
    assert "argv" not in repr(data) and "python" not in repr(data)
    with pytest.raises(Exception, match="invalid record"):
        store.put({**record, "argv": ["private command"]})


def test_background_start_logs_stop_and_restart_use_verified_fake_group(tmp_path):
    config = _config(tmp_path)
    processes = FakeProcesses()
    manager = ServiceManager(
        config, state_dir=tmp_path / "state",
        process_factory=processes.factory, identity_reader=processes.identity,
        signal_group=processes.signal_group, stop_grace_s=0,
    )

    started = manager.start("api")
    assert started["state"] == "running"
    assert started["pid"] == 10001
    assert processes.calls[0]["kwargs"]["cwd"] == str(tmp_path / "services" / "api")
    reader = manager._reader_threads[started["id"]]
    reader.join(timeout=1)
    assert manager.logs("api", max_bytes=64) == "background log line\n"

    state_text = (tmp_path / "state" / "state.json").read_text()
    assert "python" not in state_text and "app" not in state_text
    assert json.loads(state_text)["records"][started["id"]]["command_fingerprint"]

    stopped = manager.stop("api")
    assert stopped["state"] == "stopped"
    restarted = manager.restart("api")
    assert restarted["state"] == "running"
    assert restarted["pid"] == 10002
    assert len(processes.calls) == 2


def test_foreground_run_returns_bounded_output_and_terminal_state(tmp_path):
    config = _config(tmp_path)
    processes = FakeProcesses()
    manager = ServiceManager(
        config, state_dir=tmp_path / "state", process_factory=processes.factory,
        identity_reader=processes.identity, output_limit=5,
    )
    result = manager.run("migrate")
    assert result["state"] == "exited"
    assert result["exit_code"] == 0
    assert result["mode"] == "foreground"
    assert result["output"] == "bound"
    saved = json.loads((tmp_path / "state" / "state.json").read_text())
    assert "bounded output" not in json.dumps(saved)
    assert "foreground" not in saved["records"][result["id"]]["command_fingerprint"]


def test_restarted_manager_marks_unverifiable_process_stale_and_never_signals(tmp_path):
    config = _config(tmp_path)
    processes = FakeProcesses()
    first = ServiceManager(
        config, state_dir=tmp_path / "state", process_factory=processes.factory,
        identity_reader=processes.identity, signal_group=processes.signal_group,
    )
    first.start("api")
    signalled: list[tuple[int, int]] = []
    second = ServiceManager(
        config, state_dir=tmp_path / "state",
        identity_reader=lambda _pid: (None, None),
        signal_group=lambda group, sig: signalled.append((group, sig)),
    )
    status = second.status("api")
    assert status["state"] == "stale"
    with pytest.raises(RuntimeErrorBase, match="unverified"):
        second.stop("api")
    assert signalled == []
    assert processes.created[0].active


def test_raw_apis_are_explicit_and_do_not_persist_argv(tmp_path):
    config = _config(tmp_path)
    processes = FakeProcesses()
    manager = ServiceManager(
        config, state_dir=tmp_path / "state", process_factory=processes.factory,
        identity_reader=processes.identity,
    )
    result = manager.run_raw(["python", "foreground"])
    assert result["state"] == "exited"
    assert result["configured"] is False
    state = (tmp_path / "state" / "state.json").read_text()
    assert "python" not in state


class FakeLease:
    local_host = "127.0.0.1"
    local_port = 43123

    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class FakeRelay:
    def __init__(self):
        self.lease = FakeLease()
        self.closed = False
        self.calls: list[dict[str, Any]] = []

    def open_tunnel(self, target, **kwargs):
        self.calls.append({"target": target, **kwargs})
        return self.lease

    def close(self):
        self.closed = True


def test_tunnels_distinguish_configured_running_and_stale(tmp_path):
    config = _config(tmp_path)
    relays: list[FakeRelay] = []
    policies = []

    def relay_factory(relay_config, policy):
        assert relay_config.name == "local"
        policies.append(policy)
        relay = FakeRelay()
        relays.append(relay)
        return relay

    first = TunnelManager(
        config, state_dir=tmp_path / "state", relay_factory=relay_factory)
    configured = first.status("database")
    assert configured["state"] == "configured"
    assert configured["running"] is False
    with pytest.raises(RuntimeErrorBase, match="explicit"):
        first.start("database")

    running = first.start("database", allow_live=True, allow_network=True)
    assert running["state"] == "running"
    assert running["local_port"] == 43123
    assert policies[0].allowed_kinds == frozenset({"local"})
    assert policies[0].allow_live and policies[0].allow_network
    assert relays[0].calls[0]["target"].host == "db.example.test"

    second = TunnelManager(
        config, state_dir=tmp_path / "state", relay_factory=relay_factory)
    assert second.status("database")["state"] == "stale"
    with pytest.raises(RuntimeErrorBase, match="unverified"):
        second.start("database", allow_live=True, allow_network=True)
    with pytest.raises(RuntimeErrorBase, match="unheld"):
        second.stop("database")
    assert relays[0].lease.closed is False

    stopped = first.stop("database")
    assert stopped["state"] == "stopped"
    assert relays[0].lease.closed and relays[0].closed


def test_runtime_context_matches_distinctive_terms_and_is_bounded(tmp_path):
    _config(tmp_path)

    class Memory:
        root = tmp_path

    assert runtime_context(Memory(), "show status")["items"] == []
    result = runtime_context(Memory(), "payments", max_chars=120)
    assert "tunnel database: configured, not connected" in result["context"]
    assert len(result["context"]) <= 120
    assert result["items"] == [{
        "name": "database", "kind": "tunnel", "state": "configured",
        "configured": True, "running": False, "relay": "local",
        "target": "db.example.test:5432", "description": "Payments database",
    }]
    assert "connected" in result["context"]
    assert runtime_context(Memory(), "api service", max_chars=0)["context"] == ""
    api = runtime_context(Memory(), "api service")
    assert api["items"][0]["name"] == "api"
    assert api["items"][0]["state"] == "configured"
    assert "python" not in api["context"]
    assert "app" not in api["context"]


def test_runtime_context_is_read_only_and_never_creates_state(tmp_path):
    config = _config(tmp_path)
    missing = tmp_path / "missing-runtime"
    class Memory:
        root = tmp_path

    assert runtime_context(Memory(), "api", state_dir=missing)["items"]
    assert not missing.exists()

    first_relay = FakeRelay()
    first = TunnelManager(
        config, state_dir=tmp_path / "state",
        relay_factory=lambda _relay, _policy: first_relay)
    first.start("database", allow_live=True, allow_network=True)
    before = (tmp_path / "state" / "state.json").read_bytes()
    result = runtime_context(Memory(), "payments", state_dir=tmp_path / "state")
    assert result["items"][0]["state"] == "stale"
    assert "connected" not in result["context"]
    assert (tmp_path / "state" / "state.json").read_bytes() == before


def test_merged_config_feeds_runtime_managers_and_status(tmp_path):
    _write_config(tmp_path, """
[relays.local]
specs = [{ kind = "local" }]

[runtime.services.api]
command = ["python", "-m", "app"]
""")
    repo = tmp_path / "ozagia" / "config.toml"
    repo.parent.mkdir()
    repo.write_text("""
[runtime.services.worker]
command = ["python", "-m", "worker"]

[runtime.tunnels.database]
relay = "local"
target = "db.example.test:5432"
""", encoding="utf-8")
    config = load_project_config(tmp_path)
    assert [item.name for item in config.runtime_services] == ["api", "worker"]
    assert [item.name for item in config.runtime_tunnels] == ["database"]

    fake = FakeProcesses()
    services = ServiceManager(
        config, state_dir=tmp_path / "service-state",
        process_factory=fake.factory, identity_reader=fake.identity)
    assert [item["name"] for item in services.list()] == ["api", "worker"]

    tunnels = TunnelManager(config, state_dir=tmp_path / "tunnel-state")
    assert [item["name"] for item in tunnels.list()] == ["database"]

    payload = runtime_status(config, state_dir=tmp_path / "missing-state")
    assert [item["name"] for item in payload["services"]] == ["api", "worker"]
    assert [item["name"] for item in payload["tunnels"]] == ["database"]
    assert payload["relays"] == [{"name": "local", "kinds": ["local"]}]


def test_runtime_status_and_logs_are_read_only_and_never_claim_connectivity(
    tmp_path, monkeypatch,
):
    config = _config(tmp_path)
    missing = tmp_path / "missing-runtime"
    payload = runtime_status(config, state_dir=missing)
    assert not missing.exists()
    assert payload["configured"] is True and payload["live"] is False
    assert [item["name"] for item in payload["services"]] == ["api", "migrate"]
    assert payload["processes"] == []
    assert payload["tunnels"][0]["state"] == "configured"
    assert payload["tunnels"][0]["running"] is False
    assert "configured, not connected" in payload["context"]
    assert "connected" not in payload["tunnels"][0]["state"]

    processes = FakeProcesses()
    monkeypatch.setattr("ozagia.core.runtime._process_identity", processes.identity)
    manager = ServiceManager(
        config, state_dir=tmp_path / "state", process_factory=processes.factory,
        identity_reader=processes.identity)
    started = manager.start("api")
    manager._reader_threads[started["id"]].join(timeout=1)
    observed = runtime_status(config, name="api", state_dir=tmp_path / "state")
    assert observed["services"][0]["state"] == "running"
    assert observed["services"][0]["pid"] == started["pid"]
    assert observed["tunnels"] == []

    raw = manager.start_raw(["python", "worker"])
    raw_view = runtime_status(
        config, name=raw["name"], state_dir=tmp_path / "state")
    assert raw_view["processes"][0]["configured"] is False
    assert raw_view["processes"][0]["state"] == "running"
    assert "process " + raw["name"] in raw_view["context"]

    first_relay = FakeRelay()
    tunnels = TunnelManager(
        config, state_dir=tmp_path / "state",
        relay_factory=lambda _relay, _policy: first_relay)
    tunnels.start("database", allow_live=True, allow_network=True)
    tunnel_view = runtime_status(config, name="database", state_dir=tmp_path / "state")
    assert tunnel_view["tunnels"][0]["state"] == "stale"
    assert tunnel_view["tunnels"][0]["running"] is False
    assert "not verified" in tunnel_view["context"]

    logs = runtime_logs(config, "api", state_dir=tmp_path / "state", max_bytes=8)
    assert logs["logs"]
    assert len(logs["logs"].encode()) <= 8
    with pytest.raises(RuntimeErrorBase, match="unknown"):
        runtime_logs(config, "missing", state_dir=tmp_path / "state")
    with pytest.raises(RuntimeErrorBase, match="unknown"):
        runtime_status(config, name="missing", state_dir=tmp_path / "state")


def test_manager_restart_marks_held_tunnel_stale_without_claiming_connection(tmp_path):
    config = _config(tmp_path)
    first_relay = FakeRelay()
    first = TunnelManager(
        config, state_dir=tmp_path / "state",
        relay_factory=lambda _relay, _policy: first_relay)
    first.start("database", allow_live=True, allow_network=True)

    second = TunnelManager(
        config, state_dir=tmp_path / "state",
        relay_factory=lambda _relay, _policy: FakeRelay())
    result = runtime_context(
        type("Memory", (), {"root": tmp_path})(),
        "payments",
        state_dir=tmp_path / "state",
    )
    assert result["items"][0]["state"] == "stale"
    assert "stale" in result["context"]
    assert "connected" not in result["context"]
    assert second.status("database")["running"] is False


class _EnvProcesses:
    """A process factory that records the child env and emits fixed output."""

    def __init__(self, output: bytes) -> None:
        self.output = output
        self.calls: list[dict[str, Any]] = []
        self.created: list[FakeProcess] = []

    def factory(self, argv: list[str], **kwargs: Any) -> FakeProcess:
        self.calls.append({"argv": argv, "kwargs": kwargs})
        process = FakeProcess(50001 + len(self.created), output=self.output,
                              auto_exit=True)
        self.created.append(process)
        return process

    def identity(self, pid: int) -> tuple[str | None, int | None]:
        return f"start-{pid}", pid


def _env_config(root: Path, mode: str = "foreground",
                token: str = "secret://env/FAKE_TOKEN") -> Any:
    root.mkdir(parents=True, exist_ok=True)
    _write_config(root, f"""
[runtime.services.api]
command = ["python", "-m", "app"]
mode = "{mode}"
env = {{ LOG_LEVEL = "info", API_TOKEN = "{token}" }}
""")
    return load_project_config(root)


def test_runtime_env_refs_resolve_at_spawn_and_logs_are_redacted(tmp_path):
    config = _env_config(tmp_path)
    secret = "runtime-secret-value-1234"
    processes = _EnvProcesses(f"line {secret} end\n".encode())
    broker = SecretBroker(tmp_path, environ={"FAKE_TOKEN": secret})
    manager = ServiceManager(
        config, state_dir=tmp_path / "state",
        process_factory=processes.factory,
        identity_reader=processes.identity, secret_broker=broker)

    result = manager.run("api")
    assert secret not in result["output"]
    assert "[REDACTED]" in result["output"]
    child_env = processes.calls[0]["kwargs"]["env"]
    assert child_env["API_TOKEN"] == secret
    assert child_env["LOG_LEVEL"] == "info"
    assert child_env["PATH"] == os.environ["PATH"]
    state_text = (tmp_path / "state" / "state.json").read_text()
    assert secret not in state_text
    assert "API_TOKEN" not in state_text

    background = _env_config(tmp_path / "bg", mode="background")
    processes = _EnvProcesses(f"log {secret}\n".encode())
    manager = ServiceManager(
        background, state_dir=tmp_path / "bg-state",
        process_factory=processes.factory,
        identity_reader=processes.identity, secret_broker=broker)
    started = manager.start("api")
    manager._reader_threads[started["id"]].join(timeout=2)
    logs = manager.logs("api")
    assert secret not in logs and "[REDACTED]" in logs
    assert secret not in (tmp_path / "bg-state" / "state.json").read_text()
    log_path = tmp_path / "bg-state" / "logs" / f"{started['id']}.log"
    assert secret not in log_path.read_text()


def test_runtime_env_without_refs_keeps_inherited_environment(tmp_path):
    _write_config(tmp_path, """
[runtime.services.api]
command = ["python", "-m", "app"]
mode = "foreground"
""")
    config = load_project_config(tmp_path)
    processes = _EnvProcesses(b"ok\n")
    manager = ServiceManager(
        config, state_dir=tmp_path / "state",
        process_factory=processes.factory,
        identity_reader=processes.identity,
        secret_broker=SecretBroker(tmp_path, environ={}))
    manager.run("api")
    assert "env" not in processes.calls[0]["kwargs"]


def test_runtime_session_reference_is_denied_without_a_value(tmp_path):
    store = SessionSecretStore()
    captured = store.capture("session-value-1234", harness="opencode",
                             session="s1")
    config = _env_config(tmp_path, token=captured.ref)
    processes = _EnvProcesses(b"never\n")
    manager = ServiceManager(
        config, state_dir=tmp_path / "state",
        process_factory=processes.factory,
        identity_reader=processes.identity,
        secret_broker=SecretBroker(tmp_path, environ={}, session_store=store))
    with pytest.raises(RuntimeErrorBase) as excinfo:
        manager.run("api")
    message = str(excinfo.value)
    assert "session_context_required" in message
    assert "API_TOKEN" in message
    assert "session-value-1234" not in message
    assert processes.calls == []
    assert not (tmp_path / "state" / "state.json").exists()


def test_runtime_service_fingerprint_includes_env_refs(tmp_path):
    first = _env_config(tmp_path)
    second = _env_config(tmp_path, token="secret://env/OTHER_TOKEN")
    manager = ServiceManager(
        first, state_dir=tmp_path / "state",
        secret_broker=SecretBroker(tmp_path, environ={}))
    service_a = first.runtime_services[0]
    service_b = second.runtime_services[0]
    assert manager._service_fingerprint(service_a) != manager._service_fingerprint(service_b)
    assert manager._service_fingerprint(service_a) == manager._service_fingerprint(service_a)
