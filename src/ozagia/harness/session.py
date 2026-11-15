"""A graph session: a current snapshot and a running graph daemon for one repository.

    with GraphSession(root) as s:
        ...                      # s.sock answers the daemon protocol
        s.stats()
    s.final_stats                # the daemon's stats at shutdown

On enter:

1. The snapshot (`dump`, default `<state dir>/graph.json`) is made current.
   `build="auto"` builds it when missing; when present it asks
   `DevMemory.staleness()` and, if stale, runs `DevMemory.refresh()`. A
   snapshot `refresh` cannot update (version 1, built for another root) is
   rebuilt, and so is one that is still stale after a refresh that
   reingested nothing or left `DevMemory.unhandled_changes()` (changed files
   that are neither indexed nor adapter-owned). `refresh` does not recompute
   git history, so after a HEAD change with files to reingest the report
   says `still_stale`; `build="always"` rebuilds. `build="never"`
   fails when the snapshot is missing and never refreshes. This runs in a
   subprocess with `tool_python` and `OZAGIA_ROOT=root`, because
   `ozagia.core.memory` takes its layout from `$OZAGIA_ROOT` at import.
2. The daemon starts (`python -m ozagia.daemon serve --no-build
   --parent-pid <this pid>`, its own process group) on a short socket path
   in the temp dir; it exits by itself if this process dies. A daemon that does
   not print `ready` within `eval.agent.DAEMON_READY_TIMEOUT_S` is killed and
   `SessionError` is raised.

On exit (also after an exception or Ctrl-C): `stats` is recorded in
`final_stats`, `shutdown` is sent (the daemon writes the snapshot), the
process group is killed if it is still alive, and the socket is removed.
SIGINT is held off during this teardown so a second Ctrl-C cannot cut it short.

Subprocesses get `OZAGIA_ROOT=root`, `PYTHONDONTWRITEBYTECODE=1`, no inherited
`PYTHONPATH` (a target's path must not break the tool), and, when
`tool_python` is this interpreter, `PYTHONPATH` set to the directory this
`ozagia` package was imported from, so they run the same ozagia code.
`config` becomes `OZAGIA_CONFIG`; `state_dir` becomes `OZAGIA_STATE_DIR`.
With `run_python`, a `bin/` shim under `log_dir` whose `python` is
`run_python` goes first on the daemon's PATH (the daemon runs the agent's
shell commands).
"""
from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Iterator

import ozagia
from ozagia.config import ENV_STATE_DIR, state_path
from ozagia.eval import agent as ea

BUILD_MODES = ("auto", "always", "never")
DAEMON_JOURNAL = "daemon_journal.jsonl"

# argv: root, dump, mode. Prints one JSON line, the report.
_PREP_CODE = r"""
import json, sys, time
from pathlib import Path
root, dump, mode = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
from ozagia.core.memory import DevMemory, build_index
t0 = time.monotonic()

def built(reason):
    r = build_index(root, dump)
    return {"action": "built", "reason": reason, "nodes": r["nodes"],
            "edges": r["edges"], "types": r["types"]}

if mode == "always":
    out = built("rebuild requested")
elif not dump.exists():
    if mode == "never":
        print(json.dumps({"error": f"no snapshot at {dump} (build=never)"}))
        sys.exit(3)
    out = built("no snapshot")
else:
    mem = DevMemory(dump, dump.parent / "journal.jsonl", root=root)
    st = mem.staleness()
    if not st["stale"] or mode == "never":
        out = {"action": "stale" if st["stale"] else "fresh",
               "nodes": len(mem.nodes), "edges": len(mem.edges)}
    elif mem.version < 2 or st["root_mismatch"]:
        out = built("snapshot cannot be refreshed (version 1 or another root)")
    else:
        delta = mem.refresh()
        after = mem.staleness()
        unhandled = mem.unhandled_changes() if after["stale"] else []
        if after["stale"] and (not delta.files or unhandled):
            # Nothing refresh could take in (or something it cannot): only a
            # build makes the snapshot current.
            out = built("stale, and refresh reingested nothing"
                        if not delta.files else
                        f"stale after refresh: {len(unhandled)} changed "
                        f"file(s) refresh cannot take in")
        else:
            out = {"action": "refreshed", "files": len(delta.files),
                   "added": len(delta.added), "removed": len(delta.removed),
                   "changed": len(delta.changed), "nodes": len(mem.nodes),
                   "edges": len(mem.edges), "still_stale": bool(after["stale"]),
                   "head_moved": bool(after["head"]
                                      and after["snapshot_head"] != after["head"])}
out["seconds"] = round(time.monotonic() - t0, 3)
out["dump"] = str(dump)
print(json.dumps(out))
"""


class SessionError(RuntimeError):
    """The snapshot could not be prepared or the daemon did not start."""


@contextlib.contextmanager
def no_interrupt() -> Iterator[None]:
    """Hold off SIGINT (main thread only) while cleanup runs; a Ctrl-C that
    arrives meanwhile is re-raised as KeyboardInterrupt afterwards."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    got: list[int] = []
    previous = signal.signal(signal.SIGINT, lambda signum, _frame: got.append(signum))
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous)
    if got and previous not in (signal.SIG_IGN, None):
        raise KeyboardInterrupt


def tool_env(root: str | Path, *, tool_python: str, config: str | Path | None = None,
             state_dir: str | Path | None = None) -> dict[str, str]:
    """Environment for ozagia subprocesses acting on `root` (see module docstring)."""
    env = dict(os.environ, OZAGIA_ROOT=str(Path(root).resolve()),
               PYTHONDONTWRITEBYTECODE="1")
    env.pop("PYTHONPATH", None)
    if os.path.abspath(tool_python) == os.path.abspath(sys.executable):
        env["PYTHONPATH"] = str(Path(ozagia.__file__).resolve().parents[1])
    if config is not None:
        env["OZAGIA_CONFIG"] = str(config)
    else:
        env.pop("OZAGIA_CONFIG", None)
    if state_dir is not None:
        env[ENV_STATE_DIR] = str(state_dir)
    return env


def run_group(argv: list[str], *, cwd: str | Path, env: dict[str, str],
              timeout_s: float | None, stdout: Any, stderr: Any) -> dict[str, Any]:
    """Run argv in its own session and process group; stdin is /dev/null.

    The group is killed (SIGTERM, then SIGKILL after `KILL_GRACE_S`) on
    timeout, on any exception including KeyboardInterrupt (then re-raised),
    and after a normal exit if members are left. Returns {returncode,
    timed_out, seconds}; returncode is None after a timeout."""
    started = time.monotonic()
    env = dict(env, PWD=str(Path(cwd).resolve()))
    proc = subprocess.Popen(argv, cwd=str(cwd), env=env, stdin=subprocess.DEVNULL,
                            stdout=stdout, stderr=stderr, start_new_session=True)
    timed_out = False
    try:
        code: int | None = proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out, code = True, None
    except BaseException:
        with no_interrupt():
            ea._kill_group(proc)
        raise
    ea._kill_group(proc)
    return {"returncode": code, "timed_out": timed_out,
            "seconds": round(time.monotonic() - started, 3)}


def prepare_snapshot(root: str | Path, dump: str | Path, *, build: str = "auto",
                     tool_python: str = sys.executable, env: dict[str, str] | None = None,
                     log_dir: str | Path | None = None,
                     timeout_s: float = ea.PREP_TIMEOUT_S) -> dict[str, Any]:
    """Build or refresh the snapshot (see module docstring). Returns the report:
    {action: built|refreshed|fresh|stale, nodes, edges, seconds, dump, ...}."""
    if build not in BUILD_MODES:
        raise SessionError(f"build must be one of {', '.join(BUILD_MODES)}, not {build!r}")
    root = Path(root).resolve()
    dump = Path(dump)
    env = env if env is not None else tool_env(root, tool_python=tool_python)
    with tempfile.TemporaryDirectory(prefix="ozagia-prep-") as tmp:
        logs = Path(log_dir) if log_dir else Path(tmp)
        logs.mkdir(parents=True, exist_ok=True)
        out_p, err_p = logs / "prep.stdout.txt", logs / "prep.stderr.txt"
        with out_p.open("w", encoding="utf-8") as out, err_p.open("w", encoding="utf-8") as err:
            res = run_group([tool_python, "-c", _PREP_CODE, str(root), str(dump), build],
                            cwd=root, env=env, timeout_s=timeout_s, stdout=out, stderr=err)
        stdout = out_p.read_text(encoding="utf-8", errors="replace")
        stderr = err_p.read_text(encoding="utf-8", errors="replace")
    report: dict[str, Any] = {}
    for line in reversed(stdout.splitlines()):
        if line.startswith("{"):
            try:
                report = json.loads(line)
            except ValueError:
                pass
            break
    if res["timed_out"]:
        raise SessionError(f"snapshot preparation timed out after {timeout_s} s")
    if res["returncode"] != 0 or "error" in report or "action" not in report:
        detail = report.get("error") or ea._tail(stderr, 12) or f"exit {res['returncode']}"
        raise SessionError(f"snapshot preparation failed for {root}: {detail}")
    return report


def _daemon_argv(tool_python: str, root: Path, dump: Path, journal: Path,
                 sock: Path) -> list[str]:
    # --parent-pid: the daemon exits if this process dies without teardown.
    return [tool_python, "-m", "ozagia.daemon", "serve", "--root", str(root),
            "--dump", str(dump), "--journal", str(journal), "--sock", str(sock),
            "--no-build", "--parent-pid", str(os.getpid())]


def start_daemon(root: Path, dump: Path, log_dir: Path, *, tool_python: str,
                 env: dict[str, str]) -> tuple[ea.GraphDaemon, float]:
    """Start the daemon on a fresh socket and wait for `ready`.

    Like `eval.agent.start_daemon`, which fixes the snapshot at
    `<log_dir>/dev_graph.json`; this one serves `dump`. Returns (daemon,
    seconds to ready); raises SessionError after killing a daemon that did
    not get ready."""
    log_dir.mkdir(parents=True, exist_ok=True)
    sock = ea._daemon_sock_path()
    argv = _daemon_argv(tool_python, root, dump, log_dir / DAEMON_JOURNAL, sock)
    env = dict(env, PWD=str(root))
    started = time.monotonic()
    stderr_path = log_dir / "daemon.stderr.txt"
    try:
        with stderr_path.open("w", encoding="utf-8") as stderr:
            proc = subprocess.Popen(argv, cwd=str(root), env=env, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=stderr, text=True,
                                    encoding="utf-8", errors="replace",
                                    start_new_session=True)
    except OSError as exc:
        raise SessionError(f"daemon did not start: {exc}") from exc
    ready = threading.Event()
    seen: dict[str, Any] = {}

    def pump() -> None:
        # Copies the daemon's stdout to the log for its whole life, so the
        # pipe never fills; the first `ready <path>` line marks it ready.
        with (log_dir / "daemon.stdout.txt").open("w", encoding="utf-8") as out:
            for line in proc.stdout:
                out.write(line)
                out.flush()
                if "ready" not in seen and line.startswith("ready "):
                    seen["ready"] = line[len("ready "):].strip()
                    ready.set()
        seen["eof"] = True
        ready.set()

    thread = threading.Thread(target=pump, name="ozagia-session-daemon", daemon=True)
    thread.start()
    try:
        deadline = started + ea.DAEMON_READY_TIMEOUT_S
        while not ready.wait(0.05):
            if proc.poll() is not None:
                ready.wait(0.5)  # let the pump read a last `ready` line, if any
                break
            if time.monotonic() >= deadline:
                break
    except BaseException:
        with no_interrupt():
            ea._kill_group(proc)
            if sock.is_socket():
                sock.unlink()
        raise
    if seen.get("ready"):
        return (ea.GraphDaemon(proc, Path(seen["ready"]), thread),
                round(time.monotonic() - started, 3))
    if proc.poll() is not None:
        error = (f"daemon exited before ready (exit {proc.returncode}): "
                 + ea._tail(stderr_path.read_text(encoding="utf-8", errors="replace"), 12))
    else:
        error = f"daemon not ready after {ea.DAEMON_READY_TIMEOUT_S} s"
    ea._kill_group(proc)
    thread.join(timeout=ea.KILL_GRACE_S)
    if sock.is_socket():
        sock.unlink()
    raise SessionError(f"{error.strip()} (logs: {log_dir})")


class GraphSession:
    """A current snapshot and a running daemon for `root` (see module docstring).

    Attributes after enter: `sock`, `dump`, `prep` (the snapshot report),
    `build_s` (snapshot seconds), `ready_s` (daemon seconds to ready),
    `log_dir`, `env` (the tool env). After exit: `final_stats`,
    `stop_error`."""

    def __init__(self, root: str | Path, *, dump: str | Path | None = None,
                 state_dir: str | Path | None = None, tool_python: str = sys.executable,
                 run_python: str | None = None, build: str = "auto",
                 log_dir: str | Path | None = None,
                 config: str | Path | None = None) -> None:
        if build not in BUILD_MODES:
            raise SessionError(f"build must be one of {', '.join(BUILD_MODES)}, not {build!r}")
        self.root = Path(root).resolve()
        if not self.root.is_dir():
            raise SessionError(f"not a directory: {self.root}")
        self.state_dir: Path | None = None
        if state_dir is not None:
            sd = Path(state_dir)
            self.state_dir = (sd if sd.is_absolute() else self.root / sd).resolve()
        if dump is not None:
            self.dump = Path(dump).resolve()
        elif self.state_dir is not None:
            self.dump = self.state_dir / "graph.json"
        else:
            self.dump = state_path(self.root, "graph.json")
        self.tool_python = tool_python
        self.run_python = run_python
        self.build = build
        self.config = config
        self.log_dir = (Path(log_dir) if log_dir is not None
                        else Path(tempfile.mkdtemp(prefix="ozagia-session-")))
        self.env = tool_env(self.root, tool_python=tool_python, config=config,
                            state_dir=self.state_dir)
        self.daemon: ea.GraphDaemon | None = None
        self.sock: Path | None = None
        self.prep: dict[str, Any] = {}
        self.build_s: float | None = None
        self.ready_s: float | None = None
        self.final_stats: dict[str, Any] | None = None
        self.stop_error: str | None = None

    def __enter__(self) -> "GraphSession":
        self.log_dir.mkdir(parents=True, exist_ok=True)
        if self.run_python:
            self.env["PATH"] = os.pathsep.join(
                [str(ea.python_shim(self.log_dir, self.run_python)), self.env.get("PATH", "")])
        self.prep = prepare_snapshot(self.root, self.dump, build=self.build,
                                     tool_python=self.tool_python, env=self.env,
                                     log_dir=self.log_dir)
        self.build_s = self.prep.get("seconds")
        self.daemon, self.ready_s = start_daemon(self.root, self.dump, self.log_dir,
                                                 tool_python=self.tool_python, env=self.env)
        self.sock = self.daemon.sock
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    @property
    def pid(self) -> int | None:
        """The daemon's pid (also its process group id)."""
        return self.daemon.proc.pid if self.daemon is not None else None

    def stats(self) -> dict[str, Any] | None:
        """The daemon's stats now; after exit, the stats taken at shutdown."""
        if self.daemon is None:
            return self.final_stats
        return ea._daemon_request(self.daemon.sock, {"op": "stats"}).get("stats")

    def close(self) -> None:
        """Record stats, shut the daemon down, kill its group, remove the socket.
        Idempotent."""
        if self.daemon is None:
            return
        with no_interrupt():
            daemon, self.daemon = self.daemon, None
            stopped = daemon.stop()
            self.final_stats = stopped.get("daemon_stats")
            self.stop_error = stopped.get("daemon_stop_error")


__all__ = ["BUILD_MODES", "GraphSession", "SessionError", "no_interrupt",
           "prepare_snapshot", "run_group", "start_daemon", "tool_env"]
