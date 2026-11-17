from __future__ import annotations

import json
import subprocess
from pathlib import Path

from ozagia.core import federation
from ozagia.core.reference_sync import sync_reference
from ozagia.federation_cli import main as federation_main


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def _repo_pair(tmp_path: Path) -> tuple[Path, Path, Path]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "--initial-branch=main", str(origin)],
                   check=True, capture_output=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", str(origin), str(seed)], check=True,
                   capture_output=True)
    _git(seed, "config", "user.name", "Test")
    _git(seed, "config", "user.email", "test@example.invalid")
    (seed / "version.txt").write_text("one\n", encoding="utf-8")
    _git(seed, "add", "version.txt")
    _git(seed, "commit", "-m", "initial")
    _git(seed, "push", "-u", "origin", "main")
    local = tmp_path / "local"
    subprocess.run(["git", "clone", str(origin), str(local)], check=True,
                   capture_output=True)
    return origin, seed, local


def _publish(seed: Path, value: str) -> str:
    (seed / "version.txt").write_text(value + "\n", encoding="utf-8")
    _git(seed, "add", "version.txt")
    _git(seed, "commit", "-m", value)
    _git(seed, "push", "origin", "main")
    return _git(seed, "rev-parse", "HEAD")


def _manifest(root: Path, *, members=None, references=None) -> None:
    state = root / ".ozagia"
    state.mkdir(parents=True, exist_ok=True)
    data = {"version": 1, "links": []}
    if members is not None:
        data["members"] = members
    if references is not None:
        data["reference_repos"] = references
    (state / "federation.json").write_text(json.dumps(data), encoding="utf-8")


def test_reference_fast_forward_and_post_merge_hook_disabled(tmp_path: Path) -> None:
    _origin, seed, local = _repo_pair(tmp_path)
    target = _publish(seed, "two")
    hook = local / ".git" / "hooks" / "post-merge"
    hook.write_text("#!/bin/sh\ntouch hook-ran\n", encoding="utf-8")
    hook.chmod(0o755)

    result = sync_reference(local, force=True)

    assert result["state"] == "updated"
    assert result["index_state"] == "stale_until_reindexed"
    assert _git(local, "rev-parse", "HEAD") == target
    assert (local / "version.txt").read_text(encoding="utf-8") == "two\n"
    assert not (local / "hook-ran").exists()


def test_dirty_and_diverged_references_are_not_changed(tmp_path: Path) -> None:
    _origin, seed, local = _repo_pair(tmp_path)
    (local / "local.txt").write_text("untracked\n", encoding="utf-8")
    dirty_head = _git(local, "rev-parse", "HEAD")
    assert sync_reference(local, force=True)["reason"] == "working_tree_dirty"
    assert _git(local, "rev-parse", "HEAD") == dirty_head
    (local / "local.txt").unlink()

    _git(local, "config", "user.name", "Test")
    _git(local, "config", "user.email", "test@example.invalid")
    (local / "local-only.txt").write_text("local\n", encoding="utf-8")
    _git(local, "add", "local-only.txt")
    _git(local, "commit", "-m", "local-only")
    _publish(seed, "remote-only")
    diverged_head = _git(local, "rev-parse", "HEAD")

    result = sync_reference(local, force=True)

    assert result["reason"] == "not_fast_forward"
    assert _git(local, "rev-parse", "HEAD") == diverged_head


def test_pin_preserves_checkout_and_reports_mismatch(tmp_path: Path) -> None:
    _origin, seed, local = _repo_pair(tmp_path)
    pin = _git(local, "rev-parse", "HEAD")
    _publish(seed, "two")

    assert sync_reference(local, pin=pin, force=True)["state"] == "pinned"
    result = sync_reference(local, pin="0" * 40, force=True)
    assert result["reason"] == "pin_mismatch"
    assert _git(local, "rev-parse", "HEAD") == pin


def test_automatic_sync_only_selected_designated_references(tmp_path: Path,
                                                           capsys) -> None:
    anchor, first, second = tmp_path / "anchor", tmp_path / "first", tmp_path / "second"
    anchor.mkdir()
    _origin1, seed1, repo1 = _repo_pair(tmp_path / "one")
    _origin2, seed2, repo2 = _repo_pair(tmp_path / "two")
    # The anchor manifest is the only authority to update either checkout.
    _manifest(anchor, members=[str(repo2)], references=[{"path": str(repo1)}])
    expected1 = _publish(seed1, "first-new")
    expected2 = _publish(seed2, "second-new")

    assert federation_main(["status", "--root", str(anchor), "--no-sync"]) == 0
    capsys.readouterr()
    assert _git(repo1, "rev-parse", "HEAD") != expected1
    assert _git(repo2, "rev-parse", "HEAD") != expected2

    automatic = federation.status(anchor)
    states = {member["root"]: member.get("sync", {}).get("state")
              for member in automatic["members"]}
    assert _git(repo1, "rev-parse", "HEAD") == expected1
    assert _git(repo2, "rev-parse", "HEAD") != expected2
    assert states[str(repo1)] == "updated"
    assert states[str(repo2)] is None

    assert federation_main(["sync", "--root", str(anchor), "--repo", str(repo1)]) == 0
    capsys.readouterr()

    assert _git(repo1, "rev-parse", "HEAD") == expected1
    assert _git(repo2, "rev-parse", "HEAD") != expected2
    status = federation.reference_status(anchor)
    assert status["references"][0]["state"] == "current"
