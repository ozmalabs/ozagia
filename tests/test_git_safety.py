"""Target-controlled Git filters stay out of read-only tree inspection."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from ozagia.core import fingerprint, memory


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("GIT_")}
    env.update({"GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
                "GIT_COMMITTER_NAME": "Test",
                "GIT_COMMITTER_EMAIL": "test@example.invalid"})
    return subprocess.run(["git", "-C", str(root), *args], env=env,
                          capture_output=True, check=True)


def _repository_with_process_filter(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.invalid")
    (root / ".gitattributes").write_text("*.txt filter=marker\n", encoding="utf-8")
    sample = root / "sample.txt"
    sample.write_text("committed\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "initial")

    marker = tmp_path / "filter-was-run"
    process = tmp_path / "clean-filter.sh"
    process.write_text(
        "#!/bin/sh\n"
        f"printf invoked > '{marker}'\n"
        "exit 0\n", encoding="utf-8")
    process.chmod(0o755)
    _git(root, "config", "filter.marker.process", str(process))
    sample.write_text("changed after filter setup\n", encoding="utf-8")
    return root, marker


def test_fingerprint_status_does_not_execute_configured_process_filter(tmp_path):
    root, marker = _repository_with_process_filter(tmp_path)

    result = fingerprint.tree_fingerprint(root)

    assert result is not None
    assert result["dirty"]
    assert not marker.exists()


def test_memory_tree_digest_diff_does_not_execute_configured_process_filter(tmp_path):
    root, marker = _repository_with_process_filter(tmp_path)

    digest, error = memory._tree_digest(root, include_docs="none", watch_globs=())

    assert digest is not None
    assert error is None
    assert not marker.exists()


def test_memory_changed_status_does_not_execute_configured_process_filter(tmp_path):
    root, marker = _repository_with_process_filter(tmp_path)
    snapshot = {"version": 1, "nodes": [], "edges": [], "next_id": 0}
    mem = memory.DevMemory(dump=root / ".ozagia" / "graph.json",
                          journal=root / ".ozagia" / "journal.jsonl",
                          root=root, snapshot_data=snapshot)

    result = mem.changed()

    assert result["ok"] is True
    assert not marker.exists()
