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
    "created_at", "expires_at", "digest", "redactions",
})

#: The closed typed body shape for each Ozma type.  Values are validators.
BODY_FIELDS: dict[str, frozenset[str]] = {
    "request": frozenset({"purpose", "subject", "text"}),
    "response": frozenset({"accepted", "criteria", "subject", "text"}),
    "ack": frozenset({"accepted", "reason", "text"}),
    "status": frozenset({"subject", "text"}),
    "release": frozenset({"claim_id"}),
    "heads-up": frozenset({"subject", "text"}),
    "block": frozenset({"reason", "resource"}),
    "unblock": frozenset({"reason", "resource"}),
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
                                  if resource else {})}}
    elif kind == "unblock":
        why = _text(reason or text, "reason", MAX_REASON, allow_empty=True,
                    one_line=True)
        translated = {"type": "unblock",
                      "body": {**({"reason": why} if why else {}),
                               **({"resource": _text(resource, "resource",
                                                     MAX_RESOURCE,
                                                     one_line=True)}
                                  if resource else {})}}
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
            ("expires_at", 64, True)):
        if name in body:
            result[name] = _text(body[name], name, limit,
                                 one_line=one_line)
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
    return validate_envelope(envelope, direction=direction,
                             flight_id=flight_id)


# --------------------------------------------------------------------------- #
# paths, storage and locks
# --------------------------------------------------------------------------- #
def bridge_root(root: str | Path) -> Path:
    """``<state>/mesh/ozma`` (no directory is created here)."""
    return state_dir(root) / "mesh" / OZMA_DIRNAME


def store_dir(root: str | Path, kind: str, flight_id: str) -> Path:
    if kind not in {OUTBOX_DIR, INBOX_DIR, CLAIMS_DIR, DEDUPE_DIR}:
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
    if normalized["type"] == "claim":
        _record_inbound_claim(root, cfg, normalized, now=moment)
    elif normalized["type"] == "ack":
        reply = normalized.get("in_reply_to")
        if reply:
            _set_outbox_state(root, flight, str(reply), state="acked",
                              now=moment)
    elif normalized["type"] == "release":
        _release_claim(root, flight, normalized.get("in_reply_to"),
                       now=moment, from_agent=normalized.get("from"))
    return {"accepted": True, "duplicate": False, "id": normalized["id"],
            "state": record.get("state"), "redactions": labels,
            "evicted": evicted}


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
    record = _read_record(path)
    if record is None:
        _refuse("unknown Ozma inbox message", code="unknown_message")
    moment = time.time() if now is None else float(now)
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
                now: float | None = None) -> dict[str, Any]:
    """Mark one inbound message read and acked."""
    cfg = config if config is not None else O.load_config(root)
    flight = flight_id(root, cfg)
    path = record_path(root, INBOX_DIR, flight, message_id, "inbound")
    if not _is_file(path):
        _refuse("unknown Ozma inbox message", code="unknown_message")
    moment = time.time() if now is None else float(now)
    with _flight_lock(root, flight):
        record = _read_record(path)
        if record is None:
            _refuse("unknown Ozma inbox message", code="unknown_message")
        record["state"] = "acked"
        record["acked_at"] = _iso(moment)
        if not record.get("read_at"):
            record["read_at"] = _iso(moment)
        _write_record(path, record)
    return {"schema": SCHEMA, "flight_id": flight, "id": message_id,
            "state": "acked", "acked_at": record["acked_at"],
            "read_at": record["read_at"]}


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
    outbox_counts = {state: 0 for state in OUTBOX_STATES}
    status_now = time.time()
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
                  + len(claim_report.get("errors") or ()),
    }


__all__ = [
    "BRIDGE_NAME",
    "CLAIM_MODES",
    "CLAIM_SCOPES",
    "CONFORMANCE",
    "DIRECTIONS",
    "GRANT_MAPPING",
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
    "ack_message",
    "bridge_root",
    "build_envelope",
    "claim_scope_for",
    "claims",
    "dedupe_key",
    "deliver_pending",
    "enqueue",
    "enqueue_claim",
    "enqueue_local_message",
    "envelope_digest",
    "flight_id",
    "inbox",
    "ingest",
    "mapping_table",
    "message_gate",
    "outbox",
    "read_message",
    "read_status",
    "record_key",
    "record_path",
    "store_dir",
    "subscription_accepts",
    "sync",
    "translate_local",
    "translate_ozma",
    "validate_envelope",
]
