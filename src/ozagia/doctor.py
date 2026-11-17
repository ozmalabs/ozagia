"""Read-only diagnostics for a local repository and optional tools."""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from ozagia.config import (
    ConfigError, config_sources, load_project_config, state_path,
)

#: Bound the per-key settings detail in the doctor report.
SETTINGS_NON_DEFAULT_LIMIT = 50
#: Bound the versioned-config keys the project loader did not map.
CONFIG_UNMAPPED_LIMIT = 20


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
            result["snapshot"] = {"path": str(dump), "state": status["freshness"],
                                  "details": status}
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
    return result
