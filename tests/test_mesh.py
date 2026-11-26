"""The filesystem session mesh: scope, sessions, messages and leases."""
from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

from ozagia.config import load_project_config
from ozagia.core import mesh as M


@pytest.fixture()
def env(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    state = tmp_path / "state"
    monkeypatch.setenv("OZAGIA_ROOT", str(repo))
    monkeypatch.setenv("OZAGIA_STATE_DIR", str(state))
    monkeypatch.delenv("OZAGIA_MESH_ID", raising=False)
    monkeypatch.delenv("OZAGIA_MESH", raising=False)
    return repo, state


def _register(root: Path, harness: str = "opencode", session: str = "a",
              *, exposure: str = "coordinate") -> dict:
    return M.register(root, harness, session, pid=os.getpid(),
                      pid_start=M._proc_start(os.getpid()),
                      boot_id=M._boot_id(), exposure=exposure)


def _lease_path(repo: Path, mesh: str, lease_id: str) -> Path:
    return repo.parent / "state" / "mesh" / mesh / "leases" / f"{lease_id}.json"


def _rewrite(path: Path, **changes) -> dict:
    record = json.loads(path.read_text(encoding="utf-8"))
    record.update(changes)
    path.write_text(json.dumps(record), encoding="utf-8")
    return record


def test_mesh_id_repo_id_and_layout(env) -> None:
    repo, state = env
    (repo / ".ozagia.toml").write_text('[mesh]\nid = "team_a"\n',
                                       encoding="utf-8")
    assert M.mesh_id(repo) == "team_a"
    assert load_project_config(repo).root == repo
    first = M.repo_id(repo)
    other = repo.parent / "other"
    other.mkdir()
    assert first.startswith("r_") and len(first) == 18
    assert M.repo_id(repo) == first
    assert M.repo_id(other) != first

    (repo / ".ozagia.toml").write_text('[mesh]\nid = "bad id"\n',
                                       encoding="utf-8")
    with pytest.raises(M.MeshConfigError):
        M.mesh_id(repo)

    (repo / ".ozagia.toml").unlink()
    _register(repo)
    base = state / "mesh" / "default"
    assert (base / "mesh.json").is_file()
    guard = json.loads((base / "mesh.json").read_text(encoding="utf-8"))
    assert guard == {"schema": M.SCHEMA, "repo_id": first,
                     "mesh_id": "default"}
    for name in ("sessions", "leases", "mail", "links", "delegations",
                 "audit", "rate"):
        assert (base / name).is_dir()
        assert stat.S_IMODE((base / name).stat().st_mode) == 0o700


def test_versioned_config_supplies_mesh_id(env) -> None:
    repo, _ = env
    config = repo / "ozagia" / "config.toml"
    config.parent.mkdir()
    config.write_text('[mesh]\nid = "versioned"\n', encoding="utf-8")
    assert M.mesh_id(repo) == "versioned"
    assert load_project_config(repo).root == repo
    config.write_text('[mesh]\nid = "ok"\nextra = 1\n', encoding="utf-8")
    with pytest.raises(M.MeshConfigError):
        M.mesh_id(repo)


def test_shared_state_dir_does_not_merge_repositories(env) -> None:
    repo, state = env
    _register(repo)
    other = repo.parent / "other"
    other.mkdir()
    with pytest.raises(M.MeshScopeError):
        M.peers(other)
    with pytest.raises(M.MeshScopeError):
        _register(other)


def test_register_is_idempotent_and_a_live_pid_conflicts(env) -> None:
    repo, _ = env
    first = _register(repo)
    again = _register(repo)
    assert again["key"] == first["key"]
    assert again["started_at"] == first["started_at"]
    with pytest.raises(M.MeshRefusal) as refused:
        M.register(repo, "opencode", "a", pid=999_999,
                   pid_start="999999", boot_id=M._boot_id(),
                   exposure="coordinate")
    assert refused.value.code == "session_already_registered"

    base = M.mesh_dir(repo)
    record_path = base / "sessions" / f"{first['key']}.json"
    _rewrite(record_path, pid=999_999, pid_start="888888",
             heartbeat_at=M._iso(M._epoch() - M.STALE_S - 10))
    replacement = M.register(repo, "opencode", "a", pid=os.getpid(),
                            pid_start=M._proc_start(os.getpid()),
                            boot_id=M._boot_id(), exposure="coordinate")
    assert replacement["effective_state"] == "live"
    ended = M.unregister(repo, "opencode", "a")
    assert ended["state"] == "ended"
    assert record_path.is_file()


def test_liveness_never_guesses_dead(env) -> None:
    repo, _ = env
    now = M._epoch()
    current = {
        "state": "active", "pid": os.getpid(),
        "pid_start": M._proc_start(os.getpid()), "boot_id": M._boot_id(),
        "heartbeat_at": M._iso(now),
    }
    assert M.session_liveness(current, now=now) == "live"
    assert M.session_liveness(
        {**current, "heartbeat_at": M._iso(now - M.STALE_S - 1)},
        now=now) == "stale"
    # A live PID with a very old heartbeat is contradictory, not dead.
    assert M.session_liveness(
        {**current, "heartbeat_at": M._iso(now - M.DEAD_S - 1)},
        now=now) == "stale"
    dead = {**current, "pid": 2 ** 31 - 1, "pid_start": "1",
            "heartbeat_at": M._iso(now - M.DEAD_S - 1)}
    assert M.session_liveness(dead, now=now) == "dead"
    assert M.session_liveness({**current, "boot_id": "other-boot"},
                              now=now) == "foreign"
    assert M.session_liveness({**current, "state": "ended"},
                              now=now) == "ended"


def test_heartbeat_refreshes_or_rebinds_a_stale_registration(env) -> None:
    repo, _ = env
    record = _register(repo)
    path = M.mesh_dir(repo) / "sessions" / f"{record['key']}.json"
    _rewrite(path, heartbeat_at=M._iso(M._epoch() - M.STALE_S - 5))
    beat = M.heartbeat(repo, "opencode", "a", pid=os.getpid(),
                       pid_start=M._proc_start(os.getpid()))
    assert beat["effective_state"] == "live"
    assert beat["heartbeat_at"] >= record["heartbeat_at"]


def test_messages_are_bounded_redacted_and_metadata_only(env) -> None:
    repo, _ = env
    _register(repo, session="a")
    _register(repo, session="b")
    canary = "ghp_" + "a" * 28
    sent = M.send(repo, "opencode", "a", kind="request",
                  subject="please review", body=f"token {canary}",
                  to="opencode:b")
    assert sent["delivered"] == 1
    msg_id = sent["message"]["msg_id"]
    box = M.inbox(repo, "opencode", "b")
    assert box["count"] == 1
    assert "body" not in box["messages"][0]
    item = box["messages"][0]
    assert item["body_untrusted"] is True
    stored = (M.mesh_dir(repo) / "mail" / item["to"]["key"]
              / f"{msg_id}.json").read_text(encoding="utf-8")
    assert canary not in stored
    assert "<redacted:" in stored
    body = M.read(repo, "opencode", "b", msg_id)["body"]
    assert canary not in body
    assert M.ack(repo, "opencode", "b", msg_id)["unread"] == 0

    with pytest.raises(M.MeshRefusal):
        M.send(repo, "opencode", "a", kind="note", subject="x" * 121,
               to="opencode:b")
    with pytest.raises(M.MeshRefusal):
        M.send(repo, "opencode", "a", kind="note", subject="x",
               body="y" * 1025, to="opencode:b")
    with pytest.raises(M.MeshRefusal):
        M.send(repo, "opencode", "a", kind="note", subject="bad\x00subject",
               to="opencode:b")
    with pytest.raises(M.MeshRefusal):
        M.send(repo, "opencode", "a", kind="note", subject="bad\nsubject",
               to="opencode:b")
    with pytest.raises(M.MeshRefusal):
        M.send(repo, "opencode", "a", kind="note", subject="ref",
               body="secret=abc", to="opencode:b")


def test_mailbox_caps_refuse_rather_than_drop(env, monkeypatch) -> None:
    repo, _ = env
    _register(repo, session="a")
    _register(repo, session="b")
    monkeypatch.setattr(M, "MAILBOX_MAX_UNREAD", 2)
    for index in range(2):
        M.send(repo, "opencode", "a", kind="note", subject=f"m{index}",
               to="opencode:b")
    with pytest.raises(M.MeshRefusal) as refused:
        M.send(repo, "opencode", "a", kind="note", subject="third",
               to="opencode:b")
    assert refused.value.code == "mailbox_full"
    assert M.inbox(repo, "opencode", "b")["count"] == 2


def test_broadcast_reaches_only_live_same_scope_peers(env) -> None:
    repo, _ = env
    _register(repo, session="a")
    _register(repo, session="b")
    _register(repo, session="c")
    M.unregister(repo, "opencode", "c")
    sent = M.send(repo, "opencode", "a", kind="status", subject="hello",
                  broadcast=True)
    assert sent["delivered"] == 1
    assert sent["recipients"] == [M.session_key(
        M.repo_id(repo), M.mesh_id(repo), "opencode", "b")]
    assert M.inbox(repo, "opencode", "c")["count"] == 0
    M.unregister(repo, "opencode", "b")
    assert M.send(repo, "opencode", "a", kind="status", subject="again",
                  broadcast=True)["delivered"] == 0


def test_inbox_read_ack_and_explicit_prune_never_delete_unread(env) -> None:
    repo, _ = env
    _register(repo, session="a")
    _register(repo, session="b")
    first = M.send(repo, "opencode", "a", kind="note", subject="one",
                   body="one body", to="opencode:b", ttl_s=1)["message"]
    second = M.send(repo, "opencode", "a", kind="note", subject="two",
                    body="two body", to="opencode:b", ttl_s=1)["message"]
    peeked = M.read(repo, "opencode", "b", first["msg_id"], peek=True)
    assert peeked["peeked"] is True
    assert M.inbox(repo, "opencode", "b", unread=True)["count"] == 2
    M.read(repo, "opencode", "b", first["msg_id"])
    box = M.inbox(repo, "opencode", "b")
    assert box["unread"] == 1

    base = M.mesh_dir(repo)
    key = M.session_key(M.repo_id(repo), M.mesh_id(repo), "opencode", "b")
    for msg in (first, second):
        path = base / "mail" / key / f"{msg['msg_id']}.json"
        _rewrite(path, expires_at=M._iso(M._epoch() - 10))
    plan = M.prune(repo, expired=True)
    assert [item["msg_id"] for item in plan["candidates"]] == [first["msg_id"]]
    assert any(item["msg_id"] == second["msg_id"] and item["reason"] == "unread"
               for item in plan["kept"])
    applied = M.prune(repo, expired=True, apply=True)
    assert [item["msg_id"] for item in applied["removed"]] == [first["msg_id"]]
    assert not (base / "mail" / key / f"{first['msg_id']}.json").exists()
    assert (base / "mail" / key / f"{second['msg_id']}.json").exists()


def test_lease_acquire_queue_read_coexist_and_promotion(env) -> None:
    repo, _ = env
    _register(repo, session="a")
    _register(repo, session="b")
    _register(repo, session="c")
    held = M.lease_acquire(repo, "opencode", "a", kind="branch",
                           resource_id="main", mode="write")
    assert held["acquired"] is True
    assert held["lease"]["effective_state"] == "active"
    queued = M.lease_acquire(repo, "opencode", "b", kind="branch",
                             resource_id="main", mode="write")
    assert queued["queued"] is True and queued["position"] == 1
    duplicate = M.lease_acquire(repo, "opencode", "b", kind="branch",
                                resource_id="main", mode="write")
    assert duplicate["position"] == 1

    read_a = M.lease_acquire(repo, "opencode", "a", kind="branch",
                             resource_id="dev", mode="read")
    read_c = M.lease_acquire(repo, "opencode", "c", kind="branch",
                             resource_id="dev", mode="read")
    assert read_a["acquired"] and read_c["acquired"]
    read_queued = M.lease_acquire(repo, "opencode", "b", kind="branch",
                                  resource_id="dev", mode="write")
    assert read_queued["queued"] is True

    released = M.lease_release(repo, "opencode", "a",
                               held["lease"]["lease_id"])
    assert released["lease"]["effective_state"] == "released"
    assert released["promoted"]["holder"]["session"] == "b"
    assert _lease_path(repo, "default", held["lease"]["lease_id"]).is_file()
    assert M.lease_list(repo)["counts"]["queued"] == 1


def test_release_skips_a_stale_queued_session(env) -> None:
    repo, _ = env
    _register(repo, session="a")
    _register(repo, session="b")
    _register(repo, session="c")
    held = M.lease_acquire(repo, "opencode", "a", kind="worktree",
                           resource_id=str(repo), mode="write")
    M.lease_acquire(repo, "opencode", "b", kind="worktree",
                    resource_id=str(repo), mode="write")
    M.lease_acquire(repo, "opencode", "c", kind="worktree",
                    resource_id=str(repo), mode="write")
    b = M.mesh_dir(repo) / "sessions" / f"{_session_key(repo, 'b')}.json"
    _rewrite(b, heartbeat_at=M._iso(M._epoch() - M.STALE_S - 5))
    result = M.lease_release(repo, "opencode", "a",
                             held["lease"]["lease_id"])
    assert result["promoted"]["holder"]["session"] == "c"
    assert [item["entry"]["session"] for item in result["skipped"]] == ["b"]


def _session_key(repo: Path, session: str) -> str:
    return M.session_key(M.repo_id(repo), M.mesh_id(repo), "opencode", session)


def test_lease_expiry_takeover_confirmation_and_live_refusal(env) -> None:
    repo, _ = env
    _register(repo, session="a")
    _register(repo, session="b")
    held = M.lease_acquire(repo, "opencode", "a", kind="merge",
                           resource_id="main", mode="merge")
    with pytest.raises(M.MeshRefusal) as live:
        M.lease_takeover(repo, "opencode", "b",
                         held["lease"]["lease_id"], confirm=True,
                         reason="too early")
    assert live.value.code == "live_holder"

    path = _lease_path(repo, "default", held["lease"]["lease_id"])
    _rewrite(path, expires_at=M._iso(M._epoch() - 10))
    assert M.lease_status(repo, held["lease"]["lease_id"])[
        "lease"]["effective_state"] == "expired"
    with pytest.raises(M.MeshRefusal):
        M.lease_takeover(repo, "opencode", "b",
                         held["lease"]["lease_id"], confirm=False,
                         reason="crashed")
    taken = M.lease_takeover(repo, "opencode", "b",
                             held["lease"]["lease_id"], confirm=True,
                             reason="holder expired")
    lease = taken["lease"]
    assert lease["holder"]["session"] == "b"
    assert lease["took_over"]["holder"]["session"] == "a"
    assert lease["took_over"]["reason"] == "holder expired"
    assert path.is_file()


def test_merge_is_exclusive_and_never_runs_git(env, monkeypatch) -> None:
    repo, _ = env
    _register(repo, session="a")
    _register(repo, session="b")
    commands: list[list[str]] = []
    original = subprocess.run

    def recording(*args, **kwargs):
        if args and isinstance(args[0], list):
            commands.append([str(item) for item in args[0]])
        return original(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", recording)
    first = M.merge_request(repo, "opencode", "a", target="refs/heads/main")
    assert first["acquired"] is True
    assert first["advisory"] is True
    assert first["lease"]["resource"] == {
        "kind": "merge", "id": "merge:main", "mode": "merge"}
    second = M.merge_request(repo, "opencode", "b", target="main")
    assert second["queued"] is True and second["position"] == 1
    assert all("merge" not in command for command in commands)


def test_worktree_request_uses_discovery_and_refuses_missing(env, monkeypatch) -> None:
    repo, _ = env
    _register(repo, session="a")
    worktree = repo.parent / "wt"
    worktree.mkdir()
    monkeypatch.setattr(M.W, "discover", lambda root: [{
        "path": str(worktree.resolve()), "branch": "main", "head": "abc",
        "detached": False, "bare": False, "locked": None, "prunable": None,
        "is_main": False, "exists": True,
    }])
    request = M.worktree_request(repo, "opencode", "a", branch="main")
    assert request["acquired"] is True
    assert request["worktree"]["path"] == str(worktree.resolve())
    assert request["lease"]["resource"]["kind"] == "worktree"
    assert request["branch_lease"]["resource"]["kind"] == "branch"
    with pytest.raises(M.MeshRefusal) as missing:
        M.worktree_request(repo, "opencode", "a", branch="nope")
    assert missing.value.code == "worktree_missing"


def test_state_files_are_private_and_symlinked_state_is_refused(env, tmp_path) -> None:
    repo, state = env
    record = _register(repo)
    base = state / "mesh" / "default"
    assert stat.S_IMODE(base.stat().st_mode) == 0o700
    path = base / "sessions" / f"{record['key']}.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE((base / "mesh.json").stat().st_mode) == 0o600


def test_symlinked_state_through_environment_is_refused(env, tmp_path,
                                                        monkeypatch) -> None:
    repo, _ = env
    real = tmp_path / "real-state"
    real.mkdir()
    link = tmp_path / "linked-state"
    link.symlink_to(real, target_is_directory=True)
    monkeypatch.setenv("OZAGIA_STATE_DIR", str(link))
    with pytest.raises(M.MeshScopeError):
        _register(repo)


def test_lease_lock_times_out(tmp_path) -> None:
    path = tmp_path / "lock" / ".lock"
    with M._Lock(path, timeout=0.2):
        with pytest.raises(M.MeshLockError):
            with M._Lock(path, timeout=0.05):
                pass


def test_unregister_removes_own_queued_requests(env) -> None:
    repo, _ = env
    _register(repo, session="a")
    _register(repo, session="b")
    held = M.lease_acquire(repo, "opencode", "a", kind="grant",
                           resource_id="cpu", mode="write")
    M.lease_acquire(repo, "opencode", "b", kind="grant",
                    resource_id="cpu", mode="write")
    assert M.lease_list(repo)["counts"]["queued"] == 1
    M.unregister(repo, "opencode", "b", release_leases=True)
    assert M.lease_list(repo)["counts"]["queued"] == 0
    released = M.lease_release(repo, "opencode", "a",
                               held["lease"]["lease_id"])
    assert released["promoted"] is None


def test_lease_queue_is_bounded(env, monkeypatch) -> None:
    repo, _ = env
    _register(repo, session="a")
    held = M.lease_acquire(repo, "opencode", "a", kind="grant",
                           resource_id="cpu", mode="write")
    monkeypatch.setattr(M, "LEASE_QUEUE_MAX", 1)
    _register(repo, session="b")
    _register(repo, session="c")
    M.lease_acquire(repo, "opencode", "b", kind="grant",
                    resource_id="cpu", mode="write")
    with pytest.raises(M.MeshRefusal) as full:
        M.lease_acquire(repo, "opencode", "c", kind="grant",
                        resource_id="cpu", mode="write")
    assert full.value.code == "queue_full"
    assert held["lease"]["lease_id"]


def test_mesh_opt_in_defaults_off_and_validates_config(env, monkeypatch) -> None:
    repo, state = env
    assert M.mesh_enabled(repo) is False
    assert M.mesh_configured(repo) is False
    assert not (state / "mesh").exists()

    (repo / ".ozagia.toml").write_text(
        '[mesh]\nid = "team"\nenabled = true\n', encoding="utf-8")
    assert M.mesh_enabled(repo) is True
    assert M.mesh_configured(repo) is True
    assert M.mesh_id(repo) == "team"
    assert not (state / "mesh").exists()

    monkeypatch.setenv("OZAGIA_MESH", "0")
    assert M.mesh_enabled(repo) is False
    monkeypatch.setenv("OZAGIA_MESH", "1")
    assert M.mesh_enabled(repo) is True
    monkeypatch.setenv("OZAGIA_MESH", "maybe")
    with pytest.raises(M.MeshConfigError):
        M.mesh_enabled(repo)
    monkeypatch.delenv("OZAGIA_MESH")

    (repo / ".ozagia.toml").write_text("[mesh]\nenabled = 'yes'\n",
                                       encoding="utf-8")
    with pytest.raises(M.MeshConfigError):
        M.mesh_enabled(repo)
    (repo / ".ozagia.toml").write_text("[mesh]\nextra = 1\n",
                                       encoding="utf-8")
    with pytest.raises(M.MeshConfigError):
        M.mesh_configured(repo)
    assert not (state / "mesh").exists()
