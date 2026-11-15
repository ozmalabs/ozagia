"""Repository adapters: per-project extensions loaded at run time.

An adapter adds what only one project knows about, without ozagia importing
that project. It is a callable `register(registry)` that calls the methods
below. Adapters are found in two places, in this order:

- the `ozagia.adapters` entry-point group of installed packages;
- `$OZAGIA_ADAPTERS`, a comma-separated list of `module:attr` names, for an
  adapter that lives in the target repository (the module is imported with the
  target root on `sys.path`).

Extension points:

- `add_edit_engine(name, ops, apply)`: a Python edit engine. A script with
  `engine: <name>`, or one using any op in `ops`, goes to
  `apply(root, script, write=...)` instead of the built-in Python adapter.
- `add_indexer(fn)`: called during a graph build as
  `fn(builder, root, file_node)` after the built-in indexers, to add
  project-specific nodes and edges. `builder` is the build's node/edge builder;
  `file_node(rel)` returns the node id of a repository file, creating it if
  needed.
- `add_graph_query(name, fn)`: a named read-only query, exposed by
  `DevMemory.adapter_query(name, **kwargs)` as `fn(memory, **kwargs)`.
"""
from __future__ import annotations

import importlib
import os
import sys
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any, Callable, Iterable

ENV_ADAPTERS = "OZAGIA_ADAPTERS"
ENTRY_POINT_GROUP = "ozagia.adapters"


@dataclass
class Registry:
    edit_engines: dict[str, tuple[frozenset[str], Callable[..., Any]]] = field(
        default_factory=dict)
    indexers: list[Callable[..., Any]] = field(default_factory=list)
    graph_queries: dict[str, Callable[..., Any]] = field(default_factory=dict)
    loaded: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def add_edit_engine(self, name: str, ops: Iterable[str],
                        apply: Callable[..., Any]) -> None:
        self.edit_engines[str(name).lower()] = (frozenset(ops), apply)

    def add_indexer(self, fn: Callable[..., Any]) -> None:
        self.indexers.append(fn)

    def add_graph_query(self, name: str, fn: Callable[..., Any]) -> None:
        self.graph_queries[str(name)] = fn


_REGISTRY: Registry | None = None
_REGISTRY_ROOT: str | None = None


def _import_ref(ref: str, root: Path | None) -> Callable[..., Any]:
    module, _, attr = ref.partition(":")
    if not module or not attr:
        raise ValueError(f"adapter reference must be module:attr, got {ref!r}")
    added = False
    if root is not None and str(root) not in sys.path:
        sys.path.insert(0, str(root))
        added = True
    try:
        obj: Any = importlib.import_module(module)
    finally:
        if added:
            try:
                sys.path.remove(str(root))
            except ValueError:
                pass
    for part in attr.split("."):
        obj = getattr(obj, part)
    return obj


def registry(root: str | Path | None = None, *, reload: bool = False) -> Registry:
    """The adapter registry, loaded once per process (and per root)."""
    global _REGISTRY, _REGISTRY_ROOT
    key = str(Path(root).resolve()) if root is not None else None
    if _REGISTRY is not None and not reload and _REGISTRY_ROOT == key:
        return _REGISTRY
    reg = Registry()
    refs: list[tuple[str, Callable[[], Callable[..., Any]]]] = []
    for ep in entry_points(group=ENTRY_POINT_GROUP):
        refs.append((f"entry point {ep.name}", ep.load))
    for ref in filter(None, (r.strip() for r in os.environ.get(ENV_ADAPTERS, "").split(","))):
        refs.append((ref, lambda ref=ref: _import_ref(ref, Path(key) if key else None)))
    for name, load in refs:
        try:
            load()(reg)
            reg.loaded.append(name)
        except Exception as e:  # noqa: BLE001 - one bad adapter must not stop the others
            reg.errors.append(f"{name}: {type(e).__name__}: {e}")
    _REGISTRY, _REGISTRY_ROOT = reg, key
    return reg
