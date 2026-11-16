"""Linux cgroup v2 detection and per-job cgroup management.

Every test uses a fake cgroup filesystem in a temporary directory; no test
writes to the host's real cgroup hierarchy.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from ozagia.core import cgroup as C


def _fake_base(tmp_path: Path, *, kill: bool = True,
               controllers: str = "memory pids cpu") -> Path:
    base = tmp_path / "cgroup"
    base.mkdir()
    (base / "cgroup.controllers").write_text(controllers + "\n")
    (base / "cgroup.subtree_control").write_text("")
    if kill:
        (base / "cgroup.kill").write_text("")
    for name in ("memory.max", "memory.peak", "memory.events", "pids.max"):
        (base / name).write_text("")
    return base


def _events(path: Path, populated: bool = True) -> None:
    (path / "cgroup.events").write_text(
        f"populated {1 if populated else 0}\nfrozen 0\n")


# --------------------------------------------------------------------------- #
# detection
# --------------------------------------------------------------------------- #
def test_detect_reports_a_writable_v2_hierarchy(tmp_path: Path) -> None:
    base = _fake_base(tmp_path)
    report = C.detect(base)
    assert report["schema"] == C.SCHEMA
    assert report["version"] == "v2"
    assert report["available"] is True
    assert report["delegated"] is True
    assert report["controllers"][:2] == ["memory", "pids"]
    assert report["reason"] is None
    for name in C.FEATURES:
        assert report["features"][name] is True


def test_detect_without_v2_marker_is_unsupported(tmp_path: Path) -> None:
    report = C.detect(tmp_path / "missing")
    assert report["version"] == "none"
    assert report["available"] is False
    assert "not mounted" in report["reason"]
    # A v2 hierarchy without the memory controller cannot enforce memory.
    base = _fake_base(tmp_path, controllers="cpu pids")
    report = C.detect(base)
    assert report["available"] is False
    assert "memory" in report["reason"]


def test_detect_permission_failure_is_reported_not_raised(
        tmp_path: Path, monkeypatch) -> None:
    base = _fake_base(tmp_path)
    monkeypatch.setattr(C.os, "access", lambda *args, **kwargs: False)
    report = C.detect(base)
    assert report["version"] == "v2"
    assert report["available"] is False
    assert report["delegated"] is False
    assert "delegation" in report["reason"]


# --------------------------------------------------------------------------- #
# create, limits, attach
# --------------------------------------------------------------------------- #
def test_create_enables_controllers_sets_limits_and_attaches(
        tmp_path: Path) -> None:
    base = _fake_base(tmp_path)
    handle = C.JobCgroup(base, job_id="j-7")
    report = handle.create(memory_mb=100, pids=16)
    assert report["ok"] is True, report
    assert report["mechanism"] == C.MECHANISM
    assert report["fallback"] is False
    assert handle.path == base / "ozagia" / "job-j-7"
    assert handle.path.is_dir()
    assert (base / "cgroup.subtree_control").read_text().split() == [
        "+memory", "+pids"]
    assert (handle.group_path / "cgroup.subtree_control").read_text().split() == [
        "+memory", "+pids"]
    assert (handle.path / "memory.max").read_text().strip() == str(
        100 * 1024 * 1024)
    assert (handle.path / "pids.max").read_text().strip() == "16"
    assert report["controllers"] == ["memory", "pids"]

    attached = handle.attach(12345)
    assert attached["ok"] is True, attached
    assert "12345" in (handle.path / "cgroup.procs").read_text().splitlines()
    # Attaching twice is idempotent.
    assert handle.attach(12345)["ok"] is True


def test_create_enables_the_optional_io_controller_when_available(
        tmp_path: Path) -> None:
    base = _fake_base(tmp_path, controllers="memory pids io cpu")
    group = base / C.GROUP_NAME
    group.mkdir()
    (group / "cgroup.controllers").write_text("memory pids io\n")
    handle = C.JobCgroup(base, job_id="j-io")
    report = handle.create(memory_mb=10, pids=2)
    assert report["ok"] is True, report
    assert set(report["controllers"]) == {"memory", "pids", "io"}
    assert (base / "cgroup.subtree_control").read_text().split() == [
        "+memory", "+pids", "+io"]


def test_create_reuses_an_empty_group_and_refuses_a_populated_leftover(
        tmp_path: Path) -> None:
    base = _fake_base(tmp_path)
    first = C.JobCgroup(base, job_id="j1")
    assert first.create(memory_mb=10, pids=2)["ok"]
    assert first.cleanup()["removed"] is True

    # A new daemon can reuse the job id once the old group is empty.
    second = C.JobCgroup(base, job_id="j1")
    assert second.create(memory_mb=20, pids=3)["ok"]
    _events(second.path, populated=True)
    third = C.JobCgroup(base, job_id="j1")
    report = third.create(memory_mb=30, pids=4)
    assert report["ok"] is False
    assert report["fallback"] is True
    assert "not provably empty" in report["reason"]


def test_create_permission_failure_falls_back_without_raising(
        tmp_path: Path, monkeypatch) -> None:
    base = _fake_base(tmp_path)

    def deny(path: Path, value: str) -> tuple[bool, str | None]:
        return False, f"{path}: permission denied"

    monkeypatch.setattr(C, "_write_text", deny)
    handle, report = C.CgroupManager(base).start("j1", memory_mb=64, pids=4)
    assert handle is None
    assert report["ok"] is False
    assert report["fallback"] is True
    assert report["errors"]
    assert "cannot enable controllers" in report["reason"]


def test_manager_start_reports_unsupported_without_creating_anything(
        tmp_path: Path) -> None:
    manager = C.CgroupManager(tmp_path / "missing")
    handle, report = manager.start("j1", memory_mb=64, pids=4)
    assert handle is None
    assert report["fallback"] is True
    assert report["path"] is None
    assert "not mounted" in report["reason"]
    assert manager.available is False


# --------------------------------------------------------------------------- #
# stats and OOM
# --------------------------------------------------------------------------- #
def test_stats_maps_memory_peak_and_oom_events(tmp_path: Path) -> None:
    base = _fake_base(tmp_path)
    handle = C.JobCgroup(base, job_id="j2")
    assert handle.create(memory_mb=100, pids=16)["ok"]
    (handle.path / "memory.peak").write_text(str(100 * 1024 * 1024))
    (handle.path / "memory.current").write_text(str(50 * 1024 * 1024))
    (handle.path / "pids.current").write_text("3\n")
    (handle.path / "memory.events").write_text(
        "low 0\nhigh 0\nmax 2\noom 1\noom_kill 1\n")
    _events(handle.path, populated=True)

    stats = handle.stats()
    assert stats["memory_peak_mb"] == 100.0
    assert stats["memory_current_mb"] == 50.0
    assert stats["memory_max_mb"] == 100.0
    assert stats["peak_source"] == "memory.peak"
    assert stats["pids_current"] == 3
    assert stats["pids_max"] == 16
    assert stats["oom_events"] == 1
    assert stats["oom_kill"] == 1
    assert stats["populated"] is True
    assert stats["events"]["max"] == "2"


def test_stats_falls_back_to_memory_current_when_peak_is_absent(
        tmp_path: Path) -> None:
    base = _fake_base(tmp_path)
    handle = C.JobCgroup(base, job_id="j3")
    assert handle.create(memory_mb=10, pids=2)["ok"]
    (handle.path / "memory.current").write_text(str(5 * 1024 * 1024))
    stats = handle.stats()
    assert stats["memory_peak_mb"] == 5.0
    assert stats["peak_source"] == "memory.current"
    assert stats["oom_events"] == 0


def test_stats_parses_io_stat_across_devices(tmp_path: Path) -> None:
    base = _fake_base(tmp_path)
    handle = C.JobCgroup(base, job_id="j4")
    assert handle.create(memory_mb=10, pids=2)["ok"]
    (handle.path / "io.stat").write_text(
        "8:0 rbytes=1048576 wbytes=2097152 rios=10 wios=20 dbytes=0 dios=0\n"
        "8:16 rbytes=2097152 wbytes=1048576 rios=4 wios=8\n")
    stats = handle.stats()
    assert stats["io_read_mb"] == 3.0
    assert stats["io_write_mb"] == 3.0
    assert stats["io_source"] == "cgroup.io.stat"
    assert stats["io_device_count"] == 2
    assert stats["io_devices"] == [
        {"device": "8:0", "read_mb": 1.0, "write_mb": 2.0},
        {"device": "8:16", "read_mb": 2.0, "write_mb": 1.0},
    ]


def test_stats_missing_io_stat_is_none_and_empty_is_zero(tmp_path: Path) -> None:
    base = _fake_base(tmp_path)
    handle = C.JobCgroup(base, job_id="j5")
    assert handle.create(memory_mb=10, pids=2)["ok"]
    stats = handle.stats()
    assert stats["io_read_mb"] is None
    assert stats["io_write_mb"] is None
    assert stats["io_source"] is None
    assert stats["io_devices"] == []

    (handle.path / "io.stat").write_text("")
    stats = handle.stats()
    assert stats["io_read_mb"] == 0.0
    assert stats["io_write_mb"] == 0.0
    assert stats["io_source"] == "cgroup.io.stat"
    assert stats["io_devices"] == []


def test_stats_skips_malformed_io_stat_lines(tmp_path: Path) -> None:
    base = _fake_base(tmp_path)
    handle = C.JobCgroup(base, job_id="j6")
    assert handle.create(memory_mb=10, pids=2)["ok"]
    (handle.path / "io.stat").write_text(
        "garbage\n"
        "8:0 rbytes=bogus wbytes=1048576 rios=1 wios=2\n"
        "8:16 rbytes=2097152 wbytes=oops rios=3 wios=4\n")
    stats = handle.stats()
    assert stats["io_read_mb"] == 2.0
    assert stats["io_write_mb"] == 1.0
    assert stats["io_source"] == "cgroup.io.stat"
    assert len(stats["io_devices"]) == 2

    (handle.path / "io.stat").write_text("nonsense\n8:0 rbytes=no wbytes=no\n")
    stats = handle.stats()
    assert stats["io_read_mb"] is None
    assert stats["io_write_mb"] is None
    assert stats["io_source"] is None
    assert stats["io_devices"] == []


def test_stats_bounds_the_io_device_breakdown(tmp_path: Path) -> None:
    base = _fake_base(tmp_path)
    handle = C.JobCgroup(base, job_id="j7")
    assert handle.create(memory_mb=10, pids=2)["ok"]
    lines = [f"{index}:0 rbytes={index * 1048576} wbytes=0"
             for index in range(1, 21)]
    (handle.path / "io.stat").write_text("\n".join(lines) + "\n")
    stats = handle.stats()
    assert stats["io_device_count"] == 20
    assert len(stats["io_devices"]) == C.MAX_IO_DEVICES
    # Busiest first; the breakdown is bounded but the totals are complete.
    assert stats["io_devices"][0]["device"] == "20:0"
    assert stats["io_read_mb"] == round(sum(range(1, 21)), 4)


# --------------------------------------------------------------------------- #
# kill
# --------------------------------------------------------------------------- #
def test_kill_uses_cgroup_kill(tmp_path: Path) -> None:
    base = _fake_base(tmp_path)
    handle = C.JobCgroup(base, job_id="j4")
    assert handle.create(memory_mb=10, pids=2)["ok"]
    (handle.path / "cgroup.kill").write_text("")
    _events(handle.path, populated=True)

    report = handle.kill(term_first=False)
    assert report["ok"] is True
    assert report["method"] == "cgroup.kill"
    assert report["action"] == "cgroup.kill"
    assert (handle.path / "cgroup.kill").read_text().strip() == "1"


def test_kill_falls_back_to_sigterm_then_sigkill(tmp_path: Path) -> None:
    base = _fake_base(tmp_path)
    handle = C.JobCgroup(base, job_id="j5")
    assert handle.create(memory_mb=10, pids=2)["ok"]
    _events(handle.path, populated=True)
    signals: list[int] = []

    report = handle.kill(fallback=signals.append, grace_s=0)
    assert report["ok"] is True
    assert report["method"] == "signal"
    assert report["action"] == "sigkill"
    assert report["signals"] == ["SIGTERM", "SIGKILL"]
    assert len(signals) == 2


def test_kill_fallback_stops_after_sigterm_when_the_group_empties(
        tmp_path: Path) -> None:
    base = _fake_base(tmp_path)
    handle = C.JobCgroup(base, job_id="j6")
    assert handle.create(memory_mb=10, pids=2)["ok"]
    _events(handle.path, populated=True)
    signals: list[int] = []

    def term(sig: int) -> None:
        signals.append(sig)
        _events(handle.path, populated=False)

    report = handle.kill(fallback=term, grace_s=0.1)
    assert report["ok"] is True
    assert report["action"] == "sigterm"
    assert report["signals"] == ["SIGTERM"]
    assert len(signals) == 1


# --------------------------------------------------------------------------- #
# cleanup
# --------------------------------------------------------------------------- #
def test_cleanup_removes_an_empty_job_cgroup(tmp_path: Path) -> None:
    base = _fake_base(tmp_path)
    handle = C.JobCgroup(base, job_id="j8")
    assert handle.create(memory_mb=10, pids=2)["ok"]
    report = handle.cleanup()
    assert report["ok"] is True
    assert report["removed"] is True
    assert not handle.path.exists()
    again = handle.cleanup()
    assert again["ok"] is True and again["reason"] == "absent"


def test_cleanup_refuses_a_populated_cgroup(tmp_path: Path) -> None:
    base = _fake_base(tmp_path)
    handle = C.JobCgroup(base, job_id="j9")
    assert handle.create(memory_mb=10, pids=2)["ok"]
    _events(handle.path, populated=True)
    report = handle.cleanup()
    assert report["ok"] is False
    assert report["removed"] is False
    assert "populated" in report["reason"]
    assert handle.path.exists()


def test_remove_path_never_leaves_the_ozagia_job_tree(tmp_path: Path) -> None:
    base = _fake_base(tmp_path)
    outside = tmp_path / "elsewhere" / "job-j1"
    outside.mkdir(parents=True)
    report = C.remove_path(outside, base=base)
    assert report["ok"] is False
    assert "outside" in report["reason"]
    assert outside.exists()

    handle = C.JobCgroup(base, job_id="j10")
    assert handle.create(memory_mb=10, pids=2)["ok"]
    report = C.remove_path(handle.path, base=base)
    assert report["ok"] is True and report["removed"] is True
    assert not handle.path.exists()


def test_join_self_writes_the_calling_pid(tmp_path: Path) -> None:
    group = tmp_path / "ozagia" / "job-j1"
    group.mkdir(parents=True)
    (group / "cgroup.procs").write_text("")
    assert C.join_self(group) is True
    assert str(os.getpid()) in (group / "cgroup.procs").read_text()


def test_kill_reports_a_missing_fallback_and_missing_kill_file(
        tmp_path: Path) -> None:
    base = _fake_base(tmp_path)
    handle = C.JobCgroup(base, job_id="j11")
    assert handle.create(memory_mb=10, pids=2)["ok"]
    _events(handle.path, populated=True)
    report = handle.kill(term_first=False)
    assert report["ok"] is False
    assert report["method"] == "none"


@pytest.mark.parametrize("job_id", ["j1", "a/b", "..", ""])
def test_job_paths_are_sanitised(tmp_path: Path, job_id: str) -> None:
    handle = C.JobCgroup(_fake_base(tmp_path), job_id=job_id)
    assert handle.path.parent == handle.base / C.GROUP_NAME
    assert handle.path.name.startswith("job-")
    assert "/" not in handle.path.name
