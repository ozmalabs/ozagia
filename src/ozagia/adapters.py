"""Repository adapters: per-project extensions loaded at run time.

An adapter adds what only one project knows about, without ozagia importing
that project. It is a callable `register(registry)` that calls the methods
below. Adapters are found in three places, in this order:

- the `ozagia.adapters` entry-point group of installed packages;
- `[adapters] modules` in the project configuration (see `ozagia.config`),
  each `module:attr` imported with the directory that contains the
  configuration file on `sys.path`. With `$OZAGIA_CONFIG` pointing at another
  checkout's config, that checkout's adapter applies to the target root;
- `$OZAGIA_ADAPTERS`, a comma-separated list of `module:attr` names, imported
  with the target root on `sys.path`.

`sys.path` is only extended while the module is imported. An adapter that
imports more of its own repository later must arrange that itself.

Extension points:

- `add_edit_engine(name, ops, apply)`: a Python edit engine. A script with
  `engine: <name>`, or one using any op in `ops`, goes to
  `apply(root, script, write=...)` instead of the built-in Python adapter.
- `add_indexer(fn, *, watch=(), texts=None)`: called once per graph build
  as `fn(ctx)`, where `ctx` is an `IndexContext`, after the built-in file,
  test and doc indexers and before the git history and failure baseline. The
  indexer adds project-specific nodes and edges through `ctx.builder`
  (`node`, `edge`, `find`, `find_nodes`, `attrs`) and `ctx.file_node(rel)`. A reingest
  (`ozagia.core.reingest`) runs the indexers again (an adapter pass) when
  a reingested file is one they depend on, which they declare:
  - `watch`: globs (root-relative, `**` allowed) of the files the indexer
    reads from disk itself. New, changed and deleted matches are pending
    changes, count in the tree digest, and run the pass.
  - `texts`: which indexed source, test and doc texts (`ctx.sources`,
    `ctx.tests`, `ctx.docs`) the indexer's output depends on. `None` (the
    default): any, so a reingest of any indexed file runs the pass.
    `False`: none. A regex (string or compiled): the indexer depends on a
    file only through the distinct strings the regex matches in it, and on
    the set of nodes (`IndexedTests.tests`) of a file with a match. The
    pass runs for a file whose distinct matches changed, or a matching file
    that gained a node; moving a match within a file does not run it.
  Core node keys an indexer looks up with `ctx.builder.find` are recorded:
  a reingest that adds or removes one runs the pass. An indexer that
  chooses nodes any other way (iterating every Test, `ctx.tests` of
  unmatched files) must leave `texts` as None.
  Attributes an indexer sets on core nodes (`ctx.builder.attrs` on a
  Symbol, Test, Doc or File node) are recorded and survive a reingest that
  replaces the node's parsed attributes.
- `add_graph_query(name, fn)`: a named read-only query, exposed by
  `DevMemory.adapter_query(name, **kwargs)` as `fn(memory, **kwargs)`.
- `add_mcp_tool(spec, handler)`: an extra MCP tool. `spec` is an MCP tool
  description (`name`, `description`, `inputSchema`); the server validates the
  arguments against `inputSchema` and calls `handler(memory, args)`, whose
  JSON-serialisable return value is the tool result. A name that clashes with
  a built-in tool is ignored.
"""
from __future__ import annotations

import importlib
import os
import re
import sys
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any, Callable, Iterable

ENV_ADAPTERS = "OZAGIA_ADAPTERS"
ENTRY_POINT_GROUP = "ozagia.adapters"


@dataclass
class IndexedSource:
    """A parsed source file: repo-relative path, File node id, text."""
    rel: str
    node: int
    text: str


@dataclass
class IndexedTests:
    """A parsed test file and the Test node ids it defines."""
    rel: str
    node: int
    text: str
    tests: list[int]


@dataclass
class IndexedDoc:
    """An indexed document: Doc node id, its File node id, text."""
    rel: str
    node: int
    file: int
    text: str


@dataclass
class IndexContext:
    """What an indexer sees of a graph build in progress."""
    builder: Any
    root: Path
    repo: int
    file_node: Callable[..., int]
    skipped: Callable[[str], bool]
    sources: list[IndexedSource]
    tests: list[IndexedTests]
    docs: list[IndexedDoc]
    options: dict[str, Any]


@dataclass(frozen=True)
class IndexerInputs:
    """What one indexer depends on (see `Registry.add_indexer`): `watch`
    globs, and `texts`: None (any indexed text), False (none) or a compiled
    regex (texts it matches)."""
    watch: tuple[str, ...] = ()
    texts: Any = None


@dataclass
class Registry:
    edit_engines: dict[str, tuple[frozenset[str], Callable[..., Any]]] = field(
        default_factory=dict)
    indexers: list[Callable[[IndexContext], Any]] = field(default_factory=list)
    # Parallel to `indexers`: what each one depends on.
    indexer_inputs: list[IndexerInputs] = field(default_factory=list)
    graph_queries: dict[str, Callable[..., Any]] = field(default_factory=dict)
    mcp_tools: dict[str, tuple[dict[str, Any], Callable[..., Any]]] = field(
        default_factory=dict)
    loaded: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def add_edit_engine(self, name: str, ops: Iterable[str],
                        apply: Callable[..., Any]) -> None:
        self.edit_engines[str(name).lower()] = (frozenset(ops), apply)

    def add_indexer(self, fn: Callable[[IndexContext], Any], *,
                    watch: Iterable[str] = (), texts: Any = None) -> None:
        if isinstance(watch, str):
            watch = (watch,)
        globs = tuple(str(g) for g in watch)
        if texts is True:
            texts = None
        elif isinstance(texts, str):
            texts = re.compile(texts)
        elif texts not in (None, False) and not hasattr(texts, "search"):
            raise ValueError("texts must be None, False, a regex or a string")
        self.indexers.append(fn)
        self.indexer_inputs.append(IndexerInputs(globs, texts))

    @property
    def watch_globs(self) -> list[str]:
        """Every indexer's `watch` globs, in order, without repeats."""
        return list(dict.fromkeys(g for i in self.indexer_inputs for g in i.watch))

    def add_graph_query(self, name: str, fn: Callable[..., Any]) -> None:
        self.graph_queries[str(name)] = fn

    def add_mcp_tool(self, spec: dict[str, Any],
                     handler: Callable[..., Any]) -> None:
        name = spec.get("name") if isinstance(spec, dict) else None
        if not isinstance(name, str) or not name:
            raise ValueError("MCP tool spec needs a string 'name'")
        schema = spec.get("inputSchema") or {"type": "object", "properties": {}}
        if not isinstance(schema, dict):
            raise ValueError(f"MCP tool {name!r}: inputSchema must be an object")
        self.mcp_tools[name] = ({**spec, "inputSchema": schema}, handler)


_REGISTRY: Registry | None = None
_REGISTRY_KEY: tuple | None = None


def _config_file_key(path: Path | None) -> tuple[str, int, int]:
    """``(path, mtime_ns, size)`` for one configuration reload key.

    A missing file keeps its path with zero mtime/size, so creating it changes
    the key. Stat only: the project-config loader already reads and caches the
    contents by digest.
    """
    if path is None:
        return ("", 0, 0)
    try:
        stat = path.stat()
    except OSError:
        return (str(path), 0, 0)
    return (str(path), stat.st_mtime_ns, stat.st_size)


def _import_ref(ref: str, base: Path | None) -> Callable[..., Any]:
    module, _, attr = ref.partition(":")
    if not module or not attr:
        raise ValueError(f"adapter reference must be module:attr, got {ref!r}")
    added = False
    if base is not None and str(base) not in sys.path:
        sys.path.insert(0, str(base))
        added = True
    try:
        obj: Any = importlib.import_module(module)
    finally:
        if added:
            try:
                sys.path.remove(str(base))
            except ValueError:
                pass
    for part in attr.split("."):
        obj = getattr(obj, part)
    return obj


def registry(root: str | Path | None = None, *, reload: bool = False) -> Registry:
    """The adapter registry for `root`, loaded once per process.

    It is loaded again when the root, either project configuration file
    (legacy or ``ozagia/config.toml``; path, mtime, size and content digest)
    or `$OZAGIA_ADAPTERS` changes. Editing the non-active file therefore
    invalidates the cache even when the other file supplies the active
    configuration.
    """
    global _REGISTRY, _REGISTRY_KEY
    from ozagia.config import (
        ConfigError, config_sources, load_project_config, repo_root,
    )

    root_p = Path(root).resolve() if root is not None else repo_root()
    cfg = None
    cfg_error = None
    try:
        cfg = load_project_config(root_p)
    except ConfigError as e:
        cfg_error = f"config: {e}"
    if cfg is not None:
        cfg_key: tuple = cfg.source_signatures
    else:
        try:
            sources = config_sources(root_p)
        except ConfigError:
            env_config = os.environ.get("OZAGIA_CONFIG")
            env_path = Path(env_config).expanduser() if env_config else None
            cfg_key = ("unresolved", _config_file_key(env_path))
        else:
            cfg_key = (_config_file_key(sources.legacy),
                       _config_file_key(sources.repo_config))
    env = os.environ.get(ENV_ADAPTERS, "")
    key = (str(root_p), cfg_key, env)
    if _REGISTRY is not None and not reload and _REGISTRY_KEY == key:
        return _REGISTRY
    reg = Registry()
    if cfg_error:
        reg.errors.append(cfg_error)
    refs: list[tuple[str, Callable[[], Callable[..., Any]]]] = []
    for ep in entry_points(group=ENTRY_POINT_GROUP):
        refs.append((f"entry point {ep.name}", ep.load))
    if cfg is not None:
        for ref in cfg.adapters:
            refs.append((ref, lambda ref=ref: _import_ref(ref, cfg.adapter_base)))
    for ref in filter(None, (r.strip() for r in env.split(","))):
        refs.append((ref, lambda ref=ref: _import_ref(ref, root_p)))
    for name, load in refs:
        try:
            load()(reg)
            reg.loaded.append(name)
        except Exception as e:  # noqa: BLE001 - one bad adapter must not stop the others
            reg.errors.append(f"{name}: {type(e).__name__}: {e}")
    _REGISTRY, _REGISTRY_KEY = reg, key
    return reg
