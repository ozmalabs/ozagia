from __future__ import annotations

import json
import subprocess

from ozagia import cli
from ozagia.core.memory import build_index


def test_doctor_reports_missing_fresh_and_stale(tmp_path, monkeypatch, capsys):
    for name in ("OZAGIA_CONFIG", "OZAGIA_ROOT", "OZAGIA_STATE_DIR", "OZAGIA_ADAPTERS"):
        monkeypatch.delenv(name, raising=False)
    def report():
        assert cli.main(["doctor", "--root", str(tmp_path), "--json"]) == 0
        return json.loads(capsys.readouterr().out)

    assert report()["snapshot"]["state"] == "missing"
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "Test"], check=True)
    (tmp_path / "module.py").write_text("def example():\n    return 1\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "module.py"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "initial"], check=True)
    build_index(tmp_path, tmp_path / ".ozagia" / "graph.json")
    rep = report()
    assert rep["snapshot"]["state"] == "fresh"
    assert rep["parsers"]["python"]["available"]
    (tmp_path / "module.py").write_text("def example():\n    return 2\n")
    assert report()["snapshot"]["state"] == "stale"


def test_doctor_reports_relay_binaries_without_running_them(tmp_path, monkeypatch, capsys):
    from ozagia import doctor as doctor_module

    monkeypatch.delenv("OZAGIA_CONFIG", raising=False)
    monkeypatch.delenv("OZAGIA_STATE_DIR", raising=False)
    monkeypatch.setattr(
        doctor_module.shutil, "which",
        lambda name: f"/fake/bin/{name}" if name in {"ssh", "aws"} else None)
    assert cli.main(["doctor", "--root", str(tmp_path), "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["external_tools"]["ssh"] == {
        "available": True, "path": "/fake/bin/ssh"}
    assert result["external_tools"]["aws"]["available"] is True
    assert result["external_tools"]["session-manager-plugin"]["available"] is False
    assert "no relay" in result["external_tool_note"]


def test_doctor_invalid_config_and_snapshot(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("OZAGIA_CONFIG", raising=False)
    monkeypatch.delenv("OZAGIA_STATE_DIR", raising=False)
    (tmp_path / ".ozagia.toml").write_text("[broken\n")
    (tmp_path / ".ozagia").mkdir()
    (tmp_path / ".ozagia" / "graph.json").write_text("bad json")
    assert cli.main(["doctor", "--root", str(tmp_path), "--json"]) == 2
    result = json.loads(capsys.readouterr().out)
    assert not result["config"]["valid"]
    assert result["snapshot"]["state"] == "invalid"


def test_doctor_does_not_certify_different_index_scope(tmp_path, monkeypatch):
    from ozagia.core import memory as memory_module
    from ozagia.doctor import diagnose

    monkeypatch.delenv("OZAGIA_CONFIG", raising=False)
    monkeypatch.delenv("OZAGIA_STATE_DIR", raising=False)
    monkeypatch.setattr(memory_module, "SOURCE_DIRS", ("another-project",))
    (tmp_path / ".ozagia").mkdir()
    (tmp_path / ".ozagia" / "graph.json").write_text("{}")

    class Memory:
        def __init__(self, **kwargs):
            pass

        def staleness(self):
            return {"freshness": "fresh", "verification_errors": []}

    monkeypatch.setattr(memory_module, "DevMemory", Memory)
    result = diagnose(tmp_path)
    assert result["snapshot"]["state"] == "unknown"
    assert "configuration differs" in result["snapshot"]["details"]["verification_errors"][0]


def _isolate_settings(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    for name in ("OZAGIA_CONFIG", "OZAGIA_STATE_DIR", "OZAGIA_USER_CONFIG",
                 "OZAGIA_ROOT"):
        monkeypatch.delenv(name, raising=False)


def test_doctor_reports_settings_paths_and_sources(tmp_path, monkeypatch, capsys):
    _isolate_settings(tmp_path, monkeypatch)
    (tmp_path / "ozagia").mkdir()
    config = tmp_path / "ozagia" / "config.toml"
    config.write_text(
        '[ui]\ntheme = "dark"\n\n[models.tasks]\ndiff_review = "local/qwen"\n',
        encoding="utf-8")

    assert cli.main(["doctor", "--root", str(tmp_path), "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    settings = result["settings"]
    assert settings["valid"] is True
    assert settings["registry"]["valid"] is True
    assert settings["registry"]["keys"] > 0
    assert settings["locations"]["repo_config"] == str(config.resolve())
    assert settings["locations"]["repo_config_exists"] is True
    assert settings["source_counts"]["repo"] == 2
    non_default = {item["key"]: item["source"]
                   for item in settings["non_default"]}
    assert non_default["ui.theme"] == "repo"
    assert non_default["models.tasks.diff_review"] == "repo"
    assert settings["non_default_truncated"] is False

    assert cli.main(["doctor", "--root", str(tmp_path)]) == 0
    text = capsys.readouterr().out
    assert "Settings: valid" in text
    assert "repo=2" in text


def test_doctor_reports_invalid_settings_without_crashing(
        tmp_path, monkeypatch, capsys):
    _isolate_settings(tmp_path, monkeypatch)
    (tmp_path / "ozagia").mkdir()
    config = tmp_path / "ozagia" / "config.toml"
    config.write_text('[index]\nsource_dir = ["."]\n', encoding="utf-8")

    # The project config (.ozagia.toml) is still valid, so the exit status is
    # unchanged; the settings section reports the registry error.
    assert cli.main(["doctor", "--root", str(tmp_path), "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    settings = result["settings"]
    assert settings["valid"] is False
    assert "index.source_dir" in settings["error"]
    assert settings["errors"]
    assert settings["locations"]["repo_config"] == str(config.resolve())

    assert cli.main(["doctor", "--root", str(tmp_path)]) == 0
    assert "Settings: invalid" in capsys.readouterr().out


def test_doctor_reports_both_config_paths_precedence_and_unmapped(
        tmp_path, monkeypatch, capsys):
    from ozagia.doctor import CONFIG_UNMAPPED_LIMIT

    _isolate_settings(tmp_path, monkeypatch)
    legacy = tmp_path / ".ozagia.toml"
    legacy.write_text('[index]\nsource_dirs = ["src"]\n', encoding="utf-8")
    repo = tmp_path / "ozagia" / "config.toml"
    repo.parent.mkdir()
    repo.write_text(
        '[index]\nskip_dirs = ["vendor"]\n\n[hints]\nlibrary = "strict"\n',
        encoding="utf-8")

    assert cli.main(["doctor", "--root", str(tmp_path), "--json"]) == 0
    config = json.loads(capsys.readouterr().out)["config"]
    assert config["valid"] is True
    assert config["path"] == str(legacy.resolve())
    assert config["legacy_path"] == str(legacy.resolve())
    assert config["legacy_kind"] == "root"
    assert config["repo_path"] == str(repo.resolve())
    assert config["repo_exists"] is True
    assert config["active_source"] == "legacy"
    assert config["precedence"] == ["legacy", "repo"]
    assert config["sources"] == [str(legacy.resolve()), str(repo.resolve())]
    assert config["unmapped"] == [
        {"key": "hints", "reason": "read by ozagia.core.library_hints"}]
    assert config["unmapped_count"] == 1
    assert config["unmapped_truncated"] is False
    assert CONFIG_UNMAPPED_LIMIT >= 1


def test_doctor_active_source_is_the_versioned_config_without_a_legacy_file(
        tmp_path, monkeypatch, capsys):
    _isolate_settings(tmp_path, monkeypatch)
    repo = tmp_path / "ozagia" / "config.toml"
    repo.parent.mkdir()
    repo.write_text('[index]\nsource_dirs = ["src"]\n', encoding="utf-8")

    assert cli.main(["doctor", "--root", str(tmp_path), "--json"]) == 0
    config = json.loads(capsys.readouterr().out)["config"]
    assert config["legacy_path"] is None
    assert config["active_source"] == "repo"
    assert config["path"] == str(repo.resolve())
    assert config["precedence"] == ["repo"]


def test_doctor_bounds_the_unmapped_config_keys(
        tmp_path, monkeypatch, capsys):
    from ozagia.doctor import CONFIG_UNMAPPED_LIMIT

    _isolate_settings(tmp_path, monkeypatch)
    repo = tmp_path / "ozagia" / "config.toml"
    repo.parent.mkdir()
    unknown = "\n".join(f"unknown_{index} = true"
                        for index in range(CONFIG_UNMAPPED_LIMIT + 5))
    repo.write_text(f"[index]\n{unknown}\n", encoding="utf-8")

    # The project config ignores the unknown keys (and reports them), so the
    # project-config validity and the exit status are unchanged.
    assert cli.main(["doctor", "--root", str(tmp_path), "--json"]) == 0
    config = json.loads(capsys.readouterr().out)["config"]
    assert config["valid"] is True
    assert len(config["unmapped"]) == CONFIG_UNMAPPED_LIMIT
    assert config["unmapped_count"] == CONFIG_UNMAPPED_LIMIT + 5
    assert config["unmapped_truncated"] is True


def test_doctor_reports_an_invalid_versioned_config(tmp_path, monkeypatch,
                                                   capsys):
    _isolate_settings(tmp_path, monkeypatch)
    repo = tmp_path / "ozagia" / "config.toml"
    repo.parent.mkdir()
    repo.write_text('[index]\nsource_dirs = ["../outside"]\n',
                    encoding="utf-8")

    assert cli.main(["doctor", "--root", str(tmp_path), "--json"]) == 2
    config = json.loads(capsys.readouterr().out)["config"]
    assert config["valid"] is False
    assert config["active_source"] == "repo"
    assert str(repo.resolve()) in config["error"]
