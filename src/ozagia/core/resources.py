"""Host resource observation, admission checks and first-layer enforcement.

The module reports what the host and ozagia-owned process groups are using,
keeps a bounded history of observed runs, and answers whether a requested
job/task/worktree is likely to fit. It also carries the first enforcement
layer: child rlimits (POSIX ``resource``), a watchdog that samples a verified
process group and the filesystem, and the child ``oom_score_adj`` hint.

The watchdog never signals a process itself. Callers inject a killer, and a
watchdog only calls it while its injected verifier says the process group is
still the caller's. cgroups are out of scope for this layer.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

import tomllib

from ozagia.config import state_path

try:  # POSIX only; never fail the import on Windows.
    import resource as _resource
except ImportError:  # pragma: no cover - exercised on non-POSIX hosts
    _resource = None

SCHEMA = 1
HISTORY_DIR = "resources"
HISTORY_FILE = "history.jsonl"
MAX_HISTORY_RECORDS = 5000
MAX_HISTORY_BYTES = 4 * 1024 * 1024
PROC_ROOT = Path("/proc")
try:
    PAGE_SIZE = int(os.sysconf("SC_PAGE_SIZE"))
except (AttributeError, OSError, ValueError):
    PAGE_SIZE = 4096

DEFAULT_POLICY: dict[str, Any] = {
    "enabled": True,
    "sample_s": 2.0,
    "watchdog": "auto",
    "reserve_memory_pct": 10.0,
    "reserve_disk_pct": 5.0,
    "reserve_disk_gb": 1.0,
    "history": True,
    "limits": {
        "job_memory_mb": 2048,
        "job_disk_mb": 4096,
        "job_pids": 256,
    },
    "admission": {
        "min_free_memory_pct": 15.0,
        "min_free_disk_pct": 10.0,
        "min_free_disk_gb": 2.0,
        "wait_max_s": 300,
    },
    "worktrees": {
        "root": "../ozagia-wt",
        "min_free_gb": 5.0,
        "avoid_tmpfs": True,
    },
}
TMPFS_TYPES = frozenset({"tmpfs", "ramfs"})
# The first enforcement layer: rlimits in the child plus a sampling watchdog.
# cgroups (memory.max/pids.max) are a later layer.
ENFORCEMENT_MECHANISM = "rlimit+watch"
# Best-effort: if the kernel OOM killer runs anyway, prefer ozagia's children
# over the user's shell or desktop. Raising oom_score_adj needs no privilege.
DEFAULT_OOM_SCORE_ADJ = 500
_RESOURCE_LIMIT_FIELDS = ("memory_mb", "pids", "disk_mb", "wall_s", "file_mb")
_MAX_RESOURCE_EVENTS = 50


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _clean_number(value: Any, *, name: str, low: float = 0.0,
                  high: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number")
    number = float(value)
    if number < low or (high is not None and number > high):
        bound = f"between {low} and {high}" if high is not None else f">= {low}"
        raise ValueError(f"{name} must be {bound}")
    return number


def _deep_merge(base: Mapping[str, Any], update: Mapping[str, Any]) -> dict[str, Any]:
    out = {key: (dict(value) if isinstance(value, Mapping) else value)
           for key, value in base.items()}
    for key, value in update.items():
        if isinstance(value, Mapping) and isinstance(out.get(key), Mapping):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _memory_snapshot() -> dict[str, Any]:
    info: dict[str, int] = {}
    try:
        text = Path("/proc/meminfo").read_text(encoding="ascii")
    except OSError:
        text = ""
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        parts = rest.split()
        if parts:
            try:
                info[key.strip()] = int(parts[0])
            except ValueError:
                pass
    if info:
        total_kb = info.get("MemTotal")
        available_kb = info.get("MemAvailable")
        if available_kb is None:
            available_kb = (info.get("MemFree", 0) + info.get("Buffers", 0)
                            + info.get("Cached", 0) + info.get("SReclaimable", 0))
        swap_total = info.get("SwapTotal")
        swap_free = info.get("SwapFree")
        result = {
            "source": "/proc/meminfo",
            "total_mb": round(total_kb / 1024, 1) if total_kb else None,
            "available_mb": round(available_kb / 1024, 1)
            if total_kb else None,
            "used_pct": round((1 - available_kb / total_kb) * 100, 1)
            if total_kb else None,
            "swap_total_mb": round(swap_total / 1024, 1)
            if swap_total is not None else None,
            "swap_free_mb": round(swap_free / 1024, 1)
            if swap_free is not None else None,
        }
        return result

    try:
        import psutil  # type: ignore[import-not-found]
    except Exception:  # noqa: BLE001 - optional
        psutil = None
    if psutil is not None:
        try:
            vm = psutil.virtual_memory()
            swap = psutil.swap_memory()
            return {
                "source": "psutil",
                "total_mb": round(vm.total / (1024 * 1024), 1),
                "available_mb": round(vm.available / (1024 * 1024), 1),
                "used_pct": round(vm.percent, 1),
                "swap_total_mb": round(swap.total / (1024 * 1024), 1),
                "swap_free_mb": round(swap.free / (1024 * 1024), 1),
            }
        except Exception:  # noqa: BLE001 - optional
            pass

    total = None
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
        total = round(pages * page_size / (1024 * 1024), 1)
    except (AttributeError, OSError, ValueError):
        total = None
    return {
        "source": "sysconf" if total else "unknown",
        "total_mb": total,
        "available_mb": None,
        "used_pct": None,
        "swap_total_mb": None,
        "swap_free_mb": None,
    }


def _load_snapshot() -> dict[str, Any]:
    try:
        one, five, fifteen = os.getloadavg()
        return {"1m": round(one, 2), "5m": round(five, 2),
                "15m": round(fifteen, 2)}
    except (AttributeError, OSError):
        return {"1m": None, "5m": None, "15m": None}


def _pressure_one(name: str) -> dict[str, Any] | None:
    try:
        text = Path(f"/proc/pressure/{name}").read_text(encoding="ascii")
    except OSError:
        return None
    result: dict[str, Any] = {}
    for line in text.splitlines():
        parts = line.split()
        if not parts:
            continue
        kind = parts[0]
        values: dict[str, float] = {}
        for item in parts[1:]:
            key, _, raw = item.partition("=")
            try:
                values[key] = float(raw)
            except ValueError:
                continue
        result[kind] = values
    return result or None


def _unescape_mount(value: str) -> str:
    return (value.replace("\\040", " ").replace("\\011", "\t")
            .replace("\\012", "\n").replace("\\134", "\\"))


def _mounts() -> list[tuple[str, str]]:
    mounts: list[tuple[str, str]] = []
    try:
        text = Path("/proc/self/mounts").read_text(encoding="ascii")
    except OSError:
        return mounts
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 3:
            mounts.append((_unescape_mount(parts[1]), parts[2]))
    mounts.sort(key=lambda item: len(item[0]), reverse=True)
    return mounts


def _mount_for(path: Path) -> tuple[str | None, str | None]:
    real = os.path.realpath(path)
    for mount, fstype in _mounts():
        if real == mount or real.startswith(mount.rstrip(os.sep) + os.sep):
            return mount, fstype
    return None, None


def _gb(value: float | int | None) -> float | None:
    if value is None:
        return None
    return round(float(value) / (1024 ** 3), 2)


def disk_usage(path: str | Path) -> dict[str, Any]:
    """Filesystem facts for ``path`` without writing to it."""
    requested = Path(path).expanduser()
    probe = requested
    try:
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        exists = requested.exists()
    except OSError:
        probe = requested
        exists = False

    result: dict[str, Any] = {
        "path": str(requested),
        "exists": exists,
        "probe": str(probe),
        "mount": None,
        "fstype": None,
        "total_gb": None,
        "free_gb": None,
        "free_pct": None,
        "inodes_free": None,
        "is_tmpfs": False,
        "source": "unknown",
        "error": None,
    }
    try:
        vfs = os.statvfs(probe)
        total = vfs.f_blocks * vfs.f_frsize
        free = vfs.f_bavail * vfs.f_frsize
        result.update(
            total_gb=_gb(total),
            free_gb=_gb(free),
            free_pct=round(free / total * 100, 1) if total else None,
            inodes_free=vfs.f_favail,
            source="statvfs",
        )
    except (AttributeError, OSError):
        try:
            usage = shutil.disk_usage(probe)
            result.update(
                total_gb=_gb(usage.total),
                free_gb=_gb(usage.free),
                free_pct=round(usage.free / usage.total * 100, 1)
                if usage.total else None,
                source="shutil",
            )
        except OSError as exc:
            result["error"] = str(exc)
    mount, fstype = _mount_for(probe)
    result["mount"] = mount
    result["fstype"] = fstype
    result["is_tmpfs"] = bool(fstype and fstype in TMPFS_TYPES)
    return result


def host_snapshot() -> dict[str, Any]:
    """A cheap host snapshot; unknown values are ``None``, never guesses."""
    return {
        "schema": SCHEMA,
        "checked_at": _now(),
        "platform": sys.platform,
        "cpu_count": os.cpu_count(),
        "load": _load_snapshot(),
        "memory": _memory_snapshot(),
        "pressure": {
            name: _pressure_one(name)
            for name in ("cpu", "memory", "io")
        },
    }


def _proc_stat(pid: int) -> list[str] | None:
    try:
        raw = (PROC_ROOT / str(pid) / "stat").read_text(encoding="ascii",
                                                       errors="replace")
    except OSError:
        return None
    left, right = raw.find("("), raw.rfind(")")
    if left < 0 or right < 0:
        return None
    return [raw[:left].strip(), raw[left + 1:right], *raw[right + 2:].split()]


def _proc_rss_mb(pid: int) -> float | None:
    try:
        fields = (PROC_ROOT / str(pid) / "statm").read_text(
            encoding="ascii").split()
        resident = int(fields[1])
        return round(resident * PAGE_SIZE / (1024 * 1024), 2)
    except (OSError, ValueError, IndexError):
        return None


def _proc_command(pid: int) -> str:
    try:
        raw = (PROC_ROOT / str(pid) / "cmdline").read_bytes()
    except OSError:
        return ""
    text = raw.replace(b"\x00", b" ").decode("utf-8", errors="replace").strip()
    return text[:200]


def _command_head(value: str) -> str:
    """Only the executable name: process arguments may contain secrets."""
    head = value.split(" ", 1)[0] if value else ""
    return Path(head).name[:80]


def process_usage(pid: int) -> dict[str, Any] | None:
    """Memory and identity for one process, or ``None`` when it is gone."""
    stat = _proc_stat(pid)
    if stat is None or len(stat) < 22:
        return None
    try:
        state = stat[2]
        ppid = int(stat[3])
        pgid = int(stat[4])
    except (ValueError, IndexError):
        return None
    return {
        "pid": pid,
        "ppid": ppid,
        "pgid": pgid,
        "state": state,
        "memory_mb": _proc_rss_mb(pid),
        "command": _command_head(_proc_command(pid)),
    }


def _pids() -> list[int]:
    try:
        entries = list(os.scandir(PROC_ROOT))
    except OSError:
        return []
    out: list[int] = []
    for entry in entries:
        if entry.name.isdigit():
            out.append(int(entry.name))
    return out


def process_group_usage(pgid: int) -> dict[str, Any]:
    """Aggregate RSS for the live processes in one process group."""
    members: list[dict[str, Any]] = []
    total = 0.0
    for pid in _pids():
        item = process_usage(pid)
        if item is None or item.get("pgid") != pgid:
            continue
        memory = item.get("memory_mb")
        if isinstance(memory, (int, float)):
            total += float(memory)
        members.append(item)
    command = next((m["command"] for m in members if m.get("command")), "")
    return {
        "pgid": pgid,
        "process_count": len(members),
        "memory_mb": round(total, 2),
        "command": command,
        "processes": sorted(members, key=lambda m: m["pid"])[:100],
    }


@dataclass(frozen=True)
class ResourceLimits:
    """Limits for one child run; ``None`` means "not limited".

    ``memory_mb`` is enforced with ``RLIMIT_AS`` and the watchdog, ``pids``
    with the watchdog (and optionally ``RLIMIT_NPROC``, which counts every
    process of the real UID), ``file_mb`` with ``RLIMIT_FSIZE``, and ``wall_s``
    with the watchdog. ``disk_mb`` is an admission/expected-use budget and a
    best-effort watchdog delta (a drop in filesystem free space since the
    first sample); exact per-job disk use cannot be attributed.
    """

    memory_mb: float | None = None
    pids: int | None = None
    disk_mb: float | None = None
    wall_s: float | None = None
    file_mb: float | None = None

    def __post_init__(self) -> None:
        for name in ("memory_mb", "disk_mb", "wall_s", "file_mb"):
            value = getattr(self, name)
            if value is None:
                continue
            try:
                number = _clean_number(value, name=name, low=0)
            except ValueError as exc:
                raise ValueError(f"limit {name}: {exc}") from None
            object.__setattr__(self, name, number if number > 0 else None)
        if self.pids is not None:
            try:
                pids = int(_clean_number(self.pids, name="pids", low=0))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"limit pids: {exc}") from None
            object.__setattr__(self, "pids", pids if pids > 0 else None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "memory_mb": self.memory_mb,
            "pids": self.pids,
            "disk_mb": self.disk_mb,
            "wall_s": self.wall_s,
            "file_mb": self.file_mb,
        }

    @property
    def enabled(self) -> bool:
        return any(getattr(self, name) is not None
                   for name in _RESOURCE_LIMIT_FIELDS)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ResourceLimits":
        if not isinstance(value, Mapping):
            raise ValueError("limits must be a mapping")
        return cls(**{name: value.get(name) for name in _RESOURCE_LIMIT_FIELDS
                      if name in value})


def limits_from_policy(policy: Mapping[str, Any],
                       kind: str = "job") -> ResourceLimits:
    """Limits for ``kind`` from ``[resources.limits]``.

    Keys are ``<kind>_memory_mb``, ``<kind>_pids``, ``<kind>_disk_mb``,
    ``<kind>_wall_s`` and ``<kind>_file_mb``; a bare field name is accepted as
    a fallback. Zero or a negative value means "no limit".
    """
    if kind not in {"job", "task", "worktree"}:
        raise ValueError("kind must be job, task or worktree")
    if not isinstance(policy, Mapping):
        raise ValueError("policy must be a mapping")
    limits = policy.get("limits")
    if not isinstance(limits, Mapping):
        limits = {}
    values: dict[str, Any] = {}
    for name in _RESOURCE_LIMIT_FIELDS:
        raw = limits.get(f"{kind}_{name}")
        if raw is None:
            raw = limits.get(name)
        if raw is not None:
            values[name] = raw
    return ResourceLimits(**values)


def _rlimit_value(report: dict[str, Any], name: str, value: int) -> None:
    """Set one rlimit, recording unsupported/failed limits instead of raising."""
    if _resource is None:
        report["unsupported"].append(name)
        return
    try:
        which = getattr(_resource, name)
    except AttributeError:
        report["unsupported"].append(name)
        return
    try:
        _soft, hard = _resource.getrlimit(which)
        desired = int(value)
        if hard != _resource.RLIM_INFINITY and desired > hard:
            desired = int(hard)
        _resource.setrlimit(which, (desired, desired))
        report["applied"].append(name)
        report["values"][name] = desired
    except (OSError, ValueError, OverflowError) as exc:
        report["errors"].append(f"{name}: {exc}")


def apply_child_limits(limits: ResourceLimits | Mapping[str, Any] | None, *,
                       apply_nproc: bool = True,
                       oom_score_adj: int | None = DEFAULT_OOM_SCORE_ADJ
                       ) -> dict[str, Any]:
    """Apply child rlimits and the OOM hint; never raise (``preexec_fn``).

    Returns a report: ``applied`` and ``unsupported`` limit names, ``values``
    set, ``errors``, and the ``oom_score_adj`` written (or ``None``). The
    return value is for direct callers and tests; a ``preexec_fn`` return is
    discarded by :mod:`subprocess`.

    ``RLIMIT_NPROC`` is per real UID, not per process group, so a job wrapper
    normally leaves it to the watchdog (``apply_nproc=False``); the other
    limits are per process. ``oom_score_adj`` is best-effort.
    """
    report: dict[str, Any] = {
        "mechanism": "rlimit",
        "applied": [],
        "unsupported": [],
        "errors": [],
        "values": {},
        "oom_score_adj": None,
    }
    try:
        if limits is None:
            parsed = ResourceLimits()
        elif isinstance(limits, Mapping):
            parsed = ResourceLimits.from_mapping(limits)
        else:
            parsed = limits
        if not isinstance(parsed, ResourceLimits):
            report["errors"].append("limits is not a ResourceLimits")
            return report
        if parsed.memory_mb is not None:
            _rlimit_value(report, "RLIMIT_AS",
                          int(parsed.memory_mb * 1024 * 1024))
        if parsed.file_mb is not None:
            _rlimit_value(report, "RLIMIT_FSIZE",
                          int(parsed.file_mb * 1024 * 1024))
        if parsed.pids is not None and apply_nproc:
            _rlimit_value(report, "RLIMIT_NPROC", int(parsed.pids))
        if oom_score_adj is not None:
            try:
                value = max(-1000, min(1000, int(oom_score_adj)))
                target = PROC_ROOT / "self" / "oom_score_adj"
                with open(target, "w", encoding="ascii") as handle:
                    handle.write(f"{value}\n")
                report["oom_score_adj"] = value
            except (OSError, TypeError, ValueError) as exc:
                report["errors"].append(f"oom_score_adj: {exc}")
    except Exception as exc:  # noqa: BLE001 - the child path must never raise
        report["errors"].append(f"{type(exc).__name__}: {exc}")
    return report


def describe_resource_event(event: Mapping[str, Any]) -> str:
    """One human-readable body for a ``[ozagia] resource:`` log line."""
    kind = event.get("kind")
    if kind == "memory":
        return (f"memory limit {_fmt_mb(event.get('limit_mb'))} exceeded "
                f"(peak {_fmt_mb(event.get('observed_mb'))})")
    if kind == "pids":
        return (f"process limit {event.get('limit_pids')} exceeded "
                f"({event.get('observed')} processes in the group)")
    if kind == "wall":
        return (f"wall-time limit {event.get('limit_s')} s exceeded "
                f"(elapsed {event.get('observed_s')} s)")
    if kind == "disk" and event.get("budget_mb") is not None:
        return (f"disk budget {_fmt_mb(event.get('budget_mb'))} exceeded "
                f"({_fmt_mb(event.get('observed_mb'))} written since the job "
                "started, best-effort)")
    if kind == "disk":
        return (f"disk reserve {event.get('reserve_gb')} GiB crossed "
                f"({event.get('free_gb')} GiB free)")
    if kind == "unverified":
        return "process group is not verified as the job's; not signalled"
    return f"resource event {kind or 'unknown'}"


class ResourceWatchdog:
    """Sample one process group and filesystem; call an injected killer.

    The watchdog does not know how to signal a process. ``killer`` receives
    the breach event and is called at most once, and only while ``verify``
    returns true. Without a verifier, the group must still report at least one
    process; jobs pass a start-token/ownership verifier so a stale or reused
    process group is never treated as the job's.
    """

    def __init__(self, limits: ResourceLimits | Mapping[str, Any],
                 *, pgid: int, verify: Callable[[], bool] | None = None,
                 killer: Callable[[dict[str, Any]], Any] | None = None,
                 sampler: Callable[[int], Mapping[str, Any]] = process_group_usage,
                 disk_probe: Callable[[], Mapping[str, Any]] | None = None,
                 path: str | Path | None = None,
                 interval_s: float = 2.0,
                 reserve_disk_gb: float = 0.0,
                 on_sample: Callable[[dict[str, Any]], Any] | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.limits = (limits if isinstance(limits, ResourceLimits)
                       else ResourceLimits.from_mapping(limits))
        self.pgid = int(pgid)
        self.verify = verify
        self.killer = killer
        self._sampler = sampler
        self._disk_probe = disk_probe
        self.path = str(path) if path is not None else None
        self.interval_s = max(0.01, float(interval_s))
        self.reserve_disk_gb = max(0.0, float(reserve_disk_gb))
        self._on_sample = on_sample
        self._clock = clock
        self.started_at = clock()
        self.peak_memory_mb: float | None = None
        self.disk_used_mb: float | None = None
        self._disk_baseline_free_gb: float | None = None
        self.samples = 0
        self.kills = 0
        self.events: list[dict[str, Any]] = []
        self.last_sample: dict[str, Any] | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._kill_attempted = False
        self._unverified_reported = False

    # -- sampling ------------------------------------------------------------ #
    def _usage(self) -> dict[str, Any]:
        try:
            value = self._sampler(self.pgid)
        except Exception as exc:  # noqa: BLE001 - observation must not crash
            return {"pgid": self.pgid, "process_count": 0, "memory_mb": 0.0,
                    "error": f"{type(exc).__name__}: {exc}"}
        if not isinstance(value, Mapping):
            return {"pgid": self.pgid, "process_count": 0,
                    "memory_mb": 0.0}
        return dict(value)

    def _verified(self, usage: Mapping[str, Any]) -> bool:
        if self.verify is not None:
            try:
                return bool(self.verify())
            except Exception:  # noqa: BLE001 - a failed check is not proof
                return False
        # The default sampler reports process_count; a caller-supplied sampler
        # that omits it is taken as first-hand evidence for its own group.
        if "process_count" not in usage:
            return True
        try:
            return int(usage.get("process_count") or 0) > 0
        except (TypeError, ValueError):
            return False

    def _disk(self) -> dict[str, Any] | None:
        if self._disk_probe is not None:
            try:
                value = self._disk_probe()
            except Exception:  # noqa: BLE001
                return None
            return dict(value) if isinstance(value, Mapping) else None
        if self.path is None:
            return None
        try:
            return disk_usage(self.path)
        except Exception:  # noqa: BLE001
            return None

    def _breach(self, usage: Mapping[str, Any], disk: Mapping[str, Any] | None,
                elapsed: float) -> dict[str, Any] | None:
        limits = self.limits
        memory = usage.get("memory_mb")
        if (limits.memory_mb is not None and isinstance(memory, (int, float))
                and float(memory) > float(limits.memory_mb)):
            return {"kind": "memory", "limit_mb": limits.memory_mb,
                    "observed_mb": round(float(memory), 2)}
        count = usage.get("process_count")
        if (limits.pids is not None and isinstance(count, (int, float))
                and int(count) > int(limits.pids)):
            return {"kind": "pids", "limit_pids": int(limits.pids),
                    "observed": int(count)}
        if limits.wall_s is not None and elapsed > float(limits.wall_s):
            return {"kind": "wall", "limit_s": float(limits.wall_s),
                    "observed_s": round(elapsed, 2)}
        if disk is not None:
            free = disk.get("free_gb")
            if (isinstance(free, (int, float)) and self.reserve_disk_gb > 0
                    and float(free) < self.reserve_disk_gb):
                return {"kind": "disk",
                        "reserve_gb": self.reserve_disk_gb,
                        "free_gb": round(float(free), 2)}
            if (limits.disk_mb is not None and isinstance(free, (int, float))
                    and self._disk_baseline_free_gb is not None):
                used = max(0.0, (self._disk_baseline_free_gb
                                 - float(free)) * 1024)
                if used > float(limits.disk_mb):
                    return {"kind": "disk",
                            "budget_mb": limits.disk_mb,
                            "observed_mb": round(used, 1),
                            "free_gb": round(float(free), 2),
                            "baseline_free_gb": round(
                                self._disk_baseline_free_gb, 2)}
        return None

    def sample_once(self) -> dict[str, Any] | None:
        """Take one sample; return the sample dict (or the last one)."""
        with self._lock:
            if self._kill_attempted:
                return self.last_sample
            self.samples += 1
            usage = self._usage()
            now = self._clock()
            if not self._verified(usage):
                if not self._unverified_reported:
                    self._unverified_reported = True
                    self._add_event({
                        "ts": _now(), "pgid": self.pgid, "kind": "unverified",
                        "verified": False, "action": "not_signalled",
                    })
                return None
            memory = usage.get("memory_mb")
            if isinstance(memory, (int, float)):
                value = round(float(memory), 2)
                if self.peak_memory_mb is None or value > self.peak_memory_mb:
                    self.peak_memory_mb = value
            disk = self._disk()
            free = (disk or {}).get("free_gb")
            if isinstance(free, (int, float)):
                if self._disk_baseline_free_gb is None:
                    self._disk_baseline_free_gb = float(free)
                used = max(0.0, (self._disk_baseline_free_gb
                                 - float(free)) * 1024)
                self.disk_used_mb = max(self.disk_used_mb or 0.0,
                                        round(used, 1))
            elapsed = max(0.0, now - self.started_at)
            sample = {
                "ts": _now(),
                "pgid": self.pgid,
                "memory_mb": self.peak_memory_mb,
                "process_count": usage.get("process_count"),
                "elapsed_s": round(elapsed, 3),
                "disk_free_gb": free,
                "disk_used_mb": self.disk_used_mb,
                "peak_memory_mb": self.peak_memory_mb,
            }
            self.last_sample = sample
            breach = self._breach(usage, disk, elapsed)
            on_sample = self._on_sample
            killer = self.killer
            if breach is not None:
                self._kill_attempted = True
                breach.update({"ts": _now(), "pgid": self.pgid,
                               "verified": True, "action": "recorded"})
                self._add_event(breach)
            else:
                breach = None
        if on_sample is not None:
            try:
                on_sample(sample)
            except Exception:  # noqa: BLE001 - reporting must not crash
                pass
        if breach is not None:
            if killer is not None:
                try:
                    killer(breach)
                    breach["action"] = "signalled"
                except Exception as exc:  # noqa: BLE001
                    breach["action"] = "killer_error"
                    breach["error"] = f"{type(exc).__name__}: {exc}"
            self.kills += 1 if killer is not None else 0
        return sample

    def _add_event(self, event: dict[str, Any]) -> None:
        self.events.append(event)
        del self.events[:-_MAX_RESOURCE_EVENTS]

    # -- thread -------------------------------------------------------------- #
    def start(self) -> "ResourceWatchdog":
        with self._lock:
            if self._thread is not None:
                return self
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run, name=f"ozagia-resource-watch-{self.pgid}",
                daemon=True)
            self._thread.start()
        return self

    def _run(self) -> None:
        while not self._stop.is_set() and not self._kill_attempted:
            self.sample_once()
            if self._stop.wait(self.interval_s):
                break

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(2.0, min(self.interval_s * 2, 10.0)))

    @property
    def alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())


def top_process_groups(limit: int = 10) -> list[dict[str, Any]]:
    """The largest process groups by RSS, without attributing them to ozagia."""
    groups: dict[int, dict[str, Any]] = {}
    for pid in _pids():
        item = process_usage(pid)
        if item is None:
            continue
        pgid = item.get("pgid")
        if not isinstance(pgid, int):
            continue
        group = groups.setdefault(pgid, {
            "pgid": pgid, "process_count": 0, "memory_mb": 0.0,
            "command": "",
        })
        group["process_count"] += 1
        memory = item.get("memory_mb")
        if isinstance(memory, (int, float)):
            group["memory_mb"] = round(group["memory_mb"] + float(memory), 2)
        if not group["command"] and item.get("command"):
            group["command"] = item["command"]
    ordered = sorted(groups.values(), key=lambda g: (-g["memory_mb"], g["pgid"]))
    return ordered[:max(0, min(int(limit), 100))]


def command_key(argv: Sequence[str]) -> str:
    """A stable key that does not persist raw (possibly secret) arguments."""
    if not argv:
        return "cmd:unknown"
    head = Path(str(argv[0])).name[:40] or "unknown"
    digest = hashlib.sha256(
        "\0".join(str(item) for item in argv).encode("utf-8", "replace")
    ).hexdigest()[:12]
    return f"cmd:{head}:{digest}"


def history_path(root: str | Path) -> Path:
    return state_path(root, HISTORY_DIR, HISTORY_FILE)


def _read_history(root: str | Path, *, limit: int = MAX_HISTORY_RECORDS) -> list[dict[str, Any]]:
    path = history_path(root)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    records: list[dict[str, Any]] = []
    for line in lines[-max(0, int(limit)):]:
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            records.append(item)
    return records


def _trim_history(path: Path) -> None:
    try:
        if path.stat().st_size <= MAX_HISTORY_BYTES:
            return
    except OSError:
        return
    records: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines[-MAX_HISTORY_RECORDS:]:
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            records.append(item)
    keep = records[-MAX_HISTORY_RECORDS // 2:]
    try:
        path.write_text(
            "".join(json.dumps(item, sort_keys=True) + "\n" for item in keep),
            encoding="utf-8")
    except OSError:
        pass


def record_sample(root: str | Path, key: str, *, memory_mb: float | None = None,
                  disk_mb: float | None = None, duration_s: float | None = None,
                  exit_code: int | None = None, kind: str | None = None,
                  meta: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Append one bounded observation. Never stores raw argv or file content."""
    if not isinstance(key, str) or not key.strip() or len(key) > 200 or "\n" in key:
        raise ValueError("key must be a non-empty single-line string")
    record = {
        "schema": SCHEMA,
        "ts": _now(),
        "key": key,
        "kind": kind,
        "memory_mb": round(float(memory_mb), 2) if memory_mb is not None else None,
        "disk_mb": round(float(disk_mb), 2) if disk_mb is not None else None,
        "duration_s": round(float(duration_s), 3) if duration_s is not None else None,
        "exit_code": exit_code,
    }
    if meta:
        record["meta"] = {str(k): v for k, v in list(meta.items())[:20]}
    path = history_path(root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
    except OSError:
        return record
    _trim_history(path)
    return record


def _percentile(values: Iterable[float], fraction: float) -> float | None:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return None
    index = max(0, min(len(ordered) - 1,
                       int(round(fraction * (len(ordered) - 1)))))
    return round(ordered[index], 2)


def _confidence(count: int) -> str:
    if count >= 20:
        return "high"
    if count >= 5:
        return "medium"
    if count:
        return "low"
    return "none"


def history_summary(root: str | Path, *, key: str | None = None,
                    limit: int = 2000) -> dict[str, Any]:
    """Per-key p50/p90/max from the bounded history."""
    records = _read_history(root, limit=limit)
    groups: dict[str, list[dict[str, Any]]] = {}
    for item in records:
        name = item.get("key")
        if isinstance(name, str):
            groups.setdefault(name, []).append(item)
    if key is not None:
        groups = {name: values for name, values in groups.items() if name == key}
    result: dict[str, Any] = {}
    for name, values in sorted(groups.items()):
        memory = [v["memory_mb"] for v in values
                  if isinstance(v.get("memory_mb"), (int, float))]
        disk = [v["disk_mb"] for v in values
                if isinstance(v.get("disk_mb"), (int, float))]
        duration = [v["duration_s"] for v in values
                    if isinstance(v.get("duration_s"), (int, float))]
        result[name] = {
            "samples": len(values),
            "confidence": _confidence(len(values)),
            "last": values[-1].get("ts"),
            "memory_mb": {
                "p50": _percentile(memory, 0.50),
                "p90": _percentile(memory, 0.90),
                "max": round(max(memory), 2) if memory else None,
            },
            "disk_mb": {
                "p50": _percentile(disk, 0.50),
                "p90": _percentile(disk, 0.90),
                "max": round(max(disk), 2) if disk else None,
            },
            "duration_s": {
                "p50": _percentile(duration, 0.50),
                "p90": _percentile(duration, 0.90),
                "max": round(max(duration), 3) if duration else None,
            },
            "exit_codes": sorted({v.get("exit_code") for v in values
                                  if v.get("exit_code") is not None}),
        }
    return result


def estimate(root: str | Path, key: str, *, default_mb: float | None = None,
             default_disk_mb: float | None = None) -> dict[str, Any]:
    summary = history_summary(root, key=key).get(key)
    if not summary:
        return {
            "key": key,
            "samples": 0,
            "confidence": "none",
            "memory_mb": default_mb,
            "disk_mb": default_disk_mb,
            "source": "default" if default_mb is not None else "none",
        }
    memory = (summary.get("memory_mb") or {}).get("p90")
    disk = (summary.get("disk_mb") or {}).get("p90")
    return {
        "key": key,
        "samples": summary.get("samples", 0),
        "confidence": summary.get("confidence", "none"),
        "memory_mb": memory if memory is not None else default_mb,
        "disk_mb": disk if disk is not None else default_disk_mb,
        "source": "history",
    }


def _policy_file(root: Path) -> tuple[Path | None, str | None]:
    direct = root / "ozagia" / "resources.toml"
    if direct.is_file():
        return direct, None
    config = root / "ozagia" / "config.toml"
    if config.is_file():
        return config, "resources"
    return None, None


def load_policy(root: str | Path) -> dict[str, Any]:
    """Load ``ozagia/resources.toml`` or ``[resources]`` from ``ozagia/config.toml``."""
    root = Path(root).resolve()
    policy = _deep_merge(DEFAULT_POLICY, {})
    errors: list[str] = []
    path, section = _policy_file(root)
    if path is not None:
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
            if section:
                data = data.get(section) or {}
            if isinstance(data, dict):
                policy = _deep_merge(policy, data)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            errors.append(f"{path}: {exc}")
    for section in ("limits", "admission", "worktrees"):
        if not isinstance(policy.get(section), dict):
            errors.append(f"[{section}] must be a table")
            policy[section] = dict(DEFAULT_POLICY[section])
    try:
        policy["reserve_memory_pct"] = _clean_number(
            policy.get("reserve_memory_pct"), name="reserve_memory_pct",
            high=80)
        policy["reserve_disk_pct"] = _clean_number(
            policy.get("reserve_disk_pct"), name="reserve_disk_pct", high=80)
        policy["reserve_disk_gb"] = _clean_number(
            policy.get("reserve_disk_gb"), name="reserve_disk_gb")
        admission = policy.setdefault("admission", {})
        for key in ("min_free_memory_pct", "min_free_disk_pct"):
            admission[key] = _clean_number(admission.get(key), name=key, high=100)
        admission["min_free_disk_gb"] = _clean_number(
            admission.get("min_free_disk_gb"), name="min_free_disk_gb")
        admission["wait_max_s"] = int(_clean_number(
            admission.get("wait_max_s"), name="wait_max_s", high=86400))
        limits = policy.setdefault("limits", {})
        for key in ("job_memory_mb", "job_disk_mb", "job_pids"):
            limits[key] = int(_clean_number(limits.get(key), name=key, low=1))
        worktrees = policy.setdefault("worktrees", {})
        worktrees["min_free_gb"] = _clean_number(
            worktrees.get("min_free_gb"), name="min_free_gb")
        worktrees["avoid_tmpfs"] = bool(worktrees.get("avoid_tmpfs", True))
    except (TypeError, ValueError) as exc:
        errors.append(str(exc))
    policy["errors"] = errors
    return policy


def _dedupe_disks(disks: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str | None, str | None]] = set()
    out: list[dict[str, Any]] = []
    for item in disks:
        key = (item.get("mount"), item.get("fstype"))
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _retry_after(freeing: Sequence[Mapping[str, Any]], shortfall: Mapping[str, Any]) -> int | None:
    memory = shortfall.get("memory_mb") or 0
    disk = shortfall.get("disk_mb") or 0
    if memory <= 0 and disk <= 0:
        return None
    memory_gain = 0.0
    disk_gain = 0.0
    for item in sorted(freeing, key=lambda value: float(value.get("eta_s") or 0)):
        try:
            memory_gain += float(item.get("memory_mb") or 0)
            disk_gain += float(item.get("disk_mb") or 0)
        except (TypeError, ValueError):
            continue
        if memory_gain >= memory and disk_gain >= disk:
            return max(1, int(float(item.get("eta_s") or 1)))
    if freeing:
        try:
            return max(1, int(max(float(item.get("eta_s") or 0)
                                    for item in freeing)))
        except (TypeError, ValueError):
            return None
    return None


def check_admission(root: str | Path, *, kind: str = "job",
                    memory_mb: float | None = None,
                    disk_mb: float | None = None,
                    path: str | Path | None = None,
                    key: str | None = None,
                    freeing: Sequence[Mapping[str, Any]] = (),
                    policy: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Decide whether a run is likely to fit. Read-only: nothing is started."""
    if kind not in {"job", "task", "worktree"}:
        raise ValueError("kind must be job, task or worktree")
    if memory_mb is not None and memory_mb < 0:
        raise ValueError("memory_mb must be >= 0")
    if disk_mb is not None and disk_mb < 0:
        raise ValueError("disk_mb must be >= 0")
    root = Path(root).resolve()
    loaded = dict(policy) if policy is not None else load_policy(root)
    if not loaded.get("enabled", True):
        return {
            "schema": SCHEMA, "ok": True, "kind": kind,
            "checked_at": _now(), "disabled": True,
            "checks": [{"level": "info", "id": "resources.disabled",
                        "message": "resource checks are disabled"}],
            "suggestions": [],
        }
    host = host_snapshot()
    disk = disk_usage(path or root)
    limits = loaded.get("limits") or {}
    admission = loaded.get("admission") or {}
    worktree_policy = loaded.get("worktrees") or {}
    default_memory = float(limits.get("job_memory_mb") or 0)
    default_disk = float(limits.get("job_disk_mb") or 0)
    if kind == "worktree":
        default_memory = 0.0
        default_disk = 0.0
    expected = estimate(root, key, default_mb=default_memory,
                        default_disk_mb=default_disk) if key else {
        "key": key, "samples": 0, "confidence": "none",
        "memory_mb": None, "disk_mb": None, "source": "request",
    }
    requested_memory = (default_memory if memory_mb is None
                        else float(memory_mb))
    requested_disk = (default_disk if disk_mb is None else float(disk_mb))
    if key and memory_mb is None and isinstance(expected.get("memory_mb"), (int, float)):
        requested_memory = float(expected["memory_mb"])
    if key and disk_mb is None and isinstance(expected.get("disk_mb"), (int, float)):
        requested_disk = float(expected["disk_mb"])

    checks: list[dict[str, Any]] = []
    shortfall_memory = 0.0
    shortfall_disk = 0.0

    memory = host.get("memory") or {}
    available_mb = memory.get("available_mb")
    total_mb = memory.get("total_mb")
    if available_mb is None or total_mb is None:
        checks.append({
            "id": "memory.unknown", "level": "not_checked",
            "message": "available memory could not be read on this platform",
        })
    else:
        available_pct = (available_mb / total_mb * 100) if total_mb else 0.0
        if available_pct < float(admission.get("min_free_memory_pct", 15)):
            checks.append({
                "id": "memory.host_min", "level": "error",
                "message": (f"host available memory is {available_pct:.1f}% "
                            f"(minimum {admission.get('min_free_memory_pct')}%)"),
                "available_pct": round(available_pct, 1),
            })
        reserve = total_mb * float(loaded.get("reserve_memory_pct", 10)) / 100
        usable = max(0.0, float(available_mb) - reserve)
        if requested_memory > usable:
            shortfall_memory = requested_memory - usable
            checks.append({
                "id": "memory.request", "level": "error",
                "message": (f"requested {requested_memory:.0f} MiB, "
                            f"usable {usable:.0f} MiB after reserve"),
                "requested_mb": round(requested_memory, 1),
                "usable_mb": round(usable, 1),
            })
        else:
            checks.append({
                "id": "memory.request", "level": "ok",
                "message": (f"requested {requested_memory:.0f} MiB fits in "
                            f"{usable:.0f} MiB usable"),
            })

    free_gb = disk.get("free_gb")
    if free_gb is None:
        checks.append({
            "id": "disk.unknown", "level": "not_checked",
            "message": "filesystem free space could not be read",
            "path": disk.get("path"),
        })
    else:
        free_pct = disk.get("free_pct")
        min_pct = float(admission.get("min_free_disk_pct", 10))
        min_gb = float(admission.get("min_free_disk_gb", 2))
        if (free_pct is not None and free_pct < min_pct) or free_gb < min_gb:
            checks.append({
                "id": "disk.host_min", "level": "error",
                "message": (f"filesystem {disk.get('mount') or disk.get('path')} "
                            f"has {free_gb:.2f} GiB free "
                            f"({free_pct if free_pct is not None else '?'}%), "
                            f"below the reserve"),
                "free_gb": free_gb, "free_pct": free_pct,
            })
        total_gb = disk.get("total_gb") or 0.0
        reserve_gb = max(float(loaded.get("reserve_disk_gb", 1)),
                         float(total_gb) * float(loaded.get("reserve_disk_pct", 5)) / 100)
        usable_disk = max(0.0, float(free_gb) - reserve_gb)
        requested_disk_gb = requested_disk / 1024
        if requested_disk_gb > usable_disk:
            shortfall_disk = (requested_disk_gb - usable_disk) * 1024
            checks.append({
                "id": "disk.request", "level": "error",
                "message": (f"requested {requested_disk:.0f} MiB, usable "
                            f"{usable_disk * 1024:.0f} MiB after reserve"),
                "requested_mb": round(requested_disk, 1),
                "usable_mb": round(usable_disk * 1024, 1),
            })
        else:
            checks.append({
                "id": "disk.request", "level": "ok",
                "message": (f"requested {requested_disk:.0f} MiB fits in "
                            f"{usable_disk * 1024:.0f} MiB usable"),
            })
        if kind == "worktree" and disk.get("is_tmpfs") and worktree_policy.get("avoid_tmpfs", True):
            checks.append({
                "id": "disk.tmpfs", "level": "error",
                "message": (f"worktree path is on tmpfs ({disk.get('mount')}); "
                            "it would consume memory"),
                "path": disk.get("path"),
            })
        if kind == "worktree":
            min_worktree = float(worktree_policy.get("min_free_gb", 5))
            if free_gb < min_worktree:
                checks.append({
                    "id": "disk.worktree_min", "level": "error",
                    "message": (f"worktree needs {min_worktree:.1f} GiB free; "
                                f"{free_gb:.2f} GiB available"),
                })

    shortfall = {"memory_mb": round(shortfall_memory, 1),
                 "disk_mb": round(shortfall_disk, 1)}
    retry_after = _retry_after(freeing, shortfall)
    suggestions: list[str] = []
    if shortfall_memory > 0:
        suggestions.append("stop or finish another job, or lower --memory-mb")
    if shortfall_disk > 0:
        suggestions.append("free disk space or choose another worktree root")
    if retry_after:
        suggestions.append(f"retry in about {retry_after} s")
    if kind == "worktree" and disk.get("is_tmpfs"):
        suggestions.append("use a worktree root on a normal filesystem")
    errors = [check for check in checks if check["level"] == "error"]
    return {
        "schema": SCHEMA,
        "ok": not errors,
        "kind": kind,
        "reason": errors[0]["id"] if errors else None,
        "checked_at": _now(),
        "path": disk.get("path"),
        "requested": {
            "memory_mb": round(requested_memory, 1),
            "disk_mb": round(requested_disk, 1),
        },
        "available": {
            "memory_mb": available_mb,
            "memory_total_mb": total_mb,
            "disk_free_gb": free_gb,
            "disk_free_pct": disk.get("free_pct"),
            "disk_total_gb": disk.get("total_gb"),
            "disk_mount": disk.get("mount"),
            "disk_fstype": disk.get("fstype"),
        },
        "expected": expected,
        "shortfall": shortfall,
        "retry_after_s": retry_after,
        "checks": checks,
        "suggestions": suggestions,
    }


def status(root: str | Path, *, path: str | Path | None = None,
           top: int = 10) -> dict[str, Any]:
    root = Path(root).resolve()
    host = host_snapshot()
    disks = [disk_usage(root)]
    if path is not None:
        disks.append(disk_usage(path))
    policy = load_policy(root)
    return {
        "schema": SCHEMA,
        "checked_at": _now(),
        "host": host,
        "disks": _dedupe_disks(disks),
        "top_groups": top_process_groups(top),
        "policy": policy,
        "history": history_summary(root, limit=500),
        "enforcement": {
            "available": _resource is not None,
            "mechanism": ENFORCEMENT_MECHANISM if _resource is not None else "watch",
            "rlimits": sorted({"RLIMIT_AS", "RLIMIT_FSIZE", "RLIMIT_NPROC"}
                              & {name for name in dir(_resource)
                                 if name.startswith("RLIMIT_")}
                              ) if _resource is not None else [],
            "cgroups": False,
            "note": ("child rlimits + verified-group watchdog; cgroups are "
                     "not used"),
        },
    }


def _fmt_mb(value: Any) -> str:
    if isinstance(value, (int, float)):
        return f"{value:.0f} MiB"
    return "unknown"


def format_memory(value: Any) -> str:
    """``value`` MiB as text, or ``unknown`` (public for job log lines)."""
    return _fmt_mb(value)


def format_status(report: Mapping[str, Any]) -> str:
    host = report.get("host") or {}
    memory = host.get("memory") or {}
    load = host.get("load") or {}
    lines = [
        f"host: {host.get('platform')}  cpus={host.get('cpu_count')}  "
        f"load={load.get('1m')}/{load.get('5m')}/{load.get('15m')}",
        f"memory: {_fmt_mb(memory.get('available_mb'))} available of "
        f"{_fmt_mb(memory.get('total_mb'))} "
        f"({memory.get('used_pct')}% used, source={memory.get('source')})",
    ]
    for disk in report.get("disks") or []:
        lines.append(
            f"disk {disk.get('mount') or disk.get('path')}: "
            f"{disk.get('free_gb')} GiB free ({disk.get('free_pct')}%), "
            f"fstype={disk.get('fstype')}"
            + (" tmpfs" if disk.get("is_tmpfs") else ""))
    pressure = host.get("pressure") or {}
    for name in ("memory", "io", "cpu"):
        value = pressure.get(name)
        if not value:
            continue
        some = value.get("some") or {}
        lines.append(f"pressure {name}: avg10={some.get('avg10')} "
                     f"avg60={some.get('avg60')}")
    groups = report.get("top_groups") or []
    if groups:
        lines.append("top process groups by RSS:")
        for group in groups[:10]:
            command = (group.get("command") or "").split(" ")[0][:40]
            lines.append(f"  pgid {group.get('pgid')}: "
                         f"{_fmt_mb(group.get('memory_mb'))} "
                         f"({group.get('process_count')} procs) {command}")
    if report.get("policy", {}).get("errors"):
        lines.append("policy errors: " + "; ".join(report["policy"]["errors"]))
    enforcement = report.get("enforcement") or {}
    if enforcement.get("available"):
        lines.append("enforcement: child rlimits + verified-group watchdog "
                     "(cgroups are not used)")
    else:
        lines.append("enforcement: observation only (rlimits unavailable on "
                     "this platform)")
    return "\n".join(lines)


def format_check(report: Mapping[str, Any]) -> str:
    head = "ok" if report.get("ok") else "refused"
    lines = [f"{head}: {report.get('kind')} "
             f"memory={_fmt_mb((report.get('requested') or {}).get('memory_mb'))} "
             f"disk={_fmt_mb((report.get('requested') or {}).get('disk_mb'))}"]
    for check in report.get("checks") or []:
        if check.get("level") in {"ok", "info"}:
            continue
        lines.append(f"  {check.get('level')}: {check.get('id')}: "
                     f"{check.get('message')}")
    for suggestion in report.get("suggestions") or []:
        lines.append(f"  suggestion: {suggestion}")
    if report.get("retry_after_s"):
        lines.append(f"  retry_after_s: {report['retry_after_s']}")
    return "\n".join(lines)


__all__ = [
    "DEFAULT_OOM_SCORE_ADJ", "DEFAULT_POLICY", "ENFORCEMENT_MECHANISM",
    "ResourceLimits", "ResourceWatchdog", "apply_child_limits",
    "check_admission", "command_key", "describe_resource_event", "disk_usage",
    "estimate", "format_check", "format_memory", "format_status", "history_path",
    "history_summary", "host_snapshot", "limits_from_policy", "load_policy",
    "process_group_usage", "process_usage", "record_sample", "status",
    "top_process_groups",
]
