"""Bounded local runtime state for configured processes and relay tunnels.

Configuration describes what may be run. The versioned state store records
observations only; it never stores argv, environment, prompts, or process output.
Tunnel configuration is deliberately distinct from a live tunnel lease.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ozagia.config import (
    ProjectConfig,
    RelayConfig,
    RuntimeServiceConfig,
    RuntimeTunnelConfig,
    load_project_config,
    state_path,
)
from ozagia.core.relay import Relay, RelayPolicy, RelaySpec, TunnelTarget

STATE_VERSION = 1
MAX_RECORDS = 500
MAX_STATE_BYTES = 1_048_576
MAX_LOG_BYTES = 256 * 1_024
MAX_REQUESTED_LOG_BYTES = 64 * 1_024
MAX_CONTEXT_ITEMS = 24
_ID = re.compile(r"[a-f0-9]{24,32}\Z")
_STATES = {
    "configured", "starting", "running", "exited", "failed", "stopped",
    "stale", "unknown",
}
_RECORD_FIELDS = {
    "id", "kind", "name", "state", "mode", "pid", "process_group",
    "process_start", "exit_code", "exit_signal", "started_at", "ended_at",
    "updated_at", "config_fingerprint", "command_fingerprint", "relay",
    "target", "local_host", "local_port", "lease_token", "reason",
}
_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}\Z")
_SHELL_META = re.compile(r"[;&|`$<>\\\n\r]")


class RuntimeErrorBase(RuntimeError):
    """A safe runtime operation error without command/output detail."""


class RuntimeStateError(RuntimeErrorBase):
    """The local runtime state store is invalid or unavailable."""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _fingerprint(value: Any) -> str:
    packed = json.dumps(value, sort_keys=True, separators=(",", ":"),
                        ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(packed).hexdigest()


def _runtime_id(kind: str, name: str) -> str:
    return hashlib.sha256(f"{kind}\0{name}".encode("utf-8")).hexdigest()[:24]


def _process_identity(pid: int) -> tuple[str | None, int | None]:
    """Return Linux process start ticks and process-group ID when verifiable."""
    if os.name != "posix":
        return None, None
    try:
        # comm may contain spaces and parentheses; fields after its final ')'
        # start at field 3 (state), making starttime field 22 index 19.
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        rest = stat[stat.rfind(")") + 1:].strip().split()
        token = rest[19]
        group = os.getpgid(pid)
    except (OSError, ValueError, IndexError, ProcessLookupError):
        return None, None
    return token, group


def _valid_record(record: Any, record_id: str) -> dict[str, Any]:
    if not isinstance(record, dict) or set(record) - _RECORD_FIELDS:
        raise RuntimeStateError("runtime state contains an invalid record")
    if record.get("id") != record_id or not _ID.fullmatch(record_id):
        raise RuntimeStateError("runtime state contains an invalid record ID")
    if (not isinstance(record.get("kind"), str)
            or record["kind"] not in {"service", "process", "tunnel"}):
        raise RuntimeStateError("runtime state contains an invalid record kind")
    if not isinstance(record.get("name"), str) or len(record["name"]) > 128:
        raise RuntimeStateError("runtime state contains an invalid name")
    if (not isinstance(record.get("state"), str)
            or record["state"] not in _STATES):
        raise RuntimeStateError("runtime state contains an invalid status")
    for field in (
        "mode", "process_start", "started_at", "ended_at", "updated_at",
        "config_fingerprint", "command_fingerprint", "relay", "target",
        "local_host", "lease_token", "reason",
    ):
        value = record.get(field)
        if value is not None and (not isinstance(value, str) or len(value) > 512):
            raise RuntimeStateError("runtime state contains an oversized field")
    for field in ("pid", "process_group", "exit_code", "exit_signal", "local_port"):
        value = record.get(field)
        if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
            raise RuntimeStateError("runtime state contains an invalid numeric field")
    if record.get("local_port") is not None and not 0 <= record["local_port"] <= 65_535:
        raise RuntimeStateError("runtime state contains an invalid port")
    # A second line of defense against accidental persistence of execution data.
    if {"argv", "environment", "env", "output", "stdout", "stderr", "prompt"} & set(record):
        raise RuntimeStateError("runtime state contains forbidden execution data")
    return dict(record)


class RuntimeStore:
    """Atomic, versioned local state plus mode-restricted bounded log files."""

    def __init__(self, state_dir: str | Path, *, max_log_bytes: int = MAX_LOG_BYTES,
                 create: bool = True):
        self.path = Path(state_dir).expanduser().resolve()
        self.logs_dir = self.path / "logs"
        self.max_log_bytes = min(MAX_LOG_BYTES, max(0, int(max_log_bytes)))
        self._lock = threading.RLock()
        if not create:
            return
        try:
            self.logs_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(self.path, 0o700)
            os.chmod(self.logs_dir, 0o700)
        except OSError:
            raise RuntimeStateError("could not prepare runtime state directory") from None

    def _read_unlocked(self) -> dict[str, dict[str, Any]]:
        try:
            if not (self.path / "state.json").exists():
                return {}
            state_path = self.path / "state.json"
            if state_path.stat().st_size > MAX_STATE_BYTES:
                raise RuntimeStateError("runtime state file exceeds its size limit")
            value = json.loads(state_path.read_text(encoding="utf-8"))
        except RuntimeStateError:
            raise
        except (OSError, json.JSONDecodeError):
            raise RuntimeStateError("could not read runtime state") from None
        if (not isinstance(value, dict)
                or set(value) != {"version", "records"}
                or isinstance(value.get("version"), bool)
                or value.get("version") != STATE_VERSION
                or not isinstance(value.get("records"), dict)
                or len(value["records"]) > MAX_RECORDS):
            raise RuntimeStateError("runtime state has an unsupported format")
        return {
            record_id: _valid_record(record, record_id)
            for record_id, record in value["records"].items()
        }

    def _write_unlocked(self, records: dict[str, dict[str, Any]]) -> None:
        if len(records) > MAX_RECORDS:
            raise RuntimeStateError("runtime state record limit exceeded")
        payload = json.dumps(
            {"version": STATE_VERSION, "records": records},
            sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        ).encode("utf-8")
        if len(payload) > MAX_STATE_BYTES:
            raise RuntimeStateError("runtime state file exceeds its size limit")
        fd = -1
        temp_name: str | None = None
        try:
            fd, temp_name = tempfile.mkstemp(prefix=".state-", dir=self.path)
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as stream:
                fd = -1
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_name, self.path / "state.json")
            os.chmod(self.path / "state.json", 0o600)
        except OSError:
            raise RuntimeStateError("could not persist runtime state") from None
        finally:
            if fd >= 0:
                os.close(fd)
            if temp_name is not None:
                try:
                    os.unlink(temp_name)
                except FileNotFoundError:
                    pass

    def all(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return self._read_unlocked()

    def get(self, record_id: str) -> dict[str, Any] | None:
        with self._lock:
            record = self._read_unlocked().get(record_id)
            return dict(record) if record is not None else None

    def put(self, record: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(record, Mapping):
            raise RuntimeStateError("runtime record must be a mapping")
        record_id = record.get("id")
        if not isinstance(record_id, str):
            raise RuntimeStateError("runtime record ID is required")
        clean = _valid_record(dict(record), record_id)
        with self._lock:
            records = self._read_unlocked()
            records[record_id] = clean
            self._write_unlocked(records)
        return dict(clean)

    def append_log(self, record_id: str, data: bytes) -> None:
        if not _ID.fullmatch(record_id) or not isinstance(data, bytes):
            raise RuntimeStateError("invalid runtime log request")
        if self.max_log_bytes == 0 or not data:
            return
        log_path = self.logs_dir / f"{record_id}.log"
        with self._lock:
            try:
                existing = log_path.read_bytes()[-self.max_log_bytes:]
            except FileNotFoundError:
                existing = b""
            payload = (existing + data)[-self.max_log_bytes:]
            fd, temp_name = tempfile.mkstemp(prefix=f".{record_id}-", dir=self.logs_dir)
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "wb") as stream:
                    fd = -1
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temp_name, log_path)
                os.chmod(log_path, 0o600)
            except OSError:
                raise RuntimeStateError("could not persist runtime log") from None
            finally:
                if fd >= 0:
                    os.close(fd)
                try:
                    os.unlink(temp_name)
                except FileNotFoundError:
                    pass

    def logs(self, record_id: str, *, max_bytes: int = 4_096) -> str:
        if not _ID.fullmatch(record_id):
            raise RuntimeStateError("invalid runtime log request")
        limit = min(MAX_REQUESTED_LOG_BYTES, max(0, int(max_bytes)))
        if limit == 0:
            return ""
        try:
            data = (self.logs_dir / f"{record_id}.log").read_bytes()[-limit:]
        except FileNotFoundError:
            return ""
        except OSError:
            raise RuntimeStateError("could not read runtime log") from None
        return data.decode("utf-8", errors="replace")


def _validate_argv(argv: Sequence[str]) -> tuple[str, ...]:
    if isinstance(argv, (str, bytes)) or not isinstance(argv, Sequence):
        raise RuntimeErrorBase("argv must be a non-empty sequence")
    if not 1 <= len(argv) <= 128:
        raise RuntimeErrorBase("argv must be a non-empty sequence")
    checked: list[str] = []
    total = 0
    for index, item in enumerate(argv):
        if (not isinstance(item, str) or not item or len(item) > 4_096
                or "\x00" in item or any(ord(char) < 32 for char in item)
                or _SHELL_META.search(item)):
            raise RuntimeErrorBase("argv contains an unsafe value")
        if index == 0 and any(char.isspace() for char in item):
            raise RuntimeErrorBase("argv must not contain a shell command string")
        total += len(item.encode("utf-8"))
        if total > 32 * 1_024:
            raise RuntimeErrorBase("argv exceeds the supported size")
        checked.append(item)
    return tuple(checked)


def _project_config(config: ProjectConfig | str | Path) -> ProjectConfig:
    if isinstance(config, ProjectConfig):
        return config
    return load_project_config(config)


def _result(record: Mapping[str, Any], *, configured: bool = True) -> dict[str, Any]:
    clean = dict(record)
    clean["configured"] = configured
    clean["running"] = clean.get("state") == "running"
    return clean


class ServiceManager:
    """Run configured local services and explicitly reviewed raw argv."""

    def __init__(
        self,
        config: ProjectConfig | str | Path,
        *,
        store: RuntimeStore | None = None,
        state_dir: str | Path | None = None,
        process_factory: Callable[..., Any] | None = None,
        identity_reader: Callable[[int], tuple[str | None, int | None]] | None = None,
        signal_group: Callable[[int, int], None] | None = None,
        stop_grace_s: float = 0.5,
        output_limit: int = 64 * 1_024,
    ):
        self.config = _project_config(config)
        selected_state_dir = (state_dir if state_dir is not None else
                              state_path(self.config.root, "runtime"))
        self.store = store or RuntimeStore(selected_state_dir)
        self._process_factory = process_factory or subprocess.Popen
        self._identity_reader = identity_reader or _process_identity
        self._signal_group = signal_group or os.killpg
        self._stop_grace_s = max(0.0, min(float(stop_grace_s), 10.0))
        self._output_limit = min(MAX_REQUESTED_LOG_BYTES, max(0, int(output_limit)))
        self._handles: dict[str, Any] = {}
        self._reader_threads: dict[str, threading.Thread] = {}
        self._lock = threading.RLock()
        self._services = {item.name: item for item in self.config.runtime_services}
        self._reconcile()

    def _reconcile(self) -> None:
        for record_id, record in self.store.all().items():
            if record.get("kind") not in {"service", "process"}:
                continue
            if record.get("state") not in {"starting", "running"}:
                continue
            if self._verified(record):
                record["state"] = "running"
            else:
                record["state"] = "stale"
                record["reason"] = "process identity could not be verified"
            record["updated_at"] = _now()
            self.store.put(record)

    def _identity(self, pid: int) -> tuple[str | None, int | None]:
        try:
            result = self._identity_reader(pid)
        except Exception:
            return None, None
        if (not isinstance(result, tuple) or len(result) != 2
                or (result[0] is not None and not isinstance(result[0], str))
                or (result[1] is not None and
                    (isinstance(result[1], bool) or not isinstance(result[1], int)))):
            return None, None
        return result

    def _verified(self, record: Mapping[str, Any]) -> bool:
        pid, group, token = (
            record.get("pid"), record.get("process_group"), record.get("process_start"))
        if (isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0
                or group != pid or not isinstance(token, str) or not token):
            return False
        current_token, current_group = self._identity(pid)
        return current_token == token and current_group == group

    def _service_fingerprint(self, service: RuntimeServiceConfig) -> str:
        return _fingerprint({
            "name": service.name, "command": service.command,
            "workdir": service.workdir, "relay": service.relay,
            "mode": service.mode,
        })

    def _base_record(
        self,
        record_id: str,
        *,
        kind: str,
        name: str,
        mode: str,
        command: Sequence[str] | None = None,
        config_fingerprint: str | None = None,
        relay: str | None = None,
    ) -> dict[str, Any]:
        now = _now()
        return {
            "id": record_id, "kind": kind, "name": name, "state": "starting",
            "mode": mode, "pid": None, "process_group": None,
            "process_start": None, "exit_code": None, "exit_signal": None,
            "started_at": now, "ended_at": None, "updated_at": now,
            "config_fingerprint": config_fingerprint,
            "command_fingerprint": _fingerprint(tuple(command)) if command else None,
            "relay": relay, "target": None, "local_host": None,
            "local_port": None, "lease_token": None, "reason": None,
        }

    def _workdir(self, relative: str | None) -> str | None:
        if relative is None:
            return str(self.config.root)
        root = self.config.root.resolve()
        candidate = (root / relative).resolve()
        if not candidate.is_relative_to(root):
            raise RuntimeErrorBase("configured workdir escapes the repository")
        return str(candidate)

    def _spawn(
        self,
        *,
        record: dict[str, Any],
        argv: Sequence[str],
        workdir: str | None,
        background: bool,
    ) -> tuple[Any, bytearray | None]:
        record["state"] = "starting"
        record["updated_at"] = _now()
        self.store.put(record)
        try:
            process = self._process_factory(
                list(argv),
                cwd=workdir,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                shell=False,
                start_new_session=(os.name == "posix"),
                close_fds=True,
            )
        except Exception:
            record["state"] = "failed"
            record["ended_at"] = _now()
            record["updated_at"] = record["ended_at"]
            record["reason"] = "process could not be started"
            self.store.put(record)
            raise RuntimeErrorBase("process could not be started") from None
        pid = getattr(process, "pid", None)
        if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0:
            token, group = self._identity(pid)
            record.update(pid=pid, process_group=group, process_start=token)
        record["state"] = (
            "running" if record.get("process_start")
            and record.get("process_group") == record.get("pid")
            else "unknown"
        )
        record["updated_at"] = _now()
        if record["state"] == "unknown":
            record["reason"] = "process identity or owned group is unavailable"
        self.store.put(record)
        self._handles[record["id"]] = process
        collected = bytearray() if not background else None
        output = getattr(process, "stdout", None)
        if output is not None:
            thread = threading.Thread(
                target=self._drain_output,
                args=(record["id"], output, background, collected),
                name="ozagia-runtime-output",
                daemon=True,
            )
            self._reader_threads[record["id"]] = thread
            thread.start()
        return process, collected

    def _drain_output(
        self, record_id: str, stream: Any, background: bool,
        collected: bytearray | None,
    ) -> None:
        try:
            while True:
                chunk = stream.read(8 * 1_024)
                if not chunk:
                    return
                if not isinstance(chunk, bytes):
                    chunk = str(chunk).encode("utf-8", errors="replace")
                if background:
                    self.store.append_log(record_id, chunk)
                elif collected is not None:
                    remaining = self._output_limit - len(collected)
                    if remaining > 0:
                        collected.extend(chunk[:remaining])
        except (OSError, ValueError, RuntimeStateError):
            return

    def _finalize(self, record: dict[str, Any], code: int) -> dict[str, Any]:
        record["state"] = "exited"
        record["exit_code"] = code if code >= 0 else None
        record["exit_signal"] = -code if code < 0 else None
        record["ended_at"] = _now()
        record["updated_at"] = record["ended_at"]
        self.store.put(record)
        self._handles.pop(record["id"], None)
        return record

    def _status_record(
        self, kind: str, name: str, *, fingerprint: str | None = None,
        mode: str = "background", relay: str | None = None,
    ) -> dict[str, Any]:
        record_id = _runtime_id(kind, name)
        record = self.store.get(record_id)
        if record is None:
            return {
                "id": record_id, "kind": kind, "name": name, "state": "configured",
                "mode": mode, "pid": None, "process_group": None,
                "process_start": None, "exit_code": None, "exit_signal": None,
                "started_at": None, "ended_at": None, "updated_at": None,
                "config_fingerprint": fingerprint, "command_fingerprint": None,
                "relay": relay, "target": None, "local_host": None,
                "local_port": None, "lease_token": None, "reason": None,
            }
        process = self._handles.get(record_id)
        if record.get("state") == "running" and process is not None:
            try:
                code = process.poll()
            except Exception:
                code = None
            if code is not None:
                record = self._finalize(record, int(code))
        elif record.get("state") in {"starting", "running"} and not self._verified(record):
            record["state"] = "stale"
            record["reason"] = "process identity could not be verified"
            record["updated_at"] = _now()
            self.store.put(record)
        return record

    def _require_service(self, name: str) -> RuntimeServiceConfig:
        if not isinstance(name, str) or name not in self._services:
            raise RuntimeErrorBase("unknown configured service")
        return self._services[name]

    def start(self, name: str) -> dict[str, Any]:
        service = self._require_service(name)
        if service.mode != "background":
            raise RuntimeErrorBase("service is not configured for background start")
        if service.relay is not None:
            raise RuntimeErrorBase("remote/background relay services are unsupported")
        record_id = _runtime_id("service", name)
        current = self._status_record(
            "service", name, fingerprint=self._service_fingerprint(service),
            mode="background", relay=service.relay)
        if current["state"] in {"running", "starting", "stale", "unknown"}:
            raise RuntimeErrorBase("service has an active or unverified runtime record")
        record = self._base_record(
            record_id, kind="service", name=name, mode="background",
            command=service.command,
            config_fingerprint=self._service_fingerprint(service),
            relay=service.relay,
        )
        self._spawn(
            record=record, argv=service.command,
            workdir=self._workdir(service.workdir), background=True)
        return _result(self._status_record(
            "service", name, fingerprint=self._service_fingerprint(service),
            mode="background", relay=service.relay))

    def run(self, name: str) -> dict[str, Any]:
        service = self._require_service(name)
        if service.relay is not None:
            raise RuntimeErrorBase("foreground relay execution is unsupported")
        record_id = _runtime_id("service", name)
        current = self._status_record(
            "service", name, fingerprint=self._service_fingerprint(service),
            mode="foreground", relay=service.relay)
        if current["state"] in {"running", "starting", "stale", "unknown"}:
            raise RuntimeErrorBase("service has an active or unverified runtime record")
        record = self._base_record(
            record_id, kind="service", name=name, mode="foreground",
            command=service.command,
            config_fingerprint=self._service_fingerprint(service),
            relay=service.relay,
        )
        process, output = self._spawn(
            record=record, argv=service.command,
            workdir=self._workdir(service.workdir), background=False)
        try:
            code = process.wait()
        except Exception:
            record["state"] = "failed"
            record["ended_at"] = _now()
            record["updated_at"] = record["ended_at"]
            record["reason"] = "foreground process wait failed"
            self.store.put(record)
            self._handles.pop(record_id, None)
            raise RuntimeErrorBase("foreground process failed") from None
        reader = self._reader_threads.pop(record_id, None)
        if reader is not None:
            reader.join(timeout=1.0)
        result = self._finalize(record, int(code))
        result["output"] = (
            bytes(output or b"").decode("utf-8", errors="replace")
        )
        return _result(result)

    def _raw_name(self) -> str:
        return f"raw-{uuid.uuid4().hex[:16]}"

    def start_raw(
        self, argv: Sequence[str], *, workdir: str | Path | None = None,
    ) -> dict[str, Any]:
        checked = _validate_argv(argv)
        name = self._raw_name()
        record_id = _runtime_id("process", name)
        raw_workdir = str(Path(workdir).expanduser().resolve()) if workdir else None
        record = self._base_record(
            record_id, kind="process", name=name, mode="background",
            command=checked,
            config_fingerprint=_fingerprint({"kind": "reviewed-raw", "name": name}),
        )
        self._spawn(record=record, argv=checked, workdir=raw_workdir, background=True)
        return _result(self._status_record("process", name, mode="background"),
                       configured=False)

    def run_raw(
        self, argv: Sequence[str], *, workdir: str | Path | None = None,
    ) -> dict[str, Any]:
        checked = _validate_argv(argv)
        name = self._raw_name()
        record_id = _runtime_id("process", name)
        raw_workdir = str(Path(workdir).expanduser().resolve()) if workdir else None
        record = self._base_record(
            record_id, kind="process", name=name, mode="foreground",
            command=checked,
            config_fingerprint=_fingerprint({"kind": "reviewed-raw", "name": name}),
        )
        process, output = self._spawn(
            record=record, argv=checked, workdir=raw_workdir, background=False)
        try:
            code = process.wait()
        except Exception:
            record["state"] = "failed"
            record["ended_at"] = _now()
            record["updated_at"] = record["ended_at"]
            record["reason"] = "foreground process wait failed"
            self.store.put(record)
            self._handles.pop(record_id, None)
            raise RuntimeErrorBase("foreground process failed") from None
        reader = self._reader_threads.pop(record_id, None)
        if reader is not None:
            reader.join(timeout=1.0)
        result = self._finalize(record, int(code))
        result["output"] = bytes(output or b"").decode("utf-8", errors="replace")
        return _result(result, configured=False)

    def status(self, name: str) -> dict[str, Any]:
        if name in self._services:
            service = self._services[name]
            return _result(self._status_record(
                "service", name, fingerprint=self._service_fingerprint(service),
                mode=service.mode, relay=service.relay))
        raw_record = self.store.get(_runtime_id("process", name))
        if raw_record is not None:
            return _result(self._status_record("process", name,
                                               mode=raw_record.get("mode", "background")),
                           configured=False)
        raise RuntimeErrorBase("unknown runtime process")

    def list(self) -> list[dict[str, Any]]:
        rows = [self.status(name) for name in self._services]
        known = {row["id"] for row in rows}
        for record in self.store.all().values():
            if record["kind"] == "process" and record["id"] not in known:
                rows.append(self.status(record["name"]))
        return rows

    def logs(self, name: str, *, max_bytes: int = 4_096) -> str:
        if name in self._services:
            record_id = _runtime_id("service", name)
        else:
            candidate = self.store.get(_runtime_id("process", name))
            if candidate is None:
                raise RuntimeErrorBase("unknown runtime process")
            record_id = candidate["id"]
        return self.store.logs(record_id, max_bytes=max_bytes)

    def stop(self, name: str) -> dict[str, Any]:
        current = self.status(name)
        if current["state"] in {"configured", "stopped", "exited", "failed"}:
            return current
        record = self.store.get(current["id"])
        if record is None or not self._verified(record):
            if record is not None:
                record["state"] = "stale"
                record["reason"] = "process identity could not be verified"
                record["updated_at"] = _now()
                self.store.put(record)
            raise RuntimeErrorBase("refusing to stop an unverified process")
        group = record["process_group"]
        try:
            self._signal_group(group, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except OSError:
            raise RuntimeErrorBase("could not stop verified process") from None

        process = self._handles.get(record["id"])
        exited = False
        if process is not None:
            try:
                process.wait(timeout=self._stop_grace_s)
                exited = True
            except (subprocess.TimeoutExpired, TimeoutError):
                exited = False
            except Exception:
                exited = process.poll() is not None
        else:
            deadline = time.monotonic() + self._stop_grace_s
            while time.monotonic() < deadline:
                if not self._verified(record):
                    exited = True
                    break
                time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
        if not exited and self._verified(record):
            try:
                self._signal_group(group, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except OSError:
                raise RuntimeErrorBase("could not terminate verified process") from None
            if process is not None:
                try:
                    process.wait(timeout=1.0)
                except Exception:
                    pass
        if self._verified(record):
            record["state"] = "unknown"
            record["reason"] = "verified process did not exit after stop"
        else:
            record["state"] = "stopped"
            record["ended_at"] = _now()
            record["reason"] = None
        record["updated_at"] = _now()
        self.store.put(record)
        self._handles.pop(record["id"], None)
        return _result(record, configured=record["kind"] == "service")

    def restart(self, name: str) -> dict[str, Any]:
        current = self.status(name)
        if current["state"] in {"running", "starting"}:
            stopped = self.stop(name)
            if stopped["state"] != "stopped":
                raise RuntimeErrorBase("restart refused because the prior process is not stopped")
        elif current["state"] in {"stale", "unknown"}:
            raise RuntimeErrorBase("restart refused for an unverified process")
        if current["kind"] == "service":
            return self.start(name)
        record = self.store.get(current["id"])
        if record is None or record.get("kind") != "process":
            raise RuntimeErrorBase("raw process cannot be restarted without a reviewed argv")
        raise RuntimeErrorBase("raw process restart requires a new reviewed argv")


# The process manager is the same local ownership boundary as service manager.
ProcessManager = ServiceManager


class TunnelManager:
    """Open configured tunnels while keeping config and live leases distinct."""

    def __init__(
        self,
        config: ProjectConfig | str | Path,
        *,
        store: RuntimeStore | None = None,
        state_dir: str | Path | None = None,
        relay_factory: Callable[[RelayConfig, RelayPolicy], Any] | None = None,
    ):
        self.config = _project_config(config)
        selected_state_dir = (state_dir if state_dir is not None else
                              state_path(self.config.root, "runtime"))
        self.store = store or RuntimeStore(selected_state_dir)
        self._relay_factory = relay_factory or self._default_relay
        self._tunnels = {item.name: item for item in self.config.runtime_tunnels}
        self._relays = {item.name: item for item in self.config.relays}
        self._leases: dict[str, tuple[Any, Any]] = {}
        self._lock = threading.RLock()
        self._reconcile()

    @staticmethod
    def _default_relay(relay: RelayConfig, policy: RelayPolicy) -> Relay:
        specs = [
            RelaySpec(spec.kind, dict(spec.options), name=spec.name)
            for spec in relay.specs
        ]
        return Relay.from_specs(specs, policy=policy, ephemeral=True)

    def _reconcile(self) -> None:
        for record in self.store.all().values():
            if record.get("kind") == "tunnel" and record.get("state") in {
                "starting", "running",
            }:
                record["state"] = "stale"
                record["reason"] = "tunnel lease is not held by this manager"
                record["updated_at"] = _now()
                self.store.put(record)

    def _tunnel_fingerprint(self, tunnel: RuntimeTunnelConfig) -> str:
        return _fingerprint({
            "name": tunnel.name, "relay": tunnel.relay,
            "target": tunnel.target, "local_port": tunnel.local_port,
        })

    def _status_record(self, tunnel: RuntimeTunnelConfig) -> dict[str, Any]:
        record_id = _runtime_id("tunnel", tunnel.name)
        record = self.store.get(record_id)
        if record is None:
            return {
                "id": record_id, "kind": "tunnel", "name": tunnel.name,
                "state": "configured", "mode": "tunnel", "pid": None,
                "process_group": None, "process_start": None, "exit_code": None,
                "exit_signal": None, "started_at": None, "ended_at": None,
                "updated_at": None,
                "config_fingerprint": self._tunnel_fingerprint(tunnel),
                "command_fingerprint": None, "relay": tunnel.relay,
                "target": tunnel.target, "local_host": None, "local_port": None,
                "lease_token": None, "reason": None,
            }
        if record.get("state") == "running" and record_id not in self._leases:
            record["state"] = "stale"
            record["reason"] = "tunnel lease is not held by this manager"
            record["updated_at"] = _now()
            self.store.put(record)
        return record

    def _require_tunnel(self, name: str) -> RuntimeTunnelConfig:
        if not isinstance(name, str) or name not in self._tunnels:
            raise RuntimeErrorBase("unknown configured tunnel")
        return self._tunnels[name]

    def start(
        self, name: str, *, allow_live: bool = False,
        allow_network: bool = False,
    ) -> dict[str, Any]:
        if not allow_live or not allow_network:
            raise RuntimeErrorBase(
                "tunnel start requires explicit live and network approval")
        tunnel = self._require_tunnel(name)
        current = self._status_record(tunnel)
        if current["state"] in {"running", "starting", "stale", "unknown"}:
            raise RuntimeErrorBase("tunnel has an active or unverified runtime record")
        relay_config = self._relays[tunnel.relay]
        policy = RelayPolicy(
            allowed_kinds=frozenset(relay_config.allowed_kinds),
            allow_live=allow_live, allow_network=allow_network,
            max_timeout_s=relay_config.max_timeout_s,
            command_allowlist=relay_config.command_allowlist,
            max_output_bytes=relay_config.max_output_bytes,
        )
        record_id = _runtime_id("tunnel", name)
        now = _now()
        record = {
            "id": record_id, "kind": "tunnel", "name": name,
            "state": "starting", "mode": "tunnel", "pid": None,
            "process_group": None, "process_start": None, "exit_code": None,
            "exit_signal": None, "started_at": now, "ended_at": None,
            "updated_at": now,
            "config_fingerprint": self._tunnel_fingerprint(tunnel),
            "command_fingerprint": None, "relay": tunnel.relay,
            "target": tunnel.target, "local_host": None, "local_port": None,
            "lease_token": uuid.uuid4().hex, "reason": None,
        }
        self.store.put(record)
        relay: Any | None = None
        lease: Any | None = None
        try:
            relay = self._relay_factory(relay_config, policy)
            host, port_text = tunnel.target.rsplit(":", 1)
            lease = relay.open_tunnel(
                TunnelTarget(host, int(port_text)),
                local_port=tunnel.local_port,
                timeout_s=min(relay_config.max_timeout_s, 30.0),
                dry_run=False,
            )
            local_host = getattr(lease, "local_host", None)
            local_port = getattr(lease, "local_port", None)
            if (getattr(lease, "dry_run", False)
                    or local_host != "127.0.0.1"
                    or isinstance(local_port, bool)
                    or not isinstance(local_port, int)
                    or not 1 <= local_port <= 65_535):
                raise RuntimeErrorBase("relay returned an invalid live tunnel lease")
        except Exception:
            record["state"] = "failed"
            record["ended_at"] = _now()
            record["updated_at"] = record["ended_at"]
            record["reason"] = "tunnel could not be opened"
            self.store.put(record)
            if lease is not None:
                try:
                    lease.close()
                except Exception:
                    pass
            if relay is not None:
                try:
                    relay.close()
                except Exception:
                    pass
            raise RuntimeErrorBase("tunnel could not be opened") from None
        record["state"] = "running"
        record["local_host"] = "127.0.0.1"
        record["local_port"] = local_port
        record["updated_at"] = _now()
        self.store.put(record)
        self._leases[record_id] = (relay, lease)
        return _result(record)

    def stop(self, name: str) -> dict[str, Any]:
        tunnel = self._require_tunnel(name)
        record = self._status_record(tunnel)
        if record["state"] in {"configured", "stopped", "failed"}:
            return _result(record)
        record_id = record["id"]
        held = self._leases.pop(record_id, None)
        if held is None:
            record["state"] = "stale"
            record["reason"] = "tunnel lease is not held by this manager"
            record["updated_at"] = _now()
            self.store.put(record)
            raise RuntimeErrorBase("refusing to claim an unheld tunnel was stopped")
        relay, lease = held
        try:
            lease.close()
        except Exception:
            record["state"] = "failed"
            record["reason"] = "tunnel close failed"
            record["ended_at"] = _now()
            record["updated_at"] = record["ended_at"]
            self.store.put(record)
            raise RuntimeErrorBase("tunnel close failed") from None
        try:
            relay.close()
        except Exception:
            pass
        record["state"] = "stopped"
        record["reason"] = None
        record["ended_at"] = _now()
        record["updated_at"] = record["ended_at"]
        self.store.put(record)
        return _result(record)

    def status(self, name: str) -> dict[str, Any]:
        return _result(self._status_record(self._require_tunnel(name)))

    def list(self) -> list[dict[str, Any]]:
        return [self.status(name) for name in self._tunnels]


_GENERIC_WORDS = {
    "a", "about", "all", "an", "and", "any", "are", "can", "check",
    "connected", "connection", "configure", "configured", "do", "does",
    "for", "from", "help", "how", "is", "it", "list", "local", "logs",
    "me", "of", "on", "or", "process", "remote", "run", "service",
    "services", "show", "start", "status", "stop", "the", "through",
    "to", "tunnel", "tunnels", "what", "with",
}
_WORD = re.compile(r"[a-z0-9]+")


def _words(text: str) -> set[str]:
    return {
        word for word in _WORD.findall(text.lower())
        if len(word) > 1 and word not in _GENERIC_WORDS
    }


def runtime_line(item: Mapping[str, Any]) -> str:
    name = item["name"]
    state = item["state"]
    if item["kind"] == "tunnel":
        if state == "configured":
            status = "configured, not connected"
        elif state == "running":
            status = "running"
        elif state == "stale":
            status = "stale, connection not verified"
        else:
            status = state
        target = item.get("target")
        suffix = f" (target {target})" if target else ""
        if state == "running" and item.get("local_port"):
            suffix = (f" (local {item.get('local_host')}:{item['local_port']} "
                      f"to {target})")
        return f"[ozagia] tunnel {name}: {status}{suffix}"
    if state == "configured":
        status = f"configured, not running ({item.get('mode', 'background')})"
    elif state == "running":
        status = f"running ({item.get('mode', 'background')})"
    elif state == "exited":
        code = item.get("exit_code")
        status = f"exited {code if code is not None else 'by signal'}"
    else:
        status = state
    label = "service" if item["kind"] == "service" else "process"
    return f"[ozagia] {label} {name}: {status}"


def _observed_service(
    service: RuntimeServiceConfig, store: RuntimeStore,
) -> dict[str, Any]:
    """Observe one service without reconciling or writing runtime state."""
    record = store.get(_runtime_id("service", service.name))
    state = "configured"
    pid = None
    mode = service.mode
    if record is not None:
        state = record["state"]
        mode = record.get("mode") or service.mode
        if state in {"running", "starting"}:
            candidate = record.get("pid")
            group = record.get("process_group")
            token = record.get("process_start")
            if (isinstance(candidate, int) and not isinstance(candidate, bool)
                    and isinstance(group, int) and group == candidate
                    and isinstance(token, str)):
                current, current_group = _process_identity(candidate)
                if current != token or current_group != group:
                    state = "stale"
                else:
                    pid = candidate
            else:
                state = "stale"
    return {
        "name": service.name, "kind": "service",
        "state": state, "configured": True,
        "running": state == "running",
        "relay": service.relay, "target": None,
        "mode": mode, "pid": pid if state == "running" else None,
        "description": service.description or None,
        "workdir": service.workdir,
    }


def _observed_tunnel(
    tunnel: RuntimeTunnelConfig, store: RuntimeStore,
) -> dict[str, Any]:
    """Observe one tunnel; a persisted lease is never called connected."""
    record = store.get(_runtime_id("tunnel", tunnel.name))
    state = "configured"
    if record is not None:
        state = record["state"]
        # A persisted running/starting lease is not verifiable from this
        # read-only view. Never call it connected without a held lease.
        if state in {"running", "starting"}:
            state = "stale"
    return {
        "name": tunnel.name, "kind": "tunnel",
        "state": state, "configured": True,
        "running": False,
        "relay": tunnel.relay, "target": tunnel.target,
        "description": tunnel.description or None,
    }


def _observed_process(record: Mapping[str, Any]) -> dict[str, Any]:
    """Observe one explicit raw process record without reconciling it."""
    state = record["state"]
    pid = None
    if state in {"running", "starting"}:
        candidate = record.get("pid")
        group = record.get("process_group")
        token = record.get("process_start")
        if (isinstance(candidate, int) and not isinstance(candidate, bool)
                and isinstance(group, int) and group == candidate
                and isinstance(token, str)):
            current, current_group = _process_identity(candidate)
            if current != token or current_group != group:
                state = "stale"
            else:
                pid = candidate
        else:
            state = "stale"
    return {
        "name": record["name"], "kind": "process",
        "state": state, "configured": False,
        "running": state == "running",
        "relay": record.get("relay"), "target": None,
        "mode": record.get("mode"), "pid": pid if state == "running" else None,
        "description": None, "workdir": None,
    }


def runtime_status(
    config: ProjectConfig | str | Path,
    *,
    name: str | None = None,
    state_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Return read-only configured and observed runtime state.

    This never creates the state directory, starts a process, or opens a tunnel.
    A persisted tunnel lease is reported as stale because this view cannot
    verify that a live lease is still held by another process.
    """
    project = _project_config(config)
    if name is not None and not isinstance(name, str):
        raise RuntimeErrorBase("runtime name must be text")
    selected_state_dir = state_dir or state_path(project.root, "runtime")
    store = RuntimeStore(selected_state_dir, create=False)
    services = [_observed_service(item, store) for item in project.runtime_services]
    tunnels = [_observed_tunnel(item, store) for item in project.runtime_tunnels]
    processes = [
        _observed_process(record)
        for record in store.all().values() if record["kind"] == "process"
    ]
    if name is not None:
        services = [item for item in services if item["name"] == name]
        tunnels = [item for item in tunnels if item["name"] == name]
        processes = [item for item in processes if item["name"] == name]
        if not services and not tunnels and not processes:
            raise RuntimeErrorBase("unknown runtime item")
    items = sorted(services + tunnels + processes,
                   key=lambda item: (item["kind"], item["name"]))
    return {
        "configured": True,
        "live": False,
        "services": services,
        "tunnels": tunnels,
        "processes": processes,
        "items": items,
        "context": "\n".join(runtime_line(item) for item in items),
        "relays": [
            {"name": item.name,
             "kinds": [spec.kind for spec in item.specs]}
            for item in project.relays
        ],
        "note": ("Configuration is declarative. Runtime states come from local "
                 "state and PID checks; a configured tunnel is not connected."),
    }


def runtime_logs(
    config: ProjectConfig | str | Path,
    name: str,
    *,
    state_dir: str | Path | None = None,
    max_bytes: int = 4_096,
) -> dict[str, Any]:
    """Read one bounded local log without reconciling runtime state."""
    project = _project_config(config)
    if not isinstance(name, str) or not name:
        raise RuntimeErrorBase("runtime name must be text")
    store = RuntimeStore(
        state_dir or state_path(project.root, "runtime"), create=False)
    if any(item.name == name for item in project.runtime_services):
        record_id = _runtime_id("service", name)
    else:
        record = store.get(_runtime_id("process", name))
        if record is None:
            raise RuntimeErrorBase("unknown runtime process")
        record_id = record["id"]
    limit = min(MAX_REQUESTED_LOG_BYTES, max(0, int(max_bytes)))
    return {
        "name": name, "max_bytes": limit,
        "logs": store.logs(record_id, max_bytes=limit),
    }


def runtime_context(
    memory: Any,
    query: str,
    history: Iterable[str] = (),
    *,
    max_chars: int = 1_200,
    config: ProjectConfig | str | Path | None = None,
    state_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Return matching runtime hints and safe structured status under a char cap.

    Matching is exact-token based over configured names/descriptions/workdirs;
    generic runtime words do not cause every configured item to be emitted.
    Logs, commands, environment, and credentials are never read or rendered.
    """
    if config is None:
        root = getattr(memory, "root", None)
        if root is None:
            return {"context": "", "items": [], "truncated": False}
        project = load_project_config(root)
    else:
        project = _project_config(config)
    query_text = query[:4_096] if isinstance(query, str) else ""
    history_rows: list[str] = []
    if history is not None:
        for index, item in enumerate(history):
            if index >= 8:
                break
            if isinstance(item, str) and item:
                history_rows.append(item[-1_200:])
    history_text = " ".join(history_rows[-4:])
    requested = _words(f"{query_text} {history_text}")
    if not requested:
        return {"context": "", "items": [], "truncated": False}
    selected_state_dir = state_dir or state_path(project.root, "runtime")
    # Context is a read-only observation: do not create the state directory,
    # reconcile process records, or mark another manager's tunnel stale here.
    store = RuntimeStore(selected_state_dir, create=False)
    entries: list[tuple[dict[str, Any], set[str]]] = []
    for service in project.runtime_services:
        identifiers = _words(" ".join(filter(None, (
            service.name, service.description, service.workdir or "",
        ))))
        if identifiers & requested:
            entries.append((_observed_service(service, store), identifiers))
    for tunnel in project.runtime_tunnels:
        identifiers = _words(" ".join((tunnel.name, tunnel.description)))
        if identifiers & requested:
            entries.append((_observed_tunnel(tunnel, store), identifiers))
    entries.sort(key=lambda row: (row[0]["kind"], row[0]["name"]))
    items = [item for item, _identifiers in entries[:MAX_CONTEXT_ITEMS]]
    budget = max(0, int(max_chars))
    lines: list[str] = []
    used = 0
    truncated = len(entries) > len(items)
    for item in items:
        line = runtime_line(item)
        prefix = "\n" if lines else ""
        if used + len(prefix) + len(line) > budget:
            truncated = True
            break
        lines.append(prefix + line)
        used += len(prefix) + len(line)
    return {
        "context": "".join(lines), "items": items,
        "truncated": truncated or len(lines) < len(items),
    }


__all__ = [
    "ProcessManager", "RuntimeErrorBase", "RuntimeStateError", "RuntimeStore",
    "ServiceManager", "TunnelManager", "runtime_context", "runtime_line",
    "runtime_logs", "runtime_status",
]
