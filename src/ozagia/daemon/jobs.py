"""Background jobs in the daemon: start a command, wait for it, read its log.

An agent that starts a long command (a server, a build, a test run) and then
polls it (`write_stdin` with no input, `sleep N; tail log`, `ps -p PID`)
spends a model call per poll. A job runs the command under the daemon, which
hooks, the MCP server and the CLI share, and `wait` blocks server-side until
the job exits, a file appears or a log line matches, then returns only the
output that is new to the caller.

A job (`JobTable.start`): an argv list, or a command string run by `bash -c`
(`/bin/sh -c` without bash), in `cwd` (inside the root; default the root),
with the daemon's test environment (`server.captured_test_env`) plus `env`
additions. It starts in a new session, so its own process group (pgid =
pid), with stdin from /dev/null and stdout and stderr appended to
`<state dir>/jobs/<id>.log`. A ref-moving git command is refused, as in the
shell capture. At most `MAX_RUNNING` jobs run at once; the table keeps the
last `MAX_JOBS` jobs (finished ones beyond that are dropped with their logs),
and is written to `<state dir>/jobs/jobs.json` on every change.

Status: `running`, `exited` (exit code), `killed` (by a signal; `signal`
names it), and for jobs a previous daemon left running (see shutdown):
`detached` (still alive; no exit code can be had) or `gone`.

Logs are not truncated while a job runs. Every read is capped instead: a
read returns at most `TAIL_LINES` lines and `TAIL_BYTES` bytes, the newest
ones, and says how many it skipped.

Cursors. Every reader (the protocol's `session`, or `reader`; default
"default") has a byte offset per job. `wait` and `log` return the lines from
that offset (or from an explicit `cursor`) to the end of the log, and move
the offset to the end. A partial last line of a running job is kept for the
next read unless it is the only new text.

`wait(job | "any", until, deadline_s)`: `until` is `exit` (default),
`file:<path>` (exists; relative to the job's cwd, or the root) or
`log:<regex>` (a new line of the job's log matches). It returns when the
condition holds, when the job exits (for file and log), or at the deadline
(default `WAIT_DEFAULT_S`, at most `WAIT_MAX_S`), with `met`, the status,
`exit_code`, `elapsed_s` (since the job started), `waited_s`, the new
`output` and the `cursor`. `job: "any"` waits on the jobs running when the
call starts (log too); for `file:` the job may be left out.

Shutdown (`JobTable.shutdown`, when the daemon exits for any reason): each
running job's process group gets SIGTERM, then SIGKILL after
`SHUTDOWN_GRACE_S`, except jobs started with `detach`, which are left
running, logged to stderr (the daemon log) and kept in `jobs.json`; the next
daemon lists them as `detached` (it can stop them, not reap them).

`capture_ps(command, ...)` answers `pgrep` and `ps -p` commands about job
processes (see its docstring); `shell_hints(command, ...)` gives the
`[ozagia]` lines for a shell command's `after`: `sleep N` while a job runs,
and background starts (`... &`, `nohup`, `setsid`).
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

MAX_JOBS = 50
MAX_RUNNING = 16
TAIL_LINES = 200
TAIL_BYTES = 16 * 1024
WAIT_DEFAULT_S = 45.0
WAIT_MAX_S = 3600.0
STOP_GRACE_S = 3.0
SHUTDOWN_GRACE_S = 2.0
POLL_S = 0.2
# Bytes of new log text one poll of a `log:` wait scans.
SCAN_CHUNK = 1 << 20
MAX_ENV = 64
MAX_COMMAND_CHARS = 16000
MAX_READERS = 1000
TABLE_NAME = "jobs.json"
JOBS_DIR = "jobs"
# `sleep N` at least this long, while a job runs, gets the job_wait hint.
SLEEP_HINT_S = 20.0
# Each kind of hint is shown at most this often per session.
HINTS_PER_SESSION = 3
OPS = ("job_start", "job_wait", "job_list", "job_log", "job_stop", "job_events",
       "job_ps")
_ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SIGNALS = {"TERM": signal.SIGTERM, "KILL": signal.SIGKILL, "INT": signal.SIGINT,
            "HUP": signal.SIGHUP}


class JobError(ValueError):
    """A bad job request (unknown job, bad input, cwd outside the root)."""


def _now() -> float:
    return time.time()


def _proc_stat(pid: int) -> list[str] | None:
    """/proc/<pid>/stat as [pid, comm, state, ...], or None."""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(errors="replace")
    except OSError:
        return None
    lp, rp = raw.find("("), raw.rfind(")")
    if lp < 0 or rp < 0:
        return None
    return [raw[:lp].strip(), raw[lp + 1:rp], *raw[rp + 2:].split()]


def _proc_start(pid: int) -> str | None:
    st = _proc_stat(pid)
    return st[21] if st and len(st) > 21 else None


def _alive(pid: int | None, start: str | None) -> bool:
    """Whether `pid` is the process that started at `start` (not a reuse)."""
    if not pid:
        return False
    st = _proc_stat(pid)
    if st is None or len(st) < 22 or st[2] == "Z":
        return False
    return start is None or st[21] == start


def _group_alive(pgid: int | None) -> bool:
    if not pgid:
        return False
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _killpg(pgid: int | None, sig: int) -> None:
    if not pgid:
        return
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def parse_until(until: Any) -> tuple[str, Any]:
    """`exit`, `file:<path>` or `log:<regex>` as (kind, value)."""
    if until is None or until == "" or until == "exit":
        return "exit", None
    if not isinstance(until, str):
        raise JobError("until must be exit, file:<path> or log:<regex>")
    if until.startswith("file:") and until[5:]:
        return "file", until[5:]
    if until.startswith("log:") and until[4:]:
        try:
            return "log", re.compile(until[4:])
        except re.error as e:
            raise JobError(f"bad log regex {until[4:]!r}: {e}") from None
    raise JobError(f"until must be exit, file:<path> or log:<regex>, not {until!r}")


def _deadline(value: Any, default: float = WAIT_DEFAULT_S) -> float:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise JobError("deadline_s must be a number")
    return min(max(float(value), 0.0), WAIT_MAX_S)


def _int_arg(value: Any, name: str, default: int | None = None) -> int | None:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise JobError(f"{name} must be a non-negative integer")
    return value


def _elapsed(rec: dict[str, Any]) -> float:
    end = rec.get("ended") or _now()
    return round(max(end - (rec.get("started") or end), 0.0), 1)


def _shell() -> str:
    return shutil.which("bash") or "/bin/sh"


class JobTable:
    """The daemon's jobs (see the module docstring). Thread-safe."""

    def __init__(self, root: str | Path, jobs_dir: str | Path | None = None, *,
                 base_env: dict[str, str] | None = None) -> None:
        from ozagia.config import state_path
        self.root = Path(root).resolve()
        self.dir = Path(jobs_dir) if jobs_dir else state_path(self.root, JOBS_DIR)
        self.base_env = base_env
        self.cond = threading.Condition()
        self.jobs: dict[str, dict[str, Any]] = {}     # id -> record, oldest first
        self._procs: dict[str, subprocess.Popen] = {}
        self._reapers: dict[str, threading.Thread] = {}
        self.cursors: dict[tuple[str, str], int] = {}
        self.events: list[dict[str, Any]] = []       # job ends, for job_events
        self.event_seq = 0                           # the last event's `seq`
        self._event_seen: dict[str, int] = {}        # reader -> last seq delivered
        self._hints: dict[tuple[str, str], int] = {}
        self.next_id = 1
        self.last_end = time.monotonic()
        self.closed = False
        self.counts = {"jobs_started": 0, "waits": 0, "wait_seconds": 0.0,
                       "polls_avoided": 0, "job_stops": 0, "ps_captures": 0,
                       "sleep_hints": 0, "background_hints": 0, "job_events": 0}
        self._load()

    # -- persistence --------------------------------------------------------- #
    def _load(self) -> None:
        path = self.dir / TABLE_NAME
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        n = data.get("next")
        if isinstance(n, int) and n > 0:
            self.next_id = n
        for rec in data.get("jobs") or []:
            if not isinstance(rec, dict) or not isinstance(rec.get("id"), str):
                continue
            if rec.get("status") in ("running", "detached"):
                if _alive(rec.get("pid"), rec.get("proc_start")):
                    rec["status"] = "detached"
                else:
                    rec["status"] = "gone"
                    rec.setdefault("ended", None)
            rec["owned"] = False
            self.jobs[rec["id"]] = rec

    def _save(self) -> None:
        """Write the table (call with `cond` held)."""
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            path = self.dir / TABLE_NAME
            tmp = path.with_suffix(".tmp")
            recs = [{k: v for k, v in r.items() if k != "owned"}
                    for r in self.jobs.values()]
            tmp.write_text(json.dumps({"next": self.next_id, "jobs": recs},
                                      indent=1, sort_keys=True), encoding="utf-8")
            os.replace(tmp, path)
        except OSError as e:
            print(f"job table write failed: {e}", file=sys.stderr, flush=True)

    def _evict(self) -> None:
        """Drop the oldest finished jobs beyond MAX_JOBS (with `cond` held)."""
        over = len(self.jobs) - MAX_JOBS
        if over <= 0:
            return
        for jid in [j for j, r in self.jobs.items()
                    if r.get("status") not in ("running", "detached")][:over]:
            rec = self.jobs.pop(jid)
            self._procs.pop(jid, None)
            self._reapers.pop(jid, None)
            for key in [k for k in self.cursors if k[0] == jid]:
                del self.cursors[key]
            try:
                Path(rec["log"]).unlink()
            except (OSError, KeyError, TypeError):
                pass

    # -- queries ------------------------------------------------------------- #
    def running_count(self) -> int:
        with self.cond:
            return sum(1 for r in self.jobs.values()
                       if r.get("status") == "running" and r.get("owned"))

    def running(self) -> list[dict[str, Any]]:
        with self.cond:
            return [dict(r) for r in self.jobs.values()
                    if r.get("status") in ("running", "detached")]

    def get(self, jid: Any) -> dict[str, Any]:
        if not isinstance(jid, str) or not jid:
            raise JobError("job is required (a job id such as j1)")
        rec = self.jobs.get(jid)
        if rec is None:
            raise JobError(f"unknown job {jid!r}")
        return rec

    def public(self, rec: dict[str, Any]) -> dict[str, Any]:
        """A job record as the ops report it."""
        log = Path(rec.get("log") or "")
        try:
            size = log.stat().st_size
        except OSError:
            size = None
        out = {k: rec.get(k) for k in (
            "id", "command", "argv", "cwd", "env_keys", "pid", "pgid", "status",
            "exit_code", "signal", "started", "ended", "detach", "session",
            "harness", "log")}
        out["elapsed_s"] = _elapsed(rec)
        out["log_bytes"] = size
        return out

    def flat(self, rec: dict[str, Any]) -> dict[str, Any]:
        """`public(rec)` with `id` as `job`, for replies that are the job
        itself (the protocol's `id` is the request's)."""
        out = self.public(rec)
        out["job"] = out.pop("id")
        return out

    # -- start --------------------------------------------------------------- #
    def _cwd(self, cwd: Any) -> Path:
        if cwd is None or cwd == "":
            return self.root
        if not isinstance(cwd, str):
            raise JobError("cwd must be a string")
        p = Path(cwd)
        if not p.is_absolute():
            p = self.root / p
        real = Path(os.path.realpath(p))
        if real != self.root and self.root not in real.parents:
            raise JobError(f"cwd {cwd} is outside the project root {self.root}")
        if not real.is_dir():
            raise JobError(f"cwd {cwd} is not a directory")
        return real

    def start(self, *, command: Any = None, argv: Any = None, cwd: Any = None,
              env: Any = None, detach: bool = False, session: Any = None,
              harness: Any = None) -> dict[str, Any]:
        if (command is None) == (argv is None):
            raise JobError("give exactly one of command (a shell string) and "
                           "argv (a list of strings)")
        if argv is not None:
            if (not isinstance(argv, list) or not argv
                    or not all(isinstance(a, str) for a in argv)):
                raise JobError("argv must be a non-empty list of strings")
            text = shlex.join(argv)
            args = list(argv)
        else:
            if not isinstance(command, str) or not command.strip():
                raise JobError("command must be a non-empty string")
            text = command
            args = [_shell(), "-c", command]
        if len(text) > MAX_COMMAND_CHARS:
            raise JobError(f"command is longer than {MAX_COMMAND_CHARS} characters")
        from ozagia.intent.shell import classify
        got = classify(text)
        if got["class"] == "git_refs":
            raise JobError(f"refused: `git {got.get('subcommand')}` moves refs; "
                           "leave commits, branches and stashes to the user")
        if env is None:
            env = {}
        if (not isinstance(env, dict) or len(env) > MAX_ENV
                or not all(isinstance(k, str) and _ENV_KEY.match(k)
                           and isinstance(v, str) for k, v in env.items())):
            raise JobError(f"env must map at most {MAX_ENV} variable names to strings")
        wd = self._cwd(cwd)
        if self.base_env is not None:
            base = dict(self.base_env)
        else:
            from ozagia.daemon.server import captured_test_env
            base = captured_test_env()
        full_env = {**base, **env, "PWD": str(wd)}
        with self.cond:
            if self.closed:
                raise JobError("the daemon is shutting down")
            if sum(1 for r in self.jobs.values()
                   if r.get("status") == "running" and r.get("owned")) >= MAX_RUNNING:
                raise JobError(f"{MAX_RUNNING} jobs are running already; "
                               "stop one first (job_stop)")
            jid = f"j{self.next_id}"
            self.next_id += 1
            self.dir.mkdir(parents=True, exist_ok=True)
            log = self.dir / f"{jid}.log"
            try:
                with open(log, "wb") as fh, open(os.devnull, "rb") as devnull:
                    proc = subprocess.Popen(
                        args, cwd=str(wd), env=full_env, stdin=devnull,
                        stdout=fh, stderr=subprocess.STDOUT,
                        start_new_session=True, close_fds=True)
            except OSError as e:
                raise JobError(f"cannot start {text!r}: {e}") from None
            rel = os.path.relpath(wd, self.root)
            rec = {"id": jid, "command": text,
                   "argv": list(argv) if argv is not None else None,
                   "cwd": "." if rel == "." else rel.replace(os.sep, "/"),
                   "env_keys": sorted(env), "pid": proc.pid, "pgid": proc.pid,
                   "proc_start": _proc_start(proc.pid), "status": "running",
                   "exit_code": None, "signal": None, "started": _now(),
                   "ended": None, "detach": bool(detach), "log": str(log),
                   "session": session if isinstance(session, str) else None,
                   "harness": harness if isinstance(harness, str) else None,
                   "owned": True}
            self.jobs[jid] = rec
            self._procs[jid] = proc
            self.counts["jobs_started"] += 1
            self._evict()
            self._save()
            t = threading.Thread(target=self._reap, args=(jid, proc),
                                 name=f"ozagia-job-{jid}", daemon=True)
            self._reapers[jid] = t
            t.start()
            return self.public(rec)

    def _reap(self, jid: str, proc: subprocess.Popen) -> None:
        code = proc.wait()
        with self.cond:
            rec = self.jobs.get(jid)
            if rec is not None:
                rec["ended"] = _now()
                if code < 0:
                    rec["status"] = "killed"
                    try:
                        rec["signal"] = signal.Signals(-code).name
                    except ValueError:
                        rec["signal"] = str(-code)
                    rec["exit_code"] = 128 - code
                else:
                    rec["status"] = "exited"
                    rec["exit_code"] = code
                self.event_seq += 1
                self.events.append({
                    "seq": self.event_seq, "job": jid, "command": rec["command"], "status": rec["status"],
                    "exit_code": rec["exit_code"], "signal": rec["signal"],
                    "elapsed_s": _elapsed(rec), "session": rec.get("session"),
                    "harness": rec.get("harness")})
                del self.events[:-200]
                self._save()
            self.last_end = time.monotonic()
            self.cond.notify_all()

    # -- logs ---------------------------------------------------------------- #
    def read_new(self, rec: dict[str, Any], start: int, *,
                 tail_lines: int = TAIL_LINES, tail_bytes: int = TAIL_BYTES
                 ) -> dict[str, Any]:
        """The log text from byte `start` to the end, tail-capped."""
        path = Path(rec.get("log") or "")
        try:
            size = path.stat().st_size
        except OSError:
            return {"output": "", "lines": 0, "cursor": start,
                    "skipped_lines": 0, "skipped_bytes": 0}
        if start > size:            # the log was replaced
            start = 0
        begin = max(start, size - tail_bytes)
        try:
            with open(path, "rb") as fh:
                fh.seek(begin)
                data = fh.read(size - begin)
        except OSError:
            data = b""
        end = begin + len(data)
        skipped = begin - start
        if skipped and b"\n" in data:
            cut = data.index(b"\n") + 1
            data = data[cut:]
            skipped += cut
        if (rec.get("status") in ("running", "detached") and data
                and not data.endswith(b"\n") and b"\n" in data):
            keep = data.rindex(b"\n") + 1
            end -= len(data) - keep
            data = data[:keep]
        lines = data.decode("utf-8", errors="replace").splitlines()
        skipped_lines = max(len(lines) - tail_lines, 0)
        if skipped_lines:
            lines = lines[-tail_lines:]
        return {"output": "\n".join(lines), "lines": len(lines), "cursor": end,
                "skipped_lines": skipped_lines, "skipped_bytes": skipped}

    def _cursor(self, jid: str, reader: str, cursor: Any) -> int:
        explicit = _int_arg(cursor, "cursor")
        if explicit is not None:
            return explicit
        return self.cursors.get((jid, reader), 0)

    def _set_cursor(self, jid: str, reader: str, value: int) -> None:
        self.cursors.pop((jid, reader), None)
        self.cursors[(jid, reader)] = value
        while len(self.cursors) > MAX_READERS:
            del self.cursors[next(iter(self.cursors))]

    def log(self, jid: Any, *, reader: str = "default", cursor: Any = None,
            tail: Any = None) -> dict[str, Any]:
        tail_lines = min(_int_arg(tail, "tail", TAIL_LINES) or TAIL_LINES, 2000)
        with self.cond:
            rec = self.get(jid)
            start = self._cursor(rec["id"], reader, cursor)
            got = self.read_new(rec, start, tail_lines=tail_lines,
                                tail_bytes=max(TAIL_BYTES, tail_lines * 160))
            self._set_cursor(rec["id"], reader, got["cursor"])
            return {**self.flat(rec), **got}

    # -- wait ---------------------------------------------------------------- #
    def _done(self, rec: dict[str, Any]) -> bool:
        st = rec.get("status")
        if st == "detached":
            if not _alive(rec.get("pid"), rec.get("proc_start")):
                rec["status"] = "gone"
                rec["ended"] = _now()
                return True
            return False
        return st != "running"

    def wait(self, job: Any = None, *, until: Any = None, deadline_s: Any = None,
             reader: str = "default", cursor: Any = None) -> dict[str, Any]:
        kind, value = parse_until(until)
        deadline = _deadline(deadline_s)
        t0 = time.monotonic()
        with self.cond:
            if job in (None, "") and kind != "file":
                raise JobError("job is required (a job id, or \"any\")")
            if job == "any":
                targets = [r for r in self.jobs.values()
                           if r.get("status") in ("running", "detached")]
                if not targets:
                    self.counts["waits"] += 1
                    return {"ok": True, "job": None, "met": kind == "exit",
                            "status": "no_running_jobs", "waited_s": 0.0,
                            "output": "", "note": "no job is running"}
            elif job in (None, ""):
                targets = []
            else:
                targets = [self.get(job)]
            starts = {r["id"]: self._cursor(r["id"], reader,
                                             cursor if len(targets) == 1 else None)
                      for r in targets}
            scan = dict(starts)
            partial = {r["id"]: b"" for r in targets}
            fpath: Path | None = None
            if kind == "file":
                p = Path(os.path.expanduser(value))
                if not p.is_absolute():
                    base = (self._cwd(targets[0].get("cwd")) if len(targets) == 1
                            else self.root)
                    p = base / p
                fpath = p
            subject = targets[0] if len(targets) == 1 else None
            met = False
            match = None
            while True:
                if kind == "exit":
                    ended = [r for r in targets if self._done(r)]
                    if ended:
                        subject, met = ended[0], True
                elif kind == "file":
                    assert fpath is not None
                    if fpath.exists():
                        met = True
                else:
                    for r in targets:
                        hit, scan[r["id"]], partial[r["id"]] = self._scan(
                            r, scan[r["id"]], partial[r["id"]], value)
                        if hit is not None:
                            subject, met, match = r, True, hit
                            break
                if met:
                    break
                ended = [r for r in targets if self._done(r)]
                if kind != "exit" and ended:
                    if kind == "log":     # lines written just before the exit
                        r = ended[0]
                        hit, scan[r["id"]], partial[r["id"]] = self._scan(
                            r, scan[r["id"]], partial[r["id"]], value, final=True)
                        if hit is not None:
                            subject, met, match = r, True, hit
                            break
                    subject = ended[0]
                    break
                left = deadline - (time.monotonic() - t0)
                if left <= 0 or self.closed:
                    break
                self.cond.wait(min(POLL_S, left))
            waited = round(time.monotonic() - t0, 2)
            self.counts["waits"] += 1
            self.counts["wait_seconds"] = round(self.counts["wait_seconds"] + waited, 2)
            out: dict[str, Any] = {"ok": True, "until": until or "exit",
                                   "met": met, "waited_s": waited,
                                   "deadline_s": deadline}
            if match is not None:
                out["match"] = match
            if fpath is not None:
                out["file"] = str(fpath)
            if subject is None:
                out.update(job=None, status="waiting" if not met else "met",
                           output="")
                if met:
                    self.counts["polls_avoided"] += 1
                return out
            got = self.read_new(subject, starts[subject["id"]])
            self._set_cursor(subject["id"], reader, got["cursor"])
            pub = self.public(subject)
            out.update({"job": subject["id"], "status": pub["status"],
                        "exit_code": pub["exit_code"], "signal": pub["signal"],
                        "elapsed_s": pub["elapsed_s"], "command": pub["command"],
                        **got})
            if len(targets) > 1:
                out["others"] = [{"job": r["id"], "status": r.get("status")}
                                 for r in targets if r is not subject]
            if met or got["lines"] or pub["status"] not in ("running", "detached"):
                self.counts["polls_avoided"] += 1
            if not met and pub["status"] in ("running", "detached"):
                out["note"] = (f"still running after {pub['elapsed_s']} s; call "
                               f"job_wait again (job {subject['id']!r}) for the "
                               "next output")
            return out

    def _scan(self, rec: dict[str, Any], offset: int, partial: bytes,
              rx: re.Pattern, final: bool = False) -> tuple[str | None, int, bytes]:
        """Search the log text after `offset` for a line matching `rx`."""
        path = Path(rec.get("log") or "")
        try:
            with open(path, "rb") as fh:
                fh.seek(offset)
                data = fh.read(SCAN_CHUNK)
        except OSError:
            return None, offset, partial
        offset += len(data)
        buf = partial + data
        lines = buf.split(b"\n")
        rest = lines.pop()
        if final and rest:
            lines.append(rest)
            rest = b""
        for raw in lines:
            text = raw.decode("utf-8", errors="replace")
            if rx.search(text):
                return text[:500], offset, rest
        return None, offset, rest[-65536:]

    # -- events (OpenCode announcements) ------------------------------------- #
    def wait_events(self, *, reader: str, after_seq: Any = None,
                    deadline_s: Any = None) -> dict[str, Any]:
        """Job ends after `after_seq` (default: the last one `reader` was
        given; a new reader: from now), waiting up to the deadline for one
        while a job runs. Returns them and the latest `seq`."""
        deadline = _deadline(deadline_s, 300.0)
        t0 = time.monotonic()
        with self.cond:
            since = _int_arg(after_seq, "after_seq")
            if since is None:
                since = self._event_seen.get(reader, self.event_seq)
            while True:
                new = [e for e in self.events if e["seq"] > since]
                running = sum(1 for r in self.jobs.values()
                              if r.get("status") == "running")
                left = deadline - (time.monotonic() - t0)
                if new or not running or left <= 0 or self.closed:
                    break
                self.cond.wait(min(1.0, left))
            self._event_seen.pop(reader, None)
            self._event_seen[reader] = self.event_seq
            while len(self._event_seen) > MAX_READERS:
                del self._event_seen[next(iter(self._event_seen))]
            self.counts["job_events"] += len(new)
            return {"ok": True, "events": new, "running": running,
                    "seq": self.event_seq}

    # -- stop ---------------------------------------------------------------- #
    def stop(self, jid: Any, *, sig: Any = "TERM", grace_s: Any = None) -> dict[str, Any]:
        name = str(sig or "TERM").upper().removeprefix("SIG")
        if name not in _SIGNALS:
            raise JobError(f"signal must be one of {', '.join(_SIGNALS)}")
        grace = STOP_GRACE_S if grace_s is None else _deadline(grace_s)
        with self.cond:
            rec = self.get(jid)
            if rec.get("status") == "detached":
                if _alive(rec.get("pid"), rec.get("proc_start")):
                    _killpg(rec.get("pgid"), _SIGNALS[name])
            elif rec.get("status") == "running":
                _killpg(rec.get("pgid"), _SIGNALS[name])
            else:
                _killpg(rec.get("pgid") if rec.get("owned") else None, signal.SIGKILL)
                return {"ok": True, **self.flat(rec),
                        "note": f"job {rec['id']} had already ended"}
            self.counts["job_stops"] += 1
            t0 = time.monotonic()
            while not self._done(rec) and time.monotonic() - t0 < grace:
                self.cond.wait(0.05)
            if not self._done(rec) and name != "KILL":
                _killpg(rec.get("pgid"), signal.SIGKILL)
                t1 = time.monotonic()
                while not self._done(rec) and time.monotonic() - t1 < 2.0:
                    self.cond.wait(0.05)
            # Members of the group that outlived the leader.
            if rec.get("owned") and _group_alive(rec.get("pgid")):
                _killpg(rec.get("pgid"), signal.SIGKILL)
            if rec.get("status") == "detached" and self._done(rec):
                self._save()
            return {"ok": True, **self.flat(rec)}

    def shutdown(self, grace_s: float = SHUTDOWN_GRACE_S) -> list[str]:
        """Stop owned jobs that were not started with `detach`; leave the
        others running. Returns the log lines written."""
        notes: list[str] = []
        with self.cond:
            self.closed = True
            self.cond.notify_all()
            doomed = [r for r in self.jobs.values()
                      if r.get("owned") and not r.get("detach")]
            for r in doomed:
                if r.get("status") == "running" or _group_alive(r.get("pgid")):
                    _killpg(r.get("pgid"), signal.SIGTERM)
            t0 = time.monotonic()
            while (any(r.get("status") == "running" for r in doomed)
                   and time.monotonic() - t0 < grace_s):
                self.cond.wait(0.05)
            for r in doomed:
                if r.get("status") == "running" or _group_alive(r.get("pgid")):
                    _killpg(r.get("pgid"), signal.SIGKILL)
                    if r.get("status") == "running":
                        notes.append(f"killed job {r['id']} (pid {r['pid']}): "
                                     f"{r['command'][:200]}")
            t1 = time.monotonic()
            while (any(r.get("status") == "running" for r in doomed)
                   and time.monotonic() - t1 < 2.0):
                self.cond.wait(0.05)
            for r in self.jobs.values():
                if r.get("owned") and r.get("detach") and r.get("status") == "running":
                    notes.append(f"leaving detached job {r['id']} (pid {r['pid']}) "
                                 f"running: {r['command'][:200]}")
            if self.jobs:
                self._save()
        for line in notes:
            print(line, file=sys.stderr, flush=True)
        return notes

    # -- the protocol -------------------------------------------------------- #
    def dispatch(self, op: str, msg: dict[str, Any]) -> dict[str, Any]:
        reader = msg.get("reader") or msg.get("session") or "default"
        reader = str(reader)[:200]
        try:
            if op == "job_start":
                rec = self.start(command=msg.get("command"), argv=msg.get("argv"),
                                 cwd=msg.get("cwd"), env=msg.get("env"),
                                 detach=msg.get("detach") is True,
                                 session=msg.get("session"),
                                 harness=msg.get("harness"))
                return {"ok": True, "job": rec}
            if op == "job_wait":
                return self.wait(msg.get("job"), until=msg.get("until"),
                                 deadline_s=msg.get("deadline_s"), reader=reader,
                                 cursor=msg.get("cursor"))
            if op == "job_list":
                with self.cond:
                    for r in self.jobs.values():
                        self._done(r)
                    jobs = [self.public(r) for r in reversed(self.jobs.values())]
                return {"ok": True, "jobs": jobs,
                        "running": sum(1 for j in jobs
                                       if j["status"] in ("running", "detached"))}
            if op == "job_log":
                return {"ok": True, **self.log(msg.get("job"), reader=reader,
                                               cursor=msg.get("cursor"),
                                               tail=msg.get("tail"))}
            if op == "job_stop":
                return self.stop(msg.get("job"), sig=msg.get("signal"),
                                 grace_s=msg.get("grace_s"))
            if op == "job_ps":
                command = msg.get("command")
                if not isinstance(command, str):
                    raise JobError("command must be a string")
                return {"ok": True, "output": self.capture_ps(command)}
            if op == "job_events":
                return self.wait_events(reader=reader, after_seq=msg.get("after_seq"),
                                        deadline_s=msg.get("deadline_s"))
        except JobError as e:
            return {"ok": False, "error": str(e)}
        return {"ok": False, "error": f"unknown op {op!r}"}

    def stats(self) -> dict[str, Any]:
        with self.cond:
            running = sum(1 for r in self.jobs.values() if r.get("status") == "running")
            return {**self.counts, "jobs_running": running,
                    "jobs_known": len(self.jobs)}

    # -- shell capture and hints --------------------------------------------- #
    def capture_ps(self, command: str, *, count: bool = True) -> str | None:
        """The output of `command` when it is a `pgrep` or `ps -p` form
        about a job process (see `capture_ps`), else None. `count`: add it
        to `ps_captures` (not for a preflight)."""
        with self.cond:
            for r in self.jobs.values():
                self._done(r)
            jobs = [dict(r) for r in self.jobs.values()]
        out = capture_ps(command, jobs)
        if out is not None and count:
            with self.cond:
                self.counts["ps_captures"] += 1
        return out

    def hint_ok(self, session: Any, kind: str) -> bool:
        key = (str(session or ""), kind)
        with self.cond:
            n = self._hints.get(key, 0)
            if n >= HINTS_PER_SESSION:
                return False
            self._hints[key] = n + 1
            if len(self._hints) > MAX_READERS:
                del self._hints[next(iter(self._hints))]
            self.counts[f"{kind}_hints"] += 1
            return True

    def shell_hints(self, command: str, session: Any = None) -> list[str]:
        """`[ozagia]` lines for a shell command that ran (see the module
        docstring)."""
        if not isinstance(command, str) or not command.strip():
            return []
        lines: list[str] = []
        secs = sleep_seconds(command)
        if secs is not None and secs >= SLEEP_HINT_S:
            running = [r for r in self.running() if r.get("status") == "running"]
            if running and self.hint_ok(session, "sleep"):
                j = running[-1]["id"]
                lines.append(
                    "[ozagia] a job tool can wait for this: job_wait "
                    f"{{\"job\": \"{j if len(running) == 1 else 'any'}\", "
                    "\"until\": \"exit\"} returns as soon as the job exits "
                    "(or until \"log:<regex>\" / \"file:<path>\"), with only the "
                    "new log lines, instead of sleeping a fixed time")
        if background_start(command) and self.hint_ok(session, "background"):
            lines.append(
                "[ozagia] for a long-running command, job_start {\"command\": "
                "\"...\"} runs it in the background with its output in a log; "
                "job_wait then blocks until it exits or prints a matching line "
                "(no sleep or ps polling), job_log reads new output, job_stop "
                "stops its process group")
        return lines


# --------------------------------------------------------------------------- #
# shell forms
# --------------------------------------------------------------------------- #
_DURATION = re.compile(r"^(\d+(?:\.\d+)?)([smhd]?)$")


def _duration(text: str) -> float | None:
    m = _DURATION.match(text)
    if not m:
        return None
    return float(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]


def sleep_seconds(command: str) -> float | None:
    """The longest `sleep N` the command runs, or None."""
    from ozagia.intent.shell import command_heads
    best: float | None = None
    for head in command_heads(command):
        if not head or os.path.basename(head[0]) != "sleep" or len(head) < 2:
            continue
        total = 0.0
        for a in head[1:]:
            d = _duration(a)
            if d is None:
                total = -1.0
                break
            total += d
        if total >= 0 and (best is None or total > best):
            best = total
    return best


def background_start(command: str) -> bool:
    """Whether the command starts something in the background: a trailing
    or mid-line `&` (not `&&` or `&>`), or `nohup`, `setsid` or `disown`
    in command position."""
    from ozagia.intent.shell import _lex
    toks, _why = _lex(command)
    if not toks:
        return False
    prev = None
    for kind, text, _val in toks:
        if kind == "op" and text == "&":
            return True
        if (kind == "word" and text in ("nohup", "setsid", "disown")
                and (prev is None or prev[0] == "op")):
            return True
        prev = (kind, text)
    return False


def _proc_rows() -> list[tuple[int, str, str]]:
    """(pid, comm, cmdline) of every process, from /proc."""
    rows = []
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        pid = int(name)
        st = _proc_stat(pid)
        if st is None:
            continue
        try:
            raw = Path(f"/proc/{pid}/cmdline").read_bytes()
        except OSError:
            continue
        args = [a.decode("utf-8", errors="replace") for a in raw.split(b"\0")]
        while args and args[-1] == "":
            args.pop()
        rows.append((pid, st[1], " ".join(args)))
    rows.sort()
    return rows


def _cputime(pid: int) -> str | None:
    st = _proc_stat(pid)
    if st is None or len(st) < 15:
        return None
    try:
        ticks = int(st[13]) + int(st[14])
    except ValueError:
        return None
    secs = ticks // os.sysconf("SC_CLK_TCK")
    days, secs = divmod(secs, 86400)
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    text = f"{h:02d}:{m:02d}:{s:02d}"
    return f"{days}-{text}" if days else text


_PGREP_FLAGS = set("fal")
_PS_FIELDS = {"pid", "args", "cmd", "command", "comm"}


def _job_line(rec: dict[str, Any]) -> str:
    st = rec.get("status")
    if st in ("running", "detached"):
        what = f"running {_elapsed(rec)} s"
    elif rec.get("signal"):
        what = f"killed by {rec['signal']} after {_elapsed(rec)} s"
    elif rec.get("exit_code") is not None:
        what = f"exited {rec['exit_code']} after {_elapsed(rec)} s"
    else:
        what = st or "ended"
    tail = (f"; job_wait {{\"job\": \"{rec['id']}\"}} blocks until it exits"
            if st in ("running", "detached") else
            f"; job_log {{\"job\": \"{rec['id']}\"}} shows its output")
    return (f"[ozagia] job {rec['id']} (pid {rec.get('pid')}) {what}: "
            f"{str(rec.get('command'))[:200]}{tail}")


def capture_ps(command: str, jobs: list[dict[str, Any]]) -> str | None:
    """The output of a `pgrep` or `ps -p` command about job processes, or
    None (run it as usual).

    - `pgrep [-f] [-a|-l] PATTERN` (flags may be combined, `-fa`), when
      PATTERN (an ERE without back-references; matched as a Python regex)
      matches a job's command line (`-f`) or process name: every process
      whose command line or name matches, from /proc, as pgrep prints them
      (`PID`, `PID NAME` with -l, `PID CMDLINE` with -a). The shell that
      would have run pgrep (`bash -c 'pgrep -f X'`, which matches X itself)
      is not there to match, so it is not listed.
    - `ps -p PID[,PID...]` (or `--pid`), when every PID is a job's: ps's
      default columns (`PID TTY TIME CMD`; a job has no terminal, so TTY
      is `?`), with `-o FIELD=` for one of pid, args, cmd, command, comm.

    Each job named gets one `[ozagia] job ...` line (status, elapsed, and
    the job_wait or job_log call to use)."""
    if not isinstance(command, str) or not jobs:
        return None
    from ozagia.intent.shell import _ere_safe, classify
    got = classify(command)
    if got["class"] not in ("other",):
        return None
    try:
        toks = shlex.split(command)
    except ValueError:
        return None
    if not toks or any(t in (";", "&&", "||", "|", "&") for t in toks):
        return None
    if toks[0] == "pgrep":
        flags: set[str] = set()
        pats: list[str] = []
        for t in toks[1:]:
            if t.startswith("-") and len(t) > 1 and not pats:
                if not set(t[1:]) <= _PGREP_FLAGS:
                    return None
                flags |= set(t[1:])
            else:
                pats.append(t)
        if len(pats) != 1 or not _ere_safe(pats[0]) or {"a", "l"} <= flags:
            return None
        try:
            rx = re.compile(pats[0])
        except re.error:
            return None
        full = "f" in flags
        named = []
        for r in jobs:
            text = r.get("command") or ""
            if full and rx.search(text):
                named.append(r)
            elif not full and r.get("status") in ("running", "detached"):
                st = _proc_stat(int(r.get("pid") or 0))
                if st and rx.search(st[1]):
                    named.append(r)
        if not named:
            return None
        lines = []
        for pid, comm, cmdline in _proc_rows():
            subject = (cmdline or comm) if full else comm
            if not rx.search(subject):
                continue
            if "a" in flags:
                lines.append(f"{pid} {cmdline or comm}")
            elif "l" in flags:
                lines.append(f"{pid} {comm}")
            else:
                lines.append(str(pid))
        lines.extend(_job_line(r) for r in named)
        return "\n".join(lines)
    if toks[0] == "ps":
        pids_text: str | None = None
        field: str | None = None
        i = 1
        while i < len(toks):
            t = toks[i]
            if t in ("-p", "--pid") and i + 1 < len(toks):
                pids_text, i = toks[i + 1], i + 2
            elif t.startswith("-p") and len(t) > 2:
                pids_text, i = t[2:], i + 1
            elif t.startswith("--pid="):
                pids_text, i = t[6:], i + 1
            elif t == "-o" and i + 1 < len(toks):
                field, i = toks[i + 1], i + 2
            else:
                return None
        if not pids_text or not re.fullmatch(r"\d+(,\d+)*", pids_text):
            return None
        if field is not None:
            if not field.endswith("=") or field[:-1] not in _PS_FIELDS:
                return None
            field = field[:-1]
        by_pid = {int(r["pid"]): r for r in jobs if r.get("pid")}
        pids = sorted({int(p) for p in pids_text.split(",")})
        if not all(p in by_pid for p in pids):
            return None
        lines = [] if field else ["    PID TTY          TIME CMD"]
        for p in pids:
            r = by_pid[p]
            if not _alive(p, r.get("proc_start")):
                continue
            st = _proc_stat(p)
            cpu = _cputime(p)
            if st is None or cpu is None:
                continue
            if field is None:
                lines.append(f"{p:>7} {'?':<8} {cpu:>8} {st[1]}")
            elif field == "pid":
                lines.append(f"{p:>7}")
            elif field == "comm":
                lines.append(st[1])
            else:
                try:
                    raw = Path(f"/proc/{p}/cmdline").read_bytes()
                except OSError:
                    continue
                args = [a.decode("utf-8", errors="replace")
                        for a in raw.split(b"\0") if a]
                lines.append(" ".join(args) or f"[{st[1]}]")
        lines.extend(_job_line(by_pid[p]) for p in pids)
        return "\n".join(lines)
    return None


__all__ = ["JobError", "JobTable", "MAX_JOBS", "OPS", "WAIT_DEFAULT_S",
           "background_start", "capture_ps", "parse_until", "sleep_seconds"]
