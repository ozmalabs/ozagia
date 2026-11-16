"""Host resource observation, history and read-only admission checks."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from ozagia.core import resources as R
from ozagia import resources_cli


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
    assert payload["enforcement"]["available"] is False
