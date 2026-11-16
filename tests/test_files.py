"""Created-file ledger, Markdown routing and dry-run cleanup."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from ozagia.core import files as F
from ozagia import files_cli


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env.update({
        "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.com",
        "GIT_CONFIG_NOSYSTEM": "1",
    })
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          text=True, check=True, env=env)


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    (root / "README.md").write_text("# Test\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(root, "commit", "-q", "-m", "initial")
    return root


def test_root_allowlist_and_one_off(tmp_path: Path) -> None:
    readme = F.markdown_route("README.md", "# Test\n", root=tmp_path)
    assert readme["kind"] == "root"
    assert readme["allowed"] is True
    note = F.markdown_route("notes.md", "# Notes\nOne-off\n", root=tmp_path)
    assert note["kind"] == "note"
    assert note["allowed"] is False
    assert note["suggested_path"].startswith("history/")
    assert note["suggested_path"].endswith("/notes.md")


def test_docs_and_plan_routing(tmp_path: Path) -> None:
    plan = F.markdown_route(
        "docs/plans/2026-11-16_worktree.md", "# Plan\n", root=tmp_path)
    assert plan["kind"] == "plan"
    assert plan["allowed"] is True
    guide = F.markdown_route("docs/guide.md", "# Guide\n", root=tmp_path)
    assert guide["kind"] == "docs"
    assert guide["allowed"] is True
    one_off = F.markdown_route(
        "docs/one-off.md", "---\nkind: note\n---\n# Note\n", root=tmp_path)
    assert one_off["kind"] == "note"
    assert one_off["allowed"] is False
    assert one_off["suggested_path"].startswith("history/")


def test_duplicate_detection(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    content = "# Same title\n\nSame body.\n"
    (tmp_path / "docs" / "existing.md").write_text(content, encoding="utf-8")
    route = F.markdown_route("notes.md", content, root=tmp_path)
    assert route["duplicate_of"] == "docs/existing.md"
    assert route["append_to"] == "docs/existing.md"
    assert route["confidence"] == "high"


def test_scan_explain_orphans_and_cleanup(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    note = root / "notes.md"
    note.write_text("# Notes\n\nsecret content\n", encoding="utf-8")
    recorded = F.scan_new_files(root, session="ses-1", reason="scratch note")
    assert any(item["path"] == "notes.md" for item in recorded)
    explained = F.explain(root, "notes.md")
    assert explained["record"]["reason"] == "scratch note"
    assert explained["record"]["created_by"]["session"] == "ses-1"
    assert explained["route"]["kind"] == "note"
    ledger = F.ledger_path(root).read_text(encoding="utf-8")
    assert "secret content" not in ledger
    assert any(item["path"] == "notes.md" for item in F.orphans(root))
    candidates = F.cleanup_candidates(root, older_than_days=0)
    assert any(item["path"] == "notes.md" for item in candidates)
    assert F.new_file_count(root)["count"] >= 1


def test_scan_records_history_as_keep(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    history = root / "history" / "2026-11-16" / "note.md"
    history.parent.mkdir(parents=True)
    history.write_text("# History\n", encoding="utf-8")
    recorded = F.scan_new_files(root, session="ses-2", reason="one-off")
    item = next(item for item in recorded if item["path"].startswith("history/"))
    assert item["classification"] == "history"
    assert item["cleanup"]["policy"] == "keep"
    assert not any(candidate["path"].startswith("history/")
                   for candidate in F.cleanup_candidates(root, older_than_days=0))


def test_scan_uses_front_matter_for_classification(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    note = root / "docs" / "one-off.md"
    note.parent.mkdir(parents=True)
    note.write_text("---\nkind: note\nstatus: historical\n---\n# Note\n",
                    encoding="utf-8")
    recorded = F.scan_new_files(root, session="ses-3", reason="one-off")
    item = next(item for item in recorded if item["path"] == "docs/one-off.md")
    assert item["classification"] == "note"
    assert item["cleanup"]["policy"] == "ask"


def test_files_cli_route_and_scan(tmp_path: Path, capsys) -> None:
    root = _repo(tmp_path)
    code = files_cli.main(["route", "notes.md", "--root", str(root), "--json"])
    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["kind"] == "note"
    assert payload["allowed"] is False
    (root / "notes.md").write_text("# Notes\n", encoding="utf-8")
    code = files_cli.main(["scan", "--root", str(root), "--json"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["count"] == 1
    code = files_cli.main(["list", "--root", str(root), "--json"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["count"] == 1


def test_record_file_dedupes_and_stores_no_content(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    note = root / "notes.md"
    note.write_text("# Notes\n\nsecret content\n", encoding="utf-8")
    item = F.record_file(root, "notes.md", session="ses-1", harness="claude",
                         task="notes.write", tool="write",
                         reason="one-off note")
    assert item is not None
    assert item["path"] == "notes.md"
    assert item["classification"] == "note"
    assert item["created_by"] == {"session": "ses-1", "harness": "claude",
                                  "task": "notes.write", "tool": "write",
                                  "command": None}
    assert item["reason"] == "one-off note"
    ledger = F.ledger_path(root).read_text(encoding="utf-8")
    assert "secret content" not in ledger
    # Same path and hash: no second row.
    assert F.record_file(root, "notes.md", session="ses-1",
                         reason="again") is None
    assert len(F._read_ledger(root)) == 1
    # A changed file is a new record.
    note.write_text("# Notes\n\nchanged\n", encoding="utf-8")
    again = F.record_file(root, "notes.md", session="ses-1",
                          reason="changed note")
    assert again is not None and again["hash"] != item["hash"]
    assert F.explain(root, "notes.md")["record"]["reason"] == "changed note"


def test_record_paths_skips_ignored_outside_and_missing(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    (root / "a.py").write_text("x = 1\n", encoding="utf-8")
    (root / ".ozagia").mkdir()
    (root / ".ozagia" / "state.txt").write_text("x", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("x", encoding="utf-8")
    got = F.record_paths(
        root, ["a.py", ".ozagia/state.txt", outside, "missing.py"],
        session="ses-2", harness="codex", tool="edit", reason="line\nbreak")
    assert [item["path"] for item in got] == ["a.py"]
    assert got[0]["reason"] == "line break"
    assert F.record_file(root, "a.py", reason="x") is None


def test_scan_new_files_paths_and_limit(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    (root / "one.md").write_text("# One\n", encoding="utf-8")
    (root / "two.md").write_text("# Two\n", encoding="utf-8")
    got = F.scan_new_files(root, session="s", paths=["one.md"])
    assert [item["path"] for item in got] == ["one.md"]
    assert F.scan_new_files(root, session="s", paths=[]) == []
    got = F.scan_new_files(root, session="s", limit=1)
    assert [item["path"] for item in got] == ["two.md"]
    assert F.scan_new_files(root, session="s", limit=10) == []
