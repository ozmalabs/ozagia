"""Tests for the durable outcome journal and Stage 1 lifecycle (S1-A)."""

from __future__ import annotations

import json
import multiprocessing
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from ozagia.core import contracts as C
from ozagia.core.outcomes import (
    OutcomeBoundsError,
    OutcomeConflictError,
    OutcomeIntegrityError,
    OutcomeNotFoundError,
    OutcomeStaleError,
    OutcomeStore,
    OutcomeTransitionError,
    OutcomeValidationError,
    journal_path,
    recover,
)
from ozagia.core.outcomes import records as R


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


def _store(tmp_path: Path, *, name: str = "repo",
           state: str = "state") -> OutcomeStore:
    root = tmp_path / name
    root.mkdir(exist_ok=True)
    return OutcomeStore(root, state_dir=root / state)


def _lines(store: OutcomeStore) -> list[str]:
    return store.journal.read_text(encoding="utf-8").splitlines()


def _records(store: OutcomeStore) -> list[dict]:
    return [json.loads(line) for line in _lines(store)]


def _intent(store: OutcomeStore, intent_id: str = "intent-1") -> str:
    result = store.submit_intent(
        "Add a module docstring", criteria=["docstring present"],
        context_refs=["workspace:fixture"], project_ref="project:fixture",
        interpretation="one-file documentation change",
        conversation_ref="conversation:1", intent_id=intent_id)
    return result["intent_id"]


def _process_submit(root: str, state: str, index: int, barrier) -> None:
    store = OutcomeStore(Path(root), state_dir=Path(state))
    barrier.wait(timeout=20)
    store.submit_intent(f"process intent {index}",
                        intent_id=f"intent-process-{index}")


# ---------------------------------------------------------------------------
# Store, submit, reload and replay
# ---------------------------------------------------------------------------


def test_submit_is_canonical_reloads_and_replays(tmp_path: Path) -> None:
    store = _store(tmp_path)
    result = store.submit_intent(
        "Add a module docstring", criteria=["docstring present", "no change"],
        context_refs=["workspace:fixture"], project_ref="project:fixture",
        interpretation="documentation only", principal="alice",
        conversation_ref="conversation:1", intent_id="intent-1")

    assert result["intent_id"] == "intent-1"
    assert result["revision"] == 1
    assert result["state"] == "accepted"
    assert result["duplicate"] is False

    lines = _lines(store)
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["schema"] == "ozagia.outcomes.record.v1"
    assert record["seq"] == 1
    assert record["event"] == "intent.submitted"
    assert record["intent_id"] == "intent-1"
    assert record["actor"] == "operator"
    assert isinstance(record["timestamp"], int)
    assert record["correlation_id"] is None
    assert record["causation_id"] is None
    assert record["prev_hash"] is None
    assert record["payload"]["revision"] == 1
    assert record["payload"]["parent"] is None
    assert record["payload"]["criteria"] == ["docstring present", "no change"]
    assert record["payload"]["project_ref"] == "project:fixture"
    assert record["payload"]["interpretation"] == "documentation only"
    assert lines[0] == C.canonical_json(record)
    assert record["record_hash"] == C.content_hash({
        key: value for key, value in record.items() if key != "record_hash"
    })
    assert (store.directory / "journal.jsonl").exists()
    assert (store.directory / ".lock").exists()

    reloaded = _store(tmp_path)
    page = reloaded.inspect("intent-1")
    assert page["journal"]["state"] == "current"
    assert page["intent"]["state"] == "accepted"
    assert page["intent"]["revision"] == 1
    assert page["intent"]["principal"] == "alice"
    assert page["revisions"][0]["requested_outcome"] == (
        "Add a module docstring")
    assert page["revisions"][0]["revision_hash"] == result["revision_hash"]
    assert page["work"] == []
    assert page["attempts"] == []
    assert len(page["events"]) == 1

    state = reloaded.materialize()
    assert state.cursor == 1
    assert state.intents["intent-1"].current_revision.revision_hash == (
        result["revision_hash"])

    assert not (reloaded.root / ".ozagia" / "graph.json").exists()
    assert not (reloaded.root / "graph.json").exists()


# ---------------------------------------------------------------------------
# Immutable steering and stale queued work
# ---------------------------------------------------------------------------


def test_revision_is_immutable_and_stales_only_queued_work(
        tmp_path: Path) -> None:
    store = _store(tmp_path)
    intent = _intent(store)
    queued = store.add_work(intent, "queued change")
    running = store.add_work(intent, "running change")
    attempt = store.start_attempt(intent, running["work_id"])
    before = store.inspect(intent)
    old_revision = dict(before["revisions"][0])

    revised = store.revise_intent(
        intent, "Investigate only, do not change files",
        criteria=["investigation note"], reason="operator steering")
    assert revised["revision"] == 2
    assert revised["parent"] == old_revision["revision_hash"]
    assert revised["stale_work"] == [queued["work_id"]]

    after = store.inspect(intent)
    assert after["revisions"][0] == old_revision
    assert after["revisions"][1]["parent"] == old_revision["revision_hash"]
    assert after["revisions"][1]["criteria"] == ["investigation note"]
    assert after["revisions"][1]["reason"] == "operator steering"
    assert after["intent"]["revision"] == 2

    states = {item["work_id"]: item for item in after["work"]}
    assert states[queued["work_id"]]["state"] == "stale"
    assert states[queued["work_id"]]["stale_reason"] == "operator steering"
    assert states[queued["work_id"]]["stale_reference"] == (
        revised["revision_hash"])
    assert states[running["work_id"]]["state"] == "running"
    assert after["attempts"][0]["state"] == "running"
    assert after["attempts"][0]["attempt_id"] == attempt["attempt_id"]

    with pytest.raises(OutcomeTransitionError):
        store.start_attempt(intent, queued["work_id"])
    with pytest.raises(OutcomeStaleError):
        store.add_work(intent, "old revision work",
                       revision_hash=old_revision["revision_hash"])

    # Completed/running work keeps its original revision for audit.
    assert states[running["work_id"]]["revision_hash"] == (
        old_revision["revision_hash"])
    reloaded = _store(tmp_path)
    assert reloaded.inspect(intent)["revisions"][0] == old_revision


def test_revision_expected_state_is_checked(tmp_path: Path) -> None:
    store = _store(tmp_path)
    intent = _intent(store)
    with pytest.raises(OutcomeStaleError):
        store.revise_intent(intent, "changed", expected_revision=2)
    store.revise_intent(intent, "changed", expected_revision=1)
    with pytest.raises(OutcomeStaleError):
        store.revise_intent(intent, "again", expected_revision=1)


# ---------------------------------------------------------------------------
# Idempotency and conflicts
# ---------------------------------------------------------------------------


def test_idempotent_duplicate_and_payload_conflict(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = store.submit_intent(
        "Add a docstring", intent_id="intent-1", idempotency_key="submit-1")
    duplicate = store.submit_intent(
        "Add a docstring", intent_id="intent-1", idempotency_key="submit-1")
    assert duplicate["duplicate"] is True
    assert duplicate["intent_id"] == first["intent_id"]
    assert duplicate["revision_hash"] == first["revision_hash"]
    assert len(_lines(store)) == 1

    with pytest.raises(OutcomeConflictError) as conflict:
        store.submit_intent(
            "Do something different", intent_id="intent-1",
            idempotency_key="submit-1")
    assert conflict.value.code == "outcomes.conflict"

    event = store.append_event(
        "intent-1", "progress", {"percent": 10}, idempotency_key="event-1")
    repeat = store.append_event(
        "intent-1", "progress", {"percent": 10}, idempotency_key="event-1")
    assert repeat["duplicate"] is True
    assert repeat["event_ref"] == event["event_ref"]
    count = len(_lines(store))
    with pytest.raises(OutcomeConflictError):
        store.append_event(
            "intent-1", "progress", {"percent": 20},
            idempotency_key="event-1")
    assert len(_lines(store)) == count


def test_submit_retry_without_explicit_key_is_a_duplicate(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = store.submit_intent("same", intent_id="intent-1")
    second = store.submit_intent("same", intent_id="intent-1")
    assert second["duplicate"] is True
    assert second["revision_hash"] == first["revision_hash"]
    assert len(_lines(store)) == 1
    with pytest.raises(OutcomeConflictError):
        store.submit_intent("different", intent_id="intent-1")


# ---------------------------------------------------------------------------
# Recovery and integrity
# ---------------------------------------------------------------------------


def test_torn_tail_is_truncated_on_recovery(tmp_path: Path) -> None:
    store = _store(tmp_path)
    intent = _intent(store)
    store.append_event(intent, "note", {"step": 1})
    store.append_event(intent, "note", {"step": 2})
    complete = len(_lines(store))
    with store.journal.open("ab") as handle:
        handle.write(b'{"schema":"ozagia.outcomes.record.v1"')

    page = store.inspect(intent)
    assert page["journal"]["state"] == "partial"
    assert page["journal"]["torn_bytes"] > 0
    assert len(page["events"]) == 3

    report = store.recover()
    assert report["state"] == "recovered"
    assert report["recovered"] is True
    assert report["truncated_bytes"] > 0
    assert report["records"] == complete
    assert len(_lines(store)) == complete
    assert store.recover()["truncated_bytes"] == 0

    store.append_event(intent, "note", {"step": 3})
    assert len(_lines(store)) == complete + 1
    assert store.follow(0)["tail_truncated"] is False


def test_malformed_interior_fails_closed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    intent = _intent(store)
    store.append_event(intent, "note", {"step": 1})
    lines = _lines(store)
    damaged = [lines[0], "this is not json", lines[1]]
    store.journal.write_text("\n".join(damaged) + "\n", encoding="utf-8")
    before = store.journal.read_bytes()

    with pytest.raises(OutcomeIntegrityError):
        store.inspect(intent)
    with pytest.raises(OutcomeIntegrityError):
        store.recover()
    with pytest.raises(OutcomeIntegrityError):
        store.append_event(intent, "note", {"step": 2})
    assert store.journal.read_bytes() == before


def test_hash_chain_damage_fails_closed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    intent = _intent(store)
    store.append_event(intent, "note", {"step": 1})
    records = _records(store)
    records[1]["payload"]["data"]["step"] = 99
    store.journal.write_text(
        "\n".join(C.canonical_json(record) for record in records) + "\n",
        encoding="utf-8")

    with pytest.raises(OutcomeIntegrityError):
        store.materialize()
    with pytest.raises(OutcomeIntegrityError):
        store.recover()


def test_replay_rejects_an_invalid_transition(tmp_path: Path) -> None:
    store = _store(tmp_path)
    intent = _intent(store)
    first = json.loads(_lines(store)[0])
    payload = {
        "schema": R.INTENT_TRANSITION_SCHEMA,
        "from_state": "accepted",
        "to_state": "accepted",
        "reason": "not a real move",
        "outcome_ref": None,
        "decision_ref": None,
        "request_hash": "0" * 64,
    }
    second = R.make_record(
        seq=2, event=R.INTENT_TRANSITIONED, intent_id=intent,
        actor="operator", timestamp=first["timestamp"], payload=payload,
        idempotency_key="bad-transition", prev_hash=first["record_hash"])
    store.journal.write_text(
        C.canonical_json(first) + "\n" +
        C.canonical_json(second.to_dict()) + "\n", encoding="utf-8")

    with pytest.raises(OutcomeIntegrityError):
        store.materialize()
    with pytest.raises(OutcomeIntegrityError):
        store.inspect(intent)
    with pytest.raises(OutcomeIntegrityError):
        store.recover()


def test_recover_on_absent_store_does_not_create_it(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    report = recover(root)
    assert report["state"] == "absent"
    assert report["records"] == 0
    assert not (root / ".ozagia").exists()


# ---------------------------------------------------------------------------
# Intent lifecycle
# ---------------------------------------------------------------------------


def test_invalid_intent_transitions_are_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path)
    intent = _intent(store)

    with pytest.raises(OutcomeValidationError):
        store.transition_intent(intent, "not-a-state")
    with pytest.raises(OutcomeTransitionError):
        store.transition_intent(intent, "satisfied")
    store.transition_intent(intent, "running")
    with pytest.raises(OutcomeTransitionError):
        store.transition_intent(intent, "accepted")
    store.transition_intent(intent, "waiting_decision")
    with pytest.raises((OutcomeNotFoundError, OutcomeTransitionError)):
        store.start_attempt(intent, "work-missing")
    store.transition_intent(intent, "running")
    store.cancel_intent(intent, reason="operator stop")
    with pytest.raises(OutcomeTransitionError):
        store.cancel_intent(intent)
    with pytest.raises(OutcomeTransitionError):
        store.revise_intent(intent, "after cancel")
    assert store.inspect(intent)["intent"]["state"] == "cancelled"


def test_attempt_lifecycle_and_lease_generation(tmp_path: Path) -> None:
    store = _store(tmp_path)
    intent = _intent(store)
    work = store.add_work(intent, "change app/base.py")
    queued = store.queue_attempt(
        intent, work["work_id"], attempt_id="attempt-1",
        worker_ref="worker:old", baseline_ref="git:abc123",
        lease_generation=7)
    assert queued["state"] == "queued"
    reloaded = _store(tmp_path)
    assert reloaded.inspect(intent)["attempts"][0]["state"] == "queued"

    started = reloaded.start_attempt(
        intent, work["work_id"], attempt_id="attempt-1",
        worker_ref="worker:new")
    assert started["state"] == "running"
    assert started["lease_generation"] == 7
    assert reloaded.inspect(intent)["attempts"][0]["baseline_ref"] == (
        "git:abc123")
    assert reloaded.inspect(intent)["intent"]["state"] == "running"

    count = len(_lines(reloaded))
    with pytest.raises(OutcomeStaleError):
        reloaded.finish_attempt(
            intent, "attempt-1", "succeeded", lease_generation=6)
    assert len(_lines(reloaded)) == count

    finished = reloaded.finish_attempt(
        intent, "attempt-1", "succeeded", lease_generation=7,
        evidence_refs=["evidence:test"])
    assert finished["state"] == "terminal"
    assert finished["outcome"] == "succeeded"
    page = reloaded.inspect(intent)
    assert page["work"][0]["state"] == "completed"
    assert page["attempts"][0]["state"] == "terminal"
    assert page["attempts"][0]["outcome"] == "succeeded"

    with pytest.raises(OutcomeTransitionError):
        reloaded.start_attempt(
            intent, work["work_id"], attempt_id="attempt-1")
    with pytest.raises(OutcomeTransitionError):
        reloaded.queue_attempt(intent, work["work_id"])


def test_uncertain_attempt_moves_work_to_waiting(tmp_path: Path) -> None:
    store = _store(tmp_path)
    intent = _intent(store)
    work = store.add_work(intent, "uncertain change")
    attempt = store.start_attempt(intent, work["work_id"])
    store.finish_attempt(intent, attempt["attempt_id"], "uncertain",
                         detail="worker disappeared")
    page = store.inspect(intent)
    assert page["attempts"][0]["state"] == "terminal"
    assert page["attempts"][0]["outcome"] == "uncertain"
    assert page["work"][0]["state"] == "waiting"
    with pytest.raises(OutcomeTransitionError):
        store.queue_attempt(intent, work["work_id"])


# ---------------------------------------------------------------------------
# Satisfaction hook
# ---------------------------------------------------------------------------


def test_satisfied_requires_outcome_evidence_and_approved_decision(
        tmp_path: Path) -> None:
    store = _store(tmp_path)
    intent = _intent(store)
    store.transition_intent(intent, "running")

    store.record_outcome(intent, evidence_refs=[])
    with pytest.raises(OutcomeTransitionError):
        store.transition_intent(intent, "satisfied")

    store.record_gate(
        intent, "gate-1", required=True, action_digest="sha256:" + "a" * 64,
        target="app/base.py", expected_base="git:base")
    rejected = store.record_decision(
        intent, "gate-1", "rejected", detail="not yet")
    store.record_outcome(
        intent, evidence_refs=["evidence:validation"],
        decision_ref=rejected["decision_ref"])
    with pytest.raises(OutcomeTransitionError):
        store.transition_intent(intent, "satisfied")

    approved = store.record_decision(
        intent, "gate-1", "approved", detail="operator accepted")
    outcome = store.record_outcome(
        intent, evidence_refs=["evidence:validation", "evidence:diff"],
        criteria_satisfied=["docstring present"],
        decision_ref=approved["decision_ref"], summary="verified")
    satisfied = store.transition_intent(
        intent, "satisfied", outcome_ref=outcome["outcome_ref"])
    assert satisfied["state"] == "satisfied"

    page = store.inspect(intent)
    assert page["intent"]["state"] == "satisfied"
    assert page["intent"]["outcome_ref"] == outcome["outcome_ref"]
    assert page["intent"]["decision_ref"] == approved["decision_ref"]
    assert page["outcome"]["decision_ref"] == approved["decision_ref"]
    assert page["decisions"][-1]["decision"] == "approved"

    with pytest.raises(OutcomeTransitionError):
        store.transition_intent(intent, "running")
    with pytest.raises(OutcomeTransitionError):
        store.revise_intent(intent, "too late")


def test_satisfaction_is_bound_to_current_revision(tmp_path: Path) -> None:
    store = _store(tmp_path)
    intent = _intent(store)
    store.transition_intent(intent, "running")
    store.record_gate(intent, "gate-1")
    decision = store.record_decision(intent, "gate-1", "approved")
    store.revise_intent(intent, "a narrower request", reason="steer")
    with pytest.raises(OutcomeStaleError):
        store.record_outcome(
            intent, evidence_refs=["evidence:old"],
            decision_ref=decision["decision_ref"])
    store.record_outcome(intent, evidence_refs=["evidence:current"])
    with pytest.raises(OutcomeTransitionError):
        store.transition_intent(intent, "satisfied")


# ---------------------------------------------------------------------------
# Event cursor and bounded reads
# ---------------------------------------------------------------------------


def test_follow_cursor_exactly_once_filter_and_gap(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = _intent(store, "intent-1")
    store.append_event(first, "note", {"n": 1})
    store.append_event(first, "note", {"n": 2})
    second = _intent(store, "intent-2")
    store.append_event(second, "note", {"n": 3})

    page = store.follow(0, limit=2)
    assert page["count"] == 2
    assert page["more"] is True
    assert page["truncated"] is True
    assert [item["seq"] for item in page["records"]] == [1, 2]
    assert page["cursor"] == 2
    rest = store.follow(page["cursor"], limit=100)
    assert rest["more"] is False
    assert [item["seq"] for item in rest["records"]] == [3, 4, 5]

    all_records = store.follow(0, limit=100)["records"]
    assert [item["seq"] for item in all_records] == [1, 2, 3, 4, 5]
    assert len({item["seq"] for item in all_records}) == 5

    filtered = store.follow(0, intent_id=first, limit=100)
    assert {item["intent_id"] for item in filtered["records"]} == {first}
    assert [item["seq"] for item in filtered["records"]] == [1, 2, 3]
    typed = store.follow(0, event_type="event.appended", limit=100)
    assert typed["count"] == 3

    assert store.follow(99, limit=10)["records"] == []
    assert store.follow(99, limit=10)["cursor"] == 99

    # A journal whose leading record was removed reports the gap rather than
    # silently pretending the cursor starts at the first available record.
    gap_root = tmp_path / "gap"
    gap_root.mkdir()
    gap_dir = gap_root / "state"
    gap_dir.mkdir()
    gap_lines = _lines(store)[1:]
    (gap_dir / "journal.jsonl").write_text(
        "\n".join(gap_lines) + "\n", encoding="utf-8")
    gap_store = OutcomeStore(gap_root, state_dir=gap_dir)
    gap = gap_store.follow(0, limit=100)
    assert gap["gap"] == {
        "expected_seq": 1, "first_available_seq": 2, "missing": 1,
    }
    assert gap["records"][0]["seq"] == 2


def test_follow_reports_tail_truncation_and_inspect_is_bounded(
        tmp_path: Path) -> None:
    store = _store(tmp_path)
    intent = _intent(store)
    for index in range(5):
        store.append_event(intent, "note", {"n": index})
    with store.journal.open("ab") as handle:
        handle.write(b'{"torn":')
    page = store.follow(0, limit=100)
    assert page["tail_truncated"] is True
    assert page["torn_bytes"] > 0
    assert page["journal_state"] == "partial"
    assert page["count"] == 6

    inspected = store.inspect(intent, events_limit=2)
    assert len(inspected["events"]) == 2
    assert inspected["event_count"] == 6
    assert inspected["events_truncated"] is True
    assert inspected["events"][-1]["seq"] == 6


def test_list_active_is_bounded_and_deterministic(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = _intent(store, "intent-1")
    second = _intent(store, "intent-2")
    third = _intent(store, "intent-3")
    store.cancel_intent(second)
    store.transition_intent(first, "running")

    page = store.list_active(limit=10)
    assert page["total"] == 2
    assert page["count"] == 2
    assert page["truncated"] is False
    assert [item["intent_id"] for item in page["intents"]] == [
        first, third]
    assert page == store.list_active(limit=10)

    bounded = store.list_active(limit=1)
    assert bounded["count"] == 1
    assert bounded["total"] == 2
    assert bounded["truncated"] is True
    assert all(item["state"] != "cancelled" for item in page["intents"])
    assert third in {item["intent_id"] for item in page["intents"]}


# ---------------------------------------------------------------------------
# Reserved effect/evidence schemas
# ---------------------------------------------------------------------------


def test_reserved_effect_and_evidence_records(tmp_path: Path) -> None:
    store = _store(tmp_path)
    intent = _intent(store)
    work = store.add_work(intent, "change")
    attempt = store.start_attempt(intent, work["work_id"])
    evidence = store.record_evidence(
        intent, source="git diff --check", artifact_digest="sha256:" + "b" * 64,
        method="deterministic-shell", coverage="changed file",
        refs=["workspace:fixture"])
    assert evidence["evidence_ref"].startswith("or-")
    intended = store.record_effect(
        intent, attempt["attempt_id"], "effect-1", state_name="intended",
        target="app/base.py", expected_base="git:base")
    applied = store.record_effect(
        intent, attempt["attempt_id"], "effect-1", state_name="applied",
        target="app/base.py", expected_base="git:base",
        observed_receipt={"new_base": "git:new", "bytes": 209})
    assert applied["record_id"] != intended["record_id"]

    page = store.inspect(intent)
    assert page["evidence"][0]["source"] == "git diff --check"
    assert [item["state"] for item in page["effects"]] == [
        "intended", "applied"]
    assert page["effects"][-1]["observed_receipt"]["bytes"] == 209

    with pytest.raises(OutcomeStaleError):
        store.record_effect(
            intent, attempt["attempt_id"], "effect-1", state_name="applied",
            target="app/base.py", lease_generation=999)


# ---------------------------------------------------------------------------
# Bounds, isolation and concurrency
# ---------------------------------------------------------------------------


def test_bounded_values_raise_typed_errors(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(OutcomeBoundsError):
        store.submit_intent("x" * 5000)
    with pytest.raises(OutcomeValidationError):
        store.submit_intent("valid", criteria=["same", "same"])
    intent = _intent(store)
    with pytest.raises(OutcomeBoundsError):
        store.append_event(intent, "note", {"data": "x" * 20_000})
    with pytest.raises(OutcomeValidationError):
        store.follow(-1)


def test_default_state_directory_is_isolated(tmp_path: Path,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    external = tmp_path / "external-state"
    monkeypatch.setenv("OZAGIA_STATE_DIR", str(external))
    store = OutcomeStore(root)
    intent = _intent(store)
    store.append_event(intent, "note", {"ok": True})

    assert (external / "outcomes" / "journal.jsonl").exists()
    assert journal_path(root) == external.resolve() / "outcomes" / "journal.jsonl"
    assert not (root / ".ozagia").exists()
    assert not any(root.rglob("graph.json"))
    assert not any(root.rglob("*.pyc"))


def test_concurrent_writers_serialize(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    state = root / "state"
    count = 12
    barrier = threading.Barrier(count)

    def write(index: int) -> str:
        store = OutcomeStore(root, state_dir=state)
        barrier.wait(timeout=10)
        return store.submit_intent(
            f"intent {index}", intent_id=f"intent-{index}")["intent_id"]

    with ThreadPoolExecutor(max_workers=count) as pool:
        futures = [pool.submit(write, index) for index in range(count)]
        ids = {future.result(timeout=30) for future in futures}
    assert len(ids) == count

    store = OutcomeStore(root, state_dir=state)
    records = _records(store)
    assert [record["seq"] for record in records] == list(
        range(1, count + 1))
    page = store.follow(0, limit=100)
    assert page["count"] == count
    assert page["gap"] is None
    assert store.materialize().cursor == count


def test_concurrent_process_writers_serialize(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    state = root / "process-state"
    count = 4
    context = multiprocessing.get_context("fork")
    barrier = context.Barrier(count)
    processes = [
        context.Process(
            target=_process_submit,
            args=(str(root), str(state), index, barrier))
        for index in range(count)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=30)
        assert process.exitcode == 0

    store = OutcomeStore(root, state_dir=state)
    records = _records(store)
    assert [record["seq"] for record in records] == list(
        range(1, count + 1))
    assert store.materialize().cursor == count
