"""Migration configuration: schema, root safety and status/settings exposure."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from ozagia.config import (
    ConfigError,
    clear_config_cache,
    load_project_config,
)
from ozagia.core import settings as S
from ozagia.doctor import diagnose

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "py_rust_migration"


def _copy(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    shutil.copytree(FIXTURE, root)
    clear_config_cache()
    return root


def _config(root: Path, text: str) -> Path:
    path = root / ".ozagia.toml"
    path.write_text(text, encoding="utf-8")
    clear_config_cache()
    return path


def test_fixture_migration_config_loads(tmp_path: Path) -> None:
    root = _copy(tmp_path)
    cfg = load_project_config(root)

    assert cfg.migration.configured is True
    assert cfg.migration.python == "python"
    assert cfg.migration.rust == "rust"
    assert cfg.migration.infer is True
    assert [(pair.python, pair.rust, pair.status)
            for pair in cfg.migration.pairs] == [
        ("fixturepy.models.User", "fixturecrate::models::User", "ported"),
        ("fixturepy.models.Widget", "fixturecrate::models::Widget", None),
        ("fixturepy.service.fetch_user",
         "fixturecrate::service::fetch_user", None),
        ("fixturepy.service.checksum",
         "fixturecrate::service::checksum", "in_progress"),
        ("fixturepy.models.legacy_only",
         "fixturecrate::legacy_only", "dropped"),
    ]
    assert cfg.migration.to_dict()["configured"] is True


def test_migration_tree_must_stay_inside_the_repository(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    _config(root, '[migration]\npython = "../outside"\nrust = "."\n')

    with pytest.raises(ConfigError, match="inside the root"):
        load_project_config(root)


def test_migration_tree_symlink_must_not_escape(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "linked").symlink_to(outside, target_is_directory=True)
    _config(root, '[migration]\npython = "linked"\nrust = "."\n')

    with pytest.raises(ConfigError, match="outside the repository root"):
        load_project_config(root)


def test_migration_tree_must_exist(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "python").mkdir()
    _config(root, '[migration]\npython = "python"\nrust = "missing"\n')

    with pytest.raises(ConfigError, match="does not exist"):
        load_project_config(root)


def test_migration_section_requires_both_trees(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "python").mkdir()
    _config(root, '[migration]\npython = "python"\ninfer = true\n')

    with pytest.raises(ConfigError, match="python and rust trees are required"):
        load_project_config(root)


@pytest.mark.parametrize("pairs, message", [
    ('''
[[migration.pairs]]
python = "pkg.A"
rust = "crate::A"
[[migration.pairs]]
python = "pkg.A"
rust = "crate::B"
''', "duplicate python symbol"),
    ('''
[[migration.pairs]]
python = "pkg.A"
rust = "crate::A"
[[migration.pairs]]
python = "pkg.B"
rust = "crate::A"
''', "duplicate rust symbol"),
    ('''
[[migration.pairs]]
python = "pkg.A"
rust = "crate::A"
[[migration.pairs]]
python = "pkg.A"
rust = "crate::A"
''', "duplicate pair"),
    ('''
[[migration.pairs]]
python = "pkg.A"
rust = "crate::A"
status = "almost_done"
''', "unknown status"),
    ('''
[[migration.pairs]]
python = "pkg.A"
''', "python and rust are required"),
    ('''
[[migration.pairs]]
python = "pkg.A"
rust = "crate::A"
extra = true
''', "unknown key"),
])
def test_invalid_pairs_are_rejected(tmp_path: Path, pairs: str,
                                    message: str) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "python").mkdir()
    (root / "rust").mkdir()
    _config(root, '[migration]\npython = "python"\nrust = "rust"\n' + pairs)

    with pytest.raises(ConfigError, match=message):
        load_project_config(root)


def test_dynamic_versioned_section_exposes_settings_and_doctor(
        tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "py").mkdir()
    (root / "rs").mkdir()
    path = root / "ozagia" / "config.toml"
    path.parent.mkdir(parents=True)
    path.write_text(
        '[migration]\npython = "py"\nrust = "rs"\ninfer = true\n',
        encoding="utf-8")
    clear_config_cache()

    cfg = load_project_config(root)
    assert cfg.migration.python == "py"
    assert cfg.migration.rust == "rs"
    assert cfg.migration.infer is True
    assert S.get(root, "migration.python")["value"] == "py"
    assert S.get(root, "migration.rust")["value"] == "rs"
    assert S.get(root, "migration.infer")["value"] is True
    report = diagnose(root)
    assert report["config"]["migration"] == {
        "configured": True,
        "python": "py",
        "rust": "rs",
        "infer": True,
        "pairs": [],
    }


def test_versioned_pairs_are_reported_as_unmapped(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "py").mkdir()
    (root / "rs").mkdir()
    path = root / "ozagia" / "config.toml"
    path.parent.mkdir(parents=True)
    path.write_text(
        '[migration]\npython = "py"\nrust = "rs"\n'
        '[[migration.pairs]]\npython = "pkg.A"\nrust = "crate::A"\n',
        encoding="utf-8")
    clear_config_cache()

    cfg = load_project_config(root)
    assert cfg.migration.pairs == ()
    reasons = dict(cfg.unmapped)
    assert "migration.pairs" in reasons
    assert "array of tables" in reasons["migration.pairs"]
