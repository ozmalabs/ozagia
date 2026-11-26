"""Read-only diagnostics for a local repository and optional tools."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

from ozagia.config import (
    ConfigError, config_sources, load_project_config, state_path,
)
from ozagia.integrations import versions as integration_versions

#: Bound the per-key settings detail in the doctor report.
SETTINGS_NON_DEFAULT_LIMIT = 50
#: Bound the versioned-config keys the project loader did not map.
CONFIG_UNMAPPED_LIMIT = 20
#: A host binary gets this long to answer ``--version``.
HOST_VERSION_TIMEOUT_S = integration_versions.VERSION_TIMEOUT_S
#: Bound error text inside one integration row.
HOST_ERROR_LIMIT = 300
#: One page of audit findings is enough for the counts; none are copied.
CAPABILITY_AUDIT_LIMIT = 1


def _settings_report(root: Path) -> dict[str, Any]:
    """Registry paths, validity and a bounded per-key source summary.

    A broken settings file is reported here, not raised: ``doctor`` must keep
    diagnosing the rest of the workspace.
    """
    from ozagia.core import settings as settings_registry

    report: dict[str, Any] = {
        "valid": False,
        "keys": 0,
        "registry": {"valid": False, "keys": 0, "error": None},
        "locations": {},
        "source_counts": {},
        "non_default": [],
        "non_default_truncated": False,
        "error": None,
        "errors": [],
    }
    try:
        report["locations"] = settings_registry.locations(root)
    except (settings_registry.SettingsError, OSError, ValueError) as exc:
        report["errors"].append(str(exc))

    registry_error: str | None = None
    try:
        registry = settings_registry.schema()
    except Exception as exc:  # noqa: BLE001 - report, never fail doctor
        registry_error = str(exc)
        report["errors"].append(registry_error)
    else:
        report["registry"] = {
            "valid": True, "keys": len(registry), "error": None}
    if registry_error is not None:
        report["registry"] = {
            "valid": False, "keys": 0, "error": registry_error}

    try:
        values = settings_registry.values(root)
    except (settings_registry.SettingsError, OSError, ValueError) as exc:
        report["error"] = str(exc)
        if str(exc) not in report["errors"]:
            report["errors"].append(str(exc))
        return report

    counts: dict[str, int] = {}
    non_default: list[dict[str, str]] = []
    for key, item in values.items():
        source = str(item.get("source") or "unknown")
        counts[source] = counts.get(source, 0) + 1
        if source != "default":
            non_default.append({"key": key, "source": source})
    report["valid"] = bool(report["registry"]["valid"])
    report["keys"] = len(values)
    report["source_counts"] = dict(sorted(counts.items()))
    report["non_default"] = non_default[:SETTINGS_NON_DEFAULT_LIMIT]
    report["non_default_truncated"] = (
        len(non_default) > SETTINGS_NON_DEFAULT_LIMIT)
    return report


def _short_error(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}".replace("\n", " ")[:HOST_ERROR_LIMIT]


def _host_path(spec: integration_versions.HostVersion, root: Path) -> str | None:
    """Find a host binary, optionally preferring the project/ozagia `.venv`."""
    if spec.search_venv:
        checkout = Path(__file__).resolve().parents[2]
        for base in (root / ".venv" / "bin", checkout / ".venv" / "bin"):
            candidate = base / spec.command
            try:
                if candidate.is_file():
                    return str(candidate)
            except OSError:
                continue
    return shutil.which(spec.command)


def _probe_host_version(
    path: str, timeout: float,
) -> tuple[str | None, str | None]:
    """Run ``path --version`` only; never a model, daemon or tool call."""
    try:
        proc = subprocess.run(
            [path, "--version"],
            capture_output=True, text=True, timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return None, f"`{path} --version` timed out after {timeout:g} s"
    except OSError as exc:
        return None, _short_error(exc)
    output = f"{proc.stdout or ''}\n{proc.stderr or ''}"
    version = integration_versions.parse_version(output)
    if version is None:
        return None, (
            f"`{path} --version` printed no version (exit {proc.returncode})"
        )[:HOST_ERROR_LIMIT]
    return version, None


def _host_report(
    spec: integration_versions.HostVersion, root: Path,
) -> dict[str, Any]:
    path = _host_path(spec, root)
    row: dict[str, Any] = {
        "label": spec.label,
        "command": spec.command,
        "state": "not_installed",
        "installed": False,
        "version": None,
        "path": None,
        "tested_versions": list(spec.tested_versions),
        "tested_range": spec.tested_range,
        "verification": spec.verification,
        "matrix": f"{integration_versions.MATRIX_DOC}#{spec.matrix_anchor}",
        "error": None,
    }
    if path is None:
        return row
    row["installed"] = True
    row["path"] = path
    version, error = _probe_host_version(path, HOST_VERSION_TIMEOUT_S)
    row["version"] = version
    row["error"] = error
    row["state"] = integration_versions.classify(spec, version)
    return row


def _integration_warnings(
    hosts: dict[str, dict[str, Any]],
) -> list[dict[str, str]]:
    warnings: list[dict[str, str]] = []
    for key in integration_versions.HOST_ORDER:
        row = hosts.get(key) or {}
        state = row.get("state")
        if state == "unknown":
            warnings.append({
                "component": "integrations",
                "code": f"integration.{key}.unknown",
                "message": (f"{row.get('label') or key}: installed at "
                            f"{row.get('path')}, but its version could not be "
                            f"determined: {row.get('error')}"),
            })
        elif state == "installed_untested_version":
            spec = integration_versions.HOSTS[key]
            if integration_versions.version_matches(spec, row.get("version")):
                message = (f"{row.get('label') or key} "
                           f"{row.get('version')} is in the offline-tested "
                           f"range ({spec.tested_range}), but the live "
                           f"integration is not verified.")
            else:
                message = (f"{row.get('label') or key} "
                           f"{row.get('version')} is outside the tested "
                           f"versions ({spec.tested_range}); the integration "
                           f"is not verified for it.")
            warnings.append({
                "component": "integrations",
                "code": f"integration.{key}.version",
                "message": message,
            })
    return warnings


def _integrations_report(root: Path) -> dict[str, Any]:
    from ozagia.integrations.presence import config_presence

    hosts = {
        key: _host_report(integration_versions.HOSTS[key], root)
        for key in integration_versions.HOST_ORDER
    }
    try:
        config = config_presence(root)
    except Exception as exc:  # noqa: BLE001 - one section must not stop doctor
        config = {"state": "unknown", "error": _short_error(exc)}
    warnings = _integration_warnings(hosts)
    for key in integration_versions.CONFIG_KEYS:
        item = config.get(key) or {}
        if item.get("state") == "unknown":
            errors = "; ".join(item.get("errors") or []) or "unknown reason"
            warnings.append({
                "component": "integrations",
                "code": f"integration.{key}.config",
                "message": (f"{key} integration configuration could not be "
                            f"read: {errors}"),
            })
    return {
        "note": ("Binary and configuration presence only. No host agent or "
                 "model was started; see docs/integrations.md for the "
                 "verification level of each surface."),
        "hosts": hosts,
        "config": config,
        "warnings": warnings,
    }


def _capabilities_report(root: Path) -> dict[str, Any]:
    report: dict[str, Any] = {
        "state": "unknown",
        "catalogue": {"version": None, "hash": None, "capabilities": None,
                      "domains": {}, "surfaces": None},
        "audit": {"state": None, "counts": {}, "findings_by_kind": {},
                  "findings_by_severity": {}},
        "error": None,
        "warnings": [],
    }
    try:
        from ozagia.core import self_graph as self_graph_module
        audit = self_graph_module.audit_self_graph(
            root=root, limit=CAPABILITY_AUDIT_LIMIT)
    except Exception as exc:  # noqa: BLE001 - report, never fail doctor
        report["error"] = _short_error(exc)
        report["warnings"].append({
            "component": "capabilities",
            "code": "capabilities.unknown",
            "message": f"capability catalogue audit did not run: {report['error']}",
        })
        return report

    catalogue = audit.get("catalogue") or {}
    counts = audit.get("counts") or {}
    state = str(audit.get("state") or "unknown")
    keys = (
        "capabilities", "capabilities_without_surfaces", "declared_surfaces",
        "present_surfaces", "observed_surfaces", "covered_surfaces",
        "undeclared_surfaces", "duplicate_declarations", "findings",
    )
    report.update({
        "state": state,
        "catalogue": {
            "version": catalogue.get("version"),
            "hash": catalogue.get("hash"),
            "capabilities": catalogue.get("capabilities"),
            "domains": dict(catalogue.get("domains") or {}),
            "surfaces": catalogue.get("surfaces"),
        },
        "audit": {
            "state": state,
            "counts": {key: counts.get(key) for key in keys},
            "findings_by_kind": dict(counts.get("findings_by_kind") or {}),
            "findings_by_severity": dict(
                counts.get("findings_by_severity") or {}),
            "truncation": dict(audit.get("truncation") or {}),
        },
    })
    findings = int(counts.get("findings") or 0)
    if state != "current":
        report["warnings"].append({
            "component": "capabilities",
            "code": "capabilities.audit_state",
            "message": f"capability catalogue audit state is {state}",
        })
    if findings:
        severity = ", ".join(
            f"{name}={count}"
            for name, count in sorted(
                (counts.get("findings_by_severity") or {}).items()))
        report["warnings"].append({
            "component": "capabilities",
            "code": "capabilities.findings",
            "message": (f"capability catalogue audit reports {findings} "
                        f"finding(s)" + (f" ({severity})" if severity else "")),
        })
    return report


def diagnose(root: str | Path) -> dict[str, Any]:
    """Inspect local configuration and snapshot without building or starting servers."""
    root = Path(root).resolve()
    result: dict[str, Any] = {"root": str(root), "root_exists": root.is_dir()}
    cfg = None
    config: dict[str, Any] = {
        "path": None, "present": False, "valid": False,
        "legacy_path": None, "legacy_kind": None,
        "repo_path": None, "repo_exists": False,
        "active_source": None, "precedence": [], "sources": [],
        "unmapped": [], "unmapped_count": 0, "unmapped_truncated": False,
    }
    try:
        sources = config_sources(root)
    except (ConfigError, OSError) as exc:
        config["error"] = str(exc)
    else:
        config.update({
            "legacy_path": (str(sources.legacy)
                            if sources.legacy is not None else None),
            "legacy_kind": sources.legacy_kind,
            "repo_path": str(sources.repo_config),
            "repo_exists": sources.repo_exists,
            "active_source": sources.active_kind,
            "path": str(sources.active) if sources.active is not None else None,
            "present": bool(sources.active is not None
                            and sources.active.is_file()),
            "precedence": list(sources.precedence),
            "sources": [str(path) for path in sources.paths],
        })
        try:
            cfg = load_project_config(root)
        except (ConfigError, OSError) as exc:
            config["error"] = str(exc)
        else:
            unmapped = [{"key": key, "reason": reason}
                        for key, reason in cfg.unmapped]
            config.update({
                "valid": True,
                "adapters_configured": list(cfg.adapters),
                "migration": cfg.migration.to_dict(),
                "unmapped": unmapped[:CONFIG_UNMAPPED_LIMIT],
                "unmapped_count": len(unmapped),
                "unmapped_truncated": len(unmapped) > CONFIG_UNMAPPED_LIMIT,
            })
    result["config"] = config

    try:
        result["settings"] = _settings_report(root)
    except Exception as exc:  # noqa: BLE001 - one section must not stop doctor
        result["settings"] = {
            "valid": False, "keys": 0,
            "registry": {"valid": False, "keys": 0, "error": str(exc)},
            "locations": {}, "source_counts": {}, "non_default": [],
            "non_default_truncated": False,
            "error": str(exc), "errors": [str(exc)],
        }

    dump = state_path(root, "graph.json")
    result["snapshot"] = {"path": str(dump), "state": "missing"}
    if dump.exists():
        try:
            from ozagia.core import memory as memory_module
            from ozagia.core.memory import DevMemory
            memory = DevMemory(dump=dump, journal=state_path(root, "journal.jsonl"), root=root)
            status = memory.staleness()
            config_error = None
            if not result["config"]["valid"]:
                config_error = "repository index configuration could not be loaded"
            elif (tuple(cfg.source_dirs) != tuple(memory_module.SOURCE_DIRS)
                    or tuple(cfg.test_dirs) != tuple(memory_module.TEST_DIRS)
                    or set(cfg.memory_skip_dirs) != set(memory_module.SKIP_PARTS)):
                config_error = "repository index configuration differs from the loaded memory configuration"
            if config_error:
                status = dict(status)
                status["verification_errors"] = [*status.get("verification_errors", []), config_error]
                if status["freshness"] == "fresh":
                    status["freshness"] = "unknown"
            nodes = getattr(memory, "nodes", None)
            edges = getattr(memory, "edges", None)
            result["snapshot"] = {
                "path": str(dump), "state": status["freshness"],
                "details": status,
                "nodes": len(nodes) if isinstance(nodes, (dict, list)) else None,
                "edges": len(edges) if isinstance(edges, list) else None,
            }
        except (OSError, ValueError, KeyError, TypeError) as exc:
            result["snapshot"] = {"path": str(dump), "state": "invalid", "error": str(exc)}

    from ozagia.index.tag_languages import LANGUAGES, backend_status
    from ozagia.lsp import available_servers
    result["parsers"] = {lang: backend_status(lang) for lang in
                         sorted({entry["language"] for entry in LANGUAGES.values()})}
    result["language_servers"] = available_servers(root)["languages"]
    result["language_server_note"] = "Binary presence only; server startup and readiness are not tested."
    result["external_tools"] = {
        name: {"available": bool(path), "path": path}
        for name in ("ssh", "aws", "session-manager-plugin", "kubectl")
        for path in (shutil.which(name),)
    }
    result["external_tool_note"] = (
        "Binary presence only; no relay, cloud or cluster command is run."
    )

    try:
        result["integrations"] = _integrations_report(root)
    except Exception as exc:  # noqa: BLE001 - one section must not stop doctor
        result["integrations"] = {
            "state": "unknown",
            "note": "integration detection did not run",
            "hosts": {},
            "config": {"state": "unknown", "error": _short_error(exc)},
            "warnings": [],
        }
    try:
        result["capabilities"] = _capabilities_report(root)
    except Exception as exc:  # noqa: BLE001 - one section must not stop doctor
        result["capabilities"] = {
            "state": "unknown",
            "catalogue": {"version": None, "hash": None, "capabilities": None,
                          "domains": {}, "surfaces": None},
            "audit": {"state": None, "counts": {}, "findings_by_kind": {},
                      "findings_by_severity": {}},
            "error": _short_error(exc),
            "warnings": [],
        }

    warnings: list[dict[str, str]] = []
    for key in ("integrations", "capabilities"):
        section = result.get(key) or {}
        warnings.extend(section.get("warnings") or [])
    result["warnings"] = warnings
    return result
