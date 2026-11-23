"""Migration inventories: Python public API and Rust pub items."""
from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import pytest

from ozagia.config import MigrationConfig, clear_config_cache, load_project_config
from ozagia.migration import inventory as I
from ozagia.migration.hints import HINTS, annotation_hints, hints_for

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "py_rust_migration"


def _copy(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    shutil.copytree(FIXTURE, root)
    clear_config_cache()
    return root


def _config(root: Path) -> MigrationConfig:
    return load_project_config(root).migration


def test_python_inventory_public_names_all_and_shapes(tmp_path: Path) -> None:
    root = _copy(tmp_path)
    inventory = I.inventory_python(root, _config(root))
    by_key = inventory.by_key()

    assert inventory.language == "python"
    assert inventory.files == 3
    assert inventory.truncated is False
    assert inventory.omitted == ()
    assert inventory.fingerprint
    assert inventory.api
    assert "fixturepy.models.User" in by_key
    assert "fixturepy.models._private_helper" not in by_key
    assert "fixturepy.models.Widget" in by_key
    user = by_key["fixturepy.models.User"]
    assert user.kind == "dataclass"
    assert user.exported is True
    assert user.api is True
    assert user.decorators == ("dataclass",)
    assert [field.name for field in user.fields] == ["id", "name"]
    assert user.doc == "A user record."
    assert user.args == ("id", "name")
    assert by_key["fixturepy.models.ProtocolThing"].kind == "protocol"
    assert by_key["fixturepy.models.Colour"].kind == "enum"
    fetch = by_key["fixturepy.service.fetch_user"]
    assert fetch.async_ is True
    assert fetch.args == ("user_id",)
    assert fetch.returns == "str | None"
    assert fetch.doc == "Fetch a user's display name."
    assert fetch.arg_annotations == (("user_id", "int"),)
    assert by_key["fixturepy.service.cached_normalize"].decorators == (
        "functools.lru_cache(maxsize=8)",)
    assert inventory.api == tuple(
        item for item in inventory.symbols if item.api)


def test_python_all_restricts_api_and_reports_undefined_names(
        tmp_path: Path) -> None:
    root = tmp_path / "repo"
    package = root / "py" / "pkg"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text('"""pkg."""\n')
    (package / "mod.py").write_text(
        '__all__ = ["visible", "missing"]\n\n'
        'def visible() -> int:\n    return 1\n\n'
        'def internal() -> int:\n    return 2\n\n'
        'def _private() -> int:\n    return 3\n')
    (root / "rs").mkdir()
    clear_config_cache()
    inventory = I.inventory_python(
        root, MigrationConfig("py", "rs", infer=False))
    keys = {item.key for item in inventory.api}

    assert keys == {"pkg.mod.visible"}
    assert any("__all__ names not defined: missing" in item["reason"]
               for item in inventory.omitted)


def test_python_inventory_omits_unparseable_files(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    (root / "py").mkdir(parents=True)
    (root / "py" / "bad.py").write_text("def broken(:\n")
    (root / "rs").mkdir()
    clear_config_cache()
    inventory = I.inventory_python(
        root, MigrationConfig("py", "rs", infer=False))

    assert inventory.files == 1
    assert inventory.symbols == ()
    assert [(item["path"], item["reason"]) for item in inventory.omitted] == [
        ("py/bad.py", "could not be parsed as Python")]


def test_python_inventory_is_bounded(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "repo"
    (root / "py").mkdir(parents=True)
    for index in range(3):
        (root / "py" / f"m{index}.py").write_text(
            f"def f{index}() -> int:\n    return {index}\n")
    (root / "rs").mkdir()
    monkeypatch.setattr(I, "MAX_FILES", 1)
    clear_config_cache()
    inventory = I.inventory_python(
        root, MigrationConfig("py", "rs", infer=False))

    assert inventory.files == 1
    assert inventory.truncated is True
    assert any("file limit" in item["reason"] for item in inventory.omitted)


def test_rust_inventory_pub_paths_fields_and_ownership(tmp_path: Path) -> None:
    root = _copy(tmp_path)
    inventory = I.inventory_rust(root, _config(root))
    by_key = inventory.by_key()

    assert inventory.language == "rust"
    assert inventory.files == 3
    assert inventory.backends == (("tree-sitter", 3),)
    assert "fixturecrate::models::User" in by_key
    assert "fixturecrate::private_helper" not in by_key
    user = by_key["fixturecrate::models::User"]
    assert user.kind == "struct"
    assert user.visibility == "pub"
    assert [field.name for field in user.fields] == ["id", "name"]
    assert user.doc == "A user record."
    assert user.api is True
    method = by_key["fixturecrate::models::User::display_name"]
    assert method.kind == "method"
    assert method.api is False
    assert method.owner == "User"
    assert method.owner_kind == "impl"
    assert method.args == ()
    trait_method = by_key["fixturecrate::models::Runner::run"]
    assert trait_method.owner_kind == "trait"
    assert trait_method.public is True
    fetch = by_key["fixturecrate::service::fetch_user"]
    assert fetch.async_ is True
    assert fetch.returns == "Option<String>"
    assert fetch.args == ("id",)
    assert inventory.api == tuple(item for item in inventory.symbols
                                  if item.api)


def test_rust_inventory_finds_manifest_above_the_configured_tree(tmp_path):
    root = _copy(tmp_path)
    inventory = I.inventory_rust(
        root, MigrationConfig("python", "rust/src", infer=False))
    by_key = inventory.by_key()
    assert "fixturecrate::models::User" in by_key
    assert by_key["fixturecrate::models::User"].crate == "fixturecrate"


def test_inventory_tree_cannot_escape_the_repository(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(ValueError, match="outside the repository"):
        I.inventory_python(root, MigrationConfig("python", "rust"),
                           tree="../outside")
    with pytest.raises(ValueError, match="outside the repository"):
        I.inventory_rust(root, MigrationConfig("python", "rust"),
                         tree="../outside")


def test_hints_flag_mocking_imports(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "py").mkdir()
    (root / "rs").mkdir()
    (root / "py" / "m.py").write_text(
        "from unittest import mock\n\n\ndef f(x: int) -> int:\n    return x\n",
        encoding="utf-8")
    clear_config_cache()
    inventory = I.inventory_python(
        root, MigrationConfig("py", "rs", infer=False))
    hints = {item.id: item for item in
             hints_for(inventory.by_key()["m.f"])}
    assert hints["monkeypatching"].support == "unsupported"


def test_rust_inventory_omits_unreadable_and_invalid_utf8(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    (root / "rs" / "src").mkdir(parents=True)
    (root / "rs" / "Cargo.toml").write_text(
        '[package]\nname = "demo"\nversion = "0.1.0"\n', encoding="utf-8")
    (root / "rs" / "src" / "lib.rs").write_text("pub fn ok() -> u8 { 1 }\n")
    (root / "rs" / "src" / "bad.rs").write_bytes(b"\xff\xfe\x00")
    (root / "py").mkdir()
    clear_config_cache()
    inventory = I.inventory_rust(root, MigrationConfig("py", "rs", infer=False))

    assert inventory.files == 1
    assert any(item["path"].endswith("bad.rs")
               and item["reason"] == "not valid UTF-8"
               for item in inventory.omitted)
    assert "demo::ok" in inventory.by_key()


def test_inventory_fingerprint_tracks_file_content(tmp_path: Path) -> None:
    root = _copy(tmp_path)
    config = _config(root)
    first = I.inventory_rust(root, config)
    second = I.inventory_rust(root, config)
    digest = hashlib.sha256
    assert first.fingerprint == second.fingerprint
    assert len(first.fingerprint) == len(digest(b"").hexdigest())
    path = root / "rust" / "src" / "service.rs"
    path.write_text(path.read_text(encoding="utf-8") + "\n// changed\n",
                    encoding="utf-8")
    third = I.inventory_rust(root, config)
    assert third.fingerprint != first.fingerprint


def test_inventory_fingerprint_tracks_omitted_files(tmp_path):
    root = tmp_path / "repo"
    (root / "py").mkdir(parents=True)
    (root / "rs").mkdir()
    bad = root / "py" / "bad.py"
    bad.write_bytes(b"\xff")
    clear_config_cache()
    config = MigrationConfig("py", "rs", infer=False)
    first = I.inventory_python(root, config)
    bad.write_bytes(b"\xfe\xff")
    second = I.inventory_python(root, config)
    assert first.fingerprint != second.fingerprint
    assert any(item["path"] == "py/bad.py" for item in first.omitted)


def test_hints_classify_supported_manual_and_unsupported_shapes(
        tmp_path: Path) -> None:
    root = _copy(tmp_path)
    inventory = I.inventory_python(root, _config(root))
    by_key = inventory.by_key()

    user = {item.id: item for item in hints_for(by_key["fixturepy.models.User"])}
    assert user["dataclass"].support == "supported"
    assert user["int"].support == "supported"
    assert user["str"].choice is True
    assert user["str"].rust == "String or &str"

    protocol = {item.id: item for item in
                hints_for(by_key["fixturepy.models.ProtocolThing"])}
    assert protocol["protocol"].support == "manual"

    fetch = {item.id: item for item in
             hints_for(by_key["fixturepy.service.fetch_user"])}
    assert fetch["async"].support == "supported"
    assert fetch["optional"].support == "manual"

    union = {item.id for item in annotation_hints("int | str")}
    assert "union" in union
    assert annotation_hints("Callable[[int], str]")[0].support == "manual"
    assert annotation_hints("Any")[0].support == "manual"
    assert annotation_hints("SomeProjectType")[0].support == "unsupported"

    ids = {item.id for item in HINTS}
    assert {"metaprogramming", "monkeypatching", "c-extension",
            "kwargs", "varargs", "callable", "any"} <= ids
    unsupported = {item.id for item in HINTS
                   if item.support == "unsupported"}
    assert {"metaprogramming", "monkeypatching", "c-extension",
            "kwargs"} <= unsupported
    assert all(item.to_dict()["compilable"] is False for item in HINTS)
