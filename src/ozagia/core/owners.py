"""Who edited a file: sessions in the edit journal, liveness, git state.

Every edit DevMemory journals while a session acts (`acting`) carries
`session: {"harness", "id"}`, `epoch` (Unix seconds) and `head` (the
commit HEAD named when the edit was made): the edit is committed once a
commit after that HEAD touches the file, or the file is clean. Edits the
harness's own tools made are journaled by `ozagia.intent.after` as
`builtin_edit` entries with the same fields.

A session is live when the daemon saw a request or `hello` from it in the
last `LIVE_WINDOW_S` seconds (`Owners.touch`; at start the daemon reads
the times back from its journal, `seen_sessions`) and has not ended: a
`session_event` `end` (Claude's SessionEnd) drops it (`Owners.end`). An MCP server session
(`harness` "mcp", id `pid<N>`) is live while that process runs.

Before an edit, `Owners.warnings` names each other live session whose last
edit of the file is newer than the file's last commit, while the file is
dirty in git:

    [ozagia] warning: <file> has uncommitted edits by another live session (<harness> <short id>, <age>)

An MCP server session is named `MCP server pid <N>`. The MCP server does
not know which harness session drives it, so an agent that edits through
both MCP tools and its built-in tools would be warned about itself. A
best-effort heuristic suppresses that: a warning about an MCP session's
edit is dropped when the requesting session itself sent the daemon a
request within `MCP_SELF_WINDOW_S` seconds of that edit (either side).
It can hide a real warning about another agent's MCP server whose edit
happened to coincide with the requester's activity.

When no other session is live (`Owners.seen`, and no running MCP server
has journaled an edit), the edit journal is not read at all.

Committed is judged per file, not per hunk: the edit counts as committed
when a commit in `<head>..HEAD` (with `<head>`, recorded at edit time, an
ancestor of HEAD) touched the file. A partial commit of the file by
another session (`git add -p`) therefore marks a still-dirty edit as
committed, and its warning is suppressed. After a branch switch that
leaves `<head>` off HEAD's history the range is not trusted and the edit
counts as uncommitted while the file is dirty.

`who` answers `ozagia who <path>` and MCP `dev_who`.
"""
from __future__ import annotations

import contextlib
import json
import os
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Iterator

LIVE_WINDOW_S = 15 * 60
# The journals are read from their end, at most this many bytes.
JOURNAL_TAIL_BYTES = 4 * 1024 * 1024
DAEMON_JOURNAL_TAIL_BYTES = 2 * 1024 * 1024
GIT_TIMEOUT_S = 3
SHORT_ID = 8
# A warning about an MCP session's edit is dropped when the requesting
# session sent a request within this many seconds of it (module docstring).
MCP_SELF_WINDOW_S = 5.0
# Request times kept per session for that check.
REQUEST_HISTORY = 256
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
    # The journal is `json.dumps` output: a line about `rel` contains it
    # JSON-encoded, so most lines are skipped without parsing.
    needle = json.dumps(rel) if rel is not None else None
    for line in _tail_lines(Path(journal), JOURNAL_TAIL_BYTES):
        if '"session"' not in line or (needle is not None and needle not in line):
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
    return _daemon_sessions(daemon_journal)[0]


def _daemon_sessions(daemon_journal: str | Path
                     ) -> tuple[dict[tuple[str, str], float],
                                dict[tuple[str, str], list[float]]]:
    """`seen_sessions`, and each session's request times (oldest first)."""
    history: dict[tuple[str, str], list[float]] = {}
    seen = _seen_sessions(daemon_journal, history)
    return seen, history


def _seen_sessions(daemon_journal: str | Path,
                   history: dict[tuple[str, str], list[float]]
                   ) -> dict[tuple[str, str], float]:
    """(harness, session) -> the time of its last request in the daemon
    journal. A session whose last word is a `session_event` `end` is left
    out."""
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
        if e.get("op") == "session_event" and e.get("intent") == "session_end":
            out.pop(key, None)          # an ended session is not live
            history.pop(key, None)
            continue
        out[key] = max(out.get(key, 0.0), t)
        history.setdefault(key, []).append(t)
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


def _is_ancestor(root: Path, commit: str) -> bool | None:
    """Whether `commit` is an ancestor of HEAD; None when git cannot say
    (an unknown commit, no git)."""
    try:
        proc = subprocess.run(["git", "-C", str(root), "merge-base", "--is-ancestor",
                               commit, "HEAD"], capture_output=True,
                              timeout=GIT_TIMEOUT_S, stdin=subprocess.DEVNULL,
                              check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return {0: True, 1: False}.get(proc.returncode)


def _committed(root: Path, rel: str, edit: dict[str, Any],
               state: dict[str, Any]) -> bool:
    """Whether an edit is in git: the file is clean, or a commit after the
    HEAD the edit was made on touched it, that HEAD being an ancestor of
    the current one (not: uncommitted). File-granular: a partial commit of
    the file by someone else counts (module docstring). Without that HEAD
    (an older entry, or a commit git no longer knows), whether the file's
    last commit is not older than the edit (commit times are whole
    seconds)."""
    if state.get("dirty") is False:
        return True
    h = edit.get("head")
    if h:
        anc = _is_ancestor(root, h)
        if anc is False:
            # HEAD moved off that history (a branch switch): `h..HEAD`
            # would hold another branch's commits. Not trusted.
            return False
        if anc:
            out = _git(root, "rev-list", "-1", f"{h}..HEAD", "--", rel)
            if out is not None:
                return bool(out.strip())
    last = state.get("last_commit")
    return last is not None and int(edit["epoch"]) <= last


def label(harness: str, session: str) -> str:
    """How a warning names a session: `claude abcd1234`, `MCP server pid N`."""
    if harness == "mcp" and session.startswith("pid") and session[3:].isdigit():
        return f"MCP server pid {session[3:]}"
    return f"{harness} {session[:SHORT_ID]}"


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
        seen, history = _daemon_sessions(daemon_journal) if daemon_journal else ({}, {})
        self.seen: dict[tuple[str, str], float] = seen
        # Recent request times per session (the MCP self-warning check).
        self.requests: dict[tuple[str, str], deque[float]] = {
            k: deque(v, maxlen=REQUEST_HISTORY) for k, v in history.items()}
        # Per edit journal: the MCP session ids that journaled an edit, and
        # the offset read up to (`_mcp_sessions`).
        self._mcp: dict[str, set[str]] = {}
        self._mcp_offset: dict[str, int] = {}

    def touch(self, harness: Any, session: Any, now: float | None = None) -> None:
        if not isinstance(session, str) or not session:
            return
        t = time.time() if now is None else now
        key = (str(harness or "opencode"), session)
        with self._lock:
            self.seen[key] = t
            self.requests.setdefault(key, deque(maxlen=REQUEST_HISTORY)).append(t)

    def end(self, harness: Any, session: Any) -> None:
        """The session ended (SessionEnd): it is no longer live."""
        if not isinstance(session, str) or not session:
            return
        key = (str(harness or "opencode"), session)
        with self._lock:
            self.seen.pop(key, None)
            self.requests.pop(key, None)

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

    def _mcp_sessions(self, journal: str | Path) -> set[str]:
        """Ids of the MCP sessions that journaled an edit in `journal`,
        read incrementally (only bytes appended since the last call; the
        first call reads the tail)."""
        path = Path(journal)
        key = str(path)
        try:
            size = path.stat().st_size
        except OSError:
            return set()
        with self._lock:
            start = self._mcp_offset.get(key)
            found = self._mcp.setdefault(key, set())
        skip_first = False
        if start is None or start > size:      # first read, or rewritten
            start = max(0, size - JOURNAL_TAIL_BYTES)
            skip_first = start > 0
            found.clear()
        if start == size:
            return set(found)
        try:
            with open(path, "rb") as fh:
                fh.seek(start)
                data = fh.read(size - start)
        except OSError:
            return set(found)
        end = data.rfind(b"\n") + 1           # complete lines only
        lines = data[:end].decode("utf-8", errors="replace").splitlines()
        if skip_first and lines:
            lines = lines[1:]
        for line in lines:
            if '"harness": "mcp"' not in line:
                continue
            try:
                sess = json.loads(line).get("session")
            except (ValueError, AttributeError):
                continue
            if isinstance(sess, dict) and sess.get("harness") == "mcp" and isinstance(
                    sess.get("id"), str):
                found.add(sess["id"])
        with self._lock:
            self._mcp_offset[key] = start + end
        return set(found)

    def others_live(self, journal: str | Path, me: tuple[str, str],
                    now: float | None = None) -> bool:
        """Whether a session other than `me` is live: one the daemon saw in
        the window, or a running MCP server that journaled an edit."""
        now = time.time() if now is None else now
        with self._lock:
            if any(k != me and now - t <= self.window_s
                   for k, t in self.seen.items()):
                return True
        return any(("mcp", s) != me and _pid_alive(s)
                   for s in self._mcp_sessions(journal))

    def active_near(self, harness: str, session: str, t: float,
                    window_s: float = MCP_SELF_WINDOW_S) -> bool:
        """Whether (harness, session) sent a request within `window_s` of
        `t`."""
        with self._lock:
            times = list(self.requests.get((harness, session), ()))
        return any(abs(r - t) <= window_s for r in times)

    def warnings(self, journal: str | Path, rel: str, harness: Any, session: Any,
                 now: float | None = None) -> list[str]:
        """Warning lines for an edit of `rel` by (harness, session)."""
        now = time.time() if now is None else now
        me = (str(harness or "opencode"), session if isinstance(session, str) else "")
        if not self.others_live(journal, me, now):
            return []                   # the journal is not read
        last: dict[tuple[str, str], dict[str, Any]] = {}
        for e in edits(journal, rel):
            key = (e["harness"], e["session"])
            if key != me and (key not in last or e["epoch"] >= last[key]["epoch"]):
                last[key] = e
        others = {k: e for k, e in last.items() if self.live(*k, now=now)
                  and not (k[0] == "mcp" and me[1]
                           and self.active_near(*me, e["epoch"]))}
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
                         f"another live session ({label(h, s)}, "
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


__all__ = ["LIVE_WINDOW_S", "MCP_SELF_WINDOW_S", "Owners", "acting", "age", "edits",
           "format_who", "git_state", "head", "label", "mcp_session", "seen_sessions",
           "session_field", "who"]
