"""Human gates, exact decisions and outcome finalization.

A required gate binds the current intent revision, the exact action digest,
target, expected base, policy version and validation evidence references.  A
decision is recorded through the outcome store's ``record_decision``; approval
or rejection is refused when the intent revision, action, target or base has
drifted.  Satisfaction is recorded only after the approved gate and current
validation evidence both resolve for the current revision.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Mapping

from ozagia.core import contracts as C

from . import records as R
from .effects import (
    EffectResult,
    resolve_target,
    sha256_file,
)
from .errors import (
    OutcomeNotFoundError,
    OutcomeStaleError,
    OutcomeTransitionError,
    OutcomeValidationError,
)
from .state import IntentState, OutcomeState, ReservedRecord
from .store import OutcomeStore
from .validation import (
    VALIDATION_SOURCE,
    ValidationResult,
    validation_record_for_ref,
)

DEFAULT_POLICY_VERSION = "stage1-policy.v1"
GATE_STATUSES = (
    "pending", "approved", "rejected", "expired", "cancelled", "stale",
)


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def _moment(now_ms: int | None) -> int:
    return _now_ms() if now_ms is None else R.timestamp(now_ms, "now_ms")


def _key(prefix: str, value: str) -> str:
    digest = C.content_hash({"prefix": prefix, "value": value})[:40]
    return f"{prefix}-{digest}"


@dataclass(frozen=True)
class GateStatus:
    intent_id: str
    gate_id: str
    status: str
    required: bool
    revision_hash: str
    current_revision_hash: str
    action_digest: str | None
    target: str | None
    expected_base: str | None
    policy_version: str | None
    evidence_refs: tuple[str, ...] = ()
    decision_ref: str | None = None
    decision: str | None = None
    gate_ref: str | None = None
    expires_at: int | None = None
    reason: str | None = None
    record: dict[str, Any] = field(default_factory=dict)

    @property
    def decided(self) -> bool:
        return self.status in ("approved", "rejected", "expired", "cancelled")

    @property
    def current(self) -> bool:
        return self.status not in ("stale", "expired")

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent_id": self.intent_id,
            "gate_id": self.gate_id,
            "status": self.status,
            "required": self.required,
            "revision_hash": self.revision_hash,
            "current_revision_hash": self.current_revision_hash,
            "action_digest": self.action_digest,
            "target": self.target,
            "expected_base": self.expected_base,
            "policy_version": self.policy_version,
            "evidence_refs": list(self.evidence_refs),
            "decision_ref": self.decision_ref,
            "decision": self.decision,
            "gate_ref": self.gate_ref,
            "expires_at": self.expires_at,
            "reason": self.reason,
            "decided": self.decided,
            "current": self.current,
        }


def _gate(state: OutcomeState, intent_id: str,
          gate_id: str) -> ReservedRecord:
    gate = state.gates.get(gate_id)
    if gate is None or gate.intent_id != intent_id:
        raise OutcomeNotFoundError(f"gate {gate_id!r} does not exist")
    return gate


def _latest_decision(state: OutcomeState,
                     gate_id: str) -> ReservedRecord | None:
    decisions = sorted(
        (item for item in state.decisions.values()
         if item.payload.get("gate_id") == gate_id),
        key=lambda item: item.seq)
    return decisions[-1] if decisions else None


def _ref_list(value: Any, name: str = "evidence_refs") -> tuple[str, ...]:
    return tuple(R.ref_list(value, name, limit=R.MAX_EVIDENCE_REFS))


def _validation_records(store: OutcomeStore, state: OutcomeState,
                        intent: IntentState, refs: tuple[str, ...], *,
                        revision_hash: str, target: str | None = None,
                        artifact_digest: str | None = None
                        ) -> list[dict[str, Any]]:
    """Resolve outcome evidence refs to current evidence-store validations."""
    if not refs:
        raise OutcomeTransitionError(
            "a required gate needs validation evidence references")
    resolved: list[dict[str, Any]] = []
    for ref in refs:
        record = state.evidence.get(ref)
        if record is None or record.intent_id != intent.intent_id:
            raise OutcomeNotFoundError(
                f"evidence {ref!r} does not exist for this intent")
        payload = record.payload
        if payload.get("revision_hash") != revision_hash:
            raise OutcomeStaleError(
                "the validation evidence is bound to another intent revision")
        if payload.get("freshness") != "current":
            raise OutcomeStaleError(
                "the validation evidence is not current")
        if payload.get("source") != VALIDATION_SOURCE:
            raise OutcomeTransitionError(
                "a worker success response is not validation evidence")
        artifact = payload.get("artifact_digest")
        if not isinstance(artifact, str):
            raise OutcomeTransitionError(
                "the validation evidence has no artifact digest")
        if artifact_digest is not None and artifact != artifact_digest:
            raise OutcomeStaleError(
                "the validation evidence is for another artifact")
        durable_ref = next(
            (item for item in payload.get("refs") or ()
             if isinstance(item, str) and item.startswith("evidence:")),
            None)
        if durable_ref is None:
            raise OutcomeTransitionError(
                f"evidence {ref!r} does not reference the evidence store")
        validation = validation_record_for_ref(store.root, durable_ref)
        if validation is None or validation.get("kind") != "validation":
            raise OutcomeTransitionError(
                f"evidence {ref!r} has no durable validation record")
        section = validation.get("validation")
        if not isinstance(section, Mapping) or section.get("passed") is not True:
            raise OutcomeTransitionError(
                "the validation evidence did not pass")
        if section.get("artifact_digest") != artifact:
            raise OutcomeStaleError(
                "the validation evidence digest does not match its record")
        validation_target = section.get("target")
        if not isinstance(validation_target, str):
            raise OutcomeTransitionError(
                "the validation record has no target")
        if target is not None and validation_target != target:
            raise OutcomeStaleError(
                "the validation evidence is for another target")
        path = resolve_target(store.root, validation_target)
        if sha256_file(path) != artifact:
            raise OutcomeStaleError(
                "the target changed after validation; the evidence is stale")
        resolved.append(validation)
    return resolved


def _decision_mismatch(gate: ReservedRecord,
                       decision: ReservedRecord) -> str | None:
    for name in ("revision_hash", "action_digest", "target", "expected_base",
                 "policy_version"):
        if decision.payload.get(name) != gate.payload.get(name):
            return name
    if tuple(decision.payload.get("evidence_refs") or ()) != tuple(
            gate.payload.get("evidence_refs") or ()):
        return "evidence_refs"
    return None


def gate_status(store: OutcomeStore, intent_id: str, gate_id: str, *,
                now_ms: int | None = None) -> GateStatus:
    """Derive the gate's current status from journal and target state."""
    if not isinstance(store, OutcomeStore):
        raise OutcomeValidationError("store must be an OutcomeStore")
    intent_value = R.identifier(intent_id, "intent_id")
    gate_value = R.identifier(gate_id, "gate_id")
    state = store.materialize()
    intent = state.intent(intent_value)
    gate = _gate(state, intent_value, gate_value)
    payload = gate.payload
    current = intent.revision_hash
    base = dict(
        intent_id=intent_value,
        gate_id=gate_value,
        required=bool(payload.get("required", True)),
        revision_hash=str(payload.get("revision_hash") or ""),
        current_revision_hash=current,
        action_digest=payload.get("action_digest"),
        target=payload.get("target"),
        expected_base=payload.get("expected_base"),
        policy_version=payload.get("policy_version"),
        evidence_refs=tuple(payload.get("evidence_refs") or ()),
        gate_ref=gate.record_id,
        expires_at=payload.get("expires_at"),
        record=gate.to_dict(),
    )
    if base["revision_hash"] != current:
        return GateStatus(status="stale", reason="intent revision changed",
                          **base)
    decision = _latest_decision(state, gate_value)
    if decision is not None:
        mismatch = _decision_mismatch(gate, decision)
        if mismatch is not None:
            return GateStatus(
                status="stale", decision_ref=decision.record_id,
                decision=decision.payload.get("decision"),
                reason=f"the decision {mismatch} does not match its gate",
                **base)
        decision_name = decision.payload.get("decision")
        if decision_name == "approved":
            try:
                _validation_records(
                    store, state, intent, base["evidence_refs"],
                    revision_hash=current, target=base["target"])
            except (OutcomeStaleError, OutcomeTransitionError,
                    OutcomeNotFoundError) as exc:
                return GateStatus(
                    status="stale", decision_ref=decision.record_id,
                    decision=decision_name, reason=str(exc), **base)
            return GateStatus(status="approved",
                              decision_ref=decision.record_id,
                              decision=decision_name, **base)
        return GateStatus(status=str(decision_name),
                          decision_ref=decision.record_id,
                          decision=decision_name, **base)
    expires = payload.get("expires_at")
    if expires is not None and _moment(now_ms) >= expires:
        return GateStatus(status="expired", reason="the gate expired", **base)
    if base["required"]:
        try:
            _validation_records(
                store, state, intent, base["evidence_refs"],
                revision_hash=current, target=base["target"])
        except (OutcomeStaleError, OutcomeTransitionError,
                OutcomeNotFoundError) as exc:
            return GateStatus(status="stale", reason=str(exc), **base)
    return GateStatus(status="pending", **base)


def pending_gate(store: OutcomeStore, intent_id: str, *,
                 now_ms: int | None = None) -> GateStatus | None:
    """The newest current-revision pending gate, or ``None``."""
    state = store.materialize()
    intent = state.intent(R.identifier(intent_id, "intent_id"))
    gates = sorted(
        (item for item in state.gates.values()
         if item.intent_id == intent.intent_id
         and item.payload.get("revision_hash") == intent.revision_hash),
        key=lambda item: item.seq)
    for gate in reversed(gates):
        status = gate_status(store, intent.intent_id,
                             R.identifier(gate.payload.get("gate_id"),
                                          "gate_id"), now_ms=now_ms)
        if status.status == "pending":
            return status
    return None


def create_gate(store: OutcomeStore, intent_id: str, gate_id: str, *,
                effect: EffectResult | None = None,
                validation: ValidationResult | None = None,
                action_digest: Any = None,
                target: Any = None,
                expected_base: Any = None,
                policy_version: Any = DEFAULT_POLICY_VERSION,
                evidence_refs: Any = None,
                required: bool = True,
                expires_at: int | None = None,
                hold: bool = True,
                detail: Any = None,
                actor: Any = None,
                idempotency_key: str | None = None,
                now_ms: int | None = None) -> dict[str, Any]:
    """Create a gate bound to the current revision and exact action."""
    if not isinstance(store, OutcomeStore):
        raise OutcomeValidationError("store must be an OutcomeStore")
    intent_value = R.identifier(intent_id, "intent_id")
    gate_value = R.identifier(gate_id, "gate_id")
    state = store.materialize()
    intent = state.intent(intent_value)
    revision = intent.revision_hash
    if effect is not None:
        if not isinstance(effect, EffectResult):
            raise OutcomeValidationError("effect must be an EffectResult")
        if effect.intent_id != intent_value:
            raise OutcomeValidationError(
                "the effect belongs to another intent")
        if effect.revision_hash != revision:
            raise OutcomeStaleError(
                "the effect belongs to another intent revision")
        if not effect.applied:
            raise OutcomeTransitionError(
                "a gate requires an applied or reconciled effect")
        if effect.action_digest:
            action_digest = action_digest or effect.action_digest
            if action_digest != effect.action_digest:
                raise OutcomeValidationError(
                    "the gate action digest does not match the effect")
        target = target or effect.target
        expected_base = expected_base or effect.expected_base
    if validation is not None:
        if not isinstance(validation, ValidationResult):
            raise OutcomeValidationError(
                "validation must be a ValidationResult")
        if validation.intent_id != intent_value:
            raise OutcomeValidationError(
                "the validation evidence belongs to another intent")
        if validation.revision_hash != revision:
            raise OutcomeStaleError(
                "the validation evidence belongs to another revision")
        if not validation.passed or not validation.current:
            raise OutcomeTransitionError(
                "a gate requires passed, current validation evidence")
        derived_refs = (_ref_list(evidence_refs)
                        if evidence_refs is not None
                        else (validation.evidence_ref,))
        if derived_refs != (validation.evidence_ref,):
            raise OutcomeValidationError(
                "the gate evidence does not name the supplied validation")
        evidence_refs = derived_refs
        target = target or validation.target
    target_value = R.ref(target, "target", required=False)
    action = (None if action_digest is None
              else R.ref(action_digest, "action_digest", required=False))
    base = (None if expected_base is None
            else R.ref(expected_base, "expected_base", required=False))
    policy = R.text(policy_version, "policy_version", limit=R.MAX_SHORT,
                    required=False)
    refs = _ref_list(evidence_refs or ())
    if required:
        missing = [name for name, value in (
            ("action_digest", action), ("target", target_value),
            ("expected_base", base), ("policy_version", policy))
            if not value]
        if missing:
            raise OutcomeValidationError(
                f"a required gate needs {missing}")
        if not refs:
            raise OutcomeValidationError(
                "a required gate needs validation evidence references")
        artifact = (effect.new_hash if effect is not None
                    else validation.artifact_digest
                    if validation is not None else None)
        _validation_records(store, state, intent, refs,
                            revision_hash=revision, target=target_value,
                            artifact_digest=artifact)
    result = store.record_gate(
        intent_value, gate_value, required=required, revision_hash=revision,
        action_digest=action, target=target_value, expected_base=base,
        policy_version=policy, expires_at=expires_at, evidence_refs=refs,
        detail=detail, actor=actor, idempotency_key=idempotency_key,
        now_ms=now_ms)
    transitioned = False
    if hold:
        current = store.materialize().intent(intent_value)
        if current.state in ("accepted", "running"):
            store.transition_intent(
                intent_value, "waiting_decision",
                reason=f"gate {gate_value} requires a decision",
                idempotency_key=_key("gate-hold", gate_value), actor=actor,
                now_ms=now_ms)
            transitioned = True
    status = gate_status(store, intent_value, gate_value, now_ms=now_ms)
    return {
        "intent_id": intent_value,
        "gate_id": gate_value,
        "gate_ref": result["record_id"],
        "status": status.status,
        "revision_hash": revision,
        "action_digest": action,
        "target": target_value,
        "expected_base": base,
        "policy_version": policy,
        "evidence_refs": list(refs),
        "required": required,
        "duplicate": bool(result["duplicate"]),
        "transitioned": transitioned,
        "record": result["record"],
    }


def decide_gate(store: OutcomeStore, intent_id: str, gate_id: str,
                decision: str, *, revision_hash: str | None = None,
                action_digest: Any = None, target: Any = None,
                expected_base: Any = None, policy_version: Any = None,
                evidence_refs: Any = None, detail: Any = None,
                actor: Any = None, idempotency_key: str | None = None,
                now_ms: int | None = None) -> dict[str, Any]:
    """Record an exact approve/reject decision for a current gate."""
    if not isinstance(store, OutcomeStore):
        raise OutcomeValidationError("store must be an OutcomeStore")
    intent_value = R.identifier(intent_id, "intent_id")
    gate_value = R.identifier(gate_id, "gate_id")
    decision_value = R.text(decision, "decision", limit=64)
    if decision_value not in R.DECISIONS:
        raise OutcomeValidationError(
            f"decision must be one of {R.DECISIONS}")
    state = store.materialize()
    intent = state.intent(intent_value)
    gate = _gate(state, intent_value, gate_value)
    current = intent.revision_hash
    gate_revision = str(gate.payload.get("revision_hash") or "")
    if gate_revision != current:
        raise OutcomeStaleError(
            "the gate is bound to an old intent revision")
    if revision_hash is not None and (
            R._hash(revision_hash, "revision_hash") != gate_revision):
        raise OutcomeStaleError(
            "the decision revision does not match the current gate")
    expected = {
        "action_digest": gate.payload.get("action_digest"),
        "target": gate.payload.get("target"),
        "expected_base": gate.payload.get("expected_base"),
        "policy_version": gate.payload.get("policy_version"),
    }
    supplied = {
        "action_digest": action_digest,
        "target": target,
        "expected_base": expected_base,
        "policy_version": policy_version,
    }
    for name, value in supplied.items():
        if value is None:
            continue
        if name == "policy_version":
            value = R.text(value, name, limit=R.MAX_SHORT, required=False)
        else:
            value = R.ref(value, name, required=False)
        if value != expected[name]:
            raise OutcomeStaleError(
                f"the decision {name} does not match its gate")
    gate_refs = tuple(gate.payload.get("evidence_refs") or ())
    if evidence_refs is not None:
        supplied_refs = _ref_list(evidence_refs)
        if supplied_refs != gate_refs:
            raise OutcomeStaleError(
                "the decision evidence does not match its gate")
    refs = gate_refs
    existing = _latest_decision(state, gate_value)
    if existing is not None:
        mismatch = _decision_mismatch(gate, existing)
        if mismatch is None and existing.payload.get("decision") == (
                decision_value):
            return {
                "intent_id": intent_value,
                "gate_id": gate_value,
                "decision": decision_value,
                "decision_ref": existing.record_id,
                "duplicate": True,
                "record": existing.to_dict(),
            }
        raise OutcomeTransitionError(
            f"gate {gate_value!r} is already decided")
    expires = gate.payload.get("expires_at")
    if expires is not None and _moment(now_ms) >= expires:
        raise OutcomeStaleError("the gate has expired")
    if decision_value == "approved":
        _validation_records(store, state, intent, refs,
                            revision_hash=current,
                            target=expected["target"])
    elif not refs and bool(gate.payload.get("required", True)):
        raise OutcomeTransitionError(
            "a required gate needs validation evidence references")
    result = store.record_decision(
        intent_value, gate_value, decision_value,
        revision_hash=gate_revision,
        action_digest=expected["action_digest"],
        target=expected["target"], expected_base=expected["expected_base"],
        policy_version=expected["policy_version"], evidence_refs=refs,
        detail=detail, actor=actor, idempotency_key=idempotency_key,
        now_ms=now_ms)
    return {
        "intent_id": intent_value,
        "gate_id": gate_value,
        "decision": decision_value,
        "decision_ref": result["record_id"],
        "duplicate": bool(result["duplicate"]),
        "record": result["record"],
    }


def approve_gate(store: OutcomeStore, intent_id: str, gate_id: str,
                 **kwargs: Any) -> dict[str, Any]:
    return decide_gate(store, intent_id, gate_id, "approved", **kwargs)


def reject_gate(store: OutcomeStore, intent_id: str, gate_id: str,
                **kwargs: Any) -> dict[str, Any]:
    return decide_gate(store, intent_id, gate_id, "rejected", **kwargs)


def finalize_outcome(store: OutcomeStore, intent_id: str, *,
                     gate_id: str | None = None,
                     decision_ref: str | None = None,
                     status: str = "satisfied",
                     evidence_refs: Any = (),
                     criteria_satisfied: Any = (),
                     summary: Any = None, detail: Any = None,
                     actor: Any = None,
                     idempotency_key: str | None = None,
                     now_ms: int | None = None) -> dict[str, Any]:
    """Record an outcome and apply the matching intent transition.

    ``satisfied`` is strict: the current revision, an approved exact gate and
    current passed validation evidence are all required.  ``waiting_decision``
    holds for a pending gate; ``partial`` and ``failed`` record the explicit
    status the caller supplies.  A task/worker success response alone never
    satisfies this path.
    """
    if not isinstance(store, OutcomeStore):
        raise OutcomeValidationError("store must be an OutcomeStore")
    intent_value = R.identifier(intent_id, "intent_id")
    status_value = R.text(status, "status", limit=64)
    if status_value not in R.OUTCOME_STATUSES:
        raise OutcomeValidationError(
            f"outcome status must be one of {R.OUTCOME_STATUSES}")
    state = store.materialize()
    intent = state.intent(intent_value)
    current = intent.revision_hash
    if status_value == "satisfied":
        if gate_id is None:
            raise OutcomeValidationError(
                "satisfied requires a gate_id")
        gate_value = R.identifier(gate_id, "gate_id")
        gate_state = gate_status(
            store, intent_value, gate_value, now_ms=now_ms)
        if gate_state.status == "stale":
            raise OutcomeStaleError(
                gate_state.reason or "the gate is stale")
        if gate_state.status != "approved":
            raise OutcomeTransitionError(
                f"gate {gate_value!r} is {gate_state.status}; satisfaction "
                "requires an approved decision")
        if decision_ref is not None and (
                R.ref(decision_ref, "decision_ref") !=
                gate_state.decision_ref):
            raise OutcomeStaleError(
                "the decision reference is not the approved gate decision")
        refs = (_ref_list(evidence_refs) if evidence_refs
                else tuple(gate_state.evidence_refs))
        if refs != tuple(gate_state.evidence_refs):
            raise OutcomeStaleError(
                "the outcome evidence does not match the approved gate")
        _validation_records(store, state, intent, refs,
                            revision_hash=current, target=gate_state.target)
        if intent.state == "satisfied":
            outcome = state.outcomes.get(intent.outcome_ref or "")
            if (outcome is not None
                    and outcome.payload.get("decision_ref") ==
                    gate_state.decision_ref):
                return {
                    "intent_id": intent_value,
                    "status": "satisfied",
                    "state": "satisfied",
                    "outcome_ref": outcome.record_id,
                    "decision_ref": gate_state.decision_ref,
                    "gate": gate_state.to_dict(),
                    "duplicate": True,
                    "transitioned": False,
                }
            raise OutcomeTransitionError(
                "the intent is already satisfied by another outcome")
        outcome_result = store.record_outcome(
            intent_value, status="satisfied", evidence_refs=refs,
            criteria_satisfied=criteria_satisfied,
            decision_ref=gate_state.decision_ref, summary=summary,
            detail=detail, revision_hash=current, actor=actor,
            idempotency_key=idempotency_key, now_ms=now_ms)
        transition = store.transition_intent(
            intent_value, "satisfied",
            outcome_ref=outcome_result["record_id"],
            idempotency_key=(None if idempotency_key is None
                             else _key("outcome-transition",
                                       f"{idempotency_key}:satisfied")),
            actor=actor, now_ms=now_ms)
        return {
            "intent_id": intent_value,
            "status": "satisfied",
            "state": transition["state"],
            "outcome_ref": outcome_result["record_id"],
            "decision_ref": gate_state.decision_ref,
            "gate": gate_state.to_dict(),
            "duplicate": bool(outcome_result["duplicate"]),
            "transitioned": True,
        }
    if status_value == "waiting_decision":
        if gate_id is None:
            raise OutcomeValidationError(
                "waiting_decision requires a gate_id")
        gate_value = R.identifier(gate_id, "gate_id")
        gate_state = gate_status(store, intent_value, gate_value,
                                 now_ms=now_ms)
        if gate_state.status != "pending":
            raise OutcomeTransitionError(
                f"gate {gate_value!r} is {gate_state.status}, not pending")
        transitioned = False
        if intent.state in ("accepted", "running"):
            store.transition_intent(
                intent_value, "waiting_decision",
                reason=f"gate {gate_value} requires a decision",
                idempotency_key=(None if idempotency_key is None
                                 else _key("outcome-transition",
                                           f"{idempotency_key}:waiting")),
                actor=actor, now_ms=now_ms)
            transitioned = True
        return {
            "intent_id": intent_value,
            "status": "waiting_decision",
            "state": store.materialize().intent(intent_value).state,
            "outcome_ref": None,
            "decision_ref": None,
            "gate": gate_state.to_dict(),
            "duplicate": False,
            "transitioned": transitioned,
        }
    outcome_result = store.record_outcome(
        intent_value, status=status_value,
        evidence_refs=_ref_list(evidence_refs or ()),
        criteria_satisfied=criteria_satisfied, decision_ref=decision_ref,
        summary=summary, detail=detail, revision_hash=current, actor=actor,
        idempotency_key=idempotency_key, now_ms=now_ms)
    transition = store.transition_intent(
        intent_value, status_value,
        outcome_ref=outcome_result["record_id"],
        idempotency_key=(None if idempotency_key is None
                         else _key("outcome-transition",
                                   f"{idempotency_key}:{status_value}")),
        actor=actor, now_ms=now_ms)
    return {
        "intent_id": intent_value,
        "status": status_value,
        "state": transition["state"],
        "outcome_ref": outcome_result["record_id"],
        "decision_ref": decision_ref,
        "gate": None,
        "duplicate": bool(outcome_result["duplicate"]),
        "transitioned": True,
    }


set_outcome = finalize_outcome


__all__ = [
    "DEFAULT_POLICY_VERSION",
    "GATE_STATUSES",
    "GateStatus",
    "approve_gate",
    "create_gate",
    "decide_gate",
    "finalize_outcome",
    "gate_status",
    "pending_gate",
    "reject_gate",
    "set_outcome",
]
