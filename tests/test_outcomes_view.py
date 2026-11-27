"""Tests for the read-only outcome view and CLI (S1-C)."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from ozagia import outcomes_cli
from ozagia.core.outcomes import (
    OutcomeBoundsError,
    OutcomeStore,
    OutcomeValidationError,
)
from ozagia.core.outcomes.view import (
    KIND_INTENT,
    KIND_LIST,
    VIEW_SCHEMA,
    list_view,
    render_operation,
    view,
)


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


def _store(tmp_path: Path, *, name: str = "repo") -> OutcomeStore:
    root = tmp_path / name
    root.mkdir(exist_ok=True)
    return OutcomeStore(root, state_dir=root / "state")


def _intent(store: OutcomeStore, intent_id: str = "intent-1",
            criteria: list[str] | None = None) -> str:
    return store.submit_intent(
        "Add a module docstring",
        criteria=criteria or ["docstring present", "no behaviour change"],
        context_refs=["workspace:fixture"],
        project_ref="project:fixture",
        interpretation="one-file documentation change",
        principal="alice",
        conversation_ref="conversation:1",
        intent_id=intent_id,
    )["intent_id"]


def _snapshot(directory: Path) -> dict[str, bytes]:
    if not directory.exists():
        return {}
    return {
        path.relative_to(directory).as_posix(): path.read_bytes()
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


def _satisfied_intent(store: OutcomeStore) -> dict[str, Any]:
    intent = _intent(store)
    work = store.add_work(intent, "change app/base.py")
    attempt = store.start_attempt(intent, work["work_id"])
    store.finish_attempt(intent, attempt["attempt_id"], "succeeded")
    evidence = store.record_evidence(
        intent, source="git diff --check",
        artifact_digest="sha256:" + "b" * 64,
        method="deterministic-shell", coverage="app/base.py",
        freshness="current", refs=["workspace:fixture"])
    store.record_gate(
        intent, "gate-1", required=True,
        action_digest="sha256:" + "a" * 64, target="app/base.py",
        expected_base="git:base")
    decision = store.record_decision(
        intent, "gate-1", "approved", detail="operator accepted")
    outcome = store.record_outcome(
        intent, status="satisfied",
        evidence_refs=[evidence["evidence_ref"]],
        criteria_satisfied=["docstring present", "no behaviour change"],
        decision_ref=decision["decision_ref"], summary="verified")
    store.transition_intent(
        intent, "satisfied", outcome_ref=outcome["outcome_ref"])
    return {
        "intent": intent,
        "work": work,
        "attempt": attempt,
        "evidence": evidence,
        "decision": decision,
        "outcome": outcome,
    }


def _run_cli(capsys: pytest.CaptureFixture[str],
             argv: list[str]) -> tuple[int, str, str]:
    code = outcomes_cli.main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


# ---------------------------------------------------------------------------
# Reload agreement
# ---------------------------------------------------------------------------


def test_view_and_list_match_materialized_records_after_reload(
        tmp_path: Path) -> None:
    store = _store(tmp_path)
    ids = _satisfied_intent(store)
    intent = ids["intent"]

    first_view = view(intent, store)
    first_list = list_view(store)
    reloaded = _store(tmp_path)
    page = reloaded.inspect(intent)

    again = view(intent, reloaded)
    again_list = list_view(reloaded)

    assert again == first_view
    assert again_list == first_list
    assert first_view["schema"] == VIEW_SCHEMA
    assert first_view["kind"] == KIND_INTENT
    assert first_view["intent_id"] == intent
    assert first_view["status"] == page["intent"]["state"] == "satisfied"
    assert first_view["intent"]["revision"] == page["intent"]["revision"] == 1
    assert first_view["revision"]["revision_hash"] == (
        page["intent"]["revision_hash"])
    assert first_view["revision"]["criteria"] == [
        "docstring present", "no behaviour change"]
    assert first_view["criteria_summary"] == {
        "total": 2, "satisfied": 2, "pending": 0}
    assert all(row["status"] == "satisfied"
               for row in first_view["criteria"])

    assert first_view["outcome"]["record_id"] == (
        page["outcome"]["record_id"])
    assert first_view["outcome"]["record_id"] == ids["outcome"]["outcome_ref"]
    assert first_view["outcome"]["status"] == "satisfied"
    assert first_view["outcome"]["data"]["summary"] == "verified"
    assert first_view["latest_outcome"]["record_id"] == (
        first_view["outcome"]["record_id"])
    assert first_view["current_work"]["work_id"] == ids["work"]["work_id"]
    assert first_view["current_work"]["state"] == "completed"
    assert first_view["current_attempt"]["attempt_id"] == (
        ids["attempt"]["attempt_id"])
    assert first_view["current_attempt"]["state"] == "terminal"
    assert first_view["current_attempt"]["outcome"] == "succeeded"
    assert first_view["pending_decision"] is None
    assert first_view["blocker"] is None
    assert first_view["operations"] == []

    verified = first_view["evidence"]["verified"]
    assert [row["record_id"] for row in verified] == [
        ids["evidence"]["evidence_ref"]]
    assert first_view["evidence"]["pending"] == []
    assert first_view["references"]["events"] == [
        {
            "seq": record["seq"],
            "record_id": record["record_id"],
            "event": record["event"],
            "timestamp": record["timestamp"],
        }
        for record in page["events"]
    ]
    assert first_view["last_event"]["seq"] == page["events"][-1]["seq"]
    assert first_view["last_updated_seq"] == page["intent"]["last_seq"]
    assert first_view["cursor"] == page["cursor"]
    assert first_view["journal"]["state"] == "current"

    assert first_list["schema"] == VIEW_SCHEMA
    assert first_list["kind"] == KIND_LIST
    assert first_list["count"] == first_list["total"] == 1
    assert first_list["truncated"] is False
    assert first_list["cursor"] == page["cursor"]
    row = first_list["intents"][0]
    assert row["intent_id"] == intent
    assert row["state"] == "satisfied"
    assert row["revision"] == 1
    assert row["last_seq"] == page["intent"]["last_seq"]
    assert row["last_updated_seq"] == page["intent"]["last_seq"]
    assert row["last_event"]["seq"] == page["events"][-1]["seq"]
    assert row["current_work"]["work_id"] == ids["work"]["work_id"]
    assert row["current_attempt"]["attempt_id"] == (
        ids["attempt"]["attempt_id"])
    assert row["pending_decision"] is None
    assert row["blocker"] is None
    assert row["outcome"]["status"] == "satisfied"
    assert row["outcome"]["record_id"] == ids["outcome"]["outcome_ref"]
    assert row["outcome_summary"] == "satisfied: verified"
    assert row["criteria_summary"] == {
        "total": 2, "satisfied": 2, "pending": 0}
    assert row["operations"] == []
    assert row["next_operation"] is None


# ---------------------------------------------------------------------------
# Derived decisions, evidence, blockers and operations
# ---------------------------------------------------------------------------


def test_pending_decision_blocker_and_operations_are_derived(
        tmp_path: Path) -> None:
    store = _store(tmp_path)
    intent = _intent(store)
    work = store.add_work(intent, "change app/base.py")
    attempt = store.start_attempt(intent, work["work_id"])
    store.finish_attempt(intent, attempt["attempt_id"], "succeeded")

    store.record_gate(
        intent, "gate-1", required=True, target="app/base.py",
        expected_base="git:base")
    pending = view(intent, store)
    assert pending["pending_decision"]["gate_id"] == "gate-1"
    assert pending["pending_decision"]["status"] == "pending"
    assert pending["pending_decision"]["revision_current"] is True
    assert pending["pending_decision"]["requires_decision"] is True
    assert pending["blocker"]["kind"] == "decision"
    assert pending["blocker"]["gate_id"] == "gate-1"
    operations = {item["id"]: item for item in pending["operations"]}
    assert operations["decision.record"]["enabled"] is True
    assert "intent.satisfy" not in operations
    assert pending["outcome"] is None
    assert pending["criteria_summary"]["pending"] == 2

    decision = store.record_decision(intent, "gate-1", "approved")
    approved = view(intent, store)
    assert approved["pending_decision"] is None
    assert approved["blocker"] is None
    assert "decision.record" not in {
        item["id"] for item in approved["operations"]}

    evidence = store.record_evidence(
        intent, source="python -m py_compile", freshness="current")
    outcome = store.record_outcome(
        intent, evidence_refs=[
            evidence["evidence_ref"], "evidence:not-recorded"],
        criteria_satisfied=["docstring present"],
        decision_ref=decision["decision_ref"], summary="partial proof")
    ready = view(intent, store)
    assert ready["outcome"]["record_id"] == outcome["outcome_ref"]
    assert ready["criteria"] == [
        {
            "criterion": "docstring present",
            "status": "satisfied",
            "in_current_revision": True,
        },
        {
            "criterion": "no behaviour change",
            "status": "pending",
            "in_current_revision": True,
        },
    ]
    assert [row["record_id"] for row in ready["evidence"]["verified"]] == [
        evidence["evidence_ref"]]
    assert [row["reference"] for row in ready["evidence"]["pending"]] == [
        "evidence:not-recorded"]
    assert ready["evidence"]["pending"][0]["reason"]
    operations = {item["id"]: item for item in ready["operations"]}
    assert operations["intent.satisfy"]["enabled"] is True
    assert operations["intent.satisfy"]["requires_gate"] is True
    assert ready["blocker"] is None

    store.transition_intent(
        intent, "satisfied", outcome_ref=outcome["outcome_ref"])
    satisfied = view(intent, store)
    assert satisfied["status"] == "satisfied"
    assert satisfied["operations"] == []
    assert satisfied["blocker"] is None
    assert satisfied["criteria_summary"] == {
        "total": 2, "satisfied": 1, "pending": 1}

    # An uncertain attempt is a blocker even without a gate.
    uncertain_intent = _intent(store, "intent-2", ["reconcile"])
    uncertain_work = store.add_work(uncertain_intent, "uncertain change")
    uncertain_attempt = store.start_attempt(
        uncertain_intent, uncertain_work["work_id"])
    store.finish_attempt(
        uncertain_intent, uncertain_attempt["attempt_id"], "uncertain",
        detail="worker disappeared")
    uncertain = view(uncertain_intent, store)
    assert uncertain["current_work"]["state"] == "waiting"
    assert uncertain["current_attempt"]["outcome"] == "uncertain"
    assert uncertain["pending_decision"] is None
    assert uncertain["blocker"]["kind"] == "uncertain_attempt"
    assert uncertain["blocker"]["next_operation"] == "work.transition"
    operations = {item["id"]: item for item in uncertain["operations"]}
    assert "work.transition" in operations
    assert "attempt.start" not in operations


# ---------------------------------------------------------------------------
# Generic fallback
# ---------------------------------------------------------------------------


class _ExtraRecords:
    """Forward a real store while adding future record types."""

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
        page["operations"] = [{
            "id": "future.operation",
            "label": "Future operation",
        }]
        page["evidence"].append({
            "schema": "ozagia.outcomes.future_evidence.v1",
            "record_id": "or-00000097-abcdefabcdef",
            "seq": 97,
            "timestamp": 121,
            "actor": "operator",
            "intent_id": intent_id,
            "source": "future checker",
        })
        return page

    def follow(self, after_seq: int = 0, *, intent_id: str | None = None,
               limit: int = 100) -> dict[str, Any]:
        return self.store.follow(
            after_seq, intent_id=intent_id, limit=limit)

    def list_active(self, limit: int = 100) -> dict[str, Any]:
        return self.store.list_active(limit=limit)


def test_unknown_record_types_and_operations_render_generically(
        tmp_path: Path) -> None:
    store = _store(tmp_path)
    intent = _intent(store)
    wrapped = _ExtraRecords(store)

    result = view(intent, wrapped)

    unknown_event = next(
        row for row in result["records"]
        if row.get("type") == "future.signal")
    assert unknown_event["display_type"] == "other"
    assert unknown_event["event"] == "future.signal"
    assert unknown_event["record"]["payload"]["note"] == "kept"
    assert "preserved" in unknown_event["summary"]

    unknown_gate = next(
        row for row in result["gates"]
        if row.get("record_id") == "or-00000098-fedcbafedcba")
    assert unknown_gate["display_type"] == "other"
    assert unknown_gate["data"]["gate_id"] == "future-gate"

    unknown_collection = next(
        row for row in result["other"] if row.get("source") == "signals")
    assert unknown_collection["display_type"] == "other"
    assert unknown_collection["data"]["value"] == 3

    unknown_evidence = next(
        row for row in result["other"]
        if row.get("record_id") == "or-00000097-abcdefabcdef")
    assert unknown_evidence["display_type"] == "other"
    assert unknown_evidence["data"]["source"] == "future checker"

    other_ids = {
        row.get("record_id")
        for row in result["other"] if row.get("record_id")
    }
    assert "or-00000099-abcdefabcdef" in other_ids
    assert "or-00000098-fedcbafedcba" in other_ids
    assert "or-00000097-abcdefabcdef" in other_ids

    unknown_operation = render_operation("future.operation")
    assert unknown_operation["display_type"] == "other"
    assert unknown_operation["enabled"] is False
    assert "Unknown operation" in unknown_operation["reason"]
    viewed_operation = next(
        item for item in result["operations"]
        if item.get("id") == "future.operation")
    assert viewed_operation["display_type"] == "other"
    assert viewed_operation["enabled"] is False
    assert "not inferred" in viewed_operation["reason"]
    known_operation = render_operation("intent.revise")
    assert known_operation["display_type"] == "operation"
    assert known_operation["enabled"] is True


# ---------------------------------------------------------------------------
# Bounds and determinism
# ---------------------------------------------------------------------------


def test_list_is_bounded_and_deterministic(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = _intent(store, "intent-1", ["one"])
    second = _intent(store, "intent-2", ["two"])
    third = _intent(store, "intent-3", ["three"])
    store.cancel_intent(second)
    store.append_event(first, "note", {"step": 1})

    full = list_view(store, limit=10)
    assert full["count"] == full["total"] == 3
    assert full["truncated"] is False
    assert [row["intent_id"] for row in full["intents"]] == [
        first, second, third]
    assert [row["state"] for row in full["intents"]] == [
        "accepted", "cancelled", "accepted"]
    assert full == list_view(store, limit=10)

    bounded = list_view(store, limit=1)
    assert bounded["count"] == 1
    assert bounded["total"] == 3
    assert bounded["truncated"] is True
    assert bounded["intents"][0]["intent_id"] == first
    assert bounded["omitted"]["intents"] == 2
    assert bounded["omitted"]["reason"] == "limit"

    with pytest.raises(OutcomeBoundsError):
        list_view(store, limit=0)
    with pytest.raises(OutcomeBoundsError):
        list_view(store, limit=501)
    with pytest.raises(OutcomeValidationError):
        list_view(store, limit=True)


# ---------------------------------------------------------------------------
# Read-only behavior
# ---------------------------------------------------------------------------


def test_view_and_list_never_append_or_change_journal_bytes(
        tmp_path: Path) -> None:
    store = _store(tmp_path)
    intent = _intent(store)
    store.add_work(intent, "change")
    store.record_gate(intent, "gate-1")

    before_files = _snapshot(store.directory)
    before_bytes = store.journal.read_bytes()

    view(intent, store)
    view(intent, store, events_limit=1)
    list_view(store)
    list_view(store, limit=1)

    assert store.journal.read_bytes() == before_bytes
    assert _snapshot(store.directory) == before_files

    empty_root = tmp_path / "empty"
    empty_root.mkdir()
    empty = OutcomeStore(empty_root, state_dir=empty_root / "state")
    empty_view = list_view(empty)
    assert empty_view["count"] == 0
    assert empty_view["intents"] == []
    assert not (empty_root / "state").exists()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_read_only_commands_emit_valid_json(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    store = _store(tmp_path)
    intent = _intent(store)
    store.add_work(intent, "change app/base.py")
    store.record_gate(intent, "gate-1", required=True)
    root = store.root
    state = store.directory

    before_files = _snapshot(state)
    before_bytes = store.journal.read_bytes()
    base = ["--root", str(root), "--state-dir", str(state)]

    code, out, err = _run_cli(
        capsys, ["show", intent, *base, "--json"])
    assert code == 0 and err == ""
    detail = json.loads(out)
    assert detail["schema"] == VIEW_SCHEMA
    assert detail["kind"] == KIND_INTENT
    assert detail["intent_id"] == intent
    assert detail["pending_decision"]["gate_id"] == "gate-1"
    assert detail["blocker"]["kind"] == "decision"

    code, out, err = _run_cli(capsys, ["list", *base, "--json"])
    assert code == 0 and err == ""
    listing = json.loads(out)
    assert listing["schema"] == VIEW_SCHEMA
    assert listing["kind"] == KIND_LIST
    assert listing["count"] == 1
    assert listing["intents"][0]["intent_id"] == intent
    assert listing["intents"][0]["pending_decision"]["gate_id"] == "gate-1"

    code, out, err = _run_cli(
        capsys, ["events", intent, "--after", "0", *base, "--json"])
    assert code == 0 and err == ""
    events = json.loads(out)
    assert events["schema"] == VIEW_SCHEMA
    assert events["kind"] == "events"
    assert events["intent_id"] == intent
    assert events["after_seq"] == 0
    assert events["records"]
    assert events["cursor"] == events["records"][-1]["seq"]
    assert events["records"][0]["event"] == "intent.submitted"

    code, out, err = _run_cli(
        capsys, ["events", intent, "--after", "1", *base, "--json"])
    assert code == 0 and err == ""
    later = json.loads(out)
    assert later["after_seq"] == 1
    assert all(record["seq"] > 1 for record in later["records"])

    code, out, err = _run_cli(capsys, ["show", intent, *base])
    assert code == 0 and err == ""
    assert intent in out
    assert "pending decision: gate-1" in out
    assert "blocker: decision" in out

    code, out, err = _run_cli(capsys, ["list", *base])
    assert code == 0 and err == ""
    assert intent in out
    assert "decision=gate-1" in out

    code, out, err = _run_cli(capsys, ["events", intent, *base])
    assert code == 0 and err == ""
    assert "events after 0" in out
    assert "intent.submitted" in out

    assert store.journal.read_bytes() == before_bytes
    assert _snapshot(state) == before_files

    code, out, err = _run_cli(
        capsys, ["show", "intent-missing", *base, "--json"])
    assert code == 2
    assert "outcomes.not_found" in err
    assert store.journal.read_bytes() == before_bytes
    assert _snapshot(state) == before_files
