"""Reference awareness and the asset GC policy (Stage D)."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from ozagia import cli
from ozagia.core import assets as A


@pytest.fixture()
def env(tmp_path: Path, monkeypatch) -> tuple[Path, Path, Path, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    state = tmp_path / "state"
    data = tmp_path / "xdg"
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("OZAGIA_ROOT", str(repo))
    monkeypatch.setenv("OZAGIA_STATE_DIR", str(state))
    monkeypatch.setenv("XDG_DATA_HOME", str(data))
    monkeypatch.setenv("HOME", str(home))
    return tmp_path, repo, state, home


def _write(path: Path, size: int = 20) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)


def _json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _owned(repo: Path, base: Path, name: str = "cache", **kwargs
           ) -> dict:
    target = base / name
    target.mkdir(parents=True)
    _write(target / "blob.bin")
    return A.register(repo, target, kind="cache", owner="ozagia",
                      deletable=True, **kwargs)


@pytest.mark.parametrize("kind", ["path", "id", "revision", "digest"])
def test_state_reference_by_path_id_revision_and_digest_blocks_gc(
        env, kind: str) -> None:
    base, repo, state, _home = env
    asset = _owned(repo, base)
    value: object
    if kind == "path":
        value = asset["path"]
    elif kind == "id":
        value = asset["id"]
    elif kind == "revision":
        asset = A.register(repo, asset["path"], kind="cache", owner="ozagia",
                           deletable=True, revision="r1")
        value = "r1"
    else:
        target = Path(asset["path"]) / "blob.bin"
        digest = _sha(target)
        asset = A.register(repo, asset["path"], kind="cache", owner="ozagia",
                           deletable=True,
                           digests=[{"path": "blob.bin", "sha256": digest}])
        asset = A.verify(repo, asset["id"], hash_files=True)
        value = digest

    _json(state / "goals" / "goals.json",
          {"version": 1, "goals": [{"id": "g1", "targets": [value]}]})

    result = A.referenced_by(repo, asset["id"])
    assert result["referenced"] is True
    assert result["reference_status"] == "referenced"
    assert result["referenced_by"]
    assert result["scan"]["state"] == "complete"

    plan = A.gc_plan(repo)
    assert plan["candidates"] == []
    kept = {row["id"]: row for row in plan["kept"]}
    assert kept[asset["id"]]["reason"] == "referenced"
    assert "referenced" in kept[asset["id"]]["why"]


def test_unrelated_state_record_is_not_a_reference(env) -> None:
    base, repo, state, _home = env
    asset = _owned(repo, base)
    _json(state / "goals" / "goals.json",
          {"version": 1,
           "goals": [{"id": "g1", "targets": ["sym:src/app.py::main"]}]})

    result = A.referenced_by(repo, asset)
    assert result["referenced"] is False
    assert result["reference_status"] == "unreferenced"
    assert result["referenced_by"] == []

    listed = A.list_assets(repo)[0]
    assert listed["referenced"] is False
    assert listed["reference_scan"] == "complete"
    status = A.status(repo)
    assert status["totals"]["referenced"] == 0
    assert status["references"]["state"] == "complete"
    assert [row["id"] for row in A.gc_plan(repo)["candidates"]] == [
        asset["id"]]


def test_partial_scan_keeps_the_asset_and_reports_partial(env,
                                                          monkeypatch) -> None:
    base, repo, state, _home = env
    asset = _owned(repo, base)
    _json(state / "goals" / "goals.json",
          {"goals": [{"targets": [asset["path"]]}]})
    monkeypatch.setattr(A, "MAX_REFERENCE_FILES", 0)

    result = A.referenced_by(repo, asset["id"])
    assert result["referenced"] is False
    assert result["reference_status"] == "partial"
    assert result["reference_scan"] == "partial"
    assert result["scan"]["state"] == "partial"
    assert result["scan"]["notes"]

    plan = A.gc_plan(repo)
    assert plan["totals"]["reference_partial"] is True
    assert plan["candidates"] == []
    kept = {row["id"]: row for row in plan["kept"]}
    assert kept[asset["id"]]["reason"] == "reference-scan"


def test_every_gc_block_reason_is_reported(env) -> None:
    base, repo, _state, _home = env
    candidate = _owned(repo, base, "candidate")
    pinned = _owned(repo, base, "pinned", pinned=True)
    keep = _owned(repo, base, "keep")
    # Re-register without --deletable; the default is not deletable.
    keep = A.register(repo, keep["path"], kind="cache", owner="ozagia")
    external_path = base / "external"
    external_path.mkdir()
    _write(external_path / "blob.bin")
    external = A.register(repo, external_path, kind="cache",
                          owner="external")

    unverified = base / "unverified.bin"
    _write(unverified)
    unverified_asset = A.register(
        repo, unverified, kind="cache", owner="ozagia", deletable=True,
        sha256=_sha(unverified))

    mismatch = base / "mismatch.bin"
    _write(mismatch)
    mismatch_asset = A.register(
        repo, mismatch, kind="cache", owner="ozagia", deletable=True,
        sha256=_sha(mismatch))
    A.verify(repo, mismatch_asset["id"], hash_files=True)
    mismatch.write_bytes(b"changed")
    mismatched = A.verify(repo, mismatch_asset["id"], hash_files=True)
    assert mismatched["state"] == "mismatch"

    missing = _owned(repo, base, "missing")
    Path(missing["path"]).rename(base / "moved")

    plan = A.gc_plan(repo)
    assert [row["id"] for row in plan["candidates"]] == [candidate["id"]]
    assert plan["candidates"][0]["reason"] == "apply-deferred"

    kept = {row["id"]: row for row in plan["kept"]}
    assert kept[pinned["id"]]["reason"] == "pinned"
    assert kept[keep["id"]]["reason"] == "not-deletable"
    assert kept[external["id"]]["reason"] == "owner"
    assert kept[unverified_asset["id"]]["reason"] == "state"
    assert kept[mismatch_asset["id"]]["reason"] == "state"
    assert kept[missing["id"]]["reason"] == "state"
    assert all("why" in row for row in plan["kept"])


def test_gc_apply_is_still_refused_with_the_future_gate(env) -> None:
    _base, repo, _state, _home = env
    with pytest.raises(A.AssetGcRefused) as error:
        A.gc(repo, apply=True)
    message = str(error.value)
    for part in ("dry-run", "ownership", "deletable", "unpinned", "verified",
                 "unreferenced", "explicit approval"):
        assert part in message
    assert not (Path(repo) / ".ozagia").exists()


def test_policy_and_status_shapes_do_not_leak_state_bodies(env, capsys) -> None:
    base, repo, state, _home = env
    _owned(repo, base)
    _json(state / "goals" / "goals.json",
          {"goals": [{"targets": ["TOP-SECRET-REPORT-BODY"]}]})

    policy = A.policy()
    assert policy["schema"] == "ozagia.asset.policy/1"
    assert policy["gc"]["apply_allowed"] is False
    assert policy["gc"]["apply_gate"]
    for owner in ("ocelot", "huggingface", "user", "upstream", "external"):
        assert policy["owners"][owner]["gc_candidate"] is False
        assert policy["owners"][owner]["deletion"] == "never"
        assert policy["owners"][owner]["read_only"] is True
    assert policy["owners"]["ozagia"]["gc_candidate"] is True

    status = A.status(repo)
    payload = json.dumps(status)
    assert "TOP-SECRET-REPORT-BODY" not in payload
    assert status["references"]["schema"] == A.REFERENCE_SCHEMA
    assert "referenced" in A.list_assets(repo)[0]

    assert cli.main(["assets", "policy", "--json"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["ok"] is True
    assert printed["owners"]["ozagia"]["gc_candidate"] is True

    assert cli.main(["assets", "gc", "--apply", "--json"]) == 1
    refused = json.loads(capsys.readouterr().out)
    assert refused["ok"] is False
    assert "unreferenced" in refused["error"]


def test_home_and_target_repository_are_not_scanned(env) -> None:
    base, repo, _state, home = env
    asset = _owned(repo, base)
    _json(home / "goals" / "goals.json",
          {"goals": [{"targets": [asset["path"], asset["id"]]}]})
    _json(repo / "notes.json",
          {"notes": [asset["path"], asset["id"]]})
    _json(repo / "goals" / "goals.json",
          {"goals": [{"targets": [asset["path"], asset["id"]]}]})

    result = A.referenced_by(repo, asset["id"])
    assert result["referenced"] is False
    assert result["reference_status"] == "unreferenced"
    assert result["scan"]["files"] == 0
