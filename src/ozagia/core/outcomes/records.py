"""Canonical envelopes, payload schemas and lifecycle vocabulary.

The outcome journal is an append-only sequence of canonical JSON records.  A
record has a versioned envelope (``ozagia.outcomes.record.v1``) and a payload
whose own ``schema`` field identifies the logical type.  This module owns the
envelope, the immutable intent revision hash, the bounded-value helpers and the
state-transition vocabulary.  It performs no I/O and reads no clock.

Hash chain
----------
``record_hash`` is ``content_hash`` of every envelope field except
``record_hash`` itself.  ``prev_hash`` is the previous record's
``record_hash`` (``None`` for sequence 1).  The chain therefore binds sequence,
payload, actor, timestamp, correlation identifiers and the idempotency key.
Replay rejects a broken chain or a damaged record.

Intent revision hash
--------------------
An intent revision's ``revision_hash`` is the canonical content hash of its
semantic fields (requested outcome, criteria, context, project, interpretation,
principal, conversation, parent and reason).  It excludes the journal record
id and timestamp, so a retried request keeps the same revision identity.  A
steering revision names its parent by hash, never by mutating an earlier
record.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from ozagia.core import contracts as C

from .errors import (
    OutcomeBoundsError,
    OutcomeIntegrityError,
    OutcomeUnsupportedError,
    OutcomeValidationError,
)

# ---------------------------------------------------------------------------
# Versioned schemas and event vocabulary
# ---------------------------------------------------------------------------

RECORD_SCHEMA = "ozagia.outcomes.record.v1"
REQUEST_SCHEMA = "ozagia.outcomes.request.v1"

REVISION_SCHEMA = "ozagia.outcomes.intent_revision.v1"
INTENT_TRANSITION_SCHEMA = "ozagia.outcomes.intent_transition.v1"
WORK_SCHEMA = "ozagia.outcomes.work.v1"
WORK_TRANSITION_SCHEMA = "ozagia.outcomes.work_transition.v1"
ATTEMPT_SCHEMA = "ozagia.outcomes.attempt.v1"
EVENT_SCHEMA = "ozagia.outcomes.event.v1"
GATE_SCHEMA = "ozagia.outcomes.gate.v1"
DECISION_SCHEMA = "ozagia.outcomes.decision.v1"
EVIDENCE_SCHEMA = "ozagia.outcomes.evidence_reference.v1"
EFFECT_SCHEMA = "ozagia.outcomes.effect_receipt.v1"
OUTCOME_SCHEMA = "ozagia.outcomes.outcome.v1"

INTENT_SUBMITTED = "intent.submitted"
INTENT_REVISED = "intent.revised"
INTENT_TRANSITIONED = "intent.transitioned"
WORK_ADDED = "work.added"
WORK_TRANSITIONED = "work.transitioned"
WORK_STALE = "work.stale"
ATTEMPT_QUEUED = "attempt.queued"
ATTEMPT_STARTED = "attempt.started"
ATTEMPT_FINISHED = "attempt.finished"
EVENT_APPENDED = "event.appended"
GATE_CREATED = "gate.created"
DECISION_RECORDED = "decision.recorded"
EVIDENCE_RECORDED = "evidence.recorded"
EFFECT_RECORDED = "effect.recorded"
OUTCOME_RECORDED = "outcome.recorded"

EVENT_TYPES = frozenset({
    INTENT_SUBMITTED, INTENT_REVISED, INTENT_TRANSITIONED,
    WORK_ADDED, WORK_TRANSITIONED, WORK_STALE,
    ATTEMPT_QUEUED, ATTEMPT_STARTED, ATTEMPT_FINISHED,
    EVENT_APPENDED, GATE_CREATED, DECISION_RECORDED, EVIDENCE_RECORDED,
    EFFECT_RECORDED, OUTCOME_RECORDED,
})

PAYLOAD_SCHEMAS = {
    INTENT_SUBMITTED: REVISION_SCHEMA,
    INTENT_REVISED: REVISION_SCHEMA,
    INTENT_TRANSITIONED: INTENT_TRANSITION_SCHEMA,
    WORK_ADDED: WORK_SCHEMA,
    WORK_TRANSITIONED: WORK_TRANSITION_SCHEMA,
    WORK_STALE: WORK_TRANSITION_SCHEMA,
    ATTEMPT_QUEUED: ATTEMPT_SCHEMA,
    ATTEMPT_STARTED: ATTEMPT_SCHEMA,
    ATTEMPT_FINISHED: ATTEMPT_SCHEMA,
    EVENT_APPENDED: EVENT_SCHEMA,
    GATE_CREATED: GATE_SCHEMA,
    DECISION_RECORDED: DECISION_SCHEMA,
    EVIDENCE_RECORDED: EVIDENCE_SCHEMA,
    EFFECT_RECORDED: EFFECT_SCHEMA,
    OUTCOME_RECORDED: OUTCOME_SCHEMA,
}

INTENT_STATES = (
    "accepted", "running", "waiting_decision", "satisfied", "partial",
    "failed", "cancelled",
)
ACTIVE_INTENT_STATES = frozenset({"accepted", "running", "waiting_decision"})
TERMINAL_INTENT_STATES = frozenset({"satisfied", "partial", "failed", "cancelled"})

INTENT_TRANSITIONS: dict[str, frozenset[str]] = {
    "accepted": frozenset({
        "running", "waiting_decision", "satisfied", "partial", "failed",
        "cancelled",
    }),
    "running": frozenset({
        "waiting_decision", "satisfied", "partial", "failed", "cancelled",
    }),
    "waiting_decision": frozenset({
        "running", "satisfied", "partial", "failed", "cancelled",
    }),
    "satisfied": frozenset(),
    "partial": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
}

WORK_STATES = (
    "queued", "running", "waiting", "completed", "failed", "cancelled",
    "stale",
)
WORK_TERMINAL_STATES = frozenset({
    "completed", "failed", "cancelled", "stale",
})
WORK_TRANSITIONS: dict[str, frozenset[str]] = {
    "queued": frozenset({"running", "stale", "failed", "cancelled"}),
    "running": frozenset({"waiting", "completed", "failed", "cancelled"}),
    "waiting": frozenset({"running", "completed", "failed", "cancelled"}),
    "completed": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
    "stale": frozenset(),
}

ATTEMPT_STATES = ("queued", "running", "terminal")
ATTEMPT_OUTCOMES = ("succeeded", "failed", "cancelled", "uncertain")
ATTEMPT_WORK_STATE = {
    "succeeded": "completed",
    "failed": "failed",
    "cancelled": "cancelled",
    "uncertain": "waiting",
}

DECISIONS = ("approved", "rejected", "expired", "cancelled")
OUTCOME_STATUSES = ("satisfied", "partial", "failed", "cancelled")
EFFECT_STATES = ("intended", "applied", "uncertain", "reconciled", "failed")
EVIDENCE_FRESHNESS = frozenset({"current", "stale", "unknown", "absent"})

# ---------------------------------------------------------------------------
# Bounds
# ---------------------------------------------------------------------------

MAX_TEXT = 4000
MAX_SHORT = 300
MAX_DETAIL = 2000
MAX_ID = 128
MAX_ACTOR = 200
MAX_KEY = 192
MAX_REF = 512
MAX_EVENT = 64
MAX_CRITERIA = 32
MAX_REFS = 32
MAX_EVIDENCE_REFS = 64
MAX_LIST = 64
MAX_PAYLOAD_BYTES = 16 * 1024
MAX_RECORD_BYTES = 32 * 1024
MAX_PAYLOAD_DEPTH = 8
MAX_PAYLOAD_NODES = 512
MAX_TIMESTAMP = C.INT64_MAX

ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
EVENT_RE = re.compile(r"[a-z][a-z0-9_.-]{0,63}\Z")
KEY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,191}\Z")
HASH_RE = re.compile(r"[0-9a-f]{64}\Z")
RECORD_ID_RE = re.compile(r"or-[0-9]{8,}-[0-9a-f]{12}\Z")

RECORD_FIELDS = frozenset({
    "schema", "seq", "record_id", "event", "intent_id", "actor",
    "timestamp", "correlation_id", "causation_id", "payload",
    "idempotency_key", "prev_hash", "record_hash",
})


# ---------------------------------------------------------------------------
# Bounded value helpers
# ---------------------------------------------------------------------------


def _error(name: str, message: str) -> OutcomeValidationError:
    return OutcomeValidationError(f"{name}: {message}")


def text(value: Any, name: str, *, limit: int = MAX_TEXT,
         required: bool = True) -> str | None:
    """A bounded, control-free, single-line text value."""
    if value is None:
        if required:
            raise _error(name, "is required")
        return None
    if not isinstance(value, str):
        raise _error(name, f"must be text, got {type(value).__name__}")
    normalized = " ".join(value.split())
    if not normalized:
        if required:
            raise _error(name, "must not be empty")
        return None
    if len(normalized) > limit:
        raise OutcomeBoundsError(f"{name}: exceeds {limit} characters")
    return normalized


def detail_text(value: Any, name: str, *,
                limit: int = MAX_DETAIL) -> str | None:
    return text(value, name, limit=limit, required=False)


def identifier(value: Any, name: str) -> str:
    """A stable entity or record identifier."""
    if not isinstance(value, str) or not value:
        raise _error(name, "must be a non-empty identifier")
    if value != value.strip() or not ID_RE.fullmatch(value):
        raise _error(name, "must match [A-Za-z0-9][A-Za-z0-9_.:-]*")
    if len(value) > MAX_ID:
        raise OutcomeBoundsError(f"{name}: exceeds {MAX_ID} characters")
    return value


def ref(value: Any, name: str, *, required: bool = True) -> str | None:
    """A bounded opaque reference (path, digest, artifact or record id)."""
    return text(value, name, limit=MAX_REF, required=required)


def event_name(value: Any, name: str = "event") -> str:
    if not isinstance(value, str) or not EVENT_RE.fullmatch(value):
        raise _error(name, "must be lowercase letters, digits, '_', '.', '-'")
    if len(value) > MAX_EVENT:
        raise OutcomeBoundsError(f"{name}: exceeds {MAX_EVENT} characters")
    return value


def idempotency_key(value: Any) -> str:
    if not isinstance(value, str) or not KEY_RE.fullmatch(value):
        raise _error(
            "idempotency_key",
            "must match [A-Za-z0-9][A-Za-z0-9_.:-]{0,191}")
    return value


def timestamp(value: Any, name: str = "timestamp") -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _error(name, "must be an integer millisecond timestamp")
    if value < 0 or value > MAX_TIMESTAMP:
        raise OutcomeBoundsError(f"{name}: out of range")
    return value


def boolean(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise _error(name, "must be true or false")
    return value


def integer(value: Any, name: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _error(name, "must be an integer")
    if value < minimum:
        raise _error(name, f"must be at least {minimum}")
    if value > C.INT64_MAX:
        raise OutcomeBoundsError(f"{name}: out of range")
    return value


def lease_generation(value: Any, name: str = "lease_generation") -> int | str:
    """A lease generation token; an opaque string is allowed by design."""
    if isinstance(value, bool):
        raise _error(name, "must not be a bool")
    if isinstance(value, int):
        if value < 0 or value > C.INT64_MAX:
            raise OutcomeBoundsError(f"{name}: out of range")
        return value
    if isinstance(value, str):
        token = text(value, name, limit=MAX_ID)
        assert token is not None
        return token
    raise _error(name, "must be an integer or an opaque string")


def ref_list(value: Any, name: str = "refs", *,
             limit: int = MAX_REFS) -> list[str]:
    items = _sequence(value, name, limit=limit)
    result: list[str] = []
    for index, item in enumerate(items):
        item_ref = ref(item, f"{name}[{index}]")
        assert item_ref is not None
        if item_ref not in result:
            result.append(item_ref)
    return result


def criteria_list(value: Any, name: str = "criteria") -> list[str]:
    items = _sequence(value, name, limit=MAX_CRITERIA)
    result: list[str] = []
    for index, item in enumerate(items):
        criterion = text(item, f"{name}[{index}]", limit=MAX_SHORT)
        assert criterion is not None
        if criterion in result:
            raise _error(f"{name}[{index}]", "duplicates an earlier criterion")
        result.append(criterion)
    return result


def _sequence(value: Any, name: str, *, limit: int) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (str, Mapping, set, frozenset)):
        raise _error(
            name, "must be an ordered sequence, not a string, mapping or set")
    try:
        items = list(value)
    except TypeError as exc:
        raise _error(name, "must be a sequence") from exc
    if len(items) > limit:
        raise OutcomeBoundsError(f"{name}: exceeds {limit} items")
    return items


def canonical_payload(value: Any, name: str = "payload") -> dict[str, Any]:
    """Validate and return a bounded canonical JSON mapping."""
    if not isinstance(value, Mapping):
        raise _error(name, f"must be a mapping, got {type(value).__name__}")
    payload = dict(value)
    try:
        encoded = C.canonical_json(payload)
    except C.ContractError as exc:
        raise _error(name, f"is not canonical JSON: {exc}") from exc
    if len(encoded.encode("utf-8")) > MAX_PAYLOAD_BYTES:
        raise OutcomeBoundsError(
            f"{name}: exceeds {MAX_PAYLOAD_BYTES} canonical bytes")
    _walk_bounds(payload, name=name, depth=0, nodes=[0])
    return payload


def _walk_bounds(value: Any, *, name: str, depth: int,
                 nodes: list[int]) -> None:
    nodes[0] += 1
    if nodes[0] > MAX_PAYLOAD_NODES:
        raise OutcomeBoundsError(f"{name}: exceeds {MAX_PAYLOAD_NODES} nodes")
    if depth > MAX_PAYLOAD_DEPTH:
        raise OutcomeBoundsError(f"{name}: exceeds depth {MAX_PAYLOAD_DEPTH}")
    if isinstance(value, dict):
        for item in value.values():
            _walk_bounds(item, name=name, depth=depth + 1, nodes=nodes)
    elif isinstance(value, list):
        for item in value:
            _walk_bounds(item, name=name, depth=depth + 1, nodes=nodes)


def request_hash(event: str, intent_id: str,
                 request: Mapping[str, Any]) -> str:
    """Stable idempotency fingerprint for a caller request.

    The fingerprint covers the event, intent and caller-supplied request
    fields.  It deliberately excludes generated record metadata (sequence,
    record id, timestamp, resulting revision number and record hash), so a
    retry of the same request is recognisable as a duplicate.
    """
    return C.content_hash({
        "schema": REQUEST_SCHEMA,
        "event": event,
        "intent_id": intent_id,
        "request": dict(request),
    })


def _payload_schema(payload: Mapping[str, Any], expected: str) -> dict[str, Any]:
    actual = payload.get("schema")
    if actual != expected:
        if isinstance(actual, str) and actual.startswith("ozagia.outcomes."):
            raise OutcomeUnsupportedError(
                f"payload schema {actual!r} does not match {expected!r}")
        raise OutcomeIntegrityError(
            f"payload schema must be {expected!r}, got {actual!r}")
    return dict(payload)


def payload_with_schema(payload: Mapping[str, Any], schema: str) -> dict[str, Any]:
    result = dict(payload)
    result["schema"] = schema
    return result


def require_request_hash(payload: Mapping[str, Any]) -> str | None:
    value = payload.get("request_hash")
    if value is None:
        return None
    if not isinstance(value, str) or not HASH_RE.fullmatch(value):
        raise OutcomeValidationError("payload request_hash is malformed")
    return value


# ---------------------------------------------------------------------------
# Immutable intent revisions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IntentRevision:
    """One immutable intent version."""

    intent_id: str
    revision: int
    parent: str | None
    revision_hash: str
    requested_outcome: str
    criteria: tuple[str, ...]
    context_refs: tuple[str, ...]
    project_ref: str | None
    interpretation: str | None
    principal: str
    conversation_ref: str | None
    reason: str
    record_id: str
    created_at: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent_id": self.intent_id,
            "revision": self.revision,
            "parent": self.parent,
            "revision_hash": self.revision_hash,
            "requested_outcome": self.requested_outcome,
            "criteria": list(self.criteria),
            "context_refs": list(self.context_refs),
            "project_ref": self.project_ref,
            "interpretation": self.interpretation,
            "principal": self.principal,
            "conversation_ref": self.conversation_ref,
            "reason": self.reason,
            "record_id": self.record_id,
            "created_at": self.created_at,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any], *,
                     record_id: str, created_at: int) -> IntentRevision:
        data = _payload_schema(payload, REVISION_SCHEMA)
        intent_id = identifier(data.get("intent_id"), "intent_id")
        revision = integer(data.get("revision"), "revision", minimum=1)
        parent = data.get("parent")
        if parent is not None:
            parent = _hash(parent, "parent")
        requested = text(data.get("requested_outcome"), "requested_outcome")
        assert requested is not None
        criteria = tuple(criteria_list(data.get("criteria"), "criteria"))
        contexts = tuple(ref_list(data.get("context_refs"), "context_refs"))
        project = ref(data.get("project_ref"), "project_ref", required=False)
        interpretation = detail_text(
            data.get("interpretation"), "interpretation")
        principal = text(data.get("principal"), "principal", limit=MAX_ACTOR)
        assert principal is not None
        conversation = ref(
            data.get("conversation_ref"), "conversation_ref", required=False)
        reason = text(data.get("reason"), "reason", limit=MAX_SHORT)
        assert reason is not None
        expected = revision_hash(
            intent_id=intent_id, revision=revision, parent=parent,
            requested_outcome=requested, criteria=criteria,
            context_refs=contexts, project_ref=project,
            interpretation=interpretation, principal=principal,
            conversation_ref=conversation, reason=reason)
        stored = _hash(data.get("revision_hash"), "revision_hash")
        if stored != expected:
            raise OutcomeIntegrityError(
                f"intent revision {revision} hash does not match its content")
        return cls(
            intent_id=intent_id, revision=revision, parent=parent,
            revision_hash=stored, requested_outcome=requested,
            criteria=criteria, context_refs=contexts, project_ref=project,
            interpretation=interpretation, principal=principal,
            conversation_ref=conversation, reason=reason,
            record_id=record_id, created_at=created_at)


def revision_hash(*, intent_id: str, revision: int, parent: str | None,
                  requested_outcome: str, criteria: Iterable[str],
                  context_refs: Iterable[str], project_ref: str | None,
                  interpretation: str | None, principal: str,
                  conversation_ref: str | None, reason: str) -> str:
    """The canonical identity of one intent revision."""
    return C.content_hash({
        "schema": REVISION_SCHEMA,
        "intent_id": intent_id,
        "revision": revision,
        "parent": parent,
        "requested_outcome": requested_outcome,
        "criteria": list(criteria),
        "context_refs": list(context_refs),
        "project_ref": project_ref,
        "interpretation": interpretation,
        "principal": principal,
        "conversation_ref": conversation_ref,
        "reason": reason,
    })


def build_revision_payload(
        *, intent_id: str, revision: int, parent: str | None,
        requested_outcome: Any, criteria: Any, context_refs: Any,
        project_ref: Any, interpretation: Any, principal: Any,
        conversation_ref: Any, reason: Any,
        request_fingerprint: str) -> dict[str, Any]:
    """Validate and build one revision payload (including its hash)."""
    intent = identifier(intent_id, "intent_id")
    number = integer(revision, "revision", minimum=1)
    parent_hash = _hash(parent, "parent") if parent is not None else None
    requested = text(requested_outcome, "requested_outcome")
    assert requested is not None
    criterion_values = criteria_list(criteria, "criteria")
    context_values = ref_list(context_refs, "context_refs")
    project = ref(project_ref, "project_ref", required=False)
    interpretation_value = detail_text(
        interpretation, "interpretation")
    principal_value = text(principal, "principal", limit=MAX_ACTOR)
    assert principal_value is not None
    conversation = ref(
        conversation_ref, "conversation_ref", required=False)
    reason_value = text(reason, "reason", limit=MAX_SHORT)
    assert reason_value is not None
    digest = revision_hash(
        intent_id=intent, revision=number, parent=parent_hash,
        requested_outcome=requested, criteria=criterion_values,
        context_refs=context_values, project_ref=project,
        interpretation=interpretation_value, principal=principal_value,
        conversation_ref=conversation, reason=reason_value)
    return {
        "schema": REVISION_SCHEMA,
        "intent_id": intent,
        "revision": number,
        "parent": parent_hash,
        "revision_hash": digest,
        "requested_outcome": requested,
        "criteria": criterion_values,
        "context_refs": context_values,
        "project_ref": project,
        "interpretation": interpretation_value,
        "principal": principal_value,
        "conversation_ref": conversation,
        "reason": reason_value,
        "request_hash": request_fingerprint,
    }


# ---------------------------------------------------------------------------
# Journal envelope
# ---------------------------------------------------------------------------


def _hash(value: Any, name: str) -> str:
    if not isinstance(value, str) or not HASH_RE.fullmatch(value):
        raise OutcomeValidationError(
            f"{name} must be a lowercase 64-character SHA-256 hash")
    return value


def record_hash(record: Mapping[str, Any]) -> str:
    """Hash every envelope field except ``record_hash`` itself."""
    return C.content_hash({
        key: value for key, value in record.items() if key != "record_hash"
    })


@dataclass(frozen=True)
class OutcomeRecord:
    """One validated journal envelope."""

    schema: str
    seq: int
    record_id: str
    event: str
    intent_id: str
    actor: str
    timestamp: int
    correlation_id: str | None
    causation_id: str | None
    payload: Mapping[str, Any]
    idempotency_key: str
    prev_hash: str | None
    record_hash: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "seq": self.seq,
            "record_id": self.record_id,
            "event": self.event,
            "intent_id": self.intent_id,
            "actor": self.actor,
            "timestamp": self.timestamp,
            "correlation_id": self.correlation_id,
            "causation_id": self.causation_id,
            "payload": dict(self.payload),
            "idempotency_key": self.idempotency_key,
            "prev_hash": self.prev_hash,
            "record_hash": self.record_hash,
        }

    @property
    def computed_hash(self) -> str:
        return record_hash(self.to_dict())

    @classmethod
    def from_dict(cls, raw: Any) -> OutcomeRecord:
        """Validate a journal mapping; malformed records fail closed."""
        try:
            return _validate_record(raw)
        except (OutcomeUnsupportedError, OutcomeIntegrityError):
            raise
        except Exception as exc:  # contract helpers raise many value errors
            raise OutcomeIntegrityError(f"invalid outcome record: {exc}") from exc


def make_record(*, seq: int, event: str, intent_id: str, actor: str,
                timestamp: int, payload: Mapping[str, Any],
                idempotency_key: str, prev_hash: str | None,
                correlation_id: str | None = None,
                causation_id: str | None = None) -> OutcomeRecord:
    """Build a canonically hashed envelope for an append."""
    sequence = integer(seq, "seq", minimum=1)
    event_value = event_name(event)
    if event_value not in EVENT_TYPES:
        raise OutcomeUnsupportedError(f"unknown outcome event {event_value!r}")
    intent = identifier(intent_id, "intent_id")
    actor_value = text(actor, "actor", limit=MAX_ACTOR)
    assert actor_value is not None
    at = timestamp_value_from(timestamp)
    payload_value = canonical_payload(payload)
    key = _valid_key(idempotency_key)
    previous = _hash(prev_hash, "prev_hash") if prev_hash is not None else None
    correlation = ref(
        correlation_id, "correlation_id", required=False)
    causation = ref(causation_id, "causation_id", required=False)
    base = {
        "schema": RECORD_SCHEMA,
        "seq": sequence,
        "event": event_value,
        "intent_id": intent,
        "actor": actor_value,
        "timestamp": at,
        "correlation_id": correlation,
        "causation_id": causation,
        "payload": payload_value,
        "idempotency_key": key,
        "prev_hash": previous,
    }
    digest = C.content_hash(base)
    record_id = f"or-{sequence:08d}-{digest[:12]}"
    full = dict(base)
    full["record_id"] = record_id
    full["record_hash"] = C.content_hash(full)
    return OutcomeRecord(
        schema=RECORD_SCHEMA, seq=sequence, record_id=record_id,
        event=event_value, intent_id=intent, actor=actor_value,
        timestamp=at, correlation_id=correlation, causation_id=causation,
        payload=payload_value, idempotency_key=key, prev_hash=previous,
        record_hash=full["record_hash"])


def timestamp_value_from(value: Any) -> int:
    return timestamp(value)


def _valid_key(value: Any) -> str:
    return idempotency_key(value)


def _validate_record(raw: Any) -> OutcomeRecord:
    if not isinstance(raw, Mapping):
        raise OutcomeIntegrityError(
            f"outcome record must be a mapping, got {type(raw).__name__}")
    keys = set(raw)
    unknown = keys - RECORD_FIELDS
    missing = RECORD_FIELDS - keys
    if unknown:
        raise OutcomeUnsupportedError(
            f"outcome record has unknown fields: {sorted(unknown)}")
    if missing:
        raise OutcomeIntegrityError(
            f"outcome record is missing fields: {sorted(missing)}")
    schema = raw.get("schema")
    if schema != RECORD_SCHEMA:
        if isinstance(schema, str) and schema.startswith("ozagia.outcomes."):
            raise OutcomeUnsupportedError(
                f"unsupported outcome record schema {schema!r}")
        raise OutcomeIntegrityError(
            f"outcome record schema must be {RECORD_SCHEMA!r}")
    seq = integer(raw.get("seq"), "seq", minimum=1)
    record_id = raw.get("record_id")
    if not isinstance(record_id, str) or not RECORD_ID_RE.fullmatch(record_id):
        raise OutcomeIntegrityError("outcome record_id is malformed")
    event = event_name(raw.get("event"))
    if event not in EVENT_TYPES:
        raise OutcomeUnsupportedError(f"unknown outcome event {event!r}")
    intent = identifier(raw.get("intent_id"), "intent_id")
    actor = text(raw.get("actor"), "actor", limit=MAX_ACTOR)
    assert actor is not None
    at = timestamp(raw.get("timestamp"))
    correlation = ref(
        raw.get("correlation_id"), "correlation_id", required=False)
    causation = ref(
        raw.get("causation_id"), "causation_id", required=False)
    payload = canonical_payload(raw.get("payload"))
    key = idempotency_key(raw.get("idempotency_key"))
    prev_hash = raw.get("prev_hash")
    previous = _hash(prev_hash, "prev_hash") if prev_hash is not None else None
    stored = _hash(raw.get("record_hash"), "record_hash")
    expected = record_hash(raw)
    if stored != expected:
        raise OutcomeIntegrityError(
            f"outcome record {record_id} hash does not match its content")
    return OutcomeRecord(
        schema=RECORD_SCHEMA, seq=seq, record_id=record_id, event=event,
        intent_id=intent, actor=actor, timestamp=at,
        correlation_id=correlation, causation_id=causation, payload=payload,
        idempotency_key=key, prev_hash=previous, record_hash=stored)
