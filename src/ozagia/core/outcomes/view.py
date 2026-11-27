"""Versioned read-only outcome view (Stage 1, S1-C).

``view(intent_id)`` projects one materialized intent and
``list_view()`` projects bounded list rows.  Both return schema
``ozagia.outcomes.view.v1`` and are pure reads over
:meth:`OutcomeStore.inspect`, :meth:`OutcomeStore.follow` and
:meth:`OutcomeStore.list_active`.  Nothing here appends to the journal or
stores presentation state in it.

Contract
--------
A detail view (``kind == "intent"``) carries:

- ``intent`` / ``revision``: the materialized summary and current immutable
  revision;
- ``status``: the intent lifecycle state;
- ``outcome``: the latest outcome bound to the *current* revision, plus
  ``latest_outcome`` for an older revision when one exists;
- ``criteria``: one row per criterion with ``satisfied`` or ``pending``;
- ``evidence``: ``verified`` (recorded, current freshness, current revision)
  and ``pending`` (stale freshness, superseded revision, or referenced but
  not recorded) rows;
- ``current_work`` / ``current_attempt``: deterministic current selections;
- ``pending_decision``: the latest undecided gate bound to the current
  revision;
- ``blocker``: the derived blocking condition, if any;
- ``operations``: read-only operation proposals (never authorization);
- ``references``: compact event and evidence references;
- ``last_updated_seq`` and ``cursor``: the intent's last journal sequence and
  the global journal cursor.

A list view (``kind == "list"``) has bounded, deterministic rows ordered by
last sequence (descending) then intent id.  Each row has the intent state,
revision, current work/attempt, pending decision, blocker, last event and
sequence, criteria counts and an outcome summary.  The list includes terminal
intents so a verified outcome stays visible; ``total`` and ``truncated``
describe the bound.

Generic fallback
----------------
Known event, reserved-record and operation types have typed rows.  An unknown
event, an unfamiliar payload schema, an unknown reserved record, an unknown
operation or an extra collection returned by ``inspect`` becomes a readable
``display_type: "other"`` row that keeps its original data.  Unknown
operations are disabled with a reason and are never inferred.  ``other``
indexes those generic rows; an unknown event also remains in ``records`` and
an unknown gate remains in ``gates``.  Bounded event inspection is reported
through ``omitted`` rather than pretending an omitted record did not exist.
"""

from __future__ import annotations

from typing import Any, Mapping, Protocol

from . import records as R
from .errors import OutcomeBoundsError, OutcomeValidationError
from .store import (
    DEFAULT_ACTIVE,
    DEFAULT_EVENTS,
    MAX_ACTIVE,
    MAX_FOLLOW,
    OutcomeStore,
)

VIEW_SCHEMA = "ozagia.outcomes.view.v1"
KIND_INTENT = "intent"
KIND_LIST = "list"

_MAX_LIST_SCAN_PAGES = 20
_TERMINAL_INTENT_STATES = frozenset({
    "satisfied", "partial", "failed", "cancelled",
})
_ACTIVE_WORK_STATES = frozenset({"queued", "running", "waiting"})

_EVENT_META = {
    R.INTENT_SUBMITTED: ("intent", "Intent submitted"),
    R.INTENT_REVISED: ("intent", "Intent revised"),
    R.INTENT_TRANSITIONED: ("intent", "Intent state changed"),
    R.WORK_ADDED: ("work", "Work added"),
    R.WORK_TRANSITIONED: ("work", "Work state changed"),
    R.WORK_STALE: ("work", "Work became stale"),
    R.ATTEMPT_QUEUED: ("attempt", "Attempt queued"),
    R.ATTEMPT_STARTED: ("attempt", "Attempt started"),
    R.ATTEMPT_FINISHED: ("attempt", "Attempt finished"),
    R.EVENT_APPENDED: ("event", "Event appended"),
    R.GATE_CREATED: ("gate", "Gate recorded"),
    R.DECISION_RECORDED: ("decision", "Decision recorded"),
    R.EVIDENCE_RECORDED: ("evidence", "Evidence recorded"),
    R.EFFECT_RECORDED: ("effect", "Effect recorded"),
    R.OUTCOME_RECORDED: ("outcome", "Outcome recorded"),
}

_RESERVED_META = {
    R.GATE_SCHEMA: ("gate", "Gate"),
    R.DECISION_SCHEMA: ("decision", "Decision"),
    R.EVIDENCE_SCHEMA: ("evidence", "Evidence"),
    R.EFFECT_SCHEMA: ("effect", "Effect"),
    R.OUTCOME_SCHEMA: ("outcome", "Outcome"),
}
_RESERVED_BY_TYPE = {
    display_type: schema
    for schema, (display_type, _label) in _RESERVED_META.items()
}

_OPERATION_META = {
    "intent.revise": ("Revise intent", False),
    "intent.cancel": ("Cancel intent", False),
    "intent.satisfy": ("Mark intent satisfied", True),
    "work.add": ("Add work", False),
    "work.transition": ("Change work state", False),
    "attempt.queue": ("Queue attempt", False),
    "attempt.start": ("Start attempt", False),
    "attempt.finish": ("Finish attempt", False),
    "evidence.record": ("Record evidence", False),
    "gate.record": ("Record gate", False),
    "decision.record": ("Record decision", False),
    "outcome.record": ("Record outcome", False),
}


class _OutcomeReader(Protocol):
    def inspect(self, intent_id: str, *,
                events_limit: int = DEFAULT_EVENTS) -> dict[str, Any]: ...

    def follow(self, after_seq: int = 0, *, intent_id: str | None = None,
               limit: int = 100) -> dict[str, Any]: ...

    def list_active(self, limit: int = DEFAULT_ACTIVE) -> dict[str, Any]: ...


# ---------------------------------------------------------------------------
# Generic value helpers
# ---------------------------------------------------------------------------


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _mappings(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, (list, tuple)):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


def _text(value: Any, fallback: str = "") -> str:
    if isinstance(value, str):
        return " ".join(value.split())
    if value is None:
        return fallback
    return str(value)


def _seq(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value


def _record_ref(record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "seq": record.get("seq"),
        "record_id": record.get("record_id"),
        "event": record.get("event"),
        "timestamp": record.get("timestamp"),
    }


def _generic_row(value: Any, *, kind: str, source: str | None = None,
                 reason: str | None = None) -> dict[str, Any]:
    """A readable row for an unknown record value; never drops ``value``."""
    if isinstance(value, Mapping):
        data = dict(value)
        schema = data.get("schema")
        event = data.get("event")
        type_label = _text(
            (event or schema) if kind == "record" else (schema or event),
            "unknown")
        record_id = data.get("record_id")
        seq = data.get("seq")
        status = _text(
            data.get("status") or data.get("state") or "recorded", "recorded")
        identifier = _text(record_id or seq or "", "")
        suffix = f" {identifier}" if identifier else ""
        summary = (
            f"Unrecognised {kind} type {type_label!r}{suffix}; "
            "preserved for display")
        if reason:
            summary = f"{reason}; preserved for display"
        row = {
            "display_type": "other",
            "type": type_label,
            "label": f"Unrecognised {kind}{suffix}",
            "status": status,
            "summary": summary,
            "record_id": record_id,
            "seq": seq,
            "source": source,
            "data": data,
        }
        if event is not None:
            row["event"] = event
        return row
    return {
        "display_type": "other",
        "type": kind,
        "label": f"Unrecognised {kind}",
        "status": "unknown",
        "summary": f"Unrecognised {kind} value preserved for display",
        "source": source,
        "data": value,
    }


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


def _event_status(event: str, payload: Mapping[str, Any]) -> str:
    if event in (R.WORK_ADDED, R.WORK_TRANSITIONED, R.WORK_STALE):
        return _text(payload.get("to_state") or payload.get("state"),
                     "queued")
    if event in (R.ATTEMPT_QUEUED, R.ATTEMPT_STARTED):
        return _text(payload.get("state"), "queued")
    if event == R.ATTEMPT_FINISHED:
        return _text(payload.get("outcome"), "terminal")
    if event == R.INTENT_TRANSITIONED:
        return _text(payload.get("to_state"), "recorded")
    if event == R.GATE_CREATED:
        return "pending"
    if event == R.DECISION_RECORDED:
        return _text(payload.get("decision"), "recorded")
    if event == R.EVIDENCE_RECORDED:
        return _text(payload.get("freshness"), "unknown")
    if event == R.EFFECT_RECORDED:
        return _text(payload.get("state"), "recorded")
    if event == R.OUTCOME_RECORDED:
        return _text(payload.get("status"), "recorded")
    if event in (R.INTENT_SUBMITTED, R.INTENT_REVISED):
        return "accepted" if event == R.INTENT_SUBMITTED else "revision"
    return "recorded"


def _event_summary(event: str, payload: Mapping[str, Any]) -> str:
    if event == R.INTENT_SUBMITTED:
        return _text(payload.get("requested_outcome"), "Intent accepted")
    if event == R.INTENT_REVISED:
        return (f"revision {payload.get('revision')}: "
                f"{_text(payload.get('requested_outcome'))}")
    if event == R.INTENT_TRANSITIONED:
        return (f"{_text(payload.get('from_state'))} -> "
                f"{_text(payload.get('to_state'))}")
    if event == R.WORK_ADDED:
        return _text(payload.get("title"), "work queued")
    if event in (R.WORK_TRANSITIONED, R.WORK_STALE):
        return (f"{_text(payload.get('work_id'))}: "
                f"{_text(payload.get('from_state'))} -> "
                f"{_text(payload.get('to_state'))}")
    if event == R.ATTEMPT_QUEUED:
        return f"attempt {_text(payload.get('attempt_id'))} queued"
    if event == R.ATTEMPT_STARTED:
        return f"attempt {_text(payload.get('attempt_id'))} running"
    if event == R.ATTEMPT_FINISHED:
        return (f"attempt {_text(payload.get('attempt_id'))} finished: "
                f"{_text(payload.get('outcome'))}")
    if event == R.EVENT_APPENDED:
        return _text(payload.get("kind"), "event appended")
    if event == R.GATE_CREATED:
        target = _text(payload.get("target"))
        return f"gate {_text(payload.get('gate_id'))}" + (
            f" for {target}" if target else "")
    if event == R.DECISION_RECORDED:
        return (f"gate {_text(payload.get('gate_id'))}: "
                f"{_text(payload.get('decision'))}")
    if event == R.EVIDENCE_RECORDED:
        return _text(payload.get("source"), "evidence recorded")
    if event == R.EFFECT_RECORDED:
        return (f"{_text(payload.get('effect_key'))}: "
                f"{_text(payload.get('state'))}")
    if event == R.OUTCOME_RECORDED:
        summary = _text(payload.get("summary"))
        return f"outcome {_text(payload.get('status'))}" + (
            f": {summary}" if summary else "")
    return _text(event, "recorded")


def render_record(record: Mapping[str, Any]) -> dict[str, Any]:
    """Render one journal envelope as a typed or generic row."""
    if not isinstance(record, Mapping):
        return _generic_row(record, kind="record")
    event = record.get("event")
    payload = _mapping(record.get("payload"))
    actual_schema = payload.get("schema")
    expected_schema = (R.PAYLOAD_SCHEMAS.get(event)
                       if isinstance(event, str) else None)
    meta = _EVENT_META.get(event) if isinstance(event, str) else None
    if meta is not None and not (
            isinstance(actual_schema, str) and isinstance(expected_schema, str)
            and actual_schema != expected_schema):
        display_type, label = meta
        row = {
            "display_type": display_type,
            "type": event,
            "label": label,
            "status": _event_status(event, payload),
            "summary": _event_summary(event, payload),
            "record_id": record.get("record_id"),
            "seq": record.get("seq"),
            "timestamp": record.get("timestamp"),
            "actor": record.get("actor"),
            "intent_id": record.get("intent_id"),
            "correlation_id": record.get("correlation_id"),
            "causation_id": record.get("causation_id"),
            "record": dict(record),
        }
        return row
    if meta is not None and isinstance(actual_schema, str):
        reason = (f"Record {event!r} uses unfamiliar payload schema "
                  f"{actual_schema!r}")
    elif isinstance(event, str):
        reason = f"Unrecognised event type {event!r}"
    else:
        reason = "Record has no recognised event type"
    row = _generic_row(record, kind="record", reason=reason)
    row["event"] = event
    row["record"] = dict(record)
    return row


# ---------------------------------------------------------------------------
# Reserved records
# ---------------------------------------------------------------------------


def _reserved_status(display_type: str, item: Mapping[str, Any]) -> str:
    if display_type == "gate":
        return "pending"
    if display_type == "decision":
        return _text(item.get("decision"), "recorded")
    if display_type == "evidence":
        return _text(item.get("freshness"), "unknown")
    if display_type == "effect":
        return _text(item.get("state"), "recorded")
    if display_type == "outcome":
        return _text(item.get("status"), "recorded")
    return "recorded"


def _reserved_summary(display_type: str, item: Mapping[str, Any]) -> str:
    if display_type == "gate":
        target = _text(item.get("target"))
        return "requires a decision" + (f" for {target}" if target else "")
    if display_type == "decision":
        return (f"gate {_text(item.get('gate_id'))}: "
                f"{_text(item.get('decision'))}")
    if display_type == "evidence":
        return _text(item.get("source"), "evidence recorded")
    if display_type == "effect":
        return (f"{_text(item.get('effect_key'))}: "
                f"{_text(item.get('state'))}")
    if display_type == "outcome":
        summary = _text(item.get("summary"))
        return _text(item.get("status")) + (f": {summary}" if summary else "")
    return display_type


def _render_reserved(item: Mapping[str, Any], hint: str | None = None) -> dict[str, Any]:
    """Render one flattened reserved record, with a generic fallback."""
    if not isinstance(item, Mapping):
        return _generic_row(item, kind=hint or "record")
    schema = item.get("schema")
    meta = _RESERVED_META.get(schema) if isinstance(schema, str) else None
    if meta is None and schema is None and hint in _RESERVED_BY_TYPE:
        schema = _RESERVED_BY_TYPE[hint]
        meta = _RESERVED_META[schema]
    if meta is None or (hint is not None and meta[0] != hint):
        reason = None
        if isinstance(schema, str):
            reason = f"Unrecognised {hint or 'record'} schema {schema!r}"
        return _generic_row(item, kind=hint or "record", reason=reason)
    display_type, label_prefix = meta
    identifier = _text(
        item.get("gate_id") or item.get("record_id") or item.get("seq"),
        "")
    suffix = f" {identifier}" if identifier else ""
    row = dict(item)
    row.update({
        "display_type": display_type,
        "type": schema,
        "label": f"{label_prefix}{suffix}",
        "status": _reserved_status(display_type, item),
        "summary": _reserved_summary(display_type, item),
        "data": dict(item),
    })
    return row


def _split_reserved(items: Any, hint: str) -> tuple[list[dict[str, Any]],
                                                    list[dict[str, Any]]]:
    typed: list[dict[str, Any]] = []
    other: list[dict[str, Any]] = []
    for item in _mappings(items):
        row = _render_reserved(item, hint)
        if row.get("display_type") == "other":
            other.append(row)
        else:
            typed.append(row)
    return typed, other


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------


def render_operation(operation: Mapping[str, Any] | str) -> dict[str, Any]:
    """Render an operation proposal, disabling any unknown operation.

    Known operations keep their catalogue label and ``requires_gate`` flag.
    An unknown id is never inferred: it is returned as a generic, disabled row
    with the original data and a reason.
    """
    supplied = ({"id": operation}
                if isinstance(operation, str)
                else dict(operation) if isinstance(operation, Mapping)
                else {"operation": operation})
    operation_id = _text(supplied.get("id") or supplied.get("operation"),
                         "unknown")
    meta = _OPERATION_META.get(operation_id)
    if meta is None:
        return {
            "display_type": "other",
            "id": operation_id,
            "label": _text(supplied.get("label"),
                           f"Unrecognised operation {operation_id}"),
            "enabled": False,
            "requires_gate": bool(supplied.get("requires_gate", False)),
            "reason": _text(
                supplied.get("reason"),
                f"Unknown operation {operation_id!r}; it is not inferred"),
            "data": supplied,
        }
    label, requires_gate = meta
    enabled = supplied.get("enabled", True)
    if not isinstance(enabled, bool):
        enabled = bool(enabled)
    return {
        "display_type": "operation",
        "id": operation_id,
        "label": _text(supplied.get("label"), label),
        "enabled": enabled,
        "requires_gate": bool(supplied.get("requires_gate", requires_gate)),
        "reason": _text(supplied.get("reason"), "") or None,
    }


def _operation(operation_id: str) -> dict[str, Any]:
    return render_operation({"id": operation_id})


# ---------------------------------------------------------------------------
# Selection, evidence, decisions and operations
# ---------------------------------------------------------------------------


def _select_work(items: list[dict[str, Any]],
                 current_hash: str) -> dict[str, Any] | None:
    if not items:
        return None

    def rank(item: Mapping[str, Any]) -> tuple[Any, ...]:
        return (
            item.get("revision_hash") == current_hash,
            item.get("state") in _ACTIVE_WORK_STATES,
            _seq(item.get("created_at")),
            _seq(item.get("last_seq")),
            _text(item.get("work_id")),
        )

    return max(items, key=rank)


def _select_attempt(items: list[dict[str, Any]],
                    work_id: str | None) -> dict[str, Any] | None:
    if work_id is None:
        return None
    candidates = [item for item in items if item.get("work_id") == work_id]
    if not candidates:
        return None

    def rank(item: Mapping[str, Any]) -> tuple[Any, ...]:
        return (
            item.get("state") == "running",
            item.get("state") == "queued",
            _seq(item.get("created_at")),
            _seq(item.get("last_seq")),
            _text(item.get("attempt_id")),
        )

    return max(candidates, key=rank)


def _work_row(item: Mapping[str, Any], current_hash: str) -> dict[str, Any]:
    row = dict(item)
    row.update({
        "display_type": "work",
        "type": "work",
        "label": _text(item.get("title") or item.get("work_id"), "work"),
        "status": _text(item.get("state"), "unknown"),
        "revision_current": item.get("revision_hash") == current_hash,
    })
    return row


def _attempt_row(item: Mapping[str, Any], current_hash: str) -> dict[str, Any]:
    row = dict(item)
    row.update({
        "display_type": "attempt",
        "type": "attempt",
        "label": f"Attempt {_text(item.get('attempt_id'))}",
        "status": _text(item.get("outcome") or item.get("state"), "unknown"),
        "revision_current": item.get("revision_hash") == current_hash,
    })
    return row


def _gate_rows(gates: list[dict[str, Any]],
               decisions: list[dict[str, Any]],
               current_hash: str) -> tuple[list[dict[str, Any]],
                                           dict[str, Any] | None]:
    latest: dict[str, dict[str, Any]] = {}
    for decision in decisions:
        gate_id = decision.get("gate_id")
        if not isinstance(gate_id, str) or not gate_id:
            continue
        previous = latest.get(gate_id)
        if previous is None or _seq(decision.get("seq")) >= _seq(
                previous.get("seq")):
            latest[gate_id] = decision
    rows: list[dict[str, Any]] = []
    pending_current: list[dict[str, Any]] = []
    for gate in gates:
        row = _render_reserved(gate, "gate")
        if row.get("display_type") == "gate":
            gate_id = _text(gate.get("gate_id"))
            decision = latest.get(gate_id)
            row["decision"] = (
                _render_reserved(decision, "decision") if decision else None)
            row["status"] = (
                _text(decision.get("decision"), "recorded")
                if decision else "pending")
            row["revision_current"] = (
                gate.get("revision_hash") == current_hash)
            row["requires_decision"] = (
                bool(gate.get("required", True)) and decision is None)
            target = _text(gate.get("target"))
            row["label"] = f"Gate {gate_id}" + (
                f" for {target}" if target else "")
            if decision is None:
                row["summary"] = "requires a decision"
            else:
                row["summary"] = (
                    f"decision: {_text(decision.get('decision'))}")
            if (decision is None and row["revision_current"]
                    and bool(gate.get("required", True))):
                pending_current.append(row)
        rows.append(row)
    pending = pending_current[-1] if pending_current else None
    return rows, pending


def _evidence_rows(evidence: list[dict[str, Any]],
                   current_hash: str) -> tuple[list[dict[str, Any]],
                                                list[dict[str, Any]],
                                                list[dict[str, Any]],
                                                set[str]]:
    verified: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    other: list[dict[str, Any]] = []
    ids: set[str] = set()
    for item in evidence:
        row = _render_reserved(item, "evidence")
        if row.get("display_type") == "other":
            other.append(row)
            continue
        record_id = row.get("record_id")
        if isinstance(record_id, str):
            ids.add(record_id)
        current = (
            row.get("revision_hash") == current_hash
            and row.get("freshness") == "current")
        row["verified"] = current
        if current:
            row["status"] = "verified"
            row["reason"] = None
            verified.append(row)
        else:
            row["status"] = "pending"
            if row.get("revision_hash") != current_hash:
                row["reason"] = "bound to a superseded intent revision"
            else:
                row["reason"] = (
                    f"freshness is {_text(row.get('freshness'), 'unknown')!r}")
            pending.append(row)
    return verified, pending, other, ids


def _pending_evidence_refs(refs: list[str], known: set[str],
                           reason: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for reference in refs:
        if not isinstance(reference, str) or not reference:
            continue
        if reference in known or reference in seen:
            continue
        seen.add(reference)
        rows.append({
            "display_type": "evidence",
            "type": "evidence_reference",
            "record_id": None,
            "reference": reference,
            "source": reference,
            "status": "pending",
            "freshness": "unknown",
            "verified": False,
            "reason": reason,
        })
    return rows


def _satisfaction_ready(state: str, outcome: Mapping[str, Any] | None,
                        decisions: list[dict[str, Any]],
                        current_hash: str) -> bool:
    if outcome is None or outcome.get("status") != "satisfied":
        return False
    if outcome.get("revision_hash") != current_hash:
        return False
    if not outcome.get("evidence_refs"):
        return False
    decision_ref = outcome.get("decision_ref")
    if not isinstance(decision_ref, str) or not decision_ref:
        return False
    decision = next(
        (item for item in decisions
         if item.get("record_id") == decision_ref), None)
    if decision is None:
        return False
    if decision.get("decision") != "approved":
        return False
    if decision.get("revision_hash") != current_hash:
        return False
    return "satisfied" in R.INTENT_TRANSITIONS.get(state, frozenset())


def _operations(state: str, current_work: Mapping[str, Any] | None,
                current_attempt: Mapping[str, Any] | None,
                pending_decision: Mapping[str, Any] | None,
                outcome: Mapping[str, Any] | None,
                decisions: list[dict[str, Any]],
                current_hash: str) -> list[dict[str, Any]]:
    if state in _TERMINAL_INTENT_STATES:
        return []
    operations = [
        _operation("intent.revise"),
        _operation("intent.cancel"),
        _operation("work.add"),
    ]
    if current_work is not None:
        claimable = (
            state in ("accepted", "running")
            and current_work.get("revision_current") is True
            and current_work.get("state") in ("queued", "running"))
        if claimable:
            operations.append(_operation("attempt.queue"))
            if current_attempt is None or current_attempt.get(
                    "state") != "running":
                operations.append(_operation("attempt.start"))
        if (current_work.get("state") == "waiting"
                and current_work.get("revision_current") is True):
            operations.append(_operation("work.transition"))
    if current_attempt is not None and current_attempt.get(
            "state") == "running":
        operations.append(_operation("attempt.finish"))
    operations.extend([
        _operation("evidence.record"),
        _operation("gate.record"),
        _operation("outcome.record"),
    ])
    if pending_decision is not None:
        operations.append(_operation("decision.record"))
    if _satisfaction_ready(state, outcome, decisions, current_hash):
        operations.append(_operation("intent.satisfy"))
    return operations


def _blocker(state: str, current_work: Mapping[str, Any] | None,
             current_attempt: Mapping[str, Any] | None,
             pending_decision: Mapping[str, Any] | None,
             outcome: Mapping[str, Any] | None,
             gates: list[dict[str, Any]] | None = None
             ) -> dict[str, Any] | None:
    if state in _TERMINAL_INTENT_STATES:
        return None
    if pending_decision is not None:
        gate_id = _text(pending_decision.get("gate_id"))
        return {
            "kind": "decision",
            "summary": f"Decision required for gate {gate_id}",
            "gate_id": gate_id,
            "gate_ref": pending_decision.get("record_id"),
            "next_operation": "decision.record",
        }
    if state == "waiting_decision":
        for gate in reversed(gates or []):
            decision = gate.get("decision")
            if (gate.get("revision_current") is True
                    and isinstance(decision, Mapping)
                    and decision.get("status") == "approved"):
                gate_id = _text(gate.get("gate_id"))
                return {
                    "kind": "finalize",
                    "summary": f"Gate {gate_id} is approved; record the "
                               "verified outcome",
                    "gate_id": gate_id,
                    "gate_ref": gate.get("record_id"),
                    "next_operation": "outcome.record",
                }
        return {
            "kind": "decision",
            "summary": "Intent is waiting for a decision; no current gate is "
                       "pending",
            "next_operation": None,
        }
    if (current_attempt is not None
            and current_attempt.get("state") == "terminal"
            and current_attempt.get("outcome") == "uncertain"):
        return {
            "kind": "uncertain_attempt",
            "summary": "The latest attempt finished uncertain; reconcile "
                       "before retrying",
            "attempt_id": current_attempt.get("attempt_id"),
            "next_operation": "work.transition",
        }
    if current_work is not None and current_work.get("state") == "waiting":
        return {
            "kind": "work_waiting",
            "summary": "Work is waiting; resume or replace it",
            "work_id": current_work.get("work_id"),
            "next_operation": "work.transition",
        }
    if current_work is not None and current_work.get("state") == "failed":
        return {
            "kind": "failed_work",
            "summary": "The current work item failed",
            "work_id": current_work.get("work_id"),
            "next_operation": "work.add",
        }
    if (current_work is not None
            and current_work.get("revision_current") is False
            and current_work.get("state") in ("stale", "cancelled")):
        return {
            "kind": "stale_work",
            "summary": "The visible work is bound to a superseded revision",
            "work_id": current_work.get("work_id"),
            "next_operation": "work.add",
        }
    if outcome is not None and outcome.get("status") == "failed":
        return {
            "kind": "failed_outcome",
            "summary": "The recorded outcome is failed",
            "next_operation": "intent.revise",
        }
    return None


# ---------------------------------------------------------------------------
# Detail view
# ---------------------------------------------------------------------------


def _unknown_collections(page: Mapping[str, Any]) -> list[dict[str, Any]]:
    known = {
        "intent", "revisions", "work", "attempts", "events", "event_count",
        "events_truncated", "gates", "decisions", "evidence", "effects",
        "outcomes", "outcome", "latest_outcome", "cursor", "journal",
        "operations",
    }
    rows: list[dict[str, Any]] = []
    for key, value in page.items():
        if key in known:
            continue
        if isinstance(value, Mapping):
            rows.append(_generic_row(value, kind="record", source=key))
        elif isinstance(value, (list, tuple)):
            for item in value:
                if isinstance(item, Mapping):
                    rows.append(_generic_row(
                        item, kind="record", source=key))
                else:
                    rows.append(_generic_row(
                        item, kind=f"record from {key}", source=key))
    return rows


def _criteria_rows(revision: Mapping[str, Any],
                   outcome: Mapping[str, Any] | None) -> tuple[
                       list[dict[str, Any]], dict[str, int]]:
    values = list(revision.get("criteria") or [])
    satisfied = set(outcome.get("criteria_satisfied") or []) if outcome else set()
    rows: list[dict[str, Any]] = []
    for criterion in values:
        if not isinstance(criterion, str):
            criterion = str(criterion)
        rows.append({
            "criterion": criterion,
            "status": "satisfied" if criterion in satisfied else "pending",
            "in_current_revision": True,
        })
    for criterion in sorted(satisfied):
        if criterion not in values:
            rows.append({
                "criterion": criterion,
                "status": "satisfied",
                "in_current_revision": False,
            })
    summary = {
        "total": len(rows),
        "satisfied": sum(1 for row in rows
                         if row["status"] == "satisfied"),
        "pending": sum(1 for row in rows if row["status"] == "pending"),
    }
    return rows, summary


def _build_view(page: Mapping[str, Any]) -> dict[str, Any]:
    intent = _mapping(page.get("intent"))
    revisions = _mappings(page.get("revisions"))
    revision = revisions[-1] if revisions else {}
    current_hash = _text(
        intent.get("revision_hash") or revision.get("revision_hash"))
    state = _text(intent.get("state"), "unknown")

    work_items = _mappings(page.get("work"))
    attempt_items = _mappings(page.get("attempts"))
    current_work_raw = _select_work(work_items, current_hash)
    current_work = (
        _work_row(current_work_raw, current_hash)
        if current_work_raw is not None else None)
    current_attempt_raw = _select_attempt(
        attempt_items,
        current_work_raw.get("work_id") if current_work_raw else None)
    current_attempt = (
        _attempt_row(current_attempt_raw, current_hash)
        if current_attempt_raw is not None else None)

    gates_raw = _mappings(page.get("gates"))
    decisions_raw = _mappings(page.get("decisions"))
    gates, pending_decision = _gate_rows(
        gates_raw, decisions_raw, current_hash)
    decisions, decision_other = _split_reserved(decisions_raw, "decision")
    effects, effect_other = _split_reserved(page.get("effects"), "effect")
    outcomes, outcome_other = _split_reserved(page.get("outcomes"), "outcome")

    outcome_raw = _mapping(page.get("outcome"))
    latest_outcome_raw = _mapping(page.get("latest_outcome"))
    outcome = _render_reserved(outcome_raw, "outcome") if outcome_raw else None
    latest_outcome = (
        _render_reserved(latest_outcome_raw, "outcome")
        if latest_outcome_raw else None)

    evidence_raw = _mappings(page.get("evidence"))
    verified, pending, evidence_other, evidence_ids = _evidence_rows(
        evidence_raw, current_hash)
    refs: list[str] = []
    if outcome_raw:
        refs.extend(
            ref for ref in outcome_raw.get("evidence_refs") or []
            if isinstance(ref, str))
    if current_attempt_raw:
        refs.extend(
            ref for ref in current_attempt_raw.get("evidence_refs") or []
            if isinstance(ref, str))
    if pending_decision is not None:
        refs.extend(
            ref for ref in pending_decision.get("evidence_refs") or []
            if isinstance(ref, str))
    pending.extend(_pending_evidence_refs(
        refs, evidence_ids,
        "referenced by current work but no evidence record is stored"))

    records = [
        render_record(record)
        for record in _mappings(page.get("events"))
    ]
    other: list[dict[str, Any]] = []
    other.extend(row for row in records
                 if row.get("display_type") == "other")
    other.extend(decision_other)
    other.extend(effect_other)
    other.extend(outcome_other)
    other.extend(evidence_other)
    other.extend(row for row in gates
                 if row.get("display_type") == "other")
    other.extend(row for row in pending
                 if row.get("display_type") == "other")
    other.extend(_unknown_collections(page))

    criteria, criteria_summary = _criteria_rows(revision, outcome_raw or None)
    operations = _operations(
        state, current_work, current_attempt, pending_decision,
        outcome_raw or None, decisions, current_hash)
    seen_operations = {item.get("id") for item in operations}
    extra_operations = page.get("operations")
    if isinstance(extra_operations, Mapping):
        extra_operations = [extra_operations]
    if isinstance(extra_operations, (list, tuple)):
        for item in extra_operations:
            operation = render_operation(item)
            if operation.get("id") in seen_operations:
                continue
            seen_operations.add(operation.get("id"))
            operations.append(operation)
    blocker = _blocker(
        state, current_work, current_attempt, pending_decision,
        outcome_raw or None, gates)

    events = _mappings(page.get("events"))
    last_event = _record_ref(events[-1]) if events else None
    last_updated = max(
        _seq(intent.get("last_seq")),
        _seq(last_event.get("seq")) if last_event else 0,
    )
    event_count = page.get("event_count")
    if not isinstance(event_count, int) or isinstance(event_count, bool):
        event_count = len(events)
    omitted_events = max(0, event_count - len(events))
    evidence_rows = verified + pending
    references = {
        "evidence": [
            {
                "record_id": row.get("record_id"),
                "reference": row.get("reference"),
                "seq": row.get("seq"),
                "source": row.get("source"),
                "status": row.get("status"),
            }
            for row in evidence_rows
        ],
        "events": [
            _record_ref(record)
            for record in events
        ],
    }
    return {
        "schema": VIEW_SCHEMA,
        "kind": KIND_INTENT,
        "intent_id": intent.get("intent_id"),
        "status": state,
        "intent": intent,
        "revision": revision,
        "outcome": outcome,
        "latest_outcome": latest_outcome,
        "criteria": criteria,
        "criteria_summary": criteria_summary,
        "evidence": {"verified": verified, "pending": pending},
        "current_work": current_work,
        "current_attempt": current_attempt,
        "pending_decision": pending_decision,
        "blocker": blocker,
        "operations": operations,
        "gates": gates,
        "decisions": decisions,
        "effects": effects,
        "outcomes": outcomes,
        "records": records,
        "other": other,
        "references": references,
        "last_event": last_event,
        "last_updated_seq": last_updated,
        "cursor": page.get("cursor"),
        "journal": _mapping(page.get("journal")),
        "omitted": {
            "events": omitted_events,
            "events_truncated": bool(page.get("events_truncated")),
            "reason": "bounded" if omitted_events else "none",
        },
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _default_store() -> OutcomeStore:
    from ozagia.config import repo_root
    return OutcomeStore(repo_root())


def _reader(store: _OutcomeReader | None) -> _OutcomeReader:
    return store if store is not None else _default_store()


def view(intent_id: str, store: _OutcomeReader | None = None, *,
         events_limit: int = DEFAULT_EVENTS) -> dict[str, Any]:
    """Project one intent as ``ozagia.outcomes.view.v1`` (read only)."""
    page = _reader(store).inspect(intent_id, events_limit=events_limit)
    return _build_view(page)


def _valid_limit(limit: Any) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise OutcomeValidationError("limit must be an integer")
    if limit < 1 or limit > MAX_ACTIVE:
        raise OutcomeBoundsError(
            f"limit must be between 1 and {MAX_ACTIVE}")
    return limit


def _discover(store: _OutcomeReader,
              active_page: Mapping[str, Any]) -> tuple[
                  list[dict[str, Any]], bool, dict[str, Any] | None]:
    found: dict[str, dict[str, Any]] = {}

    def add(intent_id: Any, seq: Any,
            event: Mapping[str, Any] | None = None) -> None:
        if not isinstance(intent_id, str) or not intent_id:
            return
        entry = found.setdefault(intent_id, {
            "intent_id": intent_id, "last_seq": 0, "last_event": None})
        number = _seq(seq)
        if number >= entry["last_seq"]:
            entry["last_seq"] = number
            if event is not None:
                entry["last_event"] = _record_ref(event)

    for row in _mappings(active_page.get("intents")):
        add(row.get("intent_id"), row.get("last_seq"))

    after = 0
    pages = 0
    gap: dict[str, Any] | None = None
    scan_truncated = False
    while pages < _MAX_LIST_SCAN_PAGES:
        page = store.follow(after, limit=MAX_FOLLOW)
        if gap is None and page.get("gap"):
            gap = dict(page["gap"])
        for record in _mappings(page.get("records")):
            add(record.get("intent_id"), record.get("seq"), record)
        cursor = _seq(page.get("cursor"))
        if not page.get("more"):
            break
        if cursor <= after:
            break
        after = cursor
        pages += 1
    else:
        scan_truncated = True
    ordered = sorted(
        found.values(),
        key=lambda item: (-int(item["last_seq"]), item["intent_id"]))
    return ordered, scan_truncated, gap


def _outcome_summary(view_row: Mapping[str, Any]) -> dict[str, Any] | None:
    outcome = _mapping(view_row.get("outcome"))
    latest = _mapping(view_row.get("latest_outcome"))
    chosen = outcome or latest
    if not chosen:
        return None
    return {
        "status": chosen.get("status"),
        "summary": chosen.get("summary"),
        "record_id": chosen.get("record_id"),
        "seq": chosen.get("seq"),
        "revision_hash": chosen.get("revision_hash"),
        "current_revision": bool(outcome),
    }


def _compact_work(work: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not work:
        return None
    return {
        "work_id": work.get("work_id"),
        "title": work.get("title"),
        "state": work.get("state"),
        "revision": work.get("revision"),
        "revision_current": work.get("revision_current"),
        "stale_reason": work.get("stale_reason"),
    }


def _compact_attempt(attempt: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not attempt:
        return None
    return {
        "attempt_id": attempt.get("attempt_id"),
        "work_id": attempt.get("work_id"),
        "state": attempt.get("state"),
        "outcome": attempt.get("outcome"),
        "revision_current": attempt.get("revision_current"),
        "lease_generation": attempt.get("lease_generation"),
    }


def _list_row(detail: Mapping[str, Any],
              fallback_event: Mapping[str, Any] | None = None) -> dict[str, Any]:
    intent = _mapping(detail.get("intent"))
    last_event = _mapping(detail.get("last_event")) or _mapping(fallback_event)
    outcome = _outcome_summary(detail)
    operations = [
        operation.get("id")
        for operation in detail.get("operations") or []
        if isinstance(operation, Mapping) and operation.get("enabled")
    ]
    last_seq = max(
        _seq(intent.get("last_seq")),
        _seq(last_event.get("seq")),
        _seq(detail.get("last_updated_seq")),
    )
    return {
        "display_type": "intent",
        "intent_id": intent.get("intent_id"),
        "state": detail.get("status"),
        "revision": intent.get("revision"),
        "revision_hash": intent.get("revision_hash"),
        "requested_outcome": intent.get("requested_outcome"),
        "principal": intent.get("principal"),
        "created_at": intent.get("created_at"),
        "updated_at": intent.get("updated_at"),
        "current_work": _compact_work(_mapping(detail.get("current_work"))),
        "current_attempt": _compact_attempt(
            _mapping(detail.get("current_attempt"))),
        "pending_decision": detail.get("pending_decision"),
        "blocker": detail.get("blocker"),
        "criteria_summary": _mapping(detail.get("criteria_summary")),
        "outcome": outcome,
        "outcome_summary": (
            outcome.get("summary") or outcome.get("status")
            if outcome else None),
        "last_event": last_event or None,
        "last_seq": last_seq,
        "last_updated_seq": last_seq,
        "operations": operations,
        "next_operation": operations[0] if operations else None,
    }


def list_view(store: _OutcomeReader | None = None,
              limit: int = DEFAULT_ACTIVE) -> dict[str, Any]:
    """Project bounded, deterministic list rows as the same view schema.

    The list includes active and terminal intents so a verified outcome stays
    visible after satisfaction.  Rows are ordered by last sequence descending
    then intent id; ``limit`` is bounded by the store's active-intent bound.
    """
    target = _reader(store)
    bound = _valid_limit(limit)
    active_page = target.list_active(limit=MAX_ACTIVE)
    ordered, scan_truncated, gap = _discover(target, active_page)
    selected = ordered[:bound]
    rows: list[dict[str, Any]] = []
    for candidate in selected:
        detail = view(candidate["intent_id"], target, events_limit=1)
        rows.append(_list_row(detail, candidate.get("last_event")))
    omitted = len(ordered) - len(rows)
    reasons: list[str] = []
    if omitted:
        reasons.append("limit")
    if scan_truncated:
        reasons.append("journal scan bound")
    if gap:
        reasons.append("sequence gap")
    return {
        "schema": VIEW_SCHEMA,
        "kind": KIND_LIST,
        "count": len(rows),
        "total": len(ordered),
        "truncated": bool(omitted) or scan_truncated,
        "cursor": active_page.get("cursor"),
        "intents": rows,
        "omitted": {
            "intents": omitted,
            "scan_truncated": scan_truncated,
            "gap": gap,
            "reason": ", ".join(reasons) if reasons else "none",
        },
    }


__all__ = [
    "KIND_INTENT",
    "KIND_LIST",
    "VIEW_SCHEMA",
    "list_view",
    "render_operation",
    "render_record",
    "view",
]
