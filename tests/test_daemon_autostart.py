"""Daemon autostart (`python -m ozagia.daemon ensure`) and the idle timeout,
against a real temp repository."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from ozagia.daemon import autostart
from ozagia.daemon.client import DaemonClient
from ozagia.daemon.protocol import socket_path
from ozagia.daemon.server import DaemonState

SRC = Path(__file__).resolve().parents[1] / "src"
WAIT_S = 60.0


def _env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("OZAGIA_")}
    env["PYTHONPATH"] = str(SRC)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


@pytest.fixture
def repo(tmp_path, monkeypatch):
    for k in [k for k in os.environ if k.startswith("OZAGIA_")]:
        monkeypatch.delenv(k)
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "__init__.py").write_text("")
    (root / "pkg" / "mod.py").write_text("def helper(a):\n    return a\n")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    yield root
    _stop(root)


def _stop(root: Path) -> None:
    sock = socket_path(root)
    reply = autostart.ping(sock)
    if reply is None:
        return
    pid = reply.get("pid")
    try:
        with DaemonClient(sock, timeout=10) as c:
            c.shutdown()
    except Exception:
        pass
    deadline = time.monotonic() + 15
    while pid and time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return
        time.sleep(0.05)
    if pid:
        os.kill(pid, signal.SIGKILL)


def test_ensure_starts_once_then_reuses(repo):
    first = autostart.ensure(repo, wait_s=WAIT_S, idle_timeout_s=600)
    assert first["ok"] and first["started"], first
    second = autostart.ensure(repo, wait_s=WAIT_S)
    assert second["ok"] and not second["started"]
    assert second["pid"] == first["pid"]
    with DaemonClient(socket_path(repo), timeout=10) as c:
        stats = c.stats()["stats"]
    assert stats["idle_timeout_s"] == 600
    assert stats["root"] == str(repo.resolve())
    # stdout and stderr of the detached daemon go to the state dir's log.
    assert "ready" in (repo / ".ozagia" / autostart.LOG_NAME).read_text()


def test_concurrent_ensure_starts_one_daemon(repo):
    cmd = [sys.executable, "-m", "ozagia.daemon", "ensure", "--root", str(repo),
           "--wait", str(WAIT_S)]
    procs = [subprocess.Popen(cmd, env=_env(), stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True) for _ in range(4)]
    results = []
    for p in procs:
        out, err = p.communicate(timeout=WAIT_S + 30)
        assert p.returncode == 0, (out, err)
        results.append(json.loads(out))
    assert sum(r["started"] for r in results) == 1, results
    assert len({r["pid"] for r in results}) == 1, results


def test_ensure_reports_a_daemon_that_exits(repo):
    # --no-build without a snapshot: serve exits with an error.
    res = autostart.ensure(repo, wait_s=WAIT_S, extra_args=["--no-build"])
    assert not res["ok"] and res["started"]
    assert "exited" in res["reason"] and "no snapshot" in res["reason"]


def test_ensure_lock_held_waits_then_gives_up(repo):
    import fcntl
    lock = repo / ".ozagia" / autostart.LOCK_NAME
    lock.parent.mkdir(parents=True, exist_ok=True)
    with open(lock, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        t0 = time.monotonic()
        res = autostart.ensure(repo, wait_s=0.3)
        assert time.monotonic() - t0 < 3
    assert res == {"ok": False, "started": False, "sock": str(socket_path(repo)),
                   "reason": "another daemon is starting"}


def test_cli_ensure_and_idle_timeout(repo):
    r = subprocess.run([sys.executable, "-m", "ozagia.daemon", "ensure", "--root",
                        str(repo), "--wait", str(WAIT_S), "--idle-timeout", "1"],
                       env=_env(), capture_output=True, text=True, timeout=WAIT_S + 30)
    assert r.returncode == 0, r.stderr
    res = json.loads(r.stdout)
    assert res["ok"] and res["started"]
    sock = socket_path(repo)
    deadline = time.monotonic() + 20
    while sock.exists() and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not sock.exists()
    assert autostart.ping(sock) is None
    assert "idle for 1 s; shutting down" in (repo / ".ozagia" / autostart.LOG_NAME).read_text()


def test_idle_timeout_waits_for_requests_in_progress(tmp_path):
    state = DaemonState(tmp_path, None, tmp_path / "j.jsonl", idle_timeout=0.1)
    state.begin()
    time.sleep(0.15)
    assert state.idle_for() == 0.0
    state.end()
    assert state.idle_for() < 0.1
    time.sleep(0.15)
    assert state.idle_for() >= 0.1
    assert DaemonState(tmp_path, None, tmp_path / "j.jsonl", idle_timeout=0).idle_timeout is None


def test_stats_count_requests_by_harness(tmp_path):
    state = DaemonState(tmp_path, None, tmp_path / "j.jsonl")
    for h in ("codex", "codex", "claude", None):
        msg = {"op": "hello", "harness": h} if h else {"op": "ping"}
        state.record(msg, {"ok": True}, 0.1)
    assert state.stats()["by_harness"] == {"codex": 2, "claude": 1}


def test_serve_idle_timeout_in_process(tmp_path, monkeypatch):
    # The idle watcher stops serve() on its own.
    from ozagia.daemon import server
    root = tmp_path / "r"
    root.mkdir()
    monkeypatch.setattr(server, "load_memory", lambda *a, **k: _FakeMemory())
    sock = Path("/tmp") / f"oz-idle-{os.getpid()}.sock"
    out: list[int] = []
    t = threading.Thread(target=lambda: out.append(server.serve(
        root, sock=sock, journal=tmp_path / "j.jsonl", idle_timeout=0.3,
        ready_stream=open(os.devnull, "w"))))
    t0 = time.monotonic()
    t.start()
    t.join(timeout=20)
    assert out == [0] and time.monotonic() - t0 < 10
    assert not sock.exists()


class _FakeMemory:
    defer_writes = False
    snapshot_dirty = False
