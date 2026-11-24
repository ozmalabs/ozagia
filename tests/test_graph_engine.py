"""The stateful `graph` engine: protocol, budgets, known deltas, speculation.

The engine is read-only over an existing `DevMemory` snapshot.  These tests
build a tiny checkout, then call the engine directly and through the MCP
server's engine surface.
"""
from __future__ import annotations

import json
import multiprocessing
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from ozagia.core.graph_engine import (
    CAP_MASK,
    DEFAULT_BUDGET,
    ENGINE_TOOL_NAME,
    MAX_BUDGET,
    MAX_FETCH_WAIT_MS,
    MAX_SESSIONS,
    MAX_SPEC_PREPARE,
    OPS,
    PROTOCOL,
    SPEC_BUDGET,
    SPEC_TTL_S,
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
FORK_AVAILABLE = "fork" in multiprocessing.get_all_start_methods()


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
def engine(memory: DevMemory):
    instance = GraphEngine(memory)
    yield instance
    instance.close()


class ManualWorker:
    def __init__(self) -> None:
        self.jobs: list[Any] = []
        self.closed = False

    def submit(self, job: Any) -> bool:
        if self.closed:
            return False
        self.jobs.append(job)
        return True

    def close(self, timeout: float = 1.0) -> None:
        self.closed = True
        self.jobs.clear()


def _manual_engine(memory: DevMemory, *, clock: Any = None
                   ) -> tuple[GraphEngine, ManualWorker]:
    worker = ManualWorker()
    instance = GraphEngine(memory, spec_worker=worker, clock=clock)
    return instance, worker


@pytest.fixture()
def manual(memory: DevMemory):
    instance, worker = _manual_engine(memory)
    yield instance, worker
    instance.close()


def _run_next(engine: GraphEngine, worker: ManualWorker) -> None:
    assert worker.jobs, "no speculation job was submitted"
    engine._run_spec_job(worker.jobs.pop(0))


def _wait_status(engine: GraphEngine, sid: str, handles: list[str],
                 wanted: str, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    session = engine.sessions[sid]
    while time.monotonic() < deadline:
        if all(session.spec.get(handle, {}).get("status") == wanted
               for handle in handles):
            return
        time.sleep(0.01)
    raise AssertionError(
        f"handles did not become {wanted}: "
        f"{[(h, session.spec.get(h, {}).get('status')) for h in handles]}")


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
    assert {"compact", "known", "spec", "fetch"} <= set(detail["ft"])
    assert "async-spec" in detail["ft"]
    assert detail["bd"] == {"d": DEFAULT_BUDGET, "m": MAX_BUDGET,
                            "c": MAX_BUDGET}
    assert detail["st"]["prepared"] == 0
    assert detail["st"]["fetched"] == 0
    assert detail["st"]["hit_rate"] == 0.0
    assert set(detail["st"]) >= {"prepared", "fetched", "wasted", "evicted",
                                 "cancelled", "worker_ms", "hits", "misses",
                                 "hit_rate"}
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
    assert with_stats["st"]["prepared"] == 0
    assert with_stats["st"]["fetched"] == 0
    assert with_stats["st"]["hit_rate"] == 0.0
    assert "worker_ms" in with_stats["st"]


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
def _spec_result(engine: GraphEngine, sid: str,
                 **extra: Any) -> dict[str, Any]:
    result = engine.handle({"op": "find", "session": sid, "q": "add",
                            "spec": True, "budget": 1200, **extra})
    assert "e" not in result, result
    return result


def test_background_response_returns_before_preparation(manual) -> None:
    engine, worker = manual
    sid = _hello(engine)["sid"]
    first = _spec_result(engine, sid)
    handles = first["q"]["h"]
    assert 0 < len(handles) <= MAX_SPEC_PREPARE
    assert first["q"]["r"] == 0
    assert first["q"]["p"] >= 0
    assert len(worker.jobs) == 1
    job = worker.jobs[0]
    assert 0 < len(job.requests) <= MAX_SPEC_PREPARE
    assert len(job.requests) <= len(job.suggestions) <= 6
    session = engine.sessions[sid]
    assert all(session.spec[handle]["status"] == "pending"
               for handle in handles)
    assert session.ledger["prepared"] == 0
    assert "st" not in first


def test_fetch_after_completion_hits_without_recompute(
        manual, monkeypatch: pytest.MonkeyPatch) -> None:
    engine, worker = manual
    sid = _hello(engine)["sid"]
    first = _spec_result(engine, sid)
    handles = first["q"]["h"]
    _run_next(engine, worker)
    session = engine.sessions[sid]
    assert session.ledger["prepared"] == len(handles)
    entry = session.spec[handles[0]]
    assert entry["status"] == "ready"
    assert estimate_tokens(_short_keys(entry["result"])) <= SPEC_BUDGET

    def no_graph_work(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("fetch must not recompute")

    monkeypatch.setattr(engine, "_execute", no_graph_work)
    fetched = engine.handle({"op": "fetch", "session": sid,
                             "fetch": [handles[0]], "budget": 400,
                             "stats": True})
    assert "e" not in fetched
    assert fetched["h"] == handles[0]
    assert fetched["t"] <= 400
    assert fetched["st"]["fetched"] == 1
    assert fetched["st"]["hits"] == 1
    assert fetched["st"]["hit_rate"] == 1.0
    assert fetched.get("r") is not None or fetched.get("qo")
    again = engine.handle({"op": "fetch", "session": sid, "h": handles[0]})
    assert "e" in again and "unknown or expired" in again["e"]


def test_fetch_pending_bounded_wait_returns_pending(manual) -> None:
    engine, worker = manual
    sid = _hello(engine)["sid"]
    first = _spec_result(engine, sid)
    handles = first["q"]["h"]
    started = time.monotonic()
    pending = engine.handle({"op": "fetch", "session": sid,
                             "fetch": handles, "wait_ms": 5,
                             "stats": True})
    elapsed = time.monotonic() - started
    assert "e" not in pending
    assert elapsed < MAX_FETCH_WAIT_MS / 1000 + 0.5
    items = pending.get("it") or [pending]
    assert all(item["fs"] == "pending" for item in items)
    assert pending["st"]["misses"] == len(handles)
    assert pending["st"]["hit_rate"] == 0.0
    one = engine.handle({"op": "fetch", "session": sid,
                         "fetch": [handles[0]], "wait_ms": 1,
                         "stats": True})
    assert one["fs"] == "pending" and "st" in one
    # A later fetch can still use the entry once the worker has run.
    _run_next(engine, worker)
    ready = engine.handle({"op": "fetch", "session": sid,
                           "fetch": handles, "stats": True})
    assert "e" not in ready
    assert ready["st"]["hits"] == len(handles)
    assert ready["st"]["hit_rate"] == 0.4


@pytest.mark.parametrize("change", ["generation", "scope", "snapshot"])
def test_changed_generation_scope_or_snapshot_discards(
        manual, change: str) -> None:
    engine, worker = manual
    sid = _hello(engine)["sid"]
    first = _spec_result(engine, sid)
    handles = first["q"]["h"]
    session = engine.sessions[sid]
    if change == "generation":
        session.spec_generation += 1
    elif change == "scope":
        session.scope = "different-scope"
    else:
        session.snap = "different-snapshot"
    _run_next(engine, worker)
    assert session.ledger["prepared"] == 0
    assert all(session.spec[handle]["status"] != "ready"
               for handle in handles)
    fetched = engine.handle({"op": "fetch", "session": sid,
                             "fetch": handles, "stats": True})
    if "e" in fetched:
        assert "unknown or expired" in fetched["e"]
    else:
        items = fetched.get("it") or [fetched]
        assert all(item.get("fs") == "cancelled" for item in items)
    assert session.ledger["prepared"] == 0


def test_worker_budget_candidate_cap_and_expiry(memory: DevMemory) -> None:
    now = [0.0]

    def clock() -> float:
        return now[0]

    engine, worker = _manual_engine(memory, clock=clock)
    try:
        sid = _hello(engine)["sid"]
        first = _spec_result(engine, sid)
        handles = first["q"]["h"]
        assert len(handles) <= MAX_SPEC_PREPARE
        job = worker.jobs.pop(0)
        now[0] = job.deadline + 0.001
        engine._run_spec_job(job)
        session = engine.sessions[sid]
        assert session.ledger["prepared"] == 0
        assert session.ledger["cancelled"] == len(handles)

        engine.worker_budget_s = 100.0
        second = _spec_result(engine, sid)
        handles = second["q"]["h"]
        job = worker.jobs.pop(0)
        now[0] = SPEC_TTL_S + 1
        engine._run_spec_job(job)
        fetched = engine.handle({"op": "fetch", "session": sid,
                                 "fetch": handles, "stats": True})
        items = fetched.get("it") or [fetched]
        assert all(item.get("fs") == "cancelled" for item in items)
        assert session.ledger["prepared"] == 0
    finally:
        engine.close()


def test_close_and_eviction_clear_speculation(manual) -> None:
    engine, worker = manual
    sid = _hello(engine)["sid"]
    first = _spec_result(engine, sid)
    assert first["q"]["h"]
    session = engine.sessions[sid]
    closed = engine.handle({"op": "bye", "session": sid})
    assert closed["bye"] == 1
    assert sid not in engine.sessions
    assert session.closed is True
    assert not session.spec
    _run_next(engine, worker)
    assert session.ledger["prepared"] == 0
    engine.close()
    assert worker.closed is True


def test_refused_spec_request_cancels_cleanly(manual) -> None:
    engine, worker = manual
    sid = _hello(engine)["sid"]
    first = _spec_result(engine, sid)
    handles = first["q"]["h"]
    job = worker.jobs[0]
    job.requests = [(handles[0], {"op": "not_an_op"})]
    engine._run_spec_job(job)
    session = engine.sessions[sid]
    assert session.ledger["prepared"] == 0
    assert session.spec[handles[0]]["status"] == "cancelled"
    fetched = engine.handle({"op": "fetch", "session": sid,
                             "fetch": [handles[0]], "stats": True})
    assert fetched["fs"] == "cancelled"


def test_ledger_records_hit_and_waste(manual) -> None:
    engine, worker = manual
    sid = _hello(engine)["sid"]
    first = _spec_result(engine, sid)
    handles = first["q"]["h"]
    _run_next(engine, worker)
    session = engine.sessions[sid]
    fetched = engine.handle({"op": "fetch", "session": sid,
                             "fetch": [handles[0]], "stats": True})
    assert fetched["st"]["hits"] == 1
    engine.handle({"op": "bye", "session": sid})
    ledger = engine._ledger(session)
    assert ledger["prepared"] == len(handles)
    assert ledger["fetched"] == 1
    assert ledger["wasted"] == len(handles) - 1
    assert ledger["hit_rate"] == 1.0


def test_speculation_can_be_disabled(memory: DevMemory,
                                     monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OZAGIA_ENGINE_SPEC", "0")
    engine = GraphEngine(memory)
    try:
        sid = _hello(engine)["sid"]
        result = _spec_result(engine, sid)
        assert result["q"] == {"h": [], "r": 0, "p": 0}
        assert engine.stats()["spec_enabled"] is False
        assert engine.spec_worker is None
    finally:
        engine.close()


def test_thread_worker_prepares_and_stops(memory: DevMemory,
                                          monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OZAGIA_ENGINE_SPEC", raising=False)
    monkeypatch.setenv("OZAGIA_ENGINE_SPEC_PROCESS", "0")
    engine = GraphEngine(memory)
    try:
        assert engine.stats()["worker_mode"] == "thread"
        sid = _hello(engine)["sid"]
        first = _spec_result(engine, sid)
        handles = first["q"]["h"]
        _wait_status(engine, sid, handles, "ready")
        fetched = engine.handle({"op": "fetch", "session": sid,
                                 "fetch": handles, "stats": True})
        assert "e" not in fetched
        assert fetched["st"]["hits"] == len(handles)
        assert fetched["st"]["worker_ms"] > 0
    finally:
        worker = engine.spec_worker
        engine.close()
    assert worker is not None and not worker.alive
    assert engine.stats()["worker_alive"] is False


@pytest.mark.skipif(not FORK_AVAILABLE, reason="fork is not available")
def test_process_worker_prepares_after_response_and_reaps(
        memory: DevMemory, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OZAGIA_ENGINE_SPEC", raising=False)
    monkeypatch.delenv("OZAGIA_ENGINE_SPEC_PROCESS", raising=False)
    engine = GraphEngine(memory)
    try:
        assert engine.stats()["worker_mode"] == "process"
        sid = _hello(engine)["sid"]
        first = _spec_result(engine, sid)
        handles = first["q"]["h"]
        assert handles
        _wait_status(engine, sid, handles, "ready")
        fetched = engine.handle({"op": "fetch", "session": sid,
                                 "fetch": handles, "stats": True})
        assert "e" not in fetched
        assert fetched["st"]["hits"] == len(handles)
        assert fetched["st"]["worker_ms"] > 0
        worker = engine.spec_worker
        assert worker is not None and worker.mode == "process"
    finally:
        worker = engine.spec_worker
        engine.close()
    assert worker is not None and not worker.alive
    assert getattr(worker, "_process", None) is None
    assert engine.stats()["worker_alive"] is False


@pytest.mark.skipif(not FORK_AVAILABLE, reason="fork is not available")
def test_process_child_crash_falls_back_to_thread(
        memory: DevMemory, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OZAGIA_ENGINE_SPEC", raising=False)
    monkeypatch.delenv("OZAGIA_ENGINE_SPEC_PROCESS", raising=False)
    engine = GraphEngine(memory)
    try:
        sid = _hello(engine)["sid"]
        first = _spec_result(engine, sid)
        worker = engine.spec_worker
        assert worker is not None
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            process = getattr(worker, "_process", None)
            if process is not None and process.is_alive():
                break
            time.sleep(0.01)
        process = getattr(worker, "_process", None)
        assert process is not None and process.is_alive()
        process.kill()
        process.join(1.0)
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and worker.mode != "thread":
            time.sleep(0.01)
            if worker.mode == "dead":
                break
        second = _spec_result(engine, sid)
        handles = second["q"]["h"]
        assert handles
        _wait_status(engine, sid, handles, "ready")
        assert engine.stats()["worker_mode"] == "thread"
        fetched = engine.handle({"op": "fetch", "session": sid,
                                 "fetch": handles, "stats": True})
        assert fetched["st"]["hits"] == len(handles)
        assert first["q"]["h"]
    finally:
        engine.close()


@pytest.mark.skipif(not FORK_AVAILABLE, reason="fork is not available")
def test_process_worker_discards_changed_generation(
        memory: DevMemory, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OZAGIA_ENGINE_SPEC", raising=False)
    monkeypatch.delenv("OZAGIA_ENGINE_SPEC_PROCESS", raising=False)
    engine = GraphEngine(memory)
    try:
        sid = _hello(engine)["sid"]
        first = _spec_result(engine, sid)
        handles = first["q"]["h"]
        session = engine.sessions[sid]
        session.spec_generation += 1
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if all(session.spec.get(handle, {}).get("status")
                   in ("cancelled", "ready")
                   for handle in handles):
                break
            time.sleep(0.01)
        assert session.ledger["prepared"] == 0
        fetched = engine.handle({"op": "fetch", "session": sid,
                                 "fetch": handles, "stats": True})
        items = fetched.get("it") or [fetched]
        assert all(item.get("fs") == "cancelled" for item in items)
    finally:
        engine.close()


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
    engine, worker = _manual_engine(memory)
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
    speculative = engine.handle({"op": "find", "session": sid, "q": "add",
                                 "spec": True})
    _run_next(engine, worker)
    engine.handle({"op": "fetch", "session": sid,
                   "fetch": speculative["q"]["h"]})
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
    engine.close()
