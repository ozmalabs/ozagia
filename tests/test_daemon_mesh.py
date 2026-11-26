"""The daemon `mesh` op: layer-2 model coordination, gates and limits."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ozagia.core import mesh as M
from ozagia.daemon import server


@pytest.fixture()
def state(tmp_path: Path) -> server.DaemonState:
    return server.DaemonState(tmp_path, None, tmp_path / "journal.jsonl")


def _mesh(state: server.DaemonState, **fields) -> dict:
    return state.dispatch({"op": "mesh", **fields})


def _register(state: server.DaemonState, session: str = "ses_a",
              exposure: str = "coordinate", harness: str = "opencode") -> dict:
    result = _mesh(state, action="register", harness=harness, session=session,
                   exposure=exposure)
    assert result["ok"] is True, result
    return result


def test_register_heartbeat_and_status_are_compact(tmp_path,
                                                   state: server.DaemonState):
    first = _register(state, "ses_a")
    _register(state, "ses_b", exposure="read")

    assert first["mesh_id"] == "default"
    assert first["self"]["harness"] == "opencode"
    assert first["self"]["session"] == "ses_a"
    assert first["self"]["exposure"] == "coordinate"
    assert first["self"]["key"].startswith("sk_")

    beat = _mesh(state, action="heartbeat", harness="opencode",
                 session="ses_a")
    assert beat["ok"] is True
    assert beat["self"]["effective_state"] == "live"

    status = _mesh(state, action="status", harness="opencode",
                   session="ses_a")
    assert status["ok"] is True
    assert status["self"]["session"] == "ses_a"
    assert [item["session"] for item in status["peers"]] == ["ses_b"]
    assert status["unread"] == 0
    assert "body_subject" not in json.dumps(status)


def test_send_delivers_metadata_and_never_echoes_the_body(
        tmp_path: Path, state: server.DaemonState):
    _register(state, "ses_a")
    _register(state, "ses_b")

    sent = _mesh(state, action="send", harness="opencode", session="ses_a",
                 to="opencode:ses_b", kind="note", subject="hello",
                 body="the canary body")
    assert sent["ok"] is True
    assert sent["delivered"] == 1
    assert sent["message"]["msg_id"].startswith("msg_")
    assert "canary" not in json.dumps(sent)

    inbox = M.inbox(tmp_path, "opencode", "ses_b")
    assert inbox["count"] == 1
    msg_id = inbox["messages"][0]["msg_id"]
    assert "canary" not in json.dumps(inbox)
    read = M.read(tmp_path, "opencode", "ses_b", msg_id)
    assert read["body"] == "the canary body"


def test_lease_actions_and_merge_are_advisory(tmp_path, state):
    _register(state, "ses_a")
    _register(state, "ses_b")

    held = _mesh(state, action="lease_acquire", harness="opencode",
                 session="ses_a", resource="branch:main", mode="write",
                 ttl_s=60, reason="work")
    assert held["ok"] is True and held["acquired"] is True
    lease_id = held["lease"]["lease_id"]

    queued = _mesh(state, action="lease_acquire", harness="opencode",
                   session="ses_b", resource="branch:main", mode="write",
                   ttl_s=60)
    assert queued["ok"] is True and queued["queued"] is True
    assert queued["position"] == 1

    renewed = _mesh(state, action="lease_renew", harness="opencode",
                    session="ses_a", lease_id=lease_id, ttl_s=120)
    assert renewed["ok"] is True and renewed["lease"]["lease_id"] == lease_id

    released = _mesh(state, action="lease_release", harness="opencode",
                     session="ses_a", lease_id=lease_id)
    assert released["ok"] is True
    assert released["promoted"]["holder"]["session"] == "ses_b"

    merged = _mesh(state, action="merge_request", harness="opencode",
                   session="ses_b", target="main", ttl_s=60)
    assert merged["ok"] is True and merged["advisory"] is True
    assert merged["lease"]["resource"]["kind"] == "merge"


def test_worktree_request_and_offer_use_existing_worktrees(
        state, monkeypatch):
    _register(state, "ses_a")
    worktree = Path(state.root) / "wt"
    worktree.mkdir()
    record = {"path": str(worktree.resolve()), "branch": "main",
              "head": "abc", "detached": False, "bare": False,
              "locked": None, "prunable": None, "is_main": False,
              "exists": True}
    monkeypatch.setattr(M.W, "discover", lambda root: [record])
    monkeypatch.setattr(M.W, "status",
                        lambda path: {"branch": "main", "dirty": False})

    request = _mesh(state, action="worktree_request", harness="opencode",
                    session="ses_a", branch="main")
    assert request["ok"] is True and request["acquired"] is True
    assert request["worktree"]["branch"] == "main"
    assert {item["resource"]["kind"] for item in request["leases"]} == {
        "worktree", "branch"}

    offer = _mesh(state, action="worktree_offer", harness="opencode",
                  session="ses_a", branch="main")
    assert offer["ok"] is True
    assert offer["worktree"]["path"] == str(worktree.resolve())


def test_read_exposure_only_reaches_status(state):
    _register(state, "ses_read", exposure="read")
    status = _mesh(state, action="status", harness="opencode",
                   session="ses_read")
    assert status["ok"] is True
    for action, fields in (
            ("send", {"to": "opencode:ses_b", "kind": "note",
                      "subject": "hi"}),
            ("lease_acquire", {"resource": "branch:main", "mode": "read"}),
            ("worktree_request", {"branch": "main"}),
            ("merge_request", {"target": "main"})):
        refused = _mesh(state, action=action, harness="opencode",
                        session="ses_read", **fields)
        assert refused["ok"] is False
        assert refused["code"] == "not_permitted"


def test_actor_field_or_missing_registration_cannot_spoof_identity(
        tmp_path, state):
    _register(state, "ses_a")
    _register(state, "ses_b")
    sent = _mesh(state, action="send", harness="opencode", session="ses_a",
                 actor={"harness": "opencode", "session": "ses_admin"},
                 to="opencode:ses_b", kind="note", subject="from a")
    assert sent["ok"] is True
    inbox = M.inbox(tmp_path, "opencode", "ses_b")
    assert inbox["messages"][0]["from"]["session"] == "ses_a"

    missing = _mesh(state, action="send", harness="opencode",
                    session="ses_missing",
                    actor={"harness": "opencode", "session": "ses_a"},
                    to="opencode:ses_b", kind="note", subject="forged")
    assert missing["ok"] is False
    assert missing["code"] == "unknown_session"

    arbitrary = _mesh(state, action="status", harness="elsewhere",
                      session="ses_a")
    assert arbitrary["ok"] is False and arbitrary["code"] == "not_permitted"


def test_rate_limit_is_per_actor_and_action_group(state, monkeypatch):
    _register(state, "ses_a")
    _register(state, "ses_b")
    monkeypatch.setitem(server.MESH_RATE_LIMITS, "message", 1)
    first = _mesh(state, action="send", harness="opencode", session="ses_a",
                  to="opencode:ses_b", kind="note", subject="one")
    assert first["ok"] is True
    limited = _mesh(state, action="send", harness="opencode", session="ses_a",
                    to="opencode:ses_b", kind="note", subject="two")
    assert limited["ok"] is False and limited["code"] == "rate_limited"
    assert isinstance(limited["retry_s"], int) and limited["retry_s"] > 0
    # Another action group (and another actor) is not consumed by the send.
    other = _mesh(state, action="lease_acquire", harness="opencode",
                  session="ses_a", resource="grant:cpu", mode="read")
    assert other["ok"] is True


def test_admin_actions_are_refused_and_audited_without_contents(
        tmp_path: Path, state: server.DaemonState):
    _register(state, "ses_a")
    for action in ("approve", "takeover", "lease_takeover", "link_request",
                   "link_accept", "delegation_grant", "prune", "unregister",
                   "worktree_create"):
        refused = _mesh(state, action=action, harness="opencode",
                        session="ses_a")
        assert refused["ok"] is False
        assert refused["code"] == "not_permitted"
    unknown = _mesh(state, action="explode", harness="opencode",
                    session="ses_a")
    assert unknown["ok"] is False and unknown["code"] == "invalid_action"

    sent = _mesh(state, action="send", harness="opencode", session="ses_a",
                 broadcast=True, kind="note", subject="subject canary",
                 body="body canary")
    assert sent["ok"] is True
    audit = (M.mesh_dir(tmp_path) / "audit" / "events.jsonl").read_text(
        encoding="utf-8")
    assert "model_action" in audit and '"action": "send"' in audit
    assert "body canary" not in audit and "subject canary" not in audit
    assert "actor" in audit
