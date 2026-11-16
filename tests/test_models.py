"""Model registry, configuration overlay, resolution and the models CLI.

Every test injects the discovery runner or mocks ``subprocess.run``. Nothing
here runs the real ``opencode`` CLI, contacts a provider or calls a model.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from ozagia import models_cli
from ozagia.core import models as M


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for name in ("OZAGIA_ROOT", "OZAGIA_CONFIG", "OZAGIA_STATE_DIR",
                 "OZAGIA_USER_CONFIG", "OZAGIA_MODELS_DEFAULT",
                 "OZAGIA_MODELS_LOCAL_ONLY"):
        monkeypatch.delenv(name, raising=False)
    return home


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    return root


def _config(root: Path, text: str) -> Path:
    path = root / "ozagia" / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _user_config(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "user.toml"
    path.write_text(text, encoding="utf-8")
    return path


def _runner(stdout: str = "", *, returncode: int = 0, stderr: str = "",
            raises: BaseException | None = None):
    calls: list[tuple[tuple[str, ...], float]] = []

    def run(argv, timeout):
        calls.append((tuple(argv), timeout))
        if raises is not None:
            raise raises
        return subprocess.CompletedProcess(list(argv), returncode, stdout, stderr)

    run.calls = calls  # type: ignore[attr-defined]
    return run


# --------------------------------------------------------------------------- #
# discovery
# --------------------------------------------------------------------------- #
def test_discovery_parses_ids_and_never_guesses(tmp_path: Path) -> None:
    root = _root(tmp_path)
    output = (
        "\x1b[32mopenai/gpt-5\x1b[0m\n"
        "\n"
        "# a comment\n"
        "local/qwen\n"
        "claude-agent/claude-fable-5-1[1m]\n"
        "this is not a model\n"
        "local/qwen\n"
    )
    runner = _runner(output)
    item = M.catalog(root, runner=runner)

    assert runner.calls == [(("opencode", "models"), M.DISCOVERY_TIMEOUT_S)]  # type: ignore[attr-defined]
    assert item.discovery.status == M.DISCOVERY_AVAILABLE
    assert [record.id for record in item] == [
        "claude-agent/claude-fable-5-1[1m]",
        "local/qwen",
        "openai/gpt-5",
    ]
    for record in item:
        assert record.source == M.SOURCE_OPENCODE
        assert record.status == M.STATUS_AVAILABLE
        assert record.context is None
        assert record.input_cost is None
        assert record.output_cost is None
        assert record.capabilities == ()
        assert record.local is None
        assert record.remote is None
    assert len(item.discovery.warnings) == 1
    assert "opencode models line 6" in item.discovery.warnings[0]


@pytest.mark.parametrize(("exc", "status", "needle"), [
    (FileNotFoundError("opencode"), M.DISCOVERY_UNAVAILABLE, "not available"),
    (subprocess.TimeoutExpired(["opencode", "models"], 60),
     M.DISCOVERY_UNAVAILABLE, "timed out"),
    (OSError("cannot execute"), M.DISCOVERY_UNAVAILABLE, "cannot run"),
    (RuntimeError("injected failure"), M.DISCOVERY_ERROR, "injected failure"),
])
def test_discovery_failures_are_reported_not_raised(
        tmp_path: Path, exc: BaseException, status: str, needle: str) -> None:
    item = M.catalog(_root(tmp_path), runner=_runner(raises=exc))
    assert item.discovery.status == status
    assert needle in (item.discovery.message or "")
    assert item.records == ()


def test_discovery_nonzero_and_empty_output_are_errors(tmp_path: Path) -> None:
    root = _root(tmp_path)
    failed = M.catalog(root, runner=_runner(returncode=2, stderr="boom"))
    assert failed.discovery.status == M.DISCOVERY_ERROR
    assert "boom" in (failed.discovery.message or "")

    empty = M.catalog(root, runner=_runner(returncode=0, stdout="\n# nothing\n"))
    assert empty.discovery.status == M.DISCOVERY_ERROR
    assert "no recognisable" in (empty.discovery.message or "")


def test_discovery_does_not_call_models_or_the_real_cli(
        tmp_path: Path, monkeypatch) -> None:
    def forbidden(*_args, **_kwargs):
        raise AssertionError("subprocess must not run when a runner is injected")

    monkeypatch.setattr(M.subprocess, "run", forbidden)
    runner = _runner("local/qwen\n")
    item = M.catalog(_root(tmp_path), runner=runner)
    assert [record.id for record in item] == ["local/qwen"]
    assert len(runner.calls) == 1  # type: ignore[attr-defined]


def test_default_runner_runs_only_the_listing_command(monkeypatch) -> None:
    calls: dict[str, object] = {}

    def fake_run(argv, **kwargs):
        calls["argv"] = argv
        calls["kwargs"] = kwargs
        return subprocess.CompletedProcess(argv, 0, "local/qwen\n", "")

    monkeypatch.setattr(M.subprocess, "run", fake_run)
    result = M.discover()
    assert result.status == M.DISCOVERY_AVAILABLE
    assert result.models == ("local/qwen",)
    assert calls["argv"] == ["opencode", "models"]
    kwargs = calls["kwargs"]
    assert kwargs["timeout"] == M.DISCOVERY_TIMEOUT_S
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["capture_output"] is True


# --------------------------------------------------------------------------- #
# configuration
# --------------------------------------------------------------------------- #
def test_configuration_defaults_fallback_tasks_and_local(
        tmp_path: Path) -> None:
    root = _root(tmp_path)
    _config(root, """
[models]
default = "openai/gpt-5"
fallback = "ollama/qwen"
local_only = false

[models.tasks]
diff_review = "openai/gpt-5-mini"
paper_triage = "ollama/qwen"

[models.local]
providers = ["ollama"]
models = ["vllm/private"]
""")
    settings = M.load_settings(root)
    assert settings.default == "openai/gpt-5"
    assert settings.fallback == "ollama/qwen"
    assert settings.local_only is False
    assert dict(settings.tasks) == {
        "diff_review": "openai/gpt-5-mini",
        "paper_triage": "ollama/qwen",
    }
    assert settings.task_model("diff_review") == "openai/gpt-5-mini"
    assert settings.task_model("unknown") is None
    assert settings.source("default") == "repo"
    assert settings.task_source("diff_review") == "repo"
    assert settings.local_providers == ("ollama",)
    assert settings.local_models == ("vllm/private",)
    assert settings.is_local("ollama/qwen", "ollama") is True
    assert settings.is_local("vllm/private", "vllm") is True
    assert settings.is_local("openai/gpt-5", "openai") is None
    assert settings.config_files == (
        str(root.resolve() / "ozagia" / "config.toml"),)


def test_by_task_class_alias_and_both_tables_rejected(tmp_path: Path) -> None:
    root = _root(tmp_path)
    _config(root, '[models.by_task_class]\ndiff_review = "local/qwen"\n')
    assert M.load_settings(root).task_model("diff_review") == "local/qwen"

    _config(root, """
[models.tasks]
diff_review = "local/one"

[models.by_task_class]
diff_review = "local/two"
""")
    with pytest.raises(M.ModelConfigError, match="not both"):
        M.load_settings(root)


def test_precedence_user_repo_env_override(tmp_path: Path, monkeypatch) -> None:
    root = _root(tmp_path)
    user = _user_config(tmp_path, """
[models]
default = "user/one"
fallback = "user/two"

[models.tasks]
paper_triage = "user/three"
diff_review = "user/four"

[models.local]
providers = ["ollama"]
""")
    monkeypatch.setenv("OZAGIA_USER_CONFIG", str(user))
    _config(root, """
[models]
default = "repo/one"

[models.tasks]
diff_review = "repo/five"
reference_screen = "repo/six"

[models.local]
models = ["vllm/seven"]
""")
    settings = M.load_settings(root)
    assert (settings.default, settings.source("default")) == (
        "repo/one", "repo")
    assert (settings.fallback, settings.source("fallback")) == (
        "user/two", "user")
    assert dict(settings.tasks) == {
        "diff_review": "repo/five",
        "paper_triage": "user/three",
        "reference_screen": "repo/six",
    }
    assert settings.task_source("diff_review") == "repo"
    assert settings.task_source("paper_triage") == "user"
    assert settings.local_providers == ("ollama",)
    assert settings.local_models == ("vllm/seven",)

    monkeypatch.setenv("OZAGIA_MODELS_DEFAULT", "env/one")
    monkeypatch.setenv("OZAGIA_MODELS_LOCAL_ONLY", "1")
    settings = M.load_settings(root)
    assert (settings.default, settings.source("default")) == (
        "env/one", "env")
    assert (settings.local_only, settings.source("local_only")) == (
        True, "env")

    settings = M.load_settings(root, overrides={
        "default": "override/one",
        "tasks": {"diff_review": "override/two"},
        "local": {"providers": ["lmstudio"]},
    })
    assert (settings.default, settings.source("default")) == (
        "override/one", "override")
    assert settings.task_model("diff_review") == "override/two"
    assert settings.task_source("diff_review") == "override"
    assert settings.task_model("paper_triage") == "user/three"
    assert settings.local_providers == ("lmstudio", "ollama")


@pytest.mark.parametrize(("text", "needle"), [
    ('[models]\ndefault = "gpt-5"\n', "provider/model"),
    ('[models]\ndefault = 3\n', "must be a string"),
    ('[models]\nlocal_only = "yes"\n', "true or false"),
    ('[models]\nunknown_key = "local/qwen"\n', "unknown key"),
    ('[models.tasks]\ndiff_review = ""\n', "must not be empty"),
    ('[models.tasks]\n"bad class" = "local/qwen"\n', "task class"),
    ('[models.local]\nproviders = ["bad/provider"]\n', "provider"),
    ('[models.local]\nother = []\n', "unknown key"),
    ('[models]\ndefault = "bad id/x"\n', "whitespace"),
])
def test_invalid_configuration_is_rejected(
        tmp_path: Path, text: str, needle: str) -> None:
    root = _root(tmp_path)
    _config(root, text)
    with pytest.raises(M.ModelConfigError, match=needle):
        M.load_settings(root)


def test_malformed_toml_and_models_table_type(tmp_path: Path) -> None:
    root = _root(tmp_path)
    _config(root, "[[[")
    with pytest.raises(M.ModelConfigError, match="config.toml"):
        M.load_settings(root)

    _config(root, 'models = "not a table"\n')
    with pytest.raises(M.ModelConfigError, match=r"\[models\] must be a table"):
        M.load_settings(root)


def test_unknown_key_has_a_suggestion(tmp_path: Path) -> None:
    root = _root(tmp_path)
    _config(root, '[models]\ndefualt = "local/qwen"\n')
    with pytest.raises(M.ModelConfigError, match="did you mean 'default'"):
        M.load_settings(root)


def test_unrelated_sections_are_left_to_other_loaders(tmp_path: Path) -> None:
    root = _root(tmp_path)
    _config(root, """
[ui]
theme = "dark"

[resources.limits]
job_memory_mb = 512

[models]
default = "local/qwen"
""")
    assert M.load_settings(root).default == "local/qwen"


def test_override_keys_and_duplicates(tmp_path: Path) -> None:
    root = _root(tmp_path)
    settings = M.load_settings(root, overrides={"models.default": "local/qwen"})
    assert settings.default == "local/qwen"
    with pytest.raises(M.ModelConfigError, match="unknown key"):
        M.load_settings(root, overrides={"defaultt": "local/qwen"})
    with pytest.raises(M.ModelConfigError, match="defined twice"):
        M.load_settings(root, overrides={
            "default": "local/one", "models.default": "local/two"})


# --------------------------------------------------------------------------- #
# catalog
# --------------------------------------------------------------------------- #
def test_catalog_merges_discovery_with_configured_references(
        tmp_path: Path) -> None:
    root = _root(tmp_path)
    _config(root, """
[models]
default = "openai/gpt-5"
fallback = "missing/two"
local_only = false

[models.tasks]
diff_review = "missing/three"

[models.local]
models = ["vllm/private"]
""")
    item = M.catalog(root, runner=_runner(
        "openai/gpt-5\nollama/qwen\n"))
    assert [record.id for record in item] == [
        "missing/three", "missing/two", "ollama/qwen", "openai/gpt-5",
        "vllm/private",
    ]
    gpt = item.get("openai/gpt-5")
    assert gpt is not None
    assert gpt.source == M.SOURCE_OPENCODE
    assert gpt.status == M.STATUS_AVAILABLE
    assert gpt.references == ("default",)

    missing = item.get("missing/three")
    assert missing is not None
    assert missing.source == M.SOURCE_CONFIG
    assert missing.status == M.STATUS_UNKNOWN
    assert missing.references == ("task:diff_review",)

    fallback = item.get("missing/two")
    assert fallback is not None
    assert fallback.source == M.SOURCE_SETTINGS
    assert fallback.status == M.STATUS_UNKNOWN

    local = item.get("vllm/private")
    assert local is not None
    assert local.local is True and local.remote is False
    assert local.source == M.SOURCE_CONFIG
    assert local.status == M.STATUS_UNKNOWN

    assert any("not listed" in warning for warning in item.warnings)
    assert "missing/three" in " ".join(item.warnings)
    assert len(item) == 5
    assert item[0].id == "missing/three"
    assert [record.id for record in list(item)] == [record.id for record in item]


def test_catalog_unknown_warning_is_emitted_once_when_discovery_fails(
        tmp_path: Path) -> None:
    root = _root(tmp_path)
    _config(root, """
[models]
default = "local/one"
fallback = "local/two"
""")
    runner = _runner(raises=FileNotFoundError("opencode"))
    item = M.catalog(root, runner=runner)
    assert item.discovery.status == M.DISCOVERY_UNAVAILABLE
    message = item.discovery.message
    assert message is not None
    assert list(item.warnings).count(message) == 1
    assert item.get("local/one").status == M.STATUS_UNKNOWN  # type: ignore[union-attr]
    assert item.get("local/two").source == M.SOURCE_SETTINGS  # type: ignore[union-attr]


def test_override_records_have_override_source(tmp_path: Path) -> None:
    root = _root(tmp_path)
    item = M.catalog(root, runner=_runner("local/qwen\n"), overrides={
        "default": "override/one",
        "tasks": {"diff_review": "override/two"},
    })
    first = item.get("override/one")
    second = item.get("override/two")
    assert first is not None and first.source == M.SOURCE_OVERRIDE
    assert second is not None and second.source == M.SOURCE_OVERRIDE
    assert first.references == ("default",)
    assert second.references == ("task:diff_review",)


def test_override_local_provider_keeps_the_local_model_config_source(
        tmp_path: Path) -> None:
    root = _root(tmp_path)
    _config(root, """
[models.local]
models = ["vllm/private"]
""")
    item = M.catalog(root, runner=_runner("local/qwen\n"), overrides={
        "local": {"providers": ["lmstudio"]}})
    record = item.get("vllm/private")
    assert record is not None
    assert record.local is True
    assert record.source == M.SOURCE_CONFIG
    assert dict(item.settings.local_model_sources) == {
        "vllm/private": "repo"}


def test_catalog_and_resolution_write_nothing(tmp_path: Path) -> None:
    root = _root(tmp_path)
    _config(root, '[models]\ndefault = "local/qwen"\n')
    before = sorted(path.relative_to(root).as_posix()
                    for path in root.rglob("*") if path.is_file())
    runner = _runner("local/qwen\n")
    M.catalog(root, runner=runner)
    M.get(root, "local/qwen", runner=runner)
    M.resolve(root, "diff_review", runner=runner)
    after = sorted(path.relative_to(root).as_posix()
                   for path in root.rglob("*") if path.is_file())
    assert after == before
    assert not (root / ".ozagia").exists()


# --------------------------------------------------------------------------- #
# resolution
# --------------------------------------------------------------------------- #
def test_task_mapping_wins_then_default(tmp_path: Path) -> None:
    root = _root(tmp_path)
    _config(root, """
[models]
default = "openai/gpt-5"
fallback = "ollama/qwen"

[models.tasks]
diff_review = "openai/gpt-5-mini"
""")
    runner = _runner("openai/gpt-5\nopenai/gpt-5-mini\nollama/qwen\n")
    task = M.resolve(root, "diff_review", runner=runner)
    assert task.status == M.RESOLUTION_RESOLVED
    assert task.source == "task"
    assert task.selected is not None and task.selected.id == "openai/gpt-5-mini"
    assert task.requested == "openai/gpt-5-mini"
    assert task.requested_role == "task"
    assert task.requested_layer == "repo"
    assert task.fallback_used is False
    assert [candidate.role for candidate in task.candidates] == ["task"]

    default = M.resolve(root, "paper_triage", runner=runner)
    assert default.source == "default"
    assert default.selected is not None and default.selected.id == "openai/gpt-5"
    assert default.requested_role == "default"


def test_unavailable_default_uses_only_the_explicit_fallback(
        tmp_path: Path) -> None:
    root = _root(tmp_path)
    _config(root, """
[models]
default = "missing/one"
fallback = "ollama/qwen"
""")
    item = M.resolve(root, runner=_runner("ollama/qwen\n"))
    assert item.status == M.RESOLUTION_RESOLVED
    assert item.source == "fallback"
    assert item.fallback_used is True
    assert item.selected is not None and item.selected.id == "ollama/qwen"
    assert "not listed" in item.reason
    assert [candidate.status for candidate in item.candidates] == [
        M.STATUS_UNKNOWN, M.STATUS_AVAILABLE]
    assert [candidate.selected for candidate in item.candidates] == [False, True]


def test_unresolved_default_reports_the_unknown_model(tmp_path: Path) -> None:
    root = _root(tmp_path)
    _config(root, '[models]\ndefault = "missing/one"\n')
    item = M.resolve(root, runner=_runner("ollama/qwen\n"))
    assert item.status == M.RESOLUTION_UNRESOLVED
    assert item.selected is None
    assert item.requested == "missing/one"
    assert "no fallback is configured" in item.reason
    assert item.warnings and "missing/one" in item.warnings[0]


def test_no_configured_default_is_host_default(tmp_path: Path) -> None:
    root = _root(tmp_path)
    item = M.resolve(root, runner=_runner("ollama/qwen\n"))
    assert item.status == M.RESOLUTION_HOST_DEFAULT
    assert item.source == "host"
    assert item.selected is None
    assert item.requested is None
    assert item.candidates == ()
    assert "host CLI" in item.reason


def test_configured_model_is_selected_unknown_when_discovery_fails(
        tmp_path: Path) -> None:
    root = _root(tmp_path)
    _config(root, '[models]\ndefault = "local/qwen"\n')
    item = M.resolve(root, runner=_runner(
        raises=FileNotFoundError("opencode")))
    assert item.status == M.RESOLUTION_UNKNOWN
    assert item.selected is not None and item.selected.id == "local/qwen"
    assert item.selected.status == M.STATUS_UNKNOWN
    assert "not verified" in item.reason
    assert item.discovery.status == M.DISCOVERY_UNAVAILABLE


def test_local_only_filters_remote_and_uses_local_fallback(
        tmp_path: Path) -> None:
    root = _root(tmp_path)
    _config(root, """
[models]
default = "remote/big"
fallback = "ollama/qwen"
local_only = true

[models.local]
providers = ["ollama"]
""")
    item = M.resolve(root, runner=_runner("remote/big\nollama/qwen\n"))
    assert item.status == M.RESOLUTION_RESOLVED
    assert item.source == "fallback"
    assert item.selected is not None and item.selected.id == "ollama/qwen"
    assert "local_only" in item.candidates[0].reason
    assert item.candidates[0].status == "filtered"


def test_local_only_without_a_local_model_is_unresolved(
        tmp_path: Path) -> None:
    root = _root(tmp_path)
    _config(root, """
[models]
default = "remote/big"
local_only = true
""")
    item = M.resolve(root, runner=_runner("remote/big\n"))
    assert item.status == M.RESOLUTION_UNRESOLVED
    assert item.selected is None
    assert "local_only" in item.reason
    assert "not declared local" in item.reason


def test_local_only_can_use_a_declared_local_model_without_discovery(
        tmp_path: Path) -> None:
    root = _root(tmp_path)
    _config(root, """
[models]
default = "local/qwen"
local_only = true

[models.local]
providers = ["local"]
""")
    item = M.resolve(root, runner=_runner(
        raises=FileNotFoundError("opencode")))
    assert item.status == M.RESOLUTION_UNKNOWN
    assert item.selected is not None and item.selected.id == "local/qwen"
    assert item.selected.local is True


def test_local_only_refuses_undeclared_locality_without_discovery(
        tmp_path: Path) -> None:
    root = _root(tmp_path)
    _config(root, """
[models]
default = "google/gemini"
local_only = true
""")
    item = M.resolve(root, runner=_runner(
        raises=FileNotFoundError("opencode")))
    assert item.status == M.RESOLUTION_UNRESOLVED
    assert item.selected is None
    assert "local_only" in item.reason


def test_same_fallback_as_default_is_not_silently_accepted(
        tmp_path: Path) -> None:
    root = _root(tmp_path)
    _config(root, """
[models]
default = "missing/one"
fallback = "missing/one"
""")
    item = M.resolve(root, runner=_runner("local/qwen\n"))
    assert item.status == M.RESOLUTION_UNRESOLVED
    assert item.selected is None
    assert "same model" in item.reason
    assert len(item.candidates) == 1


def test_override_default_is_reflected_in_resolution(tmp_path: Path) -> None:
    root = _root(tmp_path)
    item = M.resolve(root, runner=_runner("override/one\n"), overrides={
        "default": "override/one"})
    assert item.status == M.RESOLUTION_RESOLVED
    assert item.source == "default"
    assert item.requested_layer == "override"
    assert item.selected is not None and item.selected.id == "override/one"
    # Discovery is the record's source; the override is its config provenance.
    assert item.selected.source == M.SOURCE_OPENCODE
    assert item.selected.references == ("default",)


def test_invalid_task_class_is_a_configuration_error(tmp_path: Path) -> None:
    root = _root(tmp_path)
    with pytest.raises(M.ModelConfigError, match="task class"):
        M.resolve(root, "not a class", runner=_runner("local/qwen\n"))


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def test_cli_list_json_and_text(tmp_path: Path, monkeypatch, capsys) -> None:
    root = _root(tmp_path)
    _config(root, '[models]\ndefault = "openai/gpt-5"\n')
    monkeypatch.setattr(M, "default_runner",
                        _runner("openai/gpt-5\nollama/qwen\n"))

    assert models_cli.main(["list", "--root", str(root), "--json"]) == 0
    captured = capsys.readouterr()
    doc = json.loads(captured.out)
    assert doc["schema"] == M.SCHEMA
    assert doc["discovery"]["status"] == M.DISCOVERY_AVAILABLE
    assert [record["id"] for record in doc["models"]] == [
        "ollama/qwen", "openai/gpt-5"]
    assert doc["settings"]["default"] == "openai/gpt-5"
    assert doc["warnings"] == []
    assert captured.err == ""

    assert models_cli.main(["list", "--root", str(root)]) == 0
    text = capsys.readouterr().out
    assert "discovery: available" in text
    assert "openai/gpt-5" in text
    assert "status=available" in text


def test_cli_show_text_json_and_unknown(
        tmp_path: Path, monkeypatch, capsys) -> None:
    root = _root(tmp_path)
    _config(root, '[models]\ndefault = "openai/gpt-5"\n')
    monkeypatch.setattr(M, "default_runner", _runner("openai/gpt-5\n"))

    assert models_cli.main(["show", "openai/gpt-5", "--root", str(root)]) == 0
    text = capsys.readouterr().out
    assert "id: openai/gpt-5" in text
    assert "status: available" in text
    assert "references: default" in text

    assert models_cli.main([
        "show", "openai/gpt-5", "--root", str(root), "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["model"]["id"] == "openai/gpt-5"
    assert doc["model"]["source"] == M.SOURCE_OPENCODE

    assert models_cli.main(["show", "unknown/model", "--root", str(root)]) == 2
    captured = capsys.readouterr()
    assert "not listed or configured" in captured.err


def test_cli_show_reports_a_configured_unknown_model_as_such(
        tmp_path: Path, monkeypatch, capsys) -> None:
    root = _root(tmp_path)
    _config(root, '[models]\ndefault = "missing/one"\n')
    monkeypatch.setattr(M, "default_runner", _runner("local/qwen\n"))
    assert models_cli.main([
        "show", "missing/one", "--root", str(root), "--json"]) == 0
    captured = capsys.readouterr()
    doc = json.loads(captured.out)
    assert doc["model"]["status"] == M.STATUS_UNKNOWN
    assert any("not listed" in warning for warning in doc["warnings"])
    assert captured.err == ""


def test_cli_resolve_json_and_text(tmp_path: Path, monkeypatch, capsys) -> None:
    root = _root(tmp_path)
    _config(root, """
[models]
default = "openai/gpt-5"

[models.tasks]
diff_review = "ollama/qwen"
""")
    monkeypatch.setattr(M, "default_runner", _runner(
        "openai/gpt-5\nollama/qwen\n"))

    assert models_cli.main([
        "resolve", "--task", "diff_review", "--root", str(root), "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["status"] == M.RESOLUTION_RESOLVED
    assert doc["selected"]["id"] == "ollama/qwen"
    assert doc["source"] == "task"
    assert doc["candidates"][0]["selected"] is True

    assert models_cli.main([
        "resolve", "--task", "diff_review", "--root", str(root)]) == 0
    text = capsys.readouterr().out
    assert "selected: ollama/qwen" in text
    assert "source: task" in text
    assert "candidates:" in text


def test_cli_unavailable_discovery_is_not_fatal(
        tmp_path: Path, monkeypatch, capsys) -> None:
    root = _root(tmp_path)
    _config(root, '[models]\ndefault = "local/qwen"\n')
    monkeypatch.setattr(M, "default_runner", _runner(
        raises=FileNotFoundError("opencode")))

    assert models_cli.main(["list", "--root", str(root), "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["discovery"]["status"] == M.DISCOVERY_UNAVAILABLE
    assert doc["models"][0]["status"] == M.STATUS_UNKNOWN
    assert doc["warnings"]


def test_cli_resolve_unresolved_is_still_exit_0(
        tmp_path: Path, monkeypatch, capsys) -> None:
    root = _root(tmp_path)
    _config(root, '[models]\ndefault = "missing/one"\n')
    monkeypatch.setattr(M, "default_runner", _runner("local/qwen\n"))
    assert models_cli.main([
        "resolve", "--root", str(root), "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["status"] == M.RESOLUTION_UNRESOLVED
    assert doc["selected"] is None
    assert doc["requested"]["id"] == "missing/one"


def test_cli_show_invalid_id_exits_2(tmp_path: Path, monkeypatch, capsys) -> None:
    root = _root(tmp_path)
    monkeypatch.setattr(M, "default_runner", _runner("local/qwen\n"))
    assert models_cli.main(["show", "not a model", "--root", str(root)]) == 2
    captured = capsys.readouterr()
    assert "must not contain whitespace" in captured.err
    assert captured.out == ""


def test_cli_validation_error_exits_2(tmp_path: Path, monkeypatch, capsys) -> None:
    root = _root(tmp_path)
    _config(root, '[models]\ndefault = "gpt-5"\n')
    monkeypatch.setattr(M, "default_runner", _runner("local/qwen\n"))
    assert models_cli.main(["list", "--root", str(root)]) == 2
    captured = capsys.readouterr()
    assert "provider/model" in captured.err
    assert captured.out == ""


def test_cli_does_not_run_subprocess_when_the_module_runner_is_patched(
        tmp_path: Path, monkeypatch, capsys) -> None:
    root = _root(tmp_path)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("no subprocess expected")

    monkeypatch.setattr(M.subprocess, "run", forbidden)
    monkeypatch.setattr(M, "default_runner", _runner("local/qwen\n"))
    assert models_cli.main(["list", "--root", str(root), "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert [record["id"] for record in doc["models"]] == ["local/qwen"]


# --------------------------------------------------------------------------- #
# ids
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("model_id", [
    "openai/gpt-5",
    "claude-agent/claude-fable-5-1[1m]",
    "local/qwen#thinking",
    "provider/model:free",
    "a/b/c",
])
def test_valid_model_ids(model_id: str) -> None:
    assert M.validate_model_id(model_id) == model_id


@pytest.mark.parametrize("model_id", [
    "", "gpt-5", "/model", "provider/", "provider/model/",
    "provider//model", "bad id/model", "provider/a\tb",
    "x" * 201 + "/model",
])
def test_invalid_model_ids(model_id: str) -> None:
    with pytest.raises(M.ModelConfigError):
        M.validate_model_id(model_id)
    assert M.validate_model_id("", allow_empty=True) == ""
