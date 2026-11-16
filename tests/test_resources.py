"""Host resource observation, history and read-only admission checks."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from ozagia.core import resources as R
from ozagia import resources_cli

SRC = Path(__file__).resolve().parents[1] / "src"


def _host(*, total=1000.0, available=100.0):
    return {
        "schema": R.SCHEMA,
        "checked_at": "2026-11-16T00:00:00+00:00",
        "platform": "linux",
        "cpu_count": 4,
        "load": {"1m": 1.0, "5m": 1.0, "15m": 1.0},
        "memory": {
            "source": "test", "total_mb": total, "available_mb": available,
            "used_pct": round((1 - available / total) * 100, 1),
            "swap_total_mb": 0.0, "swap_free_mb": 0.0,
        },
        "pressure": {"cpu": None, "memory": None, "io": None},
    }


def _disk(*, free=50.0, total=100.0, tmpfs=False):
    return {
        "path": "/repo", "exists": True, "probe": "/repo", "mount": "/",
        "fstype": "tmpfs" if tmpfs else "ext4", "total_gb": total,
        "free_gb": free, "free_pct": round(free / total * 100, 1),
        "inodes_free": 1000, "is_tmpfs": tmpfs, "source": "statvfs",
        "error": None,
    }


def _policy(**overrides):
    policy = R._deep_merge(R.DEFAULT_POLICY, {})
    policy.update(overrides)
    return policy


def test_disk_usage_reports_tmpfs(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(R, "_mounts", lambda: [(str(tmp_path), "tmpfs")])
    report = R.disk_usage(tmp_path)
    assert report["is_tmpfs"] is True
    assert report["fstype"] == "tmpfs"
    assert report["free_gb"] is not None
    assert report["source"] in {"statvfs", "shutil"}


def test_host_snapshot_has_memory_and_load() -> None:
    if not Path("/proc/meminfo").exists():
        pytest.skip("Linux memory counters are unavailable")
    report = R.host_snapshot()
    assert report["memory"]["total_mb"] and report["memory"]["total_mb"] > 0
    assert report["memory"]["available_mb"] is not None
    assert report["cpu_count"] == os.cpu_count()


def test_history_estimate_and_confidence(tmp_path: Path) -> None:
    for value in (100.0, 200.0, 300.0):
        R.record_sample(tmp_path, "cmd:python:abc", memory_mb=value,
                        disk_mb=10.0, duration_s=2.0, exit_code=0)
    summary = R.history_summary(tmp_path, key="cmd:python:abc")
    item = summary["cmd:python:abc"]
    assert item["samples"] == 3
    assert item["confidence"] == "low"
    assert item["memory_mb"]["max"] == 300.0
    estimate = R.estimate(tmp_path, "cmd:python:abc")
    assert estimate["samples"] == 3
    assert estimate["memory_mb"] == item["memory_mb"]["p90"]


def test_check_admission_refuses_low_memory(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(R, "host_snapshot", lambda: _host(available=100.0))
    monkeypatch.setattr(R, "disk_usage", lambda path: _disk())
    policy = _policy()
    policy["admission"]["min_free_memory_pct"] = 15.0
    report = R.check_admission(tmp_path, kind="job", memory_mb=3000,
                               disk_mb=10, policy=policy)
    assert report["ok"] is False
    assert report["shortfall"]["memory_mb"] > 0
    assert any(check["id"] == "memory.request" and check["level"] == "error"
               for check in report["checks"])


def test_check_admission_tmpfs_worktree(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(R, "host_snapshot", lambda: _host(available=900.0))
    monkeypatch.setattr(R, "disk_usage", lambda path: _disk(free=50.0, tmpfs=True))
    policy = _policy()
    policy["worktrees"]["avoid_tmpfs"] = True
    report = R.check_admission(tmp_path, kind="worktree", path=tmp_path,
                               policy=policy)
    assert report["ok"] is False
    assert any(check["id"] == "disk.tmpfs" for check in report["checks"])


def test_check_admission_offers_delay(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(R, "host_snapshot", lambda: _host(available=100.0))
    monkeypatch.setattr(R, "disk_usage", lambda path: _disk(free=50.0))
    policy = _policy()
    report = R.check_admission(
        tmp_path, kind="job", memory_mb=3000, disk_mb=10,
        freeing=[{"memory_mb": 4000, "disk_mb": 0, "eta_s": 90}],
        policy=policy)
    assert report["ok"] is False
    assert report["retry_after_s"] == 90
    assert any("retry" in item for item in report["suggestions"])


def test_process_group_usage_current_process() -> None:
    if not Path("/proc/self/stat").exists():
        pytest.skip("procfs is unavailable")
    report = R.process_group_usage(os.getpgrp())
    assert report["pgid"] == os.getpgrp()
    assert report["process_count"] >= 1


def test_resources_cli_status_json(tmp_path: Path, capsys) -> None:
    if not Path("/proc/meminfo").exists():
        pytest.skip("Linux memory counters are unavailable")
    code = resources_cli.main(["status", "--root", str(tmp_path), "--json"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["host"]["memory"]["total_mb"]
    expected = sys.platform != "win32"
    assert payload["enforcement"]["available"] is expected
    assert payload["enforcement"]["mechanism"] in {"rlimit+watch", "watch"}
    assert payload["enforcement"]["cgroups"] is False


# --------------------------------------------------------------------------- #
# limits, child rlimits and the enforcement watchdog
# --------------------------------------------------------------------------- #
def test_limits_from_policy_defaults_and_overrides(tmp_path: Path) -> None:
    policy = R.load_policy(tmp_path)
    limits = R.limits_from_policy(policy)
    assert limits.memory_mb == 2048
    assert limits.pids == 256
    assert limits.disk_mb == 4096
    assert limits.wall_s is None and limits.file_mb is None
    assert limits.enabled is True
    assert limits.to_dict()["memory_mb"] == 2048

    overridden = R.limits_from_policy({
        "limits": {"job_memory_mb": 256, "job_pids": 8, "job_disk_mb": 10,
                   "job_wall_s": 30, "job_file_mb": 1}}, "job")
    assert overridden == R.ResourceLimits(memory_mb=256, pids=8, disk_mb=10,
                                          wall_s=30, file_mb=1)
    # Zero and negative values mean "no limit".
    assert R.limits_from_policy({"limits": {"job_memory_mb": 0}}).memory_mb is None
    assert not R.ResourceLimits().enabled
    with pytest.raises(ValueError):
        R.limits_from_policy({}, kind="nonsense")


def test_apply_child_limits_in_a_subprocess() -> None:
    if sys.platform == "win32":
        pytest.skip("POSIX rlimits are unavailable")
    script = """
import json, resource
from ozagia.core import resources as R
report = R.apply_child_limits(R.ResourceLimits(memory_mb=64, file_mb=1,
                                               pids=123))
out = {"report": report, "limits": {}}
for name in ("RLIMIT_AS", "RLIMIT_FSIZE", "RLIMIT_NPROC"):
    try:
        out["limits"][name] = list(resource.getrlimit(getattr(resource, name)))
    except AttributeError:
        pass
print(json.dumps(out))
"""
    env = {**os.environ, "PYTHONPATH": str(SRC), "PYTHONDONTWRITEBYTECODE": "1"}
    done = subprocess.run([sys.executable, "-c", script], env=env, text=True,
                          capture_output=True, timeout=60)
    assert done.returncode == 0, done.stderr
    payload = json.loads(done.stdout)
    limits = payload["limits"]
    assert limits["RLIMIT_AS"][0] == 64 * 1024 * 1024
    assert limits["RLIMIT_FSIZE"][0] == 1 * 1024 * 1024
    assert limits["RLIMIT_NPROC"][0] == 123
    assert set(payload["report"]["applied"]) >= {
        "RLIMIT_AS", "RLIMIT_FSIZE", "RLIMIT_NPROC"}
    assert payload["report"]["errors"] == []
    # The OOM hint is best-effort: written, or reported as unavailable.
    assert payload["report"]["oom_score_adj"] in (None, R.DEFAULT_OOM_SCORE_ADJ)


def test_apply_child_limits_never_raises_and_can_skip_nproc() -> None:
    report = R.apply_child_limits(None)
    assert report["applied"] == [] and report["errors"] == []
    report = R.apply_child_limits({"memory_mb": 0})
    assert report["applied"] == []


def _sample(*, memory=10.0, count=1, free=50.0):
    return {"pgid": 4242, "memory_mb": memory, "process_count": count}


def test_watchdog_records_peak_and_kills_only_once_over_limit() -> None:
    samples = [_sample(memory=40.0), _sample(memory=80.0),
               _sample(memory=120.0)]
    killed: list[dict] = []
    seen: list[dict] = []
    watchdog = R.ResourceWatchdog(
        R.ResourceLimits(memory_mb=100), pgid=4242,
        verify=lambda: True, killer=killed.append,
        sampler=lambda pgid: samples.pop(0),
        disk_probe=lambda: {"free_gb": 50.0}, interval_s=10,
        on_sample=seen.append)
    watchdog.sample_once()
    watchdog.sample_once()
    assert watchdog.peak_memory_mb == 80.0
    assert killed == [] and seen[-1]["peak_memory_mb"] == 80.0
    watchdog.sample_once()
    assert len(killed) == 1 and killed[0]["kind"] == "memory"
    assert killed[0]["limit_mb"] == 100 and killed[0]["observed_mb"] == 120.0
    assert watchdog.events and watchdog.events[-1]["kind"] == "memory"
    # A second over-limit sample never signals again.
    watchdog.sample_once()
    assert len(killed) == 1 and watchdog.kills == 1


def test_watchdog_disk_pids_and_wall_limits() -> None:
    killed: list[dict] = []
    disk = R.ResourceWatchdog(
        R.ResourceLimits(disk_mb=10), pgid=7, verify=lambda: True,
        killer=killed.append, sampler=lambda pgid: _sample(),
        disk_probe=lambda: {"free_gb": 0.5}, reserve_disk_gb=2.0)
    disk.sample_once()
    assert killed[-1]["kind"] == "disk" and killed[-1]["free_gb"] == 0.5

    killed.clear()
    probes = iter([{"free_gb": 50.0}, {"free_gb": 49.0}])
    budget = R.ResourceWatchdog(
        R.ResourceLimits(disk_mb=100), pgid=7, verify=lambda: True,
        killer=killed.append, sampler=lambda pgid: _sample(),
        disk_probe=lambda: next(probes))
    budget.sample_once()
    assert killed == []
    budget.sample_once()
    assert killed[-1]["kind"] == "disk"
    assert killed[-1]["budget_mb"] == 100
    assert killed[-1]["observed_mb"] == 1024.0
    assert budget.disk_used_mb == 1024.0

    killed.clear()
    pids = R.ResourceWatchdog(
        R.ResourceLimits(pids=3), pgid=7, verify=lambda: True,
        killer=killed.append, sampler=lambda pgid: _sample(count=4),
        disk_probe=lambda: {"free_gb": 50.0})
    pids.sample_once()
    assert killed[-1]["kind"] == "pids" and killed[-1]["observed"] == 4

    killed.clear()
    ticks = iter([0.0, 10.0])
    wall = R.ResourceWatchdog(
        R.ResourceLimits(wall_s=5), pgid=7, verify=lambda: True,
        killer=killed.append, sampler=lambda pgid: _sample(),
        disk_probe=lambda: {"free_gb": 50.0}, clock=lambda: next(ticks))
    wall.sample_once()
    assert killed[-1]["kind"] == "wall" and killed[-1]["limit_s"] == 5.0


def test_watchdog_never_signals_an_unverified_or_stale_group() -> None:
    killed: list[dict] = []
    watchdog = R.ResourceWatchdog(
        R.ResourceLimits(memory_mb=1), pgid=999999,
        verify=lambda: False, killer=killed.append,
        sampler=lambda pgid: _sample(memory=5000.0),
        disk_probe=lambda: {"free_gb": 50.0})
    watchdog.sample_once()
    assert killed == [] and watchdog.peak_memory_mb is None
    assert watchdog.events[-1]["kind"] == "unverified"
    assert watchdog.events[-1]["action"] == "not_signalled"

    # No verifier: an empty group (a stale pgid) is not signalled either.
    stale = R.ResourceWatchdog(
        R.ResourceLimits(memory_mb=1), pgid=999999,
        killer=killed.append, sampler=lambda pgid: _sample(memory=5000.0,
                                                           count=0),
        disk_probe=lambda: {"free_gb": 50.0})
    stale.sample_once()
    assert killed == []


def test_watchdog_runs_in_a_thread() -> None:
    killed: list[dict] = []
    watchdog = R.ResourceWatchdog(
        R.ResourceLimits(memory_mb=1), pgid=7, verify=lambda: True,
        killer=killed.append, sampler=lambda pgid: _sample(memory=50.0),
        disk_probe=lambda: {"free_gb": 50.0}, interval_s=0.01)
    watchdog.start()
    try:
        deadline = time.monotonic() + 5
        while not killed and time.monotonic() < deadline:
            time.sleep(0.01)
        assert killed and killed[0]["kind"] == "memory"
    finally:
        watchdog.stop()
    assert not watchdog.alive
