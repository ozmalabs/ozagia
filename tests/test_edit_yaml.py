import builtins

import pytest

from ozagia.edit.code_edit import CodeEditError
from ozagia.edit.dispatch import adapter_for, apply_edit_script
from ozagia.edit.yaml_edit import apply_yaml_edit_script


def _has_ruamel():
    pytest.importorskip("ruamel.yaml")


def _write(root, source, name="values.yaml"):
    path = root / name
    path.write_text(source, encoding="utf-8", newline="")
    return path


def test_set_delete_nested_paths_preserve_comments_and_hide_values(tmp_path):
    _has_ruamel()
    path = _write(tmp_path, (
        "# top comment\n"
        "service:\n"
        "  # image comment\n"
        "  image: old\n"
        "  ports:\n"
        "    - 8080\n"
        "    - 8443 # retain inline comment\n"
    ))
    result = apply_edit_script(tmp_path, {
        "file": "values.yaml",
        "ops": [{"op": "set", "path": ["service", "image"], "value": "CANARY_VALUE"}],
    })
    assert "CANARY_VALUE" not in repr(result)
    assert not result["applied"]
    assert "image: old" in path.read_text()
    apply_yaml_edit_script(tmp_path, {
        "file": "values.yaml",
        "ops": [{"op": "set", "path": ["service", "image"], "value": "new"}],
    }, write=True)
    edited = path.read_text()
    assert "# top comment" in edited
    assert "# image comment" in edited
    assert "image: new" in edited
    assert "8443" in edited and "# retain inline comment" in edited
    apply_yaml_edit_script(tmp_path, {
        "file": "values.yaml",
        "ops": [{"op": "delete", "path": ["service", "ports", 0]}],
    }, write=True)
    assert "8080" not in path.read_text()
    assert "8443" in path.read_text() and "# retain inline comment" in path.read_text()


def test_missing_path_duplicate_keys_aliases_and_malformed_yaml_refused(tmp_path):
    _has_ruamel()
    _write(tmp_path, "nested:\n  value: ok\n")
    with pytest.raises(CodeEditError, match="does not exist"):
        apply_yaml_edit_script(tmp_path, {
            "file": "values.yaml",
            "ops": [{"op": "delete", "path": ["nested", "missing"]}],
        })
    for source in (
        "value: first\nvalue: second\n",
        "defaults: &defaults {image: safe}\nservice: *defaults\n",
        "broken: [\n",
    ):
        _write(tmp_path, source)
        with pytest.raises(CodeEditError):
            apply_yaml_edit_script(tmp_path, {
                "file": "values.yaml",
                "ops": [{"op": "set", "path": ["value"], "value": "new"}],
            })


def test_base_hash_and_dispatch(tmp_path):
    _has_ruamel()
    _write(tmp_path, "value: old\n")
    assert adapter_for("values.yaml") == "yaml"
    assert adapter_for("values.yml") == "yaml"
    with pytest.raises(CodeEditError, match="stale base"):
        apply_yaml_edit_script(tmp_path, {
            "file": "values.yaml",
            "base_sha256": "0" * 64,
            "ops": [{"op": "set", "path": ["value"], "value": "new"}],
        })


def test_missing_optional_parser_refuses_without_fallback(tmp_path, monkeypatch):
    _write(tmp_path, "value: old\n")
    original_import = builtins.__import__

    def missing_ruamel(name, *args, **kwargs):
        if name == "ruamel.yaml" or name.startswith("ruamel.yaml."):
            raise ImportError("simulated missing optional extra")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing_ruamel)
    with pytest.raises(CodeEditError, match=r"ozagia\[edit-yaml\]"):
        apply_edit_script(tmp_path, {
            "file": "values.yaml",
            "ops": [{"op": "set", "path": ["value"], "value": "new"}],
        })
    assert (tmp_path / "values.yaml").read_text() == "value: old\n"


def test_raw_patch_is_refused_before_parser_load(tmp_path):
    _write(tmp_path, "value: old\n")
    with pytest.raises(CodeEditError, match="raw 'patch'"):
        apply_yaml_edit_script(tmp_path, {
            "file": "values.yaml", "patch": "replace all",
            "ops": [{"op": "set", "path": ["value"], "value": "new"}],
        })
