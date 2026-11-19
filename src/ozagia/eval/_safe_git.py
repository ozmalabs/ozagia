"""Git helpers for synthetic evaluation checkouts.

Evaluation fixtures are data. Do not let copied repository metadata, Git
templates, or host Git configuration turn that data into hooks or filters.
"""
from __future__ import annotations

import os
from pathlib import Path


def command(root: Path, *args: str) -> tuple[list[str], dict[str, str]]:
    """Build a Git command isolated from external config and hook execution."""
    settings = root.parent / ".ozagia-eval-git"
    settings.mkdir(parents=True, exist_ok=True)
    empty_config = settings / "empty.config"
    empty_config.touch(exist_ok=True)
    templates = settings / "templates"
    templates.mkdir(exist_ok=True)
    hooks = settings / "hooks"
    hooks.mkdir(exist_ok=True)

    env = dict(os.environ)
    for key in tuple(env):
        if (key in {"GIT_CONFIG", "GIT_CONFIG_PARAMETERS", "GIT_DIR",
                    "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR",
                    "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
                    "GIT_PREFIX"}
                or key.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))):
            env.pop(key, None)
    env.update({
        "GIT_CONFIG_COUNT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_SYSTEM": str(empty_config),
        "GIT_CONFIG_GLOBAL": str(empty_config),
        "GIT_TEMPLATE_DIR": str(templates),
        "GIT_OPTIONAL_LOCKS": "0",
    })
    return (["git", "-c", f"core.hooksPath={hooks}",
             "-c", "core.fsmonitor=false", "-C", str(root), *args], env)
