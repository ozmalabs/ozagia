"""Durable external assets (`ozagia.core.assets`)."""
from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

import pytest

from ozagia.core import assets as A


@pytest.fixture()
def env(tmp_path: Path, monkeypatch) -> tuple[Path, Path, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    state = tmp_path / "state"
    data = tmp_path / "xdg"
    monkeypatch.setenv("OZAGIA_ROOT", str(repo))
    monkeypatch.setenv("OZAGIA_STATE_DIR", str(state))
    monkeypatch.setenv("XDG_DATA_HOME", str(data))
    return tmp_path, repo, state


def _write(path: Path, size: int = 100, *, content: bytes = b"x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content * size)


def _record(root: Path, asset_id: str) -> dict:
    path = A.registry_dir(root) / f"{asset_id}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _rewrite(root: Path, asset_id: str, **changes) -> None:
    record = _record(root, asset_id)
    record.update(changes)
    (A.registry_dir(root) / f"{asset_id}.json").write_text(
        json.dumps(record), encoding="utf-8")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_register_list_status_verify_pin_unpin_forget(env) -> None:
    base, repo, _state = env
    dataset = base / "data" / "wikipedia"
    dataset.mkdir(parents=True)
    _write(dataset / "part.parquet", 900)

    asset = A.register(
        repo, dataset, kind="dataset", owner="ozagia", scope="user",
        source={"kind": "url", "url": "https://example.test/wiki.parquet"},
        version="2026-11", revision="r1", license="CC-BY-4.0",
        pinned=False, deletable=True, reason="wikipedia fixture")

    assert asset["schema"] == A.SCHEMA
    assert asset["id"].startswith("ast_")
    assert asset["path"] == str(dataset.resolve())
    assert asset["kind"] == "dataset"
    assert asset["owner"] == "ozagia"
    assert asset["state"] == "present"
    assert asset["bytes"] > 0
    assert asset["apparent_bytes"] >= 900
    assert asset["inodes"] >= 1
    assert asset["allocation"] in ("blocks", "apparent-fallback")
    assert asset["walk"] == "complete"
    assert asset["license"] == {
        "spdx": "CC-BY-4.0", "status": "declared", "source": "caller"}
    assert asset["source"]["kind"] == "url"
    assert asset["source"]["url"] == "https://example.test/wiki.parquet"
    assert asset["source"]["revision"] == "r1"

    registry = A.registry_dir(repo)
    assert registry.name == "registry"
    assert stat.S_IMODE(registry.stat().st_mode) == 0o700
    record_path = registry / f"{asset['id']}.json"
    assert stat.S_IMODE(record_path.stat().st_mode) == 0o600
    record = _record(repo, asset["id"])
    assert record["schema"] == A.SCHEMA

    listed = A.list_assets(repo)
    assert [row["id"] for row in listed] == [asset["id"]]
    assert A.list_assets(repo, owner="ozagia", kind="dataset",
                         state="present")[0]["id"] == asset["id"]
    assert A.list_assets(repo, pinned=True) == []

    result = A.status(repo, top=1)
    assert result["totals"]["entries"] == 1
    assert result["totals"]["bytes"] == asset["bytes"]
    assert result["by_kind"]["dataset"]["entries"] == 1
    assert result["by_owner"]["ozagia"]["entries"] == 1
    assert result["by_state"]["present"]["entries"] == 1
    assert result["top"][0]["id"] == asset["id"]

    verified = A.verify(repo, asset["id"])
    assert verified["state"] == "present"
    assert "measurement complete" in verified["reason"]

    pinned = A.pin(repo, asset["id"])
    assert pinned["pinned"] is True
    assert A.list_assets(repo, pinned=True)[0]["id"] == asset["id"]
    assert A.unpin(repo, asset["id"])["pinned"] is False

    forgotten = A.forget(repo, asset["id"])
    assert forgotten["id"] == asset["id"]
    assert not record_path.exists()
    assert dataset.is_dir()
    assert A.list_assets(repo) == []
    with pytest.raises(A.AssetNotFound):
        A.get(repo, asset["id"])


def test_missing_path_is_missing_not_zero_silently(env) -> None:
    base, repo, _state = env
    target = base / "gone"
    target.mkdir()
    _write(target / "blob.bin", 50)
    asset = A.register(repo, target, kind="model", owner="ozagia")

    target.rename(base / "elsewhere")
    result = A.verify(repo, asset["id"])
    assert result["state"] == "missing"
    assert result["bytes"] == 0
    assert result["apparent_bytes"] == 0
    assert "does not exist" in result["reason"]
    assert A.list_assets(repo)[0]["state"] == "missing"
    assert A.status(repo)["totals"]["missing"] == 1


def test_hash_verify_file_and_mismatch(env) -> None:
    base, repo, _state = env
    model = base / "models" / "weights.bin"
    _write(model, 128)
    digest = _sha(model)
    asset = A.register(repo, model, kind="model", owner="huggingface",
                       sha256=digest)

    assert A.verify(repo, asset["id"])["state"] == "unverified"
    verified = A.verify(repo, asset["id"], hash_files=True)
    assert verified["state"] == "verified"
    assert verified["sha256"] == digest
    assert verified["verified_at"] is not None

    model.write_bytes(b"tampered")
    mismatch = A.verify(repo, asset["id"], hash_files=True)
    assert mismatch["state"] == "mismatch"
    assert "sha256 mismatch" in mismatch["reason"]


def test_digest_verify_for_a_directory(env) -> None:
    base, repo, _state = env
    corpus = base / "corpus"
    corpus.mkdir()
    part = corpus / "a.txt"
    part.write_text("alpha", encoding="utf-8")
    asset = A.register(
        repo, corpus, kind="corpus", owner="ozagia",
        digests=[{"path": "a.txt", "sha256": _sha(part)}])
    assert A.verify(repo, asset["id"])["state"] == "unverified"

    verified = A.verify(repo, asset["id"], hash_files=True)
    assert verified["state"] == "verified"
    assert verified["digests"][0]["verified"] is True

    part.write_text("beta", encoding="utf-8")
    assert A.verify(repo, asset["id"], hash_files=True)["state"] == "mismatch"


def test_gc_candidates_are_only_owned_deletable_unpinned_present(env) -> None:
    base, repo, _state = env
    created = {}
    for owner in A.OWNERS:
        target = base / f"{owner}-cache"
        target.mkdir()
        _write(target / "blob.bin", 20)
        created[owner] = A.register(
            repo, target, kind="cache", owner=owner, deletable=True)
    non_deletable = base / "ozagia-keep"
    non_deletable.mkdir()
    _write(non_deletable / "blob.bin", 20)
    keep = A.register(repo, non_deletable, kind="cache", owner="ozagia")
    pinned = base / "ozagia-pinned"
    pinned.mkdir()
    _write(pinned / "blob.bin", 20)
    pin = A.register(repo, pinned, kind="cache", owner="ozagia",
                     deletable=True, pinned=True)

    plan = A.gc_plan(repo)
    candidate_ids = {row["id"] for row in plan["candidates"]}
    assert created["ozagia"]["id"] in candidate_ids
    assert keep["id"] not in candidate_ids
    assert pin["id"] not in candidate_ids
    for owner in ("ocelot", "huggingface", "user", "upstream", "external"):
        assert created[owner]["id"] not in candidate_ids
    kept = {row["id"]: row["why"] for row in plan["kept"]}
    assert "pinned" in kept[pin["id"]]
    assert "not deletable" in kept[keep["id"]]
    assert "owner is ocelot" in kept[created["ocelot"]["id"]]
    assert plan["dry_run"] is True
    assert plan["apply"] is False


def test_gc_orders_cache_before_data_and_oldest_first(env) -> None:
    base, repo, _state = env
    entries = {}
    for name, kind in (("cache", "cache"), ("dataset", "dataset"),
                       ("model", "model"), ("corpus", "corpus")):
        target = base / name
        target.mkdir()
        _write(target / "blob.bin", 10)
        entries[name] = A.register(repo, target, kind=kind, owner="ozagia",
                                   deletable=True)
    _rewrite(repo, entries["cache"]["id"], last_used_at=4000)
    _rewrite(repo, entries["dataset"]["id"], last_used_at=1000)
    _rewrite(repo, entries["model"]["id"], last_used_at=2000)
    _rewrite(repo, entries["corpus"]["id"], last_used_at=500)

    plan = A.gc_plan(repo)
    assert [row["id"] for row in plan["candidates"]] == [
        entries["cache"]["id"],
        entries["dataset"]["id"],
        entries["model"]["id"],
        entries["corpus"]["id"],
    ]


def test_gc_budget_stops_at_the_first_over_budget_candidate(env) -> None:
    base, repo, _state = env
    cache = base / "cache"
    cache.mkdir()
    _write(cache / "blob.bin", 100)
    dataset = base / "dataset"
    dataset.mkdir()
    _write(dataset / "blob.bin", 200)
    first = A.register(repo, cache, kind="cache", owner="ozagia",
                       deletable=True)
    second = A.register(repo, dataset, kind="dataset", owner="ozagia",
                        deletable=True)
    _rewrite(repo, first["id"], last_used_at=1000)
    _rewrite(repo, second["id"], last_used_at=2000)

    plan = A.gc_plan(repo, max_bytes=first["bytes"])
    assert [row["id"] for row in plan["candidates"]] == [first["id"]]
    kept = {row["id"]: row["why"] for row in plan["kept"]}
    assert "budget" in kept[second["id"]]

    plan = A.gc_plan(repo, max_inodes=first["inodes"])
    assert [row["id"] for row in plan["candidates"]] == [first["id"]]


def test_gc_apply_is_refused(env) -> None:
    _base, repo, _state = env
    with pytest.raises(A.AssetGcRefused):
        A.gc(repo, apply=True)


def test_marker_only_for_owned_assets_and_explicit_mark(env) -> None:
    base, repo, _state = env
    owned = base / "owned"
    owned.mkdir()
    _write(owned / "blob.bin", 10)
    asset = A.register(repo, owned, kind="cache", owner="ozagia",
                       mark=True, reason="owned")
    marker = owned / A.MARKER_NAME
    assert marker.is_file()
    assert stat.S_IMODE(marker.stat().st_mode) == 0o600
    assert asset["marker"] == str(marker)
    payload = json.loads(marker.read_text(encoding="utf-8"))
    assert payload["schema"] == A.SCHEMA
    assert payload["id"] == asset["id"]
    assert "blob.bin" not in marker.read_text(encoding="utf-8")

    unmarked = base / "unmarked"
    unmarked.mkdir()
    A.register(repo, unmarked, kind="cache", owner="ozagia")
    assert not (unmarked / A.MARKER_NAME).exists()

    external = base / "external"
    external.mkdir()
    with pytest.raises(A.AssetMarkerError):
        A.register(repo, external, kind="cache", owner="external", mark=True)
    assert not (external / A.MARKER_NAME).exists()

    file_asset = base / "single.bin"
    _write(file_asset, 5)
    with pytest.raises(A.AssetMarkerError):
        A.register(repo, file_asset, kind="cache", owner="ozagia", mark=True)


def test_path_validation(env) -> None:
    base, repo, state = env
    with pytest.raises(A.AssetPathError):
        A.register(repo, "", kind="other", owner="ozagia")
    with pytest.raises(A.AssetPathError):
        A.register(repo, "/", kind="other", owner="ozagia")
    with pytest.raises(A.AssetPathError):
        A.register(repo, repo, kind="other", owner="ozagia")

    target = base / "real"
    target.mkdir()
    link = base / "link"
    link.symlink_to(target)
    with pytest.raises(A.AssetPathError):
        A.register(repo, link, kind="other", owner="ozagia")

    temp_path = state / "tmp" / "scratch"
    temp_path.mkdir(parents=True)
    with pytest.raises(A.AssetPathError):
        A.register(repo, temp_path, kind="other", owner="ozagia")

    home = base / "home"
    home.mkdir()
    monkeypatch_value = str(home)
    old_home = os.environ.get("HOME")
    os.environ["HOME"] = monkeypatch_value
    try:
        with pytest.raises(A.AssetPathError):
            A.register(repo, home, kind="other", owner="ozagia")
    finally:
        if old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = old_home


def test_canonical_relative_path(env, monkeypatch) -> None:
    base, repo, _state = env
    target = base / "relative-data"
    target.mkdir()
    monkeypatch.chdir(base)
    asset = A.register(repo, "relative-data/..", kind="other", owner="ozagia")
    assert asset["path"] == str(base.resolve())
    assert asset["state"] == "present"


def test_registry_falls_back_when_data_home_is_not_a_directory(env,
                                                              monkeypatch) -> None:
    base, repo, state = env
    not_a_dir = base / "not-a-directory"
    not_a_dir.write_text("x", encoding="utf-8")
    monkeypatch.setenv("XDG_DATA_HOME", str(not_a_dir))
    target = base / "fallback"
    target.mkdir()
    _write(target / "blob.bin", 5)
    A.register(repo, target, kind="cache", owner="ozagia")
    assert A.registry_dir(repo) == state / "assets" / "registry"
    assert (state / "assets" / "registry").is_dir()


def test_relative_xdg_data_home_uses_the_home_default(env, monkeypatch) -> None:
    base, repo, _state = env
    home = base / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_DATA_HOME", "relative/path")
    assert A.registry_dir(repo) == (
        home / ".local" / "share" / "ozagia" / "assets" / "registry")


def test_scan_only_explicit_paths_and_markers(env, monkeypatch) -> None:
    base, repo, _state = env
    marked = base / "marked"
    marked.mkdir()
    _write(marked / "blob.bin", 10)
    asset = A.register(repo, marked, kind="cache", owner="ozagia",
                       mark=True, reason="scan me")
    A.forget(repo, asset["id"])

    empty = base / "empty"
    empty.mkdir()
    nested = base / "parent"
    (nested / "child").mkdir(parents=True)
    (nested / "child" / A.MARKER_NAME).write_text(
        json.dumps({"schema": A.SCHEMA, "id": "ast_bad", "kind": "cache",
                    "owner": "ozagia", "path": str(nested / "child")}),
        encoding="utf-8")

    result = A.scan(repo, paths=[marked, empty, nested])
    assert result["found"] == 1
    assert [row["id"] for row in result["registered"]] == [asset["id"]]
    reasons = {row["path"]: row["reason"] for row in result["skipped"]}
    assert "no .ozagia-asset.json" in reasons[str(empty.resolve())]
    assert "no .ozagia-asset.json" in reasons[str(nested.resolve())]

    assert A.scan(repo)["found"] == 0
    assert A.list_assets(repo)[0]["id"] == asset["id"]


def test_scan_refuses_home_and_external_markers(env) -> None:
    base, repo, _state = env
    home = base / "home"
    home.mkdir()
    old_home = os.environ.get("HOME")
    os.environ["HOME"] = str(home)
    try:
        result = A.scan(repo, paths=[home])
        assert result["found"] == 0
        assert "home" in result["skipped"][0]["reason"]
    finally:
        if old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = old_home

    external = base / "external"
    external.mkdir()
    (external / A.MARKER_NAME).write_text(
        json.dumps({"schema": A.SCHEMA, "id": "ast_ext",
                    "kind": "dataset", "owner": "huggingface",
                    "path": str(external)}), encoding="utf-8")
    result = A.scan(repo, paths=[external])
    assert result["found"] == 1
    assert result["registered"] == []
    assert result["errors"]
    assert A.list_assets(repo) == []


def test_registry_metadata_never_contains_file_contents(env) -> None:
    base, repo, _state = env
    secret = base / "secret.txt"
    secret.write_text("TOP-SECRET-CONTENT", encoding="utf-8")
    asset = A.register(repo, secret, kind="other", owner="ozagia",
                       reason="label only")
    record_text = (A.registry_dir(repo) / f"{asset['id']}.json").read_text(
        encoding="utf-8")
    assert "TOP-SECRET-CONTENT" not in record_text
    record = json.loads(record_text)
    assert set(record) <= {
        "schema", "id", "path", "kind", "owner", "scope", "source", "version",
        "revision", "sha256", "digests", "bytes", "apparent_bytes", "inodes",
        "hardlinks", "allocation", "walk", "cross_device", "license",
        "created_at", "verified_at", "last_used_at", "pinned", "deletable",
        "state", "reason", "manifest_path", "marker",
    }


def test_corrupt_record_never_breaks_list_or_status(env) -> None:
    base, repo, _state = env
    target = base / "good"
    target.mkdir()
    _write(target / "blob.bin", 10)
    asset = A.register(repo, target, kind="cache", owner="ozagia")
    registry = A.registry_dir(repo)
    (registry / "ast_broken.json").write_text("{not json", encoding="utf-8")
    (registry / "ast_wrong.json").write_text(
        json.dumps({"schema": A.SCHEMA, "id": "ast_other", "path": "/x",
                    "kind": "cache", "owner": "ozagia", "scope": "user",
                    "state": "present"}), encoding="utf-8")

    assert [row["id"] for row in A.list_assets(repo)] == [asset["id"]]
    status = A.status(repo)
    assert status["totals"]["entries"] == 1
    assert len(status["registry_errors"]) == 2
    assert A.gc_plan(repo)["totals"]["entries"] == 1


def test_declared_hash_blocks_gc_until_verified(env) -> None:
    base, repo, _state = env
    model = base / "model.bin"
    _write(model, 32)
    asset = A.register(repo, model, kind="cache", owner="ozagia",
                       deletable=True, sha256=_sha(model))
    assert asset["state"] == "unverified"
    assert A.gc_plan(repo)["candidates"] == []
    assert A.verify(repo, asset["id"], hash_files=True)["state"] == "verified"
    assert [row["id"] for row in A.gc_plan(repo)["candidates"]] == [
        asset["id"]]


def test_partial_walk_is_reported_and_not_a_gc_candidate(env, monkeypatch) -> None:
    base, repo, _state = env
    target = base / "partial"
    target.mkdir()
    _write(target / "blob.bin", 10)
    real = A.TS.walk_usage

    def partial(path, **kwargs):
        usage = real(path, **kwargs)
        usage.update({"walk": "partial", "unknown": True})
        return usage

    monkeypatch.setattr(A.TS, "walk_usage", partial)
    asset = A.register(repo, target, kind="cache", owner="ozagia",
                       deletable=True)
    assert asset["state"] == "partial"
    assert A.verify(repo, asset["id"])["state"] == "partial"
    assert A.gc_plan(repo)["candidates"] == []
