"""Stage 1 end-to-end integration over a disposable local Git repository.

Each fixture is a committed local repository under pytest's ``tmp_path``.  The
flow uses the real edit transaction, validation helper, gate/decision layer and
the read-only ``ozagia outcomes`` CLI.  No model, network, daemon or caller
checkout is used, and every durable file stays under the fixture root.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from ozagia import cli
from ozagia.core import evidence_store as ES
from ozagia.core.outcomes import (
    EffectAdapter,
    EffectRequest,
    EffectRevokedError,
    EffectStaleBaseError,
    EffectUncertainError,
    OutcomeStaleError,
    OutcomeStore,
    OutcomeTransitionError,
    approve_gate,
    create_gate,
    decide_gate,
    effect_records,
    effect_status,
    finalize_outcome,
    gate_status,
    pending_gate,
    revoke_pilot,
    validate_effect,
)
from ozagia.core.outcomes.view import (
    KIND_INTENT,
    KIND_LIST,
    VIEW_SCHEMA,
    list_view,
    view,
)
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
CRITERIA = [
    "app/base.py starts with a module docstring",
    "base() still returns 1",
]


@pytest.fixture(autouse=True)
def _isolated_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never inherit a caller's state directory into a fixture run."""
    for key in [key for key in os.environ if key.startswith("OZAGIA_")]:
        monkeypatch.delenv(key, raising=False)


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, check=True)


def _make_repo(parent: Path) -> Path:
    root = parent / "repo"
    (root / "app").mkdir(parents=True)
    (root / TARGET).write_text(ORIGINAL, encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "user.name", "Test Operator")
    _git(root, "add", TARGET)
    _git(root, "commit", "-qm", "initial")
    return root


def _setup(store: OutcomeStore) -> tuple[str, dict[str, Any],
                                         dict[str, Any]]:
    intent = store.submit_intent(
        "Add a one-line module docstring to app/base.py",
        criteria=CRITERIA,
        context_refs=["workspace:fixture"],
        project_ref="project:fixture",
        interpretation="one-file documentation change",
        intent_id="intent-1",
    )
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


def _snapshot(directory: Path) -> dict[str, bytes]:
    if not directory.exists():
        return {}
    return {
        path.relative_to(directory).as_posix(): path.read_bytes()
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


def _backups(root: Path) -> list[Path]:
    directory = root / ".ozagia" / "edits" / "backups"
    return sorted(directory.iterdir()) if directory.exists() else []


def _applied_gate(store: OutcomeStore, root: Path, *,
                  gate_id: str = "gate-1") -> tuple[str, Any]:
    intent, _, attempt = _setup(store)
    effect = EffectAdapter(store).apply(
        _request(root, intent, attempt["attempt_id"]))
    validation = validate_effect(store, effect)
    create_gate(
        store, intent, gate_id, effect=effect, validation=validation)
    return intent, effect


def _assert_confined(root: Path, state_dir: Path) -> None:
    assert root.is_dir()
    assert state_dir == (root / ".ozagia" / "outcomes").resolve()
    assert state_dir.is_relative_to(root)
    assert ES.journal_path(root).is_relative_to(root)


# ---------------------------------------------------------------------------
# Happy path, restart and read-only projection
# ---------------------------------------------------------------------------


def test_docstring_flow_survives_restart_at_gate_and_decision(
        tmp_path: Path) -> None:
    root = _make_repo(tmp_path)
    store = OutcomeStore(root)
    intent, work, attempt = _setup(store)

    request = _request(root, intent, attempt["attempt_id"])
    effect = EffectAdapter(store).apply(request)
    assert effect.state == "applied"
    assert effect.applied is True
    assert effect.new_hash == _sha(root / TARGET)
    assert (root / TARGET).read_text(encoding="utf-8") == EDITED
    assert effect.backup_ref is not None
    assert Path(effect.backup_ref).is_relative_to(root)
    assert Path(effect.backup_ref).read_bytes() == ORIGINAL.encode()
    assert effect.diff_digest is not None

    validation = validate_effect(store, effect)
    assert validation.passed is True
    assert validation.current is True
    assert validation.artifact_digest == effect.new_hash
    evidence_view = ES.read_store(root, kind="validation")
    assert evidence_view["state"] == "current"
    assert evidence_view["counts"]["total"] == 1
    evidence_record = evidence_view["records"][0]
    assert evidence_record["kind"] == "validation"
    assert evidence_record["validation"]["passed"] is True
    assert evidence_record["validation"]["target"] == TARGET
    assert evidence_record["validation"]["artifact_digest"] == effect.new_hash

    gate = create_gate(
        store, intent, "gate-1", effect=effect, validation=validation)
    assert gate["status"] == "pending"
    assert gate["action_digest"] == effect.action_digest
    assert gate["expected_base"] == request.expected_base
    assert store.inspect(intent)["intent"]["state"] == "waiting_decision"
    assert pending_gate(store, intent).gate_id == "gate-1"

    # Restart while the decision is pending: the effect must not be applied
    # twice and the view/list must read the same journal.
    journal_at_gate = store.journal.read_bytes()
    backups_at_gate = _backups(root)
    reloaded = OutcomeStore(root)
    assert reloaded.journal.read_bytes() == journal_at_gate
    pending = pending_gate(reloaded, intent)
    assert pending is not None
    assert pending.gate_id == "gate-1"
    assert pending.status == "pending"

    pending_view = view(intent, reloaded)
    assert pending_view["schema"] == VIEW_SCHEMA
    assert pending_view["kind"] == KIND_INTENT
    assert pending_view["status"] == "waiting_decision"
    assert pending_view["pending_decision"]["gate_id"] == "gate-1"
    assert pending_view["blocker"]["kind"] == "decision"
    assert pending_view["outcome"] is None
    assert [row["record_id"] for row in pending_view["evidence"]["verified"]] \
        == [validation.evidence_ref]

    pending_list = list_view(reloaded)
    assert pending_list["schema"] == VIEW_SCHEMA
    assert pending_list["kind"] == KIND_LIST
    assert pending_list["count"] == pending_list["total"] == 1
    row = pending_list["intents"][0]
    assert row["intent_id"] == intent
    assert row["state"] == "waiting_decision"
    assert row["pending_decision"]["gate_id"] == "gate-1"
    assert row["blocker"]["kind"] == "decision"
    assert row["outcome"] is None

    replay = EffectAdapter(reloaded).apply(request)
    assert replay.duplicate is True
    assert replay.effect_ref == effect.effect_ref
    assert replay.new_hash == effect.new_hash
    assert reloaded.journal.read_bytes() == journal_at_gate
    assert _backups(root) == backups_at_gate
    assert [item["state"] for item in effect_records(
        reloaded, intent, effect.effect_key)] == ["intended", "applied"]

    with pytest.raises(OutcomeTransitionError):
        finalize_outcome(reloaded, intent, gate_id="gate-1",
                         status="satisfied")
    assert reloaded.journal.read_bytes() == journal_at_gate

    finished = reloaded.finish_attempt(
        intent, attempt["attempt_id"], "succeeded",
        lease_generation=effect.lease_generation)
    assert finished["state"] == "terminal"
    assert finished["outcome"] == "succeeded"

    decision = approve_gate(
        reloaded, intent, "gate-1",
        detail="operator approved the exact diff")
    assert decision["decision"] == "approved"
    assert decision["duplicate"] is False
    journal_at_decision = reloaded.journal.read_bytes()
    assert len(reloaded.inspect(intent)["decisions"]) == 1

    # Restart after the decision: no re-ask and no duplicate decision.
    after_decision = OutcomeStore(root)
    assert after_decision.journal.read_bytes() == journal_at_decision
    approved = gate_status(after_decision, intent, "gate-1")
    assert approved.status == "approved"
    assert approved.decision_ref == decision["decision_ref"]
    assert pending_gate(after_decision, intent) is None

    repeated = approve_gate(after_decision, intent, "gate-1")
    assert repeated["duplicate"] is True
    assert repeated["decision_ref"] == decision["decision_ref"]
    assert after_decision.journal.read_bytes() == journal_at_decision
    assert len(after_decision.inspect(intent)["decisions"]) == 1

    approved_view = view(intent, after_decision)
    assert approved_view["pending_decision"] is None
    assert approved_view["blocker"]["kind"] == "finalize"
    assert approved_view["blocker"]["next_operation"] == "outcome.record"
    assert [row["record_id"] for row in approved_view["decisions"]] == [
        decision["decision_ref"]]
    assert approved_view["current_work"]["work_id"] == work["work_id"]
    assert approved_view["current_work"]["state"] == "completed"
    assert approved_view["current_attempt"]["state"] == "terminal"
    assert approved_view["current_attempt"]["outcome"] == "succeeded"

    approved_list = list_view(after_decision)
    assert approved_list["count"] == 1
    approved_row = approved_list["intents"][0]
    assert approved_row["state"] == "waiting_decision"
    assert approved_row["pending_decision"] is None
    assert approved_row["blocker"]["kind"] == "finalize"
    assert approved_row["outcome"] is None

    result = finalize_outcome(
        after_decision, intent, gate_id="gate-1",
        decision_ref=decision["decision_ref"],
        criteria_satisfied=CRITERIA,
        summary="Documented base() without changing behaviour")
    assert result["state"] == "satisfied"
    assert result["decision_ref"] == decision["decision_ref"]
    assert result["outcome_ref"].startswith("or-")
    journal_at_outcome = after_decision.journal.read_bytes()

    final = OutcomeStore(root)
    assert final.journal.read_bytes() == journal_at_outcome
    page = final.inspect(intent)
    assert page["intent"]["state"] == "satisfied"
    assert page["intent"]["outcome_ref"] == result["outcome_ref"]
    assert page["outcome"]["record_id"] == result["outcome_ref"]
    assert len(page["decisions"]) == 1
    assert len(page["outcomes"]) == 1
    assert [item["state"] for item in page["effects"]] == [
        "intended", "applied"]

    final_view = view(intent, final)
    assert final_view["status"] == "satisfied"
    assert final_view["outcome"]["record_id"] == result["outcome_ref"]
    assert final_view["outcome"]["status"] == "satisfied"
    assert final_view["latest_outcome"]["record_id"] == result["outcome_ref"]
    assert final_view["pending_decision"] is None
    assert final_view["blocker"] is None
    assert final_view["operations"] == []
    assert final_view["criteria_summary"] == {
        "total": 2, "satisfied": 2, "pending": 0}

    final_list = list_view(final)
    assert final_list["count"] == final_list["total"] == 1
    final_row = final_list["intents"][0]
    assert final_row["intent_id"] == intent
    assert final_row["state"] == "satisfied"
    assert final_row["outcome"]["record_id"] == result["outcome_ref"]
    assert final_row["outcome"]["status"] == "satisfied"
    assert final_row["last_seq"] == page["intent"]["last_seq"]
    assert final_list["cursor"] == page["cursor"]

    repeated_outcome = finalize_outcome(
        final, intent, gate_id="gate-1",
        decision_ref=decision["decision_ref"])
    assert repeated_outcome["duplicate"] is True
    assert repeated_outcome["transitioned"] is False
    assert final.journal.read_bytes() == journal_at_outcome
    assert len(final.inspect(intent)["outcomes"]) == 1

    _assert_confined(root, store.directory)
    assert set(tmp_path.iterdir()) == {root}
    assert _git(root, "diff", "--name-only").stdout.decode().splitlines() == [
        TARGET]


# ---------------------------------------------------------------------------
# Refusals and reconciliation
# ---------------------------------------------------------------------------


def test_stale_base_refuses_before_write_and_records_failure(
        tmp_path: Path) -> None:
    root = _make_repo(tmp_path)
    store = OutcomeStore(root)
    intent, _, attempt = _setup(store)
    expected = _sha(root / TARGET)
    drifted = ORIGINAL + "# external drift\n"
    (root / TARGET).write_text(drifted, encoding="utf-8")
    drifted_bytes = (root / TARGET).read_bytes()

    request = _request(
        root, intent, attempt["attempt_id"], expected=expected,
        effect_key="effect-stale")
    with pytest.raises(EffectStaleBaseError):
        EffectAdapter(store).apply(request)

    assert (root / TARGET).read_bytes() == drifted_bytes
    assert _backups(root) == []
    records = effect_records(store, intent, "effect-stale")
    assert [record["state"] for record in records] == ["intended", "failed"]
    assert records[-1]["observed_receipt"]["observed_base"] == _sha(
        root / TARGET)
    assert effect_status(store, intent, "effect-stale")["state"] == "failed"

    fresh = OutcomeStore(root)
    assert [record["state"] for record in effect_records(
        fresh, intent, "effect-stale")] == ["intended", "failed"]
    assert fresh.inspect(intent)["gates"] == []
    assert fresh.inspect(intent)["evidence"] == []
    stale_view = view(intent, fresh)
    assert stale_view["outcome"] is None
    assert stale_view["pending_decision"] is None
    assert stale_view["effects"][-1]["state"] == "failed"

    _assert_confined(root, store.directory)
    assert set(tmp_path.iterdir()) == {root}


def test_revision_change_invalidates_pending_and_approved_gates(
        tmp_path: Path) -> None:
    pending_root = _make_repo(tmp_path / "pending")
    pending_store = OutcomeStore(pending_root)
    pending_intent, pending_effect = _applied_gate(
        pending_store, pending_root)
    artifact = _sha(pending_root / TARGET)

    pending_store.revise_intent(
        pending_intent, "Investigate the docstring without editing the file",
        reason="operator steering")
    assert gate_status(
        pending_store, pending_intent, "gate-1").status == "stale"
    assert pending_gate(pending_store, pending_intent) is None
    with pytest.raises(OutcomeStaleError):
        decide_gate(pending_store, pending_intent, "gate-1", "approved")
    with pytest.raises(OutcomeStaleError):
        finalize_outcome(
            pending_store, pending_intent, gate_id="gate-1",
            status="satisfied")
    assert _sha(pending_root / TARGET) == artifact
    assert [record["state"] for record in effect_records(
        pending_store, pending_intent, pending_effect.effect_key)] == [
            "intended", "applied"]

    pending_view = view(pending_intent, OutcomeStore(pending_root))
    assert pending_view["pending_decision"] is None
    stale_gate = next(
        row for row in pending_view["gates"]
        if row.get("gate_id") == "gate-1")
    assert stale_gate["revision_current"] is False
    assert pending_view["outcome"] is None

    approved_root = _make_repo(tmp_path / "approved")
    approved_store = OutcomeStore(approved_root)
    approved_intent, _ = _applied_gate(approved_store, approved_root)
    decision = approve_gate(approved_store, approved_intent, "gate-1")
    approved_store.revise_intent(
        approved_intent, "Only investigate", reason="operator steering")

    approved_fresh = OutcomeStore(approved_root)
    assert gate_status(
        approved_fresh, approved_intent, "gate-1").status == "stale"
    assert pending_gate(approved_fresh, approved_intent) is None
    with pytest.raises(OutcomeStaleError):
        decide_gate(
            approved_fresh, approved_intent, "gate-1", "approved")
    with pytest.raises(OutcomeStaleError):
        finalize_outcome(
            approved_fresh, approved_intent, gate_id="gate-1",
            decision_ref=decision["decision_ref"], status="satisfied")

    approved_view = view(approved_intent, approved_fresh)
    assert approved_view["pending_decision"] is None
    assert approved_view["outcome"] is None
    assert [row["record_id"] for row in approved_view["decisions"]] == [
        decision["decision_ref"]]
    stale_approved = next(
        row for row in approved_view["gates"]
        if row.get("gate_id") == "gate-1")
    assert stale_approved["revision_current"] is False

    _assert_confined(pending_root, pending_store.directory)
    _assert_confined(approved_root, approved_store.directory)


def test_revocation_refuses_new_effect(tmp_path: Path) -> None:
    root = _make_repo(tmp_path)
    store = OutcomeStore(root)
    intent, _, attempt = _setup(store)
    original = (root / TARGET).read_bytes()

    revoked = revoke_pilot(
        store, intent, epoch=1, reason="operator revoked the pilot grant")
    assert revoked["epoch"] == 1
    assert revoked["targets"] == []

    with pytest.raises(EffectRevokedError):
        EffectAdapter(store).apply(
            _request(root, intent, attempt["attempt_id"]))
    assert (root / TARGET).read_bytes() == original
    assert _backups(root) == []
    assert store.inspect(intent)["effects"] == []

    fresh = OutcomeStore(root)
    assert fresh.inspect(intent)["effects"] == []
    assert view(intent, fresh)["effects"] == []
    assert any(
        record["payload"].get("kind") == "grant.revoked"
        for record in fresh.inspect(intent)["events"])

    _assert_confined(root, store.directory)
    assert set(tmp_path.iterdir()) == {root}


def test_uncertain_effect_is_reconciled_after_restart(
        tmp_path: Path) -> None:
    root = _make_repo(tmp_path)
    store = OutcomeStore(root)
    intent, _, attempt = _setup(store)
    request = _request(root, intent, attempt["attempt_id"])
    real_write = txn.write_source
    calls = {"count": 0}

    def ambiguous_write(root_path: Path, source: Any, new_text: str, *,
                        error_cls: type[Exception]) -> str:
        calls["count"] += 1
        real_write(root_path, source, new_text, error_cls=error_cls)
        raise OSError("simulated crash after replace")

    adapter = EffectAdapter(store, write=ambiguous_write)
    uncertain = adapter.apply(request)
    assert uncertain.state == "uncertain"
    assert uncertain.needs_reconciliation is True
    assert calls["count"] == 1
    digest = _sha(root / TARGET)
    assert digest != request.expected_base
    assert (root / TARGET).read_text(encoding="utf-8") == EDITED

    fresh = OutcomeStore(root)
    assert [record["state"] for record in effect_records(
        fresh, intent, uncertain.effect_key)] == ["intended", "uncertain"]
    with pytest.raises(EffectUncertainError):
        EffectAdapter(fresh).apply(request)
    assert calls["count"] == 1
    assert _sha(root / TARGET) == digest

    revoke_pilot(fresh, intent, epoch=1, reason="revoked mid-flight")
    reconciled = EffectAdapter(fresh).reconcile(
        intent, attempt["attempt_id"], uncertain.effect_key, "applied",
        note="operator confirmed the write landed")
    assert reconciled.state == "reconciled"
    assert reconciled.applied is True
    assert reconciled.resolution == "applied"
    assert reconciled.new_hash == digest
    assert calls["count"] == 1
    journal = fresh.journal.read_bytes()

    final = OutcomeStore(root)
    assert final.journal.read_bytes() == journal
    again = EffectAdapter(final).reconcile(
        intent, attempt["attempt_id"], uncertain.effect_key, "applied")
    assert again.duplicate is True
    assert final.journal.read_bytes() == journal
    assert [record["state"] for record in effect_records(
        final, intent, uncertain.effect_key)] == [
            "intended", "uncertain", "reconciled"]
    assert view(intent, final)["effects"][-1]["state"] == "reconciled"

    _assert_confined(root, store.directory)
    assert set(tmp_path.iterdir()) == {root}


# ---------------------------------------------------------------------------
# Read-only CLI
# ---------------------------------------------------------------------------


def test_read_only_cli_agrees_with_view_and_journal(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _make_repo(tmp_path)
    store = OutcomeStore(root)
    intent, _, attempt = _setup(store)
    effect = EffectAdapter(store).apply(
        _request(root, intent, attempt["attempt_id"]))
    validation = validate_effect(store, effect)
    create_gate(
        store, intent, "gate-1", effect=effect, validation=validation)

    journal_before = store.journal.read_bytes()
    files_before = _snapshot(root / ".ozagia")
    base = ["--root", str(root)]

    def run_json(argv: list[str]) -> dict[str, Any]:
        code = cli.main(["outcomes", *argv, *base, "--json"])
        out, err = capsys.readouterr()
        assert code == 0
        assert err == ""
        return json.loads(out)

    detail = run_json(["show", intent])
    assert detail == view(intent, store)
    assert detail["schema"] == VIEW_SCHEMA
    assert detail["kind"] == KIND_INTENT
    assert detail["pending_decision"]["gate_id"] == "gate-1"

    listing = run_json(["list"])
    assert listing == list_view(store)
    assert listing["schema"] == VIEW_SCHEMA
    assert listing["kind"] == KIND_LIST
    assert listing["count"] == 1
    assert listing["intents"][0]["intent_id"] == intent
    assert listing["intents"][0]["pending_decision"]["gate_id"] == "gate-1"

    events = run_json(["events", intent, "--after", "0"])
    expected_page = store.follow(0, intent_id=intent)
    assert events["schema"] == VIEW_SCHEMA
    assert events["kind"] == "events"
    assert events["intent_id"] == intent
    assert events["after_seq"] == 0
    assert events["records"] == expected_page["records"]
    assert events["cursor"] == expected_page["cursor"]
    assert events["journal_state"] == "current"
    assert events["tail_truncated"] is False

    code = cli.main(["outcomes", "show", intent, *base])
    out, err = capsys.readouterr()
    assert code == 0 and err == ""
    assert intent in out
    assert "pending decision: gate-1" in out
    assert "blocker: decision" in out

    code = cli.main(["outcomes", "list", *base])
    out, err = capsys.readouterr()
    assert code == 0 and err == ""
    assert "decision=gate-1" in out

    code = cli.main(["outcomes", "events", intent, *base])
    out, err = capsys.readouterr()
    assert code == 0 and err == ""
    assert "events after 0" in out
    assert "intent.submitted" in out

    assert store.journal.read_bytes() == journal_before
    assert _snapshot(root / ".ozagia") == files_before

    store.finish_attempt(
        intent, attempt["attempt_id"], "succeeded",
        lease_generation=effect.lease_generation)
    decision = approve_gate(store, intent, "gate-1")
    outcome = finalize_outcome(
        store, intent, gate_id="gate-1",
        decision_ref=decision["decision_ref"], criteria_satisfied=CRITERIA)
    journal_final = store.journal.read_bytes()
    files_final = _snapshot(root / ".ozagia")

    final_detail = run_json(["show", intent])
    assert final_detail["status"] == "satisfied"
    assert final_detail["outcome"]["record_id"] == outcome["outcome_ref"]
    assert final_detail["pending_decision"] is None
    final_listing = run_json(["list"])
    assert final_listing["intents"][0]["state"] == "satisfied"
    assert final_listing["intents"][0]["outcome"]["record_id"] == (
        outcome["outcome_ref"])
    final_events = run_json(["events", intent])
    assert final_events["records"] == store.follow(
        0, intent_id=intent)["records"]

    assert store.journal.read_bytes() == journal_final
    assert _snapshot(root / ".ozagia") == files_final
    _assert_confined(root, store.directory)
    assert set(tmp_path.iterdir()) == {root}


# ---------------------------------------------------------------------------
# Forward-compatible rendering
# ---------------------------------------------------------------------------


class _ForwardReader:
    """Forward a real store while adding future record types in memory."""

    def __init__(self, store: OutcomeStore) -> None:
        self.store = store

    def inspect(self, intent_id: str, *,
                events_limit: int = 50) -> dict[str, Any]:
        page = copy.deepcopy(self.store.inspect(
            intent_id, events_limit=events_limit))
        page["events"].append({
            "schema": "ozagia.outcomes.record.v1",
            "seq": 99,
            "record_id": "or-00000099-abcdefabcdef",
            "event": "future.signal",
            "intent_id": intent_id,
            "actor": "operator",
            "timestamp": 123,
            "correlation_id": None,
            "causation_id": None,
            "payload": {
                "schema": "ozagia.outcomes.signal.v1",
                "note": "kept",
            },
            "idempotency_key": "future-1",
            "prev_hash": "0" * 64,
            "record_hash": "0" * 64,
        })
        page["gates"].append({
            "schema": "ozagia.outcomes.future_gate.v1",
            "record_id": "or-00000098-fedcbafedcba",
            "seq": 98,
            "timestamp": 122,
            "actor": "operator",
            "intent_id": intent_id,
            "gate_id": "future-gate",
            "required": True,
        })
        page["signals"] = [{
            "schema": "ozagia.outcomes.signal.v1",
            "value": 3,
        }]
        return page

    def follow(self, after_seq: int = 0, *, intent_id: str | None = None,
               limit: int = 100) -> dict[str, Any]:
        return self.store.follow(
            after_seq, intent_id=intent_id, limit=limit)

    def list_active(self, limit: int = 100) -> dict[str, Any]:
        return self.store.list_active(limit=limit)


def test_unknown_record_types_render_generically_without_writes(
        tmp_path: Path) -> None:
    root = _make_repo(tmp_path)
    store = OutcomeStore(root)
    intent = store.submit_intent(
        "Keep future record types visible", criteria=CRITERIA,
        intent_id="intent-1")["intent_id"]
    store.add_work(intent, "Change app/base.py")
    journal = store.journal.read_bytes()

    result = view(intent, _ForwardReader(store))
    unknown_event = next(
        row for row in result["records"]
        if row.get("event") == "future.signal")
    assert unknown_event["display_type"] == "other"
    assert "Unrecognised event type" in unknown_event["summary"]
    assert unknown_event["record"]["payload"]["note"] == "kept"

    unknown_gate = next(
        row for row in result["gates"]
        if row.get("record_id") == "or-00000098-fedcbafedcba")
    assert unknown_gate["display_type"] == "other"
    assert unknown_gate["data"]["gate_id"] == "future-gate"

    unknown_collection = next(
        row for row in result["other"] if row.get("source") == "signals")
    assert unknown_collection["display_type"] == "other"
    assert unknown_collection["data"]["value"] == 3
    other_ids = {
        row.get("record_id") for row in result["other"]
        if row.get("record_id")
    }
    assert "or-00000099-abcdefabcdef" in other_ids
    assert "or-00000098-fedcbafedcba" in other_ids
    assert store.journal.read_bytes() == journal

    # The forward-compatible rows were injected at the reader boundary only.
    fresh = OutcomeStore(root)
    clean = view(intent, fresh)
    assert all(
        row.get("event") != "future.signal" for row in clean["records"])
    assert all(
        row.get("record_id") != "or-00000098-fedcbafedcba"
        for row in clean["gates"])
    assert clean["other"] == []
    assert fresh.journal.read_bytes() == journal

    _assert_confined(root, store.directory)
    assert set(tmp_path.iterdir()) == {root}
