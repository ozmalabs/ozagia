"""Tests for read-only Ocelot seed validation.

Every fixture is a temporary repository.  The validator only parses JSON and
reads file metadata: no Ocelot import, loader execution, build, test run,
network access or model call is involved.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ozagia.integrations.ocelot import workspace_validate as V


def _write_json(path: Path, value: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    return path


def _manifest(seed_id: str, **overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": seed_id,
        "version": "1.0.0",
        "depends_on": ["core"],
        "nodes": [],
        "edges": [],
        "productions": [],
        "rules": [],
    }
    data.update(overrides)
    return data


def _valid_manifest(seed_id: str = "valid_seed") -> dict[str, Any]:
    return _manifest(
        seed_id,
        depends_on=["core"],
        self_load_only=False,
        capability_keys=["validation", "ocelot_seed"],
        nodes=[
            {"local_id": 1, "type": "Agent", "attrs": {"name": "root"}},
            {"local_id": 2, "type": "Concept", "attrs": {"name": "target"}},
        ],
        edges=[
            {"type": "knows", "src": {"local": 1}, "tgt": {"local": 2}},
            {"type": "sees", "src": {"role": "agent_root"},
             "tgt": {"local": 2}},
            {"type": "uses_shared", "src": {"local": 1},
             "tgt": {"type": "Concept", "name": "shared_target"}},
        ],
        productions=[
            {"edge_type": "knows", "src": {"type": "Agent"},
             "tgt": {"type": "Concept"}},
            {"edge_type": "sees", "src": {"type": "Agent"},
             "tgt": {"type": "Concept"}},
            {"edge_type": "uses_shared", "src": {"type": "Agent"},
             "tgt": {"type": "Concept"}},
        ],
        rules=[
            {
                "name": "recognise_target",
                "match": [["agent", "knows", "target"]],
                "where": {"type": "True"},
                "effects": [["set_attr", "target", "recognised", 1.0]],
            },
        ],
        reflexes=[
            {
                "name": "blink",
                "stimulus": {"type": "Lit", "value": 1},
                "response": "close_eyes",
                "refractory_ticks": 3,
            },
        ],
    )


def _errors(result: dict[str, Any]) -> str:
    return "\n".join(result["errors"])


def _warnings(result: dict[str, Any]) -> str:
    return "\n".join(result["warnings"])


def test_valid_seed_passes_with_structural_summary(tmp_path: Path) -> None:
    root = tmp_path / "ocelot"
    _write_json(root / "seeds" / "valid_seed.json", _valid_manifest())

    result = V.validate_seed(root, "valid_seed")

    assert result["ok"] is True, result
    assert result["seed"] == "valid_seed"
    assert result["path"] == "seeds/valid_seed.json"
    assert result["errors"] == []
    assert result["warnings"] == []
    assert result["counts"] == {
        "nodes": 2, "edges": 3, "productions": 3, "rules": 1,
        "reflexes": 1,
    }
    by_name = {check["name"]: check for check in result["checks"]}
    assert by_name["filename"]["ok"] is True
    assert by_name["dependencies"]["ok"] is True
    assert by_name["dependency_cycle"]["ok"] is True
    assert by_name["nodes"]["ok"] is True
    assert by_name["edges"]["ok"] is True
    assert by_name["productions"]["ok"] is True
    assert by_name["rules"]["ok"] is True
    assert by_name["reflexes"]["ok"] is True
    assert by_name["production_coverage"]["ok"] is True


def test_id_filename_mismatch_and_duplicate_local_id(tmp_path: Path) -> None:
    root = tmp_path / "ocelot"
    _write_json(root / "seeds" / "expected.json", _manifest(
        "actual",
        nodes=[
            {"local_id": 7, "type": "Agent", "attrs": {}},
            {"local_id": 7, "type": "Concept", "attrs": {}},
        ],
    ))

    result = V.validate_seed(root, "expected")

    assert result["ok"] is False
    errors = _errors(result)
    assert "differs from filename" in errors, result
    assert "local_id 7 duplicates" in errors, result
    assert result["counts"]["nodes"] == 2


def test_missing_dependency_and_dependency_cycle(tmp_path: Path) -> None:
    root = tmp_path / "ocelot"
    _write_json(root / "seeds" / "a.json", _manifest(
        "a", depends_on=["core", "missing_seed", "b"]))
    _write_json(root / "seeds" / "b.json", _manifest("b", depends_on=["a"]))

    result = V.validate_seed(root, "a")

    assert result["ok"] is False
    errors = _errors(result)
    assert "missing seed 'missing_seed'" in errors, result
    assert "dependency cycle: a -> b -> a" in errors, result
    # ``core`` is the built-in boundary and must not be reported missing.
    assert "missing seed 'core'" not in errors
    by_name = {check["name"]: check for check in result["checks"]}
    assert by_name["dependency_cycle"]["ok"] is False


def test_malformed_edge_ref_and_missing_production_warning(
        tmp_path: Path) -> None:
    root = tmp_path / "ocelot"
    _write_json(root / "seeds" / "bad_edges.json", _manifest(
        "bad_edges",
        nodes=[{"local_id": 1, "type": "Agent", "attrs": {}}],
        edges=[
            {"type": "novel_edge", "src": {"local": 99},
             "tgt": {"type": "Concept", "name": "elsewhere"}},
            {"type": "unknown_role_edge", "src": {"role": "some_other_role"},
             "tgt": {"local": 1}},
            {"type": "missing_attr_ref", "src": {"local": 1},
             "tgt": {"type": "Concept"}},
        ],
        productions=[],
        rules=[
            {
                "name": "introduces_edge",
                "match": [["agent", "novel_edge", "thing"]],
                "effects": [["add_edge", "agent", "novel_edge", "thing"]],
            },
        ],
    ))

    result = V.validate_seed(root, "bad_edges")

    assert result["ok"] is False
    errors = _errors(result)
    assert "edges[0].src.local 99 does not resolve" in errors, result
    assert "cross-seed reference without a non-empty string" in errors, result
    warnings = _warnings(result)
    assert "novel_edge" in warnings and "has no production" in warnings
    assert "unknown_role_edge" in warnings and "has no production" in warnings
    assert "not a known Ocelot role" in warnings
    by_name = {check["name"]: check for check in result["checks"]}
    assert by_name["production_coverage"]["ok"] is False


def test_legacy_declarative_rule_is_accepted_with_warning(
        tmp_path: Path) -> None:
    root = tmp_path / "ocelot"
    _write_json(root / "seeds" / "legacy.json", _manifest(
        "legacy",
        rules=[
            {
                "name": "declarative_if_then",
                "if": {"type": "Lit", "value": True},
                "then": [["set_attr", "agent", "legacy", 1]],
                "why": "kept for boot_all's declarative materialisation",
            },
        ],
    ))

    result = V.validate_seed(root, "legacy")

    assert result["ok"] is True, result
    assert result["errors"] == []
    warnings = _warnings(result)
    assert "legacy declarative if/then rule" in warnings, result
    assert "accepted without match/effects" in warnings
    by_name = {check["name"]: check for check in result["checks"]}
    assert by_name["rules"]["ok"] is True


def test_oversized_manifest_yields_partial_result_with_warning(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "ocelot"
    monkeypatch.setattr(V, "MAX_VALIDATE_BYTES", 256)
    monkeypatch.setattr(V, "MAX_VALIDATE_HEAD_BYTES", 512)
    manifest = _manifest(
        "huge_seed",
        depends_on=["core"],
        padding="x" * 4096,
    )
    _write_json(root / "seeds" / "huge_seed.json", manifest)

    result = V.validate_seed(root, "huge_seed")

    assert result["ok"] is True, result
    assert result["partial"] is True
    assert result["counts"] == {
        "nodes": 0, "edges": 0, "productions": 0, "rules": 0,
        "reflexes": 0,
    }
    assert any("manifest exceeds full validation size" in warning
               for warning in result["warnings"]), result
    by_name = {check["name"]: check for check in result["checks"]}
    assert by_name["size"]["ok"] is False
    assert by_name["nodes"]["ok"] is True


def test_seed_validate_and_registration_surface(tmp_path: Path) -> None:
    root = tmp_path / "ocelot"
    _write_json(root / "seeds" / "valid_seed.json", _valid_manifest())

    class Memory:
        raw_repo_root = str(root)

        def node_by_key(self, key: str) -> None:
            return None

        def nodes_of_type(self, node_type: str) -> list[int]:
            return []

    class Registry:
        def __init__(self) -> None:
            self.queries: dict[str, Any] = {}
            self.tools: dict[str, Any] = {}

        def add_graph_query(self, name: str, query: Any) -> None:
            self.queries[name] = query

        def add_mcp_tool(self, spec: dict[str, Any], handler: Any) -> None:
            self.tools[spec["name"]] = (spec, handler)

    memory = Memory()
    result = V.seed_validate(memory, "valid_seed")
    assert result["ok"] is True, result
    assert result["graph"]["indexed"] is False
    assert any("not indexed in the graph" in warning
               for warning in result["warnings"]), result

    registry = Registry()
    V.register_validate(registry)
    assert "ocelot_seed_validate" in registry.queries
    spec, handler = registry.tools["ocelot_seed_validate"]
    assert spec["inputSchema"] == {
        "type": "object",
        "properties": {"id": {"type": "string"}},
        "required": ["id"],
        "additionalProperties": False,
    }
    assert registry.queries["ocelot_seed_validate"](memory, id="valid_seed")["ok"]
    assert handler(memory, {"id": "valid_seed"})["ok"] is True
