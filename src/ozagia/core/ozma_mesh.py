"""Read-only identity mapping for the Ozma mesh bridge.

The local session mesh in :mod:`ozagia.core.mesh` is the source of truth.  This
module is the Stage F slice of the Ozma bridge: it validates the owned
``[ozma_mesh]`` configuration, maps a local Ozagia session to a host-supplied
Ozma agent identity, and reports readiness.  It never opens a socket, sends a
message, claims a resource or asks a model for anything.

Ozma identities are supplied by the host, never generated here.  A local card
file is hashed so the configured ``card_fingerprint`` can be compared, but a
matching digest is only a host *declaration*: no signature or PKI verification
is available, so the result is reported as ``host_declared`` and not as a
verified identity.  A missing agent id or card stays ``missing``.

The mapping record lives at
``<state dir>/mesh/ozma/identities/<sha256(flight_id + NUL + ozagia key)>.json``
and is written only by ``map --save``.  ``status`` and ``check`` are read-only.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import os
import re
import stat
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

from ozagia.config import load_config_section, state_dir
from ozagia.core import mesh as M

SCHEMA = 1
SECTION = "ozma_mesh"

MODES = ("off", "status", "messages", "full")
TRANSPORTS = ("native", "a2a")
SCOPES = ("workspace", "user", "global")
WAKE_MODES = ("passive", "message-triggered", "scheduled")
VERIFIERS = ("host", "none")

#: The fields a v1.0 A2A adapter cannot carry faithfully.  Kept in one place so
#: `map` and `check` report the same list.
A2A_LOSS_FIELDS = (
    "criteria",
    "claim_scope",
    "blockers",
    "subscriptions",
    "priority_floor",
    "compliance",
)

DEFAULT_CHANNELS: tuple[str, ...] = ()
DEFAULT_SUBSCRIPTIONS: tuple[str, ...] = ()

MAX_LIST_ITEMS = 64
MAX_CHANNEL = 128
MAX_SUBSCRIPTION = 256
MAX_CARD_PATH = 512
MAX_AUTH_REF = 512
MAX_SHIM_URL = 512
MAX_AGENT_ID = 160

FLIGHT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
AGENT_RE = re.compile(
    r"^agent:[a-z][a-z0-9_-]{0,63}(?::[A-Za-z0-9][A-Za-z0-9._-]{0,63})?$")
FINGERPRINT_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
RATE_RE = re.compile(r"^([1-9][0-9]{0,5})/([1-9][0-9]{0,5})(s|min|h|d)$")
CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")

QUEUE_MAX_MESSAGES_MIN = 1
QUEUE_MAX_MESSAGES_MAX = 10_000
QUEUE_MAX_BYTES_MIN = 1024
QUEUE_MAX_BYTES_MAX = 16 * 1024 * 1024
TTL_MIN_S = 1
TTL_MAX_S = 30 * 24 * 60 * 60
ACK_MIN_S = 1
ACK_MAX_S = 60 * 60
DEDUPE_MIN_S = 0
DEDUPE_MAX_S = 30 * 24 * 60 * 60
PRIORITY_MIN = 0.0
PRIORITY_MAX = 1.0

OWNED_KEYS = frozenset({
    "enabled", "mode", "transport", "shim_url", "allow_remote", "auth_ref",
    "flight_id", "agent_id", "agent_scope", "card_path", "card_fingerprint",
    "verified_by", "wake_modes", "channels", "subscriptions",
    "priority_floor", "queue_max_messages", "queue_max_bytes",
    "outbox_ttl_s", "ack_timeout_s", "dedupe_window_s", "message_rate",
})


class OzmaMeshError(M.MeshError):
    """A Stage F Ozma bridge failure (configuration, IO or schema)."""

    code = "ozma_mesh"


class OzmaMeshConfigError(OzmaMeshError):
    code = "ozma_mesh_config"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code:
            self.code = code


class OzmaMeshIOError(OzmaMeshError):
    code = "ozma_mesh_io"


class OzmaMeshSchemaError(OzmaMeshError):
    code = "ozma_mesh_schema"


@dataclass(frozen=True)
class OzmaMeshConfig:
    """The closed ``[ozma_mesh]`` schema, with every value validated."""

    enabled: bool = False
    mode: str = "off"
    transport: str = "native"
    shim_url: str = ""
    allow_remote: bool = False
    auth_ref: str = ""
    flight_id: str = ""
    agent_id: str = ""
    agent_scope: str = "workspace"
    card_path: str = ""
    card_fingerprint: str = ""
    verified_by: str = "host"
    wake_modes: tuple[str, ...] = ("passive",)
    channels: tuple[str, ...] = DEFAULT_CHANNELS
    subscriptions: tuple[str, ...] = DEFAULT_SUBSCRIPTIONS
    priority_floor: float = 0.3
    queue_max_messages: int = 200
    queue_max_bytes: int = 262_144
    outbox_ttl_s: int = 86_400
    ack_timeout_s: int = 60
    dedupe_window_s: int = 86_400
    message_rate: str = "60/5min"
    configured: bool = False
    sources: Mapping[str, str] = field(default_factory=dict)

    @property
    def effective_mode(self) -> str:
        """The mode that can actually run; ``off`` when the bridge is off."""
        return self.mode if self.enabled else "off"

    def public(self) -> dict[str, Any]:
        """A JSON-safe view with no credential reference value."""
        return {
            "enabled": self.enabled,
            "mode": self.mode,
            "effective_mode": self.effective_mode,
            "transport": self.transport,
            "shim_url": bool(self.shim_url),
            "allow_remote": self.allow_remote,
            "auth_ref_set": bool(self.auth_ref),
            "flight_id": self.flight_id,
            "agent_id": self.agent_id,
            "agent_scope": self.agent_scope,
            "card_path": self.card_path,
            "card_fingerprint": self.card_fingerprint,
            "verified_by": self.verified_by,
            "wake_modes": list(self.wake_modes),
            "channels": list(self.channels),
            "subscriptions": list(self.subscriptions),
            "priority_floor": self.priority_floor,
            "queue_max_messages": self.queue_max_messages,
            "queue_max_bytes": self.queue_max_bytes,
            "outbox_ttl_s": self.outbox_ttl_s,
            "ack_timeout_s": self.ack_timeout_s,
            "dedupe_window_s": self.dedupe_window_s,
            "message_rate": self.message_rate,
            "configured": self.configured,
        }


# --------------------------------------------------------------------------- #
# validation
# --------------------------------------------------------------------------- #


def _fail(message: str, *, code: str | None = None) -> None:
    raise OzmaMeshConfigError(message, code=code)


def _bounded_text(value: Any, name: str, limit: int, *,
                  allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        _fail(f"{name} must be a string")
    if CONTROL_RE.search(value):
        _fail(f"{name} must not contain control characters")
    if len(value) > limit:
        _fail(f"{name} exceeds {limit} characters")
    if not allow_empty and not value:
        _fail(f"{name} must not be empty")
    return value


def _boolean(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        _fail(f"{name} must be true or false")
    return value


def _integer(value: Any, name: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _fail(f"{name} must be an integer")
    if value < low or value > high:
        _fail(f"{name} must be between {low} and {high}")
    return value


def _number(value: Any, name: str, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(f"{name} must be a number")
    number = float(value)
    if number < low or number > high:
        _fail(f"{name} must be between {low:g} and {high:g}")
    return number


def _choice(value: Any, name: str, choices: tuple[str, ...]) -> str:
    if not isinstance(value, str) or value not in choices:
        _fail(f"{name} must be one of " + ", ".join(choices))
    return value


def _string_list(value: Any, name: str, *, limit: int,
                 item_limit: int) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        _fail(f"{name} must be a list of strings")
    if len(value) > limit:
        _fail(f"{name} exceeds {limit} entries")
    result: list[str] = []
    for item in value:
        text = _bounded_text(item, f"{name} entry", item_limit,
                             allow_empty=False)
        if text not in result:
            result.append(text)
    return tuple(result)


def _message_rate(value: Any) -> str:
    text = _bounded_text(value, "message_rate", 64)
    match = RATE_RE.fullmatch(text)
    if not match:
        _fail("message_rate must look like '60/5min'")
    count = int(match.group(1))
    window = int(match.group(2))
    if count > 100_000 or window > 100_000:
        _fail("message_rate count and window are out of bounds")
    return text


def _shim_url(value: Any, *, allow_remote: bool) -> str:
    text = _bounded_text(value, "shim_url", MAX_SHIM_URL, allow_empty=True)
    if not text:
        return ""
    parsed = urlsplit(text)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        _fail("shim_url must be an http(s) URL with a host")
    if parsed.username or parsed.password:
        _fail("shim_url must not contain inline credentials")
    host = parsed.hostname
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = host == "localhost" or host.endswith(".localhost")
    if not loopback and not allow_remote:
        _fail("shim_url must be loopback/localhost unless allow_remote=true")
    return text


def _auth_ref(value: Any) -> str:
    text = _bounded_text(value, "auth_ref", MAX_AUTH_REF, allow_empty=True)
    if not text:
        return ""
    # Deliberately do not echo the value: an attempted inline credential may
    # be a real secret, and a rejected value must not leak into logs.
    if (not text.startswith("secret://") or len(text) == len("secret://")
            or any(char.isspace() for char in text)):
        _fail("auth_ref must be a secret:// reference; inline credentials "
              "are not accepted")
    return text


def _agent_id(value: Any) -> str:
    text = _bounded_text(value, "agent_id", MAX_AGENT_ID, allow_empty=True)
    if text and not AGENT_RE.fullmatch(text):
        _fail("agent_id must look like agent:<role>[:<workunit>]")
    return text


def _card_path(value: Any) -> str:
    text = _bounded_text(value, "card_path", MAX_CARD_PATH, allow_empty=True)
    if not text:
        return ""
    path = Path(text)
    if not path.is_absolute() and ".." in path.parts:
        _fail("card_path must stay inside the repository root unless it "
              "is absolute")
    return text


def _card_fingerprint(value: Any) -> str:
    text = _bounded_text(value, "card_fingerprint", 80, allow_empty=True)
    if not text:
        return ""
    lowered = text.lower()
    if not FINGERPRINT_RE.fullmatch(lowered):
        _fail("card_fingerprint must be sha256:<64 hex characters>")
    return lowered


def load_config(root: str | Path) -> OzmaMeshConfig:
    """Load and validate ``[ozma_mesh]``; default off, unknown keys rejected."""
    root_p = Path(root).resolve()
    section = load_config_section(root_p, SECTION)
    if section.errors:
        first = section.errors[0]
        raise OzmaMeshConfigError(f"{first.path}: {first.message}")
    unknown = sorted(set(section.values) - OWNED_KEYS)
    if unknown:
        raise OzmaMeshConfigError(
            "unknown key(s) in [ozma_mesh]: " + ", ".join(unknown))
    values = section.values

    enabled = _boolean(values.get("enabled", False), "enabled")
    mode = _choice(values.get("mode", "off"), "mode", MODES)
    transport = _choice(values.get("transport", "native"), "transport",
                        TRANSPORTS)
    allow_remote = _boolean(values.get("allow_remote", False), "allow_remote")
    shim_url = _shim_url(values.get("shim_url", ""),
                         allow_remote=allow_remote)
    auth_ref = _auth_ref(values.get("auth_ref", ""))
    flight = _bounded_text(values.get("flight_id", ""), "flight_id", 64,
                           allow_empty=True)
    if flight and not FLIGHT_RE.fullmatch(flight):
        _fail("flight_id must match ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    agent_id = _agent_id(values.get("agent_id", ""))
    agent_scope = _choice(values.get("agent_scope", "workspace"),
                          "agent_scope", SCOPES)
    card_path = _card_path(values.get("card_path", ""))
    card_fingerprint = _card_fingerprint(values.get("card_fingerprint", ""))
    verified_by = _choice(values.get("verified_by", "host"), "verified_by",
                          VERIFIERS)
    wake_modes = _string_list(values.get("wake_modes", ["passive"]),
                              "wake_modes", limit=len(WAKE_MODES),
                              item_limit=32)
    for wake in wake_modes:
        if wake not in WAKE_MODES:
            _fail("wake_modes must be a subset of " + ", ".join(WAKE_MODES))
    if "passive" not in wake_modes:
        wake_modes = ("passive", *wake_modes)
    channels = _string_list(values.get("channels", []), "channels",
                            limit=MAX_LIST_ITEMS, item_limit=MAX_CHANNEL)
    subscriptions = _string_list(values.get("subscriptions", []),
                                  "subscriptions", limit=MAX_LIST_ITEMS,
                                  item_limit=MAX_SUBSCRIPTION)
    priority_floor = _number(values.get("priority_floor", 0.3),
                             "priority_floor", PRIORITY_MIN, PRIORITY_MAX)
    queue_max_messages = _integer(values.get("queue_max_messages", 200),
                                  "queue_max_messages",
                                  QUEUE_MAX_MESSAGES_MIN,
                                  QUEUE_MAX_MESSAGES_MAX)
    queue_max_bytes = _integer(values.get("queue_max_bytes", 262_144),
                               "queue_max_bytes", QUEUE_MAX_BYTES_MIN,
                               QUEUE_MAX_BYTES_MAX)
    outbox_ttl_s = _integer(values.get("outbox_ttl_s", 86_400),
                            "outbox_ttl_s", TTL_MIN_S, TTL_MAX_S)
    ack_timeout_s = _integer(values.get("ack_timeout_s", 60), "ack_timeout_s",
                             ACK_MIN_S, ACK_MAX_S)
    dedupe_window_s = _integer(values.get("dedupe_window_s", 86_400),
                               "dedupe_window_s", DEDUPE_MIN_S, DEDUPE_MAX_S)
    message_rate = _message_rate(values.get("message_rate", "60/5min"))

    if mode in {"messages", "full"} and not (agent_id and card_path):
        _fail(f"mode '{mode}' requires a host-provided agent_id and card_path; "
              "use mode='status' until both are present")

    return OzmaMeshConfig(
        enabled=enabled, mode=mode, transport=transport, shim_url=shim_url,
        allow_remote=allow_remote, auth_ref=auth_ref, flight_id=flight,
        agent_id=agent_id, agent_scope=agent_scope, card_path=card_path,
        card_fingerprint=card_fingerprint, verified_by=verified_by,
        wake_modes=wake_modes, channels=channels,
        subscriptions=subscriptions, priority_floor=priority_floor,
        queue_max_messages=queue_max_messages,
        queue_max_bytes=queue_max_bytes, outbox_ttl_s=outbox_ttl_s,
        ack_timeout_s=ack_timeout_s, dedupe_window_s=dedupe_window_s,
        message_rate=message_rate, configured=bool(section.values_by_source),
        sources=dict(section.sources),
    )


# --------------------------------------------------------------------------- #
# identity and card
# --------------------------------------------------------------------------- #


def _iso(epoch: float | None = None) -> str:
    moment = datetime.fromtimestamp(
        epoch if epoch is not None else datetime.now(tz=timezone.utc).timestamp(),
        tz=timezone.utc)
    return moment.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _flight(root: str | Path, config: OzmaMeshConfig) -> tuple[str, str]:
    if config.flight_id:
        return config.flight_id, "config"
    return M.mesh_id(root), "local_mesh"


def identity(root: str | Path, *, harness: str | None = None,
             session: str | None = None) -> dict[str, Any] | None:
    """The local ``{harness, session, key}`` identity, if one is supplied.

    A missing identity is not an error for read-only status, but half an
    identity is.  The key comes from an existing local mesh registration when
    there is one, otherwise from the same deterministic session-key function.
    """
    harness = harness if harness is not None else os.environ.get(
        "OZAGIA_MESH_HARNESS")
    session = session if session is not None else os.environ.get(
        "OZAGIA_MESH_SESSION")
    if not harness and not session:
        return None
    if not harness or not session:
        raise OzmaMeshConfigError(
            "an Ozagia identity needs both --harness and --session "
            "(or OZAGIA_MESH_HARNESS/OZAGIA_MESH_SESSION)")
    harness = str(harness)
    session = str(session)
    if not M.NAME_RE.fullmatch(harness) or not M.NAME_RE.fullmatch(session):
        raise OzmaMeshConfigError(
            "harness and session must be safe session names",
            code="invalid_identity")
    repo = M.repo_id(root)
    mesh = M.mesh_id(root)
    key = M.session_key(repo, mesh, harness, session)
    registered = False
    effective_state = None
    source = "explicit"
    state = M.status(root, harness=harness, session=session)
    me = state.get("self")
    if me is not None:
        registered = True
        key = str(me.get("key") or key)
        effective_state = me.get("effective_state")
        source = "registration"
    return {
        "harness": harness,
        "session": session,
        "key": key,
        "repo_id": repo,
        "mesh_id": mesh,
        "registered": registered,
        "effective_state": effective_state,
        "source": source,
    }


def _card_file(root: str | Path, config: OzmaMeshConfig) -> Path | None:
    if not config.card_path:
        return None
    path = Path(config.card_path)
    if not path.is_absolute():
        path = Path(root).resolve() / path
    return path


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def card_state(root: str | Path, config: OzmaMeshConfig) -> dict[str, Any]:
    """Hash the local card and compare it with ``card_fingerprint``.

    ``fingerprint`` is ``match``, ``mismatch``, ``missing`` or ``unknown``.
    No signature or PKI check exists, so a match is evidence of file identity
    only.
    """
    expected = config.card_fingerprint or ""
    result: dict[str, Any] = {
        "path": config.card_path,
        "present": False,
        "expected": expected or None,
        "computed": None,
        "fingerprint": "missing",
        "error": None,
    }
    path = _card_file(root, config)
    if path is None:
        return result
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        result["error"] = "card file is missing"
        return result
    except OSError:
        result["fingerprint"] = "unknown"
        result["error"] = "card file cannot be inspected"
        return result
    if stat.S_ISLNK(info.st_mode):
        result["fingerprint"] = "unknown"
        result["error"] = "card file is a symlink; refusing to follow it"
        return result
    if not stat.S_ISREG(info.st_mode):
        result["fingerprint"] = "unknown"
        result["error"] = "card path is not a regular file"
        return result
    result["present"] = True
    try:
        digest = _sha256_file(path)
    except OSError:
        result["fingerprint"] = "unknown"
        result["error"] = "card file cannot be read"
        return result
    result["computed"] = f"sha256:{digest}"
    if not expected:
        result["fingerprint"] = "missing"
        result["error"] = "no card_fingerprint is configured"
    elif hmac.compare_digest(result["computed"], expected):
        result["fingerprint"] = "match"
    else:
        result["fingerprint"] = "mismatch"
    return result


def _verification(config: OzmaMeshConfig, card: Mapping[str, Any]) -> dict[str, Any]:
    if not config.agent_id or not card.get("present"):
        status = "missing"
        note = "an agent id or a readable card file is missing"
    elif card.get("fingerprint") == "mismatch":
        status = "fingerprint_mismatch"
        note = "the card file does not match card_fingerprint"
    elif card.get("fingerprint") == "match":
        if config.verified_by == "host":
            status = "host_declared"
            note = ("the host supplied the id and card; the fingerprint "
                    "matches, but no signature verification is available")
        else:
            status = "fingerprint_matched"
            note = ("the card fingerprint matches, but verified_by is none; "
                    "the host did not declare trust")
    else:
        status = "unverified"
        note = "the card fingerprint is missing or unknown"
    return {
        "status": status,
        "source": "host" if config.verified_by == "host" else "none",
        "cryptographic": False,
        "host_declared": status == "host_declared",
        "note": note,
    }


def _state(config: OzmaMeshConfig, card: Mapping[str, Any],
           verification: Mapping[str, Any]) -> str:
    if not config.enabled or config.mode == "off":
        return "off"
    if card.get("fingerprint") == "mismatch":
        return "mismatch"
    if card.get("fingerprint") == "unknown":
        return "unknown"
    if not config.agent_id or not card.get("present"):
        return "missing"
    status = str(verification.get("status"))
    if status == "host_declared":
        return "host_declared"
    if status in {"fingerprint_matched", "unverified"}:
        return "unverified"
    if status == "missing":
        return "missing"
    return "unknown"


def _ready_for(state: str) -> list[str]:
    if state == "off":
        return ["off"]
    if state in {"mismatch", "unknown"}:
        return []
    if state == "ready":
        return ["status", "messages", "full"]
    return ["status"]


def _readiness(config: OzmaMeshConfig, state: str,
               ozagia: Mapping[str, Any] | None) -> dict[str, Any]:
    effective = config.effective_mode
    ready_for = _ready_for(state)
    reasons: list[str] = []
    if effective == "off":
        ready = state == "off"
        reasons.append("the bridge is off; no network, messages or claims")
    elif state == "mismatch":
        ready = False
        reasons.append("the card fingerprint does not match; this is never "
                       "treated as ready")
    elif state == "unknown":
        ready = False
        reasons.append("the card fingerprint is unknown")
    elif effective == "status":
        ready = state in {"missing", "unverified", "host_declared", "ready"}
        if state == "missing":
            reasons.append("agent id and/or card are missing; status is "
                           "read-only")
        elif state == "unverified":
            reasons.append("the card is not host-verified; status is read-only")
        elif state == "host_declared":
            reasons.append("host-declared identity; ready for status only")
    elif effective in {"messages", "full"}:
        # A fingerprint match with verified_by=host is still only a host
        # declaration.  There is no future verification flag in this slice,
        # so messages/full are never ready and never send anything.
        ready = state == "ready"
        if ozagia is None:
            reasons.append("no local Ozagia identity is supplied")
        if state == "missing":
            reasons.append("agent id and/or card are missing")
        elif state == "unverified":
            reasons.append("the card fingerprint is not host-verified")
        elif state == "host_declared":
            reasons.append("host_declared is not message-ready without an "
                           "explicit future host verification flag")
        elif state == "off":
            reasons.append("the bridge is disabled")
        elif state == "ready":
            reasons.append("ready for the configured mode")
    else:
        ready = False
        reasons.append("the configured mode is not recognised")
    if config.transport == "a2a":
        reasons.append("A2A transport is lossy: " + ", ".join(A2A_LOSS_FIELDS))
    if "scheduled" in config.wake_modes:
        reasons.append("scheduled wake is reported only; it cannot spawn in "
                       "this slice")
    return {
        "mode": config.mode,
        "effective_mode": effective,
        "ready": ready,
        "ready_for": ready_for,
        "reasons": reasons,
    }


def mapping(root: str | Path, *, harness: str | None = None,
            session: str | None = None,
            config: OzmaMeshConfig | None = None) -> dict[str, Any]:
    """Build the read-only Ozagia -> Ozma identity mapping."""
    cfg = config if config is not None else load_config(root)
    flight_id, flight_source = _flight(root, cfg)
    card = card_state(root, cfg)
    verification = _verification(cfg, card)
    state = _state(cfg, card, verification)
    ozagia = identity(root, harness=harness, session=session)
    result: dict[str, Any] = {
        "schema": SCHEMA,
        "section": SECTION,
        "configured": cfg.configured,
        "enabled": cfg.enabled,
        "mode": cfg.mode,
        "effective_mode": cfg.effective_mode,
        "state": state,
        "flight": {"id": flight_id, "source": flight_source},
        "flight_id": flight_id,
        "flight_source": flight_source,
        "agent_id": cfg.agent_id,
        "agent_id_present": bool(cfg.agent_id),
        "agent_scope": cfg.agent_scope,
        "card_fingerprint": cfg.card_fingerprint,
        "card_fingerprint_state": card["fingerprint"],
        "verification_status": verification["status"],
        "ozagia": ozagia,
        "ozma": {
            "agent_id": cfg.agent_id,
            "agent_present": bool(cfg.agent_id),
            "scope": cfg.agent_scope,
            "card_path": cfg.card_path,
            "card_fingerprint": cfg.card_fingerprint,
            "transport": cfg.transport,
        },
        "verification": verification,
        "card": card,
        "transport": cfg.transport,
        "lossy": cfg.transport == "a2a",
        "loss_fields": list(A2A_LOSS_FIELDS) if cfg.transport == "a2a" else [],
        "wake_modes": list(cfg.wake_modes),
        "channels": list(cfg.channels),
        "subscriptions": list(cfg.subscriptions),
        "queue": {
            "max_messages": cfg.queue_max_messages,
            "max_bytes": cfg.queue_max_bytes,
        },
        "priority_floor": cfg.priority_floor,
        "outbox_ttl_s": cfg.outbox_ttl_s,
        "ack_timeout_s": cfg.ack_timeout_s,
        "dedupe_window_s": cfg.dedupe_window_s,
        "message_rate": cfg.message_rate,
        "network": False,
    }
    result["readiness"] = _readiness(cfg, state, ozagia)
    return result


# --------------------------------------------------------------------------- #
# mapping record
# --------------------------------------------------------------------------- #


def mapping_record_path(root: str | Path, flight_id: str,
                        ozagia_key: str) -> Path:
    """Where the mapping record for this flight and local key lives."""
    digest = hashlib.sha256(
        f"{flight_id}\0{ozagia_key}".encode("utf-8")).hexdigest()
    return state_dir(root) / "mesh" / "ozma" / "identities" / f"{digest}.json"


def _record_exists(path: Path) -> bool:
    try:
        info = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISREG(info.st_mode)


def _ensure_private_dir(path: Path) -> None:
    """Create only the missing directory chain below the state dir."""
    missing: list[Path] = []
    current = path
    while True:
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            missing.append(current)
            if current.parent == current:
                break
            current = current.parent
            continue
        except OSError as exc:
            raise OzmaMeshIOError(f"cannot inspect {current}: {exc}") from exc
        if stat.S_ISLNK(info.st_mode):
            raise OzmaMeshIOError(f"refusing symlinked state path: {current}")
        if not stat.S_ISDIR(info.st_mode):
            raise OzmaMeshIOError(f"not a directory: {current}")
        break
    for item in reversed(missing):
        try:
            os.mkdir(item, 0o700)
        except FileExistsError:
            pass
        except OSError as exc:
            raise OzmaMeshIOError(f"cannot create {item}: {exc}") from exc
        try:
            os.chmod(item, 0o700)
        except OSError as exc:
            raise OzmaMeshIOError(f"cannot secure {item}: {exc}") from exc
    try:
        os.chmod(path, 0o700)
    except OSError as exc:
        raise OzmaMeshIOError(f"cannot secure {path}: {exc}") from exc


def _fsync_dir(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    _ensure_private_dir(path.parent)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True,
                      allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        _fsync_dir(path.parent)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _read_json(path: Path) -> dict[str, Any] | None:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise OzmaMeshIOError(f"cannot read {path}: {exc}") from exc
    try:
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as exc:
        raise OzmaMeshSchemaError(f"cannot parse {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise OzmaMeshSchemaError(f"{path}: record must be a JSON object")
    return data


def _record(mapping: Mapping[str, Any]) -> dict[str, Any]:
    flight = mapping.get("flight") or {}
    ozagia = mapping.get("ozagia") or {}
    ozma = mapping.get("ozma") or {}
    verification = mapping.get("verification") or {}
    return {
        "schema": SCHEMA,
        "flight_id": flight.get("id"),
        "ozagia": {
            "harness": ozagia.get("harness"),
            "session": ozagia.get("session"),
            "key": ozagia.get("key"),
        },
        "ozma": {
            "agent_id": ozma.get("agent_id"),
            "agent_scope": ozma.get("scope"),
            "card_path": ozma.get("card_path"),
            "card_fingerprint": ozma.get("card_fingerprint"),
            "transport": ozma.get("transport"),
        },
        "verification": {
            "status": verification.get("status"),
            "source": verification.get("source"),
            "cryptographic": False,
        },
        "transport": mapping.get("transport"),
        "lossy": bool(mapping.get("lossy")),
        "loss_fields": list(mapping.get("loss_fields") or ()),
        "wake_modes": list(mapping.get("wake_modes") or ()),
        "channels": list(mapping.get("channels") or ()),
        "subscriptions": list(mapping.get("subscriptions") or ()),
        "status": mapping.get("state"),
        "updated_at": _iso(),
    }


def save_mapping(root: str | Path,
                 mapping_record: Mapping[str, Any]) -> Path:
    """Write the mapping record atomically; never stores a credential value."""
    ozagia = mapping_record.get("ozagia") or {}
    key = str(ozagia.get("key") or "")
    flight = mapping_record.get("flight") or {}
    flight_id = str(flight.get("id") or "")
    if not key or not flight_id:
        raise OzmaMeshConfigError(
            "map --save needs a flight id and an Ozagia identity")
    path = mapping_record_path(root, flight_id, key)
    record = _record(mapping_record)
    existing = _read_json(path) if _record_exists(path) else None
    if existing is not None:
        if existing.get("schema") != SCHEMA:
            raise OzmaMeshSchemaError(f"{path}: unsupported mapping schema")
        if existing.get("flight_id") != flight_id:
            raise OzmaMeshSchemaError(
                f"{path}: mapping belongs to another flight")
        old = existing.get("ozagia") or {}
        if old.get("key") != key:
            raise OzmaMeshSchemaError(
                f"{path}: mapping belongs to another Ozagia identity")
        if existing.get("created_at"):
            record["created_at"] = existing["created_at"]
    record.setdefault("created_at", record["updated_at"])
    _write_json(path, record)
    return path


def map_report(root: str | Path, *, harness: str | None = None,
               session: str | None = None, save: bool = False,
               config: OzmaMeshConfig | None = None) -> dict[str, Any]:
    """The ``map`` payload: mapping, loss table and optional record write."""
    cfg = config if config is not None else load_config(root)
    result = mapping(root, harness=harness, session=session, config=cfg)
    flight = result["flight"]
    ozagia = result.get("ozagia") or {}
    record: dict[str, Any] = {
        "path": None, "exists": False, "saved": False,
    }
    if ozagia:
        path = mapping_record_path(root, str(flight["id"]),
                                   str(ozagia["key"]))
        record["path"] = str(path)
        record["exists"] = _record_exists(path)
        if save:
            save_mapping(root, result)
            record["exists"] = True
            record["saved"] = True
    elif save:
        raise OzmaMeshConfigError(
            "map --save needs a local identity: --harness/--session or "
            "OZAGIA_MESH_HARNESS/OZAGIA_MESH_SESSION")
    result["loss_table"] = [
        {
            "field": name,
            "native": "preserved",
            "a2a": "lost",
        }
        for name in A2A_LOSS_FIELDS
    ]
    result["record"] = record
    result["network"] = False
    return result


# --------------------------------------------------------------------------- #
# read-only status and check
# --------------------------------------------------------------------------- #


def _shim(config: OzmaMeshConfig) -> dict[str, Any]:
    host = "none"
    if config.shim_url:
        parsed = urlsplit(config.shim_url)
        name = parsed.hostname or ""
        try:
            host = ("loopback" if ipaddress.ip_address(name).is_loopback
                    else "remote")
        except ValueError:
            host = ("loopback" if name == "localhost"
                    or name.endswith(".localhost") else "remote")
    return {
        "configured": bool(config.shim_url),
        "host": host,
        "allow_remote": config.allow_remote,
        "network": False,
    }


def _wake(config: OzmaMeshConfig) -> dict[str, Any]:
    scheduled = "scheduled" in config.wake_modes
    triggered = "message-triggered" in config.wake_modes
    return {
        "passive": "passive" in config.wake_modes,
        "message_triggered": {
            "requested": triggered,
            "spawnable": False,
            "reason": ("message-triggered wake needs host approval and a "
                       "budget; this slice is status-only")
            if triggered else None,
        },
        "scheduled": {
            "requested": scheduled,
            "approved": scheduled and config.verified_by == "host",
            "spawnable": False,
            "reason": ("scheduled wake needs host approval and a budget; this "
                       "slice is status-only") if scheduled else None,
        },
    }


def status(root: str | Path, *, harness: str | None = None,
           session: str | None = None,
           config: OzmaMeshConfig | None = None) -> dict[str, Any]:
    """Report configuration, identity mapping and readiness; no writes."""
    cfg = config if config is not None else load_config(root)
    mapped = mapping(root, harness=harness, session=session, config=cfg)
    flight = mapped["flight"]
    ozagia = mapped.get("ozagia")
    payload: dict[str, Any] = {
        "schema": SCHEMA,
        "section": SECTION,
        "configured": cfg.configured,
        "enabled": cfg.enabled,
        "mode": cfg.mode,
        "effective_mode": cfg.effective_mode,
        "state": mapped["state"],
        "transport": cfg.transport,
        "lossy": mapped["lossy"],
        "loss_fields": mapped["loss_fields"],
        "flight": flight,
        "flight_id": flight["id"],
        "flight_source": flight["source"],
        "agent": {
            "id": cfg.agent_id,
            "present": bool(cfg.agent_id),
            "scope": cfg.agent_scope,
        },
        "agent_id": cfg.agent_id,
        "agent_id_present": bool(cfg.agent_id),
        "agent_scope": cfg.agent_scope,
        "card_fingerprint": cfg.card_fingerprint,
        "card_fingerprint_state": mapped["card"]["fingerprint"],
        "verification_status": mapped["verification"]["status"],
        "card": mapped["card"],
        "verification": mapped["verification"],
        "wake_modes": mapped["wake_modes"],
        "wake": _wake(cfg),
        "channels": {"count": len(cfg.channels),
                     "items": list(cfg.channels)},
        "subscriptions": {"count": len(cfg.subscriptions),
                          "items": list(cfg.subscriptions)},
        "channel_count": len(cfg.channels),
        "subscription_count": len(cfg.subscriptions),
        "queue": mapped["queue"],
        "queue_max_messages": cfg.queue_max_messages,
        "queue_max_bytes": cfg.queue_max_bytes,
        "priority_floor": cfg.priority_floor,
        "outbox_ttl_s": cfg.outbox_ttl_s,
        "ack_timeout_s": cfg.ack_timeout_s,
        "dedupe_window_s": cfg.dedupe_window_s,
        "message_rate": cfg.message_rate,
        "auth_ref_set": bool(cfg.auth_ref),
        "shim": _shim(cfg),
        "identity": ozagia,
        "mapping": None,
        "readiness": mapped["readiness"],
        "network": False,
    }
    if ozagia:
        path = mapping_record_path(root, str(flight["id"]),
                                   str(ozagia["key"]))
        payload["mapping"] = {
            "path": str(path),
            "exists": _record_exists(path),
        }
    return payload


def _live(live: bool) -> dict[str, Any]:
    if not live:
        return {
            "requested": False,
            "state": "not_requested",
            "network": False,
        }
    return {
        "requested": True,
        "state": "unavailable",
        "code": "live_not_configured",
        "reason": ("no approved Ozma shim contract exists in this slice; "
                   "nothing was contacted"),
        "network": False,
    }


def check(root: str | Path, *, harness: str | None = None,
          session: str | None = None, live: bool = False,
          config: OzmaMeshConfig | None = None) -> dict[str, Any]:
    """Validate readiness for the configured mode; never calls out."""
    cfg = config if config is not None else load_config(root)
    mapped = mapping(root, harness=harness, session=session, config=cfg)
    readiness = mapped["readiness"]
    live_info = _live(live)
    flight = mapped["flight"]
    return {
        "schema": SCHEMA,
        "section": SECTION,
        "state": mapped["state"],
        "mode": cfg.mode,
        "effective_mode": cfg.effective_mode,
        "ready": readiness["ready"],
        "ready_for": readiness["ready_for"],
        "reasons": readiness["reasons"],
        "flight": flight,
        "flight_id": flight["id"],
        "flight_source": flight["source"],
        "transport": cfg.transport,
        "lossy": mapped["lossy"],
        "loss_fields": mapped["loss_fields"],
        "agent": {
            "id": cfg.agent_id,
            "present": bool(cfg.agent_id),
            "scope": cfg.agent_scope,
        },
        "agent_id": cfg.agent_id,
        "agent_id_present": bool(cfg.agent_id),
        "agent_scope": cfg.agent_scope,
        "card": mapped["card"],
        "card_fingerprint": cfg.card_fingerprint,
        "card_fingerprint_state": mapped["card"]["fingerprint"],
        "verification": mapped["verification"],
        "verification_status": mapped["verification"]["status"],
        "identity": mapped.get("ozagia"),
        "live": live_info,
        "live_state": live_info["state"],
        "live_code": live_info.get("code"),
        "network": False,
    }


__all__ = [
    "A2A_LOSS_FIELDS",
    "MODES",
    "OzmaMeshConfig",
    "OzmaMeshConfigError",
    "OzmaMeshError",
    "OzmaMeshIOError",
    "OzmaMeshSchemaError",
    "SCHEMA",
    "SECTION",
    "SCOPES",
    "TRANSPORTS",
    "WAKE_MODES",
    "card_state",
    "check",
    "identity",
    "load_config",
    "map_report",
    "mapping",
    "mapping_record_path",
    "save_mapping",
    "status",
]
