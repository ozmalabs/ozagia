"""Tests for preview-first semantic Ocelot seed editing.

Every fixture is a temporary repository.  The editor uses the JSON edit
adapter and the edit transaction: no Ocelot import, seed loader, model call or
network access is involved.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ozagia.integrations.ocelot import workspace_seed_edit as E
from ozagia.integrations.ocelot.workspace_validate import validate_seed_data


def _write_json(path: Path, value: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    return path


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _rule(name: str = "rule_one") -> dict[str, Any]:
    return {
        "name": name,
        "match": [["agent", "knows", "target"]],
        "effects": [["set_attr", "target", "seen", 1.0]],
    }


def _manifest(seed_id: str, **overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": seed_id,
        "version": "1.0.0",
        "description": f"Synthetic {seed_id} seed.",
        "depends_on": ["core"],
        "self_load_only": False,
        "capability_keys": ["alpha"],
        "nodes": [
            {"local_id": 1, "type": "Agent", "attrs": {"name": "root"}},
            {"local_id": 2, "type": "Concept", "attrs": {"name": "target"}},
        ],
        "edges": [
            {"type": "knows", "src": {"local": 1}, "tgt": {"local": 2}},
        ],
        "productions": [
            {"edge_type": "knows", "src": {"type": "Agent"},
             "tgt": {"type": "Concept"}},
        ],
        "rules": [_rule()],
        "concepts": [{"name": "ConceptOne"}, {"name": "ConceptTwo"}],
        "reflexes": [
            {"name": "blink", "stimulus": {"type": "Lit", "value": 1},
             "response": "close_eyes", "refractory_ticks": 3},
        ],
    }
    data.update(overrides)
    return data


def _workspace(tmp_path: Path, *, seed_id: str = "seed_a") -> tuple[Path, Path]:
    root = tmp_path / "ocelot"
    path = _write_json(root / "seeds" / f"{seed_id}.json",
                       _manifest(seed_id))
    _write_json(root / "seeds" / "helper.json", _manifest(
        "helper", nodes=[], edges=[], productions=[], rules=[], concepts=[],
        reflexes=[]))
    return root, path


def _errors(result: dict[str, Any]) -> str:
    return "\n".join(result.get("errors") or [])


def test_preview_append_rule_leaves_bytes_and_shows_diff(
        tmp_path: Path) -> None:
    root, path = _workspace(tmp_path)
    before = path.read_bytes()
    rule = _rule("rule_two")

    result = E.seed_edit(
        root, "seed_a", [{"op": "append", "kind": "rule", "value": rule}])

    assert result["ok"] is True, result
    assert result["seed"] == "seed_a"
    assert result["path"] == "seeds/seed_a.json"
    assert result["applied"] is False
    assert result["backup"] is None
    assert result["base_sha256"]
    assert result["new_sha256"]
    assert result["base_sha256"] != result["new_sha256"]
    assert "rule_two" in result["diff_view"]
    assert any(op["op"] == "append" and op["path"] == ["rules"]
               for op in result["ops"])
    assert result["validation_before"]["ok"] is True, result
    assert result["validation_after"]["ok"] is True, result
    assert any("preview only" in warning
               for warning in result["warnings"])
    assert path.read_bytes() == before
    skeleton = result["test_skeleton"]
    assert skeleton["ok"] is True
    assert skeleton["path"] == "tests/test_seed_a_migrated.py"
    assert skeleton["written"] is False
    assert skeleton["exists"] is False
    assert "seed_a" in skeleton["content"]
    assert "def test_manifest_id" in skeleton["content"]
    assert not (root / "tests" / "test_seed_a_migrated.py").exists()


def test_test_skeleton_previews_an_existing_target_without_overwriting(
        tmp_path: Path) -> None:
    root, _path = _workspace(tmp_path)
    target = root / "tests" / "test_seed_a_migrated.py"
    target.parent.mkdir(parents=True)
    target.write_text("original\n", encoding="utf-8")

    result = E.test_skeleton(root, "seed_a")

    assert result["ok"] is True
    assert result["exists"] is True
    assert result["written"] is False
    assert any("already exists" in warning for warning in result["warnings"])
    assert target.read_text(encoding="utf-8") == "original\n"
    missing = E.test_skeleton(root, "missing_seed")
    assert missing["ok"] is False
    assert "not found" in _errors(missing)


def test_apply_add_node_allocates_local_id_and_backs_up(
        tmp_path: Path) -> None:
    root, path = _workspace(tmp_path)
    before = path.read_bytes()

    result = E.seed_edit(
        root, "seed_a",
        [{"op": "add_node",
          "value": {"type": "Concept", "attrs": {"name": "new_node"}}}],
        write=True)

    assert result["ok"] is True, result
    assert result["applied"] is True
    assert result.get("rolled_back") is not True
    assert result["backup"] is not None
    backup = Path(result["backup"])
    assert backup.is_file()
    assert backup.read_bytes() == before

    data = _read(path)
    added = data["nodes"][-1]
    assert added["local_id"] == 3
    assert added["attrs"]["name"] == "new_node"
    assert path.read_bytes() != before
    assert result["validation_after"]["ok"] is True, result


def test_add_edge_allocates_local_id_and_appends(tmp_path: Path) -> None:
    root, path = _workspace(tmp_path)

    result = E.seed_edit(
        root, "seed_a",
        [{"op": "add_edge",
          "value": {"type": "knows", "src": {"local": 1},
                    "tgt": {"local": 2}}}],
        write=True)

    assert result["ok"] is True, result
    data = _read(path)
    assert data["edges"][-1]["local_id"] == 3
    assert data["edges"][-1]["type"] == "knows"


def test_replace_and_delete_by_name_or_index_and_ambiguous_name(
        tmp_path: Path) -> None:
    root, path = _workspace(tmp_path)

    replaced = E.seed_edit(
        root, "seed_a",
        [{"op": "replace", "kind": "rule", "name": "rule_one",
          "value": _rule("rule_one_v2")}],
        write=True)
    assert replaced["ok"] is True, replaced
    assert _read(path)["rules"][0]["name"] == "rule_one_v2"

    deleted = E.seed_edit(
        root, "seed_a", [{"op": "delete", "kind": "concept", "index": 0}],
        write=True)
    assert deleted["ok"] is True, deleted
    assert [item["name"] for item in _read(path)["concepts"]] == [
        "ConceptTwo"]

    replaced_node = E.seed_edit(
        root, "seed_a",
        [{"op": "replace", "kind": "node", "name": "root",
          "value": {"local_id": 1, "type": "Agent",
                    "attrs": {"name": "root_renamed"}}}],
        write=True)
    assert replaced_node["ok"] is True, replaced_node
    assert _read(path)["nodes"][0]["attrs"]["name"] == "root_renamed"

    duplicate = _read(path)
    duplicate["rules"] = [_rule("duplicate"), _rule("duplicate")]
    _write_json(path, duplicate)
    before = path.read_bytes()
    ambiguous = E.seed_edit(
        root, "seed_a",
        [{"op": "replace", "kind": "rule", "name": "duplicate",
          "value": _rule("other")}])
    assert ambiguous["ok"] is False
    assert "ambiguous" in _errors(ambiguous)
    assert path.read_bytes() == before

    both = E.seed_edit(
        root, "seed_a",
        [{"op": "delete", "kind": "rule", "name": "duplicate", "index": 0}])
    assert both["ok"] is False
    assert "ambiguous" in _errors(both)
    assert path.read_bytes() == before


def test_set_metadata_updates_version_capabilities_and_dependencies(
        tmp_path: Path) -> None:
    root, path = _workspace(tmp_path)
    before = _read(path)

    result = E.seed_edit(
        root, "seed_a",
        [{"op": "set_metadata",
          "fields": {
              "version": "2.0.0",
              "capability_keys": ["alpha", "beta"],
              "depends_on": ["core", "helper"],
              "area": "perception",
              "order": 7,
              "latency_class": "fast",
              "description": "Updated description.",
          }}],
        write=True)

    assert result["ok"] is True, result
    data = _read(path)
    assert data["version"] == "2.0.0"
    assert data["capability_keys"] == ["alpha", "beta"]
    assert data["depends_on"] == ["core", "helper"]
    assert data["area"] == "perception"
    assert data["order"] == 7
    assert data["latency_class"] == "fast"
    assert data["description"] == "Updated description."
    # Existing key order is preserved; new fields are appended.
    assert list(data)[:3] == list(before)[:3]
    assert data["nodes"] == before["nodes"]


def test_generated_acquired_and_escaping_paths_are_refused(
        tmp_path: Path) -> None:
    root, path = _workspace(tmp_path)
    generated = _write_json(
        root / "seeds" / "generated" / "generated_seed.json",
        _manifest("generated_seed"))
    acquired = _write_json(
        root / "seeds" / "acquired" / "acquired_seed.json",
        _manifest("acquired_seed"))
    outside = _write_json(tmp_path / "outside.json", _manifest("outside"))
    link = root / "seeds" / "link.json"
    try:
        link.symlink_to(outside)
    except OSError as exc:  # pragma: no cover - unusual filesystem
        pytest.skip(f"symlinks unavailable: {exc}")

    before = {candidate: candidate.read_bytes()
              for candidate in (path, generated, acquired, outside)}
    operations = [{"op": "append", "kind": "rule", "value": _rule("new")}]

    for seed_id in ("generated/generated_seed",
                    "acquired/acquired_seed",
                    "generated_seed",
                    "acquired_seed",
                    "../outside",
                    "link"):
        result = E.seed_edit(root, seed_id, operations, write=True)
        assert result["ok"] is False, (seed_id, result)
        assert result["applied"] is False
        assert result["errors"], result
        assert result["backup"] is None
        if seed_id in ("generated_seed", "acquired_seed"):
            assert "generated cache" in _errors(result), result

    for candidate, original in before.items():
        assert candidate.read_bytes() == original


def test_apply_that_would_be_invalid_is_rolled_back(tmp_path: Path) -> None:
    root, path = _workspace(tmp_path)
    before = path.read_bytes()

    result = E.seed_edit(
        root, "seed_a",
        [{"op": "add_node",
          "value": {"local_id": 1, "type": "Concept",
                    "attrs": {"name": "duplicate_id"}}}],
        write=True)

    assert result["ok"] is False
    assert result["applied"] is False
    assert result["rolled_back"] is True
    assert "duplicates" in _errors(result)
    assert result["validation_after"]["ok"] is False
    assert path.read_bytes() == before
    assert result["restored_sha256"] == result["base_sha256"]
    assert Path(result["backup"]).read_bytes() == before


def test_missing_dependency_apply_is_rolled_back(tmp_path: Path) -> None:
    root, path = _workspace(tmp_path)
    before = path.read_bytes()

    result = E.seed_edit(
        root, "seed_a",
        [{"op": "set_metadata", "fields": {"depends_on": ["missing_seed"]}}],
        write=True)

    assert result["ok"] is False
    assert result["rolled_back"] is True
    assert "missing seed 'missing_seed'" in _errors(result)
    assert path.read_bytes() == before
    assert result["restored_sha256"] == result["base_sha256"]


def test_prospective_dependency_cycle_is_refused_without_write(
        tmp_path: Path) -> None:
    root, path = _workspace(tmp_path)
    helper = root / "seeds" / "helper.json"
    helper_data = _read(helper)
    helper_data["depends_on"] = ["seed_a"]
    _write_json(helper, helper_data)
    before = path.read_bytes()
    operations = [
        {"op": "set_metadata", "fields": {"depends_on": ["helper"]}},
    ]

    prospective = validate_seed_data(
        root, "seed_a", {**_read(path), "depends_on": ["helper"]})
    assert prospective["ok"] is False
    assert prospective["prospective"] is True
    assert prospective["file_written"] is False
    assert "dependency cycle" in _errors(prospective)
    assert "seed_a" in _errors(prospective)
    assert "helper" in _errors(prospective)
    assert path.read_bytes() == before

    preview = E.seed_edit(root, "seed_a", operations)
    assert preview["ok"] is False
    assert preview["applied"] is False
    assert preview["validation_after"]["ok"] is False
    assert preview["validation_after"]["prospective"] is True
    assert "dependency cycle" in _errors(preview["validation_after"])
    assert path.read_bytes() == before

    applied = E.seed_edit(root, "seed_a", operations, write=True)
    assert applied["ok"] is False
    assert applied["applied"] is False
    assert applied["rolled_back"] is True
    assert "dependency cycle" in _errors(applied)
    assert path.read_bytes() == before
    assert applied["restored_sha256"] == applied["base_sha256"]
    assert Path(applied["backup"]).read_bytes() == before


def test_multiple_appends_to_empty_or_emptied_arrays(tmp_path: Path) -> None:
    root, path = _workspace(tmp_path)
    manifest = _read(path)
    manifest["rules"] = []
    _write_json(path, manifest)

    appended = E.seed_edit(
        root, "seed_a",
        [{"op": "append", "kind": "rule", "value": _rule("rule_a")},
         {"op": "replace", "kind": "rule", "name": "rule_a",
          "value": _rule("rule_b")},
         {"op": "append", "kind": "rule", "value": _rule("rule_c")}],
        write=True)

    assert appended["ok"] is True, appended
    assert [rule["name"] for rule in _read(path)["rules"]] == [
        "rule_b", "rule_c"]

    manifest = _read(path)
    manifest["rules"] = [_rule("only")]
    _write_json(path, manifest)
    replaced = E.seed_edit(
        root, "seed_a",
        [{"op": "delete", "kind": "rule", "index": 0},
         {"op": "append", "kind": "rule", "value": _rule("after_delete")}],
        write=True)

    assert replaced["ok"] is True, replaced
    assert [rule["name"] for rule in _read(path)["rules"]] == [
        "after_delete"]


def test_unknown_ops_and_kinds_are_refused(tmp_path: Path) -> None:
    root, path = _workspace(tmp_path)
    before = path.read_bytes()

    for operations in (
        [{"op": "rename", "kind": "rule", "name": "rule_one"}],
        [{"op": "append", "kind": "frame", "value": {}}],
        [{"op": "delete", "kind": "edge", "name": "knows"}],
        [{"op": "replace", "kind": "rule", "name": "rule_one"}],
    ):
        result = E.seed_edit(root, "seed_a", operations, write=True)
        assert result["ok"] is False, (operations, result)
        assert result["applied"] is False
        assert path.read_bytes() == before


def test_mcp_registration_and_tool_preview_do_not_write(
        tmp_path: Path) -> None:
    root, path = _workspace(tmp_path)
    before = path.read_bytes()

    class Memory:
        raw_repo_root = str(root)

    class Registry:
        def __init__(self) -> None:
            self.tools: dict[str, Any] = {}

        def add_mcp_tool(self, spec: dict[str, Any],
                         handler: Any) -> None:
            self.tools[spec["name"]] = (spec, handler)

    registry = Registry()
    E.register_seed_edit(registry)
    # Registration is idempotent.
    E.register_seed_edit(registry)
    assert list(registry.tools) == ["ocelot_seed_edit"]
    spec, handler = registry.tools["ocelot_seed_edit"]
    assert spec["name"] == "ocelot_seed_edit"
    assert spec["inputSchema"]["properties"]["write"]["default"] is False
    assert set(spec["inputSchema"]["required"]) == {"id", "operations"}

    memory = Memory()
    operations = [
        {"op": "append", "kind": "rule", "value": _rule("from_mcp")},
    ]
    result = handler(memory, {"id": "seed_a", "operations": operations})
    assert result["ok"] is True, result
    assert result["applied"] is False
    assert "from_mcp" in result["diff_view"]
    assert path.read_bytes() == before

    preview = E.seed_edit_tool(memory, "seed_a", operations)
    assert preview["applied"] is False
    assert path.read_bytes() == before

    bad_write = handler(
        memory, {"id": "seed_a", "operations": operations, "write": "yes"})
    assert bad_write["ok"] is False
    assert "write must be a boolean" in _errors(bad_write)
    assert path.read_bytes() == before
