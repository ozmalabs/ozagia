"""Legacy project-model migration into ``ozagia/config.toml``.

The four named-table sections (``environments``, ``services``, ``relays`` and
``runtime``) migrate entry by entry, validated by the same parser the project
loader uses. Unrelated TOML and comments are preserved, and the legacy file is
never modified.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ozagia import config as project_config
from ozagia import settings_cli
from ozagia.core import migrate as M
from ozagia.core import settings as S

FIXTURES = Path(__file__).parent / "fixtures" / "config_migration"


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for name in ("OZAGIA_ROOT", "OZAGIA_CONFIG", "OZAGIA_STATE_DIR",
                 "OZAGIA_USER_CONFIG", *S.ENV_SETTINGS):
        monkeypatch.delenv(name, raising=False)
    project_config.clear_config_cache()
    return home


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    return root


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _statuses(report: dict) -> dict[str, str]:
    return {item["legacy"]: item["status"] for item in report["entries"]}


def test_migrate_copies_all_project_models_and_preserves_unrelated_toml(
        tmp_path: Path) -> None:
    root = _root(tmp_path)
    legacy = _write(root / ".ozagia.toml", _fixture("legacy.toml"))
    target = _write(root / "ozagia" / "config.toml", _fixture("target.toml"))
    legacy_before = legacy.read_text(encoding="utf-8")
    target_before = target.read_text(encoding="utf-8")

    preview = M.migrate(root)
    statuses = _statuses(preview)
    for label in (
            "environments.dev", "environments.staging",
            "services.api", "services.worker", "relays.prod",
            "runtime.services.api", "runtime.services.migrate",
            "runtime.tunnels.database"):
        assert statuses[label] == "move", label
    assert preview["moved_tables"] == [
        "environments.dev", "environments.staging",
        "services.api", "services.worker", "relays.prod",
        "runtime.services.api", "runtime.services.migrate",
        "runtime.tunnels.database",
    ]
    assert preview["ok"] is True
    assert preview["changed"] is True
    assert preview["token"]
    assert target.read_text(encoding="utf-8") == target_before
    assert legacy.read_text(encoding="utf-8") == legacy_before
    assert "[runtime.services.api]" in preview["diff"]
    assert "max_timeout_s = 300.5" in preview["diff"]

    applied = M.migrate(root, dry_run=False, confirm=preview["token"])
    assert applied["applied"] is True
    assert applied["written_tables"] == preview["moved_tables"]
    text = target.read_text(encoding="utf-8")
    assert legacy.read_text(encoding="utf-8") == legacy_before
    assert "# Existing versioned configuration." in text
    assert "# keep the inline comment" in text
    assert '[ui]\ntheme = "dark"  # keep theme' in text
    assert '[environments.dev]\npaths = ["envs/dev"]' in text
    assert '[services.api]\nchart = "charts/api"' in text
    assert '[relays.prod]' in text
    assert '[runtime.services.api]' in text
    assert '[runtime.tunnels.database]' in text
    # Nested runtime tables, arrays of tables, nested inline tables, lists,
    # booleans, numbers and strings all round-trip.
    assert 'env = { LOG_LEVEL = "info", API_TOKEN = "secret://env/API_TOKEN" }' in text
    assert 'strict = true' in text and 'port = 22' in text
    assert 'command_allowlist = [["uptime"], ["kubectl", "get"]]' in text
    assert S.layer_values(root)["index.source_dirs"] == ["src"]

    # The legacy file still wins entry by entry; the versioned-only worker is
    # appended by the loader.
    project_config.clear_config_cache()
    cfg = project_config.load_project_config(root)
    assert [item.name for item in cfg.environments] == ["dev", "staging"]
    assert [item.name for item in cfg.services] == ["api", "worker"]
    assert [item.name for item in cfg.relays] == ["prod"]
    assert [item.name for item in cfg.runtime_services] == [
        "api", "migrate", "worker"]
    assert [item.name for item in cfg.runtime_tunnels] == ["database"]

    # The migrated file is the whole project model when the legacy file goes.
    legacy.unlink()
    project_config.clear_config_cache()
    migrated = project_config.load_project_config(root)
    assert [item.name for item in migrated.environments] == ["dev", "staging"]
    assert [item.name for item in migrated.services] == ["api", "worker"]
    assert [item.name for item in migrated.relays] == ["prod"]
    assert [item.name for item in migrated.runtime_services] == [
        "worker", "api", "migrate"]
    assert [item.name for item in migrated.runtime_tunnels] == ["database"]
    assert migrated.relays[0].max_output_bytes == 4096
    assert migrated.runtime_tunnels[0].target == "db.internal.example:5432"


def test_conflicting_entry_blocks_project_tables_and_legacy_still_wins(
        tmp_path: Path) -> None:
    root = _root(tmp_path)
    _write(root / ".ozagia.toml", """
[environments.dev]
paths = ["envs/legacy"]
region = "us-east-1"

[environments.staging]
paths = ["envs/staging"]

[runtime.services.api]
command = ["python", "-m", "app"]
""")
    target = _write(root / "ozagia" / "config.toml", """
# keep this comment
[environments.dev]
paths = ["envs/versioned"]
cluster = "versioned-cluster"

[runtime.services.worker]
command = ["python", "-m", "worker"]
""")
    before = target.read_text(encoding="utf-8")

    preview = M.migrate(root)
    statuses = _statuses(preview)
    assert statuses["environments.dev"] == "conflict"
    assert statuses["environments.staging"] == "move"
    assert statuses["runtime.services.api"] == "move"
    # A conflict in a project model writes no project-model table at all.
    assert preview["moved_tables"] == []
    assert preview["changed"] is False
    assert preview["token"] is None
    assert "different entry" in next(
        item["message"] for item in preview["entries"]
        if item["legacy"] == "environments.dev")
    assert target.read_text(encoding="utf-8") == before

    noop = M.migrate(root, dry_run=False)
    assert noop["applied"] is False
    assert target.read_text(encoding="utf-8") == before

    # The legacy entry wins as a whole: no versioned field is merged in.
    project_config.clear_config_cache()
    cfg = project_config.load_project_config(root)
    assert [(item.name, item.paths, item.region, item.cluster)
            for item in cfg.environments] == [
        ("dev", ("envs/legacy",), "us-east-1", None),
        ("staging", ("envs/staging",), None, None),
    ]
    assert [item.name for item in cfg.runtime_services] == ["api", "worker"]


def test_same_entry_and_versioned_only_names_are_left_alone(
        tmp_path: Path) -> None:
    root = _root(tmp_path)
    _write(root / ".ozagia.toml",
           '[environments.dev]\npaths = ["envs/dev"]\n')
    target = _write(root / "ozagia" / "config.toml", """
[environments.dev]
paths = ["envs/dev"]

[environments.staging]
paths = ["envs/staging"]

[runtime.services.worker]
command = ["python", "-m", "worker"]
""")
    before = target.read_text(encoding="utf-8")

    preview = M.migrate(root)
    assert _statuses(preview) == {"environments.dev": "same"}
    assert preview["changed"] is False
    assert preview["token"] is None
    assert preview["diff"] == ""
    assert target.read_text(encoding="utf-8") == before

    project_config.clear_config_cache()
    cfg = project_config.load_project_config(root)
    assert [item.name for item in cfg.environments] == ["dev", "staging"]
    assert [item.name for item in cfg.runtime_services] == ["worker"]


def test_invalid_and_unknown_project_entries_name_file_and_entry(
        tmp_path: Path) -> None:
    root = _root(tmp_path)
    legacy = _write(root / ".ozagia.toml", _fixture("invalid.toml"))
    report = M.migrate(root)
    statuses = _statuses(report)
    assert statuses["environments.dev"] == "invalid"
    assert statuses["services.api"] == "invalid"
    assert statuses["relays.prod"] == "invalid"
    assert statuses["runtime.services.api"] == "invalid"
    assert statuses["runtime.tunnels.database"] == "invalid"
    assert statuses["runtime.other"] == "unknown"
    assert report["ok"] is False
    assert report["changed"] is False
    assert report["token"] is None
    assert report["moved_tables"] == []
    for label in ("environments.dev", "services.api", "relays.prod",
                  "runtime.services.api", "runtime.tunnels.database"):
        message = next(item["message"] for item in report["entries"]
                       if item["legacy"] == label)
        assert str(legacy) in message, label
        assert label.split(".", 1)[-1] in message, label
    assert not (root / "ozagia" / "config.toml").exists()
    assert legacy.read_text(encoding="utf-8") == _fixture("invalid.toml")


def test_stale_token_refuses_and_apply_writes_atomically_once(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _root(tmp_path)
    legacy = _write(root / ".ozagia.toml",
                    '[environments.dev]\npaths = ["envs/dev"]\n')
    legacy_before = legacy.read_text(encoding="utf-8")
    target = root / "ozagia" / "config.toml"

    preview = M.migrate(root)
    assert preview["token"]
    assert not target.exists()
    target.parent.mkdir(parents=True)
    target.write_text("# another process wrote this\n", encoding="utf-8")
    with pytest.raises(M.MigrateError, match="stale"):
        M.migrate(root, dry_run=False, confirm=preview["token"])
    assert target.read_text(encoding="utf-8") == "# another process wrote this\n"

    fresh = M.migrate(root)
    assert fresh["token"] and fresh["token"] != preview["token"]
    calls: list[Path] = []
    original = M.S._atomic_write

    def counted(path: Path, text: str) -> None:
        calls.append(path)
        original(path, text)

    monkeypatch.setattr(M.S, "_atomic_write", counted)
    applied = M.migrate(root, dry_run=False, confirm=fresh["token"])
    assert applied["applied"] is True
    assert calls == [target]
    assert legacy.read_text(encoding="utf-8") == legacy_before
    text = target.read_text(encoding="utf-8")
    assert "# another process wrote this" in text
    assert '[environments.dev]\npaths = ["envs/dev"]' in text


def test_json_and_text_output_show_project_tables(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _root(tmp_path)
    _write(root / ".ozagia.toml",
           '[environments.dev]\npaths = ["envs/dev"]\n')
    assert settings_cli.main(
        ["migrate", "--root", str(root), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["dry_run"] is True
    assert report["moved_tables"] == ["environments.dev"]
    assert report["token"]
    assert not (root / "ozagia" / "config.toml").exists()

    assert settings_cli.main(["migrate", "--root", str(root)]) == 0
    text = capsys.readouterr().out
    assert "dry run:" in text
    assert "tables: environments.dev" in text
    assert "token=" in text

    assert settings_cli.main(
        ["migrate", "--root", str(root), "--apply",
         "--confirm", report["token"]]) == 0
    assert "wrote" in capsys.readouterr().out
    assert "[environments.dev]" in (
        root / "ozagia" / "config.toml").read_text(encoding="utf-8")


def test_writer_refuses_unserializable_values() -> None:
    with pytest.raises(M.MigrateError, match="cannot serialize"):
        M._render_value(None, where="environments.dev.value")
    with pytest.raises(M.MigrateError, match="finite"):
        M._render_value(float("nan"), where="relays.prod.max_timeout_s")
    with pytest.raises(M.MigrateError, match="TOML range"):
        M._render_value(2 ** 64, where="relays.prod.max_output_bytes")


def test_writer_preserves_crlf_line_endings(tmp_path: Path) -> None:
    root = _root(tmp_path)
    _write(root / ".ozagia.toml",
           '[environments.dev]\npaths = ["envs/dev"]\n')
    target = root / "ozagia" / "config.toml"
    target.parent.mkdir(parents=True)
    target.write_bytes(b'# crlf\r\n[index]\r\ntest_dirs = ["spec"]\r\n')
    preview = M.migrate(root)
    applied = M.migrate(root, dry_run=False, confirm=preview["token"])
    assert applied["applied"] is True
    data = target.read_bytes()
    assert data.count(b"\n") == data.count(b"\r\n")
    assert b"[environments.dev]\r\npaths = [\"envs/dev\"]\r\n" in data
