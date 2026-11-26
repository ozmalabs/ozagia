"""Bidirectional Ozma mesh bridge: typed messages, claims and a bounded inbox.

This is the Stage G slice of the Ozma bridge.  The read-only identity mapping
lives in :mod:`ozagia.core.ozma_mesh`; the local session mesh in
:mod:`ozagia.core.mesh` remains the source of truth.  This module translates
between the two, records durable outbox/inbox/claim state below
``<state dir>/mesh/ozma/`` and defines a small shim seam.

There is no approved Ozma shim contract or upstream envelope in this checkout,
so the envelope in this module is a **closed local bridge schema** (``schema:
1``).  Its mapping is *declared* from the plan's table and is reported as
``declared``/``unverified`` -- never as tested upstream conformance.  The
production shim (:class:`UnavailableShim`) raises ``shim_unavailable`` and opens
no socket.  A fake in-process shim exists only in tests.

Safety properties enforced here:

* remote bodies are bounded, redacted on ingest and marked untrusted;
* claims are advisory and never become local leases or merge authority;
* list views omit bodies; a body is returned only by an explicit read;
* unknown local kinds, unknown envelope fields and unknown typed body fields
  are rejected rather than dropped;
* every store read works when the store is absent and creates nothing.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

from ozagia.config import state_dir
from ozagia.core import mesh as M
from ozagia.core import ozma_mesh as O

SCHEMA = 1
BRIDGE_NAME = "ozagia.ozma_bridge"
CONFORMANCE = "declared"
CONFORMANCE_NOTE = (
    "declared from the plan's mapping table; no approved Ozma shim contract "
    "or upstream envelope exists in this checkout, so conformance is "
    "unverified")

OZMA_DIRNAME = "ozma"
OUTBOX_DIR = "outbox"
INBOX_DIR = "inbox"
CLAIMS_DIR = "claims"
DEDUPE_DIR = "dedupe"
HANDOFFS_DIR = "handoffs"
BLOCKERS_DIR = "blockers"
WAKE_DIR = "wake"
LOCK_FILE = ".lock"

DIRECTIONS = ("outbound", "inbound")
OZMA_TYPES = (
    "request", "response", "ack", "block", "unblock", "handoff", "claim",
    "release", "heads-up", "status",
)
OUTBOX_STATES = ("pending", "sent", "acked", "expired", "failed")
INBOX_STATES = ("unread", "read", "acked")
OUTBOX_TERMINAL_STATES = frozenset({"sent", "acked", "expired"})
CLAIM_STATES = ("queued", "sent", "active", "expired", "released")
CLAIM_SCOPES = ("worktree", "branch", "file", "subtree", "area")
CLAIM_MODES = ("read", "write", "merge")
REQUEST_PURPOSES = ("general", "review", "question")
COMPLETIONS = ("accepted", "rejected", "done", "partial", "declined")
HANDOFF_STATES = ("pending", "accepted", "rejected", "complete")
BLOCKER_STATES = ("open", "cleared")
WAKE_REQUEST_MODES = ("message", "scheduled")
WAKE_APPROVAL_MODES = ("message-triggered", "scheduled")
WAKE_REQUEST_STATES = ("pending", "claimed")
WAKE_APPROVAL_STATES = ("active", "revoked")

#: The plan's declared local -> Ozma table.  ``offer`` is a caller choice and
#: ``hello`` is identity metadata, never a peer message.
LOCAL_TO_OZMA: dict[str, str] = {
    "request": "request",
    "accept": "ack",
    "reject": "ack",
    "offer": "response|handoff",
    "status": "status",
    "release": "release",
    "note": "heads-up",
    "block": "block",
    "unblock": "unblock",
    "handoff": "handoff",
}
OZMA_TO_LOCAL: dict[str, str] = {
    "request": "request",
    "response": "offer",
    "ack": "accept|reject",
    "block": "block",
    "unblock": "unblock",
    "handoff": "handoff",
    "claim": "remote-claim",
    "release": "release",
    "heads-up": "note",
    "status": "status",
}
IDENTITY_ONLY_KINDS = ("hello",)
GRANT_MAPPING = "unsupported"

MAX_ID = 160
MAX_THREAD = 160
MAX_REFS = 16
MAX_REF = 200
MAX_COMPLIANCE = 16
MAX_COMPLIANCE_ITEM = 64
MAX_CHANNEL = 128
MAX_WORKUNIT = 128
MAX_REASON = 300
MAX_SUBJECT = 120
MAX_BODY = 1024
MAX_RESOURCE = 512
MAX_CRITERIA = 16
MAX_CRITERION = 200
MAX_RECORDS = 10_000
MAX_CLAIMS = 2_000
MAX_HANDOFFS = 500
MAX_BLOCKER_EDGES = 256
MAX_WAKE_APPROVALS = 64
MAX_WAKE_REQUESTS = 500
MAX_WAKE_AUDIT = 200
MAX_WAKE_BUDGET = 100
MIN_WAKE_TTL_S = 60
MAX_WAKE_TTL_S = 7 * 24 * 60 * 60
MAX_FLIGHT_MEMBERS = 64
MAX_FLIGHT_CLAIMS = 16
MAX_FLIGHT_BLOCKERS = 16
MAX_FLIGHT_HANDOFFS = 16
DEDUPE_MAX_ENTRIES = 2000
DEFAULT_POLL_LIMIT = 100
MAX_POLL_LIMIT = 500

ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
THREAD_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/#-]{0,159}$")
CHANNEL_RE = re.compile(r"^#[A-Za-z0-9][A-Za-z0-9._:-]{0,126}$")
WORKUNIT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")

#: The closed set of envelope fields.  Anything else is rejected.
ENVELOPE_FIELDS = frozenset({
    "schema", "bridge", "flight_id", "id", "type", "from", "from_card", "to",
    "in_reply_to", "thread", "refs", "priority", "deliver_at", "requires_ack",
    "compliance", "channel", "workunit", "body", "body_trust", "direction",
    "created_at", "expires_at", "digest", "redactions", "wake", "wake_at",
})

#: The closed typed body shape for each Ozma type.  Values are validators.
BODY_FIELDS: dict[str, frozenset[str]] = {
    "request": frozenset({"purpose", "subject", "text"}),
    "response": frozenset({"accepted", "criteria", "subject", "text"}),
    "ack": frozenset({"accepted", "reason", "text"}),
    "status": frozenset({"subject", "text"}),
    "release": frozenset({"claim_id"}),
    "heads-up": frozenset({"subject", "text"}),
    "block": frozenset({"reason", "resource", "blocked_id", "blocker_id"}),
    "unblock": frozenset({"reason", "resource", "blocked_id", "blocker_id"}),
    "handoff": frozenset({
        "workunit", "criteria", "refs", "completion", "subject", "text",
    }),
    "claim": frozenset({
        "scope", "resource", "mode", "reason", "lease_id", "expires_at",
    }),
}

#: Fields whose values are remote free text.  Their presence means the body is
#: untrusted even when the shim did not say so.
TEXT_BODY_FIELDS = frozenset({
    "subject", "text", "reason", "criteria", "refs",
})


class OzmaBridgeRefusal(M.MeshRefusal):
    """A valid refusal (queue full, rate limited, conflict, unsupported)."""

    code = "ozma_bridge_refusal"


class OzmaBridgeError(O.OzmaMeshError):
    """A bridge configuration, IO or schema failure."""

    code = "ozma_bridge"


class OzmaBridgeIOError(OzmaBridgeError):
    code = "ozma_bridge_io"


class OzmaBridgeSchemaError(OzmaBridgeError):
    code = "ozma_bridge_schema"


# --------------------------------------------------------------------------- #
# small validation helpers
# --------------------------------------------------------------------------- #
def _refuse(message: str, *, code: str) -> None:
    raise OzmaBridgeRefusal(message, code=code)


def _text(value: Any, name: str, limit: int, *, allow_empty: bool = False,
          one_line: bool = False) -> str:
    if not isinstance(value, str):
        _refuse(f"{name} must be text", code="invalid_value")
    if not allow_empty and not value:
        _refuse(f"{name} must not be empty", code="invalid_value")
    if len(value) > limit:
        _refuse(f"{name} exceeds {limit} characters", code="too_large")
    if CONTROL_RE.search(value):
        _refuse(f"{name} contains control characters", code="control_character")
    if one_line and ("\n" in value or "\r" in value or "\t" in value):
        _refuse(f"{name} must be one line", code="control_character")
    return value


def _optional_text(value: Any, name: str, limit: int, *,
                   one_line: bool = False) -> str | None:
    if value is None:
        return None
    return _text(value, name, limit, one_line=one_line)


def _flag(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        _refuse(f"{name} must be true or false", code="invalid_value")
    return value


def _number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _refuse(f"{name} must be a number", code="invalid_value")
    number = float(value)
    if not 0.0 <= number <= 1.0:
        _refuse(f"{name} must be between 0 and 1", code="invalid_value")
    return number


def _string_list(value: Any, name: str, *, limit: int,
                 item_limit: int) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        _refuse(f"{name} must be a list of strings", code="invalid_value")
    if len(value) > limit:
        _refuse(f"{name} exceeds {limit} entries", code="too_large")
    result: list[str] = []
    for item in value:
        text = _text(item, f"{name} entry", item_limit)
        if text not in result:
            result.append(text)
    return result


def _iso(epoch: float | None = None) -> str:
    stamp = datetime.fromtimestamp(
        time.time() if epoch is None else epoch, tz=timezone.utc)
    return stamp.isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_iso(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _digest(value: Mapping[str, Any]) -> str:
    # Timestamps are transport bookkeeping, not content: excluding them keeps
    # a re-enqueued or retried message on the same digest and id.
    payload = {key: item for key, item in value.items()
               if key not in {"digest", "created_at", "expires_at"}}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=True, allow_nan=False)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def envelope_digest(envelope: Mapping[str, Any]) -> str:
    """The canonical digest of one envelope (the ``digest`` field excluded)."""
    return _digest(envelope)


def dedupe_key(flight_id: str, ozma_message_id: str, direction: str) -> str:
    """The ``(flight, message id, direction)`` dedupe key, hashed for storage."""
    if direction not in DIRECTIONS:
        _refuse("direction must be outbound or inbound", code="invalid_value")
    return hashlib.sha256(
        f"{flight_id}\0{ozma_message_id}\0{direction}".encode("utf-8")
    ).hexdigest()[:32]


def record_key(flight_id: str, ozma_message_id: str, direction: str) -> str:
    """The filename stem for a store record (no raw ids in the name)."""
    return dedupe_key(flight_id, ozma_message_id, direction)


def mapping_table() -> list[dict[str, Any]]:
    """The declared mapping table, with hello and grant stated explicitly."""
    rows = [
        {"local": "hello", "ozma": None, "note": "identity metadata; not a peer message"},
        {"local": "offer", "ozma": "response|handoff",
         "note": "caller chooses; the choice is recorded"},
        {"local": "accept", "ozma": "ack", "note": "ack{accepted:true}"},
        {"local": "reject", "ozma": "ack", "note": "ack{accepted:false,reason}"},
    ]
    for local, ozma in LOCAL_TO_OZMA.items():
        if local in {"offer", "accept", "reject"}:
            continue
        rows.append({"local": local, "ozma": ozma, "note": "declared"})
    rows.append({"local": "lease", "ozma": "claim",
                 "note": "advisory; grant is unsupported and merge is advisory"})
    return rows


# --------------------------------------------------------------------------- #
# typed translation
# --------------------------------------------------------------------------- #
def _free_text(subject: str, body: str) -> tuple[str, bool]:
    """Bounded free text and whether the caller supplied any."""
    subject = _text(subject or "", "subject", MAX_SUBJECT, allow_empty=True,
                    one_line=True)
    body = _text(body or "", "body", MAX_BODY, allow_empty=True)
    text = subject if subject and not body else body
    if subject and body and subject not in body:
        text = f"{subject}\n{body}"
    if len(text) > MAX_BODY:
        _refuse(f"combined subject and body exceed {MAX_BODY} characters",
                code="too_large")
    return text, bool(text)


def translate_local(kind: str, *, subject: str = "", body: str = "",
                    offer_as: str | None = None,
                    purpose: str = "general",
                    accepted: bool | None = None,
                    reason: str = "",
                    workunit: str | None = None,
                    criteria: Sequence[str] | None = None,
                    completion: str | None = None,
                    resource: str | None = None,
                    blocked_id: str | None = None,
                    blocker_id: str | None = None,
                    claim_scope: str | None = None,
                    claim_mode: str | None = None,
                    claim_id: str | None = None) -> dict[str, Any]:
    """Translate one local kind to a declared Ozma type and bounded body.

    Free-form local text is carried only in ``request``/``response``/
    ``heads-up``/``status``/``ack`` text or ``reason`` fields, always marked
    ``body_trust: untrusted``.  No typed verdict is fabricated.
    """
    if kind in IDENTITY_ONLY_KINDS:
        _refuse("hello is identity metadata, not a peer message",
                code="identity_metadata")
    if kind not in LOCAL_TO_OZMA:
        _refuse(f"unsupported local message kind: {kind}", code="unsupported_kind")
    text, has_text = _free_text(subject, body)
    translated: dict[str, Any]
    if kind == "request":
        if purpose not in REQUEST_PURPOSES:
            _refuse("request purpose must be general, review or question",
                    code="invalid_value")
        translated = {"type": "request",
                      "body": {"purpose": purpose, "text": text} if text
                      else {"purpose": purpose}}
    elif kind == "accept":
        translated = {"type": "ack",
                      "body": {"accepted": True,
                               **({"text": text} if text else {})}}
    elif kind == "reject":
        why = _text(reason or text, "reason", MAX_REASON, allow_empty=True,
                    one_line=True)
        translated = {"type": "ack",
                      "body": {"accepted": False,
                               **({"reason": why} if why else {})}}
    elif kind == "offer":
        if offer_as not in {"response", "handoff"}:
            _refuse("offer needs --offer-as response|handoff", code="offer_choice")
        typed: dict[str, Any] = {}
        if text:
            typed["text"] = text
        if accepted is not None:
            typed["accepted"] = _flag(accepted, "accepted")
        if criteria:
            typed["criteria"] = _string_list(
                list(criteria), "criteria", limit=MAX_CRITERIA,
                item_limit=MAX_CRITERION)
        translated = {"type": offer_as, "body": typed, "choice": offer_as}
    elif kind == "status":
        translated = {"type": "status",
                      "body": {"text": text} if text else {"text": "status"}}
    elif kind == "release":
        translated = {"type": "release",
                      "body": {"claim_id": claim_id} if claim_id else {}}
        if claim_id:
            translated["in_reply_to"] = claim_id
    elif kind == "note":
        translated = {"type": "heads-up",
                      "body": {"text": text} if text else {"text": "note"}}
    elif kind == "block":
        why = _text(reason or text, "reason", MAX_REASON, allow_empty=True,
                    one_line=True)
        translated = {"type": "block",
                      "body": {**({"reason": why} if why else {}),
                               **({"resource": _text(resource, "resource",
                                                     MAX_RESOURCE,
                                                     one_line=True)}
                                  if resource else {}),
                               **({"blocked_id": _text(
                                   blocked_id, "blocked_id", MAX_ID,
                                   one_line=True)} if blocked_id else {}),
                               **({"blocker_id": _text(
                                   blocker_id, "blocker_id", MAX_ID,
                                   one_line=True)} if blocker_id else {})}}
    elif kind == "unblock":
        why = _text(reason or text, "reason", MAX_REASON, allow_empty=True,
                    one_line=True)
        translated = {"type": "unblock",
                      "body": {**({"reason": why} if why else {}),
                               **({"resource": _text(resource, "resource",
                                                     MAX_RESOURCE,
                                                     one_line=True)}
                                  if resource else {}),
                               **({"blocked_id": _text(
                                   blocked_id, "blocked_id", MAX_ID,
                                   one_line=True)} if blocked_id else {}),
                               **({"blocker_id": _text(
                                   blocker_id, "blocker_id", MAX_ID,
                                   one_line=True)} if blocker_id else {})}}
    elif kind == "handoff":
        data: dict[str, Any] = {}
        if text:
            data["text"] = text
        if workunit:
            data["workunit"] = _text(workunit, "workunit", MAX_WORKUNIT,
                                     one_line=True)
        if criteria:
            data["criteria"] = _string_list(
                list(criteria), "criteria", limit=MAX_CRITERIA,
                item_limit=MAX_CRITERION)
        if completion:
            if completion not in COMPLETIONS:
                _refuse("completion is not recognised", code="invalid_value")
            data["completion"] = completion
        translated = {"type": "handoff", "body": data}
    else:  # pragma: no cover - the table is closed above
        _refuse(f"unsupported local message kind: {kind}", code="unsupported_kind")
    translated["declared"] = True
    translated["conformance"] = CONFORMANCE
    if has_text or "reason" in translated.get("body", {}):
        translated["body_trust"] = "untrusted"
    else:
        translated["body_trust"] = "typed"
    if claim_scope or claim_mode:
        translated["claim"] = {
            "scope": claim_scope, "mode": claim_mode,
            "resource": resource, "advisory": kind == "offer",
        }
    return translated


def translate_ozma(ozma_type: str, body: Mapping[str, Any] | None = None
                   ) -> dict[str, Any]:
    """Translate one declared Ozma type back to a local kind.

    ``claim`` is not a local mailbox kind: it returns ``local: false`` so the
    caller records a remote claim instead of injecting a local message.
    """
    if ozma_type == "hello":
        _refuse("hello is identity metadata, not a peer message",
                code="identity_metadata")
    if ozma_type not in OZMA_TO_LOCAL:
        _refuse(f"unsupported Ozma type: {ozma_type}", code="unsupported_kind")
    data = dict(body or {})
    subject = str(data.get("subject") or data.get("text") or "")
    text = str(data.get("text") or data.get("reason") or "")
    result: dict[str, Any] = {
        "ozma_type": ozma_type,
        "local": ozma_type != "claim",
        "body_trust": "untrusted" if any(
            field in data for field in TEXT_BODY_FIELDS) else "typed",
        "declared": True,
        "conformance": CONFORMANCE,
    }
    if ozma_type == "response":
        if data.get("accepted") is True:
            result["kind"] = "accept"
        elif data.get("accepted") is False:
            result["kind"] = "reject"
        else:
            result["kind"] = "offer"
            result["offer_kind"] = "response"
    elif ozma_type == "ack":
        if "accepted" not in data:
            _refuse("ack needs accepted", code="invalid_value")
        result["kind"] = "accept" if data.get("accepted") is True else "reject"
    elif ozma_type == "claim":
        result["kind"] = None
        result["advisory"] = True
    else:
        result["kind"] = OZMA_TO_LOCAL[ozma_type]
    result["subject"] = subject[:MAX_SUBJECT]
    result["body"] = text[:MAX_BODY]
    if ozma_type == "handoff":
        result["workunit"] = data.get("workunit")
        result["criteria"] = list(data.get("criteria") or ())
        result["completion"] = data.get("completion")
    return result


# --------------------------------------------------------------------------- #
# envelope construction and validation
# --------------------------------------------------------------------------- #
def _validate_body(ozma_type: str, body: Any) -> dict[str, Any]:
    if not isinstance(body, Mapping):
        _refuse("body must be an object", code="invalid_value")
    allowed = BODY_FIELDS[ozma_type]
    unknown = sorted(set(body) - allowed)
    if unknown:
        _refuse(f"unknown {ozma_type} body field(s): " + ", ".join(unknown),
                code="unsupported_field")
    result: dict[str, Any] = {}
    if "purpose" in body:
        value = body["purpose"]
        if value not in REQUEST_PURPOSES:
            _refuse("request purpose must be general, review or question",
                    code="invalid_value")
        result["purpose"] = value
    if "accepted" in body:
        result["accepted"] = _flag(body["accepted"], "accepted")
    if ozma_type == "ack" and "accepted" not in result:
        _refuse("ack needs accepted", code="invalid_value")
    for name, limit, one_line in (
            ("subject", MAX_SUBJECT, True), ("text", MAX_BODY, False),
            ("reason", MAX_REASON, True), ("resource", MAX_RESOURCE, True),
            ("lease_id", 64, True), ("claim_id", 64, True),
            ("expires_at", 64, True), ("blocked_id", MAX_ID, True),
            ("blocker_id", MAX_ID, True)):
        if name in body:
            result[name] = _text(body[name], name, limit,
                                 one_line=one_line)
    for name in ("blocked_id", "blocker_id"):
        if name in result and not ID_RE.fullmatch(result[name]):
            _refuse(f"{name} is not a safe id", code="invalid_value")
    for name, limit in (("criteria", MAX_CRITERIA), ("refs", MAX_REFS)):
        if name in body:
            result[name] = _string_list(body[name], name, limit=limit,
                                        item_limit=MAX_CRITERION)
    if "workunit" in body:
        value = _text(body["workunit"], "workunit", MAX_WORKUNIT,
                      one_line=True)
        if not WORKUNIT_RE.fullmatch(value):
            _refuse("workunit is not a safe id", code="invalid_value")
        result["workunit"] = value
    if "completion" in body:
        if body["completion"] not in COMPLETIONS:
            _refuse("completion is not recognised", code="invalid_value")
        result["completion"] = body["completion"]
    if ozma_type == "claim":
        if "scope" not in body or body["scope"] not in CLAIM_SCOPES:
            _refuse("claim scope must be one of " + ", ".join(CLAIM_SCOPES),
                    code="invalid_value")
        result["scope"] = body["scope"]
        if "resource" not in result:
            _refuse("claim needs a resource", code="invalid_value")
        if "mode" in body:
            if body["mode"] not in CLAIM_MODES:
                _refuse("claim mode must be one of " + ", ".join(CLAIM_MODES),
                        code="invalid_value")
            result["mode"] = body["mode"]
        if "expires_at" in result:
            if result["expires_at"] != "now" and _parse_iso(
                    result["expires_at"]) is None:
                _refuse("claim expires_at is not a timestamp",
                        code="invalid_value")
    if "claim_id" in result and not ID_RE.fullmatch(result["claim_id"]):
        _refuse("claim_id is not a safe id", code="invalid_value")
    if "lease_id" in result and result["lease_id"]:
        if not M.LEASE_RE.fullmatch(result["lease_id"]):
            _refuse("lease_id is not a lease id", code="invalid_value")
    return result


def validate_envelope(envelope: Mapping[str, Any], *, direction: str | None = None,
                      flight_id: str | None = None) -> dict[str, Any]:
    """Validate and normalise one closed bridge envelope.

    Unknown envelope fields and unknown typed body fields are rejected; no
    field is silently dropped.
    """
    if not isinstance(envelope, Mapping):
        _refuse("envelope must be an object", code="invalid_envelope")
    unknown = sorted(set(envelope) - ENVELOPE_FIELDS)
    if unknown:
        _refuse("unknown envelope field(s): " + ", ".join(unknown),
                code="unsupported_field")
    if envelope.get("schema") != SCHEMA:
        _refuse("unsupported bridge envelope schema", code="unsupported_schema")
    if envelope.get("bridge") != BRIDGE_NAME:
        _refuse("not an Ozagia bridge envelope", code="invalid_envelope")
    resolved_direction = envelope.get("direction", direction)
    if resolved_direction is None:
        resolved_direction = "inbound" if direction is None else direction
    if resolved_direction not in DIRECTIONS:
        _refuse("direction must be outbound or inbound", code="invalid_value")
    if direction is not None and resolved_direction != direction:
        _refuse("envelope direction does not match the store direction",
                code="invalid_envelope")
    ozma_type = envelope.get("type")
    if ozma_type in IDENTITY_ONLY_KINDS:
        _refuse("hello is identity metadata, not a peer message",
                code="identity_metadata")
    if ozma_type not in OZMA_TYPES:
        _refuse(f"unsupported Ozma type: {ozma_type!r}", code="unsupported_kind")
    message_id = _text(envelope.get("id"), "id", MAX_ID, one_line=True)
    if not ID_RE.fullmatch(message_id):
        _refuse("id is not a safe message id", code="invalid_value")
    flight = envelope.get("flight_id", flight_id)
    if flight is None:
        _refuse("envelope needs a flight_id", code="invalid_value")
    flight = _text(flight, "flight_id", 64, one_line=True)
    if not O.FLIGHT_RE.fullmatch(flight):
        _refuse("flight_id is not a safe mesh id", code="invalid_value")
    if flight_id is not None and flight != flight_id:
        _refuse("envelope belongs to another flight", code="cross_flight")
    body = _validate_body(ozma_type, envelope.get("body", {}))
    result: dict[str, Any] = {
        "schema": SCHEMA,
        "bridge": BRIDGE_NAME,
        "flight_id": flight,
        "id": message_id,
        "type": ozma_type,
        "direction": resolved_direction,
        "body": body,
    }
    for name, limit, one_line in (("from", MAX_ID, True),
                                  ("to", MAX_ID, True),
                                  ("thread", MAX_THREAD, True)):
        if envelope.get(name) is not None:
            value = _text(envelope[name], name, limit, one_line=one_line)
            if name == "thread":
                if not THREAD_RE.fullmatch(value):
                    _refuse("thread is not a safe opaque id",
                            code="invalid_value")
            elif not O.AGENT_RE.fullmatch(value):
                _refuse(f"{name} must be a host-provided agent:<role> id",
                        code="invalid_value")
            result[name] = value
    if envelope.get("from_card") is not None:
        value = str(envelope["from_card"]).lower()
        if not O.FINGERPRINT_RE.fullmatch(value):
            _refuse("from_card must be sha256:<64 hex characters>",
                    code="invalid_value")
        result["from_card"] = value
    if envelope.get("in_reply_to") is not None:
        value = _text(envelope["in_reply_to"], "in_reply_to", MAX_ID,
                      one_line=True)
        if not ID_RE.fullmatch(value):
            _refuse("in_reply_to is not a safe id", code="invalid_value")
        result["in_reply_to"] = value
    if envelope.get("refs") is not None:
        result["refs"] = _string_list(envelope["refs"], "refs",
                                      limit=MAX_REFS, item_limit=MAX_REF)
    if envelope.get("priority") is not None:
        result["priority"] = _number(envelope["priority"], "priority")
    if envelope.get("deliver_at") is not None:
        value = _text(envelope["deliver_at"], "deliver_at", 64, one_line=True)
        if value != "now" and _parse_iso(value) is None:
            _refuse("deliver_at is not 'now' or a timestamp",
                    code="invalid_value")
        result["deliver_at"] = value
    if envelope.get("requires_ack") is not None:
        result["requires_ack"] = _flag(envelope["requires_ack"], "requires_ack")
    if envelope.get("compliance") is not None:
        result["compliance"] = _string_list(
            envelope["compliance"], "compliance", limit=MAX_COMPLIANCE,
            item_limit=MAX_COMPLIANCE_ITEM)
    if envelope.get("channel") is not None:
        value = _text(envelope["channel"], "channel", MAX_CHANNEL,
                      one_line=True)
        if not CHANNEL_RE.fullmatch(value):
            _refuse("channel must look like #name", code="invalid_value")
        result["channel"] = value
    if envelope.get("workunit") is not None:
        value = _text(envelope["workunit"], "workunit", MAX_WORKUNIT,
                      one_line=True)
        if not WORKUNIT_RE.fullmatch(value):
            _refuse("workunit is not a safe id", code="invalid_value")
        result["workunit"] = value
    if envelope.get("wake") is not None:
        value = _text(envelope["wake"], "wake", 32, one_line=True)
        if value not in WAKE_REQUEST_MODES:
            _refuse("wake must be message or scheduled", code="invalid_value")
        result["wake"] = value
    if envelope.get("wake_at") is not None:
        value = _text(envelope["wake_at"], "wake_at", 64, one_line=True)
        if _parse_iso(value) is None:
            _refuse("wake_at is not a timestamp", code="invalid_value")
        result["wake_at"] = value
    for name in ("created_at", "expires_at"):
        if envelope.get(name) is not None:
            value = _text(envelope[name], name, 64, one_line=True)
            if _parse_iso(value) is None:
                _refuse(f"{name} is not a timestamp", code="invalid_value")
            result[name] = value
    trust = envelope.get("body_trust", "typed")
    if trust not in ("typed", "untrusted"):
        _refuse("body_trust must be typed or untrusted", code="invalid_value")
    if any(field in body for field in TEXT_BODY_FIELDS):
        trust = "untrusted"
    result["body_trust"] = trust
    if envelope.get("redactions") is not None:
        result["redactions"] = _string_list(
            envelope["redactions"], "redactions", limit=32, item_limit=64)
    result["digest"] = envelope_digest(result)
    return result


def _redact_body(body: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]:
    redacted: dict[str, Any] = {}
    labels: set[str] = set()
    for key, value in body.items():
        if isinstance(value, str):
            text, found = M._redact(value)
            labels.update(found)
            if len(text) > MAX_BODY and key == "text":
                _refuse("redacted body is too large", code="too_large")
            redacted[key] = text
        elif isinstance(value, list):
            items: list[str] = []
            for item in value:
                if isinstance(item, str):
                    text, found = M._redact(item)
                    labels.update(found)
                    items.append(text)
                else:
                    items.append(item)
            redacted[key] = items
        else:
            redacted[key] = value
    return redacted, sorted(labels)


def build_envelope(*, flight_id: str, message_id: str, ozma_type: str,
                   body: Mapping[str, Any], direction: str = "outbound",
                   from_agent: str | None = None,
                   from_card: str | None = None,
                   to_agent: str | None = None,
                   in_reply_to: str | None = None, thread: str | None = None,
                   refs: Sequence[str] | None = None,
                   priority: float | None = None,
                   deliver_at: str | None = None,
                   requires_ack: bool | None = None,
                   compliance: Sequence[str] | None = None,
                   channel: str | None = None,
                   workunit: str | None = None,
                   wake: str | None = None,
                   wake_at: str | None = None,
                   body_trust: str = "typed",
                   now: float | None = None,
                   ttl_s: int | None = None) -> dict[str, Any]:
    """Build and validate one envelope with a canonical digest."""
    created = time.time() if now is None else float(now)
    envelope: dict[str, Any] = {
        "schema": SCHEMA,
        "bridge": BRIDGE_NAME,
        "flight_id": flight_id,
        "id": message_id,
        "type": ozma_type,
        "direction": direction,
        "body": dict(body),
        "body_trust": body_trust,
        "created_at": _iso(created),
    }
    if ttl_s is not None:
        envelope["expires_at"] = _iso(created + ttl_s)
    if from_agent:
        envelope["from"] = from_agent
    if from_card:
        envelope["from_card"] = from_card
    if to_agent:
        envelope["to"] = to_agent
    if in_reply_to:
        envelope["in_reply_to"] = in_reply_to
    if thread:
        envelope["thread"] = thread
    if refs:
        envelope["refs"] = list(refs)
    if priority is not None:
        envelope["priority"] = priority
    if deliver_at:
        envelope["deliver_at"] = deliver_at
    if requires_ack is not None:
        envelope["requires_ack"] = requires_ack
    if compliance:
        envelope["compliance"] = list(compliance)
    if channel:
        envelope["channel"] = channel
    if workunit:
        envelope["workunit"] = workunit
    if wake:
        envelope["wake"] = wake
    if wake_at:
        envelope["wake_at"] = wake_at
    return validate_envelope(envelope, direction=direction,
                             flight_id=flight_id)


# --------------------------------------------------------------------------- #
# paths, storage and locks
# --------------------------------------------------------------------------- #
def bridge_root(root: str | Path) -> Path:
    """``<state>/mesh/ozma`` (no directory is created here)."""
    return state_dir(root) / "mesh" / OZMA_DIRNAME


def store_dir(root: str | Path, kind: str, flight_id: str) -> Path:
    if kind not in {OUTBOX_DIR, INBOX_DIR, CLAIMS_DIR, DEDUPE_DIR,
                    HANDOFFS_DIR, WAKE_DIR}:
        raise OzmaBridgeSchemaError(f"unknown bridge store: {kind}")
    if not O.FLIGHT_RE.fullmatch(str(flight_id)):
        raise OzmaBridgeError("flight_id is not a safe mesh id")
    return bridge_root(root) / kind / flight_id


def record_path(root: str | Path, kind: str, flight_id: str,
                ozma_message_id: str, direction: str) -> Path:
    """A hashed store path: no raw message ids or paths in the filename."""
    key = record_key(flight_id, ozma_message_id, direction)
    return store_dir(root, kind, flight_id) / f"{key}.json"


def _is_dir(path: Path) -> bool:
    try:
        return stat.S_ISDIR(os.lstat(path).st_mode)
    except OSError:
        return False


def _is_file(path: Path) -> bool:
    try:
        return stat.S_ISREG(os.lstat(path).st_mode)
    except OSError:
        return False


def _read_record(path: Path) -> dict[str, Any] | None:
    if not _is_file(path):
        return None
    data = O._read_json(path)
    if data is None:
        return None
    if data.get("schema") != SCHEMA:
        raise OzmaBridgeSchemaError(f"{path}: unsupported bridge schema")
    return data


def _write_record(path: Path, record: Mapping[str, Any]) -> None:
    O._write_json(path, record)


def _flight_lock(root: str | Path, flight_id: str) -> "_Lock":
    """One lock per flight, so enqueue and ingest share the dedupe index."""
    digest = hashlib.sha256(flight_id.encode("utf-8")).hexdigest()[:32]
    return _Lock(bridge_root(root) / "locks" / f"{digest}.lock")


class _Lock:
    """A small advisory lock for bridge writes; readers never take it."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle: Any = None

    def __enter__(self) -> "_Lock":
        try:
            import fcntl
        except ImportError:  # pragma: no cover - non-POSIX
            return self
        O._ensure_private_dir(self.path.parent)
        descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        self.handle = os.fdopen(descriptor, "r+")
        try:
            os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
        except OSError as exc:  # pragma: no cover - unusual host
            raise OzmaBridgeIOError(f"cannot lock bridge store: {exc}") from exc
        return self

    def __exit__(self, *exc_info: Any) -> None:
        if self.handle is None:
            return
        try:
            import fcntl
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        except (ImportError, OSError):  # pragma: no cover
            pass
        finally:
            self.handle.close()


def _records(root: str | Path, kind: str, flight_id: str
             ) -> tuple[list[dict[str, Any]], list[str]]:
    """Read every record in one store; absent stores read as empty."""
    directory = store_dir(root, kind, flight_id)
    if not _is_dir(directory):
        return [], []
    records: list[dict[str, Any]] = []
    errors: list[str] = []
    for path in sorted(directory.iterdir()):
        if not path.name.endswith(".json") or not _is_file(path):
            continue
        try:
            record = _read_record(path)
        except (M.MeshError, O.OzmaMeshError, OSError) as exc:
            errors.append(str(exc))
            continue
        if record is not None:
            records.append(record)
    return records, errors


def _store_bytes(root: str | Path, kind: str, flight_id: str) -> int:
    directory = store_dir(root, kind, flight_id)
    if not _is_dir(directory):
        return 0
    total = 0
    for path in directory.iterdir():
        if path.name.endswith(".json") and _is_file(path):
            try:
                total += os.lstat(path).st_size
            except OSError:
                pass
    return total


def _record_age(record: Mapping[str, Any], kind: str) -> float:
    """When a terminal record became evictable; oldest sorts first."""
    names = (("updated_at", "acked_at", "read_at", "received_at", "created_at")
             if kind == INBOX_DIR else
             ("updated_at", "acked_at", "sent_at", "created_at"))
    for name in names:
        at = _parse_iso(record.get(name))
        if at is not None:
            return at
    return 0.0


def _terminal_record(kind: str, record: Mapping[str, Any],
                     now: float) -> bool:
    """Whether a record can be evicted without losing live work.

    Unsent and unread records are never terminal.  Read inbound messages that
    still require an ack stay until acked.  Expiry is reported, not treated as
    deletion, except that an already-expired pending outbox row is no longer
    deliverable.
    """
    state = str(record.get("state") or "")
    if kind == OUTBOX_DIR:
        if state in OUTBOX_TERMINAL_STATES:
            return True
        if state in {"pending", "failed"}:
            due = _parse_iso(record.get("expires_at"))
            return due is not None and due <= now
        return False
    if state == "acked":
        return True
    if state == "read":
        envelope = record.get("envelope") or {}
        return not bool(envelope.get("requires_ack"))
    return False


def _claims_full(root: str | Path, flight_id: str) -> bool:
    """Whether the advisory claim store is at its metadata cap."""
    records, _ = _records(root, CLAIMS_DIR, flight_id)
    return len(records) >= MAX_CLAIMS


def _evict_terminal(root: str | Path, kind: str, flight_id: str, *,
                    now: float, extra_count: int, extra_bytes: int,
                    max_count: int, max_bytes: int) -> int:
    """Evict oldest terminal rows until a new row would fit; report the count.

    This keeps the queue bounded without dropping pending outbound work or
    unread inbound messages.  The caller still re-checks the caps afterwards.
    """
    directory = store_dir(root, kind, flight_id)
    if not _is_dir(directory):
        return 0
    rows: list[tuple[float, int, Path]] = []
    count = 0
    total = 0
    for path in directory.iterdir():
        if not path.name.endswith(".json") or not _is_file(path):
            continue
        try:
            size = os.lstat(path).st_size
        except OSError:
            continue
        count += 1
        total += size
        try:
            record = _read_record(path)
        except (M.MeshError, O.OzmaMeshError, OSError):
            continue
        if record is not None and _terminal_record(kind, record, now):
            rows.append((_record_age(record, kind), size, path))
    rows.sort(key=lambda item: item[0])
    evicted = 0
    for _, size, path in rows:
        if count + extra_count <= max_count and total + extra_bytes <= max_bytes:
            break
        try:
            os.unlink(path)
        except OSError:
            continue
        count -= 1
        total -= size
        evicted += 1
    return evicted


# --------------------------------------------------------------------------- #
# dedupe index
# --------------------------------------------------------------------------- #
def _dedupe_path(root: str | Path, flight_id: str) -> Path:
    return store_dir(root, DEDUPE_DIR, flight_id) / "index.json"


def _load_dedupe(root: str | Path, flight_id: str) -> dict[str, Any]:
    path = _dedupe_path(root, flight_id)
    record = _read_record(path) if _is_file(path) else None
    if record is None:
        return {"schema": SCHEMA, "flight_id": flight_id, "entries": {},
                "conflicts": 0, "cursor": None}
    record.setdefault("entries", {})
    record.setdefault("conflicts", 0)
    record.setdefault("cursor", None)
    return record


def _prune_dedupe(index: dict[str, Any], *, now: float,
                  window_s: int) -> None:
    entries = index.get("entries") or {}
    if window_s <= 0:
        index["entries"] = {}
        index["updated_at"] = _iso(now)
        return
    kept: dict[str, Any] = {}
    for key, entry in entries.items():
        at = _parse_iso(entry.get("at")) if isinstance(entry, Mapping) else None
        if at is not None and now - at > window_s:
            continue
        kept[key] = entry
    if len(kept) > DEDUPE_MAX_ENTRIES:
        ordered = sorted(kept.items(),
                         key=lambda item: str((item[1] or {}).get("at") or ""),
                         reverse=True)
        kept = dict(ordered[:DEDUPE_MAX_ENTRIES])
    index["entries"] = kept
    index["updated_at"] = _iso(now)


def _dedupe_check(index: Mapping[str, Any], key: str, digest: str, *,
                  now: float, window_s: int) -> tuple[str, Mapping[str, Any] | None]:
    entry = (index.get("entries") or {}).get(key)
    if not isinstance(entry, Mapping):
        return "new", None
    if window_s <= 0:
        return "new", None
    at = _parse_iso(entry.get("at"))
    if at is not None and now - at > window_s:
        return "new", None
    if entry.get("digest") != digest:
        return "conflict", entry
    return "replay", entry


def _dedupe_remember(index: dict[str, Any], key: str, digest: str, *,
                     direction: str, now: float) -> None:
    index.setdefault("entries", {})[key] = {
        "digest": digest, "direction": direction, "at": _iso(now)}
    index["updated_at"] = _iso(now)


def _note_conflict(index: dict[str, Any], *, now: float) -> None:
    index["conflicts"] = int(index.get("conflicts") or 0) + 1
    index["updated_at"] = _iso(now)


# --------------------------------------------------------------------------- #
# configuration gates and channels
# --------------------------------------------------------------------------- #
def flight_id(root: str | Path, config: O.OzmaMeshConfig | None = None) -> str:
    cfg = config if config is not None else O.load_config(root)
    return cfg.flight_id or M.mesh_id(root)


def message_gate(root: str | Path,
                 config: O.OzmaMeshConfig | None = None) -> dict[str, Any]:
    """Whether the declared bridge may queue a message.

    ``host_declared`` plus a matching card fingerprint is enough for this
    stage; the verification is still declared, not cryptographic, and never
    reported as tested upstream conformance.
    """
    cfg = config if config is not None else O.load_config(root)
    reasons: list[str] = []
    if not cfg.enabled or cfg.mode == "off":
        reasons.append("the bridge is off")
    elif cfg.mode not in {"messages", "full"}:
        reasons.append("mode must be messages or full")
    if not cfg.agent_id:
        reasons.append("a host-provided agent_id is missing")
    card = O.card_state(root, cfg)
    if card.get("fingerprint") != "match":
        reasons.append("the card fingerprint does not match")
    if cfg.verified_by != "host":
        reasons.append("the host did not declare the identity")
    if cfg.transport == "a2a":
        reasons.append("A2A transport is not implemented in this stage")
    return {
        "ready": not reasons,
        "reasons": reasons,
        "conformance": CONFORMANCE,
        "verified": False,
        "lossy": cfg.transport == "a2a",
    }


def _require_message_gate(root: str | Path, config: O.OzmaMeshConfig) -> None:
    gate = message_gate(root, config)
    if not gate["ready"]:
        _refuse("the bridge is not ready to queue messages: "
                + "; ".join(gate["reasons"]), code="bridge_not_ready")


def channel_allowed(config: O.OzmaMeshConfig, channel: str | None) -> bool:
    if not channel:
        return True
    return channel in config.channels


def _parse_subscription(value: str) -> tuple[str, str]:
    if value.startswith("#"):
        return "channel", value
    if ":" in value:
        kind, item = value.split(":", 1)
        if kind in {"channel", "workunit", "event", "type"}:
            return kind, item
    return "unknown", value


def subscription_accepts(config: O.OzmaMeshConfig,
                         envelope: Mapping[str, Any]) -> tuple[bool, str]:
    """Apply configured subscriptions, channel scope and priority floor."""
    priority = envelope.get("priority")
    if priority is not None and float(priority) < config.priority_floor:
        return False, "below_priority_floor"
    channel = envelope.get("channel")
    if channel and not channel_allowed(config, str(channel)):
        return False, "channel_not_allowed"
    subscriptions = config.subscriptions
    if not subscriptions:
        return True, "channel_scope"
    local = translate_ozma(str(envelope.get("type")), envelope.get("body") or {})
    for subscription in subscriptions:
        kind, item = _parse_subscription(subscription)
        if kind == "channel" and channel == item:
            return True, "channel"
        if kind == "type" and envelope.get("type") == item:
            return True, "type"
        if kind == "event" and local.get("kind") == item:
            return True, "event"
        if kind == "workunit":
            candidate = envelope.get("workunit") or (
                envelope.get("body") or {}).get("workunit")
            if candidate == item:
                return True, "workunit"
    return False, "not_subscribed"


# --------------------------------------------------------------------------- #
# outbox
# --------------------------------------------------------------------------- #
def _rate_window(config: O.OzmaMeshConfig) -> tuple[int, int]:
    match = O.RATE_RE.fullmatch(config.message_rate)
    if not match:  # validated by load_config; be defensive
        return 60, 300
    count = int(match.group(1))
    unit = match.group(3)
    window = int(match.group(2))
    seconds = {"s": 1, "min": 60, "h": 3600, "d": 86400}[unit]
    return count, window * seconds


def _record_bytes(record: Mapping[str, Any]) -> int:
    return len((json.dumps(record, indent=2, sort_keys=True,
                           allow_nan=False) + "\n").encode("utf-8"))


def _outbox_view(record: Mapping[str, Any]) -> dict[str, Any]:
    envelope = record.get("envelope") or {}
    return {
        "id": envelope.get("id"),
        "type": envelope.get("type"),
        "from": envelope.get("from"),
        "to": envelope.get("to"),
        "channel": envelope.get("channel"),
        "state": record.get("state"),
        "attempts": record.get("attempts", 0),
        "created_at": record.get("created_at"),
        "expires_at": record.get("expires_at"),
        "sent_at": record.get("sent_at"),
        "acked_at": record.get("acked_at"),
        "priority": envelope.get("priority"),
        "requires_ack": envelope.get("requires_ack"),
        "digest": envelope.get("digest"),
        "error": record.get("error"),
        "offer_as": record.get("offer_as"),
        "advisory": record.get("advisory", False),
    }


def enqueue(root: str | Path, envelope: Mapping[str, Any], *,
            config: O.OzmaMeshConfig | None = None,
            offer_as: str | None = None,
            advisory: bool = False,
            track_handoff: bool = True,
            now: float | None = None) -> dict[str, Any]:
    """Queue one validated outbound envelope.  Refuses, never drops."""
    cfg = config if config is not None else O.load_config(root)
    _require_message_gate(root, cfg)
    moment = time.time() if now is None else float(now)
    flight = flight_id(root, cfg)
    normalized = validate_envelope(envelope, direction="outbound",
                                   flight_id=flight)
    channel = normalized.get("channel")
    if channel and not channel_allowed(cfg, str(channel)):
        _refuse("channel is not in the configured channels",
                code="channel_not_allowed")
    redacted_body, labels = _redact_body(normalized["body"])
    if labels:
        normalized = dict(normalized)
        normalized["body"] = redacted_body
        normalized["redactions"] = sorted(
            set(normalized.get("redactions") or ()) | set(labels))
        normalized["digest"] = envelope_digest(normalized)
    digest = normalized["digest"]
    key = dedupe_key(flight, normalized["id"], "outbound")
    path = record_path(root, OUTBOX_DIR, flight, normalized["id"], "outbound")
    with _flight_lock(root, flight):
        existing_record = _read_record(path)
        index = _load_dedupe(root, flight)
        state, entry = _dedupe_check(index, key, digest, now=moment,
                                     window_s=cfg.dedupe_window_s)
        if state == "conflict":
            _note_conflict(index, now=moment)
            _write_record(_dedupe_path(root, flight), index)
            _refuse("message id was reused with different content",
                    code="id_conflict")
        if state == "replay":
            if existing_record is not None:
                view = _outbox_view(existing_record)
                view.update({"duplicate": True, "queued": False})
                return view
            # An index entry without a record (a pruned store) is re-created.
        if existing_record is not None:
            _refuse("message id already exists in the outbox",
                    code="id_conflict")
        records, _ = _records(root, OUTBOX_DIR, flight)
        ttl = max(1, int(cfg.outbox_ttl_s))
        record = {
            "schema": SCHEMA,
            "kind": "outbox",
            "flight_id": flight,
            "direction": "outbound",
            "outbox_id": key,
            "envelope": normalized,
            "state": "pending",
            "attempts": 0,
            "created_at": _iso(moment),
            "updated_at": _iso(moment),
            "expires_at": _iso(moment + ttl),
            "sent_at": None,
            "acked_at": None,
            "error": None,
            "offer_as": offer_as,
            "advisory": bool(advisory),
            "remote_message_id": None,
        }
        encoded = _record_bytes(record)
        if encoded > cfg.queue_max_bytes:
            _refuse("the message is larger than the outbox byte cap",
                    code="too_large")
        limit, window = _rate_window(cfg)
        recent = 0
        for item in records:
            created = _parse_iso(item.get("created_at"))
            if created is not None and moment - created < window:
                recent += 1
        if recent >= limit:
            _refuse("the configured message_rate is exceeded",
                    code="rate_limited")
        # Terminal history yields to a new row; pending/failed work never does.
        evicted = _evict_terminal(
            root, OUTBOX_DIR, flight, now=moment, extra_count=1,
            extra_bytes=encoded, max_count=cfg.queue_max_messages,
            max_bytes=cfg.queue_max_bytes)
        remaining, _ = _records(root, OUTBOX_DIR, flight)
        remaining_bytes = _store_bytes(root, OUTBOX_DIR, flight)
        if (len(remaining) + 1 > cfg.queue_max_messages
                or remaining_bytes + encoded > cfg.queue_max_bytes):
            _refuse("the outbox is full", code="queue_full")
        _write_record(path, record)
        _dedupe_remember(index, key, digest, direction="outbound", now=moment)
        _prune_dedupe(index, now=moment, window_s=cfg.dedupe_window_s)
        _write_record(_dedupe_path(root, flight), index)
    if track_handoff and normalized["type"] == "handoff":
        _track_outbound_handoff(root, cfg, normalized, now=moment)
    if normalized["type"] == "block":
        _apply_blocked_envelope(root, cfg, normalized, now=moment)
    elif normalized["type"] == "unblock":
        _apply_unblocked_envelope(root, cfg, normalized, now=moment)
    view = _outbox_view(record)
    view.update({"duplicate": False, "queued": True, "evicted": evicted})
    return view


def _message_id(message: Mapping[str, Any]) -> str:
    value = str(message.get("msg_id") or "")
    if not ID_RE.fullmatch(value):
        _refuse("local message id is not a bridge-safe id",
                code="invalid_message_id")
    return value


def peer_agent_id(root: str | Path, flight: str, ozagia_key: str
                  ) -> str | None:
    """The saved host-provided Ozma id for a local peer, if any.

    A local session key is never used as an Ozma id.  Only a flight-scoped
    mapping record written by ``map --save`` can supply the recipient id, and
    its verification must still be a host declaration or fingerprint match.
    """
    if not isinstance(ozagia_key, str) or not ozagia_key:
        return None
    path = O.mapping_record_path(root, flight, ozagia_key)
    if not _is_file(path):
        return None
    try:
        record = O._read_json(path)
    except (OSError, O.OzmaMeshError):
        return None
    if not isinstance(record, Mapping) or record.get("flight_id") != flight:
        return None
    verification = record.get("verification") or {}
    if verification.get("status") not in {"host_declared",
                                          "fingerprint_matched"}:
        return None
    ozma = record.get("ozma") or {}
    agent = ozma.get("agent_id")
    if isinstance(agent, str) and O.AGENT_RE.fullmatch(agent):
        return agent
    return None


def enqueue_local_message(root: str | Path, message: Mapping[str, Any], *,
                          config: O.OzmaMeshConfig | None = None,
                          offer_as: str | None = None,
                          channel: str | None = None,
                          priority: float | None = None,
                          thread: str | None = None,
                          deliver_at: str | None = None,
                          requires_ack: bool | None = None,
                          compliance: Sequence[str] | None = None,
                          now: float | None = None) -> dict[str, Any]:
    """Map and queue one existing local mailbox message."""
    cfg = config if config is not None else O.load_config(root)
    _require_message_gate(root, cfg)
    kind = str(message.get("kind") or "")
    translated = translate_local(
        kind, subject=str(message.get("subject") or ""),
        body=str(message.get("body") or ""), offer_as=offer_as)
    message_id = _message_id(message)
    flight = flight_id(root, cfg)
    target = message.get("to") or {}
    to_agent = None
    if isinstance(target, Mapping) and target:
        to_agent = peer_agent_id(root, flight, str(target.get("key") or ""))
        if to_agent is None:
            _refuse("the recipient has no saved Ozma identity mapping in this "
                    "flight; run `ozagia mesh ozma map --save` for that session",
                    code="unknown_peer_mapping")
    elif not message.get("broadcast"):
        _refuse("a direct bridge message needs a recipient",
                code="invalid_recipient")
    if message.get("broadcast") and not channel:
        if len(cfg.channels) == 1:
            channel = cfg.channels[0]
        else:
            _refuse("a broadcast needs an explicit configured channel",
                    code="channel_required")
    refs = list(message.get("evidence") or ()) or None
    envelope = build_envelope(
        flight_id=flight, message_id=message_id,
        ozma_type=translated["type"], body=translated["body"],
        from_agent=cfg.agent_id,
        from_card=cfg.card_fingerprint or None,
        to_agent=to_agent,
        in_reply_to=translated.get("in_reply_to") or message.get("in_reply_to"),
        thread=thread,
        refs=refs, priority=priority,
        deliver_at=deliver_at, requires_ack=requires_ack,
        compliance=compliance, channel=channel,
        body_trust=translated["body_trust"], now=now)
    return enqueue(root, envelope, config=cfg, offer_as=offer_as, now=now)


def outbox(root: str | Path, *, config: O.OzmaMeshConfig | None = None,
           include_all: bool = False, now: float | None = None
           ) -> dict[str, Any]:
    """List outbox metadata only; bodies are never included."""
    cfg = config if config is not None else O.load_config(root)
    flight = flight_id(root, cfg)
    records, errors = _records(root, OUTBOX_DIR, flight)
    moment = time.time() if now is None else float(now)
    rows = []
    counts = {state: 0 for state in OUTBOX_STATES}
    for record in records:
        state = str(record.get("state") or "pending")
        due = _parse_iso(record.get("expires_at"))
        expired = (state in {"pending", "failed"} and due is not None
                   and due <= moment)
        if expired:
            state = "expired"
        counts[state] = counts.get(state, 0) + 1
        if include_all or state in {"pending", "failed", "expired"}:
            view = _outbox_view({**record, "state": state})
            view["expired"] = expired
            rows.append(view)
    rows.sort(key=lambda item: (str(item.get("created_at")),
                                str(item.get("id"))), reverse=True)
    return {"schema": SCHEMA, "flight_id": flight, "messages": rows,
            "counts": counts, "count": len(rows),
            "queue": {"messages": len(records),
                      "bytes": _store_bytes(root, OUTBOX_DIR, flight),
                      "max_messages": cfg.queue_max_messages,
                      "max_bytes": cfg.queue_max_bytes},
            "errors": errors}


def _set_outbox_state(root: str | Path, flight: str, message_id: str,
                      *, state: str, now: float,
                      error: str | None = None,
                      remote_id: str | None = None,
                      increment: bool = False) -> None:
    path = record_path(root, OUTBOX_DIR, flight, message_id, "outbound")
    record = _read_record(path)
    if record is None:
        return
    if increment:
        record["attempts"] = int(record.get("attempts") or 0) + 1
    record["state"] = state
    record["updated_at"] = _iso(now)
    if state == "sent":
        record["sent_at"] = _iso(now)
    if state == "acked":
        record["acked_at"] = _iso(now)
    if error is not None:
        record["error"] = error
    if remote_id is not None:
        record["remote_message_id"] = remote_id
    _write_record(path, record)


def _set_claim_state(root: str | Path, flight: str, claim_id: str,
                     direction: str, *, state: str, now: float) -> None:
    path = _claim_path(root, flight, claim_id, direction)
    record = _read_record(path)
    if record is None:
        return
    record["state"] = state
    record["updated_at"] = _iso(now)
    _write_record(path, record)


def deliver_pending(root: str | Path, shim: "Shim", *,
                    config: O.OzmaMeshConfig | None = None,
                    now: float | None = None) -> dict[str, Any]:
    """Deliver pending/failed outbox rows with the same ids on retry."""
    cfg = config if config is not None else O.load_config(root)
    flight = flight_id(root, cfg)
    moment = time.time() if now is None else float(now)
    records, errors = _records(root, OUTBOX_DIR, flight)
    delivered = 0
    failed = 0
    expired = 0
    retried = 0
    for record in sorted(records, key=lambda item: str(item.get("created_at"))):
        state = str(record.get("state") or "pending")
        if state not in {"pending", "failed"}:
            continue
        due = _parse_iso(record.get("expires_at"))
        if due is not None and due <= moment:
            _set_outbox_state(root, flight, str(record["envelope"]["id"]),
                              state="expired", now=moment, increment=True)
            if record["envelope"].get("type") == "claim":
                _set_claim_state(root, flight, str(record["envelope"]["id"]),
                                 "outbound", state="expired", now=moment)
            expired += 1
            continue
        if state == "failed":
            retried += 1
        envelope = record.get("envelope") or {}
        try:
            result = shim.deliver(envelope)
        except OzmaBridgeRefusal:
            raise
        except Exception as exc:  # a shim failure must degrade, not pretend
            failed += 1
            _set_outbox_state(root, flight, str(envelope.get("id")),
                              state="failed", now=moment,
                              error=type(exc).__name__)
            continue
        accepted = bool(isinstance(result, Mapping) and result.get("accepted"))
        if accepted:
            delivered += 1
            remote_id = (result.get("message_id")
                         if isinstance(result, Mapping) else None)
            _set_outbox_state(
                root, flight, str(envelope.get("id")), state="sent",
                now=moment, error=None,
                remote_id=str(remote_id) if remote_id else None,
                increment=True)
            if envelope.get("type") == "claim":
                _set_claim_state(root, flight, str(envelope.get("id")),
                                 "outbound", state="sent", now=moment)
        else:
            failed += 1
            error = (result.get("code") if isinstance(result, Mapping)
                     else None) or "not_accepted"
            _set_outbox_state(root, flight, str(envelope.get("id")),
                              state="failed", now=moment, error=str(error),
                              increment=True)
            if envelope.get("type") == "claim":
                _set_claim_state(root, flight, str(envelope.get("id")),
                                 "outbound", state="queued", now=moment)
    return {"schema": SCHEMA, "flight_id": flight, "delivered": delivered,
            "failed": failed, "expired": expired, "retried": retried,
            "errors": errors, "network": False}


# --------------------------------------------------------------------------- #
# inbox
# --------------------------------------------------------------------------- #
def _inbox_view(record: Mapping[str, Any], *, now: float
                ) -> dict[str, Any]:
    envelope = record.get("envelope") or {}
    due = _parse_iso(record.get("expires_at"))
    return {
        "id": envelope.get("id"),
        "from": envelope.get("from"),
        "type": envelope.get("type"),
        "channel": envelope.get("channel"),
        "created_at": record.get("received_at") or envelope.get("created_at"),
        "expires_at": record.get("expires_at"),
        "priority": envelope.get("priority"),
        "requires_ack": envelope.get("requires_ack"),
        "read": record.get("state") in {"read", "acked"},
        "acked": record.get("state") == "acked",
        "expired": due is not None and due <= now,
        "state": record.get("state"),
        "advisory": bool(record.get("advisory")),
    }


def _inbox_ttl(envelope: Mapping[str, Any], now: float) -> float:
    due = _parse_iso(envelope.get("expires_at"))
    return due if due is not None else now + M.MESSAGE_TTL_S


def ingest(root: str | Path, envelope: Mapping[str, Any], *,
           config: O.OzmaMeshConfig | None = None,
           now: float | None = None) -> dict[str, Any]:
    """Validate, filter, redact and durably record one inbound envelope."""
    cfg = config if config is not None else O.load_config(root)
    moment = time.time() if now is None else float(now)
    flight = flight_id(root, cfg)
    try:
        normalized = validate_envelope(envelope, direction="inbound",
                                       flight_id=flight)
    except M.MeshRefusal as exc:
        if exc.code == "cross_flight":
            raise
        return {"accepted": False, "code": exc.code, "reason": str(exc),
                "id": None}
    accepts, reason = subscription_accepts(cfg, normalized)
    if not accepts:
        return {"accepted": False, "code": reason,
                "reason": "inbound message is outside the configured scope",
                "id": normalized["id"]}
    redacted_body, labels = _redact_body(normalized["body"])
    normalized = dict(normalized)
    normalized["body"] = redacted_body
    normalized["body_trust"] = "untrusted"
    if labels:
        normalized["redactions"] = sorted(
            set(normalized.get("redactions") or ()) | set(labels))
    normalized["digest"] = envelope_digest(normalized)
    digest = normalized["digest"]
    key = dedupe_key(flight, normalized["id"], "inbound")
    path = record_path(root, INBOX_DIR, flight, normalized["id"], "inbound")
    with _flight_lock(root, flight):
        existing = _read_record(path)
        index = _load_dedupe(root, flight)
        state, _ = _dedupe_check(index, key, digest, now=moment,
                                 window_s=cfg.dedupe_window_s)
        if state == "conflict":
            _note_conflict(index, now=moment)
            _write_record(_dedupe_path(root, flight), index)
            return {"accepted": False, "code": "id_conflict",
                    "reason": "message id was reused with different content",
                    "id": normalized["id"]}
        if state == "replay" and existing is not None:
            return {"accepted": True, "duplicate": True, "id": normalized["id"],
                    "state": existing.get("state")}
        if existing is not None:
            existing_digest = (existing.get("envelope") or {}).get("digest")
            if existing_digest and existing_digest != digest:
                _note_conflict(index, now=moment)
                _write_record(_dedupe_path(root, flight), index)
                return {"accepted": False, "code": "id_conflict",
                        "reason": "message id was reused with different content",
                        "id": normalized["id"]}
        if (normalized["type"] == "claim" and existing is None
                and not _is_file(_claim_path(
                    root, flight, normalized["id"], "inbound"))
                and _claims_full(root, flight)):
            return {"accepted": False, "code": "claim_store_full",
                    "reason": "the remote claim store is full",
                    "id": normalized["id"]}
        if existing is None:
            expires = _iso(_inbox_ttl(normalized, moment))
            record = {
                "schema": SCHEMA,
                "kind": "inbox",
                "flight_id": flight,
                "direction": "inbound",
                "inbox_id": key,
                "envelope": normalized,
                "state": "unread",
                "received_at": _iso(moment),
                "expires_at": expires,
                "read_at": None,
                "acked_at": None,
                "advisory": normalized["type"] == "claim",
            }
            encoded = _record_bytes(record)
            if encoded > cfg.queue_max_bytes:
                _refuse("the message is larger than the inbox byte cap",
                        code="too_large")
            # Acked history yields to new mail; unread mail never does.
            evicted = _evict_terminal(
                root, INBOX_DIR, flight, now=moment, extra_count=1,
                extra_bytes=encoded, max_count=cfg.queue_max_messages,
                max_bytes=cfg.queue_max_bytes)
            remaining, _ = _records(root, INBOX_DIR, flight)
            remaining_bytes = _store_bytes(root, INBOX_DIR, flight)
            if (len(remaining) + 1 > cfg.queue_max_messages
                    or remaining_bytes + encoded > cfg.queue_max_bytes):
                _refuse("the Ozma inbox is full", code="queue_full")
            _write_record(path, record)
        else:
            record = existing
            evicted = 0
        _dedupe_remember(index, key, digest, direction="inbound", now=moment)
        _prune_dedupe(index, now=moment, window_s=cfg.dedupe_window_s)
        _write_record(_dedupe_path(root, flight), index)
    wake_result = None
    if normalized["type"] == "claim":
        _record_inbound_claim(root, cfg, normalized, now=moment)
    elif normalized["type"] == "ack":
        reply = normalized.get("in_reply_to")
        if reply:
            _set_outbox_state(root, flight, str(reply), state="acked",
                              now=moment)
            _apply_handoff_ack(root, flight, normalized, now=moment)
    elif normalized["type"] == "handoff":
        _record_inbound_handoff(root, cfg, normalized, now=moment)
    elif normalized["type"] == "release":
        _release_claim(root, flight, normalized.get("in_reply_to"),
                       now=moment, from_agent=normalized.get("from"))
    elif normalized["type"] == "block":
        _apply_blocked_envelope(root, cfg, normalized, now=moment)
    elif normalized["type"] == "unblock":
        _apply_unblocked_envelope(root, cfg, normalized, now=moment)
    if normalized.get("wake"):
        wake_result = _record_wake_request(root, cfg, normalized, now=moment)
    outcome = {"accepted": True, "duplicate": False, "id": normalized["id"],
               "state": record.get("state"), "redactions": labels,
               "evicted": evicted}
    if wake_result is not None:
        outcome["wake"] = wake_result
    return outcome


def inbox(root: str | Path, *, config: O.OzmaMeshConfig | None = None,
          unread: bool = False, now: float | None = None) -> dict[str, Any]:
    """List inbound metadata only; bodies require an explicit read."""
    cfg = config if config is not None else O.load_config(root)
    flight = flight_id(root, cfg)
    records, errors = _records(root, INBOX_DIR, flight)
    moment = time.time() if now is None else float(now)
    counts = {state: 0 for state in INBOX_STATES}
    rows = []
    for record in records:
        view = _inbox_view(record, now=moment)
        counts[str(view["state"])] = counts.get(str(view["state"]), 0) + 1
        if unread and view["read"]:
            continue
        rows.append(view)
    rows.sort(key=lambda item: (str(item.get("created_at")),
                                str(item.get("id"))), reverse=True)
    return {"schema": SCHEMA, "conformance": CONFORMANCE,
            "flight_id": flight, "messages": rows, "counts": counts,
            "count": len(rows),
            "queue": {"messages": len(records),
                      "bytes": _store_bytes(root, INBOX_DIR, flight),
                      "max_messages": cfg.queue_max_messages,
                      "max_bytes": cfg.queue_max_bytes},
            "errors": errors}


def read_message(root: str | Path, message_id: str, *,
                 config: O.OzmaMeshConfig | None = None,
                 now: float | None = None) -> dict[str, Any]:
    """Explicit body fetch; marks the message read."""
    cfg = config if config is not None else O.load_config(root)
    flight = flight_id(root, cfg)
    path = record_path(root, INBOX_DIR, flight, message_id, "inbound")
    moment = time.time() if now is None else float(now)
    with _flight_lock(root, flight):
        record = _read_record(path)
        if record is None:
            _refuse("unknown Ozma inbox message", code="unknown_message")
        if record.get("state") == "unread":
            record["state"] = "read"
            record["read_at"] = _iso(moment)
            _write_record(path, record)
        envelope = dict(record.get("envelope") or {})
        envelope["read"] = record.get("state") in {"read", "acked"}
        envelope["acked"] = record.get("state") == "acked"
        envelope["received_at"] = record.get("received_at")
        envelope["expires_at"] = record.get("expires_at")
        envelope["untrusted"] = True
    return envelope


def ack_message(root: str | Path, message_id: str, *,
                config: O.OzmaMeshConfig | None = None,
                now: float | None = None,
                accepted: bool | None = None,
                reason: str = "",
                harness: str | None = None,
                session: str | None = None) -> dict[str, Any]:
    """Mark one inbound message read and acked.

    The default is local-only.  A typed reply is queued only when the caller
    explicitly asks with ``accepted`` and the message declared
    ``requires_ack``; it needs bridge mode ``messages``/``full``.
    """
    cfg = config if config is not None else O.load_config(root)
    flight = flight_id(root, cfg)
    path = record_path(root, INBOX_DIR, flight, message_id, "inbound")
    if not _is_file(path):
        _refuse("unknown Ozma inbox message", code="unknown_message")
    moment = time.time() if now is None else float(now)
    reply: dict[str, Any] | None = None
    if accepted is not None:
        _require_message_gate(root, cfg)
        record = _read_record(path)
        if record is None:
            _refuse("unknown Ozma inbox message", code="unknown_message")
        envelope = record.get("envelope") or {}
        if not envelope.get("requires_ack"):
            _refuse("the message did not request an ack", code="ack_not_required")
        why = _text(reason, "reason", MAX_REASON, allow_empty=True,
                    one_line=True)
        body: dict[str, Any] = {"accepted": bool(accepted)}
        if why:
            body["reason"] = why
        reply = build_envelope(
            flight_id=flight,
            message_id=_derived_id("ack", flight, message_id),
            ozma_type="ack", body=body,
            from_agent=cfg.agent_id,
            from_card=cfg.card_fingerprint or None,
            to_agent=envelope.get("from"),
            in_reply_to=message_id,
            thread=envelope.get("thread"),
            channel=envelope.get("channel"),
            requires_ack=False,
            body_trust="untrusted" if why else "typed",
            now=moment)
        reply = enqueue(root, reply, config=cfg, track_handoff=False,
                        now=moment)
    with _flight_lock(root, flight):
        record = _read_record(path)
        if record is None:
            _refuse("unknown Ozma inbox message", code="unknown_message")
        record["state"] = "acked"
        record["acked_at"] = _iso(moment)
        if not record.get("read_at"):
            record["read_at"] = _iso(moment)
        _write_record(path, record)
    result = {"schema": SCHEMA, "flight_id": flight, "id": message_id,
              "state": "acked", "acked_at": record["acked_at"],
              "read_at": record["read_at"]}
    if reply is not None:
        result["reply"] = {"id": reply.get("id"), "queued": reply.get("queued"),
                           "duplicate": reply.get("duplicate")}
    return result


# --------------------------------------------------------------------------- #
# claims
# --------------------------------------------------------------------------- #
def claim_scope_for(kind: str, resource: str) -> tuple[str, str | None]:
    """Map a local lease kind to a declared Ozma claim scope.

    ``grant`` is local-only and returns ``unsupported``; ``merge`` is visible
    but always advisory.
    """
    if kind == "worktree":
        return "worktree", resource
    if kind == "branch":
        return "branch", resource
    if kind == "merge":
        return "branch", resource
    if kind == "path":
        if resource:  # path resources are repository-relative
            return ("subtree" if resource.endswith("/") else "file"), resource
        return "file", resource
    return GRANT_MAPPING, None


def _strip_resource(kind: str, resource_id: str) -> str:
    prefix = f"{kind}:"
    if resource_id.startswith(prefix):
        return resource_id[len(prefix):]
    return resource_id


def _claim_message_id(flight: str, lease_id: str) -> str:
    digest = hashlib.sha256(
        f"{flight}\0{lease_id}\0claim".encode("utf-8")).hexdigest()[:24]
    return f"clm_{digest}"


def _claim_path(root: str | Path, flight: str, claim_id: str,
                direction: str) -> Path:
    return record_path(root, CLAIMS_DIR, flight, claim_id, direction)


def _claim_record(*, flight: str, claim_id: str, direction: str,
                  local_kind: str | None, scope: str, resource: str,
                  mode: str | None, holder: Mapping[str, Any],
                  digest: str, expiry: str | None, now: float,
                  lease_id: str | None, advisory: bool) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "kind": "claim",
        "flight_id": flight,
        "claim_id": claim_id,
        "direction": direction,
        "local_kind": local_kind,
        "scope": scope,
        "resource": resource,
        "mode": mode,
        "holder": {
            "agent_id": holder.get("agent_id"),
            "card_fingerprint": holder.get("card_fingerprint"),
            "key": holder.get("key"),
        },
        "lease_id": lease_id,
        "advisory": True,
        "digest": digest,
        "state": "active" if direction == "inbound" else "queued",
        "expiry": expiry,
        "created_at": _iso(now),
        "updated_at": _iso(now),
    }


def enqueue_claim(root: str | Path, lease: Mapping[str, Any], *,
                  config: O.OzmaMeshConfig | None = None,
                  merge: bool = False,
                  now: float | None = None) -> dict[str, Any]:
    """Queue a declared Ozma claim for one local advisory lease."""
    cfg = config if config is not None else O.load_config(root)
    _require_message_gate(root, cfg)
    moment = time.time() if now is None else float(now)
    flight = flight_id(root, cfg)
    lease_id = str(lease.get("lease_id") or "")
    if not M.LEASE_RE.fullmatch(lease_id):
        _refuse("invalid lease id", code="invalid_lease_id")
    effective = lease.get("effective_state")
    if effective is not None and effective not in {"active", "stale"}:
        _refuse(f"lease is {effective}; a claim needs an active lease",
                code="lease_closed")
    resource = lease.get("resource") or {}
    kind = str(resource.get("kind") or "")
    resource_id = str(resource.get("id") or "")
    if kind == "grant":
        _refuse("grant leases are local-only and have no claim mapping",
                code="unsupported_mapping")
    scope, claim_resource = claim_scope_for(kind, _strip_resource(
        kind, resource_id))
    if scope == GRANT_MAPPING or claim_resource is None:
        _refuse(f"lease kind {kind!r} has no claim mapping",
                code="unsupported_mapping")
    if kind == "merge" and not merge:
        merge = True
    mode = str(resource.get("mode") or "") or None
    claim_id = _claim_message_id(flight, lease_id)
    if (not _is_file(_claim_path(root, flight, claim_id, "outbound"))
            and _claims_full(root, flight)):
        _refuse("the claim store is full", code="claim_store_full")
    body: dict[str, Any] = {
        "scope": scope, "resource": claim_resource,
        "reason": "local lease " + lease_id,
        "lease_id": lease_id,
    }
    if mode in CLAIM_MODES:
        body["mode"] = mode
    expires = lease.get("expires_at")
    if isinstance(expires, str):
        body["expires_at"] = expires
    envelope = build_envelope(
        flight_id=flight, message_id=claim_id, ozma_type="claim",
        body=body, from_agent=cfg.agent_id,
        from_card=cfg.card_fingerprint or None,
        requires_ack=False, body_trust="untrusted", now=moment)
    queued = enqueue(root, envelope, config=cfg, advisory=True, now=moment)
    record = _claim_record(
        flight=flight, claim_id=claim_id, direction="outbound",
        local_kind=kind, scope=scope, resource=claim_resource, mode=mode,
        holder={"agent_id": cfg.agent_id,
                "card_fingerprint": cfg.card_fingerprint or None},
        digest=envelope["digest"], expiry=expires, now=moment,
        lease_id=lease_id, advisory=True)
    _write_record(_claim_path(root, flight, claim_id, "outbound"), record)
    queued.update({"claim_id": claim_id, "scope": scope,
                   "resource": claim_resource, "advisory": True,
                   "merge": kind == "merge"})
    return queued


def _record_inbound_claim(root: str | Path, config: O.OzmaMeshConfig,
                          envelope: Mapping[str, Any], *, now: float
                          ) -> dict[str, Any]:
    flight = flight_id(root, config)
    body = envelope.get("body") or {}
    claim_id = str(envelope["id"])
    record = _claim_record(
        flight=flight, claim_id=claim_id, direction="inbound",
        local_kind=None, scope=str(body.get("scope")),
        resource=str(body.get("resource") or ""),
        mode=body.get("mode"),
        holder={"agent_id": envelope.get("from"),
                "card_fingerprint": envelope.get("from_card")},
        digest=str(envelope.get("digest")),
        expiry=str(body.get("expires_at") or envelope.get("expires_at")
                   or envelope.get("deliver_at") or "") or None,
        now=now, lease_id=body.get("lease_id"), advisory=True)
    _write_record(_claim_path(root, flight, claim_id, "inbound"), record)
    return record


def _release_claim(root: str | Path, flight: str, claim_id: Any, *,
                   now: float, from_agent: str | None = None) -> None:
    """Release an inbound remote claim only when its holder sent the release."""
    if not isinstance(claim_id, str) or not ID_RE.fullmatch(claim_id):
        return
    if not from_agent:
        return
    path = _claim_path(root, flight, claim_id, "inbound")
    record = _read_record(path)
    if record is None:
        return
    holder = (record.get("holder") or {}).get("agent_id")
    if holder != from_agent:
        return
    record["state"] = "released"
    record["updated_at"] = _iso(now)
    _write_record(path, record)


def _claim_state(record: Mapping[str, Any], now: float) -> str:
    state = str(record.get("state") or "active")
    if state in {"released", "expired"}:
        return state
    expiry = _parse_iso(record.get("expiry"))
    if expiry is not None and expiry <= now:
        return "expired"
    return state


def _local_claim_targets(root: str | Path) -> dict[str, dict[str, Any]]:
    """Normalised local lease resources (read-only)."""
    leases = M.lease_list(root)
    targets: dict[str, dict[str, Any]] = {}
    for lease in leases.get("leases") or ():
        if lease.get("effective_state") not in {"active", "stale"}:
            continue
        resource = lease.get("resource") or {}
        kind = str(resource.get("kind") or "")
        resource_id = str(resource.get("id") or "")
        key = _claim_target(kind, _strip_resource(kind, resource_id))
        if key:
            targets[key] = lease
    return targets


def _claim_target(scope: str, resource: str) -> str:
    if scope in {"worktree", "branch", "file", "subtree"}:
        return f"{scope}:{resource}"
    if scope in {"path"}:
        return f"file:{resource}"
    if scope == "merge":
        return f"branch:{resource}"
    return f"area:{resource}"


def _normalize_remote_target(root: str | Path, scope: str,
                             resource: str) -> str:
    if scope == "worktree":
        try:
            return "worktree:" + M.normalize_resource(
                root, "worktree", resource).split(":", 1)[1]
        except M.MeshRefusal:
            return "worktree:" + resource
    if scope == "branch":
        try:
            return "branch:" + M.normalize_resource(
                root, "branch", resource).split(":", 1)[1]
        except M.MeshRefusal:
            return "branch:" + resource
    if scope in {"file", "subtree"}:
        try:
            normalized = M.normalize_resource(root, "path", resource)
        except M.MeshRefusal:
            return f"{scope}:{resource}"
        return ("subtree:" if scope == "subtree" else "file:") + normalized[
            len("path:"):]
    return f"area:{resource}"


def claims(root: str | Path, *, config: O.OzmaMeshConfig | None = None,
           now: float | None = None) -> dict[str, Any]:
    """Report outbound claim refs, remote claims and advisory conflicts."""
    cfg = config if config is not None else O.load_config(root)
    flight = flight_id(root, cfg)
    records, errors = _records(root, CLAIMS_DIR, flight)
    moment = time.time() if now is None else float(now)
    targets = _local_claim_targets(root)
    rows: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    counts = {"active": 0, "expired": 0, "released": 0, "queued": 0,
              "sent": 0, "remote": 0, "outbound": 0}
    for record in records:
        state = _claim_state(record, moment)
        direction = str(record.get("direction") or "inbound")
        counts[state] = counts.get(state, 0) + 1
        counts["remote" if direction == "inbound" else "outbound"] += 1
        view = {
            "claim_id": record.get("claim_id"),
            "direction": direction,
            "scope": record.get("scope"),
            "resource": record.get("resource"),
            "mode": record.get("mode"),
            "state": state,
            "advisory": True,
            "holder": record.get("holder"),
            "lease_id": record.get("lease_id"),
            "expiry": record.get("expiry"),
            "digest": record.get("digest"),
        }
        rows.append(view)
        if direction != "inbound" or state != "active":
            continue
        target = _normalize_remote_target(
            root, str(record.get("scope")), str(record.get("resource")))
        lease = targets.get(target)
        if lease is None and record.get("scope") == "subtree":
            # A subtree claim conflicts with a local file/path at or below it.
            for candidate, item in targets.items():
                if candidate.startswith("file:") and candidate[5:].startswith(
                        str(record.get("resource")).rstrip("/") + "/"):
                    lease = item
                    break
        if lease is not None:
            conflict = {
                "claim_id": record.get("claim_id"),
                "lease_id": lease.get("lease_id"),
                "scope": record.get("scope"),
                "resource": record.get("resource"),
                "advisory": True,
                "blocking": False,
                "warning": "a remote claim and a local lease name the same resource",
            }
            conflicts.append(conflict)
            view["conflict"] = True
    rows.sort(key=lambda item: (str(item.get("direction")),
                                str(item.get("claim_id"))))
    return {"schema": SCHEMA, "conformance": CONFORMANCE,
            "flight_id": flight, "claims": rows, "conflicts": conflicts,
            "counts": counts, "conflict_count": len(conflicts),
            "errors": errors}


# --------------------------------------------------------------------------- #
# Stage H: flights, handoffs, blockers, A2A and host-approved wake primitives
# --------------------------------------------------------------------------- #

A2A_VERSION = "1.0"
A2A_EXTRA_LOSS_FIELDS = ("refs", "workunit")
#: A field whose loss makes the message meaningless is refused, not sent.
A2A_REQUIRED_UNREPRESENTABLE: dict[str, str] = {
    "claim": "claim_scope",
    "block": "blockers",
    "unblock": "blockers",
}


def _derived_id(prefix: str, flight: str, value: str) -> str:
    """A stable, opaque id derived from flight-scoped content."""
    digest = hashlib.sha256(
        f"{flight}\0{value}\0{prefix}".encode("utf-8")).hexdigest()[:24]
    return f"{prefix}_{digest}"


def _hashed_path(root: str | Path, kind: str, flight: str,
                 prefix: str, value: str) -> Path:
    digest = hashlib.sha256(
        f"{flight}\0{value}".encode("utf-8")).hexdigest()[:32]
    return store_dir(root, kind, flight) / f"{prefix}-{digest}.json"


def agent_role_workunit(agent_id: Any) -> dict[str, str | None]:
    """Derive role/workunit from a host-provided agent id, and nothing else."""
    if not isinstance(agent_id, str) or not O.AGENT_RE.fullmatch(agent_id):
        return {"role": None, "workunit": None}
    parts = agent_id[len("agent:"):].split(":", 1)
    return {"role": parts[0] or None,
            "workunit": parts[1] if len(parts) > 1 and parts[1] else None}


def _identity_records(root: str | Path) -> tuple[list[dict[str, Any]],
                                                list[str]]:
    """Read every flight-scoped identity mapping record; never creates one."""
    directory = bridge_root(root) / "identities"
    if not _is_dir(directory):
        return [], []
    records: list[dict[str, Any]] = []
    errors: list[str] = []
    for path in sorted(directory.iterdir()):
        if not path.name.endswith(".json") or not _is_file(path):
            continue
        try:
            record = O._read_json(path)
        except (M.MeshError, O.OzmaMeshError, OSError) as exc:
            errors.append(str(exc))
            continue
        if isinstance(record, dict):
            records.append(record)
    return records, errors


def _require_coordinate_identity(root: str | Path, harness: str | None,
                                 session: str | None) -> dict[str, Any]:
    if not harness or not session:
        raise M.MeshConfigError(
            "an identity is required: --harness/--session or "
            "OZAGIA_MESH_HARNESS/OZAGIA_MESH_SESSION")
    state = M.status(root, harness=harness, session=session)
    me = state.get("self")
    if me is None:
        raise M.MeshRefusal("session is not registered", code="unknown_session")
    if me.get("effective_state") != "live":
        raise M.MeshRefusal(
            f"session is {me.get('effective_state')}, not live",
            code="session_not_live")
    if me.get("exposure") not in M.COORDINATE:
        raise M.MeshRefusal(
            "this command needs exposure=coordinate; re-register with "
            "--exposure coordinate", code="coordinate_required")
    return me


def _endpoint_key(value: Any) -> str | None:
    if isinstance(value, Mapping):
        key = value.get("key")
        return str(key) if key else None
    return None


def _mesh_record_in_flight(item: Mapping[str, Any], fields: tuple[str, ...],
                           keys: set[str]) -> bool:
    if not keys:
        return False
    endpoints = {_endpoint_key(item.get(field)) for field in fields}
    return bool(endpoints) and None not in endpoints and endpoints <= keys


def flight(root: str | Path, *, config: O.OzmaMeshConfig | None = None,
           harness: str | None = None, session: str | None = None,
           limit: int = MAX_FLIGHT_MEMBERS) -> dict[str, Any]:
    """A read-only flight view: members, labels, counts and readiness.

    Membership is the set of identity mapping records for this flight.  A
    local mesh registration is joined by its key to report live state, but the
    registration never declares a role or a workunit.  Role and workunit come
    only from the host-provided ``agent:<role>[:<workunit>]`` id.
    """
    cfg = config if config is not None else O.load_config(root)
    fid, source = O._flight(root, cfg)
    bound = max(0, min(int(limit), MAX_FLIGHT_MEMBERS))
    try:
        mesh = M.status(root, harness=harness, session=session)
    except (M.MeshError, O.OzmaMeshError) as exc:
        mesh = {"sessions": [], "link_counts": {}, "delegation_counts": {},
                "errors": [str(exc)]}
    sessions = {str(item.get("key")): item
                for item in (mesh.get("sessions") or ())}
    records, errors = _identity_records(root)
    members: list[dict[str, Any]] = []
    for record in records:
        if record.get("flight_id") != fid:
            continue
        ozagia = record.get("ozagia") or {}
        ozma = record.get("ozma") or {}
        verification = record.get("verification") or {}
        if not isinstance(ozagia, Mapping):
            ozagia = {}
        if not isinstance(ozma, Mapping):
            ozma = {}
        if not isinstance(verification, Mapping):
            verification = {}
        key = str(ozagia.get("key") or "")
        local = sessions.get(key) or {}
        agent_id = ozma.get("agent_id")
        labels = agent_role_workunit(agent_id)
        wake_modes = record.get("wake_modes")
        if not isinstance(wake_modes, (list, tuple)):
            wake_modes = ()
        members.append({
            "harness": ozagia.get("harness") or local.get("harness"),
            "session": ozagia.get("session") or local.get("session"),
            "key": key or None,
            "agent_id": agent_id if isinstance(agent_id, str) else None,
            "role": labels["role"],
            "workunit": labels["workunit"],
            "scope": ozma.get("agent_scope"),
            "verification": verification.get("status") or "unverified",
            "transport": record.get("transport") or ozma.get("transport"),
            "wake_modes": list(wake_modes),
            "state": local.get("effective_state") or record.get("status"),
            "registered": bool(local),
            "exposure": local.get("exposure"),
        })
    members.sort(key=lambda item: (str(item.get("harness")),
                                   str(item.get("session")),
                                   str(item.get("key"))))
    role_counts: dict[str, int] = {}
    workunit_counts: dict[str, int] = {}
    member_keys = {str(item.get("key")) for item in members if item.get("key")}
    for member in members:
        role_counts[str(member.get("role") or "unassigned")] = (
            role_counts.get(str(member.get("role") or "unassigned"), 0) + 1)
        workunit = member.get("workunit")
        if workunit:
            workunit_counts[str(workunit)] = (
                workunit_counts.get(str(workunit), 0) + 1)
    claim_report = claims(root, config=cfg)
    blocker_report = blockers(root, config=cfg)
    handoff_report = handoffs(root, config=cfg, limit=MAX_FLIGHT_HANDOFFS)
    wake_report = read_wake_status(root, config=cfg)
    try:
        mapped = O.mapping(root, harness=harness, session=session, config=cfg)
        readiness = mapped["readiness"]
        state = mapped["state"]
    except (M.MeshError, O.OzmaMeshError) as exc:
        readiness = {"ready": False, "reasons": [str(exc)]}
        state = "unknown"
    claim_rows = []
    for row in (claim_report.get("claims") or ())[:MAX_FLIGHT_CLAIMS]:
        holder = row.get("holder") or {}
        claim_rows.append({
            "claim_id": row.get("claim_id"),
            "direction": row.get("direction"),
            "scope": row.get("scope"),
            "resource": row.get("resource"),
            "state": row.get("state"),
            "advisory": True,
            "holder": holder.get("agent_id"),
        })
    return {
        "schema": SCHEMA,
        "conformance": CONFORMANCE,
        "verified": False,
        "network": False,
        "flight": {"id": fid, "source": source},
        "flight_id": fid,
        "flight_source": source,
        "state": state,
        "members": members[:bound],
        "member_count": len(members),
        "members_truncated": len(members) > bound,
        "role_counts": role_counts,
        "workunit_counts": workunit_counts,
        "counts": {
            "members": len(members),
            "roles": len([name for name in role_counts
                          if name != "unassigned"]),
            "workunits": len(workunit_counts),
            "links": sum(
                1 for item in (mesh.get("links") or ())
                if _mesh_record_in_flight(item, ("from", "to"), member_keys)),
            "delegations": sum(
                1 for item in (mesh.get("delegations") or ())
                if _mesh_record_in_flight(
                    item, ("grantor", "grantee"), member_keys)),
            "claims_active": int(
                (claim_report.get("counts") or {}).get("active") or 0),
            "claim_conflicts": int(claim_report.get("conflict_count") or 0),
            "blockers_open": int(
                (blocker_report.get("counts") or {}).get("open") or 0),
            "handoffs_pending": int(
                (handoff_report.get("counts") or {}).get("pending") or 0),
            "wake_pending": int(
                (wake_report.get("counts") or {}).get("pending_requests") or 0),
        },
        "claims": claim_rows,
        "blockers": list(blocker_report.get("edges") or ())[
            :MAX_FLIGHT_BLOCKERS],
        "handoff_counts": dict(handoff_report.get("counts") or {}),
        "wake": {
            "approved": wake_report.get("approved"),
            "budget_turns": wake_report.get("budget_turns"),
            "used_turns": wake_report.get("used_turns"),
            "pending_requests": wake_report.get("pending_requests"),
            "claimable": True,
            "spawnable": False,
        },
        "readiness": readiness,
        "errors": (errors + list(mesh.get("errors") or ())
                   + list(claim_report.get("errors") or ())
                   + list(blocker_report.get("errors") or ())
                   + list(handoff_report.get("errors") or ())
                   + list(wake_report.get("errors") or ()))[:16],
    }



def _criteria_digest(criteria: Any) -> str | None:
    if not isinstance(criteria, (list, tuple)) or not criteria:
        return None
    payload = json.dumps(list(criteria), sort_keys=True, separators=(",", ":"),
                         ensure_ascii=True, allow_nan=False)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _handoff_path(root: str | Path, flight: str, handoff_id: str) -> Path:
    if not ID_RE.fullmatch(str(handoff_id)):
        _refuse("handoff id is not a safe id", code="invalid_handoff_id")
    return _hashed_path(root, HANDOFFS_DIR, flight, "handoff", handoff_id)


def _handoff_view(record: Mapping[str, Any]) -> dict[str, Any]:
    sender = record.get("sender") or {}
    return {
        "handoff_id": record.get("handoff_id"),
        "direction": record.get("direction"),
        "state": record.get("state"),
        "sender": sender.get("agent_id"),
        "recipient": record.get("recipient"),
        "workunit": record.get("workunit"),
        "criteria_digest": record.get("criteria_digest"),
        "criteria_count": int(record.get("criteria_count") or 0),
        "ack_message_id": record.get("ack_message_id"),
        "completion": record.get("completion"),
        "created_at": record.get("created_at"),
        "updated_at": record.get("updated_at"),
        "authority_transferred": False,
    }


def _handoff_records(root: str | Path, flight: str
                     ) -> tuple[list[dict[str, Any]], list[str]]:
    return _records(root, HANDOFFS_DIR, flight)


def _evict_handoffs(root: str | Path, flight: str) -> int:
    records, _ = _handoff_records(root, flight)
    terminal = sorted(
        (item for item in records
         if item.get("state") in {"complete", "rejected"}),
        key=lambda item: str(item.get("updated_at") or ""))
    removed = 0
    for record in terminal:
        if len(records) - removed < MAX_HANDOFFS:
            break
        try:
            os.unlink(_handoff_path(root, flight,
                                    str(record.get("handoff_id"))))
        except OSError:
            continue
        removed += 1
    return removed


def _write_handoff(root: str | Path, flight: str,
                   record: Mapping[str, Any]) -> bool:
    """Write one lifecycle record if capacity allows; never overwrite state."""
    path = _handoff_path(root, flight, str(record.get("handoff_id")))
    if _is_file(path):
        return False
    records, _ = _handoff_records(root, flight)
    if len(records) >= MAX_HANDOFFS:
        _evict_handoffs(root, flight)
        records, _ = _handoff_records(root, flight)
        if len(records) >= MAX_HANDOFFS:
            return False
    _write_record(path, record)
    return True


def _handoff_record(*, flight: str, handoff_id: str, direction: str,
                    envelope: Mapping[str, Any], now: float,
                    state: str = "pending") -> dict[str, Any]:
    body = envelope.get("body") or {}
    return {
        "schema": SCHEMA,
        "kind": "handoff",
        "flight_id": flight,
        "handoff_id": handoff_id,
        "direction": direction,
        "state": state,
        "sender": {
            "agent_id": envelope.get("from"),
            "card_fingerprint": envelope.get("from_card"),
        },
        "recipient": envelope.get("to"),
        "workunit": body.get("workunit") or envelope.get("workunit"),
        "criteria_digest": _criteria_digest(body.get("criteria")),
        "criteria_count": len(body.get("criteria") or ()),
        "ack_message_id": None,
        "thread": envelope.get("thread"),
        "channel": envelope.get("channel"),
        "completion": body.get("completion"),
        "authority_transferred": False,
        "created_at": _iso(now),
        "updated_at": _iso(now),
    }


def _track_outbound_handoff(root: str | Path, config: O.OzmaMeshConfig,
                            envelope: Mapping[str, Any], *, now: float) -> None:
    flight = flight_id(root, config)
    record = _handoff_record(
        flight=flight, handoff_id=str(envelope["id"]), direction="outbound",
        envelope=envelope, now=now)
    _write_handoff(root, flight, record)


def _record_inbound_handoff(root: str | Path, config: O.OzmaMeshConfig,
                            envelope: Mapping[str, Any], *, now: float
                            ) -> None:
    flight = flight_id(root, config)
    handoff_id = str(envelope["id"])
    reply_to = envelope.get("in_reply_to")
    if isinstance(reply_to, str) and ID_RE.fullmatch(reply_to):
        existing = _read_record(_handoff_path(root, flight, reply_to))
        if existing is not None:
            completion = (envelope.get("body") or {}).get("completion")
            expected = existing.get("recipient")
            if (completion in {"done", "partial"}
                    and (expected is None or envelope.get("from") == expected)):
                existing["state"] = "complete"
                existing["completion"] = completion
                existing["ack_message_id"] = handoff_id
                existing["updated_at"] = _iso(now)
                _write_record(_handoff_path(root, flight, reply_to), existing)
                return
    record = _handoff_record(
        flight=flight, handoff_id=handoff_id, direction="inbound",
        envelope=envelope, now=now)
    _write_handoff(root, flight, record)


def _apply_handoff_ack(root: str | Path, flight: str,
                       envelope: Mapping[str, Any], *, now: float) -> None:
    reply = envelope.get("in_reply_to")
    if not isinstance(reply, str) or not ID_RE.fullmatch(reply):
        return
    path = _handoff_path(root, flight, reply)
    record = _read_record(path)
    if record is None or record.get("state") == "complete":
        return
    recipient = record.get("recipient")
    if recipient and envelope.get("from") != recipient:
        return
    body = envelope.get("body") or {}
    record["state"] = "accepted" if body.get("accepted") is True else "rejected"
    record["ack_message_id"] = envelope.get("id")
    record["updated_at"] = _iso(now)
    _write_record(path, record)


def handoffs(root: str | Path, *, config: O.OzmaMeshConfig | None = None,
             limit: int = MAX_HANDOFFS) -> dict[str, Any]:
    """List handoff lifecycle metadata; no body or criteria text is retained."""
    cfg = config if config is not None else O.load_config(root)
    flight = flight_id(root, cfg)
    records, errors = _handoff_records(root, flight)
    counts = {state: 0 for state in HANDOFF_STATES}
    for record in records:
        state = str(record.get("state") or "pending")
        counts[state] = counts.get(state, 0) + 1
    bound = max(0, min(int(limit), MAX_HANDOFFS))
    rows = [_handoff_view(record) for record in records]
    rows.sort(key=lambda item: (str(item.get("updated_at")),
                                str(item.get("handoff_id"))), reverse=True)
    return {"schema": SCHEMA, "conformance": CONFORMANCE,
            "flight_id": flight, "handoffs": rows[:bound],
            "count": len(rows), "counts": counts,
            "truncated": len(rows) > bound, "errors": errors,
            "network": False}


def handoff_show(root: str | Path, handoff_id: str, *,
                 config: O.OzmaMeshConfig | None = None) -> dict[str, Any]:
    cfg = config if config is not None else O.load_config(root)
    flight = flight_id(root, cfg)
    record = _read_record(_handoff_path(root, flight, handoff_id))
    if record is None:
        _refuse("unknown handoff", code="unknown_handoff")
    return {"schema": SCHEMA, "conformance": CONFORMANCE,
            "flight_id": flight, "handoff": _handoff_view(record),
            "network": False}


def handoff_respond(root: str | Path, handoff_id: str, action: str, *,
                    reason: str = "", harness: str | None = None,
                    session: str | None = None,
                    config: O.OzmaMeshConfig | None = None,
                    now: float | None = None) -> dict[str, Any]:
    """Host/coordinate acknowledgement of one handoff.

    ``accept``/``reject`` queue an ``ack``; ``complete`` queues a ``handoff``
    with ``completion=done``.  None of them transfers a credential, lease,
    delegation or merge authority.
    """
    if action not in {"accept", "reject", "complete"}:
        _refuse("handoff action must be accept, reject or complete",
                code="invalid_value")
    cfg = config if config is not None else O.load_config(root)
    _require_coordinate_identity(root, harness, session)
    _require_message_gate(root, cfg)
    flight = flight_id(root, cfg)
    path = _handoff_path(root, flight, handoff_id)
    record = _read_record(path)
    if record is None:
        _refuse("unknown handoff", code="unknown_handoff")
    state = str(record.get("state") or "pending")
    direction = str(record.get("direction") or "")
    if action in {"accept", "reject"}:
        if direction != "inbound":
            _refuse("only an inbound handoff can be accepted or rejected",
                    code="handoff_direction")
        if state != "pending":
            _refuse(f"handoff is already {state}", code="handoff_closed")
    else:
        if state not in {"pending", "accepted"}:
            _refuse(f"handoff is already {state}", code="handoff_closed")
    why = _text(reason, "reason", MAX_REASON, allow_empty=True, one_line=True)
    sender = (record.get("sender") or {}).get("agent_id")
    target = sender if direction == "inbound" else record.get("recipient")
    if direction == "inbound":
        recipient = record.get("recipient")
        if recipient and recipient != cfg.agent_id:
            _refuse("this handoff is addressed to another agent",
                    code="not_handoff_recipient")
    else:
        if sender and sender != cfg.agent_id:
            _refuse("only the handoff sender may complete it",
                    code="not_handoff_sender")
    if not isinstance(target, str) or not O.AGENT_RE.fullmatch(target):
        _refuse("handoff has no host-provided peer id to answer",
                code="handoff_no_recipient")
    if action == "complete":
        reply_type = "handoff"
        body: dict[str, Any] = {"completion": "done"}
        if why:
            body["text"] = why
        prefix = "hco"
    else:
        reply_type = "ack"
        body = {"accepted": action == "accept"}
        if why:
            body["reason"] = why
        prefix = "ack"
    moment = time.time() if now is None else float(now)
    reply = build_envelope(
        flight_id=flight,
        message_id=_derived_id(prefix, flight, handoff_id),
        ozma_type=reply_type, body=body,
        from_agent=cfg.agent_id, from_card=cfg.card_fingerprint or None,
        to_agent=target, in_reply_to=handoff_id,
        thread=record.get("thread"), channel=record.get("channel"),
        workunit=record.get("workunit"),
        requires_ack=False,
        body_trust="untrusted" if why else "typed", now=moment)
    queued = enqueue(root, reply, config=cfg, track_handoff=False, now=moment)
    record["state"] = ("complete" if action == "complete"
                       else "accepted" if action == "accept" else "rejected")
    if action == "complete":
        record["completion"] = "done"
    record["ack_message_id"] = reply["id"]
    record["updated_at"] = _iso(moment)
    _write_record(path, record)
    return {"schema": SCHEMA, "conformance": CONFORMANCE, "flight_id": flight,
            "handoff": _handoff_view(record),
            "reply": {"id": queued.get("id"), "queued": queued.get("queued"),
                      "duplicate": queued.get("duplicate")},
            "authority_transferred": False, "network": False}


def _blocker_path(root: str | Path, flight: str) -> Path:
    if not O.FLIGHT_RE.fullmatch(str(flight)):
        raise OzmaBridgeError("flight_id is not a safe mesh id")
    return bridge_root(root) / BLOCKERS_DIR / f"{flight}.json"


def _edge_id(flight: str, blocked_id: str, blocker_id: str,
             workunit: str | None, resource: str | None) -> str:
    digest = hashlib.sha256(
        f"{flight}\0{blocked_id}\0{blocker_id}\0{workunit or ''}\0"
        f"{resource or ''}".encode("utf-8")).hexdigest()[:24]
    return f"blk_{digest}"


def _load_blockers(root: str | Path, flight: str, *,
                   strict: bool = False) -> dict[str, Any]:
    path = _blocker_path(root, flight)
    try:
        record = _read_record(path) if _is_file(path) else None
    except (M.MeshError, O.OzmaMeshError, OSError) as exc:
        if strict:
            raise
        return {"schema": SCHEMA, "flight_id": flight, "edges": [],
                "updated_at": None, "errors": [str(exc)]}
    if record is None:
        return {"schema": SCHEMA, "flight_id": flight, "edges": [],
                "updated_at": None}
    record.setdefault("edges", [])
    return record


def _blocker_view(edge: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "edge_id": edge.get("edge_id"),
        "blocked_id": edge.get("blocked_id"),
        "blocker_id": edge.get("blocker_id"),
        "workunit": edge.get("workunit"),
        "resource": edge.get("resource"),
        "state": edge.get("state"),
        "created_at": edge.get("created_at"),
        "cleared_at": edge.get("cleared_at"),
        "source": edge.get("source"),
        "advisory": True,
        "blocking": False,
    }


def _blocker_text(value: Any, name: str, limit: int) -> str | None:
    if value is None:
        return None
    return _text(value, name, limit, one_line=True)


def add_blocker(root: str | Path, *, blocked_id: Any, blocker_id: Any,
                workunit: Any = None, resource: Any = None,
                source: str = "cli",
                config: O.OzmaMeshConfig | None = None,
                now: float | None = None) -> dict[str, Any]:
    """Create one advisory blocker edge; never affects a lease or merge."""
    cfg = config if config is not None else O.load_config(root)
    flight = flight_id(root, cfg)
    blocked = _blocker_text(blocked_id, "blocked_id", MAX_ID)
    blocker = _blocker_text(blocker_id, "blocker_id", MAX_ID)
    if not blocked or not ID_RE.fullmatch(blocked):
        _refuse("blocked_id is not a safe id", code="invalid_blocker_id")
    if not blocker or not ID_RE.fullmatch(blocker):
        _refuse("blocker_id is not a safe id", code="invalid_blocker_id")
    unit = _blocker_text(workunit, "workunit", MAX_WORKUNIT)
    if unit and not WORKUNIT_RE.fullmatch(unit):
        _refuse("workunit is not a safe id", code="invalid_value")
    res = _blocker_text(resource, "resource", MAX_RESOURCE)
    origin = _text(source, "source", 32, one_line=True)
    moment = time.time() if now is None else float(now)
    with _flight_lock(root, flight):
        record = _load_blockers(root, flight, strict=True)
        edges = list(record.get("edges") or ())
        edge_id = _edge_id(flight, blocked, blocker, unit, res)
        for edge in edges:
            if edge.get("edge_id") != edge_id:
                continue
            if edge.get("state") == "open":
                return {"schema": SCHEMA, "flight_id": flight,
                        "edge": _blocker_view(edge), "created": False,
                        "advisory": True, "network": False}
            edge.update({"state": "open", "created_at": _iso(moment),
                         "cleared_at": None, "source": origin})
            record["edges"] = edges
            record["updated_at"] = _iso(moment)
            _write_record(_blocker_path(root, flight), record)
            return {"schema": SCHEMA, "flight_id": flight,
                    "edge": _blocker_view(edge), "created": True,
                    "advisory": True, "network": False}
        if len(edges) >= MAX_BLOCKER_EDGES:
            cleared = [edge for edge in edges
                       if edge.get("state") == "cleared"]
            cleared.sort(key=lambda item: str(item.get("cleared_at") or ""))
            room = len(edges) - MAX_BLOCKER_EDGES + 1
            drop = {str(edge.get("edge_id")) for edge in cleared[:room]}
            edges = [edge for edge in edges
                     if str(edge.get("edge_id")) not in drop]
            if len(edges) >= MAX_BLOCKER_EDGES:
                _refuse("the blocker store is full", code="blocker_store_full")
        edge = {
            "edge_id": edge_id,
            "blocked_id": blocked,
            "blocker_id": blocker,
            "workunit": unit,
            "resource": res,
            "state": "open",
            "created_at": _iso(moment),
            "cleared_at": None,
            "source": origin,
        }
        edges.append(edge)
        record["edges"] = edges
        record["updated_at"] = _iso(moment)
        _write_record(_blocker_path(root, flight), record)
    return {"schema": SCHEMA, "flight_id": flight, "edge": _blocker_view(edge),
            "created": True, "advisory": True, "network": False}


def clear_blockers(root: str | Path, *, identifier: Any = None,
                   blocked_id: Any = None, blocker_id: Any = None,
                   workunit: Any = None, resource: Any = None,
                   source: str = "cli",
                   config: O.OzmaMeshConfig | None = None,
                   now: float | None = None) -> dict[str, Any]:
    """Clear advisory edges by edge id, party id, workunit or resource."""
    cfg = config if config is not None else O.load_config(root)
    flight = flight_id(root, cfg)
    label = _blocker_text(identifier, "identifier", MAX_ID)
    blocked = _blocker_text(blocked_id, "blocked_id", MAX_ID)
    blocker = _blocker_text(blocker_id, "blocker_id", MAX_ID)
    unit = _blocker_text(workunit, "workunit", MAX_WORKUNIT)
    res = _blocker_text(resource, "resource", MAX_RESOURCE)
    if not any((label, blocked, blocker, unit, res)):
        _refuse("unblock needs an id, workunit or resource",
                code="invalid_value")
    origin = _text(source, "source", 32, one_line=True)
    moment = time.time() if now is None else float(now)
    cleared: list[dict[str, Any]] = []
    with _flight_lock(root, flight):
        record = _load_blockers(root, flight, strict=True)
        edges = list(record.get("edges") or ())
        for edge in edges:
            if edge.get("state") != "open":
                continue
            if label is not None:
                if label not in {str(edge.get("edge_id")),
                                 str(edge.get("blocked_id")),
                                 str(edge.get("blocker_id"))}:
                    continue
            if blocked is not None and edge.get("blocked_id") != blocked:
                continue
            if blocker is not None and edge.get("blocker_id") != blocker:
                continue
            if unit is not None and edge.get("workunit") != unit:
                continue
            if res is not None and edge.get("resource") != res:
                continue
            edge.update({"state": "cleared", "cleared_at": _iso(moment),
                         "source": origin})
            cleared.append(edge)
        if cleared:
            record["edges"] = edges
            record["updated_at"] = _iso(moment)
            _write_record(_blocker_path(root, flight), record)
    return {"schema": SCHEMA, "flight_id": flight,
            "edges": [_blocker_view(edge) for edge in cleared],
            "cleared": len(cleared), "advisory": True, "network": False}


def blockers(root: str | Path, *, config: O.OzmaMeshConfig | None = None,
             include_cleared: bool = True,
             limit: int = MAX_BLOCKER_EDGES) -> dict[str, Any]:
    """Read-only advisory blocker edges; never a hard block."""
    cfg = config if config is not None else O.load_config(root)
    flight = flight_id(root, cfg)
    record = _load_blockers(root, flight)
    edges = list(record.get("edges") or ())
    counts = {state: 0 for state in BLOCKER_STATES}
    for edge in edges:
        state = str(edge.get("state") or "open")
        counts[state] = counts.get(state, 0) + 1
    visible = [edge for edge in edges
               if include_cleared or edge.get("state") == "open"]
    bound = max(0, min(int(limit), MAX_BLOCKER_EDGES))
    rows = [_blocker_view(edge) for edge in visible]
    rows.sort(key=lambda item: (str(item.get("created_at")),
                                str(item.get("edge_id"))), reverse=True)
    return {"schema": SCHEMA, "conformance": CONFORMANCE,
            "flight_id": flight, "edges": rows[:bound], "counts": counts,
            "count": len(rows), "truncated": len(rows) > bound,
            "advisory": True, "blocking": False,
            "errors": list(record.get("errors") or ()), "network": False}


def _blocked_parts(envelope: Mapping[str, Any]) -> tuple[str | None, str | None]:
    body = envelope.get("body") or {}
    blocked = (body.get("blocked_id") or body.get("resource")
               or envelope.get("workunit") or envelope.get("in_reply_to"))
    blocker = body.get("blocker_id") or envelope.get("from") or envelope.get("id")
    return (str(blocked) if blocked else None,
            str(blocker) if blocker else None)


def _apply_blocked_envelope(root: str | Path, config: O.OzmaMeshConfig,
                            envelope: Mapping[str, Any], *, now: float) -> None:
    blocked, blocker = _blocked_parts(envelope)
    if not blocked or not blocker:
        return
    body = envelope.get("body") or {}
    try:
        add_blocker(
            root, blocked_id=blocked, blocker_id=blocker,
            workunit=envelope.get("workunit"), resource=body.get("resource"),
            source=str(envelope.get("direction") or "envelope"),
            config=config, now=now)
    except (M.MeshRefusal, O.OzmaMeshError):
        return


def _apply_unblocked_envelope(root: str | Path, config: O.OzmaMeshConfig,
                              envelope: Mapping[str, Any], *, now: float
                              ) -> None:
    blocked, blocker = _blocked_parts(envelope)
    body = envelope.get("body") or {}
    if not blocked and not blocker:
        return
    try:
        clear_blockers(
            root, blocked_id=blocked,
            blocker_id=body.get("blocker_id") or blocker,
            resource=body.get("resource"),
            source=str(envelope.get("direction") or "envelope"),
            config=config, now=now)
    except (M.MeshRefusal, O.OzmaMeshError):
        return


def _a2a_text(ozma_type: str, body: Mapping[str, Any]) -> str:
    parts: list[str] = []
    subject = body.get("subject")
    text = body.get("text")
    reason = body.get("reason")
    if isinstance(subject, str) and subject:
        parts.append(subject)
    if isinstance(text, str) and text and text not in " ".join(parts):
        parts.append(text)
    if isinstance(reason, str) and reason and reason not in " ".join(parts):
        parts.append(reason)
    criteria = body.get("criteria")
    if isinstance(criteria, (list, tuple)) and criteria:
        parts.append("criteria: " + "; ".join(str(item) for item in criteria))
    if not parts:
        if ozma_type == "ack":
            parts.append("accepted" if body.get("accepted") is True
                         else "rejected")
        else:
            parts.append(ozma_type)
    flattened = "\n".join(parts)
    if len(flattened) > MAX_BODY:
        _refuse("A2A text exceeds the bridge body bound", code="too_large")
    return flattened


def to_a2a(envelope: Mapping[str, Any], *,
           config: O.OzmaMeshConfig | None = None) -> dict[str, Any]:
    """Declared, lossy A2A v1.0-shaped payload; never opens a socket.

    A claim or a block/unblock edge is refused: its scope or edge is required
    and cannot be represented, so silently dropping it would change meaning.
    """
    normalized = validate_envelope(envelope)
    ozma_type = str(normalized["type"])
    required_loss = A2A_REQUIRED_UNREPRESENTABLE.get(ozma_type)
    if required_loss:
        _refuse(
            f"A2A cannot represent the required field {required_loss!r}; "
            "nothing was translated",
            code="a2a_unsupported_required")
    body = normalized.get("body") or {}
    loss_fields = list(O.A2A_LOSS_FIELDS)
    for field in A2A_EXTRA_LOSS_FIELDS:
        present = (normalized.get(field) is not None
                   or body.get(field) is not None)
        if present and field not in loss_fields:
            loss_fields.append(field)
    loss_fields = sorted(set(loss_fields))
    text, redactions = M._redact(_a2a_text(ozma_type, body))
    metadata: dict[str, Any] = {}
    for name in ("from", "to", "from_card", "in_reply_to", "thread",
                 "requires_ack", "channel", "priority", "deliver_at",
                 "wake", "wake_at"):
        value = normalized.get(name)
        if value is not None:
            metadata[name] = value
    for name in ("purpose", "accepted", "reason", "completion", "claim_id"):
        value = body.get(name)
        if value is None:
            continue
        if name == "reason" and isinstance(value, str):
            value, found = M._redact(value)
            redactions.extend(found)
        metadata[name] = value
    if redactions:
        metadata["redactions"] = sorted(set(redactions))
    metadata.update({
        "bridge": BRIDGE_NAME,
        "schema": SCHEMA,
        "type": ozma_type,
        "flight_id": normalized["flight_id"],
        "body_trust": "untrusted",
    })
    payload: dict[str, Any] = {
        "kind": "message",
        "role": "agent" if normalized.get("direction") == "outbound"
                else "user",
        "messageId": normalized["id"],
        "parts": [{"kind": "text", "text": text}],
        "metadata": {
            "ozagia": metadata,
            "lossy": True,
            "loss_fields": loss_fields,
            "extensions": {
                "priority": "extension",
                "deliver_at": "extension",
                "requires_ack": "extension",
                "channel": "extension",
            },
            "a2a_version": A2A_VERSION,
        },
        "lossy": True,
        "loss_fields": loss_fields,
        "declared": True,
        "conformance": CONFORMANCE,
        "network": False,
    }
    if normalized.get("thread"):
        payload["contextId"] = normalized["thread"]
    return payload


def from_a2a(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Rebuild one declared bridge envelope from an A2A-shaped payload.

    The result is always lossy and untrusted: the adapter cannot restore
    structured criteria, claim scope, blockers, subscriptions, the priority
    floor, compliance, refs or a workunit.
    """
    if not isinstance(payload, Mapping):
        _refuse("A2A payload must be an object", code="a2a_invalid")
    if payload.get("kind") != "message":
        _refuse("A2A payload is not a message", code="a2a_invalid")
    message_id = payload.get("messageId")
    if not isinstance(message_id, str) or not ID_RE.fullmatch(message_id):
        _refuse("A2A messageId is not a safe id", code="a2a_invalid")
    metadata = payload.get("metadata")
    if not isinstance(metadata, Mapping):
        _refuse("A2A payload has no metadata", code="a2a_invalid")
    ozagia = metadata.get("ozagia")
    if not isinstance(ozagia, Mapping):
        _refuse("A2A payload has no declared ozagia metadata",
                code="a2a_invalid")
    ozma_type = ozagia.get("type")
    if ozma_type not in OZMA_TYPES:
        _refuse("A2A metadata has no declared bridge type", code="a2a_invalid")
    flight = ozagia.get("flight_id")
    if not isinstance(flight, str) or not O.FLIGHT_RE.fullmatch(flight):
        _refuse("A2A metadata has no safe flight id", code="a2a_invalid")
    texts: list[str] = []
    parts = payload.get("parts")
    if parts is not None:
        if not isinstance(parts, list):
            _refuse("A2A parts must be a list", code="a2a_invalid")
        for part in parts[:32]:
            if (isinstance(part, Mapping) and part.get("kind") == "text"
                    and isinstance(part.get("text"), str)):
                texts.append(part["text"])
    text = "\n".join(texts)[:MAX_BODY]
    text, redactions = M._redact(text)
    body: dict[str, Any] = {}
    if ozma_type == "request":
        purpose = ozagia.get("purpose", "general")
        body["purpose"] = purpose if purpose in REQUEST_PURPOSES else "general"
        if text:
            body["text"] = text
    elif ozma_type == "response":
        if isinstance(ozagia.get("accepted"), bool):
            body["accepted"] = ozagia["accepted"]
        if text:
            body["text"] = text
    elif ozma_type == "ack":
        accepted = ozagia.get("accepted")
        if not isinstance(accepted, bool):
            _refuse("A2A ack has no accepted flag", code="a2a_invalid")
        body["accepted"] = accepted
        reason = ozagia.get("reason")
        if isinstance(reason, str) and reason:
            body["reason"], found = M._redact(reason)
            redactions.extend(found)
        elif text:
            body["reason"] = text
    elif ozma_type in {"status", "heads-up"}:
        body["text"] = text or ozma_type
    elif ozma_type == "handoff":
        completion = ozagia.get("completion")
        if completion in COMPLETIONS:
            body["completion"] = completion
        if text:
            body["text"] = text
    elif ozma_type == "release":
        claim_id = ozagia.get("claim_id")
        if isinstance(claim_id, str) and claim_id:
            body["claim_id"] = claim_id
    envelope: dict[str, Any] = {
        "schema": SCHEMA,
        "bridge": BRIDGE_NAME,
        "flight_id": flight,
        "id": message_id,
        "type": ozma_type,
        "direction": "inbound",
        "body": body,
        "body_trust": "untrusted",
    }
    for name in ("from", "to", "from_card", "in_reply_to", "thread",
                 "requires_ack", "channel", "priority", "deliver_at",
                 "wake", "wake_at"):
        value = ozagia.get(name)
        if value is not None:
            envelope[name] = value
    if redactions:
        envelope["redactions"] = sorted({str(item)[:64]
                                         for item in redactions})[:32]
    normalized = validate_envelope(envelope, direction="inbound",
                                   flight_id=flight)
    loss_fields = metadata.get("loss_fields")
    if not isinstance(loss_fields, list):
        loss_fields = list(O.A2A_LOSS_FIELDS)
    loss_fields = sorted({str(item)[:64] for item in loss_fields
                          if isinstance(item, str)})[:32]
    return {"schema": SCHEMA, "envelope": normalized, "lossy": True,
            "loss_fields": loss_fields, "declared": True,
            "conformance": CONFORMANCE, "untrusted": True,
            "network": False}


def _wake_path(root: str | Path, flight: str, kind: str,
               record_id: str) -> Path:
    prefix = "approval" if kind == "approval" else "request"
    return _hashed_path(root, WAKE_DIR, flight, prefix, record_id)


def _wake_records(root: str | Path, flight: str
                  ) -> tuple[list[dict[str, Any]], list[str]]:
    return _records(root, WAKE_DIR, flight)


def _approval_state(record: Mapping[str, Any], now: float) -> str:
    if record.get("revoked_at"):
        return "revoked"
    expiry = _parse_iso(record.get("expires_at"))
    if expiry is not None and expiry <= now:
        return "expired"
    return "active"


def _approval_view(record: Mapping[str, Any], *, now: float) -> dict[str, Any]:
    budget = int(record.get("budget_turns") or 0)
    used = int(record.get("used_turns") or 0)
    return {
        "approval_id": record.get("approval_id"),
        "mode": record.get("mode"),
        "target": record.get("target"),
        "budget_turns": budget,
        "used_turns": used,
        "remaining_turns": max(0, budget - used),
        "expires_at": record.get("expires_at"),
        "state": _approval_state(record, now),
        "approved_by": record.get("approved_by"),
        "source": record.get("source"),
        "created_at": record.get("created_at"),
        "revoked_at": record.get("revoked_at"),
        "claim_ids": [item.get("request_id")
                      for item in (record.get("claim_log") or ())
                      if isinstance(item, Mapping)],
    }


def _request_view(record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "request_id": record.get("request_id"),
        "message_id": record.get("message_id"),
        "mode": record.get("mode"),
        "target": record.get("target"),
        "from": record.get("from"),
        "wake_at": record.get("wake_at"),
        "state": record.get("state"),
        "claimed_at": record.get("claimed_at"),
        "approval_id": record.get("approval_id"),
        "created_at": record.get("created_at"),
    }


def _target_wake_modes(root: str | Path, config: O.OzmaMeshConfig, flight: str,
                       target: str) -> list[str]:
    if target == config.agent_id:
        return list(config.wake_modes)
    records, _ = _identity_records(root)
    for record in records:
        if record.get("flight_id") != flight:
            continue
        ozma = record.get("ozma") or {}
        if ozma.get("agent_id") == target:
            return list(record.get("wake_modes") or ())
    return []


def wake_approve(root: str | Path, *, mode: str, budget_turns: int,
                 ttl_s: int = 3600, agent_id: str | None = None,
                 approved_by: str, source: str,
                 config: O.OzmaMeshConfig | None = None,
                 now: float | None = None) -> dict[str, Any]:
    """Write a host approval.  Only the CLI passes ``source='cli'``."""
    if source != "cli" or approved_by != "host":
        _refuse("wake approval is a host CLI action", code="wake_cli_only")
    cfg = config if config is not None else O.load_config(root)
    if mode not in WAKE_APPROVAL_MODES:
        _refuse("wake mode must be message-triggered or scheduled",
                code="wake_mode_invalid")
    if mode not in cfg.wake_modes:
        _refuse("the configured wake_modes do not declare this mode",
                code="wake_mode_not_declared")
    if isinstance(budget_turns, bool) or not isinstance(budget_turns, int) or (
            not 1 <= budget_turns <= MAX_WAKE_BUDGET):
        _refuse(f"budget_turns must be 1..{MAX_WAKE_BUDGET}",
                code="wake_budget_invalid")
    if isinstance(ttl_s, bool) or not isinstance(ttl_s, int) or not (
            MIN_WAKE_TTL_S <= ttl_s <= MAX_WAKE_TTL_S):
        _refuse(f"wake TTL must be {MIN_WAKE_TTL_S}..{MAX_WAKE_TTL_S} seconds",
                code="wake_ttl_invalid")
    target = agent_id or cfg.agent_id
    if not isinstance(target, str) or not O.AGENT_RE.fullmatch(target):
        _refuse("wake needs a host-provided agent id",
                code="wake_identity_required")
    moment = time.time() if now is None else float(now)
    flight = flight_id(root, cfg)
    with _flight_lock(root, flight):
        records, _ = _wake_records(root, flight)
        approvals = [item for item in records
                     if item.get("kind") == "wake_approval"]
        if len(approvals) >= MAX_WAKE_APPROVALS:
            _refuse("the wake approval store is full", code="wake_store_full")
        approval_id = _derived_id("wak", flight,
                                  f"{target}\0{mode}\0{moment}")
        record = {
            "schema": SCHEMA,
            "kind": "wake_approval",
            "flight_id": flight,
            "approval_id": approval_id,
            "mode": mode,
            "target": target,
            "budget_turns": budget_turns,
            "used_turns": 0,
            "expires_at": _iso(moment + ttl_s),
            "approved_by": "host",
            "source": "cli",
            "created_at": _iso(moment),
            "revoked_at": None,
            "claim_log": [],
        }
        _write_record(_wake_path(root, flight, "approval", approval_id), record)
    return {"schema": SCHEMA, "flight_id": flight,
            "approval": _approval_view(record, now=moment),
            "spawnable": False, "network": False}


def wake_revoke(root: str | Path, approval_id: str, *,
                source: str,
                config: O.OzmaMeshConfig | None = None,
                now: float | None = None) -> dict[str, Any]:
    """Revoke one host approval; CLI-only like approval."""
    if source != "cli":
        _refuse("wake revocation is a host CLI action", code="wake_cli_only")
    cfg = config if config is not None else O.load_config(root)
    flight = flight_id(root, cfg)
    moment = time.time() if now is None else float(now)
    path = _wake_path(root, flight, "approval", approval_id)
    with _flight_lock(root, flight):
        record = _read_record(path)
        if record is None:
            _refuse("unknown wake approval", code="unknown_wake_approval")
        record["revoked_at"] = _iso(moment)
        _write_record(path, record)
    return {"schema": SCHEMA, "flight_id": flight,
            "approval": _approval_view(record, now=moment),
            "spawnable": False, "network": False}


def wake_list(root: str | Path, *, config: O.OzmaMeshConfig | None = None,
              now: float | None = None) -> dict[str, Any]:
    """Read-only approval and request metadata; creates nothing."""
    cfg = config if config is not None else O.load_config(root)
    flight = flight_id(root, cfg)
    records, errors = _wake_records(root, flight)
    moment = time.time() if now is None else float(now)
    approvals = []
    requests = []
    counts = {"active": 0, "expired": 0, "revoked": 0, "pending": 0,
              "claimed": 0}
    for record in records:
        if record.get("kind") == "wake_approval":
            view = _approval_view(record, now=moment)
            counts[str(view["state"])] += 1
            approvals.append(view)
        elif record.get("kind") == "wake_request":
            view = _request_view(record)
            counts[str(view.get("state") or "pending")] += 1
            requests.append(view)
    approvals.sort(key=lambda item: str(item.get("created_at")), reverse=True)
    requests.sort(key=lambda item: str(item.get("wake_at")), reverse=True)
    return {"schema": SCHEMA, "conformance": CONFORMANCE,
            "flight_id": flight, "approvals": approvals,
            "requests": requests, "counts": counts,
            "claimable": True, "spawnable": False, "errors": errors,
            "network": False}


def wake_claim(root: str | Path, *, config: O.OzmaMeshConfig | None = None,
               now: float | None = None) -> dict[str, Any]:
    """Host runner primitive: return the next due request if approved.

    It never spawns a process, model turn or lease.  A claim only increments
    the approval budget and writes an id-only audit entry.
    """
    cfg = config if config is not None else O.load_config(root)
    flight = flight_id(root, cfg)
    moment = time.time() if now is None else float(now)
    with _flight_lock(root, flight):
        records, _ = _wake_records(root, flight)
        requests = [item for item in records
                    if item.get("kind") == "wake_request"
                    and item.get("state") == "pending"]
        due = [item for item in requests
               if (_parse_iso(item.get("wake_at")) or 0) <= moment]
        due.sort(key=lambda item: (str(item.get("wake_at")),
                                   str(item.get("created_at"))))
        if not due:
            _refuse("no wake request is due", code="wake_not_due")
        request = due[0]
        target = str(request.get("target") or cfg.agent_id)
        mode = str(request.get("mode") or "")
        if mode not in _target_wake_modes(root, cfg, flight, target):
            _refuse("the target member does not declare this wake mode",
                    code="wake_mode_not_declared")
        candidates = [item for item in records
                      if item.get("kind") == "wake_approval"
                      and item.get("target") == target
                      and item.get("mode") == mode]
        active = [item for item in candidates
                  if _approval_state(item, moment) == "active"
                  and int(item.get("used_turns") or 0) < int(
                      item.get("budget_turns") or 0)]
        if not active:
            if not candidates and any(
                    item.get("kind") == "wake_approval"
                    and item.get("target") == target
                    for item in records):
                _refuse("a host approval exists for a different wake mode",
                        code="wake_mode_not_declared")
            if any(_approval_state(item, moment) == "expired"
                   for item in candidates):
                _refuse("the wake approval expired", code="wake_expired")
            if any(_approval_state(item, moment) == "active"
                   for item in candidates):
                _refuse("the wake budget is exhausted",
                        code="wake_budget_exhausted")
            _refuse("no host approval covers this wake request",
                    code="wake_not_approved")
        approval = sorted(active, key=lambda item: str(
            item.get("expires_at")))[0]
        approval["used_turns"] = int(approval.get("used_turns") or 0) + 1
        log = list(approval.get("claim_log") or ())
        log.append({"request_id": request.get("request_id"),
                    "at": _iso(moment)})
        approval["claim_log"] = log[-MAX_WAKE_AUDIT:]
        _write_record(
            _wake_path(root, flight, "approval",
                       str(approval.get("approval_id"))), approval)
        request["state"] = "claimed"
        request["claimed_at"] = _iso(moment)
        request["approval_id"] = approval.get("approval_id")
        _write_record(
            _wake_path(root, flight, "request",
                       str(request.get("request_id"))), request)
    return {"schema": SCHEMA, "conformance": CONFORMANCE, "flight_id": flight,
            "claimed": True, "request": _request_view(request),
            "approval": _approval_view(approval, now=moment),
            "claimable": True, "spawnable": False,
            "reason": "a host runner may act on the request id; the bridge "
                      "never spawns a process or model turn",
            "network": False}


def _record_wake_request(root: str | Path, config: O.OzmaMeshConfig,
                         envelope: Mapping[str, Any], *, now: float
                         ) -> dict[str, Any]:
    requested = envelope.get("wake")
    mode = "message-triggered" if requested == "message" else requested
    if mode not in WAKE_APPROVAL_MODES:
        return {"recorded": False, "reason": "wake_mode_invalid"}
    flight = flight_id(root, config)
    target = envelope.get("to") or config.agent_id
    if not isinstance(target, str) or not O.AGENT_RE.fullmatch(target):
        return {"recorded": False, "reason": "wake_identity_required"}
    if mode not in _target_wake_modes(root, config, flight, target):
        return {"recorded": False, "reason": "wake_mode_not_declared"}
    request_id = _derived_id("wkr", flight, str(envelope["id"]))
    path = _wake_path(root, flight, "request", request_id)
    with _flight_lock(root, flight):
        if _is_file(path):
            return {"recorded": True, "duplicate": True,
                    "request_id": request_id, "mode": mode}
        records, _ = _wake_records(root, flight)
        requests = [item for item in records
                    if item.get("kind") == "wake_request"]
        if len(requests) >= MAX_WAKE_REQUESTS:
            claimed = [item for item in requests
                       if item.get("state") == "claimed"]
            claimed.sort(key=lambda item: str(item.get("claimed_at") or ""))
            room = len(requests) - MAX_WAKE_REQUESTS + 1
            for item in claimed[:room]:
                try:
                    os.unlink(_wake_path(root, flight, "request",
                                         str(item.get("request_id"))))
                except OSError:
                    pass
            records, _ = _wake_records(root, flight)
            requests = [item for item in records
                        if item.get("kind") == "wake_request"]
            if len(requests) >= MAX_WAKE_REQUESTS:
                return {"recorded": False, "reason": "wake_store_full"}
        record = {
            "schema": SCHEMA,
            "kind": "wake_request",
            "flight_id": flight,
            "request_id": request_id,
            "message_id": envelope.get("id"),
            "from": envelope.get("from"),
            "target": target,
            "mode": mode,
            "wake_at": envelope.get("wake_at") or _iso(now),
            "state": "pending",
            "claimed_at": None,
            "approval_id": None,
            "created_at": _iso(now),
        }
        _write_record(path, record)
    return {"recorded": True, "duplicate": False, "request_id": request_id,
            "mode": mode, "wake_at": record["wake_at"]}


def read_wake_status(root: str | Path,
                     config: O.OzmaMeshConfig | None = None,
                     now: float | None = None) -> dict[str, Any]:
    """Wake counts for ``status``/``check``; read-only and no bodies."""
    cfg = config if config is not None else O.load_config(root)
    report = wake_list(root, config=cfg, now=now)
    approvals = report.get("approvals") or []
    requests = report.get("requests") or []
    moment = time.time() if now is None else float(now)
    active = [item for item in approvals if item.get("state") == "active"]
    by_mode: dict[str, int] = {mode: 0 for mode in WAKE_APPROVAL_MODES}
    budget = 0
    used = 0
    for item in active:
        mode = str(item.get("mode"))
        by_mode[mode] = by_mode.get(mode, 0) + 1
        budget += int(item.get("budget_turns") or 0)
        used += int(item.get("used_turns") or 0)
    pending = sum(1 for item in requests
                  if item.get("state") == "pending")
    due = sum(1 for item in requests
              if item.get("state") == "pending"
              and (_parse_iso(item.get("wake_at")) or 0) <= moment)
    return {
        "schema": SCHEMA,
        "conformance": CONFORMANCE,
        "store_present": (bridge_root(root) / WAKE_DIR).is_dir(),
        "approvals": {"active": len(active), "by_mode": by_mode,
                      "expired": int(report["counts"].get("expired") or 0),
                      "revoked": int(report["counts"].get("revoked") or 0)},
        "approved": len(active),
        "budget_turns": budget,
        "used_turns": used,
        "pending_requests": pending,
        "due_requests": due,
        "claimable": True,
        "spawnable": False,
        "reason": "host approval is required; a claim never spawns a process "
                  "or model turn",
        "counts": {
            "pending_requests": pending,
            "due_requests": due,
            "claimed_requests": int(
                report["counts"].get("claimed") or 0),
            "active_approvals": len(active),
        },
        "errors": list(report.get("errors") or ()),
        "network": False,
    }


# --------------------------------------------------------------------------- #
# shim seam and sync
# --------------------------------------------------------------------------- #
class Shim(Protocol):
    """The tiny transport seam.  Implementations stay outside this module."""

    def deliver(self, envelope: Mapping[str, Any]) -> Mapping[str, Any]:
        ...

    def poll(self, after: str | None, limit: int
             ) -> Sequence[Mapping[str, Any]]:
        ...


class UnavailableShim:
    """Production/CLI shim: refuses and never opens a socket."""

    def deliver(self, envelope: Mapping[str, Any]) -> Mapping[str, Any]:
        _refuse("no approved Ozma shim is configured; nothing was sent",
                code="shim_unavailable")

    def poll(self, after: str | None, limit: int
             ) -> Sequence[Mapping[str, Any]]:
        _refuse("no approved Ozma shim is configured; nothing was polled",
                code="shim_unavailable")


def sync(root: str | Path, shim: Shim, *,
         config: O.OzmaMeshConfig | None = None,
         after: str | None = None, limit: int = DEFAULT_POLL_LIMIT,
         now: float | None = None) -> dict[str, Any]:
    """Deliver pending rows, then ingest a bounded shim poll with dedupe."""
    cfg = config if config is not None else O.load_config(root)
    if cfg.effective_mode not in {"messages", "full"}:
        _refuse("sync needs bridge mode messages or full",
                code="bridge_not_enabled")
    if isinstance(limit, bool) or not isinstance(limit, int) or not (
            1 <= limit <= MAX_POLL_LIMIT):
        _refuse(f"limit must be 1..{MAX_POLL_LIMIT}", code="invalid_value")
    moment = time.time() if now is None else float(now)
    flight = flight_id(root, cfg)
    index = _load_dedupe(root, flight)
    cursor = after if after is not None else index.get("cursor")
    delivery = deliver_pending(root, shim, config=cfg, now=moment)
    polled = list(shim.poll(cursor, limit))
    accepted = replay = conflicts = rejected = 0
    results = []
    for envelope in polled[:limit]:
        if not isinstance(envelope, Mapping):
            rejected += 1
            results.append({"accepted": False, "code": "invalid_envelope"})
            continue
        try:
            outcome = ingest(root, envelope, config=cfg, now=moment)
        except M.MeshRefusal as exc:
            if exc.code == "cross_flight":
                rejected += 1
                results.append({"accepted": False, "code": exc.code})
                continue
            raise
        results.append(outcome)
        if outcome.get("accepted") and outcome.get("duplicate"):
            replay += 1
        elif outcome.get("accepted"):
            accepted += 1
        elif outcome.get("code") == "id_conflict":
            conflicts += 1
        else:
            rejected += 1
        if outcome.get("id"):
            cursor = str(outcome["id"])
    with _flight_lock(root, flight):
        index = _load_dedupe(root, flight)
        index["cursor"] = cursor
        index["updated_at"] = _iso(moment)
        _write_record(_dedupe_path(root, flight), index)
    return {"schema": SCHEMA, "flight_id": flight, "conformance": CONFORMANCE,
            "delivered": delivery["delivered"], "delivery": delivery,
            "polled": len(polled),
            "accepted": accepted, "replay": replay, "conflicts": conflicts,
            "rejected": rejected, "results": results, "cursor": cursor,
            "network": False}


# --------------------------------------------------------------------------- #
# read-only status
# --------------------------------------------------------------------------- #
def read_status(root: str | Path, config: O.OzmaMeshConfig | None = None
                ) -> dict[str, Any]:
    """Bridge counts for ``status``/``check``; creates nothing, no bodies."""
    cfg = config if config is not None else O.load_config(root)
    flight = flight_id(root, cfg)
    root_path = bridge_root(root)
    present = _is_dir(root_path)
    outbox_records, outbox_errors = _records(root, OUTBOX_DIR, flight)
    inbox_records, inbox_errors = _records(root, INBOX_DIR, flight)
    claim_report = claims(root, config=cfg)
    handoff_report = handoffs(root, config=cfg, limit=0)
    blocker_report = blockers(root, config=cfg)
    status_now = time.time()
    wake_report = read_wake_status(root, config=cfg, now=status_now)
    outbox_counts = {state: 0 for state in OUTBOX_STATES}
    for record in outbox_records:
        state = str(record.get("state") or "pending")
        due = _parse_iso(record.get("expires_at"))
        if state in {"pending", "failed"} and due is not None and due <= status_now:
            state = "expired"
        outbox_counts[state] = outbox_counts.get(state, 0) + 1
    inbox_counts = {state: 0 for state in INBOX_STATES}
    for record in inbox_records:
        inbox_counts[str(record.get("state") or "unread")] = (
            inbox_counts.get(str(record.get("state") or "unread"), 0) + 1)
    index = _load_dedupe(root, flight) if present else {}
    outbox_bytes = _store_bytes(root, OUTBOX_DIR, flight)
    inbox_bytes = _store_bytes(root, INBOX_DIR, flight)
    claim_counts = dict(claim_report["counts"])
    for row in claim_report["claims"]:
        prefix = "remote" if row["direction"] == "inbound" else "outbound"
        claim_counts[f"{prefix}_{row['state']}"] = (
            claim_counts.get(f"{prefix}_{row['state']}", 0) + 1)
    return {
        "store_present": present,
        "conformance": CONFORMANCE,
        "conformance_note": CONFORMANCE_NOTE,
        "network": False,
        "outbox": outbox_counts,
        "inbox": inbox_counts,
        "claims": claim_counts,
        "conflicts": claim_report["conflict_count"],
        "handoffs": {
            "counts": dict(handoff_report["counts"]),
            "total": handoff_report["count"],
        },
        "blockers": {
            "open": int(blocker_report["counts"].get("open") or 0),
            "cleared": int(blocker_report["counts"].get("cleared") or 0),
            "advisory": True,
            "blocking": False,
        },
        "wake": {
            "approved": wake_report["approved"],
            "budget_turns": wake_report["budget_turns"],
            "used_turns": wake_report["used_turns"],
            "pending_requests": wake_report["pending_requests"],
            "claimable": True,
            "spawnable": False,
        },
        "queue": {
            "messages": len(outbox_records) + len(inbox_records),
            "bytes": outbox_bytes + inbox_bytes,
            "max_messages": cfg.queue_max_messages,
            "max_bytes": cfg.queue_max_bytes,
        },
        "dedupe": {
            "entries": len(index.get("entries") or {}),
            "conflicts": int(index.get("conflicts") or 0),
        },
        "errors": len(outbox_errors) + len(inbox_errors)
                  + len(claim_report.get("errors") or ())
                  + len(handoff_report.get("errors") or ())
                  + len(blocker_report.get("errors") or ())
                  + len(wake_report.get("errors") or ()),
    }


__all__ = [
    "A2A_EXTRA_LOSS_FIELDS",
    "A2A_REQUIRED_UNREPRESENTABLE",
    "A2A_VERSION",
    "BLOCKER_STATES",
    "BRIDGE_NAME",
    "CLAIM_MODES",
    "CLAIM_SCOPES",
    "CONFORMANCE",
    "DIRECTIONS",
    "GRANT_MAPPING",
    "HANDOFF_STATES",
    "INBOX_STATES",
    "LOCAL_TO_OZMA",
    "OZMA_TO_LOCAL",
    "OZMA_TYPES",
    "OUTBOX_STATES",
    "OzmaBridgeError",
    "OzmaBridgeIOError",
    "OzmaBridgeRefusal",
    "OzmaBridgeSchemaError",
    "SCHEMA",
    "Shim",
    "UnavailableShim",
    "WAKE_APPROVAL_MODES",
    "WAKE_REQUEST_MODES",
    "ack_message",
    "add_blocker",
    "agent_role_workunit",
    "blockers",
    "bridge_root",
    "build_envelope",
    "claim_scope_for",
    "claims",
    "clear_blockers",
    "dedupe_key",
    "deliver_pending",
    "enqueue",
    "enqueue_claim",
    "enqueue_local_message",
    "envelope_digest",
    "flight",
    "flight_id",
    "from_a2a",
    "handoff_respond",
    "handoff_show",
    "handoffs",
    "inbox",
    "ingest",
    "mapping_table",
    "message_gate",
    "outbox",
    "read_message",
    "read_status",
    "read_wake_status",
    "record_key",
    "record_path",
    "store_dir",
    "subscription_accepts",
    "sync",
    "to_a2a",
    "translate_local",
    "translate_ozma",
    "validate_envelope",
    "wake_approve",
    "wake_claim",
    "wake_list",
    "wake_revoke",
]
