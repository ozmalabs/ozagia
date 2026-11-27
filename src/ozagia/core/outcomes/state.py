"""Materialized outcome state and replay validation.

``replay_records`` is the only authority for what the journal means.  It walks
the hash chain in order and applies every record exactly once.  A record that
is malformed, names an unknown entity, breaks a transition or violates a
satisfaction rule aborts replay with :class:`OutcomeIntegrityError`; the store
fails closed rather than serving a partially applied history.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from . import records as R
from .errors import (
    OutcomeConflictError,
    OutcomeError,
    OutcomeIntegrityError,
    OutcomeNotFoundError,
    OutcomeStaleError,
    OutcomeTransitionError,
    OutcomeUnsupportedError,
    OutcomeValidationError,
)


# ---------------------------------------------------------------------------
# Materialized entities
# ---------------------------------------------------------------------------


@dataclass
class IntentState:
    intent_id: str
    principal: str
    state: str
    created_at: int
    updated_at: int
    last_seq: int
    revisions: list[R.IntentRevision] = field(default_factory=list)
    conversation_ref: str | None = None
    outcome_ref: str | None = None
    decision_ref: str | None = None

    @property
    def current_revision(self) -> R.IntentRevision:
        return self.revisions[-1]

    @property
    def revision(self) -> int:
        return self.current_revision.revision

    @property
    def revision_hash(self) -> str:
        return self.current_revision.revision_hash

    def revision_for(self, digest: str) -> R.IntentRevision | None:
        for revision in self.revisions:
            if revision.revision_hash == digest:
                return revision
        return None

    def summary(self) -> dict[str, Any]:
        current = self.current_revision
        return {
            "intent_id": self.intent_id,
            "principal": self.principal,
            "state": self.state,
            "revision": current.revision,
            "revision_hash": current.revision_hash,
            "requested_outcome": current.requested_outcome,
            "criteria": list(current.criteria),
            "context_refs": list(current.context_refs),
            "project_ref": current.project_ref,
            "interpretation": current.interpretation,
            "conversation_ref": self.conversation_ref,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_seq": self.last_seq,
            "revision_count": len(self.revisions),
            "outcome_ref": self.outcome_ref,
            "decision_ref": self.decision_ref,
        }


@dataclass
class WorkItem:
    work_id: str
    intent_id: str
    revision: int
    revision_hash: str
    title: str
    kind: str
    state: str
    project_ref: str | None
    context_refs: list[str]
    details: dict[str, Any]
    created_at: int
    updated_at: int
    last_seq: int
    stale_reason: str | None = None
    stale_reference: str | None = None
    closed_at: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "work_id": self.work_id,
            "intent_id": self.intent_id,
            "revision": self.revision,
            "revision_hash": self.revision_hash,
            "title": self.title,
            "kind": self.kind,
            "state": self.state,
            "project_ref": self.project_ref,
            "context_refs": list(self.context_refs),
            "details": dict(self.details),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_seq": self.last_seq,
            "stale_reason": self.stale_reason,
            "stale_reference": self.stale_reference,
            "closed_at": self.closed_at,
        }


@dataclass
class Attempt:
    attempt_id: str
    intent_id: str
    work_id: str
    revision: int
    revision_hash: str
    state: str
    outcome: str | None
    worker_ref: str | None
    assignment_ref: str | None
    baseline_ref: str | None
    lease_generation: int | str
    lease_ref: str | None
    created_at: int
    started_at: int | None
    finished_at: int | None
    detail: str | None
    evidence_refs: list[str]
    last_seq: int
    finish_record_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempt_id": self.attempt_id,
            "intent_id": self.intent_id,
            "work_id": self.work_id,
            "revision": self.revision,
            "revision_hash": self.revision_hash,
            "state": self.state,
            "outcome": self.outcome,
            "worker_ref": self.worker_ref,
            "assignment_ref": self.assignment_ref,
            "baseline_ref": self.baseline_ref,
            "lease_generation": self.lease_generation,
            "lease_ref": self.lease_ref,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "detail": self.detail,
            "evidence_refs": list(self.evidence_refs),
            "last_seq": self.last_seq,
            "finish_record_id": self.finish_record_id,
        }


@dataclass
class ReservedRecord:
    """A typed Gate, Decision, Evidence, Effect or Outcome payload."""

    record_id: str
    seq: int
    timestamp: int
    actor: str
    intent_id: str
    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        result = dict(self.payload)
        result.update({
            "record_id": self.record_id,
            "seq": self.seq,
            "timestamp": self.timestamp,
            "actor": self.actor,
            "intent_id": self.intent_id,
        })
        return result


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------


def replay_records(records: Iterable[R.OutcomeRecord]) -> OutcomeState:
    """Validate the hash chain and transitions, then return materialized state."""
    state = OutcomeState()
    expected_prev: str | None = None
    for index, item in enumerate(records):
        record = item if isinstance(item, R.OutcomeRecord) else R.OutcomeRecord.from_dict(item)
        if index == 0 and record.seq != 1:
            raise OutcomeIntegrityError(
                "the outcome journal does not begin at sequence 1")
        if record.seq != state.cursor + 1:
            raise OutcomeIntegrityError(
                f"outcome journal sequence gap before {record.seq}; "
                f"expected {state.cursor + 1}")
        if record.prev_hash != expected_prev:
            raise OutcomeIntegrityError(
                f"outcome record {record.record_id} has a broken previous "
                "hash")
        try:
            state.apply(record)
        except OutcomeError as exc:
            raise OutcomeIntegrityError(
                f"outcome record {record.seq} ({record.event}) cannot be "
                f"replayed: {exc}") from exc
        expected_prev = record.record_hash
    return state


class OutcomeState:
    """Materialized view of one canonical journal."""

    def __init__(self) -> None:
        self.records: list[R.OutcomeRecord] = []
        self.intents: dict[str, IntentState] = {}
        self.work: dict[str, WorkItem] = {}
        self.attempts: dict[str, Attempt] = {}
        self.gates: dict[str, ReservedRecord] = {}
        self.decisions: dict[str, ReservedRecord] = {}
        self.evidence: dict[str, ReservedRecord] = {}
        self.effects: dict[str, ReservedRecord] = {}
        self.outcomes: dict[str, ReservedRecord] = {}
        self.idempotency: dict[str, R.OutcomeRecord] = {}
        self.record_ids: set[str] = set()
        self.cursor = 0

    # -- lookups ---------------------------------------------------------

    def intent(self, intent_id: str) -> IntentState:
        intent = self.intents.get(intent_id)
        if intent is None:
            raise OutcomeNotFoundError(f"intent {intent_id!r} does not exist")
        return intent

    def revision(self, intent_id: str, digest: str) -> R.IntentRevision:
        intent = self.intent(intent_id)
        revision = intent.revision_for(digest)
        if revision is None:
            raise OutcomeNotFoundError(
                f"intent {intent_id!r} has no revision {digest!r}")
        return revision

    def work_for(self, intent_id: str) -> list[WorkItem]:
        return sorted(
            (item for item in self.work.values()
             if item.intent_id == intent_id),
            key=lambda item: (item.created_at, item.last_seq, item.work_id))

    def attempts_for(self, intent_id: str) -> list[Attempt]:
        return sorted(
            (item for item in self.attempts.values()
             if item.intent_id == intent_id),
            key=lambda item: (item.created_at, item.last_seq, item.attempt_id))

    def decisions_for(self, intent_id: str) -> list[ReservedRecord]:
        return _reserved_for(self.decisions, intent_id)

    def outcomes_for(self, intent_id: str,
                     revision_hash: str | None = None,
                     status: str | None = None) -> list[ReservedRecord]:
        items = [
            item for item in _reserved_for(self.outcomes, intent_id)
            if (revision_hash is None
                or item.payload.get("revision_hash") == revision_hash)
            and (status is None or item.payload.get("status") == status)
        ]
        return sorted(items, key=lambda item: item.seq)

    # -- apply -----------------------------------------------------------

    def apply(self, record: R.OutcomeRecord) -> None:
        """Apply one envelope, validating sequence and transitions."""
        if record.seq != self.cursor + 1:
            raise OutcomeIntegrityError(
                f"record sequence {record.seq} does not follow {self.cursor}")
        if record.record_id in self.record_ids:
            raise OutcomeIntegrityError(
                f"duplicate outcome record id {record.record_id!r}")
        if record.idempotency_key in self.idempotency:
            raise OutcomeConflictError(
                f"duplicate idempotency key {record.idempotency_key!r}")
        handler = _HANDLERS.get(record.event)
        if handler is None:
            raise OutcomeUnsupportedError(
                f"unsupported outcome event {record.event!r}")
        handler(self, record)
        self.records.append(record)
        self.record_ids.add(record.record_id)
        self.idempotency[record.idempotency_key] = record
        self.cursor = record.seq
        intent = self.intents.get(record.intent_id)
        if intent is not None:
            intent.updated_at = max(intent.updated_at, record.timestamp)
            intent.last_seq = record.seq

    # -- satisfaction ----------------------------------------------------

    def resolve_satisfaction(
            self, intent: IntentState, *,
            outcome_ref: str | None = None) -> tuple[ReservedRecord,
                                                    ReservedRecord]:
        """Find an eligible Outcome and its approved Decision.

        The current intent revision must match.  A valid outcome carries at
        least one evidence reference and points at an approved decision for
        the same revision.  S1-B records those immutable records; this method
        only checks the binding.
        """
        current = intent.revision_hash
        if outcome_ref is not None:
            outcome = self.outcomes.get(outcome_ref)
            if outcome is None or outcome.intent_id != intent.intent_id:
                raise OutcomeTransitionError(
                    f"outcome {outcome_ref!r} does not exist for this intent")
        else:
            candidates = self.outcomes_for(
                intent.intent_id, revision_hash=current, status="satisfied")
            if not candidates:
                raise OutcomeTransitionError(
                    "satisfied requires a recorded outcome for the current "
                    "revision")
            outcome = candidates[-1]
        payload = outcome.payload
        if payload.get("status") != "satisfied":
            raise OutcomeTransitionError(
                "satisfied requires an outcome whose status is 'satisfied'")
        if payload.get("revision_hash") != current:
            raise OutcomeStaleError(
                "the outcome is bound to a different intent revision")
        evidence = payload.get("evidence_refs") or []
        if not evidence:
            raise OutcomeTransitionError(
                "satisfied requires recorded evidence references")
        decision_ref = payload.get("decision_ref")
        if not isinstance(decision_ref, str) or not decision_ref:
            raise OutcomeTransitionError(
                "satisfied requires an approved decision reference")
        decision = self.decisions.get(decision_ref)
        if decision is None:
            raise OutcomeTransitionError(
                f"decision {decision_ref!r} does not exist")
        if decision.intent_id != intent.intent_id:
            raise OutcomeTransitionError(
                "the decision belongs to a different intent")
        if decision.payload.get("revision_hash") != current:
            raise OutcomeStaleError(
                "the decision is bound to a different intent revision")
        if decision.payload.get("decision") != "approved":
            raise OutcomeTransitionError(
                "satisfied requires an approved decision")
        return outcome, decision


# ---------------------------------------------------------------------------
# Apply handlers
# ---------------------------------------------------------------------------


def _payload(record: R.OutcomeRecord, schema: str) -> dict[str, Any]:
    return R._payload_schema(record.payload, schema)


def _at(record: R.OutcomeRecord) -> int:
    return record.timestamp


def _apply_intent_submitted(state: OutcomeState,
                            record: R.OutcomeRecord) -> None:
    payload = _payload(record, R.REVISION_SCHEMA)
    if record.intent_id in state.intents:
        raise OutcomeConflictError(
            f"intent {record.intent_id!r} already exists")
    revision = R.IntentRevision.from_payload(
        payload, record_id=record.record_id, created_at=record.timestamp)
    if revision.intent_id != record.intent_id:
        raise OutcomeValidationError(
            "revision intent_id does not match the envelope")
    if revision.revision != 1 or revision.parent is not None:
        raise OutcomeValidationError(
            "the first intent revision must be 1 with a null parent")
    intent = IntentState(
        intent_id=record.intent_id,
        principal=revision.principal,
        state="accepted",
        created_at=record.timestamp,
        updated_at=record.timestamp,
        last_seq=record.seq,
        revisions=[revision],
        conversation_ref=revision.conversation_ref,
    )
    state.intents[intent.intent_id] = intent


def _apply_intent_revised(state: OutcomeState,
                          record: R.OutcomeRecord) -> None:
    payload = _payload(record, R.REVISION_SCHEMA)
    intent = state.intent(record.intent_id)
    if intent.state in R.TERMINAL_INTENT_STATES:
        raise OutcomeTransitionError(
            f"intent is {intent.state}; a revision cannot follow")
    revision = R.IntentRevision.from_payload(
        payload, record_id=record.record_id, created_at=record.timestamp)
    current = intent.current_revision
    if revision.intent_id != intent.intent_id:
        raise OutcomeValidationError(
            "revision intent_id does not match the envelope")
    if revision.revision != current.revision + 1:
        raise OutcomeValidationError(
            f"revision must be {current.revision + 1}, "
            f"got {revision.revision}")
    if revision.parent != current.revision_hash:
        raise OutcomeStaleError(
            "revision parent does not match the current revision")
    if intent.revision_for(revision.revision_hash) is not None:
        raise OutcomeConflictError("revision already exists")
    intent.revisions.append(revision)


def _apply_intent_transitioned(state: OutcomeState,
                               record: R.OutcomeRecord) -> None:
    payload = _payload(record, R.INTENT_TRANSITION_SCHEMA)
    intent = state.intent(record.intent_id)
    from_state = R.text(payload.get("from_state"), "from_state",
                        limit=R.MAX_SHORT)
    to_state = R.text(payload.get("to_state"), "to_state", limit=R.MAX_SHORT)
    assert from_state is not None and to_state is not None
    if from_state != intent.state:
        raise OutcomeStaleError(
            f"intent is {intent.state}, not {from_state}")
    allowed = R.INTENT_TRANSITIONS.get(from_state, frozenset())
    if to_state not in allowed:
        raise OutcomeTransitionError(
            f"intent cannot move from {from_state} to {to_state}")
    outcome_ref = payload.get("outcome_ref")
    decision_ref = payload.get("decision_ref")
    if to_state == "satisfied":
        outcome, decision = state.resolve_satisfaction(
            intent, outcome_ref=outcome_ref if isinstance(outcome_ref, str)
            else None)
        if outcome_ref != outcome.record_id:
            raise OutcomeValidationError(
                "the transition outcome_ref does not name the resolved "
                "outcome")
        if decision_ref != decision.record_id:
            raise OutcomeValidationError(
                "the transition decision_ref does not name the resolved "
                "decision")
        intent.outcome_ref = outcome.record_id
        intent.decision_ref = decision.record_id
    elif outcome_ref is not None:
        outcome = state.outcomes.get(str(outcome_ref))
        if outcome is None or outcome.intent_id != intent.intent_id:
            raise OutcomeValidationError(
                "the transition names an unknown outcome")
        intent.outcome_ref = outcome.record_id
    intent.state = to_state


def _apply_work_added(state: OutcomeState, record: R.OutcomeRecord) -> None:
    payload = _payload(record, R.WORK_SCHEMA)
    intent = state.intent(record.intent_id)
    work_id = R.identifier(payload.get("work_id"), "work_id")
    revision_hash = R._hash(payload.get("revision_hash"), "revision_hash")
    revision = state.revision(intent.intent_id, revision_hash)
    revision_number = R.integer(payload.get("revision"), "revision", minimum=1)
    if revision_number != revision.revision:
        raise OutcomeValidationError(
            "work revision number does not match its revision hash")
    state_value = R.text(payload.get("state"), "state", limit=64)
    if state_value != "queued":
        raise OutcomeValidationError("new work must be queued")
    if work_id in state.work:
        raise OutcomeConflictError(f"work item {work_id!r} already exists")
    title = R.text(payload.get("title"), "title", limit=R.MAX_SHORT)
    kind = R.text(payload.get("kind"), "kind", limit=R.MAX_SHORT)
    assert title is not None and kind is not None
    project = R.ref(payload.get("project_ref"), "project_ref",
                    required=False)
    contexts = R.ref_list(payload.get("context_refs"), "context_refs")
    details = R.canonical_payload(payload.get("details") or {}, "details")
    state.work[work_id] = WorkItem(
        work_id=work_id, intent_id=intent.intent_id,
        revision=revision.revision, revision_hash=revision.revision_hash,
        title=title, kind=kind, state="queued", project_ref=project,
        context_refs=contexts, details=details, created_at=_at(record),
        updated_at=_at(record), last_seq=record.seq)


def _apply_work_transitioned(state: OutcomeState,
                             record: R.OutcomeRecord) -> None:
    payload = _payload(record, R.WORK_TRANSITION_SCHEMA)
    intent = state.intent(record.intent_id)
    work_id = R.identifier(payload.get("work_id"), "work_id")
    item = state.work.get(work_id)
    if item is None or item.intent_id != intent.intent_id:
        raise OutcomeNotFoundError(f"work item {work_id!r} does not exist")
    from_state = R.text(payload.get("from_state"), "from_state",
                        limit=64)
    to_state = R.text(payload.get("to_state"), "to_state", limit=64)
    assert from_state is not None and to_state is not None
    if from_state != item.state:
        raise OutcomeStaleError(
            f"work {work_id!r} is {item.state}, not {from_state}")
    allowed = R.WORK_TRANSITIONS.get(from_state, frozenset())
    if to_state not in allowed:
        raise OutcomeTransitionError(
            f"work cannot move from {from_state} to {to_state}")
    reason = R.detail_text(payload.get("reason"), "reason")
    reference = R.ref(
        payload.get("revision_ref"), "revision_ref", required=False)
    if record.event == R.WORK_STALE:
        if from_state != "queued" or to_state != "stale":
            raise OutcomeTransitionError(
                "work.stale may only move queued work to stale")
        if not reason:
            raise OutcomeValidationError(
                "work.stale requires a reason")
        item.stale_reason = reason
        item.stale_reference = reference
    elif to_state == "stale":
        raise OutcomeTransitionError(
            "stale work must be recorded with a work.stale event")
    item.state = to_state
    item.updated_at = max(item.updated_at, _at(record))
    item.last_seq = record.seq
    if to_state in R.WORK_TERMINAL_STATES:
        item.closed_at = _at(record)


def _check_claimable(state: OutcomeState, intent: IntentState,
                     item: WorkItem) -> None:
    if intent.state not in ("accepted", "running"):
        raise OutcomeTransitionError(
            f"intent is {intent.state}; no new attempt may start")
    if item.state not in ("queued", "running"):
        raise OutcomeTransitionError(
            f"work {item.work_id!r} is {item.state} and cannot accept an "
            "attempt")
    if item.revision_hash != intent.revision_hash:
        raise OutcomeStaleError(
            "the work item is bound to a superseded intent revision")


def _attempt_payload(record: R.OutcomeRecord) -> dict[str, Any]:
    return _payload(record, R.ATTEMPT_SCHEMA)


def _apply_attempt_queued(state: OutcomeState,
                          record: R.OutcomeRecord) -> None:
    payload = _attempt_payload(record)
    intent = state.intent(record.intent_id)
    if intent.state not in ("accepted", "running"):
        raise OutcomeTransitionError(
            f"intent is {intent.state}; an attempt cannot be queued")
    attempt_id = R.identifier(payload.get("attempt_id"), "attempt_id")
    if attempt_id in state.attempts:
        raise OutcomeConflictError(f"attempt {attempt_id!r} already exists")
    work_id = R.identifier(payload.get("work_id"), "work_id")
    item = state.work.get(work_id)
    if item is None or item.intent_id != intent.intent_id:
        raise OutcomeNotFoundError(f"work item {work_id!r} does not exist")
    _check_claimable(state, intent, item)
    revision_hash = R._hash(payload.get("revision_hash"), "revision_hash")
    revision = state.revision(intent.intent_id, revision_hash)
    if revision.revision_hash != item.revision_hash:
        raise OutcomeStaleError(
            "the attempt revision does not match the work item")
    revision_number = R.integer(payload.get("revision"), "revision", minimum=1)
    if revision_number != revision.revision:
        raise OutcomeValidationError(
            "attempt revision number does not match its revision hash")
    attempt_state = R.text(payload.get("state"), "state", limit=64)
    if attempt_state != "queued":
        raise OutcomeValidationError("a queued attempt must be queued")
    state.attempts[attempt_id] = Attempt(
        attempt_id=attempt_id, intent_id=intent.intent_id,
        work_id=work_id, revision=revision.revision,
        revision_hash=revision.revision_hash, state="queued", outcome=None,
        worker_ref=R.ref(payload.get("worker_ref"), "worker_ref",
                         required=False),
        assignment_ref=R.ref(payload.get("assignment_ref"),
                             "assignment_ref", required=False),
        baseline_ref=R.ref(payload.get("baseline_ref"), "baseline_ref",
                           required=False),
        lease_generation=R.lease_generation(payload.get("lease_generation")),
        lease_ref=R.ref(payload.get("lease_ref"), "lease_ref", required=False),
        created_at=_at(record), started_at=None, finished_at=None,
        detail=None, evidence_refs=[], last_seq=record.seq)


def _apply_attempt_started(state: OutcomeState,
                           record: R.OutcomeRecord) -> None:
    payload = _attempt_payload(record)
    intent = state.intent(record.intent_id)
    if intent.state not in ("accepted", "running"):
        raise OutcomeTransitionError(
            f"intent is {intent.state}; an attempt cannot start")
    attempt_id = R.identifier(payload.get("attempt_id"), "attempt_id")
    work_id = R.identifier(payload.get("work_id"), "work_id")
    item = state.work.get(work_id)
    if item is None or item.intent_id != intent.intent_id:
        raise OutcomeNotFoundError(f"work item {work_id!r} does not exist")
    _check_claimable(state, intent, item)
    revision_hash = R._hash(payload.get("revision_hash"), "revision_hash")
    revision = state.revision(intent.intent_id, revision_hash)
    if revision.revision_hash != item.revision_hash:
        raise OutcomeStaleError(
            "the attempt revision does not match the work item")
    revision_number = R.integer(payload.get("revision"), "revision", minimum=1)
    if revision_number != revision.revision:
        raise OutcomeValidationError(
            "attempt revision number does not match its revision hash")
    previous = payload.get("previous_state")
    if previous not in (None, "queued"):
        raise OutcomeValidationError(
            "attempt previous_state must be null or 'queued'")
    attempt = state.attempts.get(attempt_id)
    if previous is None:
        if attempt is not None:
            raise OutcomeConflictError(
                f"attempt {attempt_id!r} already exists")
        if any(other.work_id == work_id and other.state == "running"
               for other in state.attempts.values()):
            raise OutcomeTransitionError(
                "another attempt is already running for this work item")
        attempt = Attempt(
            attempt_id=attempt_id, intent_id=intent.intent_id,
            work_id=work_id, revision=revision.revision,
            revision_hash=revision.revision_hash, state="queued",
            outcome=None,
            worker_ref=None, assignment_ref=None, baseline_ref=None,
            lease_generation=R.lease_generation(
                payload.get("lease_generation")),
            lease_ref=None, created_at=_at(record), started_at=None,
            finished_at=None, detail=None, evidence_refs=[],
            last_seq=record.seq)
        state.attempts[attempt_id] = attempt
    else:
        if attempt is None:
            raise OutcomeNotFoundError(
                f"queued attempt {attempt_id!r} does not exist")
        if attempt.state != "queued":
            raise OutcomeTransitionError(
                f"attempt {attempt_id!r} is {attempt.state}, not queued")
        if attempt.work_id != work_id:
            raise OutcomeValidationError(
                "the started attempt does not belong to this work item")
        if attempt.revision_hash != revision.revision_hash:
            raise OutcomeStaleError(
                "the attempt revision does not match the work item")
        if any(other.attempt_id != attempt_id and other.work_id == work_id
               and other.state == "running"
               for other in state.attempts.values()):
            raise OutcomeTransitionError(
                "another attempt is already running for this work item")
    attempt.state = "running"
    attempt.worker_ref = R.ref(
        payload.get("worker_ref"), "worker_ref", required=False)
    attempt.assignment_ref = R.ref(
        payload.get("assignment_ref"), "assignment_ref", required=False)
    attempt.baseline_ref = R.ref(
        payload.get("baseline_ref"), "baseline_ref", required=False)
    attempt.lease_generation = R.lease_generation(
        payload.get("lease_generation"))
    attempt.lease_ref = R.ref(
        payload.get("lease_ref"), "lease_ref", required=False)
    attempt.started_at = _at(record)
    attempt.last_seq = record.seq
    if item.state == "queued":
        item.state = "running"
        item.updated_at = max(item.updated_at, _at(record))
        item.last_seq = record.seq
    if intent.state == "accepted":
        intent.state = "running"


def _apply_attempt_finished(state: OutcomeState,
                            record: R.OutcomeRecord) -> None:
    payload = _attempt_payload(record)
    intent = state.intent(record.intent_id)
    attempt_id = R.identifier(payload.get("attempt_id"), "attempt_id")
    attempt = state.attempts.get(attempt_id)
    if attempt is None or attempt.intent_id != intent.intent_id:
        raise OutcomeNotFoundError(f"attempt {attempt_id!r} does not exist")
    if attempt.state != "running":
        raise OutcomeTransitionError(
            f"attempt {attempt_id!r} is {attempt.state}; only a running "
            "attempt can finish")
    outcome = R.text(payload.get("outcome"), "outcome", limit=64)
    if outcome not in R.ATTEMPT_OUTCOMES:
        raise OutcomeValidationError(
            f"attempt outcome must be one of {R.ATTEMPT_OUTCOMES}")
    terminal = R.text(payload.get("state"), "state", limit=64)
    if terminal != "terminal":
        raise OutcomeValidationError("a finished attempt must be terminal")
    payload_work = R.identifier(payload.get("work_id"), "work_id")
    if payload_work != attempt.work_id:
        raise OutcomeValidationError(
            "the finished attempt does not match its work item")
    payload_revision = R._hash(
        payload.get("revision_hash"), "revision_hash")
    if payload_revision != attempt.revision_hash:
        raise OutcomeStaleError(
            "the finished attempt does not match its revision")
    payload_number = R.integer(
        payload.get("revision"), "revision", minimum=1)
    if payload_number != attempt.revision:
        raise OutcomeValidationError(
            "the finished attempt revision number is inconsistent")
    finishing_generation = R.lease_generation(
        payload.get("lease_generation"))
    if finishing_generation != attempt.lease_generation:
        raise OutcomeStaleError(
            "the lease generation does not match the running attempt")
    attempt.state = "terminal"
    attempt.outcome = outcome
    attempt.finished_at = _at(record)
    attempt.last_seq = record.seq
    attempt.finish_record_id = record.record_id
    attempt.detail = R.detail_text(payload.get("detail"), "detail")
    attempt.evidence_refs = R.ref_list(
        payload.get("evidence_refs"), "evidence_refs",
        limit=R.MAX_EVIDENCE_REFS)
    item = state.work.get(attempt.work_id)
    if item is not None and item.state in ("queued", "running", "waiting"):
        target = R.ATTEMPT_WORK_STATE[outcome]
        if target in R.WORK_TRANSITIONS.get(item.state, frozenset()):
            item.state = target
            item.updated_at = max(item.updated_at, _at(record))
            item.last_seq = record.seq
            if target in R.WORK_TERMINAL_STATES:
                item.closed_at = _at(record)


def _apply_event_appended(state: OutcomeState,
                          record: R.OutcomeRecord) -> None:
    payload = _payload(record, R.EVENT_SCHEMA)
    state.intent(record.intent_id)
    R.event_name(payload.get("kind"), "kind")
    R.canonical_payload(payload.get("data") or {}, "data")


def _apply_gate_created(state: OutcomeState,
                        record: R.OutcomeRecord) -> None:
    payload = _payload(record, R.GATE_SCHEMA)
    intent = state.intent(record.intent_id)
    gate_id = R.identifier(payload.get("gate_id"), "gate_id")
    if gate_id in state.gates:
        raise OutcomeConflictError(f"gate {gate_id!r} already exists")
    revision_hash = R._hash(payload.get("revision_hash"), "revision_hash")
    state.revision(intent.intent_id, revision_hash)
    R.boolean(payload.get("required"), "required")
    R.ref(payload.get("action_digest"), "action_digest", required=False)
    R.ref(payload.get("target"), "target", required=False)
    R.ref(payload.get("expected_base"), "expected_base", required=False)
    R.text(payload.get("policy_version"), "policy_version",
           limit=R.MAX_SHORT, required=False)
    if payload.get("expires_at") is not None:
        R.timestamp(payload.get("expires_at"), "expires_at")
    R.ref_list(payload.get("evidence_refs"), "evidence_refs",
               limit=R.MAX_EVIDENCE_REFS)
    R.detail_text(payload.get("detail"), "detail")
    state.gates[gate_id] = _reserved(record, payload)


def _apply_decision_recorded(state: OutcomeState,
                             record: R.OutcomeRecord) -> None:
    payload = _payload(record, R.DECISION_SCHEMA)
    intent = state.intent(record.intent_id)
    gate_id = R.identifier(payload.get("gate_id"), "gate_id")
    gate = state.gates.get(gate_id)
    if gate is None or gate.intent_id != intent.intent_id:
        raise OutcomeNotFoundError(f"gate {gate_id!r} does not exist")
    revision_hash = R._hash(payload.get("revision_hash"), "revision_hash")
    state.revision(intent.intent_id, revision_hash)
    if revision_hash != gate.payload.get("revision_hash"):
        raise OutcomeStaleError(
            "the decision revision does not match its gate")
    decision = R.text(payload.get("decision"), "decision", limit=64)
    if decision not in R.DECISIONS:
        raise OutcomeValidationError(
            f"decision must be one of {R.DECISIONS}")
    for name in ("action_digest", "target", "expected_base"):
        value = R.ref(payload.get(name), name, required=False)
        gate_value = gate.payload.get(name)
        if value is not None and gate_value is not None and value != gate_value:
            raise OutcomeStaleError(
                f"the decision {name} does not match its gate")
    R.text(payload.get("policy_version"), "policy_version",
           limit=R.MAX_SHORT, required=False)
    R.ref_list(payload.get("evidence_refs"), "evidence_refs",
               limit=R.MAX_EVIDENCE_REFS)
    R.detail_text(payload.get("detail"), "detail")
    state.decisions[record.record_id] = _reserved(record, payload)


def _apply_evidence_recorded(state: OutcomeState,
                             record: R.OutcomeRecord) -> None:
    payload = _payload(record, R.EVIDENCE_SCHEMA)
    intent = state.intent(record.intent_id)
    revision_hash = R._hash(payload.get("revision_hash"), "revision_hash")
    state.revision(intent.intent_id, revision_hash)
    source = R.text(payload.get("source"), "source", limit=R.MAX_SHORT)
    assert source is not None
    freshness = R.text(payload.get("freshness"), "freshness", limit=64)
    if freshness not in R.EVIDENCE_FRESHNESS:
        raise OutcomeValidationError(
            f"evidence freshness must be one of {sorted(R.EVIDENCE_FRESHNESS)}")
    R.ref(payload.get("artifact_digest"), "artifact_digest", required=False)
    R.text(payload.get("method"), "method", limit=R.MAX_SHORT,
           required=False)
    R.text(payload.get("coverage"), "coverage", limit=R.MAX_SHORT,
           required=False)
    R.ref_list(payload.get("refs"), "refs")
    R.detail_text(payload.get("detail"), "detail")
    state.evidence[record.record_id] = _reserved(record, payload)


def _apply_effect_recorded(state: OutcomeState,
                           record: R.OutcomeRecord) -> None:
    payload = _payload(record, R.EFFECT_SCHEMA)
    intent = state.intent(record.intent_id)
    attempt_id = R.identifier(payload.get("attempt_id"), "attempt_id")
    attempt = state.attempts.get(attempt_id)
    if attempt is None or attempt.intent_id != intent.intent_id:
        raise OutcomeNotFoundError(f"attempt {attempt_id!r} does not exist")
    revision_hash = R._hash(payload.get("revision_hash"), "revision_hash")
    if revision_hash != attempt.revision_hash:
        raise OutcomeStaleError(
            "the effect revision does not match its attempt")
    effect_state = R.text(payload.get("state"), "state", limit=64)
    if effect_state not in R.EFFECT_STATES:
        raise OutcomeValidationError(
            f"effect state must be one of {R.EFFECT_STATES}")
    R.text(payload.get("effect_key"), "effect_key", limit=R.MAX_REF)
    R.text(payload.get("target"), "target", limit=R.MAX_REF)
    R.ref(payload.get("expected_base"), "expected_base", required=False)
    if payload.get("observed_receipt") is not None:
        R.canonical_payload(payload.get("observed_receipt"),
                            "observed_receipt")
    R.ref_list(payload.get("evidence_refs"), "evidence_refs",
               limit=R.MAX_EVIDENCE_REFS)
    R.detail_text(payload.get("detail"), "detail")
    generation = R.lease_generation(payload.get("lease_generation"))
    if generation != attempt.lease_generation:
        raise OutcomeStaleError(
            "the effect lease generation does not match its attempt")
    state.effects[record.record_id] = _reserved(record, payload)


def _apply_outcome_recorded(state: OutcomeState,
                            record: R.OutcomeRecord) -> None:
    payload = _payload(record, R.OUTCOME_SCHEMA)
    intent = state.intent(record.intent_id)
    revision_hash = R._hash(payload.get("revision_hash"), "revision_hash")
    state.revision(intent.intent_id, revision_hash)
    status = R.text(payload.get("status"), "status", limit=64)
    if status not in R.OUTCOME_STATUSES:
        raise OutcomeValidationError(
            f"outcome status must be one of {R.OUTCOME_STATUSES}")
    R.ref_list(payload.get("evidence_refs"), "evidence_refs",
               limit=R.MAX_EVIDENCE_REFS)
    R.criteria_list(payload.get("criteria_satisfied"), "criteria_satisfied")
    decision_ref = R.ref(
        payload.get("decision_ref"), "decision_ref", required=False)
    R.text(payload.get("summary"), "summary", limit=R.MAX_DETAIL,
           required=False)
    R.detail_text(payload.get("detail"), "detail")
    if decision_ref is not None:
        decision = state.decisions.get(decision_ref)
        if decision is None or decision.intent_id != intent.intent_id:
            raise OutcomeValidationError(
                f"decision {decision_ref!r} does not exist")
        if decision.payload.get("revision_hash") != revision_hash:
            raise OutcomeStaleError(
                "the outcome decision is bound to a different revision")
    state.outcomes[record.record_id] = _reserved(record, payload)


def _reserved(record: R.OutcomeRecord,
              payload: Mapping[str, Any]) -> ReservedRecord:
    return ReservedRecord(
        record_id=record.record_id, seq=record.seq, timestamp=record.timestamp,
        actor=record.actor, intent_id=record.intent_id,
        payload=dict(payload))


def _reserved_for(bucket: Mapping[str, ReservedRecord],
                  intent_id: str) -> list[ReservedRecord]:
    return sorted(
        (item for item in bucket.values() if item.intent_id == intent_id),
        key=lambda item: item.seq)


_HANDLERS = {
    R.INTENT_SUBMITTED: _apply_intent_submitted,
    R.INTENT_REVISED: _apply_intent_revised,
    R.INTENT_TRANSITIONED: _apply_intent_transitioned,
    R.WORK_ADDED: _apply_work_added,
    R.WORK_TRANSITIONED: _apply_work_transitioned,
    R.WORK_STALE: _apply_work_transitioned,
    R.ATTEMPT_QUEUED: _apply_attempt_queued,
    R.ATTEMPT_STARTED: _apply_attempt_started,
    R.ATTEMPT_FINISHED: _apply_attempt_finished,
    R.EVENT_APPENDED: _apply_event_appended,
    R.GATE_CREATED: _apply_gate_created,
    R.DECISION_RECORDED: _apply_decision_recorded,
    R.EVIDENCE_RECORDED: _apply_evidence_recorded,
    R.EFFECT_RECORDED: _apply_effect_recorded,
    R.OUTCOME_RECORDED: _apply_outcome_recorded,
}
