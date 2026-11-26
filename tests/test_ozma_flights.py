"""Stage H Ozma flights, handoffs, blockers, A2A loss and wake primitives."""
from __future__ import annotations

import hashlib
import json
import os
import socket
import stat
import subprocess
from pathlib import Path

import pytest

from ozagia import cli
from ozagia.core import mesh as M
from ozagia.core import ozma_bridge as B
from ozagia.core import ozma_mesh as O


@pytest.fixture()
def env(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    state = tmp_path / "state"
    monkeypatch.setenv("OZAGIA_ROOT", str(repo))
    monkeypatch.setenv("OZAGIA_STATE_DIR", str(state))
    monkeypatch.delenv("OZAGIA_MESH_ID", raising=False)
    monkeypatch.delenv("OZAGIA_MESH_HARNESS", raising=False)
    monkeypatch.delenv("OZAGIA_MESH_SESSION", raising=False)
    return repo, state


def _card(repo: Path) -> str:
    path = repo / "card.json"
    path.write_text('{"agent":"agent:reviewer"}\n', encoding="utf-8")
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _configured(repo: Path, *, wake_modes=("passive", "message-triggered",
                                          "scheduled"),
                transport: str = "native") -> O.OzmaMeshConfig:
    digest = _card(repo)
    lines = [
        "[ozma_mesh]",
        "enabled = true",
        'mode = "messages"',
        f'transport = "{transport}"',
        'agent_id = "agent:reviewer:wu1"',
        'card_path = "card.json"',
        f'card_fingerprint = "{digest}"',
        'verified_by = "host"',
        "wake_modes = ["
        + ", ".join(f'"{item}"' for item in wake_modes) + "]",
        'channels = ["#flight"]',
        'subscriptions = ["#flight"]',
        "priority_floor = 0.1",
        'message_rate = "600/5min"',
    ]
    (repo / ".ozagia.toml").write_text("\n".join(lines) + "\n",
                                       encoding="utf-8")
    return O.load_config(repo)


def _mapping(repo: Path, key: str, agent_id: str | None, *, session: str,
             wake_modes=("passive", "message-triggered", "scheduled")) -> None:
    cfg = O.load_config(repo)
    O.save_mapping(repo, {
        "flight": {"id": B.flight_id(repo, cfg)},
        "ozagia": {"harness": "opencode", "session": session, "key": key},
        "ozma": {
            "agent_id": agent_id,
            "scope": "workspace",
            "card_path": "card.json",
            "card_fingerprint": cfg.card_fingerprint,
            "transport": "native",
        },
        "verification": {
            "status": "host_declared" if agent_id else "unverified",
            "source": "host",
        },
        "transport": "native",
        "wake_modes": list(wake_modes),
        "state": "host_declared" if agent_id else "unverified",
    })


def _envelope(repo: Path, config: O.OzmaMeshConfig, message_id: str,
              ozma_type: str = "heads-up", body=None, *,
              direction: str = "inbound", **extra):
    return B.build_envelope(
        flight_id=B.flight_id(repo, config), message_id=message_id,
        ozma_type=ozma_type, body=body or {"text": "hello"},
        direction=direction, channel=extra.pop("channel", "#flight"),
        from_agent=extra.pop("from_agent", "agent:peer:wu2"),
        **extra)


def _outbox_envelope(repo: Path, config: O.OzmaMeshConfig, message_id: str):
    path = B.record_path(repo, B.OUTBOX_DIR, B.flight_id(repo, config),
                         message_id, "outbound")
    return B._read_record(path)["envelope"]


def _snapshot(state: Path) -> dict[str, tuple[int, bytes]]:
    if not state.exists():
        return {}
    return {
        str(path.relative_to(state)): (
            stat.S_IMODE(path.stat().st_mode), path.read_bytes())
        for path in sorted(state.rglob("*")) if path.is_file()
    }


# --------------------------------------------------------------------------- #
# flights
# --------------------------------------------------------------------------- #
def test_flight_roles_and_workunits_come_only_from_host_agent_ids(
        env, monkeypatch) -> None:
    repo, state = env
    cfg = _configured(repo)
    first = M.register(repo, "opencode", "a", exposure="supervise")
    second = M.register(repo, "opencode", "b", exposure="supervise")
    _mapping(repo, first["key"], "agent:reviewer:wu1", session="a")
    _mapping(repo, second["key"], "agent:implementer", session="b")
    _mapping(repo, "key-unassigned-000000", None, session="c")
    link = M.link_request(repo, "opencode", "a", to="opencode:b",
                          relation="sub")["link"]
    M.link_accept(repo, "opencode", "b", link["link_id"])
    M.register(repo, "opencode", "d", exposure="supervise")
    outside = M.link_request(repo, "opencode", "a", to="opencode:d",
                             relation="peer")["link"]
    M.link_accept(repo, "opencode", "d", outside["link_id"])
    M.send(repo, "opencode", "a", kind="note", subject="SUBJECT CANARY",
           body="BODY CANARY", to="opencode:b")

    before = _snapshot(state)

    def forbidden(*args, **kwargs):
        raise AssertionError("a flight view must not open a socket")

    with monkeypatch.context() as patch:
        patch.setattr(socket, "socket", forbidden)
        report = B.flight(repo, config=cfg)
    assert report["flight_id"] == "default"
    assert report["member_count"] == 3
    assert report["role_counts"] == {"implementer": 1, "reviewer": 1,
                                     "unassigned": 1}
    assert report["workunit_counts"] == {"wu1": 1}
    assert report["counts"]["links"] == 1
    roles = {(item["session"], item["role"], item["workunit"])
             for item in report["members"]}
    assert ("a", "reviewer", "wu1") in roles
    assert ("b", "implementer", None) in roles
    assert ("c", None, None) in roles
    # A link never derives an agent role, and no body or credential leaks.
    text = json.dumps(report)
    assert "SUBJECT CANARY" not in text
    assert "BODY CANARY" not in text
    assert "card.json" not in text
    assert report["verified"] is False
    assert report["conformance"] == "declared"
    assert _snapshot(state) == before

    assert cli.main(["mesh", "ozma", "flight", "--json"]) == 0
    assert _snapshot(state) == before
    assert cfg.mode == "messages"


def test_flight_role_parser_never_infers_a_role() -> None:
    assert B.agent_role_workunit("agent:reviewer:wu1") == {
        "role": "reviewer", "workunit": "wu1"}
    assert B.agent_role_workunit("agent:implementer") == {
        "role": "implementer", "workunit": None}
    for value in (None, "", "reviewer", "opencode:a", "agent:Bad Role"):
        assert B.agent_role_workunit(value) == {"role": None, "workunit": None}


def test_status_flight_summary_is_counts_only_and_read_only(env) -> None:
    from ozagia.core import status as S

    repo, state = env
    cfg = _configured(repo)
    M.register(repo, "opencode", "a", exposure="coordinate")
    before = _snapshot(state)
    report = S.status(repo, timeout=0.1)
    section = report["flight"]
    assert section["state"] == "ok"
    data = section["report"]
    assert set(data) == {"flight_id", "source", "counts", "ready",
                         "conformance", "verified", "network"}
    assert "members" not in data
    assert data["verified"] is False
    assert data["network"] is False
    assert _snapshot(state) == before

    ready_before = report["flight"]["report"]["ready"]
    B.add_blocker(repo, blocked_id="wu1", blocker_id="wu2",
                  workunit="wu1", config=cfg)
    B.ingest(repo, _envelope(
        repo, cfg, "hf_status", "handoff", {"workunit": "wu2"},
        requires_ack=True, to_agent="agent:reviewer:wu1"), config=cfg)
    B.ingest(repo, _envelope(
        repo, cfg, "ozm_status_wake", body={"text": "wake"},
        to_agent="agent:reviewer:wu1", wake="message"), config=cfg)
    counts = S.status(repo, timeout=0.1)["flight"]["report"]["counts"]
    assert counts["blockers_open"] == 1
    assert counts["handoffs_pending"] == 1
    assert counts["wake_pending"] == 1
    checked = O.check(repo)
    assert checked["ready"] is ready_before
    assert checked["bridge"]["blockers"]["blocking"] is False
    assert checked["bridge"]["handoffs"]["counts"]["pending"] == 1
    assert checked["bridge"]["wake"]["spawnable"] is False
    assert checked["network"] is False


# --------------------------------------------------------------------------- #
# handoffs
# --------------------------------------------------------------------------- #
def test_handoff_lifecycle_ack_and_no_authority_transfer(env) -> None:
    repo, state = env
    cfg = _configured(repo)
    M.register(repo, "opencode", "a", exposure="coordinate")
    inbound = _envelope(
        repo, cfg, "hf_in", "handoff",
        {"workunit": "wu2", "criteria": ["passes tests", "docs"],
         "refs": ["r1"], "text": "BODY CANARY"},
        requires_ack=True, thread="thread-1", to_agent="agent:reviewer:wu1")
    assert B.ingest(repo, inbound, config=cfg)["accepted"] is True
    listed = B.handoffs(repo, config=cfg)
    assert listed["counts"]["pending"] == 1
    row = listed["handoffs"][0]
    assert row["handoff_id"] == "hf_in"
    assert row["workunit"] == "wu2"
    assert row["criteria_count"] == 2
    assert row["criteria_digest"].startswith("sha256:")
    assert row["authority_transferred"] is False
    assert "BODY CANARY" not in json.dumps(listed)
    files = list((state / "mesh" / "ozma" / "handoffs" / "default").glob(
        "*.json"))
    assert len(files) == 1
    assert stat.S_IMODE(files[0].stat().st_mode) == 0o600
    assert stat.S_IMODE(files[0].parent.stat().st_mode) == 0o700

    accepted = B.handoff_respond(
        repo, "hf_in", "accept", harness="opencode", session="a",
        config=cfg)
    assert accepted["handoff"]["state"] == "accepted"
    assert accepted["authority_transferred"] is False
    reply = _outbox_envelope(repo, cfg, accepted["reply"]["id"])
    assert reply["type"] == "ack"
    assert reply["body"] == {"accepted": True}
    assert reply["in_reply_to"] == "hf_in"
    assert reply["thread"] == "thread-1"

    complete = B.handoff_respond(
        repo, "hf_in", "complete", reason="done", harness="opencode",
        session="a", config=cfg)
    assert complete["handoff"]["state"] == "complete"
    completion = _outbox_envelope(repo, cfg, complete["reply"]["id"])
    assert completion["type"] == "handoff"
    assert completion["body"]["completion"] == "done"
    assert completion["in_reply_to"] == "hf_in"
    assert len(B.handoffs(repo, config=cfg)["handoffs"]) == 1
    # No lease, delegation or merge authority moved.
    assert M.lease_list(repo)["leases"] == []
    assert M.delegation_list(repo)["delegations"] == []

    second = _envelope(
        repo, cfg, "hf_reject", "handoff",
        {"workunit": "wu3", "criteria": ["x"]},
        requires_ack=True, thread="thread-2", to_agent="agent:reviewer:wu1")
    B.ingest(repo, second, config=cfg)
    rejected = B.handoff_respond(
        repo, "hf_reject", "reject", reason="not mine", harness="opencode",
        session="a", config=cfg)
    assert rejected["handoff"]["state"] == "rejected"
    assert _outbox_envelope(repo, cfg, rejected["reply"]["id"])["body"] == {
        "accepted": False, "reason": "not mine"}

    with pytest.raises(M.MeshConfigError):
        B.handoff_respond(repo, "hf_reject", "accept", config=cfg)

    other = _envelope(
        repo, cfg, "hf_other", "handoff", {"workunit": "wu4"},
        requires_ack=True, to_agent="agent:other:wu9")
    B.ingest(repo, other, config=cfg)
    with pytest.raises(B.OzmaBridgeRefusal) as excinfo:
        B.handoff_respond(repo, "hf_other", "accept", harness="opencode",
                          session="a", config=cfg)
    assert excinfo.value.code == "not_handoff_recipient"


def test_inbound_handoff_ack_from_the_wrong_sender_is_ignored(env) -> None:
    repo, _ = env
    cfg = _configured(repo)
    outbound = B.build_envelope(
        flight_id=B.flight_id(repo, cfg), message_id="hf_out",
        ozma_type="handoff",
        body={"workunit": "wu1", "criteria": ["x"]},
        direction="outbound", from_agent=cfg.agent_id,
        to_agent="agent:peer:wu2", channel="#flight", thread="thread-9")
    B.enqueue(repo, outbound, config=cfg)
    assert B.handoffs(repo, config=cfg)["counts"]["pending"] == 1

    wrong = _envelope(
        repo, cfg, "ozm_ack_wrong", "ack", {"accepted": True},
        in_reply_to="hf_out", from_agent="agent:impostor:wu9")
    assert B.ingest(repo, wrong, config=cfg)["accepted"] is True
    assert B.handoffs(repo, config=cfg)["handoffs"][0]["state"] == "pending"

    right = _envelope(
        repo, cfg, "ozm_ack_right", "ack", {"accepted": True},
        in_reply_to="hf_out", from_agent="agent:peer:wu2")
    assert B.ingest(repo, right, config=cfg)["accepted"] is True
    row = B.handoffs(repo, config=cfg)["handoffs"][0]
    assert row["state"] == "accepted"
    assert row["ack_message_id"] == "ozm_ack_right"


def test_handoff_list_show_and_respond_cli(env, capsys) -> None:
    repo, _ = env
    cfg = _configured(repo)
    M.register(repo, "opencode", "a", exposure="coordinate")
    B.ingest(repo, _envelope(
        repo, cfg, "hf_cli", "handoff", {"workunit": "wu9"},
        requires_ack=True, to_agent="agent:reviewer:wu1"), config=cfg)
    assert cli.main(["mesh", "ozma", "handoff", "list", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["counts"]["pending"] == 1
    assert cli.main(["mesh", "ozma", "handoff", "show", "hf_cli",
                     "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["handoff"]["state"] == "pending"
    assert cli.main(["mesh", "ozma", "handoff", "accept", "hf_cli",
                     "--harness", "opencode", "--session", "a",
                     "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["handoff"]["state"] == "accepted"


def test_ack_defaults_local_only_and_optional_typed_reply(env, capsys) -> None:
    repo, _ = env
    cfg = _configured(repo)
    M.register(repo, "opencode", "a", exposure="coordinate")
    first = _envelope(
        repo, cfg, "ozm_ack_default", "request", {"purpose": "general"},
        requires_ack=True, to_agent="agent:reviewer:wu1")
    B.ingest(repo, first, config=cfg)
    assert cli.main(["mesh", "ozma", "ack", "ozm_ack_default",
                     "--json"]) == 0
    first_payload = json.loads(capsys.readouterr().out)
    assert "reply" not in first_payload
    assert B.outbox(repo, config=cfg)["messages"] == []

    second = _envelope(
        repo, cfg, "ozm_ack_typed", "request", {"purpose": "review"},
        requires_ack=True, to_agent="agent:reviewer:wu1")
    B.ingest(repo, second, config=cfg)
    assert cli.main([
        "mesh", "ozma", "ack", "ozm_ack_typed", "--accepted",
        "--reason", "ok", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    reply = _outbox_envelope(repo, cfg, payload["reply"]["id"])
    assert reply["type"] == "ack"
    assert reply["body"] == {"accepted": True, "reason": "ok"}
    assert reply["in_reply_to"] == "ozm_ack_typed"

    plain = _envelope(
        repo, cfg, "ozm_no_ack", body={"text": "no ack"},
        to_agent="agent:reviewer:wu1")
    B.ingest(repo, plain, config=cfg)
    with pytest.raises(B.OzmaBridgeRefusal) as excinfo:
        B.ack_message(repo, "ozm_no_ack", config=cfg, accepted=True)
    assert excinfo.value.code == "ack_not_required"


# --------------------------------------------------------------------------- #
# blockers
# --------------------------------------------------------------------------- #
def test_blockers_are_advisory_and_do_not_touch_leases_or_merge(env) -> None:
    repo, state = env
    cfg = _configured(repo)
    M.register(repo, "opencode", "a", exposure="coordinate")
    lease = M.lease_acquire(repo, "opencode", "a", kind="path",
                            resource_id="src/foo.py", mode="write")["lease"]
    merge = M.lease_acquire(repo, "opencode", "a", kind="merge",
                            resource_id="main", mode="merge")["lease"]
    ready_before = B.flight(repo, config=cfg)["readiness"]["ready"]
    edge = B.add_blocker(repo, blocked_id="wu1", blocker_id="wu2",
                         workunit="wu1", resource="src/foo.py", config=cfg)
    assert edge["edge"]["advisory"] is True
    assert edge["edge"]["blocking"] is False
    blocker_file = state / "mesh" / "ozma" / "blockers" / "default.json"
    assert stat.S_IMODE(blocker_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(blocker_file.parent.stat().st_mode) == 0o700
    report = B.flight(repo, config=cfg)
    assert report["counts"]["blockers_open"] == 1
    assert report["readiness"]["ready"] == ready_before
    assert M.lease_status(repo, lease["lease_id"])["lease"][
        "effective_state"] == "active"
    assert M.lease_status(repo, merge["lease_id"])["lease"][
        "effective_state"] == "active"
    cleared = B.clear_blockers(repo, workunit="wu1", config=cfg)
    assert cleared["cleared"] == 1
    assert B.blockers(repo, config=cfg)["counts"]["open"] == 0


def test_block_and_unblock_envelopes_and_cli(env, capsys) -> None:
    repo, _ = env
    cfg = _configured(repo)
    block = _envelope(
        repo, cfg, "ozm_block", "block",
        {"blocked_id": "wu1", "blocker_id": "wu2", "resource": "src/a.py"})
    assert B.ingest(repo, block, config=cfg)["accepted"] is True
    assert B.blockers(repo, config=cfg)["counts"]["open"] == 1
    unblock = _envelope(
        repo, cfg, "ozm_unblock", "unblock",
        {"blocked_id": "wu1", "blocker_id": "wu2"})
    assert B.ingest(repo, unblock, config=cfg)["accepted"] is True
    assert B.blockers(repo, config=cfg)["counts"]["open"] == 0

    assert cli.main(["mesh", "ozma", "block", "--blocked", "wu3",
                     "--blocker", "wu4", "--json"]) == 0
    blocked = json.loads(capsys.readouterr().out)
    assert blocked["edge"]["blocking"] is False
    assert cli.main(["mesh", "ozma", "blockers", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["counts"]["open"] == 1
    assert cli.main(["mesh", "ozma", "unblock", "--id", "wu4",
                     "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["cleared"] == 1
    assert cli.main(["mesh", "ozma", "unblock", "--id", "wu4",
                     "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["code"] == "unknown_blocker"


# --------------------------------------------------------------------------- #
# A2A lossy translation
# --------------------------------------------------------------------------- #
def test_a2a_round_trip_reports_named_losses_and_never_networks(
        env, monkeypatch) -> None:
    repo, _ = env
    cfg = _configured(repo)
    handoff = B.build_envelope(
        flight_id=B.flight_id(repo, cfg), message_id="hf_a2a",
        ozma_type="handoff",
        body={"workunit": "wu1", "criteria": ["tests", "docs"],
              "refs": ["r1"], "text": "take it"},
        refs=["msg_a"], workunit="wu1", thread="thread-a")
    payload = B.to_a2a(handoff)
    assert payload["lossy"] is True
    assert payload["conformance"] == "declared"
    assert payload["network"] is False
    assert {"criteria", "claim_scope", "blockers", "compliance",
            "workunit", "refs"} <= set(payload["loss_fields"])
    assert payload["metadata"]["extensions"]["priority"] == "extension"

    def forbidden(*args, **kwargs):
        raise AssertionError("A2A translation must not open a socket")

    monkeypatch.setattr(socket, "socket", forbidden)
    rebuilt = B.from_a2a(payload)
    assert rebuilt["lossy"] is True
    assert rebuilt["network"] is False
    assert rebuilt["envelope"]["type"] == "handoff"
    assert "criteria: tests; docs" in rebuilt["envelope"]["body"]["text"]
    assert "criteria" not in rebuilt["envelope"]["body"]
    assert "refs" not in rebuilt["envelope"]
    assert "workunit" not in rebuilt["envelope"]


def test_a2a_redacts_free_text_on_both_directions(env) -> None:
    repo, _ = env
    cfg = _configured(repo)
    secret = "password=supersecret123"
    envelope = B.build_envelope(
        flight_id=B.flight_id(repo, cfg), message_id="ozm_a2a_secret",
        ozma_type="heads-up", body={"text": secret})
    payload = B.to_a2a(envelope)
    assert secret not in json.dumps(payload)
    rebuilt = B.from_a2a(payload)
    assert secret not in json.dumps(rebuilt)
    assert rebuilt["envelope"]["redactions"]

    ack = B.build_envelope(
        flight_id=B.flight_id(repo, cfg), message_id="ozm_a2a_secret_ack",
        ozma_type="ack", body={"accepted": False, "reason": secret})
    ack_payload = B.to_a2a(ack)
    assert secret not in json.dumps(ack_payload)
    ack_rebuilt = B.from_a2a(ack_payload)
    assert secret not in json.dumps(ack_rebuilt)
    assert ack_rebuilt["envelope"]["redactions"]


def test_a2a_refuses_required_unrepresentable_fields(env) -> None:
    repo, _ = env
    cfg = _configured(repo)
    claim = B.build_envelope(
        flight_id=B.flight_id(repo, cfg), message_id="clm_a2a",
        ozma_type="claim", body={"scope": "file", "resource": "src/a.py"})
    with pytest.raises(B.OzmaBridgeRefusal) as excinfo:
        B.to_a2a(claim)
    assert excinfo.value.code == "a2a_unsupported_required"

    block = B.build_envelope(
        flight_id=B.flight_id(repo, cfg), message_id="ozm_block_a2a",
        ozma_type="block", body={"blocked_id": "wu1", "blocker_id": "wu2"})
    with pytest.raises(B.OzmaBridgeRefusal) as excinfo:
        B.to_a2a(block)
    assert excinfo.value.code == "a2a_unsupported_required"

    with pytest.raises(B.OzmaBridgeRefusal) as excinfo:
        B.from_a2a({})
    assert excinfo.value.code == "a2a_invalid"


# --------------------------------------------------------------------------- #
# wake approvals and claim primitive
# --------------------------------------------------------------------------- #
def test_wake_approval_is_cli_only_and_bounded(env) -> None:
    repo, _ = env
    cfg = _configured(repo, wake_modes=("passive", "message-triggered"))
    with pytest.raises(B.OzmaBridgeRefusal) as excinfo:
        B.wake_approve(repo, approved_by="host", mode="message-triggered",
                       budget_turns=1, source="model", config=cfg)
    assert excinfo.value.code == "wake_cli_only"
    with pytest.raises(B.OzmaBridgeRefusal) as excinfo:
        B.wake_approve(repo, approved_by="host", source="cli",
                       mode="scheduled", budget_turns=1, config=cfg)
    assert excinfo.value.code == "wake_mode_not_declared"
    with pytest.raises(B.OzmaBridgeRefusal) as excinfo:
        B.wake_approve(repo, approved_by="host", source="cli",
                       mode="message-triggered", budget_turns=0, config=cfg)
    assert excinfo.value.code == "wake_budget_invalid"
    with pytest.raises(B.OzmaBridgeRefusal) as excinfo:
        B.wake_approve(repo, approved_by="host", source="cli",
                       mode="message-triggered", budget_turns=1, ttl_s=1,
                       config=cfg)
    assert excinfo.value.code == "wake_ttl_invalid"

    approval = B.wake_approve(repo, approved_by="host", source="cli",
                              mode="message-triggered", budget_turns=2,
                              ttl_s=3600, config=cfg, now=1000)
    assert approval["approval"]["approved_by"] == "host"
    assert approval["approval"]["source"] == "cli"
    assert approval["approval"]["used_turns"] == 0
    assert approval["spawnable"] is False


def test_wake_claim_budget_and_no_spawn(env, monkeypatch) -> None:
    repo, state = env
    cfg = _configured(repo)
    M.register(repo, "opencode", "a", exposure="coordinate")
    inbound = _envelope(
        repo, cfg, "ozm_wake_1", body={"text": "BODY CANARY"},
        to_agent="agent:reviewer:wu1", wake="message")
    outcome = B.ingest(repo, inbound, config=cfg, now=1000)
    assert outcome["wake"]["recorded"] is True
    with pytest.raises(B.OzmaBridgeRefusal) as excinfo:
        B.wake_claim(repo, config=cfg, now=1000)
    assert excinfo.value.code == "wake_not_approved"

    B.wake_approve(repo, approved_by="host", source="cli", mode="message-triggered", budget_turns=1,
                   ttl_s=3600, config=cfg, now=1000)
    wake_files = list((state / "mesh" / "ozma" / "wake" / "default").glob(
        "approval-*.json"))
    assert len(wake_files) == 1
    assert stat.S_IMODE(wake_files[0].stat().st_mode) == 0o600
    assert stat.S_IMODE(wake_files[0].parent.stat().st_mode) == 0o700

    def forbidden(*args, **kwargs):
        raise AssertionError("wake claim must never spawn or connect")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(os, "system", forbidden)
    claimed = B.wake_claim(repo, config=cfg, now=1000)
    assert claimed["claimed"] is True
    assert claimed["spawnable"] is False
    assert claimed["approval"]["used_turns"] == 1
    assert claimed["request"]["state"] == "claimed"
    assert claimed["approval"]["claim_ids"] == [
        claimed["request"]["request_id"]]
    assert "BODY CANARY" not in json.dumps(claimed)

    second = _envelope(
        repo, cfg, "ozm_wake_2", body={"text": "second"},
        to_agent="agent:reviewer:wu1", wake="message")
    B.ingest(repo, second, config=cfg, now=1001)
    with pytest.raises(B.OzmaBridgeRefusal) as excinfo:
        B.wake_claim(repo, config=cfg, now=1001)
    assert excinfo.value.code == "wake_budget_exhausted"
    assert state.exists()


def test_scheduled_wake_due_time_and_wrong_mode(env) -> None:
    repo, _ = env
    cfg = _configured(repo)
    M.register(repo, "opencode", "a", exposure="coordinate")
    inbound = _envelope(
        repo, cfg, "ozm_sched", body={"text": "later"},
        to_agent="agent:reviewer:wu1", wake="scheduled",
        wake_at="1970-01-01T00:20:00Z")
    B.ingest(repo, inbound, config=cfg, now=1000)
    with pytest.raises(B.OzmaBridgeRefusal) as excinfo:
        B.wake_claim(repo, config=cfg, now=1000)
    assert excinfo.value.code == "wake_not_due"
    with pytest.raises(B.OzmaBridgeRefusal) as excinfo:
        B.wake_claim(repo, config=cfg, now=1300)
    assert excinfo.value.code == "wake_not_approved"

    B.wake_approve(repo, approved_by="host", source="cli", mode="scheduled", budget_turns=1, ttl_s=3600,
                   config=cfg, now=1300)
    claimed = B.wake_claim(repo, config=cfg, now=1300)
    assert claimed["claimed"] is True
    assert claimed["request"]["mode"] == "scheduled"

    B.wake_approve(repo, approved_by="host", source="cli", mode="scheduled", budget_turns=1, ttl_s=3600,
                   config=cfg, now=1400)
    message = _envelope(
        repo, cfg, "ozm_msg_mode", body={"text": "now"},
        to_agent="agent:reviewer:wu1", wake="message")
    B.ingest(repo, message, config=cfg, now=1400)
    with pytest.raises(B.OzmaBridgeRefusal) as excinfo:
        B.wake_claim(repo, config=cfg, now=1400)
    assert excinfo.value.code == "wake_mode_not_declared"


def test_wake_expiry_and_read_only_listing(env) -> None:
    repo, state = env
    cfg = _configured(repo)
    M.register(repo, "opencode", "a", exposure="coordinate")
    B.wake_approve(repo, approved_by="host", source="cli", mode="message-triggered", budget_turns=2,
                   ttl_s=60, config=cfg, now=1000)
    inbound = _envelope(
        repo, cfg, "ozm_expire", body={"text": "expire"},
        to_agent="agent:reviewer:wu1", wake="message")
    B.ingest(repo, inbound, config=cfg, now=1000)
    before = _snapshot(state)
    listed = B.wake_list(repo, config=cfg, now=1100)
    assert listed["counts"]["pending"] == 1
    assert B.read_wake_status(repo, cfg, now=1100)["pending_requests"] == 1
    assert _snapshot(state) == before

    with pytest.raises(B.OzmaBridgeRefusal) as excinfo:
        B.wake_claim(repo, config=cfg, now=2000)
    assert excinfo.value.code == "wake_expired"


def test_wake_cli_is_host_only_and_never_spawns(env, capsys) -> None:
    repo, _ = env
    cfg = _configured(repo, wake_modes=("passive", "message-triggered"))
    assert cli.main(["mesh", "ozma", "wake", "approve", "--mode",
                     "message-triggered", "--budget-turns", "1",
                     "--json"]) == 0
    approval = json.loads(capsys.readouterr().out)["approval"]
    assert approval["budget_turns"] == 1

    inbound = _envelope(
        repo, cfg, "ozm_cli_wake", body={"text": "cli"},
        to_agent="agent:reviewer:wu1", wake="message")
    B.ingest(repo, inbound, config=cfg)
    assert cli.main(["mesh", "ozma", "wake", "claim", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["claimed"] is True and payload["spawnable"] is False
    status = O.status(repo)["wake"]
    assert status["approved"] == 1
    assert status["budget_turns"] == 1
    assert status["used_turns"] == 1
    assert status["claimable"] is True
    assert status["spawnable"] is False
    assert cli.main(["mesh", "ozma", "wake", "list", "--json"]) == 0
    assert len(json.loads(capsys.readouterr().out)["approvals"]) == 1
    assert cli.main(["mesh", "ozma", "wake", "revoke",
                     approval["approval_id"], "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["approval"]["state"] == "revoked"


def test_wake_revoke_withdraws_an_approval(env, capsys) -> None:
    repo, _ = env
    cfg = _configured(repo)
    assert cli.main(["mesh", "ozma", "wake", "approve", "--mode",
                     "message-triggered", "--budget-turns", "1",
                     "--json"]) == 0
    approval_id = json.loads(capsys.readouterr().out)["approval"][
        "approval_id"]
    inbound = _envelope(
        repo, cfg, "ozm_revoked", body={"text": "revoke"},
        to_agent="agent:reviewer:wu1", wake="message")
    B.ingest(repo, inbound, config=cfg)
    assert cli.main(["mesh", "ozma", "wake", "revoke", approval_id,
                     "--json"]) == 0
    revoked = json.loads(capsys.readouterr().out)["approval"]
    assert revoked["state"] == "revoked"
    with pytest.raises(B.OzmaBridgeRefusal) as excinfo:
        B.wake_claim(repo, config=cfg)
    assert excinfo.value.code == "wake_not_approved"
