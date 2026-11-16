"""Linux cgroup v2 enforcement for ozagia-owned jobs.

The daemon prefers a per-job cgroup: the kernel then accounts and enforces the
job's memory and process count even when the sampling watchdog misses a spike.
The layout is::

    <base>/ozagia/cgroup.subtree_control
    <base>/ozagia/job-<id>/cgroup.procs
    <base>/ozagia/job-<id>/memory.max
    <base>/ozagia/job-<id>/memory.peak
    <base>/ozagia/job-<id>/memory.events
    <base>/ozagia/job-<id>/io.stat
    <base>/ozagia/job-<id>/pids.max
    <base>/ozagia/job-<id>/cgroup.kill

``base`` defaults to ``/sys/fs/cgroup`` and is injectable so tests use a
temporary directory. Detection is read-only. Every creation, limit write,
attach, kill and removal returns a report instead of raising: cgroups are an
optimisation over rlimits plus the watchdog, never a requirement for a job.

Cgroup v2 needs delegation: the daemon's user must be able to create a child
cgroup and write the parent's ``cgroup.subtree_control``. A cgroup that cannot
be written is reported as ``unsupported``/``fallback`` and the caller keeps
rlimits and the watchdog. This module never removes or signals anything outside
the ``ozagia/job-*`` subtree it created.
"""
from __future__ import annotations

import os
import re
import signal
import sys
import time
from pathlib import Path
from typing import Any, Callable

SCHEMA = 1
CGROUP_BASE = Path("/sys/fs/cgroup")
GROUP_NAME = "ozagia"
# The mechanism string recorded on a job that uses both the cgroup and rlimits.
MECHANISM = "cgroup+rlimit"
CONTROLLERS = ("memory", "pids")
# Disk write accounting needs the io controller; enable it when the parent
# exposes it, but never make cgroup enforcement depend on it.
OPTIONAL_CONTROLLERS = ("io",)
FEATURES = (
    "cgroup.controllers",
    "cgroup.subtree_control",
    "cgroup.kill",
    "memory.max",
    "memory.peak",
    "memory.events",
    "pids.max",
)
MAX_NAME_CHARS = 80
# io.stat can list many devices; the job record keeps only the busiest few.
MAX_IO_DEVICES = 16
_ID_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")
_JOB_PREFIX = "job-"
# Regular files a test fixture may hold; a real cgroup directory carries
# cgroup.events and cgroup.subtree_control and is never unlinked this way.
_FAKE_FILES = frozenset({
    "cgroup.procs", "cgroup.kill", "cgroup.events",
    "cgroup.subtree_control", "memory.current", "memory.max", "memory.peak",
    "memory.events", "pids.current", "pids.max", "io.stat",
})


def _safe_name(value: Any) -> str:
    name = _ID_SAFE.sub("-", str(value)).strip(".-")
    return (name or "job")[:MAX_NAME_CHARS]


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="ascii", errors="replace")
    except OSError:
        return None


def _write_text(path: Path, value: str) -> tuple[bool, str | None]:
    """Write one cgroup control value; never raise."""
    try:
        with path.open("w", encoding="ascii") as handle:
            handle.write(value if value.endswith("\n") else value + "\n")
        return True, None
    except OSError as exc:
        return False, f"{path}: {exc}"


def _read_list(path: Path) -> list[str]:
    text = _read_text(path)
    # A real cgroup.subtree_control read returns bare controller names; a
    # fake filesystem keeps the written "+memory +pids" text, so strip the
    # prefix for both.
    return [token.lstrip("+") for token in text.split()] if text else []


def _read_values(path: Path) -> dict[str, str]:
    text = _read_text(path)
    values: dict[str, str] = {}
    if not text:
        return values
    for line in text.splitlines():
        key, _, value = line.partition(" ")
        if key:
            values[key.strip()] = value.strip()
    return values


def _bytes_mb(raw: Any) -> float | None:
    if raw is None:
        return None
    try:
        return round(int(raw) / (1024 * 1024), 4)
    except (TypeError, ValueError):
        return None


def _io_stats(path: Path) -> dict[str, Any]:
    """Parse cgroup v2 ``io.stat`` into read/write megabytes.

    Each line is ``<major>:<minor> rbytes=N wbytes=N rios=N wios=N ...``.
    Values are summed across devices; malformed keys or lines are skipped.
    A missing file means the io controller is not available and every value is
    ``None``. An empty file is a real cgroup with no I/O yet and reports
    ``0.0``. ``io_devices`` keeps the busiest ``MAX_IO_DEVICES`` devices only.
    """
    text = _read_text(path)
    if text is None:
        return {"io_read_mb": None, "io_write_mb": None,
                "io_source": None, "io_devices": [], "io_device_count": 0}
    devices: list[dict[str, Any]] = []
    read_total = 0
    write_total = 0
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        values: dict[str, int] = {}
        for item in parts[1:]:
            key, _, raw = item.partition("=")
            if key not in {"rbytes", "wbytes"}:
                continue
            try:
                values[key] = int(raw)
            except ValueError:
                continue
        read = values.get("rbytes")
        write = values.get("wbytes")
        if read is None and write is None:
            continue
        read_total += read or 0
        write_total += write or 0
        devices.append({
            "device": parts[0],
            "read_mb": _bytes_mb(read),
            "write_mb": _bytes_mb(write),
            "_bytes": (read or 0) + (write or 0),
        })
    if not devices:
        if text.strip():
            # Non-empty io.stat with no parseable device line: report no
            # attribution rather than a false zero.
            return {"io_read_mb": None, "io_write_mb": None,
                    "io_source": None, "io_devices": [],
                    "io_device_count": 0}
        return {"io_read_mb": 0.0, "io_write_mb": 0.0,
                "io_source": "cgroup.io.stat", "io_devices": [],
                "io_device_count": 0}
    devices.sort(key=lambda item: (-item["_bytes"], item["device"]))
    bounded = [{key: item[key] for key in ("device", "read_mb", "write_mb")}
               for item in devices[:MAX_IO_DEVICES]]
    return {
        "io_read_mb": round(read_total / (1024 * 1024), 4),
        "io_write_mb": round(write_total / (1024 * 1024), 4),
        "io_source": "cgroup.io.stat",
        "io_devices": bounded,
        "io_device_count": len(devices),
    }


def _mb(raw: Any) -> float | None:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text or text == "max":
        return None
    try:
        return round(int(text) / (1024 * 1024), 2)
    except (TypeError, ValueError):
        return None


def _int(raw: Any) -> int | None:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text or text == "max":
        return None
    try:
        return int(text)
    except (TypeError, ValueError):
        return None


def _rmdir_cgroup(path: Path) -> tuple[bool, str | None]:
    """Remove an empty cgroup; clean a test fixture's regular files first."""
    try:
        path.rmdir()
        return True, None
    except OSError as exc:
        if (path / "cgroup.events").exists() or (
                path / "cgroup.subtree_control").exists():
            return False, str(exc)
        try:
            entries = list(path.iterdir())
        except OSError as list_exc:
            return False, str(list_exc)
        if any(entry.name not in _FAKE_FILES for entry in entries):
            return False, str(exc)
        for entry in entries:
            try:
                entry.unlink()
            except OSError as unlink_exc:
                return False, str(unlink_exc)
        try:
            path.rmdir()
            return True, None
        except OSError as final_exc:
            return False, str(final_exc)


def _enable_controllers(parent: Path, controllers: tuple[str, ...],
                        errors: list[str]) -> bool:
    control = parent / "cgroup.subtree_control"
    current = _read_list(control)
    missing = [name for name in controllers if name not in current]
    if not missing:
        return True
    wanted = list(dict.fromkeys([*current, *missing]))
    ok, error = _write_text(control, " ".join(f"+{name}" for name in wanted))
    if not ok:
        errors.append(str(error))
        return False
    now = _read_list(control)
    absent = [name for name in controllers if name not in now]
    if absent:
        errors.append(f"{control}: not enabled after write: {', '.join(absent)}")
        return False
    return True


def _enable_optional(parent: Path, name: str) -> bool:
    """Enable one optional controller when the parent exposes it.

    Failure is not an error: memory/pids limits stay active and disk
    attribution falls back to ``/proc/<pid>/io``.
    """
    if name not in _read_list(parent / "cgroup.controllers"):
        return False
    return _enable_controllers(parent, (name,), [])


def detect(base: str | Path | None = None) -> dict[str, Any]:
    """Read-only cgroup v2 capability report for ``base``.

    ``available`` means memory and pids are listed in ``cgroup.controllers``
    and the daemon can create a child and write ``cgroup.subtree_control``.
    Missing delegation is reported in ``reason``; it is not an error.
    """
    path = Path(base) if base is not None else CGROUP_BASE
    report: dict[str, Any] = {
        "schema": SCHEMA,
        "platform": sys.platform,
        "base": str(path),
        "version": "none",
        "available": False,
        "delegated": False,
        "status": "unsupported",
        "controllers": [],
        "subtree_control": [],
        "features": {name: (path / name).is_file() for name in FEATURES},
        "reason": None,
    }
    if not sys.platform.startswith("linux"):
        report["reason"] = "cgroup v2 is Linux-only"
        return report
    marker = path / "cgroup.controllers"
    if not marker.is_file():
        report["reason"] = f"cgroup v2 is not mounted at {path}"
        return report
    report["version"] = "v2"
    controllers = _read_list(marker)
    report["controllers"] = controllers
    report["subtree_control"] = _read_list(path / "cgroup.subtree_control")
    absent = [name for name in CONTROLLERS if name not in controllers]
    if absent:
        report["reason"] = ("controllers unavailable in this cgroup: "
                            + ", ".join(absent))
        return report
    if not os.access(path, os.W_OK):
        report["reason"] = (f"{path} is not writable; cgroup v2 delegation "
                            "is required")
        return report
    control = path / "cgroup.subtree_control"
    if not control.is_file() or not os.access(control, os.W_OK):
        report["reason"] = ("cgroup.subtree_control is not writable; cgroup v2 "
                            "delegation is required")
        return report
    report["delegated"] = True
    report["available"] = True
    report["status"] = "supported"
    return report


class JobCgroup:
    """One ``ozagia/job-<id>`` cgroup. Every method reports, never raises."""

    def __init__(self, base: str | Path | None = None, *, job_id: Any,
                 group: str = GROUP_NAME) -> None:
        self.base = Path(base) if base is not None else CGROUP_BASE
        self.group = _safe_name(group)
        self.job_id = _safe_name(job_id)
        self.group_path = self.base / self.group
        self.path = self.group_path / f"{_JOB_PREFIX}{self.job_id}"
        self.created = False
        self._group_created = False
        self.report: dict[str, Any] = {}

    def create(self, *, memory_mb: float | int | None = None,
               pids: int | None = None) -> dict[str, Any]:
        """Create the group, enable ``+memory +pids`` (and ``+io`` when the
        parent exposes it) and set the limits."""
        report: dict[str, Any] = {
            "schema": SCHEMA, "mechanism": MECHANISM, "ok": False,
            "fallback": False, "created": False, "status": "fallback",
            "path": str(self.path), "group": str(self.group_path),
            "limits": {"memory_mb": memory_mb, "pids": pids},
            "controllers": [], "errors": [], "reason": None,
        }
        try:
            if not self.base.is_dir():
                raise NotADirectoryError(f"{self.base} is not a directory")
            if self.path.exists():
                if self.is_populated() is not False:
                    report["reason"] = (
                        f"a leftover cgroup exists at {self.path} and is not "
                        "provably empty")
                    return report
                removed, error = _rmdir_cgroup(self.path)
                if not removed:
                    report["reason"] = (
                        f"cannot remove stale cgroup: {error}")
                    return report
            self._group_created = not self.group_path.exists()
            self.group_path.mkdir(parents=True, exist_ok=True)
            errors: list[str] = report["errors"]
            if not _enable_controllers(self.base, CONTROLLERS, errors):
                report["reason"] = "cannot enable controllers in the parent cgroup"
                return report
            for name in OPTIONAL_CONTROLLERS:
                _enable_optional(self.base, name)
            self.path.mkdir()
            procs = self.path / "cgroup.procs"
            if not procs.exists():           # a real cgroup has this already
                try:
                    procs.touch()
                except OSError:
                    pass
            if not _enable_controllers(self.group_path, CONTROLLERS, errors):
                report["reason"] = (
                    f"cannot enable controllers in {self.group_path}")
                return report
            for name in OPTIONAL_CONTROLLERS:
                _enable_optional(self.group_path, name)
            report["controllers"] = _read_list(
                self.group_path / "cgroup.subtree_control")
            memory = None if memory_mb is None else float(memory_mb)
            if memory is not None and memory > 0:
                ok, error = _write_text(
                    self.path / "memory.max", str(int(memory * 1024 * 1024)))
                if not ok:
                    errors.append(str(error))
                    report["reason"] = "cannot set memory.max"
                    return report
            count = None if pids is None else int(pids)
            if count is not None and count > 0:
                ok, error = _write_text(self.path / "pids.max", str(count))
                if not ok:
                    errors.append(str(error))
                    report["reason"] = "cannot set pids.max"
                    return report
            self.created = True
            report.update(ok=True, created=True, status="active",
                          path=str(self.path))
            return report
        except (OSError, TypeError, ValueError) as exc:
            report["reason"] = f"{type(exc).__name__}: {exc}"
            return report
        finally:
            if not report["ok"]:
                report["fallback"] = True
                self._remove_if_empty()
            self.report = report

    def _remove_if_empty(self) -> None:
        if not self.path.exists() or self.is_populated() is not False:
            return
        ok, _error = _rmdir_cgroup(self.path)
        if ok and self._group_created:
            try:
                self.group_path.rmdir()
                self._group_created = False
            except OSError:
                pass

    def _procs(self) -> list[int]:
        text = _read_text(self.path / "cgroup.procs") or ""
        out: list[int] = []
        for line in text.splitlines():
            try:
                out.append(int(line.strip()))
            except ValueError:
                continue
        return out

    def is_populated(self) -> bool | None:
        """Whether processes remain; ``None`` when the cgroup is absent."""
        if not self.path.exists():
            return None
        values = _read_values(self.path / "cgroup.events")
        if "populated" in values:
            return values["populated"] == "1"
        text = _read_text(self.path / "cgroup.procs")
        if text is None:
            # A cgroup always has cgroup.procs. A directory without it is a
            # test fixture or a partial state; treat it as empty only because
            # every removal path also checks the ozagia/job-* location.
            return False
        return bool(text.strip())

    def attach(self, pid: int) -> dict[str, Any]:
        """Put ``pid`` in the cgroup, verifying the membership when possible."""
        report: dict[str, Any] = {"ok": False, "pid": int(pid),
                                  "path": str(self.path), "error": None}
        if int(pid) in self._procs():
            report["ok"] = True
            return report
        ok, error = _write_text(self.path / "cgroup.procs", str(int(pid)))
        if not ok:
            report["error"] = error
            return report
        members = self._procs()
        if int(pid) in members:
            report["ok"] = True
        elif members:
            report["error"] = (f"cgroup.procs lists {members[:5]} but not "
                               f"{int(pid)}")
        else:
            # A very short job can exit before the read; the accepted write is
            # the evidence that the kernel attached it.
            report["ok"] = True
            report["note"] = "cgroup.procs was empty after the write"
        return report

    def stats(self) -> dict[str, Any]:
        """Peak/current memory, pids, ``memory.events`` and ``io.stat``.

        ``io_read_mb``/``io_write_mb``/``io_source`` are ``None`` when the
        cgroup has no readable ``io.stat``. They are cumulative bytes sent to
        storage for the group, not retained blocks, and are exact for the
        group as the kernel accounts it.
        """
        events = _read_values(self.path / "memory.events")
        peak = _mb(_read_text(self.path / "memory.peak"))
        current = _mb(_read_text(self.path / "memory.current"))
        source = "memory.peak"
        if peak is None:
            peak = current
            source = "memory.current" if current is not None else None
        return {
            "path": str(self.path),
            "populated": self.is_populated(),
            "memory_current_mb": current,
            "memory_peak_mb": peak,
            "memory_max_mb": _mb(_read_text(self.path / "memory.max")),
            "peak_source": source,
            "pids_current": _int(_read_text(self.path / "pids.current")),
            "pids_max": _int(_read_text(self.path / "pids.max")),
            "oom_events": _int(events.get("oom", 0)) if events else 0,
            "oom_kill": _int(events.get("oom_kill", 0)) if events else 0,
            **_io_stats(self.path / "io.stat"),
            "events": events or None,
        }

    def kill(self, *, fallback: Callable[[int], Any] | None = None,
             grace_s: float = 0.0, term_first: bool = True) -> dict[str, Any]:
        """Stop the cgroup with ``cgroup.kill``, or SIGTERM/SIGKILL fallback.

        ``fallback`` receives a ``signal`` constant and is expected to signal
        only the caller's verified process group. ``term_first`` gives the
        group the configured grace period before the kernel-wide kill.
        """
        report: dict[str, Any] = {
            "ok": False, "method": "none", "action": "none",
            "path": str(self.path), "signals": [], "error": None,
        }
        if not self.path.exists():
            report["error"] = "cgroup is gone"
            return report
        if term_first and fallback is not None:
            try:
                fallback(signal.SIGTERM)
                report["signals"].append("SIGTERM")
            except Exception as exc:  # noqa: BLE001 - fallback is best-effort
                report["error"] = f"fallback SIGTERM: {exc}"
            deadline = time.monotonic() + max(0.0, float(grace_s))
            while (self.is_populated() is not False
                   and time.monotonic() < deadline):
                time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
        if self.is_populated() is False:
            report.update(ok=True, method="signal",
                          action="sigterm" if report["signals"] else "empty")
            return report
        kill_file = self.path / "cgroup.kill"
        if kill_file.is_file():
            ok, error = _write_text(kill_file, "1")
            if ok:
                report.update(ok=True, method="cgroup.kill",
                              action="cgroup.kill")
                return report
            report["error"] = error
        if fallback is not None:
            try:
                fallback(signal.SIGKILL)
                report["signals"].append("SIGKILL")
                report.update(ok=True, method="signal", action="sigkill")
                return report
            except Exception as exc:  # noqa: BLE001 - fallback is best-effort
                report["error"] = f"fallback SIGKILL: {exc}"
        return report

    def cleanup(self) -> dict[str, Any]:
        """Remove the per-job cgroup when it is provably empty."""
        report: dict[str, Any] = {"ok": False, "removed": False,
                                  "path": str(self.path), "reason": None}
        if not self.path.exists():
            report.update(ok=True, reason="absent")
            self.created = False
            return report
        populated = self.is_populated()
        if populated is not False:
            report["reason"] = ("cgroup is still populated" if populated
                                else "cannot verify the cgroup is empty")
            return report
        try:
            ok, error = _rmdir_cgroup(self.path)
        except OSError as exc:  # pragma: no cover - _rmdir_cgroup reports
            ok, error = False, str(exc)
        if not ok:
            report["reason"] = error
            return report
        self.created = False
        report.update(ok=True, removed=True)
        if self._group_created:
            try:
                self.group_path.rmdir()
                self._group_created = False
            except OSError:
                pass
        return report


class CgroupManager:
    """Detect once, then create per-job cgroups on demand."""

    def __init__(self, base: str | Path | None = None,
                 group: str = GROUP_NAME) -> None:
        self.base = Path(base) if base is not None else CGROUP_BASE
        self.group = _safe_name(group)
        self._capability: dict[str, Any] | None = None

    def detect(self, *, refresh: bool = False) -> dict[str, Any]:
        if self._capability is None or refresh:
            self._capability = detect(self.base)
        return dict(self._capability)

    @property
    def available(self) -> bool:
        return bool(self.detect().get("available"))

    def start(self, job_id: Any, *, memory_mb: float | int | None = None,
              pids: int | None = None) -> tuple[JobCgroup | None, dict[str, Any]]:
        """Return ``(handle, report)``; ``handle`` is None on fallback."""
        capability = self.detect()
        limits = {"memory_mb": memory_mb, "pids": pids}
        if not capability.get("available"):
            return None, {
                "schema": SCHEMA, "mechanism": MECHANISM, "ok": False,
                "fallback": True, "created": False, "status": "unsupported",
                "path": None,
                "group": str(self.base / self.group), "limits": limits,
                "controllers": capability.get("controllers") or [],
                "errors": [], "reason": capability.get("reason"),
                "capability": capability,
            }
        handle = JobCgroup(self.base, job_id=job_id, group=self.group)
        report = handle.create(memory_mb=memory_mb, pids=pids)
        if report.get("ok"):
            report["capability"] = capability
            return handle, report
        report["fallback"] = True
        report["capability"] = capability
        return None, report


def join_self(path: str | Path) -> bool:
    """Put the calling process in ``path`` (used from a child ``preexec_fn``)."""
    try:
        with open(Path(path) / "cgroup.procs", "w", encoding="ascii") as handle:
            handle.write(str(os.getpid()))
        return True
    except OSError:
        return False


def remove_path(path: str | Path, *, base: str | Path | None = None,
                group: str = GROUP_NAME) -> dict[str, Any]:
    """Remove one recorded ``ozagia/job-*`` cgroup, never anything else."""
    base_path = Path(base) if base is not None else CGROUP_BASE
    target = Path(path)
    report: dict[str, Any] = {"ok": False, "removed": False,
                              "path": str(target), "reason": None}
    try:
        root = base_path.resolve()
        resolved = target.resolve()
    except OSError as exc:
        report["reason"] = str(exc)
        return report
    expected = root / _safe_name(group)
    if (resolved.parent != expected
            or not resolved.name.startswith(_JOB_PREFIX)):
        report["reason"] = ("refusing to remove a path outside the ozagia "
                            "job cgroup tree")
        return report
    if not resolved.exists():
        report.update(ok=True, reason="absent")
        return report
    values = _read_values(resolved / "cgroup.events")
    if values.get("populated") == "1":
        report["reason"] = "cgroup is still populated"
        return report
    procs = _read_text(resolved / "cgroup.procs")
    if procs and procs.strip():
        report["reason"] = "cgroup still lists processes"
        return report
    try:
        ok, error = _rmdir_cgroup(resolved)
    except OSError as exc:  # pragma: no cover - _rmdir_cgroup reports
        ok, error = False, str(exc)
    if not ok:
        report["reason"] = error
        return report
    report.update(ok=True, removed=True)
    return report


__all__ = [
    "CGROUP_BASE", "CONTROLLERS", "CgroupManager", "FEATURES", "GROUP_NAME",
    "JobCgroup", "MECHANISM", "SCHEMA", "detect", "join_self", "remove_path",
]
