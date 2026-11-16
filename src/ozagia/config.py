"""Where ozagia works: the target repository, its state and its configuration.

The target repository is `$OZAGIA_ROOT` if set, else the Git top level of the
current directory, else the current directory. ozagia never assumes it is
installed inside the repository it works on.

State (graph snapshot, journal, tag cache, sessions, edit backups) lives in
`<root>/.ozagia/` unless `$OZAGIA_STATE_DIR` names another directory (an
absolute path, or one relative to the root). Add `.ozagia/` to the target's
`.gitignore`; the indexers skip it either way.

The project configuration (`load_project_config`) is a TOML file, the first of:
`$OZAGIA_CONFIG`, `<root>/.ozagia.toml`, `<state dir>/config.toml`. Without
one, every key has the generic default below. All keys are optional:

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

    [iac]
    roots = []                    # explicit Terraform/OpenTofu roots
    skip_globs = []               # extra IaC source files/directories to skip
    [[iac.remote_states]]
    consumer = "envs/app"
    name = "iam"                  # data "terraform_remote_state" label
    producer = "envs/iam"

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

Directory entries are paths relative to the root ("." is the root itself).
"""
from __future__ import annotations

import os
import posixpath
import re
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ENV_ROOT = "OZAGIA_ROOT"
ENV_STATE_DIR = "OZAGIA_STATE_DIR"
ENV_CONFIG = "OZAGIA_CONFIG"
STATE_DIRNAME = ".ozagia"
CONFIG_FILENAME = ".ozagia.toml"
STATE_CONFIG_FILENAME = "config.toml"
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
    iac_roots: tuple[str, ...] = ()
    iac_skip_globs: tuple[str, ...] = DEFAULT_IAC_SKIP_GLOBS
    remote_states: tuple[RemoteStateConfig, ...] = ()
    environments: tuple[EnvironmentConfig, ...] = ()
    services: tuple[ServiceConfig, ...] = ()

    @property
    def adapter_base(self) -> Path | None:
        """The directory adapter modules are imported from."""
        return self.source.parent if self.source is not None else None


_KEYS = {
    "index": {"source_dirs", "map_dirs", "test_dirs", "skip_dirs", "docs",
              "docs_all", "key_docs", "command_docs", "import_roots",
              "path_prefixes", "changed_exclude", "entrypoint_prefixes"},
    "memory": {"skip_dirs"},
    "adapters": {"modules"},
    "iac": {"roots", "skip_globs", "remote_states"},
}

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


def config_path(root: str | Path) -> Path | None:
    """The configuration file that applies to `root`, or None for defaults."""
    env = os.environ.get(ENV_CONFIG)
    if env:
        p = Path(env).expanduser().resolve()
        if not p.is_file():
            raise ConfigError(f"${ENV_CONFIG} names {p}, which is not a file")
        return p
    for p in (Path(root).resolve() / CONFIG_FILENAME,
              state_path(root, STATE_CONFIG_FILENAME)):
        if p.is_file():
            return p
    return None


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


def _parse(root: Path, source: Path | None, data: dict[str, Any]) -> ProjectConfig:
    where = str(source) if source is not None else "defaults"
    for section, table in data.items():
        if section in {"environments", "services"}:
            if not isinstance(table, dict):
                raise ConfigError(f"{where}: [{section}] must contain named tables")
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
    environment_names = {item.name for item in environments}

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
        iac_roots=iac_roots,
        iac_skip_globs=iac_skip_globs,
        remote_states=tuple(remote_states),
        environments=tuple(environments),
        services=tuple(services),
    )


_CONFIG_CACHE: dict[tuple[str, str, int], ProjectConfig] = {}


def load_project_config(root: str | Path | None = None) -> ProjectConfig:
    """The project configuration for `root` (default `repo_root()`).

    Cached per resolved root, configuration file and file mtime, so editing
    the file or changing `$OZAGIA_CONFIG` takes effect on the next call.
    Raises ConfigError for a malformed file.
    """
    root_p = Path(root).resolve() if root is not None else repo_root()
    path = config_path(root_p)
    mtime = path.stat().st_mtime_ns if path is not None else 0
    key = (str(root_p), str(path or ""), mtime)
    cached = _CONFIG_CACHE.get(key)
    if cached is not None:
        return cached
    data: dict[str, Any] = {}
    if path is not None:
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as e:
            raise ConfigError(f"{path}: {e}") from e
    cfg = _parse(root_p, path, data)
    _CONFIG_CACHE[key] = cfg
    return cfg


def clear_config_cache() -> None:
    _CONFIG_CACHE.clear()
