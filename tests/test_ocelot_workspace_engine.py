"""Integration tests for the syntactic Ocelot Rust/Python/seed bridge.

The fixture is a small Ocelot-shaped checkout: a seed manifest with a
``ShapeDecode`` rule and a ``Concept`` node, ``TermNode`` variants plus
``jsonio`` wire names, PyO3 ``#[pyfunction]``/``#[pyclass]``/``#[pymethods]``
definitions, Python uses in a source file and a test, and a deliberately
unknown seed term.  The target tree is written to disk and never imported or
executed.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ozagia.core.memory import DevMemory, build_index
from ozagia.integrations.mcp.server import DevMemoryServer
from ozagia.integrations.ocelot import workspace_engine


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _write_json(path: Path, value: Any) -> None:
    _write(path, json.dumps(value, indent=2, sort_keys=True) + "\n")


def _write_repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    _write(root / ".ozagia.toml", """\
[index]
source_dirs = ["runners", "substrate"]
test_dirs = ["tests"]
path_prefixes = ["runners", "substrate", "seeds", "tests"]

[adapters]
modules = ["ozagia.integrations.ocelot.workspace:register"]
""")

    _write_json(root / "seeds" / "demo.json", {
        "id": "demo",
        "version": "1.0.0",
        "depends_on": [],
        "description": "Synthetic seed for the engine bridge.",
        "nodes": [
            {"type": "Concept", "attrs": {"name": "DemoConcept"}},
        ],
        "rules": [
            {
                "name": "decode",
                "match": [
                    {"type": "ShapeDecode", "input": "demo"},
                    {"type": "AliasTerm"},
                ],
                "where": [
                    {"type": "MysteryTerm"},
                    {"type": "Count", "value": 1},
                ],
                "effects": [],
            },
        ],
        "reflexes": [
            {"name": "decode_reflex", "when": {"type": "Count"}},
        ],
        "productions": [
            {"type": "Count", "edge": "decoded_by"},
        ],
    })

    _write(root / "runners" / "dsl" / "src" / "term.rs", """\
//! Term shapes used by the synthetic engine.

pub enum TermNode {
    /// A shape decode request.
    ShapeDecode { input: String },
    #[allow(dead_code)]
    Count { value: u64 },
    Latent { value: u64 },
    AliasTerm { value: u64 },
}
""")
    _write(root / "runners" / "dsl" / "src" / "jsonio.rs", """\
//! Wire names for the TermNode variants.

use crate::term::TermNode;

pub fn wire_name(term: &TermNode) -> &'static str {
    match term {
        TermNode::ShapeDecode { .. } => "ShapeDecode",
        TermNode::Count { .. } => "Count",
        TermNode::Latent { .. } => "Latent",
        TermNode::AliasTerm { .. } => "alias_wire",
    }
}
""")
    _write(root / "runners" / "dsl" / "src" / "py.rs", """\
//! PyO3 hand-off for the synthetic engine.

use pyo3::prelude::*;

#[pyfunction]
pub fn substrate_version() -> &'static str {
    "fixture"
}

#[pyclass(name = "Substrate")]
pub struct Substrate {
    value: u64,
}

#[pymethods]
impl Substrate {
    #[new]
    fn new() -> Self {
        Self { value: 1 }
    }

    fn tick(&mut self) -> u64 {
        self.value += 1;
        self.value
    }
}
""")

    _write(root / "substrate" / "__init__.py", '"""Synthetic substrate."""\n')
    _write(root / "substrate" / "use.py", """\
\"\"\"A source file that uses the synthetic native extension.\"\"\"
import substrate_rs
from substrate_rs import Substrate


def make_substrate() -> Substrate:
    return Substrate()


def engine_version() -> str:
    return substrate_rs.substrate_version()
""")
    _write(root / "tests" / "test_use.py", """\
\"\"\"A migrated test that imports and calls the synthetic engine.\"\"\"
import substrate_rs
from substrate_rs import Substrate


def test_substrate_tick():
    assert Substrate().tick() == 2


def test_substrate_version():
    assert substrate_rs.substrate_version() == "fixture"
""")
    return root


@pytest.fixture
def engine_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch
                ) -> tuple[Path, DevMemory]:
    monkeypatch.delenv("OZAGIA_CONFIG", raising=False)
    monkeypatch.delenv("OZAGIA_ADAPTERS", raising=False)
    root = _write_repo(tmp_path / "ocelot-engine")
    out = root / ".ozagia" / "graph.json"
    build_index(root, out, include_docs="none", include_git=False)
    mem = DevMemory(out, root / ".ozagia" / "journal.jsonl", root=root)
    return root, mem


def _node(mem: DevMemory, key: str) -> tuple[int, dict[str, Any]]:
    nid = mem.node_by_key(key)
    assert nid is not None, f"missing node {key}; keys={sorted(mem._key_to_id)[:20]}"
    return nid, dict((mem.node(nid) or {}).get("attrs") or {})


def _has_edge(mem: DevMemory, src: int, edge: str, tgt: int) -> bool:
    return tgt in mem._neighbours(src, edge)


# --------------------------------------------------------------------------- #
# 1. Rust definitions and Term variants
# --------------------------------------------------------------------------- #
def test_indexes_rust_primitives_and_terms(
        engine_repo: tuple[Path, DevMemory]) -> None:
    _root, mem = engine_repo

    version_id, version = _node(
        mem, "ocelot_primitive:substrate_version")
    assert version["kind"] == "pyfunction"
    assert version["rust_path"] == "runners/dsl/src/py.rs"
    assert version["defined"] is True
    assert version["line"] > 0

    class_id, substrate = _node(mem, "ocelot_primitive:Substrate")
    assert substrate["kind"] == "pyclass"
    assert substrate["defined"] is True
    assert substrate["rust_path"] == "runners/dsl/src/py.rs"

    tick_id, tick = _node(mem, "ocelot_primitive:Substrate.tick")
    assert tick["kind"] == "pymethod"
    assert tick["defined"] is True
    assert tick["line"] > substrate["line"]

    py_file = mem.node_by_key("file:runners/dsl/src/py.rs")
    assert py_file is not None
    assert _has_edge(mem, py_file, "ocelot_defines_primitive", version_id)
    assert _has_edge(mem, py_file, "ocelot_defines_primitive", class_id)
    assert _has_edge(mem, py_file, "ocelot_defines_primitive", tick_id)

    shape_id, shape = _node(mem, "ocelot_term:ShapeDecode")
    assert shape["defined"] is True
    assert shape["rust_path"] == "runners/dsl/src/term.rs"
    assert shape["line"] > 0
    _node(mem, "ocelot_term:Count")
    _node(mem, "ocelot_term:Latent")

    # jsonio wire names win, and the Rust variant stays an alias.
    alias_id, alias = _node(mem, "ocelot_term:alias_wire")
    assert alias["defined"] is True
    assert alias["name"] == "alias_wire"
    assert "AliasTerm" in (alias.get("aliases") or [])
    assert mem.node_by_key("ocelot_term:AliasTerm") is None

    term_file = mem.node_by_key("file:runners/dsl/src/term.rs")
    assert term_file is not None
    assert _has_edge(mem, term_file, "ocelot_defines_term", shape_id)
    assert _has_edge(mem, term_file, "ocelot_defines_term", alias_id)

    # A node type in the seed manifest is not a Term.
    assert mem.node_by_key("ocelot_term:Concept") is None
    assert not any(
        str((node.get("attrs") or {}).get("name")) == "Concept"
        for node in mem.nodes.values() if node.get("type") == "OcelotTerm")


# --------------------------------------------------------------------------- #
# 2. Python uses and seed Term edges
# --------------------------------------------------------------------------- #
def test_links_python_uses_and_seed_terms(
        engine_repo: tuple[Path, DevMemory]) -> None:
    _root, mem = engine_repo
    version_id, _version = _node(mem, "ocelot_primitive:substrate_version")
    class_id, _substrate = _node(mem, "ocelot_primitive:Substrate")

    use_file = mem.node_by_key("file:substrate/use.py")
    test_file = mem.node_by_key("file:tests/test_use.py")
    assert use_file is not None and test_file is not None
    for pid in (version_id, class_id):
        assert _has_edge(mem, use_file, "ocelot_uses_primitive", pid)
        assert _has_edge(mem, test_file, "ocelot_uses_primitive", pid)

    test_ids = mem.nodes_of_type("Test")
    assert test_ids
    assert any(
        _has_edge(mem, tid, "ocelot_uses_primitive", class_id)
        for tid in test_ids
    ), "Test nodes must link to the primitives their file uses"

    shape_id, _shape = _node(mem, "ocelot_term:ShapeDecode")
    count_id, _count = _node(mem, "ocelot_term:Count")
    alias_id, _alias = _node(mem, "ocelot_term:alias_wire")
    mystery_id, mystery = _node(mem, "ocelot_term:MysteryTerm")
    assert mystery["defined"] is False

    seed = mem.node_by_key("ocelot_seed:demo")
    assert seed is not None
    for tid in (shape_id, count_id, alias_id, mystery_id):
        assert _has_edge(mem, seed, "ocelot_uses_term", tid)
    concept = mem.node_by_key("ocelot_term:Concept")
    assert concept is None
    assert not any(
        _has_edge(mem, seed, "ocelot_uses_term", tid)
        and str(((mem.node(tid) or {}).get("attrs") or {}).get("name")) == "Concept"
        for tid in mem.nodes_of_type("OcelotTerm"))

    repo_id, repo = _node(mem, "repo:")
    assert repo["ocelot_primitive_count"] >= 3
    assert repo["ocelot_term_count"] >= 5
    assert repo["ocelot_bridge_error_count"] == 0
    assert repo["ocelot_bridge_errors"] == []
    assert repo_id == mem.node_by_key("repo:")


# --------------------------------------------------------------------------- #
# 3. queries
# --------------------------------------------------------------------------- #
def test_primitive_summary(engine_repo: tuple[Path, DevMemory]) -> None:
    _root, mem = engine_repo

    payload = workspace_engine.engine_summary(
        mem, target="Substrate", op="primitive")
    assert payload["ok"] is True
    assert payload["primitive"] == "Substrate"
    assert payload["definitions"][0]["rust_path"] == "runners/dsl/src/py.rs"
    assert "substrate/use.py" in payload["python_files"]
    assert any("tests/test_use.py" in path for path in payload["tests"])

    all_primitives = workspace_engine.engine_summary(mem)
    assert all_primitives["ok"] is True
    names = {item["name"] for item in all_primitives["primitives"]}
    assert {"Substrate", "Substrate.tick", "substrate_version"} <= names

    missing = workspace_engine.engine_summary(mem, target="NoSuchThing")
    assert missing["ok"] is False
    assert "NoSuchThing" in missing["error"]


def test_rust_impact_for_path_and_primitive(
        engine_repo: tuple[Path, DevMemory]) -> None:
    _root, mem = engine_repo

    impact = workspace_engine.engine_summary(
        mem, target="runners/dsl/src/py.rs", op="rust_impact")
    assert impact["ok"] is True
    assert impact["kind"] == "path"
    assert impact["engine_rebuild"] is True
    assert {"Substrate", "Substrate.tick", "substrate_version"} <= set(
        impact["primitives"])
    assert "substrate/use.py" in impact["python_files"]
    assert any("tests/test_use.py" in path for path in impact["tests"])

    primitive = workspace_engine.engine_summary(
        mem, target="substrate_rs.substrate_version", op="rust_impact")
    assert primitive["ok"] is True
    assert primitive["kind"] == "primitive"
    assert primitive["primitives"] == ["substrate_version"]

    terms = workspace_engine.engine_summary(
        mem, target="runners/dsl/src/term.rs", op="rust_impact")
    assert terms["ok"] is True
    assert {"ShapeDecode", "Count", "Latent", "alias_wire"} <= set(
        terms["terms"])
    assert "demo" in terms["seeds"]


def test_term_census(engine_repo: tuple[Path, DevMemory]) -> None:
    _root, mem = engine_repo

    census = workspace_engine.engine_summary(mem, op="term_census")
    assert census["ok"] is True
    assert {"ShapeDecode", "Count", "Latent", "alias_wire"} <= set(
        census["defined_terms"])
    assert {"ShapeDecode", "Count", "alias_wire", "MysteryTerm"} <= set(
        census["used_terms"])
    assert "MysteryTerm" in census["unknown_terms"]
    assert "MysteryTerm" not in census["defined_terms"]
    assert "Latent" in census["unused_terms"]
    assert "Concept" not in census["defined_terms"]
    assert "Concept" not in census["used_terms"]
    assert "AliasTerm" not in census["unknown_terms"]
    assert census["seed_count"] == 1
    assert census["seeds_with_terms"] == 1
    assert census["seeds"][0]["id"] == "demo"
    top = {item["name"]: item["seeds"] for item in census["top_terms"]}
    assert top.get("ShapeDecode") == 1


# --------------------------------------------------------------------------- #
# 4. MCP and graph-query registration
# --------------------------------------------------------------------------- #
def test_register_engine_is_idempotent_and_declares_inputs() -> None:
    from ozagia.adapters import Registry

    registry = Registry()
    workspace_engine.register_engine(registry)
    workspace_engine.register_engine(registry)

    assert sum(indexer is workspace_engine.index_engine_bridge
               for indexer in registry.indexers) == 1
    assert "ocelot_engine" in registry.graph_queries
    assert "ocelot_engine" in registry.mcp_tools
    assert {"runners/dsl/src/*.rs",
            "runners/dsl/python/substrate_rs/*.py",
            "seeds/*.json"} <= set(registry.watch_globs)
    inputs = registry.indexer_inputs[0]
    assert inputs.texts is not None
    assert inputs.texts.pattern == "substrate_rs|TermNode"


def test_mcp_tool_and_graph_query(engine_repo: tuple[Path, DevMemory]) -> None:
    _root, mem = engine_repo

    query = mem.adapter_query("ocelot_engine", op="primitive",
                              target="Substrate")
    assert query["ok"] is True
    assert query["primitive"] == "Substrate"

    server = DevMemoryServer(mem)
    listed = server.handle(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    names = {tool["name"] for tool in listed["result"]["tools"]}
    assert "ocelot_engine" in names

    response = server.handle({
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "ocelot_engine",
                   "arguments": {"op": "term_census"}},
    })
    assert "error" not in response, response
    result = response["result"]
    assert result.get("isError") is False, result
    payload = json.loads(result["content"][0]["text"])
    assert payload["ok"] is True
    assert "MysteryTerm" in payload["unknown_terms"]
