"""Where ozagia works: the target repository, its state and its configuration.

The target repository is `$OZAGIA_ROOT` if set, else the Git top level of the
current directory, else the current directory. ozagia never assumes it is
installed inside the repository it works on.

State (graph snapshot, journal, tag cache, sessions, edit backups) lives in
`<root>/.ozagia/` unless `$OZAGIA_STATE_DIR` names another directory (an
absolute path, or one relative to the root). Add `.ozagia/` to the target's
`.gitignore`; the indexers skip it either way.

The project configuration (`load_project_config`) comes from two TOML files.
The legacy file is the first of `$OZAGIA_CONFIG`, `<root>/.ozagia.toml`,
`<state dir>/config.toml`; it keeps the highest priority for every key it
defines. The versioned repository config `<root>/ozagia/config.toml` (the
settings registry, `ozagia.core.settings`) supplies the supported values for
keys the legacy file does not define. Without either file, every key has the
generic default below. `config_path()` names the highest-priority file that
exists; `config_sources()` reports both files and the precedence. All keys are
optional:

    [index]
    source_dirs = ["."]           # dirs indexed into the graph (memory)
    map_dirs = [...]              # dirs scanned for the repo map, search and
                                  # symbol graph; default source_dirs + test_dirs
                                  # without dirs inside an earlier entry
    test_dirs = ["tests"]         # dirs whose test_*.py files become Test nodes
    skip_dirs = []                # dir names skipped in addition to the
                                  # built-in GENERIC_SKIP_DIRS
    docs = ["README.md", "docs/**/*.md"]   # Doc globs (include_docs="features")
    docs_all = ["**/*.md"]        # globs added for include_docs="all"
    key_docs = ["README.md"]      # docs listed first in the brief
    command_docs = [...]          # docs scanned for build/test commands;
                                  # default key_docs
    import_roots = [...]          # extra Python import roots besides the root;
                                  # default ["src"] when the root has src/
    path_prefixes = [...]         # first path components recognised as file
                                  # mentions in tests and docs; default the
                                  # first component of each source/test dir
                                  # plus "docs"
    changed_exclude = ["__pycache__/", ".pytest_cache/", ".ozagia/"]
                                  # path prefixes hidden from `changed`
    entrypoint_prefixes = ["scripts/", "tools/", "bin/"]
                                  # never dead-code candidates

    [memory]
    skip_dirs = []                # extra names skipped by the graph build only

    [adapters]
    modules = []                  # "module:attr" adapters, imported with the
                                  # directory of this config file on sys.path

    [reads]
    structured_outline_lines = 2000   # a read with no range of a .json,
    structured_outline_bytes = 65536  # .yaml, .yml or .toml file over either
                                      # limit returns its outline; 0 turns a
                                      # limit off
    outline_lines = 400               # the same for any other text file read
    outline_bytes = 40960             # by the daemon (read tool, `cat F`):
                                      # symbols/sections with keys, then the
                                      # first lines; 0 turns a limit off
    repeat = "note" | "stub" | "off"  # what a same-session re-read of an
                                      # unchanged range gets; unset: `note`
                                      # for every harness. `stub` is
                                      # experimental (one live eval showed no
                                      # benefit: docs/results/ocelot_live_
                                      # eval_2026-11-17.md) and needs
                                      # the harness to report compaction
                                      # (`ozagia.intent.ledger`)
    repeat_by_harness = {opencode = "stub"}  # per-harness override
                                      # (wins over `repeat`)

    [tests]
    select = "all" | "affected"   # a captured pytest with no test selector:
                                  # "all" (default) runs what the command
                                  # names; "affected" runs only the tests
                                  # linked to files changed since the last
                                  # fully green run and reports the rest as
                                  # not run (`ozagia.intent.affected`). Off by
                                  # default because changing what a command
                                  # runs is surprising. OZAGIA_TESTS_ALL=1 on
                                  # the command runs everything.
    cache = false                 # skip a selected test whose inputs (test
                                  # file, conftests, imported project files,
                                  # pytest config, interpreter, environment)
                                  # are unchanged since it passed

    [jobs]
    enabled = false               # allow job_start (background jobs in the
                                  # daemon; see ozagia.daemon.jobs). Jobs run
                                  # with the daemon's privileges, outside any
                                  # harness sandbox. $OZAGIA_JOBS=1 in the
                                  # daemon's environment also enables them.

    [lint]
    enabled = true                # lint the changed files after a handled
                                  # edit or write and in `after` for a
                                  # built-in edit, write or patch; unset: on
                                  # when a linter is configured
                                  # (`ozagia.intent.lint`)
    timeout_s = 3                 # the budget for the linter runs of one call
                                  # (at most 10 s is used)
    python = "ruff" | "off"       # unset: ruff when pyproject.toml has
                                  # [tool.ruff] or ruff.toml/.ruff.toml exists
    rust = "rustfmt" | "off"      # unset: rustfmt when Cargo.toml exists

    [iac]
    roots = []                    # explicit Terraform/OpenTofu roots
    skip_globs = []               # extra IaC source files/directories to skip
    [[iac.remote_states]]
    consumer = "envs/app"
    name = "iam"                  # data "terraform_remote_state" label
    producer = "envs/iam"

    [review]
    layers = []                   # `ozagia review-repo`: path prefixes from
                                  # the lowest layer to the highest; a file
                                  # importing a file of a higher layer is a
                                  # layering violation (docs/repo_review.md)

    [environments.dev]
    paths = ["envs/dev"]
    namespaces = ["dev"]
    account_alias = "development"
    region = "us-east-1"
    cluster = "dev-cluster"
    risk = "low"
    pipelines = ["deploy-dev"]

    [services.api]
    chart = "charts/api"
    values_globs = ["charts/api/values.*.yaml"]
    buildspec = "buildspec.yml"
    pipeline = "pipelines/api.yaml"
    namespace = "api"
    environment = "dev"
    paths = ["services/api"]

    [relays.prod]
    specs = [{ kind = "local" }, { kind = "ssh", options = { host = "internal.example" } }]
    allowed_kinds = ["local", "ssh"]
    max_timeout_s = 600
    command_allowlist = [["uptime"], ["kubectl", "get"]]

    [runtime.services.api]
    command = ["python", "-m", "app"]
    workdir = "services/api"
    relay = "prod"
    mode = "background"
    description = "API service"

    [runtime.tunnels.database]
    relay = "prod"
    target = "db.internal.example:5432"
    local_port = 0

Directory entries are paths relative to the root ("." is the root itself).

`<root>/ozagia/config.toml` is read with the settings registry's names. The
project config maps these groups and keys into the fields above:

    [index]          all keys above
    [memory] skip_dirs
    [adapters] modules
    [reads]          all keys above (including repeat_by_harness)
    [tests]          select, cache
    [testing]        captured, cache
    [jobs] enabled
    [lint]           enabled, timeout_s, python, rust
    [iac]            roots, skip_globs
    [review] layers
    [environments.NAME] all environment fields
    [services.NAME]     all service fields
    [relays.NAME]       specs, allowed_kinds, max_timeout_s, command_allowlist,
                        max_output_bytes
    [runtime.services.NAME]
    [runtime.tunnels.NAME]

The project-model tables (`[environments]`, `[services]`, `[relays]` and
`[runtime]`) are merged by named entry: a legacy entry wins as a whole over a
versioned entry with the same name, and versioned-only entries are appended.
Keys are never combined across the two files for one entry, so a legacy relay
chain cannot acquire a versioned `allowed_kinds`, and a legacy runtime command
cannot acquire a versioned relay. Unknown fields are rejected by the legacy
parser with the offending file's path.

`[security]` is consumed by `ozagia.core.security` from both files through
:func:`load_config_section`; its supported keys are `enabled`, `tools`,
`allow_network`, `fail_on`, `exclude`, `max_findings`, `block_secrets` and
`secret_allowlist`. Values from the other registry groups (`hints`, `models`,
`ui`, `resources`, `worktrees`, `markdown`, `files`, `tasks`) stay with the
modules that own them. The loader reports them, and any unknown key or nested
table, in `ProjectConfig.unmapped` instead of silently ignoring them. The
legacy file is never rewritten; a migrated `ozagia/config.toml` is read for
every key the legacy file leaves unset.
"""
from __future__ import annotations

import hashlib
import os
import posixpath
import re
import subprocess
import tomllib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable

ENV_ROOT = "OZAGIA_ROOT"
ENV_STATE_DIR = "OZAGIA_STATE_DIR"
ENV_CONFIG = "OZAGIA_CONFIG"
STATE_DIRNAME = ".ozagia"
CONFIG_FILENAME = ".ozagia.toml"
STATE_CONFIG_FILENAME = "config.toml"
#: Versioned repository configuration (the settings registry's file).
REPO_CONFIG_DIRNAME = "ozagia"
REPO_CONFIG_FILENAME = "config.toml"
DEFAULT_IAC_SKIP_GLOBS = ("*.backup.last", "*.backup", "*.bak", "*.orig")

# Directory names no indexer enters, in any repository.
GENERIC_SKIP_DIRS = frozenset({
    ".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", ".tox", "node_modules", "target", "build", "dist",
    ".worktrees", ".claude", STATE_DIRNAME,
})


def repo_root(start: str | Path | None = None) -> Path:
    """The repository ozagia acts on (see the module docstring)."""
    env = os.environ.get(ENV_ROOT)
    if env:
        return Path(env).resolve()
    base = Path(start) if start is not None else Path.cwd()
    try:
        proc = subprocess.run(["git", "-C", str(base), "rev-parse", "--show-toplevel"],
                              capture_output=True, text=True, timeout=10,
                              stdin=subprocess.DEVNULL, check=False)
    except (OSError, subprocess.SubprocessError):
        proc = None
    if proc is not None and proc.returncode == 0 and proc.stdout.strip():
        return Path(proc.stdout.strip()).resolve()
    return base.resolve()


def state_dir(root: str | Path) -> Path:
    """The state directory for `root`."""
    env = os.environ.get(ENV_STATE_DIR)
    if env:
        p = Path(env)
        return (p if p.is_absolute() else Path(root) / p).resolve()
    return Path(root).resolve() / STATE_DIRNAME


def state_path(root: str | Path, *parts: str) -> Path:
    """A path inside the state directory of `root`."""
    return state_dir(root).joinpath(*parts)


def state_rel(root: str | Path) -> str | None:
    """The state directory relative to `root`, or None if it is outside it."""
    root_p = Path(root).resolve()
    try:
        rel = state_dir(root_p).relative_to(root_p).as_posix()
    except ValueError:
        return None
    return None if rel in ("", ".") else rel


# --------------------------------------------------------------------------- #
# project configuration
# --------------------------------------------------------------------------- #
class ConfigError(ValueError):
    """The project configuration file is malformed."""


@dataclass(frozen=True)
class EnvironmentConfig:
    name: str
    paths: tuple[str, ...] = ()
    namespaces: tuple[str, ...] = ()
    account_alias: str | None = None
    region: str | None = None
    cluster: str | None = None
    risk: str | None = None
    pipelines: tuple[str, ...] = ()


@dataclass(frozen=True)
class ServiceConfig:
    name: str
    chart: str | None = None
    values_globs: tuple[str, ...] = ()
    buildspec: str | None = None
    pipeline: str | None = None
    namespace: str | None = None
    environment: str | None = None
    paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class RemoteStateConfig:
    consumer: str
    name: str
    producer: str


@dataclass(frozen=True)
class RelaySpecConfig:
    """A declarative relay backend specification with immutable options."""

    kind: str
    options: tuple[tuple[str, str | int | bool], ...] = ()
    name: str | None = None


@dataclass(frozen=True)
class RelayConfig:
    """A relay chain and its execution policy (never credentials/environment)."""

    name: str
    specs: tuple[RelaySpecConfig, ...]
    allowed_kinds: tuple[str, ...]
    max_timeout_s: float
    command_allowlist: tuple[tuple[str, ...], ...] | None = None
    max_output_bytes: int = 4_096


@dataclass(frozen=True)
class RuntimeServiceConfig:
    """A local, argv-only runtime process definition."""

    name: str
    command: tuple[str, ...]
    workdir: str | None = None
    relay: str | None = None
    mode: str = "background"
    description: str = ""


@dataclass(frozen=True)
class RuntimeTunnelConfig:
    """A declarative tunnel definition; it is not evidence of connectivity."""

    name: str
    relay: str
    target: str
    local_port: int = 0
    description: str = ""


@dataclass(frozen=True)
class ProjectConfig:
    """The resolved project configuration (see the module docstring)."""
    root: Path
    source: Path | None
    source_dirs: tuple[str, ...]
    map_dirs: tuple[str, ...]
    test_dirs: tuple[str, ...]
    # GENERIC_SKIP_DIRS plus `[index] skip_dirs`.
    skip_dirs: frozenset[str]
    # skip_dirs plus `[memory] skip_dirs`.
    memory_skip_dirs: frozenset[str]
    docs: tuple[str, ...]
    docs_all: tuple[str, ...]
    key_docs: tuple[str, ...]
    command_docs: tuple[str, ...]
    import_roots: tuple[str, ...]
    path_prefixes: tuple[str, ...]
    changed_exclude: tuple[str, ...]
    entrypoint_prefixes: tuple[str, ...]
    adapters: tuple[str, ...]
    structured_outline_lines: int = 2000
    structured_outline_bytes: int = 64 * 1024
    outline_lines: int = 400
    outline_bytes: int = 40 * 1024
    # None: each harness's default (`ozagia.intent.ledger.HARNESS_REPEAT`, `note`).
    repeat: str | None = None
    repeat_by_harness: tuple[tuple[str, str], ...] = ()
    jobs_enabled: bool = False
    tests_select: str = "all"
    tests_cache: bool = False
    # `[lint]`: None means "unset" (decided by the files of the project).
    lint_enabled: bool | None = None
    lint_timeout_s: float = 3.0
    lint_python: str | None = None
    lint_rust: str | None = None
    iac_roots: tuple[str, ...] = ()
    iac_skip_globs: tuple[str, ...] = DEFAULT_IAC_SKIP_GLOBS
    remote_states: tuple[RemoteStateConfig, ...] = ()
    environments: tuple[EnvironmentConfig, ...] = ()
    services: tuple[ServiceConfig, ...] = ()
    relays: tuple[RelayConfig, ...] = ()
    runtime_services: tuple[RuntimeServiceConfig, ...] = ()
    runtime_tunnels: tuple[RuntimeTunnelConfig, ...] = ()
    # `[review] layers`: path prefixes, lowest layer first.
    review_layers: tuple[str, ...] = ()
    #: The configuration files that supplied values, highest priority first.
    sources: tuple[Path, ...] = ()
    #: Reload identity for both configuration locations:
    #: (kind, path, mtime_ns, size, sha256). A missing file keeps its path
    #: with zero mtime/size and an empty digest, so creating it changes the
    #: identity. `ozagia.adapters` uses this to invalidate its registry.
    source_signatures: tuple[tuple[str, str, int, int, str], ...] = ()
    #: Versioned-config keys this loader does not map, as (key, reason).
    unmapped: tuple[tuple[str, str], ...] = ()
    #: Directory adapter modules are imported from when it differs from the
    #: active source file's directory (a new-only `ozagia/config.toml` uses the
    #: repository root). None falls back to the source file's directory.
    adapter_base_dir: Path | None = None

    @property
    def adapter_base(self) -> Path | None:
        """The directory adapter modules are imported from."""
        if self.adapter_base_dir is not None:
            return self.adapter_base_dir
        return self.source.parent if self.source is not None else None


@dataclass(frozen=True)
class ConfigSources:
    """The project configuration files that apply to a root.

    ``legacy`` is the highest-priority legacy file (``$OZAGIA_CONFIG``,
    ``<root>/.ozagia.toml`` or the state directory's ``config.toml``).
    ``repo_config`` is the versioned ``<root>/ozagia/config.toml``, whether or
    not it exists. ``active`` is the file ``config_path`` returns:
    ``legacy`` when present, else the repository config. ``precedence`` names
    the files that can supply values, highest priority first (``"legacy"``,
    ``"repo"``).
    """

    root: Path
    legacy: Path | None = None
    legacy_kind: str | None = None
    repo_config: Path | None = None
    repo_exists: bool = False
    active: Path | None = None
    precedence: tuple[str, ...] = ()

    @property
    def active_kind(self) -> str | None:
        """``"legacy"``, ``"repo"`` or None for the active file."""
        if self.active is None:
            return None
        if self.legacy is not None and self.active == self.legacy:
            return "legacy"
        return "repo"

    @property
    def paths(self) -> tuple[Path, ...]:
        """Existing configuration files, highest priority first."""
        by_kind = {"legacy": self.legacy, "repo": self.repo_config}
        return tuple(path for kind in self.precedence
                     if (path := by_kind.get(kind)) is not None)


_KEYS = {
    "index": {"source_dirs", "map_dirs", "test_dirs", "skip_dirs", "docs",
              "docs_all", "key_docs", "command_docs", "import_roots",
              "path_prefixes", "changed_exclude", "entrypoint_prefixes"},
    "memory": {"skip_dirs"},
    "adapters": {"modules"},
    "reads": {"structured_outline_lines", "structured_outline_bytes",
              "outline_lines", "outline_bytes", "repeat",
              "repeat_by_harness"},
    "jobs": {"enabled"},
    "tests": {"select", "cache"},
    "lint": {"enabled", "timeout_s", "python", "rust"},
    "iac": {"roots", "skip_globs", "remote_states"},
    "review": {"layers"},
}
#: `[security]` keys `ozagia.core.security` reads from either config file.
SECURITY_SECTION = "security"
SECURITY_KEYS = (
    "enabled", "tools", "allow_network", "fail_on", "exclude",
    "max_findings", "block_secrets", "secret_allowlist",
)
_SECURITY_KEY_SET = frozenset(SECURITY_KEYS)
REPEAT_POLICIES = ("stub", "note", "off")
TEST_SELECT_POLICIES = ("all", "affected")

#: Versioned-config keys mapped into ProjectConfig -> legacy TOML location.
#: `testing.captured` and `tests.select` are aliases, as in the registry.
_PROJECT_CONFIG_KEYS: dict[str, tuple[str, str]] = {
    "index.source_dirs": ("index", "source_dirs"),
    "index.map_dirs": ("index", "map_dirs"),
    "index.test_dirs": ("index", "test_dirs"),
    "index.skip_dirs": ("index", "skip_dirs"),
    "index.docs": ("index", "docs"),
    "index.docs_all": ("index", "docs_all"),
    "index.key_docs": ("index", "key_docs"),
    "index.command_docs": ("index", "command_docs"),
    "index.import_roots": ("index", "import_roots"),
    "index.path_prefixes": ("index", "path_prefixes"),
    "index.changed_exclude": ("index", "changed_exclude"),
    "index.entrypoint_prefixes": ("index", "entrypoint_prefixes"),
    "memory.skip_dirs": ("memory", "skip_dirs"),
    "adapters.modules": ("adapters", "modules"),
    "reads.structured_outline_lines": ("reads", "structured_outline_lines"),
    "reads.structured_outline_bytes": ("reads", "structured_outline_bytes"),
    "reads.outline_lines": ("reads", "outline_lines"),
    "reads.outline_bytes": ("reads", "outline_bytes"),
    "reads.repeat": ("reads", "repeat"),
    "reads.repeat_by_harness": ("reads", "repeat_by_harness"),
    "tests.select": ("tests", "select"),
    "tests.cache": ("tests", "cache"),
    "testing.captured": ("tests", "select"),
    "testing.cache": ("tests", "cache"),
    "jobs.enabled": ("jobs", "enabled"),
    "lint.enabled": ("lint", "enabled"),
    "lint.timeout_s": ("lint", "timeout_s"),
    "lint.python": ("lint", "python"),
    "lint.rust": ("lint", "rust"),
    "iac.roots": ("iac", "roots"),
    "iac.skip_globs": ("iac", "skip_globs"),
    "review.layers": ("review", "layers"),
}
#: Tables whose named entries are project models rather than flat settings.
#: They are validated by `_parse` (the legacy parser) and merged by name, so
#: the legacy entry stays the unit of precedence.
_PROJECT_MODEL_SECTIONS = frozenset({"environments", "services", "relays", "runtime"})
_PROJECT_CONFIG_SECTIONS = frozenset(_KEYS) | _PROJECT_MODEL_SECTIONS | {"testing"}

#: Registry groups another module reads. Reported once per group, never
#: mapped into ProjectConfig and never copied into the legacy shape.
_OWNED_SECTIONS = {
    "hints": "read by ozagia.core.library_hints",
    "models": "read by ozagia.core.models",
    "ui": "UI presentation setting; not part of the index configuration",
    "resources": "read by ozagia.core.resources",
    "worktrees": "read by the worktree modules",
    "markdown": "read by the Markdown routing modules",
    "files": "read by the file-ledger and cleanup modules",
    "tasks": "read by the bounded-task modules",
}

#: Individual registry keys another module reads, or with no mapping.
_OWNED_KEYS = {
    "lint.trust_project_venv": "user-level lint setting (scope user)",
    "review.min_reviewers": "read by the review modules",
    "review.require_clean": "read by the review modules",
    "testing.mode": "read by ozagia.core.testing",
    "testing.fallback": "read by ozagia.core.testing",
    "testing.max_tests": "read by ozagia.core.testing",
    "testing.timeout_s": "read by ozagia.core.testing",
    "iac.remote_states": "an array of tables has no registry equivalent yet",
}

#: Registry values that mean "derive the project default" when empty.
_DERIVED_WHEN_EMPTY = frozenset({
    "index.map_dirs", "index.import_roots", "index.path_prefixes",
    "index.command_docs",
})

#: Registry `testing.captured` -> the selector the existing executor uses
#: (`ozagia.core.testing.CAPTURED_SELECT`, plus `off` as an alias for `none`).
_CAPTURED_SELECT = {
    "all": "all", "affected": "affected",
    "none": "all", "off": "all", "package": "all", "smoke": "affected",
}

_SKIP = object()

_RELAY_KEYS = {
    "specs", "allowed_kinds", "max_timeout_s", "command_allowlist",
    "max_output_bytes",
}
_RELAY_SPEC_KEYS = {"kind", "options", "name"}
_RUNTIME_KEYS = {"services", "tunnels"}
_RUNTIME_SERVICE_KEYS = {"command", "workdir", "relay", "mode", "description"}
_RUNTIME_TUNNEL_KEYS = {"relay", "target", "local_port", "description"}
_ENVIRONMENT_KEYS = {
    "paths", "namespaces", "account_alias", "region", "cluster", "risk",
    "pipelines",
}
_SERVICE_KEYS = {
    "chart", "values_globs", "buildspec", "pipeline", "namespace",
    "environment", "paths",
}
_REMOTE_STATE_KEYS = {"consumer", "name", "producer"}
_STRUCTURAL_VALUE = re.compile(r"[A-Za-z0-9_./:@#*?+\[\]-]{1,256}\Z")
_CONFIG_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}\Z")
_RELAY_OPTION_KEY = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}\Z")
_SENSITIVE_KEY = re.compile(
    r"(?:secret|password|passwd|token|credential|access[_-]?key|private[_-]?key|"
    r"api[_-]?key|authorization|environment|env_vars?)",
    re.IGNORECASE,
)
# An argv element is credential-like when it is an option (`-x`, `--x`,
# `--x=v`) or a `key=value` whose name contains a credential word; a
# path that happens to contain one (`tests/test_tokenizer.py`,
# `.../environments/.venv/bin/python`) is not.
_OPTION_NAME = re.compile(r"--?(?P<name>[A-Za-z][A-Za-z0-9_.-]*)(?:=|\Z)")
_KEY_VALUE = re.compile(r"(?P<name>[A-Za-z][A-Za-z0-9_.-]*)=")


def _sensitive_arg(arg: str) -> bool:
    """Whether one argv element names a credential (see above)."""
    match = _OPTION_NAME.match(arg) or _KEY_VALUE.match(arg)
    return bool(match and _SENSITIVE_KEY.search(match.group("name")))


_SHELL_META = re.compile(r"[;&|`$<>\\\n\r]")
_SHELL_EXECUTABLES = {"sh", "bash", "dash", "zsh", "fish", "cmd", "powershell"}
_HOST_PORT = re.compile(
    r"(?P<host>[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?):(?P<port>[0-9]{1,5})\Z"
)


def repo_config_path(root: str | Path) -> Path:
    """``<root>/ozagia/config.toml`` (the versioned repository config)."""
    return Path(root).resolve() / REPO_CONFIG_DIRNAME / REPO_CONFIG_FILENAME


def _legacy_config(root: Path) -> tuple[Path | None, str | None]:
    """The legacy config path and its kind: env, root or state."""
    env = os.environ.get(ENV_CONFIG)
    if env:
        p = Path(env).expanduser().resolve()
        if not p.is_file():
            raise ConfigError(f"${ENV_CONFIG} names {p}, which is not a file")
        return p, "env"
    for p, kind in ((root / CONFIG_FILENAME, "root"),
                    (state_path(root, STATE_CONFIG_FILENAME), "state")):
        if p.is_file():
            return p, kind
    return None, None


def config_sources(root: str | Path) -> ConfigSources:
    """Both configuration locations and the precedence that applies to `root`.

    The legacy file (``$OZAGIA_CONFIG``, ``<root>/.ozagia.toml`` or the state
    directory's ``config.toml``) wins for every key it defines. The versioned
    ``<root>/ozagia/config.toml`` supplies the rest. When a location selector
    names the versioned file itself, it keeps the registry schema instead of
    becoming a legacy layer. Reads only.
    """
    root_p = Path(root).resolve()
    repo = repo_config_path(root_p)
    legacy, kind = _legacy_config(root_p)
    if legacy is not None and legacy == repo:
        # A subprocess can receive the versioned file through OZAGIA_CONFIG.
        # Its schema is the registry, not the legacy one.
        legacy, kind = None, None
    repo_exists = repo.is_file()
    precedence = tuple(
        name for name, present in (("legacy", legacy is not None),
                                   ("repo", repo_exists)) if present)
    return ConfigSources(
        root=root_p, legacy=legacy, legacy_kind=kind, repo_config=repo,
        repo_exists=repo_exists,
        active=legacy if legacy is not None else (repo if repo_exists else None),
        precedence=precedence,
    )


def config_path(root: str | Path) -> Path | None:
    """The configuration file that applies to `root`, or None for defaults.

    The legacy file when present, else ``<root>/ozagia/config.toml``. Values
    from both files are loaded; see :func:`load_project_config` and
    :func:`config_sources`.
    """
    return config_sources(root).active


@dataclass(frozen=True)
class ConfigSectionError:
    """One configuration file that could not supply a section."""

    #: ``"legacy"`` or ``"repo"``.
    kind: str
    path: Path
    message: str


@dataclass(frozen=True)
class ConfigSection:
    """One named table merged from both configuration files.

    ``values`` holds the raw TOML values. ``sources`` maps each key to
    ``"legacy"`` or ``"repo"``; a key defined in both reports ``"legacy"``
    because the legacy file wins. ``legacy`` is the applicable legacy file
    (or None) and ``repo`` is ``<root>/ozagia/config.toml``, whether or not it
    exists. ``values_by_source`` keeps each file's raw table, so a caller can
    validate values a higher-priority file overrides. ``errors`` reports
    unreadable or malformed files. Callers decide whether that is fatal, so the
    security loader can keep its historical tolerance for the legacy file
    while still reporting a broken versioned file.
    """

    name: str
    values: dict[str, Any]
    sources: dict[str, str]
    legacy: Path | None
    repo: Path
    #: Raw table from each file that supplies the section, keyed by kind.
    values_by_source: dict[str, dict[str, Any]]
    errors: tuple[ConfigSectionError, ...] = ()

    def source_of(self, key: str) -> str | None:
        """``"legacy"``, ``"repo"`` or None for a key the section omits."""
        return self.sources.get(key)


def load_config_section(root: str | Path, section: str) -> ConfigSection:
    """Merge one named table from both configuration files, legacy keys first.

    The legacy file (``$OZAGIA_CONFIG``, ``<root>/.ozagia.toml`` or the state
    directory's ``config.toml``) wins for every key it defines;
    ``<root>/ozagia/config.toml`` fills the keys it leaves unset. The helper
    is read-only and uncached, so an edit is visible on the next call.
    Malformed or unreadable files are reported in ``ConfigSection.errors``
    with their kind and path rather than raised: each caller keeps the error
    policy it needs.
    """
    root_p = Path(root).resolve()
    sources = config_sources(root_p)
    values: dict[str, Any] = {}
    origins: dict[str, str] = {}
    values_by_source: dict[str, dict[str, Any]] = {}
    errors: list[ConfigSectionError] = []
    # Repo first, then legacy, so a legacy value overwrites the repo value
    # for the same key without losing per-key source tracking.
    layers = (
        ("repo", sources.repo_config if sources.repo_exists else None),
        ("legacy", sources.legacy),
    )
    for kind, path in layers:
        if path is None:
            continue
        try:
            raw = path.read_bytes()
        except OSError as exc:
            errors.append(ConfigSectionError(kind, path, f"cannot read: {exc}"))
            continue
        try:
            data = tomllib.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
            errors.append(ConfigSectionError(kind, path, str(exc)))
            continue
        table = data.get(section)
        if table is None:
            continue
        if not isinstance(table, dict):
            errors.append(ConfigSectionError(
                kind, path, f"[{section}] must be a table"))
            continue
        for name, value in table.items():
            values[name] = value
            origins[name] = kind
        values_by_source[kind] = dict(table)
    return ConfigSection(
        name=section, values=values, sources=origins,
        legacy=sources.legacy, repo=sources.repo_config,
        values_by_source=values_by_source, errors=tuple(errors),
    )


def _str_list(table: dict[str, Any], key: str, where: str) -> list[str] | None:
    if key not in table:
        return None
    value = table[key]
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConfigError(f"{where}: {key} must be a list of strings")
    return list(value)


def _rel_dir(value: str, key: str, where: str) -> str:
    v = value.strip().replace("\\", "/")
    norm = posixpath.normpath(v) if v else "."
    if posixpath.isabs(norm) or norm == ".." or norm.startswith("../"):
        raise ConfigError(f"{where}: {key} entry {value!r} must be inside the root")
    return norm


def _structural(value: Any, *, key: str, where: str) -> str:
    if (not isinstance(value, str) or len(value) > 256
            or not _STRUCTURAL_VALUE.fullmatch(value)
            or any(part == ".." for part in value.replace("\\", "/").split("/"))):
        raise ConfigError(f"{where}: {key} must be a bounded non-secret structural string")
    return value


def _path_pattern(value: Any, *, key: str, where: str) -> str:
    if (not isinstance(value, str) or not value
            or len(value) > 256 or not _STRUCTURAL_VALUE.fullmatch(value)):
        raise ConfigError(f"{where}: {key} must be a bounded non-secret structural string")
    result = value.replace("\\", "/")
    if (posixpath.isabs(result) or re.match(r"^[A-Za-z]:/", result)
            or any(part == ".." for part in result.split("/"))):
        raise ConfigError(f"{where}: {key} must stay inside the repository root")
    return result


def _repo_dir(value: Any, *, key: str, where: str) -> str:
    result = _path_pattern(value, key=key, where=where)
    if any(char in result for char in "*?[]"):
        raise ConfigError(
            f"{where}: {key} entries must be repository-relative directories")
    return _rel_dir(result, key, where)


def _config_list(table: dict[str, Any], key: str, where: str,
                 *, limit: int = 100) -> list[str]:
    values = _str_list(table, key, where) or []
    if len(values) > limit:
        raise ConfigError(f"{where}: {key} exceeds the {limit}-item limit")
    return values


def _config_name(value: Any, *, key: str, where: str) -> str:
    if not isinstance(value, str) or not _CONFIG_NAME.fullmatch(value):
        raise ConfigError(f"{where}: {key} must be a bounded name")
    return value


def _command_argv(value: Any, *, key: str, where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not 1 <= len(value) <= 128:
        raise ConfigError(f"{where}: {key} must be a non-empty argv list")
    result: list[str] = []
    total = 0
    for index, arg in enumerate(value):
        if (not isinstance(arg, str) or not arg or len(arg) > 4_096
                or "\x00" in arg or any(ord(char) < 32 for char in arg)
                or _SHELL_META.search(arg)):
            raise ConfigError(f"{where}: {key} contains an unsafe argv value")
        if index == 0 and (any(char.isspace() for char in arg) or
                           posixpath.basename(arg).lower() in _SHELL_EXECUTABLES):
            raise ConfigError(f"{where}: {key} must not use a shell command string")
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", arg):
            raise ConfigError(f"{where}: {key} must not contain environment assignments")
        if _sensitive_arg(arg):
            raise ConfigError(f"{where}: {key} must not contain credential-like arguments")
        total += len(arg.encode("utf-8"))
        if total > 32 * 1_024:
            raise ConfigError(f"{where}: {key} exceeds the supported size")
        result.append(arg)
    if (posixpath.basename(result[0]).lower() in _SHELL_EXECUTABLES
            or (len(result) > 1 and result[0].rsplit("/", 1)[-1].lower()
                in _SHELL_EXECUTABLES and result[1] in {"-c", "/c", "-Command"})):
        raise ConfigError(f"{where}: {key} must not invoke a shell")
    return tuple(result)


def _description(value: Any, *, key: str, where: str) -> str:
    if (not isinstance(value, str) or not value or len(value) > 256
            or not re.fullmatch(r"[A-Za-z0-9 _.,:/@#*?+\[\]-]{1,256}", value)
            or _SENSITIVE_KEY.search(value)):
        raise ConfigError(f"{where}: {key} must be bounded structural text")
    return value.strip()


def _relay_configs(data: Any, *, where: str) -> tuple[RelayConfig, ...]:
    configs: list[RelayConfig] = []
    for name, table in _table_entries(
            data, section="relays", allowed=_RELAY_KEYS, where=where):
        _config_name(name, key="[relays] name", where=where)
        section = f"{where} [relays.{name}]"
        if "specs" not in table or not isinstance(table["specs"], list):
            raise ConfigError(f"{section}: specs must be a non-empty array of tables")
        raw_specs = table["specs"]
        if not 1 <= len(raw_specs) <= 16:
            raise ConfigError(f"{section}: specs must contain 1 to 16 entries")
        specs: list[RelaySpecConfig] = []
        spec_names: set[str] = set()
        spec_keys: set[tuple[Any, ...]] = set()
        for index, raw in enumerate(raw_specs):
            location = f"{section} specs[{index}]"
            if not isinstance(raw, dict):
                raise ConfigError(f"{location}: entry must be an inline table")
            unknown = sorted(set(raw) - _RELAY_SPEC_KEYS)
            if unknown:
                raise ConfigError(f"{location}: unknown key(s): " + ", ".join(unknown))
            if "kind" not in raw:
                raise ConfigError(f"{location}: kind is required")
            kind = _config_name(raw["kind"], key="kind", where=location)
            raw_name = raw.get("name")
            spec_name = (_config_name(raw_name, key="name", where=location)
                         if raw_name is not None else None)
            if spec_name is not None and spec_name in spec_names:
                raise ConfigError(f"{location}: duplicate relay spec name")
            if spec_name is not None:
                spec_names.add(spec_name)
            raw_options = raw.get("options", {})
            if not isinstance(raw_options, dict) or len(raw_options) > 32:
                raise ConfigError(f"{location}: options must be a bounded table")
            options: list[tuple[str, str | int | bool]] = []
            for option_key, option_value in raw_options.items():
                if (not isinstance(option_key, str)
                        or not _RELAY_OPTION_KEY.fullmatch(option_key)
                        or _SENSITIVE_KEY.search(option_key)
                        or option_key == "proxy_command"):
                    raise ConfigError(f"{location}: unsafe relay option key")
                if isinstance(option_value, str):
                    clean_value: str | int | bool = _structural(
                        option_value, key=f"options.{option_key}", where=location)
                elif isinstance(option_value, bool):
                    clean_value = option_value
                elif isinstance(option_value, int) and 0 <= option_value <= 65_535:
                    clean_value = option_value
                else:
                    raise ConfigError(f"{location}: relay options must be bounded scalars")
                options.append((option_key, clean_value))
            options.sort()
            signature = (kind, tuple(options))
            if signature in spec_keys:
                raise ConfigError(f"{location}: duplicate relay spec")
            spec_keys.add(signature)
            specs.append(RelaySpecConfig(kind, tuple(options), spec_name))

        kinds = tuple(spec.kind for spec in specs)
        raw_allowed = _str_list(table, "allowed_kinds", section)
        allowed = tuple(dict.fromkeys(kinds)) if raw_allowed is None else tuple(
            _config_name(item, key="allowed_kinds", where=section)
            for item in raw_allowed)
        if not allowed or len(allowed) > 16 or len(set(allowed)) != len(allowed):
            raise ConfigError(f"{section}: allowed_kinds must be a unique non-empty list")
        if not set(allowed).issubset(kinds):
            raise ConfigError(f"{section}: allowed_kinds must name configured spec kinds")

        timeout = table.get("max_timeout_s", 30)
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not 0 < timeout <= 86_400):
            raise ConfigError(f"{section}: max_timeout_s must be between 0 and 86400")
        output_limit = table.get("max_output_bytes", 4_096)
        if (isinstance(output_limit, bool) or not isinstance(output_limit, int)
                or not 0 <= output_limit <= 64 * 1_024):
            raise ConfigError(f"{section}: max_output_bytes is outside the supported range")

        raw_allowlist = table.get("command_allowlist")
        allowlist: tuple[tuple[str, ...], ...] | None = None
        if raw_allowlist is not None:
            if not isinstance(raw_allowlist, list) or not 1 <= len(raw_allowlist) <= 64:
                raise ConfigError(f"{section}: command_allowlist must be a bounded list")
            prefixes = tuple(_command_argv(
                prefix, key="command_allowlist", where=section)
                for prefix in raw_allowlist)
            if len(set(prefixes)) != len(prefixes):
                raise ConfigError(f"{section}: command_allowlist contains duplicates")
            allowlist = prefixes
        configs.append(RelayConfig(
            name=name, specs=tuple(specs), allowed_kinds=tuple(dict.fromkeys(allowed)),
            max_timeout_s=float(timeout), command_allowlist=allowlist,
            max_output_bytes=output_limit,
        ))
    return tuple(configs)


def _table_entries(data: Any, *, section: str, allowed: set[str],
                   where: str) -> list[tuple[str, dict[str, Any]]]:
    if data is None:
        return []
    if not isinstance(data, dict):
        raise ConfigError(f"{where}: [{section}] must contain named tables")
    if len(data) > 100:
        raise ConfigError(f"{where}: [{section}] exceeds the 100-table limit")
    result = []
    for name, table in data.items():
        _structural(name, key=f"[{section}] name", where=where)
        if not isinstance(table, dict):
            raise ConfigError(f"{where}: [{section}.{name}] must be a table")
        unknown = sorted(set(table) - allowed)
        if unknown:
            raise ConfigError(f"{where}: unknown key(s) in [{section}.{name}]: "
                              + ", ".join(unknown))
        result.append((name, table))
    return result


def _dedup(values: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


def _uncovered(dirs: list[str]) -> list[str]:
    """`dirs` without entries inside an earlier entry ("tests" after ".")."""
    out: list[str] = []
    for d in dirs:
        if not any(o == "." or d == o or d.startswith(o + "/") for o in out):
            out.append(d)
    return out


def _parse(root: Path, source: Path | None, data: dict[str, Any], *,
           extra_environment_names: Iterable[str] = (),
           extra_relay_names: Iterable[str] = ()) -> ProjectConfig:
    """Validate and resolve the legacy-shaped project configuration.

    `extra_*_names` name entries supplied by the other configuration file, so
    a service or runtime entry can reference an environment or relay that only
    the other file defines. They affect the cross-reference check only; the
    entries themselves are still validated in the file that defines them.
    """
    where = str(source) if source is not None else "defaults"
    for section, table in data.items():
        if section in {"environments", "services", "relays"}:
            if not isinstance(table, dict):
                raise ConfigError(f"{where}: [{section}] must contain named tables")
            continue
        if section == "runtime":
            if not isinstance(table, dict):
                raise ConfigError(f"{where}: [runtime] must be a table")
            unknown = sorted(set(table) - _RUNTIME_KEYS)
            if unknown:
                raise ConfigError(f"{where}: unknown key(s) in [runtime]: "
                                  + ", ".join(unknown))
            continue
        if section not in _KEYS:
            raise ConfigError(f"{where}: unknown section [{section}]")
        if not isinstance(table, dict):
            raise ConfigError(f"{where}: [{section}] must be a table")
        unknown = sorted(set(table) - _KEYS[section])
        if unknown:
            raise ConfigError(f"{where}: unknown key(s) in [{section}]: "
                              + ", ".join(unknown))
    index = data.get("index", {})
    memory = data.get("memory", {})
    adapters = data.get("adapters", {})
    iac = data.get("iac", {})
    review = data.get("review", {})
    review_layers = tuple(_dedup([
        _rel_dir(v, "layers", f"{where} [review]")
        for v in (_str_list(review, "layers", f"{where} [review]") or [])]))
    runtime = data.get("runtime", {})
    relay_configs = _relay_configs(data.get("relays"), where=where)
    relay_names = {relay.name for relay in relay_configs}
    known_relay_names = relay_names | set(extra_relay_names)

    def dirs(key: str, default: list[str]) -> tuple[str, ...]:
        got = _str_list(index, key, where)
        return _dedup([_rel_dir(v, key, where)
                       for v in (default if got is None else got)])

    def names(table: dict[str, Any], key: str, section: str) -> frozenset[str]:
        got = _str_list(table, key, f"{where} [{section}]") or []
        for v in got:
            if not v or "/" in v:
                raise ConfigError(f"{where}: [{section}] {key} entries are "
                                  f"directory names, got {v!r}")
        return frozenset(got)

    def strings(key: str, default: list[str]) -> tuple[str, ...]:
        got = _str_list(index, key, where)
        return tuple(default if got is None else got)

    source_dirs = dirs("source_dirs", ["."])
    test_dirs = dirs("test_dirs", ["tests"])
    map_dirs = dirs("map_dirs", _uncovered(list(source_dirs) + list(test_dirs)))
    skip = GENERIC_SKIP_DIRS | names(index, "skip_dirs", "index")
    memory_skip = skip | names(memory, "skip_dirs", "memory")
    key_docs = strings("key_docs", ["README.md"])
    default_prefixes = [d.split("/", 1)[0] for d in (*source_dirs, *test_dirs)
                        if d != "."] + ["docs"]
    prefixes = _dedup(list(strings("path_prefixes", default_prefixes)))
    for p in prefixes:
        if not p or "/" in p:
            raise ConfigError(f"{where}: path_prefixes entries are single path "
                              f"components, got {p!r}")
    modules = _str_list(adapters, "modules", f"{where} [adapters]") or []
    reads = data.get("reads", {})

    def count(key: str, default: int) -> int:
        value = reads.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ConfigError(f"{where}: [reads] {key} must be a "
                              f"non-negative integer")
        return value

    def policy(value: Any, what: str) -> str:
        if value not in REPEAT_POLICIES:
            raise ConfigError(f"{where}: [reads] {what} must be one of "
                              + ", ".join(f'"{p}"' for p in REPEAT_POLICIES))
        return value
    repeat = (policy(reads["repeat"], "repeat") if "repeat" in reads
              else None)
    by_harness = reads.get("repeat_by_harness", {})
    if not isinstance(by_harness, dict):
        raise ConfigError(f"{where}: [reads] repeat_by_harness must be a table")
    repeat_by_harness = tuple(sorted(
        (str(h), policy(v, f"repeat_by_harness.{h}"))
        for h, v in by_harness.items()))
    jobs_enabled = data.get("jobs", {}).get("enabled", False)
    if not isinstance(jobs_enabled, bool):
        raise ConfigError(f"{where}: [jobs] enabled must be true or false")
    tests = data.get("tests", {})
    tests_select = tests.get("select", "all")
    if tests_select not in TEST_SELECT_POLICIES:
        raise ConfigError(f"{where}: [tests] select must be one of "
                          + ", ".join(f'"{p}"' for p in TEST_SELECT_POLICIES))
    tests_cache = tests.get("cache", False)
    if not isinstance(tests_cache, bool):
        raise ConfigError(f"{where}: [tests] cache must be true or false")
    lint = data.get("lint", {})
    lint_enabled = lint.get("enabled")
    if lint_enabled is not None and not isinstance(lint_enabled, bool):
        raise ConfigError(f"{where}: [lint] enabled must be true or false")
    lint_timeout = lint.get("timeout_s", 3.0)
    if (isinstance(lint_timeout, bool) or not isinstance(lint_timeout, (int, float))
            or not 0 < lint_timeout <= 600):
        raise ConfigError(f"{where}: [lint] timeout_s must be a number of "
                          f"seconds in (0, 600]")
    lint_langs: dict[str, str | None] = {}
    for lang, tool in (("python", "ruff"), ("rust", "rustfmt")):
        value = lint.get(lang)
        if value is not None and value not in (tool, "off"):
            raise ConfigError(f'{where}: [lint] {lang} must be "{tool}" or "off"')
        lint_langs[lang] = value

    roots = _config_list(iac, "roots", f"{where} [iac]")
    iac_roots = _dedup([
        _repo_dir(v, key="roots", where=f"{where} [iac]") for v in roots
    ])
    configured_iac_skip_globs = [
        _path_pattern(v, key="skip_globs", where=f"{where} [iac]")
        for v in _config_list(iac, "skip_globs", f"{where} [iac]")
    ]
    iac_skip_globs = _dedup([
        *DEFAULT_IAC_SKIP_GLOBS, *configured_iac_skip_globs,
    ])

    raw_remote_states = iac.get("remote_states", [])
    if not isinstance(raw_remote_states, list):
        raise ConfigError(f"{where} [iac]: remote_states must be an array of tables")
    if len(raw_remote_states) > 256:
        raise ConfigError(f"{where} [iac]: remote_states exceeds the 256-item limit")
    remote_states: list[RemoteStateConfig] = []
    seen_remote_states: set[tuple[str, str]] = set()
    for mapping_index, mapping in enumerate(raw_remote_states):
        location = f"{where} [iac.remote_states[{mapping_index}]]"
        if not isinstance(mapping, dict):
            raise ConfigError(f"{location}: entry must be a table")
        unknown = sorted(set(mapping) - _REMOTE_STATE_KEYS)
        if unknown:
            raise ConfigError(f"{location}: unknown key(s): " + ", ".join(unknown))
        if set(mapping) != _REMOTE_STATE_KEYS:
            raise ConfigError(f"{location}: consumer, name and producer are required")
        consumer = _repo_dir(
            _structural(mapping["consumer"], key="consumer", where=location),
            key="consumer", where=location)
        name = _structural(mapping["name"], key="name", where=location)
        producer = _repo_dir(
            _structural(mapping["producer"], key="producer", where=location),
            key="producer", where=location)
        identity = (consumer, name)
        if identity in seen_remote_states:
            raise ConfigError(f"{location}: duplicate consumer/name mapping")
        seen_remote_states.add(identity)
        remote_states.append(RemoteStateConfig(consumer, name, producer))

    environments: list[EnvironmentConfig] = []
    for name, table in _table_entries(
            data.get("environments"), section="environments",
            allowed=_ENVIRONMENT_KEYS, where=where):
        section = f"{where} [environments.{name}]"

        def env_list(key: str, *, paths: bool = False) -> tuple[str, ...]:
            values = _config_list(table, key, section)
            clean = [(_repo_dir(v, key=key, where=section) if paths else
                      _structural(v, key=key, where=section)) for v in values]
            return _dedup(clean)

        def env_scalar(key: str) -> str | None:
            if key not in table:
                return None
            return _structural(table[key], key=key, where=section)

        environments.append(EnvironmentConfig(
            name=name, paths=env_list("paths", paths=True),
            namespaces=env_list("namespaces"),
            account_alias=env_scalar("account_alias"),
            region=env_scalar("region"), cluster=env_scalar("cluster"),
            risk=env_scalar("risk"), pipelines=env_list("pipelines"),
        ))
    environment_names = (
        {item.name for item in environments} | set(extra_environment_names))

    services: list[ServiceConfig] = []
    for name, table in _table_entries(
            data.get("services"), section="services", allowed=_SERVICE_KEYS,
            where=where):
        section = f"{where} [services.{name}]"

        def service_scalar(key: str, *, path: bool = False) -> str | None:
            if key not in table:
                return None
            if path:
                return _repo_dir(table[key], key=key, where=section)
            return _structural(table[key], key=key, where=section)

        values = _config_list(table, "values_globs", section)
        values_globs = _dedup([
            _path_pattern(value, key="values_globs", where=section)
            for value in values
        ])
        service_paths = _config_list(table, "paths", section)
        paths = _dedup([
            _repo_dir(value, key="paths", where=section)
            for value in service_paths
        ])
        environment = service_scalar("environment")
        if environment is not None and environment not in environment_names:
            raise ConfigError(f"{section}: environment {environment!r} is not configured")
        services.append(ServiceConfig(
            name=name, chart=service_scalar("chart", path=True),
            values_globs=values_globs,
            buildspec=service_scalar("buildspec", path=True),
            pipeline=service_scalar("pipeline", path=True),
            namespace=service_scalar("namespace"), environment=environment,
            paths=paths,
        ))

    runtime_services: list[RuntimeServiceConfig] = []
    for name, table in _table_entries(
            runtime.get("services"), section="runtime.services",
            allowed=_RUNTIME_SERVICE_KEYS, where=where):
        _config_name(name, key="[runtime.services] name", where=where)
        section = f"{where} [runtime.services.{name}]"
        if "command" not in table:
            raise ConfigError(f"{section}: command is required")
        command = _command_argv(table["command"], key="command", where=section)
        workdir = None
        if "workdir" in table:
            workdir = _repo_dir(table["workdir"], key="workdir", where=section)
        relay = None
        if "relay" in table:
            relay = _config_name(table["relay"], key="relay", where=section)
            if relay not in known_relay_names:
                raise ConfigError(f"{section}: relay {relay!r} is not configured")
        mode = table.get("mode", "background")
        if not isinstance(mode, str) or mode not in {"background", "foreground"}:
            raise ConfigError(f"{section}: mode must be background or foreground")
        description = (_description(
            table["description"], key="description", where=section)
            if "description" in table else "")
        runtime_services.append(RuntimeServiceConfig(
            name=name, command=command, workdir=workdir, relay=relay,
            mode=mode, description=description,
        ))

    runtime_tunnels: list[RuntimeTunnelConfig] = []
    for name, table in _table_entries(
            runtime.get("tunnels"), section="runtime.tunnels",
            allowed=_RUNTIME_TUNNEL_KEYS, where=where):
        _config_name(name, key="[runtime.tunnels] name", where=where)
        section = f"{where} [runtime.tunnels.{name}]"
        if "relay" not in table or "target" not in table:
            raise ConfigError(f"{section}: relay and target are required")
        relay = _config_name(table["relay"], key="relay", where=section)
        if relay not in known_relay_names:
            raise ConfigError(f"{section}: relay {relay!r} is not configured")
        target = table["target"]
        if not isinstance(target, str) or len(target) > 259:
            raise ConfigError(f"{section}: target must be HOST:PORT")
        match = _HOST_PORT.fullmatch(target)
        if match is None:
            raise ConfigError(f"{section}: target must be HOST:PORT")
        host = match.group("host")
        labels = host.rstrip(".").split(".")
        if (not host or len(host) > 253 or
                any(not re.fullmatch(
                    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
                    for label in labels)):
            raise ConfigError(f"{section}: target host is invalid")
        port = int(match.group("port"))
        if not 1 <= port <= 65_535:
            raise ConfigError(f"{section}: target port is outside 1..65535")
        local_port = table.get("local_port", 0)
        if (isinstance(local_port, bool) or not isinstance(local_port, int)
                or not 0 <= local_port <= 65_535):
            raise ConfigError(f"{section}: local_port is outside 0..65535")
        description = (_description(
            table["description"], key="description", where=section)
            if "description" in table else "")
        runtime_tunnels.append(RuntimeTunnelConfig(
            name=name, relay=relay, target=f"{host}:{port}",
            local_port=local_port, description=description,
        ))
    return ProjectConfig(
        root=root,
        source=source,
        source_dirs=source_dirs,
        map_dirs=map_dirs,
        test_dirs=test_dirs,
        skip_dirs=skip,
        memory_skip_dirs=memory_skip,
        docs=strings("docs", ["README.md", "docs/**/*.md"]),
        docs_all=strings("docs_all", ["**/*.md"]),
        key_docs=key_docs,
        command_docs=strings("command_docs", list(key_docs)),
        import_roots=dirs("import_roots", ["src"] if (root / "src").is_dir() else []),
        path_prefixes=prefixes,
        changed_exclude=strings("changed_exclude",
                                ["__pycache__/", ".pytest_cache/",
                                 f"{STATE_DIRNAME}/"]),
        entrypoint_prefixes=strings("entrypoint_prefixes",
                                    ["scripts/", "tools/", "bin/"]),
        adapters=tuple(m.strip() for m in modules if m.strip()),
        structured_outline_lines=count("structured_outline_lines", 2000),
        structured_outline_bytes=count("structured_outline_bytes", 64 * 1024),
        outline_lines=count("outline_lines", 400),
        outline_bytes=count("outline_bytes", 40 * 1024),
        repeat=repeat,
        repeat_by_harness=repeat_by_harness,
        jobs_enabled=jobs_enabled,
        tests_select=tests_select,
        tests_cache=tests_cache,
        lint_enabled=lint_enabled,
        lint_timeout_s=float(lint_timeout),
        lint_python=lint_langs["python"],
        lint_rust=lint_langs["rust"],
        iac_roots=iac_roots,
        iac_skip_globs=iac_skip_globs,
        remote_states=tuple(remote_states),
        environments=tuple(environments),
        services=tuple(services),
        relays=relay_configs,
        runtime_services=tuple(runtime_services),
        runtime_tunnels=tuple(runtime_tunnels),
        review_layers=review_layers,
    )


_CONFIG_CACHE: dict[tuple[Any, ...], ProjectConfig] = {}


def _read_source(path: Path | None) -> bytes | None:
    if path is None:
        return None
    try:
        return path.read_bytes()
    except OSError as exc:
        raise ConfigError(f"{path}: {exc}") from exc


def _source_signature(path: Path | None, raw: bytes | None) -> tuple[Any, ...]:
    """A cache-key component: path, mtime, size and content digest."""
    if path is None:
        return ("", 0, 0, "")
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        mtime = 0
    if raw is None:
        return (str(path), mtime, 0, "")
    return (str(path), mtime, len(raw), hashlib.sha256(raw).hexdigest())


def _toml_data(path: Path, raw: bytes | None) -> dict[str, Any]:
    if raw is None:
        return {}
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    if not isinstance(data, dict):  # tomllib always returns a mapping
        raise ConfigError(f"{path}: not a TOML table")
    return data


def _map_project_value(key: str, value: Any, *, source: Path) -> Any:
    """The legacy-shaped value, `_SKIP`, or a ConfigError naming `source`."""
    if key in {"testing.captured", "tests.select"}:
        if not isinstance(value, str):
            section, name = key.split(".", 1)
            raise ConfigError(
                f"{source}: [{section}] {name} must be a string")
        policy = value.strip().lower()
        if policy not in _CAPTURED_SELECT:
            section, name = key.split(".", 1)
            raise ConfigError(
                f"{source}: [{section}] {name} must be one of "
                + ", ".join(sorted(_CAPTURED_SELECT)))
        return _CAPTURED_SELECT[policy]
    if key in {"lint.python", "lint.rust"} and value == "auto":
        return _SKIP
    if key in _DERIVED_WHEN_EMPTY and value == []:
        return _SKIP
    return value


def _validate_registry_value(key: str, value: Any, *, source: Path) -> Any:
    """Validate a mapped value against the settings registry as well.

    The project-config parser still validates every value; this step catches
    registry-only rules (for example, source_dirs must not be empty or contain
    globs) so the versioned file has one schema, whichever module reads it.
    """
    if key == "reads.repeat_by_harness":
        if isinstance(value, dict):
            from ozagia.core import settings as settings_registry
            for harness, policy in value.items():
                try:
                    settings_registry.validate_file_value(
                        f"{key}.{harness}", policy,
                        where=f"{source}: [reads.repeat_by_harness] {harness}")
                except (settings_registry.SettingsError, ValueError) as exc:
                    message = str(exc)
                    if str(source) not in message:
                        message = f"{source}: {message}"
                    raise ConfigError(message) from exc
        return value
    from ozagia.core import settings as settings_registry
    section, _, name = key.partition(".")
    try:
        return settings_registry.validate_file_value(
            key, value, where=f"{source}: [{section}] {name}")
    except (settings_registry.SettingsError, ValueError) as exc:
        message = str(exc)
        if str(source) not in message:
            message = f"{source}: {message}"
        raise ConfigError(message) from exc


def _security_unmapped(table: dict[str, Any]) -> list[tuple[str, str]]:
    """Unknown or nested `[security]` keys, as bounded unmapped reports.

    Supported keys are consumed by `ozagia.core.security` from either file, so
    they are not reported. Values are never retained or printed.
    """
    out: list[tuple[str, str]] = []
    for name, value in table.items():
        if name in _SECURITY_KEY_SET and not isinstance(value, dict):
            continue
        reason = ("nested table is not mapped" if isinstance(value, dict)
                  else "not a supported security setting")
        out.append((f"security.{name}", reason))
    return out


def _project_config_values(
        data: dict[str, Any], *, source: Path,
) -> tuple[dict[str, Any], tuple[tuple[str, str], ...]]:
    """Map the supported registry keys in `data` to the legacy TOML shape.

    Values the project config does not consume are returned, with a reason, as
    unmapped entries. Their values are not retained: doctor reports key names,
    and the owning module still reads the setting itself.
    """
    out: dict[str, dict[str, Any]] = {}
    origins: dict[tuple[str, str], str] = {}
    unmapped: list[tuple[str, str]] = []
    for section, table in data.items():
        if section == SECURITY_SECTION:
            if not isinstance(table, dict):
                raise ConfigError(f"{source}: [{SECURITY_SECTION}] must be a table")
            for name, value in table.items():
                if name in _SECURITY_KEY_SET and not isinstance(value, dict):
                    _validate_registry_value(
                        f"{SECURITY_SECTION}.{name}", value, source=source)
            unmapped.extend(_security_unmapped(table))
            continue
        if section in _OWNED_SECTIONS:
            unmapped.append((section, _OWNED_SECTIONS[section]))
            continue
        if section in _PROJECT_MODEL_SECTIONS:
            # Named tables are passed through in the legacy shape; `_parse`
            # applies the same field, argv, port and cross-reference rules.
            # Both files are validated separately, so an error keeps the
            # path of the file that defines the entry.
            if not isinstance(table, dict):
                message = ("must be a table" if section == "runtime"
                           else "must contain named tables")
                raise ConfigError(f"{source}: [{section}] {message}")
            out[section] = table
            continue
        if section not in _PROJECT_CONFIG_SECTIONS:
            unmapped.append(
                (section, "not part of the project configuration"))
            continue
        if not isinstance(table, dict):
            raise ConfigError(f"{source}: [{section}] must be a table")
        for name, value in table.items():
            dotted = f"{section}.{name}"
            if isinstance(value, dict) and dotted != "reads.repeat_by_harness":
                unmapped.append((dotted, "nested table is not mapped"))
                continue
            target = _PROJECT_CONFIG_KEYS.get(dotted)
            if target is None:
                unmapped.append((dotted, _OWNED_KEYS.get(
                    dotted, "not mapped into the project configuration")))
                continue
            mapped = _map_project_value(dotted, value, source=source)
            if mapped is _SKIP:
                continue
            mapped = _validate_registry_value(dotted, mapped, source=source)
            if target in origins:
                raise ConfigError(
                    f"{source}: [{origins[target]}] and [{dotted}] define "
                    "the same value; use one")
            out.setdefault(target[0], {})[target[1]] = mapped
            origins[target] = dotted
    unmapped.sort()
    return out, tuple(unmapped)


def _legacy_values(
        data: dict[str, Any], *, source: Path | None = None,
) -> tuple[dict[str, Any], tuple[tuple[str, str], ...]]:
    """Legacy TOML minus the groups another module owns.

    `ozagia.core.security` reads `[security]` from `.ozagia.toml` directly;
    the project config reports that group instead of rejecting the file.
    A `[testing]` table is accepted as the registry's alias for `[tests]`
    `select`/`cache`, so an explicitly selected `ozagia/config.toml` (for
    example through `$OZAGIA_CONFIG` in a subprocess) also loads. Unknown
    sections still fail in `_parse`.
    """
    parsed: dict[str, Any] = {}
    unmapped: list[tuple[str, str]] = []
    for section, table in data.items():
        if section == "testing":
            continue
        if section == SECURITY_SECTION:
            # The security module owns this table and reads it from both
            # files. Report only what it cannot consume; supported keys are
            # no longer unmapped.
            if isinstance(table, dict):
                unmapped.extend(_security_unmapped(table))
            else:
                unmapped.append((SECURITY_SECTION, "must be a table"))
            continue
        if section in _OWNED_SECTIONS:
            unmapped.append((section, _OWNED_SECTIONS[section]))
            continue
        parsed[section] = table
    if "testing" in data:
        table = data["testing"]
        if not isinstance(table, dict):
            raise ConfigError(f"{source}: [testing] must be a table")
        mapped, more = _project_config_values(
            {"testing": table}, source=source or Path("configuration"))
        for target_section, values in mapped.items():
            existing = parsed.setdefault(target_section, {})
            for name, value in values.items():
                if name in existing:
                    raise ConfigError(
                        f"{source}: [testing] and [{target_section}] "
                        "define the same value; use one")
                existing[name] = value
        unmapped.extend(more)
    unmapped.sort()
    return parsed, tuple(unmapped)


def _table(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _raw_names(data: dict[str, Any], section: str) -> frozenset[str]:
    """Names in one project-model table, before field validation."""
    table = data.get(section)
    return frozenset(table) if isinstance(table, dict) else frozenset()


def _merge_named_entries(legacy_table: Any,
                         new_table: Any) -> dict[str, Any]:
    """Named project-model entries, legacy entries first and whole.

    A legacy entry wins over a versioned entry with the same name. Its fields
    are never combined with that versioned entry's fields: overridden entries
    keep the legacy meaning. Versioned-only entries are appended in their
    file's order.
    """
    merged = _table(legacy_table)
    for name, value in _table(new_table).items():
        merged.setdefault(name, value)
    return merged


def _merge_runtime(legacy_runtime: Any, new_runtime: Any) -> dict[str, Any]:
    """`[runtime]` with per-name services and tunnels; other keys are setdefault."""
    merged = _table(legacy_runtime)
    for name, value in _table(new_runtime).items():
        if name in _RUNTIME_KEYS:
            merged[name] = _merge_named_entries(merged.get(name), value)
        else:
            merged.setdefault(name, value)
    return merged


def _merge_legacy(new_data: dict[str, Any],
                  legacy_data: dict[str, Any]) -> dict[str, Any]:
    """Legacy-shaped data with new-config values only where legacy omits one.

    Flat tables merge per key (`legacy` wins). Project-model tables merge per
    named entry: `[environments.NAME]`, `[services.NAME]`, `[relays.NAME]`,
    `[runtime.services.NAME]` and `[runtime.tunnels.NAME]`. A legacy entry
    replaces the versioned entry with the same name as a whole; versioned-only
    entries are added. `_parse` validates each file before this merge, so an
    invalid versioned entry keeps the versioned file's path in its error.
    """
    merged = dict(legacy_data)
    for section, table in new_data.items():
        if section in {"environments", "services", "relays"}:
            merged[section] = _merge_named_entries(
                merged.get(section), table)
            continue
        if section == "runtime":
            merged[section] = _merge_runtime(
                merged.get(section), table)
            continue
        existing = merged.get(section)
        if not isinstance(existing, dict):
            existing = {}
        else:
            existing = dict(existing)
        merged[section] = existing
        for name, value in table.items():
            existing.setdefault(name, value)
    return merged


def _adapter_base(legacy_data: dict[str, Any], legacy: Path | None,
                  new_data: dict[str, Any], root: Path) -> Path | None:
    """The directory adapter modules are imported from.

    Legacy modules keep the historical base: the legacy file's directory. A
    module from the versioned repository config uses the repository root, so a
    migrated root-level adapter module keeps working.
    """
    legacy_adapters = legacy_data.get("adapters")
    if (legacy is not None and isinstance(legacy_adapters, dict)
            and "modules" in legacy_adapters):
        if (legacy.name == REPO_CONFIG_FILENAME
                and legacy.parent.name == REPO_CONFIG_DIRNAME):
            # An explicitly selected registry-schema file from another root.
            return legacy.parent.parent
        return legacy.parent
    if "adapters" in new_data:
        return root
    return None


def load_project_config(root: str | Path | None = None) -> ProjectConfig:
    """The project configuration for `root` (default `repo_root()`).

    The legacy file supplies every key it defines; the versioned
    `<root>/ozagia/config.toml` supplies the supported keys it leaves unset.
    Project models merge by named entry: a legacy `[environments.NAME]`,
    `[services.NAME]`, `[relays.NAME]`, `[runtime.services.NAME]` or
    `[runtime.tunnels.NAME]` entry wins as a whole, and versioned-only entries
    are added. Cached per resolved root, both configuration paths, their mtimes
    and the content digests, so editing either file or changing
    `$OZAGIA_CONFIG` takes effect on the next call. Raises ConfigError for a
    malformed file.
    """
    root_p = Path(root).resolve() if root is not None else repo_root()
    sources = config_sources(root_p)
    repo_path = sources.repo_config if sources.repo_exists else None
    legacy_raw = _read_source(sources.legacy)
    new_raw = _read_source(repo_path)
    legacy_signature = _source_signature(sources.legacy, legacy_raw)
    repo_signature = _source_signature(sources.repo_config, new_raw)
    key = (str(root_p), legacy_signature, repo_signature)
    cached = _CONFIG_CACHE.get(key)
    if cached is not None:
        return cached

    legacy_data, legacy_unmapped = _legacy_values(
        _toml_data(sources.legacy, legacy_raw)
        if sources.legacy is not None else {}, source=sources.legacy)
    new_data: dict[str, Any] = {}
    new_unmapped: tuple[tuple[str, str], ...] = ()
    if repo_path is not None:
        new_data, new_unmapped = _project_config_values(
            _toml_data(repo_path, new_raw), source=repo_path)

    # Each file is validated from its own text, so an invalid versioned entry
    # names the versioned path even when a legacy entry overrides it. Names
    # from the other file are allowed for cross-references only: a versioned
    # service may use a legacy environment or relay, and vice versa.
    extra_environments = (_raw_names(legacy_data, "environments")
                          | _raw_names(new_data, "environments"))
    extra_relays = (_raw_names(legacy_data, "relays")
                    | _raw_names(new_data, "relays"))
    if sources.legacy is not None:
        _parse(root_p, sources.legacy, legacy_data,
               extra_environment_names=extra_environments,
               extra_relay_names=extra_relays)
    if repo_path is not None and new_data:
        _parse(root_p, repo_path, new_data,
               extra_environment_names=extra_environments,
               extra_relay_names=extra_relays)
    unmapped = tuple(sorted(set(legacy_unmapped) | set(new_unmapped)))

    if repo_path is None:
        cfg = _parse(root_p, sources.legacy, legacy_data)
    else:
        cfg = _parse(root_p, sources.active,
                     _merge_legacy(new_data, legacy_data))
    adapter_base = _adapter_base(legacy_data, sources.legacy, new_data, root_p)
    cfg = replace(cfg, sources=sources.paths, unmapped=unmapped,
                  adapter_base_dir=adapter_base,
                  source_signatures=(legacy_signature, repo_signature))
    _CONFIG_CACHE[key] = cfg
    return cfg


def clear_config_cache() -> None:
    _CONFIG_CACHE.clear()
