"""Preview-first, audited process and connection actions.

This module is the bounded command layer planned for runtime services,
tunnels, the graph daemon and the integration installers. It is not another
process manager: previews describe a call, and applies call the existing
``ServiceManager``, ``TunnelManager``, daemon client/autostart and installer
modules.

Every mutating action follows the same shape:

1. ``preview(root, action, **args)`` observes the current state, returns the
   exact operation, argv or manager call, expected effect, preconditions,
   risks and a one-time confirmation token. It never starts, stops or signals
   anything.
2. ``apply(root, action, token, **args)`` checks the token, re-checks the
   preconditions, consumes the token, performs the action through the landed
   manager and appends a bounded audit record under ``<state>/actions``.

The token is stored under the state directory, not in argv or the repository.
Audit records contain the action, outcome and target label only: never argv,
environment, credentials, process output or log contents.
"""
from __future__ import annotations

import contextlib
import hashlib
import importlib
import importlib.util
import io
import json
import math
import os
import re
import secrets
import shutil
import sys
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ozagia.config import ConfigError, ProjectConfig, load_project_config, state_path
from ozagia.core.relay import Relay, RelayPolicy, RelaySpec, TunnelTarget
from ozagia.core.runtime import (
    RuntimeErrorBase,
    RuntimeStateError,
    ServiceManager,
    TunnelManager,
    runtime_status,
)
from ozagia.daemon import protocol
from ozagia.daemon.autostart import DEFAULT_IDLE_TIMEOUT_S, DEFAULT_WAIT_S
from ozagia.daemon.client import (
    DaemonClient,
    DaemonError,
    DaemonTimeout,
    DaemonUnavailable,
)

SCHEMA = 1
ACTIONS_DIR = "actions"
PENDING_DIR = "pending"
AUDIT_FILE = "audit.jsonl"
DEFAULT_DAEMON_TIMEOUT_S = 2.0
DEFAULT_SERVICE_LOG_BYTES = 4_096
MAX_SERVICE_LOG_BYTES = 64 * 1_024
TOKEN_TTL_S = 600
MAX_PENDING_TOKENS = 100
MAX_AUDIT_RECORDS = 200
MAX_AUDIT_BYTES = 256 * 1_024
MAX_ERROR_CHARS = 500
_TOKEN = re.compile(r"[A-Za-z0-9_-]{20,128}\Z")
_NAME = re.compile(r"[^\x00-\x1f\x7f]{1,128}\Z")
_INTEGRATIONS = ("opencode", "claude", "codex", "kiro")
_SCOPES = ("project", "user")
_INSTALLER_MODULES = {
    "opencode": "ozagia.integrations.opencode",
    "claude": "ozagia.integrations.claude_code",
    "codex": "ozagia.integrations.codex",
    "kiro": "ozagia.integrations.kiro",
}
_INTEGRATION_SCOPES = {
    "opencode": ("project",),
    "claude": ("project",),
    "codex": ("project", "user"),
    "kiro": ("project", "user"),
}
_SECRET_TEXT = re.compile(
    r"(?i)(token|secret|password|passwd|api[_-]?key|authorization|"
    r"access[_-]?key|private[_-]?key|credential)(\s*[=:]\s*)\S+"
)


class ActionError(ValueError):
    """The request is malformed, unsupported or cannot be read."""


class ActionRefused(RuntimeError):
    """A well-formed request was refused before it changed anything."""


@dataclass(frozen=True)
class ActionSpec:
    name: str
    description: str
    mutating: bool = False
    destructive: bool = False
    live_gated: bool = False


ACTION_SPECS: tuple[ActionSpec, ...] = (
    ActionSpec("daemon.status", "Show whether the graph daemon answers."),
    ActionSpec("daemon.ensure", "Start the graph daemon only when none answers.",
               mutating=True),
    ActionSpec("daemon.stop", "Stop the graph daemon if it is running.",
               mutating=True, destructive=True),
    ActionSpec("daemon.restart", "Stop a running daemon and ensure one answers.",
               mutating=True, destructive=True),
    ActionSpec("service.list", "List configured services and observed state."),
    ActionSpec("service.status", "Show one service and verify its process identity."),
    ActionSpec("service.start", "Start one configured service in the background.",
               mutating=True),
    ActionSpec("service.run", "Run one configured service in the foreground.",
               mutating=True),
    ActionSpec("service.stop", "Stop one verified service process group.",
               mutating=True, destructive=True),
    ActionSpec("service.restart", "Restart one configured service.",
               mutating=True, destructive=True),
    ActionSpec("service.logs", "Read one bounded local service log."),
    ActionSpec("tunnel.list", "List configured tunnels and observed state."),
    ActionSpec("tunnel.status", "Show one configured tunnel."),
    ActionSpec("tunnel.start", "Open one configured live tunnel through its relay.",
               mutating=True, live_gated=True),
    ActionSpec("integration.status", "Show read-only OpenCode/Claude/Codex/Kiro paths."),
    ActionSpec("integration.install", "Install one integration through its installer.",
               mutating=True),
    ActionSpec("integration.uninstall", "Remove one integration's own entries.",
               mutating=True, destructive=True),
    ActionSpec("integration.verify", "Run a read-only installer check when one exists."),
)
_ACTIONS: dict[str, ActionSpec] = {spec.name: spec for spec in ACTION_SPECS}


@dataclass
class ActionContext:
    """Test and host seams. Production code supplies the landed managers."""

    manager_factory: Callable[..., Any] | None = None
    tunnel_manager_factory: Callable[..., Any] | None = None
    daemon_client_factory: Callable[..., Any] | None = None
    daemon_ensure: Callable[..., Any] | None = None
    relay_factory: Callable[[Any, RelayPolicy], Any] | None = None
    installer_runner: Callable[[str, list[str]], int] | None = None
    integration_check: Callable[[str, Path], dict[str, Any]] | None = None
    clock: Callable[[], datetime] | None = None
    token_factory: Callable[[], str] | None = None
    runtime_state_dir: str | Path | None = None


@dataclass(frozen=True)
class ActionRequest:
    root: Path
    action: str
    arguments: Mapping[str, Any] = field(default_factory=dict)
    confirmation: str | None = None


@dataclass
class ActionResult:
    action: str
    mode: str
    state: str
    ok: bool = True
    operation: str = ""
    manager_call: str | None = None
    argv: tuple[str, ...] = ()
    expected_effect: str = ""
    preconditions: tuple[str, ...] = ()
    risks: tuple[str, ...] = ()
    current: dict[str, Any] | None = None
    current_state: str = ""
    applicable: bool = True
    confirmation: str | None = None
    result: Any = None
    error: str | None = None
    live: bool = False
    gated: bool = False
    notes: tuple[str, ...] = ()
    scope: str | None = None
    targets: tuple[str, ...] = ()
    installer: str | None = None
    python: str | None = None
    audit: str | None = None
    audit_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "action": self.action,
            "mode": self.mode,
            "state": self.state,
            "ok": self.ok,
            "operation": self.operation,
            "manager_call": self.manager_call,
            "argv": list(self.argv),
            "expected_effect": self.expected_effect,
            "preconditions": list(self.preconditions),
            "risks": list(self.risks),
            "current": self.current,
            "current_state": self.current_state,
            "applicable": self.applicable,
            "confirmation": self.confirmation,
            "result": self.result,
            "error": self.error,
            "live": self.live,
            "gated": self.gated,
            "notes": list(self.notes),
            "scope": self.scope,
            "targets": list(self.targets),
            "installer": self.installer,
            "python": self.python,
            "audit": self.audit,
            "audit_error": self.audit_error,
        }

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.to_dict().get(key, default)


def list_actions() -> list[dict[str, Any]]:
    """The supported actions, for CLI/UI discovery."""
    return [
        {
            "name": spec.name,
            "description": spec.description,
            "mutating": spec.mutating,
            "destructive": spec.destructive,
            "live_gated": spec.live_gated,
        }
        for spec in ACTION_SPECS
    ]


# --------------------------------------------------------------------------- #
# request and token plumbing
# --------------------------------------------------------------------------- #
def _root_path(root: str | Path) -> Path:
    path = Path(root).expanduser().resolve()
    if not path.is_dir():
        raise ActionError(f"repository root is not a directory: {path}")
    return path


def _project(root: Path) -> ProjectConfig:
    try:
        return load_project_config(root)
    except (ConfigError, OSError) as exc:
        raise ActionError(f"project configuration could not be read: {exc}") from None


def _now(context: ActionContext) -> datetime:
    value = context.clock() if context.clock is not None else datetime.now(UTC)
    if not isinstance(value, datetime):
        raise ActionError("action clock returned an invalid time")
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _text_arg(value: Any, name: str, *, required: bool = False) -> str | None:
    if value is None:
        if required:
            raise ActionError(f"{name} is required")
        return None
    if not isinstance(value, str) or not value or not _NAME.fullmatch(value):
        raise ActionError(f"{name} must be non-empty text without control characters")
    return value


def _bool_arg(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise ActionError(f"{name} must be a boolean")
    return value


def _python_arg(value: Any) -> str:
    if value is None:
        value = sys.executable
    text = _text_arg(value, "python", required=True)
    assert text is not None
    return text


def _int_arg(value: Any, name: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ActionError(f"{name} must be an integer")
    if not minimum <= value <= maximum:
        raise ActionError(f"{name} must be between {minimum} and {maximum}")
    return value


def _float_arg(value: Any, name: str, *, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ActionError(f"{name} must be a number")
    number = float(value)
    if not math.isfinite(number) or not minimum <= number <= maximum:
        raise ActionError(f"{name} must be between {minimum:g} and {maximum:g}")
    return number


def _canonical(action: str, args: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(action, str) or action not in _ACTIONS:
        raise ActionError(f"unsupported action: {action!r}")
    if not isinstance(args, Mapping):
        raise ActionError("action arguments must be a mapping")
    allowed = {
        "daemon.status": set(),
        "daemon.ensure": {"wait_s", "idle_timeout_s"},
        "daemon.stop": set(),
        "daemon.restart": {"wait_s", "idle_timeout_s"},
        "service.list": set(),
        "service.status": {"name"},
        "service.start": {"name"},
        "service.run": {"name"},
        "service.stop": {"name"},
        "service.restart": {"name"},
        "service.logs": {"name", "max_bytes"},
        "tunnel.list": set(),
        "tunnel.status": {"name"},
        "tunnel.start": {"name", "live", "network"},
        "integration.status": {"name", "scope"},
        "integration.install": {"name", "scope", "python"},
        "integration.uninstall": {"name", "scope"},
        "integration.verify": {"name", "scope"},
    }[action]
    unknown = set(args) - allowed
    if unknown:
        raise ActionError(
            f"unsupported argument(s) for {action}: {', '.join(sorted(unknown))}")
    out: dict[str, Any] = {}
    if "name" in allowed:
        name = args.get("name")
        if action == "integration.status":
            if name is None:
                out["name"] = None
            elif isinstance(name, str) and name in _INTEGRATIONS:
                out["name"] = name
            else:
                raise ActionError(f"name must be one of {', '.join(_INTEGRATIONS)}")
        else:
            selected = _text_arg(name, "name", required=True)
            if action.startswith("integration.") and selected not in _INTEGRATIONS:
                raise ActionError(f"name must be one of {', '.join(_INTEGRATIONS)}")
            out["name"] = selected
    if action == "service.logs":
        out["max_bytes"] = _int_arg(
            args.get("max_bytes", DEFAULT_SERVICE_LOG_BYTES), "max_bytes",
            minimum=0, maximum=MAX_SERVICE_LOG_BYTES)
    if action in {"daemon.ensure", "daemon.restart"}:
        out["wait_s"] = _float_arg(
            args.get("wait_s", DEFAULT_WAIT_S), "wait_s", minimum=0.0, maximum=600.0)
        out["idle_timeout_s"] = _float_arg(
            args.get("idle_timeout_s", DEFAULT_IDLE_TIMEOUT_S), "idle_timeout_s",
            minimum=0.0, maximum=86_400.0)
    if action == "tunnel.start":
        out["live"] = _bool_arg(args.get("live", False), "live")
        out["network"] = _bool_arg(args.get("network", False), "network")
    if action.startswith("integration."):
        scope = args.get("scope", "project")
        if scope not in _SCOPES:
            raise ActionError(f"scope must be one of {', '.join(_SCOPES)}")
        out["scope"] = scope
        if action == "integration.install":
            if out.get("name") == "opencode":
                if args.get("python") is not None:
                    raise ActionError("python is not used by the opencode installer")
            else:
                out["python"] = _python_arg(args.get("python"))
    return out


def _token_dir(root: Path) -> Path:
    return state_path(root, ACTIONS_DIR, PENDING_DIR)


def _audit_path(root: Path) -> Path:
    return state_path(root, ACTIONS_DIR, AUDIT_FILE)


def _token_id(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]


def _prepare_actions_dir(root: Path) -> Path:
    directory = _token_dir(root)
    try:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(directory.parent, 0o700)
        os.chmod(directory, 0o700)
    except OSError:
        raise ActionError("could not prepare the actions state directory") from None
    return directory


def _prune_tokens(root: Path, context: ActionContext) -> None:
    directory = _token_dir(root)
    try:
        entries = list(directory.glob("*.json"))
    except OSError:
        return
    now = _now(context)
    kept: list[tuple[float, Path]] = []
    for path in entries:
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            value = {}
        expires = _parse_time(value.get("expires_at")) if isinstance(value, dict) else None
        if expires is None or expires <= now:
            try:
                path.unlink()
            except OSError:
                pass
            continue
        kept.append((mtime, path))
    if len(kept) >= MAX_PENDING_TOKENS:
        for _mtime, path in sorted(kept)[:len(kept) - MAX_PENDING_TOKENS + 1]:
            try:
                path.unlink()
            except OSError:
                pass


def _issue_token(root: Path, action: str, args: Mapping[str, Any],
                 context: ActionContext) -> str:
    directory = _prepare_actions_dir(root)
    _prune_tokens(root, context)
    now = _now(context)
    token = (context.token_factory() if context.token_factory is not None
             else secrets.token_urlsafe(24))
    if not isinstance(token, str) or not _TOKEN.fullmatch(token):
        raise ActionError("token factory returned an invalid token")
    payload = {
        "schema": SCHEMA,
        "token": token,
        "action": action,
        "arguments": dict(args),
        "root": str(root),
        "created_at": _iso(now),
        "expires_at": _iso(now + timedelta(seconds=TOKEN_TTL_S)),
    }
    path = directory / f"{token}.json"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
    except FileExistsError:
        raise ActionError("confirmation token collision; preview again") from None
    except OSError:
        raise ActionError("could not store the confirmation token") from None
    return token


def _load_token(root: Path, action: str, args: Mapping[str, Any],
                confirmation: str | None, context: ActionContext) -> str:
    if not isinstance(confirmation, str) or not confirmation:
        raise ActionError("--confirm TOKEN is required for apply")
    if not _TOKEN.fullmatch(confirmation):
        raise ActionRefused("confirmation token is malformed")
    path = _token_dir(root) / f"{confirmation}.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ActionRefused("confirmation token is unknown or was already used") from None
    except (OSError, ValueError):
        raise ActionRefused("confirmation token is unreadable") from None
    if not isinstance(value, dict):
        raise ActionRefused("confirmation token is invalid")
    expires = _parse_time(value.get("expires_at"))
    if expires is None:
        raise ActionRefused("confirmation token has no valid expiry")
    if expires <= _now(context):
        try:
            path.unlink()
        except OSError:
            pass
        raise ActionRefused("confirmation token has expired")
    if value.get("action") != action:
        raise ActionRefused("confirmation token is for a different action")
    if value.get("arguments") != dict(args):
        raise ActionRefused("confirmation token is for different arguments")
    if value.get("root") != str(root):
        raise ActionRefused("confirmation token is for a different repository")
    return confirmation


def _consume_token(root: Path, confirmation: str) -> None:
    path = _token_dir(root) / f"{confirmation}.json"
    discard = path.with_name(
        f"{path.name}.{os.getpid()}.{secrets.token_hex(4)}.used")
    try:
        os.rename(path, discard)
    except FileNotFoundError:
        raise ActionRefused("confirmation token was already used") from None
    except OSError:
        raise ActionRefused("confirmation token could not be consumed") from None
    finally:
        try:
            discard.unlink()
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# audit
# --------------------------------------------------------------------------- #
def _safe_error(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).replace("\x00", " ").strip()
    text = _SECRET_TEXT.sub(r"\1\2[redacted]", text)
    return text[:MAX_ERROR_CHARS] or None


def _append_audit(root: Path, result: ActionResult, args: Mapping[str, Any],
                  confirmation: str | None, context: ActionContext) -> str:
    path = _audit_path(root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(path.parent, 0o700)
        lines: list[str] = []
        if path.exists():
            lines = path.read_text(encoding="utf-8").splitlines()
        record = {
            "schema": SCHEMA,
            "timestamp": _iso(_now(context)),
            "action": result.action,
            "result": {
                "state": result.state,
                "ok": bool(result.ok),
                "error": _safe_error(result.error),
            },
            "target": args.get("name"),
            "live": bool(args.get("live", False)),
            "network": bool(args.get("network", False)),
            "token": _token_id(confirmation) if confirmation else None,
        }
        if args.get("scope") is not None:
            record["scope"] = args["scope"]
        lines.append(json.dumps(record, sort_keys=True, separators=(",", ":"),
                                ensure_ascii=True))
        lines = lines[-MAX_AUDIT_RECORDS:]
        while len(lines) > 1 and sum(len(line.encode("utf-8")) + 1 for line in lines) > MAX_AUDIT_BYTES:
            lines.pop(0)
        fd, temp_name = tempfile.mkstemp(prefix=".audit-", dir=path.parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                fd = -1
                stream.write("\n".join(lines) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_name, path)
            os.chmod(path, 0o600)
        finally:
            if fd >= 0:
                os.close(fd)
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass
    except OSError:
        raise
    return str(path)


# --------------------------------------------------------------------------- #
# shared observations
# --------------------------------------------------------------------------- #
def _runtime_state_dir(root: Path, context: ActionContext) -> Path:
    if context.runtime_state_dir is not None:
        return Path(context.runtime_state_dir).expanduser().resolve()
    return state_path(root, "runtime")


def _manager(config: ProjectConfig, root: Path, context: ActionContext) -> Any:
    try:
        if context.manager_factory is not None:
            return context.manager_factory(config, root=root)
        return ServiceManager(config, state_dir=_runtime_state_dir(root, context))
    except RuntimeStateError:
        raise ActionError("runtime state could not be read") from None


def _tunnel_manager(config: ProjectConfig, root: Path,
                    context: ActionContext) -> Any:
    try:
        if context.tunnel_manager_factory is not None:
            return context.tunnel_manager_factory(config, root=root)
        return TunnelManager(config, state_dir=_runtime_state_dir(root, context))
    except RuntimeStateError:
        raise ActionError("runtime state could not be read") from None


def _service_observation(
    root: Path, name: str, context: ActionContext,
) -> tuple[ProjectConfig, dict[str, Any] | None]:
    config = _project(root)
    if not any(service.name == name for service in config.runtime_services):
        return config, None
    try:
        payload = runtime_status(config, name=name,
                                 state_dir=_runtime_state_dir(root, context))
    except RuntimeStateError:
        raise ActionError("runtime state could not be read") from None
    except RuntimeErrorBase:
        return config, None
    rows = payload.get("services") or []
    return config, dict(rows[0]) if rows else None


def _tunnel_observation(
    root: Path, name: str, context: ActionContext,
) -> tuple[ProjectConfig, dict[str, Any] | None]:
    config = _project(root)
    if not any(tunnel.name == name for tunnel in config.runtime_tunnels):
        return config, None
    try:
        payload = runtime_status(config, name=name,
                                 state_dir=_runtime_state_dir(root, context))
    except RuntimeStateError:
        raise ActionError("runtime state could not be read") from None
    except RuntimeErrorBase:
        return config, None
    rows = payload.get("tunnels") or []
    return config, dict(rows[0]) if rows else None


def _absent(name: str, kind: str) -> dict[str, Any]:
    return {"name": name, "kind": kind, "state": "absent", "configured": False,
            "running": False}


def _service_config(config: ProjectConfig, name: str) -> Any | None:
    return next((item for item in config.runtime_services if item.name == name), None)


def _tunnel_config(config: ProjectConfig, name: str) -> Any | None:
    return next((item for item in config.runtime_tunnels if item.name == name), None)


def _relay_config(config: ProjectConfig, name: str) -> Any | None:
    return next((item for item in config.relays if item.name == name), None)


def _daemon_client(context: ActionContext, sock: str,
                   timeout: float) -> Any:
    factory = context.daemon_client_factory or DaemonClient
    return factory(sock, timeout=timeout)


def _daemon_probe(root: Path, context: ActionContext,
                  timeout: float = DEFAULT_DAEMON_TIMEOUT_S) -> dict[str, Any]:
    try:
        sock = str(protocol.socket_path(root))
    except (OSError, ValueError) as exc:
        return {"state": "unknown", "socket": None, "error": _safe_error(exc)}
    try:
        with _daemon_client(context, sock, timeout) as client:
            reply = client.ping(timeout=timeout)
    except DaemonUnavailable as exc:
        return {"state": "absent", "socket": sock, "error": _safe_error(exc)}
    except DaemonTimeout as exc:
        return {"state": "unknown", "socket": sock, "error": _safe_error(exc)}
    except DaemonError as exc:
        return {"state": "unknown", "socket": sock, "error": _safe_error(exc)}
    except OSError as exc:
        return {"state": "unknown", "socket": sock, "error": _safe_error(exc)}
    if not isinstance(reply, dict) or reply.get("ok") is False:
        return {"state": "unknown", "socket": sock,
                "error": _safe_error((reply or {}).get("error") or "daemon ping failed")}
    return {
        "state": "running",
        "socket": sock,
        "pid": reply.get("pid"),
        "version": reply.get("version"),
        "root": reply.get("root"),
    }


def _daemon_ensure(root: Path, args: Mapping[str, Any],
                   context: ActionContext) -> dict[str, Any]:
    ensure = context.daemon_ensure
    if ensure is None:
        from ozagia.daemon import autostart
        ensure = autostart.ensure
    try:
        return dict(ensure(
            str(root), wait_s=args["wait_s"],
            idle_timeout_s=args["idle_timeout_s"]))
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        return {"ok": False, "started": False, "reason": _safe_error(exc)}


def _result(action: str, *, mode: str, state: str, ok: bool = True,
            current_state: str = "", **fields: Any) -> ActionResult:
    return ActionResult(action=action, mode=mode, state=state, ok=ok,
                        current_state=current_state, **fields)


def _preview(action: str, *, operation: str, expected_effect: str,
             manager_call: str | None = None, argv: tuple[str, ...] = (),
             preconditions: tuple[str, ...] = (), risks: tuple[str, ...] = (),
             current: dict[str, Any] | None = None,
             current_state: str = "", applicable: bool = True,
             live: bool = False, gated: bool = False,
             notes: tuple[str, ...] = (), **fields: Any) -> ActionResult:
    return _result(
        action, mode="preview", state="preview", operation=operation,
        expected_effect=expected_effect, manager_call=manager_call, argv=argv,
        preconditions=preconditions, risks=risks, current=current,
        current_state=current_state, applicable=applicable, live=live,
        gated=gated, notes=notes, **fields)


def _applied(action: str, *, current_state: str, expected_effect: str,
             result: Any = None, manager_call: str | None = None,
             argv: tuple[str, ...] = (), live: bool = False,
             notes: tuple[str, ...] = (), **fields: Any) -> ActionResult:
    return _result(
        action, mode="apply", state="applied", ok=True,
        current_state=current_state, expected_effect=expected_effect,
        result=result, manager_call=manager_call, argv=argv, live=live,
        notes=notes, **fields)


def _noop(action: str, *, current_state: str, message: str,
          result: Any = None, **fields: Any) -> ActionResult:
    return _applied(action, current_state=current_state,
                    expected_effect=f"none ({message})", result=result,
                    **fields)


def _refused(action: str, *, current_state: str, error: str,
             result: Any = None, **fields: Any) -> ActionResult:
    return _result(action, mode="apply", state="refused", ok=False,
                   current_state=current_state, error=_safe_error(error),
                   result=result, **fields)


def _absent_result(action: str, *, name: str | None, error: str | None,
                   ok: bool = True) -> ActionResult:
    return _result(
        action, mode="apply", state="absent", ok=ok, current_state="absent",
        result={"name": name, "state": "absent"}, error=_safe_error(error))


# --------------------------------------------------------------------------- #
# service actions
# --------------------------------------------------------------------------- #
def _preview_service(root: Path, action: str, args: Mapping[str, Any],
                     context: ActionContext) -> ActionResult:
    config = _project(root)
    if action == "service.list":
        try:
            payload = runtime_status(config, state_dir=_runtime_state_dir(root, context))
        except (RuntimeErrorBase, OSError) as exc:
            raise ActionError(f"runtime state could not be read: {exc}") from None
        return _preview(
            action,
            operation="list configured services and observed local state",
            expected_effect="none (read-only)",
            manager_call="ServiceManager.list()",
            preconditions=("project configuration is readable",),
            risks=("state is observational; a persisted PID is verified before it is trusted",),
            current={"services": payload.get("services") or []},
            current_state="configured",
            notes=("a configured service is not evidence that it is running",))
    name = args["name"]
    config, row = _service_observation(root, name, context)
    service = _service_config(config, name)
    if service is None:
        return _preview(
            action, operation=f"{action} for service {name!r}",
            expected_effect="none (service is not configured)",
            manager_call=f"ServiceManager.{action.split('.', 1)[1]}({name!r})"
            if action != "service.logs" else f"ServiceManager.logs({name!r})",
            preconditions=(f"service {name!r} is configured",),
            risks=("no process will be started or signalled",),
            current=_absent(name, "service"), current_state="absent",
            applicable=False)
    row = row if row is not None else _absent(name, "service")
    state = str(row.get("state") or "unknown")
    argv = tuple(service.command) if action in {
        "service.start", "service.run", "service.restart"} else ()
    if action == "service.status":
        return _preview(
            action, operation=f"read service {name!r} status and verify its process identity",
            expected_effect="none (read-only)",
            manager_call=f"ServiceManager.status({name!r})",
            preconditions=(f"service {name!r} is configured",),
            risks=("a recorded PID is trusted only while its start token and group match",),
            current=row, current_state=state)
    if action == "service.logs":
        return _preview(
            action, operation=f"read at most {args['max_bytes']} bytes of service {name!r} log",
            expected_effect="none (read-only)",
            manager_call=f"ServiceManager.logs({name!r}, max_bytes={args['max_bytes']})",
            preconditions=(f"service {name!r} is configured",),
            risks=("logs can contain application data and are returned only to the caller",),
            current=row, current_state=state)
    if action in {"service.start", "service.run"}:
        busy = state in {"running", "starting"}
        unverified = state in {"stale", "unknown"}
        mode_blocked = (
            (action == "service.start" and service.mode != "background")
            or service.relay is not None)
        applicable = not busy and not unverified and not mode_blocked
        mode = "background" if action == "service.start" else "foreground"
        notes: tuple[str, ...] = ()
        if unverified:
            notes = ("stale or unknown process identities are refused, never reused",)
        elif mode_blocked:
            notes = ("service is not configured for this mode or uses a relay",)
        return _preview(
            action,
            operation=f"{action.split('.', 1)[1]} service {name!r} in the {mode}",
            expected_effect=(
                "start the configured argv in a new process group and record it only "
                "after PID, start-token and group verification"
                if action == "service.start" else
                "run the configured argv in the foreground and return its bounded output"),
            manager_call=f"ServiceManager.{action.split('.', 1)[1]}({name!r})",
            argv=argv,
            preconditions=(
                f"service {name!r} is configured",
                "no active or unverified runtime record for this service",
                "background mode and no relay for service.start"
                if action == "service.start" else "no relay for service.run",
            ),
            risks=(
                "the configured command runs directly, without a shell",
                "a background process can outlive this preview until it is stopped",
                "foreground run blocks until the process exits" if action == "service.run"
                else "service output is recorded only in the bounded runtime log",
            ),
            current=row, current_state=state, applicable=applicable,
            notes=notes)
    if action == "service.stop":
        applicable = state in {"running", "starting"}
        return _preview(
            action,
            operation=f"stop the verified process group for service {name!r}",
            expected_effect=(
                "send SIGTERM to the verified process group, then SIGKILL after the "
                "manager's grace period if it is still verified"),
            manager_call=f"ServiceManager.stop({name!r})",
            preconditions=(
                f"service {name!r} has a running runtime record",
                "PID, start token and process group are verified",
            ),
            risks=(
                "signals the whole process group",
                "an unverified PID is refused instead of signalled",
            ),
            current=row, current_state=state, applicable=applicable,
            notes=("not running; stop would be a no-op",) if not applicable else ())
    applicable = (state not in {"stale", "unknown"}
                  and service.mode == "background" and service.relay is None)
    return _preview(
        action,
        operation=f"restart service {name!r}",
        expected_effect="stop a running verified process group, then start the configured argv",
        manager_call=f"ServiceManager.restart({name!r})",
        argv=argv,
        preconditions=(
            f"service {name!r} is configured",
            "background mode and no relay",
            "the prior process (if any) is verified and stops cleanly",
        ),
        risks=(
            "restart stops the current process group before starting again",
            "an unverified process is refused",
        ),
        current=row, current_state=state, applicable=applicable,
        notes=("service is not configured for background restart",)
        if not applicable and state not in {"stale", "unknown"} else ())


def _service_rows(result: Any) -> list[dict[str, Any]]:
    return [dict(row) for row in (result or []) if isinstance(row, Mapping)]


def _apply_service(root: Path, action: str, args: Mapping[str, Any],
                   context: ActionContext, consume: Callable[[], None]) -> ActionResult:
    config = _project(root)
    name = args.get("name")
    if action == "service.list":
        manager = _manager(config, root, context)
        consume()
        try:
            rows = manager.list()
        except (RuntimeErrorBase, OSError) as exc:
            return _refused(action, current_state="unknown", error=str(exc))
        return _applied(action, current_state="configured",
                        expected_effect="none (read-only)",
                        result={"services": _service_rows(rows)},
                        manager_call="ServiceManager.list()")
    assert isinstance(name, str)
    service = _service_config(config, name)
    if service is None:
        consume()
        return _absent_result(
            action, name=name,
            error=f"service {name!r} is not configured",
            ok=action in {"service.status", "service.logs", "service.stop"})
    manager = _manager(config, root, context)
    try:
        current = manager.status(name)
    except (RuntimeErrorBase, OSError) as exc:
        consume()
        if action in {"service.status", "service.stop", "service.logs"}:
            return _absent_result(action, name=name, error=str(exc))
        return _refused(action, current_state="absent", error=str(exc))
    state = str(current.get("state") or "unknown")
    if action == "service.status":
        consume()
        return _applied(action, current_state=state,
                        expected_effect="none (read-only)", result=current,
                        manager_call=f"ServiceManager.status({name!r})")
    if action == "service.logs":
        consume()
        try:
            logs = manager.logs(name, max_bytes=args["max_bytes"])
        except (RuntimeErrorBase, OSError) as exc:
            return _refused(action, current_state=state, error=str(exc))
        return _applied(
            action, current_state=state, expected_effect="none (read-only)",
            result={"name": name, "max_bytes": args["max_bytes"], "logs": logs},
            manager_call=(f"ServiceManager.logs({name!r}, "
                          f"max_bytes={args['max_bytes']})"))
    if action in {"service.start", "service.run"}:
        if state in {"running", "starting"}:
            consume()
            return _noop(action, current_state=state,
                         message="service is already active", result=current)
        if state in {"stale", "unknown"}:
            consume()
            return _refused(
                action, current_state=state,
                error="service has an unverified process record; refusing to start")
        if ((action == "service.start" and service.mode != "background")
                or service.relay is not None):
            consume()
            return _refused(
                action, current_state=state,
                error="service is not configured for this mode or uses a relay")
        consume()
        try:
            result = (manager.start(name) if action == "service.start"
                      else manager.run(name))
        except (RuntimeErrorBase, OSError) as exc:
            return _refused(action, current_state=state, error=str(exc))
        return _applied(
            action, current_state=str(result.get("state") or "unknown"),
            expected_effect="start the configured argv through the existing manager",
            result=result,
            manager_call=f"ServiceManager.{action.split('.', 1)[1]}({name!r})",
            argv=tuple(service.command))
    if action == "service.stop":
        if state in {"configured", "stopped", "exited", "failed"}:
            consume()
            return _noop(action, current_state=state,
                         message="service is not running", result=current)
        if state in {"stale", "unknown"}:
            consume()
            return _refused(
                action, current_state=state,
                error="process identity is unverified; refusing to signal")
        consume()
        try:
            result = manager.stop(name)
        except (RuntimeErrorBase, OSError) as exc:
            return _refused(action, current_state=state, error=str(exc))
        return _applied(
            action, current_state=str(result.get("state") or "unknown"),
            expected_effect="stop the verified process group through the existing manager",
            result=result, manager_call=f"ServiceManager.stop({name!r})")
    if state in {"stale", "unknown"}:
        consume()
        return _refused(
            action, current_state=state,
            error="process identity is unverified; restart is refused")
    if service.mode != "background" or service.relay is not None:
        consume()
        return _refused(
            action, current_state=state,
            error="service is not configured for background restart")
    consume()
    try:
        result = manager.restart(name)
    except (RuntimeErrorBase, OSError) as exc:
        return _refused(action, current_state=state, error=str(exc))
    return _applied(
        action, current_state=str(result.get("state") or "unknown"),
        expected_effect="stop if running, then start through the existing manager",
        result=result, manager_call=f"ServiceManager.restart({name!r})",
        argv=tuple(service.command))


# --------------------------------------------------------------------------- #
# daemon actions
# --------------------------------------------------------------------------- #
def _preview_daemon(root: Path, action: str, args: Mapping[str, Any],
                    context: ActionContext) -> ActionResult:
    observation = _daemon_probe(root, context)
    state = str(observation.get("state") or "unknown")
    if action == "daemon.status":
        return _preview(
            action, operation="ping the graph daemon socket",
            expected_effect="none (read-only)",
            manager_call="DaemonClient(socket_path(root)).ping()",
            preconditions=("daemon socket path can be resolved",),
            risks=("a missing daemon is reported as absent, not as an error",),
            current=observation, current_state=state)
    if action == "daemon.ensure":
        call = (f"ozagia.daemon.autostart.ensure({str(root)!r}, "
                f"wait_s={args['wait_s']:g}, "
                f"idle_timeout_s={args['idle_timeout_s']:g})")
        return _preview(
            action,
            operation="ensure a graph daemon answers; start one only if absent",
            expected_effect=(
                "none (a daemon already answers)" if state == "running"
                else "start `python -m ozagia.daemon serve` detached and wait for ping"),
            manager_call=call,
            preconditions=("no daemon answers on the configured socket",),
            risks=(
                "starting loads the repository graph and can take seconds",
                "the daemon takes a lock and exits if another start wins",
            ),
            current=observation, current_state=state,
            applicable=state == "absent",
            notes=("unknown daemon state is not treated as absent",))
    if action == "daemon.stop":
        return _preview(
            action,
            operation="shut down the graph daemon",
            expected_effect=(
                "none (no daemon answers)" if state == "absent"
                else "send `shutdown` on the daemon socket"),
            manager_call="DaemonClient(socket_path(root)).shutdown()",
            preconditions=("daemon answers a ping",),
            risks=(
                "hooks and MCP clients lose the daemon until it is ensured again",
                "a daemon that does not answer is not signalled",
            ),
            current=observation, current_state=state,
            applicable=state == "running")
    return _preview(
        action,
        operation="restart the graph daemon",
        expected_effect=(
            "start a daemon (none is running)" if state == "absent"
            else "shut down the running daemon, then ensure one answers"),
        manager_call=(
            "DaemonClient(socket_path(root)).shutdown(); "
            f"ozagia.daemon.autostart.ensure({str(root)!r}, "
            f"wait_s={args['wait_s']:g}, "
            f"idle_timeout_s={args['idle_timeout_s']:g})"),
        preconditions=("daemon state is known (running or absent)",),
        risks=(
            "restart interrupts in-flight daemon requests",
            "the ensure step uses the autostart lock and wait policy",
        ),
        current=observation, current_state=state,
        applicable=state in {"running", "absent"})


def _apply_daemon(root: Path, action: str, args: Mapping[str, Any],
                  context: ActionContext, consume: Callable[[], None]) -> ActionResult:
    observation = _daemon_probe(root, context)
    state = str(observation.get("state") or "unknown")
    if action == "daemon.status":
        consume()
        return _applied(action, current_state=state,
                        expected_effect="none (read-only)", result=observation,
                        manager_call="DaemonClient(socket_path(root)).ping()")
    if action == "daemon.ensure":
        if state == "running":
            consume()
            return _noop(action, current_state=state,
                         message="daemon is already running", result=observation)
        if state != "absent":
            consume()
            return _refused(action, current_state=state,
                            error="daemon state is unknown; refusing to start another")
        consume()
        result = _daemon_ensure(root, args, context)
        if not result.get("ok"):
            return _result(
                action, mode="apply", state="failed", ok=False,
                current_state="unknown", result=result,
                error=_safe_error(result.get("reason") or "daemon did not answer"),
                manager_call="ozagia.daemon.autostart.ensure")
        return _applied(
            action, current_state="running",
            expected_effect="start the daemon only if none answered",
            result=result, manager_call="ozagia.daemon.autostart.ensure")
    if action == "daemon.stop":
        if state == "absent":
            consume()
            return _absent_result(action, name="daemon", error=None)
        if state != "running":
            consume()
            return _refused(action, current_state=state,
                            error="daemon state is unknown; refusing to shut it down")
        consume()
        try:
            with _daemon_client(context, str(observation["socket"]),
                                DEFAULT_DAEMON_TIMEOUT_S) as client:
                reply = client.shutdown()
        except (DaemonError, OSError) as exc:
            return _refused(action, current_state=state, error=str(exc))
        if not isinstance(reply, dict) or reply.get("ok") is False:
            return _result(
                action, mode="apply", state="failed", ok=False,
                current_state=state, result={"reply": reply},
                error=_safe_error((reply or {}).get("error") or "daemon shutdown failed"))
        return _applied(
            action, current_state="stopped",
            expected_effect="shut down the graph daemon",
            result={"ok": True}, manager_call="DaemonClient.shutdown()")
    if state == "unknown":
        consume()
        return _refused(action, current_state=state,
                        error="daemon state is unknown; refusing to restart")
    consume()
    if state == "running":
        try:
            with _daemon_client(context, str(observation["socket"]),
                                DEFAULT_DAEMON_TIMEOUT_S) as client:
                reply = client.shutdown()
        except (DaemonError, OSError) as exc:
            return _refused(action, current_state=state, error=str(exc))
        if not isinstance(reply, dict) or reply.get("ok") is False:
            return _result(
                action, mode="apply", state="failed", ok=False,
                current_state=state, result={"reply": reply},
                error=_safe_error((reply or {}).get("error") or "daemon shutdown failed"))
    result = _daemon_ensure(root, args, context)
    if not result.get("ok"):
        return _result(
            action, mode="apply", state="failed", ok=False,
            current_state="unknown", result=result,
            error=_safe_error(result.get("reason") or "daemon did not answer"),
            manager_call="ozagia.daemon.autostart.ensure")
    return _applied(
        action, current_state="running",
        expected_effect="shut down the prior daemon, then ensure one answers",
        result=result, manager_call="DaemonClient.shutdown(); autostart.ensure")


# --------------------------------------------------------------------------- #
# tunnel actions
# --------------------------------------------------------------------------- #
def _planned_tunnel_argv(config: ProjectConfig, tunnel: Any,
                         live: bool, network: bool,
                         context: ActionContext) -> tuple[tuple[str, ...], str | None]:
    relay_config = _relay_config(config, tunnel.relay)
    if relay_config is None:
        return (), "tunnel relay is not configured"
    policy = RelayPolicy(
        allowed_kinds=frozenset(relay_config.allowed_kinds),
        allow_live=live,
        allow_network=network,
        max_timeout_s=relay_config.max_timeout_s,
        command_allowlist=relay_config.command_allowlist,
        max_output_bytes=relay_config.max_output_bytes,
    )
    relay: Any = None
    lease: Any = None
    try:
        if context.relay_factory is not None:
            relay = context.relay_factory(relay_config, policy)
        else:
            specs = [
                RelaySpec(spec.kind, dict(spec.options), name=spec.name)
                for spec in relay_config.specs
            ]
            relay = Relay.from_specs(specs, policy=policy, ephemeral=True)
        host, port_text = tunnel.target.rsplit(":", 1)
        lease = relay.open_tunnel(
            TunnelTarget(host, int(port_text)),
            local_port=tunnel.local_port,
            timeout_s=min(relay_config.max_timeout_s, 30.0),
            dry_run=True,
        )
        return tuple(getattr(lease, "planned_argv", ()) or ()), None
    except Exception as exc:  # noqa: BLE001 - preview reports a safe planning failure
        return (), _safe_error(exc) or "tunnel command plan is unavailable"
    finally:
        if lease is not None:
            try:
                lease.close()
            except Exception:  # noqa: BLE001 - best-effort cleanup
                pass
        if relay is not None:
            try:
                relay.close()
            except Exception:  # noqa: BLE001 - best-effort cleanup
                pass


def _preview_tunnel(root: Path, action: str, args: Mapping[str, Any],
                    context: ActionContext) -> ActionResult:
    config = _project(root)
    if action == "tunnel.list":
        try:
            payload = runtime_status(config, state_dir=_runtime_state_dir(root, context))
        except (RuntimeErrorBase, OSError) as exc:
            raise ActionError(f"runtime state could not be read: {exc}") from None
        return _preview(
            action, operation="list configured tunnels and observed local state",
            expected_effect="none (read-only)",
            manager_call="TunnelManager.list()",
            preconditions=("project configuration is readable",),
            risks=("a configured tunnel is not connected and never reported as live",),
            current={"tunnels": payload.get("tunnels") or []},
            current_state="configured",
            notes=("a persisted lease not held by this process is stale",))
    name = args["name"]
    config, row = _tunnel_observation(root, name, context)
    tunnel = _tunnel_config(config, name)
    if tunnel is None:
        return _preview(
            action, operation=f"{action} for tunnel {name!r}",
            expected_effect="none (tunnel is not configured)",
            manager_call=f"TunnelManager.{action.split('.', 1)[1]}({name!r})",
            preconditions=(f"tunnel {name!r} is configured",),
            risks=("no tunnel will be opened",),
            current=_absent(name, "tunnel"), current_state="absent",
            applicable=False)
    row = row if row is not None else _absent(name, "tunnel")
    state = str(row.get("state") or "unknown")
    if action == "tunnel.status":
        return _preview(
            action, operation=f"read tunnel {name!r} status",
            expected_effect="none (read-only)",
            manager_call=f"TunnelManager.status({name!r})",
            preconditions=(f"tunnel {name!r} is configured",),
            risks=("a persisted lease not held by this process is reported stale",),
            current=row, current_state=state)
    argv, planning_error = _planned_tunnel_argv(
        config, tunnel, args["live"], args["network"], context)
    gates = args["live"] and args["network"]
    applicable = gates and state not in {"running", "starting", "stale", "unknown"}
    return _preview(
        action,
        operation=f"open live tunnel {name!r} through relay {tunnel.relay!r}",
        expected_effect=(
            "open a loopback listener and forward to the configured target; "
            "the tunnel is held by this process until stopped"),
        manager_call=(f"TunnelManager.start({name!r}, allow_live=True, "
                      f"allow_network=True)"),
        argv=argv,
        preconditions=(
            f"tunnel {name!r} is configured",
            "the relay/target policy allows live and network execution",
            "no active or unverified tunnel lease exists",
            "--allow-live and --allow-network are both explicit",
        ),
        risks=(
            "opens a live network connection and a local listener",
            "the target and relay may be remote; the allowlist is the only gate",
            "an unverified lease is reported stale and cannot be claimed",
        ),
        current=row, current_state=state, applicable=applicable,
        live=True, gated=True,
        notes=((f"command planning failed: {planning_error}",) if planning_error else ()))


def _apply_tunnel(root: Path, action: str, args: Mapping[str, Any],
                  context: ActionContext, consume: Callable[[], None]) -> ActionResult:
    if action == "tunnel.list":
        config = _project(root)
        manager = _tunnel_manager(config, root, context)
        consume()
        try:
            rows = manager.list()
        except (RuntimeErrorBase, OSError) as exc:
            return _refused(action, current_state="unknown", error=str(exc))
        return _applied(action, current_state="configured",
                        expected_effect="none (read-only)",
                        result={"tunnels": _service_rows(rows)},
                        manager_call="TunnelManager.list()")
    name = args["name"]
    config = _project(root)
    tunnel = _tunnel_config(config, name)
    if tunnel is None:
        consume()
        return _absent_result(action, name=name,
                              error=f"tunnel {name!r} is not configured", ok=False)
    if action == "tunnel.status":
        manager = _tunnel_manager(config, root, context)
        consume()
        try:
            row = manager.status(name)
        except (RuntimeErrorBase, OSError) as exc:
            return _absent_result(action, name=name, error=str(exc))
        return _applied(action, current_state=str(row.get("state") or "unknown"),
                        expected_effect="none (read-only)", result=row,
                        manager_call=f"TunnelManager.status({name!r})")
    if not args["live"] or not args["network"]:
        consume()
        return _refused(
            action, current_state="configured",
            error="tunnel start requires both --allow-live and --allow-network")
    manager = _tunnel_manager(config, root, context)
    try:
        current = manager.status(name)
    except (RuntimeErrorBase, OSError) as exc:
        consume()
        return _refused(action, current_state="unknown", error=str(exc))
    state = str(current.get("state") or "unknown")
    if state in {"running", "starting", "stale", "unknown"}:
        consume()
        return _refused(
            action, current_state=state,
            error="tunnel has an active or unverified lease; refusing to open another")
    consume()
    try:
        row = manager.start(name, allow_live=True, allow_network=True)
    except (RuntimeErrorBase, OSError, ValueError) as exc:
        return _refused(action, current_state=state, error=str(exc))
    return _applied(
        action, current_state=str(row.get("state") or "unknown"),
        expected_effect="open and hold the configured live tunnel",
        result=row,
        manager_call=(f"TunnelManager.start({name!r}, allow_live=True, "
                      f"allow_network=True)"), live=True)


# --------------------------------------------------------------------------- #
# integration status
# --------------------------------------------------------------------------- #
def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _component(name: str, path: Path, installed: bool) -> dict[str, Any]:
    return {"name": name, "path": str(path), "present": path.is_file(),
            "installed": installed}


def _opencode_status(root: Path) -> dict[str, Any]:
    from ozagia.integrations.opencode import install as opener
    server = opener.plugin_path(root)
    brand = opener.tui_brand_path(root)
    components = [
        _component("server plugin", server, server.is_file()),
        _component("tui brand plugin", brand, brand.is_file()),
    ]
    installed = sum(1 for item in components if item["installed"])
    state = ("installed" if installed == len(components)
             else "partial" if installed else "absent")
    return {
        "name": "opencode", "scope": "project", "state": state,
        "components": components,
        "trust": ("OpenCode loads project plugins from .opencode/plugins; "
                  "restart an already-running OpenCode after install."),
    }


def _claude_status(root: Path, scope: str) -> dict[str, Any]:
    from ozagia.integrations.claude_code import install as claude
    components = []
    for settings in ("local", "project"):
        path = claude.settings_path(root, settings)
        text = _read_text(path)
        components.append(_component(
            f"{settings} settings", path, claude.HOOK_SUFFIX in text))
    if scope == "user":
        return {"name": "claude", "scope": scope, "state": "absent",
                "components": [], "trust": "no user-scope install path is defined"}
    installed = any(item["installed"] for item in components)
    return {
        "name": "claude", "scope": "project",
        "state": "installed" if installed else "absent",
        "components": components,
        "trust": ("Claude Code asks before running project hooks; keep the "
                  "personal settings file out of version control."),
    }


def _codex_status(root: Path, scope: str) -> dict[str, Any]:
    from ozagia.integrations.codex import install as codex
    paths = codex.paths(root, scope)
    hooks = _read_text(paths["hooks"])
    config = _read_text(paths["config"])
    agents = _read_text(paths["agents"])
    components = [
        _component("hooks", paths["hooks"], codex.HOOK_SUFFIX in hooks),
        _component("mcp config", paths["config"],
                   f"[mcp_servers.{codex.MCP_NAME}" in config),
        _component("AGENTS.md section", paths["agents"], codex.BEGIN in agents),
    ]
    installed = sum(1 for item in components if item["installed"])
    state = ("installed" if installed == len(components)
             else "partial" if installed else "absent")
    return {
        "name": "codex", "scope": scope, "state": state,
        "components": components, "trust": codex.TRUST_STEPS,
    }


def _kiro_status(root: Path, scope: str) -> dict[str, Any]:
    from ozagia.integrations.kiro import install as kiro
    paths = kiro.paths(root, scope)
    hooks = _read_text(paths["hooks"])
    agent = _read_text(paths["agent"])
    mcp = _read_text(paths["mcp"])
    components = [
        _component("hook file", paths["hooks"], kiro.HOOK_SUFFIX in hooks),
        _component("agent profile", paths["agent"], kiro.HOOK_SUFFIX in agent),
        _component("mcp config", paths["mcp"],
                   f'"{kiro.MCP_NAME}"' in mcp),
    ]
    installed = sum(1 for item in components if item["installed"])
    state = ("installed" if installed == len(components)
             else "partial" if installed else "absent")
    return {
        "name": "kiro", "scope": scope, "state": state,
        "components": components, "trust": kiro.NEXT_STEPS,
    }


def _integration_report(root: Path, name: str | None,
                        scope: str) -> list[dict[str, Any]]:
    request = [name] if name else list(_INTEGRATIONS)
    rows: list[dict[str, Any]] = []
    for item in request:
        if item == "opencode":
            rows.append(_opencode_status(root))
        elif item == "claude":
            rows.append(_claude_status(root, scope))
        elif item == "codex":
            rows.append(_codex_status(root, scope))
        else:
            rows.append(_kiro_status(root, scope))
    return rows


def _preview_integration(root: Path, action: str, args: Mapping[str, Any],
                         context: ActionContext) -> ActionResult:
    reports = _integration_report(root, args["name"], args["scope"])
    return _preview(
        action,
        operation="inspect integration install paths without writing",
        expected_effect="none (read-only)",
        manager_call=None,
        preconditions=("integration path inspection only",),
        risks=("paths outside the repository may be shown for user-scope installs",),
        current={"integrations": reports},
        current_state=reports[0]["state"] if len(reports) == 1 else "inspected",
        scope=args.get("scope"))


def _apply_integration(root: Path, action: str, args: Mapping[str, Any],
                       context: ActionContext, consume: Callable[[], None]) -> ActionResult:
    reports = _integration_report(root, args["name"], args["scope"])
    consume()
    return _applied(
        action, current_state=reports[0]["state"] if len(reports) == 1 else "inspected",
        expected_effect="none (read-only)", result={"integrations": reports},
        scope=args.get("scope"))


# --------------------------------------------------------------------------- #
# integration install, uninstall and verify
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class _IntegrationPlan:
    name: str
    scope: str
    module: str
    arguments: tuple[str, ...]
    argv: tuple[str, ...]
    targets: tuple[str, ...]
    python: str | None
    supported: bool
    reason: str


def _integration_targets(name: str, root: Path, scope: str,
                         action: str) -> tuple[str, ...]:
    """The files this action would read or write, using installer path helpers."""
    try:
        if name == "opencode":
            from ozagia.integrations.opencode import install as opener
            paths = [opener.plugin_path(root)]
            if action != "verify":
                paths.append(opener.tui_brand_path(root))
            return tuple(str(path) for path in paths)
        if name == "claude":
            from ozagia.integrations.claude_code import install as claude
            settings = ("local",) if action == "install" else ("local", "project")
            return tuple(str(claude.settings_path(root, item)) for item in settings)
        if name == "codex":
            from ozagia.integrations.codex import install as codex
            paths = codex.paths(root, scope)
            return tuple(str(paths[key]) for key in ("hooks", "config", "agents"))
        from ozagia.integrations.kiro import install as kiro
        paths = kiro.paths(root, scope, agent=kiro.DEFAULT_AGENT)
        return tuple(str(paths[key]) for key in ("hooks", "agent", "mcp"))
    except (ImportError, OSError, ValueError):
        return ()


def _integration_plan(root: Path, action: str,
                      args: Mapping[str, Any]) -> _IntegrationPlan:
    name = str(args["name"])
    scope = str(args["scope"])
    module = _INSTALLER_MODULES[name]
    supported = scope in _INTEGRATION_SCOPES[name]
    python = args.get("python") if action == "install" and name != "opencode" else None
    if not supported:
        reason = (f"the {name} installer supports "
                  f"{', '.join(_INTEGRATION_SCOPES[name])} scope only, not {scope}")
        return _IntegrationPlan(name, scope, module, (), (), (), python,
                                False, reason)
    arguments: list[str] = []
    if action == "install":
        arguments = ["install", "--workdir", str(root)]
        if name == "claude":
            arguments += ["--settings", "local", "--python", str(python)]
        elif name == "codex":
            arguments += ["--scope", scope, "--python", str(python)]
        elif name == "kiro":
            arguments += ["--scope", scope, "--python", str(python),
                          "--format", "v3", "--agent", "ozagia"]
    elif action == "uninstall":
        arguments = ["uninstall", "--workdir", str(root)]
        if name == "codex":
            arguments += ["--scope", scope]
        elif name == "kiro":
            arguments += ["--scope", scope, "--agent", "ozagia"]
    elif action == "verify" and name == "opencode":
        arguments = ["check", "--workdir", str(root)]
    argv = ((sys.executable, "-m", module, *arguments) if arguments else ())
    targets = _integration_targets(name, root, scope, action)
    reason = ""
    if action == "verify" and name != "opencode":
        reason = f"the {name} installer has no read-only status/check command"
    return _IntegrationPlan(name, scope, module, tuple(arguments), argv,
                            targets, python, True, reason)


def _installer_available(module: str, context: ActionContext) -> bool:
    if context.installer_runner is not None:
        return True
    try:
        return importlib.util.find_spec(f"{module}.__main__") is not None
    except (AttributeError, ImportError, ValueError):
        return False


def _python_available(value: str | None) -> bool:
    if not value:
        return False
    path = Path(value).expanduser()
    if path.is_absolute() or os.sep in value:
        return path.is_file() and os.access(path, os.X_OK)
    return shutil.which(value) is not None


def _integration_complete(report: Mapping[str, Any], name: str,
                          action: str) -> bool:
    if name == "claude" and action == "install":
        # Install writes the personal settings file; a project settings file
        # alone does not make that install complete.
        return any(item.get("name") == "local settings" and item.get("installed")
                   for item in report.get("components") or [])
    return report.get("state") == "installed"


def _integration_effect(plan: _IntegrationPlan, action: str,
                        check_available: bool) -> str:
    if not plan.supported:
        return "none (unsupported scope)"
    labels = ", ".join(plan.targets) or "no target paths"
    if action == "install":
        return (f"write ozagia's {plan.name} files ({labels}); merge only their "
                "own entries, keep unrelated settings, and run no hook or service")
    if action == "uninstall":
        return (f"remove only ozagia's entries from {labels}; keep everything "
                "else, and run no hook or service")
    if check_available:
        return ("none (read-only): run the installer's local load check; "
                "no model call, network request or install")
    return ("none: this installer has no status/check command, so apply reports "
            "`unavailable`")


def _integration_operation(plan: _IntegrationPlan, action: str) -> str:
    if action == "install":
        return f"install ozagia's {plan.name} integration for {plan.scope} scope"
    if action == "uninstall":
        return f"uninstall ozagia's {plan.name} integration for {plan.scope} scope"
    return f"verify ozagia's {plan.name} integration for {plan.scope} scope"


def _integration_manager_call(plan: _IntegrationPlan, root: Path,
                              action: str, check_available: bool,
                              context: ActionContext) -> str | None:
    if not plan.supported:
        return None
    if action in {"install", "uninstall"}:
        return f"{plan.module}.__main__.main({list(plan.arguments)!r})"
    if check_available:
        if context.integration_check is not None:
            return "integration_check(name, root)"
        return f"{plan.module}.install.check_loads({str(root)!r})"
    return None


def _preview_integration_action(root: Path, action: str, args: Mapping[str, Any],
                                context: ActionContext) -> ActionResult:
    kind = action.split(".", 1)[1]
    plan = _integration_plan(root, kind, args)
    report = _integration_report(root, plan.name, plan.scope)[0]
    state = str(report.get("state") or "unknown")
    current = {
        **report,
        "requested_scope": plan.scope,
        "supported_scopes": list(_INTEGRATION_SCOPES[plan.name]),
    }
    notes: list[str] = []
    check_available = (kind == "verify"
                       and (plan.name == "opencode"
                            or context.integration_check is not None))
    if not plan.supported:
        notes.append(plan.reason)
    elif kind == "verify" and not check_available:
        notes.append(f"{plan.reason}; apply reports `unavailable`")

    complete = _integration_complete(report, plan.name, kind)
    absent = state == "absent"
    installer_available = _installer_available(plan.module, context)
    python_ok = (plan.python is None or _python_available(plan.python))
    applicable = plan.supported
    effect = _integration_effect(plan, kind, check_available)
    if plan.supported:
        if kind == "install" and complete:
            notes.append("the integration is already installed for this scope; apply "
                         "re-runs the idempotent installer and keeps unrelated settings")
        elif kind == "uninstall" and absent:
            applicable = False
            effect = "none (the integration is not installed for this scope)"
            notes.append("the integration is not installed for this scope; "
                         "apply is a no-op")
        elif kind in {"install", "uninstall"} and not installer_available:
            applicable = False
            notes.append("the installer module is not available")
        elif kind == "install" and not python_ok:
            applicable = False
            notes.append("the configured Python does not exist or is not executable")

    preconditions = [
        f"integration {plan.name!r} is supported",
        f"{plan.scope} scope is supported by the {plan.name} installer",
        "existing target files, if present, are valid and mergeable",
    ]
    if kind in {"install", "uninstall"}:
        preconditions.append("the installer module is available")
    if kind == "install" and plan.python is not None:
        preconditions.append("the configured Python exists and is executable")
    if kind == "verify" and check_available:
        preconditions.append(
            "the tool's own CLI is optional; a missing tool is reported, never installed")

    risks = [
        "no hook is started and no service is started by this action",
        "the installer changes only ozagia's own entries; unrelated settings are kept",
    ]
    if plan.scope == "user":
        risks.append("user scope writes outside the repository, under the tool's home directory")
    if kind == "install":
        risks.append("hooks take effect on the next session or tool call, after the "
                     "tool's own trust step")
    elif kind == "uninstall":
        risks.append("a file left with nothing else can be deleted; ozagia's entries "
                     "are removed, not the user's")
    elif check_available:
        risks.append("the check may run local tool/Node processes and take up to 60 seconds")
    else:
        risks.append("`unavailable` is reported honestly; it is not a claim that the "
                     "integration works")

    return _preview(
        action,
        operation=_integration_operation(plan, kind),
        expected_effect=effect,
        manager_call=_integration_manager_call(plan, root, kind, check_available,
                                               context),
        argv=plan.argv,
        preconditions=tuple(preconditions),
        risks=tuple(risks),
        current=current,
        current_state=state,
        applicable=applicable,
        notes=tuple(dict.fromkeys(notes)),
        scope=plan.scope,
        targets=plan.targets,
        installer=plan.module if plan.supported else None,
        python=plan.python,
    )


def _invoke_installer(module: str, arguments: tuple[str, ...],
                      context: ActionContext) -> tuple[int | None, str | None]:
    """Call an installer main() in-process (or the test runner). Never spawns."""
    if context.installer_runner is not None:
        try:
            value = context.installer_runner(module, list(arguments))
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            return None, _safe_error(exc)
        if isinstance(value, bool) or not isinstance(value, int):
            return None, "installer runner returned an invalid status"
        if value != 0:
            return value, f"installer exited with status {value}"
        return 0, None
    output = io.StringIO()
    try:
        imported = importlib.import_module(f"{module}.__main__")
        main = getattr(imported, "main", None)
        if not callable(main):
            return None, "installer module has no main()"
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            code = main(list(arguments))
    except SystemExit as exc:
        code = exc.code if exc.code is not None else 0
        if isinstance(code, bool) or not isinstance(code, int):
            return None, "installer exited without a numeric status"
    except (ImportError, OSError, RuntimeError, TypeError, ValueError) as exc:
        return None, _safe_error(exc)
    if isinstance(code, bool) or not isinstance(code, int):
        return None, "installer returned an invalid status"
    if code != 0:
        message = _safe_error(output.getvalue().strip())
        return code, message or f"installer exited with status {code}"
    return 0, None


def _run_integration_check(name: str, root: Path,
                           context: ActionContext) -> dict[str, Any]:
    if context.integration_check is not None:
        try:
            value = context.integration_check(name, root)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise ActionError(_safe_error(exc) or "integration check failed") from None
        if not isinstance(value, Mapping):
            raise ActionError("integration check returned an invalid result")
        payload = dict(value)
        payload.setdefault("available", True)
        return payload
    if name != "opencode":
        return {"available": False,
                "reason": f"the {name} installer has no read-only status/check command"}
    from ozagia.integrations.opencode import install as opener
    try:
        value = opener.check_loads(root)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise ActionError(_safe_error(exc) or "OpenCode load check failed") from None
    if not isinstance(value, Mapping):
        raise ActionError("OpenCode load check returned an invalid result")
    return {"available": True, **dict(value)}


def _apply_integration_action(root: Path, action: str, args: Mapping[str, Any],
                              context: ActionContext,
                              consume: Callable[[], None]) -> ActionResult:
    kind = action.split(".", 1)[1]
    plan = _integration_plan(root, kind, args)
    report = _integration_report(root, plan.name, plan.scope)[0]
    state = str(report.get("state") or "unknown")
    scope_fields = {
        "scope": plan.scope,
        "targets": plan.targets,
        "installer": plan.module if plan.supported else None,
        "python": plan.python,
    }
    if not plan.supported:
        consume()
        return _refused(action, current_state=state, error=plan.reason,
                         result={"integration": report}, **scope_fields)

    if kind == "verify":
        check_available = (plan.name == "opencode"
                           or context.integration_check is not None)
        consume()
        try:
            payload = _run_integration_check(plan.name, root, context)
        except ActionError as exc:
            return _result(
                action, mode="apply", state="failed", ok=False,
                current_state=state, expected_effect="none (read-only)",
                error=_safe_error(exc), result={"available": True},
                **scope_fields)
        if payload.get("available") is False:
            return _result(
                action, mode="apply", state="unavailable", ok=True,
                current_state=state,
                expected_effect="none (no installer check available)",
                result={**payload, "integration": report},
                **scope_fields)
        ok = payload.get("ok") is not False
        return _result(
            action, mode="apply", state="verified" if ok else "failed", ok=ok,
            current_state=state, expected_effect="none (read-only)",
            result=payload,
            manager_call=_integration_manager_call(plan, root, kind, check_available,
                                                   context),
            error=None if ok else (_safe_error(payload.get("error"))
                                   or "integration check failed"),
            **scope_fields)

    absent = state == "absent"
    if kind == "uninstall" and absent:
        consume()
        return _noop(action, current_state=state,
                     message="the integration is not installed for this scope",
                     result={"integration": report}, **scope_fields)
    if not _installer_available(plan.module, context):
        consume()
        return _refused(action, current_state=state,
                        error="the installer module is not available",
                        result={"integration": report}, **scope_fields)
    if kind == "install" and plan.python is not None and not _python_available(plan.python):
        consume()
        return _refused(action, current_state=state,
                        error="the configured Python does not exist or is not executable",
                        result={"integration": report}, **scope_fields)

    effect = _integration_effect(plan, kind, False)
    manager_call = _integration_manager_call(plan, root, kind, False, context)
    consume()
    code, error = _invoke_installer(plan.module, plan.arguments, context)
    if code != 0 or error is not None:
        return _result(
            action, mode="apply", state="failed", ok=False,
            current_state=state, expected_effect=effect,
            error=error or f"installer exited with status {code}",
            result={"installer": plan.module, "exit": code,
                    "scope": plan.scope},
            manager_call=manager_call, argv=plan.argv, **scope_fields)
    target_state = "installed" if kind == "install" else "absent"
    return _applied(
        action, current_state=target_state, expected_effect=effect,
        result={"installer": plan.module, "exit": 0, "scope": plan.scope,
                "targets": list(plan.targets)},
        manager_call=manager_call, argv=plan.argv, **scope_fields)


# --------------------------------------------------------------------------- #
# public entry points
# --------------------------------------------------------------------------- #
def preview(root: str | Path, action: str, *,
            context: ActionContext | None = None, **args: Any) -> ActionResult:
    """Describe an action without starting, stopping or signalling anything."""
    context = context or ActionContext()
    root_path = _root_path(root)
    canonical = _canonical(action, args)
    try:
        result = _dispatch_preview(root_path, action, canonical, context)
    except RuntimeStateError:
        raise ActionError("runtime state could not be read") from None
    result.confirmation = _issue_token(root_path, action, canonical, context)
    return result


def _dispatch_preview(root: Path, action: str, args: Mapping[str, Any],
                      context: ActionContext) -> ActionResult:
    if action.startswith("daemon."):
        return _preview_daemon(root, action, args, context)
    if action.startswith("service."):
        return _preview_service(root, action, args, context)
    if action.startswith("tunnel."):
        return _preview_tunnel(root, action, args, context)
    if action == "integration.status":
        return _preview_integration(root, action, args, context)
    if action.startswith("integration."):
        return _preview_integration_action(root, action, args, context)
    raise ActionError(f"unsupported action: {action!r}")


def apply(root: str | Path, action: str, confirmation: str | None, *,
          context: ActionContext | None = None, **args: Any) -> ActionResult:
    """Apply a previously previewed action with its one-time confirmation token."""
    context = context or ActionContext()
    root_path = _root_path(root)
    canonical = _canonical(action, args)
    token = _load_token(root_path, action, canonical, confirmation, context)

    def consume() -> None:
        _consume_token(root_path, token)

    try:
        result = _dispatch_apply(root_path, action, canonical, context, consume)
    except RuntimeStateError:
        raise ActionError("runtime state could not be read") from None
    result.mode = "apply"
    result.confirmation = None
    try:
        result.audit = _append_audit(root_path, result, canonical, token, context)
    except OSError as exc:
        result.audit_error = _safe_error(exc)
    return result


def _dispatch_apply(root: Path, action: str, args: Mapping[str, Any],
                    context: ActionContext,
                    consume: Callable[[], None]) -> ActionResult:
    if action.startswith("daemon."):
        return _apply_daemon(root, action, args, context, consume)
    if action.startswith("service."):
        return _apply_service(root, action, args, context, consume)
    if action.startswith("tunnel."):
        return _apply_tunnel(root, action, args, context, consume)
    if action == "integration.status":
        return _apply_integration(root, action, args, context, consume)
    if action.startswith("integration."):
        return _apply_integration_action(root, action, args, context, consume)
    raise ActionError(f"unsupported action: {action!r}")


__all__ = [
    "ACTION_SPECS",
    "ActionContext",
    "ActionError",
    "ActionRefused",
    "ActionRequest",
    "ActionResult",
    "apply",
    "list_actions",
    "preview",
]
