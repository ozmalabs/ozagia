"""Read-only readiness for the declared service and tunnel actions.

This module describes the service/tunnel operations already declared in
:mod:`ozagia.core.actions` and joins only read-only observations:

* the merged project configuration (configured/absent, mode, relay, env refs);
* the existing runtime status snapshot (process identity, tunnel lease);
* local runtime log metadata (presence and size, never content).

It never previews, applies, issues a token, appends an audit record, resolves a
secret reference, constructs a mutating manager, starts/stops/signals a process
or opens a tunnel.  Readiness is an observation, not an authorization: a
``preview`` plus its one-time confirmation token remains the only path to an
apply, and apply re-checks every precondition.

States
------

``available``
    Every observed prerequisite for the operation is satisfied.  This is not a
    permission and it does not satisfy a live/network gate.
``blocked``
    A definite configuration or runtime precondition prevents the operation
    (wrong mode, relay present, live gate not granted, unverified process or
    lease, already active/no-op).
``unknown``
    The observation is insufficient.  An unresolved ``secret://`` reference,
    an unreadable configuration/runtime state or an unobserved target keeps the
    action from being called available.
``absent``
    The named service or tunnel is not configured.

Secret values are never read: an environment table is represented by its
key names, its ``secret://...`` reference strings and counts.  A service whose
environment contains references is therefore never ``available`` for a
mutating operation until the execution boundary resolves them; this module
does not resolve them at all.

Nothing in this module writes.  It reads configuration, ``/proc``-derived
runtime status through :func:`ozagia.core.runtime.runtime_status`, and log
filesystem metadata through :class:`RuntimeStore` with ``create=False``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ozagia.config import ConfigError, load_project_config, state_path
from ozagia.core.actions import ACTION_SPECS, ActionSpec
from ozagia.core.runtime import RuntimeStateError, RuntimeStore, runtime_status

SCHEMA_NAMESPACE = "ozagia.action_readiness"
SCHEMA_VERSION = "1.0"
REPORT_VERSION = "1.0"

AVAILABLE = "available"
BLOCKED = "blocked"
UNKNOWN = "unknown"
ABSENT = "absent"
STATES = (AVAILABLE, BLOCKED, UNKNOWN, ABSENT)

MAX_SELECTIONS = 16
MAX_ENV_KEYS = 64
MAX_REASON = 400
MAX_TARGETS = 32

_NAME = re.compile(r"[^\x00-\x1f\x7f]{1,128}\Z")
_ACTION = re.compile(r"[a-z]+\.[a-z]+\Z")
_SERVICE_ACTIONS = frozenset({
    "service.list", "service.status", "service.start", "service.run",
    "service.stop", "service.restart", "service.logs",
})
_TUNNEL_ACTIONS = frozenset({
    "tunnel.list", "tunnel.status", "tunnel.start",
})
_LIST_ACTIONS = frozenset({"service.list", "tunnel.list"})
_LIFECYCLE_ACTIONS = ("service.start", "service.run", "service.stop",
                      "service.restart")


class ActionReadinessError(ValueError):
    """An invalid readiness request."""


@dataclass(frozen=True)
class ActionSelection:
    """One explicitly selected service/tunnel operation."""

    action: str
    name: str | None = None

    def identity(self) -> tuple[str, str | None]:
        return self.action, self.name


def _clip(value: Any, limit: int = MAX_REASON) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit]


def _selection(value: Any) -> ActionSelection:
    if isinstance(value, ActionSelection):
        action, name = value.action, value.name
    elif isinstance(value, Mapping):
        unknown = set(value) - {"action", "name"}
        if unknown:
            raise ActionReadinessError(
                "unsupported action selection field(s): "
                + ", ".join(sorted(str(item) for item in unknown)))
        action, name = value.get("action"), value.get("name")
    else:
        raise ActionReadinessError(
            "action selections must be mappings with action and optional name")
    if not isinstance(action, str) or not _ACTION.fullmatch(action):
        raise ActionReadinessError("action must be a bounded dotted action name")
    if action not in _SERVICE_ACTIONS and action not in _TUNNEL_ACTIONS:
        raise ActionReadinessError(
            f"action readiness covers service/tunnel actions only: {action!r}")
    if action in _LIST_ACTIONS:
        if name is not None:
            raise ActionReadinessError(f"{action} does not take a target name")
        return ActionSelection(action, None)
    if (not isinstance(name, str) or not name or not _NAME.fullmatch(name)):
        raise ActionReadinessError(f"{action} requires a bounded target name")
    return ActionSelection(action, name)


def parse_selections(value: Any) -> tuple[ActionSelection, ...] | None:
    """Validate an explicit selection; ``None`` selects every declared target."""
    if value is None:
        return None
    if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
        raise ActionReadinessError("action readiness selection must be a list")
    selections = tuple(_selection(item) for item in value)
    if len(selections) > MAX_SELECTIONS:
        raise ActionReadinessError(
            f"at most {MAX_SELECTIONS} action selections are allowed")
    seen: set[tuple[str, str | None]] = set()
    for item in selections:
        if item.identity() in seen:
            raise ActionReadinessError(
                f"duplicate action selection: {item.action} {item.name or ''}".strip())
        seen.add(item.identity())
    return selections


def _spec(name: str) -> ActionSpec | None:
    return next((item for item in ACTION_SPECS if item.name == name), None)


def _relay(config: Any, name: str | None) -> Any | None:
    return next((item for item in config.relays if item.name == name), None)


def _environment(service: Any) -> dict[str, Any]:
    """Describe a service env by key/reference only; never read a value."""
    pairs = tuple(getattr(service, "env", ()) or ())
    keys = [str(key) for key, _value in pairs[:MAX_ENV_KEYS]]
    references = [
        {"key": str(key), "reference": str(value)}
        for key, value in pairs[:MAX_ENV_KEYS]
        if isinstance(value, str) and value.startswith("secret://")
    ]
    plain_keys = [str(key) for key, value in pairs[:MAX_ENV_KEYS]
                  if not (isinstance(value, str) and value.startswith("secret://"))]
    session = [item["reference"] for item in references
               if item["reference"].startswith("secret://session/")]
    if session:
        state = "session_reference"
    elif references:
        state = "unresolved"
    elif keys:
        state = "plain_only"
    else:
        state = "none"
    return {
        "state": state,
        "keys": keys,
        "references": references,
        "plain_keys": plain_keys,
        "unresolved": len(references),
        "session_references": session,
        "truncated": len(pairs) > MAX_ENV_KEYS,
        "resolved": False,
    }


def _process(runtime_row: Mapping[str, Any] | None,
             runtime_error: str | None) -> dict[str, Any]:
    if runtime_error is not None:
        return {"state": UNKNOWN, "pid": None, "verified": False,
                "stale": False, "recorded": None,
                "source": "ozagia.core.runtime.runtime_status",
                "error": runtime_error}
    row = runtime_row or {}
    state = str(row.get("state") or "configured")
    pid = row.get("pid")
    verified = (state == "running" and isinstance(pid, int)
                and not isinstance(pid, bool))
    return {
        "state": state,
        "pid": pid if isinstance(pid, int) and not isinstance(pid, bool) else None,
        "verified": verified,
        "stale": state == "stale",
        "recorded": state != "configured",
        "source": "ozagia.core.runtime.runtime_status",
    }


def _log_row(name: str, log_state: Mapping[str, Any] | None,
             log_error: str | None) -> dict[str, Any]:
    if log_error is not None:
        return {"state": UNKNOWN, "path": None, "bytes": None,
                "error": log_error, "content_read": False}
    if log_state is None:
        return {"state": "not_recorded", "path": None, "bytes": None,
                "content_read": False}
    return dict(log_state)


def _read_log_metadata(root: Path, state_dir: Path | None
                       ) -> tuple[dict[str, dict[str, Any]], str | None]:
    """Return per-service log presence/size; never read log content."""
    selected = state_dir if state_dir is not None else state_path(root, "runtime")
    try:
        store = RuntimeStore(selected, create=False)
        records = store.all()
    except RuntimeStateError:
        return {}, "runtime log metadata is unavailable"
    by_name: dict[str, dict[str, Any]] = {}
    for record in records.values():
        if record.get("kind") != "service":
            continue
        name = record.get("name")
        if not isinstance(name, str):
            continue
        path = store.logs_dir / f"{record['id']}.log"
        try:
            size = path.stat().st_size if path.is_file() else None
        except OSError:
            size = None
        by_name[name] = {
            "state": ("empty" if size == 0 else "available")
            if size is not None else "absent",
            "path": str(path) if size is not None else None,
            "bytes": size,
            "content_read": False,
            "source": "ozagia.core.runtime.RuntimeStore",
        }
    return by_name, None


def _service_target(name: str | None, service: Any,
                    runtime_row: Mapping[str, Any] | None,
                    runtime_error: str | None,
                    logs: Mapping[str, Any] | None,
                    log_error: str | None) -> dict[str, Any]:
    if service is None:
        return {
            "kind": "service", "name": name, "configured": False,
            "mode": None, "relay": None, "process": None,
            "environment": None, "logs": None,
            "readiness": ABSENT,
            "reason": f"service {name!r} is not configured",
            "operations": {},
        }
    env = _environment(service)
    process = _process(runtime_row, runtime_error)
    row = {
        "kind": "service",
        "name": service.name,
        "configured": True,
        "mode": service.mode,
        "relay": service.relay,
        "description": service.description or None,
        "process": process,
        "environment": env,
        "logs": _log_row(service.name, logs, log_error),
        "operations": {},
    }
    state, reason = _service_target_state(row)
    row["readiness"] = state
    row["reason"] = reason
    return row


def _service_target_state(row: Mapping[str, Any]) -> tuple[str, str]:
    env = row.get("environment") or {}
    if env.get("session_references"):
        return (BLOCKED,
                "the environment contains a session secret reference that a "
                "runtime service cannot resolve at spawn")
    if env.get("unresolved"):
        return (UNKNOWN,
                "the environment has secret references; readiness does not "
                "resolve them and cannot call the service available")
    process = row.get("process") or {}
    state = str(process.get("state") or UNKNOWN)
    operations = row.get("operations") or {}
    states = [operations.get(action) for action in _LIFECYCLE_ACTIONS]
    if any(item == UNKNOWN for item in states):
        return (UNKNOWN, "a lifecycle operation could not be assessed")
    if any(item == AVAILABLE for item in states):
        return (AVAILABLE,
                "no configured mode/relay/process blocker was observed; "
                "preview and confirmation are still required")
    if state == "stale" or state == UNKNOWN:
        return (BLOCKED,
                "the process identity is stale or unverified; no lifecycle "
                "operation would be applied")
    return (BLOCKED, "no lifecycle operation is currently applicable")


def _service_requirements(action: str, row: Mapping[str, Any]) -> list[str]:
    requirements = [
        "one-time confirmation token from a preview",
        f"service {row.get('name')!r} is configured",
    ]
    short = action.split(".", 1)[1]
    if short == "start":
        requirements += [
            "mode is background",
            "no relay is configured",
            "no active or stale/unverified process record",
            "environment references resolve at the spawn boundary (not checked here)",
        ]
    elif short == "run":
        requirements += [
            "no relay is configured",
            "no active or stale/unverified process record",
            "environment references resolve at the spawn boundary (not checked here)",
        ]
    elif short == "stop":
        requirements += [
            "the service is running or starting",
            "PID, start token and process group are verified",
        ]
    elif short == "restart":
        requirements += [
            "mode is background",
            "no relay is configured",
            "no stale/unverified process record",
            "environment references resolve at the spawn boundary (not checked here)",
        ]
    elif short == "logs":
        requirements.append("only local log metadata is joined; log content is caller-requested")
    return requirements


def _service_operation(action: str, row: Mapping[str, Any]) -> tuple[str, str, list[str]]:
    short = action.split(".", 1)[1]
    process = row.get("process") or {}
    env = row.get("environment") or {}
    state = str(process.get("state") or UNKNOWN)
    if short == "status":
        if row.get("configured") is not True:
            return ABSENT, row.get("reason") or "service is not configured", []
        if process.get("error"):
            return UNKNOWN, "runtime state could not be read", []
        return AVAILABLE, "read-only status observation is available", []
    if short == "logs":
        if row.get("configured") is not True:
            return ABSENT, row.get("reason") or "service is not configured", []
        if process.get("error"):
            return UNKNOWN, "runtime state could not be read", []
        log = row.get("logs") or {}
        state_text = log.get("state")
        return (AVAILABLE,
                f"read-only bounded log access is available (log state: {state_text})",
                [])
    if short in {"start", "run", "restart"}:
        if env.get("session_references"):
            return (BLOCKED,
                    "a session secret reference cannot be resolved for a "
                    "runtime service", _service_requirements(action, row))
        if env.get("unresolved"):
            return (UNKNOWN,
                    "environment secret references were not resolved; readiness "
                    "never resolves them", _service_requirements(action, row))
    if process.get("error"):
        return UNKNOWN, "runtime state could not be read", _service_requirements(action, row)
    if short == "start":
        if row.get("mode") != "background":
            return BLOCKED, "service.start requires background mode", _service_requirements(action, row)
        if row.get("relay") is not None:
            return BLOCKED, "a relay-backed service cannot be started", _service_requirements(action, row)
        if state in {"stale", "unknown"}:
            return BLOCKED, "the process identity is stale or unverified; it is never reused", _service_requirements(action, row)
        if state in {"running", "starting"}:
            return BLOCKED, "the service is already active; apply would be a no-op", _service_requirements(action, row)
        return AVAILABLE, "background start prerequisites were observed", _service_requirements(action, row)
    if short == "run":
        if row.get("relay") is not None:
            return BLOCKED, "a relay-backed service cannot run in the foreground", _service_requirements(action, row)
        if state in {"stale", "unknown"}:
            return BLOCKED, "the process identity is stale or unverified", _service_requirements(action, row)
        if state in {"running", "starting"}:
            return BLOCKED, "the service is already active; apply would be a no-op", _service_requirements(action, row)
        return AVAILABLE, "foreground run prerequisites were observed", _service_requirements(action, row)
    if short == "stop":
        if state in {"stale", "unknown"}:
            return BLOCKED, "the process identity is stale or unverified; it is never signalled", _service_requirements(action, row)
        if state in {"running", "starting"}:
            return AVAILABLE, "a verified stop target was observed", _service_requirements(action, row)
        return BLOCKED, "the service is not running; apply would be a no-op", _service_requirements(action, row)
    if short == "restart":
        if row.get("mode") != "background":
            return BLOCKED, "service.restart requires background mode", _service_requirements(action, row)
        if row.get("relay") is not None:
            return BLOCKED, "a relay-backed service cannot be restarted", _service_requirements(action, row)
        if state in {"stale", "unknown"}:
            return BLOCKED, "the process identity is stale or unverified", _service_requirements(action, row)
        return AVAILABLE, "background restart prerequisites were observed", _service_requirements(action, row)
    return UNKNOWN, "the operation is not described", []


def _tunnel_target(name: str | None, tunnel: Any, config: Any,
                   runtime_row: Mapping[str, Any] | None,
                   runtime_error: str | None) -> dict[str, Any]:
    if tunnel is None:
        return {
            "kind": "tunnel", "name": name, "configured": False,
            "relay": None, "target": None, "lease": None,
            "readiness": ABSENT,
            "reason": f"tunnel {name!r} is not configured",
            "operations": {},
        }
    relay = _relay(config, tunnel.relay)
    relay_row = {
        "name": tunnel.relay,
        "configured": relay is not None,
        "kinds": [spec.kind for spec in relay.specs] if relay is not None else [],
        "allowed_kinds": list(relay.allowed_kinds) if relay is not None else [],
    }
    if runtime_error is not None:
        lease = {"state": UNKNOWN, "held": False, "stale": False,
                 "error": runtime_error,
                 "source": "ozagia.core.runtime.runtime_status"}
    else:
        lease_state = str((runtime_row or {}).get("state") or "configured")
        lease = {
            "state": lease_state,
            "held": False,
            "stale": lease_state == "stale",
            "running": lease_state in {"running", "starting"},
            "source": "ozagia.core.runtime.runtime_status",
        }
    row = {
        "kind": "tunnel",
        "name": tunnel.name,
        "configured": True,
        "relay": relay_row,
        "target": tunnel.target,
        "local_port": tunnel.local_port,
        "description": tunnel.description or None,
        "lease": lease,
        "operations": {},
    }
    state, reason = _tunnel_target_state(row, tunnel)
    row["readiness"] = state
    row["reason"] = reason
    return row


def _tunnel_target_state(row: Mapping[str, Any], tunnel: Any) -> tuple[str, str]:
    relay = row.get("relay") or {}
    lease = row.get("lease") or {}
    if lease.get("error"):
        return UNKNOWN, "runtime state could not be read"
    if relay.get("configured") is not True:
        return BLOCKED, "the tunnel relay is not configured"
    kinds = set(relay.get("kinds") or ())
    allowed = set(relay.get("allowed_kinds") or ())
    if not allowed or not kinds.intersection(allowed):
        return BLOCKED, "the relay policy allows none of the configured relay kinds"
    if lease.get("stale"):
        return BLOCKED, "the persisted tunnel lease is stale and cannot be claimed"
    if lease.get("state") == UNKNOWN:
        return BLOCKED, "the tunnel lease state is unverified"
    if lease.get("running"):
        return BLOCKED, "a tunnel lease is already active"
    return (BLOCKED,
            "tunnel.start is live-gated: readiness never grants "
            "--allow-live/--allow-network")


def _tunnel_requirements(action: str) -> list[str]:
    requirements = ["one-time confirmation token from a preview"]
    if action == "tunnel.start":
        requirements += [
            "the tunnel and its relay are configured",
            "the relay policy allows the configured relay kind",
            "no active or stale/unverified lease exists",
            "--allow-live and --allow-network are passed explicitly at apply",
        ]
    else:
        requirements.append("the tunnel is configured")
    return requirements


def _tunnel_operation(action: str, row: Mapping[str, Any]) -> tuple[str, str, list[str]]:
    if row.get("configured") is not True:
        return ABSENT, row.get("reason") or "tunnel is not configured", []
    lease = row.get("lease") or {}
    if lease.get("error"):
        return UNKNOWN, "runtime state could not be read", []
    if action == "tunnel.status":
        return AVAILABLE, "read-only status observation is available", []
    relay = row.get("relay") or {}
    if relay.get("configured") is not True:
        return BLOCKED, "the tunnel relay is not configured", _tunnel_requirements(action)
    kinds = set(relay.get("kinds") or ())
    allowed = set(relay.get("allowed_kinds") or ())
    if not allowed or not kinds.intersection(allowed):
        return (BLOCKED, "the relay policy allows none of the configured relay kinds",
                _tunnel_requirements(action))
    if lease.get("stale"):
        return BLOCKED, "the persisted tunnel lease is stale and cannot be claimed", _tunnel_requirements(action)
    if lease.get("state") == UNKNOWN:
        return BLOCKED, "the tunnel lease state is unverified", _tunnel_requirements(action)
    if lease.get("running"):
        return BLOCKED, "a tunnel lease is already active", _tunnel_requirements(action)
    return (BLOCKED,
            "tunnel.start is live-gated; readiness never grants "
            "--allow-live/--allow-network", _tunnel_requirements(action))


def _operation_row(action: str, target: Mapping[str, Any] | None,
                   state: str, reason: str,
                   requirements: list[str]) -> dict[str, Any]:
    spec = _spec(action)
    return {
        "action": action,
        "kind": action.split(".", 1)[0],
        "target": target.get("name") if target else None,
        "mutating": bool(spec.mutating) if spec else False,
        "destructive": bool(spec.destructive) if spec else False,
        "live_gated": bool(spec.live_gated) if spec else False,
        "confirmation_required": True,
        "readiness": state,
        "reason": _clip(reason),
        "requirements": list(requirements),
        "gates": {
            "confirmation": True,
            "live": bool(spec.live_gated) if spec else False,
            "network": bool(spec.live_gated) if spec else False,
        },
        "observations": _observations(target),
    }


def _observations(target: Mapping[str, Any] | None) -> dict[str, Any]:
    if target is None:
        return {}
    if target.get("kind") == "service":
        process = target.get("process") or {}
        env = target.get("environment") or {}
        return {
            "configured": target.get("configured"),
            "mode": target.get("mode"),
            "relay": target.get("relay"),
            "process_state": process.get("state"),
            "process_verified": process.get("verified"),
            "log_state": (target.get("logs") or {}).get("state"),
            "secret_references": len(env.get("references") or ()),
            "resolved_secrets": False,
        }
    return {
        "configured": target.get("configured"),
        "relay": (target.get("relay") or {}).get("name"),
        "relay_configured": (target.get("relay") or {}).get("configured"),
        "lease_state": (target.get("lease") or {}).get("state"),
        "lease_held": (target.get("lease") or {}).get("held"),
    }


def _operation_state(action: str, target: Mapping[str, Any] | None,
                     config_error: str | None, runtime_error: str | None
                     ) -> tuple[str, str, list[str]]:
    if config_error is not None:
        return UNKNOWN, "project configuration could not be read", [
            "project configuration is readable"]
    if action in _LIST_ACTIONS:
        if runtime_error is not None:
            return UNKNOWN, "runtime state could not be read", [
                "runtime state is readable"]
        return AVAILABLE, "read-only configuration/runtime listing is available", [
            "project configuration is readable"]
    if target is None:
        return UNKNOWN, "the action target was not observed", []
    if target.get("configured") is not True:
        return ABSENT, str(target.get("reason") or "target is not configured"), []
    if action.startswith("service."):
        return _service_operation(action, target)
    return _tunnel_operation(action, target)


def _target_state(operations: Iterable[Mapping[str, Any]]) -> tuple[str, str]:
    states = [str(item.get("readiness") or UNKNOWN) for item in operations]
    if any(state == UNKNOWN for state in states):
        return UNKNOWN, "an operation could not be assessed"
    if any(state == AVAILABLE for state in states):
        return AVAILABLE, "at least one operation is available"
    if any(state == BLOCKED for state in states):
        return BLOCKED, "every operation is blocked"
    if any(state == ABSENT for state in states):
        return ABSENT, "the target is not configured"
    return UNKNOWN, "no operation was assessed"


def _aggregate(states: Iterable[str]) -> tuple[str, str]:
    values = list(states)
    if not values:
        return UNKNOWN, "no service or tunnel action was explicitly selected"
    if any(value == UNKNOWN for value in values):
        return UNKNOWN, "at least one selected operation is unknown"
    if any(value == BLOCKED for value in values):
        return BLOCKED, "at least one selected operation is blocked"
    if any(value == ABSENT for value in values):
        return ABSENT, "at least one selected target is absent"
    return AVAILABLE, "every selected operation is available; apply is still required"


def _counts(rows: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    counts = {state: 0 for state in STATES}
    total = 0
    for row in rows:
        total += 1
        state = str(row.get("readiness") or UNKNOWN)
        counts[state] = counts.get(state, 0) + 1
    counts["operations"] = total
    return counts


def _target_key(kind: str, name: str | None) -> tuple[str, str]:
    return kind, str(name)


def readiness(root: str | Path, *, selections: Any = None,
              observed_at: int | str | None = None,
              state_dir: str | Path | None = None) -> dict[str, Any]:
    """Join read-only service/tunnel action readiness for one repository.

    ``selections=None`` describes every operation for every configured target.
    An explicit iterable (including an empty one) describes only those
    operations.  The function writes nothing and resolves no secret.
    """
    selected = parse_selections(selections)
    path = Path(root).expanduser().resolve()
    if not path.is_dir():
        raise ActionReadinessError(f"repository root is not a directory: {path}")

    config_error: str | None = None
    try:
        config = load_project_config(path)
    except (ConfigError, OSError):
        config = None
        config_error = "config_unreadable"

    runtime_error: str | None = None
    runtime: Mapping[str, Any] = {}
    if config is not None:
        selected_state_dir = (Path(state_dir).expanduser().resolve()
                              if state_dir is not None
                              else state_path(path, "runtime"))
        try:
            runtime = runtime_status(config, state_dir=selected_state_dir)
        except (RuntimeStateError, OSError, ValueError):
            runtime_error = "runtime_unreadable"
        logs, log_error = _read_log_metadata(path, selected_state_dir)
    else:
        logs, log_error = {}, None

    services = {item.name: item for item in getattr(config, "runtime_services", ())}
    tunnels = {item.name: item for item in getattr(config, "runtime_tunnels", ())}
    runtime_services = {
        str(row.get("name")): row
        for row in (runtime.get("services") or [])
        if isinstance(row, Mapping) and isinstance(row.get("name"), str)
    }
    runtime_tunnels = {
        str(row.get("name")): row
        for row in (runtime.get("tunnels") or [])
        if isinstance(row, Mapping) and isinstance(row.get("name"), str)
    }

    wanted: list[tuple[str, str | None]] = []
    if selected is None:
        wanted.extend(("service", name) for name in services)
        wanted.extend(("tunnel", name) for name in tunnels)
    else:
        for item in selected:
            kind = item.action.split(".", 1)[0]
            if item.name is not None:
                wanted.append((kind, item.name))
    # Stable unique target order.
    seen_targets: set[tuple[str, str]] = set()
    targets: list[dict[str, Any]] = []
    target_index: dict[tuple[str, str], dict[str, Any]] = {}
    for kind, name in wanted:
        key = _target_key(kind, name)
        if key in seen_targets:
            continue
        seen_targets.add(key)
        if len(targets) >= MAX_TARGETS:
            break
        if kind == "service":
            row = _service_target(name, services.get(name),
                                  runtime_services.get(name),
                                  runtime_error, logs.get(name), log_error)
        else:
            row = _tunnel_target(name, tunnels.get(name), config,
                                 runtime_tunnels.get(name), runtime_error)
        if config_error is not None and row.get("configured") is not True:
            row["readiness"] = UNKNOWN
            row["reason"] = "project configuration could not be read"
        targets.append(row)
        target_index[key] = row

    operations: list[dict[str, Any]] = []
    if selected is None:
        for spec in ACTION_SPECS:
            if spec.name not in _SERVICE_ACTIONS and spec.name not in _TUNNEL_ACTIONS:
                continue
            if spec.name in _LIST_ACTIONS:
                state, reason, requirements = _operation_state(
                    spec.name, None, config_error, runtime_error)
                operations.append(_operation_row(spec.name, None, state, reason,
                                                 requirements))
                continue
            kind = spec.name.split(".", 1)[0]
            names = services if kind == "service" else tunnels
            for name in names:
                target = target_index.get((kind, name))
                state, reason, requirements = _operation_state(
                    spec.name, target, config_error, runtime_error)
                if target is not None:
                    target["operations"][spec.name] = state
                operations.append(_operation_row(spec.name, target, state, reason,
                                                 requirements))
    else:
        for item in selected:
            kind = item.action.split(".", 1)[0]
            target = target_index.get((kind, item.name)) if item.name else None
            state, reason, requirements = _operation_state(
                item.action, target, config_error, runtime_error)
            if target is not None:
                target["operations"][item.action] = state
            operations.append(_operation_row(item.action, target, state, reason,
                                             requirements))

    # A target summary is derived after all requested operations are known.
    # Lifecycle operations drive it: a tunnel's only mutating operation is
    # start, while a service can still be usable when its configured mode
    # blocks the other lifecycle verb.
    for target in targets:
        target_operations = target.get("operations") or {}
        if target_operations:
            mutating = {
                name: state for name, state in target_operations.items()
                if (name in _LIFECYCLE_ACTIONS)
            }
            if not mutating:
                mutating = dict(target_operations)
            state, reason = _target_state(
                operations=[{"readiness": value}
                            for value in mutating.values()])
            target["readiness"] = state
            target["reason"] = reason
        elif config_error is not None:
            target["readiness"] = UNKNOWN
        elif target.get("configured") is not True:
            target["readiness"] = ABSENT
        elif target.get("kind") == "service":
            target["readiness"] = target.get("readiness") or UNKNOWN
        else:
            target["readiness"] = target.get("readiness") or BLOCKED

    overall, overall_reason = _aggregate(
        row["readiness"] for row in operations)
    operation_counts = _counts(operations)
    target_counts = _counts(targets)
    report = {
        "schema": {"namespace": SCHEMA_NAMESPACE, "version": SCHEMA_VERSION},
        "report_version": REPORT_VERSION,
        "root": str(path),
        "selection": "all_configured" if selected is None else "explicit",
        "state": overall,
        "readiness": overall,
        "reason": overall_reason,
        "targets": targets,
        "operations": operations,
        "counts": {"operations": operation_counts, "targets": target_counts},
        "error": config_error or runtime_error,
        "provenance": {
            "reader": "ozagia.core.action_readiness",
            "configuration": "ozagia.config.load_project_config",
            "runtime": "ozagia.core.runtime.runtime_status",
            "logs": "ozagia.core.runtime.RuntimeStore",
            "actions": "ozagia.core.actions.ACTION_SPECS",
            "secrets_resolved": False,
            "previewed": False,
            "applied": False,
            "wrote": False,
        },
        "not_assessed": [
            "secret references were not resolved; provider availability, "
            "permission and credential validity were not checked",
            "process identity is only the existing runtime observation; no "
            "signal, start, stop or additional process probe was performed",
            "live and network gates are never satisfied by readiness; apply "
            "still requires explicit --allow-live/--allow-network and a token",
            "relay connectivity, remote authorization and target reachability "
            "were not tested",
            "log content was not read; only presence and size metadata",
            "no confirmation token, audit record or runtime record was created",
            "daemon and integration actions are outside this join",
            "tunnel.start is always blocked unless explicit live and network "
            "gates are supplied",
            "an unresolved secret reference keeps readiness unknown or blocked",
            "readiness does not call actions.preview or actions.apply",
            "readiness never starts, stops or signals a process",
        ],
        "deferred": [
            "action preview and one-time confirmation tokens",
            "action apply",
            "live tunnel and relay execution",
            "secret resolution at the spawn boundary",
            "service start/stop/restart and process signals",
            "daemon and integration actions",
            "an MCP or CLI surface for readiness",
        ],
    }
    if observed_at is not None:
        report["observed_at"] = observed_at
    return report


__all__ = [
    "ABSENT", "ActionReadinessError", "ActionSelection", "AVAILABLE",
    "BLOCKED", "MAX_SELECTIONS", "REPORT_VERSION", "SCHEMA_NAMESPACE",
    "SCHEMA_VERSION", "STATES", "UNKNOWN", "parse_selections", "readiness",
]
