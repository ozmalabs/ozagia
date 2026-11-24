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
    CAP_MASK,
    DEFAULT_BUDGET,
    ENGINE_TOOL_NAME,
    MAX_BUDGET,
    MAX_SESSIONS,
    MAX_SPEC,
    OPS,
    PROTOCOL,
    SPEC_BUDGET,
    GraphEngine,
    _short_keys,
    description_tokens,
    estimate_tokens,
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
    assert "e" not in result, result
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


def _columns(result: dict[str, Any], engine: GraphEngine,
             sid: str, op: str) -> list[str]:
    return list(result.get("c") or engine.sessions[sid].cols[op])


def _cell(result: dict[str, Any], rows: list[Any], row: list[Any],
          column: str, engine: GraphEngine, sid: str, op: str) -> Any:
    cols = _columns(result, engine, sid, op)
    return row[cols.index(column)]


# --------------------------------------------------------------------------- #
# handshake and sessions
# --------------------------------------------------------------------------- #
def test_hello_is_minimal_and_detail_is_opt_in(engine: GraphEngine) -> None:
    first = _hello(engine, budget=999_999)
    second = _hello(engine)
    assert first["p"] == PROTOCOL
    assert first["v"] == "2.0"
    assert first["sid"] != second["sid"]
    assert first["f"] == "fresh" and first["s"]
    assert first["b"] == [DEFAULT_BUDGET, MAX_BUDGET]
    assert first["c"] == CAP_MASK
    assert first["t"] < 50 and second["t"] < 50
    assert "ops" not in first and "st" not in first
    assert "sl" not in first and "snap" not in first
    detail = _hello(engine, caps=True, budget=MAX_BUDGET)
    assert set(detail["ops"]) == set(OPS)
    assert set(detail["ft"]) == {"compact", "known", "spec", "fetch"}
    assert detail["bd"] == {"d": DEFAULT_BUDGET, "m": MAX_BUDGET,
                            "c": MAX_BUDGET}
    assert detail["st"] == {"prepared": 0, "fetched": 0,
                            "wasted": 0, "evicted": 0}
    # Both handles stay isolated and usable.
    one = engine.handle({"op": "find", "session": first["sid"], "q": "add"})
    two = engine.handle({"op": "find", "session": second["sid"], "q": "add"})
    assert "e" not in one and "e" not in two
    assert first["sid"] != second["sid"]


def test_sessions_are_bounded_and_evicted(engine: GraphEngine) -> None:
    handles = [_hello(engine)["sid"] for _ in range(MAX_SESSIONS + 3)]
    assert len(engine.sessions) == MAX_SESSIONS
    expired = engine.handle({"op": "find", "session": handles[0], "q": "add"})
    assert "e" in expired
    assert "unknown or expired session" in expired["e"]
    assert engine.stats()["totals"]["evicted_sessions"] == 3
    # The newest handle still works.
    assert "e" not in engine.handle({"op": "find", "session": handles[-1],
                                     "q": "add"})


# --------------------------------------------------------------------------- #
# compact rows, column reuse and hard budgets
# --------------------------------------------------------------------------- #
def test_compact_rows_columns_and_hard_budget(engine: GraphEngine) -> None:
    sid = _hello(engine)["sid"]
    result = engine.handle({"op": "find", "session": sid, "q": "add",
                            "budget": 80})
    assert "e" not in result
    assert result["t"] <= 80
    assert result["c"] == ["k", "n", "f", "sp", "sig", "sum", "sc"]
    assert all(isinstance(row, list) for row in result.get("r", []))
    # Fixed columns are sent once per session unless requested again.
    second = engine.handle({"op": "find", "session": sid, "q": "add"})
    assert "c" not in second
    again = engine.handle({"op": "find", "session": sid, "q": "add",
                           "cols": True})
    assert again["c"] == ["k", "n", "f", "sp", "sig", "sum", "sc"]
    verbose = engine.handle({"op": "find", "session": sid, "q": "add",
                             "verbose": True, "budget": 1200})
    assert verbose["r"] and isinstance(verbose["r"][0], dict)
    assert "key" in verbose["r"][0]
    for budget in (64, 80, 120):
        for request in (
            {"op": "find", "q": "add", "limit": 20},
            {"op": "context", "task": "add two numbers", "limit": 8},
            {"op": "shape", "limit": 20},
        ):
            reply = engine.handle({"session": sid, "budget": budget,
                                   **request})
            assert reply["t"] <= budget, (budget, request, reply)


def test_stats_are_opt_in(engine: GraphEngine) -> None:
    sid = _hello(engine)["sid"]
    plain = engine.handle({"op": "find", "session": sid, "q": "add"})
    assert "st" not in plain
    with_stats = engine.handle({"op": "find", "session": sid, "q": "add",
                                "stats": True})
    assert with_stats["st"] == {"prepared": 0, "fetched": 0,
                                "wasted": 0, "evicted": 0}


def test_page_trimming_returns_next(engine: GraphEngine) -> None:
    sid = _hello(engine)["sid"]
    first = engine.handle({"op": "find", "session": sid, "q": "pkg",
                           "limit": 20, "budget": 100})
    assert "e" not in first
    assert first["t"] <= 100
    assert first["x"] is True
    assert first["o"] > 0
    assert first["n"]["op"] == "find"
    assert first["n"]["offset"] == len(first["r"])
    second = engine.handle({**first["n"], "session": sid, "budget": 100})
    assert "e" not in second and second["t"] <= 100
    assert second["r"]
    assert second["r"][0] != first["r"][0]


# --------------------------------------------------------------------------- #
# known deltas
# --------------------------------------------------------------------------- #
def test_known_outline_is_a_handle_and_change_reserializes(
        engine: GraphEngine) -> None:
    sid = _hello(engine)["sid"]
    key = "sym:pkg/core.py::add"
    first = engine.handle({"op": "outline", "session": sid, "key": key})
    assert first["r"]
    digest = _cell(first, first["r"], first["r"][0], "h", engine, sid,
                   "outline")
    same = engine.handle({
        "op": "outline", "session": sid, "key": key,
        "known": [{"key": key, "hash": digest}]})
    assert same.get("r", []) == []
    assert same["u"] == [key]
    assert same["t"] < first["t"]
    changed = engine.handle({
        "op": "outline", "session": sid, "key": key,
        "known": [{"key": key, "hash": "not-the-hash"}]})
    assert changed["r"]
    assert key not in changed.get("u", [])


def test_known_context_is_a_handle(engine: GraphEngine) -> None:
    sid = _hello(engine)["sid"]
    first = engine.handle({"op": "context", "session": sid,
                           "task": "add two numbers", "limit": 8,
                           "budget": 2000})
    assert first["r"], first
    key = _cell(first, first["r"], first["r"][0], "k", engine, sid, "context")
    digest = _cell(first, first["r"], first["r"][0], "h", engine, sid,
                   "context")
    same = engine.handle({
        "op": "context", "session": sid, "task": "add two numbers",
        "limit": 8, "budget": 2000, "known": [{"key": key, "hash": digest}]})
    assert key in same.get("u", [])
    assert all(row[same["c"].index("k")] != key
               for row in same.get("r", []) if "c" in same)


# --------------------------------------------------------------------------- #
# operations and freshness
# --------------------------------------------------------------------------- #
def test_read_operations_against_fixture(engine: GraphEngine) -> None:
    sid = _hello(engine)["sid"]
    found = engine.handle({"op": "find", "session": sid, "q": "add"})
    assert found["r"]
    key = _cell(found, found["r"], found["r"][0], "k", engine, sid, "find")
    assert key == "sym:pkg/core.py::add"
    span = _cell(found, found["r"], found["r"][0], "sp", engine, sid, "find")
    assert span and span[0] > 0

    outline = engine.handle({"op": "outline", "session": sid, "key": key})
    assert _cell(outline, outline["r"], outline["r"][0], "k", engine, sid,
                 "outline") == key
    assert _cell(outline, outline["r"], outline["r"][0], "f", engine, sid,
                 "outline") == "pkg/core.py"

    expanded = engine.handle({"op": "expand", "session": sid, "keys": [key],
                              "edges": ["callers"], "depth": 1})
    assert "e" not in expanded and expanded["r"]
    assert any("double" in str(row) for row in expanded["r"])

    impact = engine.handle({"op": "impact", "session": sid, "keys": [key],
                            "depth": 2})
    assert "e" not in impact and impact["ct"]
    assert impact["r"]

    tests = engine.handle({"op": "tests", "session": sid, "key": key})
    assert "e" not in tests and tests["r"]
    assert any("test_add" in str(row) for row in tests["r"])

    context = engine.handle({"op": "context", "session": sid,
                             "task": "add two numbers", "budget": 2000})
    assert "e" not in context and context["r"]

    shape = engine.handle({"op": "shape", "session": sid})
    assert "e" not in shape and shape["r"]
    assert any("repo" == row[0] for row in shape["r"])


def test_stale_snapshot_withholds_structure(repo: Path,
                                             memory: DevMemory) -> None:
    engine = GraphEngine(memory)
    sid = _hello(engine)["sid"]
    (repo / "pkg" / "core.py").write_text(CORE + "\n# changed\n",
                                          encoding="utf-8")
    result = engine.handle({"op": "find", "session": sid, "q": "add"})
    assert "e" in result
    assert result["f"] == "stale"
    assert result["s"]
    assert "r" not in result and "c" not in result


def test_unknown_snapshot_withholds_structure(
        engine: GraphEngine, monkeypatch: pytest.MonkeyPatch) -> None:
    sid = _hello(engine)["sid"]
    monkeypatch.setattr(engine, "_freshness",
                        lambda: ("unknown", "unknown-snapshot"))
    result = engine.handle({"op": "outline", "session": sid,
                            "key": "sym:pkg/core.py::add"})
    assert "e" in result
    assert result["f"] == "unknown"
    assert "r" not in result


# --------------------------------------------------------------------------- #
# speculation
# --------------------------------------------------------------------------- #
def test_speculation_prepares_fetches_and_accounts(engine: GraphEngine) -> None:
    sid = _hello(engine)["sid"]
    first = engine.handle({"op": "find", "session": sid, "q": "add",
                           "spec": True, "budget": 1200})
    assert "e" not in first
    handles = first["q"]["h"]
    assert len(handles) <= 2
    assert first["q"]["r"] == len(handles)
    assert "st" not in first
    if not handles:
        pytest.skip("fixture produced no speculation candidates")
    session = engine.sessions[sid]
    handle = handles[0]
    entry = session.spec[handle]
    assert estimate_tokens(_short_keys(entry["result"])) <= SPEC_BUDGET
    fetched = engine.handle({"op": "fetch", "session": sid,
                             "fetch": [handle], "budget": 400,
                             "stats": True})
    assert "e" not in fetched
    assert fetched["h"] == handle
    assert fetched["t"] <= 400
    assert fetched["st"]["fetched"] == 1
    # Fetching does not repeat graph work; the prepared payload is returned.
    assert fetched.get("r") is not None or fetched.get("qo")
    again = engine.handle({"op": "fetch", "session": sid, "h": handle})
    assert "e" in again and "unknown or expired" in again["e"]


def test_speculation_bound_evicts_and_records_waste(
        engine: GraphEngine) -> None:
    sid = _hello(engine)["sid"]
    prepared = 0
    for _ in range(6):
        result = engine.handle({"op": "find", "session": sid, "q": "add",
                                "spec": True, "budget": 1200})
        prepared += len(result["q"]["h"])
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
    assert hello["sid"] and hello["f"] == "fresh" and hello["t"] < 50
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
        assert "e" not in result, (op, result)
    after = {
        path.relative_to(repo).as_posix(): path.read_bytes()
        for path in repo.rglob("*") if path.is_file()
        and ".ozagia" not in path.parts and ".git" not in path.parts
    }
    assert before == after
    for op in ("edit", "create", "check", "refresh"):
        rejected = engine.handle({"op": op, "session": sid})
        assert "e" in rejected
        assert "unknown op" in rejected["e"]
