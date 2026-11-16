"""Opt-in Git hooks for the worktree commit/push policy."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from ozagia import cli, hooks_cli
from ozagia.core import hooks as H
from ozagia.core import worktree as W

AWS_KEY = "AKIAIOSFODNN7EXAMPLE"


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env.update({
        "GIT_AUTHOR_NAME": "Hooks Test", "GIT_AUTHOR_EMAIL": "hooks@example.com",
        "GIT_COMMITTER_NAME": "Hooks Test",
        "GIT_COMMITTER_EMAIL": "hooks@example.com",
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
    })
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          text=True, check=True, env=env)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "symbolic-ref", "HEAD", "refs/heads/main")
    _git(root, "config", "user.name", "Hooks Test")
    _git(root, "config", "user.email", "hooks@example.com")
    (root / "README.md").write_text("# Test\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(root, "commit", "-q", "-m", "initial")
    return root


def _hook(root: Path, name: str) -> Path:
    return root / ".git" / "hooks" / name


def _env() -> dict[str, str]:
    env = dict(os.environ)
    source = Path(__file__).resolve().parents[1] / "src"
    env["PYTHONPATH"] = str(source) + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    return env


def test_install_writes_executable_marked_scripts_and_is_idempotent(
        repo: Path) -> None:
    report = H.install(repo, ["commit", "push"], python=sys.executable)
    assert report["ok"] is True
    assert report["changed"] is True
    assert report["installed"] is True
    assert report["installed_kinds"] == ["commit", "push"]
    commit = _hook(repo, "pre-commit")
    push = _hook(repo, "pre-push")
    for path, kind in ((commit, "commit"), (push, "push")):
        assert path.is_file()
        assert path.stat().st_mode & 0o111
        text = path.read_text(encoding="utf-8")
        assert text.startswith("#!/bin/sh\n")
        assert H.MARKER in text
        assert f"--for {kind}" in text
        assert "-m ozagia.worktree_cli policy" in text
        assert sys.executable in text

    second = H.install(repo, ["commit", "push"], python=sys.executable)
    assert second["changed"] is False
    assert {item["status"] for item in second["entries"]} == {"unchanged"}
    assert second["ok"] is True
    assert second["installed"] is True

    commit.chmod(0o644)
    repaired = H.install(repo, ["commit"], python=sys.executable)
    assert repaired["changed"] is True
    assert repaired["entries"][0]["status"] == "updated"
    assert commit.stat().st_mode & 0o111

    status = H.status(repo)
    assert status["installed"] is True
    assert status["installed_kinds"] == ["commit", "push"]
    assert status["foreign"] == []
    assert all(item["executable"] for item in status["entries"])
    assert all(item["ozagia"] for item in status["entries"])


def test_install_preserves_a_foreign_hook_until_force(repo: Path) -> None:
    path = _hook(repo, "pre-commit")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\necho local\n", encoding="utf-8")
    path.chmod(0o755)

    report = H.install(repo, ["commit"], python=sys.executable)
    assert report["ok"] is False
    assert report["skipped"] == ["commit"]
    assert path.read_text(encoding="utf-8") == "#!/bin/sh\necho local\n"
    status = H.status(repo)
    assert status["entries"][0]["foreign"] is True

    forced = H.install(repo, ["commit"], force=True, python=sys.executable)
    assert forced["ok"] is True
    assert forced["entries"][0]["status"] == "overwritten"
    assert H.MARKER in path.read_text(encoding="utf-8")
    assert H.status(repo)["installed_kinds"] == ["commit"]


def test_uninstall_removes_only_ozagia_hooks(repo: Path) -> None:
    H.install(repo, ["commit"], python=sys.executable)
    foreign = _hook(repo, "pre-push")
    foreign.parent.mkdir(parents=True, exist_ok=True)
    foreign.write_text("#!/bin/sh\necho local\n", encoding="utf-8")
    foreign.chmod(0o755)

    report = H.uninstall(repo, ["commit", "push"])
    assert report["removed"] is True
    assert report["removed_kinds"] == ["commit"]
    assert report["skipped"] == ["push"]
    assert report["ok"] is False
    assert not _hook(repo, "pre-commit").exists()
    assert foreign.read_text(encoding="utf-8") == "#!/bin/sh\necho local\n"

    absent = H.uninstall(repo, ["commit"])
    assert absent["removed"] is False
    assert absent["ok"] is True
    status = H.status(repo)
    assert {item["kind"]: item["status"] for item in status["entries"]} == {
        "commit": "absent", "push": "foreign"}


def test_install_requires_a_git_repository(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(H.HooksError, match="git"):
        H.install(plain, ["commit"], python=sys.executable)


def test_hooks_cli_install_status_uninstall(repo: Path,
                                            capsys: pytest.CaptureFixture[str]
                                            ) -> None:
    assert hooks_cli.main([
        "install", "--pre-commit", "--root", str(repo),
        "--python", sys.executable, "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["installed"] is True
    assert payload["installed_kinds"] == ["commit"]
    assert payload["ok"] is True

    assert hooks_cli.main(["status", "--root", str(repo), "--json"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["installed_kinds"] == ["commit"]
    assert status["entries"][0]["executable"] is True

    assert cli.main(["hooks", "status", "--root", str(repo)]) == 0
    assert "pre-commit: installed" in capsys.readouterr().out

    assert hooks_cli.main([
        "uninstall", "--pre-commit", "--root", str(repo)]) == 0
    assert "removed" in capsys.readouterr().out
    assert not _hook(repo, "pre-commit").exists()

    assert hooks_cli.main(["install", "--root", str(repo)]) == 2
    assert "choose at least one hook" in capsys.readouterr().err


def test_hooks_cli_uninstall_defaults_to_both(repo: Path,
                                             capsys: pytest.CaptureFixture[str]
                                             ) -> None:
    H.install(repo, ["commit", "push"], python=sys.executable)
    assert hooks_cli.main(["uninstall", "--root", str(repo)]) == 0
    assert "removed: pre-commit" in capsys.readouterr().out
    assert not _hook(repo, "pre-commit").exists()
    assert not _hook(repo, "pre-push").exists()


def test_hook_script_runs_the_policy_and_redacts_secrets(repo: Path) -> None:
    H.install(repo, ["commit"], python=sys.executable)
    (repo / "leak.py").write_text(f'KEY = "{AWS_KEY}"\n', encoding="utf-8")
    _git(repo, "add", "leak.py")
    proc = subprocess.run(
        [str(_hook(repo, "pre-commit"))], cwd=str(repo), capture_output=True,
        text=True, env=_env(), timeout=30, check=False)
    assert proc.returncode != 0
    output = proc.stdout + proc.stderr
    assert "policy check failed" in output
    assert AWS_KEY not in output
    assert "value withheld" in output or "sha256:" in output


def test_policy_check_never_installs_a_hook(repo: Path) -> None:
    W.policy_check(repo, "commit", message="feat: clean change")
    assert not _hook(repo, "pre-commit").exists()
    assert not _hook(repo, "pre-push").exists()
