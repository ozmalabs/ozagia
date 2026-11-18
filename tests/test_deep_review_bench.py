from __future__ import annotations

import io
import subprocess

import pytest

from ozagia.core.deep_review import bench


class FakeProcess:
    pid = 12345

    def __init__(self, *, stdout=b"", stderr=b"", returncode=0):
        self.stdout = io.BytesIO(stdout)
        self.stderr = io.BytesIO(stderr)
        self.returncode = returncode
        self.calls = []

    def wait(self, timeout=None):
        self.calls.append(timeout)
        return self.returncode


def test_requires_explicit_opt_in_and_never_starts_process(monkeypatch, tmp_path):
    def forbidden(*args, **kwargs):
        raise AssertionError("process should not start")

    monkeypatch.setattr(bench.subprocess, "Popen", forbidden)
    with pytest.raises(ValueError, match="enabled=True"):
        bench.run_bench(tmp_path, [["echo", "hello"]])


def test_uses_argv_without_shell_and_caps_and_redacts_output(monkeypatch, tmp_path):
    process = FakeProcess(stdout=b"x" * 12 + b" sk-proj-12345678901234567890", stderr=b"fine")
    calls = []

    def fake_popen(argv, **kwargs):
        calls.append((argv, kwargs))
        return process

    monkeypatch.setattr(bench.subprocess, "Popen", fake_popen)
    result = bench.run_bench(
        tmp_path, [["python", "-c", "print('hello')"]], enabled=True,
        output_limit_bytes=12, labels=["smoke"])

    assert calls[0][0] == ["python", "-c", "print('hello')"]
    assert calls[0][1]["shell"] is False
    assert calls[0][1]["stdin"] is subprocess.DEVNULL
    row = result["commands"][0]
    assert row["label"] == "smoke"
    assert row["status"] == "completed"
    assert row["stdout"] == "x" * 12
    assert row["stdout_truncated"] is True
    assert row["stderr"] == "fine"
    assert result["output_limit_bytes_per_stream"] == 12


def test_timeout_is_reported_and_process_group_is_stopped(monkeypatch, tmp_path):
    process = FakeProcess(returncode=-15)
    wait_calls = 0

    def fake_wait(timeout=None):
        nonlocal wait_calls
        wait_calls += 1
        if wait_calls == 1:
            raise subprocess.TimeoutExpired("bench", timeout)
        return process.returncode

    process.wait = fake_wait
    killed = []
    monkeypatch.setattr(bench.subprocess, "Popen", lambda *a, **k: process)
    monkeypatch.setattr(bench.os, "name", "posix")
    monkeypatch.setattr(bench.os, "killpg", lambda pid, sig: killed.append((pid, sig)))

    result = bench.run_bench(tmp_path, [["bench"]], enabled=True, timeout_s=1)
    assert result["commands"][0]["status"] == "timed_out"
    assert killed[0][0] == process.pid


def test_exited_command_with_inherited_pipes_stops_its_process_group(monkeypatch, tmp_path):
    released = __import__("threading").Event()

    class InheritedPipe:
        def read(self, _size):
            released.wait(timeout=2)
            return b""

        def close(self):
            pass

    process = FakeProcess()
    process.stdout = InheritedPipe()
    process.stderr = InheritedPipe()
    killed = []

    def killpg(pid, sig):
        killed.append((pid, sig))
        released.set()

    monkeypatch.setattr(bench.subprocess, "Popen", lambda *a, **k: process)
    monkeypatch.setattr(bench.os, "name", "posix")
    monkeypatch.setattr(bench.os, "killpg", killpg)
    result = bench.run_bench(tmp_path, [["benchmark"]], enabled=True)

    row = result["commands"][0]
    assert row["status"] == "completed"
    assert row["orphaned_process_group_stopped"] is True
    assert killed == [(process.pid, bench.signal.SIGKILL)]


@pytest.mark.parametrize("kwargs", [
    {"timeout_s": 0}, {"timeout_s": 301}, {"output_limit_bytes": 0},
    {"output_limit_bytes": 65537},
])
def test_rejects_unbounded_options(kwargs, tmp_path):
    with pytest.raises(ValueError):
        bench.run_bench(tmp_path, [["tool"]], enabled=True, **kwargs)


def test_rejects_shell_string_and_invalid_argv(tmp_path):
    with pytest.raises(ValueError, match="argument vectors"):
        bench.run_bench(tmp_path, "tool; rm -rf .", enabled=True)
    with pytest.raises(ValueError, match="invalid argument"):
        bench.run_bench(tmp_path, [["tool", "bad\x00arg"]], enabled=True)
