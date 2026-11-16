"""Background jobs in the daemon: start a command, wait for it, read its log.

An agent that starts a long command (a server, a build, a test run) and then
polls it (`write_stdin` with no input, `sleep N; tail log`, `ps -p PID`)
spends a model call per poll. A job runs the command under the daemon, which
hooks, the MCP server and the CLI share, and `wait` blocks server-side until
the job exits, a file appears or a log line matches, then returns only the
output that is new to the caller.

Opt-in. A job runs with the daemon's privileges and environment, outside any
harness sandbox, approval prompt or permission preflight. So `job_start` is
refused unless the project enables jobs (`jobs_enabled`): `[jobs] enabled =
true` in the project configuration (`ozagia.config`; `ozagia install codex
--jobs` writes it), or `$OZAGIA_JOBS=1` in the daemon's environment
(`$OZAGIA_JOBS=0` turns them off whatever the file says). Listing, waiting
for, reading and stopping jobs that exist work either way.

A job (`JobTable.start`): an argv list, or a command string run by `bash -c`
(`/bin/sh -c` without bash), in `cwd` (inside the root; default the root),
with the daemon's test environment (`server.captured_test_env`) plus `env`
additions. It starts in a new session, so its own process group (pgid =
pid), with stdin from /dev/null and stdout and stderr appended to
`<state dir>/jobs/<id>.log`. Refused, as best-effort checks (the command
text is parsed, not sandboxed): a ref-moving git command (as in the shell
capture); a command the shell classifier calls `complex` that mentions git
anywhere or pipes or feeds text into a shell (`| bash`, `sh <<< ...`); and
`env` keys starting with `GIT_` (`GIT_CONFIG_*`, `GIT_DIR`, ...). At most
`MAX_RUNNING` jobs run at once; the table keeps the last `MAX_JOBS` jobs
(finished ones beyond that are dropped with their logs, unless processes
of the job still run), and is written to `<state dir>/jobs/jobs.json` on
every change.

Status: `running`, `exited` (exit code), `killed` (by a signal; `signal`
names it), and for jobs a previous daemon left running (see shutdown):
`detached` (still alive; no exit code can be had) or `gone`. Status follows
the whole process group: a job whose leader has exited while other
processes of its group run (`bash -c 'server & ...'`) stays `running`, with
`leader_exited` and `group_alive` true, until the group is empty; its exit
code is then the leader's. Waits, the `MAX_RUNNING` limit and the daemon's
idle timeout count it as running.

Signals go to the job's process group only while it is known to be the
job's. The daemon does not reap a job's leader until the group is empty, so
the pgid cannot be reused while it signals it; once the group is found
empty it is marked `group_gone` and never signalled again. For a job of an
earlier daemon (not its child), a group is the job's only while some
process in it started no earlier than the job's leader (`/proc/<pid>/stat`
field 22) and the pgid's own pid, if alive, is the leader that started then
(`_group_ours`).

Resource limits (`core.resources`, `core.cgroup`): `start` takes limits from
`[resources.limits]` (or explicit `limits=`/`policy=` arguments), runs the
memory/disk admission check before spawning, applies child rlimits
(`RLIMIT_AS`, optional `RLIMIT_FSIZE`) and starts a watchdog after the
spawn. On Linux with a delegated cgroup v2 hierarchy, a per-job
`ozagia/job-<id>` cgroup also gets `memory.max`/`pids.max`, the child joins it
before exec, and `memory.peak`/`memory.events`/`io.stat` are read back; the
record then says `cgroup+rlimit`. Without delegation, a permission failure or
a host without cgroup v2, the job records `rlimit+watch` and a `cgroup` report
that names the fallback reason. The watchdog samples the verified process group
and the cwd filesystem; on a breach it SIGTERMs the group, waits
`RESOURCE_KILL_GRACE_S`, then uses `cgroup.kill` (or SIGKILL) if members
remain. The group is verified again before any signal, and an unverified/stale
group is never signalled. Disk writes are attributed from the cgroup's
`io.stat` when there is one, otherwise from the group's `/proc/<pid>/io`
`write_bytes`; the record carries `peak_disk_write_mb` and
`disk_write_source` (`cgroup.io.stat` or `proc.io`). Those are bytes sent to
storage, not retained blocks. The job record carries `limits`, `enforcement`
(`cgroup+rlimit`, `rlimit+watch` or `off`), `cgroup`, `admission`,
`peak_memory_mb` and `resource_events`; the log gets `[ozagia] resource:`
lines. `[resources] enabled = false` or `watchdog = "off"` opts out (the
latter keeps admission); `watchdog = "rlimit"`/`"watch"` or
`OZAGIA_CGROUPS=0` opts out of cgroups only.

Logs are not truncated while a job runs. Every read is capped instead: a
read returns at most `TAIL_LINES` lines and `TAIL_BYTES` bytes, the newest
ones, and says how many it skipped. Log files are read and matched without
holding the table lock.

Cursors. Every reader (the protocol's `session`, or `reader`; default
"default") has a byte offset per job. `wait` and `log` return the lines from
that offset (or from an explicit `cursor`) to the end of the log, and move
the offset to the end (never back, unless `cursor` is given). A partial last
line of a running job is kept for the next read unless it is the only new
text.

`wait(job | "any", until, deadline_s)`: `until` is `exit` (default),
`file:<path>` (exists; relative to the job's cwd, or the root) or
`log:<regex>` (a new line of the job's log matches; the first
`MATCH_LINE_BYTES` bytes of each line are matched). It returns when the
condition holds, when the job exits (for file and log), or at the deadline
(default `WAIT_DEFAULT_S`, at most `WAIT_MAX_S`), with `met`, the status,
`exit_code`, `elapsed_s` (since the job started), `waited_s`, the new
`output` and the `cursor`. `job: "any"` waits on the jobs running when the
call starts (log too); for `file:` the job may be left out.

Shutdown (`JobTable.shutdown`, when the daemon exits cleanly): each running
job's process group gets SIGTERM, then SIGKILL after `SHUTDOWN_GRACE_S`,
except jobs started with `detach`, which are left running, logged to stderr
(the daemon log) and kept in `jobs.json`; the next daemon lists them as
`detached` (it can stop them, not reap them). `detach` is an explicit
opt-out of "jobs do not outlive the daemon". A daemon that dies without
its shutdown (SIGKILL, OOM) leaves its jobs running; the next daemon stops
the ones not started with `detach` when it loads the table (status `gone`,
`stopped_on_load`).

`capture_ps(command, ...)` answers `pgrep` and `ps -p` commands about job
processes (see its docstring); `shell_hints(command, ...)` gives the
`[ozagia]` lines for a shell command's `after`: `sleep N` while a job runs,
and background starts (`... &`, `nohup`, `setsid`; only when jobs are
enabled).
"""
from __future__ import annotations

import functools
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
from typing import Any, Mapping

from ozagia.core import cgroup as C
from ozagia.core import resources as R

MAX_JOBS = 50
MAX_RUNNING = 16
TAIL_LINES = 200
TAIL_BYTES = 16 * 1024
WAIT_DEFAULT_S = 45.0
WAIT_MAX_S = 3600.0
STOP_GRACE_S = 3.0
SHUTDOWN_GRACE_S = 2.0
RESOURCE_KILL_GRACE_S = 3.0
POLL_S = 0.2
# Bytes of new log text one poll of a `log:` wait scans.
SCAN_CHUNK = 1 << 20
# A `log:` regex sees at most this many bytes of each line.
MATCH_LINE_BYTES = 4096
# How often a job whose leader exited is checked for an empty group.
GROUP_POLL_S = 0.2
ENV_JOBS = "OZAGIA_JOBS"
ENV_CGROUPS = "OZAGIA_CGROUPS"
MAX_ENV = 64
MAX_COMMAND_CHARS = 16000
MAX_READERS = 1000
TABLE_NAME = "jobs.json"
JOBS_DIR = "jobs"
RESOURCE_LOG_PREFIX = "[ozagia] resource:"
MAX_RESOURCE_EVENTS = 50
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


class ResourceRefused(JobError):
    """Admission refused a job before it was started (structured report)."""

    def __init__(self, report: Mapping[str, Any]) -> None:
        self.report = dict(report)
        reason = report.get("reason") or "insufficient_resources"
        requested = report.get("requested") or {}
        shortfall = report.get("shortfall") or {}
        memory = requested.get("memory_mb")
        disk = requested.get("disk_mb")
        detail = []
        if memory is not None:
            detail.append(f"memory {memory:.0f} MiB")
        if disk is not None:
            detail.append(f"disk {disk:.0f} MiB")
        request = ", ".join(detail) or "request"
        super().__init__(
            f"refused: {reason}: {request} does not fit; "
            f"shortfall memory {shortfall.get('memory_mb', 0):.0f} MiB, "
            f"disk {shortfall.get('disk_mb', 0):.0f} MiB"
            + (f"; retry_after_s {report['retry_after_s']}"
               if report.get("retry_after_s") else ""))


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


def _start_int(start: Any) -> int | None:
    try:
        return int(start)
    except (TypeError, ValueError):
        return None


def _group_members(pgid: int | None) -> list[tuple[int, int]]:
    """(pid, start time) of the live (not zombie) processes in group `pgid`."""
    if not pgid:
        return []
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return []
    except PermissionError:
        pass
    out = []
    want = str(pgid)
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        st = _proc_stat(int(name))
        if (st is None or len(st) < 22 or st[4] != want
                or st[2] in ("Z", "X")):
            continue
        start = _start_int(st[21])
        if start is not None:
            out.append((int(name), start))
    return out


def _group_ours(pgid: int | None, start: Any) -> bool:
    """Whether group `pgid` still holds processes of the job whose leader
    (pid `pgid`) started at `start` (`/proc` stat field 22): some member
    started no earlier, and pid `pgid`, if alive, is that leader. A pgid
    reused by a new session fails the second test (its founder has pid
    `pgid` and a later start time)."""
    begin = _start_int(start)
    if not pgid or begin is None:
        return False
    members = _group_members(pgid)
    if not members:
        return False
    lead = _proc_stat(pgid)
    if (lead is not None and len(lead) > 21 and lead[2] not in ("Z", "X")
            and _start_int(lead[21]) != begin):
        return False
    return any(t >= begin for _pid, t in members)


def jobs_enabled(root: str | Path) -> bool:
    """Whether `job_start` is allowed for `root` (see the module docstring)."""
    env = os.environ.get(ENV_JOBS, "").strip().lower()
    if env in ("1", "true", "yes", "on"):
        return True
    if env in ("0", "false", "no", "off"):
        return False
    from ozagia.config import ConfigError, load_project_config
    try:
        return load_project_config(root).jobs_enabled
    except ConfigError:
        return False


def cgroups_enabled() -> bool:
    """Whether cgroup v2 may be attempted for new jobs.

    `OZAGIA_CGROUPS=0` is an emergency opt-out for hosts where a delegated
    hierarchy is present but not usable. The policy's `watchdog` mode is the
    normal control; this variable only turns attempts off.
    """
    env = os.environ.get(ENV_CGROUPS, "").strip().lower()
    return env not in ("0", "false", "no", "off")


def disabled_message(root: str | Path) -> str:
    return (
        "background jobs are off for this project. A job runs with the ozagia "
        "daemon's privileges and environment, outside the harness sandbox and "
        "its approval prompts (Codex, OpenCode), so starting jobs is opt-in: "
        f"add `[jobs]` with `enabled = true` to {Path(root) / '.ozagia.toml'} "
        "(`ozagia install codex --jobs` does that), or start the daemon with "
        f"{ENV_JOBS}=1 in its environment. Existing jobs can still be listed, "
        "waited for, read and stopped")


_SHELLS = r"(?:ba|da|z|k|fi|c|tc|mk)?sh"
_PIPE_TO_SHELL = re.compile(
    r"\|&?\s*(?:\S*/)?(?:(?:env|exec|command|sudo|nohup)\s+)*(?:\S*/)?"
    + _SHELLS + r"\b")
_HERE_TO_SHELL = re.compile(r"(?:^|[\s;&|(/])" + _SHELLS + r"\b[^;&|\n]*<<")
_MENTIONS_GIT = re.compile(r"\bgit\b")


def _refusal(text: str) -> str | None:
    """Why the job command `text` is refused, or None (see the module
    docstring; best-effort parsing)."""
    from ozagia.intent.shell import classify
    got = classify(text)
    if got["class"] == "git_refs":
        return (f"refused: `git {got.get('subcommand')}` moves refs; leave "
                "commits, branches and stashes to the user")
    if got["class"] == "complex":
        if _MENTIONS_GIT.search(text):
            return ("refused: the command mentions git in a form the shell "
                    "check cannot follow (pipes, substitutions, loops, "
                    "variable assignments); run git commands directly, and "
                    "leave ref-moving ones to the user")
        if _PIPE_TO_SHELL.search(text) or _HERE_TO_SHELL.search(text):
            return ("refused: the command feeds text to a shell (`| bash`, "
                    "`sh <<< ...`), which the check cannot see into; run the "
                    "commands themselves")
    return None


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
                 base_env: dict[str, str] | None = None,
                 enabled: bool | None = None,
                 resource_policy: Mapping[str, Any] | None = None,
                 cgroup_base: str | Path | None = None,
                 cgroup_manager: Any | None = None) -> None:
        from ozagia.config import state_path
        self.root = Path(root).resolve()
        self.dir = Path(jobs_dir) if jobs_dir else state_path(self.root, JOBS_DIR)
        self.base_env = base_env
        # None: `jobs_enabled(root)` decides at each start.
        self.enabled = enabled
        # A loaded `[resources]` policy, or None to load it per start.
        self.resource_policy = (dict(resource_policy)
                                if resource_policy is not None else None)
        # A fake base or manager is for tests: the default is the host's
        # `/sys/fs/cgroup`, and every write is best-effort.
        self.cgroup_base = cgroup_base
        self._cgroup_manager = cgroup_manager
        self._default_cgroup_manager: C.CgroupManager | None = None
        self.cond = threading.Condition()
        self.jobs: dict[str, dict[str, Any]] = {}     # id -> record, oldest first
        # Owned jobs whose leader is not reaped yet (its pgid is reserved).
        self._procs: dict[str, subprocess.Popen] = {}
        self._reapers: dict[str, threading.Thread] = {}
        self._watchdogs: dict[str, R.ResourceWatchdog] = {}
        self._cgroups: dict[str, Any] = {}
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
                       "sleep_hints": 0, "background_hints": 0, "job_events": 0,
                       "jobs_refused": 0, "stopped_on_load": 0,
                       "jobs_resource_killed": 0, "resource_events": 0,
                       "resource_peak_mb": 0.0,
                       "resource_peak_disk_write_mb": 0.0,
                       "resource_refusals": 0,
                       "cgroup_jobs": 0, "cgroup_fallbacks": 0,
                       "cgroup_oom_kills": 0}
        self._load()

    def is_enabled(self) -> bool:
        return self.enabled if self.enabled is not None else jobs_enabled(self.root)

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
        now = _now()
        orphans: list[dict[str, Any]] = []
        for rec in data.get("jobs") or []:
            if not isinstance(rec, dict) or not isinstance(rec.get("id"), str):
                continue
            rec["owned"] = False
            if rec.get("status") in ("running", "detached"):
                if not self._live(rec):
                    rec["status"] = "gone"
                    rec["group_gone"] = True
                elif rec.get("detach"):
                    rec["status"] = "detached"
                else:
                    # The previous daemon died without its shutdown.
                    orphans.append(rec)
            if rec.get("status") == "gone" and not rec.get("ended"):
                rec["ended"] = now
            self.jobs[rec["id"]] = rec
        if orphans:
            self._stop_orphans(orphans)
            self._save()
        # A previous daemon may have left an empty per-job cgroup behind.
        for rec in self.jobs.values():
            if rec.get("status") not in ("running", "detached"):
                self._cleanup_record_cgroup(rec)

    def _stop_orphans(self, recs: list[dict[str, Any]]) -> None:
        """Stop jobs an earlier daemon started without `detach` and left
        running (it died without its shutdown)."""
        for r in recs:
            self._signal(r, signal.SIGTERM)
        t0 = time.monotonic()
        while (any(self._live(r) for r in recs)
               and time.monotonic() - t0 < SHUTDOWN_GRACE_S):
            time.sleep(0.05)
        for r in recs:
            if self._live(r):
                self._signal(r, signal.SIGKILL)
        t1 = time.monotonic()
        while any(self._live(r) for r in recs) and time.monotonic() - t1 < 2.0:
            time.sleep(0.05)
        now = _now()
        for r in recs:
            r.update(status="gone", ended=r.get("ended") or now,
                     stopped_on_load=True, group_gone=not self._live(r))
            self.counts["stopped_on_load"] += 1
            print(f"stopped job {r['id']} (pid {r.get('pid')}) left running by "
                  f"an earlier daemon: {str(r.get('command'))[:200]}",
                  file=sys.stderr, flush=True)

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
        """Drop the oldest finished jobs beyond MAX_JOBS (with `cond` held).
        A job with processes still running is kept, so shutdown and
        job_stop can still reach them."""
        over = len(self.jobs) - MAX_JOBS
        if over <= 0:
            return
        done = [j for j, r in self.jobs.items()
                if r.get("status") not in ("running", "detached")]
        for jid in done:
            if over <= 0:
                break
            rec = self.jobs[jid]
            if jid in self._procs or self._live(rec):
                continue
            over -= 1
            handle = self._cgroups.pop(jid, None)
            if handle is not None:
                try:
                    handle.cleanup()
                except Exception:  # noqa: BLE001 - eviction is best-effort
                    pass
            self._cleanup_record_cgroup(rec)
            del self.jobs[jid]
            self._reapers.pop(jid, None)
            self._watchdogs.pop(jid, None)
            for key in [k for k in self.cursors if k[0] == jid]:
                del self.cursors[key]
            try:
                Path(rec["log"]).unlink()
            except (OSError, KeyError, TypeError):
                pass

    # -- process groups ------------------------------------------------------ #
    def _live(self, rec: dict[str, Any]) -> bool:
        """Whether processes of the job still run. An owned job: its status
        (it is `running` until the group is empty). Another daemon's: its
        leader, or its group (`_group_ours`) unless found empty before."""
        if rec.get("owned"):
            return rec.get("status") == "running"
        start = rec.get("proc_start")
        if start is not None and _alive(rec.get("pid"), start):
            return True
        if rec.get("group_gone"):
            return False
        if _group_ours(rec.get("pgid"), start):
            return True
        rec["group_gone"] = True
        return False

    def _signal(self, rec: dict[str, Any], sig: int) -> bool:
        """Send `sig` to the job's process group if it is still the job's
        (see the module docstring). Returns whether it was sent."""
        if rec.get("group_gone"):
            return False
        pgid = rec.get("pgid")
        if rec.get("owned") and rec.get("id") in self._procs:
            _killpg(pgid, sig)        # the unreaped leader holds the pgid
            return True
        if not rec.get("owned") and _group_ours(pgid, rec.get("proc_start")):
            _killpg(pgid, sig)
            return True
        rec["group_gone"] = True
        return False

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
            "harness", "log", "enforcement", "admission")}
        out["limits"] = rec.get("limits")
        out["cgroup"] = rec.get("cgroup")
        out["peak_memory_mb"] = rec.get("peak_memory_mb")
        out["peak_disk_write_mb"] = rec.get("peak_disk_write_mb")
        out["disk_write_source"] = rec.get("disk_write_source")
        out["resource_events"] = list(rec.get("resource_events") or [])
        out["elapsed_s"] = _elapsed(rec)
        out["log_bytes"] = size
        if rec.get("leader_exited") and rec.get("status") == "running":
            out["leader_exited"] = True
            out["group_alive"] = True
        if rec.get("stopped_on_load"):
            out["stopped_on_load"] = True
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

    # -- resources ----------------------------------------------------------- #
    def _policy_for(self, policy: Mapping[str, Any] | None) -> dict[str, Any]:
        if policy is not None:
            return R._deep_merge(R.DEFAULT_POLICY, dict(policy))
        if self.resource_policy is not None:
            return R._deep_merge(R.DEFAULT_POLICY, dict(self.resource_policy))
        return R.load_policy(self.root)

    @staticmethod
    def _admission_summary(report: Mapping[str, Any] | None) -> dict[str, Any] | None:
        if report is None:
            return None
        return {k: report.get(k) for k in (
            "ok", "reason", "shortfall", "retry_after_s", "suggestions",
            "requested", "available", "disabled")}

    @staticmethod
    def _disk_reserve_gb(policy: Mapping[str, Any],
                         admission: Mapping[str, Any] | None,
                         path: Path) -> float:
        try:
            reserve = float(policy.get("reserve_disk_gb") or 0.0)
        except (TypeError, ValueError):
            reserve = 0.0
        try:
            pct = float(policy.get("reserve_disk_pct") or 0.0)
        except (TypeError, ValueError):
            pct = 0.0
        total = ((admission or {}).get("available") or {}).get("disk_total_gb")
        if pct > 0 and not isinstance(total, (int, float)):
            total = R.disk_usage(path).get("total_gb")
        if pct > 0 and isinstance(total, (int, float)):
            reserve = max(reserve, float(total) * pct / 100)
        return max(0.0, reserve)

    @staticmethod
    def _child_preexec(limits: R.ResourceLimits, cgroup: Any | None = None):
        if os.name != "posix":
            return None
        # RLIMIT_NPROC is per UID: applying the job's pids limit there would
        # also count unrelated processes of the same user. The cgroup (or the
        # watchdog, without one) enforces the job's process count instead.
        if cgroup is None:
            return functools.partial(R.apply_child_limits, limits,
                                     apply_nproc=False)
        path = cgroup.path

        def prepare() -> None:
            R.apply_child_limits(limits, apply_nproc=False)
            C.join_self(path)

        return prepare

    def _manager(self) -> Any:
        """The injected manager, or one detector per job table."""
        if self._cgroup_manager is not None:
            return self._cgroup_manager
        if self._default_cgroup_manager is None:
            self._default_cgroup_manager = C.CgroupManager(self.cgroup_base)
        return self._default_cgroup_manager

    @staticmethod
    def _cgroup_record(mechanism: str, report: Mapping[str, Any] | None,
                       used: bool) -> dict[str, Any]:
        data = dict(report) if isinstance(report, Mapping) else {}
        status = "active" if used else (
            "fallback" if data else "off")
        return {
            "mechanism": mechanism,
            "attempted": bool(data),
            "used": bool(used),
            "status": status,
            "path": data.get("path"),
            "limits": data.get("limits") or {},
            "peak_memory_mb": None,
            "io_read_mb": None,
            "io_write_mb": None,
            "io_source": None,
            "oom_events": None,
            "oom_kill": None,
            "fallback": bool(data.get("fallback")),
            "reason": data.get("reason"),
            "errors": list(data.get("errors") or []),
            "cleaned_up": None,
        }

    def _note_disk_peak(self, rec: dict[str, Any], value: Any,
                        source: Any = None) -> None:
        """Raise a job's attributed write peak; prefer the cgroup counter.

        Once a job has a cgroup value, a process-group sample never replaces it
        or its ``cgroup.io.stat`` source label. Call with ``cond`` held.
        """
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return
        source = str(source) if source else None
        peak = max(0.0, round(float(value), 4))
        old = rec.get("peak_disk_write_mb")
        old_source = rec.get("disk_write_source")
        if source == "cgroup.io.stat":
            if old_source != "cgroup.io.stat":
                rec["peak_disk_write_mb"] = peak
            elif not isinstance(old, (int, float)) or peak > float(old):
                rec["peak_disk_write_mb"] = peak
            rec["disk_write_source"] = "cgroup.io.stat"
        elif old_source == "cgroup.io.stat":
            return
        else:
            if not isinstance(old, (int, float)) or peak > float(old):
                rec["peak_disk_write_mb"] = peak
            if source:
                rec["disk_write_source"] = source
        current = rec.get("peak_disk_write_mb")
        if isinstance(current, (int, float)):
            self.counts["resource_peak_disk_write_mb"] = max(
                float(self.counts.get("resource_peak_disk_write_mb") or 0.0),
                float(current))

    def _merge_cgroup_stats(self, jid: str, stats: Mapping[str, Any]) -> None:
        """Record a cgroup sample/peak on the job (call with or without lock)."""
        with self.cond:
            rec = self.jobs.get(jid)
            if rec is None:
                return
            info = rec.setdefault("cgroup", {})
            peak = stats.get("memory_peak_mb")
            if isinstance(peak, (int, float)):
                old = info.get("peak_memory_mb")
                info["peak_memory_mb"] = round(
                    max(float(old or 0.0), float(peak)), 2)
                rec_peak = rec.get("peak_memory_mb")
                value = round(max(float(rec_peak or 0.0), float(peak)), 2)
                rec["peak_memory_mb"] = value
                self.counts["resource_peak_mb"] = max(
                    float(self.counts.get("resource_peak_mb") or 0.0), value)
            for key in ("memory_current_mb", "memory_max_mb", "pids_current",
                        "pids_max", "oom_events", "oom_kill", "populated",
                        "peak_source"):
                if key in stats:
                    info[key] = stats[key]
            for key in ("io_read_mb", "io_write_mb"):
                value = stats.get(key)
                if (isinstance(value, (int, float))
                        and not isinstance(value, bool)):
                    old = info.get(key)
                    info[key] = round(max(float(old or 0.0),
                                          float(value)), 4)
            if stats.get("io_source"):
                info["io_source"] = stats["io_source"]
            for key in ("io_devices", "io_device_count"):
                if stats.get(key) is not None:
                    info[key] = stats[key]
            self._note_disk_peak(rec, stats.get("io_write_mb"),
                                 stats.get("io_source"))

    def _sample_cgroup(self, jid: str) -> dict[str, Any] | None:
        """Read and merge one cgroup sample; return it for the watchdog."""
        with self.cond:
            handle = self._cgroups.get(jid)
        if handle is None:
            return None
        try:
            stats = handle.stats()
        except Exception:  # noqa: BLE001 - observation is best-effort
            return None
        if isinstance(stats, Mapping):
            self._merge_cgroup_stats(jid, stats)
            return dict(stats)
        return None

    def _cleanup_record_cgroup(self, rec: dict[str, Any]) -> None:
        """Best-effort removal of a recorded cgroup, never inside another job."""
        info = rec.get("cgroup")
        if not isinstance(info, dict) or not info.get("path"):
            return
        if info.get("cleaned_up"):
            return
        try:
            result = C.remove_path(info["path"], base=self.cgroup_base)
        except Exception:  # noqa: BLE001 - cleanup must not fail a job
            return
        if not isinstance(result, Mapping):
            return
        if result.get("removed") or result.get("reason") == "absent":
            info["cleaned_up"] = True
        elif result.get("reason"):
            info["cleanup_reason"] = result["reason"]

    def _finish_cgroup(self, jid: str, rec: dict[str, Any] | None) -> None:
        """Read the final cgroup counters and remove the group (with `cond`)."""
        handle = self._cgroups.pop(jid, None)
        if handle is None:
            if rec is not None:
                self._cleanup_record_cgroup(rec)
            return
        if rec is None:
            try:
                handle.cleanup()
            except Exception:  # noqa: BLE001
                pass
            return
        info = rec.setdefault("cgroup", self._cgroup_record(
            rec.get("enforcement") or C.MECHANISM, None, used=True))
        try:
            stats = handle.stats()
        except Exception as exc:  # noqa: BLE001
            stats = {"error": f"{type(exc).__name__}: {exc}"}
        if isinstance(stats, Mapping):
            self._merge_cgroup_stats(jid, stats)
            info = rec.get("cgroup") or info
        oom_kill = info.get("oom_kill")
        oom_events = info.get("oom_events")
        if (isinstance(oom_kill, int) and oom_kill > 0) or (
                isinstance(oom_events, int) and oom_events > 0):
            event = {
                "ts": _now(), "kind": "oom", "job": jid,
                "mechanism": "cgroup", "path": info.get("path"),
                "limit_mb": (info.get("limits") or {}).get("memory_mb"),
                "observed_mb": info.get("peak_memory_mb"),
                "oom_events": oom_events, "oom_kill": oom_kill,
                "verified": True, "action": "cgroup_oom",
            }
            events = rec.setdefault("resource_events", [])
            if not any(item.get("kind") == "oom" for item in events):
                events.append(event)
                del events[:-MAX_RESOURCE_EVENTS]
                self.counts["resource_events"] += 1
                self.counts["cgroup_oom_kills"] += 1
                self._append_log_line(
                    rec, f"{RESOURCE_LOG_PREFIX} job {jid}: "
                         f"{R.describe_resource_event(event)}")
        try:
            cleanup = handle.cleanup()
        except Exception as exc:  # noqa: BLE001
            cleanup = {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}
        if not isinstance(cleanup, Mapping):
            cleanup = {"ok": False, "reason": "cleanup returned no report"}
        info = rec.get("cgroup") or info
        info["cleaned_up"] = bool(cleanup.get("ok"))
        if not cleanup.get("ok") and cleanup.get("reason"):
            info["cleanup_reason"] = cleanup["reason"]

    def _force_kill(self, rec: dict[str, Any]) -> str:
        """SIGKILL the job through its cgroup, or its verified process group."""
        jid = str(rec.get("id") or "")
        handle = self._cgroups.get(jid)
        if handle is not None:
            try:
                report = handle.kill(term_first=False)
            except Exception:  # noqa: BLE001 - signals are the fallback
                report = {}
            if isinstance(report, Mapping) and report.get("ok"):
                return "cgroup.kill"
        self._signal(rec, signal.SIGKILL)
        return "sigkill"

    def _verified_group(self, rec: dict[str, Any] | None) -> bool:
        """Whether the record's group is still this job's (call with `cond`)."""
        if (rec is None or not rec.get("owned")
                or rec.get("status") != "running" or rec.get("group_gone")
                or rec.get("id") not in self._procs):
            return False
        return _group_ours(rec.get("pgid"), rec.get("proc_start"))

    def _record_resource_sample(self, jid: str,
                                sample: Mapping[str, Any]) -> None:
        peak = sample.get("peak_memory_mb")
        disk_peak = sample.get("peak_disk_write_mb")
        disk_source = sample.get("disk_source")
        memory_ok = isinstance(peak, (int, float))
        disk_ok = (isinstance(disk_peak, (int, float)) and bool(disk_source))
        if not memory_ok and not disk_ok:
            return
        with self.cond:
            rec = self.jobs.get(jid)
            if rec is None:
                return
            if memory_ok:
                old = rec.get("peak_memory_mb")
                value = round(max(float(old or 0.0), float(peak)), 2)
                rec["peak_memory_mb"] = value
                self.counts["resource_peak_mb"] = max(
                    float(self.counts.get("resource_peak_mb") or 0.0), value)
            if disk_ok:
                self._note_disk_peak(rec, disk_peak, disk_source)

    @staticmethod
    def _append_log_line(rec: Mapping[str, Any], line: str) -> None:
        """Append one `[ozagia]` line to the job log, at a line boundary."""
        path = Path(str(rec.get("log") or ""))
        if not path.name:
            return
        try:
            prefix = b""
            try:
                if path.stat().st_size:
                    with open(path, "rb") as handle:
                        handle.seek(-1, os.SEEK_END)
                        if handle.read(1) != b"\n":
                            prefix = b"\n"
            except OSError:
                pass
            with open(path, "ab") as handle:
                handle.write(prefix + line.encode("utf-8", "replace") + b"\n")
        except OSError as exc:
            print(f"job log write failed for {rec.get('id')}: {exc}",
                  file=sys.stderr, flush=True)

    def _resource_breach(self, jid: str, event: dict[str, Any]) -> None:
        """The watchdog's killer: SIGTERM, grace, SIGKILL a verified group."""
        with self.cond:
            rec = self.jobs.get(jid)
            if not self._verified_group(rec):
                if rec is not None:
                    event.update(verified=False, action="not_signalled")
                    rec.setdefault("resource_events", []).append(dict(event))
                    del rec["resource_events"][:-MAX_RESOURCE_EVENTS]
                    self.counts["resource_events"] += 1
                    self._append_log_line(
                        rec, f"{RESOURCE_LOG_PREFIX} refusing to signal job "
                             f"{jid}: its process group is not verified")
                return
            assert rec is not None
            recorded = dict(event)
            rec.setdefault("resource_events", []).append(recorded)
            del rec["resource_events"][:-MAX_RESOURCE_EVENTS]
            self.counts["resource_events"] += 1
            self.counts["jobs_resource_killed"] += 1
            body = R.describe_resource_event(event)
            self._append_log_line(
                rec, f"{RESOURCE_LOG_PREFIX} job {jid}: {body}; "
                     "stopping its verified process group (SIGTERM)")
            pgid = rec.get("pgid")
            self._signal(rec, signal.SIGTERM)
            t0 = time.monotonic()
            while (_group_members(pgid)
                   and time.monotonic() - t0 < RESOURCE_KILL_GRACE_S):
                self.cond.wait(0.05)
            if _group_members(pgid):
                how = self._force_kill(rec)
                action = how
                if how == "cgroup.kill":
                    detail = "sent cgroup.kill (SIGKILL semantics)"
                else:
                    detail = "sent SIGKILL"
                self._append_log_line(
                    rec, f"{RESOURCE_LOG_PREFIX} job {jid} still running "
                         f"{RESOURCE_KILL_GRACE_S:.0f} s after SIGTERM; "
                         f"{detail}")
            else:
                action = "sigterm"
            recorded.update(action=action, job=jid)
            event["action"] = action
            event.setdefault("job", jid)
            peak = rec.get("peak_memory_mb")
            self._append_log_line(
                rec, f"{RESOURCE_LOG_PREFIX} job {jid} stopped after a "
                     f"resource limit ({body}; peak memory "
                     f"{R.format_memory(peak)})")

    def start(self, *, command: Any = None, argv: Any = None, cwd: Any = None,
              env: Any = None, detach: bool = False, session: Any = None,
              harness: Any = None, limits: R.ResourceLimits | Mapping[str, Any] | None = None,
              policy: Mapping[str, Any] | None = None) -> dict[str, Any]:
        if not self.is_enabled():
            with self.cond:
                self.counts["jobs_refused"] += 1
            raise JobError(disabled_message(self.root))
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
        why = _refusal(text)
        if why is not None:
            with self.cond:
                self.counts["jobs_refused"] += 1
            raise JobError(why)
        if env is None:
            env = {}
        if (not isinstance(env, dict) or len(env) > MAX_ENV
                or not all(isinstance(k, str) and _ENV_KEY.match(k)
                           and isinstance(v, str) for k, v in env.items())):
            raise JobError(f"env must map at most {MAX_ENV} variable names to strings")
        gitty = sorted(k for k in env if k.upper().startswith("GIT_"))
        if gitty:
            with self.cond:
                self.counts["jobs_refused"] += 1
            raise JobError(f"refused: env sets {', '.join(gitty)}; GIT_* "
                           "variables (GIT_CONFIG_*, GIT_DIR, ...) can change "
                           "what git commands do, so jobs cannot set them")
        wd = self._cwd(cwd)
        policy_data = self._policy_for(policy)
        if limits is not None and not isinstance(
                limits, (R.ResourceLimits, Mapping)):
            raise JobError("limits must be ResourceLimits or a mapping")
        try:
            if limits is None:
                chosen = R.limits_from_policy(policy_data, "job")
            elif isinstance(limits, R.ResourceLimits):
                chosen = limits
            else:
                chosen = R.ResourceLimits.from_mapping(limits)
        except ValueError as exc:
            raise JobError(f"bad resource limits: {exc}") from None
        resources_enabled = bool(policy_data.get("enabled", True))
        mode = str(policy_data.get("watchdog") or "auto").strip().lower()
        enforcing = bool(resources_enabled and mode != "off" and chosen.enabled)
        admission: dict[str, Any] | None = None
        if resources_enabled and (chosen.memory_mb is not None
                                  or chosen.disk_mb is not None):
            admission = R.check_admission(
                self.root, kind="job",
                memory_mb=chosen.memory_mb or 0.0,
                disk_mb=chosen.disk_mb or 0.0, path=wd, policy=policy_data)
            if not admission.get("ok"):
                with self.cond:
                    self.counts["jobs_refused"] += 1
                    self.counts["resource_refusals"] += 1
                raise ResourceRefused(admission)
        try:
            sample_s = float(policy_data.get("sample_s") or 2.0)
        except (TypeError, ValueError):
            sample_s = 2.0
        sample_s = max(0.05, min(sample_s, 60.0))
        reserve_gb = (self._disk_reserve_gb(policy_data, admission, wd)
                      if enforcing else 0.0)
        mechanism = R.ENFORCEMENT_MECHANISM if enforcing else "off"
        want_cgroup = (enforcing and mode in {"auto", "cgroup"}
                       and cgroups_enabled())
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
            cgroup_handle: Any | None = None
            cgroup_report: dict[str, Any] | None = None
            if want_cgroup:
                try:
                    cgroup_handle, cgroup_report = self._manager().start(
                        jid, memory_mb=chosen.memory_mb, pids=chosen.pids)
                except Exception as exc:  # noqa: BLE001 - fallback, not failure
                    cgroup_handle = None
                    cgroup_report = {
                        "ok": False, "fallback": True,
                        "reason": f"{type(exc).__name__}: {exc}",
                        "limits": {"memory_mb": chosen.memory_mb,
                                   "pids": chosen.pids},
                        "errors": [],
                    }
                if cgroup_handle is None:
                    mechanism = R.ENFORCEMENT_MECHANISM
                    self.counts["cgroup_fallbacks"] += 1
                else:
                    mechanism = C.MECHANISM
                    self.counts["cgroup_jobs"] += 1
            preexec = (self._child_preexec(chosen, cgroup_handle)
                       if enforcing else None)
            extra: dict[str, Any] = {}
            if preexec is not None:
                extra["preexec_fn"] = preexec
            try:
                with open(log, "wb") as fh, open(os.devnull, "rb") as devnull:
                    proc = subprocess.Popen(
                        args, cwd=str(wd), env=full_env, stdin=devnull,
                        stdout=fh, stderr=subprocess.STDOUT,
                        start_new_session=True, close_fds=True, **extra)
            except OSError as e:
                if cgroup_handle is not None:
                    try:
                        cgroup_handle.cleanup()
                    except Exception:  # noqa: BLE001 - cleanup is best-effort
                        pass
                raise JobError(f"cannot start {text!r}: {e}") from None
            if cgroup_handle is not None:
                try:
                    attached = cgroup_handle.attach(proc.pid)
                except Exception as exc:  # noqa: BLE001
                    attached = {"ok": False,
                                "error": f"{type(exc).__name__}: {exc}"}
                if not isinstance(attached, Mapping) or not attached.get("ok"):
                    # The child is not confirmed in the group; do not claim a
                    # cgroup mechanism, and remove the empty group.
                    try:
                        cgroup_handle.cleanup()
                    except Exception:  # noqa: BLE001
                        pass
                    cgroup_handle = None
                    mechanism = R.ENFORCEMENT_MECHANISM
                    self.counts["cgroup_fallbacks"] += 1
                    reason = (attached.get("error")
                              if isinstance(attached, Mapping) else None)
                    reason = reason or "attach was not confirmed"
                    cgroup_report = {**(cgroup_report or {}), "fallback": True,
                                     "reason": f"child could not join the "
                                               f"cgroup: {reason}"}
            cgroup_record: dict[str, Any] | None = None
            if enforcing:
                cgroup_record = self._cgroup_record(
                    mechanism, cgroup_report, used=cgroup_handle is not None)
                if not want_cgroup:
                    cgroup_record["reason"] = (
                        f"cgroup not attempted (watchdog={mode})")
                elif (cgroup_handle is None
                      and cgroup_record.get("reason") is None):
                    cgroup_record["reason"] = (
                        "cgroup v2 unavailable; rlimit+watch fallback")
            initial_peak: float | None = None
            initial_disk: float | None = None
            initial_disk_source: str | None = None
            if enforcing:
                try:
                    first = R.process_group_usage(proc.pid)
                    if first.get("process_count"):
                        initial_peak = float(first.get("memory_mb") or 0.0)
                    disk_write = first.get("disk_write_mb")
                    if isinstance(disk_write, (int, float)):
                        initial_disk = round(float(disk_write), 4)
                        initial_disk_source = first.get("disk_source")
                except Exception:  # noqa: BLE001 - observation is best-effort
                    initial_peak = 0.0
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
                   "limits": chosen.to_dict(), "enforcement": mechanism,
                   "cgroup": cgroup_record,
                   "peak_memory_mb": initial_peak,
                   "peak_disk_write_mb": None,
                   "disk_write_source": None,
                   "resource_events": [],
                   "admission": self._admission_summary(admission),
                   "owned": True}
            self.jobs[jid] = rec
            self._procs[jid] = proc
            if cgroup_handle is not None:
                self._cgroups[jid] = cgroup_handle
            self.counts["jobs_started"] += 1
            if initial_peak is not None:
                self.counts["resource_peak_mb"] = max(
                    float(self.counts.get("resource_peak_mb") or 0.0),
                    initial_peak)
            if initial_disk is not None:
                self._note_disk_peak(rec, initial_disk, initial_disk_source)
            self._evict()
            self._save()
            t = threading.Thread(target=self._reap, args=(jid, proc),
                                 name=f"ozagia-job-{jid}", daemon=True)
            self._reapers[jid] = t
            t.start()
            if enforcing:
                def verify(job_id: str = jid) -> bool:
                    with self.cond:
                        return self._verified_group(self.jobs.get(job_id))

                def on_sample(sample: Mapping[str, Any],
                              job_id: str = jid) -> None:
                    self._record_resource_sample(job_id, sample)

                def killer(event: dict[str, Any],
                           job_id: str = jid) -> None:
                    self._resource_breach(job_id, event)

                disk_attributor = None
                if cgroup_handle is not None:
                    def disk_attributor(
                            job_id: str = jid) -> dict[str, Any] | None:
                        return self._sample_cgroup(job_id)

                watchdog = R.ResourceWatchdog(
                    chosen, pgid=proc.pid, verify=verify, killer=killer,
                    path=wd, interval_s=sample_s, reserve_disk_gb=reserve_gb,
                    disk_attributor=disk_attributor, on_sample=on_sample)
                self._watchdogs[jid] = watchdog
                watchdog.start()
            return self.public(rec)

    def _reap(self, jid: str, proc: subprocess.Popen) -> None:
        """Wait for the leader to exit, without reaping it, then for its
        process group to empty; then reap it and record the end. The
        unreaped (zombie) leader keeps the pgid from being reused while
        `_signal` may still send to it."""
        try:
            info = os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOWAIT)
        except ChildProcessError:
            info = None
        with self.cond:
            rec = self.jobs.get(jid)
            if rec is not None:
                rec["leader_exited"] = True
            self.cond.notify_all()
        while info is not None and _group_members(proc.pid):
            time.sleep(GROUP_POLL_S)
        watchdog = self._watchdogs.pop(jid, None)
        if watchdog is not None:
            watchdog.stop()
        with self.cond:
            code = proc.wait()          # the group is empty: free the pgid
            self._procs.pop(jid, None)
            rec = self.jobs.get(jid)
            if rec is not None:
                self._finish_cgroup(jid, rec)
                rec["ended"] = _now()
                rec["group_gone"] = True
                rec.pop("leader_exited", None)
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
                    "seq": self.event_seq, "job": jid, "command": rec["command"],
                    "status": rec["status"], "exit_code": rec["exit_code"],
                    "signal": rec["signal"], "elapsed_s": _elapsed(rec),
                    "session": rec.get("session"), "harness": rec.get("harness")})
                del self.events[:-200]
                self._save()
            else:
                self._finish_cgroup(jid, None)
            self.last_end = time.monotonic()
            self.cond.notify_all()

    # -- logs ---------------------------------------------------------------- #
    @staticmethod
    def read_new(log_path: Any, start: int, running: bool, *,
                 tail_lines: int = TAIL_LINES, tail_bytes: int = TAIL_BYTES
                 ) -> dict[str, Any]:
        """The log text from byte `start` to the end, tail-capped. Reads the
        file only: call it without `cond` held."""
        path = Path(log_path or "")
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
        if running and data and not data.endswith(b"\n") and b"\n" in data:
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

    def _set_cursor(self, jid: str, reader: str, value: int,
                    explicit: bool = False) -> None:
        """Move `reader`'s offset to `value`: forward only, unless the read
        named its own `cursor` (two reads of one reader at once both end at
        the end of what they returned; the offset is the furthest)."""
        old = self.cursors.pop((jid, reader), None)
        if old is not None and not explicit:
            value = max(old, value)
        self.cursors[(jid, reader)] = value
        while len(self.cursors) > MAX_READERS:
            del self.cursors[next(iter(self.cursors))]

    def log(self, jid: Any, *, reader: str = "default", cursor: Any = None,
            tail: Any = None) -> dict[str, Any]:
        tail_lines = min(_int_arg(tail, "tail", TAIL_LINES) or TAIL_LINES, 2000)
        with self.cond:
            rec = self.get(jid)
            self._done(rec)
            start = self._cursor(rec["id"], reader, cursor)
            path, running = rec.get("log"), rec.get("status") in ("running", "detached")
        got = self.read_new(path, start, running, tail_lines=tail_lines,
                            tail_bytes=max(TAIL_BYTES, tail_lines * 160))
        with self.cond:
            self._set_cursor(rec["id"], reader, got["cursor"], cursor is not None)
            return {**self.flat(rec), **got}

    # -- wait ---------------------------------------------------------------- #
    def _done(self, rec: dict[str, Any]) -> bool:
        """Whether the job has ended (with `cond` held). A detached job that
        is found to have ended becomes `gone`."""
        st = rec.get("status")
        if st == "detached":
            if self._live(rec):
                return False
            rec["status"] = "gone"
            rec["ended"] = _now()
            rec["group_gone"] = True
            self._cleanup_record_cgroup(rec)
            self._save()
            return True
        if st != "running":
            self._cleanup_record_cgroup(rec)
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
                if not targets and kind != "file":
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
            fpath: Path | None = None
            if kind == "file":
                p = Path(os.path.expanduser(value))
                if not p.is_absolute():
                    base = (self._cwd(targets[0].get("cwd")) if len(targets) == 1
                            else self.root)
                    p = base / p
                fpath = p
        # The log files are read and matched outside `cond`; `scan` and
        # `partial` belong to this call, and a record's `log` never changes.
        logs = {r["id"]: r.get("log") for r in targets}
        scan = dict(starts)
        partial = {r["id"]: b"" for r in targets}
        subject = targets[0] if len(targets) == 1 else None
        met = False
        match = None

        def scan_one(r: dict[str, Any], final: bool = False) -> str | None:
            jid = r["id"]
            hit, scan[jid], partial[jid] = self._scan(
                logs[jid], scan[jid], partial[jid], value, final=final)
            return hit

        while True:
            if kind == "file":
                assert fpath is not None
                met = fpath.exists()
            elif kind == "log":
                for r in targets:
                    hit = scan_one(r)
                    if hit is not None:
                        subject, met, match = r, True, hit
                        break
            if met:
                break
            with self.cond:
                ended = [r for r in targets if self._done(r)]
                if kind == "exit" and ended:
                    subject, met = ended[0], True
                    break
                if ended:
                    subject = ended[0]
                    final = kind == "log"
                    if kind == "file":     # created just before the exit
                        assert fpath is not None
                        met = fpath.exists()
                else:
                    final = None
                    left = deadline - (time.monotonic() - t0)
                    if left <= 0 or self.closed:
                        break
                    self.cond.wait(min(POLL_S, left))
            if final is not None:
                if final:       # lines written just before the exit
                    hit = scan_one(subject, final=True)
                    if hit is not None:
                        met, match = True, hit
                break
        waited = round(time.monotonic() - t0, 2)
        out: dict[str, Any] = {"ok": True, "until": until or "exit",
                               "met": met, "waited_s": waited,
                               "deadline_s": deadline}
        if match is not None:
            out["match"] = match
        if fpath is not None:
            out["file"] = str(fpath)
        if subject is None:
            with self.cond:
                self.counts["waits"] += 1
                self.counts["wait_seconds"] = round(
                    self.counts["wait_seconds"] + waited, 2)
                if met:
                    self.counts["polls_avoided"] += 1
            out.update(job=None, status="met" if met else "waiting", output="")
            return out
        with self.cond:
            running = subject.get("status") in ("running", "detached")
        got = self.read_new(logs[subject["id"]], starts[subject["id"]], running)
        with self.cond:
            self.counts["waits"] += 1
            self.counts["wait_seconds"] = round(self.counts["wait_seconds"] + waited, 2)
            self._set_cursor(subject["id"], reader, got["cursor"],
                             cursor is not None and len(targets) == 1)
            pub = self.public(subject)
            out.update({"job": subject["id"], "status": pub["status"],
                        "exit_code": pub["exit_code"], "signal": pub["signal"],
                        "elapsed_s": pub["elapsed_s"], "command": pub["command"],
                        "limits": pub.get("limits"),
                        "cgroup": pub.get("cgroup"),
                        "peak_memory_mb": pub.get("peak_memory_mb"),
                        "peak_disk_write_mb": pub.get("peak_disk_write_mb"),
                        "disk_write_source": pub.get("disk_write_source"),
                        "resource_events": pub.get("resource_events") or [],
                        "enforcement": pub.get("enforcement"),
                        "admission": pub.get("admission"),
                        **got})
            for k in ("leader_exited", "group_alive"):
                if k in pub:
                    out[k] = pub[k]
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

    @staticmethod
    def _scan(log_path: Any, offset: int, partial: bytes, rx: re.Pattern,
              final: bool = False) -> tuple[str | None, int, bytes]:
        """Search the log text after `offset` for a line matching `rx` (its
        first MATCH_LINE_BYTES bytes). Reads the file only: call it without
        `cond` held."""
        path = Path(log_path or "")
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
            text = raw[:MATCH_LINE_BYTES].decode("utf-8", errors="replace")
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
        """Signal the job's process group, then SIGKILL it after `grace_s`.
        Works on a job of an earlier daemon (`detached`, or `gone` with
        processes left in its group) while its group is still the job's."""
        name = str(sig or "TERM").upper().removeprefix("SIG")
        if name not in _SIGNALS:
            raise JobError(f"signal must be one of {', '.join(_SIGNALS)}")
        grace = STOP_GRACE_S if grace_s is None else _deadline(grace_s)
        with self.cond:
            rec = self.get(jid)
            self._done(rec)
            if not self._live(rec):
                if rec.get("status") == "gone":
                    self._save()
                return {"ok": True, **self.flat(rec),
                        "note": f"job {rec['id']} had already ended"}
            self.counts["job_stops"] += 1
            self._signal(rec, _SIGNALS[name])
            t0 = time.monotonic()
            while self._live(rec) and time.monotonic() - t0 < grace:
                self.cond.wait(0.05)
            if self._live(rec) and name != "KILL":
                self._force_kill(rec)
            t1 = time.monotonic()
            while self._live(rec) and time.monotonic() - t1 < 2.0:
                self.cond.wait(0.05)
            if not rec.get("owned") and not self._live(rec):
                if rec.get("status") == "detached":
                    rec["status"] = "gone"
                rec["ended"] = rec.get("ended") or _now()
                self._save()
            if not self._live(rec):
                self._cleanup_record_cgroup(rec)
            return {"ok": True, **self.flat(rec)}

    def shutdown(self, grace_s: float = SHUTDOWN_GRACE_S) -> list[str]:
        """Stop owned jobs that were not started with `detach`; leave the
        others running. Returns the log lines written."""
        notes: list[str] = []
        with self.cond:
            self.closed = True
            self.cond.notify_all()
            doomed = [r for r in self.jobs.values()
                      if r.get("owned") and not r.get("detach")
                      and r.get("status") == "running"]
            for r in doomed:
                self._signal(r, signal.SIGTERM)
            t0 = time.monotonic()
            while (any(r.get("status") == "running" for r in doomed)
                   and time.monotonic() - t0 < grace_s):
                self.cond.wait(0.05)
            for r in doomed:
                if r.get("status") == "running":
                    self._force_kill(r)
                    notes.append(f"killed job {r['id']} (pid {r['pid']}): "
                                 f"{r['command'][:200]}")
            t1 = time.monotonic()
            while (any(r.get("status") == "running" for r in doomed)
                   and time.monotonic() - t1 < 2.0):
                self.cond.wait(0.05)
            for r in self.jobs.values():
                if r.get("status") not in ("running", "detached"):
                    self._cleanup_record_cgroup(r)
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
        except ResourceRefused as e:
            report = e.report
            return {"ok": False, "error": str(e), "resource_refused": True,
                    "reason": report.get("reason"),
                    "requested": report.get("requested"),
                    "available": report.get("available"),
                    "shortfall": report.get("shortfall"),
                    "retry_after_s": report.get("retry_after_s"),
                    "suggestions": report.get("suggestions") or [],
                    "admission": report}
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
        if (background_start(command) and self.is_enabled()
                and self.hint_ok(session, "background")):
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
        # Only jobs whose leader runs: an ended job's pid may belong to
        # another process now, which real ps would list.
        by_pid = {int(r["pid"]): r for r in jobs
                  if r.get("pid") and r.get("status") in ("running", "detached")
                  and not r.get("leader_exited")}
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


__all__ = ["JobError", "JobTable", "MAX_JOBS", "OPS", "ResourceRefused",
           "WAIT_DEFAULT_S", "background_start", "capture_ps", "cgroups_enabled",
           "disabled_message", "jobs_enabled", "parse_until",
           "sleep_seconds"]
