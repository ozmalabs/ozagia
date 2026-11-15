"""Adapter extension points: indexers, MCP tools, graph queries, and the
seed/rule tools that only appear when the snapshot has Seed/Rule nodes."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from conftest import write_adapter_config
from ozagia import adapters
from ozagia.core import memory as memory_module
from ozagia.core.memory import DevMemory, build_index
from ozagia.integrations.mcp.server import DevMemoryServer

TINY_ADAPTER = '''
from ozagia.adapters import IndexContext

SEEN = {}


def index(ctx: IndexContext) -> None:
    SEEN["sources"] = sorted(s.rel for s in ctx.sources)
    SEEN["tests"] = {t.rel: len(t.tests) for t in ctx.tests}
    SEEN["docs"] = sorted(d.rel for d in ctx.docs)
    SEEN["skipped_state"] = ctx.skipped(".ozagia/x.py")
    b = ctx.builder
    for doc in ctx.docs:
        if "TODO" in doc.text:
            nid = b.node("Note", {"name": "todo in " + doc.rel, "doc": doc.rel},
                         key=("note", doc.rel))
            b.edge(doc.node, "has_note", nid)
            b.edge(nid, "about", ctx.file_node("a.py"))
    b.attrs(ctx.repo)["notes"] = b.count("Note")


def echo(memory, args):
    return {"ok": True, "echo": args["text"], "times": args.get("times", 1),
            "notes": len([n for n in memory.nodes.values() if n["type"] == "Note"])}


def clash(memory, args):
    return {"ok": False}


def register(registry):
    registry.add_indexer(index)
    registry.add_mcp_tool({
        "name": "dev_echo",
        "description": "Echo text.",
        "inputSchema": {"type": "object",
                        "properties": {"text": {"type": "string"},
                                       "times": {"type": "integer"}},
                        "required": ["text"], "additionalProperties": False},
    }, echo)
    registry.add_mcp_tool({"name": "dev_stats", "description": "clash"}, clash)
    registry.add_graph_query("notes", lambda memory, prefix="":
                             [n["attrs"]["name"] for n in memory.nodes.values()
                              if n["type"] == "Note"
                              and n["attrs"]["name"].startswith(prefix)])
'''


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _repo(root: Path) -> None:
    _write(root / "a.py", '"""A."""\n\n\ndef alpha():\n    return 1\n')
    _write(root / "tests" / "test_a.py", "from a import alpha\n\n\n"
                                         "def test_alpha():\n    assert alpha()\n\n\n"
                                         "def test_beta():\n    assert True\n")
    _write(root / "README.md", "# Tiny\n\nTODO: write `a.py` docs.\n")
    _write(root / "seeds" / "demo.json", json.dumps({
        "id": "demo", "version": "1", "depends_on": [], "nodes": [],
        "rules": []}))


@pytest.fixture
def tiny_adapter(tmp_path_factory, monkeypatch) -> Path:
    base = tmp_path_factory.mktemp("tiny_adapter")
    _write(base / "ozagia_test_tiny_adapter.py", TINY_ADAPTER)
    cfg = write_adapter_config(base, ["ozagia_test_tiny_adapter:register"])
    monkeypatch.setenv("OZAGIA_CONFIG", str(cfg))
    return cfg


def _build(root: Path, tmp_path: Path, monkeypatch) -> DevMemory:
    monkeypatch.setattr(memory_module, "SOURCE_DIRS", (".",))
    monkeypatch.setattr(memory_module, "TEST_DIRS", ("tests",))
    monkeypatch.setattr(memory_module, "DOC_GLOBS", ("README.md",))
    out = tmp_path / "out" / "graph.json"
    build_index(root, out, include_docs="features", include_git=False)
    return DevMemory(out, tmp_path / "out" / "journal.jsonl", root=root)


def _names(server: DevMemoryServer) -> set[str]:
    resp = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    return {t["name"] for t in resp["result"]["tools"]}


def _call(server: DevMemoryServer, name: str, arguments: Any) -> dict[str, Any]:
    return server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                          "params": {"name": name, "arguments": arguments}})


def test_indexer_sees_the_build_and_adds_nodes(tmp_path: Path, monkeypatch,
                                               tiny_adapter) -> None:
    root = tmp_path / "repo"
    _repo(root)
    mem = _build(root, tmp_path, monkeypatch)
    import ozagia_test_tiny_adapter as tiny
    assert tiny.SEEN["sources"] == ["a.py"]
    assert tiny.SEEN["tests"] == {"tests/test_a.py": 2}
    assert tiny.SEEN["docs"] == ["README.md"]
    assert tiny.SEEN["skipped_state"] is True

    notes = [n for n in mem.nodes.values() if n["type"] == "Note"]
    assert [n["attrs"]["name"] for n in notes] == ["todo in README.md"]
    nid = int(notes[0]["id"])
    assert [x["label"] for x in mem.related(nid)["about"]] == ["a.py"]
    assert mem.stats()["repo"]["notes"] == 1
    assert mem.adapter_query("notes", prefix="todo") == ["todo in README.md"]
    assert mem.adapter_query("missing")["ok"] is False


def test_adapter_mcp_tool_is_listed_and_dispatched(tmp_path: Path, monkeypatch,
                                                   tiny_adapter) -> None:
    root = tmp_path / "repo"
    _repo(root)
    server = DevMemoryServer(_build(root, tmp_path, monkeypatch))
    names = _names(server)
    assert "dev_echo" in names
    resp = _call(server, "dev_echo", {"text": "hi", "times": "3"})
    assert resp["result"]["isError"] is False
    payload = json.loads(resp["result"]["content"][0]["text"])
    assert payload == {"ok": True, "echo": "hi", "times": 3, "notes": 1}
    # Arguments are checked against the adapter's inputSchema.
    assert _call(server, "dev_echo", {})["error"]["code"] == -32602
    assert _call(server, "dev_echo", {"text": "x", "bogus": 1})["error"]["code"] == -32602
    # A clashing name keeps the built-in tool.
    stats = json.loads(_call(server, "dev_stats", {})["result"]["content"][0]["text"])
    assert stats["ok"] is True and "types" in stats


def test_seed_and_rule_tools_hidden_without_seed_nodes(tmp_path: Path,
                                                       monkeypatch) -> None:
    monkeypatch.delenv("OZAGIA_CONFIG", raising=False)
    monkeypatch.delenv("OZAGIA_ADAPTERS", raising=False)
    root = tmp_path / "repo"
    _repo(root)
    mem = _build(root, tmp_path, monkeypatch)
    assert not [n for n in mem.nodes.values() if n["type"] == "Seed"]
    assert "seeds" not in mem.stats()["repo"]
    server = DevMemoryServer(mem)
    names = _names(server)
    assert not {"dev_seed", "dev_rule", "dev_graph_query"} & names
    assert {"dev_search", "dev_file", "dev_tests"} <= names
    resp = _call(server, "dev_seed", {"seed_id": "demo"})
    assert resp["error"]["code"] == -32602
    assert "unknown tool" in resp["error"]["message"]
    assert "seeds:" not in mem.brief()["markdown"]


def test_seed_tools_shown_with_seed_nodes(tmp_path: Path, monkeypatch,
                                          seed_adapter) -> None:
    root = tmp_path / "repo"
    _repo(root)
    server = DevMemoryServer(_build(root, tmp_path, monkeypatch))
    names = _names(server)
    # demo.json has a Seed but no rules: dev_seed is listed, dev_rule is not.
    assert "dev_seed" in names
    assert "dev_rule" not in names
    assert "dev_graph_query" in names
    payload = json.loads(_call(server, "dev_seed", {"seed_id": "demo"})
                         ["result"]["content"][0]["text"])
    assert payload["ok"] is True and payload["attrs"]["id"] == "demo"
    # The Ocelot adapter's graph query checks its arguments before it imports
    # anything from the Ocelot checkout.
    gq = json.loads(_call(server, "dev_graph_query", {})["result"]["content"][0]["text"])
    assert gq == {"ok": False, "error": "pass exactly one of term= or match="}


def test_rule_tool_shown_with_rule_nodes(tmp_path: Path, monkeypatch,
                                         seed_adapter) -> None:
    root = tmp_path / "repo"
    _repo(root)
    _write(root / "seeds" / "demo.json", json.dumps({
        "id": "demo", "version": "1", "depends_on": [],
        "rules": [{"name": "demo_rule", "match": [], "effects": []}]}))
    server = DevMemoryServer(_build(root, tmp_path, monkeypatch))
    assert {"dev_seed", "dev_rule"} <= _names(server)
    payload = json.loads(_call(server, "dev_rule", {"name": "demo_rule"})
                         ["result"]["content"][0]["text"])
    assert payload["ok"] is True


def test_add_mcp_tool_requires_a_name() -> None:
    reg = adapters.Registry()
    with pytest.raises(ValueError):
        reg.add_mcp_tool({"description": "x"}, lambda m, a: None)
    reg.add_mcp_tool({"name": "dev_x"}, lambda m, a: None)
    assert reg.mcp_tools["dev_x"][0]["inputSchema"] == {"type": "object",
                                                        "properties": {}}
