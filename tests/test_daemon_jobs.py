"""Background jobs (`ozagia.daemon.jobs`): real subprocess jobs in temp
repositories, the daemon ops, the MCP tools and the CLI against a real
daemon, the Codex hook's ps capture, and the shell hints."""
from __future__ import annotations

import json
import os
import select
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from ozagia.config import clear_config_cache
from ozagia.core import resources as R
from ozagia.core.secrets import SecretBroker, SessionSecretStore
from ozagia.daemon import jobs as J
from ozagia.daemon.client import DaemonClient
from ozagia.daemon.protocol import socket_path
from ozagia.daemon.server import DaemonState

SRC = Path(__file__).resolve().parents[1] / "src"
READY_TIMEOUT_S = 60
PATH_ENV = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
# Job admission must not depend on the machine running the suite. The budgets
# here are small enough for any host, and the floors/reserves are zero; the
# default policy is covered by tests/test_resources.py.
TEST_RESOURCE_POLICY = R._deep_merge(R.DEFAULT_POLICY, {
    "reserve_memory_pct": 0,
    "reserve_disk_pct": 0,
    "reserve_disk_gb": 0,
    "limits": {"job_memory_mb": 256, "job_disk_mb": 64},
    "admission": {"min_free_memory_pct": 0, "min_free_disk_gb": 0},
})
RESOURCE_POLICY_TOML = """\
reserve_memory_pct = 0
reserve_disk_gb = 0

[limits]
job_memory_mb = 256
job_disk_mb = 64

[admission]
min_free_memory_pct = 0
min_free_disk_gb = 0
"""


def _env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("OZAGIA_")}
    env["PYTHONPATH"] = str(SRC)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["OZAGIA_CGROUPS"] = "0"     # tests never touch the host hierarchy
    env.update(extra)
    return env


def _repo(path: Path) -> Path:
    (path / "pkg").mkdir(parents=True)
    (path / "pkg" / "__init__.py").write_text("")
    (path / "pkg" / "mod.py").write_text("def helper(a):\n    return a\n")
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    return path.resolve()


@pytest.fixture
def bare_repo(tmp_path, monkeypatch):
    """A repository without a configuration: jobs are off."""
    for k in [k for k in os.environ if k.startswith("OZAGIA_")]:
        monkeypatch.delenv(k)
    return _repo(tmp_path / "repo")


@pytest.fixture
def repo(bare_repo):
    """A repository that enables jobs (`[jobs] enabled = true`)."""
    (bare_repo / ".ozagia.toml").write_text("[jobs]\nenabled = true\n")
    (bare_repo / "ozagia").mkdir()
    (bare_repo / "ozagia" / "resources.toml").write_text(
        RESOURCE_POLICY_TOML, encoding="utf-8")
    return bare_repo


class _FakeCgroupHandle:
    """A cgroup handle that records calls and never touches the host."""

    def __init__(self, job_id: str = "j1", *,
                 stats: dict | None = None) -> None:
        self.path = Path("/fake/cgroup/ozagia") / f"job-{job_id}"
        self.attached: list[int] = []
        self.killed: list[dict] = []
        self.cleaned: list[bool] = []
        stats = stats or {}
        self._stats = {
            "memory_peak_mb": 12.5, "memory_current_mb": 12.5,
            "memory_max_mb": 64.0, "pids_current": 1, "pids_max": 8,
            "oom_events": 0, "oom_kill": 0, "populated": False,
            "peak_source": "memory.peak",
            "io_read_mb": 2.0, "io_write_mb": 4.0,
            "io_source": "cgroup.io.stat",
            "io_devices": [{"device": "8:0", "read_mb": 2.0,
                            "write_mb": 4.0}],
            "io_device_count": 1,
            **stats,
        }

    def attach(self, pid: int) -> dict:
        self.attached.append(pid)
        return {"ok": True, "pid": pid, "path": str(self.path)}

    def stats(self) -> dict:
        return dict(self._stats)

    def kill(self, *, fallback=None, grace_s: float = 0.0,
             term_first: bool = True) -> dict:
        self.killed.append({"fallback": fallback is not None,
                            "term_first": term_first})
        return {"ok": True, "method": "cgroup.kill",
                "action": "cgroup.kill", "signals": []}

    def cleanup(self) -> dict:
        self.cleaned.append(True)
        return {"ok": True, "removed": True, "path": str(self.path),
                "reason": None}


class _FakeCgroupManager:
    def __init__(self, handle: _FakeCgroupHandle | None = None,
                 report: dict | None = None) -> None:
        self.handle = handle
        self.report = report
        self.calls: list[dict] = []

    def start(self, job_id: str, *, memory_mb=None, pids=None):
        self.calls.append({"job": job_id, "memory_mb": memory_mb, "pids": pids})
        if self.handle is not None:
            return self.handle, {
                "ok": True, "fallback": False, "path": str(self.handle.path),
                "limits": {"memory_mb": memory_mb, "pids": pids},
                "errors": [], "reason": None,
            }
        report = dict(self.report or {
            "ok": False, "fallback": True, "path": None,
            "limits": {"memory_mb": memory_mb, "pids": pids},
            "errors": [],
            "reason": "cgroup v2 is not mounted at /sys/fs/cgroup",
        })
        return None, report


class _UnavailableCgroupManager(_FakeCgroupManager):
    pass


@pytest.fixture
def table(repo):
    tables: list[J.JobTable] = []

    def make(**kw) -> J.JobTable:
        kw.setdefault("cgroup_manager", _UnavailableCgroupManager())
        kw.setdefault("resource_policy", TEST_RESOURCE_POLICY)
        t = J.JobTable(repo, base_env=PATH_ENV, **kw)
        tables.append(t)
        return t

    yield make
    for t in tables:
        t.shutdown(grace_s=0.5)


def _gone(pid: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not J._alive(pid, None):
            return True
        time.sleep(0.05)
    return False


def _group_pids(pgid: int) -> list[int]:
    out = []
    for name in os.listdir("/proc"):
        if name.isdigit():
            st = J._proc_stat(int(name))
            if st and len(st) > 4 and st[4] == str(pgid) and st[2] != "Z":
                out.append(int(name))
    return out


# --------------------------------------------------------------------------- #
# the job table
# --------------------------------------------------------------------------- #
def test_start_captures_stdout_and_stderr_and_waits_for_exit(table, repo):
    t = table()
    job = t.start(command="echo out; echo err >&2; exit 3")
    assert job["status"] == "running" and job["pgid"] == job["pid"]
    assert job["cwd"] == "." and Path(job["log"]).parent == repo / ".ozagia" / "jobs"
    got = t.wait(job["id"], deadline_s=10)
    assert got["met"] and got["status"] == "exited" and got["exit_code"] == 3
    assert got["output"].splitlines() == ["out", "err"]
    assert got["cursor"] == len(b"out\nerr\n")
    # stdin is /dev/null; argv runs without a shell; env additions apply.
    job = t.start(argv=["sh", "-c", "cat; echo $FOO; pwd"], env={"FOO": "bar"},
                  cwd="pkg")
    got = t.wait(job["id"], deadline_s=10)
    assert got["output"].splitlines() == ["bar", str(repo / "pkg")]
    assert job["env_keys"] == ["FOO"] and job["cwd"] == "pkg"


def test_jobs_resolve_env_refs_and_redact_the_log_before_it_is_written(
        table, repo):
    secret = "job-secret-value-1234"
    broker = SecretBroker(repo, environ={"FAKE_TOKEN": secret})
    t = table(secret_broker=broker)
    job = t.start(
        command="printf '%s' \"$TOKEN\"; sleep 0.3; "
                "printf '\\nREADY\\n'; sleep 30",
        env={"TOKEN": "secret://env/FAKE_TOKEN", "LOG_LEVEL": "info"},
        session="s1", harness="opencode")
    assert job["env_keys"] == ["LOG_LEVEL", "TOKEN"]
    assert job["env_refs"] == {"TOKEN": "secret://env/FAKE_TOKEN"}
    got = t.wait(job["id"], until="log:READY", deadline_s=15)
    assert got["met"] and got["status"] == "running"
    assert secret not in got["output"] and "[REDACTED]" in got["output"]
    assert got["output"].splitlines()[0] == "[REDACTED]"
    raw = Path(job["log"]).read_text()
    assert secret not in raw and "[REDACTED]" in raw
    assert got["cursor"] == Path(job["log"]).stat().st_size
    # A second reader sees the same redacted text, never the value.
    seen = t.log(job["id"], reader="second")
    assert secret not in seen["output"]
    # The wait/read fields redact a legacy raw log too.
    Path(job["log"]).write_text(f"old {secret}\\n")
    legacy = t.log(job["id"], reader="legacy", cursor=0)
    assert secret not in legacy["output"] and "[REDACTED]" in legacy["output"]
    stopped = t.stop(job["id"], grace_s=1)
    assert stopped["status"] == "killed"
    assert t.stats()["secret_resolutions"] == 1
    assert t.stats()["secret_redactions"] >= 1


def test_jobs_deny_a_session_reference_for_another_session(table, repo):
    store = SessionSecretStore()
    captured = store.capture("cross-job-secret-value", harness="opencode",
                             session="s1")
    t = table(secret_broker=SecretBroker(repo, environ={},
                                         session_store=store))
    with pytest.raises(J.JobError) as excinfo:
        t.start(argv=["true"], env={"TOKEN": captured.ref},
                harness="opencode", session="s2")
    message = str(excinfo.value)
    assert "session_mismatch" in message
    assert "cross-job-secret-value" not in message
    assert t.jobs == {}
    # The same session resolves it (the child prints no value here).
    job = t.start(argv=["true"], env={"TOKEN": captured.ref},
                  harness="opencode", session="s1")
    assert job["env_refs"] == {"TOKEN": captured.ref}
    got = t.wait(job["id"], deadline_s=10)
    assert got["status"] == "exited"
    assert t.stats()["secret_resolutions"] == 1



    t = table()
    job = t.start(command="echo one; echo two; sleep 0.4; echo READY 42; "
                          "sleep 0.4; echo after; sleep 30")
    got = t.wait(job["id"], until="log:READY \\d+", deadline_s=10)
    assert got["met"] and got["match"] == "READY 42" and got["status"] == "running"
    assert got["output"].splitlines() == ["one", "two", "READY 42"]
    # The next wait returns only what is new to this reader.
    got = t.wait(job["id"], until="log:^after$", deadline_s=10)
    assert got["met"] and got["output"] == "after"
    first = got["cursor"]
    # A second reader starts at the beginning; an explicit cursor wins.
    other = t.log(job["id"], reader="other")
    assert other["output"].splitlines() == ["one", "two", "READY 42", "after"]
    assert t.log(job["id"], cursor=4)["output"].splitlines()[0] == "two"
    assert t.log(job["id"])["output"] == "" and t.log(job["id"])["cursor"] == first
    stopped = t.stop(job["id"], grace_s=2)
    assert stopped["status"] == "killed" and stopped["signal"] == "SIGTERM"


def test_wait_deadline_returns_still_running(table):
    t = table()
    job = t.start(argv=["sleep", "30"])
    t0 = time.monotonic()
    got = t.wait(job["id"], deadline_s=0.5)
    assert 0.4 < time.monotonic() - t0 < 3
    assert not got["met"] and got["status"] == "running"
    assert "still running" in got["note"] and got["output"] == ""
    assert t.stats()["polls_avoided"] == 0
    t.stop(job["id"], grace_s=1)


def test_wait_file_and_job_exit_before_the_condition(table, repo):
    t = table()
    job = t.start(command="sleep 0.3; touch done.flag; sleep 30")
    got = t.wait(job["id"], until="file:done.flag", deadline_s=10)
    assert got["met"] and got["file"] == str(repo / "done.flag")
    # Without a job, a file wait needs no job; a missing file times out.
    got = t.wait(None, until="file:never.flag", deadline_s=0.2)
    assert not got["met"] and got["job"] is None
    t.stop(job["id"], grace_s=1)
    # A job that exits without the log line ends the wait (met false).
    job = t.start(command="echo nope")
    got = t.wait(job["id"], until="log:READY", deadline_s=10)
    assert not got["met"] and got["status"] == "exited" and got["output"] == "nope"
    # A line written just before the exit still matches.
    job = t.start(command="printf 'READY'")
    got = t.wait(job["id"], until="log:READY", deadline_s=10)
    assert got["met"] and got["match"] == "READY"


def test_wait_any(table):
    t = table()
    assert t.wait("any", deadline_s=1)["status"] == "no_running_jobs"
    slow = t.start(argv=["sleep", "30"])
    fast = t.start(command="sleep 0.2; echo fast")
    got = t.wait("any", deadline_s=10)
    assert got["met"] and got["job"] == fast["id"] and got["output"] == "fast"
    assert got["others"] == [{"job": slow["id"], "status": "running"}]
    t.stop(slow["id"], grace_s=1)


def test_reads_are_tail_capped(table):
    t = table()
    job = t.start(command="seq 1 1000")
    got = t.wait(job["id"], deadline_s=10)
    lines = got["output"].splitlines()
    assert len(lines) == J.TAIL_LINES and lines[-1] == "1000"
    assert got["skipped_lines"] == 800 and got["skipped_bytes"] == 0
    job = t.start(command="for i in $(seq 1 3000); do echo line-$i-xxxxxxxxxxxx; done")
    got = t.wait(job["id"], deadline_s=10)
    assert got["skipped_bytes"] > 0
    assert len(got["output"].encode()) <= J.TAIL_BYTES
    assert got["output"].splitlines()[-1] == "line-3000-xxxxxxxxxxxx"
    assert got["output"].splitlines()[0].startswith("line-")
    assert got["cursor"] == Path(job["log"]).stat().st_size


def test_partial_line_waits_for_its_end(table):
    t = table()
    job = t.start(command="printf 'a\\nb'; sleep 0.5; printf 'c\\n'; sleep 30")
    first = t.wait(job["id"], until="log:^a$", deadline_s=10)
    assert first["met"] and first["output"] == "a"
    got = t.wait(job["id"], until="log:^bc$", deadline_s=10)
    assert got["met"] and got["output"] == "bc"
    t.stop(job["id"], grace_s=1)


def test_stop_kills_the_whole_process_group(table):
    t = table()
    job = t.start(command="trap '' TERM; sleep 30 & sleep 30 & wait")
    deadline = time.monotonic() + 5
    while len(_group_pids(job["pgid"])) < 3 and time.monotonic() < deadline:
        time.sleep(0.05)
    assert len(_group_pids(job["pgid"])) == 3
    t0 = time.monotonic()
    got = t.stop(job["id"], grace_s=0.5)
    assert time.monotonic() - t0 < 5
    assert got["status"] == "killed" and got["signal"] == "SIGKILL"
    deadline = time.monotonic() + 5
    while _group_pids(job["pgid"]) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert _group_pids(job["pgid"]) == []
    assert t.stats()["job_stops"] == 1
    assert "already ended" in t.stop(job["id"])["note"]


def test_bad_requests_are_refused(table, repo, tmp_path):
    t = table()
    for kw, why in [({"command": "true", "cwd": str(tmp_path)}, "outside the project root"),
                    ({"command": "true", "cwd": "../.."}, "outside the project root"),
                    ({"command": "true", "cwd": "nope"}, "not a directory"),
                    ({}, "exactly one of"),
                    ({"command": "a", "argv": ["a"]}, "exactly one of"),
                    ({"argv": []}, "non-empty list"),
                    ({"command": "  "}, "non-empty string"),
                    ({"command": "true", "env": {"1X": "y"}}, "env must"),
                    ({"command": "git commit -m x"}, "moves refs"),
                    ({"argv": ["git", "push"]}, "moves refs")]:
        with pytest.raises(J.JobError, match=why):
            t.start(**kw)
    (repo / "out").mkdir()
    os.symlink(tmp_path, repo / "out" / "esc")
    with pytest.raises(J.JobError, match="outside the project root"):
        t.start(command="true", cwd="out/esc")
    with pytest.raises(J.JobError, match="unknown job"):
        t.wait("j999")
    with pytest.raises(J.JobError, match="until must be"):
        t.wait("any", until="later")
    with pytest.raises(J.JobError, match="bad log regex"):
        t.wait("any", until="log:(")
    assert t.dispatch("job_wait", {"job": "j999"}) == {
        "ok": False, "error": "unknown job 'j999'"}
    assert t.jobs == {}


def test_the_table_keeps_the_last_jobs(table, monkeypatch):
    monkeypatch.setattr(J, "MAX_JOBS", 3)
    t = table()
    ids = []
    for i in range(5):
        job = t.start(command=f"echo {i}")
        t.wait(job["id"], deadline_s=10)
        ids.append(job["id"])
    assert list(t.jobs) == ids[2:]
    logs = sorted(p.name for p in t.dir.glob("*.log"))
    assert logs == sorted(f"{j}.log" for j in ids[2:])
    # The table survives a restart (ids continue).
    again = table()
    assert list(again.jobs) == ids[2:] and again.jobs[ids[-1]]["status"] == "exited"
    assert again.start(command="true")["id"] == "j6"


def test_shutdown_stops_jobs_but_leaves_detached_ones(table, capsys):
    t = table()
    normal = t.start(command="sleep 30 & wait")
    kept = t.start(argv=["sleep", "30"], detach=True)
    notes = t.shutdown(grace_s=1)
    assert _gone(normal["pid"]) and not _group_pids(normal["pgid"])
    assert J._alive(kept["pid"], None)
    assert any(f"leaving detached job {kept['id']}" in n for n in notes)
    assert "leaving detached job" in capsys.readouterr().err
    with pytest.raises(J.JobError, match="shutting down"):
        t.start(command="true")
    try:
        # The next daemon lists it as detached and can stop it.
        nxt = table()
        rec = nxt.jobs[kept["id"]]
        assert rec["status"] == "detached" and nxt.jobs[normal["id"]]["status"] == "killed"
        assert nxt.wait(kept["id"], deadline_s=0.2)["status"] == "detached"
        assert nxt.stop(kept["id"], grace_s=2)["status"] == "gone"
    finally:
        J._killpg(kept["pgid"], signal.SIGKILL)
    assert _gone(kept["pid"])


def test_events_report_job_ends(table):
    t = table()
    assert t.wait_events(reader="r", deadline_s=0.1) == {
        "ok": True, "events": [], "running": 0, "seq": 0}
    job = t.start(command="sleep 0.2; exit 4")
    got = t.wait_events(reader="r", deadline_s=10)
    assert [(e["job"], e["exit_code"], e["seq"]) for e in got["events"]] == [
        (job["id"], 4, 1)]
    assert got["seq"] == 1 and t.wait_events(reader="r", deadline_s=0.1)["events"] == []
    # A reader names the seq to start after.
    assert len(t.wait_events(reader="new", after_seq=0, deadline_s=0.1)["events"]) == 1


# --------------------------------------------------------------------------- #
# ps / pgrep capture
# --------------------------------------------------------------------------- #
def _real(argv: list[str]) -> str:
    return subprocess.run(argv, capture_output=True, text=True, timeout=10).stdout.rstrip("\n")


@pytest.mark.skipif(not (shutil.which("ps") and shutil.which("pgrep")),
                    reason="needs procps")
def test_ps_and_pgrep_match_the_real_commands(table):
    t = table()
    job = t.start(argv=["sleep", "41.25"])
    pid = job["pid"]
    time.sleep(0.2)
    line = f"[ozagia] job {job['id']} (pid {pid}) running"
    for cmd, argv in [(f"ps -p {pid}", ["ps", "-p", str(pid)]),
                      (f"ps -p {pid} -o pid=", ["ps", "-p", str(pid), "-o", "pid="]),
                      (f"ps -o args= -p {pid}", ["ps", "-o", "args=", "-p", str(pid)]),
                      (f"ps --pid={pid} -o comm=", ["ps", f"--pid={pid}", "-o", "comm="]),
                      ("pgrep -f 'sleep 41.25'", ["pgrep", "-f", "sleep 41.25"]),
                      ("pgrep -fa 'sleep 41\\.25'", ["pgrep", "-fa", "sleep 41\\.25"]),
                      ("pgrep -f -l '^sleep 41'", ["pgrep", "-f", "-l", "^sleep 41"])]:
        out = t.capture_ps(cmd)
        assert out is not None, cmd
        body, _, tail = out.rpartition("\n")
        assert body == _real(argv), cmd
        assert tail.startswith(line) and "job_wait" in tail
    assert t.stats()["ps_captures"] == 7
    # Not about a job, or not a simple form: run as usual.
    for cmd in ["ps -p 1", f"ps -p {pid},1", "pgrep -f no-such-job-xyz", "ps aux",
                f"ps -p {pid} -o pid,args", f"ps -p {pid} | cat", "pgrep -f '\\1'",
                f"ps -p {pid} -o stat=", "pgrep -v sleep", "pgrep -la sleep"]:
        assert t.capture_ps(cmd) is None, cmd
    t.stop(job["id"], grace_s=1)
    # An ended job's pid may be another process's by now: ps runs as usual.
    assert t.capture_ps(f"ps -p {pid}") is None
    # pgrep -f names the ended job (and lists no process).
    assert t.capture_ps("pgrep -f 'sleep 41.25'").startswith("[ozagia] job")


def test_ps_for_a_reused_pid_of_an_ended_job_runs_as_usual():
    """Issue 10: an ended job whose pid now belongs to a live process."""
    me = os.getpid()
    ended = {"id": "j1", "pid": me, "pgid": me, "status": "exited", "exit_code": 0,
             "command": "true", "started": 1.0, "ended": 2.0,
             "proc_start": J._proc_start(me)}
    assert J.capture_ps(f"ps -p {me}", [ended]) is None
    running = {**ended, "status": "running", "ended": None}
    assert J.capture_ps(f"ps -p {me}", [running]).startswith("    PID TTY")


def test_capture_needs_jobs():
    assert J.capture_ps("pgrep -f sleep", []) is None


# --------------------------------------------------------------------------- #
# hints
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("cmd,secs", [
    ("sleep 60", 60.0), ("sleep 1m", 60.0), ("sleep 5; tail log", 5.0),
    ("tail log && sleep 30 && tail log", 30.0), ("timeout 100 sleep 90", 90.0),
    ("sleep $N", None), ("echo sleep 60", None), ("ls", None)])
def test_sleep_seconds(cmd, secs):
    assert J.sleep_seconds(cmd) == secs


@pytest.mark.parametrize("cmd,bg", [
    ("./server > log 2>&1 &", True), ("python app.py & sleep 5; curl x", True),
    ("nohup ./run.sh", True), ("setsid ./run.sh >/dev/null", True),
    ("make && make test", False), ("cmd &> log", False), ("echo '&'", False),
    ("ls 2>&1 | tail", False), ("echo nohup", False)])
def test_background_start(cmd, bg):
    assert J.background_start(cmd) is bg


def test_shell_hints(table):
    t = table()
    assert t.shell_hints("sleep 60", "s") == []        # no job runs
    job = t.start(argv=["sleep", "30"])
    lines = t.shell_hints("sleep 60 && tail -5 .ozagia/jobs/j1.log", "s")
    assert lines == [line for line in lines if line.startswith("[ozagia] a job tool can wait")]
    assert f'job_wait {{"job": "{job["id"]}", "until": "exit"}}' in lines[0]
    assert t.shell_hints("sleep 5", "s") == []         # short
    assert "job_start" in t.shell_hints("nohup ./srv &", "s")[0]
    for _ in range(5):
        t.shell_hints("sleep 60", "s2")
    assert t.stats()["sleep_hints"] == 1 + J.HINTS_PER_SESSION
    t.stop(job["id"], grace_s=1)


# --------------------------------------------------------------------------- #
# resource limits and enforcement
# --------------------------------------------------------------------------- #
def test_job_records_limits_and_a_short_job_is_not_killed(table):
    t = table()
    job = t.start(argv=["sleep", "0.8"])
    assert job["enforcement"] == "rlimit+watch"
    assert job["limits"] == {"memory_mb": 256.0, "pids": 256,
                             "disk_mb": 64.0, "wall_s": None,
                             "file_mb": None}
    for key in ("limits", "peak_memory_mb", "resource_events",
                "peak_disk_write_mb", "disk_write_source",
                "enforcement", "admission"):
        assert key in job
    got = t.wait(job["id"], deadline_s=10)
    assert got["status"] == "exited" and got["exit_code"] == 0
    assert got["resource_events"] == []
    assert isinstance(got["peak_memory_mb"], (int, float))
    assert got["peak_memory_mb"] > 0
    assert got["disk_write_source"] in (None, "proc.io")
    listed = t.dispatch("job_list", {})
    row = next(r for r in listed["jobs"] if r["id"] == job["id"])
    assert row["limits"]["memory_mb"] == 256
    assert row["resource_events"] == []
    assert row["peak_disk_write_mb"] == got["peak_disk_write_mb"]
    assert row["disk_write_source"] == got["disk_write_source"]
    logged = t.log(job["id"], reader="resource-reader")
    assert logged["limits"]["memory_mb"] == 256
    assert logged["peak_memory_mb"] == got["peak_memory_mb"]
    assert logged["peak_disk_write_mb"] == got["peak_disk_write_mb"]
    assert t.stats()["resource_peak_disk_write_mb"] >= 0.0


def test_wall_limit_kills_the_job_and_logs_the_event(table):
    t = table()
    policy = R._deep_merge(R.DEFAULT_POLICY, {
        "sample_s": 0.05, "reserve_disk_gb": 0, "reserve_disk_pct": 0})
    job = t.start(argv=["sleep", "30"], limits=R.ResourceLimits(wall_s=0.35),
                  policy=policy)
    assert job["enforcement"] == "rlimit+watch"
    assert job["limits"]["wall_s"] == 0.35
    got = t.wait(job["id"], deadline_s=15)
    assert got["met"] and got["status"] == "killed"
    assert got["signal"] == "SIGTERM"
    assert got["resource_events"]
    event = got["resource_events"][0]
    assert event["kind"] == "wall" and event["action"] == "sigterm"
    lines = got["output"].splitlines()
    assert any(line.startswith("[ozagia] resource:") for line in lines)
    assert any("wall-time limit" in line and "SIGTERM" in line
               for line in lines if line.startswith("[ozagia] resource:"))
    stats = t.stats()
    assert stats["jobs_resource_killed"] == 1
    assert stats["resource_events"] == 1
    assert stats["resource_peak_mb"] >= 0
    # A later log read starts at the beginning for a new reader.
    logged = t.log(job["id"], reader="after-kill")
    assert "[ozagia] resource:" in logged["output"]
    assert logged["resource_events"][0]["kind"] == "wall"


def test_admission_refusal_is_structured_and_starts_nothing(
        table, repo, monkeypatch):
    denied = {
        "ok": False, "reason": "memory.request",
        "requested": {"memory_mb": 4096.0, "disk_mb": 4096.0},
        "available": {"memory_mb": 1200.0, "disk_free_gb": 12.0},
        "shortfall": {"memory_mb": 2896.0, "disk_mb": 0.0},
        "retry_after_s": 90, "suggestions": ["stop job j-11"],
    }
    monkeypatch.setattr(R, "check_admission", lambda *a, **k: dict(denied))
    t = table()
    with pytest.raises(J.ResourceRefused) as info:
        t.start(argv=["sleep", "30"])
    assert info.value.report["reason"] == "memory.request"
    assert t.jobs == {} and t.stats()["jobs_started"] == 0
    assert not (repo / ".ozagia" / "jobs").exists()
    reply = t.dispatch("job_start", {"argv": ["true"]})
    assert reply["ok"] is False and reply["resource_refused"] is True
    assert reply["reason"] == "memory.request"
    assert reply["retry_after_s"] == 90
    assert reply["shortfall"]["memory_mb"] == 2896.0
    assert reply["suggestions"] == ["stop job j-11"]
    assert reply["admission"]["available"]["memory_mb"] == 1200.0


def test_resources_disabled_means_no_enforcement(table):
    t = table()
    job = t.start(argv=["sleep", "0.3"],
                  limits=R.ResourceLimits(memory_mb=1),
                  policy={"enabled": False})
    assert job["enforcement"] == "off"
    assert job["admission"] is None
    got = t.wait(job["id"], deadline_s=10)
    assert got["status"] == "exited" and got["exit_code"] == 0
    assert got["resource_events"] == []


def test_watchdog_path_never_signals_an_unverified_group(table, monkeypatch):
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(J, "_killpg",
                        lambda pgid, sig: calls.append((pgid, sig)))
    t = table()
    event = {"kind": "memory", "limit_mb": 1.0, "observed_mb": 2.0}
    rec = {"id": "j-stale", "owned": False, "status": "running",
           "pgid": 999999, "proc_start": "1", "log": "", "resource_events": []}
    t.jobs[rec["id"]] = rec
    t._resource_breach(rec["id"], event)
    assert calls == []
    assert event["action"] == "not_signalled"
    assert rec["resource_events"][-1]["verified"] is False


def test_cgroup_mechanism_records_path_limits_peak_and_cleanup(
        table, monkeypatch):
    monkeypatch.setattr(J, "cgroups_enabled", lambda: True)
    handle = _FakeCgroupHandle("j1", stats={
        "memory_peak_mb": 12.5, "memory_current_mb": 12.5,
        "memory_max_mb": 64.0, "pids_current": 1, "pids_max": 8,
        "oom_events": 0, "oom_kill": 0, "populated": False,
        "peak_source": "memory.peak",
    })
    manager = _FakeCgroupManager(handle)
    t = table(cgroup_manager=manager)
    job = t.start(argv=["sleep", "0.3"],
                  limits=R.ResourceLimits(memory_mb=64, pids=8))
    assert job["enforcement"] == "cgroup+rlimit"
    assert job["cgroup"]["used"] is True
    assert job["cgroup"]["path"] == str(handle.path)
    assert job["cgroup"]["limits"] == {"memory_mb": 64.0, "pids": 8}
    assert manager.calls == [{"job": job["id"], "memory_mb": 64.0, "pids": 8}]
    assert handle.attached == [job["pid"]]

    got = t.wait(job["id"], deadline_s=10)
    assert got["status"] == "exited"
    assert got["enforcement"] == "cgroup+rlimit"
    assert got["cgroup"]["peak_memory_mb"] == 12.5
    assert got["cgroup"]["io_read_mb"] == 2.0
    assert got["cgroup"]["io_write_mb"] == 4.0
    assert got["cgroup"]["io_source"] == "cgroup.io.stat"
    assert got["peak_disk_write_mb"] == 4.0
    assert got["disk_write_source"] == "cgroup.io.stat"
    assert got["cgroup"]["cleaned_up"] is True
    assert handle.cleaned == [True]
    stats = t.stats()
    assert stats["cgroup_jobs"] == 1
    assert stats["cgroup_fallbacks"] == 0
    assert stats["cgroup_oom_kills"] == 0
    assert stats["resource_peak_disk_write_mb"] == 4.0


def test_job_disk_peak_prefers_the_cgroup_and_keeps_the_final_counter(table):
    t = table()
    rec = {"id": "j-disk", "command": "true", "cwd": ".", "status": "running",
           "started": time.time(), "log": "", "resource_events": [],
           "peak_memory_mb": 1.0, "cgroup": {}}
    t.jobs["j-disk"] = rec
    try:
        t._record_resource_sample("j-disk", {
            "peak_memory_mb": 2.0, "peak_disk_write_mb": 9.0,
            "disk_source": "proc.io"})
        assert rec["peak_disk_write_mb"] == 9.0
        assert rec["disk_write_source"] == "proc.io"

        # A cgroup counter is preferred even when its first value is smaller.
        t._merge_cgroup_stats("j-disk", {
            "io_read_mb": 1.0, "io_write_mb": 4.0,
            "io_source": "cgroup.io.stat"})
        assert rec["peak_disk_write_mb"] == 4.0
        assert rec["disk_write_source"] == "cgroup.io.stat"
        assert rec["cgroup"]["io_write_mb"] == 4.0

        # A missing io.stat at a later sample leaves the cgroup value alone.
        t._merge_cgroup_stats("j-disk", {"io_write_mb": None,
                                         "io_source": None})
        assert rec["peak_disk_write_mb"] == 4.0
        assert rec["disk_write_source"] == "cgroup.io.stat"

        # The final read can be larger; the record keeps the larger counter.
        t._merge_cgroup_stats("j-disk", {
            "io_read_mb": 2.0, "io_write_mb": 12.0,
            "io_source": "cgroup.io.stat"})
        assert rec["peak_disk_write_mb"] == 12.0
        assert rec["cgroup"]["io_write_mb"] == 12.0

        # A later process-group sample never downgrades the source or peak.
        t._record_resource_sample("j-disk", {
            "peak_memory_mb": 3.0, "peak_disk_write_mb": 20.0,
            "disk_source": "proc.io"})
        assert rec["peak_disk_write_mb"] == 12.0
        assert rec["disk_write_source"] == "cgroup.io.stat"

        rec["status"] = "exited"
        public = t.public(rec)
        assert public["peak_disk_write_mb"] == 12.0
        assert public["disk_write_source"] == "cgroup.io.stat"
        assert t.stats()["resource_peak_disk_write_mb"] == 12.0
    finally:
        t.jobs.pop("j-disk", None)


def test_cgroup_oom_counters_become_a_resource_event(table, monkeypatch):
    monkeypatch.setattr(J, "cgroups_enabled", lambda: True)
    handle = _FakeCgroupHandle("j1", stats={
        "memory_peak_mb": 80.0, "memory_current_mb": 0.0,
        "memory_max_mb": 64.0, "pids_current": 0, "pids_max": 8,
        "oom_events": 2, "oom_kill": 1, "populated": False,
        "peak_source": "memory.peak",
    })
    t = table(cgroup_manager=_FakeCgroupManager(handle))
    job = t.start(argv=["true"], limits=R.ResourceLimits(memory_mb=64, pids=8))
    got = t.wait(job["id"], deadline_s=10)
    assert got["status"] == "exited"
    event = next(item for item in got["resource_events"]
                 if item["kind"] == "oom")
    assert event["oom_events"] == 2 and event["oom_kill"] == 1
    assert event["observed_mb"] == 80.0
    assert event["action"] == "cgroup_oom"
    logged = t.log(job["id"], reader="oom-reader")
    assert "[ozagia] resource:" in logged["output"]
    assert "cgroup OOM kill" in logged["output"]
    assert t.stats()["cgroup_oom_kills"] == 1


def test_cgroup_unavailable_falls_back_to_rlimit_and_watch(table):
    manager = _FakeCgroupManager(report={
        "ok": False, "fallback": True, "path": None,
        "limits": {"memory_mb": 2048.0, "pids": 256},
        "errors": [], "reason": "cgroup v2 is not mounted at /sys/fs/cgroup",
    })
    t = table(cgroup_manager=manager)
    job = t.start(argv=["true"])
    assert job["enforcement"] == "rlimit+watch"
    assert job["cgroup"]["used"] is False
    assert job["cgroup"]["fallback"] is True
    assert "not mounted" in job["cgroup"]["reason"]
    got = t.wait(job["id"], deadline_s=10)
    assert got["status"] == "exited"
    assert t.stats()["cgroup_fallbacks"] == 1


def test_cgroup_modes_and_the_environment_opt_out_skip_attempts(
        table, monkeypatch):
    manager = _FakeCgroupManager(_FakeCgroupHandle("j1"))
    t = table(cgroup_manager=manager)
    job = t.start(argv=["true"],
                  policy={**TEST_RESOURCE_POLICY, "watchdog": "rlimit"})
    assert job["enforcement"] == "rlimit+watch"
    assert job["cgroup"]["attempted"] is False
    assert job["cgroup"]["status"] == "off"
    assert "not attempted" in job["cgroup"]["reason"]
    t.wait(job["id"], deadline_s=10)
    assert manager.calls == []

    monkeypatch.setattr(J, "cgroups_enabled", lambda: False)
    job = t.start(argv=["true"],
                  policy={**TEST_RESOURCE_POLICY, "watchdog": "cgroup"})
    assert job["enforcement"] == "rlimit+watch"
    assert job["cgroup"]["attempted"] is False
    t.wait(job["id"], deadline_s=10)
    assert manager.calls == []


def test_cgroup_attach_failure_falls_back_and_cleans_up(table, monkeypatch):
    monkeypatch.setattr(J, "cgroups_enabled", lambda: True)

    class _RefusingHandle(_FakeCgroupHandle):
        def attach(self, pid: int) -> dict:
            self.attached.append(pid)
            return {"ok": False, "pid": pid, "path": str(self.path),
                    "error": "permission denied"}

    handle = _RefusingHandle("j1")
    t = table(cgroup_manager=_FakeCgroupManager(handle))
    job = t.start(argv=["true"], limits=R.ResourceLimits(memory_mb=64, pids=8))
    assert job["enforcement"] == "rlimit+watch"
    assert job["cgroup"]["used"] is False
    assert "could not join the cgroup" in job["cgroup"]["reason"]
    assert handle.cleaned == [True]
    assert t.wait(job["id"], deadline_s=10)["status"] == "exited"


def test_resource_breach_uses_cgroup_kill_only_after_sigterm(table, monkeypatch):
    monkeypatch.setattr(J, "cgroups_enabled", lambda: True)
    monkeypatch.setattr(J, "_group_ours", lambda *args: True)
    monkeypatch.setattr(J, "_group_members", lambda pgid: [123])
    monkeypatch.setattr(J, "RESOURCE_KILL_GRACE_S", 0.0)
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(J, "_killpg",
                        lambda pgid, sig: signals.append((pgid, sig)))
    handle = _FakeCgroupHandle("j-breach")
    t = table()
    rec = {"id": "j-breach", "owned": True, "status": "running",
           "pgid": 424242, "proc_start": "1", "log": "",
           "resource_events": []}
    t.jobs["j-breach"] = rec
    t._procs["j-breach"] = object()
    t._cgroups["j-breach"] = handle
    event = {"kind": "memory", "limit_mb": 1.0, "observed_mb": 2.0}
    try:
        t._resource_breach("j-breach", event)
    finally:
        rec["status"] = "exited"
        t.jobs.pop("j-breach", None)
        t._procs.pop("j-breach", None)
        t._cgroups.pop("j-breach", None)
    assert signals == [(424242, signal.SIGTERM)]
    assert handle.killed and handle.killed[0]["term_first"] is False
    assert event["action"] == "cgroup.kill"


# --------------------------------------------------------------------------- #
# the daemon state
# --------------------------------------------------------------------------- #
class _FakeMemory:
    defer_writes = False
    snapshot_dirty = False
    raw_repo_root = None


def test_idle_time_does_not_run_while_a_job_runs(repo):
    state = DaemonState(repo, _FakeMemory(), repo / "j.jsonl", idle_timeout=0.1)
    state.jobs.base_env = PATH_ENV
    try:
        r = state.dispatch({"op": "job_start", "command": "sleep 0.6"})
        assert r["ok"], r
        time.sleep(0.3)
        assert state.idle_for() == 0.0
        state.dispatch({"op": "job_wait", "job": r["job"]["id"], "deadline_s": 10})
        assert state.idle_for() < 0.2      # counts from the job's end
        time.sleep(0.15)
        assert state.idle_for() >= 0.1
    finally:
        state.jobs.shutdown(grace_s=0.5)


def test_dispatch_ps_capture_hints_and_stats(repo):
    state = DaemonState(repo, _FakeMemory(), repo / "j.jsonl")
    state.jobs.base_env = PATH_ENV
    try:
        job = state.dispatch({"op": "job_start", "argv": ["sleep", "30"],
                              "session": "ses_1", "harness": "opencode"})["job"]
        cmd = {"command": f"ps -p {job['pid']}"}
        msg = {"op": "tool", "tool": "shell", "input": cmd, "harness": "opencode"}
        res = state.dispatch({**msg, "preflight": True})
        assert res["preflight"] and res["permission"]["resources"] == [cmd["command"]]
        res = state.dispatch(msg)
        state.record(msg, res, 1.0)
        assert res["handled"] and res["meta"]["intent"] == "shell_ps"
        assert res["output"].startswith("    PID TTY")
        assert res["meta"]["jobs_running"] == 1 and res["meta"]["job_seq"] == 0
        after = {"op": "after", "tool": "shell", "session": "ses_1",
                 "input": {"command": "sleep 60"}, "output": ""}
        monkey_after = state.dispatch(after)
        assert "job_wait" in monkey_after["append"]
        listed = state.dispatch({"op": "job_list"})
        assert listed["running"] == 1 and listed["jobs"][0]["id"] == job["id"]
        assert state.dispatch({"op": "job_stop", "job": job["id"]})["status"] == "killed"
        stats = state.stats()
        assert stats["shell"]["captured"] == 1
        assert stats["shell"]["by_intent"] == {"shell_ps": 1}
        assert stats["jobs_started"] == 1 and stats["jobs"]["ps_captures"] == 1
        assert stats["jobs"]["sleep_hints"] == 1 and stats["jobs"]["jobs_running"] == 0
        for key in ("waits", "wait_seconds", "polls_avoided"):
            assert key in stats
    finally:
        state.jobs.shutdown(grace_s=0.5)


def test_daemon_stats_expose_resource_counters(repo):
    state = DaemonState(repo, _FakeMemory(), repo / "j.jsonl")
    state.jobs.base_env = PATH_ENV
    try:
        job = state.dispatch({"op": "job_start", "argv": ["sleep", "0.2"]})["job"]
        state.dispatch({"op": "job_wait", "job": job["id"], "deadline_s": 10})
        stats = state.stats()
        for key in ("jobs_resource_killed", "resource_events",
                    "resource_peak_mb"):
            assert key in stats
            assert key in stats["jobs"]
        assert stats["resource_events"] == 0
    finally:
        state.jobs.shutdown(grace_s=0.5)


def test_serve_idle_timeout_waits_for_the_job(repo, monkeypatch):
    from ozagia.daemon import server
    monkeypatch.setattr(server, "load_memory", lambda *a, **k: _FakeMemory())
    sock = Path("/tmp") / f"oz-jobidle-{os.getpid()}.sock"
    out: list[int] = []
    th = threading.Thread(target=lambda: out.append(server.serve(
        repo, sock=sock, journal=repo / "j.jsonl", idle_timeout=0.3,
        ready_stream=open(os.devnull, "w"))))
    th.start()
    deadline = time.monotonic() + 20
    while not sock.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    with DaemonClient(sock, timeout=10) as c:
        r = c.request("job_start", argv=["sleep", "1.5"])
    assert r["ok"], r
    time.sleep(1.0)
    assert th.is_alive()                     # idle for 1 s, but the job runs
    th.join(timeout=10)
    assert out == [0] and not th.is_alive()
    table = json.loads((repo / ".ozagia" / "jobs" / "jobs.json").read_text())
    assert table["jobs"][0]["status"] == "exited"   # ended on its own


# --------------------------------------------------------------------------- #
# a real daemon: protocol, shutdown, MCP, CLI, Codex hook
# --------------------------------------------------------------------------- #
def _serve(root: Path) -> subprocess.Popen:
    proc = subprocess.Popen(
        [sys.executable, "-m", "ozagia.daemon", "serve", "--root", str(root)],
        env=_env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        cwd=str(root))
    ready, _, _ = select.select([proc.stdout], [], [], READY_TIMEOUT_S)
    line = proc.stdout.readline() if ready else ""
    if not line.startswith("ready "):
        proc.kill()
        raise AssertionError(f"no ready line: {line!r} {proc.stderr.read()}")
    return proc


@pytest.fixture
def live(repo):
    proc = _serve(repo)
    yield {"root": repo, "proc": proc, "sock": socket_path(repo)}
    if proc.poll() is None:
        try:
            with DaemonClient(socket_path(repo), timeout=10) as c:
                c.shutdown()
            proc.wait(timeout=15)
        except Exception:
            proc.kill()
            proc.wait(timeout=5)


def test_protocol_round_trip_and_shutdown_policy(live):
    with DaemonClient(live["sock"], timeout=30) as c:
        normal = c.request("job_start", command="echo hi; sleep 30")["job"]
        kept = c.request("job_start", argv=["sleep", "30"], detach=True)["job"]
        got = c.request("job_wait", job=normal["id"], until="log:hi", deadline_s=10)
        assert got["met"] and got["output"] == "hi"
        assert c.request("job_wait", job="j9")["error"] == "unknown job 'j9'"
        stats = c.stats()["stats"]
        assert stats["jobs_started"] == 2 and stats["waits"] == 1
        c.shutdown()
    live["proc"].wait(timeout=15)
    try:
        assert _gone(normal["pid"]) and not _group_pids(normal["pgid"])
        assert J._alive(kept["pid"], None)
        assert f"leaving detached job {kept['id']}" in live["proc"].stderr.read()
    finally:
        J._killpg(kept["pgid"], signal.SIGKILL)
    assert _gone(kept["pid"])


class _Mcp:
    def __init__(self, root: Path, **env: str) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "ozagia.integrations.mcp.server", "--root",
             str(root), "--autobuild"],
            env=_env(**env), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, cwd=str(root))
        self.n = 0
        self.rpc("initialize", {"protocolVersion": "2025-06-18"})

    def rpc(self, method: str, params: dict) -> dict:
        self.n += 1
        assert self.proc.stdin and self.proc.stdout
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self.n,
                                          "method": method, "params": params}) + "\n")
        self.proc.stdin.flush()
        ready, _, _ = select.select([self.proc.stdout], [], [], 90)
        assert ready, "no MCP reply"
        return json.loads(self.proc.stdout.readline())

    def call(self, name: str, **args) -> tuple[bool, object]:
        res = self.rpc("tools/call", {"name": name, "arguments": args})["result"]
        text = res["content"][0]["text"]
        return res["isError"], (text if res["isError"] else json.loads(text))

    def close(self) -> None:
        assert self.proc.stdin
        self.proc.stdin.close()
        self.proc.wait(timeout=15)


def test_mcp_tools_against_a_real_daemon(live):
    mcp = _Mcp(live["root"])
    try:
        tools = {t["name"]: t for t in mcp.rpc("tools/list", {})["result"]["tools"]}
        assert {"job_start", "job_wait", "job_list", "job_log", "job_stop"} <= set(tools)
        assert tools["job_wait"]["inputSchema"]["properties"]["deadline_s"]["default"] == 45
        err, got = mcp.call("job_start", command="echo a; sleep 0.3; echo b; sleep 30")
        assert not err and got["job"]["status"] == "running"
        jid = got["job"]["id"]
        err, got = mcp.call("job_wait", job=jid, until="log:^b$", deadline_s=10)
        assert not err and got["met"] and got["output"] == "a\nb"
        err, got = mcp.call("job_wait", job=jid, deadline_s=0.3)
        assert not err and not got["met"] and got["output"] == "" and "still running" in got["note"]
        err, got = mcp.call("job_log", job=jid, cursor=0)
        assert got["output"] == "a\nb"
        err, got = mcp.call("job_list")
        assert got["running"] == 1 and got["jobs"][0]["harness"] == "mcp"
        err, got = mcp.call("job_stop", job=jid, grace_s=1)
        assert not err and got["status"] == "killed"
        err, got = mcp.call("job_start", command="true", cwd="/")
        assert err and "outside the project root" in got
        # The deadline is capped below Codex's tool timeout.
        err, got = mcp.call("job_wait", job=jid, deadline_s=500)
        assert got["deadline_s"] == 50
    finally:
        mcp.close()


def test_mcp_reports_an_unreachable_daemon(repo):
    mcp = _Mcp(repo, OZAGIA_DAEMON_AUTOSTART="0")
    try:
        err, got = mcp.call("job_list")
        assert err and "no ozagia daemon could be reached" in got
        assert "ozagia daemon ensure --root" in got
    finally:
        mcp.close()


def _cli(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "ozagia", "bg", *args, "--root", str(root)]
                          if args[0] != "run" else
                          [sys.executable, "-m", "ozagia", "bg", "run", "--root", str(root),
                           *args[1:]],
                          env=_env(), capture_output=True, text=True, timeout=60,
                          cwd=str(root))


def test_cli_round_trip(live):
    root = live["root"]
    r = _cli(root, "run", "--", "echo hello; sleep 0.2; echo bye")
    assert r.returncode == 0, r.stderr
    jid = r.stdout.split()[0]
    r = _cli(root, "wait", jid, "--deadline", "10")
    assert r.returncode == 0, r.stderr
    assert r.stdout.splitlines()[:2] == ["hello", "bye"]
    assert f"job {jid}, exited, exit 0" in r.stdout
    r = _cli(root, "wait", jid, "--deadline", "1")      # nothing new
    assert r.returncode == 0 and r.stdout.startswith("[ozagia] job")
    r = _cli(root, "run", "--", "sleep", "30")
    jid2 = r.stdout.split()[0]
    r = _cli(root, "wait", jid2, "--deadline", "0.3")
    assert r.returncode == 1 and "condition not met" in r.stdout
    r = _cli(root, "list")
    assert r.stdout.splitlines()[0].startswith(f"{jid2}\trunning")
    r = _cli(root, "log", jid, "--cursor", "0", "--json")
    assert json.loads(r.stdout)["output"] == "hello\nbye"
    r = _cli(root, "stop", jid2)
    assert r.returncode == 0 and "killed" in r.stdout
    r = _cli(root, "log", "j99")
    assert r.returncode == 2 and "unknown job" in r.stderr


def test_cli_without_a_daemon(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    r = subprocess.run([sys.executable, "-m", "ozagia", "bg", "list", "--root", str(plain)],
                       env=_env(), capture_output=True, text=True, timeout=60)
    assert r.returncode == 2 and "no ozagia daemon answers" in r.stderr
    assert not (plain / ".ozagia").exists()


def test_codex_hook_answers_ps_for_a_job(live):
    from ozagia.integrations.codex import hook
    root = live["root"]
    with DaemonClient(live["sock"], timeout=10) as c:
        job = c.request("job_start", argv=["sleep", "30"])["job"]
        time.sleep(0.2)
        payload = {"hook_event_name": "PreToolUse", "tool_name": "Bash",
                   "tool_input": {"command": f"ps -p {job['pid']}"}, "cwd": str(root),
                   "session_id": "s1"}
        out = hook.handle(payload, root)
        reason = out["hookSpecificOutput"]["permissionDecisionReason"]
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "    PID TTY          TIME CMD" in reason and f"job {job['id']}" in reason
        # Other ps commands, and other commands, run as usual.
        for cmd in ["ps aux", "ps -p 1", "ls"]:
            assert hook.handle({**payload, "tool_input": {"command": cmd}}, root) is None
        c.request("job_stop", job=job["id"], grace_s=1)


def test_codex_hook_ps_without_a_daemon_starts_nothing(repo):
    from ozagia.integrations.codex import hook
    payload = {"hook_event_name": "PreToolUse", "tool_name": "Bash",
               "tool_input": {"command": "pgrep -f x"}, "cwd": str(repo)}
    assert hook.handle(payload, repo) is None
    assert not socket_path(repo).exists()


def test_agents_section_names_the_job_tools_only_with_jobs():
    from ozagia.integrations.codex.install import AGENTS_SECTION, agents_section
    with_jobs = agents_section(jobs=True)
    for word in ("job_start", "job_wait", "write_stdin", "sleep",
                 "outside the Codex sandbox"):
        assert word in with_jobs
    assert AGENTS_SECTION == agents_section() and "job_" not in AGENTS_SECTION


# --------------------------------------------------------------------------- #
# review fixes: opt-in, refusals, process-group safety, lock, MCP, CLI
# --------------------------------------------------------------------------- #
def test_jobs_are_opt_in(bare_repo, monkeypatch):
    t = J.JobTable(bare_repo, base_env=PATH_ENV,
                   resource_policy=TEST_RESOURCE_POLICY,
                   cgroup_manager=_UnavailableCgroupManager())
    try:
        with pytest.raises(J.JobError) as e:
            t.start(command="true")
        text = str(e.value)
        for word in (".ozagia.toml", "enabled = true", "OZAGIA_JOBS=1",
                     "outside the harness sandbox", "daemon's privileges"):
            assert word in text, word
        reply = t.dispatch("job_start", {"command": "true"})
        assert not reply["ok"] and "background jobs are off" in reply["error"]
        # The other ops still work.
        assert t.dispatch("job_list", {}) == {"ok": True, "jobs": [], "running": 0}
        assert t.wait(None, until="file:nope", deadline_s=0)["met"] is False
        assert t.stats()["jobs_refused"] == 2
        # No background hint points to job_start while jobs are off.
        assert t.shell_hints("./server &", "s") == []
        monkeypatch.setenv("OZAGIA_JOBS", "1")
        job = t.start(command="true")
        assert t.wait(job["id"], deadline_s=10)["status"] == "exited"
        assert t.shell_hints("./server &", "s")
        # OZAGIA_JOBS=0 wins over the file.
        (bare_repo / ".ozagia.toml").write_text("[jobs]\nenabled = true\n")
        clear_config_cache()
        monkeypatch.setenv("OZAGIA_JOBS", "0")
        assert not J.jobs_enabled(bare_repo)
        monkeypatch.delenv("OZAGIA_JOBS")
        assert J.jobs_enabled(bare_repo)
        (bare_repo / ".ozagia.toml").write_text("[jobs]\nenabled = 1\n")
        clear_config_cache()      # the cache keys on mtime, which may not move
        assert not J.jobs_enabled(bare_repo)          # malformed: off
    finally:
        t.shutdown(grace_s=0.5)


def test_git_through_env_and_shell_forms_is_refused(table, repo):
    t = table()
    alias = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "alias.x",
             "GIT_CONFIG_VALUE_0": "commit"}
    for kw, why in [
            ({"command": "git x -am m", "env": alias}, "GIT_CONFIG_COUNT"),
            ({"argv": ["git", "status"], "env": {"GIT_DIR": "/tmp/x"}}, "GIT_DIR"),
            ({"command": "true", "env": {"GIT_SSH_COMMAND": "x"}}, "GIT_"),
            ({"command": "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=alias.x "
                         "GIT_CONFIG_VALUE_0=commit git x"}, "mentions git"),
            ({"command": "echo git commit -am x | bash"}, "mentions git"),
            ({"command": "bash <<< 'git commit -am x'"}, "mentions git"),
            ({"command": "echo 'echo hi' | sh"}, "feeds text to a shell"),
            ({"command": "curl -s x | /bin/bash -s"}, "feeds text to a shell"),
            ({"command": "zsh <<< 'echo hi'"}, "feeds text to a shell"),
            ({"command": "bash -c 'git commit -m x'"}, "moves refs"),
            ({"command": "/usr/bin/env git reset --hard"}, "moves refs")]:
        with pytest.raises(J.JobError, match=why):
            t.start(**kw)
    assert t.jobs == {}
    # Nothing was committed.
    assert subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                          capture_output=True).returncode != 0
    # Pipes that do not feed a shell, and git in a simple form, still run.
    for cmd in ["seq 3 | tail -1", "git status --short"]:
        job = t.start(command=cmd)
        assert t.wait(job["id"], deadline_s=10)["exit_code"] == 0


def _kills(monkeypatch) -> list[tuple[int, int]]:
    calls: list[tuple[int, int]] = []
    real = J._killpg

    def spy(pgid, sig):
        calls.append((pgid, sig))
        real(pgid, sig)
    monkeypatch.setattr(J, "_killpg", spy)
    return calls


def test_an_ended_jobs_group_is_never_signalled(table, monkeypatch):
    """Issue 2: job_stop on a long-ended job, and shutdown, send nothing."""
    t = table()
    job = t.start(command="true")
    assert t.wait(job["id"], deadline_s=10)["status"] == "exited"
    assert t.jobs[job["id"]]["group_gone"] is True
    calls = _kills(monkeypatch)
    assert "already ended" in t.stop(job["id"], sig="KILL")["note"]
    t.shutdown(grace_s=0.2)
    assert calls == []


def _spawn_group(command: str) -> subprocess.Popen:
    return subprocess.Popen(["bash", "-c", command], start_new_session=True,
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)


def _wait_members(pgid: int, n: int) -> list[tuple[int, int]]:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        got = J._group_members(pgid)
        if len(got) >= n:
            return got
        time.sleep(0.05)
    raise AssertionError(f"group {pgid} has {J._group_members(pgid)}")


def test_group_ours_checks_start_times():
    proc = _spawn_group("exec sleep 30")
    try:
        _wait_members(proc.pid, 1)
        start = J._proc_start(proc.pid)
        assert J._group_ours(proc.pid, start)
        # A job that started earlier than this group's founder: the pgid
        # was reused.
        assert not J._group_ours(proc.pid, str(int(start) - 100))
        assert not J._group_ours(proc.pid, None)
    finally:
        proc.kill()
        proc.wait()
    assert not J._group_ours(proc.pid, start)
    # The leader is gone and a member lives: ours only if the member started
    # no earlier than the job.
    proc = _spawn_group("sleep 30 & exit 0")
    proc.wait()
    try:
        (member, mstart), = _wait_members(proc.pid, 1)
        assert J._group_ours(proc.pid, str(mstart - 5))
        assert not J._group_ours(proc.pid, str(mstart + 100))
    finally:
        J._killpg(proc.pid, signal.SIGKILL)


def _write_table(table_dir: Path, recs: list[dict]) -> None:
    table_dir.mkdir(parents=True, exist_ok=True)
    (table_dir / J.TABLE_NAME).write_text(json.dumps({"next": 9, "jobs": recs}))


def _rec(jid: str, pid: int, start: str | None, **kw) -> dict:
    return {"id": jid, "command": "sleep 30", "pid": pid, "pgid": pid,
            "proc_start": start, "status": "running", "exit_code": None,
            "signal": None, "started": time.time() - 60, "ended": None,
            "detach": False, "log": "/nonexistent.log", **kw}


def test_load_checks_pid_reuse_and_sets_ended(repo, table, monkeypatch):
    """Issues 6, 8 and the detached pid-reuse guard: a recorded pid that is
    now another process (start time differs) is `gone`, never signalled."""
    me = os.getpid()
    _write_table(repo / ".ozagia" / "jobs", [
        _rec("j1", me, "1", detach=True), _rec("j2", me, "1"),
        _rec("j3", me, None, status="gone")])
    calls = _kills(monkeypatch)
    before = time.time()
    t = table()
    for jid in ("j1", "j2", "j3"):
        rec = t.jobs[jid]
        assert rec["status"] == "gone", jid
        assert rec["ended"] >= before, jid        # not None: elapsed stops
    assert t.public(t.jobs["j1"])["elapsed_s"] == t.public(t.jobs["j1"])["elapsed_s"]
    assert "already ended" in t.stop("j1")["note"]
    assert calls == []


def test_load_stops_jobs_a_crashed_daemon_left_running(repo, table, capsys):
    """Issue 6: non-detached jobs of a daemon that died are stopped on load;
    detached ones are kept; a `gone` job with a live group can be stopped."""
    orphan = _spawn_group("exec sleep 30")
    kept = _spawn_group("exec sleep 30")
    leaderless = _spawn_group("sleep 30 & exit 0")
    leaderless.wait()
    try:
        _wait_members(orphan.pid, 1)
        _wait_members(kept.pid, 1)
        (member, _), = _wait_members(leaderless.pid, 1)
        _write_table(repo / ".ozagia" / "jobs", [
            _rec("j1", orphan.pid, J._proc_start(orphan.pid)),
            _rec("j2", kept.pid, J._proc_start(kept.pid), detach=True),
            _rec("j3", leaderless.pid, str(int(J._proc_start(member)) - 1),
                 status="gone", ended=time.time())])
        t = table()
        assert orphan.wait(timeout=10) == -signal.SIGTERM
        rec = t.jobs["j1"]
        assert rec["status"] == "gone" and rec["stopped_on_load"]
        assert t.public(rec)["stopped_on_load"] is True
        assert "left running by an earlier daemon" in capsys.readouterr().err
        assert t.stats()["stopped_on_load"] == 1
        assert t.jobs["j2"]["status"] == "detached" and kept.poll() is None
        assert t.stop("j2", grace_s=2)["status"] == "gone"
        assert kept.wait(timeout=10) == -signal.SIGTERM
        # The gone job's surviving group member is reachable.
        assert J._alive(member, None)
        t.stop("j3", grace_s=2)
        assert _gone(member) and t.jobs["j3"]["group_gone"]
    finally:
        for p in (orphan, kept):
            if p.poll() is None:
                p.kill()
                p.wait()
        J._killpg(leaderless.pid, signal.SIGKILL)


def test_status_follows_the_whole_group(table):
    """Issue 5: the leader exits while a member of its group runs."""
    t = table()
    job = t.start(command="sleep 1.2 & echo started")
    got = t.wait(job["id"], until="log:started", deadline_s=10)
    assert got["met"]
    time.sleep(0.3)
    pub = t.public(t.jobs[job["id"]])
    assert pub["status"] == "running" and pub["leader_exited"] and pub["group_alive"]
    assert t.running_count() == 1
    got = t.wait(job["id"], deadline_s=0.2)
    assert not got["met"] and got["group_alive"]
    got = t.wait(job["id"], deadline_s=10)
    assert got["met"] and got["status"] == "exited" and got["exit_code"] == 0
    assert "group_alive" not in got and t.running_count() == 0
    assert _group_pids(job["pgid"]) == []
    # Stopping such a job reaches the survivors.
    job = t.start(command="sleep 30 & echo started")
    t.wait(job["id"], until="log:started", deadline_s=10)
    deadline = time.monotonic() + 5
    while not t.jobs[job["id"]].get("leader_exited") and time.monotonic() < deadline:
        time.sleep(0.05)
    assert t.stop(job["id"], grace_s=2)["status"] == "exited"
    assert _group_pids(job["pgid"]) == []


def test_eviction_keeps_jobs_whose_group_lives(repo, table, monkeypatch):
    """Issue 9."""
    leaderless = _spawn_group("sleep 30 & exit 0")
    leaderless.wait()
    try:
        (member, mstart), = _wait_members(leaderless.pid, 1)
        _write_table(repo / ".ozagia" / "jobs", [
            _rec("j1", leaderless.pid, str(mstart - 1), status="gone",
                 ended=time.time())])
        monkeypatch.setattr(J, "MAX_JOBS", 2)
        t = table()
        for _ in range(3):
            job = t.start(command="true")
            t.wait(job["id"], deadline_s=10)
        assert "j1" in t.jobs and len(t.jobs) == 2
        t.stop("j1", grace_s=1)
        job = t.start(command="true")
        t.wait(job["id"], deadline_s=10)
        assert "j1" not in t.jobs
    finally:
        J._killpg(leaderless.pid, signal.SIGKILL)


def test_log_wait_does_not_hold_the_table_lock(table, monkeypatch):
    """Issue 3: a slow scan leaves the other ops free; long lines are cut
    before the regex sees them."""
    t = table()
    job = t.start(command="echo a; sleep 30")
    real = J.JobTable._scan

    def slow(*a, **kw):
        time.sleep(0.5)
        return real(*a, **kw)
    monkeypatch.setattr(J.JobTable, "_scan", staticmethod(slow))
    th = threading.Thread(target=t.wait, args=(job["id"],),
                          kwargs={"until": "log:never", "deadline_s": 2})
    th.start()
    time.sleep(0.2)
    for _ in range(5):
        t0 = time.monotonic()
        t.running_count()
        t.dispatch("job_list", {})
        t.shell_hints("sleep 60", "s")
        assert time.monotonic() - t0 < 0.2
        time.sleep(0.1)
    th.join()
    monkeypatch.setattr(J.JobTable, "_scan", staticmethod(real))
    t.stop(job["id"], grace_s=1)
    long = t.start(command=f"printf '%0{J.MATCH_LINE_BYTES + 10}d' 0; echo X; echo ok")
    got = t.wait(long["id"], until="log:X$", deadline_s=10)
    assert not got["met"] and got["status"] == "exited"
    long = t.start(command="printf 'x%.0s' $(seq 100); echo X")
    assert t.wait(long["id"], until="log:X$", deadline_s=10)["met"]


def test_concurrent_waits_on_one_reader(table):
    t = table()
    job = t.start(command="sleep 0.3; echo one; echo two; sleep 30")
    out: list[dict] = []
    ths = [threading.Thread(target=lambda: out.append(
        t.wait(job["id"], until="log:two", deadline_s=10, reader="r")))
        for _ in range(2)]
    for th in ths:
        th.start()
    for th in ths:
        th.join()
    assert [o["met"] for o in out] == [True, True]
    assert all(o["output"] == "one\ntwo" for o in out)
    # The reader's offset is at the end: nothing is returned twice again.
    assert t.log(job["id"], reader="r")["output"] == ""
    assert t.cursors[(job["id"], "r")] == len(b"one\ntwo\n")
    t.stop(job["id"], grace_s=1)


def test_mcp_job_wait_and_stop_arguments():
    """Issues 7 and 4: a file wait without a job passes none; job_stop's
    grace is capped."""
    from types import SimpleNamespace

    from ozagia.integrations.mcp import server as M
    sent: list[tuple[str, dict]] = []
    fake = SimpleNamespace(_job=lambda op, f: sent.append((op, f)) or {})
    M.DevMemoryServer._job_wait(fake, {"until": "file:out.txt"})
    M.DevMemoryServer._job_wait(fake, {"until": "exit"})
    M.DevMemoryServer._job_wait(fake, {"job": "j2", "until": "file:x"})
    M.DevMemoryServer._job_stop(fake, {"job": "j1", "grace_s": 500})
    assert [f.get("job") for _op, f in sent[:3]] == [None, "any", "j2"]
    assert sent[3][1]["grace_s"] == M.MCP_STOP_GRACE_MAX_S


def test_mcp_job_calls_do_not_block_other_calls(live):
    mcp = _Mcp(live["root"])
    try:
        (live["root"] / "here.txt").write_text("x")
        # A file wait with no job (and no job running) checks the file.
        err, got = mcp.call("job_wait", until="file:here.txt", deadline_s=5)
        assert not err and got["met"] and got["job"] is None
        err, job = mcp.call("job_start", argv=["sleep", "30"])
        jid = job["job"]["id"]
        tools = {t["name"]: t for t in mcp.rpc("tools/list", {})["result"]["tools"]}
        assert "outside the Codex sandbox" in tools["job_start"]["description"]
        # A 3 s wait, then a ping: the ping's answer comes first.
        assert mcp.proc.stdin and mcp.proc.stdout
        mcp.proc.stdin.write(json.dumps({
            "jsonrpc": "2.0", "id": 100, "method": "tools/call",
            "params": {"name": "job_wait", "arguments": {"job": jid, "deadline_s": 3}}})
            + "\n" + json.dumps({"jsonrpc": "2.0", "id": 101, "method": "ping"}) + "\n")
        mcp.proc.stdin.flush()
        t0 = time.monotonic()
        first = json.loads(mcp.proc.stdout.readline())
        assert first["id"] == 101 and time.monotonic() - t0 < 2
        second = json.loads(mcp.proc.stdout.readline())
        assert second["id"] == 100 and not second["result"]["isError"]
        err, got = mcp.call("job_stop", job=jid, grace_s=1)
        assert not err and got["status"] == "killed"
    finally:
        mcp.close()


@pytest.fixture
def live_off(bare_repo):
    proc = _serve(bare_repo)
    yield {"root": bare_repo, "proc": proc, "sock": socket_path(bare_repo)}
    try:
        with DaemonClient(socket_path(bare_repo), timeout=10) as c:
            c.shutdown()
        proc.wait(timeout=15)
    except Exception:
        proc.kill()
        proc.wait(timeout=5)


def test_job_start_is_refused_over_protocol_mcp_and_cli_when_off(live_off):
    root = live_off["root"]
    with DaemonClient(live_off["sock"], timeout=10) as c:
        r = c.request("job_start", command="true")
        assert not r["ok"] and "background jobs are off" in r["error"]
        assert c.request("job_list")["ok"]
    r = _cli(root, "run", "--", "true")
    assert r.returncode == 2 and "background jobs are off" in r.stderr
    assert "outside the harness sandbox" in r.stderr
    mcp = _Mcp(root)
    try:
        err, got = mcp.call("job_start", command="true")
        assert err and "background jobs are off" in got
        err, got = mcp.call("job_list")
        assert not err and got["jobs"] == []
    finally:
        mcp.close()


def test_cli_readers_are_per_shell(live):
    from ozagia.daemon.jobcli import cli_reader
    assert cli_reader().startswith(f"cli:{os.getppid()}:")
    root = live["root"]
    r = _cli(root, "run", "--", "echo hello")
    jid = r.stdout.split()[0]
    cmd = shlex.join([sys.executable, "-m", "ozagia", "bg", "wait", jid,
                      "--root", str(root), "--deadline", "10"])
    # (`; true`: bash would exec a last simple command in its own place.)
    outs = [subprocess.run(["bash", "-c", f"{cmd}; true"], env=_env(), capture_output=True,
                           text=True, timeout=60).stdout for _ in range(2)]
    # Two shells: each gets the line once.
    assert all(o.splitlines()[0] == "hello" for o in outs), outs
    again = subprocess.run(["bash", "-c", f"{cmd}; {cmd}; true"], env=_env(),
                           capture_output=True, text=True, timeout=60).stdout
    assert again.count("hello") == 1       # one shell: read once


def test_codex_brief_and_install_mention_jobs_only_when_enabled(bare_repo, tmp_path):
    from ozagia.integrations.codex import hook
    from ozagia.integrations.codex import install as inst
    assert hook.JOBS_LINE not in hook.session_brief(bare_repo)
    work = tmp_path / "proj"
    work.mkdir()
    inst.install(work, python="py")
    assert "job_start" not in (work / "AGENTS.md").read_text()
    assert not (work / ".ozagia.toml").exists()
    (work / ".ozagia.toml").write_text("[reads]\nstructured_outline_lines = 10")
    got = inst.install(work, python="py", jobs=True)
    assert got["jobs"] == work.resolve() / ".ozagia.toml"
    text = (work / ".ozagia.toml").read_text()
    assert text.startswith("[reads]\nstructured_outline_lines = 10\n\n[jobs]")
    assert J.jobs_enabled(work)
    agents = (work / "AGENTS.md").read_text()
    assert "job_start" in agents and "outside the Codex sandbox" in agents
    assert hook.JOBS_LINE in hook.session_brief(work)
    # Reinstalling without --jobs keeps the paragraph while the file enables jobs.
    inst.install(work, python="py")
    assert "job_start" in (work / "AGENTS.md").read_text()
    inst.uninstall(work)
    assert (work / ".ozagia.toml").read_text() == "[reads]\nstructured_outline_lines = 10\n"
    clear_config_cache()
    assert not J.jobs_enabled(work)
    with pytest.raises(inst.SettingsError, match="--scope project"):
        inst.install(work, scope="user", home=tmp_path / "codex", jobs=True)
    (work / ".ozagia.toml").write_text("[jobs]\nenabled = false\n")
    with pytest.raises(inst.SettingsError, match="does not enable"):
        inst.install(work, python="py", jobs=True)
