"""Start the graph daemon on demand: `python -m ozagia.daemon ensure`.

`ensure(root)` pings the daemon socket (`protocol.socket_path(root)`). When
nothing answers, it starts `python -m ozagia.daemon serve --root R
--idle-timeout S` detached (a new session, stdin from /dev/null, stdout and
stderr appended to `<state dir>/daemon.log`) and waits up to `wait_s` for a
`ping` to succeed.

Two callers at once (two hooks firing together) start one daemon. The
starter takes an exclusive `flock` on `<state dir>/daemon.lock` and hands
the locked file to the daemon it starts, which keeps it open for its whole
life. So while a daemon started here is loading (or running), the lock is
held: a second caller cannot take it and only waits for `ping`, and a
caller that gets the lock pings again before starting anything. The lock
goes away with the daemon process, however it ends. `serve` takes the same
lock itself (`--lock-fd` names the inherited descriptor), so a daemon
started by hand or by a plugin holds it too while it loads, and a second
`serve` exits at once instead of loading the graph again.

The lock is `<state dir>/daemon.lock` for the root's default socket and
`<socket>.lock` for any other socket (`lock_path`).

The default idle timeout is `DEFAULT_IDLE_TIMEOUT_S` (30 minutes); 0 turns
it off.
"""
from __future__ import annotations

import fcntl
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from ozagia.config import state_path
from ozagia.daemon import protocol

DEFAULT_IDLE_TIMEOUT_S = 1800.0
DEFAULT_WAIT_S = 10.0
LOCK_NAME = "daemon.lock"
LOG_NAME = "daemon.log"
PING_TIMEOUT_S = 0.5
POLL_S = 0.05


def ping(sock: str | Path, timeout: float = PING_TIMEOUT_S) -> dict[str, Any] | None:
    """The daemon's `ping` reply, or None when nothing answers."""
    from ozagia.daemon.client import DaemonClient, DaemonError
    try:
        with DaemonClient(sock, timeout=timeout) as c:
            reply = c.ping()
    except DaemonError:
        return None
    return reply if reply.get("ok") else None


def lock_path(root: str | Path, sock: str | Path | None = None) -> Path:
    """The lock file of the daemon for `root` on `sock` (default: the root's
    socket): `<state dir>/daemon.lock` for the root's own socket, else
    `<sock>.lock`, so daemons on separate sockets do not exclude each other."""
    root_p = Path(root).resolve()
    default = protocol.socket_path(root_p)
    sock_p = Path(sock) if sock else default
    if sock_p == default:
        return state_path(root_p, LOCK_NAME)
    return sock_p.with_name(sock_p.name + ".lock")


def _try_lock(fd: int) -> bool:
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False


def _wait_ping(sock: Path, deadline: float, proc: subprocess.Popen | None = None
               ) -> dict[str, Any] | None:
    while True:
        reply = ping(sock, timeout=max(min(PING_TIMEOUT_S, deadline - time.monotonic()),
                                       0.05))
        if reply is not None:
            return reply
        if proc is not None and proc.poll() is not None:
            return None
        if time.monotonic() >= deadline:
            return None
        time.sleep(POLL_S)


def _log_tail(path: Path, n: int = 5) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return " | ".join(lines[-n:])


def ensure(root: str | Path, *, wait_s: float = DEFAULT_WAIT_S,
           idle_timeout_s: float = DEFAULT_IDLE_TIMEOUT_S,
           python: str = sys.executable, sock: str | Path | None = None,
           extra_args: list[str] | None = None) -> dict[str, Any]:
    """Make sure a daemon answers on `root`'s socket (see the module
    docstring). Returns `{"ok", "started", "sock", "pid"?, "reason"?}`:
    `ok` when a daemon answered within `wait_s`, `started` when this call
    started one (it may still be loading when `ok` is False)."""
    root_p = Path(root).resolve()
    sock_p = Path(sock) if sock else protocol.socket_path(root_p)
    deadline = time.monotonic() + max(wait_s, 0.0)
    reply = ping(sock_p)
    if reply is not None:
        return {"ok": True, "started": False, "sock": str(sock_p), "pid": reply.get("pid")}
    lock_p = lock_path(root_p, sock_p)
    log_p = state_path(root_p, LOG_NAME)
    lock_p.parent.mkdir(parents=True, exist_ok=True)
    log_p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_p, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        while not _try_lock(fd):
            # A daemon started here holds the lock: it is loading or running.
            reply = ping(sock_p)
            if reply is not None:
                return {"ok": True, "started": False, "sock": str(sock_p),
                        "pid": reply.get("pid")}
            if time.monotonic() >= deadline:
                return {"ok": False, "started": False, "sock": str(sock_p),
                        "reason": "another daemon is starting"}
            time.sleep(POLL_S)
        reply = ping(sock_p)
        if reply is not None:
            return {"ok": True, "started": False, "sock": str(sock_p),
                    "pid": reply.get("pid")}
        cmd = [python, "-m", "ozagia.daemon", "serve", "--root", str(root_p),
               "--idle-timeout", f"{max(idle_timeout_s, 0.0):g}", "--lock-fd", str(fd)]
        if sock:
            cmd += ["--sock", str(sock_p)]
        cmd += list(extra_args or [])
        env = {**os.environ, "OZAGIA_ROOT": str(root_p)}
        with open(log_p, "ab") as log, open(os.devnull, "rb") as devnull:
            proc = subprocess.Popen(cmd, stdin=devnull, stdout=log, stderr=log,
                                    env=env, cwd=str(root_p), start_new_session=True,
                                    pass_fds=(fd,))
    finally:
        # The child keeps its copy of the locked descriptor.
        os.close(fd)
    reply = _wait_ping(sock_p, deadline, proc)
    if reply is not None:
        return {"ok": True, "started": True, "sock": str(sock_p), "pid": reply.get("pid")}
    code = proc.poll()
    if code is not None:
        return {"ok": False, "started": True, "sock": str(sock_p), "pid": proc.pid,
                "reason": f"daemon exited with {code}: {_log_tail(log_p)}"}
    return {"ok": False, "started": True, "sock": str(sock_p), "pid": proc.pid,
            "reason": f"daemon not ready after {wait_s:g} s (still loading; log {log_p})"}


__all__ = ["DEFAULT_IDLE_TIMEOUT_S", "LOCK_NAME", "LOG_NAME", "ensure", "lock_path",
           "ping"]
