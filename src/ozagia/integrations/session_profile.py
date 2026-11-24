"""Bounded, metadata-only profile events from command hooks.

Claude Code, Codex and Kiro run one command-hook process per host event. This
module turns such a payload into at most one `profile_event` for the daemon's
existing `.ozagia/session_profiles/` store (`ozagia.session_profile_cli`).
It is not a second store.

Only an allowlist of host-provided metadata leaves the hook: the opaque session
id, a host model name, numeric token counts when the host actually supplies
them, a numeric tool duration when supplied, the tool name, and the lifecycle
outcome. Prompts, source, paths, command text, tool input/output and secret
values are never written. When a host gives no tool id, a digest of its tool
input is used as the opaque event key so a replayed hook event is deduplicated;
only the digest is sent.

Recording is best-effort and fail-open: it never starts a daemon and a missing,
slow or malformed daemon answer is ignored. A hook's own behaviour must not
depend on whether a profile was written.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any

from ozagia.daemon import protocol
from ozagia.integrations.hookcore import daemon_request

# A profile event is optional: never let it consume the hook's budget.
PROFILE_TIMEOUT_S = 0.2
SOURCE = "host_hooks"
SESSION_PHASES = ("start", "end", "compact")
TOOL_STATUSES = ("started", "completed", "error")
_MODEL = re.compile(r"[A-Za-z0-9_.:/+-]{1,160}")
_TOOL = re.compile(r"[A-Za-z0-9_.-]{1,80}")
_SOURCE = re.compile(r"[A-Za-z0-9_.:-]{1,40}")
_SESSION = re.compile(r"[A-Za-z0-9._:-]{1,256}")
_HOST_ID_KEYS = ("tool_use_id", "toolUseId", "tool_call_id", "toolCallId")
_STAMP_KEYS = ("timestamp", "ts", "time")
_TOKEN_KEYS = (
    ("input", ("input", "input_tokens", "prompt_tokens")),
    ("output", ("output", "output_tokens", "completion_tokens")),
    ("reasoning", ("reasoning", "reasoning_tokens")),
    ("cache_read", ("cache_read", "cache_read_input_tokens", "cached_tokens")),
    ("cache_write", ("cache_write", "cache_creation_input_tokens")),
)


def _bounded_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return min(2**53 - 1, max(0, int(value))) if value >= 0 else None
    except (ValueError, OverflowError):
        return None


def _model(payload: dict[str, Any]) -> str | None:
    value = payload.get("model")
    return value if isinstance(value, str) and _MODEL.fullmatch(value) else None


def _tokens(payload: dict[str, Any]) -> dict[str, int] | None:
    """Numeric token counts the host actually supplied, never inferred."""
    raw = payload.get("tokens")
    if not isinstance(raw, dict):
        raw = payload.get("usage")
    if not isinstance(raw, dict):
        return None
    found: dict[str, int] = {}
    for target, names in _TOKEN_KEYS:
        for name in names:
            amount = _bounded_int(raw.get(name))
            if amount is not None:
                found[target] = amount
                break
    return found or None


def _duration_ms(payload: dict[str, Any]) -> int | None:
    for key in ("duration_ms", "durationMs", "elapsed_ms"):
        amount = _bounded_int(payload.get(key))
        if amount is not None:
            return amount
    return None


def _tool_name(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    name = value.strip().lower()
    return name if _TOOL.fullmatch(name) else None


def _host_source(value: object) -> str | None:
    return value if isinstance(value, str) and _SOURCE.fullmatch(value) else None


def _host_id(payload: dict[str, Any]) -> str | None:
    for key in _HOST_ID_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and 0 < len(value) <= 128:
            return value
    return None


def _stamp(payload: dict[str, Any]) -> str | None:
    for key in _STAMP_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and 0 < len(value) <= 64:
            return value
        if isinstance(value, int) and not isinstance(value, bool):
            return str(value)
    return None


def _fingerprint(payload: dict[str, Any]) -> str:
    """An opaque key for a host event that carries no id.

    A Kiro tool event has neither a tool id nor a timestamp in the documented
    payload. The digest lets an identical replayed event deduplicate without
    sending or storing the input itself.
    """
    try:
        text = json.dumps(payload.get("tool_input"), sort_keys=True,
                          default=str, separators=(",", ":"))[:4096]
    except (TypeError, ValueError):
        text = ""
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]


def _event_id(harness: str, session: str, kind: str, event: str,
              outcome: str, payload: dict[str, Any]) -> str:
    parts = [harness, session, kind, event, outcome]
    host_id = _host_id(payload)
    if host_id is not None:
        parts.append(host_id)
    else:
        stamp = _stamp(payload)
        if stamp is not None:
            parts.append(stamp)
        elif kind == "tool":
            parts.append(_fingerprint(payload))
        else:
            # A host session event (start/resume/clear/end) is not replayed by
            # the hook runner, and repeated starts are meaningful. Keep each
            # invocation distinct when the payload has no id or timestamp.
            parts.append(str(time.time_ns()))
    return ":".join(parts)[:256]


def record(root: Path, harness: str, payload: Any, *, kind: str,
           event: str, phase: str | None = None,
           status: str | None = None, tool: object = None) -> bool:
    """Send at most one metadata-only `profile_event`; never raise.

    `kind` is `"session"` (with a phase) or `"tool"` (with a status). True when
    a daemon accepted the event, False when it was skipped or unreachable.
    """
    if not isinstance(payload, dict):
        return False
    session = payload.get("session_id") or payload.get("session")
    if not isinstance(session, str) or not _SESSION.fullmatch(session):
        return False
    metadata: dict[str, Any] = {}
    model = _model(payload)
    if model is not None:
        metadata["model"] = model
    tokens = _tokens(payload)
    if tokens is not None:
        metadata["tokens"] = tokens
    duration = _duration_ms(payload)
    if kind == "session":
        if phase not in SESSION_PHASES:
            return False
        metadata["phase"] = phase
        source = _host_source(payload.get("source"))
        if source is not None:
            metadata["source"] = source
        outcome = phase
    elif kind == "tool":
        if status not in TOOL_STATUSES:
            return False
        name = _tool_name(tool if tool is not None else payload.get("tool_name"))
        if name is None:
            return False
        metadata["tool"] = name
        metadata["status"] = status
        outcome = status
        if duration is not None:
            metadata["duration_ms"] = duration
    else:
        return False
    event_id = _event_id(harness, session, kind, event, outcome, payload)
    try:
        daemon_request(protocol.socket_path(root), {
            "id": 1, "op": "profile_event", "harness": harness,
            "session": session, "source": SOURCE, "kind": kind,
            "event_id": event_id, "metadata": metadata,
        }, PROFILE_TIMEOUT_S)
    except Exception:  # optional metadata: a missing daemon must not block
        return False
    return True
