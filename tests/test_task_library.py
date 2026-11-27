"""The built-in task-class library, repository overrides and the copy CLI."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ozagia import task_library_cli
from ozagia.core import task_library as L
from ozagia.core import tasks as T

RESEARCH = {
    "paper_triage", "claim_extract", "reference_screen", "evidence_table",
    "theory_compare", "manuscript_check",
}
CODING = {"diff_review", "test_triage", "impact_analysis", "repo_scan",
          "review_my_change", "ocelot_workspace"}
OPS = {"log_inspect", "model_classify", "config_validate"}


def _override_text(task_id: str, *, version: int | None = None,
                   title: str | None = None,
                   new_id: str | None = None) -> str:
    task = L.get_task(task_id)
    text = task.read_text()
    if version is not None:
        text = text.replace(f"version = {task.version}",
                            f"version = {version}", 1)
    if title is not None:
        text = text.replace(f'title = "{task.spec.title}"',
                            f'title = "{title}"', 1)
    if new_id is not None:
        text = text.replace(f'id = "{task.id}"', f'id = "{new_id}"', 1)
    return text


def _write_override(root: Path, task_id: str, *, version: int | None = None,
                    title: str | None = None,
                    filename: str | None = None) -> Path:
    path = root / "ozagia" / "tasks" / (filename or f"{task_id}.toml")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_override_text(task_id, version=version, title=title),
                    encoding="utf-8")
    return path


def _write_unknown(root: Path, task_id: str = "not_a_task") -> Path:
    path = root / "ozagia" / "tasks" / f"{task_id}.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_override_text("paper_triage", new_id=task_id),
                    encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# built-ins
# --------------------------------------------------------------------------- #
def test_every_builtin_validates_through_core_tasks() -> None:
    paths = sorted(L.builtin_dir().rglob("*.toml"))
    assert len(paths) == 15
    seen: set[str] = set()
    for path in paths:
        spec = T.load_spec(path)
        assert spec.id == path.stem
        assert spec.id not in seen
        seen.add(spec.id)
        assert spec.title
        assert spec.objective
        assert spec.steps
        assert spec.output
        assert spec.output_schema is not None
        assert spec.approval.network == "deny"
        assert spec.budget.model_calls == 0
        assert "model" not in spec.tools
        assert path.parent.name in L.DOMAINS
    assert seen == RESEARCH | CODING | OPS


def test_domains_and_ids_are_unique() -> None:
    catalog = L.catalog()
    assert [domain["id"] for domain in catalog["domains"]] == [
        "research", "coding", "ops"]
    assert [domain["count"] for domain in catalog["domains"]] == [6, 6, 3]
    assert set(catalog["domains"][0]["task_ids"]) == RESEARCH
    assert set(catalog["domains"][1]["task_ids"]) == CODING
    assert set(catalog["domains"][2]["task_ids"]) == OPS
    ids = [task["id"] for task in catalog["tasks"]]
    assert len(ids) == len(set(ids)) == 15
    assert catalog["counts"] == {"builtin": 15, "repo": 0, "total": 15}


def test_review_my_change_is_discoverable_and_offline_read_only() -> None:
    task = L.get_task("review_my_change")

    assert task.domain == "coding"
    assert task.source == "builtin"
    assert task.spec.inputs == ("diff",)
    assert set(task.spec.tools) == {"read", "grep", "graph_search"}
    assert task.spec.requires_graph is True
    assert task.spec.write_scope == ()
    assert task.spec.budget.model_calls == 0
    assert task.spec.approval.mutate == "deny"
    assert task.spec.approval.network == "deny"
    assert task.spec.output == "change_review.v1"
    assert "not_assessed" in task.spec.output_schema["required"]


def test_list_show_and_catalog_metadata() -> None:
    tasks = L.list_tasks()
    assert len(tasks) == 15
    assert [task["id"] for task in tasks if task["domain"] == "coding"] == [
        "diff_review", "impact_analysis", "ocelot_workspace", "repo_scan",
        "review_my_change", "test_triage"]

    coding = L.list_tasks(domain="coding")
    assert {task["id"] for task in coding} == CODING
    with pytest.raises(L.TaskLibraryError, match="unknown task domain"):
        L.list_tasks(domain="other")

    task = L.get_task("diff_review")
    assert task.domain == "coding"
    assert task.source == "builtin"
    assert task.version == 1
    assert task.overrides is False
    entry = task.entry()
    assert entry["defaults"]["budget"]["tokens"] == 40000
    assert entry["defaults"]["resources"]["memory_mb"] == 1024
    assert entry["defaults"]["approval"] == {"mutate": "deny",
                                             "network": "deny"}
    assert entry["defaults"]["output"] == "review_packet.v1"
    assert entry["defaults"]["write_scope"] == []
    assert entry["defaults"]["requires_graph"] is True
    assert task.to_dict()["spec"]["id"] == "diff_review"

    with pytest.raises(L.TaskLibraryError, match="unknown task class"):
        L.get_task("no_such_task")

    catalog = L.catalog()
    assert catalog["schema"] == L.SCHEMA
    assert catalog["root"] is None
    assert catalog["counts"]["total"] == 15
    assert "evidence_table" in {
        task["id"] for task in catalog["tasks"]
        if task["domain"] == "research"
    }
    first = json.dumps(L.catalog(), sort_keys=True)
    second = json.dumps(L.catalog(), sort_keys=True)
    assert first == second


def test_builtin_issue_codes_are_structured() -> None:
    report = L.validate_library()
    assert report["ok"] is True
    assert report["errors"] == []
    assert report["counts"] == {"builtin": 15, "repo": 0, "total": 15}
    assert all(issue["severity"] == "warning" for issue in report["issues"])
    assert any("approval.mutate" in warning for warning in report["warnings"])


# --------------------------------------------------------------------------- #
# repository overrides
# --------------------------------------------------------------------------- #
def test_repo_override_replaces_builtin(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _write_override(root, "paper_triage", version=2,
                    title="Paper triage (local)")
    report = L.validate_library(root)
    assert report["ok"], report
    assert report["counts"] == {"builtin": 15, "repo": 1, "total": 15}

    task = L.get_task("paper_triage", root=root)
    assert task.source == "repo"
    assert task.domain == "research"
    assert task.version == 2
    assert task.overrides is True
    assert task.builtin_version == 1
    assert task.spec.title == "Paper triage (local)"

    listed = {entry["id"]: entry for entry in L.list_tasks(root)}
    assert listed["paper_triage"]["source"] == "repo"
    assert listed["paper_triage"]["version"] == 2
    assert listed["paper_triage"]["title"] == "Paper triage (local)"
    assert listed["claim_extract"]["source"] == "builtin"

    catalog = L.catalog(root)
    assert catalog["counts"] == {"builtin": 15, "repo": 1, "total": 15}
    assert catalog["domains"][0]["count"] == 6


def test_identical_override_is_allowed(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _write_override(root, "diff_review")
    report = L.validate_library(root)
    assert report["ok"], report
    task = L.get_task("diff_review", root=root)
    assert task.source == "repo"
    assert task.version == 1
    assert task.spec.digest == L.get_task("diff_review").spec.digest


def test_unknown_override_is_an_error(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _write_unknown(root)
    report = L.validate_library(root)
    assert report["ok"] is False
    assert any("does not override a built-in" in error
               for error in report["errors"])
    assert any(issue["code"] == "unknown_override"
               for issue in report["issues"])
    with pytest.raises(L.TaskLibraryError) as excinfo:
        L.list_tasks(root)
    assert excinfo.value.errors
    with pytest.raises(L.TaskLibraryError):
        L.get_task("not_a_task", root=root)


def test_duplicate_override_is_an_error(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _write_override(root, "paper_triage", version=2, title="First",
                    filename="paper_triage_a.toml")
    _write_override(root, "paper_triage", version=2, title="Second",
                    filename="paper_triage_b.toml")
    report = L.validate_library(root)
    assert report["ok"] is False
    assert any("duplicate repository override" in error
               for error in report["errors"])
    assert any(issue["code"] == "duplicate_override"
               for issue in report["issues"])


def test_same_version_with_different_content_is_a_conflict(
        tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _write_override(root, "paper_triage", version=1, title="Edited")
    report = L.validate_library(root)
    assert report["ok"] is False
    assert any("conflicts with built-in" in error
               for error in report["errors"])
    assert any(issue["code"] == "version_conflict"
               for issue in report["issues"])
    # The built-in remains available when the override is refused.
    task = next(entry for entry in report["tasks"]
                if entry["id"] == "paper_triage")
    assert task["source"] == "builtin"
    assert task["version"] == 1


def test_older_override_is_stale(tmp_path: Path, monkeypatch: pytest.MonkeyPatch
                                 ) -> None:
    builtins = tmp_path / "builtins"
    (builtins / "research").mkdir(parents=True)
    text = _override_text("paper_triage", version=2)
    (builtins / "research" / "paper_triage.toml").write_text(
        text, encoding="utf-8")
    monkeypatch.setattr(L, "builtin_dir", lambda: builtins)

    root = tmp_path / "repo"
    root.mkdir()
    _write_override(root, "paper_triage", version=1)
    report = L.validate_library(root)
    assert report["ok"] is False
    assert any(issue["code"] == "stale_override"
               for issue in report["issues"])
    assert any("older than built-in" in error for error in report["errors"])


def test_duplicate_builtin_is_an_error(tmp_path: Path,
                                       monkeypatch: pytest.MonkeyPatch) -> None:
    builtins = tmp_path / "builtins"
    (builtins / "research").mkdir(parents=True)
    (builtins / "coding").mkdir(parents=True)
    text = _override_text("paper_triage")
    (builtins / "research" / "paper_triage.toml").write_text(
        text, encoding="utf-8")
    (builtins / "coding" / "paper_triage.toml").write_text(
        text, encoding="utf-8")
    monkeypatch.setattr(L, "builtin_dir", lambda: builtins)

    report = L.validate_library()
    assert report["ok"] is False
    assert any(issue["code"] == "duplicate_builtin"
               for issue in report["issues"])


def test_invalid_override_reports_spec_errors(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    path = root / "ozagia" / "tasks" / "paper_triage.toml"
    path.parent.mkdir(parents=True)
    path.write_text("id = \"paper_triage\"\nversion = 2\n", encoding="utf-8")
    report = L.validate_library(root)
    assert report["ok"] is False
    assert any(issue["code"] == "invalid_spec" for issue in report["issues"])
    assert any(str(path) in error for error in report["errors"])


# --------------------------------------------------------------------------- #
# copy workflow
# --------------------------------------------------------------------------- #
def test_copy_dry_run_writes_nothing(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    result = L.copy_task(
        root, "diff_review", "ozagia/tasks/diff_review.toml")
    assert result["dry_run"] is True
    assert result["written"] is False
    assert result["unchanged"] is False
    assert result["relative_destination"] == "ozagia/tasks/diff_review.toml"
    assert result["bytes"] > 0
    assert "diff_review" in result["diff"]
    assert not (root / "ozagia").exists()
    assert not (root / "ozagia" / "tasks" / "diff_review.toml").exists()


def test_copy_requires_confirmation(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    with pytest.raises(L.TaskLibraryError, match="confirmation"):
        L.copy_task(root, "diff_review", "ozagia/tasks/diff_review.toml",
                    dry_run=False)
    assert not (root / "ozagia" / "tasks" / "diff_review.toml").exists()


def test_copy_apply_writes_and_validates(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    result = L.copy_task(
        root, "diff_review", "ozagia/tasks/diff_review.toml",
        dry_run=False, confirm=True)
    assert result["written"] is True
    assert result["dry_run"] is False
    destination = root / "ozagia" / "tasks" / "diff_review.toml"
    assert destination.is_file()
    assert destination.read_text(encoding="utf-8") == L.get_task(
        "diff_review").read_text()
    assert L.validate_library(root)["ok"] is True
    assert L.get_task("diff_review", root=root).source == "repo"


def test_copy_to_directory_uses_task_id(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    tasks_dir = root / "ozagia" / "tasks"
    tasks_dir.mkdir(parents=True)
    result = L.copy_task(root, "test_triage", "ozagia/tasks",
                         dry_run=False, confirm=True)
    assert result["written"] is True
    assert (tasks_dir / "test_triage.toml").is_file()


def test_copy_refuses_outside_repo_config(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    with pytest.raises(L.TaskLibraryError, match="inside"):
        L.copy_task(root, "diff_review", "diff_review.toml", dry_run=False,
                    confirm=True)
    outside = tmp_path / "elsewhere" / "diff_review.toml"
    with pytest.raises(L.TaskLibraryError, match="inside"):
        L.copy_task(root, "diff_review", outside, dry_run=False, confirm=True)
    assert not outside.exists()


def test_copy_refuses_overwrite_without_force(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    destination = root / "ozagia" / "tasks" / "diff_review.toml"
    L.copy_task(root, "diff_review", destination, dry_run=False, confirm=True)
    destination.write_text("# local edit\n", encoding="utf-8")
    with pytest.raises(L.TaskLibraryError, match="already exists"):
        L.copy_task(root, "diff_review", destination, dry_run=False,
                    confirm=True)
    result = L.copy_task(root, "diff_review", destination, dry_run=False,
                         confirm=True, force=True)
    assert result["written"] is True
    assert result["overwrote"] is True
    assert destination.read_text() == L.get_task("diff_review").read_text()


def test_copy_identical_destination_is_unchanged(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    destination = root / "ozagia" / "tasks" / "diff_review.toml"
    L.copy_task(root, "diff_review", destination, dry_run=False, confirm=True)
    result = L.copy_task(root, "diff_review", destination, dry_run=False,
                         confirm=True)
    assert result["written"] is False
    assert result["unchanged"] is True
    assert result["diff"] == ""


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def test_cli_list_show_catalog_validate(tmp_path: Path,
                                        capsys: pytest.CaptureFixture[str]) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    assert task_library_cli.main(["list", "--root", str(root)]) == 0
    human = capsys.readouterr().out
    assert "Task classes: 15" in human
    assert "research (6)" in human
    assert "paper_triage" in human

    assert task_library_cli.main(
        ["list", "--root", str(root), "--domain", "ops", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["count"] == 3
    assert {task["id"] for task in payload["tasks"]} == OPS

    assert task_library_cli.main(
        ["catalog", "--root", str(root), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["counts"]["total"] == 15

    assert task_library_cli.main(
        ["show", "diff_review", "--root", str(root), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["entry"]["domain"] == "coding"
    assert payload["spec"]["id"] == "diff_review"

    assert task_library_cli.main(
        ["show", "diff_review", "--root", str(root)]) == 0
    human = capsys.readouterr().out
    assert "id: diff_review" in human
    assert "steps:" in human

    assert task_library_cli.main(["validate", "--root", str(root)]) == 0
    human = capsys.readouterr().out
    assert "ok: 15 task classes" in human

    assert task_library_cli.main(
        ["validate", "--root", str(root), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True


def test_cli_validation_error_exits_one(tmp_path: Path,
                                        capsys: pytest.CaptureFixture[str]) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _write_unknown(root)
    assert task_library_cli.main(["validate", "--root", str(root)]) == 1
    assert "does not override a built-in" in capsys.readouterr().out
    assert task_library_cli.main(["list", "--root", str(root)]) == 1
    assert "does not override a built-in" in capsys.readouterr().err
    assert task_library_cli.main(
        ["show", "diff_review", "--root", str(root)]) == 1
    capsys.readouterr()


def test_cli_usage_and_lookup_errors_exit_two(tmp_path: Path,
                                              capsys: pytest.CaptureFixture[str]) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    assert task_library_cli.main(
        ["show", "no_such_task", "--root", str(root)]) == 2
    assert "unknown task class" in capsys.readouterr().err
    assert task_library_cli.main(
        ["list", "--root", str(root), "--domain", "nope"]) != 0
    capsys.readouterr()
    with pytest.raises(SystemExit) as excinfo:
        task_library_cli.main(["copy", "paper_triage", "--root", str(root)])
    assert excinfo.value.code == 2
    capsys.readouterr()


def test_cli_copy_defaults_to_dry_run(tmp_path: Path,
                                      capsys: pytest.CaptureFixture[str]) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    destination = root / "ozagia" / "tasks" / "paper_triage.toml"
    assert task_library_cli.main(
        ["copy", "paper_triage", "--to", str(destination),
         "--root", str(root)]) == 0
    assert not destination.exists()
    assert "dry-run" in capsys.readouterr().out

    assert task_library_cli.main(
        ["copy", "paper_triage", "--to", str(destination),
         "--root", str(root), "--dry-run"]) == 0
    assert not destination.exists()
    capsys.readouterr()

    assert task_library_cli.main(
        ["copy", "paper_triage", "--to", str(destination),
         "--root", str(root), "--apply"]) == 0
    assert destination.is_file()
    assert "copied" in capsys.readouterr().out
    assert L.validate_library(root)["ok"] is True


def test_cli_copy_json_and_outside_error(tmp_path: Path,
                                         capsys: pytest.CaptureFixture[str]) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    destination = root / "ozagia" / "tasks" / "paper_triage.toml"
    assert task_library_cli.main(
        ["copy", "paper_triage", "--to", str(destination),
         "--root", str(root), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["dry_run"] is True
    assert payload["written"] is False

    assert task_library_cli.main(
        ["copy", "paper_triage", "--to", "paper_triage.toml",
         "--root", str(root), "--apply"]) == 2
    assert "inside" in capsys.readouterr().err
    assert not (root / "paper_triage.toml").exists()


def test_cli_copy_apply_reports_validation_error(tmp_path: Path,
                                                 capsys: pytest.CaptureFixture[str]) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _write_unknown(root)
    assert task_library_cli.main(
        ["copy", "diff_review", "--to", "ozagia/tasks/diff_review.toml",
         "--root", str(root), "--apply"]) == 1
    assert "does not override a built-in" in capsys.readouterr().err
    assert not (root / "ozagia" / "tasks" / "diff_review.toml").exists()
