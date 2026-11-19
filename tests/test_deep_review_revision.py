"""Revision provenance handles both Git object formats."""
import subprocess

import pytest

from ozagia.core.deep_review.orchestrator import _revision


def test_revision_records_sha256_head_without_inherited_git_overrides(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    init = subprocess.run(["git", "init", "--object-format=sha256", "-q", str(repo)],
                          capture_output=True, check=False)
    if init.returncode:
        pytest.skip("Git SHA-256 repositories are unavailable")
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=Test", "-c",
                    "user.email=test@example.invalid", "commit", "--allow-empty",
                    "-qm", "initial"], check=True)
    expected = subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    assert len(expected) == 64
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "missing"))
    assert _revision(repo) == expected
