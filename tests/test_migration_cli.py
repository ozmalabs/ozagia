"""``ozagia migration`` CLI: status, tasks, scaffold preview and write."""
from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from ozagia import cli
from ozagia.config import clear_config_cache
from ozagia.migration_cli import main as migration_main

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "py_rust_migration"


def _copy(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    shutil.copytree(FIXTURE, root)
    clear_config_cache()
    return root


def _json(capsys) -> dict:
    out = capsys.readouterr()
    return json.loads(out.out)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_cli_status_json_and_markdown(tmp_path: Path, capsys) -> None:
    root = _copy(tmp_path)
    assert cli.main(["migration", "status", "--root", str(root),
                     "--json", "--no-record"]) == 0
    payload = _json(capsys)
    assert payload["configured"] is True
    assert payload["status_counts"]["ported"] == 1
    assert payload["status_counts"]["planned"] == 7
    assert payload["written"] == {"json": None, "markdown": None}

    assert cli.main(["migration", "status", "--root", str(root),
                     "--markdown", "--no-record"]) == 0
    text = capsys.readouterr().out
    assert "# Python→Rust migration status" in text
    assert "## Top gaps (planned)" in text


def test_cli_status_records_under_state_dir(tmp_path: Path, capsys) -> None:
    root = _copy(tmp_path)
    assert cli.main(["migration", "status", "--root", str(root),
                     "--json"]) == 0
    payload = _json(capsys)
    assert payload["freshness"]["state"] == "unknown"
    assert (root / ".ozagia" / "migration" / "status.json").is_file()
    assert cli.main(["migration", "status", "--root", str(root),
                     "--json"]) == 0
    assert _json(capsys)["freshness"]["state"] == "fresh"


def test_cli_status_broken_config_exits_2(tmp_path: Path, capsys) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".ozagia.toml").write_text(
        '[migration]\npython = "missing"\nrust = "."\n', encoding="utf-8")
    clear_config_cache()
    assert cli.main(["migration", "status", "--root", str(root)]) == 2
    assert "does not exist" in capsys.readouterr().err


def test_cli_tasks_is_a_checklist_without_goals(tmp_path: Path, capsys) -> None:
    root = _copy(tmp_path)
    assert migration_main(["tasks", "--root", str(root), "--json",
                           "--limit", "2"]) == 0
    payload = _json(capsys)
    assert payload["created_goals"] is False
    assert payload["executed"] is False
    assert payload["counts"] == {"eligible": 10, "returned": 2,
                                 "truncated": True}
    first = payload["tasks"][0]
    assert first["python"] == "fixturepy.models.Colour"
    criteria = {item["criterion"] for item in first["success_criteria"]}
    assert criteria == {"counterpart_exists", "parity_flags_clear",
                        "mapped_tests_pass"}
    assert not (root / ".ozagia" / "goals").exists()

    assert migration_main(["tasks", "--root", str(root), "--limit", "1"]) == 0
    text = capsys.readouterr().out
    assert "- [ ] **fixturepy.models.Colour**" in text
    assert "created_goals: false; executed: false" in text


def test_scaffold_preview_writes_nothing(tmp_path: Path, capsys) -> None:
    root = _copy(tmp_path)
    target = root / "rust" / "src" / "service.rs"
    before = _sha(target)
    assert migration_main([
        "scaffold", "fixturepy.service.normalize_name",
        "--target", "rust/src/service.rs", "--root", str(root), "--json",
    ]) == 0
    payload = _json(capsys)
    assert payload["applied"] is False
    assert payload["preview"] is True
    assert payload["written"] is False
    assert "pub fn normalize_name(name: String) -> String" in payload["candidate"]
    assert _sha(target) == before
    assert not (root / ".ozagia" / "edits" / "backups").exists()


def test_scaffold_write_uses_the_transaction(tmp_path: Path, capsys) -> None:
    root = _copy(tmp_path)
    target = root / "rust" / "src" / "service.rs"
    assert migration_main([
        "scaffold", "fixturepy.service.normalize_name",
        "--target", "rust/src/service.rs", "--root", str(root), "--json",
        "--write",
    ]) == 0
    payload = _json(capsys)
    assert payload["applied"] is True
    assert payload["written"] is True
    assert payload["backup"]
    assert Path(payload["backup"]).is_file()
    text = target.read_text(encoding="utf-8")
    assert "pub fn normalize_name(name: String) -> String" in text
    assert "todo!(\"port from fixturepy.service.normalize_name\")" in text
    assert (root / ".ozagia" / "edits" / "backups").is_dir()

    # The duplicate check refuses a second scaffold of the same symbol.
    before = _sha(target)
    assert migration_main([
        "scaffold", "fixturepy.service.normalize_name",
        "--target", "rust/src/service.rs", "--root", str(root),
    ]) == 2
    assert "already defines" in capsys.readouterr().err
    assert _sha(target) == before


@pytest.mark.parametrize("symbol, reason", [
    ("fixturepy.models.ProtocolThing", "protocol"),
    ("fixturepy.models.Colour", "enum"),
    ("fixturepy.models.parse_pending", "not one of the simple scaffold types"),
    ("fixturepy.service.display", "no documented mapping"),
    ("fixturepy.service.cached_normalize", "decorated"),
    ("fixturepy.models.flush_cache", "default parameter values"),
])
def test_scaffold_refusals_are_explained(tmp_path: Path, capsys,
                                        symbol: str, reason: str) -> None:
    root = _copy(tmp_path)
    target = root / "rust" / "src" / "service.rs"
    before = _sha(target)
    assert migration_main([
        "scaffold", symbol, "--target", "rust/src/service.rs",
        "--root", str(root),
    ]) == 2
    assert reason in capsys.readouterr().err
    assert _sha(target) == before
    assert not (root / ".ozagia" / "edits" / "backups").exists()


def test_scaffold_refuses_rust_keywords(tmp_path: Path, capsys) -> None:
    root = _copy(tmp_path)
    (root / "python" / "fixturepy" / "keywords.py").write_text(
        "def match(x: int) -> int:\n    return x\n", encoding="utf-8")
    assert migration_main([
        "scaffold", "fixturepy.keywords.match",
        "--target", "rust/src/service.rs", "--root", str(root),
    ]) == 2
    assert "not a Rust identifier" in capsys.readouterr().err


def test_scaffold_target_and_symbol_errors(tmp_path: Path, capsys) -> None:
    root = _copy(tmp_path)
    assert migration_main([
        "scaffold", "fixturepy.service.normalize_name",
        "--target", "python/fixturepy/service.py", "--root", str(root),
    ]) == 2
    assert ".rs" in capsys.readouterr().err
    assert migration_main([
        "scaffold", "fixturepy.service.normalize_name",
        "--target", "rust/src/new_file.rs", "--root", str(root),
    ]) == 2
    assert "must already exist" in capsys.readouterr().err
    assert migration_main([
        "scaffold", "fixturepy.service.normalize_name",
        "--target", "rust/src/service.rs", "--root", str(root),
        "--base-sha", "0" * 64,
    ]) == 2
    assert "stale base" in capsys.readouterr().err
    assert migration_main([
        "scaffold", "fixturepy.models.DoesNotExist",
        "--target", "rust/src/service.rs", "--root", str(root),
    ]) == 2
    assert "no public Python symbol" in capsys.readouterr().err


def test_scaffold_reports_hints_and_marks_them_non_compilable(
        tmp_path: Path, capsys) -> None:
    root = _copy(tmp_path)
    assert migration_main([
        "scaffold", "fixturepy.models.User", "--target", "rust/src/lib.rs",
        "--root", str(root), "--json",
    ]) == 0
    payload = _json(capsys)
    hints = {item["id"]: item for item in payload["hints"]}
    assert hints["dataclass"]["support"] == "supported"
    assert hints["str"]["choice"] is True
    assert all(item["compilable"] is False for item in payload["hints"])
    assert payload["parse_gate"]["backend"] == "tree-sitter"
