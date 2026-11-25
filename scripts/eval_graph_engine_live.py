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
        --out /tmp/opencode/engine-live/eval.json

This is a small measured sample, not a general model-task claim.  See
``docs/results/graph_engine_live_2026-11-25.md``.
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
DEFAULT_BUDGET = 800
MAX_FETCH_WAIT_MS = 250
MAX_TOOL_RESULT_CHARS = 12_000
OPS = ("hello", "find", "outline", "expand", "impact", "tests", "context",
       "shape", "fetch", "bye")
ENGINE_OPS = ("find", "outline", "expand", "impact", "tests", "context",
              "shape", "fetch")
CONTROL_OPS = ("find", "outline", "expand", "impact", "tests", "shape")


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
def _engine_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "op": {"type": "string", "enum": list(ENGINE_OPS)},
            "q": {"type": "string"},
            "key": {"type": "string"},
            "keys": {"type": "array", "items": {"type": "string"}},
            "budget": {"type": "integer"},
            "spec": {"type": "boolean"},
            "fetch": {"type": "array", "items": {"type": "string"}},
            "h": {"type": "string"},
        },
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
        items = result.get("it")
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict) and item.get("fs") is not None:
                    self.fetch_states.append(item["fs"])


class EngineArm:
    """Stateful engine surface: one session, optional spec/fetch."""

    def __init__(self, client: EngineClient, recorder: Recorder) -> None:
        self.client = client
        self.recorder = recorder
        self.sid: str | None = None
        self.closed = False
        self.session_override = 0
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
        return result

    def call(self, args: dict[str, Any]) -> tuple[dict[str, Any], int]:
        op = str(args.get("op") or "")
        if op == "hello":
            self.session_override += 1
            result, wire = self.client.graph(args)
            self.recorder.add_engine(result, wire, model_visible=True)
            sid = result.get("sid")
            if isinstance(sid, str) and sid:
                self.sid = sid
                self.closed = False
            return result, wire
        if op == "bye":
            if not self.sid or self.closed:
                return {"bye": 1, "t": 1}, 0
            payload = {"op": "bye", "session": self.sid}
            result, wire = self.client.graph(payload)
            self.recorder.add_engine(result, wire, model_visible=True)
            self.closed = True
            return result, wire
        if not self.sid or self.closed:
            self._hello()
        if not self.sid:
            raise LiveError("engine session could not be established")
        payload = dict(args)
        if payload.get("session") and payload["session"] != self.sid:
            self.session_override += 1
        payload["session"] = self.sid
        payload["budget"] = _budget(payload.get("budget"))
        if op == "find":
            payload.setdefault("spec", True)
        if op == "fetch":
            payload.setdefault("wait_ms", 200)
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
        return stats


class ControlArm:
    """Stateless control: a fresh session per call, no spec/known/fetch."""

    def __init__(self, client: EngineClient, recorder: Recorder) -> None:
        self.client = client
        self.recorder = recorder

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
        payload["budget"] = _budget(payload.get("budget"))
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
                   think_s: float = 0.2) -> dict[str, Any]:
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
                 "kinds": ["sym"], "spec": True, "budget": DEFAULT_BUDGET})
    spec = find.get("q") if isinstance(find.get("q"), dict) else {}
    handles = list(spec.get("h") or [])
    ready_at_response = _int(spec.get("r"))
    time.sleep(think_s)
    fetch_states: list[Any] = []
    if handles:
        fetched = call({"op": "fetch", "session": sid,
                        "fetch": handles[:4], "budget": DEFAULT_BUDGET,
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


def _budget(value: Any) -> int:
    """Clamp a model-supplied budget to the small evaluation window."""
    return max(64, min(MAX_BUDGET, _int(value, DEFAULT_BUDGET)))


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


TASKS: list[dict[str, Any]] = [
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
            "count, Python module paths, and the test command. Reply with only "
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

ENGINE_SYSTEM = (
    "You have exactly one tool, graph: a read-only code graph. Ops: find, "
    "outline, expand, impact, tests, context, shape, fetch. The transport "
    "creates and closes the session for you; do not call hello or bye. Use "
    "budgets of at most 800. find may return speculative q handles; fetch them "
    "with wait_ms 200 when useful. After at most six tool calls, reply with "
    "only the JSON object the user asks for."
)
CONTROL_SYSTEM = (
    "You have exactly one tool, graph: a read-only code graph. Ops: find, "
    "outline, expand, impact, tests, shape. Each call is one-shot. Use budgets "
    "of at most 800. After at most six tool calls, reply with only the JSON "
    "object the user asks for."
)


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
    system = ENGINE_SYSTEM if arm_name == "engine" else CONTROL_SYSTEM
    schema = _engine_schema() if arm_name == "engine" else _control_schema()
    description = ("Stateful read-only graph session." if arm_name == "engine"
                   else "One-shot read-only graph query.")
    tools = [_tool(schema, description)]
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
                    if arm_name == "engine" and op == "find" and spec_value is None:
                        spec_value = True
                    recorder.tool_args.append({
                        "op": op,
                        "budget": _budget(parsed_args.get("budget")),
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
# Entry point
# --------------------------------------------------------------------------- #
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
    parser.add_argument("--fixture", default=DEFAULT_FIXTURE)
    parser.add_argument("--arm", choices=("engine", "control", "both"),
                        default="both")
    parser.add_argument("--task", action="append", choices=[task["id"] for task in TASKS],
                        help="run only this task id (repeatable)")
    parser.add_argument("--repeat", type=int, default=1,
                        help="repeat the selected tasks in sequence (default 1)")
    parser.add_argument("--max-tool-calls", type=int, default=MAX_TOOL_CALLS)
    parser.add_argument("--max-tokens", type=int, default=MAX_TOKENS)
    parser.add_argument("--final-tokens", type=int, default=FINAL_TOKENS)
    parser.add_argument("--out", default=None,
                        help="write the full measurement JSON here")
    parser.add_argument("--no-spec-probe", action="store_true",
                        help="skip the deterministic find/fetch speculation probe")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
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
    selected = [task for task in TASKS
                if not args.task or task["id"] in args.task]
    if not selected:
        print("error: no tasks selected", file=sys.stderr)
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
            "final_tokens": args.final_tokens,
        }
        arms = ("engine", "control") if args.arm == "both" else (args.arm,)
        results: dict[str, Any] = {
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "model": {"id": args.model, "root": model_root,
                      "base_url": args.base_url},
            "fixture": str(Path(args.fixture).resolve()),
            "engine_url": args.engine_url,
            "engine_health": health,
            "config": {
                "temperature": 0,
                "top_p": 1,
                "seed": 0,
                "enable_thinking": False,
                "max_tool_calls": args.max_tool_calls,
                "max_tokens": args.max_tokens,
                "final_tokens": args.final_tokens,
                "repeat": args.repeat,
                "engine_default_budget": DEFAULT_BUDGET,
                "engine_find_spec_default": True,
                "engine_fetch_wait_ms_default": 200,
                "control_session": "fresh hello/bye per call",
            },
            "system_prompts": {"engine": ENGINE_SYSTEM,
                               "control": CONTROL_SYSTEM},
            "tools": {"engine": _engine_schema(),
                      "control": _control_schema()},
            "tasks": {task["id"]: {"kind": task["kind"],
                                   "prompt": task["prompt"],
                                   "expected": [name for name, _ in task["checks"]]}
                      for task in selected},
            "arms": {},
        }
        for arm_name in arms:
            arm_rows: list[dict[str, Any]] = []
            for repeat in range(args.repeat):
                for task in selected:
                    recorder = Recorder()
                    arm: EngineArm | ControlArm
                    if arm_name == "engine":
                        arm = EngineArm(client, recorder)
                    else:
                        arm = ControlArm(client, recorder)
                    print(f"[{arm_name}] repeat {repeat + 1} task {task['id']}",
                          file=sys.stderr, flush=True)
                    row = run_task(arm, arm_name, task, model)
                    arm_rows.append(row)
                    print(f"  accepted={row['accepted']} "
                          f"json={row['json_valid']} "
                          f"tool_calls={row['tool_calls']} "
                          f"wall={row['wall_s']}s "
                          f"tokens={row['usage']['total_tokens']}",
                          file=sys.stderr, flush=True)
            results["arms"][arm_name] = {
                "tasks": arm_rows,
                "totals": _summarise(arm_rows),
            }
        if not args.no_spec_probe:
            print("[engine] deterministic spec probe", file=sys.stderr,
                  flush=True)
            results["spec_probe"] = run_spec_probe(client)
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
