"""CLI ``--profile change`` preset tests."""
from __future__ import annotations

import subprocess
from pathlib import Path


def _init_repo(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True,
                   capture_output=True, text=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email",
                    "test@example.com"], check=True,
                   capture_output=True, text=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"],
                   check=True, capture_output=True, text=True)


def _commit(root: Path, message: str) -> None:
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True,
                   capture_output=True, text=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=Test",
                    "-c", "user.email=test@example.com", "commit", "-qm",
                    message], check=True, capture_output=True, text=True)


def _repo_with_history(root: Path, commits: int = 2) -> Path:
    _init_repo(root)
    for number in range(1, commits + 1):
        (root / "app.py").write_text(
            f"def value():\n    return {number}\n", encoding="utf-8")
        _commit(root, f"commit {number}")
    return root


def _stub_build(monkeypatch):
    import ozagia.core.deep_review as deep_review

    seen = {}

    def fake_build(root, out, **kwargs):
        seen.update(kwargs)
        seen["root"] = Path(root)
        seen["out"] = Path(out)
        return {"out": str(out), "findings": [], "failed": False,
                "manifest": {"schema": "ozagia.deep_review.manifest.v1"}}

    monkeypatch.setattr(deep_review, "build_deep_review", fake_build)
    return seen


def test_change_profile_explicit_range_uses_full_profile(tmp_path, monkeypatch):
    from ozagia.cli import main

    root = _repo_with_history(tmp_path / "repo")
    seen = _stub_build(monkeypatch)

    code = main(["deep-review", "--root", str(root), "--out",
                 str(tmp_path / "report"), "--profile", "review-change",
                 "--range", "HEAD~1..HEAD"])

    assert code == 0
    assert seen["profile"] == "full"
    assert seen["range_spec"] == "HEAD~1..HEAD"
    # The preset is compact only when the caller omitted --max-findings.
    assert seen["max_findings"] == 20
    assert seen["fail_on"] == "never"


def test_change_profile_defaults_to_previous_commit(tmp_path, monkeypatch):
    from ozagia.cli import main

    root = _repo_with_history(tmp_path / "repo")
    seen = _stub_build(monkeypatch)

    code = main(["deep-review", "--root", str(root), "--out",
                 str(tmp_path / "report"), "--profile", "change"])

    assert code == 0
    assert seen["profile"] == "full"
    assert seen["range_spec"] == "HEAD~1..HEAD"
    assert seen["max_findings"] == 20
    assert seen["fail_on"] == "never"


def test_change_profile_honors_explicit_limit_and_fail_on(tmp_path, monkeypatch):
    from ozagia.cli import main

    root = _repo_with_history(tmp_path / "repo")
    seen = _stub_build(monkeypatch)

    code = main(["deep-review", "--root", str(root), "--out",
                 str(tmp_path / "report"), "--profile", "change",
                 "--max-findings", "3", "--fail-on", "major"])

    assert code == 0
    assert seen["max_findings"] == 3
    assert seen["fail_on"] == "major"


def test_change_profile_rejects_non_git_or_missing_root(tmp_path, monkeypatch,
                                                       capsys):
    from ozagia.cli import main

    seen = _stub_build(monkeypatch)
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "app.py").write_text("pass\n", encoding="utf-8")

    code = main(["deep-review", "--root", str(plain), "--out",
                 str(tmp_path / "report"), "--profile", "change"])
    captured = capsys.readouterr()

    assert code == 2
    assert "requires a Git repository" in captured.err
    assert seen == {}

    missing = tmp_path / "missing"
    assert main(["deep-review", "--root", str(missing), "--profile",
                 "change"]) == 2
    assert "not a directory" in capsys.readouterr().err


def test_change_profile_reports_unresolvable_default_range(tmp_path,
                                                           monkeypatch, capsys):
    from ozagia.cli import main

    root = _repo_with_history(tmp_path / "repo", commits=1)
    seen = _stub_build(monkeypatch)

    code = main(["deep-review", "--root", str(root), "--out",
                 str(tmp_path / "report"), "--profile", "change"])
    captured = capsys.readouterr()

    assert code == 2
    assert "HEAD~1..HEAD" in captured.err
    assert "no previous commit" in captured.err
    assert "pass --range" in captured.err
    assert seen == {}
