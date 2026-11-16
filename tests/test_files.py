"""Created-file ledger, Markdown routing, session manifests and cleanup."""
from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

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


# -- session manifests ------------------------------------------------------ #
def test_session_manifest_attributes_only_new_files(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    pre = root / "pre.md"
    pre.write_text("# Pre-existing\n\nuntracked secret\n", encoding="utf-8")
    manifest = F.session_snapshot(root, "ses-a", harness="claude")
    assert manifest["count"] == 2
    assert manifest["files"]["README.md"]["status"] == "tracked"
    assert manifest["files"]["pre.md"]["status"] == "untracked"
    assert manifest["files"]["pre.md"]["hash_state"] == "hashed"
    path = F.session_manifest_path(root, "ses-a", harness="claude")
    assert path.exists()
    assert ".ozagia/files/sessions" in path.as_posix()
    stored = path.read_text(encoding="utf-8")
    assert "untracked secret" not in stored
    (root / "new.md").write_text("# New\n\nnew secret\n", encoding="utf-8")
    delta = F.session_delta(root, "ses-a", harness="claude")
    assert delta["baseline"] == "manifest" and delta["attribution"] is True
    assert [item["path"] for item in delta["new"]] == ["new.md"]
    assert delta["counts"]["new"] == 1 and delta["counts"]["recorded"] == 1
    # The pre-existing untracked file is not recorded or attributed.
    assert F.explain(root, "pre.md")["record"] is None
    record = F.explain(root, "new.md")["record"]
    assert record is not None
    assert record["created_by"]["session"] == "ses-a"
    assert record["change"] == "new" and record["attribution"] is True
    assert "new secret" not in F.ledger_path(root).read_text(encoding="utf-8")
    # A closed session is not recorded twice when the end event is replayed.
    replay = F.session_delta(root, "ses-a", harness="claude")
    assert replay["replayed"] is True and replay["recorded"] == []


def test_session_delta_without_baseline_is_unattributed(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    (root / "orphan.md").write_text("# Orphan\n", encoding="utf-8")
    delta = F.session_delta(root, "ses-missing", harness="claude")
    assert delta["baseline"] == "missing"
    assert delta["attribution"] is False
    assert delta["counts"]["new"] == 1 and delta["counts"]["recorded"] == 1
    record = F.explain(root, "orphan.md")["record"]
    assert record is not None
    assert record["baseline"] == "missing"
    assert record["attribution"] is False
    assert record["created_by"]["session"] is None
    assert record["created_by"]["harness"] is None
    assert record["created_by"]["tool"] == "session_event"


def test_session_delta_moves_and_deletes_keep_metadata(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    (root / "old.md").write_text("# Move me\n", encoding="utf-8")
    (root / "gone.md").write_text("# Delete me\n", encoding="utf-8")
    F.session_snapshot(root, "ses-move", harness="opencode")
    (root / "old.md").rename(root / "moved.md")
    (root / "gone.md").unlink()
    delta = F.session_delta(root, "ses-move", harness="opencode")
    assert delta["moved"] == [{
        "from": "old.md", "to": "moved.md",
        "size": (root / "moved.md").stat().st_size,
        "hash": F._hash_file(root / "moved.md")[0],
    }]
    assert [item["path"] for item in delta["deleted"]] == ["gone.md"]
    assert delta["new"] == []
    moved = F.explain(root, "moved.md")["record"]
    assert moved is not None
    assert moved["change"] == "moved" and moved["moved_from"] == "old.md"
    # A pre-existing untracked file is moved, not created by the session.
    assert moved["created_by"]["session"] is None
    assert moved["attribution"] is False
    old = F.explain(root, "old.md")["record"]
    assert old is not None
    assert old["status"] == "moved" and old["moved_to"] == "moved.md"
    gone = F.explain(root, "gone.md")["record"]
    assert gone is not None and gone["status"] == "deleted"
    assert gone["attribution"] is False


def test_session_delta_changed_untracked_is_not_attributed(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    pre = root / "pre.md"
    pre.write_text("# Pre\n", encoding="utf-8")
    F.session_snapshot(root, "ses-change")
    pre.write_text("# Pre changed\n", encoding="utf-8")
    delta = F.session_delta(root, "ses-change")
    assert any(item["path"] == "pre.md" for item in delta["changed"])
    assert delta["new"] == []
    record = F.explain(root, "pre.md")["record"]
    assert record is not None
    assert record["change"] == "modified"
    assert record["created_by"]["session"] is None
    assert record["attribution"] is False


def test_session_manifest_notes_large_and_budgeted_files(tmp_path: Path,
                                                         monkeypatch) -> None:
    root = _repo(tmp_path)
    (root / "big.bin").write_text("0123456789", encoding="utf-8")
    monkeypatch.setattr(F, "MAX_HASH_BYTES", 4)
    manifest = F.session_snapshot(root, "ses-large")
    assert manifest["files"]["big.bin"]["hash"] is None
    assert manifest["files"]["big.bin"]["hash_state"] == "large"
    monkeypatch.setattr(F, "MAX_MANIFEST_HASH_BYTES", 0)
    (root / "tiny").write_text("x", encoding="utf-8")
    manifest = F.session_snapshot(root, "ses-bounded")
    assert manifest["files"]["big.bin"]["hash_state"] == "large"
    assert manifest["files"]["tiny"]["hash"] is None
    assert manifest["files"]["tiny"]["hash_state"] == "bounded"


def test_session_manifests_are_pruned(tmp_path: Path, monkeypatch) -> None:
    root = _repo(tmp_path)
    monkeypatch.setattr(F, "MAX_SESSION_MANIFESTS", 2)
    for index in range(4):
        F.session_snapshot(root, f"ses-{index}")
    directory = root / ".ozagia" / "files" / "sessions"
    assert len(list(directory.glob("*.json"))) == 2


# -- cleanup plan/apply/trash/restore --------------------------------------- #
def test_cleanup_token_expiry_replay_and_apply(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    note = root / "notes.md"
    note.write_text("# Notes\n\nsecret content\n", encoding="utf-8")
    F.record_file(root, "notes.md", session="ses-c", reason="one-off note")
    plan = F.cleanup_plan(root, older_than_days=0)
    token = plan["confirmation"]
    assert token and plan["dry_run"] is True
    assert [item["path"] for item in plan["candidates"]] == ["notes.md"]
    expired = F.cleanup_plan(root, older_than_days=0, ttl_s=0)
    with pytest.raises(F.FileLedgerError, match="expired"):
        F.cleanup_apply(root, confirmation=expired["confirmation"])
    with pytest.raises(F.FileLedgerError, match="invalid"):
        F.cleanup_apply(root, confirmation="not a token")
    result = F.cleanup_apply(root, confirmation=token)
    assert result["ok"] is True
    assert result["counts"] == {"applied": 1, "refused": 0}
    assert not note.exists()
    applied = result["applied"][0]
    assert applied["path"] == "notes.md"
    assert applied["trash_path"].startswith(".ozagia/trash/")
    assert (root / applied["trash_path"]).is_file()
    record = F.explain(root, "notes.md")["record"]
    assert record is not None
    assert record["status"] == "trashed"
    assert record["source_path"] == "notes.md"
    assert record["trash_path"] == applied["trash_path"]
    assert record["decision"] == "trashed"
    with pytest.raises(F.FileLedgerError, match="already used"):
        F.cleanup_apply(root, confirmation=token)
    # Metadata only: never file contents in the ledger or trash manifest.
    ledger = F.ledger_path(root).read_text(encoding="utf-8")
    manifest = (root / applied["trash_path"]).parent / "manifest.json"
    text = manifest.read_text(encoding="utf-8")
    assert "secret content" not in ledger
    assert "secret content" not in text
    assert json.loads(text)["kind"] == "trash_manifest"


def test_cleanup_refuses_unknown_until_forced(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    note = root / "unknown.md"
    note.write_text("# Unknown\n\nsecret\n", encoding="utf-8")
    F.record_file(root, "unknown.md")
    plan = F.cleanup_plan(root, older_than_days=0)
    result = F.cleanup_apply(root, confirmation=plan["confirmation"])
    assert result["ok"] is False
    assert result["counts"]["applied"] == 0
    assert result["refused"][0]["reason"].startswith("unknown origin")
    assert note.exists()
    assert F.explain(root, "unknown.md")["record"]["status"] == "active"
    plan = F.cleanup_plan(root, older_than_days=0)
    forced = F.cleanup_apply(root, confirmation=plan["confirmation"], force=True)
    assert forced["ok"] is True and forced["counts"]["applied"] == 1
    assert forced["applied"][0]["decision"] == "forced"
    record = F.explain(root, "unknown.md")["record"]
    assert record["status"] == "trashed" and record["decision"] == "forced"
    manifest_path = (root / forced["applied"][0]["trash_path"]).parent / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["entries"][0]["forced"] is True
    assert "secret" not in manifest_path.read_text(encoding="utf-8")


def test_cleanup_refuses_referenced_file(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    (root / "referenced.md").write_text("# Referenced\n", encoding="utf-8")
    F.record_file(root, "referenced.md", reason="linked note")
    plan = F.cleanup_plan(root, older_than_days=0)
    record = dict(F.latest_records(root)["referenced.md"])
    record.pop("recorded_at", None)
    record["references"] = ["doc:README.md"]
    F.append_record(root, record)
    result = F.cleanup_apply(root, confirmation=plan["confirmation"])
    assert result["ok"] is False
    assert "referenced" in result["refused"][0]["reason"]
    assert (root / "referenced.md").exists()
    # A dry-run without referenced files still lets an explicitly named
    # candidate be validated: it is refused for being referenced, not moved.
    plan = F.cleanup_plan(root, older_than_days=0)
    assert plan["candidates"] == []
    explicit = F.cleanup_apply(root, ["referenced.md"],
                               plan["confirmation"])
    assert explicit["ok"] is False
    assert "referenced" in explicit["refused"][0]["reason"]
    assert (root / "referenced.md").exists()
    # A plan that includes referenced files can move one only with force, and
    # records the forced decision in both ledger and trash manifest.
    plan = F.cleanup_plan(root, older_than_days=0, include_referenced=True)
    assert plan["candidates"][0]["recommended"] == "refuse"
    forced = F.cleanup_apply(root, confirmation=plan["confirmation"], force=True)
    assert forced["ok"] is True and forced["applied"][0]["forced"] is True
    assert forced["applied"][0]["references"] == ["doc:README.md"]
    assert F.explain(root, "referenced.md")["record"]["decision"] == "forced"


def test_cleanup_refuses_changed_symlink_and_dot_git(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    note = root / "changed.md"
    note.write_text("# One\n", encoding="utf-8")
    F.record_file(root, "changed.md", reason="note")
    plan = F.cleanup_plan(root, older_than_days=0)
    note.write_text("# Two\n", encoding="utf-8")
    result = F.cleanup_apply(root, confirmation=plan["confirmation"])
    assert result["ok"] is False
    assert result["refused"][0]["reason"] == "file changed since the plan"
    note.unlink()
    note.symlink_to(root / "README.md")
    plan = F.cleanup_plan(root, older_than_days=0)
    result = F.cleanup_apply(root, confirmation=plan["confirmation"], force=True)
    assert result["ok"] is False
    assert result["refused"][0]["reason"] == "symlink path refused"
    # Even force never moves `.git` or the state directory, even if a forged
    # ledger row mentions one.
    F.append_record(root, {
        "schema": F.SCHEMA, "path": ".git/config", "status": "active",
        "classification": "other", "reason": "unknown", "size": 0,
        "hash": None, "references": [], "created_at": F._now(),
        "created_by": {"session": None, "harness": None, "task": None,
                       "tool": None, "command": None},
        "cleanup": {"policy": "ask", "after": None},
    })
    plan = F.cleanup_plan(root, older_than_days=0)
    result = F.cleanup_apply(root, confirmation=plan["confirmation"], force=True)
    assert any(item["path"] == ".git/config"
               and "refusing to move .git" in item["reason"]
               for item in result["refused"])
    assert (root / ".git" / "config").exists()


def test_cleanup_existing_trash_destination_refused(tmp_path: Path,
                                                    monkeypatch) -> None:
    root = _repo(tmp_path)
    note = root / "notes.md"
    note.write_text("# Notes\n", encoding="utf-8")
    F.record_file(root, "notes.md", reason="note")
    monkeypatch.setattr(F, "_trash_run_id",
                        lambda: "20260101T000000000000Z")
    destination = root / ".ozagia" / "trash" / "20260101T000000000000Z" / "notes.md"
    destination.parent.mkdir(parents=True)
    destination.write_text("existing\n", encoding="utf-8")
    plan = F.cleanup_plan(root, older_than_days=0)
    result = F.cleanup_apply(root, confirmation=plan["confirmation"])
    assert result["ok"] is False
    assert result["refused"][0]["reason"] == "trash destination already exists"
    assert note.exists() and destination.read_text(encoding="utf-8") == "existing\n"


def test_trash_list_restore_and_conflict(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    note = root / "notes.md"
    note.write_text("# Notes\n\nrestore me\n", encoding="utf-8")
    F.record_file(root, "notes.md", session="ses-r", reason="one-off")
    plan = F.cleanup_plan(root, older_than_days=0)
    applied = F.cleanup_apply(root, confirmation=plan["confirmation"])
    trash_id = applied["trash_id"]
    listing = F.list_trash(root)
    assert listing["count"] == 1
    assert listing["trash"][0]["trash_id"] == trash_id
    assert listing["trash"][0]["entries"][0]["path"] == "notes.md"
    assert listing["trash"][0]["restored"] == 0
    # A destination conflict is refused and the trash copy is kept.
    note.write_text("# Different\n", encoding="utf-8")
    conflict = F.restore(root, trash_id)
    assert conflict["ok"] is False
    assert conflict["refused"][0]["reason"] == "destination already exists"
    assert (root / applied["applied"][0]["trash_path"]).is_file()
    note.unlink()
    restored = F.restore(root, trash_id)
    assert restored["ok"] is True and restored["counts"]["restored"] == 1
    assert note.read_text(encoding="utf-8") == "# Notes\n\nrestore me\n"
    record = F.explain(root, "notes.md")["record"]
    assert record["status"] == "active"
    assert record["decision"] == "restore"
    assert record["restored_from"] == applied["applied"][0]["trash_path"]
    assert record["restored_to"] == "notes.md"
    after = F.list_trash(root)
    assert after["trash"][0]["restored"] == 1
    assert F.restore(root, trash_id)["ok"] is False
    # The trash manifest still holds metadata only.
    manifest = (root / applied["applied"][0]["trash_path"]).parent / "manifest.json"
    assert "restore me" not in manifest.read_text(encoding="utf-8")


def test_files_cli_cleanup_trash_restore(tmp_path: Path, capsys) -> None:
    root = _repo(tmp_path)
    note = root / "notes.md"
    note.write_text("# Notes\n", encoding="utf-8")
    F.record_file(root, "notes.md", reason="one-off")
    code = files_cli.main([
        "cleanup", "--older-than-days", "0", "--root", str(root), "--json"])
    assert code == 0
    plan = json.loads(capsys.readouterr().out)
    token = plan["confirmation"]
    code = files_cli.main([
        "cleanup", "--apply", "--confirm", token, "--root", str(root), "--json"])
    assert code == 0
    applied = json.loads(capsys.readouterr().out)
    assert applied["counts"]["applied"] == 1
    assert not note.exists()
    code = files_cli.main(["trash", "list", "--root", str(root), "--json"])
    assert code == 0
    listing = json.loads(capsys.readouterr().out)
    assert listing["count"] == 1
    trash_id = listing["trash"][0]["trash_id"]
    code = files_cli.main(["restore", trash_id, "--root", str(root), "--json"])
    assert code == 0
    restored = json.loads(capsys.readouterr().out)
    assert restored["counts"]["restored"] == 1
    assert note.is_file()
    # Apply without the one-time token is refused.
    code = files_cli.main(["cleanup", "--apply", "--root", str(root)])
    assert code == 2


# -- trash retention and explicit purge ------------------------------------- #
def _trash_files(root: Path, monkeypatch, trash_id: str,
                 *paths: str) -> dict:
    """Create one trash run with real cleanup manifests."""
    for rel in paths:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# {rel}\n", encoding="utf-8")
        F.record_file(root, rel, reason="one-off note")
    monkeypatch.setattr(F, "_trash_run_id", lambda: trash_id)
    plan = F.cleanup_plan(root, older_than_days=0)
    applied = F.cleanup_apply(root, confirmation=plan["confirmation"])
    assert applied["ok"] is True
    assert applied["trash_id"] == trash_id
    return applied


def _set_trash_age(root: Path, trash_id: str, days: int) -> None:
    path = root / ".ozagia" / "trash" / trash_id / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["created_at"] = (
        datetime.now(timezone.utc) - timedelta(days=days)
    ).isoformat(timespec="seconds")
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8")


def test_trash_purge_dry_run_token_expiry_and_replay(tmp_path: Path,
                                                     monkeypatch) -> None:
    root = _repo(tmp_path)
    old = _trash_files(root, monkeypatch, "20260101T000000000000Z", "old.md")
    recent = _trash_files(root, monkeypatch, "20260102T000000000000Z",
                          "recent.md")
    _set_trash_age(root, old["trash_id"], 40)
    _set_trash_age(root, recent["trash_id"], 1)
    old_path = root / old["applied"][0]["trash_path"]
    recent_path = root / recent["applied"][0]["trash_path"]

    plan = F.trash_purge(root, older_than_days=30)
    assert plan["dry_run"] is True
    assert plan["older_than_days"] == 30
    assert plan["retention_days"] == F.DEFAULT_TRASH_RETENTION_DAYS
    assert [run["trash_id"] for run in plan["candidates"]] == [old["trash_id"]]
    assert plan["counts"]["entries"] == 1
    assert any(item["trash_id"] == recent["trash_id"]
               and "day(s) old" in item["reason"]
               for item in plan["skipped"])
    # A dry run never deletes anything, including with no token at all.
    assert old_path.is_file() and recent_path.is_file()
    assert not (root / ".ozagia" / "trash" / "audit.jsonl").exists()

    expired = F.trash_purge(root, older_than_days=30, ttl_s=0)
    with pytest.raises(F.FileLedgerError, match="expired"):
        F.trash_purge(root, confirmation=expired["confirmation"])
    with pytest.raises(F.FileLedgerError, match="invalid"):
        F.trash_purge(root, confirmation="not a token")
    assert old_path.is_file() and recent_path.is_file()

    plan = F.trash_purge(root, older_than_days=30)
    result = F.trash_purge(root, confirmation=plan["confirmation"])
    assert result["ok"] is True
    assert result["counts"]["purged"] == 1
    assert result["counts"]["refused"] == 0
    assert result["counts"]["bytes"] == old["applied"][0]["size"]
    assert not old_path.exists()
    assert recent_path.is_file()
    assert not old_path.parent.exists()
    assert plan["candidates"][0]["remove_run"] is True
    with pytest.raises(F.FileLedgerError, match="already used"):
        F.trash_purge(root, confirmation=plan["confirmation"])

    audit = root / ".ozagia" / "trash" / "audit.jsonl"
    rows = [json.loads(line) for line in
            audit.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    assert rows[0]["kind"] == "trash_purge"
    assert rows[0]["path"] == "old.md"
    assert rows[0]["trash_id"] == old["trash_id"]
    assert rows[0]["older_than_days"] == 30
    assert "# old.md" not in audit.read_text(encoding="utf-8")


def test_trash_purge_uses_retention_setting(tmp_path: Path,
                                            monkeypatch) -> None:
    root = _repo(tmp_path)
    old = _trash_files(root, monkeypatch, "20260103T000000000000Z", "old.md")
    recent = _trash_files(root, monkeypatch, "20260104T000000000000Z",
                          "recent.md")
    _set_trash_age(root, old["trash_id"], 4)
    _set_trash_age(root, recent["trash_id"], 1)
    config = root / "ozagia"
    config.mkdir()
    (config / "config.toml").write_text(
        "[files]\nretention_days = 2\n", encoding="utf-8")

    assert F.trash_retention_days(root) == 2
    listing = F.list_trash(root)
    assert listing["retention_days"] == 2
    states = {run["trash_id"]: run for run in listing["trash"]}
    assert states[old["trash_id"]]["retention_state"] == "expired"
    assert states[old["trash_id"]]["age_days"] == 4
    assert states[old["trash_id"]]["purgeable"] is True
    assert states[recent["trash_id"]]["retention_state"] == "retained"
    assert states[recent["trash_id"]]["purgeable"] is False

    plan = F.trash_purge(root)
    assert plan["older_than_days"] == 2
    assert [run["trash_id"] for run in plan["candidates"]] == [old["trash_id"]]
    # An explicit age overrides the setting.
    plan = F.trash_purge(root, older_than_days=0)
    assert {run["trash_id"] for run in plan["candidates"]} == {
        old["trash_id"], recent["trash_id"]}


def test_trash_purge_leaves_restored_files_and_entries(tmp_path: Path,
                                                       monkeypatch) -> None:
    root = _repo(tmp_path)
    applied = _trash_files(root, monkeypatch, "20260105T000000000000Z",
                           "restored.md", "remaining.md")
    _set_trash_age(root, applied["trash_id"], 40)
    restored = F.restore(root, applied["trash_id"], paths=["restored.md"])
    assert restored["ok"] is True
    assert (root / "restored.md").read_text(encoding="utf-8") == "# restored.md\n"

    plan = F.trash_purge(root, older_than_days=30)
    assert len(plan["candidates"]) == 1
    run = plan["candidates"][0]
    assert run["restored"] == 1
    assert [entry["path"] for entry in run["entries"]] == ["remaining.md"]
    result = F.trash_purge(root, confirmation=plan["confirmation"])
    assert result["counts"]["purged"] == 1
    assert (root / "restored.md").read_text(encoding="utf-8") == "# restored.md\n"
    assert not (root / applied["applied"][1]["trash_path"]).exists()

    manifest = json.loads(
        (root / applied["applied"][0]["trash_path"]).parent.joinpath(
            "manifest.json").read_text(encoding="utf-8"))
    entries = {entry["path"]: entry for entry in manifest["entries"]}
    assert entries["restored.md"]["restored_at"]
    assert "purged_at" not in entries["restored.md"]
    assert entries["remaining.md"]["purged_at"]
    assert entries["remaining.md"]["decision"] == "purged"

    listing = F.list_trash(root)
    run = listing["trash"][0]
    assert run["restored"] == 1 and run["purged"] == 1
    assert run["remaining_bytes"] == 0
    audit = root / ".ozagia" / "trash" / "audit.jsonl"
    rows = [json.loads(line) for line in
            audit.read_text(encoding="utf-8").splitlines()]
    assert [row["path"] for row in rows] == ["remaining.md"]


def test_trash_purge_refuses_escape_symlink_and_unknown(tmp_path: Path,
                                                        monkeypatch) -> None:
    root = _repo(tmp_path)
    applied = _trash_files(root, monkeypatch, "20260106T000000000000Z",
                           "safe.md")
    _set_trash_age(root, applied["trash_id"], 40)
    run_dir = root / ".ozagia" / "trash" / applied["trash_id"]
    old_path = root / applied["applied"][0]["trash_path"]
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_text("outside\n", encoding="utf-8")
    (run_dir / "link.md").symlink_to(root / "README.md")
    manifest_path = run_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["entries"].extend([
        {"path": "link.md", "trash_path":
            f".ozagia/trash/{applied['trash_id']}/link.md", "size": 7},
        {"path": "README.md", "trash_path":
            f".ozagia/trash/{applied['trash_id']}/../../../README.md",
         "size": 7},
        {"path": ".git/config", "trash_path":
            f".ozagia/trash/{applied['trash_id']}/config", "size": 0},
        {"path": "secret.txt", "trash_path": str(secret),
         "size": secret.stat().st_size},
    ])
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    (root / ".ozagia" / "trash" / "evil").symlink_to(outside,
                                                     target_is_directory=True)

    plan = F.trash_purge(root, older_than_days=30)
    assert [entry["path"] for entry in plan["candidates"][0]["entries"]] == [
        "safe.md"]
    result = F.trash_purge(root, confirmation=plan["confirmation"])
    assert result["counts"]["purged"] == 1
    assert not old_path.exists()
    assert (root / "README.md").is_file()
    assert (root / ".git" / "config").is_file()
    assert secret.read_text(encoding="utf-8") == "outside\n"
    assert (run_dir / "link.md").is_symlink()

    # A trash root that is itself a symlink is refused, not followed.
    trash = root / ".ozagia" / "trash"
    real = tmp_path / "real-trash"
    trash.rename(real)
    trash.symlink_to(real, target_is_directory=True)
    with pytest.raises(F.FileLedgerError, match="symlink"):
        F.trash_purge(root, older_than_days=0)


def test_files_cli_trash_purge(tmp_path: Path, monkeypatch,
                               capsys) -> None:
    root = _repo(tmp_path)
    applied = _trash_files(root, monkeypatch, "20260107T000000000000Z",
                           "notes.md")
    _set_trash_age(root, applied["trash_id"], 40)
    trashed = root / applied["applied"][0]["trash_path"]

    code = files_cli.main([
        "trash", "purge", "--root", str(root), "--json"])
    assert code == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["dry_run"] is True
    assert plan["counts"]["entries"] == 1
    assert trashed.is_file()
    token = plan["confirmation"]

    code = files_cli.main(["trash", "list", "--root", str(root)])
    assert code == 0
    text = capsys.readouterr().out
    assert "retention=expired" in text
    assert "in trash" in text

    code = files_cli.main([
        "trash", "purge", "--confirm", token, "--root", str(root), "--json"])
    assert code == 0
    result = json.loads(capsys.readouterr().out)
    assert result["counts"]["purged"] == 1
    assert not trashed.exists()
    assert not trashed.parent.exists()
    assert (root / ".ozagia" / "trash" / "audit.jsonl").is_file()

    code = files_cli.main(["trash", "list", "--root", str(root)])
    assert code == 0
    assert capsys.readouterr().out.strip() == "trash is empty"

    code = files_cli.main([
        "trash", "purge", "--confirm", token, "--root", str(root), "--json"])
    assert code == 2
