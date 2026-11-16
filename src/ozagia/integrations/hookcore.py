"""What every harness's command hook shares (Claude Code, Codex).

A command hook reads one event as JSON on stdin and answers on stdout. The
pieces here do not depend on a harness's tool names or output envelope:

- `daemon_request`: one protocol request over the daemon's Unix socket.
- `daemon_request_autostart`: the same, starting the daemon
  (`ozagia.daemon.autostart.ensure`) when nothing listens, within a short
  budget. Off with `$OZAGIA_DAEMON_AUTOSTART=0`. A daemon is started only
  for a root that is a git work tree, is `$OZAGIA_ROOT`, or already has a
  graph snapshot (`autostart_allowed`); elsewhere (`$HOME`, `/tmp`, a
  download folder) the hook starts nothing and writes nothing.
- `run_hook`: the fail-open main. A SIGALRM timeout, any error printing
  nothing and exiting 0, each failure logged as one JSON line (`ts`,
  `event`, `tool`, `session`, `error`) in the state dir. Set the alarm a
  few seconds below the harness's handler timeout, so the hook stops
  itself before the harness kills it.
- `root_for`: the repository a hook event is about (its `cwd`).
- `git_ref_moving`: the ref-moving git subcommand in a shell command
  (`ozagia.intent.shell.classify` is `git_refs`), and `git_refusal_reason`.
- `node_key`: the node key a file path names, if any.
- `response_text`: a built-in's result flattened to text for `after`.
- `repeat_stub`, `session_event`, `start_event`: the read ledger
  (`ozagia.intent.ledger`): ask whether a read repeats an unchanged range
  (PreToolUse), and report compaction, start and end of a session.
"""
from __future__ import annotations

import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, TextIO

from ozagia.config import ENV_ROOT, repo_root, state_path
from ozagia.daemon import protocol
from ozagia.intent.shell import GIT_REF_MOVING, classify

# The total time allowed for one daemon request, from connect to response.
DAEMON_TIMEOUT_S = 15
# How long a hook waits for a daemon it started to answer.
AUTOSTART_BUDGET_S = 3.0
ENV_AUTOSTART = "OZAGIA_DAEMON_AUTOSTART"
ENV_IDLE_TIMEOUT = "OZAGIA_DAEMON_IDLE_TIMEOUT"
# The daemon's graph snapshot in the state dir (`ozagia.daemon.server.load_memory`).
SNAPSHOT_NAME = "graph.json"
GIT_TIMEOUT_S = 3

KEY_PREFIXES = ("sym:", "test:", "doc:", "text:")
# Harnesses may make file paths absolute before hooks run, so a key the model
# passed as a path may arrive as `<cwd>/sym:...`.
_KEY_IN_PATH = re.compile(r"(?:^|/)((?:sym|test|doc|text):.+)$")

# The intent layer's list; hooks refuse what `classify` calls `git_refs`, so
# listing forms (`git branch`, `git tag -l`, `git stash list`) are allowed.
REF_MOVING_GIT = GIT_REF_MOVING


class HookError(Exception):
    """The daemon could not answer."""


class HookTimeout(Exception):
    """The hook ran out of time."""


class AutostartRefused(HookError):
    """No daemon answers and the root is not one to start a daemon for.
    Not a failure: `run_hook` prints nothing and logs nothing."""


# --------------------------------------------------------------------------- #
# local checks
# --------------------------------------------------------------------------- #
def git_ref_moving(command: str) -> str | None:
    """The ref-moving git subcommand in a shell command, or None: what
    `ozagia.intent.shell.classify` calls `git_refs`."""
    got = classify(command or "")
    if got["class"] != "git_refs":
        return None
    return str(got.get("subcommand") or "git")


def git_refusal_reason(sub: str) -> str:
    """The reason a hook gives the model when it refuses `git <sub>`."""
    return (f"ozagia: `git {sub}` moves refs and is refused in this session. "
            "Read-only git (status, diff, log, show, blame) is allowed; leave "
            "commits, branches and stashes to the user.")


def node_key(file_path: Any) -> str | None:
    """The node key a file path names, or None for a real path."""
    if not isinstance(file_path, str) or not file_path:
        return None
    if file_path.startswith(KEY_PREFIXES):
        return file_path
    m = _KEY_IN_PATH.search(file_path)
    if m and not os.path.exists(file_path):
        return m.group(1)
    return None


def response_text(tool_response: Any) -> str:
    """A built-in's result as text for the protocol's `output`.

    Handles a string, a list of `{"text": ...}` content blocks, and objects
    with `stdout`/`stderr`, `file.content`, `content` or `filenames`; any
    other object is sent as JSON without `originalFile` (Edit and Write echo
    the whole original file; the daemon reads the file itself).
    """
    if tool_response is None:
        return ""
    if isinstance(tool_response, str):
        return tool_response
    if isinstance(tool_response, list):
        texts = [b.get("text") for b in tool_response
                 if isinstance(b, dict) and isinstance(b.get("text"), str)]
        if texts and len(texts) == len(tool_response):
            return "\n".join(texts)
        return json.dumps(tool_response, ensure_ascii=False)
    if isinstance(tool_response, dict):
        if "stdout" in tool_response or "stderr" in tool_response:
            parts = [str(tool_response.get(k) or "") for k in ("stdout", "stderr")]
            return "\n".join(p for p in parts if p)
        file = tool_response.get("file")
        if isinstance(file, dict) and isinstance(file.get("content"), str):
            return file["content"]
        if isinstance(tool_response.get("content"), str):
            return tool_response["content"]
        names = tool_response.get("filenames")
        if isinstance(names, list) and all(isinstance(n, str) for n in names):
            return "\n".join(names)
        slim = {k: v for k, v in tool_response.items() if k != "originalFile"}
        return json.dumps(slim, ensure_ascii=False)
    return str(tool_response)


# --------------------------------------------------------------------------- #
# daemon client
# --------------------------------------------------------------------------- #
def daemon_request(sock: Path, msg: dict[str, Any],
                   timeout: float = DAEMON_TIMEOUT_S) -> dict[str, Any]:
    """Send one protocol request and return the `ok` response.

    A missing socket or a refused connection raises the OSError
    (`FileNotFoundError`, `ConnectionRefusedError`); a bad or failed answer
    raises `HookError`.
    """
    deadline = time.monotonic() + timeout
    line = (json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8")
    buf = bytearray()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        s.connect(str(sock))
        s.sendall(line)
        while b"\n" not in buf:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise HookError("daemon request timed out")
            s.settimeout(remaining)
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
            if len(buf) > protocol.MAX_MESSAGE_BYTES:
                raise HookError("daemon response too large")
    raw = bytes(buf).split(b"\n", 1)[0]
    if not raw:
        raise HookError("daemon closed the connection without a response")
    resp = json.loads(raw.decode("utf-8"))
    if not isinstance(resp, dict) or resp.get("id") != msg.get("id"):
        raise HookError("daemon response does not match the request")
    if not resp.get("ok"):
        raise HookError(f"daemon error: {resp.get('error')}")
    return resp


def autostart_enabled() -> bool:
    return os.environ.get(ENV_AUTOSTART, "1").strip().lower() not in (
        "0", "false", "no", "off")


def idle_timeout_s() -> float:
    """The idle timeout for a daemon a hook starts (`$OZAGIA_DAEMON_IDLE_TIMEOUT`,
    default `ozagia.daemon.autostart.DEFAULT_IDLE_TIMEOUT_S`)."""
    from ozagia.daemon.autostart import DEFAULT_IDLE_TIMEOUT_S
    raw = os.environ.get(ENV_IDLE_TIMEOUT)
    try:
        return float(raw) if raw else DEFAULT_IDLE_TIMEOUT_S
    except ValueError:
        return DEFAULT_IDLE_TIMEOUT_S


def in_work_tree(root: Path) -> bool:
    """Whether `root` is inside a git work tree."""
    try:
        proc = subprocess.run(["git", "-C", str(root), "rev-parse",
                               "--is-inside-work-tree"], capture_output=True,
                              text=True, timeout=GIT_TIMEOUT_S,
                              stdin=subprocess.DEVNULL, check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def autostart_allowed(root: Path, in_repo: bool | None = None) -> bool:
    """Whether a hook may start a daemon for `root`: `$OZAGIA_ROOT` is set,
    `root` already has a graph snapshot, or it is a git work tree (`in_repo`,
    when the caller has checked already). A daemon started anywhere else
    would build a graph of an arbitrary directory, `$HOME` included."""
    if os.environ.get(ENV_ROOT):
        return True
    if state_path(root, SNAPSHOT_NAME).exists():
        return True
    return in_work_tree(root) if in_repo is None else in_repo


def ensure_daemon(root: Path, budget_s: float = AUTOSTART_BUDGET_S, *,
                  in_repo: bool | None = None) -> dict[str, Any]:
    """Start the daemon for `root` if none answers (see
    `ozagia.daemon.autostart.ensure`); `{"ok": False, ...}` when autostart
    is off, and `{"ok": False, "refused": True, ...}` when `root` is not
    one to start a daemon for (`autostart_allowed`)."""
    if not autostart_enabled():
        return {"ok": False, "started": False, "reason": "autostart is off"}
    if not autostart_allowed(root, in_repo):
        return {"ok": False, "started": False, "refused": True,
                "reason": f"{root} is not a git work tree and has no graph snapshot"}
    from ozagia.daemon.autostart import ensure
    return ensure(root, wait_s=budget_s, idle_timeout_s=idle_timeout_s())


def daemon_request_autostart(root: Path, msg: dict[str, Any], *,
                             timeout: float = DAEMON_TIMEOUT_S,
                             budget_s: float = AUTOSTART_BUDGET_S) -> dict[str, Any]:
    """`daemon_request` on `root`'s socket; when nothing listens, start the
    daemon within `budget_s` and send the request once more."""
    sock = protocol.socket_path(root)
    try:
        return daemon_request(sock, msg, timeout)
    except (FileNotFoundError, ConnectionRefusedError):
        got = ensure_daemon(root, budget_s)
        if got.get("refused"):
            raise AutostartRefused(got.get("reason")) from None
        if not got.get("ok"):
            raise HookError(f"no daemon on {sock}: {got.get('reason')}") from None
    return daemon_request(sock, msg, timeout)


# --------------------------------------------------------------------------- #
# the read ledger
# --------------------------------------------------------------------------- #
# Shell command classes that may be a file read the ledger tracks.
_READ_CLASSES = ("read", "compound")
# A PreToolUse repeat check gets this long; the read runs when it is late.
READ_CHECK_TIMEOUT_S = 3.0
SESSION_EVENT_TIMEOUT_S = 2.0


def maybe_read(tool: str, inp: dict[str, Any]) -> bool:
    """Whether a call may be a read the daemon's ledger tracks (cheap,
    local): a read, or a shell read (`classify` is read or compound)."""
    if tool == "read":
        return isinstance(inp.get("path"), str)
    if tool == "shell":
        return classify(str(inp.get("command") or ""))["class"] in _READ_CLASSES
    return False


def repeat_stub(root: Path, harness: str, session: Any, sub: Any, tool: str,
                inp: dict[str, Any], timeout: float = READ_CHECK_TIMEOUT_S
                ) -> str | None:
    """The daemon's stub for a read that repeats an unchanged range this
    session already read (`read_check`, stub policy), or None: not a
    repeat, another policy, or no daemon (nothing to compare against)."""
    if not isinstance(session, str) or not session or not maybe_read(tool, inp):
        return None
    msg: dict[str, Any] = {"id": 1, "op": "read_check", "harness": harness,
                           "session": session, "tool": tool, "input": inp,
                           "deadline_ms": int((time.time() + timeout) * 1000)}
    if isinstance(sub, str) and sub:
        msg["sub"] = sub
    try:
        resp = daemon_request(protocol.socket_path(root), msg, timeout)
    except (OSError, HookError, ValueError):
        return None
    stub = resp.get("stub")
    return stub if isinstance(stub, str) and stub else None


def session_event(root: Path, harness: str, session: Any, event: str,
                  timeout: float = SESSION_EVENT_TIMEOUT_S) -> bool:
    """Tell the daemon about a compaction, start or end of a session
    (`session_event`), so its ledger is cleared. False when no daemon
    answers: a daemon that starts later has an empty ledger anyway."""
    if not isinstance(session, str) or not session:
        return False
    try:
        daemon_request(protocol.socket_path(root), {
            "id": 1, "op": "session_event", "harness": harness,
            "session": session, "event": event}, timeout)
    except (OSError, HookError, ValueError):
        return False
    return True


def start_event(source: Any) -> str:
    """The `session_event` for a SessionStart `source`: `compact` after a
    compaction, else `start` (startup, clear, resume, fork)."""
    return "compact" if source == "compact" else "start"


# --------------------------------------------------------------------------- #
# the fail-open main
# --------------------------------------------------------------------------- #
def root_for(payload: Any) -> Path:
    """The repository of the event's `cwd`, else of this process's cwd."""
    cwd = payload.get("cwd") if isinstance(payload, dict) else None
    if isinstance(cwd, str) and cwd and os.path.isdir(cwd):
        return repo_root(cwd)
    return repo_root()


def log_failure(root: Path | None, log_name: str, payload: Any,
                error: BaseException) -> None:
    """Append one JSON line for a failed event to `<state dir>/<log_name>`."""
    try:
        base = root if root is not None else repo_root()
        path = state_path(base, log_name)
        path.parent.mkdir(parents=True, exist_ok=True)
        p = payload if isinstance(payload, dict) else {}
        entry = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 "event": p.get("hook_event_name"), "tool": p.get("tool_name"),
                 "session": p.get("session_id"),
                 "error": f"{type(error).__name__}: {error}"}
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


Handler = Callable[[dict[str, Any], Path], "dict[str, Any] | None"]

# The SIGALRM handler `run_hook` installed, while it runs.
_alarm_handler: Any = None


def rearm_alarm(seconds: int) -> bool:
    """Arm `run_hook`'s alarm again for `seconds` (after a handler caught a
    `HookTimeout` and goes on). Does nothing outside `run_hook`. True if
    armed."""
    if (_alarm_handler is None or not hasattr(signal, "SIGALRM")
            or signal.getsignal(signal.SIGALRM) is not _alarm_handler):
        return False
    signal.alarm(max(int(seconds), 1))
    return True


def run_hook(handle: Handler, *, log_name: str, timeout_s: int,
             stdin: TextIO | None = None, stdout: TextIO | None = None) -> int:
    """Read one event, print `handle(payload, root)` as JSON when it is not
    None, and return 0. Any error, including running past `timeout_s`,
    prints nothing and is logged (`AutostartRefused` is not)."""
    global _alarm_handler
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    payload: Any = None
    root: Path | None = None
    use_alarm = hasattr(signal, "SIGALRM")

    def on_alarm(signum: int, frame: Any) -> None:
        raise HookTimeout(f"hook exceeded {timeout_s} s")

    previous: Any = None
    try:
        if use_alarm:
            previous = signal.signal(signal.SIGALRM, on_alarm)
            _alarm_handler = on_alarm
            signal.alarm(timeout_s)
        payload = json.loads(stdin.read())
        if not isinstance(payload, dict):
            raise ValueError("hook input is not a JSON object")
        root = root_for(payload)
        out = handle(payload, root)
        if out is not None:
            stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
            stdout.flush()
    except AutostartRefused:
        pass
    except BaseException as exc:  # never block the agent because ozagia failed
        log_failure(root, log_name, payload, exc)
    finally:
        if use_alarm:
            signal.alarm(0)
            _alarm_handler = None
            if previous is not None:
                signal.signal(signal.SIGALRM, previous)
    return 0


__all__ = ["AUTOSTART_BUDGET_S", "DAEMON_TIMEOUT_S", "AutostartRefused", "HookError",
           "HookTimeout", "KEY_PREFIXES", "REF_MOVING_GIT", "autostart_allowed",
           "autostart_enabled", "daemon_request", "daemon_request_autostart",
           "ensure_daemon", "git_ref_moving", "git_refusal_reason", "in_work_tree",
           "log_failure", "maybe_read", "node_key", "rearm_alarm", "repeat_stub",
           "response_text", "root_for", "run_hook", "session_event", "start_event"]
