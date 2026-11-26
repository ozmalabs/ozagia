"""Known-root discovery and Hugging Face metadata for external assets."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from ozagia import cli
from ozagia.core import assets as A

SHA = "a" * 64
SHA2 = "b" * 64


@pytest.fixture()
def env(tmp_path: Path, monkeypatch
        ) -> tuple[Path, Path, Path, Path]:
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
    return repo, state, data, home


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _write(path: Path, size: int = 10) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)


def _ocelot_manifest(version: str = "1.2.3") -> dict:
    return {
        "format": "ozagia-ocelot-install/1",
        "version": version,
        "source": "https://user:pass@example.test/ocelot.tar.gz?token=secret#frag",
        "sha256": SHA,
        "installed_at": "2026-11-26T00:00:00Z",
        "license": {"spdx": "MIT", "bundled": False},
        "tree": {
            "sha256": SHA2,
            "file_count": 2,
            "bytes": 30,
            "files": [
                {"path": "ocelot/__init__.py", "sha256": SHA, "size": 10},
                {"path": "ocelot/main.py", "sha256": SHA2, "size": 20},
            ],
        },
    }


def test_managed_ocelot_install_discovery(env) -> None:
    _repo, _state, data, _home = env
    install = data / "ozagia" / "ocelot" / "1.2.3"
    _write(install / "ocelot" / "__init__.py", 10)
    _write(install / "ocelot" / "main.py", 20)
    _write_json(install / A.OCELOT_MANIFEST_NAME, _ocelot_manifest())

    result = A.discover(_repo)
    assert result["errors"] == []
    installs = [row for row in result["assets"] if row["kind"] == "install"]
    assert len(installs) == 1
    asset = installs[0]
    assert asset["owner"] == "ozagia"
    assert asset["scope"] == "user"
    assert asset["pinned"] is True
    assert asset["deletable"] is False
    assert asset["version"] == "1.2.3"
    assert asset["sha256"] == SHA
    assert asset["source"]["kind"] == "manifest"
    assert asset["source"]["manifest"].endswith(A.OCELOT_MANIFEST_NAME)
    assert asset["source"]["url"] == "https://example.test/ocelot.tar.gz"
    assert "secret" not in json.dumps(asset)
    assert asset["license"] == {
        "spdx": "MIT", "status": "declared", "source": "ocelot manifest"}
    assert asset["manifest_path"].endswith(A.OCELOT_MANIFEST_NAME)
    assert asset["tree"] == {"sha256": SHA2, "bytes": 30, "file_count": 2}
    assert len(asset["digests"]) == 2
    assert all(item["verified"] is True for item in asset["digests"])

    # A second discovery updates nothing about identity and keeps the record.
    again = A.discover(_repo)
    same = [row for row in again["assets"] if row["kind"] == "install"]
    assert [row["id"] for row in same] == [asset["id"]]


def test_ocelot_discovery_reads_manifest_without_importing_ocelot(env,
                                                                  monkeypatch) -> None:
    import builtins

    _repo, _state, data, _home = env
    install = data / "ozagia" / "ocelot" / "1.2.3"
    install.mkdir(parents=True)
    _write_json(install / A.OCELOT_MANIFEST_NAME, _ocelot_manifest())
    real_import = builtins.__import__

    def guarded(name, *args, **kwargs):
        if name == "ocelot" or name.startswith("ozagia.integrations.ocelot"):
            raise AssertionError("discovery must not import Ocelot")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded)
    result = A.discover(_repo)
    assert [row["kind"] for row in result["assets"]] == ["install"]


def test_too_large_manifest_tree_keeps_tree_metadata_without_file_list(env) -> None:
    _repo, _state, data, _home = env
    install = data / "ozagia" / "ocelot" / "2.0.0"
    install.mkdir(parents=True)
    manifest = _ocelot_manifest("2.0.0")
    manifest["tree"]["files"] = [
        {"path": f"f{index}.py", "sha256": SHA, "size": 1}
        for index in range(A.MAX_DIGESTS + 1)
    ]
    _write_json(install / A.OCELOT_MANIFEST_NAME, manifest)
    result = A.discover(_repo)
    asset = next(row for row in result["assets"] if row["kind"] == "install")
    assert asset["digests"] == []
    assert asset["sha256"] == SHA
    assert asset["tree"]["sha256"] == SHA2


def test_ocelot_sibling_is_explicit_only(env) -> None:
    repo, _state, _data, _home = env
    sibling = repo.parent / "ocelot"
    sibling.mkdir()
    _write(sibling / "README.md")
    result = A.discover(repo)
    assert [row for row in result["assets"] if row["owner"] == "ocelot"] == []
    root = next(row for row in result["roots"]
                if row["name"] == "ocelot-sibling")
    assert root["status"] == "explicit-only"
    assert any("explicit-only" in row["reason"] for row in result["skipped"])

    asset = A.register(
        repo, sibling, kind="dataset", owner="ocelot", scope="shared",
        pinned=True, deletable=False, reason="Ocelot structured Wikipedia")
    assert asset["owner"] == "ocelot"
    assert asset["pinned"] is True
    assert asset["deletable"] is False
    assert not (sibling / A.MARKER_NAME).exists()
    assert A.gc_plan(repo)["candidates"] == []
    assert [row["id"] for row in A.list_assets(repo, owner="ocelot")] == [
        asset["id"]]


def test_hf_cache_discovery_and_bad_meta(env) -> None:
    repo, state, _data, _home = env
    good = state / "eval" / "hf" / "swe-bench" / "deadbeef"
    _write(good / "rows.json", 100)
    _write_json(good / "meta.json", {
        "schema": "ozagia.hf_benchmark.meta.v1",
        "benchmark": "swe-bench",
        "dataset": "princeton-nlp/SWE-bench_Verified",
        "dataset_sha": "deadbeef",
        "revision": "r-deadbeef",
        "rows_sha256": SHA,
        "license": "MIT",
        "license_status": "declared",
        "fetched_at": "2026-11-26T00:00:00Z",
    })
    bad = state / "eval" / "hf" / "swe-bench" / "bad"
    _write(bad / "rows.json", 10)
    (bad / "meta.json").write_text("{not json", encoding="utf-8")
    missing = state / "eval" / "hf" / "swe-bench" / "nometa"
    _write(missing / "rows.json", 10)
    (state / "eval" / "hf" / "notes.txt").write_text("ignore", encoding="utf-8")

    result = A.discover(repo)
    assert result["errors"]
    assert any("nometa" in row["path"] for row in result["skipped"])
    datasets = [row for row in result["assets"] if row["kind"] == "dataset"]
    assert len(datasets) == 1
    asset = datasets[0]
    assert asset["owner"] == "ozagia"
    assert asset["scope"] == "repo"
    assert asset["pinned"] is False
    assert asset["deletable"] is True
    assert asset["sha256"] == SHA
    assert asset["bytes"] > 0
    assert asset["source"] == {
        "kind": "huggingface",
        "url": None,
        "dataset": "princeton-nlp/SWE-bench_Verified",
        "repo": None,
        "revision": "r-deadbeef",
        "commit": None,
        "manifest": asset["manifest_path"],
    }
    assert asset["license"]["spdx"] == "MIT"
    assert asset["license"]["status"] == "declared"
    assert asset["manifest_path"].endswith("meta.json")

    # A later manual pin survives re-discovery.
    A.pin(repo, asset["id"])
    A.discover(repo)
    assert A.get(repo, asset["id"])["pinned"] is True


def test_deep_review_corpus_discovery(env) -> None:
    repo, state, _data, _home = env
    base = state / "eval" / "deep_review_corpus"
    corpus = {
        "schema_version": 1,
        "repositories": [{
            "id": "flask",
            "repo": "pallets/flask",
            "license": "BSD-3-Clause",
            "license_status": "verified_from_license_file_at_pinned_commit",
            "pinned_commit": "c" * 40,
            "tree_sha256": SHA,
            "archive_sha256": SHA2,
            "local_checkout": "repositories/flask",
        }],
    }
    _write_json(base / "corpus.json", corpus)
    checkout = base / "repositories" / "flask"
    _write(checkout / "app.py", 30)
    _write_json(checkout / A.DEEP_REVIEW_MARKER_NAME, {
        "schema_version": 1,
        "corpus_id": "fixture",
        "repository_id": "flask",
        "repo": "pallets/flask",
        "pinned_commit": "c" * 40,
        "archive_sha256": SHA2,
        "tree": {"sha256": SHA, "bytes": 30, "file_count": 1},
    })

    result = A.discover(repo)
    assert result["errors"] == []
    asset = next(row for row in result["assets"] if row["kind"] == "corpus")
    assert asset["owner"] == "ozagia"
    assert asset["scope"] == "repo"
    assert asset["pinned"] is True
    assert asset["deletable"] is True
    assert asset["source"]["kind"] == "git"
    assert asset["source"]["repo"] == "pallets/flask"
    assert asset["source"]["commit"] == "c" * 40
    assert asset["revision"] == "c" * 40
    assert asset["sha256"] == SHA2
    assert asset["tree"]["sha256"] == SHA
    assert asset["license"]["spdx"] == "BSD-3-Clause"
    assert asset["license"]["status"] == "declared"
    assert asset["manifest_path"].endswith(A.DEEP_REVIEW_MARKER_NAME)

    # An explicit unpin before a future GC is preserved by re-discovery.
    A.unpin(repo, asset["id"])
    A.discover(repo)
    assert A.get(repo, asset["id"])["pinned"] is False


def test_model_meta_discovery(env) -> None:
    repo, state, _data, _home = env
    models = state / "models"
    _write_json(models / "models-dev.meta.json", {
        "source": "models-dev",
        "url": "https://example.test/models.json?token=secret",
        "license": "Apache-2.0",
        "sha256": SHA,
        "bytes": 123,
        "fetched_at": "2026-11-26T00:00:00Z",
        "format": "json",
    })
    (models / "models-dev.json").write_text("{}", encoding="utf-8")
    (models / "unrelated.txt").write_text("ignore", encoding="utf-8")

    result = A.discover(repo)
    caches = [row for row in result["assets"] if row["kind"] == "cache"]
    assert len(caches) == 1
    asset = caches[0]
    assert asset["owner"] == "ozagia"
    assert asset["scope"] == "repo"
    assert asset["version"] == "models-dev"
    assert asset["sha256"] == SHA
    assert asset["source"]["kind"] == "url"
    assert asset["source"]["url"] == "https://example.test/models.json"
    assert "secret" not in json.dumps(asset)
    assert asset["license"]["spdx"] == "Apache-2.0"
    assert asset["pinned"] is False
    assert asset["deletable"] is True
    assert asset["fetched_at"] == "2026-11-26T00:00:00Z"
    assert asset["manifest_path"].endswith("models-dev.meta.json")


def test_research_caches_are_visible_but_never_gc_candidates(env) -> None:
    repo, state, _data, _home = env
    _write(state / "research" / "cache" / "one.json", 10)
    _write(state / "research" / "fetch" / "objects" / "aa" / "blob.bin", 20)
    result = A.discover(repo)
    assets = [row for row in result["assets"]
              if row["path"].startswith(str(state / "research"))]
    assert len(assets) == 2
    assert all(row["pinned"] is True for row in assets)
    assert all(row["deletable"] is False for row in assets)
    assert A.gc_plan(repo)["candidates"] == []
    skipped = A.discover(repo, include_research=False)
    assert not [row for row in skipped["assets"]
                if row["path"].startswith(str(state / "research"))]


def test_read_hf_metadata_never_hashes_data(env, monkeypatch) -> None:
    _repo, _state, _data, _home = env
    dataset = env[0].parent / "wikipedia"
    download = dataset / ".cache" / "huggingface" / "download"
    _write(download / "data" / "train.parquet", 64)
    (download / "data" / "train.parquet.metadata").write_text(
        "revision-1\n\"abc123\"\n2026-11-26T00:00:00Z\n", encoding="utf-8")
    (download / "README.md.metadata").write_text(
        f"revision-1\n{SHA}\n2026-11-26T00:00:00Z\n", encoding="utf-8")

    def forbidden(path: Path) -> str:
        raise AssertionError("read_hf_metadata must not hash data files")

    monkeypatch.setattr(A, "_file_sha256", forbidden)
    result = A.read_hf_metadata(dataset)
    assert result["revision"] == "revision-1"
    assert result["files"] == 2
    assert [row["path"] for row in result["digests"]] == [
        "README.md", "data/train.parquet"]
    assert result["digests"][0]["sha256"] == SHA
    assert result["digests"][1]["sha256"] is None
    assert all(row["verified"] is False for row in result["digests"])

    # Inconsistent revisions are refused.
    (download / "other.metadata").write_text(
        "revision-2\n" + SHA + "\n2026-11-26T00:00:00Z\n", encoding="utf-8")
    with pytest.raises(A.AssetError, match="inconsistent revisions"):
        A.read_hf_metadata(dataset)


def test_read_hf_metadata_refuses_symlinks_and_escapes(env) -> None:
    _repo, _state, _data, _home = env
    dataset = env[0].parent / "dataset"
    download = dataset / ".cache" / "huggingface" / "download"
    download.mkdir(parents=True)
    outside = env[0].parent / "outside"
    outside.mkdir()
    (outside / "x.metadata").write_text(
        "r\n" + SHA + "\n2026-11-26T00:00:00Z\n", encoding="utf-8")
    (download / "link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(A.AssetPathError, match="symlink"):
        A.read_hf_metadata(dataset)

    (download / "link").unlink()
    (download / "file.metadata").write_text(
        "r\n" + SHA + "\n2026-11-26T00:00:00Z\n", encoding="utf-8")
    (download / "escape.metadata").symlink_to(outside / "x.metadata")
    with pytest.raises(A.AssetPathError, match="symlink"):
        A.read_hf_metadata(dataset)

    (download / "escape.metadata").unlink()
    (download / "file.metadata").unlink()
    with pytest.raises(A.AssetError, match="no HF metadata"):
        A.read_hf_metadata(dataset)


def test_discover_never_walks_home_and_ignores_unrelated_files(env) -> None:
    repo, state, _data, home = env
    decoy = home / "ozagia" / "ocelot" / "9.9.9"
    _write(decoy / "ocelot" / "__init__.py", 10)
    _write_json(decoy / A.OCELOT_MANIFEST_NAME, _ocelot_manifest("9.9.9"))
    _write(home / "wiki" / "part.parquet", 20)
    _write(state / "eval" / "hf" / "unrelated.txt", 10)
    _write(state / "models" / "unrelated.txt", 10)

    result = A.discover(repo)
    assert result["assets"] == []
    assert all(not row["path"].startswith(str(home))
               for row in result["assets"])
    assert all(str(home) != row["path"] for row in result["roots"])

    # A real known root still works, and re-discovery is idempotent.
    meta = state / "eval" / "hf" / "bench" / "abc"
    _write_json(meta / "meta.json", {
        "benchmark": "bench", "dataset": "owner/name", "dataset_sha": "abc"})
    _write(meta / "rows.json", 10)
    first = A.discover(repo)
    second = A.discover(repo)
    first_ids = sorted(row["id"] for row in first["assets"])
    second_ids = sorted(row["id"] for row in second["assets"])
    assert first_ids == second_ids
    assert A.list_assets(repo, kind="dataset")


def test_cli_from_hf_metadata_registers_external_ocelot_data(env, capsys) -> None:
    repo, _state, _data, _home = env
    dataset = repo.parent / "wikipedia"
    download = dataset / ".cache" / "huggingface" / "download"
    _write(download / "data" / "train.parquet", 32)
    (download / "data" / "train.parquet.metadata").write_text(
        "rev-42\n" + SHA + "\n2026-11-26T00:00:00Z\n", encoding="utf-8")

    assert cli.main([
        "assets", "register", "--path", str(dataset), "--kind", "dataset",
        "--owner", "ocelot", "--scope", "shared", "--pinned", "--no-delete",
        "--from-hf-metadata", "--reason", "Ocelot structured Wikipedia "
        "snapshot", "--json",
    ]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    asset = payload["asset"]
    assert asset["owner"] == "ocelot"
    assert asset["pinned"] is True
    assert asset["deletable"] is False
    assert asset["license"] == {"spdx": None, "status": "unknown",
                                "source": None}
    assert asset["revision"] == "rev-42"
    assert asset["source"]["kind"] == "huggingface"
    assert asset["source"]["revision"] == "rev-42"
    assert asset["digests"][0]["path"] == "data/train.parquet"
    assert asset["digests"][0]["sha256"] == SHA
    assert asset["digests"][0]["verified"] is False
    assert not (dataset / A.MARKER_NAME).exists()
    assert A.gc_plan(repo)["candidates"] == []

    assert cli.main([
        "assets", "register", "--path", str(dataset), "--kind", "dataset",
        "--owner", "ocelot", "--scope", "shared", "--pinned", "--no-delete",
        "--from-hf-metadata", "--sha256", SHA, "--json",
    ]) == 2
    capsys.readouterr()


def test_cli_discover_reports_roots(env, capsys) -> None:
    repo, state, _data, _home = env
    meta = state / "eval" / "hf" / "bench" / "abc"
    _write_json(meta / "meta.json", {
        "benchmark": "bench", "dataset": "owner/name", "dataset_sha": "abc"})
    _write(meta / "rows.json", 10)

    assert cli.main(["assets", "discover", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert len(payload["assets"]) == 1
    names = {row["name"] for row in payload["roots"]}
    assert {"ocelot-installs", "hf-benchmarks", "deep-review-corpus",
            "model-metadata", "research-caches"} <= names
    assert any(row["name"] == "ocelot-sibling" for row in payload["roots"])

    assert cli.main(["assets", "discover", "--no-research"]) == 0
    plain = capsys.readouterr().out
    assert "discovered=" in plain
    assert "hf-benchmarks" in plain
    assert repo.is_dir()


def test_relative_xdg_and_missing_roots_are_skipped(env, monkeypatch) -> None:
    repo = env[0]
    monkeypatch.setenv("XDG_DATA_HOME", "relative/path")
    result = A.discover(repo)
    ocelot = next(row for row in result["roots"]
                  if row["name"] == "ocelot-installs")
    assert ocelot["status"] == "missing"
    assert result["errors"] == []
    assert result["partial"] is False
    assert result["assets"] == []
    assert os.path.isdir(repo)
