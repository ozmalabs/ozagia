"""Links, derived roles and bounded delegations in the local mesh."""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from ozagia.core import mesh as M


@pytest.fixture()
def env(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    state = tmp_path / "state"
    monkeypatch.setenv("OZAGIA_ROOT", str(repo))
    monkeypatch.setenv("OZAGIA_STATE_DIR", str(state))
    monkeypatch.delenv("OZAGIA_MESH_ID", raising=False)
    return repo, state


def _register(root: Path, session: str, *,
              exposure: str = "coordinate") -> dict:
    return M.register(root, "opencode", session, pid=os.getpid(),
                      pid_start=M._proc_start(os.getpid()),
                      boot_id=M._boot_id(), exposure=exposure)


def _accept_link(repo: Path, *, requester: str = "a", target: str = "b",
                 relation: str = "peer", ttl_s: int = M.LINK_TTL_S) -> dict:
    request = M.link_request(repo, "opencode", requester,
                             to=f"opencode:{target}", relation=relation,
                             ttl_s=ttl_s)
    return M.link_accept(repo, "opencode", target,
                         request["link"]["link_id"])["link"]


def _link_path(repo: Path, link_id: str) -> Path:
    return M.mesh_dir(repo) / "links" / f"{link_id}.json"


def _delegation_path(repo: Path, delegation_id: str) -> Path:
    return M.mesh_dir(repo) / "delegations" / f"{delegation_id}.json"


def _rewrite(path: Path, **changes) -> dict:
    record = json.loads(path.read_text(encoding="utf-8"))
    record.update(changes)
    path.write_text(json.dumps(record), encoding="utf-8")
    return record


def _snapshot(state: Path) -> dict[str, tuple[int, int, int, int, bytes]]:
    return {
        str(path.relative_to(state)): (
            path.stat().st_size, path.stat().st_mtime_ns,
            path.stat().st_ctime_ns, stat.S_IMODE(path.stat().st_mode),
            path.read_bytes())
        for path in sorted(state.rglob("*")) if path.is_file()
    }


def test_request_accept_derives_reciprocal_roles_and_history(env) -> None:
    repo, _ = env
    _register(repo, "a")
    _register(repo, "b")
    requested = M.link_request(repo, "opencode", "a", to="opencode:b",
                               relation="sub", reason="own the branch")
    link = requested["link"]
    assert requested["created"] is True
    assert link["effective_state"] == "pending"
    assert link["relation"] == "sub"
    assert link["reciprocal_relation"] == "super"
    assert link["accepted_at"] is None
    assert link["to"]["session"] == "b"

    before = M.status(repo, harness="opencode", session="a")
    assert before["self"]["role"] == "peer"
    assert before["self"]["links"][0]["state"] == "pending"
    assert "history" not in before["links"][0]

    accepted = M.link_accept(repo, "opencode", "b", link["link_id"])
    assert accepted["created"] is True
    assert accepted["link"]["effective_state"] == "active"
    assert accepted["link"]["accepted_at"] is not None

    first = M.status(repo, harness="opencode", session="a")
    second = M.status(repo, harness="opencode", session="b")
    assert first["self"]["role"] == "sub"
    assert second["self"]["role"] == "super"
    assert first["self"]["links"][0]["relation"] == "sub"
    assert second["self"]["links"][0]["relation"] == "super"
    assert first["self"]["links"][0]["reciprocal_relation"] == "super"
    assert second["self"]["links"][0]["reciprocal_relation"] == "sub"

    # Registration still stores the default; the view derives the role.
    record = json.loads((M.mesh_dir(repo) / "sessions"
                         / f"{first['self']['key']}.json").read_text(
                             encoding="utf-8"))
    assert record["role"] == "peer"

    again = M.link_accept(repo, "opencode", "b", link["link_id"])
    assert again["created"] is False
    shown = M.link_show(repo, link["link_id"])
    assert [item["event"] for item in shown["link"]["history"]] == [
        "requested", "accepted"]

    revoked = M.link_revoke(repo, "opencode", "a", link["link_id"],
                            reason="finished")
    assert revoked["link"]["effective_state"] == "revoked"
    assert [item["event"] for item in revoked["link"]["history"]] == [
        "requested", "accepted", "revoked"]
    after = M.status(repo, harness="opencode", session="b")
    assert after["self"]["role"] == "peer"


def test_decline_is_target_only_and_grants_no_role(env) -> None:
    repo, _ = env
    _register(repo, "a")
    _register(repo, "b")
    request = M.link_request(repo, "opencode", "a", to="opencode:b",
                             relation="super")
    link_id = request["link"]["link_id"]
    with pytest.raises(M.MeshRefusal) as refused:
        M.link_decline(repo, "opencode", "a", link_id)
    assert refused.value.code == "not_target"
    declined = M.link_decline(repo, "opencode", "b", link_id, reason="no")
    assert declined["link"]["effective_state"] == "declined"
    assert M.link_decline(repo, "opencode", "b", link_id)["created"] is False
    assert M.status(repo, harness="opencode", session="a")["self"]["role"] == "peer"
    assert M.status(repo, harness="opencode", session="b")["self"]["role"] == "peer"
    with pytest.raises(M.MeshRefusal) as refused:
        M.link_accept(repo, "opencode", "b", link_id)
    assert refused.value.code == "link_closed"


def test_revoke_works_from_either_side_and_is_idempotent(env) -> None:
    repo, _ = env
    _register(repo, "a")
    _register(repo, "b")
    first = _accept_link(repo, requester="a", target="b", relation="peer")
    revoked = M.link_revoke(repo, "opencode", "b", first["link_id"])
    assert revoked["link"]["effective_state"] == "revoked"
    assert revoked["link"]["revoked_at"] is not None
    assert M.link_revoke(repo, "opencode", "b",
                         first["link_id"])["created"] is False
    second = _accept_link(repo, requester="b", target="a", relation="super")
    assert M.link_revoke(repo, "opencode", "a",
                         second["link_id"])["link"]["effective_state"] == "revoked"


def test_self_unknown_stale_and_conflicting_requests(env) -> None:
    repo, _ = env
    _register(repo, "a")
    _register(repo, "b")
    with pytest.raises(M.MeshRefusal) as refused:
        M.link_request(repo, "opencode", "a", to="opencode:a", relation="peer")
    assert refused.value.code == "self_link"
    with pytest.raises(M.MeshRefusal) as refused:
        M.link_request(repo, "opencode", "a", to="opencode:nobody",
                       relation="peer")
    assert refused.value.code == "unknown_peer"

    path = M.mesh_dir(repo) / "sessions" / (
        M.session_key(M.repo_id(repo), M.mesh_id(repo), "opencode", "b")
        + ".json")
    _rewrite(path, heartbeat_at=M._iso(M._epoch() - M.STALE_S - 10))
    with pytest.raises(M.MeshRefusal) as refused:
        M.link_request(repo, "opencode", "a", to="opencode:b", relation="peer")
    assert refused.value.code == "peer_not_live"
    _rewrite(path, heartbeat_at=M._iso())

    first = M.link_request(repo, "opencode", "a", to="opencode:b",
                           relation="sub")
    same = M.link_request(repo, "opencode", "a", to="opencode:b",
                          relation="sub")
    assert same["created"] is False
    assert same["link"]["link_id"] == first["link"]["link_id"]
    with pytest.raises(M.MeshRefusal) as refused:
        M.link_request(repo, "opencode", "a", to="opencode:b",
                       relation="super")
    assert refused.value.code == "link_conflict"
    with pytest.raises(M.MeshRefusal) as refused:
        M.link_request(repo, "opencode", "b", to="opencode:a",
                       relation="sub")
    assert refused.value.code == "link_conflict"
    with pytest.raises(M.MeshRefusal) as refused:
        M.link_accept(repo, "opencode", "b", "lnk_" + "0" * 16)
    assert refused.value.code == "unknown_link"


def test_idempotency_keys_are_scoped_and_conflicts_are_explicit(env) -> None:
    repo, _ = env
    _register(repo, "a")
    _register(repo, "b")
    _register(repo, "c")
    first = M.link_request(repo, "opencode", "a", to="opencode:b",
                           relation="peer", idempotency_key="K1")
    replay = M.link_request(repo, "opencode", "a", to="opencode:b",
                            relation="peer", idempotency_key="K1")
    assert replay["created"] is False
    assert replay["link"]["link_id"] == first["link"]["link_id"]
    with pytest.raises(M.MeshRefusal) as refused:
        M.link_request(repo, "opencode", "a", to="opencode:b",
                       relation="sub", idempotency_key="K1")
    assert refused.value.code == "id_conflict"
    with pytest.raises(M.MeshRefusal) as refused:
        M.link_request(repo, "opencode", "a", to="opencode:c",
                       relation="peer", idempotency_key="K1")
    assert refused.value.code == "id_conflict"


def test_ttl_expiry_and_stale_links_are_reported_not_rewritten(env) -> None:
    repo, _ = env
    _register(repo, "a")
    _register(repo, "b")
    request = M.link_request(repo, "opencode", "a", to="opencode:b",
                             relation="peer", ttl_s=60)
    link_id = request["link"]["link_id"]
    _rewrite(_link_path(repo, link_id),
             created_at=M._iso(M._epoch() - 120))
    shown = M.link_show(repo, link_id)["link"]
    assert shown["effective_state"] == "expired"
    assert shown["state"] == "pending"
    with pytest.raises(M.MeshRefusal) as refused:
        M.link_accept(repo, "opencode", "b", link_id)
    assert refused.value.code == "link_expired"

    replacement = M.link_request(repo, "opencode", "a", to="opencode:b",
                                 relation="peer", ttl_s=60)
    assert replacement["created"] is True
    assert replacement["link"]["link_id"] != link_id
    active_id = replacement["link"]["link_id"]
    M.link_accept(repo, "opencode", "b", active_id)
    _rewrite(_link_path(repo, active_id),
             expires_at=M._iso(M._epoch() - 5))
    expired = M.link_show(repo, active_id)["link"]
    assert expired["effective_state"] == "expired"
    assert expired["state"] == "active"
    relinked = M.link_request(repo, "opencode", "a", to="opencode:b",
                              relation="peer", ttl_s=60)
    assert relinked["created"] is True
    assert relinked["link"]["link_id"] != active_id
    latest_id = relinked["link"]["link_id"]
    M.link_accept(repo, "opencode", "b", latest_id)

    b_path = M.mesh_dir(repo) / "sessions" / (
        M.session_key(M.repo_id(repo), M.mesh_id(repo), "opencode", "b")
        + ".json")
    _rewrite(b_path, heartbeat_at=M._iso(M._epoch() - M.STALE_S - 10))
    stale = M.link_show(repo, latest_id)["link"]
    assert stale["effective_state"] == "stale"
    assert stale["effective_state"] != "dead"
    assert stale["state"] == "active"
    assert stale["to_liveness"] == "stale"
    _rewrite(b_path, heartbeat_at=M._iso())
    assert M.link_show(repo, latest_id)["link"][
        "effective_state"] == "active"


def test_super_role_confers_no_lease_or_mailbox_authority(env) -> None:
    repo, _ = env
    _register(repo, "a")
    _register(repo, "b")
    _accept_link(repo, requester="a", target="b", relation="super")
    assert M.status(repo, harness="opencode", session="a")["self"][
        "role"] == "super"
    assert M.status(repo, harness="opencode", session="b")["self"][
        "role"] == "sub"
    lease = M.lease_acquire(repo, "opencode", "b", kind="branch",
                            resource_id="main", mode="write")["lease"]
    with pytest.raises(M.MeshRefusal) as refused:
        M.lease_release(repo, "opencode", "a", lease["lease_id"])
    assert refused.value.code == "not_holder"
    M.send(repo, "opencode", "b", to="opencode:b", kind="note",
           subject="private")
    message_id = M.inbox(repo, "opencode", "b")["messages"][0]["msg_id"]
    with pytest.raises(M.MeshRefusal) as refused:
        M.read(repo, "opencode", "a", message_id)
    assert refused.value.code == "unknown_message"


def test_roles_mix_sub_and_super_and_peer_links_stay_peer(env) -> None:
    repo, _ = env
    for name in ("a", "b", "c", "d"):
        _register(repo, name)
    _accept_link(repo, requester="a", target="b", relation="peer")
    assert M.status(repo, harness="opencode", session="a")["self"][
        "role"] == "peer"
    _accept_link(repo, requester="a", target="c", relation="sub")
    assert M.status(repo, harness="opencode", session="a")["self"][
        "role"] == "sub"
    _accept_link(repo, requester="a", target="d", relation="super")
    assert M.status(repo, harness="opencode", session="a")["self"][
        "role"] == "mixed"


def test_links_are_scoped_and_reads_do_not_write(env, monkeypatch) -> None:
    repo, state = env
    _register(repo, "a")
    _register(repo, "b")
    link = _accept_link(repo, requester="a", target="b", relation="peer")
    before = _snapshot(state)
    assert M.status(repo, harness="opencode", session="a")["links"]
    assert M.link_list(repo)["links"]
    assert len(M.link_list(repo, harness="opencode", session="a")["links"]) == 1
    assert M.link_show(repo, link["link_id"])["link"]["link_id"]
    assert M.delegation_list(repo)["delegations"] == []
    after = _snapshot(state)
    assert before == after

    monkeypatch.setenv("OZAGIA_MESH_ID", "other")
    assert M.link_list(repo)["links"] == []
    assert M.delegation_list(repo)["delegations"] == []
    with pytest.raises(M.MeshRefusal) as refused:
        M.link_show(repo, link["link_id"])
    assert refused.value.code == "unknown_link"

    other = repo.parent / "other"
    other.mkdir()
    monkeypatch.delenv("OZAGIA_MESH_ID", raising=False)
    with pytest.raises(M.MeshScopeError):
        M.link_list(other)
    with pytest.raises(M.MeshScopeError):
        M.delegation_list(other)


def test_a_link_never_releases_a_lease_or_reads_a_mailbox(env) -> None:
    repo, _ = env
    _register(repo, "a")
    _register(repo, "b")
    link = _accept_link(repo, requester="a", target="b", relation="peer")
    assert link["effective_state"] == "active"
    lease = M.lease_acquire(repo, "opencode", "a", kind="branch",
                            resource_id="main", mode="write")["lease"]
    with pytest.raises(M.MeshRefusal) as refused:
        M.lease_release(repo, "opencode", "b", lease["lease_id"])
    assert refused.value.code == "not_holder"
    assert M.lease_status(repo, lease["lease_id"])["lease"][
        "effective_state"] == "active"

    M.send(repo, "opencode", "a", to="opencode:a", kind="note",
           subject="private")
    own = M.inbox(repo, "opencode", "b")
    assert own["count"] == 0
    message_id = M.inbox(repo, "opencode", "a")["messages"][0]["msg_id"]
    with pytest.raises(M.MeshRefusal) as refused:
        M.read(repo, "opencode", "b", message_id)
    assert refused.value.code == "unknown_message"


def test_delegation_is_owner_only_linked_and_exactly_scoped(env) -> None:
    repo, _ = env
    for name in ("a", "b", "c", "d"):
        _register(repo, name)
    _accept_link(repo, requester="a", target="b", relation="peer")
    _accept_link(repo, requester="a", target="c", relation="peer")
    first = M.lease_acquire(repo, "opencode", "a", kind="branch",
                            resource_id="main", mode="write")["lease"]
    second = M.lease_acquire(repo, "opencode", "a", kind="branch",
                             resource_id="dev", mode="write")["lease"]
    # The grantor must hold the exact lease.
    with pytest.raises(M.MeshRefusal) as refused:
        M.delegation_grant(repo, "opencode", "b", to="opencode:a",
                           actions=["lease_release"],
                           lease_id=first["lease_id"])
    assert refused.value.code == "not_holder"
    # No active link means no delegation.
    with pytest.raises(M.MeshRefusal) as refused:
        M.delegation_grant(repo, "opencode", "a", to="opencode:d",
                           actions=["lease_release"],
                           lease_id=first["lease_id"])
    assert refused.value.code == "not_linked"
    # A pending link is not enough either.
    M.link_request(repo, "opencode", "a", to="opencode:d",
                   relation="peer")
    with pytest.raises(M.MeshRefusal) as refused:
        M.delegation_grant(repo, "opencode", "a", to="opencode:d",
                           actions=["lease_release"],
                           lease_id=first["lease_id"])
    assert refused.value.code == "link_pending"

    granted = M.delegation_grant(
        repo, "opencode", "a", to="opencode:b",
        actions=["lease_release"], lease_id=first["lease_id"], max_uses=1)
    delegation = granted["delegation"]
    assert delegation["effective_state"] == "active"
    assert delegation["lease_id"] == first["lease_id"]
    assert delegation["resource"] == "branch:main"
    assert delegation["used"] == 0 and delegation["max_uses"] == 1
    status = M.status(repo, harness="opencode", session="a")
    summary = status["delegations"][0]
    assert summary["delegation_id"] == delegation["delegation_id"]
    assert summary["actions"] == ["lease_release"]
    assert summary["scope"] == f"lease_release:{first['lease_id']}"
    assert "history" not in summary and "reason" not in summary

    # Wrong exact lease.
    with pytest.raises(M.MeshRefusal) as refused:
        M.lease_release(repo, "opencode", "b", second["lease_id"],
                        delegation_id=delegation["delegation_id"])
    assert refused.value.code == "delegation_denied"
    assert M.lease_status(repo, second["lease_id"])["lease"][
        "effective_state"] == "active"

    released = M.lease_release(repo, "opencode", "b", first["lease_id"],
                               delegation_id=delegation["delegation_id"])
    assert released["lease"]["effective_state"] == "released"
    assert released["delegation_id"] == delegation["delegation_id"]
    used = M.delegation_show(repo, delegation["delegation_id"])["delegation"]
    assert used["used"] == 1 and used["effective_state"] == "expired"
    with pytest.raises(M.MeshRefusal) as refused:
        M.lease_release(repo, "opencode", "b", first["lease_id"],
                        delegation_id=delegation["delegation_id"])
    assert refused.value.code == "delegation_used"

    # Role alone is never an authorization: c is linked too but has no grant.
    with pytest.raises(M.MeshRefusal) as refused:
        M.lease_release(repo, "opencode", "c", second["lease_id"])
    assert refused.value.code == "not_holder"


def test_delegation_expiry_max_uses_and_revocation(env) -> None:
    repo, _ = env
    _register(repo, "a")
    _register(repo, "b")
    _accept_link(repo, requester="a", target="b", relation="peer")
    M.send(repo, "opencode", "b", to="opencode:a", kind="note",
           subject="one")
    M.send(repo, "opencode", "b", to="opencode:a", kind="note",
           subject="two")
    messages = M.inbox(repo, "opencode", "a")["messages"]
    assert len(messages) == 2
    by_subject = {item["subject"]: item["msg_id"] for item in messages}
    assert set(by_subject) == {"one", "two"}

    grant = M.delegation_grant(
        repo, "opencode", "a", to="opencode:b", actions=["mailbox_read"],
        mailbox=True, max_uses=2)["delegation"]
    listed = M.inbox(repo, "opencode", "b",
                     delegation_id=grant["delegation_id"])
    assert listed["count"] == 2
    assert listed["mailbox_owner"] == M.session_key(
        M.repo_id(repo), M.mesh_id(repo), "opencode", "a")
    first = M.read(repo, "opencode", "b", by_subject["one"],
                   delegation_id=grant["delegation_id"])
    assert first["subject"] == "one"
    second = M.read(repo, "opencode", "b", by_subject["two"],
                    delegation_id=grant["delegation_id"])
    assert second["subject"] == "two"
    with pytest.raises(M.MeshRefusal) as refused:
        M.inbox(repo, "opencode", "b", delegation_id=grant["delegation_id"])
    assert refused.value.code == "delegation_used"

    expiring = M.delegation_grant(
        repo, "opencode", "a", to="opencode:b", actions=["mailbox_read"],
        mailbox=True, ttl_s=60)["delegation"]
    _rewrite(_delegation_path(repo, expiring["delegation_id"]),
             expires_at=M._iso(M._epoch() - 1))
    with pytest.raises(M.MeshRefusal) as refused:
        M.inbox(repo, "opencode", "b",
                delegation_id=expiring["delegation_id"])
    assert refused.value.code == "delegation_expired"
    assert M.delegation_show(repo, expiring["delegation_id"])["delegation"][
        "effective_state"] == "expired"

    revocable = M.delegation_grant(
        repo, "opencode", "a", to="opencode:b", actions=["mailbox_read"],
        mailbox=True, max_uses=5)["delegation"]
    with pytest.raises(M.MeshRefusal) as refused:
        M.delegation_revoke(repo, "opencode", "b",
                            revocable["delegation_id"])
    assert refused.value.code == "not_grantor"
    revoked = M.delegation_revoke(repo, "opencode", "a",
                                  revocable["delegation_id"], reason="done")
    assert revoked["delegation"]["effective_state"] == "revoked"
    assert M.delegation_revoke(repo, "opencode", "a",
                               revocable["delegation_id"])["created"] is False
    with pytest.raises(M.MeshRefusal) as refused:
        M.inbox(repo, "opencode", "b",
                delegation_id=revocable["delegation_id"])
    assert refused.value.code == "delegation_revoked"


def test_mailbox_delegation_requires_the_flag_and_an_active_link(env) -> None:
    repo, _ = env
    _register(repo, "a")
    _register(repo, "b")
    _accept_link(repo, requester="a", target="b", relation="peer")
    with pytest.raises(M.MeshRefusal) as refused:
        M.delegation_grant(repo, "opencode", "a", to="opencode:b",
                           actions=["mailbox_read"])
    assert refused.value.code == "invalid_scope"
    with pytest.raises(M.MeshRefusal) as refused:
        M.delegation_grant(repo, "opencode", "a", to="opencode:b",
                           actions=["lease_release"], mailbox=True,
                           lease_id="lease_" + "0" * 16)
    assert refused.value.code == "invalid_scope"
    grant = M.delegation_grant(
        repo, "opencode", "a", to="opencode:b", actions=["mailbox_read"],
        mailbox=True)["delegation"]
    M.link_revoke(repo, "opencode", "a",
                  grant["link_id"])
    with pytest.raises(M.MeshRefusal) as refused:
        M.inbox(repo, "opencode", "b",
                delegation_id=grant["delegation_id"])
    assert refused.value.code == "delegation_denied"
    assert M.delegation_show(repo, grant["delegation_id"])["delegation"][
        "effective_reason"] == "link_revoked"


def test_merge_on_behalf_is_a_reference_and_never_authorizes(env) -> None:
    repo, _ = env
    _register(repo, "a")
    _register(repo, "b")
    _accept_link(repo, requester="a", target="b", relation="peer")
    lease = M.merge_request(repo, "opencode", "a", target="main")["lease"]
    branch = M.lease_acquire(repo, "opencode", "a", kind="branch",
                             resource_id="side", mode="write")["lease"]
    with pytest.raises(M.MeshRefusal) as refused:
        M.delegation_grant(repo, "opencode", "a", to="opencode:b",
                           actions=["merge_on_behalf"],
                           lease_id=branch["lease_id"])
    assert refused.value.code == "invalid_scope"
    delegation = M.delegation_grant(
        repo, "opencode", "a", to="opencode:b",
        actions=["merge_on_behalf"], lease_id=lease["lease_id"])
    assert delegation["delegation"]["scope"] == (
        "merge_on_behalf:reference")
    queued = M.merge_request(repo, "opencode", "b", target="main")
    assert queued["queued"] is True
    assert queued["acquired"] is False
    with pytest.raises(M.MeshRefusal) as refused:
        M.lease_release(repo, "opencode", "b", lease["lease_id"],
                        delegation_id=delegation["delegation"][
                            "delegation_id"])
    assert refused.value.code == "delegation_denied"
    assert M.lease_status(repo, lease["lease_id"])["lease"][
        "effective_state"] == "active"


def test_state_files_are_private_and_secret_reasons_are_redacted(env) -> None:
    repo, state = env
    _register(repo, "a")
    _register(repo, "b")
    canary = "ghp_" + "c" * 28
    request = M.link_request(repo, "opencode", "a", to="opencode:b",
                             relation="peer", reason=f"token {canary}")
    link_id = request["link"]["link_id"]
    M.link_accept(repo, "opencode", "b", link_id)
    lease = M.lease_acquire(repo, "opencode", "a", kind="branch",
                            resource_id="main", mode="write")["lease"]
    delegation = M.delegation_grant(
        repo, "opencode", "a", to="opencode:b", actions=["lease_release"],
        lease_id=lease["lease_id"], reason=f"secret {canary}")["delegation"]
    for directory in ("links", "delegations"):
        path = state / "mesh" / "default" / directory
        assert stat.S_IMODE(path.stat().st_mode) == 0o700
        for item in path.glob("*.json"):
            assert stat.S_IMODE(item.stat().st_mode) == 0o600
    for path in state.rglob("*.json"):
        assert canary not in path.read_text(encoding="utf-8")
    assert canary not in json.dumps(M.link_show(repo, link_id))
    assert canary not in json.dumps(
        M.delegation_show(repo, delegation["delegation_id"]))
