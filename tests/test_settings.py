"""Typed settings registry: defaults, precedence, writes and the CLI."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from ozagia import settings_cli
from ozagia.core import settings as S


REQUIRED_KEYS = {
    "index.source_dirs", "index.test_dirs", "index.docs", "index.skip_dirs",
    "resources.enabled", "resources.reserve_memory_pct",
    "resources.reserve_disk_pct", "resources.reserve_disk_gb",
    "resources.min_free_memory_pct", "resources.min_free_disk_pct",
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
