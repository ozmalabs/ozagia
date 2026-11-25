#!/usr/bin/env python3
"""Bounded live-model evaluation of the graph engine HTTP transport.

The driver talks to an OpenAI-compatible chat endpoint and to a running
``ozagia engine serve`` loopback endpoint.  It exposes exactly one ``graph``
tool to the model, executes the model's tool calls over HTTP, and records model
usage, engine tokens, speculation counters and fixed acceptance checks.

It refuses to run without ``--live`` and ``OZAGIA_EVAL_LIVE=1``.  It never
prints the API key.  Transport, authentication and model errors abort the run
with a clear message instead of producing a synthetic result.

    OZAGIA_EVAL_LIVE=1 .venv/bin/python scripts/eval_graph_engine_live.py \
        --live --engine-url http://127.0.0.1:PORT \
        --engine-token-file /tmp/opencode/engine-live/.ozagia/engine/token \
        --arm all --repeat 2 --counterbalance \
        --out /tmp/opencode/engine-live/eval.json

The arms are ``control`` (stateless, fresh session per call),
``engine`` (the previous model-facing interface) and ``engine_opt``
(conditional ``fetch``, a smaller schema and inline ready speculation).
``--arm both`` keeps the old engine+control pair.  ``--counterbalance``
reverses arm order on every other repeat.

``--suite multiturn`` runs a separate three-turn task set instead: turn 1
outlines a symbol or file (or finds a symbol), turn 2 refreshes that same
result and answers from the copy already in the conversation, and turn 3 asks a
distinct impact question in the same session.  The arms are ``control`` (fresh
session per call), ``engine_known`` (one session plus client-sent ``known``
deltas, speculation off), ``engine_auto`` (one session, no client ``known``;
the serving engine applies its own remembered hashes) and
``engine_known_spec`` (``engine_known`` with speculation on).  The client
remembers each ``{key, hash}`` it showed the model from ``find``, ``outline``
and ``context`` rows, and injects ``known`` on the matching refresh in the
explicit arms; the model-facing schema is unchanged.  See
``docs/results/graph_engine_live_multiturn_2026-11-25.md`` and
``docs/results/graph_engine_auto_known_2026-11-26.md``.

This is a small measured sample, not a general model-task claim.  See
``docs/results/graph_engine_live_2026-11-25.md`` and
``docs/results/graph_engine_live_2026-11-25_optimized.md``.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_BASE_URL = "http://127.0.0.1:18020"
DEFAULT_KEY_FILE = "/tmp/opencode/queen/key"
DEFAULT_MODEL = "qwen3.8-27b"
DEFAULT_FIXTURE = "/tmp/opencode/engine-live"
DEFAULT_ENGINE_TOKEN_FILE = "/tmp/opencode/engine-live/.ozagia/engine/token"
# The real-repository arm needs 1,200 estimated tokens per find: the engine's
# small-budget trimmer can return no rows at 800 on a large result (recorded in
# docs/results/graph_engine_live_broader_2026-11-25.md).  The tiny fixture keeps
# its fixed 800 budget.
TINY_BUDGET = 800
REPO_BUDGET = 1200
KEY_ENV = "OZAGIA_LIVE_API_KEY"
BASE_URL_ENV = "OZAGIA_LIVE_BASE_URL"
ENGINE_URL_ENV = "OZAGIA_ENGINE_URL"
ENGINE_TOKEN_ENV = "OZAGIA_ENGINE_TOKEN"
ENGINE_TOKEN_FILE_ENV = "OZAGIA_ENGINE_TOKEN_FILE"
LIVE_ENV = "OZAGIA_EVAL_LIVE"

MAX_TOOL_CALLS = 6
MAX_TOKENS = 320
FINAL_TOKENS = 220
REQUEST_TIMEOUT_S = 90.0
ENGINE_TIMEOUT_S = 30.0
MAX_BUDGET = 800
MAX_EVAL_BUDGET = 8000
DEFAULT_BUDGET = 800
MAX_FETCH_WAIT_MS = 250
MAX_TOOL_RESULT_CHARS = 12_000
OPS = ("hello", "find", "outline", "expand", "impact", "tests", "context",
       "shape", "fetch", "bye")
ENGINE_OPS = ("find", "outline", "expand", "impact", "tests", "context",
              "shape", "fetch")
CONTROL_OPS = ("find", "outline", "expand", "impact", "tests", "shape")
OPT_OPS = ("find", "outline", "expand", "impact", "tests", "shape")


class LiveError(RuntimeError):
    """A bounded live run cannot continue honestly."""


# --------------------------------------------------------------------------- #
# Secrets and HTTP
# --------------------------------------------------------------------------- #
def _read_secret(path: str | Path, label: str) -> str:
    """Read a secret from a 0600 file without printing it."""
    file_path = Path(path).expanduser()
    try:
        info = file_path.stat()
    except OSError as exc:
        raise LiveError(f"{label} is not readable at {file_path}: {exc}") from None
    if info.st_mode & 0o077:
        raise LiveError(f"{label} at {file_path} is not mode 0600")
    try:
        value = file_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise LiveError(f"{label} cannot be read: {exc}") from None
    if not value or any(char.isspace() or ord(char) < 0x21 for char in value):
        raise LiveError(f"{label} is empty or contains whitespace/control characters")
    return value


def _endpoint_key() -> str:
    value = os.environ.get(KEY_ENV)
    if value:
        return value
    return _read_secret(DEFAULT_KEY_FILE, "model API key")


def _engine_token(args: argparse.Namespace) -> str:
    value = os.environ.get(ENGINE_TOKEN_ENV)
    if value:
        return value
    path = args.engine_token_file or os.environ.get(
        ENGINE_TOKEN_FILE_ENV, DEFAULT_ENGINE_TOKEN_FILE)
    return _read_secret(path, "engine token")


def _http_json(method: str, url: str, *, token: str | None = None,
               payload: Any | None = None, timeout: float,
               label: str) -> tuple[dict[str, Any], int]:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        code = ""
        try:
            body = json.loads(raw.decode("utf-8"))
            error = body.get("error") if isinstance(body, dict) else None
            if isinstance(error, dict):
                code = str(error.get("code") or error.get("message") or "")[:160]
            elif isinstance(error, str):
                code = error[:160]
        except (UnicodeDecodeError, json.JSONDecodeError):
            code = ""
        suffix = f" ({code})" if code else ""
        raise LiveError(f"{label} returned HTTP {exc.code}{suffix}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise LiveError(f"{label} transport error: {exc}") from None
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise LiveError(f"{label} returned non-JSON") from None
    if not isinstance(value, dict):
        raise LiveError(f"{label} returned a non-object JSON value")
    return value, len(raw)


# --------------------------------------------------------------------------- #
# Tool schemas
# --------------------------------------------------------------------------- #
def _engine_schema(include_fetch: bool = True) -> dict[str, Any]:
    ops = list(ENGINE_OPS) if include_fetch else [
        op for op in ENGINE_OPS if op != "fetch"]
    props: dict[str, Any] = {
        "op": {"type": "string", "enum": ops},
        "q": {"type": "string"},
        "key": {"type": "string"},
        "keys": {"type": "array", "items": {"type": "string"}},
        "budget": {"type": "integer"},
        "spec": {"type": "boolean"},
        "h": {"type": "string"},
    }
    if include_fetch:
        props["fetch"] = {"type": "array", "items": {"type": "string"}}
    return {
        "type": "object",
        "properties": props,
        "required": ["op"],
        "additionalProperties": False,
    }


def _opt_schema(include_fetch: bool) -> dict[str, Any]:
    """Small optimized schema: only the ops these tasks need.

    ``fetch`` is conditional on live speculative handles, so a session with no
    prepared work never advertises it.
    """
    ops = list(OPT_OPS) + (["fetch"] if include_fetch else [])
    props: dict[str, Any] = {
        "op": {"type": "string", "enum": ops},
        "q": {"type": "string"},
        "key": {"type": "string"},
        "keys": {"type": "array", "items": {"type": "string"}},
        "budget": {"type": "integer"},
    }
    if include_fetch:
        props["fetch"] = {"type": "array", "items": {"type": "string"}}
    return {
        "type": "object",
        "properties": props,
        "required": ["op"],
        "additionalProperties": False,
    }


def _control_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "op": {"type": "string", "enum": list(CONTROL_OPS)},
            "q": {"type": "string"},
            "key": {"type": "string"},
            "keys": {"type": "array", "items": {"type": "string"}},
            "budget": {"type": "integer"},
        },
        "required": ["op"],
        "additionalProperties": False,
    }


def _tool(schema: dict[str, Any], description: str) -> dict[str, Any]:
    return {"type": "function", "function": {
        "name": "graph", "description": description, "parameters": schema}}


# --------------------------------------------------------------------------- #
# Engine client
# --------------------------------------------------------------------------- #
class EngineClient:
    """Small HTTP client for one running ``ozagia engine serve``."""

    def __init__(self, base_url: str, token: str) -> None:
        self.base_url = base_url.rstrip("/")
        self._token = token

    def health(self) -> dict[str, Any]:
        value, _ = _http_json("GET", self.base_url + "/healthz",
                              timeout=10.0, label="engine health")
        return value

    def graph(self, payload: dict[str, Any]) -> tuple[dict[str, Any], int]:
        return _http_json("POST", self.base_url + "/v1/graph",
                          token=self._token, payload=payload,
                          timeout=ENGINE_TIMEOUT_S, label="engine graph")


class Recorder:
    """Per-task measurements; no graph content or secrets are retained."""

    def __init__(self) -> None:
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0,
                      "total_tokens": 0, "reasoning_tokens": 0,
                      "cached_prompt_tokens": 0}
        self.model_calls = 0
        self.tool_calls = 0
        self.driver_calls = 0
        self.bad_tool_args = 0
        self.engine_t = 0
        self.engine_t_model = 0
        self.engine_wire_bytes = 0
        self.engine_wire_bytes_model = 0
        self.tool_result_chars = 0
        self.tool_result_tokens = 0
        self.find_ready: list[int] = []
        self.find_handles: list[int] = []
        self.fetch_states: list[Any] = []
        self.fetch_blocked = 0
        self.known_sent = 0
        self.unchanged = 0
        self.stats_calls = 0
        self.stats_engine_t = 0
        self.stats_wire_bytes = 0
        self.ops: list[str] = []
        self.tool_args: list[dict[str, Any]] = []

    def add_usage(self, usage: Any) -> None:
        if not isinstance(usage, dict):
            return
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            value = usage.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                self.usage[key] += value
        details = usage.get("completion_tokens_details")
        if isinstance(details, dict):
            value = details.get("reasoning_tokens")
            if isinstance(value, int) and not isinstance(value, bool):
                self.usage["reasoning_tokens"] += value
        prompt_details = usage.get("prompt_tokens_details")
        if isinstance(prompt_details, dict):
            value = prompt_details.get("cached_tokens")
            if isinstance(value, int) and not isinstance(value, bool):
                self.usage["cached_prompt_tokens"] += value

    def add_engine(self, result: dict[str, Any], wire: int, *,
                   model_visible: bool) -> None:
        tokens = result.get("t")
        if isinstance(tokens, int) and not isinstance(tokens, bool):
            self.engine_t += tokens
            if model_visible:
                self.engine_t_model += tokens
        self.engine_wire_bytes += wire
        if model_visible:
            self.engine_wire_bytes_model += wire
        spec = result.get("q")
        if isinstance(spec, dict):
            ready = spec.get("r")
            handles = spec.get("h")
            if isinstance(ready, int) and not isinstance(ready, bool):
                self.find_ready.append(ready)
            if isinstance(handles, list):
                self.find_handles.append(len(handles))
        state = result.get("fs", result.get("fetch_state"))
        if state is not None:
            self.fetch_states.append(state)
        unchanged = result.get("u")
        if isinstance(unchanged, list):
            self.unchanged += len(unchanged)
        items = result.get("it")
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict) and item.get("fs") is not None:
                    self.fetch_states.append(item["fs"])


class EngineArm:
    """Stateful engine surface: one session, optional spec/fetch.

    ``interface="legacy"`` replays the pre-optimization model-facing schema and
    prompt.  ``interface="optimized"`` exposes only the ops these tasks use and
    advertises ``fetch`` only while the session holds speculative handles.
    """

    def __init__(self, client: EngineClient, recorder: Recorder,
                 *, interface: str = "legacy",
                 budget: int = TINY_BUDGET,
                 known_reuse: bool = False,
                 auto_known: bool = False,
                 speculate: bool = True) -> None:
        self.client = client
        self.recorder = recorder
        self.interface = interface
        self.budget = budget
        self.known_reuse = known_reuse
        # The engine_auto arm sends no `known`: the serving engine recognises
        # the repeated request from its own session and returns u handles.
        self.auto_known = auto_known
        self.speculate = speculate
        self.sid: str | None = None
        self.closed = False
        self.session_override = 0
        self.handles: set[str] = set()
        # (op, key) -> the hash of the row shape the model was shown.  A find
        # row and an outline row for the same key are different shapes, so
        # both are retained instead of one overwriting the other.
        self.seen: dict[tuple[str, str], str] = {}
        self._seq = 0

    def _hello(self) -> dict[str, Any]:
        record = self.recorder
        record.driver_calls += 1
        result, wire = self.client.graph({"op": "hello"})
        record.add_engine(result, wire, model_visible=False)
        sid = result.get("sid")
        if isinstance(sid, str) and sid:
            self.sid = sid
            self.closed = False
            self.handles.clear()
            self.seen.clear()
        return result

    def session_stats(self) -> dict[str, Any]:
        """Read the session ledger without exposing it to the model."""
        if not self.sid or self.closed:
            return {}
        record = self.recorder
        record.stats_calls += 1
        result, wire = self.client.graph(
            {"op": "hello", "session": self.sid, "stats": True})
        record.stats_engine_t += _int(result.get("t"))
        record.stats_wire_bytes += wire
        value = result.get("st")
        return value if isinstance(value, dict) else {}

    def has_handles(self) -> bool:
        return bool(self.handles)

    def _track_handles(self, op: str, result: dict[str, Any]) -> None:
        if op in ("find", "impact"):
            spec = result.get("q")
            raw = spec.get("h") if isinstance(spec, dict) else None
            self.handles = ({str(handle) for handle in raw}
                            if isinstance(raw, list) else set())
        elif op == "fetch":
            items = result.get("it")
            if not isinstance(items, list):
                items = [result]
            for item in items:
                if not isinstance(item, dict):
                    continue
                handle = item.get("h")
                if not handle:
                    continue
                if item.get("fs") == "pending":
                    self.handles.add(str(handle))
                else:
                    self.handles.discard(str(handle))

    def _track_seen(self, op: str, result: dict[str, Any]) -> None:
        """Remember every ``{op, key, hash}`` the model has already been shown.

        A response exposes a reusable hash when it carries both a ``k`` and an
        ``h`` column; ``find``, ``outline`` and ``context`` all do.  The op is
        part of the identity because the hash covers that operation's row
        shape.  Bounded (last 1,000, matching the engine's per-session cap) so
        a long suite does not grow the client's state without limit.
        """
        if op not in ("find", "outline", "context"):
            return
        cols = result.get("c")
        rows = result.get("r")
        if not isinstance(cols, list) or not isinstance(rows, list):
            return
        if "k" not in cols or "h" not in cols:
            return
        key_at = cols.index("k")
        hash_at = cols.index("h")
        for row in rows:
            if not isinstance(row, list) or len(row) <= max(key_at, hash_at):
                continue
            key, digest = row[key_at], row[hash_at]
            if isinstance(key, str) and isinstance(digest, str) and key:
                self.seen[(op, key)] = digest
        if len(self.seen) > 1000:
            for entry in list(self.seen)[:len(self.seen) - 1000]:
                self.seen.pop(entry, None)

    def _inject_known(self, op: str, payload: dict[str, Any]) -> None:
        """Send the client's remembered hashes for this op (multi-turn only).

        The model-facing schema does not expose ``known`` to the model: the
        client that already showed the rows is the one that knows their
        hashes.  Only entries for the current op are sent, each explicitly
        tagged with that op, so a find hash and an outline hash for the same
        key cannot overwrite or impersonate one another.  Every matching row
        is sent (capped) because a refresh can return the same row set under
        one request key (for example a file outline or a repeated query); the
        engine ignores hashes for rows it does not return.  This only affects
        engine wire, never the model context.
        """
        if not self.known_reuse or not self.seen or payload.get("known"):
            return
        if self.auto_known:
            return
        if op not in ("find", "outline", "context"):
            return
        pairs = [{"op": entry_op, "key": key, "hash": digest}
                 for (entry_op, key), digest in self.seen.items()
                 if entry_op == op][-64:]
        if pairs:
            payload["known"] = pairs
            self.recorder.known_sent += len(pairs)

    def call(self, args: dict[str, Any]) -> tuple[dict[str, Any], int]:
        op = str(args.get("op") or "")
        if self.interface == "optimized":
            allowed = set(OPT_OPS)
            if self.handles:
                allowed.add("fetch")
            if op not in allowed:
                if op == "fetch":
                    self.recorder.fetch_blocked += 1
                    return {"e": "no_handles",
                            "why": "fetch only q handles returned by find; "
                                   "do not retry"}, 0
                return {"e": f"op {op!r} is not available in this session"}, 0
        if op == "hello":
            self.session_override += 1
            result, wire = self.client.graph(args)
            self.recorder.add_engine(result, wire, model_visible=True)
            sid = result.get("sid")
            if isinstance(sid, str) and sid:
                self.sid = sid
                self.closed = False
                self.handles.clear()
                self.seen.clear()
            return result, wire
        if op == "bye":
            if not self.sid or self.closed:
                return {"bye": 1, "t": 1}, 0
            payload = {"op": "bye", "session": self.sid}
            result, wire = self.client.graph(payload)
            self.recorder.add_engine(result, wire, model_visible=True)
            self.closed = True
            self.handles.clear()
            return result, wire
        if not self.sid or self.closed:
            self._hello()
        if not self.sid:
            raise LiveError("engine session could not be established")
        payload = dict(args)
        if payload.get("session") and payload["session"] != self.sid:
            self.session_override += 1
        payload["session"] = self.sid
        payload["budget"] = _budget(payload.get("budget"), self.budget)
        if op == "find":
            payload.setdefault("spec", bool(self.speculate))
        if op == "fetch":
            payload.setdefault("wait_ms", 200)
        if self.interface != "optimized":
            payload.setdefault("inline", False)
        self._inject_known(op, payload)
        result, wire = self.client.graph(payload)
        self.recorder.add_engine(result, wire, model_visible=True)
        if isinstance(result.get("e"), str) and "session" in result["e"].lower():
            # Nested handling: re-hello once and retry the same request.
            self._hello()
            if not self.sid:
                raise LiveError("engine session could not be re-established")
            payload["session"] = self.sid
            result, wire = self.client.graph(payload)
            self.recorder.add_engine(result, wire, model_visible=True)
        self._track_handles(op, result)
        self._track_seen(op, result)
        return result, wire

    def finish(self) -> dict[str, Any]:
        if not self.sid or self.closed:
            return {}
        stats: dict[str, Any] = {}
        try:
            result, wire = self.client.graph(
                {"op": "hello", "session": self.sid, "stats": True})
            self.recorder.add_engine(result, wire, model_visible=False)
            value = result.get("st")
            if isinstance(value, dict):
                stats = value
        except LiveError:
            stats = {}
        try:
            self.recorder.driver_calls += 1
            bye, bye_wire = self.client.graph({"op": "bye", "session": self.sid})
            self.recorder.add_engine(bye, bye_wire, model_visible=False)
        except LiveError:
            pass
        self.closed = True
        self.handles.clear()
        return stats


class ControlArm:
    """Stateless control: a fresh session per call, no spec/known/fetch."""

    def __init__(self, client: EngineClient, recorder: Recorder, *,
                 budget: int = TINY_BUDGET) -> None:
        self.client = client
        self.recorder = recorder
        self.budget = budget

    def call(self, args: dict[str, Any]) -> tuple[dict[str, Any], int]:
        op = str(args.get("op") or "")
        if op not in CONTROL_OPS:
            return {"e": f"control arm does not expose op {op!r}"}, 0
        record = self.recorder
        record.driver_calls += 1
        hello, hello_wire = self.client.graph({"op": "hello"})
        record.add_engine(hello, hello_wire, model_visible=False)
        sid = hello.get("sid")
        if not isinstance(sid, str) or not sid:
            raise LiveError("engine hello did not return a session id")
        payload = dict(args)
        payload["session"] = sid
        payload["budget"] = _budget(payload.get("budget"), self.budget)
        try:
            result, wire = self.client.graph(payload)
            record.add_engine(result, wire, model_visible=True)
        finally:
            record.driver_calls += 1
            try:
                bye, bye_wire = self.client.graph({"op": "bye", "session": sid})
                record.add_engine(bye, bye_wire, model_visible=False)
            except LiveError:
                pass
        return result, wire

    def finish(self) -> dict[str, Any]:
        return {}


def run_spec_probe(client: EngineClient, *, query: str = "average",
                   think_s: float = 0.2,
                   budget: int = TINY_BUDGET) -> dict[str, Any]:
    """Deterministic transport probe for find-spec, wait, fetch and stats."""
    calls: list[dict[str, Any]] = []
    engine_t = 0
    wire_bytes = 0

    def call(payload: dict[str, Any]) -> dict[str, Any]:
        nonlocal engine_t, wire_bytes
        result, wire = client.graph(payload)
        engine_t += _int(result.get("t"))
        wire_bytes += wire
        calls.append({"op": payload.get("op"), "t": _int(result.get("t")),
                      "wire": wire})
        return result

    hello = call({"op": "hello"})
    sid = hello.get("sid")
    if not isinstance(sid, str) or not sid:
        raise LiveError("spec probe hello did not return a session id")
    find = call({"op": "find", "session": sid, "q": query,
                 "kinds": ["sym"], "spec": True, "budget": budget})
    spec = find.get("q") if isinstance(find.get("q"), dict) else {}
    handles = list(spec.get("h") or [])
    ready_at_response = _int(spec.get("r"))
    time.sleep(think_s)
    fetch_states: list[Any] = []
    if handles:
        fetched = call({"op": "fetch", "session": sid,
                        "fetch": handles[:4], "budget": budget,
                        "wait_ms": MAX_FETCH_WAIT_MS})
        items = fetched.get("it") or [fetched]
        for item in items:
            if isinstance(item, dict):
                fetch_states.append(item.get("fs") or "ready")
    stats_result = call({"op": "hello", "session": sid, "stats": True})
    stats = stats_result.get("st") if isinstance(stats_result.get("st"), dict) else {}
    call({"op": "bye", "session": sid})
    return {
        "query": query,
        "think_s": think_s,
        "calls": calls,
        "engine_t": engine_t,
        "engine_wire_bytes": wire_bytes,
        "handles": len(handles),
        "ready_at_response": ready_at_response,
        "fetch_states": fetch_states,
        "prepared": _int(stats.get("prepared")),
        "fetched": _int(stats.get("fetched")),
        "wasted": _int(stats.get("wasted")),
        "hits": _int(stats.get("hits")),
        "misses": _int(stats.get("misses")),
        "evicted": _int(stats.get("evicted")),
        "cancelled": _int(stats.get("cancelled")),
        "worker_ms": float(stats.get("worker_ms") or 0.0),
        "hit_rate": float(stats.get("hit_rate") or 0.0),
    }


# --------------------------------------------------------------------------- #
# Tasks
# --------------------------------------------------------------------------- #
def _int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return default


def _budget(value: Any, maximum: int = TINY_BUDGET) -> int:
    """Clamp a model-supplied budget to the small evaluation window."""
    return max(64, min(maximum, _int(value, maximum)))


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value.lower()
    return json.dumps(value, ensure_ascii=False, sort_keys=True).lower()


def _has(obj: dict[str, Any], key: str, *needles: str) -> bool:
    value = _text(obj.get(key))
    return all(needle.lower() in value for needle in needles)


def _int_is(obj: dict[str, Any], key: str, expected: int) -> bool:
    value = obj.get(key)
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return value == expected
    try:
        return int(str(value)) == expected
    except (TypeError, ValueError):
        return False


def _check(name: str, ok: bool) -> dict[str, Any]:
    return {"name": name, "ok": bool(ok)}


TINY_TASKS: list[dict[str, Any]] = [
    {
        "id": "definition_callers_tests",
        "kind": "definition/callers/tests",
        "prompt": (
            "Find the symbol named average, its direct callers, and the tests "
            "that cover it. Reply with only {\"symbol\": string, "
            "\"callers\": [string], \"tests\": [string]}."
        ),
        "keys": ["symbol", "callers", "tests"],
        "checks": [
            ("symbol is average",
             lambda obj: _has(obj, "symbol", "average")),
            ("callers include scaled_average and summarize",
             lambda obj: _has(obj, "callers", "scaled_average", "summarize")),
            ("tests include test_average",
             lambda obj: _has(obj, "tests", "test_average")),
        ],
    },
    {
        "id": "impact_expansion",
        "kind": "impact/expansion",
        "prompt": (
            "Compute the impact of the symbol add to depth 2. Reply with only "
            "{\"symbol\": string, \"symbols\": [string], \"tests\": [string], "
            "\"summary\": string}."
        ),
        "keys": ["symbol", "symbols", "tests", "summary"],
        "checks": [
            ("symbol is add",
             lambda obj: _has(obj, "symbol", "add")),
            ("symbols include double, average, scaled_average and summarize",
             lambda obj: _has(obj, "symbols", "double", "average",
                              "scaled_average", "summarize")),
            ("tests include test_add",
             lambda obj: _has(obj, "tests", "test_add")),
            ("summary is non-empty",
             lambda obj: bool(str(obj.get("summary") or "").strip())),
        ],
    },
    {
        "id": "repository_shape",
        "kind": "repository shape",
        "prompt": (
            "Describe the repository shape: name, source file count, test file "
            "count, Python module file paths (repository-relative, for example "
            "pkg/core.py), and the test command. Reply with only "
            "{\"name\": string, \"source_files\": integer, "
            "\"test_files\": integer, \"modules\": [string], "
            "\"test_command\": string}."
        ),
        "keys": ["name", "source_files", "test_files", "modules",
                 "test_command"],
        "checks": [
            ("name is engine-live",
             lambda obj: _has(obj, "name", "engine-live")),
            ("source_files is 4",
             lambda obj: _int_is(obj, "source_files", 4)),
            ("test_files is 3",
             lambda obj: _int_is(obj, "test_files", 3)),
            ("modules include the three packages",
             lambda obj: _has(obj, "modules", "pkg/core.py",
                              "pkg/metrics.py", "pkg/report.py")),
            ("test_command contains pytest",
             lambda obj: _has(obj, "test_command", "pytest")),
        ],
    },
]

# A pinned real checkout (pallets/flask at 22d92470).  These checks use the
# snapshot under that checkout's .ozagia and are not interchangeable with the
# tiny fixture's checks.
FLASK_TASKS: list[dict[str, Any]] = [
    {
        "id": "definition_callers_tests",
        "kind": "definition/callers/tests",
        "prompt": (
            "Find the symbol named get_root_path, its direct callers, and the "
            "test files that cover it. Reply with only {\"symbol\": string, "
            "\"callers\": [string], \"tests\": [string]}."
        ),
        "keys": ["symbol", "callers", "tests"],
        "checks": [
            ("symbol is get_root_path",
             lambda obj: _has(obj, "symbol", "get_root_path")),
            ("callers include Scaffold.__init__",
             lambda obj: _has(obj, "callers", "scaffold.__init__")),
            ("tests include tests/test_helpers.py",
             lambda obj: _has(obj, "tests", "tests/test_helpers.py")),
        ],
    },
    {
        "id": "impact_expansion",
        "kind": "impact/expansion",
        "prompt": (
            "Compute the impact of send_from_directory to depth 2. Reply with "
            "only {\"symbol\": string, \"symbols\": [string], \"tests\": "
            "[string], \"summary\": string}."
        ),
        "keys": ["symbol", "symbols", "tests", "summary"],
        "checks": [
            ("symbol is send_from_directory",
             lambda obj: _has(obj, "symbol", "send_from_directory")),
            ("symbols include the Flask and Blueprint callers",
             lambda obj: _has(obj, "symbols", "flask.send_static_file",
                              "blueprint.send_static_file")),
            ("tests include tests/test_helpers.py",
             lambda obj: _has(obj, "tests", "tests/test_helpers.py")),
            ("summary is non-empty",
             lambda obj: bool(str(obj.get("summary") or "").strip())),
        ],
    },
    {
        "id": "repository_shape",
        "kind": "repository shape",
        "prompt": (
            "Describe the repository shape: name, source file count, test file "
            "count, Python module file paths (repository-relative), and the "
            "test command or test convention. Reply with only "
            "{\"name\": string, \"source_files\": integer, "
            "\"test_files\": integer, \"modules\": [string], "
            "\"test_command\": string}."
        ),
        "keys": ["name", "source_files", "test_files", "modules",
                 "test_command"],
        "checks": [
            ("name is flask",
             lambda obj: _has(obj, "name", "flask")),
            ("source_files is 77",
             lambda obj: _int_is(obj, "source_files", 77)),
            ("test_files is 30",
             lambda obj: _int_is(obj, "test_files", 30)),
            ("modules include the core flask modules",
             lambda obj: _has(obj, "modules", "src/flask/app.py",
                              "src/flask/helpers.py",
                              "src/flask/blueprints.py")),
            ("test_command contains pytest",
             lambda obj: _has(obj, "test_command", "pytest")),
        ],
    },
]

TASK_SETS: dict[str, list[dict[str, Any]]] = {
    "tiny": TINY_TASKS,
    "flask": FLASK_TASKS,
}
TASKS = TINY_TASKS  # backwards-compatible alias for the pinned tiny fixture


# --------------------------------------------------------------------------- #
# Multi-turn task set
# --------------------------------------------------------------------------- #
# Three turns per session.  Turn 1 locates and outlines one symbol, turn 2
# refreshes that same outline and answers from the copy already in the
# conversation, and turn 3 asks a distinct impact question.  Prompts, required
# keys and checks are identical for every arm; only the arm's transport
# behaviour (fresh session vs. stateful session + ``known``) differs.
MULTI_FLASK_TASKS: list[dict[str, Any]] = [
    {
        "id": "multiturn_get_root_path",
        "kind": "multi-turn definition/refresh/impact",
        "turns": [
            {
                "id": "t1_find_outline",
                "prompt": (
                    "Two graph calls, in order: (1) find with q=get_root_path "
                    "and kinds=[\"sym\"]; (2) outline the top symbol key from "
                    "that result. Then reply with only {\"symbol\": string, "
                    "\"file\": string, \"signature\": string, "
                    "\"summary\": string}."
                ),
                "keys": ["symbol", "file", "signature", "summary"],
                "checks": [
                    ("symbol is get_root_path",
                     lambda obj: _has(obj, "symbol", "get_root_path")),
                    ("file is src/flask/helpers.py",
                     lambda obj: _has(obj, "file", "src/flask/helpers.py")),
                    ("signature contains the definition line",
                     lambda obj: _has(
                         obj, "signature",
                         "def get_root_path(import_name: str) -> str:")),
                    ("summary mentions the root path",
                     lambda obj: _has(obj, "summary", "root path")),
                ],
            },
            {
                "id": "t2_refresh_outline",
                "prompt": (
                    "Refresh the outline of get_root_path with exactly one "
                    "graph call (op outline, same key as before), then answer "
                    "only from the outline already in this conversation. Do "
                    "not fetch or read source. Reply with only {\"symbol\": "
                    "string, \"file\": string, \"start_line\": integer, "
                    "\"end_line\": integer}."
                ),
                "keys": ["symbol", "file", "start_line", "end_line"],
                "checks": [
                    ("symbol is get_root_path",
                     lambda obj: _has(obj, "symbol", "get_root_path")),
                    ("file is src/flask/helpers.py",
                     lambda obj: _has(obj, "file", "src/flask/helpers.py")),
                    ("start_line is 577",
                     lambda obj: _int_is(obj, "start_line", 577)),
                    ("end_line is 631",
                     lambda obj: _int_is(obj, "end_line", 631)),
                ],
            },
            {
                "id": "t3_impact",
                "prompt": (
                    "In the same session, compute the impact of get_root_path "
                    "to depth 2 using the keys argument (a list) with the "
                    "symbol key from the outline. Reply with only "
                    "{\"symbol\": string, \"callers\": [string], "
                    "\"test_files\": [string], \"summary\": string}."
                ),
                "keys": ["symbol", "callers", "test_files", "summary"],
                "checks": [
                    ("symbol is get_root_path",
                     lambda obj: _has(obj, "symbol", "get_root_path")),
                    ("callers include Scaffold.__init__",
                     lambda obj: _has(obj, "callers", "scaffold.__init__")),
                    ("test_files include tests/test_helpers.py",
                     lambda obj: _has(obj, "test_files",
                                      "tests/test_helpers.py")),
                    ("summary is non-empty",
                     lambda obj: bool(str(obj.get("summary") or "").strip())),
                ],
            },
        ],
    },
    {
        "id": "multiturn_send_from_directory",
        "kind": "multi-turn definition/refresh/impact",
        "turns": [
            {
                "id": "t1_find_outline",
                "prompt": (
                    "Two graph calls, in order: (1) find with "
                    "q=send_from_directory and kinds=[\"sym\"]; (2) outline "
                    "the top symbol key from that result. Then reply with "
                    "only {\"symbol\": string, \"file\": string, "
                    "\"signature\": string, \"summary\": string}."
                ),
                "keys": ["symbol", "file", "signature", "summary"],
                "checks": [
                    ("symbol is send_from_directory",
                     lambda obj: _has(obj, "symbol", "send_from_directory")),
                    ("file is src/flask/helpers.py",
                     lambda obj: _has(obj, "file", "src/flask/helpers.py")),
                    ("signature contains the definition",
                     lambda obj: _has(obj, "signature",
                                      "def send_from_directory(")),
                    ("summary mentions sending a file",
                     lambda obj: _has(obj, "summary", "send a file")),
                ],
            },
            {
                "id": "t2_refresh_outline",
                "prompt": (
                    "Refresh the outline of send_from_directory with exactly "
                    "one graph call (op outline, same key as before), then "
                    "answer only from the outline already in this "
                    "conversation. Do not fetch or read source. Reply with "
                    "only {\"symbol\": string, \"file\": string, "
                    "\"start_line\": integer, \"end_line\": integer}."
                ),
                "keys": ["symbol", "file", "start_line", "end_line"],
                "checks": [
                    ("symbol is send_from_directory",
                     lambda obj: _has(obj, "symbol", "send_from_directory")),
                    ("file is src/flask/helpers.py",
                     lambda obj: _has(obj, "file", "src/flask/helpers.py")),
                    ("start_line is 533",
                     lambda obj: _int_is(obj, "start_line", 533)),
                    ("end_line is 574",
                     lambda obj: _int_is(obj, "end_line", 574)),
                ],
            },
            {
                "id": "t3_impact",
                "prompt": (
                    "In the same session, compute the impact of "
                    "send_from_directory to depth 2 using the keys argument "
                    "(a list) with the symbol key from the outline. Reply "
                    "with only {\"symbol\": string, \"callers\": [string], "
                    "\"test_files\": [string], \"summary\": string}."
                ),
                "keys": ["symbol", "callers", "test_files", "summary"],
                "checks": [
                    ("symbol is send_from_directory",
                     lambda obj: _has(obj, "symbol", "send_from_directory")),
                    ("callers include Flask.send_static_file",
                     lambda obj: _has(obj, "callers",
                                      "flask.send_static_file")),
                    ("callers include Blueprint.send_static_file",
                     lambda obj: _has(obj, "callers",
                                      "blueprint.send_static_file")),
                    ("test_files include tests/test_helpers.py",
                     lambda obj: _has(obj, "test_files",
                                      "tests/test_helpers.py")),
                    ("summary is non-empty",
                     lambda obj: bool(str(obj.get("summary") or "").strip())),
                ],
            },
        ],
    },
    {
        "id": "multiturn_helpers_file",
        "kind": "multi-turn file outline/refresh/impact",
        "turns": [
            {
                "id": "t1_outline_file",
                "prompt": (
                    "Outline the file src/flask/helpers.py using the file key "
                    "file:src/flask/helpers.py. Reply with only {\"file\": "
                    "string, \"has_get_root_path\": boolean, "
                    "\"has_send_from_directory\": boolean}."
                ),
                "keys": ["file", "has_get_root_path",
                         "has_send_from_directory"],
                "checks": [
                    ("file is src/flask/helpers.py",
                     lambda obj: _has(obj, "file", "src/flask/helpers.py")),
                    ("outline contains get_root_path",
                     lambda obj: obj.get("has_get_root_path") is True),
                    ("outline contains send_from_directory",
                     lambda obj: obj.get("has_send_from_directory") is True),
                ],
            },
            {
                "id": "t2_refresh_file_outline",
                "prompt": (
                    "Refresh that same file outline with exactly one graph "
                    "call (op outline, key file:src/flask/helpers.py), then "
                    "answer only from the outline already in this "
                    "conversation. Do not fetch or read source. Reply with "
                    "only {\"file\": string, \"get_root_path_line\": integer, "
                    "\"send_from_directory_line\": integer}."
                ),
                "keys": ["file", "get_root_path_line",
                         "send_from_directory_line"],
                "checks": [
                    ("file is src/flask/helpers.py",
                     lambda obj: _has(obj, "file", "src/flask/helpers.py")),
                    ("get_root_path starts at 577",
                     lambda obj: _int_is(obj, "get_root_path_line", 577)),
                    ("send_from_directory starts at 533",
                     lambda obj: _int_is(
                         obj, "send_from_directory_line", 533)),
                ],
            },
            {
                "id": "t3_impact",
                "prompt": (
                    "In the same session, compute the impact of "
                    "send_from_directory to depth 2 using the keys argument "
                    "(a list) with key "
                    "sym:src/flask/helpers.py::send_from_directory. Reply "
                    "with only {\"symbol\": string, \"callers\": [string], "
                    "\"test_files\": [string], \"summary\": string}."
                ),
                "keys": ["symbol", "callers", "test_files", "summary"],
                "checks": [
                    ("symbol is send_from_directory",
                     lambda obj: _has(obj, "symbol", "send_from_directory")),
                    ("callers include Flask.send_static_file",
                     lambda obj: _has(obj, "callers",
                                      "flask.send_static_file")),
                    ("callers include Blueprint.send_static_file",
                     lambda obj: _has(obj, "callers",
                                      "blueprint.send_static_file")),
                    ("test_files include tests/test_helpers.py",
                     lambda obj: _has(obj, "test_files",
                                      "tests/test_helpers.py")),
                    ("summary is non-empty",
                     lambda obj: bool(str(obj.get("summary") or "").strip())),
                ],
            },
        ],
    },
    {
        "id": "multiturn_find_refresh",
        "kind": "multi-turn find/refresh/impact",
        "turns": [
            {
                "id": "t1_find",
                "prompt": (
                    "One graph call: find with q=get_root_path and "
                    "kinds=[\"sym\"]. Then, from that result row, reply with "
                    "only {\"symbol\": string, \"file\": string, "
                    "\"signature\": string}."
                ),
                "keys": ["symbol", "file", "signature"],
                "checks": [
                    ("symbol is get_root_path",
                     lambda obj: _has(obj, "symbol", "get_root_path")),
                    ("file is src/flask/helpers.py",
                     lambda obj: _has(obj, "file", "src/flask/helpers.py")),
                    ("signature contains the definition line",
                     lambda obj: _has(
                         obj, "signature",
                         "def get_root_path(import_name: str) -> str:")),
                ],
            },
            {
                "id": "t2_refresh_find",
                "prompt": (
                    "Repeat exactly the same find with one graph call (op "
                    "find, same q and kinds), then answer only from the find "
                    "result already in this conversation. Do not outline, "
                    "fetch or read source. Reply with only {\"symbol\": "
                    "string, \"file\": string, \"signature\": string}."
                ),
                "keys": ["symbol", "file", "signature"],
                "checks": [
                    ("symbol is get_root_path",
                     lambda obj: _has(obj, "symbol", "get_root_path")),
                    ("file is src/flask/helpers.py",
                     lambda obj: _has(obj, "file", "src/flask/helpers.py")),
                    ("signature contains the definition line",
                     lambda obj: _has(
                         obj, "signature",
                         "def get_root_path(import_name: str) -> str:")),
                ],
            },
            {
                "id": "t3_impact",
                "prompt": (
                    "In the same session, compute the impact of get_root_path "
                    "to depth 2 using the keys argument (a list) with the "
                    "symbol key from the find row. Reply with only "
                    "{\"symbol\": string, \"callers\": [string], "
                    "\"test_files\": [string], \"summary\": string}."
                ),
                "keys": ["symbol", "callers", "test_files", "summary"],
                "checks": [
                    ("symbol is get_root_path",
                     lambda obj: _has(obj, "symbol", "get_root_path")),
                    ("callers include Scaffold.__init__",
                     lambda obj: _has(obj, "callers", "scaffold.__init__")),
                    ("test_files include tests/test_helpers.py",
                     lambda obj: _has(obj, "test_files",
                                      "tests/test_helpers.py")),
                    ("summary is non-empty",
                     lambda obj: bool(str(obj.get("summary") or "").strip())),
                ],
            },
        ],
    },
]

MULTI_TINY_TASKS: list[dict[str, Any]] = [
    {
        "id": "multiturn_average",
        "kind": "multi-turn definition/refresh/impact",
        "turns": [
            {
                "id": "t1_find_outline",
                "prompt": (
                    "Two graph calls, in order: (1) find with q=average and "
                    "kinds=[\"sym\"]; (2) outline the top symbol key from that "
                    "result. Then reply with only {\"symbol\": string, "
                    "\"file\": string, \"signature\": string, "
                    "\"summary\": string}."
                ),
                "keys": ["symbol", "file", "signature", "summary"],
                "checks": [
                    ("symbol is average",
                     lambda obj: _has(obj, "symbol", "average")),
                    ("file is pkg/metrics.py",
                     lambda obj: _has(obj, "file", "pkg/metrics.py")),
                    ("signature contains def average(values)",
                     lambda obj: _has(obj, "signature", "def average(values)")),
                    ("summary mentions average",
                     lambda obj: _has(obj, "summary", "average")),
                ],
            },
            {
                "id": "t2_refresh_outline",
                "prompt": (
                    "Refresh the outline of average with exactly one graph "
                    "call (op outline, same key as before), then answer only "
                    "from the outline already in this conversation. Do not "
                    "fetch or read source. Reply with only {\"symbol\": "
                    "string, \"file\": string, \"start_line\": integer, "
                    "\"end_line\": integer}."
                ),
                "keys": ["symbol", "file", "start_line", "end_line"],
                "checks": [
                    ("symbol is average",
                     lambda obj: _has(obj, "symbol", "average")),
                    ("file is pkg/metrics.py",
                     lambda obj: _has(obj, "file", "pkg/metrics.py")),
                    ("start_line is 6",
                     lambda obj: _int_is(obj, "start_line", 6)),
                    ("end_line is 13",
                     lambda obj: _int_is(obj, "end_line", 13)),
                ],
            },
            {
                "id": "t3_impact",
                "prompt": (
                    "In the same session, compute the impact of average to "
                    "depth 2 using the keys argument (a list) with the symbol "
                    "key from the outline. Reply with only {\"symbol\": "
                    "string, \"callers\": [string], \"test_files\": [string], "
                    "\"summary\": string}."
                ),
                "keys": ["symbol", "callers", "test_files", "summary"],
                "checks": [
                    ("symbol is average",
                     lambda obj: _has(obj, "symbol", "average")),
                    ("callers include scaled_average and summarize",
                     lambda obj: _has(obj, "callers", "scaled_average",
                                      "summarize")),
                    ("test_files include tests/test_metrics.py",
                     lambda obj: _has(obj, "test_files",
                                      "tests/test_metrics.py")),
                    ("summary is non-empty",
                     lambda obj: bool(str(obj.get("summary") or "").strip())),
                ],
            },
        ],
    },
    {
        "id": "multiturn_find_refresh",
        "kind": "multi-turn find/refresh/impact",
        "turns": [
            {
                "id": "t1_find",
                "prompt": (
                    "One graph call: find with q=average and kinds=[\"sym\"]. "
                    "Then, from that result row, reply with only "
                    "{\"symbol\": string, \"file\": string, "
                    "\"signature\": string}."
                ),
                "keys": ["symbol", "file", "signature"],
                "checks": [
                    ("symbol is average",
                     lambda obj: _has(obj, "symbol", "average")),
                    ("file is pkg/metrics.py",
                     lambda obj: _has(obj, "file", "pkg/metrics.py")),
                    ("signature contains def average(values)",
                     lambda obj: _has(obj, "signature",
                                      "def average(values)")),
                ],
            },
            {
                "id": "t2_refresh_find",
                "prompt": (
                    "Repeat exactly the same find with one graph call (op "
                    "find, same q and kinds), then answer only from the find "
                    "result already in this conversation. Do not outline, "
                    "fetch or read source. Reply with only {\"symbol\": "
                    "string, \"file\": string, \"signature\": string}."
                ),
                "keys": ["symbol", "file", "signature"],
                "checks": [
                    ("symbol is average",
                     lambda obj: _has(obj, "symbol", "average")),
                    ("file is pkg/metrics.py",
                     lambda obj: _has(obj, "file", "pkg/metrics.py")),
                    ("signature contains def average(values)",
                     lambda obj: _has(obj, "signature",
                                      "def average(values)")),
                ],
            },
            {
                "id": "t3_impact",
                "prompt": (
                    "In the same session, compute the impact of average to "
                    "depth 2 using the keys argument (a list) with the symbol "
                    "key from the find row. Reply with only {\"symbol\": "
                    "string, \"callers\": [string], \"test_files\": [string], "
                    "\"summary\": string}."
                ),
                "keys": ["symbol", "callers", "test_files", "summary"],
                "checks": [
                    ("symbol is average",
                     lambda obj: _has(obj, "symbol", "average")),
                    ("callers include scaled_average and summarize",
                     lambda obj: _has(obj, "callers", "scaled_average",
                                      "summarize")),
                    ("test_files include tests/test_metrics.py",
                     lambda obj: _has(obj, "test_files",
                                      "tests/test_metrics.py")),
                    ("summary is non-empty",
                     lambda obj: bool(str(obj.get("summary") or "").strip())),
                ],
            },
        ],
    },
]

MULTI_TASKS: dict[str, list[dict[str, Any]]] = {
    "tiny": MULTI_TINY_TASKS,
    "flask": MULTI_FLASK_TASKS,
}
MULTI_ARMS = ("control", "engine_known", "engine_auto", "engine_known_spec")


def _engine_system(budget: int = TINY_BUDGET) -> str:
    return (
        "You have exactly one tool, graph: a read-only code graph. Ops: find, "
        "outline, expand, impact, tests, context, shape, fetch. The transport "
        "creates and closes the session for you; do not call hello or bye. Use "
        f"budgets of at most {budget}. find may return speculative q handles; "
        "fetch them with wait_ms 200 when useful. After at most six tool calls, "
        "reply with only the JSON object the user asks for."
    )


def _short_system(budget: int = TINY_BUDGET) -> str:
    return (
        "You have one tool, graph: a read-only code graph. Use at most "
        f"{budget}-token budgets. If a repeated result lists only unchanged "
        "keys under u, reuse the rows already in the conversation. After at "
        "most six calls, reply with only the requested JSON object."
    )


ENGINE_SYSTEM = _engine_system(TINY_BUDGET)
SHORT_SYSTEM = _short_system(TINY_BUDGET)
CONTROL_SYSTEM = SHORT_SYSTEM


# --------------------------------------------------------------------------- #
# Model loop
# --------------------------------------------------------------------------- #
def _chat(base_url: str, key: str, model: str, messages: list[dict[str, Any]],
          tools: list[dict[str, Any]] | None, max_tokens: int,
          tool_choice: str | None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": 0,
        "top_p": 1,
        "max_tokens": max_tokens,
        "seed": 0,
        "stream": False,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = tool_choice or "auto"
    value, _ = _http_json("POST", base_url.rstrip("/") + "/v1/chat/completions",
                          token=key, payload=payload,
                          timeout=REQUEST_TIMEOUT_S, label="model")
    return value


def _message(value: dict[str, Any]) -> dict[str, Any]:
    choices = value.get("choices")
    if not isinstance(choices, list) or not choices:
        raise LiveError("model response has no choices")
    message = choices[0].get("message")
    if not isinstance(message, dict):
        raise LiveError("model response has no message")
    return message


def _extract_json(text: str) -> tuple[dict[str, Any] | None, str | None]:
    raw = text.strip()
    if raw.startswith("```"):
        lines = raw.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        raw = "\n".join(lines).strip()
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end <= start:
            return None, f"no JSON object ({exc.msg})"
        try:
            value = json.loads(raw[start:end + 1])
        except json.JSONDecodeError as inner:
            return None, f"invalid JSON ({inner.msg})"
    if not isinstance(value, dict):
        return None, "JSON value is not an object"
    return value, None


def _execute_tool(arm: EngineArm | ControlArm, name: str,
                  raw_arguments: Any) -> tuple[dict[str, Any], int]:
    if name != "graph":
        return {"e": f"unknown tool {name!r}; only graph is available"}, 0
    if not isinstance(raw_arguments, str) or not raw_arguments.strip():
        return {"e": "tool arguments must be a non-empty JSON object"}, 0
    try:
        args = json.loads(raw_arguments)
    except json.JSONDecodeError:
        return {"e": "tool arguments are not valid JSON"}, 0
    if not isinstance(args, dict):
        return {"e": "tool arguments must be a JSON object"}, 0
    result, wire = arm.call(args)
    return result, wire


def run_task(arm: EngineArm | ControlArm, arm_name: str, task: dict[str, Any],
             model: dict[str, Any]) -> dict[str, Any]:
    recorder = arm.recorder
    if arm_name == "engine":
        system = _engine_system(arm.budget)
    else:
        system = _short_system(arm.budget)

    def tools_now() -> list[dict[str, Any]]:
        if arm_name == "control":
            return [_tool(_control_schema(), "Read-only code graph.")]
        if arm_name == "engine":
            return [_tool(_engine_schema(), "Stateful read-only graph session.")]
        include_fetch = isinstance(arm, EngineArm) and arm.has_handles()
        return [_tool(_opt_schema(include_fetch), "Read-only code graph.")]

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system},
        {"role": "user", "content": task["prompt"]},
    ]
    started = time.perf_counter()
    final_text = ""
    finalized = False
    rounds = 0
    max_rounds = model["max_tool_calls"] + 2
    while rounds < max_rounds:
        rounds += 1
        tools = tools_now()
        response = _chat(model["base_url"], model["key"], model["model"],
                         messages, tools, model["max_tokens"], "auto")
        recorder.model_calls += 1
        recorder.add_usage(response.get("usage"))
        message = _message(response)
        tool_calls = message.get("tool_calls")
        if isinstance(tool_calls, list) and tool_calls:
            if recorder.tool_calls + len(tool_calls) > model["max_tool_calls"]:
                finalized = True
                break
            messages.append({
                "role": "assistant",
                "content": message.get("content") or "",
                "tool_calls": tool_calls,
            })
            for index, call in enumerate(tool_calls):
                recorder.tool_calls += 1
                function = call.get("function") if isinstance(call, dict) else None
                name = str((function or {}).get("name") or "")
                arguments = (function or {}).get("arguments")
                try:
                    result, _ = _execute_tool(arm, name, arguments)
                except LiveError:
                    raise
                try:
                    parsed_args = json.loads(arguments) if arguments else {}
                except json.JSONDecodeError:
                    parsed_args = {}
                if isinstance(parsed_args, dict):
                    op = str(parsed_args.get("op") or "")
                    spec_value = parsed_args.get("spec")
                    if (arm_name in ("engine", "engine_opt") and op == "find"
                            and spec_value is None):
                        spec_value = True
                    recorder.tool_args.append({
                        "op": op,
                        "budget": _budget(parsed_args.get("budget"),
                                          arm.budget),
                        "spec": spec_value,
                        "keys": len(parsed_args.get("keys") or []),
                        "fetch": len(parsed_args.get("fetch") or []),
                    })
                content = json.dumps(result, ensure_ascii=False,
                                     separators=(",", ":"))
                if result.get("e") and (
                        "not valid JSON" in str(result["e"])
                        or "JSON object" in str(result["e"])):
                    recorder.bad_tool_args += 1
                if len(content) > MAX_TOOL_RESULT_CHARS:
                    content = content[:MAX_TOOL_RESULT_CHARS]
                recorder.tool_result_chars += len(content)
                recorder.tool_result_tokens += (len(content) + 3) // 4
                call_id = str(call.get("id") or f"call_{rounds}_{index}")
                messages.append({
                    "role": "tool",
                    "tool_call_id": call_id,
                    "name": "graph",
                    "content": content,
                })
            continue
        final_text = str(message.get("content") or "")
        break

    if not final_text:
        finalized = True
        messages.append({
            "role": "user",
            "content": ("Tool budget is exhausted. Reply now with only the "
                        "requested JSON object and no prose."),
        })
        response = _chat(model["base_url"], model["key"], model["model"],
                         messages, None, model["final_tokens"], None)
        recorder.model_calls += 1
        recorder.add_usage(response.get("usage"))
        final_text = str(_message(response).get("content") or "")

    wall_s = round(time.perf_counter() - started, 3)
    spec = arm.finish()
    parsed, json_error = _extract_json(final_text)
    missing = [key for key in task["keys"]
               if not isinstance(parsed, dict) or key not in parsed]
    checks: list[dict[str, Any]] = []
    if parsed is not None:
        for name, check in task["checks"]:
            checks.append(_check(name, bool(check(parsed))))
    accepted = bool(parsed is not None and not missing
                    and checks and all(row["ok"] for row in checks))
    return {
        "task": task["id"],
        "kind": task["kind"],
        "arm": arm_name,
        "prompt": task["prompt"],
        "expected": [name for name, _ in task["checks"]],
        "final_json": parsed,
        "json_valid": parsed is not None,
        "json_error": json_error,
        "missing_keys": missing,
        "checks": checks,
        "accepted": accepted,
        "accepted_count": sum(1 for row in checks if row["ok"]),
        "check_count": len(checks),
        "final_text_chars": len(final_text),
        "finalized_after_tool_budget": finalized,
        "usage": recorder.usage,
        "model_calls": recorder.model_calls,
        "tool_calls": recorder.tool_calls,
        "driver_calls": recorder.driver_calls,
        "bad_tool_args": recorder.bad_tool_args,
        "fetch_blocked": recorder.fetch_blocked,
        "tool_args": recorder.tool_args,
        "wall_s": wall_s,
        "engine_t": recorder.engine_t,
        "engine_t_model": recorder.engine_t_model,
        "engine_wire_bytes": recorder.engine_wire_bytes,
        "engine_wire_bytes_model": recorder.engine_wire_bytes_model,
        "tool_result_chars": recorder.tool_result_chars,
        "tool_result_tokens": recorder.tool_result_tokens,
        "find_spec_ready_at_response": recorder.find_ready,
        "find_spec_handles": recorder.find_handles,
        "fetch_states": recorder.fetch_states,
        "spec": {
            "prepared": _int(spec.get("prepared")),
            "fetched": _int(spec.get("fetched")),
            "wasted": _int(spec.get("wasted")),
            "hits": _int(spec.get("hits")),
            "misses": _int(spec.get("misses")),
            "evicted": _int(spec.get("evicted")),
            "cancelled": _int(spec.get("cancelled")),
            "worker_ms": float(spec.get("worker_ms") or 0.0),
            "hit_rate": float(spec.get("hit_rate") or 0.0),
        },
    }


# --------------------------------------------------------------------------- #
# Multi-turn runner
# --------------------------------------------------------------------------- #
_SNAP_FIELDS = (
    "model_calls", "tool_calls", "driver_calls", "bad_tool_args",
    "fetch_blocked", "engine_t", "engine_t_model", "engine_wire_bytes",
    "engine_wire_bytes_model", "tool_result_chars", "tool_result_tokens",
    "known_sent", "unchanged", "stats_calls", "stats_engine_t",
    "stats_wire_bytes",
)
_SPEC_FIELDS = ("prepared", "fetched", "wasted", "hits", "misses",
                "evicted", "cancelled")


def _snapshot(recorder: Recorder) -> dict[str, Any]:
    snapshot: dict[str, Any] = {
        field: getattr(recorder, field) for field in _SNAP_FIELDS}
    snapshot["usage"] = dict(recorder.usage)
    snapshot["tool_args"] = len(recorder.tool_args)
    return snapshot


def _snapshot_delta(before: dict[str, Any],
                    after: dict[str, Any]) -> dict[str, Any]:
    delta: dict[str, Any] = {field: after[field] - before[field]
                             for field in _SNAP_FIELDS}
    delta["usage"] = {key: after["usage"][key] - before["usage"][key]
                      for key in after["usage"]}
    delta["tool_args"] = after["tool_args"] - before["tool_args"]
    return delta


def _stats_delta(before: dict[str, Any],
                 after: dict[str, Any]) -> dict[str, Any]:
    delta = {field: _int(after.get(field)) - _int(before.get(field))
             for field in _SPEC_FIELDS}
    delta["worker_ms"] = round(
        float(after.get("worker_ms") or 0.0)
        - float(before.get("worker_ms") or 0.0), 1)
    return delta


def _run_turn(arm: EngineArm | ControlArm, arm_name: str,
              turn: dict[str, Any], model: dict[str, Any],
              messages: list[dict[str, Any]]) -> tuple[str, bool]:
    """One tool loop for one turn, appending to the shared conversation."""
    recorder = arm.recorder

    def tools_now() -> list[dict[str, Any]]:
        if arm_name == "control":
            return [_tool(_control_schema(), "Read-only code graph.")]
        include_fetch = isinstance(arm, EngineArm) and arm.has_handles()
        return [_tool(_opt_schema(include_fetch), "Read-only code graph.")]

    messages.append({"role": "user", "content": turn["prompt"]})
    final_text = ""
    finalized = False
    rounds = 0
    max_rounds = model["max_tool_calls"] + 2
    while rounds < max_rounds:
        rounds += 1
        tools = tools_now()
        response = _chat(model["base_url"], model["key"], model["model"],
                         messages, tools, model["max_tokens"], "auto")
        recorder.model_calls += 1
        recorder.add_usage(response.get("usage"))
        message = _message(response)
        tool_calls = message.get("tool_calls")
        if isinstance(tool_calls, list) and tool_calls:
            if recorder.tool_calls + len(tool_calls) > model["max_tool_calls"]:
                finalized = True
                break
            messages.append({
                "role": "assistant",
                "content": message.get("content") or "",
                "tool_calls": tool_calls,
            })
            for index, call in enumerate(tool_calls):
                recorder.tool_calls += 1
                function = call.get("function") if isinstance(call, dict) else None
                name = str((function or {}).get("name") or "")
                arguments = (function or {}).get("arguments")
                result, _ = _execute_tool(arm, name, arguments)
                try:
                    parsed_args = json.loads(arguments) if arguments else {}
                except json.JSONDecodeError:
                    parsed_args = {}
                if isinstance(parsed_args, dict):
                    op = str(parsed_args.get("op") or "")
                    spec_value = parsed_args.get("spec")
                    if (isinstance(arm, EngineArm) and op == "find"
                            and spec_value is None):
                        spec_value = arm.speculate
                    recorder.tool_args.append({
                        "op": op,
                        "budget": _budget(parsed_args.get("budget"),
                                          arm.budget),
                        "spec": spec_value,
                        "keys": len(parsed_args.get("keys") or []),
                        "fetch": len(parsed_args.get("fetch") or []),
                    })
                content = json.dumps(result, ensure_ascii=False,
                                     separators=(",", ":"))
                if result.get("e") and (
                        "not valid JSON" in str(result["e"])
                        or "JSON object" in str(result["e"])):
                    recorder.bad_tool_args += 1
                if len(content) > MAX_TOOL_RESULT_CHARS:
                    content = content[:MAX_TOOL_RESULT_CHARS]
                recorder.tool_result_chars += len(content)
                recorder.tool_result_tokens += (len(content) + 3) // 4
                call_id = str(call.get("id") or f"call_{rounds}_{index}")
                messages.append({
                    "role": "tool",
                    "tool_call_id": call_id,
                    "name": "graph",
                    "content": content,
                })
            continue
        final_text = str(message.get("content") or "")
        break

    if not final_text:
        finalized = True
        messages.append({
            "role": "user",
            "content": ("Tool budget is exhausted. Reply now with only the "
                        "requested JSON object and no prose."),
        })
        response = _chat(model["base_url"], model["key"], model["model"],
                         messages, None, model["final_tokens"], None)
        recorder.model_calls += 1
        recorder.add_usage(response.get("usage"))
        final_text = str(_message(response).get("content") or "")
    return final_text, finalized


def run_multiturn(arm: EngineArm | ControlArm, arm_name: str,
                  task: dict[str, Any], model: dict[str, Any]) -> dict[str, Any]:
    """Run one three-turn session and return per-turn and cumulative rows."""
    recorder = arm.recorder
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": _short_system(arm.budget)}]
    session_stats: dict[str, Any] = {}
    if isinstance(arm, EngineArm):
        session_stats = arm.session_stats()
    turn_rows: list[dict[str, Any]] = []
    for index, turn in enumerate(task["turns"], 1):
        before = _snapshot(recorder)
        started = time.perf_counter()
        final_text, finalized = _run_turn(arm, arm_name, turn, model, messages)
        wall_s = round(time.perf_counter() - started, 3)
        after = _snapshot(recorder)
        delta = _snapshot_delta(before, after)
        next_stats: dict[str, Any] = {}
        if isinstance(arm, EngineArm):
            next_stats = arm.session_stats()
        spec_turn = _stats_delta(session_stats, next_stats)
        session_stats = next_stats or session_stats
        parsed, json_error = _extract_json(final_text)
        missing = [key for key in turn["keys"]
                   if not isinstance(parsed, dict) or key not in parsed]
        checks: list[dict[str, Any]] = []
        if parsed is not None:
            for name, check in turn["checks"]:
                checks.append(_check(name, bool(check(parsed))))
        accepted = bool(parsed is not None and not missing
                        and checks and all(row["ok"] for row in checks))
        ops = [arg.get("op")
               for arg in recorder.tool_args[len(recorder.tool_args)
                                            - delta["tool_args"]:]]
        turn_rows.append({
            "turn": index,
            "id": turn["id"],
            "prompt": turn["prompt"],
            "final_json": parsed,
            "json_valid": parsed is not None,
            "json_error": json_error,
            "missing_keys": missing,
            "checks": checks,
            "accepted": accepted,
            "accepted_count": sum(1 for row in checks if row["ok"]),
            "check_count": len(checks),
            "final_text_chars": len(final_text),
            "finalized_after_tool_budget": finalized,
            "ops": ops,
            "usage": delta["usage"],
            "model_calls": delta["model_calls"],
            "tool_calls": delta["tool_calls"],
            "driver_calls": delta["driver_calls"],
            "bad_tool_args": delta["bad_tool_args"],
            "known_sent": delta["known_sent"],
            "unchanged_count": delta["unchanged"],
            "tool_result_chars": delta["tool_result_chars"],
            "tool_result_tokens": delta["tool_result_tokens"],
            "engine_t": delta["engine_t"],
            "engine_t_model": delta["engine_t_model"],
            "engine_wire_bytes": delta["engine_wire_bytes"],
            "engine_wire_bytes_model": delta["engine_wire_bytes_model"],
            "wall_s": wall_s,
            "spec": spec_turn,
        })
    final_stats = arm.finish()
    spec_total = {field: _int(final_stats.get(field))
                  for field in _SPEC_FIELDS}
    spec_total["worker_ms"] = round(
        float(final_stats.get("worker_ms") or 0.0), 1)
    cumulative = {
        "turns": len(turn_rows),
        "accepted_turns": sum(1 for row in turn_rows if row["accepted"]),
        "accepted_all": all(row["accepted"] for row in turn_rows),
        "usage": {key: sum(_int(row["usage"].get(key)) for row in turn_rows)
                  for key in ("prompt_tokens", "completion_tokens",
                              "total_tokens", "reasoning_tokens",
                              "cached_prompt_tokens")},
        "model_calls": sum(_int(row["model_calls"]) for row in turn_rows),
        "tool_calls": sum(_int(row["tool_calls"]) for row in turn_rows),
        "driver_calls": sum(_int(row["driver_calls"]) for row in turn_rows),
        "known_sent": sum(_int(row["known_sent"]) for row in turn_rows),
        "unchanged_count": sum(_int(row["unchanged_count"])
                               for row in turn_rows),
        "tool_result_tokens": sum(_int(row["tool_result_tokens"])
                                  for row in turn_rows),
        "engine_t": sum(_int(row["engine_t"]) for row in turn_rows),
        "engine_t_model": sum(_int(row["engine_t_model"])
                              for row in turn_rows),
        "engine_wire_bytes": sum(_int(row["engine_wire_bytes"])
                                 for row in turn_rows),
        "engine_wire_bytes_model": sum(_int(row["engine_wire_bytes_model"])
                                       for row in turn_rows),
        "wall_s": round(sum(float(row["wall_s"]) for row in turn_rows), 3),
        "spec": spec_total,
    }
    return {
        "task": task["id"],
        "kind": task["kind"],
        "arm": arm_name,
        "turns": turn_rows,
        "cumulative": cumulative,
    }


def _multi_totals(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {}
    cumulative = [row["cumulative"] for row in rows]
    return {
        "tasks": len(rows),
        "turns": sum(_int(row["turns"]) for row in cumulative),
        "accepted_turns": sum(_int(row["accepted_turns"])
                              for row in cumulative),
        "accepted_tasks": sum(1 for row in cumulative if row["accepted_all"]),
        "accepted_all": all(row["accepted_all"] for row in cumulative),
        "usage": {key: sum(_int(row["usage"].get(key)) for row in cumulative)
                  for key in ("prompt_tokens", "completion_tokens",
                              "total_tokens", "reasoning_tokens",
                              "cached_prompt_tokens")},
        "model_calls": sum(_int(row["model_calls"]) for row in cumulative),
        "tool_calls": sum(_int(row["tool_calls"]) for row in cumulative),
        "driver_calls": sum(_int(row["driver_calls"]) for row in cumulative),
        "known_sent": sum(_int(row["known_sent"]) for row in cumulative),
        "unchanged_count": sum(_int(row["unchanged_count"])
                               for row in cumulative),
        "tool_result_tokens": sum(_int(row["tool_result_tokens"])
                                  for row in cumulative),
        "engine_t": sum(_int(row["engine_t"]) for row in cumulative),
        "engine_t_model": sum(_int(row["engine_t_model"])
                              for row in cumulative),
        "engine_wire_bytes": sum(_int(row["engine_wire_bytes"])
                                 for row in cumulative),
        "engine_wire_bytes_model": sum(_int(row["engine_wire_bytes_model"])
                                       for row in cumulative),
        "wall_s": round(sum(float(row["wall_s"]) for row in cumulative), 3),
        "spec": {field: sum(_int(row["spec"].get(field))
                            for row in cumulative)
                 for field in _SPEC_FIELDS}
        | {"worker_ms": round(sum(float(row["spec"].get("worker_ms") or 0.0)
                                  for row in cumulative), 1)},
    }


def run_known_probe(client: EngineClient, *, key: str,
                    budget: int = REPO_BUDGET,
                    query: str | None = None) -> dict[str, Any]:
    """Deterministic outline/find known-delta probe, no model involved."""
    def call(payload: dict[str, Any]) -> tuple[dict[str, Any], int]:
        result, wire = client.graph(payload)
        return result, wire

    def remembered(result: dict[str, Any],
                   op: str) -> list[dict[str, str]]:
        cols = result.get("c") if isinstance(result.get("c"), list) else []
        rows = result.get("r") if isinstance(result.get("r"), list) else []
        if not rows or "k" not in cols or "h" not in cols:
            return []
        key_at, hash_at = cols.index("k"), cols.index("h")
        return [{"op": op, "key": row[key_at], "hash": row[hash_at]}
                for row in rows
                if isinstance(row, list)
                and len(row) > max(key_at, hash_at)
                and isinstance(row[key_at], str)
                and isinstance(row[hash_at], str)]

    hello, _ = call({"op": "hello"})
    sid = hello.get("sid")
    if not isinstance(sid, str) or not sid:
        raise LiveError("known probe hello did not return a session id")
    full, full_wire = call({"op": "outline", "session": sid, "key": key,
                           "budget": budget})
    cols = full.get("c") if isinstance(full.get("c"), list) else []
    rows = full.get("r") if isinstance(full.get("r"), list) else []
    known: list[dict[str, str]] = []
    if rows and isinstance(rows[0], list) and "k" in cols and "h" in cols:
        row = rows[0]
        known = [{"op": "outline", "key": row[cols.index("k")],
                  "hash": row[cols.index("h")]}]
    delta, delta_wire = call({"op": "outline", "session": sid, "key": key,
                             "budget": budget, "known": known})
    report: dict[str, Any] = {
        "key": key,
        "full": {"t": _int(full.get("t")), "wire": full_wire,
                 "rows": len(rows)},
        "delta": {"t": _int(delta.get("t")), "wire": delta_wire,
                  "unchanged": len(delta.get("u") or [])},
        "saved_t": _int(full.get("t")) - _int(delta.get("t")),
        "saved_wire": full_wire - delta_wire,
    }
    # No client `known`: an engine with auto-known answers from the session it
    # already updated above.  When auto-known is off this is a full repeat and
    # the zero/negative saving is reported honestly.
    auto, auto_wire = call({"op": "outline", "session": sid, "key": key,
                            "budget": budget})
    report["auto"] = {
        "t": _int(auto.get("t")), "wire": auto_wire,
        "unchanged": len(auto.get("u") or []),
        "saved_t": _int(full.get("t")) - _int(auto.get("t")),
        "saved_wire": full_wire - auto_wire,
    }
    if query:
        # This find runs after the outline above, so it also proves that an
        # already-remembered outline hash for the same key does not shadow the
        # find hash: the op-tagged find entries still match the find rows.
        find_full, find_full_wire = call({
            "op": "find", "session": sid, "q": query, "kinds": ["sym"],
            "budget": budget})
        find_known = remembered(find_full, "find")
        find_delta, find_delta_wire = call({
            "op": "find", "session": sid, "q": query, "kinds": ["sym"],
            "budget": budget, "known": find_known})
        request = {"op": "find", "session": sid, "q": query,
                   "kinds": ["sym"], "budget": budget}
        known_bytes = len(json.dumps({**request, "known": find_known},
                                     separators=(",", ":")))
        find_auto, find_auto_wire = call({
            "op": "find", "session": sid, "q": query, "kinds": ["sym"],
            "budget": budget})
        report["find"] = {
            "q": query,
            "known_pairs": len(find_known),
            "full": {"t": _int(find_full.get("t")),
                     "wire": find_full_wire,
                     "rows": len(find_full.get("r") or [])},
            "delta": {"t": _int(find_delta.get("t")),
                      "wire": find_delta_wire,
                      "unchanged": len(find_delta.get("u") or [])},
            "request_bytes": known_bytes,
            "saved_t": _int(find_full.get("t")) - _int(find_delta.get("t")),
            "saved_wire": find_full_wire - find_delta_wire,
            # Response-only savings minus the larger known request, so the
            # reported value is honest total traffic, not only the response.
            "saved_total": (find_full_wire - find_delta_wire
                            - (known_bytes - len(json.dumps(
                                request, separators=(",", ":"))))),
            # Auto-known sends the same request as `full`: the saving is the
            # whole response difference and nothing is added to the request.
            "auto": {"t": _int(find_auto.get("t")),
                     "wire": find_auto_wire,
                     "unchanged": len(find_auto.get("u") or []),
                     "saved_t": (_int(find_full.get("t"))
                                 - _int(find_auto.get("t"))),
                     "saved_wire": find_full_wire - find_auto_wire},
        }
    call({"op": "bye", "session": sid})
    return report
def _summarise(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {}
    usage = {key: sum(_int(row["usage"].get(key)) for row in rows)
             for key in ("prompt_tokens", "completion_tokens", "total_tokens",
                         "reasoning_tokens", "cached_prompt_tokens")}
    return {
        "tasks": len(rows),
        "accepted": sum(1 for row in rows if row["accepted"]),
        "json_valid": sum(1 for row in rows if row["json_valid"]),
        "usage": usage,
        "tool_calls": sum(_int(row["tool_calls"]) for row in rows),
        "shape_calls": sum(1 for row in rows for arg in row["tool_args"]
                           if arg.get("op") == "shape"),
        "fetch_calls": sum(1 for row in rows for arg in row["tool_args"]
                           if arg.get("op") == "fetch"),
        "fetch_blocked": sum(_int(row.get("fetch_blocked")) for row in rows),
        "driver_calls": sum(_int(row["driver_calls"]) for row in rows),
        "model_calls": sum(_int(row["model_calls"]) for row in rows),
        "engine_t": sum(_int(row["engine_t"]) for row in rows),
        "engine_t_model": sum(_int(row["engine_t_model"]) for row in rows),
        "engine_wire_bytes": sum(_int(row["engine_wire_bytes"]) for row in rows),
        "engine_wire_bytes_model": sum(
            _int(row["engine_wire_bytes_model"]) for row in rows),
        "tool_result_tokens": sum(_int(row["tool_result_tokens"]) for row in rows),
        "wall_s": round(sum(float(row["wall_s"]) for row in rows), 3),
        "spec": {
            "prepared": sum(_int(row["spec"]["prepared"]) for row in rows),
            "ready_at_response": sum(
                sum(_int(value) for value in row.get(
                    "find_spec_ready_at_response") or [])
                for row in rows),
            "fetched": sum(_int(row["spec"]["fetched"]) for row in rows),
            "wasted": sum(_int(row["spec"]["wasted"]) for row in rows),
            "hits": sum(_int(row["spec"]["hits"]) for row in rows),
            "misses": sum(_int(row["spec"]["misses"]) for row in rows),
            "evicted": sum(_int(row["spec"]["evicted"]) for row in rows),
            "cancelled": sum(_int(row["spec"]["cancelled"]) for row in rows),
            "worker_ms": round(
                sum(float(row["spec"]["worker_ms"]) for row in rows), 1),
        },
    }


def interface_profile(budget: int = TINY_BUDGET) -> dict[str, Any]:
    """Model-facing prompt/tool overhead for each arm before a task prompt."""
    def size(system: str, schema: dict[str, Any],
             description: str) -> dict[str, Any]:
        tools = [_tool(schema, description)]
        tools_json = json.dumps(tools, ensure_ascii=False,
                                separators=(",", ":"))
        fixed = len(system) + len(tools_json)
        return {
            "system_chars": len(system),
            "system_tokens": (len(system) + 3) // 4,
            "tools_chars": len(tools_json),
            "tools_tokens": (len(tools_json) + 3) // 4,
            "fixed_chars": fixed,
            "fixed_tokens": (fixed + 3) // 4,
        }

    return {
        "control": size(_short_system(budget), _control_schema(),
                        "Read-only code graph."),
        "engine_legacy": size(_engine_system(budget), _engine_schema(),
                              "Stateful read-only graph session."),
        "engine_opt_no_handles": size(_short_system(budget), _opt_schema(False),
                                     "Read-only code graph."),
        "engine_opt_with_fetch": size(_short_system(budget), _opt_schema(True),
                                      "Read-only code graph."),
    }


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="bounded live evaluation of the graph engine HTTP transport")
    parser.add_argument("--live", action="store_true",
                        help="required; confirms a live model run")
    parser.add_argument("--base-url", default=os.environ.get(
        BASE_URL_ENV, DEFAULT_BASE_URL),
                        help="OpenAI-compatible endpoint base URL")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--engine-url", default=os.environ.get(ENGINE_URL_ENV),
                        help="running ozagia engine base URL")
    parser.add_argument("--engine-token-file", default=os.environ.get(
        ENGINE_TOKEN_FILE_ENV, DEFAULT_ENGINE_TOKEN_FILE))
    parser.add_argument("--fixture", "--repo", dest="fixture",
                        default=DEFAULT_FIXTURE,
                        help="repository path this run's tasks and snapshot describe")
    parser.add_argument("--task-set",
                        choices=tuple(dict.fromkeys(list(TASK_SETS) + list(MULTI_TASKS))),
                        default="tiny",
                        help="task set to run (default: the tiny fixture)")
    parser.add_argument("--suite", choices=("oneshot", "multiturn"),
                        default="oneshot",
                        help="one-shot task set or three-turn sessions "
                             "(default: oneshot)")
    parser.add_argument("--budget", type=int, default=None,
                        help="per-call estimated-token cap (default: 800 tiny, "
                             "1200 flask)")
    parser.add_argument("--arm",
                        choices=("engine", "engine_opt", "control", "both",
                                 "known", "all", "engine_known", "engine_auto",
                                 "engine_known_spec"),
                        default="both")
    parser.add_argument("--task", action="append",
                        choices=list(dict.fromkeys(
                            [task["id"] for task in TASKS]
                            + [task["id"] for task in FLASK_TASKS]
                            + [task["id"] for tasks in MULTI_TASKS.values()
                               for task in tasks])),
                        help="run only this task id (repeatable)")
    parser.add_argument("--repeat", type=int, default=1,
                        help="repeat the selected tasks in sequence (default 1)")
    parser.add_argument("--counterbalance", action="store_true",
                        help="reverse arm order on every other repeat")
    parser.add_argument("--max-tool-calls", type=int, default=MAX_TOOL_CALLS)
    parser.add_argument("--max-tokens", type=int, default=MAX_TOKENS)
    parser.add_argument("--final-tokens", type=int, default=None,
                        help=f"final answer budget (default {FINAL_TOKENS} "
                             "one-shot, 600 multi-turn)")
    parser.add_argument("--out", default=None,
                        help="write the full measurement JSON here")
    parser.add_argument("--no-spec-probe", action="store_true",
                        help="skip the deterministic find/fetch speculation probe")
    parser.add_argument("--spec-probe-query", default=None,
                        help="query for the deterministic speculation probe "
                             "(default: average for tiny, get_root_path for flask)")
    return parser.parse_args(argv)


def _main_multiturn(args: argparse.Namespace) -> int:
    if not args.live or os.environ.get(LIVE_ENV) != "1":
        print(f"error: live evaluation requires --live and {LIVE_ENV}=1",
              file=sys.stderr)
        return 2
    if not args.engine_url:
        print(f"error: --engine-url or {ENGINE_URL_ENV} is required",
              file=sys.stderr)
        return 2
    if args.repeat < 1:
        print("error: --repeat must be at least 1", file=sys.stderr)
        return 2
    if not (1 <= args.max_tool_calls <= 12):
        print("error: --max-tool-calls must be between 1 and 12",
              file=sys.stderr)
        return 2
    if not Path(args.fixture).expanduser().is_dir():
        print(f"error: --fixture/--repo is not a directory: {args.fixture}",
              file=sys.stderr)
        return 2
    budget = args.budget if args.budget is not None else (
        REPO_BUDGET if args.task_set == "flask" else TINY_BUDGET)
    if not (64 <= budget <= MAX_EVAL_BUDGET):
        print(f"error: --budget must be between 64 and {MAX_EVAL_BUDGET}",
              file=sys.stderr)
        return 2
    task_set = MULTI_TASKS.get(args.task_set)
    if task_set is None:
        print(f"error: no multi-turn task set {args.task_set!r}",
              file=sys.stderr)
        return 2
    selected = [task for task in task_set
                if not args.task or task["id"] in args.task]
    if not selected:
        print("error: no tasks selected", file=sys.stderr)
        return 2
    if args.arm == "all":
        arms = MULTI_ARMS
    elif args.arm == "known":
        arms = ("control", "engine_known", "engine_auto")
    elif args.arm == "both":
        arms = ("control", "engine_known")
    elif args.arm in MULTI_ARMS:
        arms = (args.arm,)
    else:
        print(f"error: multi-turn arms are {', '.join(MULTI_ARMS)} "
              "(or all/both/known)", file=sys.stderr)
        return 2
    final_tokens = (args.final_tokens if args.final_tokens is not None
                    else 600)
    try:
        endpoint_key = _endpoint_key()
        engine_token = _engine_token(args)
        client = EngineClient(args.engine_url, engine_token)
        health = client.health()
        if not health.get("ok"):
            raise LiveError("engine health check did not report ok")
        model_info, _ = _http_json(
            "GET", args.base_url.rstrip("/") + "/v1/models",
            token=endpoint_key, timeout=15.0, label="model listing")
        model_rows = model_info.get("data")
        model_root = None
        if isinstance(model_rows, list):
            for row in model_rows:
                if isinstance(row, dict) and row.get("id") == args.model:
                    model_root = row.get("root")
                    break
        model = {
            "base_url": args.base_url,
            "model": args.model,
            "model_root": model_root,
            "key": endpoint_key,
            "max_tool_calls": args.max_tool_calls,
            "max_tokens": args.max_tokens,
            "final_tokens": final_tokens,
        }
        results: dict[str, Any] = {
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "model": {"id": args.model, "root": model_root,
                      "base_url": args.base_url},
            "fixture": str(Path(args.fixture).resolve()),
            "task_set": args.task_set,
            "suite": "multiturn",
            "budget": budget,
            "engine_url": args.engine_url,
            "engine_health": health,
            "config": {
                "temperature": 0,
                "top_p": 1,
                "seed": 0,
                "enable_thinking": False,
                "max_tool_calls": args.max_tool_calls,
                "max_tokens": args.max_tokens,
                "final_tokens": final_tokens,
                "repeat": args.repeat,
                "counterbalance": args.counterbalance,
                "engine_default_budget": budget,
                "control_session": "fresh hello/bye per call",
                "engine_known_session": "one session for all three turns",
                "engine_known_injection": "find/outline/context rows; "
                                          "client remembers {key,hash}",
                "engine_auto": "one session; no client known; the serving "
                               "engine applies its own remembered hashes",
                "engine_known_spec": "engine_known + speculation on find",
                "engine_known_plain": "speculation off on find",
                "known_prompt": "shared system prompt tells the model that "
                                "u lists unchanged keys already in the "
                                "conversation",
            },
            "system_prompts": {"multi": _short_system(budget)},
            "tools": {"control": _control_schema(),
                      "engine_known": _opt_schema(False),
                      "engine_auto": _opt_schema(False),
                      "engine_known_with_fetch": _opt_schema(True)},
            "overhead": interface_profile(budget),
            "arm_order": [],
            "tasks": {
                task["id"]: {
                    "kind": task["kind"],
                    "turns": [
                        {"id": turn["id"], "prompt": turn["prompt"],
                         "expected": [name for name, _ in turn["checks"]]}
                        for turn in task["turns"]
                    ],
                }
                for task in selected
            },
            "arms": {name: {"tasks": [], "totals": {}}
                     for name in arms},
        }
        base_order = list(arms)
        for repeat in range(args.repeat):
            order = (base_order if not args.counterbalance or repeat % 2 == 0
                     else list(reversed(base_order)))
            results["arm_order"].append(order)
            for arm_name in order:
                for task in selected:
                    recorder = Recorder()
                    arm: EngineArm | ControlArm
                    if arm_name == "control":
                        arm = ControlArm(client, recorder, budget=budget)
                    else:
                        arm = EngineArm(
                            client, recorder, interface="optimized",
                            budget=budget,
                            known_reuse=(arm_name != "engine_auto"),
                            auto_known=(arm_name == "engine_auto"),
                            speculate=(arm_name == "engine_known_spec"))
                    print(f"[{arm_name}] repeat {repeat + 1} task {task['id']}",
                          file=sys.stderr, flush=True)
                    row = run_multiturn(arm, arm_name, task, model)
                    results["arms"][arm_name]["tasks"].append(row)
                    cumulative = row["cumulative"]
                    print(f"  accepted={cumulative['accepted_turns']}/"
                          f"{cumulative['turns']} "
                          f"tool_calls={cumulative['tool_calls']} "
                          f"known={cumulative['known_sent']} "
                          f"unchanged={cumulative['unchanged_count']} "
                          f"wall={cumulative['wall_s']}s "
                          f"tokens={cumulative['usage']['total_tokens']}",
                          file=sys.stderr, flush=True)
        for arm_name in arms:
            results["arms"][arm_name]["totals"] = _multi_totals(
                results["arms"][arm_name]["tasks"])
        if not args.no_spec_probe:
            probe_query = args.spec_probe_query or (
                "get_root_path" if args.task_set == "flask" else "average")
            probe_key = ("sym:src/flask/helpers.py::get_root_path"
                         if args.task_set == "flask"
                         else "sym:pkg/metrics.py::average")
            print("[engine] deterministic spec probe", file=sys.stderr,
                  flush=True)
            results["spec_probe"] = run_spec_probe(
                client, query=probe_query, budget=budget)
            print("[engine] deterministic known-delta probe", file=sys.stderr,
                  flush=True)
            results["known_probe"] = run_known_probe(
                client, key=probe_key, budget=budget, query=probe_query)
        if args.out:
            out_path = Path(args.out).expanduser()
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(json.dumps(results, indent=2) + "\n",
                                encoding="utf-8")
            print(f"wrote {out_path}", file=sys.stderr)
        print(json.dumps({
            "model": results["model"],
            "arms": {name: data["totals"]
                     for name, data in results["arms"].items()},
        }, indent=2))
        return 0
    except LiveError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.suite == "multiturn":
        return _main_multiturn(args)
    if not args.live or os.environ.get(LIVE_ENV) != "1":
        print(f"error: live evaluation requires --live and {LIVE_ENV}=1",
              file=sys.stderr)
        return 2
    if not args.engine_url:
        print(f"error: --engine-url or {ENGINE_URL_ENV} is required",
              file=sys.stderr)
        return 2
    if args.repeat < 1:
        print("error: --repeat must be at least 1", file=sys.stderr)
        return 2
    if not (1 <= args.max_tool_calls <= 12):
        print("error: --max-tool-calls must be between 1 and 12",
              file=sys.stderr)
        return 2
    if not Path(args.fixture).expanduser().is_dir():
        print(f"error: --fixture/--repo is not a directory: {args.fixture}",
              file=sys.stderr)
        return 2
    budget = args.budget if args.budget is not None else (
        REPO_BUDGET if args.task_set == "flask" else TINY_BUDGET)
    if not (64 <= budget <= MAX_EVAL_BUDGET):
        print(f"error: --budget must be between 64 and {MAX_EVAL_BUDGET}",
              file=sys.stderr)
        return 2
    final_tokens = (args.final_tokens if args.final_tokens is not None
                    else FINAL_TOKENS)
    selected = [task for task in TASK_SETS[args.task_set]
                if not args.task or task["id"] in args.task]
    if not selected:
        print("error: no tasks selected", file=sys.stderr)
        return 2
    if args.task and any(task_id not in
                         {t["id"] for t in TASK_SETS[args.task_set]}
                         for task_id in args.task):
        print(f"error: selected --task ids are not in the {args.task_set} set",
              file=sys.stderr)
        return 2

    try:
        key = _endpoint_key()
        engine_token = _engine_token(args)
        client = EngineClient(args.engine_url, engine_token)
        health = client.health()
        if not health.get("ok"):
            raise LiveError("engine health check did not report ok")
        model_info, _ = _http_json(
            "GET", args.base_url.rstrip("/") + "/v1/models", token=key,
            timeout=15.0, label="model listing")
        model_rows = model_info.get("data")
        model_root = None
        if isinstance(model_rows, list):
            for row in model_rows:
                if isinstance(row, dict) and row.get("id") == args.model:
                    model_root = row.get("root")
                    break
        model = {
            "base_url": args.base_url,
            "model": args.model,
            "model_root": model_root,
            "key": key,
            "max_tool_calls": args.max_tool_calls,
            "max_tokens": args.max_tokens,
            "final_tokens": final_tokens,
        }
        if args.arm == "all":
            arms = ("control", "engine", "engine_opt")
        elif args.arm == "both":
            arms = ("engine", "control")
        elif args.arm in ("engine", "engine_opt", "control"):
            arms = (args.arm,)
        else:
            print(f"error: one-shot arms are engine, engine_opt, control "
                  "(or both/all); use --suite multiturn for "
                  f"{', '.join(MULTI_ARMS)}", file=sys.stderr)
            return 2
        results: dict[str, Any] = {
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "model": {"id": args.model, "root": model_root,
                      "base_url": args.base_url},
            "fixture": str(Path(args.fixture).resolve()),
            "task_set": args.task_set,
            "budget": budget,
            "engine_url": args.engine_url,
            "engine_health": health,
            "config": {
                "temperature": 0,
                "top_p": 1,
                "seed": 0,
                "enable_thinking": False,
                "max_tool_calls": args.max_tool_calls,
                "max_tokens": args.max_tokens,
                "final_tokens": final_tokens,
                "repeat": args.repeat,
                "counterbalance": args.counterbalance,
                "engine_default_budget": budget,
                "engine_find_spec_default": True,
                "engine_fetch_wait_ms_default": 200,
                "control_session": "fresh hello/bye per call",
                "optimized_fetch": "schema property only while q handles exist",
            },
            "system_prompts": {"engine": _engine_system(budget),
                               "engine_opt": _short_system(budget),
                               "control": _short_system(budget)},
            "tools": {"engine": _engine_schema(),
                      "control": _control_schema(),
                      "engine_opt_base": _opt_schema(False),
                      "engine_opt_fetch": _opt_schema(True)},
            "overhead": interface_profile(budget),
            "arm_order": [],
            "tasks": {task["id"]: {"kind": task["kind"],
                                   "prompt": task["prompt"],
                                   "expected": [name for name, _ in task["checks"]]}
                      for task in selected},
            "arms": {name: {"tasks": [], "totals": {}}
                     for name in arms},
        }
        base_order = list(arms)
        for repeat in range(args.repeat):
            order = (base_order if not args.counterbalance or repeat % 2 == 0
                     else list(reversed(base_order)))
            results["arm_order"].append(order)
            for arm_name in order:
                for task in selected:
                    recorder = Recorder()
                    arm: EngineArm | ControlArm
                    if arm_name == "control":
                        arm = ControlArm(client, recorder, budget=budget)
                    else:
                        interface = ("optimized" if arm_name == "engine_opt"
                                     else "legacy")
                        arm = EngineArm(client, recorder, interface=interface,
                                        budget=budget)
                    print(f"[{arm_name}] repeat {repeat + 1} task {task['id']}",
                          file=sys.stderr, flush=True)
                    row = run_task(arm, arm_name, task, model)
                    results["arms"][arm_name]["tasks"].append(row)
                    print(f"  accepted={row['accepted']} "
                          f"json={row['json_valid']} "
                          f"tool_calls={row['tool_calls']} "
                          f"wall={row['wall_s']}s "
                          f"tokens={row['usage']['total_tokens']}",
                          file=sys.stderr, flush=True)
        for arm_name in arms:
            results["arms"][arm_name]["totals"] = _summarise(
                results["arms"][arm_name]["tasks"])
        if not args.no_spec_probe:
            print("[engine] deterministic spec probe", file=sys.stderr,
                  flush=True)
            probe_query = args.spec_probe_query or (
                "get_root_path" if args.task_set == "flask" else "average")
            results["spec_probe"] = run_spec_probe(
                client, query=probe_query, budget=budget)
        if args.out:
            out_path = Path(args.out).expanduser()
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(json.dumps(results, indent=2) + "\n",
                                encoding="utf-8")
            print(f"wrote {out_path}", file=sys.stderr)
        print(json.dumps({
            "model": results["model"],
            "arms": {name: data["totals"]
                     for name, data in results["arms"].items()},
        }, indent=2))
        return 0
    except LiveError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
