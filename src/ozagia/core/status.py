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
)
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
    doctor = _doctor_status(root_path, degraded)
    integrations = _summary_section(doctor, "integrations")
    capabilities = _summary_section(doctor, "capabilities")
    reviews = _reviews_status(root_path, degraded)

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
        "doctor": doctor,
        "integrations": integrations,
        "capabilities": capabilities,
        "reviews": reviews,
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
