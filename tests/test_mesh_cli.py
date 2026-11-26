"""The `ozagia mesh` command surface and its exit codes."""
from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from ozagia import cli
from ozagia.core import mesh as M


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


def _json(capsys) -> dict:
    return json.loads(capsys.readouterr().out)


def _register(capsys, session: str = "a", exposure: str = "coordinate") -> dict:
    code = cli.main(["mesh", "register", "--harness", "opencode",
                     "--session", session, "--exposure", exposure, "--json"])
    assert code == 0
    return _json(capsys)


def _snapshot(state: Path) -> dict[str, tuple[int, int, int, int, bytes]]:
    return {
        str(path.relative_to(state)): (
            path.stat().st_size, path.stat().st_mtime_ns,
            path.stat().st_ctime_ns, stat.S_IMODE(path.stat().st_mode),
            path.read_bytes())
        for path in sorted(state.rglob("*")) if path.is_file()
    }


def _link(capsys, requester: str = "a", target: str = "b",
          relation: str = "peer") -> dict:
    assert cli.main(["mesh", "link", "request", "--harness", "opencode",
                     "--session", requester, "--to", f"opencode:{target}",
                     "--relation", relation, "--json"]) == 0
    link_id = _json(capsys)["link"]["link_id"]
    assert cli.main(["mesh", "link", "accept", link_id, "--harness",
                     "opencode", "--session", target, "--json"]) == 0
    return _json(capsys)["link"]


def test_register_status_peers_and_json(env, capsys) -> None:
    repo, state = env
    registered = _register(capsys)
    assert registered["effective_state"] == "live"
    assert registered["exposure"] == "coordinate"
    base = state / "mesh" / "default"
    assert stat.S_IMODE(base.stat().st_mode) == 0o700
    assert stat.S_IMODE((base / "sessions" /
                         f"{registered['key']}.json").stat().st_mode) == 0o600

    assert cli.main(["mesh", "status", "--json"]) == 0
    status = _json(capsys)
    assert status["repo_id"] == M.repo_id(repo)
    assert status["mesh_id"] == "default"
    assert status["session_counts"]["live"] == 1
    assert status["lease_counts"]["active"] == 0

    assert cli.main(["mesh", "peers", "--json"]) == 0
    peers = _json(capsys)
    assert peers["counts"]["live"] == 1
    assert peers["peers"][0]["session"] == "a"


def test_peer_strict_reports_stale(env, capsys) -> None:
    repo, _ = env
    registered = _register(capsys)
    path = M.mesh_dir(repo) / "sessions" / f"{registered['key']}.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    record["heartbeat_at"] = M._iso(M._epoch() - M.STALE_S - 10)
    path.write_text(json.dumps(record), encoding="utf-8")
    assert cli.main(["mesh", "peers", "--strict", "--json"]) == 1
    assert _json(capsys)["counts"]["stale"] == 1
    assert cli.main(["mesh", "peers", "--json"]) == 0


def test_send_inbox_read_and_ack(env, capsys) -> None:
    _register(capsys, "a")
    _register(capsys, "b")
    code = cli.main(["mesh", "send", "--harness", "opencode", "--session",
                     "a", "--to", "opencode:b", "--kind", "note",
                     "--subject", "hello", "--body", "body text", "--json"])
    assert code == 0
    sent = _json(capsys)
    assert sent["delivered"] == 1
    msg_id = sent["message"]["msg_id"]

    assert cli.main(["mesh", "inbox", "--harness", "opencode", "--session",
                     "b", "--json"]) == 0
    box = _json(capsys)
    assert box["count"] == 1 and box["unread"] == 1
    assert "body" not in box["messages"][0]

    assert cli.main(["mesh", "read", msg_id, "--harness", "opencode",
                     "--session", "b", "--json"]) == 0
    read = _json(capsys)
    assert read["body"] == "body text"
    assert read["unread"] == 0
    assert cli.main(["mesh", "ack", msg_id, "--harness", "opencode",
                     "--session", "b", "--json"]) == 0
    assert _json(capsys)["unread"] == 0


def test_send_refuses_unknown_peer_and_requires_exposure(env, capsys) -> None:
    _register(capsys, "a")
    assert cli.main(["mesh", "send", "--harness", "opencode", "--session",
                     "a", "--to", "opencode:nobody", "--kind", "note",
                     "--subject", "x", "--json"]) == 1
    assert _json(capsys)["code"] == "unknown_peer"

    _register(capsys, "a", exposure="none")
    assert cli.main(["mesh", "send", "--harness", "opencode", "--session",
                     "a", "--broadcast", "--kind", "note", "--subject", "x",
                     "--json"]) == 1
    assert _json(capsys)["code"] == "coordinate_required"


def test_missing_identity_and_bad_config_are_exit_two(env, capsys) -> None:
    repo, _ = env
    assert cli.main(["mesh", "send", "--to", "opencode:b", "--kind", "note",
                     "--subject", "x", "--json"]) == 2
    assert _json(capsys)["code"] == "mesh_config"
    assert cli.main(["mesh", "inbox", "--json"]) == 2
    capsys.readouterr()
    (repo / ".ozagia.toml").write_text('[mesh]\nid = "bad id"\n',
                                       encoding="utf-8")
    assert cli.main(["mesh", "status", "--json"]) == 2
    payload = _json(capsys)
    assert payload["code"] == "mesh_config"


def test_lease_acquire_queue_and_release_exit_codes(env, capsys) -> None:
    _register(capsys, "a")
    _register(capsys, "b")
    assert cli.main(["mesh", "lease", "acquire", "--harness", "opencode",
                     "--session", "a", "--kind", "branch", "--id", "main",
                     "--mode", "write", "--json"]) == 0
    first = _json(capsys)["lease"]
    assert cli.main(["mesh", "lease", "acquire", "--harness", "opencode",
                     "--session", "b", "--kind", "branch", "--id", "main",
                     "--mode", "write", "--json"]) == 1
    queued = _json(capsys)
    assert queued["queued"] is True and queued["position"] == 1

    assert cli.main(["mesh", "lease", "list", "--json"]) == 0
    listed = _json(capsys)
    assert listed["counts"]["active"] == 1
    assert listed["counts"]["queued"] == 1

    assert cli.main(["mesh", "lease", "release", first["lease_id"],
                     "--harness", "opencode", "--session", "a",
                     "--json"]) == 0
    released = _json(capsys)
    assert released["promoted"]["holder"]["session"] == "b"


def test_merge_request_is_advisory_and_queues(env, capsys) -> None:
    _register(capsys, "a")
    _register(capsys, "b")
    assert cli.main(["mesh", "merge", "request", "--harness", "opencode",
                     "--session", "a", "--target", "main", "--json"]) == 0
    first = _json(capsys)
    assert first["advisory"] is True
    assert first["lease"]["resource"]["id"] == "merge:main"
    assert cli.main(["mesh", "merge", "request", "--harness", "opencode",
                     "--session", "b", "--target", "main", "--json"]) == 1
    assert _json(capsys)["queued"] is True


def test_json_and_state_never_contain_a_secret_canary(env, capsys) -> None:
    repo, state = env
    _register(capsys, "a")
    _register(capsys, "b")
    canary = "ghp_" + "b" * 28
    assert cli.main(["mesh", "send", "--harness", "opencode", "--session",
                     "a", "--to", "opencode:b", "--kind", "note",
                     "--subject", "canary", "--body", f"value {canary}",
                     "--json"]) == 0
    sent = _json(capsys)
    msg_id = sent["message"]["msg_id"]
    assert canary not in json.dumps(sent)
    assert cli.main(["mesh", "read", msg_id, "--harness", "opencode",
                     "--session", "b", "--json"]) == 0
    output = capsys.readouterr().out
    assert canary not in output
    for path in state.rglob("*.json"):
        assert canary not in path.read_text(encoding="utf-8")
    assert cli.main(["mesh", "inbox", "--harness", "opencode", "--session",
                     "b", "--json"]) == 0
    assert canary not in capsys.readouterr().out


def test_prune_is_a_plan_unless_apply(env, capsys) -> None:
    repo, _ = env
    _register(capsys, "a")
    _register(capsys, "b")
    assert cli.main(["mesh", "send", "--harness", "opencode", "--session",
                     "a", "--to", "opencode:b", "--kind", "note",
                     "--subject", "expired", "--ttl", "1", "--json"]) == 0
    msg = _json(capsys)["message"]
    assert cli.main(["mesh", "read", msg["msg_id"], "--harness", "opencode",
                     "--session", "b", "--json"]) == 0
    capsys.readouterr()
    key = M.session_key(M.repo_id(repo), M.mesh_id(repo), "opencode", "b")
    path = M.mesh_dir(repo) / "mail" / key / f"{msg['msg_id']}.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    record["expires_at"] = M._iso(M._epoch() - 5)
    path.write_text(json.dumps(record), encoding="utf-8")
    assert cli.main(["mesh", "prune", "--expired", "--harness", "opencode",
                     "--session", "b", "--json"]) == 0
    plan = _json(capsys)
    assert plan["dry_run"] is True and len(plan["candidates"]) == 1
    assert path.exists()
    assert cli.main(["mesh", "prune", "--expired", "--apply", "--harness",
                     "opencode", "--session", "b", "--json"]) == 0
    assert _json(capsys)["dry_run"] is False
    assert not path.exists()


def test_unregister_releases_leases_and_keeps_the_record(env, capsys) -> None:
    repo, _ = env
    registered = _register(capsys, "a")
    assert cli.main(["mesh", "lease", "acquire", "--harness", "opencode",
                     "--session", "a", "--kind", "grant", "--id", "cpu",
                     "--mode", "write", "--json"]) == 0
    lease = _json(capsys)["lease"]
    assert cli.main(["mesh", "unregister", "--harness", "opencode",
                     "--session", "a", "--release-leases", "--json"]) == 0
    ended = _json(capsys)
    assert ended["state"] == "ended"
    assert lease["lease_id"] in ended["released_leases"]
    path = M.mesh_dir(repo) / "sessions" / f"{registered['key']}.json"
    assert json.loads(path.read_text(encoding="utf-8"))["state"] == "ended"
    assert M.lease_status(repo, lease["lease_id"])[
        "lease"]["effective_state"] == "released"


def test_worktree_offer_validates_without_identity(env, capsys, monkeypatch) -> None:
    repo, _ = env
    worktree = repo.parent / "wt"
    worktree.mkdir()
    monkeypatch.setattr(M.W, "discover", lambda root: [{
        "path": str(worktree.resolve()), "branch": "main", "head": "abc",
        "detached": False, "bare": False, "locked": None, "prunable": None,
        "is_main": True, "exists": True,
    }])
    monkeypatch.setattr(M.W, "status", lambda path, base="main": {
        "error": None, "branch": "main",
        "dirty": {"staged": 0, "unstaged": 0, "untracked": 0,
                  "conflicted": 0},
    })
    assert cli.main(["mesh", "worktree", "offer", "--branch", "main",
                     "--json"]) == 0
    payload = _json(capsys)
    assert payload["worktree"]["branch"] == "main"


def test_link_cli_request_accept_roles_list_show_and_revoke(env, capsys) -> None:
    repo, _ = env
    _register(capsys, "a")
    _register(capsys, "b")
    assert cli.main(["mesh", "link", "request", "--harness", "opencode",
                     "--session", "a", "--to", "opencode:b", "--relation",
                     "sub", "--reason", "branch owner", "--json"]) == 0
    request = _json(capsys)
    link_id = request["link"]["link_id"]
    assert request["created"] is True
    assert request["link"]["effective_state"] == "pending"
    assert request["link"]["reciprocal_relation"] == "super"

    assert cli.main(["mesh", "link", "accept", link_id, "--harness",
                     "opencode", "--session", "b", "--json"]) == 0
    accepted = _json(capsys)
    assert accepted["link"]["effective_state"] == "active"

    assert cli.main(["mesh", "status", "--json"]) == 0
    status = _json(capsys)
    roles = {item["session"]: item["role"] for item in status["sessions"]}
    assert roles == {"a": "sub", "b": "super"}
    assert status["link_counts"]["active"] == 1

    assert cli.main(["mesh", "link", "list", "--json"]) == 0
    listed = _json(capsys)
    assert listed["counts"] == {"active": 1}
    assert listed["links"][0]["link_id"] == link_id

    assert cli.main(["mesh", "link", "show", link_id, "--json"]) == 0
    shown = _json(capsys)
    assert shown["link"]["history"][-1]["event"] == "accepted"

    assert cli.main(["mesh", "link", "revoke", link_id, "--harness",
                     "opencode", "--session", "b", "--reason", "done",
                     "--json"]) == 0
    revoked = _json(capsys)
    assert revoked["link"]["effective_state"] == "revoked"
    assert revoked["link"]["history"][-1]["event"] == "revoked"
    assert cli.main(["mesh", "status", "--json"]) == 0
    after = _json(capsys)
    assert {item["role"] for item in after["sessions"]} == {"peer"}


def test_link_cli_refusals_are_exit_one_and_reads_need_no_identity(
        env, capsys) -> None:
    _register(capsys, "a")
    _register(capsys, "b")
    assert cli.main(["mesh", "link", "request", "--harness", "opencode",
                     "--session", "a", "--to", "opencode:a", "--relation",
                     "peer", "--json"]) == 1
    assert _json(capsys)["code"] == "self_link"
    assert cli.main(["mesh", "link", "request", "--harness", "opencode",
                     "--session", "a", "--to", "opencode:nobody", "--relation",
                     "peer", "--json"]) == 1
    assert _json(capsys)["code"] == "unknown_peer"
    assert cli.main(["mesh", "link", "accept", "lnk_" + "0" * 16,
                     "--harness", "opencode", "--session", "b",
                     "--json"]) == 1
    assert _json(capsys)["code"] == "unknown_link"
    assert cli.main(["mesh", "link", "show", "lnk_" + "0" * 16,
                     "--json"]) == 1
    assert _json(capsys)["code"] == "unknown_link"
    assert cli.main(["mesh", "link", "list", "--json"]) == 0
    assert _json(capsys)["links"] == []

    assert cli.main(["mesh", "link", "request", "--harness", "opencode",
                     "--session", "a", "--to", "opencode:b", "--relation",
                     "sub", "--json"]) == 0
    link_id = _json(capsys)["link"]["link_id"]
    assert cli.main(["mesh", "link", "request", "--harness", "opencode",
                     "--session", "a", "--to", "opencode:b", "--relation",
                     "super", "--json"]) == 1
    assert _json(capsys)["code"] == "link_conflict"
    assert cli.main(["mesh", "link", "decline", link_id, "--harness",
                     "opencode", "--session", "a", "--json"]) == 1
    assert _json(capsys)["code"] == "not_target"
    assert cli.main(["mesh", "link", "decline", link_id, "--harness",
                     "opencode", "--session", "b", "--json"]) == 0
    assert _json(capsys)["link"]["effective_state"] == "declined"

    # Missing identity and a non-coordinate session are configuration/refusal.
    assert cli.main(["mesh", "link", "request", "--to", "opencode:b",
                     "--relation", "peer", "--json"]) == 2
    assert _json(capsys)["code"] == "mesh_config"
    _register(capsys, "a", exposure="none")
    assert cli.main(["mesh", "link", "request", "--harness", "opencode",
                     "--session", "a", "--to", "opencode:b", "--relation",
                     "peer", "--json"]) == 1
    assert _json(capsys)["code"] == "coordinate_required"


def test_delegation_cli_grant_release_list_show_and_revoke(env, capsys) -> None:
    _register(capsys, "a")
    _register(capsys, "b")
    _link(capsys, "a", "b", "peer")
    assert cli.main(["mesh", "lease", "acquire", "--harness", "opencode",
                     "--session", "a", "--kind", "branch", "--id", "main",
                     "--mode", "write", "--json"]) == 0
    lease_id = _json(capsys)["lease"]["lease_id"]
    assert cli.main(["mesh", "delegation", "grant", "--harness", "opencode",
                     "--session", "a", "--to", "opencode:b", "--action",
                     "lease_release", "--lease", lease_id, "--json"]) == 0
    delegation = _json(capsys)["delegation"]
    delegation_id = delegation["delegation_id"]
    assert delegation["effective_state"] == "active"
    assert delegation["lease_id"] == lease_id
    assert delegation["link_id"]

    assert cli.main(["mesh", "delegation", "list", "--json"]) == 0
    listed = _json(capsys)
    assert listed["counts"] == {"active": 1}
    assert listed["delegations"][0]["delegation_id"] == delegation_id
    assert cli.main(["mesh", "delegation", "show", delegation_id,
                     "--json"]) == 0
    assert _json(capsys)["delegation"]["scope"] == (
        f"lease_release:{lease_id}")

    assert cli.main(["mesh", "lease", "release", lease_id, "--harness",
                     "opencode", "--session", "b", "--delegation",
                     delegation_id, "--json"]) == 0
    released = _json(capsys)
    assert released["lease"]["effective_state"] == "released"
    assert released["delegation_id"] == delegation_id
    assert cli.main(["mesh", "delegation", "show", delegation_id,
                     "--json"]) == 0
    assert _json(capsys)["delegation"]["effective_state"] == "expired"

    assert cli.main(["mesh", "delegation", "revoke", delegation_id,
                     "--harness", "opencode", "--session", "b",
                     "--json"]) == 1
    assert _json(capsys)["code"] == "not_grantor"
    assert cli.main(["mesh", "delegation", "revoke", delegation_id,
                     "--harness", "opencode", "--session", "a",
                     "--json"]) == 0
    assert _json(capsys)["delegation"]["effective_state"] == "revoked"
    assert cli.main(["mesh", "delegation", "revoke", delegation_id,
                     "--harness", "opencode", "--session", "a",
                     "--json"]) == 0
    assert _json(capsys)["created"] is False


def test_delegation_cli_refusals_and_mailbox_read(env, capsys) -> None:
    repo, _ = env
    _register(capsys, "a")
    _register(capsys, "b")
    assert cli.main(["mesh", "send", "--harness", "opencode", "--session",
                     "b", "--to", "opencode:a", "--kind", "note",
                     "--subject", "hello", "--body", "private body",
                     "--json"]) == 0
    message_id = _json(capsys)["message"]["msg_id"]
    assert cli.main(["mesh", "delegation", "grant", "--harness", "opencode",
                     "--session", "a", "--to", "opencode:b", "--action",
                     "mailbox_read", "--mailbox", "--json"]) == 1
    assert _json(capsys)["code"] == "not_linked"
    _link(capsys, "a", "b", "peer")
    assert cli.main(["mesh", "delegation", "grant", "--harness", "opencode",
                     "--session", "a", "--to", "opencode:b", "--action",
                     "mailbox_read", "--json"]) == 1
    assert _json(capsys)["code"] == "invalid_scope"
    assert cli.main(["mesh", "delegation", "grant", "--harness", "opencode",
                     "--session", "a", "--to", "opencode:b", "--action",
                     "mailbox_read", "--mailbox", "--max-uses", "2",
                     "--json"]) == 0
    delegation_id = _json(capsys)["delegation"]["delegation_id"]

    # Role/link alone does not expose A's mailbox.
    assert cli.main(["mesh", "inbox", "--harness", "opencode", "--session",
                     "b", "--json"]) == 0
    assert _json(capsys)["count"] == 0
    assert cli.main(["mesh", "inbox", "--harness", "opencode", "--session",
                     "b", "--delegation", delegation_id, "--json"]) == 0
    mailbox = _json(capsys)
    assert mailbox["count"] == 1
    assert mailbox["mailbox_owner"] == M.session_key(
        M.repo_id(repo), M.mesh_id(repo), "opencode", "a")
    assert cli.main(["mesh", "read", message_id, "--harness", "opencode",
                     "--session", "b", "--delegation", delegation_id,
                     "--json"]) == 0
    assert _json(capsys)["body"] == "private body"
    assert cli.main(["mesh", "read", message_id, "--harness", "opencode",
                     "--session", "b", "--json"]) == 1
    assert _json(capsys)["code"] == "unknown_message"

    assert cli.main(["mesh", "delegation", "grant", "--harness", "opencode",
                     "--session", "a", "--to", "opencode:b", "--action",
                     "lease_release", "--json"]) == 1
    assert _json(capsys)["code"] == "invalid_scope"
    assert cli.main(["mesh", "delegation", "grant", "--harness", "opencode",
                     "--session", "a", "--to", "opencode:b", "--action",
                     "lease_release", "--lease", "lease_" + "0" * 16,
                     "--max-uses", "0", "--json"]) == 1
    assert _json(capsys)["code"] == "invalid_value"


def test_link_and_delegation_cli_reads_do_not_write(env, capsys) -> None:
    repo, state = env
    _register(capsys, "a")
    _register(capsys, "b")
    link = _link(capsys, "a", "b", "peer")
    assert cli.main(["mesh", "lease", "acquire", "--harness", "opencode",
                     "--session", "a", "--kind", "branch", "--id", "main",
                     "--mode", "write", "--json"]) == 0
    lease_id = _json(capsys)["lease"]["lease_id"]
    assert cli.main(["mesh", "delegation", "grant", "--harness", "opencode",
                     "--session", "a", "--to", "opencode:b", "--action",
                     "lease_release", "--lease", lease_id, "--json"]) == 0
    delegation_id = _json(capsys)["delegation"]["delegation_id"]
    before = _snapshot(state)
    assert cli.main(["mesh", "status", "--json"]) == 0
    assert cli.main(["mesh", "link", "list", "--json"]) == 0
    assert cli.main(["mesh", "link", "show", link["link_id"],
                     "--json"]) == 0
    assert cli.main(["mesh", "delegation", "list", "--json"]) == 0
    assert cli.main(["mesh", "delegation", "show", delegation_id,
                     "--json"]) == 0
    assert before == _snapshot(state)


def test_link_and_delegation_cli_files_are_private_and_canary_free(
        env, capsys) -> None:
    repo, state = env
    _register(capsys, "a")
    _register(capsys, "b")
    canary = "ghp_" + "d" * 28
    assert cli.main(["mesh", "link", "request", "--harness", "opencode",
                     "--session", "a", "--to", "opencode:b", "--relation",
                     "peer", "--reason", f"token {canary}", "--json"]) == 0
    link = _json(capsys)["link"]
    assert canary not in json.dumps(link)
    assert cli.main(["mesh", "lease", "acquire", "--harness", "opencode",
                     "--session", "a", "--kind", "branch", "--id", "main",
                     "--mode", "write", "--json"]) == 0
    lease_id = _json(capsys)["lease"]["lease_id"]
    assert cli.main(["mesh", "delegation", "grant", "--harness", "opencode",
                     "--session", "a", "--to", "opencode:b", "--action",
                     "lease_release", "--lease", lease_id, "--reason",
                     f"secret {canary}", "--json"]) == 1
    assert _json(capsys)["code"] == "link_pending"
    assert cli.main(["mesh", "link", "accept", link["link_id"], "--harness",
                     "opencode", "--session", "b", "--json"]) == 0
    _json(capsys)
    assert cli.main(["mesh", "delegation", "grant", "--harness", "opencode",
                     "--session", "a", "--to", "opencode:b", "--action",
                     "lease_release", "--lease", lease_id, "--reason",
                     f"secret {canary}", "--json"]) == 0
    delegation = _json(capsys)["delegation"]
    assert canary not in json.dumps(delegation)
    for directory in ("links", "delegations"):
        path = state / "mesh" / "default" / directory
        assert stat.S_IMODE(path.stat().st_mode) == 0o700
        for item in path.glob("*.json"):
            assert stat.S_IMODE(item.stat().st_mode) == 0o600
    for path in state.rglob("*.json"):
        assert canary not in path.read_text(encoding="utf-8")
