#!/usr/bin/env python3
"""Offline prototype measurement for the stateful graph engine.

It builds a small throwaway fixture, then runs one representative task through
three surfaces and reports estimated tokens and round-trips:

* the full MCP surface (`dev_context`, `dev_repo_map`, ...);
* the graph surface (`find`, `node`, `expand`, `impact`);
* the engine surface (`graph` with a session, spec and fetch).

A second, multi-turn run exercises the stateful path: a known outline, a
reused session and a follow-up impact, against the same stateless graph
sequence.

This is an instrumented comparison of wire size and call count.  It is not a
model-task evaluation and does not claim that fewer tokens produce a better
agent outcome.

    .venv/bin/python scripts/measure_graph_engine.py
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from ozagia.core.graph_engine import (  # noqa: E402
    OUTLINE_COLS,
    GraphEngine,
    InlineSpecWorker,
)
from ozagia.core.memory import DevMemory, build_index  # noqa: E402
from ozagia.integrations.mcp.server import DevMemoryServer  # noqa: E402

THINK_S = 0.15

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
README = """# Fixture

A tiny repository used to compare graph-tool wire sizes.

## Test

```bash
pytest -q
```
"""
TASK = ("Trace how add is used: find the symbol, inspect its outline and "
        "callers, and list the tests that cover it.")
KEY = "sym:pkg/core.py::add"


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), "-c", "user.email=t@t",
                    "-c", "user.name=t", *args], check=True,
                   capture_output=True)


def _fixture(base: Path) -> DevMemory:
    root = base / "repo"
    _write(root, "pkg/__init__.py", "")
    _write(root, "pkg/core.py", CORE)
    _write(root, "pkg/user.py", USER)
    _write(root, "tests/test_core.py", TEST_CORE)
    _write(root, "tests/test_user.py", TEST_USER)
    _write(root, "README.md", README)
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "initial")
    dump = base / "state" / "graph.json"
    build_index(root, dump, include_docs="features", include_git=False)
    return DevMemory(dump, base / "state" / "journal.jsonl", root=root)


def _call(server: DevMemoryServer, tool: str, **arguments: Any) -> dict[str, Any]:
    reply = server.handle({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": tool, "arguments": arguments}})
    if "error" in reply:
        raise RuntimeError(reply["error"])
    result = reply["result"]
    text = result["content"][0]["text"]
    if result.get("isError"):
        raise RuntimeError(text)
    return json.loads(text)


def _tokens(payload: Any) -> int:
    return (len(json.dumps(payload)) + 3) // 4


def _measure(label: str, calls: list[tuple[str, dict[str, Any]]],
             server: DevMemoryServer) -> dict[str, Any]:
    rows = []
    total = 0
    for name, args in calls:
        payload = _call(server, name, **args)
        tokens = _tokens(payload)
        total += tokens
        rows.append({"call": name, "arguments": args, "tokens": tokens})
    return {"surface": label, "round_trips": len(calls),
            "estimated_tokens": total, "calls": rows}


def _fetch_outline(payload: dict[str, Any]) -> tuple[str, str] | None:
    items = payload.get("it") or [payload]
    for item in items:
        if item.get("qo") != "outline":
            continue
        cols = item.get("c") or OUTLINE_COLS
        for row in item.get("r") or []:
            try:
                key = row[cols.index("k")]
                digest = row[cols.index("h")]
            except (ValueError, IndexError):
                continue
            if key and digest:
                return str(key), str(digest)
    return None


def _measure_engine(server: DevMemoryServer) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    total = 0
    handles: list[str] = []

    def call(arguments: dict[str, Any]) -> dict[str, Any]:
        nonlocal total
        payload = _call(server, "graph", **arguments)
        tokens = _tokens(payload)
        total += tokens
        rows.append({"call": "graph", "op": arguments.get("op"),
                     "tokens": tokens})
        return payload

    hello = call({"op": "hello"})
    sid = hello["sid"]
    started = time.perf_counter()
    find = call({"op": "find", "session": sid, "q": "add", "spec": True,
                 "budget": 700})
    find_ms = (time.perf_counter() - started) * 1000
    handles = list((find.get("q") or {}).get("h") or [])
    time.sleep(THINK_S)
    session = server.engine.sessions.get(sid)
    ready = sum(1 for handle in handles
                if (session.spec.get(handle) or {}).get("status") == "ready")
    if handles:
        call({"op": "fetch", "session": sid, "fetch": handles, "budget": 700,
              "wait_ms": 50})
    call({"op": "tests", "session": sid, "key": KEY, "budget": 700})
    result = {"surface": "engine (1 tool)", "round_trips": len(rows),
              "estimated_tokens": total, "calls": rows,
              "find_handler_ms": round(find_ms, 1)}
    result["spec"] = {"handles": handles,
                      "ready_at_response": (find.get("q") or {}).get("r"),
                      "ready_before_fetch": ready,
                      "pending": (find.get("q") or {}).get("p"),
                      "ledger": (server.engine._ledger(session)
                                 if session else {})}
    return result


def _turn(label: str, calls: list[dict[str, Any]]) -> dict[str, Any]:
    return {"turn": label, "tokens": sum(call["tokens"] for call in calls),
            "round_trips": len(calls), "calls": calls}


def _measure_engine_turns(server: DevMemoryServer) -> dict[str, Any]:
    turns: list[dict[str, Any]] = []
    total = 0

    def call(arguments: dict[str, Any]) -> dict[str, Any]:
        nonlocal total
        payload = _call(server, "graph", **arguments)
        tokens = _tokens(payload)
        total += tokens
        return {"call": "graph", "op": arguments.get("op"), "tokens": tokens,
                "payload": payload}

    first = []
    hello = call({"op": "hello"})
    first.append({k: v for k, v in hello.items() if k != "payload"})
    sid = hello["payload"]["sid"]
    started = time.perf_counter()
    find = call({"op": "find", "session": sid, "q": "add", "spec": True,
                 "budget": 700})
    find_ms = (time.perf_counter() - started) * 1000
    first.append({k: v for k, v in find.items() if k != "payload"})
    handles = list((find["payload"].get("q") or {}).get("h") or [])
    time.sleep(THINK_S)
    session = server.engine.sessions.get(sid)
    ready = sum(1 for handle in handles
                if (session.spec.get(handle) or {}).get("status") == "ready")
    fetch = None
    if handles:
        fetch = call({"op": "fetch", "session": sid, "fetch": handles,
                      "budget": 700, "wait_ms": 50})
        first.append({k: v for k, v in fetch.items() if k != "payload"})
    discovered = _fetch_outline(fetch["payload"]) if fetch else None

    second = []
    if discovered:
        known_key, digest = discovered
        same = call({"op": "outline", "session": sid, "key": known_key,
                     "known": [{"key": known_key, "hash": digest}]})
        second.append({k: v for k, v in same.items() if k != "payload"})

    third = []
    impact = call({"op": "impact", "session": sid, "keys": [KEY],
                   "depth": 2, "budget": 700})
    third.append({k: v for k, v in impact.items() if k != "payload"})

    turns.append(_turn("discover", first))
    if second:
        turns.append(_turn("known outline", second))
    turns.append(_turn("follow-up impact/tests", third))
    result = {"surface": "engine (session)", "turns": turns,
              "round_trips": sum(turn["round_trips"] for turn in turns),
              "estimated_tokens": total,
              "discovered_outline": bool(discovered),
              "find_handler_ms": round(find_ms, 1),
              "ready_at_response": (find["payload"].get("q") or {}).get("r"),
              "ready_before_fetch": ready}
    if session is not None:
        result["ledger"] = server.engine._ledger(session)
    return result


def _measure_engine_sync(memory: DevMemory) -> dict[str, Any]:
    engine = GraphEngine(memory, spec_worker=InlineSpecWorker())
    rows: list[dict[str, Any]] = []
    total = 0

    def call(arguments: dict[str, Any]) -> dict[str, Any]:
        nonlocal total
        payload = engine.handle(arguments)
        tokens = _tokens(payload)
        total += tokens
        rows.append({"call": "graph", "op": arguments.get("op"),
                     "tokens": tokens})
        return payload

    try:
        hello = call({"op": "hello"})
        sid = hello["sid"]
        started = time.perf_counter()
        find = call({"op": "find", "session": sid, "q": "add", "spec": True,
                     "budget": 700})
        find_ms = (time.perf_counter() - started) * 1000
        handles = list((find.get("q") or {}).get("h") or [])
        if handles:
            call({"op": "fetch", "session": sid, "fetch": handles,
                  "budget": 700})
        call({"op": "tests", "session": sid, "key": KEY, "budget": 700})
        session = engine.sessions.get(sid)
        return {"surface": "engine sync v2 (inline)", "round_trips": len(rows),
                "estimated_tokens": total, "calls": rows,
                "find_handler_ms": round(find_ms, 1),
                "spec": {"handles": handles,
                         "ready": (find.get("q") or {}).get("r"),
                         "pending": (find.get("q") or {}).get("p"),
                         "ledger": engine._ledger(session) if session else {}}}
    finally:
        engine.close()


def _measure_engine_sync_turns(memory: DevMemory) -> dict[str, Any]:
    engine = GraphEngine(memory, spec_worker=InlineSpecWorker())
    turns: list[dict[str, Any]] = []
    total = 0

    def call(arguments: dict[str, Any]) -> dict[str, Any]:
        nonlocal total
        payload = engine.handle(arguments)
        tokens = _tokens(payload)
        total += tokens
        return {"call": "graph", "op": arguments.get("op"), "tokens": tokens,
                "payload": payload}

    try:
        first = []
        hello = call({"op": "hello"})
        first.append({k: v for k, v in hello.items() if k != "payload"})
        sid = hello["payload"]["sid"]
        started = time.perf_counter()
        find = call({"op": "find", "session": sid, "q": "add", "spec": True,
                     "budget": 700})
        find_ms = (time.perf_counter() - started) * 1000
        first.append({k: v for k, v in find.items() if k != "payload"})
        handles = list((find["payload"].get("q") or {}).get("h") or [])
        fetch = None
        if handles:
            fetch = call({"op": "fetch", "session": sid, "fetch": handles,
                          "budget": 700})
            first.append({k: v for k, v in fetch.items() if k != "payload"})
        discovered = _fetch_outline(fetch["payload"]) if fetch else None
        second = []
        if discovered:
            known_key, digest = discovered
            same = call({"op": "outline", "session": sid, "key": known_key,
                         "known": [{"key": known_key, "hash": digest}]})
            second.append({k: v for k, v in same.items() if k != "payload"})
        third = []
        impact = call({"op": "impact", "session": sid, "keys": [KEY],
                       "depth": 2, "budget": 700})
        third.append({k: v for k, v in impact.items() if k != "payload"})
        turns.append(_turn("discover", first))
        if second:
            turns.append(_turn("known outline", second))
        turns.append(_turn("follow-up impact/tests", third))
        session = engine.sessions.get(sid)
        result = {"surface": "engine sync v2 (inline)", "turns": turns,
                  "round_trips": sum(turn["round_trips"] for turn in turns),
                  "estimated_tokens": total,
                  "discovered_outline": bool(discovered),
                  "find_handler_ms": round(find_ms, 1),
                  "ready_before_fetch": (find["payload"].get("q") or {}).get("r")}
        if session is not None:
            result["ledger"] = engine._ledger(session)
        return result
    finally:
        engine.close()


def _measure_graph_turns(server: DevMemoryServer) -> dict[str, Any]:
    first_calls = [
        ("find", {"query": "add", "limit": 10}),
        ("node", {"key": KEY, "view": "outline"}),
        ("expand", {"keys": [KEY], "edges": ["callers"], "depth": 1}),
    ]
    second_calls = [("node", {"key": KEY, "view": "outline"})]
    third_calls = [("impact", {"keys": [KEY], "depth": 2})]
    turns = []
    total = 0
    for label, calls in (("discover", first_calls),
                         ("repeat outline", second_calls),
                         ("follow-up impact/tests", third_calls)):
        rows = []
        for name, args in calls:
            payload = _call(server, name, **args)
            rows.append({"call": name, "arguments": args,
                         "tokens": _tokens(payload)})
        turn = _turn(label, rows)
        total += turn["tokens"]
        turns.append(turn)
    return {"surface": "graph (stateless)", "turns": turns,
            "round_trips": sum(turn["round_trips"] for turn in turns),
            "estimated_tokens": total}


def main() -> int:
    base = Path(tempfile.mkdtemp(prefix="graph-engine-measure-",
                                 dir="/tmp/opencode"))
    try:
        memory = _fixture(base)
        full = DevMemoryServer(memory, surface="full")
        graph = DevMemoryServer(memory, surface="graph")
        engine = DevMemoryServer(memory, surface="engine")

        full_calls = [
            ("dev_context", {"task": TASK, "budget_tokens": 6000}),
            ("dev_repo_map", {"task": TASK, "budget_tokens": 1500}),
            ("dev_symbol_callers", {"symbol": "add"}),
            ("dev_outline", {"target": "pkg/core.py"}),
            ("dev_tests", {"target": "pkg/core.py"}),
        ]
        graph_calls = [
            ("find", {"query": "add", "limit": 10}),
            ("node", {"key": KEY, "view": "outline"}),
            ("expand", {"keys": [KEY], "edges": ["callers"], "depth": 1}),
            ("impact", {"keys": [KEY], "depth": 2}),
        ]
        engine_result = _measure_engine(engine)
        sync_result = _measure_engine_sync(memory)
        results = [
            _measure("full (dev_*)", full_calls, full),
            _measure("graph (8 verbs)", graph_calls, graph),
            sync_result,
            engine_result,
        ]
        multi_turn = {
            "engine_sync": _measure_engine_sync_turns(memory),
            "engine": _measure_engine_turns(engine),
            "graph": _measure_graph_turns(graph),
        }
        out = {
            "task": TASK,
            "fixture": {"source_files": 4, "test_files": 2},
            "note": ("Prototype wire measurement, not a model-task gain. "
                     "Tokens are len(JSON)/4 estimates. The background run "
                     "sleeps 150 ms after find to simulate model thinking."),
            "engine_spec": engine_result.get("spec"),
            "results": results,
            "multi_turn": multi_turn,
        }
        print(json.dumps(out, indent=2))
        return 0
    finally:
        shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
