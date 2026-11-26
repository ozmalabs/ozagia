"""One read-only status snapshot for a repository.

This module composes checks that already exist: worktree/branch checks, host
resources, the created-file ledger, runtime services and tunnels, the graph
daemon, diagnostics, project configuration and the newest deep-review manifest.
It is not a second service manager and it never builds an index, starts a
daemon or process, opens a tunnel, creates a review, prunes one, or writes to
the repository or its state directory.

Absence and ignorance are distinct. A missing daemon is ``absent`` (a known
fact: nothing is listening); a daemon that does not answer in time is
``unknown``. The same rule holds for the file ledger, project configuration
and graph snapshot. A component is never reported as an empty object without a
state that says why.
"""
from __future__ import annotations

import json
import os
import stat
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ozagia.config import (
    ConfigError,
    config_path,
    load_project_config,
    state_path,
)
from ozagia.core import owners as owners_module
from ozagia.core import mesh as mesh_module
from ozagia.core.files import (
    FileLedgerError,
    ledger_path,
    list_files,
    new_file_count,
    orphans,
)
from ozagia.core.resources import status as resources_status
from ozagia.core.runtime import runtime_line, runtime_status
from ozagia.core.worktree import WorktreeError, check as worktree_check
from ozagia.daemon import protocol
from ozagia.daemon.client import (
    DaemonClient,
    DaemonError,
    DaemonTimeout,
    DaemonUnavailable,
)
from ozagia.doctor import diagnose

SCHEMA = 1
DAEMON_TIMEOUT_S = 2.0
MAX_DAEMON_TIMEOUT_S = 30.0
MIN_DAEMON_TIMEOUT_S = 0.05
FILE_LIMIT = 200
TEXT_WORKTREE_LIMIT = 10
TEXT_RUNTIME_LIMIT = 10
TEXT_FINDING_LIMIT = 5
SNAPSHOT_STATES = ("missing", "stale", "invalid", "unknown")
REVIEW_DIR_PREFIX = "deep-"
REVIEW_MANIFEST_NAME = "manifest.json"
REVIEW_MANIFEST_SCHEMA = "ozagia.deep_review.manifest.v1"
REVIEW_MANIFEST_MAX_BYTES = 1_000_000
REVIEW_SCAN_LIMIT = 256
REVIEW_SUBJECT_LIMIT = 32
INTEGRATION_LIMIT = 16
CAPABILITY_WARNING_LIMIT = 20
JOB_LIMIT = 50
PROVIDER_LIMIT = 20
SESSION_PROFILE_LIMIT = 50
SESSION_PROFILE_SCAN_LIMIT = 200
SESSION_PROFILE_MAX_BYTES = 32_768
SESSION_MODEL_LIMIT = 20
SESSION_TOKEN_KEYS = ("input", "output", "reasoning", "cache_read",
                      "cache_write")
SESSION_TIMING_KEYS = ("first_token", "streamed", "completed", "model_turn")
SESSION_COVERAGE_KEYS = (
    "assistant_turns", "turns_with_tokens", "turns_without_tokens",
    "turns_with_complete_timing", "turns_without_complete_timing",
    "host_tool_calls", "host_tool_calls_with_timing",
    "host_tool_calls_without_timing",
)
# Kept in sync with `session_profile_cli._CAPTURE_DIAGNOSTIC_COUNTERS`; the
# status section only copies these metadata counters, never an event body.
SESSION_CAPTURE_KEYS = (
    "events_received", "step_events", "content_events", "tool_parts_seen",
    "tool_parts_accepted", "tool_lifecycle_started", "tool_lifecycle_called",
    "tool_lifecycle_terminal", "tool_lifecycle_events_accepted",
    "profile_events_accepted", "store_attempts",
    "store_successes_observed_lower_bound",
    "store_failures_observed_lower_bound",
)
OWNERSHIP_EDIT_SCAN = 2000
OWNERSHIP_FILE_LIMIT = 16
OWNERSHIP_ROW_LIMIT = 32
OWNERSHIP_SESSION_LIMIT = 16
MESH_SESSION_LIMIT = 32
MESH_LEASE_LIMIT = 32
MESH_LINK_LIMIT = 32
MESH_DELEGATION_LIMIT = 16
MESH_ERROR_LIMIT = 16


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _issue(component: str, code: str, message: str) -> dict[str, str]:
    return {"component": component, "code": code, "message": message}


def _section(state: str, report: Any = None, *, error: str | None = None,
             **extra: Any) -> dict[str, Any]:
    """One component envelope: a state, an error and its unmodified report."""
    return {"state": state, "error": error, "report": report, **extra}


def _timeout(value: float) -> float:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        seconds = DAEMON_TIMEOUT_S
    if seconds != seconds:  # NaN
        seconds = DAEMON_TIMEOUT_S
    return min(MAX_DAEMON_TIMEOUT_S, max(MIN_DAEMON_TIMEOUT_S, seconds))


def _present(path: Path | None) -> bool:
    if path is None:
        return False
    try:
        return path.is_file()
    except OSError:
        return False


def _config_status(
    root: Path,
) -> tuple[dict[str, Any], Any | None]:
    """Load project configuration, keeping invalid config a hard error."""
    path: Path | None = None
    try:
        path = config_path(root)
        project = load_project_config(root)
    except (ConfigError, OSError) as exc:
        return (
            _section(
                "invalid", None, error=str(exc),
                path=str(path) if path is not None else None,
                present=_present(path),
            ),
            None,
        )
    report = {
        "source": str(project.source) if project.source is not None else None,
        "adapters": list(project.adapters),
        "source_dirs": list(project.source_dirs),
        "test_dirs": list(project.test_dirs),
        "jobs_enabled": project.jobs_enabled,
        "runtime_services": [item.name for item in project.runtime_services],
        "runtime_tunnels": [item.name for item in project.runtime_tunnels],
    }
    return (
        _section(
            "configured" if path is not None else "defaults", report,
            path=str(path) if path is not None else None,
            present=_present(path),
        ),
        project,
    )


def _workspace_status(
    root: Path, degraded: list[dict[str, str]],
) -> dict[str, Any]:
    try:
        report = worktree_check(root)
    except (WorktreeError, OSError) as exc:
        degraded.append(_issue(
            "workspace", "workspace.unavailable",
            f"worktree check failed: {exc}"))
        return _section("unavailable", error=str(exc))
    return _section("ok", report)


def _resources_status(
    root: Path, *, path: str | Path | None, top: int,
    degraded: list[dict[str, str]],
) -> dict[str, Any]:
    try:
        report = resources_status(root, path=path, top=top)
    except (OSError, ValueError) as exc:
        degraded.append(_issue(
            "resources", "resources.unavailable",
            f"resource observation failed: {exc}"))
        return _section("unavailable", error=str(exc))
    return _section("ok", report)


def _files_status(
    root: Path, degraded: list[dict[str, str]],
) -> dict[str, Any]:
    try:
        ledger = ledger_path(root)
        records = list_files(root, limit=FILE_LIMIT)
        orphan_records = orphans(root)
        new = new_file_count(root)
    except (FileLedgerError, OSError) as exc:
        degraded.append(_issue(
            "files", "files.unavailable",
            f"created-file ledger could not be read: {exc}"))
        return _section("unavailable", error=str(exc))
    report = {
        "ledger": {"path": str(ledger), "present": _present(ledger)},
        "records": records,
        "orphans": orphan_records,
        "new": new,
        "counts": {
            "records": len(records),
            "orphans": len(orphan_records),
            "new": new.get("count", 0),
        },
    }
    return _section("ok", report)


def _runtime_status(
    root: Path, project: Any | None, degraded: list[dict[str, str]],
) -> dict[str, Any]:
    try:
        report = runtime_status(project if project is not None else root)
    except Exception as exc:  # noqa: BLE001 - one component must not stop the snapshot
        degraded.append(_issue(
            "runtime", "runtime.unavailable",
            f"runtime status could not be composed: {exc}"))
        return _section("unavailable", error=str(exc))
    return _section("ok", report)


def _daemon_status(
    root: Path, *, timeout: float, degraded: list[dict[str, str]],
) -> dict[str, Any]:
    """Ping an existing daemon; never start or autostart one."""
    limit = _timeout(timeout)
    report: dict[str, Any] = {
        "socket": None,
        "timeout_s": limit,
        "ping": None,
        "stats": None,
        "stats_error": None,
    }
    try:
        sock = protocol.socket_path(root)
    except (OSError, ValueError) as exc:
        degraded.append(_issue(
            "daemon", "daemon.unknown",
            f"daemon socket path could not be resolved: {exc}"))
        return _section("unknown", report, error=str(exc))
    report["socket"] = str(sock)
    try:
        with DaemonClient(sock, timeout=limit) as client:
            try:
                ping = client.ping(timeout=limit)
            except DaemonUnavailable as exc:
                degraded.append(_issue(
                    "daemon", "daemon.absent",
                    f"no graph daemon is listening on {sock}: {exc}"))
                return _section("absent", report, error=str(exc))
            except DaemonTimeout as exc:
                degraded.append(_issue(
                    "daemon", "daemon.unknown",
                    f"graph daemon did not answer within {limit:g}s: {exc}"))
                return _section("unknown", report, error=str(exc))
            except DaemonError as exc:
                degraded.append(_issue(
                    "daemon", "daemon.unknown",
                    f"graph daemon could not be queried: {exc}"))
                return _section("unknown", report, error=str(exc))
            if ping.get("ok") is False:
                error = str(ping.get("error") or "daemon ping failed")
                degraded.append(_issue(
                    "daemon", "daemon.unknown",
                    f"graph daemon ping failed: {error}"))
                return _section("unknown", report, error=error)
            report["ping"] = ping
            report["version"] = ping.get("version")
            report["daemon_root"] = ping.get("root")
            report["pid"] = ping.get("pid")
            try:
                stats_reply = client.stats()
            except DaemonTimeout as exc:
                report["stats_error"] = str(exc)
                degraded.append(_issue(
                    "daemon", "daemon.stats",
                    f"graph daemon pinged but stats timed out: {exc}"))
            except DaemonError as exc:
                report["stats_error"] = str(exc)
                degraded.append(_issue(
                    "daemon", "daemon.stats",
                    f"graph daemon pinged but stats failed: {exc}"))
            else:
                if stats_reply.get("ok") is False:
                    report["stats_error"] = str(
                        stats_reply.get("error") or "daemon stats failed")
                    degraded.append(_issue(
                        "daemon", "daemon.stats",
                        f"graph daemon stats failed: {report['stats_error']}"))
                else:
                    stats = stats_reply.get("stats")
                    if isinstance(stats, dict):
                        report["stats"] = stats
                        report["uptime_s"] = stats.get("uptime_s")
                        report["idle_timeout_s"] = stats.get("idle_timeout_s")
                    else:
                        report["stats_error"] = "reply has no stats object"
                        degraded.append(_issue(
                            "daemon", "daemon.stats",
                            "graph daemon stats reply has no stats object"))
    except Exception as exc:  # noqa: BLE001 - one component must not stop the snapshot
        degraded.append(_issue(
            "daemon", "daemon.unknown",
            f"graph daemon state could not be determined: {exc}"))
        return _section("unknown", report, error=str(exc))
    return _section("running", report)


def _bounded_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if number < 0:
        return None
    return min(2**53 - 1, number)


def _bounded_text(value: Any, limit: int = 200) -> str | None:
    if not isinstance(value, str):
        return None
    return value[:limit]


def _session_label(harness: Any, session: Any) -> str | None:
    if not isinstance(session, str) or not session:
        return None
    return owners_module.label(
        harness if isinstance(harness, str) and harness else "opencode",
        session)


def _job_row(job: Any) -> dict[str, Any] | None:
    """One bounded, content-free job row.

    Command text, argv values, paths, environment values and log contents are
    deliberately not copied: a command line can carry a secret that was never
    resolved through the daemon broker. Only counts and process facts remain.
    """
    if not isinstance(job, Mapping):
        return None
    harness = job.get("harness")
    session = job.get("session")
    argv = job.get("argv")
    row: dict[str, Any] = {
        "id": _bounded_text(job.get("id"), 40),
        "state": _bounded_text(job.get("status"), 40),
        "pid": _bounded_int(job.get("pid")),
        "started": _bounded_text(job.get("started"), 60),
        "ended": _bounded_text(job.get("ended"), 60),
        "exit_code": _bounded_int(job.get("exit_code")),
        "signal": _bounded_text(job.get("signal"), 40),
        "detach": job.get("detach") is True,
        "harness": _bounded_text(harness, 80),
        "label": _session_label(harness, session),
        "enforcement": _bounded_text(job.get("enforcement"), 40),
        "peak_memory_mb": _bounded_int(job.get("peak_memory_mb")),
        "peak_disk_write_mb": _bounded_int(job.get("peak_disk_write_mb")),
        "log_bytes": _bounded_int(job.get("log_bytes")),
        "elapsed_s": _bounded_int(job.get("elapsed_s")),
        "argv_count": len(argv) if isinstance(argv, list) else None,
        "has_command": isinstance(job.get("command"), str),
        "leader_exited": job.get("leader_exited") is True,
        "group_alive": job.get("group_alive") is True,
        "stopped_on_load": job.get("stopped_on_load") is True,
    }
    return row


def _jobs_status(
    daemon: Mapping[str, Any], *, timeout: float,
    degraded: list[dict[str, str]],
) -> dict[str, Any]:
    """Read the job table of an already-running daemon; never start one."""
    limit = _timeout(timeout)
    daemon_report = daemon.get("report")
    daemon_report = daemon_report if isinstance(daemon_report, Mapping) else {}
    sock = daemon_report.get("socket")
    report: dict[str, Any] = {
        "socket": sock,
        "timeout_s": limit,
        "source": "daemon",
        "jobs": [],
        "counts": {"known": None, "running": None, "shown": 0},
        "truncated": False,
    }
    state = daemon.get("state")
    if state == "absent":
        return _section("absent", report)
    if state != "running" or not sock:
        return _section(
            "unknown", report,
            error=str(daemon.get("error") or "daemon state is unknown"))
    try:
        with DaemonClient(sock, timeout=limit) as client:
            reply = client.job_list()
    except DaemonUnavailable as exc:
        return _section("absent", report, error=str(exc))
    except DaemonTimeout as exc:
        degraded.append(_issue(
            "jobs", "jobs.unknown",
            f"job table did not answer within {limit:g}s: {exc}"))
        return _section("unknown", report, error=str(exc))
    except DaemonError as exc:
        degraded.append(_issue(
            "jobs", "jobs.unknown", f"job table could not be read: {exc}"))
        return _section("unknown", report, error=str(exc))
    if reply.get("ok") is False:
        error = str(reply.get("error") or "job list failed")
        degraded.append(_issue(
            "jobs", "jobs.unknown", f"job table could not be read: {error}"))
        return _section("unknown", report, error=error)
    jobs = reply.get("jobs")
    if not isinstance(jobs, list):
        degraded.append(_issue(
            "jobs", "jobs.unknown", "job reply has no jobs list"))
        return _section("unknown", report, error="job reply has no jobs list")
    rows = [row for row in (_job_row(job) for job in jobs[:JOB_LIMIT])
            if row is not None]
    running = sum(1 for job in jobs
                  if isinstance(job, Mapping)
                  and job.get("status") in ("running", "detached"))
    report["jobs"] = rows
    report["counts"] = {
        "known": len(jobs),
        "running": running,
        "shown": len(rows),
    }
    report["truncated"] = len(jobs) > JOB_LIMIT
    return _section("ok", report)


def _admissions_status(daemon: Mapping[str, Any]) -> dict[str, Any]:
    """Job admission and resource refusals from the running daemon's stats."""
    report: dict[str, Any] = {
        "source": "daemon_jobs",
        "counts": {key: None for key in (
            "jobs_refused", "resource_refusals", "jobs_resource_killed",
            "resource_events", "cgroup_oom_kills", "stopped_on_load",
            "resource_peak_mb", "resource_peak_disk_write_mb")},
    }
    state = daemon.get("state")
    if state == "absent":
        return _section("absent", report)
    if state != "running":
        return _section(
            "unknown", report,
            error=str(daemon.get("error") or "daemon state is unknown"))
    daemon_report = daemon.get("report")
    daemon_report = daemon_report if isinstance(daemon_report, Mapping) else {}
    stats = daemon_report.get("stats")
    if not isinstance(stats, Mapping):
        return _section(
            "unknown", report,
            error=str(daemon_report.get("stats_error")
                      or "daemon stats were not read"))
    jobs = stats.get("jobs")
    if not isinstance(jobs, Mapping):
        return _section(
            "unknown", report,
            error="daemon stats did not report job counters")
    report["counts"] = {
        key: _bounded_int(jobs.get(key)) for key in report["counts"]}
    return _section("ok", report)


def _providers_status(
    daemon: Mapping[str, Any], *, timeout: float,
    degraded: list[dict[str, str]],
) -> dict[str, Any]:
    """Secret-provider availability from the running daemon.

    This is not a model or network connection state, and an entry is never
    inferred from configuration by the status process itself.
    """
    limit = _timeout(timeout)
    daemon_report = daemon.get("report")
    daemon_report = daemon_report if isinstance(daemon_report, Mapping) else {}
    sock = daemon_report.get("socket")
    report: dict[str, Any] = {
        "socket": sock,
        "timeout_s": limit,
        "source": "daemon_secrets",
        "kind": "secret_providers",
        "note": ("provider availability/readiness reported by the running "
                 "daemon; configuration alone is not a connection"),
        "providers": [],
        "counts": {"known": None, "ready": None, "shown": 0},
        "truncated": False,
    }
    state = daemon.get("state")
    if state == "absent":
        return _section("absent", report)
    if state != "running" or not sock:
        return _section(
            "unknown", report,
            error=str(daemon.get("error") or "daemon state is unknown"))
    try:
        with DaemonClient(sock, timeout=limit) as client:
            reply = client.secret_providers()
    except DaemonUnavailable as exc:
        return _section("absent", report, error=str(exc))
    except DaemonTimeout as exc:
        degraded.append(_issue(
            "providers", "providers.unknown",
            f"secret providers did not answer within {limit:g}s: {exc}"))
        return _section("unknown", report, error=str(exc))
    except DaemonError as exc:
        degraded.append(_issue(
            "providers", "providers.unknown",
            f"secret providers could not be read: {exc}"))
        return _section("unknown", report, error=str(exc))
    if reply.get("ok") is False:
        error = str(reply.get("error") or "provider status failed")
        degraded.append(_issue(
            "providers", "providers.unknown",
            f"secret providers could not be read: {error}"))
        return _section("unknown", report, error=error)
    providers = reply.get("providers")
    if not isinstance(providers, list):
        degraded.append(_issue(
            "providers", "providers.unknown",
            "provider reply has no providers list"))
        return _section("unknown", report,
                        error="provider reply has no providers list")
    rows: list[dict[str, Any]] = []
    for item in providers[:PROVIDER_LIMIT]:
        if not isinstance(item, Mapping):
            continue
        requires = item.get("requires")
        rows.append({
            "name": _bounded_text(item.get("name"), 40),
            "enabled": item.get("enabled") is True,
            "available": item.get("available") is True,
            "ready": item.get("ready") is True,
            "requires": [str(value)[:120] for value in (requires or [])
                         if isinstance(value, str)][:8],
            "notes": _bounded_text(item.get("notes"), 200),
        })
    report["providers"] = rows
    report["counts"] = {
        "known": len(providers),
        "ready": sum(1 for item in providers
                     if isinstance(item, Mapping)
                     and item.get("ready") is True),
        "shown": len(rows),
    }
    report["truncated"] = len(providers) > PROVIDER_LIMIT
    return _section("ok", report)


def _snapshot_metrics_status(
    doctor: Mapping[str, Any], degraded: list[dict[str, str]],
) -> dict[str, Any]:
    """Snapshot size, freshness and counts already loaded by `doctor`.

    The counts come from doctor's existing snapshot load; status never builds,
    refreshes or rewrites the graph.
    """
    report: dict[str, Any] = {
        "path": None, "freshness": None, "size_bytes": None, "mtime": None,
        "age_s": None, "nodes": None, "edges": None, "counts_read": False,
        "counts_note": None,
    }
    doctor_report = doctor.get("report")
    snapshot = (doctor_report.get("snapshot")
                if isinstance(doctor_report, Mapping) else None)
    if not isinstance(snapshot, Mapping):
        return _section(
            "unknown", report,
            error=str(doctor.get("error")
                      or "diagnostics did not report a snapshot"))
    path = snapshot.get("path")
    report["path"] = str(path) if isinstance(path, str) else None
    freshness = snapshot.get("state")
    report["freshness"] = freshness if isinstance(freshness, str) else None
    report["nodes"] = _bounded_int(snapshot.get("nodes"))
    report["edges"] = _bounded_int(snapshot.get("edges"))
    if freshness == "missing":
        return _section("absent", report)
    if report["path"]:
        try:
            info = Path(report["path"]).lstat()
        except FileNotFoundError:
            return _section("absent", report)
        except OSError as exc:
            degraded.append(_issue(
                "snapshot", "snapshot.unreadable",
                f"snapshot could not be read: {exc}"))
            return _section("unknown", report, error=str(exc))
        if not stat.S_ISREG(info.st_mode):
            degraded.append(_issue(
                "snapshot", "snapshot.unreadable",
                f"snapshot is not a regular file: {report['path']}"))
            return _section(
                "unknown", report,
                error=f"snapshot is not a regular file: {report['path']}")
        report["size_bytes"] = info.st_size
        report["mtime"] = datetime.fromtimestamp(
            info.st_mtime, timezone.utc).isoformat(timespec="seconds")
        report["age_s"] = max(0.0, round(time.time() - info.st_mtime, 1))
    report["counts_read"] = (report["nodes"] is not None
                             and report["edges"] is not None)
    if not report["counts_read"]:
        report["counts_note"] = (
            "node/edge counts were not reported by the cheap snapshot load; "
            "size, mtime and freshness are still reported")
    if freshness not in ("fresh", "stale"):
        return _section(
            "unknown", report,
            error=str(snapshot.get("error") or
                      "snapshot freshness is unknown"))
    if freshness == "stale":
        return _section("degraded", report)
    return _section("ok", report)


def _profile_age(observed_at: Any, mtime: float) -> float:
    if isinstance(observed_at, str):
        try:
            stamp = datetime.fromisoformat(
                observed_at.replace("Z", "+00:00"))
        except ValueError:
            stamp = None
        if stamp is not None:
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            return max(0.0, round(
                time.time() - stamp.timestamp(), 1))
    return max(0.0, round(time.time() - mtime, 1))


def _read_profile(path: Path, limit: int) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, "rb") as handle:
        return handle.read(limit + 1)


def _empty_session_totals() -> dict[str, Any]:
    return {
        "model_calls": 0,
        "tokens": {key: None for key in SESSION_TOKEN_KEYS},
        "timing_ms": {key: None for key in SESSION_TIMING_KEYS},
        "coverage": {key: 0 for key in SESSION_COVERAGE_KEYS},
        "tool_calls": 0,
    }


def _sessions_status(
    root: Path, degraded: list[dict[str, str]],
) -> dict[str, Any]:
    """Bounded metadata summary of `.ozagia/session_profiles/`."""
    profile_dir = state_path(root, "session_profiles")
    report: dict[str, Any] = {
        "dir": str(profile_dir),
        "profiles": None,
        "profiles_read": 0,
        "malformed": 0,
        "unreadable": 0,
        "truncated": False,
        "newest_age_s": None,
        "models": [],
        "totals": _empty_session_totals(),
        "capture_diagnostics": {"captures": 0, "counters": {}},
        "sessions": [],
        "note": ("metadata only: no prompt, path, tool input/output, source "
                 "content or full session id is copied"),
    }
    try:
        info = profile_dir.lstat()
    except FileNotFoundError:
        return _section("absent", report)
    except OSError as exc:
        degraded.append(_issue(
            "sessions", "sessions.unreadable",
            f"session profile directory could not be read: {exc}"))
        return _section("unknown", report, error=str(exc))
    if stat.S_ISLNK(info.st_mode):
        degraded.append(_issue(
            "sessions", "sessions.unreadable",
            f"session profile directory is a symlink: {profile_dir}"))
        return _section(
            "unknown", report,
            error=f"session profile directory is a symlink: {profile_dir}")
    if not stat.S_ISDIR(info.st_mode):
        degraded.append(_issue(
            "sessions", "sessions.unreadable",
            f"session profile path is not a directory: {profile_dir}"))
        return _section(
            "unknown", report,
            error=f"session profile path is not a directory: {profile_dir}")
    entries: list[tuple[Path, os.stat_result]] = []
    scanned = 0
    try:
        with os.scandir(profile_dir) as listing:
            for entry in listing:
                if not entry.name.endswith(".json"):
                    continue
                scanned += 1
                if scanned > SESSION_PROFILE_SCAN_LIMIT:
                    report["truncated"] = True
                    break
                try:
                    entry_info = entry.stat(follow_symlinks=False)
                except OSError:
                    report["unreadable"] += 1
                    continue
                if stat.S_ISLNK(entry_info.st_mode) or not stat.S_ISREG(
                        entry_info.st_mode):
                    report["malformed"] += 1
                    continue
                if entry_info.st_size > SESSION_PROFILE_MAX_BYTES:
                    report["malformed"] += 1
                    continue
                entries.append((Path(entry.path), entry_info))
    except OSError as exc:
        degraded.append(_issue(
            "sessions", "sessions.unreadable",
            f"session profiles could not be listed: {exc}"))
        return _section("unknown", report, error=str(exc))
    report["profiles"] = scanned if not report["truncated"] else (
        SESSION_PROFILE_SCAN_LIMIT)
    entries.sort(key=lambda item: item[1].st_mtime, reverse=True)
    for path, entry_info in entries[:SESSION_PROFILE_LIMIT]:
        try:
            raw = _read_profile(path, SESSION_PROFILE_MAX_BYTES)
        except OSError:
            report["unreadable"] += 1
            continue
        if len(raw) > SESSION_PROFILE_MAX_BYTES:
            report["malformed"] += 1
            continue
        try:
            envelope = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            report["malformed"] += 1
            continue
        if not isinstance(envelope, Mapping):
            report["malformed"] += 1
            continue
        host = envelope.get("host_profile")
        host = host if isinstance(host, Mapping) else {}
        daemon = envelope.get("daemon_profile")
        daemon = daemon if isinstance(daemon, Mapping) else {}
        harness = envelope.get("harness") or host.get("harness")
        session = envelope.get("session_id") or host.get("session_id")
        observed_at = envelope.get("observed_at") or host.get("observed_at")
        age = _profile_age(observed_at, entry_info.st_mtime)
        if report["newest_age_s"] is None or age < report["newest_age_s"]:
            report["newest_age_s"] = age
        models = [name for name in (host.get("models") or [])
                  if isinstance(name, str) and name]
        for model in models[:SESSION_MODEL_LIMIT]:
            if model not in report["models"]:
                report["models"].append(model)
                if len(report["models"]) >= SESSION_MODEL_LIMIT:
                    break
        tokens = host.get("tokens") if isinstance(host.get("tokens"),
                                                  Mapping) else {}
        timing = host.get("timing_ms") if isinstance(host.get("timing_ms"),
                                                     Mapping) else {}
        coverage = host.get("coverage") if isinstance(host.get("coverage"),
                                                      Mapping) else {}
        totals = report["totals"]
        totals["model_calls"] += _bounded_int(
            host.get("model_calls")) or 0
        for key in SESSION_TOKEN_KEYS:
            amount = _bounded_int(tokens.get(key))
            if amount is not None:
                totals["tokens"][key] = (totals["tokens"][key] or 0) + amount
        for key in SESSION_TIMING_KEYS:
            amount = _bounded_int(timing.get(key))
            if amount is not None:
                totals["timing_ms"][key] = (
                    totals["timing_ms"][key] or 0) + amount
        for key in SESSION_COVERAGE_KEYS:
            totals["coverage"][key] += _bounded_int(coverage.get(key)) or 0
        tool_calls = _bounded_int(coverage.get("host_tool_calls"))
        if tool_calls is None:
            tool_calls = _bounded_int(daemon.get("tool_calls_observed")) or 0
        totals["tool_calls"] += tool_calls
        captures: list[dict[str, Any]] = []
        for record in host.get("capture_diagnostics") or []:
            if not isinstance(record, Mapping):
                continue
            capture_id = record.get("capture_id")
            counters = record.get("counters")
            counters = counters if isinstance(counters, Mapping) else {}
            clean = {
                key: _bounded_int(counters.get(key))
                for key in SESSION_CAPTURE_KEYS
                if _bounded_int(counters.get(key)) is not None
            }
            if not isinstance(capture_id, str) or not clean:
                continue
            captures.append({"capture_id": capture_id, "counters": clean})
        report["sessions"].append({
            "harness": _bounded_text(harness, 80),
            "label": _session_label(harness, session),
            "observed_at": _bounded_text(observed_at, 60),
            "age_s": age,
            "source": _bounded_text(
                host.get("source") or (
                    "daemon_events" if daemon else "unknown"), 80),
            "model_calls": _bounded_int(host.get("model_calls")) or 0,
            "models": models[:SESSION_MODEL_LIMIT],
            "tokens": {key: _bounded_int(tokens.get(key))
                       for key in SESSION_TOKEN_KEYS},
            "timing_ms": {key: _bounded_int(timing.get(key))
                          for key in SESSION_TIMING_KEYS},
            "tool_calls": tool_calls,
            "coverage": {key: _bounded_int(coverage.get(key))
                         for key in SESSION_COVERAGE_KEYS},
            "capture_diagnostics": captures,
        })
    report["profiles_read"] = len(report["sessions"])
    if len(entries) > SESSION_PROFILE_LIMIT:
        report["truncated"] = True
    # Deduplicate capture snapshots by id, keeping the largest counters.
    captures_by_id: dict[str, dict[str, int]] = {}
    for row in report["sessions"]:
        for capture in row["capture_diagnostics"]:
            capture_id = capture["capture_id"]
            merged = captures_by_id.setdefault(capture_id, {})
            for key, value in capture["counters"].items():
                merged[key] = max(merged.get(key, 0), value)
    aggregate = {key: 0 for key in SESSION_CAPTURE_KEYS}
    for counters in captures_by_id.values():
        for key, value in counters.items():
            aggregate[key] += value
    report["capture_diagnostics"] = {
        "captures": len(captures_by_id),
        "counters": aggregate,
    }
    problems = report["malformed"] + report["unreadable"]
    if problems:
        message = (f"{report['malformed']} malformed and "
                   f"{report['unreadable']} unreadable session profile(s)")
        degraded.append(_issue("sessions", "sessions.malformed", message))
        return _section("unknown", report, error=message)
    return _section("ok", report)


def _ownership_status(
    root: Path, degraded: list[dict[str, str]],
) -> dict[str, Any]:
    """Bounded dirty-file ownership from the edit and daemon journals."""
    journal = state_path(root, "journal.jsonl")
    report: dict[str, Any] = {
        "journal": str(journal),
        "files_scanned": 0,
        "counts": {"files": 0, "sessions": 0, "rows": 0},
        "rows": [],
        "truncated": False,
        "note": ("only journaled edits by a session are considered; a file "
                 "with no journal entry is not attributed"),
    }
    try:
        info = journal.lstat()
    except FileNotFoundError:
        return _section("absent", report)
    except OSError as exc:
        degraded.append(_issue(
            "ownership", "ownership.unreadable",
            f"edit journal could not be read: {exc}"))
        return _section("unknown", report, error=str(exc))
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        degraded.append(_issue(
            "ownership", "ownership.unreadable",
            f"edit journal is not a regular file: {journal}"))
        return _section(
            "unknown", report,
            error=f"edit journal is not a regular file: {journal}")
    try:
        with journal.open("rb") as handle:
            handle.read(1)
    except OSError as exc:
        degraded.append(_issue(
            "ownership", "ownership.unreadable",
            f"edit journal could not be read: {exc}"))
        return _section("unknown", report, error=str(exc))
    try:
        edit_rows = owners_module.edits(journal)
    except OSError as exc:
        degraded.append(_issue(
            "ownership", "ownership.unreadable",
            f"edit journal could not be read: {exc}"))
        return _section("unknown", report, error=str(exc))
    if not edit_rows:
        return _section("ok", report)
    latest: dict[str, float] = {}
    for edit in edit_rows[-OWNERSHIP_EDIT_SCAN:]:
        stamp = edit.get("epoch")
        if not isinstance(stamp, (int, float)):
            continue
        for rel in edit.get("files") or []:
            if not isinstance(rel, str) or not rel:
                continue
            latest[rel] = max(latest.get(rel, 0.0), float(stamp))
    files = sorted(latest, key=lambda rel: latest[rel],
                   reverse=True)[:OWNERSHIP_FILE_LIMIT]
    report["files_scanned"] = len(files)
    report["truncated"] = len(latest) > OWNERSHIP_FILE_LIMIT
    daemon_journal = state_path(root, "daemon_journal.jsonl")
    tracker = owners_module.Owners(root, daemon_journal=daemon_journal)
    rows: list[dict[str, Any]] = []
    dirty_files: set[str] = set()
    sessions: set[tuple[str, str]] = set()
    for rel in files:
        try:
            who = owners_module.who(root, rel, journal, live=tracker.live)
        except (OSError, ValueError):
            continue
        if who.get("dirty") is not True:
            continue
        for item in who.get("sessions") or []:
            if not isinstance(item, Mapping) or item.get(
                    "committed") is not False:
                continue
            harness = _bounded_text(item.get("harness"), 80)
            session = item.get("session")
            if not isinstance(harness, str) or not isinstance(session, str):
                continue
            dirty_files.add(rel)
            sessions.add((harness, session))
            if len(rows) < OWNERSHIP_ROW_LIMIT:
                rows.append({
                    "path": rel,
                    "harness": harness,
                    "label": _session_label(harness, session),
                    "edits": _bounded_int(item.get("edits")) or 0,
                    "last": _bounded_text(item.get("last"), 40),
                    "last_age": _bounded_text(item.get("last_age"), 40),
                    "live": item.get("live") is True,
                    "committed": False,
                })
    report["rows"] = rows
    report["counts"] = {
        "files": len(dirty_files),
        "sessions": len(sessions),
        "rows": len(rows),
    }
    report["truncated"] = (
        report["truncated"] or len(rows) >= OWNERSHIP_ROW_LIMIT)
    return _section("ok", report)


def _integrations_summary(report: Mapping[str, Any]) -> dict[str, Any] | None:
    section = report.get("integrations")
    if not isinstance(section, Mapping):
        return None
    hosts = section.get("hosts")
    hosts = hosts if isinstance(hosts, Mapping) else {}
    summary_hosts: dict[str, Any] = {}
    for key, row in list(hosts.items())[:INTEGRATION_LIMIT]:
        if not isinstance(row, Mapping):
            continue
        summary_hosts[key] = {
            "label": row.get("label"),
            "state": row.get("state"),
            "installed": row.get("installed"),
            "version": row.get("version"),
            "path": row.get("path"),
            "tested_range": row.get("tested_range"),
            "verification": row.get("verification"),
            "error": row.get("error"),
        }
    config = section.get("config")
    config = config if isinstance(config, Mapping) else {}
    summary_config: dict[str, Any] = {}
    from ozagia.integrations.versions import CONFIG_KEYS
    for key in CONFIG_KEYS:
        item = config.get(key)
        if not isinstance(item, Mapping):
            continue
        summary_config[key] = {
            "state": item.get("state"),
            "found": list(item.get("found") or [])[:INTEGRATION_LIMIT],
            "errors": list(item.get("errors") or [])[:INTEGRATION_LIMIT],
        }
    return {
        "note": section.get("note"),
        "hosts": summary_hosts,
        "config": summary_config,
        "warnings": list(section.get("warnings") or [])[:INTEGRATION_LIMIT],
    }


def _capabilities_summary(report: Mapping[str, Any]) -> dict[str, Any] | None:
    section = report.get("capabilities")
    if not isinstance(section, Mapping):
        return None
    catalogue = section.get("catalogue")
    catalogue = catalogue if isinstance(catalogue, Mapping) else {}
    audit = section.get("audit")
    audit = audit if isinstance(audit, Mapping) else {}
    counts = audit.get("counts")
    counts = counts if isinstance(counts, Mapping) else {}
    keys = (
        "capabilities", "capabilities_without_surfaces", "declared_surfaces",
        "present_surfaces", "observed_surfaces", "covered_surfaces",
        "undeclared_surfaces", "duplicate_declarations", "findings",
    )
    return {
        "state": section.get("state"),
        "catalogue": {
            "version": catalogue.get("version"),
            "hash": catalogue.get("hash"),
            "capabilities": catalogue.get("capabilities"),
            "domains": dict(catalogue.get("domains") or {}),
            "surfaces": catalogue.get("surfaces"),
        },
        "audit": {
            "state": audit.get("state"),
            "counts": {key: counts.get(key) for key in keys},
            "findings_by_kind": dict(audit.get("findings_by_kind") or {}),
            "findings_by_severity": dict(
                audit.get("findings_by_severity") or {}),
        },
        "error": section.get("error"),
        "warnings": list(section.get("warnings") or [])[
            :CAPABILITY_WARNING_LIMIT],
    }


def _summary_section(
    doctor: Mapping[str, Any], key: str,
) -> dict[str, Any]:
    summary = doctor.get(key)
    if isinstance(summary, Mapping):
        return _section("ok", summary)
    state = "unavailable" if doctor.get("state") == "unavailable" else "unknown"
    return _section(
        state, None, error=f"diagnostics did not report the {key} summary")


def _doctor_status(
    root: Path, degraded: list[dict[str, str]],
) -> dict[str, Any]:
    try:
        report = diagnose(root)
    except Exception as exc:  # noqa: BLE001 - diagnostics are one component
        degraded.append(_issue(
            "doctor", "doctor.unavailable",
            f"diagnostics could not run: {exc}"))
        return _section("unavailable", error=str(exc))
    integrations = _integrations_summary(report)
    capabilities = _capabilities_summary(report)
    snapshot = report.get("snapshot")
    snapshot = snapshot if isinstance(snapshot, Mapping) else {}
    snapshot_state = snapshot.get("state")
    if snapshot_state == "fresh":
        return _section("ok", report, integrations=integrations,
                        capabilities=capabilities)
    if snapshot_state not in SNAPSHOT_STATES:
        snapshot_state = "unknown"
    messages = {
        "missing": "no graph snapshot; run `ozagia index` to build one",
        "stale": "graph snapshot is stale; rebuild before trusting graph results",
        "invalid": f"graph snapshot is invalid: {snapshot.get('error') or 'unknown reason'}",
        "unknown": "graph snapshot freshness could not be verified",
    }
    degraded.append(_issue(
        "doctor", f"snapshot.{snapshot_state}", messages[snapshot_state]))
    return _section("degraded", report, integrations=integrations,
                    capabilities=capabilities)


def _read_review_manifest(path: Path, limit: int) -> bytes:
    """Read at most ``limit`` bytes without following a symlink."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, "rb") as handle:
        return handle.read(limit + 1)


def _review_manifest_summary(
    data: Any,
) -> tuple[dict[str, Any] | None, str | None]:
    """The bounded summary of a deep-review manifest, or why it is invalid."""
    if not isinstance(data, Mapping):
        return None, "manifest is not a JSON object"
    if data.get("schema") != REVIEW_MANIFEST_SCHEMA:
        return None, f"manifest schema is not {REVIEW_MANIFEST_SCHEMA!r}"
    status = data.get("status")
    if not isinstance(status, str):
        return None, "manifest status is not a string"
    flags: dict[str, bool] = {}
    for key in ("complete", "exhaustive", "truth_assessed"):
        value = data.get(key)
        if not isinstance(value, bool):
            return None, f"manifest {key} is not a boolean"
        flags[key] = value
    findings = data.get("findings")
    if isinstance(findings, bool) or not isinstance(findings, int) or findings < 0:
        return None, "manifest findings is not a non-negative integer"
    subjects = data.get("subjects")
    if not isinstance(subjects, Mapping):
        return None, "manifest subjects is not an object"
    names = list(subjects)
    if not all(isinstance(name, str) for name in names):
        return None, "manifest subjects has a non-string key"
    names.sort()
    subject_status: dict[str, str] = {}
    for name in names:
        row = subjects[name]
        if not isinstance(row, Mapping):
            return None, f"manifest subject {name!r} is not an object"
        subject_state = row.get("status")
        if not isinstance(subject_state, str):
            return None, f"manifest subject {name!r} has no string status"
        if len(subject_status) < REVIEW_SUBJECT_LIMIT:
            subject_status[name] = subject_state
    manifest_root = data.get("root")
    if manifest_root is not None and not isinstance(manifest_root, str):
        return None, "manifest root is not a string"
    return {
        "manifest_root": manifest_root,
        "status": status,
        "complete": flags["complete"],
        "exhaustive": flags["exhaustive"],
        "truth_assessed": flags["truth_assessed"],
        "findings": findings,
        "subject_count": len(names),
        "subjects": subject_status,
        "subjects_truncated": len(names) > REVIEW_SUBJECT_LIMIT,
    }, None


def _reviews_status(
    root: Path, degraded: list[dict[str, str]],
) -> dict[str, Any]:
    """Read the newest deep-review manifest, never writing or refreshing it."""
    state_root = root / ".ozagia"
    reviews_root = state_root / "reviews"
    report: dict[str, Any] = {
        "root": str(root),
        "reviews_root": str(reviews_root),
        "latest": None,
        "manifest": None,
        "manifest_root": None,
        "age_s": None,
        "status": None,
        "complete": None,
        "exhaustive": None,
        "truth_assessed": None,
        "findings": None,
        "subject_count": None,
        "subjects": {},
        "subjects_truncated": False,
    }

    def unknown(
        code: str, message: str, error: str | None = None,
    ) -> dict[str, Any]:
        degraded.append(_issue("reviews", code, message))
        return _section("unknown", report, error=error or message)

    try:
        state_info = state_root.lstat()
    except FileNotFoundError:
        return _section("absent", report)
    except OSError as exc:
        return unknown(
            "reviews.unreadable",
            f"state directory could not be read: {exc}", str(exc))
    if stat.S_ISLNK(state_info.st_mode):
        return unknown(
            "reviews.escape",
            f"state directory is a symlink; refusing to follow it: {state_root}")
    if not stat.S_ISDIR(state_info.st_mode):
        return unknown(
            "reviews.ambiguous",
            f"state path is not a directory: {state_root}")
    try:
        reviews_info = reviews_root.lstat()
    except FileNotFoundError:
        return _section("absent", report)
    except OSError as exc:
        return unknown(
            "reviews.unreadable",
            f"reviews directory could not be read: {exc}", str(exc))
    if stat.S_ISLNK(reviews_info.st_mode):
        return unknown(
            "reviews.escape",
            f"reviews directory is a symlink; refusing to follow it: "
            f"{reviews_root}")
    if not stat.S_ISDIR(reviews_info.st_mode):
        return unknown(
            "reviews.ambiguous",
            f"reviews path is not a directory: {reviews_root}")
    try:
        with os.scandir(reviews_root) as entries:
            latest_name: str | None = None
            count = 0
            for entry in entries:
                if not entry.name.startswith(REVIEW_DIR_PREFIX):
                    continue
                count += 1
                if count > REVIEW_SCAN_LIMIT:
                    return unknown(
                        "reviews.ambiguous",
                        f"more than {REVIEW_SCAN_LIMIT} review directories "
                        f"under {reviews_root}; the newest is ambiguous")
                if latest_name is None or entry.name > latest_name:
                    latest_name = entry.name
    except OSError as exc:
        return unknown(
            "reviews.unreadable",
            f"reviews directory could not be read: {exc}", str(exc))
    if latest_name is None:
        return _section("absent", report)
    latest = reviews_root / latest_name
    report["latest"] = str(latest)
    try:
        latest_info = latest.lstat()
    except OSError as exc:
        return unknown(
            "reviews.unreadable",
            f"newest review could not be read: {exc}", str(exc))
    if stat.S_ISLNK(latest_info.st_mode):
        return unknown(
            "reviews.escape",
            f"newest review is a symlink; refusing to follow it: {latest}")
    if not stat.S_ISDIR(latest_info.st_mode):
        return unknown(
            "reviews.ambiguous",
            f"newest review path is not a directory: {latest}")
    try:
        base = reviews_root.resolve(strict=True)
        resolved = latest.resolve(strict=True)
    except OSError as exc:
        return unknown(
            "reviews.unreadable",
            f"newest review could not be resolved: {exc}", str(exc))
    if resolved.parent != base:
        return unknown(
            "reviews.escape",
            f"newest review resolves outside {reviews_root}: {latest}")
    manifest = latest / REVIEW_MANIFEST_NAME
    report["manifest"] = str(manifest)
    try:
        manifest_info = manifest.lstat()
    except FileNotFoundError:
        return unknown(
            "reviews.manifest",
            f"newest review has no {REVIEW_MANIFEST_NAME}: {latest}",
            f"{REVIEW_MANIFEST_NAME} is missing")
    except OSError as exc:
        return unknown(
            "reviews.manifest",
            f"review manifest could not be read: {exc}", str(exc))
    if stat.S_ISLNK(manifest_info.st_mode):
        return unknown(
            "reviews.escape",
            f"review manifest is a symlink; refusing to follow it: {manifest}")
    if not stat.S_ISREG(manifest_info.st_mode):
        return unknown(
            "reviews.manifest",
            f"review manifest is not a regular file: {manifest}")
    if manifest_info.st_size > REVIEW_MANIFEST_MAX_BYTES:
        return unknown(
            "reviews.manifest",
            f"review manifest exceeds {REVIEW_MANIFEST_MAX_BYTES} bytes: "
            f"{manifest}")
    try:
        raw = _read_review_manifest(manifest, REVIEW_MANIFEST_MAX_BYTES)
    except OSError as exc:
        return unknown(
            "reviews.unreadable",
            f"review manifest could not be read: {exc}", str(exc))
    if len(raw) > REVIEW_MANIFEST_MAX_BYTES:
        return unknown(
            "reviews.manifest",
            f"review manifest grew beyond {REVIEW_MANIFEST_MAX_BYTES} bytes: "
            f"{manifest}")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return unknown(
            "reviews.manifest",
            f"review manifest is not UTF-8 JSON: {exc}", str(exc))
    summary, error = _review_manifest_summary(data)
    if summary is None:
        return unknown(
            "reviews.manifest",
            f"review manifest is invalid: {error}", error)
    report.update(summary)
    report["age_s"] = max(0.0, round(time.time() - manifest_info.st_mtime, 1))
    return _section("ok", report)


def _mesh_session_row(item: Mapping[str, Any]) -> dict[str, Any]:
    return {"key": item.get("key"), "harness": item.get("harness"),
            "session": item.get("session"),
            "effective_state": item.get("effective_state"),
            "exposure": item.get("exposure"), "role": item.get("role"),
            "unread": item.get("unread"),
            "active_leases": list(item.get("active_leases") or ())[:16]}


def _mesh_lease_row(item: Mapping[str, Any], self_key: Any) -> dict[str, Any]:
    resource = item.get("resource") or {}
    holder = item.get("holder") or {}
    return {"lease_id": item.get("lease_id"),
            "resource": {"kind": resource.get("kind"),
                         "id": resource.get("id"),
                         "mode": resource.get("mode")},
            "holder": {"harness": holder.get("harness"),
                       "session": holder.get("session"),
                       "key": holder.get("key")},
            "effective_state": item.get("effective_state"),
            "expires_at": item.get("expires_at"),
            "queued": len(item.get("queue") or ()),
            "holder_is_self": holder.get("key") == self_key}


def _mesh_link_row(item: Mapping[str, Any]) -> dict[str, Any]:
    peer = item.get("peer") or {}
    return {"link_id": item.get("link_id"),
            "peer": {"harness": peer.get("harness"),
                     "session": peer.get("session"),
                     "key": peer.get("key")},
            "relation": item.get("relation"),
            "reciprocal_relation": item.get("reciprocal_relation"),
            "state": item.get("state"),
            "expires_at": item.get("expires_at")}


def _mesh_delegation_row(item: Mapping[str, Any]) -> dict[str, Any]:
    return {"delegation_id": item.get("delegation_id"),
            "grantor": item.get("grantor"),
            "grantee": item.get("grantee"),
            "actions": list(item.get("actions") or ()),
            "scope": item.get("scope"),
            "state": item.get("state"),
            "expires_at": item.get("expires_at"),
            "remaining_uses": item.get("remaining_uses")}


def _mesh_status(root: Path) -> dict[str, Any]:
    """Read-only session-mesh section; never creates state or a daemon."""
    try:
        configured = mesh_module.mesh_configured(root)
        present = mesh_module.mesh_dir(root).is_dir()
    except Exception as exc:  # noqa: BLE001 - a broken mesh is unknown
        return _section("unknown", None, error=str(exc))
    if not configured and not present:
        return _section("absent", None)
    try:
        payload = mesh_module.status(
            root, harness=os.environ.get("OZAGIA_MESH_HARNESS"),
            session=os.environ.get("OZAGIA_MESH_SESSION"))
    except Exception as exc:  # noqa: BLE001 - never fail the whole snapshot
        return _section("unknown", None, error=str(exc))
    self_row = payload.get("self") or {}
    self_key = self_row.get("key")
    sessions = [item for item in (payload.get("sessions") or ())
                if item.get("key") != self_key]
    report = {
        "mesh_id": payload.get("mesh_id"),
        "repo_id": payload.get("repo_id"),
        "counts": {
            "sessions": payload.get("session_counts") or {},
            "leases": payload.get("lease_counts") or {},
            "links": payload.get("link_counts") or {},
            "delegations": payload.get("delegation_counts") or {},
        },
        "self": (_mesh_session_row(self_row) if self_row else None),
        "peers": [_mesh_session_row(item)
                  for item in sessions[:MESH_SESSION_LIMIT]],
        "links": [_mesh_link_row(item)
                  for item in (payload.get("links") or ())[:MESH_LINK_LIMIT]],
        "leases": [_mesh_lease_row(item, self_key)
                   for item in (payload.get("leases") or ())[:MESH_LEASE_LIMIT]],
        "delegations": [_mesh_delegation_row(item)
                        for item in (payload.get("delegations") or ())[
                            :MESH_DELEGATION_LIMIT]],
        "unread": int(self_row.get("unread") or 0),
        "errors": list(payload.get("errors") or ())[:MESH_ERROR_LIMIT],
    }
    return _section("ok", report)


def status(
    root: str | Path,
    *,
    path: str | Path | None = None,
    top: int = 10,
    timeout: float = DAEMON_TIMEOUT_S,
) -> dict[str, Any]:
    """Compose the read-only status snapshot for ``root``.

    Every component is optional to the composition: a failure is recorded in
    ``degraded`` and the report keeps its explicit state. Only a missing root
    or invalid project configuration is a hard error (``ok`` false).
    """
    root_path = Path(root).resolve()
    checked_at = _now()
    errors: list[dict[str, str]] = []
    degraded: list[dict[str, str]] = []

    if not root_path.is_dir():
        errors.append(_issue(
            "root", "root.missing",
            f"repository root does not exist: {root_path}"))

    config, project = _config_status(root_path)
    if config.get("state") == "invalid":
        errors.append(_issue(
            "config", "config.invalid",
            f"project configuration is invalid: {config.get('error')}"))

    workspace = _workspace_status(root_path, degraded)
    resources = _resources_status(root_path, path=path, top=top,
                                  degraded=degraded)
    files = _files_status(root_path, degraded)
    runtime = _runtime_status(root_path, project, degraded)
    daemon = _daemon_status(root_path, timeout=timeout, degraded=degraded)
    jobs = _jobs_status(daemon, timeout=timeout, degraded=degraded)
    admissions = _admissions_status(daemon)
    providers = _providers_status(daemon, timeout=timeout,
                                  degraded=degraded)
    doctor = _doctor_status(root_path, degraded)
    integrations = _summary_section(doctor, "integrations")
    capabilities = _summary_section(doctor, "capabilities")
    snapshot = _snapshot_metrics_status(doctor, degraded)
    sessions = _sessions_status(root_path, degraded)
    ownership = _ownership_status(root_path, degraded)
    reviews = _reviews_status(root_path, degraded)
    mesh = _mesh_status(root_path)

    return {
        "schema": SCHEMA,
        "checked_at": checked_at,
        "root": str(root_path),
        "ok": not errors,
        "errors": errors,
        "degraded": degraded,
        "workspace": workspace,
        "resources": resources,
        "files": files,
        "runtime": runtime,
        "daemon": daemon,
        "jobs": jobs,
        "admissions": admissions,
        "providers": providers,
        "doctor": doctor,
        "integrations": integrations,
        "capabilities": capabilities,
        "snapshot": snapshot,
        "sessions": sessions,
        "ownership": ownership,
        "reviews": reviews,
        "mesh": mesh,
        "config": config,
    }


def _text(value: Any, suffix: str = "") -> str:
    if value is None:
        return "unknown"
    return f"{value}{suffix}"


def format_status(report: Mapping[str, Any]) -> str:
    """A short text summary; the JSON report keeps every detail."""
    lines: list[str] = []
    errors = report.get("errors") or []
    degraded = report.get("degraded") or []
    head = "ok" if report.get("ok") else "errors"
    if degraded:
        head += f", {len(degraded)} degraded"
    lines.append(f"{head}: {report.get('root')}")
    lines.append(f"checked: {_text(report.get('checked_at'))}")

    workspace = report.get("workspace") or {}
    workspace_report = workspace.get("report")
    if isinstance(workspace_report, Mapping):
        worktrees = workspace_report.get("worktrees") or []
        findings = workspace_report.get("findings") or []
        error_findings = [item for item in findings
                          if item.get("level") == "error"]
        lines.append(
            f"workspace: {len(worktrees)} worktree(s), "
            f"{len(error_findings)} error finding(s)")
        for item in worktrees[:TEXT_WORKTREE_LIMIT]:
            dirty = item.get("dirty") or {}
            changed = sum(int(dirty.get(key, 0)) for key in
                          ("staged", "unstaged", "untracked", "conflicted"))
            base = item.get("base") or {}
            drift = ""
            if base.get("ahead") is not None or base.get("behind") is not None:
                drift = (f"  ahead={_text(base.get('ahead'))}"
                         f" behind={_text(base.get('behind'))}")
            lines.append(
                f"  {item.get('path')}  {item.get('branch') or '(detached)'}  "
                f"dirty={changed}{drift}")
        shown = 0
        for finding in findings:
            if finding.get("level") == "info":
                continue
            lines.append(f"  {finding.get('level')}: "
                         f"{finding.get('worktree')}: "
                         f"{finding.get('message')}")
            shown += 1
            if shown >= TEXT_FINDING_LIMIT:
                break
    else:
        lines.append(f"workspace: {workspace.get('state')} "
                     f"({_text(workspace.get('error'))})")

    resources = report.get("resources") or {}
    resources_report = resources.get("report")
    if isinstance(resources_report, Mapping):
        host = resources_report.get("host") or {}
        memory = host.get("memory") or {}
        load = host.get("load") or {}
        lines.append(
            "resources: "
            f"load={_text(load.get('1m'))}/{_text(load.get('5m'))}/"
            f"{_text(load.get('15m'))}  "
            f"memory={_text(memory.get('available_mb'))} MiB available of "
            f"{_text(memory.get('total_mb'))} MiB "
            f"({_text(memory.get('used_pct'))}% used)")
        for disk in (resources_report.get("disks") or [])[:5]:
            lines.append(
                f"  disk {disk.get('mount') or disk.get('path')}: "
                f"{_text(disk.get('free_gb'))} GiB free "
                f"({_text(disk.get('free_pct'))}%), "
                f"fstype={_text(disk.get('fstype'))}"
                + (" tmpfs" if disk.get("is_tmpfs") else ""))
    else:
        lines.append(f"resources: {resources.get('state')} "
                     f"({_text(resources.get('error'))})")

    files = report.get("files") or {}
    files_report = files.get("report")
    if isinstance(files_report, Mapping):
        counts = files_report.get("counts") or {}
        ledger = files_report.get("ledger") or {}
        new = files_report.get("new") or {}
        lines.append(
            f"files: {counts.get('records', 0)} record(s), "
            f"{counts.get('orphans', 0)} orphan(s), "
            f"{counts.get('new', 0)} new in "
            f"{_text(new.get('since_hours'), 'h')}; "
            f"ledger {'present' if ledger.get('present') else 'absent'}")
    else:
        lines.append(f"files: {files.get('state')} "
                     f"({_text(files.get('error'))})")

    daemon = report.get("daemon") or {}
    daemon_report = daemon.get("report") if isinstance(
        daemon.get("report"), Mapping) else {}
    socket = daemon_report.get("socket")
    if daemon.get("state") == "running":
        lines.append(
            f"daemon: running pid={_text(daemon_report.get('pid'))} "
            f"uptime={_text(daemon_report.get('uptime_s'), 's')} "
            f"socket={_text(socket)}")
        if daemon_report.get("stats_error"):
            lines.append(f"  stats: unknown ({daemon_report['stats_error']})")
    else:
        lines.append(
            f"daemon: {daemon.get('state')} socket={_text(socket)} "
            f"({_text(daemon.get('error'))})")

    jobs = report.get("jobs") or {}
    jobs_report = jobs.get("report")
    if isinstance(jobs_report, Mapping):
        counts = jobs_report.get("counts") or {}
        if jobs.get("state") == "absent":
            lines.append("jobs: absent (no daemon)")
        else:
            lines.append(
                f"jobs: {jobs.get('state')} "
                f"known={_text(counts.get('known'))} "
                f"running={_text(counts.get('running'))} "
                f"shown={_text(counts.get('shown'))}"
                + (" (truncated)" if jobs_report.get("truncated") else ""))
            for row in (jobs_report.get("jobs") or [])[:TEXT_RUNTIME_LIMIT]:
                if not isinstance(row, Mapping):
                    continue
                lines.append(
                    f"  {_text(row.get('id'))} {_text(row.get('state'))} "
                    f"pid={_text(row.get('pid'))} "
                    f"elapsed={_text(row.get('elapsed_s'), 's')} "
                    f"mem={_text(row.get('peak_memory_mb'), 'MB')} "
                    f"disk={_text(row.get('peak_disk_write_mb'), 'MB')}")
    else:
        lines.append(f"jobs: {jobs.get('state')} "
                     f"({_text(jobs.get('error'))})")

    admissions = report.get("admissions") or {}
    admissions_report = admissions.get("report")
    if isinstance(admissions_report, Mapping):
        counts = admissions_report.get("counts") or {}
        lines.append(
            f"admissions: {admissions.get('state')} "
            f"job_refusals={_text(counts.get('jobs_refused'))} "
            f"resource_refusals={_text(counts.get('resource_refusals'))} "
            f"resource_kills={_text(counts.get('jobs_resource_killed'))}")
    else:
        lines.append(f"admissions: {admissions.get('state')} "
                     f"({_text(admissions.get('error'))})")

    providers = report.get("providers") or {}
    providers_report = providers.get("report")
    if isinstance(providers_report, Mapping):
        counts = providers_report.get("counts") or {}
        lines.append(
            f"providers: {providers.get('state')} "
            f"known={_text(counts.get('known'))} "
            f"ready={_text(counts.get('ready'))} "
            f"shown={_text(counts.get('shown'))}")
    else:
        lines.append(f"providers: {providers.get('state')} "
                     f"({_text(providers.get('error'))})")

    runtime = report.get("runtime") or {}
    runtime_report = runtime.get("report")
    if isinstance(runtime_report, Mapping):
        items = runtime_report.get("items") or []
        lines.append(
            f"runtime: {len(runtime_report.get('services') or [])} service(s), "
            f"{len(runtime_report.get('tunnels') or [])} tunnel(s), "
            f"{len(runtime_report.get('processes') or [])} process(es)")
        for item in items[:TEXT_RUNTIME_LIMIT]:
            lines.append("  " + runtime_line(item))
        if len(items) > TEXT_RUNTIME_LIMIT:
            lines.append(f"  ... {len(items) - TEXT_RUNTIME_LIMIT} more")
    else:
        lines.append(f"runtime: {runtime.get('state')} "
                     f"({_text(runtime.get('error'))})")

    doctor = report.get("doctor") or {}
    doctor_report = doctor.get("report")
    if isinstance(doctor_report, Mapping):
        snapshot = doctor_report.get("snapshot") or {}
        lines.append(f"snapshot: {_text(snapshot.get('state'))} "
                     f"({_text(snapshot.get('path'))})")
    else:
        lines.append(f"doctor: {doctor.get('state')} "
                     f"({_text(doctor.get('error'))})")

    snapshot = report.get("snapshot") or {}
    snapshot_report = snapshot.get("report")
    if isinstance(snapshot_report, Mapping):
        lines.append(
            f"snapshot metrics: {snapshot.get('state')} "
            f"freshness={_text(snapshot_report.get('freshness'))} "
            f"size={_text(snapshot_report.get('size_bytes'), 'B')} "
            f"age={_text(snapshot_report.get('age_s'), 's')} "
            f"nodes={_text(snapshot_report.get('nodes'))} "
            f"edges={_text(snapshot_report.get('edges'))}"
            + ("" if snapshot_report.get("counts_read")
               else " (counts not read)"))
    else:
        lines.append(f"snapshot metrics: {snapshot.get('state')} "
                     f"({_text(snapshot.get('error'))})")

    sessions = report.get("sessions") or {}
    sessions_report = sessions.get("report")
    if isinstance(sessions_report, Mapping):
        totals = sessions_report.get("totals") or {}
        if sessions.get("state") == "absent":
            lines.append("sessions: absent (no session profiles)")
        else:
            lines.append(
                f"sessions: {sessions.get('state')} "
                f"profiles={_text(sessions_report.get('profiles'))} "
                f"newest={_text(sessions_report.get('newest_age_s'), 's')} "
                f"models={len(sessions_report.get('models') or [])} "
                f"model_calls={_text(totals.get('model_calls'))} "
                f"tool_calls={_text(totals.get('tool_calls'))}"
                + (" (truncated)" if sessions_report.get("truncated")
                   else ""))
    else:
        lines.append(f"sessions: {sessions.get('state')} "
                     f"({_text(sessions.get('error'))})")

    ownership = report.get("ownership") or {}
    ownership_report = ownership.get("report")
    if isinstance(ownership_report, Mapping):
        if ownership.get("state") == "absent":
            lines.append("ownership: absent (no edit journal)")
        else:
            counts = ownership_report.get("counts") or {}
            lines.append(
                f"ownership: {ownership.get('state')} "
                f"dirty_files={_text(counts.get('files'))} "
                f"sessions={_text(counts.get('sessions'))}")
            for row in (ownership_report.get("rows") or [])[:TEXT_FINDING_LIMIT]:
                if not isinstance(row, Mapping):
                    continue
                lines.append(
                    f"  {row.get('path')}: {_text(row.get('label'))} "
                    f"(live={_text(row.get('live'))}, "
                    f"{_text(row.get('edits'))} edit(s), "
                    f"{_text(row.get('last_age'))})")
    else:
        lines.append(f"ownership: {ownership.get('state')} "
                     f"({_text(ownership.get('error'))})")

    integrations = report.get("integrations") or {}
    integrations_report = integrations.get("report")
    if isinstance(integrations_report, Mapping):
        hosts = integrations_report.get("hosts") or {}
        rows = []
        for key, item in list(hosts.items())[:INTEGRATION_LIMIT]:
            if not isinstance(item, Mapping):
                continue
            version = item.get("version") or (
                "unknown" if item.get("installed") else "")
            suffix = f" {version}" if version else ""
            rows.append(f"{key}={_text(item.get('state'))}{suffix}")
        lines.append("integrations: " + (", ".join(rows) or "none reported"))
        config = integrations_report.get("config") or {}
        config_rows = [
            f"{key}={_text(item.get('state'))}"
            for key, item in config.items()
            if isinstance(item, Mapping)
        ]
        lines.append("integration config: "
                     + (", ".join(config_rows) or "none reported"))
    else:
        lines.append(f"integrations: {integrations.get('state')} "
                     f"({_text(integrations.get('error'))})")

    capabilities = report.get("capabilities") or {}
    capabilities_report = capabilities.get("report")
    if isinstance(capabilities_report, Mapping):
        catalogue = capabilities_report.get("catalogue") or {}
        audit = capabilities_report.get("audit") or {}
        counts = audit.get("counts") or {}
        lines.append(
            f"capabilities: {_text(capabilities_report.get('state'))} "
            f"catalogue={_text(catalogue.get('version'))} "
            f"capabilities={_text(counts.get('capabilities'))} "
            f"surfaces={_text(counts.get('declared_surfaces'))} "
            f"findings={_text(counts.get('findings'))}")
        warnings = capabilities_report.get("warnings") or []
        for warning in warnings[:TEXT_FINDING_LIMIT]:
            if isinstance(warning, Mapping):
                lines.append(f"  capability warning: {warning.get('message')}")
        if len(warnings) > TEXT_FINDING_LIMIT:
            lines.append(
                f"  ... {len(warnings) - TEXT_FINDING_LIMIT} more capability "
                f"warning(s)")
    else:
        lines.append(f"capabilities: {capabilities.get('state')} "
                     f"({_text(capabilities.get('error'))})")

    reviews = report.get("reviews") or {}
    reviews_report = reviews.get("report")
    if isinstance(reviews_report, Mapping):
        if reviews.get("state") == "absent":
            lines.append("reviews: absent (no deep-review report)")
        else:
            lines.append(
                f"reviews: {_text(reviews.get('state'))} "
                f"latest={_text(reviews_report.get('latest'))} "
                f"age={_text(reviews_report.get('age_s'), 's')} "
                f"status={_text(reviews_report.get('status'))} "
                f"findings={_text(reviews_report.get('findings'))} "
                f"subjects={_text(reviews_report.get('subject_count'))}")
    else:
        lines.append(f"reviews: {reviews.get('state')} "
                     f"({_text(reviews.get('error'))})")

    mesh = report.get("mesh") or {}
    mesh_report = mesh.get("report")
    if isinstance(mesh_report, Mapping):
        counts = mesh_report.get("counts") or {}
        session_counts = counts.get("sessions") or {}
        lease_counts = counts.get("leases") or {}
        self_row = mesh_report.get("self") or {}
        lines.append(
            f"mesh: {_text(mesh_report.get('mesh_id'))} "
            f"self={_text(self_row.get('harness'))}:"
            f"{_text(self_row.get('session'))} "
            f"role={_text(self_row.get('role'))} "
            f"sessions live={session_counts.get('live', 0)} "
            f"stale={session_counts.get('stale', 0)} "
            f"leases active={lease_counts.get('active', 0)} "
            f"queued={lease_counts.get('queued', 0)} "
            f"unread={mesh_report.get('unread', 0)}")
    elif mesh.get("state") == "absent":
        lines.append("mesh: off (no state or [mesh] configuration)")
    else:
        lines.append(f"mesh: {mesh.get('state')} ({_text(mesh.get('error'))})")

    config = report.get("config") or {}
    config_path_text = config.get("path") or "no configuration file"
    lines.append(f"config: {_text(config.get('state'))} ({config_path_text})")

    for issue in degraded:
        lines.append(f"degraded: {issue.get('component')}: "
                     f"{issue.get('code')}: {issue.get('message')}")
    for issue in errors:
        lines.append(f"error: {issue.get('component')}: "
                     f"{issue.get('code')}: {issue.get('message')}")
    return "\n".join(lines)


__all__ = ["SCHEMA", "format_status", "status"]
