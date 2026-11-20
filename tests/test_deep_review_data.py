import json
import importlib.util

import pytest

from ozagia.core.deep_review.analyzers.data import analyze


def test_inventory_is_offline_and_reports_python_models_schema_and_json_keys(tmp_path):
    (tmp_path / "models.py").write_text(
        "from dataclasses import dataclass, field\n"
        "from typing import TypedDict\n"
        "@dataclass\nclass Record:\n    key: str\n    items: list[str] = field(default_factory=list)\n"
        "class Payload(TypedDict):\n    value: int\n"
        "WIRE_SCHEMA = {'version': 1}\n", encoding="utf-8")
    (tmp_path / "settings.json").write_text(json.dumps({"service": {"port": 3}}), encoding="utf-8")

    result = analyze(tmp_path)

    assert result["checked"] == ["models.py", "settings.json"]
    assert [item["name"] for item in result["inventory"]["models"]] == ["Record", "Payload"]
    record = result["inventory"]["models"][0]
    assert record["fields"][1]["default_factory"] is True
    assert result["inventory"]["schema_declarations"][0]["name"] == "WIRE_SCHEMA"
    assert "/service/port" in result["inventory"]["documents"][0]["keys"]
    assert result["findings"] == []


def test_invalid_python_json_and_yaml_are_explicit_unknowns(tmp_path):
    (tmp_path / "broken.py").write_text("class Nope(:\n", encoding="utf-8")
    (tmp_path / "broken.json").write_text("{", encoding="utf-8")
    (tmp_path / "values.yaml").write_text("key: value\n", encoding="utf-8")

    result = analyze(tmp_path)

    reasons = {item["path"]: item["reason"] for item in result["not_checked"]}
    assert reasons["broken.json"] == "structured document is invalid"
    assert reasons["broken.py"] == "Python syntax could not be parsed"
    if importlib.util.find_spec("yaml") is None:
        assert reasons["values.yaml"] == "YAML key inventory is unsupported without optional PyYAML"
    else:
        assert "values.yaml" not in reasons


def test_missing_pyyaml_is_reported_as_unknown(tmp_path, monkeypatch):
    import builtins

    (tmp_path / "values.yaml").write_text("key: value\n", encoding="utf-8")
    original_import = builtins.__import__

    def without_yaml(name, *args, **kwargs):
        if name == "yaml":
            raise ImportError("PyYAML intentionally hidden for test")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_yaml)
    result = analyze(tmp_path)

    assert result["checked"] == []
    assert result["not_checked"] == [{
        "path": "values.yaml",
        "reason": "YAML key inventory is unsupported without optional PyYAML",
    }]


def test_yaml_inventory_is_safe_for_recursive_aliases(tmp_path):
    pytest.importorskip("yaml")
    (tmp_path / "values.yaml").write_text(
        "root: &root\n  name: demo\n  self: *root\n", encoding="utf-8")

    result = analyze(tmp_path)

    doc = result["inventory"]["documents"][0]
    assert doc["format"] == "yaml"
    assert "/root/name" in doc["keys"]
    assert "/root/self" in doc["keys"]
    assert "values.yaml" in result["checked"]


def test_yaml_duplicate_keys_are_not_reported_as_an_unambiguous_inventory(tmp_path):
    pytest.importorskip("yaml")
    (tmp_path / "duplicate.yaml").write_text(
        "setting: first\nsetting: second\n", encoding="utf-8")

    result = analyze(tmp_path)

    assert result["checked"] == []
    assert result["not_checked"] == [{
        "path": "duplicate.yaml", "reason": "structured document is invalid",
    }]


def test_pathologically_nested_yaml_does_not_escape_as_recursion_error(tmp_path):
    pytest.importorskip("yaml")
    (tmp_path / "deep.yaml").write_text(
        "".join("  " * level + f"key{level}:\n" for level in range(500))
        + "  " * 500 + "leaf: value\n",
        encoding="utf-8")

    result = analyze(tmp_path)

    reasons = {item.get("path"): item["reason"] for item in result["not_checked"]}
    assert ("deep.yaml" in result["checked"]) != ("deep.yaml" in reasons)
    if "deep.yaml" in result["checked"]:
        assert len(result["inventory"]["documents"][0]["keys"]) <= 400
    else:
        assert reasons["deep.yaml"] == "structured document is invalid"
    assert not result["findings"]


def test_budget_must_be_finite_nonnegative(tmp_path):
    for value in (-1, float("inf"), "bad"):
        with pytest.raises(ValueError, match="finite and non-negative"):
            analyze(tmp_path, budget_s=value)


def test_file_cap_reports_incomplete_coverage(tmp_path, monkeypatch):
    import ozagia.core.deep_review.analyzers.data as data

    monkeypatch.setattr(data, "MAX_FILES", 1)
    (tmp_path / "a.json").write_text("{}", encoding="utf-8")
    (tmp_path / "b.json").write_text("{}", encoding="utf-8")

    result = analyze(tmp_path)

    assert result["checked"] == ["a.json"]
    assert {item["reason"] for item in result["not_checked"]} == {"file limit reached (1)"}
