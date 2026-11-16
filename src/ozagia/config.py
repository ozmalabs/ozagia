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

    [reads]
    structured_outline_lines = 2000   # a read with no range of a .json,
    structured_outline_bytes = 65536  # .yaml, .yml or .toml file over either
                                      # limit returns its outline; 0 turns a
                                      # limit off
    outline_lines = 400               # the same for any other text file read
    outline_bytes = 40960             # by the daemon (read tool, `cat F`):
                                      # symbols/sections with keys, then the
                                      # first lines; 0 turns a limit off
    repeat = "stub" | "note" | "off"  # what a same-session re-read of an
                                      # unchanged range gets; unset: the
                                      # harness default (`ozagia.intent.ledger`)
    repeat_by_harness = {codex = "stub"}  # per-harness override of the
                                      # harness default (wins over `repeat`)

Directory entries are paths relative to the root ("." is the root itself).
"""
from __future__ import annotations

import os
import posixpath
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
    # None: each harness's default (`ozagia.intent.ledger.HARNESS_REPEAT`).
    repeat: str | None = None
    repeat_by_harness: tuple[tuple[str, str], ...] = ()

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
    "reads": {"structured_outline_lines", "structured_outline_bytes",
              "outline_lines", "outline_bytes", "repeat",
              "repeat_by_harness"},
}
REPEAT_POLICIES = ("stub", "note", "off")


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
