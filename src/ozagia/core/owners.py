"""Who edited a file: sessions in the edit journal, liveness, git state.

Every edit DevMemory journals while a session acts (`acting`) carries
`session: {"harness", "id"}`, `epoch` (Unix seconds) and `head` (the
commit HEAD named when the edit was made): the edit is committed once a
commit after that HEAD touches the file, or the file is clean. Edits the
harness's own tools made are journaled by `ozagia.intent.after` as
`builtin_edit` entries with the same fields.

A session is live when the daemon saw a request or `hello` from it in the
last `LIVE_WINDOW_S` seconds (`Owners.touch`; at start the daemon reads
the times back from its journal, `seen_sessions`). An MCP server session
(`harness` "mcp", id `pid<N>`) is live while that process runs.

Before an edit, `Owners.warnings` names each other live session whose last
edit of the file is newer than the file's last commit, while the file is
dirty in git:

    [ozagia] warning: <file> has uncommitted edits by another live session (<harness> <short id>, <age>)

`who` answers `ozagia who <path>` and MCP `dev_who`.
"""
from __future__ import annotations

import contextlib
import json
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Iterator

LIVE_WINDOW_S = 15 * 60
# The journals are read from their end, at most this many bytes.
JOURNAL_TAIL_BYTES = 4 * 1024 * 1024
DAEMON_JOURNAL_TAIL_BYTES = 2 * 1024 * 1024
GIT_TIMEOUT_S = 3
SHORT_ID = 8
_TS_FORMAT = "%Y-%m-%dT%H:%M:%S"


# --------------------------------------------------------------------------- #
# recording
# --------------------------------------------------------------------------- #
def session_field(harness: Any, session: Any) -> dict[str, str] | None:
    if not isinstance(session, str) or not session:
        return None
    return {"harness": str(harness or "opencode"), "id": session}


@contextlib.contextmanager
def acting(memory: Any, harness: Any, session: Any) -> Iterator[None]:
    """While the block runs, DevMemory journals edits with this session
    (`memory.edit_session`, read by `journal_append`)."""
    prev = getattr(memory, "edit_session", None)
    try:
        memory.edit_session = session_field(harness, session)
    except AttributeError:          # a stand-in memory without attributes
        yield
        return
    try:
        yield
    finally:
        memory.edit_session = prev


def mcp_session() -> dict[str, str]:
    return {"harness": "mcp", "id": f"pid{os.getpid()}"}


# --------------------------------------------------------------------------- #
# reading the journals
# --------------------------------------------------------------------------- #
def _tail_lines(path: Path, max_bytes: int) -> list[str]:
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            start = max(0, size - max_bytes)
            fh.seek(start)
            data = fh.read()
    except OSError:
        return []
    lines = data.decode("utf-8", errors="replace").splitlines()
    return lines[1:] if start and lines else lines


def _epoch(entry: dict[str, Any]) -> float | None:
    e = entry.get("epoch")
    if isinstance(e, (int, float)) and not isinstance(e, bool):
        return float(e)
    ts = entry.get("ts")
    if isinstance(ts, str):
        try:
            return time.mktime(time.strptime(ts[:19], _TS_FORMAT))
        except (ValueError, OverflowError):
            return None
    return None


def edits(journal: str | Path, rel: str | None = None) -> list[dict[str, Any]]:
    """Journaled edits with a session, oldest first, of `rel` (or all):
    `{"harness", "session", "epoch", "kind", "files"}`."""
    out: list[dict[str, Any]] = []
    for line in _tail_lines(Path(journal), JOURNAL_TAIL_BYTES):
        if '"session"' not in line:
            continue
        try:
            e = json.loads(line)
        except ValueError:
            continue
        sess, files = e.get("session"), e.get("files")
        if not isinstance(sess, dict) or not isinstance(files, list):
            continue
        files = [f for f in files if isinstance(f, str) and f]
        if rel is not None and rel not in files:
            continue
        t = _epoch(e)
        if t is None or not isinstance(sess.get("id"), str):
            continue
        out.append({"harness": str(sess.get("harness") or "opencode"),
                    "session": sess["id"], "epoch": t,
                    "kind": e.get("kind"), "files": files,
                    "head": e.get("head") if isinstance(e.get("head"), str) else None})
    return out


def seen_sessions(daemon_journal: str | Path) -> dict[tuple[str, str], float]:
    """(harness, session) -> the time of its last request in the daemon
    journal."""
    out: dict[tuple[str, str], float] = {}
    for line in _tail_lines(Path(daemon_journal), DAEMON_JOURNAL_TAIL_BYTES):
        if '"session"' not in line:
            continue
        try:
            e = json.loads(line)
        except ValueError:
            continue
        s = e.get("session")
        if not isinstance(s, str) or not s:
            continue
        t = _epoch(e)
        if t is None:
            continue
        key = (str(e.get("harness") or "opencode"), s)
        out[key] = max(out.get(key, 0.0), t)
    return out


def _pid_alive(session: str) -> bool:
    if not session.startswith("pid") or not session[3:].isdigit():
        return False
    try:
        os.kill(int(session[3:]), 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


# --------------------------------------------------------------------------- #
# git
# --------------------------------------------------------------------------- #
def _git(root: Path, *args: str) -> str | None:
    try:
        proc = subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                              text=True, timeout=GIT_TIMEOUT_S,
                              stdin=subprocess.DEVNULL, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 else None


def git_state(root: str | Path, rel: str) -> dict[str, Any]:
    """`{"dirty": bool | None, "last_commit": epoch | None}` of `rel`
    (dirty includes untracked; None: not a git work tree)."""
    root = Path(root)
    status = _git(root, "status", "--porcelain=v1", "--untracked-files=all",
                  "--", rel)
    log = _git(root, "log", "-1", "--format=%ct", "--", rel)
    last = None
    if log and log.strip().isdigit():
        last = float(log.strip())
    return {"dirty": None if status is None else bool(status.strip()),
            "last_commit": last}


def head(root: str | Path) -> str | None:
    """The commit HEAD names, or None (no git, no commit yet)."""
    out = _git(Path(root), "rev-parse", "--verify", "-q", "HEAD")
    return out.strip() or None if out else None


def _committed(root: Path, rel: str, edit: dict[str, Any],
               state: dict[str, Any]) -> bool:
    """Whether an edit is in git: the file is clean, or a commit after the
    HEAD the edit was made on touched it. Without that HEAD (an older
    entry), whether the file's last commit is not older than the edit
    (commit times are whole seconds)."""
    if state.get("dirty") is False:
        return True
    h = edit.get("head")
    if h:
        out = _git(root, "rev-list", "-1", f"{h}..HEAD", "--", rel)
        if out is not None:
            return bool(out.strip())
    last = state.get("last_commit")
    return last is not None and int(edit["epoch"]) <= last


def age(seconds: float) -> str:
    s = max(0, int(seconds))
    if s < 60:
        return f"{s}s ago"
    if s < 3600:
        return f"{s // 60}m ago"
    if s < 86400:
        return f"{s // 3600}h ago"
    return f"{s // 86400}d ago"


# --------------------------------------------------------------------------- #
# the daemon's view
# --------------------------------------------------------------------------- #
class Owners:
    """Live sessions of one root and the ownership warnings they cause."""

    def __init__(self, root: str | Path, *, daemon_journal: str | Path | None = None,
                 window_s: float = LIVE_WINDOW_S) -> None:
        self.root = Path(root)
        self.window_s = float(window_s)
        self._lock = threading.Lock()
        self.seen: dict[tuple[str, str], float] = (
            seen_sessions(daemon_journal) if daemon_journal else {})

    def touch(self, harness: Any, session: Any, now: float | None = None) -> None:
        if not isinstance(session, str) or not session:
            return
        with self._lock:
            self.seen[(str(harness or "opencode"), session)] = (
                time.time() if now is None else now)

    def live(self, harness: str, session: str, now: float | None = None) -> bool:
        if harness == "mcp":
            return _pid_alive(session)
        with self._lock:
            t = self.seen.get((harness, session))
        now = time.time() if now is None else now
        return t is not None and now - t <= self.window_s

    def live_sessions(self, now: float | None = None) -> int:
        now = time.time() if now is None else now
        with self._lock:
            return sum(1 for t in self.seen.values() if now - t <= self.window_s)

    def warnings(self, journal: str | Path, rel: str, harness: Any, session: Any,
                 now: float | None = None) -> list[str]:
        """Warning lines for an edit of `rel` by (harness, session)."""
        now = time.time() if now is None else now
        me = (str(harness or "opencode"), session if isinstance(session, str) else "")
        last: dict[tuple[str, str], dict[str, Any]] = {}
        for e in edits(journal, rel):
            key = (e["harness"], e["session"])
            if key != me and (key not in last or e["epoch"] >= last[key]["epoch"]):
                last[key] = e
        others = {k: e for k, e in last.items() if self.live(*k, now=now)}
        if not others:
            return []
        state = git_state(self.root, rel)
        if not state.get("dirty"):
            return []
        lines = []
        for (h, s), e in sorted(others.items(), key=lambda kv: -kv[1]["epoch"]):
            if _committed(self.root, rel, e, state):
                continue
            t = e["epoch"]
            lines.append(f"[ozagia] warning: {rel} has uncommitted edits by "
                         f"another live session ({h} {s[:SHORT_ID]}, "
                         f"{age(now - t)})")
        return lines


# --------------------------------------------------------------------------- #
# ozagia who / dev_who
# --------------------------------------------------------------------------- #
def who(root: str | Path, rel: str, journal: str | Path, *,
        live: Any = None, now: float | None = None) -> dict[str, Any]:
    """The sessions that edited `rel`: for each, its edits, first and last
    time, whether it is live (`live(harness, session) -> bool`; None:
    unknown) and whether its edits are committed, with the file's git
    state."""
    now = time.time() if now is None else now
    state = git_state(root, rel)
    root = Path(root)
    rows: dict[tuple[str, str], dict[str, Any]] = {}
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for e in edits(journal, rel):
        key = (e["harness"], e["session"])
        row = rows.setdefault(key, {"harness": key[0], "session": key[1],
                                    "edits": 0, "first": e["epoch"],
                                    "last": e["epoch"]})
        row["edits"] += 1
        row["first"] = min(row["first"], e["epoch"])
        row["last"] = max(row["last"], e["epoch"])
        if key not in latest or e["epoch"] >= latest[key]["epoch"]:
            latest[key] = e
    sessions = []
    for key, row in sorted(rows.items(), key=lambda kv: -kv[1]["last"]):
        sessions.append({
            **row,
            "first": _iso(row["first"]), "last": _iso(row["last"]),
            "last_age": age(now - row["last"]),
            "live": None if live is None else bool(live(row["harness"], row["session"])),
            "committed": _committed(root, rel, latest[key], state),
        })
    return {"path": rel, "dirty": state.get("dirty"),
            "last_commit": (_iso(state["last_commit"])
                            if state.get("last_commit") is not None else None),
            "sessions": sessions}


def _iso(t: float) -> str:
    return time.strftime(_TS_FORMAT, time.localtime(t))


def format_who(report: dict[str, Any]) -> str:
    lines = [f"{report['path']}: "
             + ("dirty" if report.get("dirty") else
                "clean" if report.get("dirty") is False else "not in git")
             + (f", last commit {report['last_commit']}"
                if report.get("last_commit") else ", never committed")]
    if not report["sessions"]:
        lines.append("  no journaled edits by a session")
    for s in report["sessions"]:
        live = {True: "live", False: "not live", None: "liveness unknown"}[s["live"]]
        lines.append(f"  {s['harness']} {s['session']}: {s['edits']} edit(s), "
                     f"last {s['last']} ({s['last_age']}), {live}, "
                     + ("committed" if s["committed"] else "uncommitted"))
    return "\n".join(lines)


__all__ = ["LIVE_WINDOW_S", "Owners", "acting", "age", "edits", "format_who",
           "git_state", "head", "mcp_session", "seen_sessions", "session_field", "who"]
