#!/usr/bin/env python3
"""Offline prototype measurement for the stateful graph engine.

It builds a small throwaway fixture, then runs one representative task through
three surfaces and reports estimated tokens and round-trips:

* the full MCP surface (`dev_context`, `dev_repo_map`, ...);
* the graph surface (`find`, `node`, `expand`, `impact`);
* the engine surface (`graph` with a session, spec and fetch).

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
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from ozagia.core.memory import DevMemory, build_index  # noqa: E402
from ozagia.integrations.mcp.server import DevMemoryServer  # noqa: E402

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


def _measure(label: str, calls: list[tuple[str, dict[str, Any]]],
             server: DevMemoryServer) -> dict[str, Any]:
    rows = []
    total_chars = 0
    for name, args in calls:
        payload = _call(server, name, **args)
        chars = len(json.dumps(payload))
        total_chars += chars
        rows.append({"call": name, "arguments": args,
                     "tokens": (chars + 3) // 4})
    return {"surface": label, "round_trips": len(calls),
            "estimated_tokens": (total_chars + 3) // 4, "calls": rows}


def _measure_engine(server: DevMemoryServer) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    total_chars = 0
    handles: list[str] = []

    def call(arguments: dict[str, Any]) -> dict[str, Any]:
        nonlocal total_chars
        payload = _call(server, "graph", **arguments)
        chars = len(json.dumps(payload))
        total_chars += chars
        rows.append({"call": "graph", "op": arguments.get("op"),
                     "tokens": (chars + 3) // 4})
        return payload

    hello = call({"op": "hello"})
    sid = hello["sid"]
    find = call({"op": "find", "session": sid, "q": "add", "spec": True,
                 "budget": 700})
    handles = list(find.get("spec") or [])
    if handles:
        call({"op": "fetch", "session": sid, "fetch": handles, "budget": 700})
    call({"op": "tests", "session": sid, "key": "sym:pkg/core.py::add",
          "budget": 700})
    result = {"surface": "engine (1 tool)", "round_trips": len(rows),
              "estimated_tokens": (total_chars + 3) // 4, "calls": rows}
    result["spec"] = {"handles": handles, "ready": find.get("ready"),
                      "pending": find.get("pending")}
    return result


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
            ("node", {"key": "sym:pkg/core.py::add", "view": "outline"}),
            ("expand", {"keys": ["sym:pkg/core.py::add"],
                        "edges": ["callers"], "depth": 1}),
            ("impact", {"keys": ["sym:pkg/core.py::add"], "depth": 2}),
        ]
        engine_result = _measure_engine(engine)
        results = [
            _measure("full (dev_*)", full_calls, full),
            _measure("graph (8 verbs)", graph_calls, graph),
            engine_result,
        ]
        out = {
            "task": TASK,
            "fixture": {"source_files": 4, "test_files": 2},
            "note": ("Prototype wire measurement, not a model-task gain. "
                     "Tokens are len(JSON)/4 estimates."),
            "engine_spec": engine_result.get("spec"),
            "results": results,
        }
        print(json.dumps(out, indent=2))
        return 0
    finally:
        shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
