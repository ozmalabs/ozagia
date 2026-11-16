"""Typed settings registry and the versioned repository configuration.

Locations
---------
Every setting resolves from one of five layers, lowest precedence first:

1. the built-in default in this module;
2. the user config, ``$OZAGIA_USER_CONFIG`` or
   ``~/.config/ozagia/config.toml``;
3. the repository config, ``<root>/ozagia/config.toml``;
4. an environment value (the small explicit map below);
5. an explicit override passed to :func:`values` or :func:`get`.

``ozagia/`` (no leading dot) is versioned project configuration. It holds data
files only: it must not contain ``__init__.py`` and it is never imported as a
Python package. ``.ozagia/`` is ignored local state (the graph snapshot,
journal, jobs, resource history, edit backups and file ledger).
``$OZAGIA_STATE_DIR`` moves the state directory; it is a location selector,
not a setting value.

Environment
-----------
Location selectors (they change *where* ozagia reads, not a registered value):

``OZAGIA_ROOT``
    The target repository root (``ozagia.config``).
``OZAGIA_STATE_DIR``
    The state directory, absolute or relative to the root.
``OZAGIA_USER_CONFIG``
    The user config file instead of ``~/.config/ozagia/config.toml``.

Value overrides (parsed with the setting's type; an invalid value is an error
that names the variable):

.. code-block:: text

    OZAGIA_RESOURCES_ENABLED        resources.enabled
    OZAGIA_WORKTREES_ROOT           worktrees.root
    OZAGIA_WORKTREES_AVOID_TMPFS    worktrees.avoid_tmpfs
    OZAGIA_MARKDOWN_HISTORY         markdown.history
    OZAGIA_MODELS_DEFAULT           models.default
    OZAGIA_MODELS_LOCAL_ONLY        models.local_only
    OZAGIA_TASKS_MAX_WALL_S         tasks.max_wall_s
    OZAGIA_TESTING_MODE             testing.mode
    OZAGIA_TESTING_FALLBACK         testing.fallback
    OZAGIA_TESTING_MAX_TESTS        testing.max_tests
    OZAGIA_TESTING_TIMEOUT_S        testing.timeout_s
    OZAGIA_TESTING_CAPTURED         testing.captured
    OZAGIA_UI_THEME                 ui.theme

TOML layout
-----------
The canonical layout follows the setting keys. The ``resources`` and
``worktrees`` groups use the nested tables the resource policy already reads:

.. code-block:: toml

    [index]
    source_dirs = ["."]
    test_dirs = ["tests"]
    docs = ["README.md", "docs/**/*.md"]
    skip_dirs = []

    [resources]
    enabled = true
    reserve_memory_pct = 10

    [resources.limits]
    job_memory_mb = 2048
    job_pids = 256

    [resources.admission]
    min_free_memory_pct = 15

    [resources.worktrees]
    root = "../ozagia-wt"
    avoid_tmpfs = true

    [markdown]
    history = "history"

The flat forms ``[resources] job_memory_mb = 2048`` and ``[worktrees]
root = "../ozagia-wt"`` are accepted as aliases when reading. Writes update an
existing alias in place and otherwise use the canonical nested table.

Unknown keys are rejected with the file path, the dotted key and a close
suggestion. Writes are preview-first: :func:`set_value` defaults to
``dry_run=True`` and returns the unified diff; applying writes a temporary
file in the same directory and replaces the target atomically. Unrelated
TOML, including comments, is left untouched.
"""
from __future__ import annotations

import difflib
import json
import math
import os
import posixpath
import re
import stat
import tempfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from ozagia.config import repo_root, state_dir

SCHEMA = 1
REPO_CONFIG_DIRNAME = "ozagia"
REPO_CONFIG_FILENAME = "config.toml"
ENV_USER_CONFIG = "OZAGIA_USER_CONFIG"
ENV_STATE_DIR = "OZAGIA_STATE_DIR"

#: Test-selection modes, least to most inclusive. `ozagia.core.testing` uses
#: the same order when resolving path overrides.
TESTING_MODES = ("none", "smoke", "affected", "package", "all")
TESTING_FALLBACKS = ("all", "refuse", "affected_with_warning")
# Open-ended override tables: values are modes. They are not scalar settings,
# so they are validated here and collected by :func:`testing_overrides`.
TESTING_TASKS_TABLE = "testing.tasks"
TESTING_PATHS_TABLE = "testing.paths"

#: Location selectors: they change where ozagia reads, not a registered value.
ENV_LOCATIONS: dict[str, str] = {
    "OZAGIA_ROOT": "root",
    ENV_STATE_DIR: "state_dir",
    ENV_USER_CONFIG: "user_config",
}

#: Setting value overrides: variable -> registry key.
ENV_SETTINGS: dict[str, str] = {
    "OZAGIA_RESOURCES_ENABLED": "resources.enabled",
    "OZAGIA_WORKTREES_ROOT": "worktrees.root",
    "OZAGIA_WORKTREES_AVOID_TMPFS": "worktrees.avoid_tmpfs",
    "OZAGIA_MARKDOWN_HISTORY": "markdown.history",
    "OZAGIA_MODELS_DEFAULT": "models.default",
    "OZAGIA_MODELS_LOCAL_ONLY": "models.local_only",
    "OZAGIA_TASKS_MAX_WALL_S": "tasks.max_wall_s",
    "OZAGIA_TESTING_MODE": "testing.mode",
    "OZAGIA_TESTING_FALLBACK": "testing.fallback",
    "OZAGIA_TESTING_MAX_TESTS": "testing.max_tests",
    "OZAGIA_TESTING_TIMEOUT_S": "testing.timeout_s",
    "OZAGIA_TESTING_CAPTURED": "testing.captured",
    "OZAGIA_UI_THEME": "ui.theme",
}

_TYPES = frozenset({"string", "integer", "number", "boolean", "string_list"})
_SCOPES = frozenset({"user", "repo", "both"})


class SettingsError(ValueError):
    """A setting, config file or write is invalid."""


# --------------------------------------------------------------------------- #
# value checks
# --------------------------------------------------------------------------- #
def _text(value: Any, *, max_len: int = 256, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError("must be a string")
    text = value.strip()
    if not text and not allow_empty:
        raise ValueError("must not be empty")
    if len(text) > max_len:
        raise ValueError(f"must be at most {max_len} characters")
    if any(ord(char) < 32 or ord(char) == 127 for char in text):
        raise ValueError("must not contain control characters")
    if any(0xD800 <= ord(char) <= 0xDFFF for char in text):
        raise ValueError("must be valid Unicode")
    return text


def _list(value: Any) -> list[Any]:
    if isinstance(value, (str, bytes, Mapping)) or not isinstance(
            value, (list, tuple)):
        raise ValueError("must be a list")
    return list(value)


def _dedupe(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _relative_path(value: Any, *, max_len: int = 512,
                   allow_root: bool = False) -> str:
    text = _text(value, max_len=max_len)
    path = text.replace("\\", "/")
    if path.startswith("/") or re.match(r"^[A-Za-z]:/", path):
        raise ValueError("must be relative to the repository root")
    parts: list[str] = []
    for part in path.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise ValueError("must stay inside the repository root")
        parts.append(part)
    if not parts:
        if allow_root:
            return "."
        raise ValueError("must name a path inside the repository root")
    return "/".join(parts)


def _check_source_dirs(value: Any) -> list[str]:
    items = _list(value)
    if not items:
        raise ValueError("must contain at least one directory")
    if len(items) > 100:
        raise ValueError("must contain at most 100 directories")
    result = []
    for item in items:
        path = _relative_path(item, allow_root=True)
        if any(char in path for char in "*?[]"):
            raise ValueError("entries must be directories, not glob patterns")
        result.append(path)
    return _dedupe(result)


def _check_test_dirs(value: Any) -> list[str]:
    items = _list(value)
    if len(items) > 100:
        raise ValueError("must contain at most 100 directories")
    result = []
    for item in items:
        path = _relative_path(item, allow_root=True)
        if any(char in path for char in "*?[]"):
            raise ValueError("entries must be directories, not glob patterns")
        result.append(path)
    return _dedupe(result)


def _check_docs(value: Any) -> list[str]:
    items = _list(value)
    if len(items) > 200:
        raise ValueError("must contain at most 200 patterns")
    result = []
    for item in items:
        pattern = _text(item, max_len=256).replace("\\", "/")
        if pattern.startswith("/") or re.match(r"^[A-Za-z]:/", pattern):
            raise ValueError("globs must be relative to the repository root")
        if any(part == ".." for part in pattern.split("/")):
            raise ValueError("globs must stay inside the repository root")
        result.append(pattern)
    return _dedupe(result)


def _check_skip_dirs(value: Any) -> list[str]:
    items = _list(value)
    if len(items) > 200:
        raise ValueError("must contain at most 200 names")
    result = []
    for item in items:
        name = _text(item, max_len=128)
        if "/" in name or "\\" in name or name in (".", ".."):
            raise ValueError("entries must be single directory names")
        result.append(name)
    return _dedupe(result)


def _check_root_allow(value: Any) -> list[str]:
    items = _list(value)
    if len(items) > 100:
        raise ValueError("must contain at most 100 names")
    result = []
    for item in items:
        name = _text(item, max_len=128)
        if "/" in name or "\\" in name or name in (".", ".."):
            raise ValueError("entries must be root-level file names")
        result.append(name)
    return _dedupe(result)


def _check_worktree_root(value: Any) -> str:
    text = _text(value, max_len=1024)
    path = text.replace("\\", "/")
    if path in ("", "."):
        raise ValueError("must name a worktree directory")
    return posixpath.normpath(path)


def _check_markdown_history(value: Any) -> str:
    path = _relative_path(value)
    if any(char in path for char in "*?[]"):
        raise ValueError("must be a directory, not a glob pattern")
    return path


def _check_trash(value: Any) -> str:
    return _relative_path(value)


def _check_bool(value: Any) -> bool:
    if not isinstance(value, bool):
        raise ValueError("must be true or false")
    return value


_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+~-]{0,199}\Z")
_APERTURE_ID = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")


def _check_model_id(value: Any) -> str:
    text = _text(value, max_len=200, allow_empty=True)
    if text and not _MODEL_ID.fullmatch(text):
        raise ValueError("must be a model id such as provider/model, or empty")
    return text


def _check_aperture(value: Any) -> str:
    text = _text(value, max_len=64)
    if not _APERTURE_ID.fullmatch(text):
        raise ValueError("must be a lowercase aperture id")
    return text


def _choice(*choices: str) -> Callable[[Any], str]:
    def check(value: Any) -> str:
        text = _text(value, max_len=64)
        if text not in choices:
            raise ValueError("must be one of "
                             + ", ".join(repr(choice) for choice in choices))
        return text
    return check


def _integer_between(low: int, high: int) -> Callable[[Any], int]:
    def check(value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("must be an integer")
        if not low <= value <= high:
            raise ValueError(f"must be between {low} and {high}")
        return value
    return check


def _number_between(low: float, high: float) -> Callable[[Any], float]:
    def check(value: Any) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("must be a number")
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("must be a finite number")
        if not low <= number <= high:
            raise ValueError(f"must be between {low:g} and {high:g}")
        return number
    return check


_TEST_MODE = _choice(*TESTING_MODES)
_TASK_CLASS = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,63}\Z")


def _check_testing_task_name(value: Any) -> str:
    name = _text(value, max_len=64)
    if not _TASK_CLASS.fullmatch(name):
        raise ValueError("must be a task class id matching "
                         "[A-Za-z][A-Za-z0-9_.-]{0,63}")
    return name


def _check_testing_path_pattern(value: Any) -> str:
    pattern = _text(value, max_len=256).replace("\\", "/")
    if pattern.startswith("/") or re.match(r"^[A-Za-z]:/", pattern):
        raise ValueError("must be relative to the repository root")
    if any(part == ".." for part in pattern.split("/")):
        raise ValueError("must stay inside the repository root")
    if "\0" in pattern:
        raise ValueError("must not contain control characters")
    return pattern


#: Dynamic table -> (key validation, value validation, description).
_DYNAMIC_TABLES: dict[str, tuple[
    Callable[[Any], str], Callable[[Any], str], str]] = {
    TESTING_TASKS_TABLE: (
        _check_testing_task_name, _TEST_MODE,
        "Test-selection mode per bounded task class.",
    ),
    TESTING_PATHS_TABLE: (
        _check_testing_path_pattern, _TEST_MODE,
        "Test-selection mode per repository path glob.",
    ),
}


# --------------------------------------------------------------------------- #
# registry
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Setting:
    """One registered setting.

    ``key`` is the API name. ``section`` and ``name`` are the canonical TOML
    location; ``aliases`` are additional accepted TOML locations. ``scope``
    names the layer that may define the value: ``user``, ``repo`` or ``both``.
    ``restart_required`` is advisory: the daemon or a reindex must run again.
    """

    key: str
    type: str
    default: Any
    scope: str
    description: str
    section: str
    name: str
    validation: str
    check: Callable[[Any], Any] = field(repr=False, compare=False)
    restart_required: bool = False
    aliases: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if self.type not in _TYPES:
            raise ValueError(f"{self.key}: unknown type {self.type!r}")
        if self.scope not in _SCOPES:
            raise ValueError(f"{self.key}: unknown scope {self.scope!r}")


_SETTINGS: tuple[Setting, ...] = (
    Setting(
        "index.source_dirs", "string_list", (".",), "repo",
        "Directories indexed into the graph; repository-relative.",
        section="index", name="source_dirs",
        validation="one or more repository-relative directories",
        check=_check_source_dirs, restart_required=True,
    ),
    Setting(
        "index.test_dirs", "string_list", ("tests",), "repo",
        "Directories whose test files become Test nodes.",
        section="index", name="test_dirs",
        validation="repository-relative directories (may be empty)",
        check=_check_test_dirs, restart_required=True,
    ),
    Setting(
        "index.docs", "string_list", ("README.md", "docs/**/*.md"), "repo",
        "Documentation globs ingested as Doc nodes.",
        section="index", name="docs",
        validation="repository-relative glob patterns (may be empty)",
        check=_check_docs, restart_required=True,
    ),
    Setting(
        "index.skip_dirs", "string_list", (), "repo",
        "Extra directory names skipped by the indexers.",
        section="index", name="skip_dirs",
        validation="single directory names (may be empty)",
        check=_check_skip_dirs, restart_required=True,
    ),
    Setting(
        "resources.enabled", "boolean", True, "both",
        "Observe host resources and apply admission checks.",
        section="resources", name="enabled",
        validation="true or false",
        check=_check_bool,
    ),
    Setting(
        "resources.sample_s", "number", 2.0, "both",
        "Sampling interval for host resource observations, in seconds.",
        section="resources", name="sample_s",
        validation="a number between 0.1 and 3600",
        check=_number_between(0.1, 3600.0),
    ),
    Setting(
        "resources.watchdog", "string", "auto", "both",
        "Resource enforcement mode.",
        section="resources", name="watchdog",
        validation="one of auto, cgroup, rlimit, watch or off",
        check=_choice("auto", "cgroup", "rlimit", "watch", "off"),
    ),
    Setting(
        "resources.history", "boolean", True, "both",
        "Keep bounded run history for admission estimates.",
        section="resources", name="history",
        validation="true or false",
        check=_check_bool,
    ),
    Setting(
        "resources.reserve_memory_pct", "number", 10.0, "both",
        "Host memory kept free for the OS and the user, in percent.",
        section="resources", name="reserve_memory_pct",
        validation="a number between 0 and 80",
        check=_number_between(0.0, 80.0),
    ),
    Setting(
        "resources.reserve_disk_pct", "number", 5.0, "both",
        "Disk space kept free for the OS and the user, in percent.",
        section="resources", name="reserve_disk_pct",
        validation="a number between 0 and 80",
        check=_number_between(0.0, 80.0),
    ),
    Setting(
        "resources.reserve_disk_gb", "number", 1.0, "both",
        "Disk space kept free for the OS and the user, in GiB.",
        section="resources", name="reserve_disk_gb",
        validation="a number between 0 and 1000000",
        check=_number_between(0.0, 1_000_000.0),
    ),
    Setting(
        "resources.min_free_memory_pct", "number", 15.0, "both",
        "Minimum free memory required before new work starts, in percent.",
        section="resources.admission", name="min_free_memory_pct",
        validation="a number between 0 and 100",
        check=_number_between(0.0, 100.0),
        aliases=(("resources", "min_free_memory_pct"),),
    ),
    Setting(
        "resources.min_free_disk_pct", "number", 10.0, "both",
        "Minimum free disk required before new work starts, in percent.",
        section="resources.admission", name="min_free_disk_pct",
        validation="a number between 0 and 100",
        check=_number_between(0.0, 100.0),
        aliases=(("resources", "min_free_disk_pct"),),
    ),
    Setting(
        "resources.min_free_disk_gb", "number", 2.0, "both",
        "Minimum free disk required before new work starts, in GiB.",
        section="resources.admission", name="min_free_disk_gb",
        validation="a number between 0 and 1000000",
        check=_number_between(0.0, 1_000_000.0),
        aliases=(("resources", "min_free_disk_gb"),),
    ),
    Setting(
        "resources.wait_max_s", "integer", 300, "both",
        "Longest explicit wait for resources, in seconds.",
        section="resources.admission", name="wait_max_s",
        validation="an integer between 0 and 86400",
        check=_integer_between(0, 86_400),
        aliases=(("resources", "wait_max_s"),),
    ),
    Setting(
        "resources.job_memory_mb", "integer", 2048, "both",
        "Default memory limit for a job or bounded task, in MiB.",
        section="resources.limits", name="job_memory_mb",
        validation="an integer between 1 and 16777216",
        check=_integer_between(1, 16_777_216),
        aliases=(("resources", "job_memory_mb"),),
    ),
    Setting(
        "resources.job_disk_mb", "integer", 4096, "both",
        "Default disk budget for a job or bounded task, in MiB.",
        section="resources.limits", name="job_disk_mb",
        validation="an integer between 1 and 16777216",
        check=_integer_between(1, 16_777_216),
        aliases=(("resources", "job_disk_mb"),),
    ),
    Setting(
        "resources.job_pids", "integer", 256, "both",
        "Default process-count limit for a job or bounded task.",
        section="resources.limits", name="job_pids",
        validation="an integer between 1 and 1000000",
        check=_integer_between(1, 1_000_000),
        aliases=(("resources", "job_pids"),),
    ),
    Setting(
        "worktrees.root", "string", "../ozagia-wt", "repo",
        "Default directory for feature worktrees, relative to the repository "
        "or absolute.",
        section="resources.worktrees", name="root",
        validation="a repository-relative or absolute directory path",
        check=_check_worktree_root,
        aliases=(("worktrees", "root"),),
    ),
    Setting(
        "worktrees.min_free_gb", "number", 5.0, "both",
        "Free disk required before creating or using a worktree, in GiB.",
        section="resources.worktrees", name="min_free_gb",
        validation="a number between 0 and 1000000",
        check=_number_between(0.0, 1_000_000.0),
        aliases=(("worktrees", "min_free_gb"),),
    ),
    Setting(
        "worktrees.avoid_tmpfs", "boolean", True, "both",
        "Refuse or warn when a worktree is on tmpfs.",
        section="resources.worktrees", name="avoid_tmpfs",
        validation="true or false",
        check=_check_bool,
        aliases=(("worktrees", "avoid_tmpfs"),),
    ),
    Setting(
        "markdown.history", "string", "history", "repo",
        "Versioned directory for dated one-off Markdown.",
        section="markdown", name="history",
        validation="a repository-relative directory, not a glob",
        check=_check_markdown_history,
    ),
    Setting(
        "markdown.history_index", "boolean", True, "repo",
        "Generate an index for the history directory.",
        section="markdown", name="history_index",
        validation="true or false",
        check=_check_bool,
    ),
    Setting(
        "markdown.new_file_budget", "integer", 3, "both",
        "New Markdown files allowed per session or task before a reason is "
        "required.",
        section="markdown", name="new_file_budget",
        validation="an integer between 0 and 1000",
        check=_integer_between(0, 1000),
    ),
    Setting(
        "markdown.duplicate_threshold", "number", 0.85, "both",
        "Similarity above which a new note is reported as a near-duplicate.",
        section="markdown", name="duplicate_threshold",
        validation="a number between 0 and 1",
        check=_number_between(0.0, 1.0),
    ),
    Setting(
        "markdown.root_allow", "string_list",
        ("README.md", "CHANGELOG.md", "LICENSE", "CONTRIBUTING.md",
         "AGENTS.md", "SECURITY.md"), "repo",
        "Root-level Markdown files that may exist outside the routing rules.",
        section="markdown", name="root_allow",
        validation="root-level file names",
        check=_check_root_allow,
    ),
    Setting(
        "files.ledger", "boolean", True, "both",
        "Record files created or changed by ozagia sessions.",
        section="files", name="ledger",
        validation="true or false",
        check=_check_bool,
    ),
    Setting(
        "files.trash", "string", ".ozagia/trash", "repo",
        "Repository-relative directory that cleanup moves files to.",
        section="files", name="trash",
        validation="a repository-relative path",
        check=_check_trash,
    ),
    Setting(
        "files.retention_days", "integer", 30, "both",
        "Days to keep scratch or trash entries before cleanup suggests "
        "removal.",
        section="files", name="retention_days",
        validation="an integer between 0 and 36500",
        check=_integer_between(0, 36_500),
    ),
    Setting(
        "models.default", "string", "", "both",
        "Default model id as provider/model; empty uses the host default.",
        section="models", name="default",
        validation="a model id, or empty for the host default",
        check=_check_model_id,
    ),
    Setting(
        "models.fallback", "string", "", "both",
        "Fallback model id used when the default is unavailable; empty "
        "disables it.",
        section="models", name="fallback",
        validation="a model id, or empty for no fallback",
        check=_check_model_id,
    ),
    Setting(
        "models.local_only", "boolean", False, "both",
        "Refuse remote model providers.",
        section="models", name="local_only",
        validation="true or false",
        check=_check_bool,
    ),
    Setting(
        "tasks.default_budget_tokens", "integer", 40_000, "both",
        "Default token budget for a bounded task.",
        section="tasks", name="default_budget_tokens",
        validation="an integer between 1 and 1000000000",
        check=_integer_between(1, 1_000_000_000),
    ),
    Setting(
        "tasks.max_wall_s", "integer", 900, "both",
        "Default wall-clock limit for a bounded task, in seconds.",
        section="tasks", name="max_wall_s",
        validation="an integer between 1 and 604800",
        check=_integer_between(1, 604_800),
    ),
    Setting(
        "review.min_reviewers", "integer", 1, "both",
        "Minimum independent reviewers for a review run.",
        section="review", name="min_reviewers",
        validation="an integer between 0 and 16",
        check=_integer_between(0, 16),
    ),
    Setting(
        "review.require_clean", "boolean", True, "both",
        "Require a clean worktree before review or merge checks.",
        section="review", name="require_clean",
        validation="true or false",
        check=_check_bool,
    ),
    Setting(
        "testing.mode", "string", "affected", "both",
        "Default test-selection mode for bounded tasks.",
        section="testing", name="mode",
        validation="one of none, smoke, affected, package, all",
        check=_TEST_MODE,
    ),
    Setting(
        "testing.fallback", "string", "all", "both",
        "What a plan does when a changed file has no linked test or the graph "
        "is missing or stale.",
        section="testing", name="fallback",
        validation="one of all, refuse, affected_with_warning",
        check=_choice(*TESTING_FALLBACKS),
    ),
    Setting(
        "testing.max_tests", "integer", 50, "both",
        "Maximum number of test files a plan selects.",
        section="testing", name="max_tests",
        validation="an integer between 1 and 100000",
        check=_integer_between(1, 100_000),
    ),
    Setting(
        "testing.timeout_s", "integer", 300, "both",
        "Timeout a caller should apply to the planned test run, in seconds.",
        section="testing", name="timeout_s",
        validation="an integer between 1 and 86400",
        check=_integer_between(1, 86_400),
    ),
    Setting(
        "testing.captured", "string", "all", "both",
        "Default mode for an interactive captured pytest.",
        section="testing", name="captured",
        validation="one of none, smoke, affected, package, all",
        check=_TEST_MODE,
    ),
    Setting(
        "ui.default_aperture", "string", "architecture", "both",
        "Aperture shown first in a graph view.",
        section="ui", name="default_aperture",
        validation="a lowercase aperture id",
        check=_check_aperture,
    ),
    Setting(
        "ui.theme", "string", "auto", "both",
        "Graph and console theme.",
        section="ui", name="theme",
        validation="one of auto, light or dark",
        check=_choice("auto", "light", "dark"),
    ),
)


def _build_indexes() -> tuple[
        dict[str, Setting], dict[tuple[str, str], Setting], set[str]]:
    by_key: dict[str, Setting] = {}
    by_storage: dict[tuple[str, str], Setting] = {}
    sections: set[str] = set()
    for setting in _SETTINGS:
        if setting.key in by_key:
            raise RuntimeError(f"duplicate setting {setting.key!r}")
        by_key[setting.key] = setting
        for storage in ((setting.section, setting.name), *setting.aliases):
            if storage in by_storage:
                raise RuntimeError(f"duplicate TOML location {storage!r}")
            by_storage[storage] = setting
            sections.add(storage[0])
    return by_key, by_storage, sections


_BY_KEY, _BY_STORAGE, _KNOWN_SECTIONS = _build_indexes()
_ENV_BY_KEY = {key: name for name, key in ENV_SETTINGS.items()}
_KEY_CANDIDATES = sorted(
    set(_BY_KEY) | {f"{section}.{name}" for section, name in _BY_STORAGE})


# --------------------------------------------------------------------------- #
# locations
# --------------------------------------------------------------------------- #
def _resolve_root(root: str | Path | None) -> Path:
    return Path(root).resolve() if root is not None else repo_root()


def repo_config_path(root: str | Path | None = None) -> Path:
    """``<root>/ozagia/config.toml`` (the versioned repository config)."""
    return _resolve_root(root) / REPO_CONFIG_DIRNAME / REPO_CONFIG_FILENAME


def user_config_path() -> Path:
    """``$OZAGIA_USER_CONFIG`` or ``~/.config/ozagia/config.toml``."""
    env = os.environ.get(ENV_USER_CONFIG)
    if env:
        return Path(env).expanduser().resolve()
    try:
        home = Path.home()
    except RuntimeError as exc:  # pragma: no cover - unusual environment
        raise SettingsError(f"cannot determine the home directory: {exc}") from exc
    return (home / ".config" / REPO_CONFIG_DIRNAME / REPO_CONFIG_FILENAME).resolve()


def locations(root: str | Path | None = None) -> dict[str, Any]:
    """The config/state locations and whether the config files exist."""
    root_p = _resolve_root(root)
    repo = repo_config_path(root_p)
    user = user_config_path()
    return {
        "root": str(root_p),
        "repo_config": str(repo),
        "repo_config_exists": repo.is_file(),
        "user_config": str(user),
        "user_config_exists": user.is_file(),
        "state_dir": str(state_dir(root_p)),
    }


# --------------------------------------------------------------------------- #
# reading and validation
# --------------------------------------------------------------------------- #
def _suggest_key(text: str) -> str | None:
    matches = difflib.get_close_matches(text, _KEY_CANDIDATES, n=1,
                                        cutoff=0.55)
    return matches[0] if matches else None


def _dynamic_setting(text: str) -> Setting | None:
    """A synthesized setting for a ``[testing.tasks]`` or ``[testing.paths]``
    entry, or None when the key is not in one of those tables."""
    for table, (key_check, value_check, description) in _DYNAMIC_TABLES.items():
        prefix = table + "."
        if not text.startswith(prefix):
            continue
        name = text[len(prefix):]
        try:
            name = key_check(name)
        except ValueError as exc:
            raise SettingsError(f"{text}: {exc}") from None
        return Setting(
            key=text, type="string", default="", scope="both",
            description=description, section=table, name=name,
            validation="a test-selection mode",
            check=value_check,
        )
    return None


def _setting_for_key(key: Any) -> Setting:
    if not isinstance(key, str) or not key.strip():
        raise SettingsError("a setting key must be a non-empty string")
    text = key.strip()
    setting = _BY_KEY.get(text)
    if setting is None and "." in text:
        section, _, name = text.rpartition(".")
        setting = _BY_STORAGE.get((section, name))
    if setting is None:
        setting = _dynamic_setting(text)
    if setting is None:
        message = f"unknown setting {text!r}"
        suggestion = _suggest_key(text)
        if suggestion:
            message += f"; did you mean {suggestion!r}?"
        raise SettingsError(message)
    return setting


def _validate(setting: Setting, value: Any, *, where: str) -> Any:
    try:
        return setting.check(value)
    except SettingsError:
        raise
    except (TypeError, ValueError) as exc:
        raise SettingsError(f"{where}: {exc}") from exc


def _json_value(setting: Setting, value: Any) -> Any:
    if setting.type == "string_list":
        return list(value)
    if setting.type == "number":
        return float(value)
    return value


def _coerce_text(setting: Setting, text: str, *, where: str) -> Any:
    if setting.type == "string":
        return _validate(setting, text, where=where)
    if setting.type == "integer":
        try:
            value = int(text.strip(), 10)
        except (TypeError, ValueError):
            raise SettingsError(
                f"{where}: must be an integer, got {text!r}") from None
        return _validate(setting, value, where=where)
    if setting.type == "number":
        try:
            value = float(text.strip())
        except (TypeError, ValueError):
            raise SettingsError(
                f"{where}: must be a number, got {text!r}") from None
        return _validate(setting, value, where=where)
    if setting.type == "boolean":
        lowered = text.strip().lower()
        if lowered in {"true", "1", "yes", "on"}:
            value = True
        elif lowered in {"false", "0", "no", "off"}:
            value = False
        else:
            raise SettingsError(
                f"{where}: must be true or false, got {text!r}")
        return _validate(setting, value, where=where)
    if setting.type == "string_list":
        return _validate(setting, _parse_list_text(text), where=where)
    raise SettingsError(f"{where}: unsupported setting type {setting.type!r}")


def _parse_list_text(text: str) -> list[Any]:
    stripped = text.strip()
    if not stripped:
        return []
    if stripped.startswith("["):
        try:
            data = tomllib.loads(f"value = {stripped}\n")
        except tomllib.TOMLDecodeError as exc:
            raise SettingsError(f"invalid list: {exc}") from exc
        value = data.get("value")
        if not isinstance(value, list):
            raise SettingsError("must be a list")
        return value
    items = []
    for part in stripped.split(","):
        item = part.strip()
        if len(item) >= 2 and item[0] == item[-1] and item[0] in "'\"":
            item = item[1:-1]
        if item:
            items.append(item)
    return items


def _coerce_value(setting: Setting, value: Any, *, where: str) -> Any:
    if setting.type == "string_list" and isinstance(value, str):
        return _validate(setting, _parse_list_text(value), where=where)
    if setting.type != "string" and isinstance(value, str):
        return _coerce_text(setting, value, where=where)
    return _validate(setting, value, where=where)


def _read_toml(path: Path) -> tuple[dict[str, Any], str]:
    if path.is_dir():
        raise SettingsError(f"{path}: is a directory, not a TOML file")
    if not path.exists():
        return {}, ""
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise SettingsError(f"{path}: {exc}") from exc
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SettingsError(f"{path}: not UTF-8: {exc}") from exc
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise SettingsError(f"{path}: {exc}") from exc
    return data, text


def _is_known_table(dotted: str) -> bool:
    if dotted in _DYNAMIC_TABLES:
        return True
    return any(dotted == section or section.startswith(dotted + ".")
               for section in _KNOWN_SECTIONS)


def _unknown_section(path: Path, dotted: str) -> str:
    message = f"{path}: unknown section [{dotted}]"
    matches = difflib.get_close_matches(dotted, sorted(_KNOWN_SECTIONS), n=1,
                                        cutoff=0.55)
    if matches:
        message += f"; did you mean [{matches[0]}]?"
    return message


def _unknown_leaf(path: Path, dotted: str) -> str:
    message = f"{path}: unknown setting {dotted!r}"
    suggestion = _suggest_key(dotted)
    if suggestion:
        message += f"; did you mean {suggestion!r}?"
    return message


def _parse_layer(data: Mapping[str, Any], path: Path, layer: str) -> tuple[
        dict[str, Any], dict[str, tuple[str, str]]]:
    """Validate one TOML layer and return values plus their TOML locations."""
    values: dict[str, Any] = {}
    where_defined: dict[str, str] = {}
    locations: dict[str, tuple[str, str]] = {}

    def walk(table: Mapping[str, Any], prefix: str) -> None:
        for name, raw in table.items():
            if not isinstance(name, str):
                raise SettingsError(f"{path}: TOML keys must be strings")
            dotted = f"{prefix}.{name}" if prefix else name
            if isinstance(raw, Mapping):
                if (prefix, name) in _BY_STORAGE:
                    raise SettingsError(
                        f"{path}: [{dotted}] must be a value, not a table")
                if dotted in _DYNAMIC_TABLES:
                    key_check, value_check, _description = _DYNAMIC_TABLES[dotted]
                    for leaf, leaf_raw in raw.items():
                        if not isinstance(leaf, str):
                            raise SettingsError(
                                f"{path}: TOML keys must be strings")
                        try:
                            leaf_name = key_check(leaf)
                        except ValueError as exc:
                            raise SettingsError(
                                f"{path}: [{dotted}].{leaf}: {exc}") from None
                        key = f"{dotted}.{leaf_name}"
                        if key in values:
                            raise SettingsError(
                                f"{path}: setting {key!r} is defined twice: "
                                f"[{where_defined[key]}] and "
                                f"[{dotted}.{leaf_name}]")
                        try:
                            values[key] = value_check(leaf_raw)
                        except ValueError as exc:
                            raise SettingsError(
                                f"{path}: [{dotted}].{leaf_name}: {exc}"
                            ) from None
                        where_defined[key] = f"{dotted}.{leaf_name}"
                        locations[key] = (dotted, leaf_name)
                    continue
                if not _is_known_table(dotted):
                    raise SettingsError(_unknown_section(path, dotted))
                walk(raw, dotted)
                continue
            if _is_known_table(dotted):
                raise SettingsError(f"{path}: [{dotted}] must be a table")
            setting = _BY_STORAGE.get((prefix, name))
            if setting is None:
                raise SettingsError(_unknown_leaf(path, dotted))
            if setting.scope != "both" and setting.scope != layer:
                raise SettingsError(
                    f"{path}: [{dotted}] ({setting.key}) is "
                    f"{setting.scope}-only and cannot be set in the "
                    f"{layer} config")
            if setting.key in values:
                raise SettingsError(
                    f"{path}: setting {setting.key!r} is defined twice: "
                    f"[{where_defined[setting.key]}] and [{dotted}]")
            values[setting.key] = _validate(
                setting, raw,
                where=f"{path}: [{dotted}] ({setting.key})")
            where_defined[setting.key] = dotted
            locations[setting.key] = (prefix, name)

    walk(data, "")
    return values, locations


# --------------------------------------------------------------------------- #
# TOML text editing
# --------------------------------------------------------------------------- #
_PLAIN_HEADER = re.compile(r"^[ \t]*\[(?P<path>[^\[\]]+)\][ \t]*(?:#.*)?\Z")
_ARRAY_HEADER = re.compile(r"^[ \t]*\[\[.*\]\][ \t]*(?:#.*)?\Z")


def _newline_for(text: str) -> str:
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    cr = text.count("\r") - crlf
    if crlf and crlf >= lf and crlf >= cr:
        return "\r\n"
    if cr and cr > lf and cr > crlf:
        return "\r"
    return "\n"


def _parse_dotted_key(raw: str) -> tuple[str, ...]:
    parts: list[str] = []
    buffer: list[str] = []
    quote: str | None = None
    escaped = False
    index = 0
    while index < len(raw):
        char = raw[index]
        if quote is not None:
            if escaped:
                buffer.append(char)
                escaped = False
            elif quote == '"' and char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            else:
                buffer.append(char)
        elif char in "'\"":
            quote = char
        elif char == ".":
            part = "".join(buffer).strip()
            if not part:
                raise ValueError("empty key part")
            parts.append(part)
            buffer = []
        else:
            buffer.append(char)
        index += 1
    if quote is not None:
        raise ValueError("unterminated quoted key")
    part = "".join(buffer).strip()
    if not part:
        raise ValueError("empty key part")
    parts.append(part)
    return tuple(parts)


def _assignment(line: str) -> tuple[str, int] | None:
    body = line.rstrip("\r\n")
    quote: str | None = None
    escaped = False
    for index, char in enumerate(body):
        if quote is not None:
            if escaped:
                escaped = False
            elif quote == '"' and char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in "'\"":
            quote = char
        elif char == "#":
            return None
        elif char == "=":
            left = body[:index].strip()
            if not left:
                return None
            try:
                parts = _parse_dotted_key(left)
            except ValueError:
                return None
            return ".".join(parts), index
    return None


def _value_end(lines: list[str], start: int, equals: int) -> int:
    """The last line of the value that starts at ``lines[start][equals]``."""
    square = 0
    curly = 0
    string: str | None = None
    escaped = False
    for index in range(start, len(lines)):
        line = lines[index]
        begin = equals + 1 if index == start else 0
        end = len(line)
        while end > begin and line[end - 1] in "\r\n":
            end -= 1
        position = begin
        while position < end:
            char = line[position]
            if string is not None:
                if string in ("'''", '"""'):
                    if line.startswith(string, position):
                        string = None
                        position += 3
                        continue
                    if string == '"""' and char == "\\":
                        position += 2
                        continue
                    position += 1
                    continue
                if escaped:
                    escaped = False
                    position += 1
                    continue
                if string == '"' and char == "\\":
                    escaped = True
                    position += 1
                    continue
                if char == string:
                    string = None
                position += 1
                continue
            if char == "#":
                break
            if char in "'\"":
                if line.startswith(char * 3, position):
                    string = char * 3
                    position += 3
                    continue
                string = char
                position += 1
                continue
            if char == "[":
                square += 1
            elif char == "]":
                square -= 1
            elif char == "{":
                curly += 1
            elif char == "}":
                curly -= 1
            position += 1
        if string is None and square == 0 and curly == 0:
            return index
    return len(lines) - 1


def _trailing_comment(line: str, equals: int) -> str:
    """A comment after a single-line value, or "" (strings are respected)."""
    body = line.rstrip("\r\n")
    string: str | None = None
    escaped = False
    position = equals + 1
    while position < len(body):
        char = body[position]
        if string is not None:
            if string in ("'''", '"""'):
                if body.startswith(string, position):
                    string = None
                    position += 3
                    continue
                if string == '"""' and char == "\\":
                    position += 2
                    continue
                position += 1
                continue
            if escaped:
                escaped = False
                position += 1
                continue
            if string == '"' and char == "\\":
                escaped = True
                position += 1
                continue
            if char == string:
                string = None
            position += 1
            continue
        if char == "#":
            return body[position:]
        if char in "'\"":
            if body.startswith(char * 3, position):
                string = char * 3
                position += 3
                continue
            string = char
            position += 1
            continue
        position += 1
    return ""


def _headers(lines: list[str]) -> list[tuple[int, tuple[str, ...] | None]]:
    found: list[tuple[int, tuple[str, ...] | None]] = []
    index = 0
    while index < len(lines):
        body = lines[index].rstrip("\r\n")
        match = _PLAIN_HEADER.match(body)
        if match:
            try:
                found.append((index, _parse_dotted_key(match.group("path"))))
            except ValueError:
                found.append((index, None))
            index += 1
            continue
        if _ARRAY_HEADER.match(body):
            found.append((index, None))
            index += 1
            continue
        assignment = _assignment(lines[index])
        if assignment is not None:
            _, equals = assignment
            index = _value_end(lines, index, equals) + 1
            continue
        index += 1
    return found


def _section_bounds(
        lines: list[str], headers: list[tuple[int, tuple[str, ...] | None]],
        section: str) -> tuple[int, int] | None:
    target = tuple(section.split("."))
    for position, (line, path) in enumerate(headers):
        if path != target:
            continue
        start = line + 1
        end = headers[position + 1][0] if position + 1 < len(headers) else len(lines)
        return start, end
    return None


def _iter_assignments(
        lines: list[str], start: int, end: int) -> Iterable[tuple[int, int, str]]:
    index = start
    while index < end:
        assignment = _assignment(lines[index])
        if assignment is None:
            index += 1
            continue
        name, equals = assignment
        last = _value_end(lines, index, equals)
        yield index, last, name
        index = last + 1


def _render_value(setting: Setting, value: Any) -> str:
    if setting.type == "boolean":
        return "true" if value else "false"
    if setting.type == "integer":
        return str(value)
    if setting.type == "number":
        return repr(float(value))
    if setting.type == "string":
        return json.dumps(value, ensure_ascii=False)
    if setting.type == "string_list":
        return "[" + ", ".join(
            json.dumps(item, ensure_ascii=False) for item in value) + "]"
    raise SettingsError(f"{setting.key}: unsupported type {setting.type!r}")


def _render_name(name: str) -> str:
    """A TOML bare key when possible, else a quoted key."""
    if re.fullmatch(r"[A-Za-z0-9_-]+", name):
        return name
    return json.dumps(name, ensure_ascii=False)


def _append_section(lines: list[str], section: str, name: str,
                    rendered: str, newline: str) -> None:
    if lines:
        if not lines[-1].endswith(("\n", "\r")):
            lines[-1] += newline
        if lines[-1].strip():
            lines.append(newline)
    lines.append(f"[{section}]{newline}")
    lines.append(f"{_render_name(name)} = {rendered}{newline}")


def _apply_setting(text: str, section: str, name: str, setting: Setting,
                   value: Any) -> str:
    newline = _newline_for(text)
    lines = text.splitlines(keepends=True)
    rendered = _render_value(setting, value)
    rendered_name = _render_name(name)
    headers = _headers(lines)
    bounds = _section_bounds(lines, headers, section)
    if bounds is None:
        _append_section(lines, section, name, rendered, newline)
        return "".join(lines)
    start, end = bounds
    existing = None
    insert_after = start - 1
    for first, last, assigned in _iter_assignments(lines, start, end):
        insert_after = last
        if assigned == name:
            existing = (first, last)
            break
    if existing is not None:
        first, last = existing
        suffix = ""
        if first == last:
            assignment = _assignment(lines[first])
            if assignment is not None:
                comment = _trailing_comment(lines[first], assignment[1])
                if comment:
                    suffix = f"  {comment}"
        lines[first:last + 1] = [
            f"{rendered_name} = {rendered}{suffix}{newline}"]
    else:
        position = insert_after + 1
        if position > 0 and not lines[position - 1].endswith(("\n", "\r")):
            lines[position - 1] += newline
        lines.insert(position, f"{rendered_name} = {rendered}{newline}")
    return "".join(lines)


def _unified_diff(path: Path, old_text: str, new_text: str) -> str:
    if old_text == new_text:
        return ""
    return "".join(difflib.unified_diff(
        old_text.splitlines(keepends=True),
        new_text.splitlines(keepends=True),
        fromfile=str(path), tofile=str(path),
    ))


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _atomic_write(path: Path, text: str) -> None:
    # Settings writes are not target code edits: they may target the user
    # config outside the repository, and the versioned repository config's
    # history is git. A state-dir backup from edit/txn.py would be the wrong
    # destination, so this uses a same-directory temp file and os.replace.
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
        temp_path = Path(temporary)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(text.encode("utf-8"))
                handle.flush()
                os.fsync(handle.fileno())
            mode = (stat.S_IMODE(path.stat().st_mode)
                    if path.exists() else 0o644)
            os.chmod(temp_path, mode)
            os.replace(temp_path, path)
            _fsync_directory(path.parent)
        except BaseException:
            try:
                temp_path.unlink()
            except OSError:
                pass
            raise
    except OSError as exc:
        raise SettingsError(f"cannot write {path}: {exc}") from exc


# --------------------------------------------------------------------------- #
# public API
# --------------------------------------------------------------------------- #
def schema() -> list[dict[str, Any]]:
    """The registry as plain data, in display order."""
    result = []
    for setting in _SETTINGS:
        result.append({
            "key": setting.key,
            "group": setting.key.split(".", 1)[0],
            "type": setting.type,
            "default": _json_value(setting, setting.default),
            "scope": setting.scope,
            "description": setting.description,
            "validation": setting.validation,
            "restart_required": setting.restart_required,
            "section": setting.section,
            "name": setting.name,
            "aliases": [f"{section}.{name}" for section, name in setting.aliases],
            "env": _ENV_BY_KEY.get(setting.key),
        })
    return result


def _record(setting: Setting, value: Any, source: str) -> dict[str, Any]:
    return {
        "key": setting.key,
        "value": _json_value(setting, value),
        "source": source,
        "type": setting.type,
        "scope": setting.scope,
        "default": _json_value(setting, setting.default),
        "description": setting.description,
        "validation": setting.validation,
        "restart_required": setting.restart_required,
        "section": setting.section,
    }


def values(root: str | Path | None = None, *,
           overrides: Mapping[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    """Every setting with its value and source.

    Reads only: this function never creates ``ozagia/`` or ``.ozagia/``.
    """
    root_p = _resolve_root(root)
    user_path = user_config_path()
    repo_path = repo_config_path(root_p)
    user_data, _ = _read_toml(user_path)
    repo_data, _ = _read_toml(repo_path)
    user_values, _ = _parse_layer(user_data, user_path, "user")
    repo_values, _ = _parse_layer(repo_data, repo_path, "repo")

    result: dict[str, dict[str, Any]] = {}
    for setting in _SETTINGS:
        value = _json_value(setting, setting.default)
        source = "default"
        if setting.key in user_values:
            value = user_values[setting.key]
            source = "user"
        if setting.key in repo_values:
            value = repo_values[setting.key]
            source = "repo"
        result[setting.key] = _record(setting, value, source)

    # Open-ended override tables: every entry present in a layer is reported
    # as a setting of its own. They have no built-in default.
    for key in sorted(set(user_values) | set(repo_values)):
        if key in result:
            continue
        setting = _dynamic_setting(key)
        if setting is None:
            continue
        value = setting.default
        source = "default"
        if key in user_values:
            value = user_values[key]
            source = "user"
        if key in repo_values:
            value = repo_values[key]
            source = "repo"
        result[key] = _record(setting, value, source)

    for variable, key in ENV_SETTINGS.items():
        if variable not in os.environ:
            continue
        setting = _BY_KEY[key]
        value = _coerce_text(setting, os.environ[variable],
                             where=f"${variable} ({key})")
        result[key] = _record(setting, value, "env")

    if overrides is not None:
        if not isinstance(overrides, Mapping):
            raise SettingsError(
                "overrides must be a mapping of setting key to value")
        for key, raw in overrides.items():
            setting = _setting_for_key(key)
            value = _coerce_value(setting, raw, where=f"override {key}")
            result[setting.key] = _record(setting, value, "override")
    return result


def get(root: str | Path | None, key: str, *,
        overrides: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """One setting with its value and source."""
    setting = _setting_for_key(key)
    items = values(root, overrides=overrides)
    if setting.key in items:
        return items[setting.key]
    # An open-ended override entry that is not present in any layer.
    return _record(setting, setting.default, "default")


def testing_overrides(root: str | Path | None = None) -> dict[
        str, dict[str, str]]:
    """The open-ended ``[testing.tasks]`` and ``[testing.paths]`` tables.

    Values are validated test-selection modes. User entries are overlaid by
    repository entries, matching the registry's precedence. The returned
    dictionaries preserve TOML order, which matters for path globs.
    """
    root_p = _resolve_root(root)
    user_data, _ = _read_toml(user_config_path())
    repo_data, _ = _read_toml(repo_config_path(root_p))
    user_values, _ = _parse_layer(user_data, user_config_path(), "user")
    repo_values, _ = _parse_layer(
        repo_data, repo_config_path(root_p), "repo")
    tables: dict[str, dict[str, str]] = {"tasks": {}, "paths": {}}
    # Repository entries come first so they win both for the same key and
    # when a repository glob and a user glob both match a path.
    for layer_values in (repo_values, user_values):
        for table in _DYNAMIC_TABLES:
            name = table.split(".", 1)[1]
            prefix = table + "."
            for key, value in layer_values.items():
                if key.startswith(prefix):
                    tables[name].setdefault(key[len(prefix):], value)
    return tables


def set_value(root: str | Path | None, key: str, value: Any,
              scope: str = "repo", dry_run: bool = True, *,
              user_config: str | Path | None = None) -> dict[str, Any]:
    """Validate and write one setting.

    ``dry_run=True`` (the default) returns the diff and changes nothing.
    Writes preserve unrelated TOML and replace the target file atomically.
    """
    setting = _setting_for_key(key)
    if scope not in {"repo", "user"}:
        raise SettingsError(f"scope must be 'repo' or 'user', got {scope!r}")
    if setting.scope != "both" and setting.scope != scope:
        raise SettingsError(
            f"{setting.key} is {setting.scope}-only; "
            f"use scope={setting.scope!r}")
    root_p = _resolve_root(root)
    if scope == "repo":
        if user_config is not None:
            raise SettingsError("user_config is only valid with scope='user'")
        if not root_p.is_dir():
            raise SettingsError(f"repository root does not exist: {root_p}")
        path = repo_config_path(root_p)
    elif user_config is not None:
        path = Path(user_config).expanduser()
    else:
        path = user_config_path()
    path = path.resolve()
    cleaned = _coerce_value(setting, value, where=setting.key)

    data, old_text = _read_toml(path)
    layer, locations = _parse_layer(data, path, scope)
    existed = path.is_file()
    file_old = layer.get(setting.key)
    current = get(root_p, setting.key)
    section, name = locations.get(
        setting.key, (setting.section, setting.name))
    new_text = _apply_setting(old_text, section, name, setting, cleaned)

    if new_text != old_text:
        try:
            new_data = tomllib.loads(new_text)
        except tomllib.TOMLDecodeError as exc:
            raise SettingsError(
                f"{path}: cannot update {setting.key} in place; move it into "
                f"a [{section}] table and retry ({exc})") from exc
        new_layer, _ = _parse_layer(new_data, path, scope)
        if new_layer.get(setting.key) != cleaned:
            raise SettingsError(
                f"{path}: cannot update {setting.key} in place without "
                "changing its meaning")
        unexpected = set(new_layer) - set(layer) - {setting.key}
        if unexpected:
            raise SettingsError(
                f"{path}: cannot update {setting.key} in place; the edit "
                f"would also add {sorted(unexpected)[0]!r}")
        for other_key, other_value in layer.items():
            if other_key != setting.key and new_layer.get(other_key) != other_value:
                raise SettingsError(
                    f"{path}: cannot update {setting.key} in place without "
                    f"changing {other_key}")

    changed = new_text != old_text
    applied = False
    if not dry_run and changed:
        if _read_toml(path)[1] != old_text:
            raise SettingsError(
                f"{path}: changed since it was read; preview again")
        _atomic_write(path, new_text)
        applied = True
    return {
        "schema": SCHEMA,
        "key": setting.key,
        "scope": scope,
        "path": str(path),
        "exists": existed,
        "dry_run": dry_run,
        "changed": changed,
        "applied": applied,
        "created": not existed and applied,
        "old": file_old,
        "new": _json_value(setting, cleaned),
        "current": {"value": current["value"], "source": current["source"]},
        "diff": _unified_diff(path, old_text, new_text),
    }


__all__ = [
    "ENV_LOCATIONS",
    "ENV_SETTINGS",
    "ENV_STATE_DIR",
    "ENV_USER_CONFIG",
    "REPO_CONFIG_DIRNAME",
    "REPO_CONFIG_FILENAME",
    "SCHEMA",
    "TESTING_FALLBACKS",
    "TESTING_MODES",
    "Setting",
    "SettingsError",
    "get",
    "locations",
    "repo_config_path",
    "schema",
    "set_value",
    "testing_overrides",
    "user_config_path",
    "values",
]
