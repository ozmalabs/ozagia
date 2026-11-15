"""ozagia.harness.native — a small agent loop over an OpenAI-compatible API.

The model gets six tools with OpenCode 2.3's names and input fields (read,
grep, glob, edit, write, shell; see `ozagia.harness.tools`). Two modes:

- `plain`: the built-in implementations of `ozagia.harness.tools` only.
- `graph`: each call goes first to `ozagia.intent.handle(...)` in process.
  A handled call returns the intent layer's output (a handled error is a
  tool error). A call it does not handle runs the built-in, then
  `ozagia.intent.after(...)`, whose `append` text is added to the output.

The loop: `POST {base_url}/chat/completions` with the tools and
`tool_choice: "auto"`; every tool call of the reply runs in order and its
result goes back as a `role: tool` message; repeat until the model replies
without tool calls, or `max_steps` model turns or `timeout_s` are used up.
A step is one model turn. `transcript.jsonl` (one JSON object per step, with
its tool calls) and `summary.json` go to `log_dir`. The API key is sent as
`Authorization: Bearer` and never written to a log or put in argv.

    python -m ozagia.harness.native run --workdir W --prompt-file P \\
        --mode plain|graph --base-url U --model M \\
        [--api-key-env NAME | --api-key-file F] [--dump D] [--log-dir L] \\
        [--max-steps N] [--timeout S] [--temperature T]

prints `summary.json` on stdout. See docs/native_loop.md.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import socket
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

MODES = ("plain", "graph")
MAX_STEPS = 100
DEFAULT_TIMEOUT_S = 900
# Attempts per model request; 429 and 5xx are retried.
HTTP_ATTEMPTS = 3
# Wait before the second attempt; doubled before each later one.
RETRY_BACKOFF_S = 1.0
STOP_REASONS = ("done", "max_steps", "timeout", "error")

_DESCRIPTIONS = {
    "read": "Read a file (numbered lines) or list a directory.",
    "grep": "Search file contents with a regular expression (ripgrep).",
    "glob": "Find files by glob pattern, newest first.",
    "edit": "Replace an exact, unique string in a file.",
    "write": "Write a whole file (creates parent directories).",
    "shell": "Run a bash command in the working directory.",
}
GRAPH_NOTE = (" Lines that start with [ozagia] are graph context from the "
              "repository graph, not file content.")


def _obj(props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": required}


def tool_schemas(mode: str) -> list[dict[str, Any]]:
    """The `tools` array: OpenCode 2.3's input fields as JSON schemas."""
    graph = mode == "graph"
    s, i, b = {"type": "string"}, {"type": "integer"}, {"type": "boolean"}
    path = dict(s, description="File path, absolute or relative to the "
                "working directory"
                + (", or a graph node key (sym:, test:, file:, doc:, text:)"
                   if graph else ""))
    params = {
        "read": _obj({"path": path,
                      "offset": dict(i, description="First line, 1-based"),
                      "limit": dict(i, description="Number of lines")},
                     ["path"]),
        "grep": _obj({"pattern": dict(s, description="Regular expression"),
                      "path": dict(s, description="Directory or file to search"),
                      "include": dict(s, description="File glob, e.g. *.py"),
                      "caseSensitive": b}, ["pattern"]),
        "glob": _obj({"pattern": s, "path": dict(s, description="Directory")},
                     ["pattern"]),
        "edit": _obj({"path": dict(s, description="File path"),
                      "oldString": s, "newString": s,
                      "replaceAll": b}, ["path", "oldString", "newString"]),
        "write": _obj({"path": dict(s, description="File path"),
                       "content": s}, ["path", "content"]),
        "shell": _obj({"command": s,
                       "workdir": dict(s, description="Directory to run in"),
                       "timeout": dict(i, description="Timeout in milliseconds"),
                       "description": dict(s, description="What it does, briefly")},
                      ["command"]),
    }
    out = []
    for name, desc in _DESCRIPTIONS.items():
        if graph:
            desc += GRAPH_NOTE
            if name == "read":
                desc += " The path may also be a graph node key."
        out.append({"type": "function", "function": {
            "name": name, "description": desc, "parameters": params[name]}})
    return out


def default_system_prompt(workdir: str | Path) -> str:
    return (f"You are a coding agent working in the repository at {workdir}. "
            "Fix the problem the user describes by editing the code, and "
            "verify the fix by running the relevant tests. Do not make git "
            "commits or move git refs. Use the tools; when you are done, reply "
            "with a short summary and no tool call.")


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
class ModelError(RuntimeError):
    """The model endpoint failed for good."""


class ModelTimeout(ModelError):
    """The run's time ran out during a model request."""


def _headers(api_key: str | None) -> dict[str, str]:
    h = {"Content-Type": "application/json", "Accept": "application/json"}
    if api_key:
        h["Authorization"] = f"Bearer {api_key}"
    return h


def http_json(method: str, url: str, *, body: Any = None,
              api_key: str | None = None, timeout_s: float = 60,
              attempts: int = HTTP_ATTEMPTS,
              deadline: float | None = None) -> Any:
    """One JSON request with retries on 429 and 5xx (`attempts` in all,
    backoff RETRY_BACKOFF_S doubling). Raises ModelTimeout when `deadline`
    (time.monotonic) passes, ModelError otherwise."""
    data = None if body is None else json.dumps(body).encode("utf-8")
    wait = RETRY_BACKOFF_S
    last = ""
    for attempt in range(1, attempts + 1):
        remaining = timeout_s
        if deadline is not None:
            remaining = min(remaining, deadline - time.monotonic())
            if remaining <= 0:
                raise ModelTimeout("run timed out before the model answered")
        req = urllib.request.Request(url, data=data, method=method,
                                     headers=_headers(api_key))
        try:
            with urllib.request.urlopen(req, timeout=remaining) as resp:
                raw = resp.read()
            try:
                return json.loads(raw.decode("utf-8"))
            except ValueError:
                raise ModelError(f"response is not JSON: {raw[:200]!r}") from None
        except urllib.error.HTTPError as e:
            detail = e.read()[:500].decode("utf-8", errors="replace")
            last = f"HTTP {e.code}: {detail}"
            if e.code in (401, 403):
                raise ModelError(f"authentication failed ({last})") from None
            if e.code != 429 and e.code < 500:
                raise ModelError(last) from None
        except (TimeoutError, socket.timeout):
            if deadline is not None and time.monotonic() >= deadline - 0.05:
                raise ModelTimeout("run timed out waiting for the model") from None
            last = "request timed out"
        except urllib.error.URLError as e:
            if isinstance(e.reason, (TimeoutError, socket.timeout)):
                if deadline is not None and time.monotonic() >= deadline - 0.05:
                    raise ModelTimeout("run timed out waiting for the model") from None
                last = "request timed out"
            else:
                raise ModelError(f"cannot reach {url}: {e.reason}") from None
        if attempt < attempts:
            if deadline is not None and time.monotonic() + wait >= deadline:
                raise ModelTimeout(f"run timed out while retrying ({last})")
            time.sleep(wait)
            wait *= 2
    raise ModelError(f"giving up after {attempts} attempts: {last}")


def list_models(base_url: str, api_key: str | None, *,
                timeout_s: float = 30) -> list[str]:
    """Model ids from `GET {base_url}/models` (OpenAI's `data[].id`)."""
    doc = http_json("GET", base_url.rstrip("/") + "/models", api_key=api_key,
                    timeout_s=timeout_s)
    items = doc.get("data") if isinstance(doc, dict) else None
    if not isinstance(items, list):
        raise ModelError("models response has no data list")
    return [str(m.get("id")) for m in items if isinstance(m, dict)]


# --------------------------------------------------------------------------- #
# tool dispatch
# --------------------------------------------------------------------------- #
class _Executor:
    """Runs one tool call in the run's mode and keeps the counts."""

    def __init__(self, workdir: Path, mode: str, memory: Any,
                 shell_env: dict[str, str], deadline: float) -> None:
        self.workdir = workdir
        self.mode = mode
        self.memory = memory
        self.shell_env = shell_env
        self.deadline = deadline
        self.captured = 0
        self.passthrough = 0   # not handled by design
        self.fallbacks = 0     # the intent layer raised: the built-in ran

    def _plain(self, name: str, args: dict[str, Any]) -> tuple[str, bool]:
        from ozagia.harness.tools import ToolError, run_tool
        from ozagia.intent.run import refusal
        from ozagia.intent.shell import classify
        if name == "shell" and isinstance(args.get("command"), str):
            # The same refusal as graph mode, so both arms keep the refs.
            if classify(args["command"])["class"] == "git_refs":
                return refusal(args["command"]), True
        try:
            return run_tool(self.workdir, name, args, env=self.shell_env,
                            max_timeout_s=self.deadline - time.monotonic()
                            ), False
        except ToolError as e:
            return str(e), True

    def _intent_input(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        """`args` plus what the plain shell would apply: its default timeout,
        the run's deadline and the agent's shell environment (the intent
        layer's reserved fields; the transcript keeps `args`)."""
        if name != "shell":
            return args
        from ozagia.harness.tools import DEFAULT_SHELL_TIMEOUT_MS
        inp = dict(args)
        if not inp.get("timeout"):
            inp["timeout"] = DEFAULT_SHELL_TIMEOUT_MS
        inp["_deadline_s_remaining"] = self.deadline - time.monotonic()
        inp["_test_env"] = dict(self.shell_env)
        return inp

    def call(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        rec: dict[str, Any] = {}
        if self.mode != "graph":
            out, err = self._plain(name, args)
            rec.update(output=out, is_error=err)
            return rec
        from ozagia import intent
        res = intent.handle(self.memory, name, self._intent_input(name, args),
                            harness="native")
        meta = res.get("meta") or {}
        rec["intent"] = meta.get("intent")
        if res.get("handled"):
            self.captured += 1
            rec.update(output=str(res.get("output", "")),
                       is_error=bool(res.get("is_error")),
                       handled=True, fallback=False)
            return rec
        failed = bool(meta.get("error"))
        if failed:
            self.fallbacks += 1
        else:
            self.passthrough += 1
        out, err = self._plain(name, args)
        after = intent.after(self.memory, name, args, out, harness="native")
        append = after.get("append") or ""
        if append:
            out = out + "\n" + append
        rec.update(output=out, is_error=err, handled=False, fallback=failed,
                   reason=res.get("reason"),
                   after_intent=(after.get("meta") or {}).get("intent"))
        return rec


def _shell_env(api_key: str | None) -> dict[str, str]:
    """The agent's shell environment: this process's, without PYTHONPATH
    (it points at ozagia, not the target), without the eval's
    `OZAGIA_NATIVE_*` settings (one names the key file) and without any
    variable that holds the API key. Graph mode's captured pytest gets the
    same environment."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("OZAGIA_NATIVE_")}
    env.pop("PYTHONPATH", None)
    if api_key:
        for k in [k for k, v in env.items() if api_key in v]:
            env.pop(k)
    return env


def _parse_args(raw: Any) -> tuple[dict[str, Any] | None, str | None]:
    if isinstance(raw, dict):
        return raw, None
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return {}, None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError) as e:
        return None, f"tool arguments are not valid JSON: {e}"
    if not isinstance(value, dict):
        return None, "tool arguments must be a JSON object"
    return value, None


def load_graph(workdir: Path, dump: str | Path | None) -> Any:
    """`DevMemory` for `workdir` from `dump` (built there when missing).
    Reingests do not rewrite the snapshot (`defer_writes`); `run` writes it
    once at the end."""
    from ozagia.daemon.server import load_memory
    memory = load_memory(Path(workdir).resolve(), dump)
    memory.defer_writes = True
    return memory


def _plain_http_warning(base_url: str) -> str | None:
    """A warning for a base URL that sends the key unencrypted to another
    host (`http://` to anything but loopback)."""
    from urllib.parse import urlsplit
    parts = urlsplit(base_url)
    if parts.scheme != "http":
        return None
    host = (parts.hostname or "").lower()
    if host == "localhost" or host.startswith("127.") or host in ("::1", "[::1]"):
        return None
    return (f"warning: {base_url} is plain http to a non-loopback host; the "
            "API key and the conversation are sent unencrypted")


# --------------------------------------------------------------------------- #
# the loop
# --------------------------------------------------------------------------- #
def _number(v: Any) -> float | None:
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def run(workdir: str | Path, prompt: str, *, mode: str, base_url: str,
        model: str, api_key: str | None = None, max_steps: int = MAX_STEPS,
        timeout_s: float = DEFAULT_TIMEOUT_S, temperature: float | None = None,
        system_prompt: str | None = None, log_dir: str | Path | None = None,
        dump: str | Path | None = None, memory: Any = None) -> dict[str, Any]:
    """Run the agent on `workdir` until it stops. Returns the summary (also
    written to `log_dir/summary.json`). `memory` (graph mode) is a loaded
    `DevMemory`; without it the graph is loaded from `dump`."""
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}, got {mode!r}")
    started = time.monotonic()
    deadline = started + timeout_s
    workdir = Path(workdir).resolve()
    log = Path(log_dir) if log_dir else Path(tempfile.mkdtemp(
        prefix="ozagia-native-"))
    log.mkdir(parents=True, exist_ok=True)
    transcript = log / "transcript.jsonl"
    transcript.write_text("", encoding="utf-8")

    def scrub(text: str) -> str:
        return text.replace(api_key, "***") if api_key else text

    summary: dict[str, Any] = {
        "exit_code": 0, "steps": 0, "tokens_in": 0, "tokens_out": 0,
        "cost_usd": None, "tool_calls": 0, "tools_by_name": {},
        "captured_calls": None, "passthrough_calls": None, "fallbacks": None,
        "stopped": None,
        "error": None, "final_text": "", "mode": mode, "model": model,
        "wall_s": 0.0, "log_dir": str(log)}
    warn = _plain_http_warning(base_url)
    if warn:
        print(warn, file=sys.stderr, flush=True)
    try:
        if mode == "graph" and memory is None:
            memory = load_graph(workdir, dump)
        elif mode == "graph":
            with contextlib.suppress(AttributeError):  # a stand-in object
                memory.defer_writes = True
    except Exception as e:  # noqa: BLE001 - reported in the summary
        summary.update(stopped="error", exit_code=1,
                       error=f"graph load failed: {type(e).__name__}: {e}")
        return _finish(summary, log, started, scrub)
    ex = _Executor(workdir, mode, memory, _shell_env(api_key), deadline)
    url = base_url.rstrip("/") + "/chat/completions"
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt or default_system_prompt(workdir)},
        {"role": "user", "content": prompt}]
    tools = tool_schemas(mode)
    cost = 0.0
    saw_cost = False
    by_name: dict[str, int] = {}
    from ozagia.harness.tools import TOOL_NAMES
    stopped = "max_steps"
    for step in range(1, max_steps + 1):
        if time.monotonic() >= deadline:
            stopped = "timeout"
            break
        body: dict[str, Any] = {"model": model, "messages": messages,
                                "tools": tools, "tool_choice": "auto"}
        if temperature is not None:
            body["temperature"] = temperature
        t0 = time.perf_counter()
        try:
            resp = http_json("POST", url, body=body, api_key=api_key,
                             timeout_s=max(deadline - time.monotonic(), 0.01),
                             deadline=deadline)
        except ModelTimeout as e:
            stopped = "timeout"
            summary["error"] = str(e)
            break
        except ModelError as e:
            stopped = "error"
            summary["error"] = str(e)
            break
        model_ms = round((time.perf_counter() - t0) * 1000, 1)
        summary["steps"] = step
        usage = resp.get("usage") if isinstance(resp, dict) else None
        usage = usage if isinstance(usage, dict) else {}
        summary["tokens_in"] += int(_number(usage.get("prompt_tokens")) or 0)
        summary["tokens_out"] += int(_number(usage.get("completion_tokens")) or 0)
        c = _number(usage.get("cost"))
        if c is None and isinstance(resp, dict):
            c = _number(resp.get("cost"))
        if c is not None:
            cost += float(c)
            saw_cost = True
        choices = resp.get("choices") if isinstance(resp, dict) else None
        msg = (choices[0].get("message") if isinstance(choices, list) and choices
               and isinstance(choices[0], dict) else None)
        if not isinstance(msg, dict):
            stopped = "error"
            summary["error"] = "response has no choices[0].message"
            _record(transcript, {"step": step, "ms": model_ms, "usage": usage,
                                 "error": summary["error"]}, scrub)
            break
        content = msg.get("content") if isinstance(msg.get("content"), str) else ""
        calls = msg.get("tool_calls") if isinstance(msg.get("tool_calls"), list) else []
        entry: dict[str, Any] = {"step": step, "ms": model_ms, "usage": usage,
                                 "finish_reason": choices[0].get("finish_reason"),
                                 "content": content, "tools": []}
        if not calls:
            summary["final_text"] = content
            _record(transcript, entry, scrub)
            stopped = "done"
            break
        norm_calls = []
        for call in calls:
            fn = call.get("function") if isinstance(call, dict) else None
            fn = fn if isinstance(fn, dict) else {}
            norm_calls.append({"id": str((call or {}).get("id") or
                                         f"call_{uuid.uuid4().hex[:12]}"),
                               "type": "function",
                               "function": {"name": str(fn.get("name") or ""),
                                            "arguments": fn.get("arguments")
                                            if isinstance(fn.get("arguments"), str)
                                            else json.dumps(fn.get("arguments") or {})}})
        messages.append({"role": "assistant", "content": content or None,
                         "tool_calls": norm_calls})
        for call in norm_calls:
            name = call["function"]["name"]
            summary["tool_calls"] += 1
            by_name[name] = by_name.get(name, 0) + 1
            args, bad = _parse_args(call["function"]["arguments"])
            t1 = time.perf_counter()
            if name not in TOOL_NAMES:
                rec: dict[str, Any] = {
                    "output": f"unknown tool: {name}; available: "
                              + ", ".join(TOOL_NAMES), "is_error": True}
            elif bad is not None:
                rec = {"output": bad, "is_error": True}
            else:
                rec = ex.call(name, args or {})
            out = rec.pop("output")
            text = f"Error: {out}" if rec.get("is_error") else out
            messages.append({"role": "tool", "tool_call_id": call["id"],
                             "content": text})
            entry["tools"].append({
                "id": call["id"], "name": name,
                "input": args if args is not None else call["function"]["arguments"],
                "output_chars": len(text),
                "ms": round((time.perf_counter() - t1) * 1000, 1),
                **rec, "output": text})
        _record(transcript, entry, scrub)
    summary["stopped"] = stopped
    if stopped == "error":
        summary["exit_code"] = 1
    summary["cost_usd"] = round(cost, 6) if saw_cost else None
    summary["tools_by_name"] = dict(sorted(by_name.items()))
    if mode == "graph":
        summary["captured_calls"] = ex.captured
        summary["passthrough_calls"] = ex.passthrough
        summary["fallbacks"] = ex.fallbacks
        if getattr(memory, "snapshot_dirty", False):
            # Once, after the loop (the reingests only marked it dirty).
            try:
                summary["snapshot_write_s"] = round(memory.write_snapshot(), 3)
                memory.snapshot_dirty = False
            except OSError as e:
                summary["snapshot_error"] = f"{type(e).__name__}: {e}"
    return _finish(summary, log, started, scrub)


def _record(path: Path, entry: dict[str, Any], scrub: Any) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write(scrub(json.dumps(entry, ensure_ascii=False, default=str)) + "\n")


def _finish(summary: dict[str, Any], log: Path, started: float,
            scrub: Any) -> dict[str, Any]:
    if summary["stopped"] is None:
        summary["stopped"] = "error"
    summary["wall_s"] = round(time.monotonic() - started, 3)
    text = scrub(json.dumps(summary, indent=2, sort_keys=True, default=str))
    (log / "summary.json").write_text(text + "\n", encoding="utf-8")
    return json.loads(text)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def read_api_key(*, env_name: str | None = None,
                 file: str | Path | None = None) -> str | None:
    """The API key from an environment variable name or a file (stripped)."""
    if env_name and file:
        raise ValueError("give either an env var name or a key file, not both")
    if env_name:
        value = os.environ.get(env_name)
        if not value:
            raise ValueError(f"environment variable {env_name} is not set")
        return value.strip()
    if file:
        value = Path(file).read_text(encoding="utf-8").strip()
        if not value:
            raise ValueError(f"key file {file} is empty")
        return value
    return None


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m ozagia.harness.native",
                                description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="command", required=True)
    r = sub.add_parser("run", help="run the agent loop on a workdir")
    r.add_argument("--workdir", required=True)
    r.add_argument("--prompt-file", required=True)
    r.add_argument("--mode", required=True, choices=MODES)
    r.add_argument("--base-url", required=True,
                   help="OpenAI-compatible API root, e.g. http://host:port/v1")
    r.add_argument("--model", required=True)
    key = r.add_mutually_exclusive_group()
    key.add_argument("--api-key-env", default=None,
                     help="name of the environment variable holding the key")
    key.add_argument("--api-key-file", default=None,
                     help="file holding the key")
    r.add_argument("--dump", default=None,
                   help="graph snapshot (graph mode; built there if missing)")
    r.add_argument("--log-dir", default=None)
    r.add_argument("--max-steps", type=int, default=MAX_STEPS)
    r.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S,
                   help="wall-clock limit in seconds")
    r.add_argument("--temperature", type=float, default=None)
    r.add_argument("--system-prompt-file", default=None)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    workdir = Path(args.workdir).resolve()
    # ozagia.core.memory reads its layout from $OZAGIA_ROOT on import.
    os.environ["OZAGIA_ROOT"] = str(workdir)
    try:
        api_key = read_api_key(env_name=args.api_key_env, file=args.api_key_file)
        prompt = Path(args.prompt_file).read_text(encoding="utf-8")
        system = (Path(args.system_prompt_file).read_text(encoding="utf-8")
                  if args.system_prompt_file else None)
    except (OSError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    summary = run(workdir, prompt, mode=args.mode, base_url=args.base_url,
                  model=args.model, api_key=api_key, max_steps=args.max_steps,
                  timeout_s=args.timeout, temperature=args.temperature,
                  system_prompt=system, log_dir=args.log_dir, dump=args.dump)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return int(summary["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
