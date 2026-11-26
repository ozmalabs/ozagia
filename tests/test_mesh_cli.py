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
