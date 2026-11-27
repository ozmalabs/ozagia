"""Durable append-only outcome journal and lifecycle operations.

The journal lives at ``<state>/outcomes/journal.jsonl`` by default and is a
single canonical JSON record per line in ``ozagia.outcomes.record.v1``.  Writes
hold an ``fcntl`` lock, append with ``fsync`` and fail closed on interior
damage.  A torn final line is truncated on recovery and before the next write.
Replay materializes :class:`OutcomeState` and rejects transition or hash-chain
damage.

The store never writes a code graph, goals, sessions, evidence or edits.  It
touches only its own directory and is deliberately separate from
``DevMemory`` (rebuildable code index), ``DevSession`` (presentation receipts)
and ``evidence_store`` (goal/run/test evidence).  Effect adapters, gate
evaluation and the versioned view are S1-B/C.
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from ozagia.config import state_path
from ozagia.core import contracts as C

from . import records as R
from . import state as S
from .errors import (
    OutcomeBoundsError,
    OutcomeConflictError,
    OutcomeIntegrityError,
    OutcomeLockError,
    OutcomeNotFoundError,
    OutcomeStaleError,
    OutcomeStoreError,
    OutcomeTransitionError,
    OutcomeUnsupportedError,
    OutcomeValidationError,
)

OUTCOMES_DIR = "outcomes"
JOURNAL_NAME = "journal.jsonl"
LOCK_NAME = ".lock"

LOCK_TIMEOUT_S = 30.0
DEFAULT_FOLLOW = 100
MAX_FOLLOW = 1000
DEFAULT_EVENTS = 50
MAX_EVENTS = 500
DEFAULT_ACTIVE = 100
MAX_ACTIVE = 500

_INHERIT = object()
_THREAD_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Public paths
# ---------------------------------------------------------------------------


def outcomes_dir(root: str | Path) -> Path:
    """The default outcome store directory for ``root``."""
    return state_path(root, OUTCOMES_DIR)


def journal_path(root: str | Path) -> Path:
    return outcomes_dir(root) / JOURNAL_NAME


# ---------------------------------------------------------------------------
# Journal scan and recovery
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _JournalScan:
    records: tuple[R.OutcomeRecord, ...]
    torn_bytes: int
    complete_bytes: int
    exists: bool

    @property
    def cursor(self) -> int:
        return self.records[-1].seq if self.records else 0

    @property
    def state(self) -> str:
        if not self.exists:
            return "absent"
        return "partial" if self.torn_bytes else "current"


def _scan_journal(path: Path) -> _JournalScan:
    """Read and structurally validate the journal without writing.

    Complete lines are canonical, individually hashed and chained.  Bytes after
    the final newline are a torn tail and are reported, not served.  A
    malformed complete line, non-canonical text or broken hash fails closed.
    """
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return _JournalScan((), 0, 0, False)
    except OSError as exc:
        raise OutcomeStoreError(f"cannot read the outcome journal: {exc}") from exc
    last = data.rfind(b"\n")
    complete_bytes = last + 1 if last >= 0 else 0
    complete = data[:complete_bytes]
    torn_bytes = len(data) - complete_bytes
    records: list[R.OutcomeRecord] = []
    if complete:
        raw_lines = complete.split(b"\n")[:-1]
        for index, raw in enumerate(raw_lines, start=1):
            if not raw:
                raise OutcomeIntegrityError(
                    f"line {index} of the outcome journal is empty")
            if len(raw) > R.MAX_RECORD_BYTES:
                raise OutcomeIntegrityError(
                    f"line {index} exceeds {R.MAX_RECORD_BYTES} bytes")
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise OutcomeIntegrityError(
                    f"line {index} is not UTF-8: {exc}") from exc
            try:
                parsed = json.loads(text)
            except ValueError as exc:
                raise OutcomeIntegrityError(
                    f"line {index} is not valid JSON: {exc}") from exc
            if not isinstance(parsed, dict):
                raise OutcomeIntegrityError(
                    f"line {index} is not a JSON object")
            try:
                canonical = C.canonical_json(parsed)
            except C.ContractError as exc:
                raise OutcomeIntegrityError(
                    f"line {index} is not canonical JSON: {exc}") from exc
            if text != canonical:
                raise OutcomeIntegrityError(
                    f"line {index} is not in canonical form")
            record = R.OutcomeRecord.from_dict(parsed)
            if records:
                previous = records[-1]
                if record.seq != previous.seq + 1:
                    raise OutcomeIntegrityError(
                        f"outcome journal sequence gap at line {index}: "
                        f"{previous.seq} then {record.seq}")
                if record.prev_hash != previous.record_hash:
                    raise OutcomeIntegrityError(
                        f"outcome record {record.record_id} has a broken "
                        "previous hash")
            else:
                if record.seq < 1:
                    raise OutcomeIntegrityError(
                        "the outcome journal starts below sequence 1")
                if record.seq == 1 and record.prev_hash is not None:
                    raise OutcomeIntegrityError(
                        "the first outcome record must have a null prev_hash")
            records.append(record)
    return _JournalScan(
        tuple(records), torn_bytes=torn_bytes, complete_bytes=complete_bytes,
        exists=True)


def _truncate_locked(path: Path, complete_bytes: int) -> None:
    try:
        with open(path, "r+b") as handle:
            handle.truncate(complete_bytes)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        raise OutcomeStoreError(
            f"cannot truncate the torn outcome journal tail: {exc}") from exc


def _append_locked(path: Path, records: Iterable[R.OutcomeRecord]) -> None:
    data = "".join(
        C.canonical_json(record.to_dict()) + "\n"
        for record in records).encode("utf-8")
    if not data:
        return
    try:
        descriptor = os.open(
            path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            view = memoryview(data)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OutcomeStoreError(
                        "cannot append to the outcome journal: short write")
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise OutcomeStoreError(
            f"cannot append to the outcome journal: {exc}") from exc


@contextlib.contextmanager
def _locked(directory: Path, timeout_s: float = LOCK_TIMEOUT_S):
    try:
        import fcntl
    except ImportError:
        raise OutcomeLockError(
            "the outcome store needs fcntl file locking, which this platform "
            "does not provide") from None
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise OutcomeStoreError(
            f"cannot create the outcome store directory: {exc}") from exc
    deadline = time.monotonic() + timeout_s
    with _THREAD_LOCK, open(directory / LOCK_NAME, "a+") as handle:
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise OutcomeLockError(
                        "the outcome store is locked by another writer "
                        f"(waited {timeout_s:.0f} s)") from None
                time.sleep(0.05)
            except OSError as exc:
                raise OutcomeLockError(
                    f"cannot lock the outcome store: {exc}") from exc
        try:
            yield
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(handle, fcntl.LOCK_UN)


# ---------------------------------------------------------------------------
# Write internals
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Pending:
    event: str
    intent_id: str
    payload: dict[str, Any]
    actor: str
    idempotency_key: str | None
    correlation_id: str | None
    causation_id: str | None
    request_hash: str


@dataclass(frozen=True)
class _WriteResult:
    records: list[R.OutcomeRecord]
    duplicate: bool
    state: S.OutcomeState
    truncated_bytes: int


def _auto_key(event: str, intent_id: str,
              payload: Mapping[str, Any]) -> str:
    digest = C.content_hash({
        "event": event, "intent_id": intent_id, "payload": dict(payload),
    })
    return f"auto-{digest}"


def _new_id(prefix: str, key: str | None) -> str:
    if key is not None:
        suffix = C.content_hash({"prefix": prefix, "key": key})[:16]
    else:
        suffix = secrets.token_hex(8)
    return f"{prefix}-{suffix}"


def _entity_id(value: Any, prefix: str, key: str | None) -> str:
    if value is None:
        return R.identifier(_new_id(prefix, key), f"{prefix}_id")
    return R.identifier(value, f"{prefix}_id")


def _request_key(event: str, intent_id: str,
                 fields: Mapping[str, Any]) -> str:
    return R.request_hash(event, intent_id, fields)


class OutcomeStore:
    """One logical store over the canonical outcome journal."""

    def __init__(self, root: str | Path, *, state_dir: str | Path | None = None,
                 actor: str = "operator",
                 clock: Callable[[], int] | None = None) -> None:
        self.root = _resolve_root(root)
        if state_dir is None:
            directory = state_path(self.root, OUTCOMES_DIR)
        else:
            candidate = Path(state_dir).expanduser()
            directory = candidate if candidate.is_absolute() else self.root / candidate
        self.directory = Path(directory).resolve()
        self.actor = R.text(actor, "actor", limit=R.MAX_ACTOR)
        assert self.actor is not None
        self._clock = clock or (lambda: time.time_ns() // 1_000_000)

    # -- paths and low-level reads --------------------------------------

    @property
    def journal(self) -> Path:
        return self.directory / JOURNAL_NAME

    @property
    def lock_path(self) -> Path:
        return self.directory / LOCK_NAME

    def _now(self, now_ms: int | None) -> int:
        return self._clock() if now_ms is None else R.timestamp(now_ms, "now_ms")

    def _scan(self) -> _JournalScan:
        return _scan_journal(self.journal)

    def _load_complete_locked(self) -> tuple[_JournalScan, int]:
        scan = self._scan()
        if not scan.torn_bytes:
            return scan, 0
        _truncate_locked(self.journal, scan.complete_bytes)
        recovered = self._scan()
        return recovered, scan.torn_bytes

    def materialize(self) -> S.OutcomeState:
        """Read the journal and return validated materialized state."""
        return S.replay_records(self._scan().records)

    def recover(self) -> dict[str, Any]:
        """Truncate a torn tail and validate the complete journal.

        A malformed interior line or broken hash chain raises
        :class:`OutcomeIntegrityError`; nothing is silently discarded.
        """
        if not self.journal.exists():
            return {
                "state": "absent",
                "records": 0,
                "cursor": 0,
                "truncated_bytes": 0,
                "recovered": False,
                "path": self.journal.as_posix(),
            }
        with _locked(self.directory):
            scan, truncated = self._load_complete_locked()
            state = S.replay_records(scan.records)
        return {
            "state": "recovered" if truncated else "current",
            "records": len(scan.records),
            "cursor": state.cursor,
            "truncated_bytes": truncated,
            "recovered": bool(truncated),
            "path": self.journal.as_posix(),
        }

    # -- write execution -------------------------------------------------

    def _request_matches(self, record: R.OutcomeRecord,
                         request_hash: str | None) -> bool:
        stored = record.payload.get("request_hash")
        return bool(request_hash and isinstance(stored, str)
                    and stored == request_hash)

    def _execute(self, build: Callable[[S.OutcomeState], Any], *,
                 idempotency_key: str | None = None,
                 request_hash: str | None = None,
                 now_ms: int | None = None) -> _WriteResult:
        moment = self._now(now_ms)
        key = (R.idempotency_key(idempotency_key)
               if idempotency_key is not None else None)
        with _locked(self.directory):
            scan, truncated = self._load_complete_locked()
            state = S.replay_records(scan.records)
            if key is not None:
                existing = state.idempotency.get(key)
                if existing is not None:
                    if self._request_matches(existing, request_hash):
                        return _WriteResult([existing], True, state, truncated)
                    raise OutcomeConflictError(
                        f"idempotency key {key!r} is already used by a "
                        "different request")
            built = build(state)
            if built is None:
                pendings: list[_Pending] = []
            elif isinstance(built, _Pending):
                pendings = [built]
            else:
                pendings = list(built)
            if not pendings:
                return _WriteResult([], False, state, truncated)
            prepared: list[_Pending] = []
            for index, pending in enumerate(pendings):
                event = R.event_name(pending.event)
                if event not in R.EVENT_TYPES:
                    raise OutcomeUnsupportedError(
                        f"unknown outcome event {event!r}")
                intent_id = R.identifier(pending.intent_id, "intent_id")
                actor = R.text(
                    pending.actor, "actor", limit=R.MAX_ACTOR)
                assert actor is not None
                payload = R.canonical_payload(pending.payload)
                pending_hash = R.require_request_hash(payload)
                resolved_key = None
                if index == 0 and key is not None:
                    resolved_key = key
                elif pending.idempotency_key is not None:
                    resolved_key = R.idempotency_key(
                        pending.idempotency_key)
                else:
                    resolved_key = _auto_key(event, intent_id, payload)
                if pending.request_hash != pending_hash:
                    raise OutcomeValidationError(
                        "internal request fingerprint mismatch")
                prepared.append(_Pending(
                    event=event, intent_id=intent_id, payload=payload,
                    actor=actor, idempotency_key=resolved_key,
                    correlation_id=R.ref(
                        pending.correlation_id, "correlation_id",
                        required=False),
                    causation_id=R.ref(
                        pending.causation_id, "causation_id",
                        required=False),
                    request_hash=pending.request_hash))
            seen_keys: set[str] = set()
            for index, pending in enumerate(prepared):
                assert pending.idempotency_key is not None
                if pending.idempotency_key in seen_keys:
                    raise OutcomeConflictError(
                        "one request produced duplicate idempotency keys")
                seen_keys.add(pending.idempotency_key)
                existing = state.idempotency.get(pending.idempotency_key)
                if existing is not None:
                    if (index == 0
                            and self._request_matches(
                                existing, pending.request_hash)):
                        return _WriteResult(
                            [existing], True, state, truncated)
                    raise OutcomeConflictError(
                        f"idempotency key {pending.idempotency_key!r} is "
                        "already used by a different request")
            seq = state.cursor
            previous = (state.records[-1].record_hash
                        if state.records else None)
            new_records: list[R.OutcomeRecord] = []
            for pending in prepared:
                seq += 1
                record = R.make_record(
                    seq=seq, event=pending.event,
                    intent_id=pending.intent_id, actor=pending.actor,
                    timestamp=moment, payload=pending.payload,
                    idempotency_key=pending.idempotency_key or "",
                    prev_hash=previous,
                    correlation_id=pending.correlation_id,
                    causation_id=pending.causation_id)
                state.apply(record)
                new_records.append(record)
                previous = record.record_hash
            _append_locked(self.journal, new_records)
        return _WriteResult(new_records, False, state, truncated)

    # -- intent lifecycle ------------------------------------------------

    def submit_intent(
            self, requested_outcome: Any, *, criteria: Any = (),
            context_refs: Any = (), project_ref: Any = None,
            interpretation: Any = None, principal: Any = None,
            conversation_ref: Any = None, intent_id: Any = None,
            actor: Any = None, idempotency_key: str | None = None,
            now_ms: int | None = None) -> dict[str, Any]:
        actor_value = R.text(actor or self.actor, "actor", limit=R.MAX_ACTOR)
        assert actor_value is not None
        principal_value = R.text(principal or actor_value, "principal",
                                 limit=R.MAX_ACTOR)
        assert principal_value is not None
        requested = R.text(requested_outcome, "requested_outcome")
        assert requested is not None
        criteria_value = R.criteria_list(criteria)
        contexts = R.ref_list(context_refs)
        project = R.ref(project_ref, "project_ref", required=False)
        interpretation_value = R.detail_text(
            interpretation, "interpretation")
        conversation = R.ref(
            conversation_ref, "conversation_ref", required=False)
        key = (R.idempotency_key(idempotency_key)
               if idempotency_key is not None else None)
        intent = _entity_id(intent_id, "intent", key)
        request = {
            "intent_id": intent,
            "requested_outcome": requested,
            "criteria": criteria_value,
            "context_refs": contexts,
            "project_ref": project,
            "interpretation": interpretation_value,
            "principal": principal_value,
            "conversation_ref": conversation,
        }
        fingerprint = _request_key(R.INTENT_SUBMITTED, intent, request)

        def build(state: S.OutcomeState) -> _Pending:
            payload = R.build_revision_payload(
                intent_id=intent, revision=1, parent=None,
                requested_outcome=requested, criteria=criteria_value,
                context_refs=contexts, project_ref=project,
                interpretation=interpretation_value, principal=principal_value,
                conversation_ref=conversation, reason="submitted",
                request_fingerprint=fingerprint)
            return _Pending(
                R.INTENT_SUBMITTED, intent, payload, actor_value, None,
                None, None, fingerprint)

        result = self._execute(
            build, idempotency_key=key, request_hash=fingerprint,
            now_ms=now_ms)
        materialized = result.state.intent(intent)
        revision = materialized.current_revision
        record = result.records[0]
        return {
            "intent_id": intent,
            "revision": revision.revision,
            "revision_hash": revision.revision_hash,
            "state": materialized.state,
            "duplicate": result.duplicate,
            "record_id": record.record_id,
            "record": record.to_dict(),
        }

    def revise_intent(
            self, intent_id: str, requested_outcome: Any, *,
            criteria: Any = None, context_refs: Any = None,
            project_ref: Any = _INHERIT, interpretation: Any = _INHERIT,
            reason: Any = "steered", expected_revision: int | None = None,
            mark_stale: bool = True, actor: Any = None,
            idempotency_key: str | None = None,
            now_ms: int | None = None) -> dict[str, Any]:
        intent_value = R.identifier(intent_id, "intent_id")
        actor_value = R.text(actor or self.actor, "actor", limit=R.MAX_ACTOR)
        assert actor_value is not None
        requested = R.text(requested_outcome, "requested_outcome")
        assert requested is not None
        criteria_value = (None if criteria is None
                          else R.criteria_list(criteria))
        contexts = (None if context_refs is None
                    else R.ref_list(context_refs))
        inherit_project = project_ref is _INHERIT
        project = (None if inherit_project
                   else R.ref(project_ref, "project_ref", required=False))
        inherit_interpretation = interpretation is _INHERIT
        interpretation_value = (
            None if inherit_interpretation
            else R.detail_text(interpretation, "interpretation"))
        reason_value = R.text(reason, "reason", limit=R.MAX_SHORT)
        assert reason_value is not None
        expected = (None if expected_revision is None
                    else R.integer(expected_revision, "expected_revision",
                                   minimum=1))
        key = (R.idempotency_key(idempotency_key)
               if idempotency_key is not None else None)
        request = {
            "intent_id": intent_value,
            "requested_outcome": requested,
            "criteria": criteria_value,
            "context_refs": contexts,
            "inherit_project": inherit_project,
            "project_ref": project,
            "inherit_interpretation": inherit_interpretation,
            "interpretation": interpretation_value,
            "reason": reason_value,
            "expected_revision": expected,
        }
        fingerprint = _request_key(R.INTENT_REVISED, intent_value, request)

        def build(state: S.OutcomeState) -> list[_Pending]:
            materialized = state.intent(intent_value)
            if expected is not None and materialized.revision != expected:
                raise OutcomeStaleError(
                    f"intent is at revision {materialized.revision}, not "
                    f"{expected}")
            if materialized.state in R.TERMINAL_INTENT_STATES:
                raise OutcomeTransitionError(
                    f"intent is {materialized.state}; it cannot be revised")
            current = materialized.current_revision
            resolved_criteria = (current.criteria if criteria_value is None
                                 else criteria_value)
            resolved_contexts = (current.context_refs if contexts is None
                                 else contexts)
            resolved_project = (current.project_ref if inherit_project
                                else project)
            resolved_interpretation = (
                current.interpretation if inherit_interpretation
                else interpretation_value)
            payload = R.build_revision_payload(
                intent_id=intent_value, revision=current.revision + 1,
                parent=current.revision_hash,
                requested_outcome=requested,
                criteria=resolved_criteria, context_refs=resolved_contexts,
                project_ref=resolved_project,
                interpretation=resolved_interpretation,
                principal=actor_value,
                conversation_ref=materialized.conversation_ref,
                reason=reason_value, request_fingerprint=fingerprint)
            pending = [_Pending(
                R.INTENT_REVISED, intent_value, payload, actor_value, None,
                None, None, fingerprint)]
            if mark_stale:
                pending.extend(self._stale_pendings(
                    state, materialized, payload["revision_hash"],
                    reason_value, actor_value))
            return pending

        result = self._execute(
            build, idempotency_key=key, request_hash=fingerprint,
            now_ms=now_ms)
        primary = result.records[0]
        revision = R.IntentRevision.from_payload(
            primary.payload, record_id=primary.record_id,
            created_at=primary.timestamp)
        stale_work = [
            record.payload.get("work_id") for record in result.records
            if record.event == R.WORK_STALE
        ]
        return {
            "intent_id": intent_value,
            "revision": revision.revision,
            "revision_hash": revision.revision_hash,
            "parent": revision.parent,
            "state": result.state.intent(intent_value).state,
            "duplicate": result.duplicate,
            "stale_work": stale_work,
            "record_id": primary.record_id,
            "record": primary.to_dict(),
        }

    def _stale_pendings(self, state: S.OutcomeState, intent: S.IntentState,
                        revision_hash: str, reason: str,
                        actor: str) -> list[_Pending]:
        pending: list[_Pending] = []
        for item in state.work_for(intent.intent_id):
            if item.state != "queued":
                continue
            if item.revision_hash == revision_hash:
                continue
            fields = {
                "work_id": item.work_id,
                "from_state": "queued",
                "to_state": "stale",
                "reason": reason,
                "revision_ref": revision_hash,
            }
            fingerprint = _request_key(
                R.WORK_STALE, intent.intent_id, fields)
            payload = dict(fields)
            payload.update({
                "schema": R.WORK_TRANSITION_SCHEMA,
                "request_hash": fingerprint,
            })
            pending.append(_Pending(
                R.WORK_STALE, intent.intent_id, payload, actor, None,
                None, None, fingerprint))
        return pending

    def mark_queued_work_stale(
            self, intent_id: str, *, revision_hash: str | None = None,
            reason: Any = "intent revision changed", actor: Any = None,
            idempotency_key: str | None = None,
            now_ms: int | None = None) -> dict[str, Any]:
        intent_value = R.identifier(intent_id, "intent_id")
        actor_value = R.text(actor or self.actor, "actor", limit=R.MAX_ACTOR)
        assert actor_value is not None
        reason_value = R.text(reason, "reason", limit=R.MAX_SHORT)
        assert reason_value is not None
        target = (None if revision_hash is None
                  else R.text(revision_hash, "revision_hash", limit=64))
        key = (R.idempotency_key(idempotency_key)
               if idempotency_key is not None else None)
        request = {
            "intent_id": intent_value,
            "revision_hash": target,
            "reason": reason_value,
        }
        fingerprint = _request_key(R.WORK_STALE, intent_value, request)

        def build(state: S.OutcomeState) -> list[_Pending]:
            materialized = state.intent(intent_value)
            resolved = (materialized.revision_hash if target is None
                        else target)
            if resolved != materialized.revision_hash:
                raise OutcomeStaleError(
                    "queued work can only be staled against the current "
                    "revision")
            return self._stale_pendings(
                state, materialized, resolved, reason_value, actor_value)

        result = self._execute(
            build, idempotency_key=key, request_hash=fingerprint,
            now_ms=now_ms)
        return {
            "intent_id": intent_value,
            "revision_hash": result.state.intent(intent_value).revision_hash,
            "duplicate": result.duplicate,
            "stale_work": [
                record.payload.get("work_id") for record in result.records
            ],
            "records": [record.to_dict() for record in result.records],
        }

    def transition_intent(
            self, intent_id: str, state_name: str, *,
            reason: Any = None, expected_state: str | None = None,
            outcome_ref: str | None = None, actor: Any = None,
            idempotency_key: str | None = None,
            now_ms: int | None = None) -> dict[str, Any]:
        intent_value = R.identifier(intent_id, "intent_id")
        target = R.text(state_name, "state", limit=64)
        assert target is not None
        if target not in R.INTENT_STATES:
            raise OutcomeValidationError(
                f"intent state must be one of {R.INTENT_STATES}")
        actor_value = R.text(actor or self.actor, "actor", limit=R.MAX_ACTOR)
        assert actor_value is not None
        reason_value = R.detail_text(reason, "reason")
        expected = (None if expected_state is None
                    else R.text(expected_state, "expected_state", limit=64))
        named_outcome = (None if outcome_ref is None
                         else R.ref(outcome_ref, "outcome_ref", required=False))
        key = (R.idempotency_key(idempotency_key)
               if idempotency_key is not None else None)
        request = {
            "intent_id": intent_value,
            "state": target,
            "reason": reason_value,
            "expected_state": expected,
            "outcome_ref": named_outcome,
        }
        fingerprint = _request_key(R.INTENT_TRANSITIONED, intent_value, request)

        def build(state: S.OutcomeState) -> _Pending:
            materialized = state.intent(intent_value)
            if expected is not None and materialized.state != expected:
                raise OutcomeStaleError(
                    f"intent is {materialized.state}, not {expected}")
            if target == materialized.state:
                raise OutcomeTransitionError(
                    f"intent is already {target}")
            allowed = R.INTENT_TRANSITIONS.get(materialized.state,
                                               frozenset())
            if target not in allowed:
                raise OutcomeTransitionError(
                    f"intent cannot move from {materialized.state} to "
                    f"{target}")
            resolved_outcome = named_outcome
            decision_ref = None
            if target == "satisfied":
                outcome, decision = state.resolve_satisfaction(
                    materialized, outcome_ref=named_outcome)
                resolved_outcome = outcome.record_id
                decision_ref = decision.record_id
            elif named_outcome is not None:
                named = state.outcomes.get(named_outcome)
                if named is None or named.intent_id != intent_value:
                    raise OutcomeNotFoundError(
                        f"outcome {named_outcome!r} does not exist")
            payload = {
                "schema": R.INTENT_TRANSITION_SCHEMA,
                "from_state": materialized.state,
                "to_state": target,
                "reason": reason_value,
                "outcome_ref": resolved_outcome,
                "decision_ref": decision_ref,
                "request_hash": fingerprint,
            }
            return _Pending(
                R.INTENT_TRANSITIONED, intent_value, payload, actor_value,
                None, None, None, fingerprint)

        result = self._execute(
            build, idempotency_key=key, request_hash=fingerprint,
            now_ms=now_ms)
        record = result.records[0]
        materialized = result.state.intent(intent_value)
        return {
            "intent_id": intent_value,
            "state": materialized.state,
            "previous_state": record.payload.get("from_state"),
            "duplicate": result.duplicate,
            "record_id": record.record_id,
            "record": record.to_dict(),
        }

    def cancel_intent(self, intent_id: str, *, reason: Any = None,
                      actor: Any = None, idempotency_key: str | None = None,
                      now_ms: int | None = None) -> dict[str, Any]:
        return self.transition_intent(
            intent_id, "cancelled", reason=reason, actor=actor,
            idempotency_key=idempotency_key, now_ms=now_ms)

    # -- work and attempts ----------------------------------------------

    def add_work(
            self, intent_id: str, title: Any, *, kind: Any = "task",
            work_id: Any = None, revision_hash: str | None = None,
            project_ref: Any = _INHERIT, context_refs: Any = (),
            details: Mapping[str, Any] | None = None, actor: Any = None,
            idempotency_key: str | None = None,
            now_ms: int | None = None) -> dict[str, Any]:
        intent_value = R.identifier(intent_id, "intent_id")
        actor_value = R.text(actor or self.actor, "actor", limit=R.MAX_ACTOR)
        assert actor_value is not None
        title_value = R.text(title, "title", limit=R.MAX_SHORT)
        kind_value = R.text(kind, "kind", limit=R.MAX_SHORT)
        assert title_value is not None and kind_value is not None
        contexts = R.ref_list(context_refs, "context_refs")
        details_value = R.canonical_payload(details or {}, "details")
        inherit_project = project_ref is _INHERIT
        project = (None if inherit_project
                   else R.ref(project_ref, "project_ref", required=False))
        target_revision = (None if revision_hash is None
                           else R._hash(revision_hash, "revision_hash"))
        key = (R.idempotency_key(idempotency_key)
               if idempotency_key is not None else None)
        work = _entity_id(work_id, "work", key)
        request = {
            "intent_id": intent_value,
            "work_id": work,
            "title": title_value,
            "kind": kind_value,
            "revision_hash": target_revision,
            "inherit_project": inherit_project,
            "project_ref": project,
            "context_refs": contexts,
            "details": details_value,
        }
        fingerprint = _request_key(R.WORK_ADDED, intent_value, request)

        def build(state: S.OutcomeState) -> _Pending:
            materialized = state.intent(intent_value)
            if materialized.state in R.TERMINAL_INTENT_STATES:
                raise OutcomeTransitionError(
                    f"intent is {materialized.state}; work cannot be added")
            current = materialized.current_revision
            revision = (current if target_revision is None
                        else state.revision(intent_value, target_revision))
            if revision.revision_hash != current.revision_hash:
                raise OutcomeStaleError(
                    "new work must target the current intent revision")
            resolved_project = (current.project_ref if inherit_project
                                else project)
            payload = {
                "schema": R.WORK_SCHEMA,
                "work_id": work,
                "revision": revision.revision,
                "revision_hash": revision.revision_hash,
                "title": title_value,
                "kind": kind_value,
                "state": "queued",
                "project_ref": resolved_project,
                "context_refs": contexts,
                "details": details_value,
                "request_hash": fingerprint,
            }
            return _Pending(
                R.WORK_ADDED, intent_value, payload, actor_value, None,
                None, None, fingerprint)

        result = self._execute(
            build, idempotency_key=key, request_hash=fingerprint,
            now_ms=now_ms)
        item = result.state.work[work]
        return {
            "work_id": work,
            "intent_id": intent_value,
            "revision": item.revision,
            "revision_hash": item.revision_hash,
            "state": item.state,
            "duplicate": result.duplicate,
            "record_id": result.records[0].record_id,
            "record": result.records[0].to_dict(),
        }

    def queue_attempt(
            self, intent_id: str, work_id: str, *, attempt_id: Any = None,
            worker_ref: Any = None, assignment_ref: Any = None,
            baseline_ref: Any = None, lease_generation: Any = 1,
            lease_ref: Any = None, actor: Any = None,
            idempotency_key: str | None = None,
            now_ms: int | None = None) -> dict[str, Any]:
        intent_value = R.identifier(intent_id, "intent_id")
        work_value = R.identifier(work_id, "work_id")
        actor_value = R.text(actor or self.actor, "actor", limit=R.MAX_ACTOR)
        assert actor_value is not None
        key = (R.idempotency_key(idempotency_key)
               if idempotency_key is not None else None)
        attempt = _entity_id(attempt_id, "attempt", key)
        worker = R.ref(worker_ref, "worker_ref", required=False)
        assignment = R.ref(assignment_ref, "assignment_ref", required=False)
        baseline = R.ref(baseline_ref, "baseline_ref", required=False)
        lease = R.lease_generation(lease_generation)
        lease_ref_value = R.ref(lease_ref, "lease_ref", required=False)
        request = {
            "intent_id": intent_value,
            "work_id": work_value,
            "attempt_id": attempt,
            "worker_ref": worker,
            "assignment_ref": assignment,
            "baseline_ref": baseline,
            "lease_generation": lease,
            "lease_ref": lease_ref_value,
        }
        fingerprint = _request_key(R.ATTEMPT_QUEUED, intent_value, request)

        def build(state: S.OutcomeState) -> _Pending:
            materialized = state.intent(intent_value)
            item = _work(state, intent_value, work_value)
            S._check_claimable(state, materialized, item)
            revision = materialized.current_revision
            payload = {
                "schema": R.ATTEMPT_SCHEMA,
                "attempt_id": attempt,
                "work_id": work_value,
                "revision": revision.revision,
                "revision_hash": revision.revision_hash,
                "state": "queued",
                "outcome": None,
                "worker_ref": worker,
                "assignment_ref": assignment,
                "baseline_ref": baseline,
                "lease_generation": lease,
                "lease_ref": lease_ref_value,
                "request_hash": fingerprint,
            }
            return _Pending(
                R.ATTEMPT_QUEUED, intent_value, payload, actor_value, None,
                None, None, fingerprint)

        result = self._execute(
            build, idempotency_key=key, request_hash=fingerprint,
            now_ms=now_ms)
        item = result.state.attempts[attempt]
        return _attempt_result(item, result, intent_value)

    def start_attempt(
            self, intent_id: str, work_id: str, *, attempt_id: Any = None,
            worker_ref: Any = None, assignment_ref: Any = None,
            baseline_ref: Any = None, lease_generation: Any = None,
            lease_ref: Any = None, actor: Any = None,
            idempotency_key: str | None = None,
            now_ms: int | None = None) -> dict[str, Any]:
        intent_value = R.identifier(intent_id, "intent_id")
        work_value = R.identifier(work_id, "work_id")
        actor_value = R.text(actor or self.actor, "actor", limit=R.MAX_ACTOR)
        assert actor_value is not None
        key = (R.idempotency_key(idempotency_key)
               if idempotency_key is not None else None)
        attempt = _entity_id(attempt_id, "attempt", key)
        worker = R.ref(worker_ref, "worker_ref", required=False)
        assignment = R.ref(assignment_ref, "assignment_ref", required=False)
        baseline = R.ref(baseline_ref, "baseline_ref", required=False)
        lease = (None if lease_generation is None
                 else R.lease_generation(lease_generation))
        lease_ref_value = R.ref(lease_ref, "lease_ref", required=False)
        request = {
            "intent_id": intent_value,
            "work_id": work_value,
            "attempt_id": attempt,
            "worker_ref": worker,
            "assignment_ref": assignment,
            "baseline_ref": baseline,
            "lease_generation": lease,
            "lease_ref": lease_ref_value,
        }
        fingerprint = _request_key(R.ATTEMPT_STARTED, intent_value, request)

        def build(state: S.OutcomeState) -> _Pending:
            materialized = state.intent(intent_value)
            item = _work(state, intent_value, work_value)
            S._check_claimable(state, materialized, item)
            existing = state.attempts.get(attempt)
            if existing is not None:
                if existing.state != "queued":
                    raise OutcomeTransitionError(
                        f"attempt {attempt!r} is {existing.state}; it cannot "
                        "start again")
                if existing.work_id != work_value:
                    raise OutcomeValidationError(
                        "the queued attempt belongs to another work item")
                previous_state = "queued"
                worker_value = worker if worker is not None else existing.worker_ref
                assignment_value = (assignment if assignment is not None
                                    else existing.assignment_ref)
                baseline_value = (baseline if baseline is not None
                                  else existing.baseline_ref)
                generation = (lease if lease is not None
                              else existing.lease_generation)
                lease_value = (lease_ref_value if lease_ref_value is not None
                               else existing.lease_ref)
            else:
                if any(other.work_id == work_value and other.state == "running"
                       for other in state.attempts.values()):
                    raise OutcomeTransitionError(
                        "another attempt is already running for this work item")
                previous_state = None
                worker_value = worker
                assignment_value = assignment
                baseline_value = baseline
                generation = lease if lease is not None else 1
                lease_value = lease_ref_value
            revision = materialized.current_revision
            payload = {
                "schema": R.ATTEMPT_SCHEMA,
                "attempt_id": attempt,
                "work_id": work_value,
                "revision": revision.revision,
                "revision_hash": revision.revision_hash,
                "state": "running",
                "previous_state": previous_state,
                "outcome": None,
                "worker_ref": worker_value,
                "assignment_ref": assignment_value,
                "baseline_ref": baseline_value,
                "lease_generation": generation,
                "lease_ref": lease_value,
                "request_hash": fingerprint,
            }
            return _Pending(
                R.ATTEMPT_STARTED, intent_value, payload, actor_value, None,
                None, None, fingerprint)

        result = self._execute(
            build, idempotency_key=key, request_hash=fingerprint,
            now_ms=now_ms)
        item = result.state.attempts[attempt]
        return _attempt_result(item, result, intent_value)

    def finish_attempt(
            self, intent_id: str, attempt_id: str, outcome: str, *,
            detail: Any = None, evidence_refs: Any = (),
            lease_generation: Any = None, actor: Any = None,
            idempotency_key: str | None = None,
            now_ms: int | None = None) -> dict[str, Any]:
        intent_value = R.identifier(intent_id, "intent_id")
        attempt = R.identifier(attempt_id, "attempt_id")
        actor_value = R.text(actor or self.actor, "actor", limit=R.MAX_ACTOR)
        assert actor_value is not None
        outcome_value = R.text(outcome, "outcome", limit=64)
        if outcome_value not in R.ATTEMPT_OUTCOMES:
            raise OutcomeValidationError(
                f"attempt outcome must be one of {R.ATTEMPT_OUTCOMES}")
        detail_value = R.detail_text(detail, "detail")
        evidence = R.ref_list(
            evidence_refs, "evidence_refs", limit=R.MAX_EVIDENCE_REFS)
        lease = (None if lease_generation is None
                 else R.lease_generation(lease_generation))
        key = (R.idempotency_key(idempotency_key)
               if idempotency_key is not None else None)
        request = {
            "intent_id": intent_value,
            "attempt_id": attempt,
            "outcome": outcome_value,
            "detail": detail_value,
            "evidence_refs": evidence,
            "lease_generation": lease,
        }
        fingerprint = _request_key(R.ATTEMPT_FINISHED, intent_value, request)

        def build(state: S.OutcomeState) -> _Pending:
            state.intent(intent_value)
            item = state.attempts.get(attempt)
            if item is None or item.intent_id != intent_value:
                raise OutcomeNotFoundError(
                    f"attempt {attempt!r} does not exist")
            if item.state != "running":
                raise OutcomeTransitionError(
                    f"attempt {attempt!r} is {item.state}; only a running "
                    "attempt can finish")
            if lease is not None and lease != item.lease_generation:
                raise OutcomeStaleError(
                    "the lease generation does not match the running attempt")
            payload = {
                "schema": R.ATTEMPT_SCHEMA,
                "attempt_id": attempt,
                "work_id": item.work_id,
                "revision": item.revision,
                "revision_hash": item.revision_hash,
                "state": "terminal",
                "outcome": outcome_value,
                "detail": detail_value,
                "evidence_refs": evidence,
                "lease_generation": item.lease_generation,
                "request_hash": fingerprint,
            }
            return _Pending(
                R.ATTEMPT_FINISHED, intent_value, payload, actor_value, None,
                None, None, fingerprint)

        result = self._execute(
            build, idempotency_key=key, request_hash=fingerprint,
            now_ms=now_ms)
        item = result.state.attempts[attempt]
        return _attempt_result(item, result, intent_value)

    def transition_work(
            self, intent_id: str, work_id: str, state_name: str, *,
            reason: Any = None, actor: Any = None,
            idempotency_key: str | None = None,
            now_ms: int | None = None) -> dict[str, Any]:
        """Move a work item on an explicit, non-stale transition.

        The store only validates the state machine; S1-B owns the reasons and
        evidence for completing or failing work.
        """
        intent_value = R.identifier(intent_id, "intent_id")
        work_value = R.identifier(work_id, "work_id")
        target = R.text(state_name, "state", limit=64)
        assert target is not None
        if target not in R.WORK_STATES:
            raise OutcomeValidationError(
                f"work state must be one of {R.WORK_STATES}")
        actor_value = R.text(actor or self.actor, "actor", limit=R.MAX_ACTOR)
        assert actor_value is not None
        reason_value = R.detail_text(reason, "reason")
        key = (R.idempotency_key(idempotency_key)
               if idempotency_key is not None else None)
        event = R.WORK_STALE if target == "stale" else R.WORK_TRANSITIONED
        request = {
            "intent_id": intent_value,
            "work_id": work_value,
            "state": target,
            "reason": reason_value,
        }
        fingerprint = _request_key(event, intent_value, request)

        def build(state: S.OutcomeState) -> _Pending:
            materialized = state.intent(intent_value)
            item = _work(state, intent_value, work_value)
            if target == item.state:
                raise OutcomeTransitionError(
                    f"work {work_value!r} is already {target}")
            if target not in R.WORK_TRANSITIONS.get(item.state, frozenset()):
                raise OutcomeTransitionError(
                    f"work cannot move from {item.state} to {target}")
            if target == "stale" and not reason_value:
                raise OutcomeValidationError("stale work requires a reason")
            payload = {
                "schema": R.WORK_TRANSITION_SCHEMA,
                "work_id": work_value,
                "from_state": item.state,
                "to_state": target,
                "reason": reason_value,
                "revision_ref": (materialized.revision_hash
                                 if target == "stale" else None),
                "request_hash": fingerprint,
            }
            return _Pending(
                event, intent_value, payload, actor_value,
                None, None, None, fingerprint)

        result = self._execute(
            build, idempotency_key=key, request_hash=fingerprint,
            now_ms=now_ms)
        item = result.state.work[work_value]
        return {
            "work_id": work_value,
            "intent_id": intent_value,
            "state": item.state,
            "duplicate": result.duplicate,
            "record_id": result.records[0].record_id,
            "record": result.records[0].to_dict(),
        }

    # -- generic events --------------------------------------------------

    def append_event(
            self, intent_id: str, event_type: str, payload: Any = None, *,
            correlation_id: str | None = None,
            causation_id: str | None = None, actor: Any = None,
            idempotency_key: str | None = None,
            now_ms: int | None = None) -> dict[str, Any]:
        intent_value = R.identifier(intent_id, "intent_id")
        kind = R.event_name(event_type, "event_type")
        data = R.canonical_payload(payload or {}, "payload")
        actor_value = R.text(actor or self.actor, "actor", limit=R.MAX_ACTOR)
        assert actor_value is not None
        correlation = R.ref(
            correlation_id, "correlation_id", required=False)
        causation = R.ref(causation_id, "causation_id", required=False)
        key = (R.idempotency_key(idempotency_key)
               if idempotency_key is not None else None)
        request = {
            "intent_id": intent_value,
            "kind": kind,
            "data": data,
            "correlation_id": correlation,
            "causation_id": causation,
        }
        fingerprint = _request_key(R.EVENT_APPENDED, intent_value, request)

        def build(state: S.OutcomeState) -> _Pending:
            state.intent(intent_value)
            body = {
                "schema": R.EVENT_SCHEMA,
                "kind": kind,
                "data": data,
                "request_hash": fingerprint,
            }
            return _Pending(
                R.EVENT_APPENDED, intent_value, body, actor_value, None,
                correlation, causation, fingerprint)

        result = self._execute(
            build, idempotency_key=key, request_hash=fingerprint,
            now_ms=now_ms)
        record = result.records[0]
        return {
            "intent_id": intent_value,
            "event_ref": record.record_id,
            "seq": record.seq,
            "cursor": record.seq,
            "duplicate": result.duplicate,
            "record": record.to_dict(),
        }

    # -- reserved S1-B/C records ----------------------------------------

    def record_gate(
            self, intent_id: str, gate_id: str, *, required: bool = True,
            revision_hash: str | None = None, action_digest: Any = None,
            target: Any = None, expected_base: Any = None,
            policy_version: Any = None, expires_at: int | None = None,
            evidence_refs: Any = (), detail: Any = None,
            actor: Any = None, idempotency_key: str | None = None,
            now_ms: int | None = None) -> dict[str, Any]:
        intent_value = R.identifier(intent_id, "intent_id")
        gate = R.identifier(gate_id, "gate_id")
        required_value = R.boolean(required, "required")
        revision = (None if revision_hash is None
                    else R._hash(revision_hash, "revision_hash"))
        action = R.ref(action_digest, "action_digest", required=False)
        target_value = R.ref(target, "target", required=False)
        base = R.ref(expected_base, "expected_base", required=False)
        policy = R.text(policy_version, "policy_version", limit=R.MAX_SHORT,
                        required=False)
        expires = (None if expires_at is None
                   else R.timestamp(expires_at, "expires_at"))
        evidence = R.ref_list(
            evidence_refs, "evidence_refs", limit=R.MAX_EVIDENCE_REFS)
        detail_value = R.detail_text(detail, "detail")
        actor_value = R.text(actor or self.actor, "actor", limit=R.MAX_ACTOR)
        assert actor_value is not None
        key = (R.idempotency_key(idempotency_key)
               if idempotency_key is not None else None)
        request = {
            "intent_id": intent_value,
            "gate_id": gate,
            "required": required_value,
            "revision_hash": revision,
            "action_digest": action,
            "target": target_value,
            "expected_base": base,
            "policy_version": policy,
            "expires_at": expires,
            "evidence_refs": evidence,
            "detail": detail_value,
        }
        fingerprint = _request_key(R.GATE_CREATED, intent_value, request)

        def build(state: S.OutcomeState) -> _Pending:
            materialized = state.intent(intent_value)
            current = materialized.current_revision
            if revision is not None and revision != current.revision_hash:
                raise OutcomeStaleError(
                    "a new gate must bind the current intent revision")
            payload = dict(request)
            payload.pop("intent_id")
            payload["revision_hash"] = current.revision_hash
            payload["schema"] = R.GATE_SCHEMA
            payload["request_hash"] = fingerprint
            return _Pending(
                R.GATE_CREATED, intent_value, payload, actor_value, None,
                None, None, fingerprint)

        result = self._execute(
            build, idempotency_key=key, request_hash=fingerprint,
            now_ms=now_ms)
        record = result.records[0]
        return {
            "intent_id": intent_value,
            "gate_id": gate,
            "gate_ref": record.record_id,
            "duplicate": result.duplicate,
            "record_id": record.record_id,
            "record": record.to_dict(),
        }

    def record_decision(
            self, intent_id: str, gate_id: str, decision: str, *,
            revision_hash: str | None = None, action_digest: Any = None,
            target: Any = None, expected_base: Any = None,
            policy_version: Any = None, evidence_refs: Any = (),
            detail: Any = None, actor: Any = None,
            idempotency_key: str | None = None,
            now_ms: int | None = None) -> dict[str, Any]:
        intent_value = R.identifier(intent_id, "intent_id")
        gate = R.identifier(gate_id, "gate_id")
        decision_value = R.text(decision, "decision", limit=64)
        if decision_value not in R.DECISIONS:
            raise OutcomeValidationError(
                f"decision must be one of {R.DECISIONS}")
        revision = (None if revision_hash is None
                    else R._hash(revision_hash, "revision_hash"))
        action = R.ref(action_digest, "action_digest", required=False)
        target_value = R.ref(target, "target", required=False)
        base = R.ref(expected_base, "expected_base", required=False)
        policy = R.text(policy_version, "policy_version", limit=R.MAX_SHORT,
                        required=False)
        evidence = R.ref_list(
            evidence_refs, "evidence_refs", limit=R.MAX_EVIDENCE_REFS)
        detail_value = R.detail_text(detail, "detail")
        actor_value = R.text(actor or self.actor, "actor", limit=R.MAX_ACTOR)
        assert actor_value is not None
        key = (R.idempotency_key(idempotency_key)
               if idempotency_key is not None else None)
        request = {
            "intent_id": intent_value,
            "gate_id": gate,
            "decision": decision_value,
            "revision_hash": revision,
            "action_digest": action,
            "target": target_value,
            "expected_base": base,
            "policy_version": policy,
            "evidence_refs": evidence,
            "detail": detail_value,
        }
        fingerprint = _request_key(R.DECISION_RECORDED, intent_value, request)

        def build(state: S.OutcomeState) -> _Pending:
            state.intent(intent_value)
            gate_record = state.gates.get(gate)
            if gate_record is None or gate_record.intent_id != intent_value:
                raise OutcomeNotFoundError(f"gate {gate!r} does not exist")
            gate_payload = gate_record.payload
            gate_revision = gate_payload.get("revision_hash")
            resolved_revision = revision or gate_revision
            if resolved_revision != gate_revision:
                raise OutcomeStaleError(
                    "the decision revision does not match its gate")
            payload = {
                "schema": R.DECISION_SCHEMA,
                "gate_id": gate,
                "decision": decision_value,
                "revision_hash": resolved_revision,
                "action_digest": (action if action is not None
                                  else gate_payload.get("action_digest")),
                "target": (target_value if target_value is not None
                           else gate_payload.get("target")),
                "expected_base": (base if base is not None
                                  else gate_payload.get("expected_base")),
                "policy_version": (policy if policy is not None
                                   else gate_payload.get("policy_version")),
                "evidence_refs": evidence,
                "detail": detail_value,
                "request_hash": fingerprint,
            }
            return _Pending(
                R.DECISION_RECORDED, intent_value, payload, actor_value,
                None, None, None, fingerprint)

        result = self._execute(
            build, idempotency_key=key, request_hash=fingerprint,
            now_ms=now_ms)
        record = result.records[0]
        return {
            "intent_id": intent_value,
            "gate_id": gate,
            "decision": decision_value,
            "decision_ref": record.record_id,
            "duplicate": result.duplicate,
            "record_id": record.record_id,
            "record": record.to_dict(),
        }

    def record_evidence(
            self, intent_id: str, *, source: Any, evidence_id: Any = None,
            artifact_digest: Any = None, method: Any = None,
            coverage: Any = None, freshness: str = "current",
            refs: Any = (), detail: Any = None, revision_hash: str | None = None,
            actor: Any = None, idempotency_key: str | None = None,
            now_ms: int | None = None) -> dict[str, Any]:
        intent_value = R.identifier(intent_id, "intent_id")
        source_value = R.text(source, "source", limit=R.MAX_SHORT)
        assert source_value is not None
        evidence_key = (None if evidence_id is None
                        else R.identifier(evidence_id, "evidence_id"))
        artifact = R.ref(artifact_digest, "artifact_digest", required=False)
        method_value = R.text(method, "method", limit=R.MAX_SHORT,
                              required=False)
        coverage_value = R.text(coverage, "coverage", limit=R.MAX_SHORT,
                                required=False)
        freshness_value = R.text(freshness, "freshness", limit=64)
        if freshness_value not in R.EVIDENCE_FRESHNESS:
            raise OutcomeValidationError(
                f"freshness must be one of {sorted(R.EVIDENCE_FRESHNESS)}")
        refs_value = R.ref_list(refs, "refs")
        detail_value = R.detail_text(detail, "detail")
        revision = (None if revision_hash is None
                    else R._hash(revision_hash, "revision_hash"))
        actor_value = R.text(actor or self.actor, "actor", limit=R.MAX_ACTOR)
        assert actor_value is not None
        key = (R.idempotency_key(idempotency_key)
               if idempotency_key is not None else None)
        request = {
            "intent_id": intent_value,
            "evidence_id": evidence_key,
            "source": source_value,
            "artifact_digest": artifact,
            "method": method_value,
            "coverage": coverage_value,
            "freshness": freshness_value,
            "refs": refs_value,
            "detail": detail_value,
            "revision_hash": revision,
        }
        fingerprint = _request_key(R.EVIDENCE_RECORDED, intent_value, request)

        def build(state: S.OutcomeState) -> _Pending:
            materialized = state.intent(intent_value)
            current = materialized.current_revision
            resolved = revision or current.revision_hash
            state.revision(intent_value, resolved)
            payload = dict(request)
            payload.pop("intent_id")
            payload["revision_hash"] = resolved
            payload["schema"] = R.EVIDENCE_SCHEMA
            payload["request_hash"] = fingerprint
            return _Pending(
                R.EVIDENCE_RECORDED, intent_value, payload, actor_value,
                None, None, None, fingerprint)

        result = self._execute(
            build, idempotency_key=key, request_hash=fingerprint,
            now_ms=now_ms)
        record = result.records[0]
        return {
            "intent_id": intent_value,
            "evidence_id": evidence_key,
            "evidence_ref": record.record_id,
            "duplicate": result.duplicate,
            "record_id": record.record_id,
            "record": record.to_dict(),
        }

    def record_effect(
            self, intent_id: str, attempt_id: str, effect_key: str, *,
            state_name: str, target: Any, expected_base: Any = None,
            observed_receipt: Mapping[str, Any] | None = None,
            lease_generation: Any = None, evidence_refs: Any = (),
            detail: Any = None, revision_hash: str | None = None,
            actor: Any = None, idempotency_key: str | None = None,
            now_ms: int | None = None) -> dict[str, Any]:
        intent_value = R.identifier(intent_id, "intent_id")
        attempt = R.identifier(attempt_id, "attempt_id")
        effect = R.text(effect_key, "effect_key", limit=R.MAX_REF)
        effect_state = R.text(state_name, "state", limit=64)
        if effect_state not in R.EFFECT_STATES:
            raise OutcomeValidationError(
                f"effect state must be one of {R.EFFECT_STATES}")
        target_value = R.ref(target, "target", required=True)
        base = R.ref(expected_base, "expected_base", required=False)
        receipt = (None if observed_receipt is None
                   else R.canonical_payload(observed_receipt,
                                            "observed_receipt"))
        lease = (None if lease_generation is None
                 else R.lease_generation(lease_generation))
        evidence = R.ref_list(
            evidence_refs, "evidence_refs", limit=R.MAX_EVIDENCE_REFS)
        detail_value = R.detail_text(detail, "detail")
        revision = (None if revision_hash is None
                    else R._hash(revision_hash, "revision_hash"))
        actor_value = R.text(actor or self.actor, "actor", limit=R.MAX_ACTOR)
        assert actor_value is not None
        key = (R.idempotency_key(idempotency_key)
               if idempotency_key is not None else None)
        request = {
            "intent_id": intent_value,
            "attempt_id": attempt,
            "effect_key": effect,
            "state": effect_state,
            "target": target_value,
            "expected_base": base,
            "observed_receipt": receipt,
            "lease_generation": lease,
            "evidence_refs": evidence,
            "detail": detail_value,
            "revision_hash": revision,
        }
        fingerprint = _request_key(R.EFFECT_RECORDED, intent_value, request)

        def build(state: S.OutcomeState) -> _Pending:
            state.intent(intent_value)
            attempt_state = state.attempts.get(attempt)
            if attempt_state is None or attempt_state.intent_id != intent_value:
                raise OutcomeNotFoundError(
                    f"attempt {attempt!r} does not exist")
            resolved = revision or attempt_state.revision_hash
            if resolved != attempt_state.revision_hash:
                raise OutcomeStaleError(
                    "the effect revision does not match its attempt")
            generation = (lease if lease is not None
                          else attempt_state.lease_generation)
            if generation != attempt_state.lease_generation:
                raise OutcomeStaleError(
                    "the effect lease generation does not match its attempt")
            payload = dict(request)
            payload.pop("intent_id")
            payload["revision_hash"] = resolved
            payload["lease_generation"] = generation
            payload["schema"] = R.EFFECT_SCHEMA
            payload["request_hash"] = fingerprint
            return _Pending(
                R.EFFECT_RECORDED, intent_value, payload, actor_value, None,
                None, None, fingerprint)

        result = self._execute(
            build, idempotency_key=key, request_hash=fingerprint,
            now_ms=now_ms)
        record = result.records[0]
        return {
            "intent_id": intent_value,
            "effect_key": effect,
            "effect_ref": record.record_id,
            "attempt_id": attempt,
            "state": effect_state,
            "duplicate": result.duplicate,
            "record_id": record.record_id,
            "record": record.to_dict(),
        }

    def record_outcome(
            self, intent_id: str, *, status: str = "satisfied",
            evidence_refs: Any = (), criteria_satisfied: Any = (),
            decision_ref: Any = None, summary: Any = None, detail: Any = None,
            revision_hash: str | None = None, actor: Any = None,
            idempotency_key: str | None = None,
            now_ms: int | None = None) -> dict[str, Any]:
        intent_value = R.identifier(intent_id, "intent_id")
        status_value = R.text(status, "status", limit=64)
        if status_value not in R.OUTCOME_STATUSES:
            raise OutcomeValidationError(
                f"outcome status must be one of {R.OUTCOME_STATUSES}")
        evidence = R.ref_list(
            evidence_refs, "evidence_refs", limit=R.MAX_EVIDENCE_REFS)
        criteria = R.criteria_list(criteria_satisfied, "criteria_satisfied")
        decision = R.ref(decision_ref, "decision_ref", required=False)
        summary_value = R.text(summary, "summary", limit=R.MAX_DETAIL,
                               required=False)
        detail_value = R.detail_text(detail, "detail")
        revision = (None if revision_hash is None
                    else R._hash(revision_hash, "revision_hash"))
        actor_value = R.text(actor or self.actor, "actor", limit=R.MAX_ACTOR)
        assert actor_value is not None
        key = (R.idempotency_key(idempotency_key)
               if idempotency_key is not None else None)
        request = {
            "intent_id": intent_value,
            "status": status_value,
            "evidence_refs": evidence,
            "criteria_satisfied": criteria,
            "decision_ref": decision,
            "summary": summary_value,
            "detail": detail_value,
            "revision_hash": revision,
        }
        fingerprint = _request_key(R.OUTCOME_RECORDED, intent_value, request)

        def build(state: S.OutcomeState) -> _Pending:
            materialized = state.intent(intent_value)
            current = materialized.current_revision
            resolved = revision or current.revision_hash
            state.revision(intent_value, resolved)
            if decision is not None:
                recorded = state.decisions.get(decision)
                if recorded is None or recorded.intent_id != intent_value:
                    raise OutcomeNotFoundError(
                        f"decision {decision!r} does not exist")
                if recorded.payload.get("revision_hash") != resolved:
                    raise OutcomeStaleError(
                        "the outcome decision is bound to another revision")
            payload = dict(request)
            payload.pop("intent_id")
            payload["revision_hash"] = resolved
            payload["schema"] = R.OUTCOME_SCHEMA
            payload["request_hash"] = fingerprint
            return _Pending(
                R.OUTCOME_RECORDED, intent_value, payload, actor_value, None,
                None, None, fingerprint)

        result = self._execute(
            build, idempotency_key=key, request_hash=fingerprint,
            now_ms=now_ms)
        record = result.records[0]
        return {
            "intent_id": intent_value,
            "status": status_value,
            "outcome_ref": record.record_id,
            "duplicate": result.duplicate,
            "record_id": record.record_id,
            "record": record.to_dict(),
        }

    # -- reads -----------------------------------------------------------

    def inspect(self, intent_id: str, *,
                events_limit: int = DEFAULT_EVENTS) -> dict[str, Any]:
        intent_value = R.identifier(intent_id, "intent_id")
        limit = _limit(events_limit, "events_limit", MAX_EVENTS)
        scan = self._scan()
        state = S.replay_records(scan.records)
        materialized = state.intent(intent_value)
        events = [record.to_dict() for record in state.records
                  if record.intent_id == intent_value]
        recent = events[-limit:]
        work = [item.to_dict() for item in state.work_for(intent_value)]
        attempts = [item.to_dict() for item in state.attempts_for(intent_value)]
        gates = [item.to_dict()
                 for item in _reserved_for(state.gates, intent_value)]
        decisions = [item.to_dict()
                     for item in state.decisions_for(intent_value)]
        evidence = [item.to_dict()
                    for item in _reserved_for(state.evidence, intent_value)]
        effects = [item.to_dict()
                   for item in _reserved_for(state.effects, intent_value)]
        outcomes = [item.to_dict()
                    for item in state.outcomes_for(intent_value)]
        current_outcomes = [
            item for item in state.outcomes_for(intent_value)
            if item.payload.get("revision_hash") == materialized.revision_hash
        ]
        return {
            "intent": materialized.summary(),
            "revisions": [item.to_dict() for item in materialized.revisions],
            "work": work,
            "attempts": attempts,
            "events": recent,
            "event_count": len(events),
            "events_truncated": len(events) > len(recent),
            "gates": gates,
            "decisions": decisions,
            "evidence": evidence,
            "effects": effects,
            "outcomes": outcomes,
            "outcome": (current_outcomes[-1].to_dict()
                        if current_outcomes else None),
            "latest_outcome": outcomes[-1] if outcomes else None,
            "cursor": state.cursor,
            "journal": {
                "path": self.journal.as_posix(),
                "state": scan.state,
                "torn_bytes": scan.torn_bytes,
                "record_count": len(scan.records),
            },
        }

    def follow(self, after_seq: int = 0, *, intent_id: str | None = None,
               limit: int = DEFAULT_FOLLOW,
               event_type: str | None = None) -> dict[str, Any]:
        cursor = R.integer(after_seq, "after_seq", minimum=0)
        bound = _limit(limit, "limit", MAX_FOLLOW)
        intent_value = (None if intent_id is None
                        else R.identifier(intent_id, "intent_id"))
        event_value = (None if event_type is None
                       else R.event_name(event_type, "event_type"))
        scan = self._scan()
        records = scan.records
        gap = None
        if records and cursor + 1 < records[0].seq:
            first = records[0].seq
            gap = {
                "expected_seq": cursor + 1,
                "first_available_seq": first,
                "missing": first - (cursor + 1),
            }
        candidates = [
            record for record in records
            if record.seq > cursor
            and (intent_value is None or record.intent_id == intent_value)
            and (event_value is None or record.event == event_value)
        ]
        selected = candidates[:bound]
        more = len(candidates) > bound
        next_cursor = selected[-1].seq if selected else cursor
        return {
            "records": [record.to_dict() for record in selected],
            "count": len(selected),
            "cursor": next_cursor,
            "next_cursor": next_cursor,
            "more": more,
            "truncated": more,
            "gap": gap,
            "tail_truncated": bool(scan.torn_bytes),
            "torn_bytes": scan.torn_bytes,
            "journal_state": scan.state,
        }

    def list_active(self, limit: int = DEFAULT_ACTIVE) -> dict[str, Any]:
        bound = _limit(limit, "limit", MAX_ACTIVE)
        state = S.replay_records(self._scan().records)
        active = [
            intent for intent in state.intents.values()
            if intent.state in R.ACTIVE_INTENT_STATES
        ]
        active.sort(key=lambda intent: (-intent.last_seq, intent.intent_id))
        selected = active[:bound]
        rows: list[dict[str, Any]] = []
        for intent in selected:
            work = state.work_for(intent.intent_id)
            attempts = state.attempts_for(intent.intent_id)
            decisions = state.decisions_for(intent.intent_id)
            approved = {
                decision.payload.get("gate_id") for decision in decisions
                if decision.payload.get("decision") == "approved"
            }
            gates = [
                gate for gate in state.gates.values()
                if gate.intent_id == intent.intent_id
            ]
            summary = intent.summary()
            summary["work_counts"] = _count_states(work)
            summary["attempt_counts"] = _count_states(attempts)
            summary["pending_gate_count"] = sum(
                1 for gate in gates
                if gate.payload.get("gate_id") not in approved)
            rows.append(summary)
        return {
            "intents": rows,
            "count": len(rows),
            "total": len(active),
            "truncated": len(active) > len(rows),
            "cursor": state.cursor,
        }


def _resolve_root(root: str | Path) -> Path:
    try:
        path = Path(root).expanduser().resolve(strict=True)
    except OSError as exc:
        raise OutcomeValidationError(
            f"repository root does not exist: {root}") from exc
    if not path.is_dir():
        raise OutcomeValidationError(f"repository root is not a directory: {path}")
    return path


def _limit(value: Any, name: str, hard: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise OutcomeValidationError(f"{name} must be an integer")
    if value < 1 or value > hard:
        raise OutcomeBoundsError(
            f"{name} must be between 1 and {hard}")
    return value


def _work(state: S.OutcomeState, intent_id: str,
          work_id: str) -> S.WorkItem:
    item = state.work.get(work_id)
    if item is None or item.intent_id != intent_id:
        raise OutcomeNotFoundError(f"work item {work_id!r} does not exist")
    return item


def _reserved_for(bucket: Mapping[str, S.ReservedRecord],
                  intent_id: str) -> list[S.ReservedRecord]:
    return sorted(
        (item for item in bucket.values() if item.intent_id == intent_id),
        key=lambda item: item.seq)


def _count_states(items: Iterable[Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        value = item.state if hasattr(item, "state") else None
        if isinstance(value, str):
            counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


def _attempt_result(item: S.Attempt, result: _WriteResult,
                    intent_id: str) -> dict[str, Any]:
    record = result.records[0]
    return {
        "attempt_id": item.attempt_id,
        "work_id": item.work_id,
        "intent_id": intent_id,
        "revision": item.revision,
        "revision_hash": item.revision_hash,
        "state": item.state,
        "outcome": item.outcome,
        "lease_generation": item.lease_generation,
        "duplicate": result.duplicate,
        "record_id": record.record_id,
        "record": record.to_dict(),
    }


# ---------------------------------------------------------------------------
# Module-level convenience API
# ---------------------------------------------------------------------------


def submit_intent(root: str | Path, requested_outcome: Any,
                  **kwargs: Any) -> dict[str, Any]:
    return OutcomeStore(root).submit_intent(requested_outcome, **kwargs)


def revise_intent(root: str | Path, intent_id: str, requested_outcome: Any,
                  **kwargs: Any) -> dict[str, Any]:
    return OutcomeStore(root).revise_intent(
        intent_id, requested_outcome, **kwargs)


def add_work(root: str | Path, intent_id: str, title: Any,
             **kwargs: Any) -> dict[str, Any]:
    return OutcomeStore(root).add_work(intent_id, title, **kwargs)


def queue_attempt(root: str | Path, intent_id: str, work_id: str,
                  **kwargs: Any) -> dict[str, Any]:
    return OutcomeStore(root).queue_attempt(intent_id, work_id, **kwargs)


def start_attempt(root: str | Path, intent_id: str, work_id: str,
                  **kwargs: Any) -> dict[str, Any]:
    return OutcomeStore(root).start_attempt(intent_id, work_id, **kwargs)


def finish_attempt(root: str | Path, intent_id: str, attempt_id: str,
                   outcome: str, **kwargs: Any) -> dict[str, Any]:
    return OutcomeStore(root).finish_attempt(
        intent_id, attempt_id, outcome, **kwargs)


def transition_intent(root: str | Path, intent_id: str, state_name: str,
                      **kwargs: Any) -> dict[str, Any]:
    return OutcomeStore(root).transition_intent(
        intent_id, state_name, **kwargs)


def cancel_intent(root: str | Path, intent_id: str,
                  **kwargs: Any) -> dict[str, Any]:
    return OutcomeStore(root).cancel_intent(intent_id, **kwargs)


def append_event(root: str | Path, intent_id: str, event_type: str,
                 payload: Any = None, **kwargs: Any) -> dict[str, Any]:
    return OutcomeStore(root).append_event(
        intent_id, event_type, payload, **kwargs)


def follow(root: str | Path, after_seq: int = 0, *,
           intent_id: str | None = None, limit: int = DEFAULT_FOLLOW,
           event_type: str | None = None) -> dict[str, Any]:
    return OutcomeStore(root).follow(
        after_seq, intent_id=intent_id, limit=limit, event_type=event_type)


def inspect_intent(root: str | Path, intent_id: str,
                   **kwargs: Any) -> dict[str, Any]:
    return OutcomeStore(root).inspect(intent_id, **kwargs)


def list_active(root: str | Path, limit: int = DEFAULT_ACTIVE) -> dict[str, Any]:
    return OutcomeStore(root).list_active(limit=limit)


def recover(root: str | Path, *, state_dir: str | Path | None = None) -> dict[str, Any]:
    return OutcomeStore(root, state_dir=state_dir).recover()
