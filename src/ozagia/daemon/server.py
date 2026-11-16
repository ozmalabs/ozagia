"""The graph daemon: `ozagia.intent` behind the Unix socket protocol.

`serve(root)` loads the snapshot (building it when missing), listens on
`protocol.socket_path(root)` and answers the protocol's JSON-line requests
until `shutdown`, SIGTERM or SIGINT, or, with `parent_pid`, until that
process is gone (checked every `PARENT_POLL_S` seconds). Connections are
served by threads; one global lock serialises `tool` and `after`, because
`DevMemory` is not thread-safe. `ping`, `hello` and `stats` do not take that
lock, and neither does a captured pytest (`shell` that classifies as
`pytest`): it reads no graph state, so a long test run does not hold up the
edits and reads sent meanwhile.

A `tool` or `after` request whose `deadline_ms` (Unix epoch, ms) has passed
when its turn comes (after the lock is taken, before the handler runs) is
answered `expired` and not executed (see `ozagia.daemon.protocol`).

Captured pytest runs with `test_env` (default: this process's environment
without `OZAGIA_*`, `PYTHONPATH` and `COLUMNS`, the variables the harness
adds for ozagia itself), passed to the intent layer as `_test_env`. Reserved
`_` fields a client puts in `input` are dropped.

Every request is journaled, one JSON object per line, to
`<state dir>/daemon_journal.jsonl` (or `journal`):

    {"ts", "op", "tool", "intent", "handled", "is_error", "expired", "ms",
     "keys" (at most 20), "reason", "fallback", "session", "harness"}

With `idle_timeout` (seconds), the daemon shuts down once no request has
arrived for that long, none is in progress and no background job runs (the
idle time then counts from the last job's end; checked every `IDLE_POLL_S`
seconds at most).

Background jobs (`ozagia.daemon.jobs`): the ops `job_start`, `job_wait`,
`job_list`, `job_log`, `job_stop`, `job_events` and `job_ps` go to the job table and
take no graph lock (a `job_wait` blocks only its own connection). When the
daemon exits, for whatever reason, it stops every job it started (SIGTERM
to the job's process group, SIGKILL after `jobs.SHUTDOWN_GRACE_S`) except
those started with `detach`, which it leaves running and names in its log.
A captured `shell` that is a `pgrep`/`ps -p` form about a job's process is
answered from /proc and the job table (`jobs.capture_ps`, intent
`shell_ps`), and `after` for `shell` adds the job hints
(`JobTable.shell_hints`). `python -m ozagia.daemon ensure` starts
daemons this way, so ones started by hooks don't linger.

`stats` counts (`DaemonState.stats`): `by_harness` (requests that name a
harness, by harness); `handled` {"true", "false"} over
`tool` requests; `passthrough` (handled false by design, not expired);
`expired` (`tool` requests dropped by their deadline; `after_expired` for
`after`); `late` (handled, but finished after the deadline, so the plugin
had likely given up); `plugin_fallbacks` (`after` requests carrying
`meta.fallback`: the plugin ran the built-in, or gave up, because the
daemon failed or timed out; `unknown_outcomes` of them carry
"unknown_outcome"); `fallbacks` = `plugin_fallbacks`, once per call: every
plugin fallback (daemon error, timeout, `ok: false`, unknown outcome) sends
exactly one such `after`. `expired` and `tool_failures` are the daemon-side
view of some of the same calls and are reported separately. `shell` counts
the `shell` tool calls the daemon answered (not expired, not preflights):
`captured` (handled), `refused` (ref-moving git) and `passthrough`, with
`capture_rate` = captured / (captured + passthrough), `by_intent` for the
captured ones, and the 30 most common `passthrough_reasons` (the
classifier's `... is not mapped` reasons; 200 distinct ones are kept, the
rest count as "(other)").

The socket is created mode 0600. A socket file left at the path by a daemon
that no longer answers `ping` is replaced; a live daemon or a non-socket
file at the path is an error.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import signal
import socketserver
import stat
import sys
import threading
import time
from pathlib import Path
from typing import Any

from ozagia.daemon import jobs as J
from ozagia.daemon import protocol as P

JOURNAL_NAME = "daemon_journal.jsonl"
# Keys kept per journal record.
JOURNAL_KEYS = 20
# Distinct shell passthrough reasons kept, and shown in `stats`.
SHELL_REASONS = 200
SHELL_REASONS_SHOWN = 30

# Passthrough reasons that name a file or directory, rewritten to one key
# each so paths do not fill the reason table: (pattern, replacement), in
# order.
_REASON_PATHS = [
    (re.compile(r"^(cannot read|cannot stat|find -newer|find traversal failed|"
                r"ls directory cannot be read)\b.*", re.S), r"\1 <path>"),
    (re.compile(r"^(no such file|no such path|not a directory|not a regular "
                r"file|find root does not exist): .*", re.S), r"\1: <path>"),
    (re.compile(r"^(cd:|git -C|workdir) .*?( is not a directory|: not a "
                r"directory| is outside the project root)$", re.S),
     r"\1 <path>\2"),
    (re.compile(r"^.*?( is binary| is larger than \d+ bytes| is not UTF-8|"
                r" is outside the project root| has no interpreter line|"
                r" is not on PATH| is not an executable)$", re.S),
     r"<path>\1"),
    (re.compile(r"^\S+(: interpreter( line)? ).*", re.S), r"<path>\1..."),
]


def shell_reason_key(reason: str) -> str:
    """A passthrough reason with any file path in it replaced by `<path>`,
    for counting."""
    for rx, repl in _REASON_PATHS:
        if rx.match(reason):
            return rx.sub(repl, reason, count=1)
    return reason
STALE_PING_TIMEOUT_S = 1.0
# How often a daemon started with `parent_pid` checks that its parent lives.
PARENT_POLL_S = 2.0
# The longest wait between idle checks.
IDLE_POLL_S = 5.0
# Variables the harness sets for ozagia's own processes, not for the agent's
# test runs.
_TOOL_ONLY_ENV = ("PYTHONPATH", "COLUMNS")
# The longest path an AF_UNIX socket address holds on Linux.
SUN_PATH_MAX = 107


class DaemonStartError(RuntimeError):
    """The daemon cannot start (socket in use, bad path)."""


def _ensure_root(root: Path) -> None:
    """`ozagia.core.memory` reads its layout from `$OZAGIA_ROOT` when it is
    first imported; point it at `root` before that happens."""
    if "ozagia.core.memory" not in sys.modules:
        os.environ["OZAGIA_ROOT"] = str(root)
        return
    from ozagia.core import memory as M
    if Path(M.ROOT).resolve() != root:
        print(f"warning: ozagia.core.memory was configured for {M.ROOT}, "
              f"not {root}", file=sys.stderr, flush=True)


def load_memory(root: Path, dump: str | Path | None, *,
                build_if_missing: bool = True):
    _ensure_root(root)
    from ozagia.config import state_path
    from ozagia.core.memory import DevMemory, build_index
    if dump:
        dump_p = Path(dump)
        mem_journal = dump_p.parent / "journal.jsonl"
    else:
        dump_p = state_path(root, "graph.json")
        mem_journal = state_path(root, "journal.jsonl")
    if not dump_p.exists():
        if not build_if_missing:
            raise FileNotFoundError(f"no snapshot at {dump_p}")
        build_index(root, dump_p)
    return DevMemory(dump_p, mem_journal, root=root)


def captured_test_env(environ: dict[str, str] | None = None) -> dict[str, str]:
    """The environment for captured pytest runs: `environ` (default this
    process's) without `OZAGIA_*`, `PYTHONPATH` and `COLUMNS`."""
    src = os.environ if environ is None else environ
    return {k: v for k, v in src.items()
            if not k.startswith("OZAGIA_") and k not in _TOOL_ONLY_ENV}


def _last(hellos: list[dict[str, Any]], field: str) -> Any:
    """`field` of the last hello that has it, or None."""
    for h in reversed(hellos):
        if h.get(field) is not None:
            return h[field]
    return None


def _expired(msg: dict[str, Any]) -> bool:
    deadline = msg.get("deadline_ms")
    if isinstance(deadline, bool) or not isinstance(deadline, (int, float)):
        return False
    return time.time() * 1000 > deadline


def _lock_free(op: Any, tool: Any, inp: Any) -> bool:
    """A captured pytest reads no graph state, so it skips the lock. So
    does a compound command with a pytest step: it takes the lock itself
    (`_lock`) for the steps that read the graph."""
    if op != "tool" or tool != "shell" or not isinstance(inp, dict):
        return False
    from ozagia.intent.shell import classify, compound_steps
    cls = classify(str(inp.get("command") or ""))
    if cls["class"] == "compound":
        return any(s["kind"] == "pytest" for s in compound_steps(cls))
    return cls["class"] == "pytest"


class DaemonState:
    """What the request handlers share: the graph, the lock, the counters."""

    def __init__(self, root: Path, memory: Any, journal: Path, *,
                 test_env: dict[str, str] | None = None,
                 idle_timeout: float | None = None) -> None:
        self.root = root
        self.idle_timeout = idle_timeout if idle_timeout and idle_timeout > 0 else None
        self.memory = memory
        self.journal = journal
        self.test_env = test_env
        self.lock = threading.Lock()          # serialises handle/after
        self._stats_lock = threading.Lock()
        self.started = time.time()
        self.by_op: dict[str, int] = {}
        self.by_tool: dict[str, int] = {}
        self.by_intent: dict[str, int] = {}
        self.by_harness: dict[str, int] = {}
        self.active = 0          # requests in progress
        self.last_activity = time.monotonic()
        self.idle_shutdown = False
        self.handled = {"true": 0, "false": 0}
        self.errors = 0          # ok: false responses
        self.tool_errors = 0     # handled, is_error
        self.passthrough = 0     # tool: handled false by design
        self.expired = 0         # tool: dropped by deadline_ms
        self.after_expired = 0   # after: dropped by deadline_ms
        self.late = 0            # tool: handled, finished after deadline_ms
        self.plugin_fallbacks = 0  # after with meta.fallback
        self.unknown_outcomes = 0  # ... whose fallback is "unknown_outcome"
        self.tool_failures = 0   # tool: ok false
        self.preflights = 0      # tool: preflight answered "permission required"
        # shell tool calls: captured (handled), refused (git refs) and
        # passthrough, with the passthrough reasons, so an eval can measure
        # the shell capture rate.
        self.shell = {"captured": 0, "refused": 0, "passthrough": 0}
        self.shell_intents: dict[str, int] = {}
        self.shell_reasons: dict[str, int] = {}
        self.hellos: list[dict[str, Any]] = []
        self.ms_total = 0.0
        self.ms_max = 0.0
        self.ms_count = 0
        self.server: socketserver.BaseServer | None = None
        self.jobs = J.JobTable(root, base_env=test_env)

    # -- requests ------------------------------------------------------------ #
    def dispatch(self, msg: dict[str, Any]) -> dict[str, Any]:
        from ozagia import intent
        op = msg.get("op")
        if op == "ping":
            return {"ok": True, "version": P.PROTOCOL_VERSION,
                    "root": str(self.root), "pid": os.getpid()}
        if op == "hello":
            tools = msg.get("tools") if isinstance(msg.get("tools"), dict) else None
            with self._stats_lock:
                self.hellos.append({
                    "ts": time.time(), "harness": msg.get("harness"),
                    "plugin": msg.get("plugin"), "version": msg.get("version"),
                    "session": msg.get("session"), "phase": msg.get("phase"),
                    "tools": tools,
                    "permission_assert": msg.get("permission_assert")})
            return {"ok": True}
        if op == "stats":
            return {"ok": True, "stats": self.stats()}
        if op == "shutdown":
            return {"ok": True}
        if op in J.OPS:
            return self.jobs.dispatch(op, msg)
        if op == "hint":
            prompt = msg.get("prompt")
            history = msg.get("history", [])
            if not isinstance(prompt, str) or len(prompt) > 12000:
                return {"ok": False, "error": "prompt must be a string of at most 12000 characters"}
            if (not isinstance(history, list) or len(history) > 6
                    or any(not isinstance(item, dict)
                           or item.get("role") not in ("user", "assistant")
                           or not isinstance(item.get("text"), str)
                           or len(item["text"]) > 2000 for item in history)):
                return {"ok": False, "error": "history must contain at most six bounded user/assistant messages"}
            with self.lock:
                if _expired(msg):
                    return {"ok": True, "context": "", "expired": True,
                            "meta": {"intent": "hint_expired", "keys": []}}
                from ozagia.core.proactive_context import build_context
                try:
                    hint = build_context(self.memory, prompt,
                                         history=[item["text"] for item in history],
                                         budget_tokens=400, max_history_turns=2)
                    infra = hint.get("infra") or {}
                    result = {"context": hint.get("context", ""),
                              "meta": {
                                  "intent": "hint",
                                  "keys": hint.get("keys", []),
                                  "freshness": hint.get("freshness", "unknown"),
                                  "truncated": hint.get("truncated", False),
                                  "estimated_tokens": hint.get("estimated_tokens"),
                                  "token_estimate_method": hint.get(
                                      "token_estimate_method"),
                                  "allocation": hint.get("allocation"),
                                  "infra_results": len(infra.get("results") or []),
                                  "infra_edges": len(infra.get("edges") or []),
                              }}
                except Exception as exc:  # a hint must never block the agent
                    return {"ok": True, "context": "", "meta": {
                        "intent": "hint_error", "keys": [], "error": type(exc).__name__}}
            return {"ok": True, **result}
        if op in ("tool", "after"):
            tool = msg.get("tool")
            if not isinstance(tool, str) or not tool:
                return {"ok": False, "error": "tool is required"}
            inp = msg.get("input")
            if inp is None:
                inp = {}
            if isinstance(inp, dict):
                inp = {k: v for k, v in inp.items()
                       if not str(k).startswith("_")}
                if op == "tool" and self.test_env is not None:
                    inp["_test_env"] = dict(self.test_env)
            harness = str(msg.get("harness") or "opencode")
            session = msg.get("session")
            if op == "tool" and tool == "shell" and isinstance(inp, dict):
                captured = self._capture_ps(inp, msg)
                if captured is not None:
                    return captured
            free = _lock_free(op, tool, inp)
            if free and isinstance(inp, dict):
                inp["_lock"] = self.lock
            lock = contextlib.nullcontext() if free else self.lock
            with lock:
                if _expired(msg):
                    if op == "tool":
                        return {"ok": True, "handled": False,
                                "reason": "expired", "expired": True,
                                "meta": {"intent": f"{tool}_expired",
                                         "keys": [], "ms": 0.0}}
                    return {"ok": True, "append": "", "expired": True,
                            "meta": {"intent": "after_expired", "keys": [],
                                     "ms": 0.0}}
                if op == "tool":
                    body = intent.handle(self.memory, tool, inp,
                                         harness=harness, session=session,
                                         preflight=msg.get("preflight") is True)
                else:
                    body = intent.after(self.memory, tool, inp,
                                        msg.get("output"), harness=harness,
                                        session=session)
            if op == "tool" and body.get("handled") and _expired(msg):
                body = {**body, "late": True}
            if op == "after" and tool == "shell" and isinstance(inp, dict):
                hints = self.jobs.shell_hints(str(inp.get("command") or ""),
                                              session)
                if hints:
                    body = {**body, "append": "\n".join(
                        [x for x in [body.get("append") or ""] if x] + hints)}
            return {"ok": True, **self._job_meta(body)}
        return {"ok": False, "error": f"unknown op {op!r}"}

    def _job_meta(self, body: dict[str, Any]) -> dict[str, Any]:
        """`body` with `meta.jobs_running` and `meta.job_seq` while a job
        runs, so a plugin knows to watch for job ends (`job_events`)."""
        n = self.jobs.running_count()
        if not n or not isinstance(body.get("meta"), dict):
            return body
        return {**body, "meta": {**body["meta"], "jobs_running": n,
                                 "job_seq": self.jobs.event_seq}}

    def _capture_ps(self, inp: dict[str, Any], msg: dict[str, Any]
                    ) -> dict[str, Any] | None:
        """The `tool` answer for a `pgrep`/`ps -p` command about a job's
        process (`jobs.capture_ps`), or None."""
        command = inp.get("command")
        if not isinstance(command, str):
            return None
        out = self.jobs.capture_ps(command, count=msg.get("preflight") is not True)
        if out is None:
            return None
        from ozagia import intent
        perm = intent.permission_request(self.memory, "shell", inp)
        meta = {"intent": "shell_ps", "keys": [], "ms": 0.0}
        if msg.get("preflight") is True:
            return {"ok": True, "handled": False, "preflight": True,
                    "reason": "permission required", "permission": perm,
                    "meta": {**meta, "intent": "shell_preflight"}}
        return {"ok": True, **self._job_meta({
            "handled": True, "output": out, "is_error": False,
            "permission": perm, "meta": meta})}

    def begin(self) -> None:
        """A request arrived (for the idle timeout)."""
        with self._stats_lock:
            self.active += 1
            self.last_activity = time.monotonic()

    def end(self) -> None:
        with self._stats_lock:
            self.active -= 1
            self.last_activity = time.monotonic()

    def idle_for(self) -> float:
        """Seconds since the last request ended or the last job exited, or
        0 while a request or a job runs."""
        if self.jobs.running_count():
            return 0.0
        with self._stats_lock:
            if self.active:
                return 0.0
            return time.monotonic() - max(self.last_activity, self.jobs.last_end)

    def record(self, msg: dict[str, Any], resp: dict[str, Any],
               ms: float) -> None:
        op = str(msg.get("op"))
        meta = resp.get("meta") or {}
        tool = msg.get("tool") if op in ("tool", "after") else None
        handled = resp.get("handled") if op == "tool" else None
        expired = bool(resp.get("expired"))
        req_meta = msg.get("meta") if isinstance(msg.get("meta"), dict) else {}
        fallback = req_meta.get("fallback") if op == "after" else None
        with self._stats_lock:
            self.by_op[op] = self.by_op.get(op, 0) + 1
            if tool:
                self.by_tool[str(tool)] = self.by_tool.get(str(tool), 0) + 1
            harness = msg.get("harness")
            if isinstance(harness, str) and harness:
                self.by_harness[harness] = self.by_harness.get(harness, 0) + 1
            if meta.get("intent"):
                it = str(meta["intent"])
                self.by_intent[it] = self.by_intent.get(it, 0) + 1
            if handled is not None:
                self.handled["true" if handled else "false"] += 1
            if not resp.get("ok"):
                self.errors += 1
            if resp.get("is_error"):
                self.tool_errors += 1
            if op == "tool":
                if not resp.get("ok"):
                    self.tool_failures += 1
                elif expired:
                    self.expired += 1
                elif resp.get("preflight"):
                    self.preflights += 1
                elif not handled:
                    self.passthrough += 1
                elif resp.get("late"):
                    self.late += 1
            if (op == "tool" and tool == "shell" and resp.get("ok")
                    and not expired and not resp.get("preflight")):
                it = str(meta.get("intent") or "")
                if not handled:
                    self.shell["passthrough"] += 1
                    reason = shell_reason_key(
                        str(resp.get("reason") or ""))[:120]
                    if (reason in self.shell_reasons
                            or len(self.shell_reasons) < SHELL_REASONS):
                        self.shell_reasons[reason] = (
                            self.shell_reasons.get(reason, 0) + 1)
                    else:
                        self.shell_reasons["(other)"] = (
                            self.shell_reasons.get("(other)", 0) + 1)
                elif it == "shell_refused":
                    self.shell["refused"] += 1
                else:
                    self.shell["captured"] += 1
                    self.shell_intents[it] = self.shell_intents.get(it, 0) + 1
            if op == "after":
                if expired:
                    self.after_expired += 1
                if fallback:
                    self.plugin_fallbacks += 1
                    if fallback == "unknown_outcome":
                        self.unknown_outcomes += 1
            if op in ("tool", "after"):
                self.ms_total += ms
                self.ms_count += 1
                self.ms_max = max(self.ms_max, ms)
            rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "op": op,
                   "tool": tool, "intent": meta.get("intent"),
                   "handled": handled, "is_error": resp.get("is_error"),
                   "expired": expired or None, "ms": round(ms, 1),
                   "keys": list(meta.get("keys") or [])[:JOURNAL_KEYS],
                   "reason": resp.get("reason") or resp.get("error"),
                   "fallback": fallback,
                   "session": msg.get("session"),
                   "harness": msg.get("harness")}
            try:
                self.journal.parent.mkdir(parents=True, exist_ok=True)
                with self.journal.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(rec, sort_keys=True) + "\n")
            except OSError as e:
                print(f"journal write failed: {e}", file=sys.stderr, flush=True)

    def stats(self) -> dict[str, Any]:
        job_stats = self.jobs.stats()
        with self._stats_lock:
            return {
                "root": str(self.root), "pid": os.getpid(),
                "uptime_s": round(time.time() - self.started, 1),
                "by_op": dict(self.by_op), "by_tool": dict(self.by_tool),
                "by_intent": dict(self.by_intent),
                "by_harness": dict(self.by_harness),
                "idle_timeout_s": self.idle_timeout,
                "handled": dict(self.handled),
                "passthrough": self.passthrough,
                "expired": self.expired,
                "after_expired": self.after_expired,
                "late": self.late,
                "plugin_fallbacks": self.plugin_fallbacks,
                "unknown_outcomes": self.unknown_outcomes,
                "fallbacks": self.plugin_fallbacks,
                "errors": self.errors,
                "tool_errors": self.tool_errors,
                "hellos": len(self.hellos),
                "hello_sessions": [h.get("session") for h in self.hellos
                                   if h.get("session")],
                "preflights": self.preflights,
                "shell": {
                    **self.shell,
                    "capture_rate": (
                        round(self.shell["captured"]
                              / (self.shell["captured"] + self.shell["passthrough"]), 4)
                        if self.shell["captured"] + self.shell["passthrough"]
                        else None),
                    "by_intent": dict(self.shell_intents),
                    "passthrough_reasons": dict(sorted(
                        self.shell_reasons.items(),
                        key=lambda kv: (-kv[1], kv[0]))[:SHELL_REASONS_SHOWN]),
                },
                # Structured data reads (`json:`/`yaml:`/`toml:` queries),
                # outlines of large structured files, captured `jq` calls.
                "structured_queries": self.by_intent.get("read_structured", 0),
                "structured_outlines": self.by_intent.get(
                    "read_structured_outline", 0),
                "jq_captures": self.by_intent.get("shell_jq", 0),
                # Background jobs (`ozagia.daemon.jobs`): started, waits and
                # the seconds spent in them, and waits that returned new
                # output or the job's end (each one a poll the agent did not
                # make); `jobs` has the rest of the job counters.
                **{k: job_stats[k] for k in ("jobs_started", "waits",
                                            "wait_seconds", "polls_avoided")},
                "jobs": job_stats,
                "permission_assert": _last(self.hellos, "permission_assert"),
                "codemode_tools": sorted({
                    name for h in self.hellos
                    for name, info in (h.get("tools") or {}).items()
                    if isinstance(info, dict) and info.get("codemode") is not False}),
                "mean_ms": (round(self.ms_total / self.ms_count, 1)
                            if self.ms_count else 0.0),
                "max_ms": round(self.ms_max, 1),
                "journal": str(self.journal),
            }

    def shutdown_later(self) -> None:
        if self.server is not None:
            threading.Thread(target=self.server.shutdown, daemon=True).start()


def _encode(resp: dict[str, Any]) -> bytes:
    data = json.dumps(resp, ensure_ascii=False).encode("utf-8") + b"\n"
    if len(data) <= P.MAX_MESSAGE_BYTES:
        return data
    small: dict[str, Any] = {"id": resp.get("id"), "ok": True}
    if "append" in resp:
        small.update(append="", meta=resp.get("meta") or {})
    elif "handled" in resp:
        small.update(handled=False, reason="response exceeds MAX_MESSAGE_BYTES",
                     meta=resp.get("meta") or {})
    else:
        small = {"id": resp.get("id"), "ok": False,
                 "error": "response exceeds MAX_MESSAGE_BYTES"}
    return json.dumps(small, ensure_ascii=False).encode("utf-8") + b"\n"


class _Handler(socketserver.StreamRequestHandler):
    server: "_Server"

    def handle(self) -> None:
        state = self.server.state
        while True:
            try:
                line = self.rfile.readline(P.MAX_MESSAGE_BYTES + 1)
            except (ConnectionError, OSError):
                return
            if not line:
                return
            if len(line) > P.MAX_MESSAGE_BYTES:
                self._send({"id": None, "ok": False,
                            "error": f"message exceeds {P.MAX_MESSAGE_BYTES} "
                                     f"bytes; connection closed"})
                state.record({"op": "oversized"}, {"ok": False,
                                                   "error": "oversized"}, 0.0)
                return
            if not line.strip():
                continue
            t0 = time.perf_counter()
            msg: Any = None
            state.begin()
            try:
                msg = json.loads(line)
            except (ValueError, UnicodeDecodeError) as e:
                resp = {"id": None, "ok": False, "error": f"bad JSON: {e}"}
            else:
                if not isinstance(msg, dict):
                    resp = {"id": None, "ok": False,
                            "error": "a request must be a JSON object"}
                else:
                    try:
                        resp = {"id": msg.get("id"), **state.dispatch(msg)}
                    except Exception as e:  # noqa: BLE001 - reported
                        resp = {"id": msg.get("id"), "ok": False,
                                "error": f"{type(e).__name__}: {e}"}
            finally:
                state.end()
            ms = (time.perf_counter() - t0) * 1000
            state.record(msg if isinstance(msg, dict) else {"op": "invalid"},
                         resp, ms)
            if not self._send(resp):
                return
            if isinstance(msg, dict) and msg.get("op") == "shutdown":
                state.shutdown_later()
                return

    def _send(self, resp: dict[str, Any]) -> bool:
        try:
            self.wfile.write(_encode(resp))
            self.wfile.flush()
            return True
        except (ConnectionError, OSError):
            return False


class _Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, path: str, state: DaemonState) -> None:
        self.state = state
        super().__init__(path, _Handler)

    def server_bind(self) -> None:
        old = os.umask(0o177)
        try:
            super().server_bind()
        finally:
            os.umask(old)


def _clear_stale(sock: Path) -> None:
    """Remove a socket file no daemon answers on; refuse anything else."""
    try:
        st = os.lstat(sock)
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(st.st_mode):
        raise DaemonStartError(f"{sock} exists and is not a socket")
    from ozagia.daemon.client import DaemonClient, DaemonError
    try:
        with DaemonClient(sock, timeout=STALE_PING_TIMEOUT_S) as c:
            c.ping()
    except DaemonError:
        sock.unlink()
        return
    raise DaemonStartError(f"a daemon already answers on {sock}")


def _take_lock(root: Path, sock: Path, lock_fd: int | None) -> int:
    """Hold the daemon lock (`ozagia.daemon.autostart.lock_path`) for this
    process's life and return its descriptor: `lock_fd` when the starter
    handed over the locked file, else a new one. Raise `DaemonStartError`
    when another process holds it (a daemon loading, running or writing its
    snapshot on the way out)."""
    import fcntl

    from ozagia.daemon.autostart import lock_path
    lock_p = lock_path(root, sock)
    if lock_fd is None:
        lock_p.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock_p, os.O_RDWR | os.O_CREAT, 0o600)
    else:
        fd = lock_fd
    try:
        # On an inherited descriptor this succeeds: the lock is on the
        # shared open file, which the starter already locked.
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        try:
            holder = os.pread(fd, 32, 0).decode(errors="replace").strip()
        except OSError:
            holder = ""
        if lock_fd is None:
            os.close(fd)
        # Only ask; the holder's socket is its own to remove.
        from ozagia.daemon.client import DaemonClient, DaemonError
        try:
            with DaemonClient(sock, timeout=STALE_PING_TIMEOUT_S) as c:
                c.ping()
        except DaemonError:
            pass
        else:
            raise DaemonStartError(f"a daemon already answers on {sock}") from None
        pid = f" (pid {holder})" if holder.isdigit() else ""
        raise DaemonStartError(
            f"another daemon for {root} holds {lock_p}{pid}: it is loading, "
            "running or shutting down; not starting a second one") from None
    os.ftruncate(fd, 0)
    os.pwrite(fd, f"{os.getpid()}\n".encode(), 0)
    return fd


def _parent_gone(parent_pid: int) -> bool:
    if os.getppid() != parent_pid:
        return True
    try:
        os.kill(parent_pid, 0)
    except OSError:
        return True
    return False


def _watch_parent(state: DaemonState, parent_pid: int,
                  stop: threading.Event) -> None:
    while not stop.wait(PARENT_POLL_S):
        if _parent_gone(parent_pid):
            print(f"parent {parent_pid} is gone; shutting down",
                  file=sys.stderr, flush=True)
            state.shutdown_later()
            return


def _watch_idle(state: DaemonState, stop: threading.Event) -> None:
    limit = state.idle_timeout
    assert limit is not None
    poll = min(IDLE_POLL_S, max(limit / 4, 0.05))
    while not stop.wait(poll):
        if state.idle_for() >= limit:
            print(f"idle for {limit:g} s; shutting down", file=sys.stderr, flush=True)
            state.idle_shutdown = True
            state.shutdown_later()
            return


def serve(root: str | Path, *, dump: str | Path | None = None,
          journal: str | Path | None = None, sock: str | Path | None = None,
          build_if_missing: bool = True, ready_stream: Any = None,
          parent_pid: int | None = None,
          idle_timeout: float | None = None, lock_fd: int | None = None) -> int:
    """Run the daemon until shutdown; returns an exit code. With
    `parent_pid`, it also shuts down once that process is no longer its
    parent or no longer exists; with `idle_timeout`, once no request has
    come for that many seconds.

    The daemon holds its lock (`_take_lock`; `lock_fd` is a locked
    descriptor handed over by `ozagia.daemon.autostart.ensure`) from before
    the snapshot load until it exits, and refuses to start when another
    process holds it."""
    root_p = Path(root).resolve()
    from ozagia.config import state_path
    sock_p = Path(sock) if sock else P.socket_path(root_p)
    journal_p = Path(journal) if journal else state_path(root_p, JOURNAL_NAME)
    if len(os.fsencode(str(sock_p))) > SUN_PATH_MAX:
        raise DaemonStartError(f"socket path too long: {sock_p}")
    # Refuse a live or loading daemon before the (slow) snapshot load.
    sock_p.parent.mkdir(parents=True, exist_ok=True)
    lock = _take_lock(root_p, sock_p, lock_fd)
    try:
        return _serve_locked(root_p, sock_p, journal_p, dump=dump,
                             build_if_missing=build_if_missing,
                             ready_stream=ready_stream, parent_pid=parent_pid,
                             idle_timeout=idle_timeout)
    finally:
        os.close(lock)


def _serve_locked(root_p: Path, sock_p: Path, journal_p: Path, *,
                  dump: str | Path | None, build_if_missing: bool,
                  ready_stream: Any, parent_pid: int | None,
                  idle_timeout: float | None) -> int:
    _clear_stale(sock_p)
    memory = load_memory(root_p, dump, build_if_missing=build_if_missing)
    # Rewriting the snapshot after every reingest costs ~1 s on a large repo;
    # the daemon keeps the graph in memory and writes it once on exit.
    memory.defer_writes = True
    state = DaemonState(root_p, memory, journal_p, test_env=captured_test_env(),
                        idle_timeout=idle_timeout)
    _clear_stale(sock_p)
    server = _Server(str(sock_p), state)
    state.server = server
    inode = os.stat(sock_p).st_ino

    def on_signal(signum: int, _frame: Any) -> None:
        state.shutdown_later()

    previous = {}
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGTERM, signal.SIGINT):
            previous[sig] = signal.signal(sig, on_signal)
    out = ready_stream or sys.stdout
    stop_watch = threading.Event()
    if parent_pid:
        threading.Thread(target=_watch_parent, name="ozagia-parent-watch",
                         args=(state, int(parent_pid), stop_watch),
                         daemon=True).start()
    if state.idle_timeout:
        state.last_activity = time.monotonic()
        threading.Thread(target=_watch_idle, name="ozagia-idle-watch",
                         args=(state, stop_watch), daemon=True).start()
    try:
        print(f"ready {sock_p}", file=out, flush=True)
        server.serve_forever(poll_interval=0.2)
    finally:
        stop_watch.set()
        server.server_close()
        try:
            state.jobs.shutdown()
        except Exception as e:  # noqa: BLE001 - the snapshot still gets written
            print(f"job shutdown failed: {e}", file=sys.stderr, flush=True)
        if getattr(memory, "snapshot_dirty", False):
            try:
                memory.write_snapshot()
                memory.snapshot_dirty = False
            except OSError as e:
                print(f"snapshot write failed: {e}", file=sys.stderr, flush=True)
        try:
            if os.stat(sock_p).st_ino == inode:
                sock_p.unlink()
        except OSError:
            pass
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    return 0


__all__ = ["DaemonStartError", "DaemonState", "IDLE_POLL_S", "JOURNAL_NAME",
           "PARENT_POLL_S",
           "captured_test_env", "load_memory", "serve"]
