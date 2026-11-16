"""Read-only diagnostics for a local repository and optional tools."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ozagia.config import ConfigError, config_path, load_project_config, state_path


def diagnose(root: str | Path) -> dict[str, Any]:
    """Inspect local configuration and snapshot without building or starting servers."""
    root = Path(root).resolve()
    result: dict[str, Any] = {"root": str(root), "root_exists": root.is_dir()}
    cfg_path = config_path(root)
    cfg_label = str(cfg_path) if cfg_path else None
    cfg_present = bool(cfg_path and cfg_path.is_file())
    try:
        cfg = load_project_config(root)
        result["config"] = {"path": cfg_label, "present": cfg_present,
                            "valid": True, "adapters_configured": list(cfg.adapters)}
    except (ConfigError, OSError) as exc:
        result["config"] = {"path": cfg_label, "present": cfg_present,
                            "valid": False, "error": str(exc)}

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
    return result
