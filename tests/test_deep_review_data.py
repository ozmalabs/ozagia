import json

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
    assert reasons == {
        "broken.json": "structured document is invalid",
        "broken.py": "Python syntax could not be parsed",
        "values.yaml": "YAML key inventory is unsupported without an optional parser",
    }


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
