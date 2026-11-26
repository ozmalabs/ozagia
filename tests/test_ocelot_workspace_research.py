"""Tests for the Ocelot research/evidence overlay.

The synthetic tree has one complete evidence chain (prereg, amendment,
results, index) and one broken topic with a prereg lacking results and a
results document lacking a prereg.  Nothing here imports or executes the
synthetic target repository.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from ozagia.adapters import registry
from ozagia.core.memory import DevMemory, build_index
from ozagia.integrations.mcp.server import DevMemoryServer
from ozagia.integrations.ocelot import workspace_doctor
from ozagia.integrations.ocelot import workspace_research as WR

PREREG = "docs/research/topic/DEMO_PREREG.md"
AMENDMENT = "docs/research/topic/DEMO_AMENDMENT.md"
RESULTS = "docs/research/topic/DEMO_RESULTS.md"
INDEX = "docs/research/topic/INDEX.md"
NOT_INDEXED = "docs/research/topic/NOT_INDEXED.md"
GAP_PREREG = "docs/research/gap/GAP_PREREG.md"
ORPHAN_RESULTS = "docs/research/gap/ORPHAN_RESULTS.md"
SEED = "seeds/demo.json"
SCRIPT = "scripts/measure_demo.py"
TOOL = "tools/check_demo.py"
TEST = "tests/test_demo_migrated.py"
RUST = "runners/dsl/src/lib.rs"
MEASUREMENT = "results/demo.json"
OUT_REL = ".ozagia/graph.json"
JOURNAL_REL = ".ozagia/journal.jsonl"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _write_json(path: Path, value: Any) -> None:
    _write(path, json.dumps(value, indent=2) + "\n")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_workspace(root: Path) -> tuple[Path, str]:
    """Write the fixture and return ``(root, current engine sha256)``."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    _write(root / ".ozagia.toml", """\
[index]
source_dirs = ["scripts", "tools", "runners"]
test_dirs = ["tests"]
docs = ["docs/**/*.md"]
path_prefixes = ["scripts", "tools", "runners", "seeds", "tests", "docs",
                 "results"]

[adapters]
modules = ["ozagia.integrations.ocelot.workspace:register"]
""")

    _write_json(root / SEED, {
        "id": "demo",
        "version": "1.0.0",
        "depends_on": [],
        "description": "Synthetic demo seed.",
        "rules": [{"name": "demo_rule", "match": [], "effects": []}],
        "nodes": [],
        "concepts": [],
        "productions": [],
    })
    _write(root / SCRIPT, "VALUE = 123000\n")
    _write(root / TOOL, "VALUE = 123000\n")
    _write(root / TEST, "def test_demo():\n    assert True\n")
    _write(root / RUST, "pub fn demo() -> u32 { 123000 }\n")
    _write_json(root / MEASUREMENT, {"block": 123000, "value": 1})

    native = root / workspace_doctor.NATIVE_REL
    native.parent.mkdir(parents=True, exist_ok=True)
    native.write_bytes(b"synthetic substrate_rs native engine\n")
    current = _sha256(native)

    _write(root / PREREG, f"""\
# Demo preregistration

Block 123000 tests the demo seed.

Engine deadbeefcafe was recorded before this change.
Commit abc1234 and pinned to def5678.

- seed: `{SEED}`
- script: `{SCRIPT}`
- gate: `{TEST}`
- source: `{RUST}`
- measurement: `{MEASUREMENT}`
""")
    _write(root / AMENDMENT, f"""\
# Demo amendment

Block 123000 adds no new gate.

Engine deadbeefcafe remains the recorded build.
See `{PREREG}` and `{TEST}`.
""")
    _write(root / RESULTS, f"""\
# Demo results

Block 123000 completed.

Engine {current[:12]} matches this checkout.
engine_sha256 = "{current}"

- seed: `{SEED}`
- script: `{SCRIPT}`
- tool: `{TOOL}`
- gate: `{TEST}`
- measurement: `{MEASUREMENT}`
""")
    _write(root / INDEX, f"""\
# Demo research index

Block 123000.

- [{PREREG}]({PREREG})
- [{AMENDMENT}]({AMENDMENT})
- [{RESULTS}]({RESULTS})
""")
    _write(root / NOT_INDEXED, """\
# Notes

Block 555000 is intentionally not a research protocol document.
""")
    _write(root / GAP_PREREG, """\
# Gap preregistration

Block 999999 has no results and no gate.

Engine deadbeefcafe has no matching results.
""")
    _write(root / ORPHAN_RESULTS, """\
# Orphan results

Block 999999 has no preregistration and no gate.
""")
    return root, current


def build_workspace(root: Path) -> DevMemory:
    out = root / OUT_REL
    build_index(root, out, include_docs="none", include_git=False)
    return DevMemory(out, root / JOURNAL_REL, root=root)


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch
              ) -> tuple[Path, DevMemory, str]:
    monkeypatch.delenv("OZAGIA_CONFIG", raising=False)
    monkeypatch.delenv("OZAGIA_ADAPTERS", raising=False)
    root, current = write_workspace(tmp_path / "ocelot-research")
    return root, build_workspace(root), current


def _by_path(memory: DevMemory) -> dict[str, tuple[int, dict[str, Any]]]:
    out: dict[str, tuple[int, dict[str, Any]]] = {}
    for nid in memory.nodes_of_type("OcelotResearch"):
        attrs = dict((memory.node(nid) or {}).get("attrs") or {})
        out[str(attrs.get("path") or "")] = (nid, attrs)
    return out


def _target_paths(memory: DevMemory, nid: int, edge: str) -> set[str]:
    out: set[str] = set()
    for target in memory._neighbours(nid, edge):
        attrs = dict((memory.node(target) or {}).get("attrs") or {})
        if attrs.get("path"):
            out.add(str(attrs["path"]))
    return out


def _assert_inside(root: Path, paths: list[str]) -> None:
    for rel in paths:
        path = Path(rel)
        assert not path.is_absolute(), rel
        candidate = root / rel
        assert candidate.is_file(), rel
        assert candidate.resolve().is_relative_to(root.resolve()), rel


def test_indexes_only_the_four_research_protocol_patterns(
        workspace: tuple[Path, DevMemory, str]) -> None:
    root, mem, _current = workspace
    docs = _by_path(mem)
    assert set(docs) == {PREREG, AMENDMENT, RESULTS, INDEX, GAP_PREREG,
                         ORPHAN_RESULTS}
    assert docs[PREREG][1]["kind"] == "prereg"
    assert docs[AMENDMENT][1]["kind"] == "amendment"
    assert docs[RESULTS][1]["kind"] == "results"
    assert docs[INDEX][1]["kind"] == "index"
    assert docs[PREREG][1]["title"] == "Demo preregistration"
    assert docs[PREREG][1]["topic"] == "docs/research/topic"
    assert docs[PREREG][1]["blocks"] == [123000]
    assert "deadbeefcafe" in docs[PREREG][1]["engine_hashes"]
    assert {"abc1234", "def5678"} <= set(docs[PREREG][1]["commit_pins"])
    assert NOT_INDEXED not in docs
    assert mem.node_by_key(f"ocelot_research:{PREREG}") is not None
    repo = mem.node_by_key("repo:")
    assert repo is not None
    repo_attrs = mem.node(repo)["attrs"]
    assert repo_attrs["ocelot_research_docs"] == len(docs)
    assert repo_attrs["ocelot_research_missing_gate"] == 2

    prereg_id = docs[PREREG][0]
    file_id = mem.node_by_key(f"file:{PREREG}")
    assert file_id is not None
    assert prereg_id in mem._neighbours(file_id, "ocelot_research_doc")
    assert _target_paths(mem, prereg_id, "ocelot_research_uses") >= {
        SEED, SCRIPT, TEST, RUST, MEASUREMENT}
    assert mem._neighbours(prereg_id, "ocelot_research_seed")
    assert _by_path(mem)[PREREG][1]["missing_results"] is False
    assert _by_path(mem)[PREREG][1]["missing_gate"] is False


def test_summary_resolves_block_topic_and_path(
        workspace: tuple[Path, DevMemory, str]) -> None:
    root, mem, _current = workspace
    by_block = WR.research_summary(mem, "123000")
    assert by_block["ok"] is True
    assert by_block["chain"]["prereg"] == [PREREG]
    assert by_block["chain"]["amendments"] == [AMENDMENT]
    assert by_block["chain"]["results"] == [RESULTS]
    assert by_block["chain"]["indexes"] == [INDEX]
    assert by_block["scripts"] == [SCRIPT, TOOL]
    assert by_block["tests"] == [TEST]
    assert by_block["seeds"] == ["demo"]
    assert by_block["missing_gate"] is False
    assert "deadbeefcafe" in by_block["engine_hashes"]
    assert by_block["resources"]
    _assert_inside(root, by_block["resources"])

    by_topic = WR.research_summary(mem, "docs/research/topic")
    assert {item["path"] for item in by_topic["nodes"]} == {
        PREREG, AMENDMENT, RESULTS, INDEX}
    assert by_topic["chain"] == by_block["chain"]

    by_path = WR.research_summary(mem, RESULTS)
    assert by_path["chain"]["prereg"] == [PREREG]
    assert by_path["chain"]["results"] == [RESULTS]

    by_index = WR.research_summary(mem, INDEX)
    assert by_index["chain"]["prereg"] == [PREREG]
    assert by_index["chain"]["results"] == [RESULTS]

    missing = WR.research_summary(mem, "not-a-topic")
    assert missing["ok"] is False
    assert missing["nodes"] == []


def test_stale_compares_only_recorded_engine_hashes(
        workspace: tuple[Path, DevMemory, str]) -> None:
    root, mem, current = workspace
    stale = WR.research_summary(mem, GAP_PREREG)
    assert stale["stale"] is True
    assert any("engine" in reason for reason in stale["reasons"])

    fresh = WR.research_summary(mem, RESULTS)
    assert current[:12] in fresh["engine_hashes"]
    assert fresh["stale"] is False

    # A block/topic summary sees both hashes; one match is enough.
    combined = WR.research_summary(mem, "123000")
    assert combined["stale"] is False
    assert {"deadbeefcafe", current[:12]} <= set(combined["engine_hashes"])


def test_missing_gate_and_census(
        workspace: tuple[Path, DevMemory, str]) -> None:
    _root, mem, _current = workspace
    gap = WR.research_summary(mem, GAP_PREREG)
    assert gap["missing_gate"] is True
    assert gap["chain"]["prereg"] == [GAP_PREREG]
    assert gap["chain"]["results"] == []
    assert gap["scripts"] == []
    assert gap["tests"] == []

    census = WR.research_census(mem)
    assert census["ok"] is True
    assert census["docs"] == 6
    assert census["by_kind"] == {
        "amendment": 1, "index": 1, "prereg": 2, "results": 2}
    assert census["prereg_without_results"] == [GAP_PREREG]
    assert census["results_without_prereg"] == [ORPHAN_RESULTS]
    assert census["missing_gates"] == [GAP_PREREG, ORPHAN_RESULTS]
    assert census["missing_gate"] == 2
    assert census["errors"] == []
    _assert_inside(_root, census["resources"])


def test_files_outside_root_are_never_indexed_or_linked(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OZAGIA_CONFIG", raising=False)
    monkeypatch.delenv("OZAGIA_ADAPTERS", raising=False)
    root, _current = write_workspace(tmp_path / "ocelot-research")
    outside = tmp_path / "outside_prereg.md"
    _write(outside, "# Outside\n\nBlock 424242 engine abcdef12345\n")
    link = root / "docs" / "research" / "topic" / "ESCAPE_PREREG.md"
    link.symlink_to(outside)
    mem = build_workspace(root)

    assert "docs/research/topic/ESCAPE_PREREG.md" not in _by_path(mem)
    repo = mem.node_by_key("repo:")
    assert repo is not None
    errors = mem.node(repo)["attrs"]["ocelot_research_errors"]
    assert any("outside the checkout" in error for error in errors), errors
    summary = WR.research_summary(mem, "123000")
    _assert_inside(root, summary["resources"])
    assert all(not path.startswith(str(tmp_path)) for path in summary["resources"])


def test_register_research_indexer_queries_and_mcp_tool(
        workspace: tuple[Path, DevMemory, str]) -> None:
    root, mem, _current = workspace
    reg = registry(root)
    assert WR.index_research in reg.indexers
    index = reg.indexers.index(WR.index_research)
    inputs = reg.indexer_inputs[index]
    assert inputs.watch == WR.RESEARCH_GLOBS
    assert inputs.texts is False
    assert "ocelot_research" in reg.graph_queries
    assert "ocelot_research_census" in reg.graph_queries

    summary = mem.adapter_query("ocelot_research", target="123000")
    assert summary["ok"] is True
    assert summary["chain"]["results"] == [RESULTS]
    census = mem.adapter_query("ocelot_research")
    assert census["docs"] == 6

    server = DevMemoryServer(mem)
    tools = server.handle({
        "jsonrpc": "2.0", "id": 1, "method": "tools/list",
    })["result"]["tools"]
    spec = next(tool for tool in tools if tool["name"] == "ocelot_research")
    assert "target" in spec["inputSchema"]["properties"]
    assert "required" not in spec["inputSchema"]

    response = server.handle({
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "ocelot_research",
                   "arguments": {"target": "123000"}},
    })
    assert "error" not in response, response
    assert response["result"]["isError"] is False
    payload = json.loads(response["result"]["content"][0]["text"])
    assert payload["chain"]["prereg"] == [PREREG]

    response = server.handle({
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": "ocelot_research", "arguments": {}},
    })
    assert response["result"]["isError"] is False
    payload = json.loads(response["result"]["content"][0]["text"])
    assert payload["docs"] == 6
    assert payload["missing_gates"] == [GAP_PREREG, ORPHAN_RESULTS]
