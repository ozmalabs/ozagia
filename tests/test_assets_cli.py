"""The `ozagia assets` command."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from ozagia import cli
from ozagia.core import assets as A


@pytest.fixture()
def env(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    state = tmp_path / "state"
    monkeypatch.setenv("OZAGIA_ROOT", str(repo))
    monkeypatch.setenv("OZAGIA_STATE_DIR", str(state))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    return repo, state


def _json(capsys) -> dict:
    return json.loads(capsys.readouterr().out)


def _write(path: Path, size: int = 10) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)


def test_register_list_status_verify_pin_unpin_forget(env, capsys) -> None:
    repo, _state = env
    data = repo.parent / "dataset"
    data.mkdir()
    _write(data / "part.parquet", 50)

    assert cli.main(["assets", "register", "--path", str(data), "--kind",
                     "dataset", "--owner", "ozagia", "--scope", "user",
                     "--source-url", "https://example.test/d",
                     "--source-kind", "url", "--revision", "r1",
                     "--version", "v1", "--license", "CC-BY-4.0",
                     "--deletable", "--reason", "fixture", "--json"]) == 0
    created = _json(capsys)
    assert created["ok"] is True
    asset = created["asset"]
    assert asset["id"].startswith("ast_")
    assert asset["source"]["url"] == "https://example.test/d"
    assert asset["source"]["revision"] == "r1"
    assert asset["license"]["spdx"] == "CC-BY-4.0"
    assert asset["deletable"] is True

    assert cli.main(["assets", "list", "--json"]) == 0
    listed = _json(capsys)
    assert listed["ok"] is True
    assert listed["count"] == 1
    assert listed["assets"][0]["id"] == asset["id"]

    assert cli.main(["assets", "list", "--owner", "ocelot", "--json"]) == 0
    assert _json(capsys)["count"] == 0

    assert cli.main(["assets", "status", "--top", "1", "--json"]) == 0
    status = _json(capsys)
    assert status["totals"]["entries"] == 1
    assert status["by_kind"]["dataset"]["entries"] == 1
    assert status["top"][0]["id"] == asset["id"]

    assert cli.main(["assets", "status", asset["id"], "--json"]) == 0
    assert _json(capsys)["asset"]["id"] == asset["id"]
    assert cli.main(["assets", "status", "--path", str(data), "--json"]) == 0
    assert _json(capsys)["asset"]["id"] == asset["id"]

    assert cli.main(["assets", "verify", asset["id"], "--json"]) == 0
    verified = _json(capsys)
    assert verified["ok"] is True
    assert verified["results"][0]["state"] == "present"

    assert cli.main(["assets", "pin", asset["id"], "--json"]) == 0
    assert _json(capsys)["asset"]["pinned"] is True
    assert cli.main(["assets", "list", "--pinned", "--json"]) == 0
    assert _json(capsys)["count"] == 1
    assert cli.main(["assets", "unpin", asset["id"], "--json"]) == 0
    assert _json(capsys)["asset"]["pinned"] is False

    assert cli.main(["assets", "forget", asset["id"], "--json"]) == 0
    forgotten = _json(capsys)
    assert forgotten["forgotten"] == asset["id"]
    assert data.is_dir()
    assert cli.main(["assets", "list", "--json"]) == 0
    assert _json(capsys)["count"] == 0


def test_verify_missing_and_mismatch_exit_one(env, capsys) -> None:
    repo, _state = env
    missing = repo.parent / "missing"
    missing.mkdir()
    _write(missing / "blob.bin", 10)
    assert cli.main(["assets", "register", "--path", str(missing), "--kind",
                     "cache", "--owner", "ozagia", "--json"]) == 0
    asset = _json(capsys)["asset"]
    missing.rename(repo.parent / "renamed")
    assert cli.main(["assets", "verify", asset["id"], "--json"]) == 1
    result = _json(capsys)
    assert result["ok"] is False
    assert result["results"][0]["state"] == "missing"

    model = repo.parent / "model.bin"
    model.write_bytes(b"weights")
    digest = hashlib.sha256(model.read_bytes()).hexdigest()
    assert cli.main(["assets", "register", "--path", str(model), "--kind",
                     "model", "--owner", "huggingface", "--sha256", digest,
                     "--json"]) == 0
    model_asset = _json(capsys)["asset"]
    assert cli.main(["assets", "verify", model_asset["id"], "--hash",
                     "--json"]) == 0
    assert _json(capsys)["results"][0]["state"] == "verified"
    model.write_bytes(b"tampered")
    assert cli.main(["assets", "verify", model_asset["id"], "--hash",
                     "--json"]) == 1
    assert _json(capsys)["results"][0]["state"] == "mismatch"


def test_gc_is_a_dry_run_and_apply_is_refused(env, capsys) -> None:
    repo, _state = env
    cache = repo.parent / "cache"
    cache.mkdir()
    _write(cache / "blob.bin", 100)
    assert cli.main(["assets", "register", "--path", str(cache), "--kind",
                     "cache", "--owner", "ozagia", "--deletable",
                     "--json"]) == 0
    asset = _json(capsys)["asset"]

    assert cli.main(["assets", "gc", "--max-bytes", "100000", "--json"]) == 0
    plan = _json(capsys)
    assert plan["ok"] is True
    assert plan["dry_run"] is True
    assert [row["id"] for row in plan["candidates"]] == [asset["id"]]

    assert cli.main(["assets", "gc", "--apply", "--json"]) == 1
    refused = _json(capsys)
    assert refused["ok"] is False
    assert "dry-run" in refused["error"]
    assert cache.is_dir()


def test_marker_scan_and_external_marker(env, capsys) -> None:
    repo, _state = env
    marked = repo.parent / "marked"
    marked.mkdir()
    _write(marked / "blob.bin", 10)
    assert cli.main(["assets", "register", "--path", str(marked), "--kind",
                     "cache", "--owner", "ozagia", "--mark", "--json"]) == 0
    asset = _json(capsys)["asset"]
    assert (marked / A.MARKER_NAME).is_file()
    assert cli.main(["assets", "forget", asset["id"], "--json"]) == 0
    _json(capsys)

    assert cli.main(["assets", "scan", "--path", str(marked),
                     "--json"]) == 0
    scanned = _json(capsys)
    assert scanned["found"] == 1
    assert [row["id"] for row in scanned["registered"]] == [asset["id"]]

    external = repo.parent / "external"
    external.mkdir()
    (external / A.MARKER_NAME).write_text(
        json.dumps({"schema": A.SCHEMA, "id": "ast_external",
                    "kind": "dataset", "owner": "huggingface",
                    "path": str(external)}), encoding="utf-8")
    assert cli.main(["assets", "scan", "--path", str(external),
                     "--json"]) == 0
    rejected = _json(capsys)
    assert rejected["registered"] == []
    assert rejected["errors"]


def test_plain_output_is_short_and_stable(env, capsys) -> None:
    repo, _state = env
    cache = repo.parent / "cache"
    cache.mkdir()
    _write(cache / "blob.bin", 10)
    assert cli.main(["assets", "register", "--path", str(cache), "--kind",
                     "cache", "--owner", "ozagia"]) == 0
    line = capsys.readouterr().out.strip()
    asset_id, path = line.split("  ", 1)
    assert asset_id.startswith("ast_")
    assert path == str(cache.resolve())

    assert cli.main(["assets", "list"]) == 0
    listed = capsys.readouterr().out.strip()
    assert asset_id in listed
    assert "cache" in listed and "ozagia" in listed

    assert cli.main(["assets", "status"]) == 0
    status = capsys.readouterr().out
    assert "assets=1" in status

    assert cli.main(["assets", "gc"]) == 0
    plan = capsys.readouterr().out
    assert "dry-run" in plan
    assert "candidates=0" in plan


def test_bad_arguments_exit_two(env, capsys) -> None:
    repo, _state = env
    _write(repo.parent / "blob.bin", 5)
    with pytest.raises(SystemExit) as error:
        cli.main(["assets", "register", "--path", str(repo.parent / "blob.bin"),
                  "--owner", "ozagia"])
    assert error.value.code == 2

    assert cli.main(["assets", "verify"]) == 2
    capsys.readouterr()
    assert cli.main(["assets", "status", "ast_x", "--path",
                     str(repo.parent)]) == 2
    capsys.readouterr()


def test_json_missing_record_is_not_found(env, capsys) -> None:
    _repo, _state = env
    assert cli.main(["assets", "status", "ast_missing", "--json"]) == 1
    result = _json(capsys)
    assert result["ok"] is False
    assert result["code"] == "not_found"
