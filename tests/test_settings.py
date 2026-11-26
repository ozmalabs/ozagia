"""Typed settings registry: defaults, precedence, writes and the CLI."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from ozagia import cli, settings_cli
from ozagia import config as project_config
from ozagia.core import migrate as M
from ozagia.core import settings as S


REQUIRED_KEYS = {
    "index.source_dirs", "index.test_dirs", "index.docs", "index.skip_dirs",
    "resources.enabled", "resources.reserve_memory_pct",
    "resources.reserve_disk_pct", "resources.reserve_disk_gb",
    "resources.min_free_memory_pct",
    "resources.min_free_disk_gb", "resources.job_memory_mb",
    "resources.job_disk_mb", "resources.job_pids",
    "worktrees.root", "worktrees.min_free_gb", "worktrees.avoid_tmpfs",
    "markdown.history", "markdown.new_file_budget",
    "markdown.duplicate_threshold", "markdown.root_allow",
    "files.trash", "files.retention_days",
    "models.default", "models.fallback", "models.local_only",
    "tasks.default_budget_tokens", "tasks.max_wall_s",
    "review.min_reviewers", "review.require_clean",
    "ui.default_aperture", "ui.theme",
}


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for name in ("OZAGIA_ROOT", "OZAGIA_CONFIG", "OZAGIA_STATE_DIR",
                 "OZAGIA_USER_CONFIG", *S.ENV_SETTINGS):
        monkeypatch.delenv(name, raising=False)
    return home


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _repo_config(root: Path, text: str) -> Path:
    return _write(root / "ozagia" / "config.toml", text)


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    return root


# --------------------------------------------------------------------------- #
# registry and defaults
# --------------------------------------------------------------------------- #
def test_schema_and_defaults(tmp_path: Path) -> None:
    items = S.schema()
    keys = [item["key"] for item in items]
    assert len(keys) == len(set(keys))
    assert REQUIRED_KEYS <= set(keys)
    groups = {item["group"] for item in items}
    assert {"index", "resources", "worktrees", "markdown", "files", "models",
            "tasks", "review", "ui"} <= groups
    root = _root(tmp_path)
    for item in items:
        assert item["type"] in {
            "string", "integer", "number", "boolean", "string_list"}
        assert item["scope"] in {"user", "repo", "both"}
        assert item["description"]
        assert item["validation"]
        assert isinstance(item["restart_required"], bool)
        record = S.get(root, item["key"])
        assert record["value"] == item["default"]
        assert record["source"] == "default"
    assert S.get(root, "index.source_dirs")["value"] == ["."]
    assert S.get(root, "resources.job_memory_mb")["value"] == 2048
    assert S.get(root, "worktrees.root")["value"] == "../ozagia-wt"
    assert S.get(root, "ui.theme")["value"] == "auto"
    assert S.get(root, "index.source_dirs")["restart_required"] is True


def test_environment_maps_are_explicit() -> None:
    assert S.ENV_LOCATIONS == {
        "OZAGIA_ROOT": "root",
        S.ENV_STATE_DIR: "state_dir",
        S.ENV_USER_CONFIG: "user_config",
    }
    assert S.ENV_SETTINGS["OZAGIA_RESOURCES_ENABLED"] == "resources.enabled"
    assert S.ENV_SETTINGS["OZAGIA_UI_THEME"] == "ui.theme"


def test_reads_never_create_config_or_state(tmp_path: Path) -> None:
    root = _root(tmp_path)
    S.schema()
    S.values(root)
    S.get(root, "ui.theme")
    S.locations(root)
    assert not (root / "ozagia").exists()
    assert not (root / ".ozagia").exists()


def test_writes_only_the_versioned_config_not_state(tmp_path: Path) -> None:
    root = _root(tmp_path)
    S.set_value(root, "ui.theme", "dark", dry_run=False)
    files = sorted(path.relative_to(root).as_posix()
                   for path in root.rglob("*") if path.is_file())
    assert files == ["ozagia/config.toml"]
    assert not (root / "ozagia" / "__init__.py").exists()
    assert not (root / ".ozagia").exists()


# --------------------------------------------------------------------------- #
# precedence and sources
# --------------------------------------------------------------------------- #
def test_precedence_default_user_repo_env_override(
        tmp_path: Path, monkeypatch) -> None:
    root = _root(tmp_path)
    item = S.get(root, "ui.theme")
    assert (item["value"], item["source"]) == ("auto", "default")

    user = _write(tmp_path / "user.toml", '[ui]\ntheme = "light"\n')
    monkeypatch.setenv("OZAGIA_USER_CONFIG", str(user))
    item = S.get(root, "ui.theme")
    assert (item["value"], item["source"]) == ("light", "user")

    _repo_config(root, '[ui]\ntheme = "dark"\n')
    item = S.get(root, "ui.theme")
    assert (item["value"], item["source"]) == ("dark", "repo")

    monkeypatch.setenv("OZAGIA_UI_THEME", "auto")
    item = S.get(root, "ui.theme")
    assert (item["value"], item["source"]) == ("auto", "env")

    item = S.get(root, "ui.theme", overrides={"ui.theme": "light"})
    assert (item["value"], item["source"]) == ("light", "override")


def test_user_values_are_used_where_the_repository_does_not_override(
        tmp_path: Path, monkeypatch) -> None:
    root = _root(tmp_path)
    user = _write(
        tmp_path / "user.toml",
        '[ui]\ntheme = "light"\n[models]\ndefault = "local/qwen"\n')
    monkeypatch.setenv("OZAGIA_USER_CONFIG", str(user))
    _repo_config(root, '[ui]\ntheme = "dark"\n')
    assert S.get(root, "ui.theme")["source"] == "repo"
    assert S.get(root, "models.default")["value"] == "local/qwen"
    assert S.get(root, "models.default")["source"] == "user"


def test_environment_values_and_errors(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("OZAGIA_RESOURCES_ENABLED", "0")
    item = S.get(tmp_path, "resources.enabled")
    assert (item["value"], item["source"]) == (False, "env")

    monkeypatch.setenv("OZAGIA_TASKS_MAX_WALL_S", "1200")
    assert S.get(tmp_path, "tasks.max_wall_s")["value"] == 1200

    monkeypatch.setenv("OZAGIA_MODELS_DEFAULT", "")
    item = S.get(tmp_path, "models.default")
    assert (item["value"], item["source"]) == ("", "env")

    monkeypatch.setenv("OZAGIA_RESOURCES_ENABLED", "maybe")
    with pytest.raises(S.SettingsError, match="OZAGIA_RESOURCES_ENABLED"):
        S.values(tmp_path)


def test_explicit_override_and_unknown_override(
        tmp_path: Path, monkeypatch) -> None:
    root = _root(tmp_path)
    _repo_config(root, '[ui]\ntheme = "dark"\n')
    monkeypatch.setenv("OZAGIA_UI_THEME", "light")
    values = S.values(root, overrides={"resources.job_pids": 12,
                                       "ui.theme": "auto"})
    assert values["resources.job_pids"]["source"] == "override"
    assert values["resources.job_pids"]["value"] == 12
    assert values["ui.theme"]["source"] == "override"
    with pytest.raises(S.SettingsError, match="ui.them"):
        S.values(root, overrides={"ui.them": "dark"})


# --------------------------------------------------------------------------- #
# paths
# --------------------------------------------------------------------------- #
def test_user_config_path_and_write(tmp_path: Path, monkeypatch,
                                    isolated_home: Path) -> None:
    default = isolated_home / ".config" / "ozagia" / "config.toml"
    assert S.user_config_path() == default.resolve()

    custom = tmp_path / "custom" / "ozagia.toml"
    monkeypatch.setenv("OZAGIA_USER_CONFIG", str(custom))
    assert S.user_config_path() == custom.resolve()

    root = _root(tmp_path)
    result = S.set_value(root, "ui.theme", "light", scope="user",
                         dry_run=False)
    assert result["path"] == str(custom.resolve())
    assert result["applied"] is True
    assert custom.is_file()
    assert S.get(root, "ui.theme")["value"] == "light"
    assert S.get(root, "ui.theme")["source"] == "user"
    assert not (root / "ozagia").exists()


def test_repo_config_path_and_nested_write(tmp_path: Path) -> None:
    root = _root(tmp_path)
    path = S.repo_config_path(root)
    assert path == root.resolve() / "ozagia" / "config.toml"

    result = S.set_value(root, "resources.job_pids", 128, dry_run=False)
    assert result["path"] == str(path)
    assert result["created"] is True
    text = path.read_text(encoding="utf-8")
    assert "[resources.limits]" in text
    assert "job_pids = 128" in text
    assert S.get(root, "resources.job_pids")["value"] == 128
    assert S.get(root, "resources.job_pids")["source"] == "repo"


def test_written_resource_settings_match_the_existing_policy_loader(
        tmp_path: Path) -> None:
    from ozagia.core import resources as R

    root = _root(tmp_path)
    S.set_value(root, "resources.job_pids", 128, dry_run=False)
    S.set_value(root, "resources.min_free_memory_pct", 25, dry_run=False)
    S.set_value(root, "worktrees.root", "../wt", dry_run=False)
    policy = R.load_policy(root)
    assert policy["limits"]["job_pids"] == 128
    assert policy["admission"]["min_free_memory_pct"] == 25.0
    assert policy["worktrees"]["root"] == "../wt"
    assert policy["errors"] == []


def test_locations_report_state_dir_without_creating_it(
        tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("OZAGIA_STATE_DIR", "work/state")
    info = S.locations(tmp_path)
    assert info["state_dir"] == str((tmp_path / "work" / "state").resolve())
    assert info["repo_config"] == str(
        tmp_path.resolve() / "ozagia" / "config.toml")
    assert info["repo_config_exists"] is False
    assert not (tmp_path / "work").exists()


# --------------------------------------------------------------------------- #
# get/set and preview
# --------------------------------------------------------------------------- #
def test_get_returns_value_source_and_default(tmp_path: Path) -> None:
    item = S.get(tmp_path, "index.test_dirs")
    assert item["key"] == "index.test_dirs"
    assert item["value"] == ["tests"]
    assert item["source"] == "default"
    assert item["default"] == ["tests"]
    assert item["scope"] == "repo"
    assert item["type"] == "string_list"


def test_dry_run_is_the_api_default_and_changes_nothing(tmp_path: Path) -> None:
    root = _root(tmp_path)
    result = S.set_value(root, "ui.theme", "dark")
    assert result["dry_run"] is True
    assert result["changed"] is True
    assert result["applied"] is False
    assert result["created"] is False
    assert not S.repo_config_path(root).exists()
    assert not (root / "ozagia").exists()
    assert 'theme = "dark"' in result["diff"]


def test_dry_run_leaves_an_existing_file_untouched(tmp_path: Path) -> None:
    root = _root(tmp_path)
    original = '# keep\n[index]\nsource_dirs = ["src"]\n'
    path = _repo_config(root, original)
    before = path.read_bytes()
    result = S.set_value(root, "index.test_dirs", ["spec"], dry_run=True)
    assert path.read_bytes() == before
    assert result["changed"] is True
    assert result["applied"] is False
    assert "+test_dirs" in result["diff"]
    assert sorted(item.name for item in path.parent.iterdir()) == ["config.toml"]


def test_set_applies_and_is_idempotent(tmp_path: Path) -> None:
    root = _root(tmp_path)
    first = S.set_value(root, "ui.theme", "dark", dry_run=False)
    assert first["applied"] is True
    assert first["created"] is True
    assert S.repo_config_path(root).read_text(encoding="utf-8") == (
        '[ui]\ntheme = "dark"\n')

    second = S.set_value(root, "ui.theme", "dark", dry_run=False)
    assert second["changed"] is False
    assert second["applied"] is False
    assert second["diff"] == ""
    assert second["old"] == "dark"


def test_current_source_is_reported_in_a_preview(tmp_path: Path,
                                                 monkeypatch) -> None:
    root = _root(tmp_path)
    monkeypatch.setenv("OZAGIA_UI_THEME", "light")
    result = S.set_value(root, "ui.theme", "dark", dry_run=False)
    assert result["current"] == {"value": "light", "source": "env"}
    assert S.get(root, "ui.theme")["value"] == "light"
    assert S.get(root, "ui.theme")["source"] == "env"


# --------------------------------------------------------------------------- #
# TOML preservation and aliases
# --------------------------------------------------------------------------- #
def test_set_preserves_unrelated_toml_and_comments(tmp_path: Path) -> None:
    root = _root(tmp_path)
    original = (
        "# project settings\n"
        "[index]\n"
        'source_dirs = ["src"]  # keep this comment\n'
        'skip_dirs = ["vendor"]\n'
        "\n"
        "[ui]\n"
        "# theme is reviewed\n"
        'theme = "dark"\n'
    )
    path = _repo_config(root, original)

    result = S.set_value(root, "index.test_dirs", ["spec"], dry_run=False)
    text = path.read_text(encoding="utf-8")
    assert result["applied"] is True
    assert "# project settings" in text
    assert "# keep this comment" in text
    assert "# theme is reviewed" in text
    assert 'source_dirs = ["src"]' in text
    assert 'skip_dirs = ["vendor"]' in text
    assert 'theme = "dark"' in text
    assert text.index("test_dirs") < text.index("[ui]")

    S.set_value(root, "ui.theme", "light", dry_run=False)
    text = path.read_text(encoding="utf-8")
    assert 'theme = "light"' in text
    assert "# theme is reviewed" in text
    assert "# project settings" in text
    assert 'source_dirs = ["src"]  # keep this comment' in text


def test_set_preserves_a_target_line_comment(tmp_path: Path) -> None:
    root = _root(tmp_path)
    path = _repo_config(root, '[ui]\ntheme = "dark"  # reviewed 2026-11\n')
    S.set_value(root, "ui.theme", "light", dry_run=False)
    assert path.read_text(encoding="utf-8") == (
        '[ui]\ntheme = "light"  # reviewed 2026-11\n')


def test_set_replaces_a_multiline_value(tmp_path: Path) -> None:
    root = _root(tmp_path)
    path = _repo_config(root, (
        "[index]\n"
        "docs = [\n"
        '  "README.md",\n'
        '  "docs/*.md",\n'
        "]\n"
    ))
    S.set_value(root, "index.docs", ["README.md", "docs/**/*.md"],
                dry_run=False)
    text = path.read_text(encoding="utf-8")
    assert text.count("docs =") == 1
    assert "docs/*.md" not in text
    assert "docs/**/*.md" in text
    assert S.get(root, "index.docs")["value"] == ["README.md", "docs/**/*.md"]


def test_set_updates_an_existing_alias_location(tmp_path: Path) -> None:
    root = _root(tmp_path)
    path = _repo_config(root, '[worktrees]\nroot = "../wt"\n')
    result = S.set_value(root, "worktrees.root", "../other", dry_run=False)
    text = path.read_text(encoding="utf-8")
    assert result["applied"] is True
    assert text.count("root =") == 1
    assert "../other" in text
    assert "[resources.worktrees]" not in text


def test_flat_resource_aliases_are_accepted_and_updated_in_place(
        tmp_path: Path) -> None:
    root = _root(tmp_path)
    path = _repo_config(root, (
        "[resources]\n"
        "job_pids = 64\n"
        "min_free_memory_pct = 20\n"
    ))
    assert S.get(root, "resources.job_pids")["value"] == 64
    assert S.get(root, "resources.min_free_memory_pct")["value"] == 20.0
    S.set_value(root, "resources.job_pids", 96, dry_run=False)
    text = path.read_text(encoding="utf-8")
    assert "job_pids = 96" in text
    assert "job_pids = 64" not in text
    assert "[resources.limits]" not in text


# --------------------------------------------------------------------------- #
# validation
# --------------------------------------------------------------------------- #
def test_unknown_key_reports_path_and_suggestion(tmp_path: Path) -> None:
    root = _root(tmp_path)
    path = _repo_config(root, '[index]\nsource_dir = ["."]\n')
    with pytest.raises(S.SettingsError) as excinfo:
        S.values(root)
    message = str(excinfo.value)
    assert str(path) in message
    assert "index.source_dir" in message
    assert "index.source_dirs" in message

    with pytest.raises(S.SettingsError, match="index.source_dir"):
        S.get(root, "index.source_dir")
    with pytest.raises(S.SettingsError, match="index.source_dir"):
        S.set_value(root, "index.source_dir", ["src"])


def test_unknown_section_reports_path_and_suggestion(tmp_path: Path) -> None:
    root = _root(tmp_path)
    path = _repo_config(root, '[indexs]\nsource_dirs = ["."]\n')
    with pytest.raises(S.SettingsError) as excinfo:
        S.values(root)
    message = str(excinfo.value)
    assert str(path) in message
    assert "unknown section [indexs]" in message
    assert "index" in message


def test_type_errors_and_ranges(tmp_path: Path) -> None:
    root = _root(tmp_path)
    _repo_config(root, '[resources]\nenabled = "yes"\n')
    with pytest.raises(S.SettingsError,
                       match=r"resources.enabled.*true or false"):
        S.values(root)

    with pytest.raises(S.SettingsError, match="between 1 and"):
        S.set_value(root, "resources.job_pids", 0)
    with pytest.raises(S.SettingsError, match="must be an integer"):
        S.set_value(root, "resources.job_pids", "many")
    with pytest.raises(S.SettingsError, match="must be one of"):
        S.set_value(root, "ui.theme", "neon")
    with pytest.raises(S.SettingsError, match="must be a list"):
        S.set_value(root, "index.docs", {"docs": True})


def test_invalid_toml_reports_the_path(tmp_path: Path) -> None:
    root = _root(tmp_path)
    path = _repo_config(root, "[ui\ntheme = 1\n")
    with pytest.raises(S.SettingsError, match=str(path)):
        S.values(root)


def test_repository_paths_stay_inside_the_repository(tmp_path: Path) -> None:
    root = _root(tmp_path)
    with pytest.raises(S.SettingsError, match="inside the repository root"):
        S.set_value(root, "index.source_dirs", ["../src"])
    with pytest.raises(S.SettingsError, match="relative to the repository root"):
        S.set_value(root, "files.trash", "/tmp/trash")
    # A worktree directory is deliberately allowed outside the repository.
    assert S.set_value(root, "worktrees.root", "/tmp/wt")["new"] == "/tmp/wt"


def test_repo_only_settings_are_rejected_in_the_user_config(
        tmp_path: Path, monkeypatch) -> None:
    user = _write(tmp_path / "user.toml",
                  '[index]\nsource_dirs = ["src"]\n')
    monkeypatch.setenv("OZAGIA_USER_CONFIG", str(user))
    with pytest.raises(S.SettingsError, match="repo-only"):
        S.values(_root(tmp_path))


def test_scope_controls_writes(tmp_path: Path) -> None:
    root = _root(tmp_path)
    with pytest.raises(S.SettingsError, match="repo-only"):
        S.set_value(root, "index.source_dirs", ["src"], scope="user")
    with pytest.raises(S.SettingsError, match="scope must be"):
        S.set_value(root, "ui.theme", "dark", scope="global")
    with pytest.raises(S.SettingsError, match="user_config"):
        S.set_value(root, "ui.theme", "dark", user_config="x.toml")

    custom = tmp_path / "user.toml"
    result = S.set_value(root, "ui.theme", "dark", scope="user",
                         user_config=custom, dry_run=False)
    assert result["path"] == str(custom.resolve())
    assert custom.read_text(encoding="utf-8") == '[ui]\ntheme = "dark"\n'


# --------------------------------------------------------------------------- #
# atomic writes
# --------------------------------------------------------------------------- #
def test_atomic_write_failure_leaves_the_original(
        tmp_path: Path, monkeypatch) -> None:
    root = _root(tmp_path)
    path = _repo_config(root, '# original\n[index]\nsource_dirs = ["src"]\n')
    before = path.read_bytes()
    calls: list[tuple[Path, Path]] = []
    real_replace = os.replace

    def failing_replace(source, destination):
        calls.append((Path(source), Path(destination)))
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", failing_replace)
    with pytest.raises(S.SettingsError, match="cannot write"):
        S.set_value(root, "index.test_dirs", ["spec"], dry_run=False)
    assert path.read_bytes() == before
    assert len(calls) == 1
    assert calls[0][0].parent == path.parent
    assert calls[0][1] == path
    assert sorted(item.name for item in path.parent.iterdir()) == ["config.toml"]

    monkeypatch.setattr(os, "replace", real_replace)
    result = S.set_value(root, "index.test_dirs", ["spec"], dry_run=False)
    assert result["applied"] is True
    assert sorted(item.name for item in path.parent.iterdir()) == ["config.toml"]


# --------------------------------------------------------------------------- #
# dynamic model tables
# --------------------------------------------------------------------------- #
def test_models_tasks_and_local_are_dynamic_settings(tmp_path: Path) -> None:
    root = _root(tmp_path)
    _repo_config(root, """
[models]
default = "openai/gpt-5"

[models.tasks]
diff_review = "openai/gpt-5-mini"
paper_triage = "ollama/qwen"

[models.local]
providers = ["ollama", "lmstudio"]
models = ["vllm/private", "claude-agent/claude-fable-5-1[1m]"]
""")
    values = S.values(root)
    assert values["models.tasks.diff_review"]["value"] == "openai/gpt-5-mini"
    assert values["models.tasks.diff_review"]["source"] == "repo"
    assert values["models.tasks.diff_review"]["type"] == "string"
    assert values["models.local.providers"]["value"] == ["ollama", "lmstudio"]
    assert values["models.local.providers"]["type"] == "string_list"
    assert values["models.local.models"]["value"] == [
        "vllm/private", "claude-agent/claude-fable-5-1[1m]"]
    assert S.get(root, "models.tasks.diff_review")["value"] == (
        "openai/gpt-5-mini")
    # `by_task_class` is an alias for the same canonical setting.
    alias = S.get(root, "models.by_task_class.diff_review")
    assert alias["key"] == "models.tasks.diff_review"
    assert S.model_overrides(root) == {
        "tasks": {
            "diff_review": "openai/gpt-5-mini",
            "paper_triage": "ollama/qwen",
        },
        "local": {
            "providers": ["lmstudio", "ollama"],
            "models": [
                "claude-agent/claude-fable-5-1[1m]", "vllm/private"],
        },
    }

    preview = S.set_value(root, "models.tasks.code_review", "local/qwen")
    assert preview["changed"] is True
    assert 'code_review = "local/qwen"' in preview["diff"]
    assert (root / "ozagia" / "config.toml").read_text(
        encoding="utf-8").count("code_review") == 0

    S.set_value(root, "models.tasks.code_review", "local/qwen", dry_run=False)
    S.set_value(root, "models.local.providers", ["ollama"], dry_run=False)
    text = (root / "ozagia" / "config.toml").read_text(encoding="utf-8")
    assert "[models.tasks]" in text
    assert 'code_review = "local/qwen"' in text
    assert 'providers = ["ollama"]' in text
    assert S.model_overrides(root)["tasks"]["code_review"] == "local/qwen"
    assert S.model_overrides(root)["local"]["providers"] == ["ollama"]


def test_models_by_task_class_alias_is_updated_in_place(tmp_path: Path) -> None:
    root = _root(tmp_path)
    path = _repo_config(
        root, '[models.by_task_class]\ndiff_review = "local/one"\n')
    assert S.get(root, "models.tasks.diff_review")["value"] == "local/one"
    S.set_value(root, "models.tasks.diff_review", "local/two", dry_run=False)
    text = path.read_text(encoding="utf-8")
    assert 'diff_review = "local/two"' in text
    assert text.count("[models.by_task_class]") == 1
    assert "[models.tasks]" not in text


def test_models_tasks_and_by_task_class_cannot_both_be_defined(
        tmp_path: Path) -> None:
    root = _root(tmp_path)
    _repo_config(root, """
[models.tasks]
diff_review = "local/one"

[models.by_task_class]
paper_triage = "local/two"
""")
    with pytest.raises(S.SettingsError, match="use one"):
        S.values(root)


@pytest.mark.parametrize(("key", "value", "needle"), [
    ("models.tasks.bad class", "local/qwen", "task class"),
    ("models.tasks.diff_review", "gpt-5", "provider/model"),
    ("models.tasks.diff_review", "provider/", "model"),
    ("models.local.providers", ["bad/provider"], "provider"),
    ("models.local.models", ["gpt-5"], "provider/model"),
])
def test_dynamic_model_validation(tmp_path: Path, key: str, value: object,
                                  needle: str) -> None:
    root = _root(tmp_path)
    with pytest.raises(S.SettingsError, match=needle):
        S.set_value(root, key, value)


def test_models_local_rejects_unknown_leaf_with_path(tmp_path: Path) -> None:
    root = _root(tmp_path)
    path = _repo_config(root, "[models.local]\nother = []\n")
    with pytest.raises(S.SettingsError) as excinfo:
        S.values(root)
    message = str(excinfo.value)
    assert str(path) in message
    assert "models.local" in message
    assert "other" in message


def test_cli_values_accepts_models_dynamic_tables(tmp_path: Path,
                                                  capsys) -> None:
    root = _root(tmp_path)
    _repo_config(root, """
[models.tasks]
diff_review = "local/qwen"

[models.local]
providers = ["ollama"]
""")
    assert settings_cli.main(
        ["values", "--root", str(root), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["settings"]["models.tasks.diff_review"]["value"] == (
        "local/qwen")
    assert payload["settings"]["models.local.providers"]["value"] == ["ollama"]


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def test_cli_schema_json(capsys) -> None:
    assert settings_cli.main(["schema", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == S.SCHEMA
    assert any(item["key"] == "ui.theme" for item in payload["settings"])


def test_cli_schema_text(capsys) -> None:
    assert settings_cli.main(["schema"]) == 0
    out = capsys.readouterr().out
    assert "[index]" in out
    assert "source_dirs" in out
    assert "OZAGIA_RESOURCES_ENABLED" in out


def test_cli_values_get_and_set(tmp_path: Path, capsys) -> None:
    root = _root(tmp_path)
    assert settings_cli.main(["values", "--root", str(root), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["settings"]["ui.theme"]["source"] == "default"
    assert payload["locations"]["repo_config"] == str(
        S.repo_config_path(root))

    assert settings_cli.main(["get", "ui.theme", "--root", str(root)]) == 0
    out = capsys.readouterr().out
    assert 'ui.theme = "auto"' in out
    assert "(default)" in out

    assert settings_cli.main(
        ["get", "ui.theme", "--root", str(root), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["setting"]["value"] == "auto"

    assert settings_cli.main(
        ["set", "ui.theme", "dark", "--root", str(root), "--dry-run",
         "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["dry_run"] is True
    assert payload["applied"] is False
    assert 'theme = "dark"' in payload["diff"]
    assert not (root / "ozagia").exists()

    assert settings_cli.main(
        ["set", "ui.theme", "dark", "--root", str(root), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["applied"] is True
    assert S.get(root, "ui.theme")["value"] == "dark"
    assert S.get(root, "ui.theme")["source"] == "repo"


def test_cli_set_text_reports_the_diff(tmp_path: Path, capsys) -> None:
    root = _root(tmp_path)
    assert settings_cli.main(
        ["set", "index.test_dirs", "spec,tests", "--root", str(root),
         "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "test_dirs = [\"spec\", \"tests\"]" in out
    assert "dry run" in out
    assert not (root / "ozagia").exists()


def test_cli_set_user_scope(tmp_path: Path, isolated_home: Path,
                            capsys) -> None:
    root = _root(tmp_path)
    assert settings_cli.main(
        ["set", "ui.theme", "dark", "--scope", "user", "--root", str(root),
         "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    expected = (isolated_home / ".config" / "ozagia" / "config.toml").resolve()
    assert payload["path"] == str(expected)
    assert payload["applied"] is True
    assert expected.is_file()
    assert S.get(root, "ui.theme")["source"] == "user"
    assert not (root / "ozagia").exists()


def test_cli_errors_exit_2(tmp_path: Path, capsys) -> None:
    root = _root(tmp_path)
    assert settings_cli.main(
        ["get", "index.nope", "--root", str(root)]) == 2
    assert "unknown setting" in capsys.readouterr().err

    assert settings_cli.main(
        ["set", "resources.job_pids", "zero", "--root", str(root)]) == 2
    assert "integer" in capsys.readouterr().err

    _repo_config(root, '[index]\nsource_dir = ["."]\n')
    assert settings_cli.main(["values", "--root", str(root)]) == 2
    assert "source_dirs" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# legacy .ozagia.toml migration
# --------------------------------------------------------------------------- #
def _migrate_statuses(report: dict) -> dict[str, str]:
    return {item["legacy"]: item["status"] for item in report["entries"]}


def test_project_model_section_keeps_registry_leaf_and_skips_named_entries(
        tmp_path: Path) -> None:
    data = {"environments": {
        "trust_project_python": True,
        "dev": {"paths": ["envs/dev"]},
    }}
    values, locations = S._parse_layer(data, Path("cfg.toml"), "user")
    assert values == {"environments.trust_project_python": True}
    assert locations["environments.trust_project_python"] == (
        "environments", "trust_project_python")

    with pytest.raises(S.SettingsError, match="user-only"):
        S._parse_layer(data, Path("cfg.toml"), "repo")


def test_trust_project_python_is_user_only_but_coexists_with_project_entries(
        tmp_path: Path) -> None:
    root = _root(tmp_path)
    _write(S.user_config_path(),
           "[environments]\ntrust_project_python = true\n")
    target = _repo_config(root, """
# keep this comment
[index]
test_dirs = ["spec"]

[environments.dev]
paths = ["envs/dev"]
""")
    record = S.get(root, "environments.trust_project_python")
    assert record["value"] is True
    assert record["source"] == "user"
    assert S.layer_values(root)["index.test_dirs"] == ["spec"]

    result = S.set_values(
        root, {"index.source_dirs": ["src"]}, scope="repo", dry_run=False)
    assert result["applied"] is True
    text = target.read_text(encoding="utf-8")
    assert "# keep this comment" in text
    assert '[environments.dev]\npaths = ["envs/dev"]' in text
    assert S.layer_values(root)["index.source_dirs"] == ["src"]
    assert S.get(root, "environments.trust_project_python")["value"] is True

    _repo_config(root, """
[environments]
trust_project_python = true

[environments.dev]
paths = ["envs/dev"]
""")
    with pytest.raises(S.SettingsError, match="user-only"):
        S.layer_values(root)


def test_migration_coexists_with_user_trust_and_versioned_project_entries(
        tmp_path: Path) -> None:
    root = _root(tmp_path)
    _write(S.user_config_path(),
           "[environments]\ntrust_project_python = true\n")
    legacy = _write(root / ".ozagia.toml",
                    '[environments.staging]\npaths = ["envs/staging"]\n')
    target = _repo_config(root, """
# keep this comment
[environments.dev]
paths = ["envs/dev"]
""")
    preview = M.migrate(root)
    assert _migrate_statuses(preview) == {"environments.staging": "move"}
    applied = M.migrate(root, dry_run=False, confirm=preview["token"])
    assert applied["applied"] is True
    text = target.read_text(encoding="utf-8")
    assert "# keep this comment" in text
    assert '[environments.dev]\npaths = ["envs/dev"]' in text
    assert '[environments.staging]\npaths = ["envs/staging"]' in text
    assert S.get(root, "environments.trust_project_python")["value"] is True

    project_config.clear_config_cache()
    cfg = project_config.load_project_config(root)
    assert [item.name for item in cfg.environments] == ["staging", "dev"]
    assert legacy.exists()

    legacy.unlink()
    project_config.clear_config_cache()
    cfg = project_config.load_project_config(root)
    assert [item.name for item in cfg.environments] == ["dev", "staging"]
    assert S.get(root, "environments.trust_project_python")["value"] is True


def test_migrate_maps_supported_sections_and_is_idempotent(
        tmp_path: Path) -> None:
    root = _root(tmp_path)
    legacy_text = """
[index]
source_dirs = ["src"]
test_dirs = ["tests"]
map_dirs = ["src", "tests"]
docs = ["README.md"]
key_docs = ["README.md", "HACKING.md"]
import_roots = ["src"]
path_prefixes = ["src", "tests"]
changed_exclude = ["__pycache__/"]
entrypoint_prefixes = ["scripts/"]

[memory]
skip_dirs = ["vendor"]

[adapters]
modules = ["project_adapter:build"]

[reads]
structured_outline_lines = 1200
outline_lines = 250
repeat = "off"

[reads.repeat_by_harness]
opencode = "stub"

[tests]
select = "affected"
cache = true

[jobs]
enabled = true

[lint]
enabled = false
timeout_s = 5
python = "off"
rust = "off"

[security]
enabled = true
tools = ["semgrep"]
allow_network = false
fail_on = "medium"
exclude = ["vendor/**"]
max_findings = 50
block_secrets = true
secret_allowlist = ["path:tests/fixtures/**", "sha256:0123abcd"]
finding_allowlist = ["sha256:0123456789abcdef"]

[hints]
library = "strict"
max_per_edit = 3
record = false
ecosystems = ["python", "rust"]

[review]
layers = ["src/core", "src/api"]

[iac]
roots = ["infra"]
skip_globs = ["*.backup"]

[environments.dev]
paths = ["envs/dev"]
"""
    legacy = _write(root / ".ozagia.toml", legacy_text)
    before = legacy.read_text(encoding="utf-8")
    target = root / "ozagia" / "config.toml"

    preview = M.migrate(root)
    assert preview["dry_run"] is True
    assert preview["ok"] is True
    assert preview["changed"] is True
    assert preview["token"]
    assert not target.exists()
    assert legacy.read_text(encoding="utf-8") == before
    statuses = _migrate_statuses(preview)
    for legacy_key in (
            "index.source_dirs", "tests.select", "tests.cache",
            "jobs.enabled", "lint.enabled", "security.block_secrets",
            "hints.library"):
        assert statuses[legacy_key] == "move", legacy_key
    assert statuses["environments.dev"] == "move"
    assert preview["counts"]["move"] >= 26
    assert 'library = "strict"' in preview["diff"]
    assert '[environments.dev]' in preview["diff"]

    applied = M.migrate(root, dry_run=False, confirm=preview["token"])
    assert applied["applied"] is True
    assert applied["ok"] is True
    assert target.is_file()
    assert legacy.read_text(encoding="utf-8") == before
    values = S.layer_values(root)
    assert values["index.source_dirs"] == ["src"]
    assert values["index.map_dirs"] == ["src", "tests"]
    assert values["index.path_prefixes"] == ["src", "tests"]
    assert values["memory.skip_dirs"] == ["vendor"]
    assert values["adapters.modules"] == ["project_adapter:build"]
    assert values["reads.repeat"] == "off"
    assert values["reads.repeat_by_harness.opencode"] == "stub"
    assert values["testing.captured"] == "affected"
    assert values["testing.cache"] is True
    assert values["jobs.enabled"] is True
    assert values["lint.enabled"] is False
    assert values["lint.timeout_s"] == 5.0
    assert values["security.secret_allowlist"] == [
        "path:tests/fixtures/**", "sha256:0123abcd"]
    assert values["security.finding_allowlist"] == [
        "sha256:0123456789abcdef"]
    assert values["hints.library"] == "strict"
    assert values["review.layers"] == ["src/core", "src/api"]
    assert values["iac.roots"] == ["infra"]

    second = M.migrate(root)
    assert second["changed"] is False
    assert second["token"] is None
    assert second["diff"] == ""
    assert set(_migrate_statuses(second).values()) == {"same"}

    noop = M.migrate(root, dry_run=False)
    assert noop["applied"] is False
    assert noop["changed"] is False


def test_migrate_reports_unknown_unmapped_and_invalid_keys(
        tmp_path: Path) -> None:
    root = _root(tmp_path)
    _write(root / ".ozagia.toml", """
[index]
source_dir = ["src"]

[unknown_section]
value = true

[iac]
remote_states = [{consumer = "envs/app", name = "iam", producer = "envs/iam"}]

[lint]
trust_project_venv = true

[hints]
library = "loud"
""")
    report = M.migrate(root)
    statuses = _migrate_statuses(report)
    assert statuses["index.source_dir"] == "unknown"
    assert "did you mean source_dirs" in next(
        item["message"] for item in report["entries"]
        if item["legacy"] == "index.source_dir")
    assert statuses["[unknown_section]"] == "unknown"
    assert statuses["iac.remote_states"] == "unmapped"
    assert statuses["lint.trust_project_venv"] == "unmapped"
    assert "user-level" in next(
        item["message"] for item in report["entries"]
        if item["legacy"] == "lint.trust_project_venv")
    assert statuses["hints.library"] == "invalid"
    assert report["ok"] is False
    assert report["changed"] is False
    assert not (root / "ozagia" / "config.toml").exists()


def test_migrate_preserves_existing_repo_config_and_reports_conflicts(
        tmp_path: Path) -> None:
    root = _root(tmp_path)
    target = _repo_config(root, '# keep this comment\n[index]\nsource_dirs = ["."]\n\n'
                                '[ui]\ntheme = "dark"  # keep theme\n')
    _write(root / ".ozagia.toml",
           '[index]\nsource_dirs = ["src"]\ntest_dirs = ["spec"]\n')
    preview = M.migrate(root)
    statuses = _migrate_statuses(preview)
    assert statuses["index.source_dirs"] == "conflict"
    assert statuses["index.test_dirs"] == "move"
    assert preview["conflicts"] == 1
    assert '+source_dirs' not in preview["diff"]
    assert '+test_dirs = ["spec"]' in preview["diff"]
    applied = M.migrate(root, dry_run=False, confirm=preview["token"])
    assert applied["applied"] is True
    text = target.read_text(encoding="utf-8")
    assert "# keep this comment" in text
    assert 'source_dirs = ["."]' in text
    assert 'test_dirs = ["spec"]' in text
    assert 'theme = "dark"  # keep theme' in text
    assert S.layer_values(root)["index.source_dirs"] == ["."]


def test_migrate_apply_requires_the_preview_token_and_rejects_staleness(
        tmp_path: Path) -> None:
    root = _root(tmp_path)
    legacy = _write(root / ".ozagia.toml", '[index]\nsource_dirs = ["src"]\n')
    target = root / "ozagia" / "config.toml"
    with pytest.raises(M.MigrateError, match="confirm"):
        M.migrate(root, dry_run=False)
    assert not target.exists()

    preview = M.migrate(root)
    assert preview["token"]
    legacy.write_text('[index]\nsource_dirs = ["lib"]\n', encoding="utf-8")
    with pytest.raises(M.MigrateError, match="stale"):
        M.migrate(root, dry_run=False, confirm=preview["token"])
    assert not target.exists()

    fresh = M.migrate(root)
    with pytest.raises(M.MigrateError, match="stale"):
        M.migrate(root, dry_run=False, confirm="not-the-token")
    applied = M.migrate(root, dry_run=False, confirm=fresh["token"])
    assert applied["applied"] is True
    assert S.layer_values(root)["index.source_dirs"] == ["lib"]


def test_migrate_cli_and_config_delegate(tmp_path: Path,
                                        capsys: pytest.CaptureFixture[str]
                                        ) -> None:
    root = _root(tmp_path)
    _write(root / ".ozagia.toml", '[hints]\nlibrary = "strict"\n')
    target = root / "ozagia" / "config.toml"
    assert settings_cli.main(
        ["migrate", "--root", str(root), "--json"]) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["dry_run"] is True
    assert preview["token"]
    assert not target.exists()

    assert settings_cli.main(
        ["migrate", "--root", str(root), "--apply", "--confirm", "wrong"]
    ) == 2
    assert "stale" in capsys.readouterr().err
    assert not target.exists()

    assert settings_cli.main(
        ["migrate", "--root", str(root), "--apply",
         "--confirm", preview["token"]]) == 0
    assert target.is_file()
    assert "wrote" in capsys.readouterr().out
    assert cli.main(["config", "migrate", "--root", str(root)]) == 0


def test_migrate_without_a_legacy_file_is_a_noop(tmp_path: Path) -> None:
    root = _root(tmp_path)
    report = M.migrate(root)
    assert report["legacy_exists"] is False
    assert report["ok"] is True
    assert report["changed"] is False
    assert report["token"] is None
    assert report["entries"] == []
    assert not (root / "ozagia" / "config.toml").exists()
