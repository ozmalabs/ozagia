"""The daemon `mesh` op at layer 3: supervision, links and delegations."""
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


def _register(state: server.DaemonState, session: str,
              exposure: str = "supervise", harness: str = "opencode",
              **fields) -> dict:
    result = _mesh(state, action="register", harness=harness, session=session,
                   exposure=exposure, **fields)
    assert result["ok"] is True, result
    return result


def _linked(state: server.DaemonState, first: str = "ses_a",
            second: str = "ses_b", relation: str = "peer") -> str:
    requested = _mesh(state, action="link_request", harness="opencode",
                      session=first, to=f"opencode:{second}",
                      relation=relation)
    assert requested["ok"] is True, requested
    link_id = requested["link"]["link_id"]
    accepted = _mesh(state, action="link_accept", harness="opencode",
                     session=second, link_id=link_id)
    assert accepted["ok"] is True, accepted
    return link_id


def test_link_handshake_is_reciprocal_and_topology_is_compact(
        tmp_path: Path, state: server.DaemonState):
    _register(state, "ses_a")
    _register(state, "ses_b")

    requested = _mesh(state, action="link_request", harness="opencode",
                      session="ses_a", to={"harness": "opencode",
                                           "session": "ses_b"},
                      relation="sub", ttl_s=600,
                      reason="REASON_CANARY")
    assert requested["ok"] is True and requested["created"] is True
    link = requested["link"]
    assert link["relation"] == "sub"
    assert link["state"] == "pending"
    assert link["peer"] == {"harness": "opencode", "session": "ses_b"}
    assert "REASON_CANARY" not in json.dumps(requested)
    assert "history" not in json.dumps(requested)

    link_id = link["link_id"]
    accepted = _mesh(state, action="link_accept", harness="opencode",
                     session="ses_b", link_id=link_id)
    assert accepted["ok"] is True
    assert accepted["link"]["state"] == "active"
    assert accepted["link"]["relation"] == "super"

    status_a = _mesh(state, action="status", harness="opencode",
                     session="ses_a")
    status_b = _mesh(state, action="status", harness="opencode",
                     session="ses_b")
    assert status_a["self"]["role"] == "sub"
    assert status_b["self"]["role"] == "super"

    listed = _mesh(state, action="link_list", harness="opencode",
                   session="ses_a")
    assert listed["ok"] is True
    assert [item["link_id"] for item in listed["links"]] == [link_id]
    shown = _mesh(state, action="link_show", harness="opencode",
                  session="ses_a", link_id=link_id)
    assert shown["ok"] is True and shown["link"]["relation"] == "sub"

    topology = _mesh(state, action="topology", harness="opencode",
                     session="ses_a", worktree="/tmp/CANARY_PATH")
    assert topology["ok"] is True
    assert topology["self"]["role"] == "sub"
    assert topology["role"] == "sub"
    assert [item["link_id"] for item in topology["links"]] == [link_id]
    assert topology["counts"]["links"] == {"active": 1}
    assert "CANARY_PATH" not in json.dumps(topology)
    assert "REASON_CANARY" not in json.dumps(topology)

    revoked = _mesh(state, action="link_revoke", harness="opencode",
                    session="ses_b", link_id=link_id,
                    reason="REASON_CANARY")
    assert revoked["ok"] is True and revoked["link"]["state"] == "revoked"


def test_link_decline_conflict_self_unknown_and_stale(state):
    _register(state, "ses_a")
    _register(state, "ses_b")
    _register(state, "ses_c")

    pending = _mesh(state, action="link_request", harness="opencode",
                    session="ses_a", to="opencode:ses_b", relation="sub")
    assert pending["ok"] is True
    conflict = _mesh(state, action="link_request", harness="opencode",
                     session="ses_a", to="opencode:ses_b", relation="super")
    assert conflict["ok"] is False and conflict["code"] == "link_conflict"

    declined = _mesh(state, action="link_decline", harness="opencode",
                     session="ses_b", link_id=pending["link"]["link_id"],
                     reason="REASON_CANARY")
    assert declined["ok"] is True and declined["link"]["state"] == "declined"

    self_link = _mesh(state, action="link_request", harness="opencode",
                      session="ses_a", to="opencode:ses_a", relation="peer")
    assert self_link["ok"] is False and self_link["code"] == "self_link"
    unknown = _mesh(state, action="link_request", harness="opencode",
                    session="ses_a", to="opencode:ses_missing",
                    relation="peer")
    assert unknown["ok"] is False and unknown["code"] == "unknown_peer"
    missing = _mesh(state, action="link_accept", harness="opencode",
                    session="ses_a", link_id="lnk_0123456789abcdef")
    assert missing["ok"] is False and missing["code"] == "unknown_link"

    M.unregister(state.root, "opencode", "ses_c")
    stale_peer = _mesh(state, action="link_request", harness="opencode",
                       session="ses_a", to="opencode:ses_c", relation="peer")
    assert stale_peer["ok"] is False and stale_peer["code"] == "peer_not_live"

    link_id = _linked(state, "ses_a", "ses_b")
    M.unregister(state.root, "opencode", "ses_b")
    stale = _mesh(state, action="link_show", harness="opencode",
                  session="ses_a", link_id=link_id)
    assert stale["ok"] is True and stale["link"]["state"] == "stale"


def test_delegation_is_owner_scoped_bounded_and_revocable(
        tmp_path: Path, state: server.DaemonState):
    _register(state, "ses_a")
    _register(state, "ses_b")
    held = _mesh(state, action="lease_acquire", harness="opencode",
                 session="ses_a", resource="branch:main", mode="write",
                 ttl_s=600)
    assert held["ok"] is True and held["acquired"] is True
    lease_id = held["lease"]["lease_id"]

    unlinked = _mesh(state, action="delegation_grant", harness="opencode",
                     session="ses_a", to="opencode:ses_b",
                     actions=["lease_release"], lease_id=lease_id)
    assert unlinked["ok"] is False and unlinked["code"] == "not_linked"

    link_id = _linked(state, "ses_a", "ses_b")
    not_owner = _mesh(state, action="delegation_grant", harness="opencode",
                      session="ses_b", to="opencode:ses_a",
                      actions=["lease_release"], lease_id=lease_id)
    assert not_owner["ok"] is False and not_owner["code"] == "not_holder"

    bad_scope = _mesh(state, action="delegation_grant", harness="opencode",
                      session="ses_a", to="opencode:ses_b",
                      actions=["mailbox_read"])
    assert bad_scope["ok"] is False and bad_scope["code"] == "invalid_scope"

    granted = _mesh(state, action="delegation_grant", harness="opencode",
                    session="ses_a", to="opencode:ses_b",
                    actions=["lease_release"], lease_id=lease_id,
                    ttl_s=600, max_uses=1, reason="REASON_CANARY")
    assert granted["ok"] is True and granted["created"] is True
    delegation = granted["delegation"]
    assert delegation["delegation_id"].startswith("dlg_")
    assert delegation["grantor"]["session"] == "ses_a"
    assert delegation["grantee"]["session"] == "ses_b"
    assert delegation["actions"] == ["lease_release"]
    assert delegation["lease_id"] == lease_id
    assert delegation["max_uses"] == 1
    assert delegation["state"] == "active"
    assert "REASON_CANARY" not in json.dumps(granted)
    delegation_id = delegation["delegation_id"]

    listed = _mesh(state, action="delegation_list", harness="opencode",
                   session="ses_b")
    assert listed["ok"] is True
    assert [item["delegation_id"] for item in listed["delegations"]] == [
        delegation_id]
    shown = _mesh(state, action="delegation_show", harness="opencode",
                  session="ses_a", delegation_id=delegation_id)
    assert shown["ok"] is True and shown["delegation"]["link_id"] == link_id

    topology = _mesh(state, action="topology", harness="opencode",
                     session="ses_b")
    assert topology["ok"] is True
    assert [item["delegation_id"] for item in topology["delegations"]] == [
        delegation_id]
    assert "resource" not in topology["delegations"][0]
    assert "lease_id" not in topology["delegations"][0]

    # One use, then max_uses is exhausted; the delegation is never a link
    # capability by itself.
    M.lease_release(state.root, "opencode", "ses_b", lease_id,
                    delegation_id=delegation_id)
    used = _mesh(state, action="delegation_show", harness="opencode",
                 session="ses_a", delegation_id=delegation_id)
    assert used["delegation"]["state"] == "expired"
    assert used["delegation"]["remaining_uses"] == 0

    second = _mesh(state, action="delegation_grant", harness="opencode",
                   session="ses_a", to="opencode:ses_b",
                   actions=["mailbox_read"], mailbox=True,
                   ttl_s=600, max_uses=2)
    assert second["ok"] is True
    second_id = second["delegation"]["delegation_id"]
    not_grantor = _mesh(state, action="delegation_revoke", harness="opencode",
                        session="ses_b", delegation_id=second_id)
    assert not_grantor["ok"] is False and not_grantor["code"] == "not_grantor"
    revoked = _mesh(state, action="delegation_revoke", harness="opencode",
                    session="ses_a", delegation_id=second_id,
                    reason="REASON_CANARY")
    assert revoked["ok"] is True and revoked["delegation"]["state"] == "revoked"
    unknown = _mesh(state, action="delegation_show", harness="opencode",
                    session="ses_a", delegation_id="dlg_0123456789abcdef")
    assert unknown["ok"] is False and unknown["code"] == "unknown_delegation"


def test_coordinate_and_read_cannot_reach_supervision_actions(state):
    _register(state, "ses_coord", exposure="coordinate")
    _register(state, "ses_read", exposure="read")
    for session in ("ses_coord", "ses_read"):
        for action, fields in (
                ("link_request", {"to": "opencode:ses_b",
                                  "relation": "peer"}),
                ("link_accept", {"link_id": "lnk_0123456789abcdef"}),
                ("delegation_grant",
                 {"to": "opencode:ses_b", "actions": ["mailbox_read"],
                  "mailbox": True}),
                ("topology", {})):
            refused = _mesh(state, action=action, harness="opencode",
                            session=session, **fields)
            assert refused["ok"] is False, (session, action, refused)
            assert refused["code"] == "not_permitted"


def test_supervision_actions_are_rate_limited_per_actor(state, monkeypatch):
    _register(state, "ses_a")
    _register(state, "ses_b")
    monkeypatch.setitem(server.MESH_RATE_LIMITS, "supervise", 1)

    first = _mesh(state, action="link_request", harness="opencode",
                  session="ses_a", to="opencode:ses_b", relation="peer")
    assert first["ok"] is True
    limited = _mesh(state, action="link_request", harness="opencode",
                    session="ses_a", to="opencode:ses_b", relation="peer")
    assert limited["ok"] is False and limited["code"] == "rate_limited"
    assert isinstance(limited["retry_s"], int) and limited["retry_s"] > 0

    # Reads and another actor are not consumed by the same bucket.
    assert _mesh(state, action="topology", harness="opencode",
                 session="ses_a")["ok"] is True
    assert _mesh(state, action="link_request", harness="opencode",
                 session="ses_b", to="opencode:ses_a",
                 relation="peer")["ok"] is True


def test_admin_actions_stay_refused_and_audit_has_no_reason_or_body(
        tmp_path: Path, state: server.DaemonState):
    _register(state, "ses_a")
    _register(state, "ses_b")
    for action in ("approve", "takeover", "lease_takeover", "merge_apply",
                   "prune", "unregister", "worktree_create", "admin"):
        refused = _mesh(state, action=action, harness="opencode",
                        session="ses_a")
        assert refused["ok"] is False and refused["code"] == "not_permitted"

    link = _mesh(state, action="link_request", harness="opencode",
                 session="ses_a", to="opencode:ses_b", relation="peer",
                 reason="REASON_CANARY")
    assert link["ok"] is True
    link_id = link["link"]["link_id"]
    _mesh(state, action="link_accept", harness="opencode", session="ses_b",
          link_id=link_id)
    held = _mesh(state, action="lease_acquire", harness="opencode",
                 session="ses_a", resource="branch:work", mode="write")
    grant = _mesh(state, action="delegation_grant", harness="opencode",
                  session="ses_a", to="opencode:ses_b",
                  actions=["lease_release"],
                  lease_id=held["lease"]["lease_id"],
                  reason="REASON_CANARY")
    assert grant["ok"] is True

    audit = (M.mesh_dir(tmp_path) / "audit" / "events.jsonl").read_text(
        encoding="utf-8")
    assert '"action": "link_request"' in audit
    assert '"action": "delegation_grant"' in audit
    assert link_id in audit
    assert grant["delegation"]["delegation_id"] in audit
    assert "REASON_CANARY" not in audit
    assert '"reason"' not in audit
    assert "subject" not in audit and "body" not in audit
