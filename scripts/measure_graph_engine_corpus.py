#!/usr/bin/env python3
"""Larger-repository wire/overlap measurement for the graph engine.

It builds a graph snapshot in each checkout's own ``.ozagia`` state (parse
only; no target import or execution), warms the graph surface, then runs a
representative engine task and the equivalent stateless graph-surface task.
Speculation runs in the default worker (process when fork is available) with
a simulated model think time between ``find`` and ``fetch``.

    .venv/bin/python scripts/measure_graph_engine_corpus.py
    .venv/bin/python scripts/measure_graph_engine_corpus.py --repo flask --repo httpx

This is a prototype wire/readiness measurement, not a model-task gain.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from ozagia.core.graph_engine import GraphEngine  # noqa: E402
from ozagia.core.memory import DevMemory, build_index  # noqa: E402
from ozagia.integrations.mcp.graph_surface import GraphSurface  # noqa: E402

CORPUS = Path("/home/matt/ozma/deep-review-corpus/repositories")
QUERIES = {"fd": "main", "flask": "Flask", "httpx": "Client",
           "ozagia": "GraphEngine"}
DEFAULT_REPOS = ("fd", "flask", "httpx", "ozagia")
THINK_S = 0.15
BUDGET = 1200


def _tokens(payload: Any) -> int:
    return (len(json.dumps(payload)) + 3) // 4


def _root(name: str) -> Path | None:
    if name == "ozagia":
        return ROOT
    root = CORPUS / name
    return root if root.is_dir() else None


def _call(engine: GraphEngine, args: dict[str, Any]) -> dict[str, Any]:
    return engine.handle(args)


def _surface_call(surface: GraphSurface, op: str, **args: Any
                  ) -> dict[str, Any]:
    return getattr(surface, op)(**args)


def _build(name: str, root: Path) -> dict[str, Any]:
    state = root / ".ozagia"
    state.mkdir(parents=True, exist_ok=True)
    dump = state / "graph.json"
    journal = state / "journal.jsonl"
    started = time.perf_counter()
    build_index(root, dump, include_docs="features", include_git=False)
    build_s = time.perf_counter() - started
    memory = DevMemory(dump, journal, root=root)
    return {"memory": memory, "build_s": round(build_s, 2),
            "dump_bytes": dump.stat().st_size,
            "nodes": len(memory.nodes), "edges": len(memory.edges)}


def _engine_task(memory: DevMemory, query: str) -> dict[str, Any]:
    engine = GraphEngine(memory)
    try:
        calls: list[dict[str, Any]] = []
        total = 0
        withdrawn: list[dict[str, Any]] = []

        def call(args: dict[str, Any]) -> dict[str, Any]:
            nonlocal total
            payload = _call(engine, args)
            tokens = _tokens(payload)
            total += tokens
            calls.append({"op": args.get("op"), "tokens": tokens})
            if "e" in payload:
                withdrawn.append({"op": args.get("op"), "f": payload.get("f"),
                                  "e": payload["e"]})
            return payload

        hello = call({"op": "hello"})
        sid = hello["sid"]
        started = time.perf_counter()
        find = call({"op": "find", "session": sid, "q": query,
                     "kinds": ["sym"], "spec": True, "budget": BUDGET})
        find_ms = (time.perf_counter() - started) * 1000
        handles = list((find.get("q") or {}).get("h") or [])
        ready_at_response = (find.get("q") or {}).get("r")
        time.sleep(THINK_S)
        session = engine.sessions.get(sid)
        ready_before_fetch = sum(
            1 for handle in handles
            if session and (session.spec.get(handle) or {}).get("status")
            == "ready")
        fetch_states: list[Any] = []
        if handles:
            fetched = call({"op": "fetch", "session": sid,
                            "fetch": handles, "budget": BUDGET,
                            "wait_ms": 50})
            fetch_items = fetched.get("it") or [fetched]
            fetch_states = [item.get("fs") for item in fetch_items
                            if isinstance(item, dict)]
        key = str((find.get("r") or [[None]])[0][0] or "")
        if key:
            call({"op": "expand", "session": sid, "keys": [key],
                  "edges": ["callees"], "depth": 1, "budget": BUDGET})
            call({"op": "impact", "session": sid, "keys": [key],
                  "depth": 2, "budget": BUDGET})
        late_ready = sum(
            1 for handle in handles
            if session and (session.spec.get(handle) or {}).get("status")
            == "ready")
        return {"query": query, "key": key, "freshness": hello.get("f"),
                "worker_mode": engine.stats().get("worker_mode"),
                "round_trips": len(calls), "estimated_tokens": total,
                "find_handler_ms": round(find_ms, 1),
                "ready_at_response": ready_at_response,
                "ready_before_fetch": ready_before_fetch,
                "ready_after_followup": late_ready,
                "fetch_states": fetch_states,
                "calls": calls,
                "withheld": withdrawn,
                "ledger": (engine._ledger(session) if session else {})}
    finally:
        engine.close()


def _graph_task(surface: GraphSurface, key: str,
                query: str) -> dict[str, Any]:
    calls: list[dict[str, Any]] = []
    total = 0
    sequence = [
        ("find", {"query": query, "kinds": ["sym"], "limit": 10}),
        ("node", {"key": key, "view": "outline"}),
        ("expand", {"keys": [key], "edges": ["callers"], "depth": 1}),
        ("expand", {"keys": [key], "edges": ["callees"], "depth": 1}),
        ("impact", {"keys": [key], "depth": 2}),
    ]
    for op, args in sequence:
        payload = _surface_call(surface, op, **args)
        tokens = _tokens(payload)
        total += tokens
        calls.append({"op": op, "tokens": tokens})
    return {"round_trips": len(sequence), "estimated_tokens": total,
            "calls": calls}


def measure(name: str) -> dict[str, Any]:
    root = _root(name)
    if root is None:
        return {"repo": name, "status": "missing"}
    built = _build(name, root)
    memory = built.pop("memory")
    surface = GraphSurface(memory)
    query = QUERIES.get(name, "main")
    warm_started = time.perf_counter()
    hits = surface.find(query, kinds=["sym"], limit=10).get("hits") or []
    warm_ms = (time.perf_counter() - warm_started) * 1000
    if not hits:
        return {"repo": name, "status": "no_symbol", "query": query, **built}
    key = hits[0]["key"]
    engine = _engine_task(memory, query)
    graph = _graph_task(surface, key, query)
    return {"repo": name, "status": "ok", "query": query, "key": key,
            **built, "warm_find_ms": round(warm_ms, 1),
            "engine": engine, "graph": graph}


def main(argv: list[str] | None = None) -> int:
    global THINK_S, BUDGET
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", action="append", default=[],
                        help="repo name under the corpus, or 'ozagia'")
    parser.add_argument("--think-ms", type=int, default=int(THINK_S * 1000))
    parser.add_argument("--budget", type=int, default=BUDGET)
    args = parser.parse_args(argv)
    THINK_S = max(0, args.think_ms) / 1000.0
    BUDGET = max(64, args.budget)
    names = args.repo or list(DEFAULT_REPOS)
    results = [measure(name) for name in names]
    print(json.dumps({
        "corpus": str(CORPUS),
        "think_ms": args.think_ms,
        "budget": BUDGET,
        "note": ("Prototype wire/readiness measurement. Builds snapshots under "
                 "each checkout's .ozagia state; no target import or execution."),
        "repos": results,
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
