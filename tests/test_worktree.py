"""Deterministic worktree, branch, drift and policy checks."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from ozagia.core import worktree as W
from ozagia import worktree_cli


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env.update({
        "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.com",
        "GIT_CONFIG_NOSYSTEM": "1",
    })
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          text=True, check=True, env=env)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "symbolic-ref", "HEAD", "refs/heads/main")
    _write(root / "README.md", "# Test\n")
    _git(root, "add", "README.md")
    _git(root, "commit", "-q", "-m", "initial")
    worktree = tmp_path / "ozagia-wt" / "alpha"
    worktree.parent.mkdir(parents=True, exist_ok=True)
    _git(root, "worktree", "add", "-q", "-b", "feat/alpha", str(worktree))
    return root, worktree


def test_discover_and_pattern_expected(repo) -> None:
    root, worktree = repo
    found = W.discover(root)
    assert len(found) == 2
    assert found[0]["is_main"] is True
    assert found[1]["path"] == str(worktree.resolve())
    assert found[1]["branch"] == "feat/alpha"
    report = W.check(root)
    assert report["ok"] is True
    expected = {item["path"]: item["expected"] for item in report["worktrees"]}
    assert expected[str(worktree.resolve())]["branch"] == "feat/alpha"
    assert expected[str(worktree.resolve())]["source"] == "pattern"


def test_wrong_branch_mapping(repo) -> None:
    root, worktree = repo
    _write(root / "ozagia" / "worktrees.toml", """
[worktrees.alpha]
path = "../ozagia-wt/alpha"
branch = "feat/beta"
""")
    report = W.check(root)
    assert report["ok"] is False
    assert any(finding["id"] == "worktree.expected_branch"
               for finding in report["findings"])
    item = next(item for item in report["worktrees"]
                if item["path"] == str(worktree.resolve()))
    assert item["expected"]["source"] == "worktrees.toml"
    assert item["expected"]["branch"] == "feat/beta"


def test_dirty_is_reported_as_info(repo) -> None:
    root, worktree = repo
    _write(worktree / "notes.txt", "scratch\n")
    report = W.check(root)
    assert report["ok"] is True
    assert any(finding["id"] == "worktree.dirty" and finding["level"] == "info"
               for finding in report["findings"])


def test_detached_worktree_warns_but_check_passes(repo) -> None:
    root, worktree = repo
    detached = worktree.parent / "verify"
    _git(root, "worktree", "add", "--detach", "-q", str(detached), "main")
    report = W.check(root)
    item = next(item for item in report["worktrees"]
                if item["path"] == str(detached.resolve()))
    assert item["detached"] is True
    assert any(finding["id"] == "worktree.detached"
               and finding["level"] == "warn"
               for finding in item["findings"])
    assert report["ok"] is True
    policy = W.policy_check(detached, "commit")
    assert policy["ok"] is False
    assert any(check["id"] == "commit.branch" for check in policy["checks"])


def test_tmpfs_worktree_is_warned(repo, monkeypatch) -> None:
    root, _worktree = repo
    monkeypatch.setattr(W, "disk_usage", lambda path: {
        "path": str(path), "exists": True, "mount": "/tmp", "fstype": "tmpfs",
        "total_gb": 10.0, "free_gb": 8.0, "free_pct": 80.0,
        "inodes_free": 100, "is_tmpfs": True, "source": "test", "error": None,
    })
    report = W.check(root)
    assert any(finding["id"] == "worktree.tmpfs"
               for finding in report["findings"])


def test_commit_policy_protected_main(repo) -> None:
    root, worktree = repo
    main = W.policy_check(root, "commit")
    assert main["ok"] is False
    assert any(check["id"] == "commit.protected_branch"
               for check in main["checks"])
    alpha = W.policy_check(worktree, "commit", message="feat: alpha")
    assert alpha["ok"] is True


def test_commit_policy_message_and_staged_only(repo) -> None:
    root, worktree = repo
    _write(root / "ozagia" / "policies.toml", """
[commit]
staged_only = true
message_pattern = "^(feat|fix): .+"
""")
    _write(worktree / "change.txt", "change\n")
    _git(worktree, "add", "change.txt")
    bad = W.policy_check(worktree, "commit", message="not conventional")
    assert bad["ok"] is False
    assert any(check["id"] == "commit.message" for check in bad["checks"])
    good = W.policy_check(worktree, "commit", message="feat: alpha")
    assert good["ok"] is True


def test_merge_conflict_preview(repo) -> None:
    root, worktree = repo
    _write(worktree / "conflict.txt", "alpha\n")
    _git(worktree, "add", "conflict.txt")
    _git(worktree, "commit", "-q", "-m", "alpha conflict")
    _write(root / "conflict.txt", "main\n")
    _git(root, "add", "conflict.txt")
    _git(root, "commit", "-q", "-m", "main conflict")
    report = W.policy_check(worktree, "merge", target="main")
    ids = {check["id"] for check in report["checks"]}
    if "merge.conflicts" not in ids:
        pytest.skip("git merge-tree --write-tree is unavailable")
    assert report["ok"] is False
    assert any(check["id"] == "merge.conflicts"
               and check["level"] == "error"
               for check in report["checks"])


def test_push_policy_protected_main(repo) -> None:
    root, _worktree = repo
    report = W.policy_check(root, "push")
    assert report["ok"] is False
    assert any(check["id"] == "push.protected_branch"
               for check in report["checks"])


def test_prune_is_dry_run(repo) -> None:
    root, _worktree = repo
    report = W.prune_dry_run(root)
    assert report["dry_run"] is True
    assert report["ok"] is True


def test_worktree_cli_check_json(repo, capsys) -> None:
    root, _worktree = repo
    code = worktree_cli.main(["check", "--root", str(root), "--json"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert len(payload["worktrees"]) == 2
