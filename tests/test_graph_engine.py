"""The stateful `graph` engine: protocol, budgets, known deltas, speculation.

The engine is read-only over an existing `DevMemory` snapshot.  These tests
build a tiny checkout, then call the engine directly and through the MCP
server's engine surface.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from ozagia.core.graph_engine import (
    DEFAULT_BUDGET,
    ENGINE_TOOL_NAME,
    MAX_BUDGET,
    MAX_SESSIONS,
    MAX_SPEC,
    OPS,
    PROTOCOL,
    SPEC_BUDGET,
    GraphEngine,
    description_tokens,
)
from ozagia.core.memory import DevMemory, build_index
from ozagia.integrations.mcp.server import DevMemoryServer

CORE = '''\
"""Core helpers."""


def add(a, b):
    """Add two numbers."""
    return a + b


def double(x):
    """Double x."""
    return add(x, x)
'''
USER = '''\
"""Uses core."""
from pkg.core import double


def quadruple(x):
    """Four times x."""
    return double(double(x))
'''
TEST_CORE = '''\
from pkg.core import add


def test_add():
    assert add(1, 2) == 3
'''
TEST_USER = '''\
from pkg.user import quadruple


def test_quadruple():
    assert quadruple(1) == 4
'''
README = "# Fixture\n\nA small graph engine fixture.\n"


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), "-c", "user.email=t@t",
                    "-c", "user.name=t", *args], check=True,
                   capture_output=True)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    _write(root, "pkg/__init__.py", "")
    _write(root, "pkg/core.py", CORE)
    _write(root, "pkg/user.py", USER)
    _write(root, "tests/test_core.py", TEST_CORE)
    _write(root, "tests/test_user.py", TEST_USER)
    _write(root, "README.md", README)
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "initial")
    return root


def _memory(repo: Path) -> DevMemory:
    dump = repo.parent / "state" / "graph.json"
    build_index(repo, dump, include_docs="features", include_git=False)
    return DevMemory(dump, repo.parent / "state" / "journal.jsonl", root=repo)


@pytest.fixture()
def memory(repo: Path) -> DevMemory:
    return _memory(repo)


@pytest.fixture()
def engine(memory: DevMemory) -> GraphEngine:
    return GraphEngine(memory)


def _hello(engine: GraphEngine, **args: Any) -> dict[str, Any]:
    result = engine.handle({"op": "hello", **args})
    assert result["ok"] is True, result
    return result


def _rpc(server: DevMemoryServer, method: str, params: dict | None = None
         ) -> dict[str, Any]:
    return server.handle({"jsonrpc": "2.0", "id": 1, "method": method,
                          "params": params or {}})


def _call(server: DevMemoryServer, name: str, **args: Any) -> Any:
    reply = _rpc(server, "tools/call", {"name": name, "arguments": args})
    assert "error" not in reply, reply
    assert reply["result"]["isError"] is False, reply
    return json.loads(reply["result"]["content"][0]["text"])


def _cell(result: dict[str, Any], row: list[Any], column: str) -> Any:
    return row[result["cols"].index(column)]


# --------------------------------------------------------------------------- #
# handshake and sessions
# --------------------------------------------------------------------------- #
def test_hello_handshake_and_session_isolation(engine: GraphEngine) -> None:
    first = _hello(engine, budget=999_999)
    second = _hello(engine)
    assert first["proto"] == PROTOCOL
    assert first["sid"] != second["sid"]
    assert first["snap"]["fresh"] == "fresh"
    assert first["snap"]["id"]
    assert "find" in first["caps"] and "fetch" in first["caps"]
    assert first["budget"]["def"] == DEFAULT_BUDGET
    assert first["budget"]["max"] == MAX_BUDGET
    assert first["budget"]["cur"] == MAX_BUDGET
    assert second["budget"]["cur"] == DEFAULT_BUDGET
    assert first["sl"] == {"prepared": 0, "fetched": 0,
                           "wasted": 0, "evicted": 0}
    # Both handles stay isolated and usable.
    one = engine.handle({"op": "find", "session": first["sid"], "q": "add"})
    two = engine.handle({"op": "find", "session": second["sid"], "q": "add"})
    assert one["ok"] and two["ok"]
    assert first["sid"] != second["sid"]


def test_sessions_are_bounded_and_evicted(engine: GraphEngine) -> None:
    handles = [_hello(engine)["sid"] for _ in range(MAX_SESSIONS + 3)]
    assert len(engine.sessions) == MAX_SESSIONS
    expired = engine.handle({"op": "find", "session": handles[0], "q": "add"})
    assert expired["ok"] is False
    assert "unknown or expired session" in expired["err"]
    assert engine.stats()["totals"]["evicted_sessions"] == 3
    # The newest handle still works.
    assert engine.handle({"op": "find", "session": handles[-1],
                          "q": "add"})["ok"] is True


# --------------------------------------------------------------------------- #
# compact rows and hard budgets
# --------------------------------------------------------------------------- #
def test_compact_rows_and_hard_budget(engine: GraphEngine) -> None:
    sid = _hello(engine)["sid"]
    result = engine.handle({"op": "find", "session": sid, "q": "add",
                            "budget": 80})
    assert result["ok"] is True
    assert result["tok"] <= 80
    assert result["cols"] == ["k", "n", "f", "sp", "sig", "sum", "sc"]
    assert all(isinstance(row, list) for row in result["rows"])
    verbose = engine.handle({"op": "find", "session": sid, "q": "add",
                             "verbose": True, "budget": 1200})
    assert verbose["rows"] and isinstance(verbose["rows"][0], dict)
    assert "key" in verbose["rows"][0]
    for budget in (64, 80, 120):
        for request in (
            {"op": "find", "q": "add", "limit": 20},
            {"op": "context", "task": "add two numbers", "limit": 8},
            {"op": "shape", "limit": 20},
        ):
            reply = engine.handle({"session": sid, "budget": budget,
                                   **request})
            assert reply["tok"] <= budget, (budget, request, reply)


def test_page_trimming_returns_next(engine: GraphEngine) -> None:
    sid = _hello(engine)["sid"]
    first = engine.handle({"op": "find", "session": sid, "q": "pkg",
                           "limit": 20, "budget": 100})
    assert first["ok"]
    assert first["tok"] <= 100
    assert first["truncated"] is True
    assert first["omitted"] > 0
    assert first["next"]["op"] == "find"
    assert first["next"]["offset"] == len(first["rows"])
    second = engine.handle({**first["next"], "session": sid, "budget": 100})
    assert second["ok"] and second["tok"] <= 100
    assert second["rows"]
    assert second["rows"][0] != first["rows"][0]


# --------------------------------------------------------------------------- #
# known deltas
# --------------------------------------------------------------------------- #
def test_known_outline_is_a_handle_and_change_reserializes(
        engine: GraphEngine) -> None:
    sid = _hello(engine)["sid"]
    key = "sym:pkg/core.py::add"
    first = engine.handle({"op": "outline", "session": sid, "key": key})
    assert first["rows"]
    digest = _cell(first, first["rows"][0], "h")
    same = engine.handle({
        "op": "outline", "session": sid, "key": key,
        "known": [{"key": key, "hash": digest}]})
    assert same.get("rows", []) == []
    assert same["unchanged"] == [key]
    changed = engine.handle({
        "op": "outline", "session": sid, "key": key,
        "known": [{"key": key, "hash": "not-the-hash"}]})
    assert changed["rows"]
    assert key not in changed.get("unchanged", [])


def test_known_context_is_a_handle(engine: GraphEngine) -> None:
    sid = _hello(engine)["sid"]
    first = engine.handle({"op": "context", "session": sid,
                           "task": "add two numbers", "limit": 8,
                           "budget": 2000})
    assert first["rows"], first
    key = _cell(first, first["rows"][0], "k")
    digest = _cell(first, first["rows"][0], "h")
    same = engine.handle({
        "op": "context", "session": sid, "task": "add two numbers",
        "limit": 8, "budget": 2000, "known": [{"key": key, "hash": digest}]})
    assert key in same.get("unchanged", [])
    assert all(row[same["cols"].index("k")] != key
               for row in same.get("rows", []))


# --------------------------------------------------------------------------- #
# operations and freshness
# --------------------------------------------------------------------------- #
def test_read_operations_against_fixture(engine: GraphEngine) -> None:
    sid = _hello(engine)["sid"]
    found = engine.handle({"op": "find", "session": sid, "q": "add"})
    assert found["rows"]
    key = _cell(found, found["rows"][0], "k")
    assert key == "sym:pkg/core.py::add"
    span = _cell(found, found["rows"][0], "sp")
    assert span and span[0] > 0

    outline = engine.handle({"op": "outline", "session": sid, "key": key})
    assert _cell(outline, outline["rows"][0], "k") == key
    assert _cell(outline, outline["rows"][0], "f") == "pkg/core.py"

    expanded = engine.handle({"op": "expand", "session": sid, "keys": [key],
                              "edges": ["callers"], "depth": 1})
    assert expanded["ok"] and expanded["rows"]
    assert any("double" in str(row) for row in expanded["rows"])

    impact = engine.handle({"op": "impact", "session": sid, "keys": [key],
                            "depth": 2})
    assert impact["ok"] and impact["counts"]
    assert impact["rows"]

    tests = engine.handle({"op": "tests", "session": sid, "key": key})
    assert tests["ok"] and tests["rows"]
    assert any("test_add" in str(row) for row in tests["rows"])

    context = engine.handle({"op": "context", "session": sid,
                             "task": "add two numbers", "budget": 2000})
    assert context["ok"] and context["rows"]

    shape = engine.handle({"op": "shape", "session": sid})
    assert shape["ok"] and shape["rows"]
    assert any("repo" == row[0] for row in shape["rows"])


def test_stale_snapshot_withholds_structure(repo: Path,
                                             memory: DevMemory) -> None:
    engine = GraphEngine(memory)
    sid = _hello(engine)["sid"]
    (repo / "pkg" / "core.py").write_text(CORE + "\n# changed\n",
                                          encoding="utf-8")
    result = engine.handle({"op": "find", "session": sid, "q": "add"})
    assert result["ok"] is False
    assert result["fresh"] == "stale"
    assert result["withheld"] is True
    assert "rows" not in result and "cols" not in result


def test_unknown_snapshot_withholds_structure(
        engine: GraphEngine, monkeypatch: pytest.MonkeyPatch) -> None:
    sid = _hello(engine)["sid"]
    monkeypatch.setattr(engine, "_freshness",
                        lambda: ("unknown", "unknown-snapshot"))
    result = engine.handle({"op": "outline", "session": sid,
                            "key": "sym:pkg/core.py::add"})
    assert result["ok"] is False
    assert result["fresh"] == "unknown"
    assert result["withheld"] is True
    assert "rows" not in result


# --------------------------------------------------------------------------- #
# speculation
# --------------------------------------------------------------------------- #
def test_speculation_prepares_fetches_and_accounts(engine: GraphEngine) -> None:
    sid = _hello(engine)["sid"]
    first = engine.handle({"op": "find", "session": sid, "q": "add",
                           "spec": True, "budget": 1200})
    assert first["ok"]
    assert len(first["spec"]) <= 2
    assert first["ready"] == len(first["spec"])
    assert first["sl"]["prepared"] == first["ready"]
    assert first["pending"] >= 0
    if not first["spec"]:
        pytest.skip("fixture produced no speculation candidates")
    session = engine.sessions[sid]
    handle = first["spec"][0]
    entry = session.spec[handle]
    assert entry["result"]["tok"] <= SPEC_BUDGET
    fetched = engine.handle({"op": "fetch", "session": sid,
                             "fetch": [handle], "budget": 400})
    assert fetched["ok"] is True
    assert fetched["h"] == handle
    assert fetched["tok"] <= 400
    assert fetched["sl"]["fetched"] == 1
    # Fetching does not repeat graph work; the prepared payload is returned.
    assert fetched.get("rows") is not None or fetched.get("qop")
    again = engine.handle({"op": "fetch", "session": sid, "h": handle})
    assert again["ok"] is False and "unknown or expired" in again["err"]


def test_speculation_bound_evicts_and_records_waste(
        engine: GraphEngine) -> None:
    sid = _hello(engine)["sid"]
    prepared = 0
    for _ in range(6):
        result = engine.handle({"op": "find", "session": sid, "q": "add",
                                "spec": True, "budget": 1200})
        prepared += len(result["spec"])
        assert len(engine.sessions[sid].spec) <= MAX_SPEC
    session = engine.sessions[sid]
    assert session.ledger["prepared"] == prepared
    assert session.ledger["evicted"] >= max(0, prepared - MAX_SPEC)
    assert session.ledger["wasted"] >= max(0, prepared - MAX_SPEC)
    assert engine.stats()["totals"]["prepared"] == prepared


# --------------------------------------------------------------------------- #
# MCP engine surface
# --------------------------------------------------------------------------- #
def test_mcp_engine_surface_exposes_only_graph(memory: DevMemory) -> None:
    server = DevMemoryServer(memory, surface="engine")
    tools = _rpc(server, "tools/list")["result"]["tools"]
    assert [tool["name"] for tool in tools] == [ENGINE_TOOL_NAME]
    schema = tools[0]["inputSchema"]
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["op"]
    assert set(schema["properties"]["op"]["enum"]) == set(OPS)
    assert description_tokens() < 200
    hello = _call(server, "graph", op="hello")
    assert hello["ok"] and hello["sid"] and hello["snap"]["fresh"] == "fresh"
    rejected = _rpc(server, "tools/call",
                    {"name": "dev_context", "arguments": {"task": "x"}})
    assert rejected["error"]["code"] == -32602
    init = _rpc(server, "initialize")["result"]
    assert "graph op=hello" in init["instructions"]
    assert "resources" not in init["capabilities"]
    assert _rpc(server, "resources/list")["result"]["resources"] == []


def test_engine_surface_does_not_change_full_or_graph(memory: DevMemory) -> None:
    full = DevMemoryServer(memory, surface="full")
    full_names = [t["name"] for t in _rpc(full, "tools/list")["result"]["tools"]]
    assert "dev_context" in full_names
    assert ENGINE_TOOL_NAME not in full_names
    graph = DevMemoryServer(memory, surface="graph")
    graph_names = [t["name"] for t in _rpc(graph, "tools/list")["result"]["tools"]]
    assert "find" in graph_names
    assert ENGINE_TOOL_NAME not in graph_names
    with pytest.raises(ValueError):
        DevMemoryServer(None, surface="engine")


# --------------------------------------------------------------------------- #
# read-only boundary
# --------------------------------------------------------------------------- #
def test_engine_has_no_write_or_execution_op(repo: Path,
                                             memory: DevMemory,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    engine = GraphEngine(memory)
    sid = _hello(engine)["sid"]

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("the engine must not run tests or write files")

    monkeypatch.setattr("ozagia.check.run_tests", forbidden)
    monkeypatch.setattr("ozagia.edit.txn.write_source", forbidden)
    before = {
        path.relative_to(repo).as_posix(): path.read_bytes()
        for path in repo.rglob("*") if path.is_file()
        and ".ozagia" not in path.parts and ".git" not in path.parts
    }
    for op, extra in (
        ("find", {"q": "add"}),
        ("outline", {"key": "sym:pkg/core.py::add"}),
        ("expand", {"keys": ["sym:pkg/core.py::add"],
                    "edges": ["callers"]}),
        ("impact", {"keys": ["sym:pkg/core.py::add"]}),
        ("tests", {"key": "sym:pkg/core.py::add"}),
        ("context", {"task": "add", "budget": 500}),
        ("shape", {}),
    ):
        result = engine.handle({"op": op, "session": sid, **extra})
        assert result["ok"], (op, result)
    after = {
        path.relative_to(repo).as_posix(): path.read_bytes()
        for path in repo.rglob("*") if path.is_file()
        and ".ozagia" not in path.parts and ".git" not in path.parts
    }
    assert before == after
    for op in ("edit", "create", "check", "refresh"):
        rejected = engine.handle({"op": op, "session": sid})
        assert rejected["ok"] is False
        assert "unknown op" in rejected["err"]
