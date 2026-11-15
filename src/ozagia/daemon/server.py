"""The graph daemon: `ozagia.intent` behind the Unix socket protocol.

`serve(root)` loads the snapshot (building it when missing), listens on
`protocol.socket_path(root)` and answers the protocol's JSON-line requests
until `shutdown`, SIGTERM or SIGINT. Connections are served by threads; one
global lock serialises `tool` and `after`, because `DevMemory` is not
thread-safe. `ping`, `hello` and `stats` do not take that lock.

Every request is journaled, one JSON object per line, to
`<state dir>/daemon_journal.jsonl` (or `journal`):

    {"ts", "op", "tool", "intent", "handled", "is_error", "ms", "keys" (at
     most 20), "reason", "session", "harness"}

The socket is created mode 0600. A socket file left at the path by a daemon
that no longer answers `ping` is replaced; a live daemon or a non-socket
file at the path is an error.
"""
from __future__ import annotations

import json
import os
import signal
import socketserver
import stat
import sys
import threading
import time
from pathlib import Path
from typing import Any

from ozagia.daemon import protocol as P

JOURNAL_NAME = "daemon_journal.jsonl"
# Keys kept per journal record.
JOURNAL_KEYS = 20
STALE_PING_TIMEOUT_S = 1.0
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


class DaemonState:
    """What the request handlers share: the graph, the lock, the counters."""

    def __init__(self, root: Path, memory: Any, journal: Path) -> None:
        self.root = root
        self.memory = memory
        self.journal = journal
        self.lock = threading.Lock()          # serialises handle/after
        self._stats_lock = threading.Lock()
        self.started = time.time()
        self.by_op: dict[str, int] = {}
        self.by_tool: dict[str, int] = {}
        self.by_intent: dict[str, int] = {}
        self.handled = {"true": 0, "false": 0}
        self.errors = 0          # ok: false responses
        self.tool_errors = 0     # handled, is_error
        self.fallbacks = 0       # tool requests not handled or failed
        self.hellos: list[dict[str, Any]] = []
        self.ms_total = 0.0
        self.ms_max = 0.0
        self.ms_count = 0
        self.server: socketserver.BaseServer | None = None

    # -- requests ------------------------------------------------------------ #
    def dispatch(self, msg: dict[str, Any]) -> dict[str, Any]:
        from ozagia import intent
        op = msg.get("op")
        if op == "ping":
            return {"ok": True, "version": P.PROTOCOL_VERSION,
                    "root": str(self.root), "pid": os.getpid()}
        if op == "hello":
            with self._stats_lock:
                self.hellos.append({
                    "ts": time.time(), "harness": msg.get("harness"),
                    "plugin": msg.get("plugin"), "version": msg.get("version"),
                    "session": msg.get("session")})
            return {"ok": True}
        if op == "stats":
            return {"ok": True, "stats": self.stats()}
        if op == "shutdown":
            return {"ok": True}
        if op in ("tool", "after"):
            tool = msg.get("tool")
            if not isinstance(tool, str) or not tool:
                return {"ok": False, "error": "tool is required"}
            inp = msg.get("input")
            if inp is None:
                inp = {}
            harness = str(msg.get("harness") or "opencode")
            session = msg.get("session")
            with self.lock:
                if op == "tool":
                    body = intent.handle(self.memory, tool, inp,
                                         harness=harness, session=session)
                else:
                    body = intent.after(self.memory, tool, inp,
                                        msg.get("output"), harness=harness,
                                        session=session)
            return {"ok": True, **body}
        return {"ok": False, "error": f"unknown op {op!r}"}

    def record(self, msg: dict[str, Any], resp: dict[str, Any],
               ms: float) -> None:
        op = str(msg.get("op"))
        meta = resp.get("meta") or {}
        tool = msg.get("tool") if op in ("tool", "after") else None
        handled = resp.get("handled") if op == "tool" else None
        with self._stats_lock:
            self.by_op[op] = self.by_op.get(op, 0) + 1
            if tool:
                self.by_tool[str(tool)] = self.by_tool.get(str(tool), 0) + 1
            if meta.get("intent"):
                it = str(meta["intent"])
                self.by_intent[it] = self.by_intent.get(it, 0) + 1
            if handled is not None:
                self.handled["true" if handled else "false"] += 1
            if not resp.get("ok"):
                self.errors += 1
            if resp.get("is_error"):
                self.tool_errors += 1
            if op == "tool" and (not resp.get("ok") or not handled):
                self.fallbacks += 1
            if op in ("tool", "after"):
                self.ms_total += ms
                self.ms_count += 1
                self.ms_max = max(self.ms_max, ms)
            rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "op": op,
                   "tool": tool, "intent": meta.get("intent"),
                   "handled": handled, "is_error": resp.get("is_error"),
                   "ms": round(ms, 1),
                   "keys": list(meta.get("keys") or [])[:JOURNAL_KEYS],
                   "reason": resp.get("reason") or resp.get("error"),
                   "session": msg.get("session"),
                   "harness": msg.get("harness")}
            try:
                self.journal.parent.mkdir(parents=True, exist_ok=True)
                with self.journal.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(rec, sort_keys=True) + "\n")
            except OSError as e:
                print(f"journal write failed: {e}", file=sys.stderr, flush=True)

    def stats(self) -> dict[str, Any]:
        with self._stats_lock:
            return {
                "root": str(self.root), "pid": os.getpid(),
                "uptime_s": round(time.time() - self.started, 1),
                "by_op": dict(self.by_op), "by_tool": dict(self.by_tool),
                "by_intent": dict(self.by_intent),
                "handled": dict(self.handled),
                "fallbacks": self.fallbacks, "errors": self.errors,
                "tool_errors": self.tool_errors,
                "hellos": len(self.hellos),
                "hello_sessions": [h.get("session") for h in self.hellos],
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


def serve(root: str | Path, *, dump: str | Path | None = None,
          journal: str | Path | None = None, sock: str | Path | None = None,
          build_if_missing: bool = True, ready_stream: Any = None) -> int:
    """Run the daemon until shutdown; returns an exit code."""
    root_p = Path(root).resolve()
    from ozagia.config import state_path
    sock_p = Path(sock) if sock else P.socket_path(root_p)
    journal_p = Path(journal) if journal else state_path(root_p, JOURNAL_NAME)
    if len(os.fsencode(str(sock_p))) > SUN_PATH_MAX:
        raise DaemonStartError(f"socket path too long: {sock_p}")
    # Refuse a live daemon before the (slow) snapshot load.
    sock_p.parent.mkdir(parents=True, exist_ok=True)
    _clear_stale(sock_p)
    memory = load_memory(root_p, dump, build_if_missing=build_if_missing)
    # Rewriting the snapshot after every reingest costs ~1 s on a large repo;
    # the daemon keeps the graph in memory and writes it once on exit.
    memory.defer_writes = True
    state = DaemonState(root_p, memory, journal_p)
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
    try:
        print(f"ready {sock_p}", file=out, flush=True)
        server.serve_forever(poll_interval=0.2)
    finally:
        server.server_close()
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


__all__ = ["DaemonStartError", "DaemonState", "JOURNAL_NAME", "load_memory",
           "serve"]
