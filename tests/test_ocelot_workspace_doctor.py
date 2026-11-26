"""Read-only Ocelot engine/worktree doctor fixtures and checks.

Every fixture is a temporary tree: no network, no Ocelot import, no build and
no interpreter execution. The expected build-record fingerprint is computed
locally (not via the module under test) so a regression in the exported
fingerprint helper cannot hide itself.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from ozagia.integrations.ocelot import workspace_doctor

NATIVE_REL = Path("runners/dsl/python/substrate_rs/_native.abi3.so")
BUILD_RECORD_REL = Path(
    "runners/dsl/python/substrate_rs/_native.build.json")
SRC_REL = Path("runners/dsl/src")
CARGO_REL = Path("runners/dsl/Cargo.toml")
NATIVE_BYTES = b"\x7fELF fake substrate_rs engine\n"
CARGO_BYTES = b'[package]\nname = "substrate_rs"\nversion = "0.1.0"\n'
LIB_BYTES = b"pub fn answer() -> u32 { 42 }\n"
PROBE_BYTES = b"fn main() {}\n"


def _write(path: Path, data: bytes | str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        path.write_text(data, encoding="utf-8")
    else:
        path.write_bytes(data)
    return path


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _expected_hashes(root: Path) -> dict[str, str]:
    hashes = {
        path.relative_to(root).as_posix(): _sha(path.read_bytes())
        for path in sorted((root / SRC_REL).rglob("*.rs"))
    }
    cargo = root / CARGO_REL
    if cargo.is_file():
        hashes[cargo.relative_to(root).as_posix()] = _sha(cargo.read_bytes())
    return hashes


def _expected_fingerprint(root: Path) -> str:
    hashes = _expected_hashes(root)
    cargo_rel = CARGO_REL.as_posix()
    digest = hashlib.sha256()
    for rel in sorted(rel for rel in hashes if rel != cargo_rel):
        digest.update(rel.encode("utf-8"))
        digest.update(hashes[rel].encode("utf-8"))
    if cargo_rel in hashes:
        digest.update(b"Cargo.toml")
        digest.update(hashes[cargo_rel].encode("utf-8"))
    return digest.hexdigest()


def _build_record(root: Path, native_bytes: bytes = NATIVE_BYTES) -> str:
    return json.dumps({
        "engine_sha256": _sha(native_bytes),
        "source_fingerprint": _expected_fingerprint(root),
        "files": _expected_hashes(root),
    }, indent=1, sort_keys=True)


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Keep the ``~/.local`` .pth scan inside the test's temp directory."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "ocelot"
    root.mkdir()
    _write(root / CARGO_REL, CARGO_BYTES)
    _write(root / SRC_REL / "lib.rs", LIB_BYTES)
    _write(root / SRC_REL / "bin" / "probe.rs", PROBE_BYTES)

    package = root / "runners/dsl/python/substrate_rs"
    _write(package / "__init__.py", "from . import _native\n")
    _write(package / "_native.abi3.so", NATIVE_BYTES)
    _write(package / "_native.build.json", _build_record(root))

    _write(root / "data" / "corpus" / "sample.txt", "sample\n")
    _write(root / "data" / "models" / "model.bin", b"model\n")
    _write(root / "results" / "failure_baseline.json", '{"nodes": []}\n')

    venv_python = root / ".venv" / "bin" / "python"
    _write(venv_python, "")
    venv_python.chmod(0o755)
    return root


def _init_git(root: Path) -> None:
    subprocess.run(
        ["git", "-C", str(root), "init", "-q"], check=True)
    subprocess.run(
        ["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(
        [
            "git", "-C", str(root),
            "-c", "user.name=Workspace Doctor",
            "-c", "user.email=doctor@example.invalid",
            "commit", "-qm", "fixture",
        ],
        check=True,
    )


def test_healthy_workspace_is_ready(workspace: Path) -> None:
    _init_git(workspace)

    result = workspace_doctor.status(workspace)

    assert result["ok"] is True
    assert result["state"] == "ready"
    assert result["root"] == str(workspace.resolve())
    assert result["blockers"] == []
    assert result["warnings"] == []

    assert result["engine"]["state"] == "ready"
    assert result["engine"]["headless_package"] is False
    assert result["engine"]["source_matches_record"] is True
    assert result["engine"]["engine_hash_matches_record"] is True
    assert result["engine"]["stale_rust"] == []
    assert result["engine"]["new_rust"] == []
    assert result["engine"]["native"]["exists"] is True
    assert result["engine"]["native"]["size"] == len(NATIVE_BYTES)
    assert result["engine"]["native"]["sha256"] == _sha(NATIVE_BYTES)
    assert result["engine"]["native"]["is_symlink"] is False
    assert result["engine"]["build_record"]["readable"] is True
    assert result["engine"]["build_record"]["files"] == _expected_hashes(
        workspace)
    assert (result["engine"]["source_fingerprint"]
            == _expected_fingerprint(workspace))
    assert (workspace_doctor.rust_source_fingerprint(workspace)
            == _expected_fingerprint(workspace))
    assert workspace_doctor.rust_source_hashes(workspace) == _expected_hashes(
        workspace)

    assert result["git"]["available"] is True
    assert result["git"]["top_level"] == str(workspace.resolve())
    assert result["git"]["branch"] is not None
    assert result["git"]["head"] is not None
    assert result["git"]["detached"] is False
    assert result["git"]["dirty"]["count"] == 0
    assert result["git"]["worktree_count"] == 1

    assert result["interpreter"]["source"] == "venv"
    assert result["interpreter"]["exists"] is True
    assert result["interpreter"]["executed"] is False

    assert result["pth"]["count"] == 0
    assert result["data"]["exists"] is True
    assert result["data"]["top_level_dirs"] == 2
    assert result["baseline"]["exists"] is True
    assert "ozagia index --root ." in result["next_commands"]

    text = workspace_doctor.format_text(result)
    assert "Ocelot workspace: ready" in text
    assert str(workspace.resolve()) in text

    explicit = workspace_doctor.status(
        workspace, python=str(workspace / ".venv" / "bin" / "python"))
    assert explicit["interpreter"]["source"] == "argument"
    assert explicit["interpreter"]["exists"] is True


def test_missing_native_is_blocked_and_headless(workspace: Path) -> None:
    (workspace / NATIVE_REL).unlink()
    (workspace / BUILD_RECORD_REL).unlink()

    result = workspace_doctor.status(workspace)

    assert result["ok"] is False
    assert result["state"] == "blocked"
    assert result["engine"]["native"]["exists"] is False
    assert result["engine"]["headless_package"] is True
    assert result["engine"]["package"]["py_file_count"] >= 1
    message = " ".join(result["blockers"]).lower()
    assert "native engine artifact is missing" in message
    assert "headless" in message
    assert "build record is missing" in message
    assert "bash scripts/engine_build.sh --lib-only" in result["next_commands"]


def test_source_changed_after_build_is_blocked(workspace: Path) -> None:
    assert workspace_doctor.status(workspace)["state"] == "ready"

    _write(workspace / SRC_REL / "lib.rs", b"pub fn answer() -> u32 { 43 }\n")
    _write(workspace / SRC_REL / "new.rs", b"pub fn added() {}\n")

    result = workspace_doctor.status(workspace)

    assert result["state"] == "blocked"
    assert result["engine"]["source_matches_record"] is False
    assert result["engine"]["engine_hash_matches_record"] is True
    assert "runners/dsl/src/lib.rs" in result["engine"]["stale_rust"]
    assert "runners/dsl/src/new.rs" in result["engine"]["stale_rust"]
    assert result["engine"]["changed_rust"] == ["runners/dsl/src/lib.rs"]
    assert result["engine"]["new_rust"] == ["runners/dsl/src/new.rs"]
    assert result["engine"]["missing_rust"] == []
    assert any(
        "Rust source does not match" in blocker
        for blocker in result["blockers"])
    assert "bash scripts/engine_build.sh --lib-only" in result["next_commands"]


def test_changed_engine_bytes_are_blocked(workspace: Path) -> None:
    assert workspace_doctor.status(workspace)["state"] == "ready"

    _write(workspace / NATIVE_REL, b"different native engine bytes\n")

    result = workspace_doctor.status(workspace)

    assert result["state"] == "blocked"
    assert result["engine"]["source_matches_record"] is True
    assert result["engine"]["engine_hash_matches_record"] is False
    assert result["engine"]["stale_rust"] == []
    assert result["engine"]["native"]["sha256"] == _sha(
        b"different native engine bytes\n")
    assert any(
        "engine sha256 does not match" in blocker
        for blocker in result["blockers"])


def test_symlinked_native_reports_target(
    workspace: Path, tmp_path: Path,
) -> None:
    target = tmp_path / "canonical" / "libsubstrate_rs.so"
    _write(target, NATIVE_BYTES)
    native = workspace / NATIVE_REL
    native.unlink()
    native.symlink_to(target)

    result = workspace_doctor.status(workspace)

    assert result["state"] == "ready"
    assert result["engine"]["native"]["exists"] is True
    assert result["engine"]["native"]["is_symlink"] is True
    assert result["engine"]["native"]["link_target"] == str(target)
    assert result["engine"]["native"]["realpath"] == str(target.resolve())
    assert result["engine"]["native"]["sha256"] == _sha(NATIVE_BYTES)
    assert result["engine"]["engine_hash_matches_record"] is True


def test_missing_data_and_baseline_warn_only(workspace: Path) -> None:
    shutil.rmtree(workspace / "data")
    (workspace / "results" / "failure_baseline.json").unlink()

    result = workspace_doctor.status(workspace)

    assert result["ok"] is False
    assert result["state"] == "warn"
    assert result["engine"]["state"] == "ready"
    assert result["blockers"] == []
    assert any("data directory is missing" in item
               for item in result["warnings"])
    assert any("failure baseline is missing" in item
               for item in result["warnings"])
    assert result["data"]["exists"] is False
    assert result["data"]["top_level_dirs"] == 0
    assert result["baseline"]["exists"] is False
    assert any("failure_baseline.py" in command
               for command in result["next_commands"])


def test_pth_outside_root_is_reported(
    workspace: Path, isolated_home: Path,
) -> None:
    root_target = workspace / "runners/dsl/python"
    _write(
        workspace / ".venv/lib/python3.12/site-packages/substrate_rs.pth",
        f"{root_target}\n",
    )
    canonical_target = workspace.parent / "canonical/runners/dsl/python"
    home_pth = (
        isolated_home
        / ".local/lib/python3.12/site-packages/substrate_rs.pth"
    )
    _write(home_pth, f"{canonical_target}\n")

    result = workspace_doctor.status(workspace)

    assert result["state"] == "warn"
    assert result["ok"] is False
    assert result["blockers"] == []
    assert result["pth"]["count"] == 2
    assert result["pth"]["outside_root"] is True
    assert str(canonical_target) in result["pth"]["outside_targets"]
    assert result["pth"]["ambiguous"] is True

    by_path = {entry["path"]: entry for entry in result["pth"]["entries"]}
    root_entry = by_path[
        str(workspace / ".venv/lib/python3.12/site-packages/substrate_rs.pth")
    ]
    home_entry = by_path[str(home_pth)]
    assert root_entry["inside_root"] is True
    assert home_entry["inside_root"] is False
    assert home_entry["target"] == str(canonical_target)
    assert home_entry["targets"][0]["resolved"] == str(canonical_target)
    assert any(
        "substrate_rs.pth points outside this worktree" in item
        for item in result["warnings"])
    assert "bash scripts/engine_build.sh --lib-only" in result["next_commands"]


def test_pth_inside_root_and_home_glob_are_reported(
    workspace: Path, isolated_home: Path,
) -> None:
    target = workspace / "runners/dsl/python"
    home_pth = (
        isolated_home
        / ".local/lib/python3.12/site-packages/substrate_rs.pth"
    )
    _write(home_pth, f"{target}\n")

    result = workspace_doctor.status(workspace)

    assert result["state"] == "ready"
    assert result["pth"]["count"] == 1
    assert result["pth"]["inside_root"] is True
    assert result["pth"]["outside_root"] is False
    assert result["pth"]["entries"][0]["path"] == str(home_pth)
    assert os.path.realpath(home_pth) == result["pth"]["entries"][0]["path"]
