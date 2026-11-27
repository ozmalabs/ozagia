"""Bounded one-file effects over the outcome journal.

The outcome store is the work journal; this module is the effect adapter.  It
applies one caller-supplied edit through :mod:`ozagia.edit.txn`, so the write
uses the edit transaction's read-once base hash, backup and atomic replace.
Every state change is an ``effect.recorded`` journal record:

``intended``
    Written before the target is read or touched.  Its presence without a
    later result means the process stopped mid-effect; it is never retried
    silently.
``applied``
    The transaction returned a backup and the target digest matches the
    intended new digest.
``uncertain``
    The write may have happened (the process stopped or failed after the
    replace attempt).  The effect must be reconciled explicitly.
``failed``
    The transaction refused before replacing the target, for example a stale
    base hash.
``reconciled``
    A human/operator explicit resolution of an ``intended`` or ``uncertain``
    effect.  The receipt carries ``resolution`` = ``applied`` or ``failed``.

A minimal pilot revocation epoch is an ordinary ``event.appended`` record with
kind ``grant.revoked``.  Effects carry a ``grant_epoch``; a revocation at that
epoch (or later) refuses a new effect before an ``intended`` record is written.
Reconciliation is deliberately not blocked by revocation, because the write
may already have happened.
"""

from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from ozagia.core import contracts as C
from ozagia.edit import txn

from . import records as R
from .errors import (
    OutcomeConflictError,
    OutcomeError,
    OutcomeNotFoundError,
    OutcomeStaleError,
    OutcomeTransitionError,
    OutcomeValidationError,
)
from .state import Attempt, IntentState, OutcomeState, ReservedRecord
from .store import OutcomeStore

DEFAULT_MAX_BYTES = 1_048_576
MAX_EFFECT_BYTES = 8 * 1_048_576
DEFAULT_GRANT_EPOCH = 1
GRANT_REVOKED = "grant.revoked"
ACTION_SCHEMA = "ozagia.outcomes.action.v1"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class EffectError(OutcomeError):
    """Base class for effect adapter failures."""

    code = "outcomes.effect"


class EffectRefusedError(EffectError):
    """The effect was refused before the target could be written."""

    code = "outcomes.effect_refused"


class EffectRevokedError(EffectRefusedError):
    """The pilot grant epoch was revoked before the effect."""

    code = "outcomes.effect_revoked"


class EffectUncertainError(EffectError):
    """An earlier effect result is unresolved and must be reconciled."""

    code = "outcomes.effect_unresolved"


class EffectStaleBaseError(OutcomeStaleError):
    """The target no longer has the expected base digest."""

    code = "outcomes.stale"


# ---------------------------------------------------------------------------
# Typed request/result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EffectRequest:
    """One bounded one-file change.

    ``new_text`` is the exact text to write (the transaction restores the
    target's BOM and keeps its mode).  ``expected_base`` is the SHA-256 of the
    bytes the caller read; a mismatch refuses before any write.
    """

    intent_id: str
    attempt_id: str
    target: str
    expected_base: str
    new_text: str
    effect_key: str | None = None
    lease_generation: int | str | None = None
    grant_epoch: int = DEFAULT_GRANT_EPOCH
    action_digest: str | None = None
    idempotency_key: str | None = None
    max_bytes: int = DEFAULT_MAX_BYTES


@dataclass(frozen=True)
class EffectResult:
    intent_id: str
    attempt_id: str
    effect_key: str
    state: str
    target: str
    expected_base: str | None
    observed_base: str | None
    new_hash: str | None
    backup_ref: str | None
    action_digest: str
    diff_digest: str | None
    lease_generation: int | str
    revision_hash: str
    effect_ref: str | None
    duplicate: bool = False
    current: bool | None = None
    resolution: str | None = None
    detail: str | None = None
    record: dict[str, Any] = field(default_factory=dict)

    @property
    def applied(self) -> bool:
        if self.state == "applied":
            return True
        return self.state == "reconciled" and self.resolution == "applied"

    @property
    def needs_reconciliation(self) -> bool:
        return self.state in ("intended", "uncertain")

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent_id": self.intent_id,
            "attempt_id": self.attempt_id,
            "effect_key": self.effect_key,
            "state": self.state,
            "target": self.target,
            "expected_base": self.expected_base,
            "observed_base": self.observed_base,
            "new_hash": self.new_hash,
            "backup_ref": self.backup_ref,
            "action_digest": self.action_digest,
            "diff_digest": self.diff_digest,
            "lease_generation": self.lease_generation,
            "revision_hash": self.revision_hash,
            "effect_ref": self.effect_ref,
            "duplicate": self.duplicate,
            "current": self.current,
            "resolution": self.resolution,
            "detail": self.detail,
            "applied": self.applied,
            "needs_reconciliation": self.needs_reconciliation,
        }


# ---------------------------------------------------------------------------
# Hashes and validation
# ---------------------------------------------------------------------------


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str | Path) -> str | None:
    try:
        return sha256_bytes(Path(path).read_bytes())
    except OSError:
        return None


def action_digest_for(target: str, expected_base: str,
                      new_text: str) -> str:
    """The exact action digest bound by a gate.

    It covers the target, the expected base bytes and the exact new text, so a
    gate approving one change cannot authorize a different one.
    """
    if not isinstance(new_text, str):
        raise OutcomeValidationError("new_text must be text")
    return C.content_hash({
        "schema": ACTION_SCHEMA,
        "target": target,
        "expected_base": expected_base,
        "new_text_sha256": sha256_bytes(new_text.encode("utf-8")),
    })


def _hash(value: Any, name: str) -> str:
    try:
        return R._hash(value, name)
    except OutcomeError:
        raise
    except Exception as exc:  # pragma: no cover - defensive
        raise OutcomeValidationError(f"{name}: {exc}") from exc


def _target_path(root: Path, target: str) -> Path:
    path = (root / target).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise OutcomeValidationError(
            f"target {target!r} escapes the repository root") from exc
    if txn.in_git_dir(path):
        raise OutcomeValidationError("an effect cannot target Git metadata")
    return path


def resolve_target(root: str | Path, target: str) -> Path:
    """Resolve a target inside ``root`` and refuse Git metadata."""
    root_path = Path(root).expanduser().resolve()
    value = R.ref(target, "target")
    assert value is not None
    return _target_path(root_path, value)


def _validate_request(request: EffectRequest) -> dict[str, Any]:
    if not isinstance(request, EffectRequest):
        raise OutcomeValidationError("request must be an EffectRequest")
    intent = R.identifier(request.intent_id, "intent_id")
    attempt = R.identifier(request.attempt_id, "attempt_id")
    target = R.ref(request.target, "target")
    assert target is not None
    expected = _hash(request.expected_base, "expected_base")
    if not isinstance(request.new_text, str):
        raise OutcomeValidationError("new_text must be text")
    encoded = request.new_text.encode("utf-8")
    if not isinstance(request.max_bytes, int) or isinstance(
            request.max_bytes, bool):
        raise OutcomeValidationError("max_bytes must be an integer")
    if request.max_bytes < 1 or request.max_bytes > MAX_EFFECT_BYTES:
        raise OutcomeValidationError(
            f"max_bytes must be between 1 and {MAX_EFFECT_BYTES}")
    if len(encoded) > request.max_bytes:
        raise OutcomeValidationError(
            f"new_text exceeds {request.max_bytes} bytes")
    effect_key = (None if request.effect_key is None
                  else R.text(request.effect_key, "effect_key",
                              limit=R.MAX_REF))
    lease = (None if request.lease_generation is None
             else R.lease_generation(request.lease_generation))
    epoch = R.integer(request.grant_epoch, "grant_epoch", minimum=1)
    action = (None if request.action_digest is None
              else _hash(request.action_digest, "action_digest"))
    key = (None if request.idempotency_key is None
           else R.idempotency_key(request.idempotency_key))
    return {
        "intent_id": intent,
        "attempt_id": attempt,
        "target": target,
        "expected_base": expected,
        "new_text": request.new_text,
        "effect_key": effect_key,
        "lease_generation": lease,
        "grant_epoch": epoch,
        "action_digest": action,
        "idempotency_key": key,
        "max_bytes": request.max_bytes,
    }


def _effect_key(intent_id: str, attempt_id: str, target: str,
                expected_base: str, new_text: str) -> str:
    digest = C.content_hash({
        "schema": "ozagia.outcomes.effect_key.v1",
        "intent_id": intent_id,
        "attempt_id": attempt_id,
        "target": target,
        "expected_base": expected_base,
        "new_text_sha256": sha256_bytes(new_text.encode("utf-8")),
    })
    return f"effect-{digest[:40]}"


def _journal_key(effect_key: str, state_name: str,
                 caller_key: str | None = None) -> str:
    material = effect_key if caller_key is None else f"{caller_key}:{effect_key}"
    digest = sha256_bytes(material.encode("utf-8"))
    return f"effect-{digest[:40]}-{state_name}"


def _receipt(record: ReservedRecord) -> dict[str, Any]:
    value = record.payload.get("observed_receipt")
    return dict(value) if isinstance(value, Mapping) else {}


def _effect_records(state: OutcomeState, intent_id: str,
                    effect_key: str) -> list[ReservedRecord]:
    return sorted(
        (item for item in state.effects.values()
         if item.intent_id == intent_id
         and item.payload.get("effect_key") == effect_key),
        key=lambda item: item.seq,
    )


def effect_records(store: OutcomeStore, intent_id: str,
                   effect_key: str) -> list[dict[str, Any]]:
    """Read one effect's ordered state records from the journal."""
    state = store.materialize()
    return [item.to_dict() for item in _effect_records(
        state, R.identifier(intent_id, "intent_id"),
        R.text(effect_key, "effect_key", limit=R.MAX_REF) or "")]


def effect_status(store: OutcomeStore, intent_id: str,
                  effect_key: str) -> dict[str, Any] | None:
    """The latest receipt for an effect key, or ``None`` when absent."""
    records = effect_records(store, intent_id, effect_key)
    return records[-1] if records else None


# ---------------------------------------------------------------------------
# Pilot revocation epoch
# ---------------------------------------------------------------------------


def _revocations(state: OutcomeState, intent_id: str) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for record in state.records:
        if record.event != R.EVENT_APPENDED:
            continue
        if record.intent_id != intent_id:
            continue
        payload = record.payload
        if payload.get("kind") != GRANT_REVOKED:
            continue
        data = payload.get("data") or {}
        if not isinstance(data, Mapping):
            continue
        epoch = data.get("epoch", DEFAULT_GRANT_EPOCH)
        if isinstance(epoch, bool) or not isinstance(epoch, int):
            continue
        targets = data.get("targets") or []
        found.append({
            "epoch": epoch,
            "targets": [str(item) for item in targets
                        if isinstance(item, str)],
            "reason": data.get("reason"),
            "policy_version": data.get("policy_version"),
            "record_id": record.record_id,
            "seq": record.seq,
        })
    return found


def pilot_revocation(state: OutcomeState, intent_id: str, *,
                     grant_epoch: int = DEFAULT_GRANT_EPOCH,
                     target: str | None = None) -> dict[str, Any] | None:
    """The revoking record at or after ``grant_epoch`` that covers ``target``."""
    for item in _revocations(state, intent_id):
        if item["epoch"] < grant_epoch:
            continue
        if item["targets"] and target not in item["targets"]:
            continue
        return item
    return None


def revoke_pilot(store: OutcomeStore, intent_id: str, *,
                 epoch: int = DEFAULT_GRANT_EPOCH,
                 reason: Any = "pilot grant revoked",
                 policy_version: Any = None,
                 targets: Any = (),
                 actor: Any = None,
                 idempotency_key: str | None = None,
                 now_ms: int | None = None) -> dict[str, Any]:
    """Append a minimal pilot revocation event.

    A later effect using ``grant_epoch`` <= ``epoch`` (and a matching target
    when ``targets`` is non-empty) is refused before its ``intended`` record.
    """
    intent = R.identifier(intent_id, "intent_id")
    value = R.integer(epoch, "epoch", minimum=1)
    reason_value = R.detail_text(reason, "reason")
    policy = R.text(policy_version, "policy_version", limit=R.MAX_SHORT,
                    required=False)
    target_refs = R.ref_list(targets, "targets")
    result = store.append_event(
        intent, GRANT_REVOKED,
        {"epoch": value, "reason": reason_value,
         "policy_version": policy, "targets": target_refs},
        actor=actor, idempotency_key=idempotency_key, now_ms=now_ms)
    return {**result, "epoch": value, "targets": target_refs}


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class EffectAdapter:
    """Apply and reconcile bounded one-file effects for an outcome store."""

    def __init__(self, store: OutcomeStore, *, actor: str | None = None,
                 read: Callable[..., txn.SourceFile] | None = None,
                 write: Callable[..., str] | None = None,
                 max_bytes: int = DEFAULT_MAX_BYTES) -> None:
        if not isinstance(store, OutcomeStore):
            raise OutcomeValidationError("store must be an OutcomeStore")
        if not isinstance(max_bytes, int) or isinstance(max_bytes, bool):
            raise OutcomeValidationError("max_bytes must be an integer")
        if max_bytes < 1 or max_bytes > MAX_EFFECT_BYTES:
            raise OutcomeValidationError(
                f"max_bytes must be between 1 and {MAX_EFFECT_BYTES}")
        self.store = store
        self.root = Path(store.root)
        self.actor = (store.actor if actor is None
                      else R.text(actor, "actor", limit=R.MAX_ACTOR))
        self.max_bytes = max_bytes
        self._read = read or txn.read_source
        self._write = write or txn.write_source

    # -- public operations ----------------------------------------------

    def apply(self, request: EffectRequest, *,
              now_ms: int | None = None) -> EffectResult:
        values = _validate_request(request)
        state = self.store.materialize()
        intent = state.intent(values["intent_id"])
        attempt = state.attempts.get(values["attempt_id"])
        if attempt is None or attempt.intent_id != intent.intent_id:
            raise OutcomeNotFoundError(
                f"attempt {values['attempt_id']!r} does not exist")
        if attempt.state != "running":
            raise OutcomeTransitionError(
                f"attempt {attempt.attempt_id!r} is {attempt.state}; only a "
                "running attempt can apply an effect")
        if attempt.revision_hash != intent.revision_hash:
            raise OutcomeStaleError(
                "the attempt is bound to an old intent revision; a new effect "
                "requires a current attempt")
        lease = (values["lease_generation"]
                 if values["lease_generation"] is not None
                 else attempt.lease_generation)
        if lease != attempt.lease_generation:
            raise OutcomeStaleError(
                "the effect lease generation does not match the running "
                "attempt")
        effect_key = values["effect_key"] or _effect_key(
            intent.intent_id, attempt.attempt_id, values["target"],
            values["expected_base"], values["new_text"])
        action = action_digest_for(
            values["target"], values["expected_base"], values["new_text"])
        if values["action_digest"] is not None \
                and values["action_digest"] != action:
            raise OutcomeValidationError(
                "action_digest does not match the requested effect")

        existing = _effect_records(state, intent.intent_id, effect_key)
        if existing:
            latest = existing[-1]
            receipt = _receipt(latest)
            if (latest.payload.get("target") != values["target"]
                    or latest.payload.get("expected_base")
                    != values["expected_base"]
                    or receipt.get("action_digest") != action):
                raise OutcomeConflictError(
                    f"effect key {effect_key!r} is already used by a "
                    "different action")
            return self._existing_result(latest)

        revoked = pilot_revocation(
            state, intent.intent_id, grant_epoch=values["grant_epoch"],
            target=values["target"])
        if revoked is not None:
            raise EffectRevokedError(
                "the pilot grant is revoked at epoch "
                f"{revoked['epoch']} (record {revoked['record_id']})")

        path = _target_path(self.root, values["target"])
        suffix = path.suffix.lower()
        intended = self._record(
            intent, attempt, effect_key, "intended", values, action,
            receipt={"grant_epoch": values["grant_epoch"]},
            detail="effect intended",
            idempotency_key=values["idempotency_key"], now_ms=now_ms)
        if intended["duplicate"]:
            refreshed = _effect_records(
                self.store.materialize(), intent.intent_id, effect_key)
            if refreshed:
                return self._existing_result(refreshed[-1])
            raise EffectUncertainError(
                "another writer recorded the effect intent; reconcile before "
                "retrying")

        try:
            source = self._read(
                self.root, values["target"],
                suffixes=(suffix,) if suffix else (),
                max_bytes=values["max_bytes"], error_cls=EffectRefusedError)
        except Exception as exc:
            self._record(
                intent, attempt, effect_key, "failed", values, action,
                receipt={"grant_epoch": values["grant_epoch"],
                         "reason": str(exc)[:R.MAX_DETAIL]},
                detail="effect refused: the target could not be read",
                idempotency_key=values["idempotency_key"], now_ms=now_ms)
            raise

        observed_base = source.sha256
        if observed_base != values["expected_base"]:
            self._record(
                intent, attempt, effect_key, "failed", values, action,
                receipt={"grant_epoch": values["grant_epoch"],
                         "observed_base": observed_base},
                detail="stale base: the target has changed since the "
                       "effect was intended",
                idempotency_key=values["idempotency_key"], now_ms=now_ms)
            raise EffectStaleBaseError(
                f"stale base: target sha256 is {observed_base}, expected "
                f"{values['expected_base']}")

        try:
            expected_new = sha256_bytes(
                txn.encode_text(source, values["new_text"]))
        except Exception as exc:  # pragma: no cover - defensive
            self._record(
                intent, attempt, effect_key, "failed", values, action,
                receipt={"grant_epoch": values["grant_epoch"],
                         "observed_base": observed_base,
                         "reason": str(exc)[:R.MAX_DETAIL]},
                detail="effect refused: encoding failed",
                idempotency_key=values["idempotency_key"], now_ms=now_ms)
            raise EffectRefusedError(
                f"cannot encode the edited target: {exc}") from exc

        try:
            backup = self._write(
                self.root, source, values["new_text"],
                error_cls=EffectRefusedError)
        except Exception as exc:
            current = sha256_file(path)
            if current == observed_base:
                self._record(
                    intent, attempt, effect_key, "failed", values, action,
                    receipt={"grant_epoch": values["grant_epoch"],
                             "observed_base": observed_base,
                             "reason": str(exc)[:R.MAX_DETAIL]},
                    detail="effect refused before replacing the target",
                    idempotency_key=values["idempotency_key"], now_ms=now_ms)
                if "stale base" in str(exc):
                    raise EffectStaleBaseError(str(exc)) from exc
                if isinstance(exc, OutcomeError):
                    raise
                raise EffectRefusedError(
                    f"the edit transaction refused before writing: {exc}"
                ) from exc
            self._record(
                intent, attempt, effect_key, "uncertain", values, action,
                receipt={"grant_epoch": values["grant_epoch"],
                         "observed_base": observed_base,
                         "new_hash": current,
                         "expected_new_hash": expected_new,
                         "reason": str(exc)[:R.MAX_DETAIL]},
                detail="effect uncertain: the transaction did not return a "
                       "confirmed result; reconcile explicitly",
                idempotency_key=values["idempotency_key"], now_ms=now_ms)
            return self._result_from(
                _effect_records(self.store.materialize(), intent.intent_id,
                                effect_key)[-1])

        current = sha256_file(path)
        if current != expected_new:
            self._record(
                intent, attempt, effect_key, "uncertain", values, action,
                receipt={"grant_epoch": values["grant_epoch"],
                         "observed_base": observed_base,
                         "new_hash": current,
                         "expected_new_hash": expected_new,
                         "backup_ref": backup},
                detail="effect uncertain: the written target does not match "
                       "the intended digest; reconcile explicitly",
                idempotency_key=values["idempotency_key"], now_ms=now_ms)
            return self._result_from(
                _effect_records(self.store.materialize(), intent.intent_id,
                                effect_key)[-1])

        receipt = {
            "grant_epoch": values["grant_epoch"],
            "observed_base": observed_base,
            "new_hash": current,
            "backup_ref": backup,
            "diff_digest": self._diff_digest(values["target"]),
        }
        try:
            applied = self._record(
                intent, attempt, effect_key, "applied", values, action,
                receipt=receipt, detail="effect applied",
                idempotency_key=values["idempotency_key"], now_ms=now_ms)
        except Exception:
            try:
                self._record(
                    intent, attempt, effect_key, "uncertain", values, action,
                    receipt={**receipt, "reason": "could not record applied"},
                    detail="effect uncertain: the write happened but the "
                           "applied receipt could not be recorded",
                    idempotency_key=values["idempotency_key"], now_ms=now_ms)
            finally:
                raise
        record = applied["record"]
        return self._result_from_record(record, duplicate=applied["duplicate"])

    def reconcile(self, intent_id: str, attempt_id: str, effect_key: str,
                  resolution: str, *, observed_base: Any = None,
                  new_hash: Any = None, backup_ref: Any = None,
                  note: Any = None, actor: Any = None,
                  idempotency_key: str | None = None,
                  now_ms: int | None = None) -> EffectResult:
        """Explicitly resolve an intended/uncertain effect.

        ``resolution="applied"`` requires the target's current digest to equal
        ``new_hash`` (supplied or observed).  Reconciliation never writes the
        target; it only appends a receipt and remains available after a pilot
        revocation.
        """
        intent_value = R.identifier(intent_id, "intent_id")
        attempt_value = R.identifier(attempt_id, "attempt_id")
        key_value = R.text(effect_key, "effect_key", limit=R.MAX_REF)
        assert key_value is not None
        if resolution not in ("applied", "failed"):
            raise OutcomeValidationError(
                "resolution must be 'applied' or 'failed'")
        base = (None if observed_base is None
                else _hash(observed_base, "observed_base"))
        new = (None if new_hash is None
               else _hash(new_hash, "new_hash"))
        backup = (None if backup_ref is None
                  else R.ref(backup_ref, "backup_ref", required=False))
        note_value = R.detail_text(note, "note")
        state = self.store.materialize()
        intent = state.intent(intent_value)
        attempt = state.attempts.get(attempt_value)
        if attempt is None or attempt.intent_id != intent.intent_id:
            raise OutcomeNotFoundError(
                f"attempt {attempt_value!r} does not exist")
        records = _effect_records(state, intent.intent_id, key_value)
        if not records:
            raise OutcomeNotFoundError(
                f"effect {key_value!r} does not exist")
        latest = records[-1]
        payload = latest.payload
        if payload.get("state") == "reconciled":
            receipt = _receipt(latest)
            if receipt.get("resolution") == resolution:
                return self._result_from(latest, duplicate=True)
            raise OutcomeConflictError(
                "the effect is already reconciled with another resolution")
        if payload.get("state") == "applied":
            return self._result_from(latest, duplicate=True)
        if payload.get("state") == "failed":
            raise OutcomeTransitionError(
                "a failed effect has no uncertain write to reconcile")
        if payload.get("state") not in ("intended", "uncertain"):
            raise OutcomeTransitionError(
                f"effect {key_value!r} is {payload.get('state')}; it cannot "
                "be reconciled")

        target = payload.get("target")
        if not isinstance(target, str):
            raise OutcomeValidationError("the effect receipt has no target")
        receipt = _receipt(latest)
        resolved_base = base or receipt.get("observed_base") \
            or payload.get("expected_base")
        path = _target_path(self.root, target)
        current = sha256_file(path)
        resolved_new = new or receipt.get("new_hash")
        if resolved_new is None and current is not None \
                and current != resolved_base:
            resolved_new = current
        if resolution == "applied":
            if resolved_new is None:
                raise OutcomeValidationError(
                    "reconciling applied requires the new target digest")
            if current != resolved_new:
                raise OutcomeStaleError(
                    "the target digest does not match the reconciliation")
        values = {
            "target": target,
            "expected_base": payload.get("expected_base"),
        }
        action = receipt.get("action_digest")
        if not isinstance(action, str):
            action = action_digest_for(
                target, str(payload.get("expected_base") or ""), "")
        record_receipt = {
            "grant_epoch": receipt.get("grant_epoch", DEFAULT_GRANT_EPOCH),
            "observed_base": resolved_base,
            "new_hash": resolved_new,
            "backup_ref": backup or receipt.get("backup_ref"),
            "resolution": resolution,
            "reconciled_from": payload.get("state"),
            "note": note_value,
        }
        result = self._record(
            intent, attempt, key_value, "reconciled", values, action,
            receipt=record_receipt,
            detail=f"effect reconciled as {resolution}",
            actor=actor, idempotency_key=idempotency_key, now_ms=now_ms)
        return self._result_from_record(
            result["record"], duplicate=result["duplicate"])

    # -- internals -------------------------------------------------------

    def _record(self, intent: IntentState, attempt: Attempt,
                effect_key: str, state_name: str, values: Mapping[str, Any],
                action: str, *, receipt: Mapping[str, Any],
                detail: str, actor: Any = None,
                idempotency_key: str | None = None,
                now_ms: int | None = None) -> dict[str, Any]:
        payload = dict(receipt)
        payload["action_digest"] = action
        return self.store.record_effect(
            intent.intent_id, attempt.attempt_id, effect_key,
            state_name=state_name, target=values["target"],
            expected_base=values.get("expected_base"),
            observed_receipt=payload,
            lease_generation=attempt.lease_generation,
            revision_hash=attempt.revision_hash, detail=detail,
            actor=actor or self.actor,
            idempotency_key=_journal_key(
                effect_key, state_name, idempotency_key),
            now_ms=now_ms)

    def _diff_digest(self, target: str) -> str | None:
        try:
            completed = subprocess.run(
                ["git", "--no-pager", "diff", "--no-color", "--no-ext-diff",
                 "--", target],
                cwd=str(self.root), capture_output=True, timeout=30,
                check=False)
        except (OSError, subprocess.SubprocessError):
            return None
        if completed.returncode != 0:
            return None
        return sha256_bytes(completed.stdout)

    def _existing_result(self, record: ReservedRecord,
                         duplicate: bool = True) -> EffectResult:
        payload = record.payload
        state_name = payload.get("state")
        if state_name in ("intended", "uncertain"):
            raise EffectUncertainError(
                f"effect {payload.get('effect_key')!r} is {state_name}; "
                "reconcile it explicitly before retrying")
        return self._result_from(record, duplicate=duplicate)

    def _result_from(self, record: ReservedRecord,
                     duplicate: bool = True) -> EffectResult:
        return self._result_from_record(record.to_dict(), duplicate=duplicate)

    def _result_from_record(self, record: Mapping[str, Any],
                            duplicate: bool = False) -> EffectResult:
        nested = record.get("payload")
        if isinstance(nested, Mapping):
            payload = dict(nested)
            for name in ("record_id", "seq", "timestamp", "actor",
                         "intent_id"):
                if name in record:
                    payload[name] = record[name]
        else:
            payload = dict(record)
        receipt = payload.get("observed_receipt") or {}
        if not isinstance(receipt, Mapping):
            receipt = {}
        target = payload.get("target") or ""
        new_hash = receipt.get("new_hash")
        current = (sha256_file(_target_path(self.root, target))
                   if target else None)
        return EffectResult(
            intent_id=record.get("intent_id") or "",
            attempt_id=payload.get("attempt_id") or "",
            effect_key=payload.get("effect_key") or "",
            state=payload.get("state") or "",
            target=target,
            expected_base=payload.get("expected_base"),
            observed_base=receipt.get("observed_base"),
            new_hash=new_hash,
            backup_ref=receipt.get("backup_ref"),
            action_digest=receipt.get("action_digest") or "",
            diff_digest=receipt.get("diff_digest"),
            lease_generation=payload.get("lease_generation"),
            revision_hash=payload.get("revision_hash") or "",
            effect_ref=record.get("record_id"),
            duplicate=duplicate,
            current=(current == new_hash if new_hash else None),
            resolution=receipt.get("resolution"),
            detail=payload.get("detail"),
            record=payload,
        )


def apply_effect(store: OutcomeStore, request: EffectRequest, *,
                 actor: str | None = None,
                 now_ms: int | None = None) -> EffectResult:
    """Convenience wrapper around :meth:`EffectAdapter.apply`."""
    return EffectAdapter(store, actor=actor).apply(request, now_ms=now_ms)


def reconcile_effect(store: OutcomeStore, intent_id: str, attempt_id: str,
                     effect_key: str, resolution: str, *,
                     actor: str | None = None,
                     **kwargs: Any) -> EffectResult:
    """Convenience wrapper around :meth:`EffectAdapter.reconcile`."""
    return EffectAdapter(store, actor=actor).reconcile(
        intent_id, attempt_id, effect_key, resolution, **kwargs)


__all__ = [
    "ACTION_SCHEMA",
    "DEFAULT_GRANT_EPOCH",
    "DEFAULT_MAX_BYTES",
    "EffectAdapter",
    "EffectError",
    "EffectRefusedError",
    "EffectRequest",
    "EffectResult",
    "EffectRevokedError",
    "EffectStaleBaseError",
    "EffectUncertainError",
    "GRANT_REVOKED",
    "MAX_EFFECT_BYTES",
    "action_digest_for",
    "apply_effect",
    "effect_records",
    "effect_status",
    "pilot_revocation",
    "reconcile_effect",
    "resolve_target",
    "revoke_pilot",
    "sha256_bytes",
    "sha256_file",
]
