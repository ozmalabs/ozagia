"""Stage 1 effect, validation, gate and evidence tests (S1-B).

Every test uses a disposable local Git repository under pytest's ``tmp_path``.
No model, network, daemon or live target is used.
"""

from __future__ import annotations

import dataclasses
import hashlib
import subprocess
from pathlib import Path
from typing import Any

import pytest

from ozagia.core import evidence_store as ES
from ozagia.core.outcomes import (
    EffectAdapter,
    EffectRequest,
    EffectRevokedError,
    EffectUncertainError,
    OutcomeConflictError,
    OutcomeStaleError,
    OutcomeStore,
    OutcomeTransitionError,
    OutcomeValidationError,
    approve_gate,
    create_gate,
    decide_gate,
    effect_records,
    finalize_outcome,
    gate_status,
    pending_gate,
    revoke_pilot,
    validate_effect,
)
from ozagia.core.outcomes.validation import ValidationResult
from ozagia.edit import txn

TARGET = "app/base.py"
ORIGINAL = "def base():\n    return 1\n"
EDITED = (
    '"""Base module providing the base() helper function."""\n'
    "\n"
    "\n"
    "def base():\n"
    "    return 1\n"
)


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, check=True)


def _make_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "app").mkdir(parents=True)
    (root / TARGET).write_text(ORIGINAL, encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "user.name", "Test Operator")
    _git(root, "add", TARGET)
    _git(root, "commit", "-qm", "initial")
    return root


def _setup(store: OutcomeStore) -> tuple[str, dict[str, Any], dict[str, Any]]:
    intent = store.submit_intent(
        "Add a one-line module docstring to app/base.py",
        criteria=["app/base.py starts with a module docstring",
                  "base() still returns 1"],
        context_refs=["workspace:fixture"],
        project_ref="project:fixture",
        interpretation="one-file documentation change",
        intent_id="intent-1")
    work = store.add_work(intent["intent_id"], "Add the docstring")
    attempt = store.start_attempt(intent["intent_id"], work["work_id"])
    return intent["intent_id"], work, attempt


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _request(root: Path, intent: str, attempt: str, *,
             expected: str | None = None, new_text: str = EDITED,
             effect_key: str | None = None) -> EffectRequest:
    return EffectRequest(
        intent_id=intent,
        attempt_id=attempt,
        target=TARGET,
        expected_base=expected or _sha(root / TARGET),
        new_text=new_text,
        effect_key=effect_key)


# ---------------------------------------------------------------------------
# Happy path and exact binding
# ---------------------------------------------------------------------------


def test_happy_path_effect_validation_gate_and_satisfaction(
        tmp_path: Path) -> None:
    root = _make_repo(tmp_path)
    store = OutcomeStore(root)
    intent, work, attempt = _setup(store)
    original = (root / TARGET).read_bytes()
    request = _request(root, intent, attempt["attempt_id"])
    effect = EffectAdapter(store).apply(request)

    assert effect.state == "applied"
    assert effect.applied is True
    assert effect.duplicate is False
    assert effect.new_hash == _sha(root / TARGET)
    assert effect.observed_base == request.expected_base
    assert effect.effect_ref is not None
    assert effect.lease_generation == attempt["lease_generation"]
    assert effect.backup_ref is not None
    assert Path(effect.backup_ref).read_bytes() == original
    assert (root / TARGET).read_text(encoding="utf-8") == EDITED
    assert effect.diff_digest is not None

    finished = store.finish_attempt(
        intent, attempt["attempt_id"], "succeeded",
        lease_generation=effect.lease_generation)
    assert finished["state"] == "terminal"
    assert store.inspect(intent)["evidence"] == []

    # A worker's success response is not validation evidence.
    with pytest.raises(OutcomeValidationError):
        create_gate(
            store, intent, "gate-0", effect=effect,
            policy_version="stage1-policy.v1", hold=False)

    validation = validate_effect(store, effect)
    assert isinstance(validation, ValidationResult)
    assert validation.passed is True
    assert validation.current is True
    assert validation.artifact_digest == effect.new_hash
    assert validation.freshness == "current"
    assert validation.evidence_ref.startswith("or-")
    assert [check.name for check in validation.checks] == [
        "git_diff_check", "python_ast_parse"]
    assert all(check.exit_code == 0 for check in validation.checks)

    evidence_view = ES.read_store(root, kind="validation")
    assert evidence_view["state"] == "current"
    assert evidence_view["counts"]["total"] == 1
    evidence_record = evidence_view["records"][0]
    assert evidence_record["kind"] == "validation"
    assert evidence_record["validation"]["passed"] is True
    assert evidence_record["validation"]["target"] == TARGET
    assert evidence_record["validation"]["artifact_digest"] == effect.new_hash
    assert evidence_record["evidence"] == "verified"
    assert evidence_record["freshness"] == "current"

    outcome_evidence = [
        item for item in store.inspect(intent)["evidence"]
        if item["record_id"] == validation.evidence_ref
    ]
    assert len(outcome_evidence) == 1
    assert outcome_evidence[0]["source"] == "ozagia.outcomes.validation"
    assert any(ref == f"evidence:{validation.evidence_record_id}"
               for ref in outcome_evidence[0]["refs"])

    gate = create_gate(
        store, intent, "gate-1", effect=effect, validation=validation,
        policy_version="stage1-policy.v1")
    assert gate["status"] == "pending"
    assert gate["action_digest"] == effect.action_digest
    assert gate["target"] == TARGET
    assert gate["expected_base"] == request.expected_base
    assert store.inspect(intent)["intent"]["state"] == "waiting_decision"
    assert pending_gate(store, intent).gate_id == "gate-1"

    with pytest.raises(OutcomeTransitionError):
        finalize_outcome(store, intent, gate_id="gate-1",
                         status="satisfied")

    decision = approve_gate(store, intent, "gate-1",
                            detail="operator approved the exact diff")
    assert decision["decision"] == "approved"
    assert decision["duplicate"] is False
    status = gate_status(store, intent, "gate-1")
    assert status.status == "approved"
    assert status.decision_ref == decision["decision_ref"]
    assert pending_gate(store, intent) is None

    result = finalize_outcome(
        store, intent, gate_id="gate-1", decision_ref=decision["decision_ref"],
        criteria_satisfied=["app/base.py starts with a module docstring"],
        summary="Documented base() without changing behaviour")
    assert result["state"] == "satisfied"
    assert result["decision_ref"] == decision["decision_ref"]
    assert result["outcome_ref"].startswith("or-")

    page = store.inspect(intent)
    assert page["intent"]["state"] == "satisfied"
    assert page["intent"]["outcome_ref"] == result["outcome_ref"]
    assert page["outcome"]["status"] == "satisfied"
    assert page["outcome"]["decision_ref"] == decision["decision_ref"]
    assert [item["state"] for item in page["effects"]] == [
        "intended", "applied"]

    # Everything durable lives under the fixture's state directory.
    assert store.directory.is_relative_to(root)
    assert ES.journal_path(root).is_relative_to(root)


# ---------------------------------------------------------------------------
# Replay, idempotency and stale base
# ---------------------------------------------------------------------------


def test_replay_does_not_apply_twice_and_stale_base_refuses(
        tmp_path: Path) -> None:
    root = _make_repo(tmp_path)
    store = OutcomeStore(root)
    intent, work, attempt = _setup(store)
    request = _request(root, intent, attempt["attempt_id"])
    adapter = EffectAdapter(store)
    first = adapter.apply(request)
    assert first.state == "applied"
    lines = store.journal.read_text(encoding="utf-8").splitlines()
    backups = list((root / ".ozagia" / "edits" / "backups").iterdir())

    replay = adapter.apply(request)
    assert replay.duplicate is True
    assert replay.state == "applied"
    assert replay.effect_ref == first.effect_ref
    assert replay.new_hash == first.new_hash
    assert store.journal.read_text(encoding="utf-8").splitlines() == lines
    assert list((root / ".ozagia" / "edits" / "backups").iterdir()) == backups

    with pytest.raises(OutcomeConflictError):
        adapter.apply(dataclasses.replace(
            request, new_text=EDITED + "\n# different action\n",
            effect_key=first.effect_key))

    # Change the target without an intent/effect record, then try a different
    # action against the old base.  It must refuse before writing.
    (root / TARGET).write_text(ORIGINAL + "# external drift\n", encoding="utf-8")
    drifted = (root / TARGET).read_bytes()
    stale_request = _request(
        root, intent, attempt["attempt_id"], expected=first.expected_base,
        new_text=EDITED + "\n# second edit\n", effect_key="effect-stale")
    with pytest.raises(OutcomeStaleError):
        adapter.apply(stale_request)
    assert (root / TARGET).read_bytes() == drifted

    stale_records = effect_records(store, intent, "effect-stale")
    assert [item["state"] for item in stale_records] == ["intended", "failed"]
    assert stale_records[-1]["observed_receipt"]["observed_base"] == (
        hashlib.sha256(drifted).hexdigest())


def test_validation_failure_is_recorded_but_not_gate_evidence(
        tmp_path: Path) -> None:
    root = _make_repo(tmp_path)
    store = OutcomeStore(root)
    intent, work, attempt = _setup(store)
    broken = "def base(:\n    return 1\n"
    request = _request(root, intent, attempt["attempt_id"], new_text=broken)
    effect = EffectAdapter(store).apply(request)
    validation = validate_effect(store, effect)

    assert validation.passed is False
    assert validation.freshness == "current"
    assert validation.checks[-1].name == "python_ast_parse"
    assert validation.checks[-1].exit_code != 0
    evidence_view = ES.read_store(root, kind="validation")
    assert evidence_view["records"][0]["evidence"] == "observed"
    assert evidence_view["records"][0]["verification"]["state"] == (
        "unverified")

    with pytest.raises(OutcomeTransitionError):
        create_gate(
            store, intent, "gate-broken", effect=effect,
            validation=validation, policy_version="stage1-policy.v1")


def test_validation_evidence_is_idempotent_per_observation(
        tmp_path: Path) -> None:
    root = _make_repo(tmp_path)
    store = OutcomeStore(root)
    intent, work, attempt = _setup(store)
    effect = EffectAdapter(store).apply(
        _request(root, intent, attempt["attempt_id"]))

    first = validate_effect(store, effect, now_ms=1_000)
    repeat = validate_effect(store, effect, now_ms=1_000)
    assert repeat.duplicate is True
    assert repeat.evidence_ref == first.evidence_ref
    assert repeat.evidence_record_id == first.evidence_record_id
    assert ES.read_store(root, kind="validation")["counts"]["total"] == 1

    later = validate_effect(store, effect, now_ms=1_001)
    assert later.evidence_ref != first.evidence_ref
    assert ES.read_store(root, kind="validation")["counts"]["total"] == 2
    assert len(store.inspect(intent)["evidence"]) == 2


# ---------------------------------------------------------------------------
# Restart and reload
# ---------------------------------------------------------------------------


def test_restart_resumes_at_pending_gate_then_after_decision(
        tmp_path: Path) -> None:
    root = _make_repo(tmp_path)
    store = OutcomeStore(root)
    intent, work, attempt = _setup(store)
    request = _request(root, intent, attempt["attempt_id"])
    effect = EffectAdapter(store).apply(request)
    validation = validate_effect(store, effect)
    create_gate(store, intent, "gate-1", effect=effect,
                validation=validation, policy_version="stage1-policy.v1")

    reloaded = OutcomeStore(root)
    pending = pending_gate(reloaded, intent)
    assert pending is not None
    assert pending.gate_id == "gate-1"
    assert pending.status == "pending"
    assert reloaded.inspect(intent)["intent"]["state"] == "waiting_decision"
    before = _sha(root / TARGET)
    replay = EffectAdapter(reloaded).apply(request)
    assert replay.duplicate is True
    assert _sha(root / TARGET) == before
    assert [item["state"] for item in
            reloaded.inspect(intent)["effects"]] == ["intended", "applied"]

    decision = approve_gate(reloaded, intent, "gate-1")
    again = OutcomeStore(root)
    status = gate_status(again, intent, "gate-1")
    assert status.status == "approved"
    assert status.decision_ref == decision["decision_ref"]
    assert pending_gate(again, intent) is None

    result = finalize_outcome(
        again, intent, gate_id="gate-1",
        decision_ref=decision["decision_ref"])
    assert result["state"] == "satisfied"
    repeat = finalize_outcome(
        again, intent, gate_id="gate-1",
        decision_ref=decision["decision_ref"])
    assert repeat["duplicate"] is True
    assert repeat["transitioned"] is False
    page = again.inspect(intent)
    assert len(page["decisions"]) == 1
    assert [item["state"] for item in page["effects"]] == [
        "intended", "applied"]


# ---------------------------------------------------------------------------
# Decision exactness and rejection
# ---------------------------------------------------------------------------


def test_decision_rejects_stale_action_base_or_target(tmp_path: Path) -> None:
    root = _make_repo(tmp_path)
    store = OutcomeStore(root)
    intent, work, attempt = _setup(store)
    effect = EffectAdapter(store).apply(
        _request(root, intent, attempt["attempt_id"]))
    validation = validate_effect(store, effect)
    create_gate(store, intent, "gate-1", effect=effect,
                validation=validation, policy_version="stage1-policy.v1")

    with pytest.raises(OutcomeStaleError):
        decide_gate(store, intent, "gate-1", "approved",
                    action_digest="a" * 64)
    with pytest.raises(OutcomeStaleError):
        decide_gate(store, intent, "gate-1", "approved",
                    expected_base="b" * 64)
    with pytest.raises(OutcomeStaleError):
        decide_gate(store, intent, "gate-1", "approved",
                    target="app/other.py")
    with pytest.raises(OutcomeStaleError):
        decide_gate(store, intent, "gate-1", "approved",
                    evidence_refs=["or-00000001-000000000000"])

    rejected = decide_gate(store, intent, "gate-1", "rejected",
                           detail="operator rejected the change")
    assert rejected["decision"] == "rejected"
    assert gate_status(store, intent, "gate-1").status == "rejected"
    with pytest.raises(OutcomeTransitionError):
        finalize_outcome(store, intent, gate_id="gate-1",
                         status="satisfied")


# ---------------------------------------------------------------------------
# Revision drift
# ---------------------------------------------------------------------------


def test_revision_change_invalidates_pending_gate_and_decision(
        tmp_path: Path) -> None:
    root = _make_repo(tmp_path)
    store = OutcomeStore(root)
    intent, work, attempt = _setup(store)
    effect = EffectAdapter(store).apply(
        _request(root, intent, attempt["attempt_id"]))
    validation = validate_effect(store, effect)
    create_gate(store, intent, "gate-1", effect=effect,
                validation=validation, policy_version="stage1-policy.v1")

    store.revise_intent(
        intent, "Investigate the docstring without changing app/base.py",
        reason="operator steering")
    status = gate_status(store, intent, "gate-1")
    assert status.status == "stale"
    before = _sha(root / TARGET)
    with pytest.raises(OutcomeStaleError):
        EffectAdapter(store).apply(_request(
            root, intent, attempt["attempt_id"],
            new_text=EDITED + "\n# revision two correction\n"))
    assert _sha(root / TARGET) == before
    with pytest.raises(OutcomeStaleError):
        decide_gate(store, intent, "gate-1", "approved")
    with pytest.raises(OutcomeStaleError):
        finalize_outcome(store, intent, gate_id="gate-1",
                         status="satisfied")

    # A decision recorded before steering is also invalid afterwards.
    root2 = _make_repo(tmp_path / "second")
    store2 = OutcomeStore(root2)
    intent2, work2, attempt2 = _setup(store2)
    effect2 = EffectAdapter(store2).apply(
        _request(root2, intent2, attempt2["attempt_id"]))
    validation2 = validate_effect(store2, effect2)
    create_gate(store2, intent2, "gate-1", effect=effect2,
                validation=validation2, policy_version="stage1-policy.v1")
    approve_gate(store2, intent2, "gate-1")
    store2.revise_intent(intent2, "Only investigate", reason="steering")
    assert gate_status(store2, intent2, "gate-1").status == "stale"
    with pytest.raises(OutcomeStaleError):
        finalize_outcome(store2, intent2, gate_id="gate-1",
                         status="satisfied")


# ---------------------------------------------------------------------------
# Revocation and uncertain effects
# ---------------------------------------------------------------------------


def test_revocation_refuses_new_effect_and_allows_reconciliation(
        tmp_path: Path) -> None:
    root = _make_repo(tmp_path)
    store = OutcomeStore(root)
    intent, work, attempt = _setup(store)
    adapter = EffectAdapter(store)
    request = _request(root, intent, attempt["attempt_id"])
    original = (root / TARGET).read_bytes()

    revoked = revoke_pilot(
        store, intent, epoch=1, reason="operator revoked the pilot grant")
    assert revoked["epoch"] == 1
    with pytest.raises(EffectRevokedError):
        adapter.apply(request)
    assert (root / TARGET).read_bytes() == original
    assert effect_records(store, intent, "effect-never-written") == []

    # A higher epoch is not covered by the earlier revocation.
    request2 = dataclasses.replace(
        request, grant_epoch=2, effect_key="effect-epoch-2")
    applied = adapter.apply(request2)
    assert applied.state == "applied"

    # The uncertain path is deliberately not blocked by revocation: the write
    # may already have happened, so reconciliation is the only safe operation.
    real_write = txn.write_source
    calls = {"count": 0}

    def ambiguous_write(root_path, source, new_text, *, error_cls):
        calls["count"] += 1
        real_write(root_path, source, new_text, error_cls=error_cls)
        raise OSError("simulated crash after replace")

    store2_root = _make_repo(tmp_path / "uncertain")
    store2 = OutcomeStore(store2_root)
    intent2, work2, attempt2 = _setup(store2)
    request3 = _request(store2_root, intent2, attempt2["attempt_id"])
    uncertain_adapter = EffectAdapter(store2, write=ambiguous_write)
    uncertain = uncertain_adapter.apply(request3)
    assert uncertain.state == "uncertain"
    assert calls["count"] == 1
    digest = _sha(store2_root / TARGET)

    with pytest.raises(EffectUncertainError):
        uncertain_adapter.apply(request3)
    assert calls["count"] == 1
    assert _sha(store2_root / TARGET) == digest

    revoke_pilot(store2, intent2, epoch=1, reason="revoked mid-flight")
    reconciled = uncertain_adapter.reconcile(
        intent2, attempt2["attempt_id"], uncertain.effect_key, "applied",
        note="operator confirmed the write landed")
    assert reconciled.state == "reconciled"
    assert reconciled.applied is True
    assert reconciled.resolution == "applied"
    assert reconciled.new_hash == digest
    again = uncertain_adapter.reconcile(
        intent2, attempt2["attempt_id"], uncertain.effect_key, "applied")
    assert again.duplicate is True
    assert [item["state"] for item in effect_records(
        store2, intent2, uncertain.effect_key)] == [
            "intended", "uncertain", "reconciled"]
