"""Deterministic, read-only inventory of Ozagia's own declared surfaces.

``collect_inventory`` introspects the installed Ozagia package, never a target
project.  It reads ``pyproject.toml``, walks the CLI parser, reads the MCP tool
lists and handler maps by AST (without instantiating the server, a graph
surface or loading a snapshot), parses the daemon protocol docstring, and asks
the settings registry for its read-only schema.  It does **not** load target
adapters: doing so would import project-supplied modules and execute target
code.  A caller that already has an adapter observation may inject it.

Nothing here imports a target project, builds an index, starts a daemon, runs a
subprocess, installs anything, contacts the network or writes a file.  When a
root is supplied, its project configuration is parsed read-only to list
declared adapter module references; those modules are never imported.

Every surface records ``id``, ``kind``, ``name``, ``source``, ``version``,
``declared``, ``observed`` and ``coverage``.  ``coverage`` is ``"observed"``
when the surface came from a live, read-only object and ``"unknown"`` when the
surface set may be incomplete (daemon declarations, entry points, adapters and
both statically observed MCP surfaces).  The report always says
``exhaustive: false``; a gap is reported rather than guessed away.
"""

from __future__ import annotations

import argparse
import ast
import re
import tomllib
from importlib import metadata
from pathlib import Path
from typing import Any, Iterable, Mapping

from ozagia.core.contracts import content_hash

SCHEMA_NAMESPACE = "ozagia.self_graph.inventory"
SCHEMA_VERSION = "1.0"
INVENTORY_VERSION = "1.0"

COVERAGE_OBSERVED = "observed"
COVERAGE_UNKNOWN = "unknown"

SURFACE_KINDS = (
    "adapter", "cli", "daemon", "entrypoint", "mcp", "settings",
)

_MCP_SERVER = Path("src/ozagia/integrations/mcp/server.py")
_MCP_GRAPH = Path("src/ozagia/integrations/mcp/graph_surface.py")
_MCP_ENGINE = Path("src/ozagia/core/graph_engine.py")
_DAEMON_PROTOCOL = Path("src/ozagia/daemon/protocol.py")
_CLI_SOURCE = Path("src/ozagia/cli.py")
_SETTINGS_SOURCE = Path("src/ozagia/core/settings.py")
_PYPROJECT_SOURCE = Path("pyproject.toml")


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _package_root() -> Path:
    """The Ozagia source tree, found by walking up to ``pyproject.toml``."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").is_file():
            return parent
    # Installed without a source tree: keep a stable, non-absolute label.
    return here.parents[3]


def _relative_source(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except (OSError, ValueError):
        return path.as_posix()


def _read_pyproject(root: Path) -> dict[str, Any]:
    path = root / "pyproject.toml"
    if not path.is_file():
        return {}
    with path.open("rb") as handle:
        return tomllib.load(handle)


def _tool_version(pyproject: Mapping[str, Any]) -> str:
    project = pyproject.get("project")
    if isinstance(project, Mapping):
        version = project.get("version")
        if version:
            return str(version)
    try:
        return metadata.version("ozagia")
    except Exception:  # noqa: BLE001 - a source tree may not be installed
        return "unknown"


def _tool_source(root: Path) -> str:
    import ozagia

    package = Path(ozagia.__file__).resolve().parent
    return _relative_source(package, root)


def _surface(*, surface_id: str, kind: str, name: str, source: str,
             version: str, declared: bool, observed: bool, coverage: str,
             details: Mapping[str, Any] | None = None) -> dict[str, Any]:
    if kind not in SURFACE_KINDS:
        raise ValueError(f"unknown surface kind {kind!r}")
    if coverage not in (COVERAGE_OBSERVED, COVERAGE_UNKNOWN):
        raise ValueError(f"unknown coverage {coverage!r}")
    return {
        "id": surface_id,
        "kind": kind,
        "name": name,
        "source": source,
        "version": version,
        "declared": bool(declared),
        "observed": bool(observed),
        "coverage": coverage,
        "details": dict(details or {}),
    }


def _gap(surface_id: str, kind: str, name: str, reason: str) -> dict[str, str]:
    return {"id": surface_id, "kind": kind, "name": name, "reason": reason}


def _as_name(value: Any) -> str:
    return value if isinstance(value, str) else str(value)


def _stringify_names(values: Any) -> list[str]:
    if values is None:
        return []
    if isinstance(values, Mapping):
        values = values.keys()
    if isinstance(values, str):
        values = [values]
    return sorted({_as_name(value) for value in values})


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _walk_cli(parser: argparse.ArgumentParser, version: str,
              path: tuple[str, ...] = ("ozagia",)) -> list[dict[str, Any]]:
    surface_id = "cli:" + ".".join(path)
    name = " ".join(path)
    subparsers = next(
        (action for action in parser._actions
         if isinstance(action, argparse._SubParsersAction)), None)
    children = sorted(subparsers.choices) if subparsers is not None else []

    options: set[str] = set()
    positionals: list[str] = []
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            continue
        if action.option_strings:
            options.update(option for option in action.option_strings
                           if option not in ("-h", "--help"))
        elif action.dest and action.dest != "help":
            positionals.append(str(action.dest))

    details: dict[str, Any] = {
        "options": sorted(options),
        "positionals": sorted(positionals),
        "subcommands": children,
        "takes_subcommand": subparsers is not None,
        "observation": "runtime",
    }
    surfaces = [_surface(
        surface_id=surface_id,
        kind="cli",
        name=name,
        source=_CLI_SOURCE.as_posix(),
        version=version,
        declared=True,
        observed=True,
        coverage=COVERAGE_OBSERVED,
        details=details,
    )]
    for child in children:
        surfaces.extend(_walk_cli(subparsers.choices[child], version,
                                  path + (child,)))
    return surfaces


def _cli_surfaces(version: str) -> list[dict[str, Any]]:
    from ozagia import cli

    return _walk_cli(cli.build_parser(), version)


# ---------------------------------------------------------------------------
# MCP
# ---------------------------------------------------------------------------


def _dict_literal_keys(node: ast.AST | None,
                       class_node: ast.ClassDef | None = None) -> list[str]:
    if node is None:
        return []
    if isinstance(node, ast.IfExp):
        return _dict_literal_keys(node.body, class_node)
    if not isinstance(node, ast.Dict):
        return []
    keys: list[str] = []
    for key, value in zip(node.keys, node.values):
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            keys.append(key.value)
        elif key is None:
            # ``**self._federation_handlers()`` and similar spreads.
            if (isinstance(value, ast.Call)
                    and isinstance(value.func, ast.Attribute)
                    and value.func.attr == "_federation_handlers"):
                keys.extend(_federation_handler_keys(class_node))
    return keys


def _method(node: ast.ClassDef, name: str) -> ast.FunctionDef | None:
    for item in node.body:
        if isinstance(item, ast.FunctionDef) and item.name == name:
            return item
    return None


def _federation_handler_keys(class_node: ast.ClassDef | None) -> list[str]:
    if class_node is None:
        return []
    method = _method(class_node, "_federation_handlers")
    if method is None:
        return []
    for item in ast.walk(method):
        if isinstance(item, ast.Return):
            return _dict_literal_keys(item.value, class_node)
    return []


def _handler_keys_from_ast(path: Path) -> tuple[list[str], str | None]:
    """Read ``DevMemoryServer._handlers`` keys without instantiating it."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError) as exc:
        return [], f"cannot parse {path.name}: {type(exc).__name__}: {exc}"
    class_node = next(
        (node for node in tree.body
         if isinstance(node, ast.ClassDef) and node.name == "DevMemoryServer"),
        None)
    if class_node is None:
        return [], "DevMemoryServer class not found"
    init = _method(class_node, "__init__")
    if init is None:
        return [], "DevMemoryServer.__init__ not found"
    keys: set[str] = set()
    for node in ast.walk(init):
        if isinstance(node, ast.Assign):
            targets = node.targets
            value = node.value
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
            value = node.value
        else:
            continue
        for target in targets:
            if (isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "self"
                    and target.attr == "_handlers"):
                if isinstance(value, ast.Call):
                    # The graph/engine surfaces are separate, memory-backed
                    # maps; the full handler map is a literal.
                    continue
                for name in _dict_literal_keys(value, class_node):
                    keys.add(name)
    return sorted(keys), None


def _tool_specs(tools: Iterable[Any]) -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, Mapping):
            continue
        name = tool.get("name")
        if not isinstance(name, str) or not name:
            continue
        schema = tool.get("inputSchema")
        properties: list[str] = []
        if isinstance(schema, Mapping):
            raw = schema.get("properties")
            if isinstance(raw, Mapping):
                properties = sorted(str(key) for key in raw)
        specs.append({"name": name, "parameters": properties})
    return specs


def _mcp_tools_from_module() -> list[dict[str, Any]]:
    from ozagia.integrations.mcp import server

    return _tool_specs(server._TOOLS)


def _graph_tools_from_module() -> list[dict[str, Any]]:
    from ozagia.integrations.mcp import graph_surface

    return _tool_specs(graph_surface.GRAPH_TOOLS)


def _graph_handler_keys_from_ast(path: Path) -> tuple[list[str], str | None]:
    """Read ``GraphSurface.handlers`` verb names without instantiating it."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError) as exc:
        return [], f"cannot parse {path.name}: {type(exc).__name__}: {exc}"
    class_node = next(
        (node for node in tree.body
         if isinstance(node, ast.ClassDef) and node.name == "GraphSurface"),
        None)
    if class_node is None:
        return [], "GraphSurface class not found"
    method = _method(class_node, "handlers")
    if method is None:
        return [], "GraphSurface.handlers not found"
    for node in ast.walk(method):
        if isinstance(node, ast.Assign):
            if any(isinstance(target, ast.Name) and target.id == "verbs"
                   for target in node.targets):
                keys = _dict_literal_keys(node.value, class_node)
                if keys:
                    return sorted(set(keys)), None
    return [], "GraphSurface.handlers verbs dict not found"


def _mcp_surfaces(tools: Iterable[Any], handler_keys: Iterable[str],
                  version: str, *, surface: str, id_prefix: str,
                  source: Path,
                  observation: str = "static-ast") -> tuple[
                      list[dict[str, Any]], list[dict[str, str]],
                      list[dict[str, str]]]:
    declared: dict[str, list[str]] = {}
    for tool in tools:
        if isinstance(tool, Mapping):
            name = tool.get("name")
            parameters = _stringify_names(tool.get("parameters"))
        else:
            name = tool
            parameters = []
        if not isinstance(name, str) or not name:
            continue
        declared[name] = parameters
    observed = {_as_name(name) for name in handler_keys}
    surfaces: list[dict[str, Any]] = []
    missing: list[dict[str, str]] = []
    undeclared: list[dict[str, str]] = []
    for name in sorted(set(declared) | observed):
        is_declared = name in declared
        is_observed = name in observed
        surfaces.append(_surface(
            surface_id=f"{id_prefix}{name}",
            kind="mcp",
            name=name,
            source=source.as_posix(),
            version=version,
            declared=is_declared,
            observed=is_observed,
            # Both MCP maps are static AST views; the runtime map depends on
            # mode and surface selection.
            coverage=COVERAGE_UNKNOWN,
            details={
                "surface": surface,
                "observation": observation,
                "parameters": declared.get(name, []),
            },
        ))
        if is_declared and not is_observed:
            missing.append(_gap(
                f"{id_prefix}{name}", "mcp", name,
                f"declared in {surface} tools but absent from the static "
                "handler map"))
        if is_observed and not is_declared:
            undeclared.append(_gap(
                f"{id_prefix}{name}", "mcp", name,
                f"handler present in {surface} but no declared tool entry"))
    return surfaces, missing, undeclared


def _mcp_default_parts() -> tuple[list[dict[str, Any]], list[str],
                                  str | None]:
    root = _package_root()
    handler_keys, error = _handler_keys_from_ast(root / _MCP_SERVER)
    return _mcp_tools_from_module(), handler_keys, error


def _graph_default_parts() -> tuple[list[dict[str, Any]], list[str],
                                    str | None]:
    root = _package_root()
    handler_keys, error = _graph_handler_keys_from_ast(root / _MCP_GRAPH)
    return _graph_tools_from_module(), handler_keys, error


def _engine_tools_from_module() -> list[dict[str, Any]]:
    from ozagia.core import graph_engine

    return _tool_specs(graph_engine.ENGINE_TOOLS)


def _engine_default_parts() -> tuple[list[dict[str, Any]], list[str],
                                     str | None]:
    from ozagia.core import graph_engine

    return _engine_tools_from_module(), [graph_engine.ENGINE_TOOL_NAME], None


# ---------------------------------------------------------------------------
# Daemon
# ---------------------------------------------------------------------------

_OP_RE = re.compile(r'"op"\s*:\s*"([a-z][a-z0-9_]*)"')


def _module_int_constant(tree: ast.Module, name: str) -> int | None:
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = node.targets
            value = node.value
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
            value = node.value
        else:
            continue
        if not any(isinstance(target, ast.Name) and target.id == name
                   for target in targets):
            continue
        if (isinstance(value, ast.Constant)
                and isinstance(value.value, int)
                and not isinstance(value.value, bool)):
            return value.value
    return None


def _daemon_surfaces(path: Path) -> tuple[list[dict[str, Any]],
                                          list[dict[str, str]]]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError) as exc:
        reason = f"cannot parse {path.name}: {type(exc).__name__}: {exc}"
        return [], [_gap("daemon:operations", "daemon", "operations", reason)]
    docstring = ast.get_docstring(tree) or ""
    names = sorted(set(_OP_RE.findall(docstring)))
    protocol = _module_int_constant(tree, "PROTOCOL_VERSION")
    version = str(protocol) if protocol is not None else "unknown"
    surfaces = [_surface(
        surface_id=f"daemon:op:{name}",
        kind="daemon",
        name=name,
        source=_DAEMON_PROTOCOL.as_posix(),
        version=version,
        declared=True,
        observed=False,
        coverage=COVERAGE_UNKNOWN,
        details={"documented": True, "cross_checked": False,
                 "protocol_version": version},
    ) for name in names]
    gaps = [_gap(
        f"daemon:op:{name}", "daemon", name,
        "declared in the protocol docstring; dispatch was not cross-checked")
        for name in names]
    if not names:
        gaps.append(_gap(
            "daemon:operations", "daemon", "operations",
            "no documented operations could be parsed"))
    return surfaces, gaps


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def _settings_surfaces(version: str) -> list[dict[str, Any]]:
    from ozagia.core import settings

    surfaces = []
    for row in settings.schema():
        if not isinstance(row, Mapping):
            continue
        key = row.get("key")
        if not isinstance(key, str) or not key:
            continue
        details = {
            "type": row.get("type"),
            "group": row.get("group"),
            "scope": row.get("scope"),
            "restart_required": row.get("restart_required"),
            "observation": "runtime",
        }
        surfaces.append(_surface(
            surface_id=f"settings:{key}",
            kind="settings",
            name=key,
            source=_SETTINGS_SOURCE.as_posix(),
            version=version,
            declared=True,
            observed=True,
            coverage=COVERAGE_OBSERVED,
            details=details,
        ))
    return surfaces


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def _entrypoint_surfaces(pyproject: Mapping[str, Any]) -> list[dict[str, Any]]:
    project = pyproject.get("project")
    if not isinstance(project, Mapping):
        return []
    surfaces: list[dict[str, Any]] = []
    scripts = project.get("scripts")
    if isinstance(scripts, Mapping):
        for name, target in scripts.items():
            surfaces.append(_surface(
                surface_id=f"entrypoint:script:{name}",
                kind="entrypoint",
                name=f"scripts.{name}",
                source=_PYPROJECT_SOURCE.as_posix(),
                version="unknown",
                declared=True,
                observed=False,
                coverage=COVERAGE_UNKNOWN,
                details={"group": "project.scripts", "target": str(target)},
            ))
    groups = project.get("entry-points")
    if isinstance(groups, Mapping):
        for group, entries in groups.items():
            if not isinstance(entries, Mapping):
                continue
            for name, target in entries.items():
                surfaces.append(_surface(
                    surface_id=f"entrypoint:{group}:{name}",
                    kind="entrypoint",
                    name=f"{group}:{name}",
                    source=_PYPROJECT_SOURCE.as_posix(),
                    version="unknown",
                    declared=True,
                    observed=False,
                    coverage=COVERAGE_UNKNOWN,
                    details={"group": str(group), "target": str(target)},
                ))
    return surfaces


# ---------------------------------------------------------------------------
# Adapters
# ---------------------------------------------------------------------------


def _observation_value(observation: Any, name: str,
                       default: Any) -> Any:
    if isinstance(observation, Mapping):
        return observation.get(name, default)
    return getattr(observation, name, default)


def _adapter_surfaces(observation: Any = None) -> tuple[
        list[dict[str, Any]], list[dict[str, str]]]:
    if observation is None:
        return [_surface(
            surface_id="adapter:registry",
            kind="adapter",
            name="adapter-registry",
            source="adapter-registry",
            version="unknown",
            declared=False,
            observed=False,
            coverage=COVERAGE_UNKNOWN,
            details={
                "imported": False,
                "reason": "loading adapters would execute target-project code",
            },
        )], [_gap(
            "adapter:registry", "adapter", "adapter-registry",
            "adapter coverage is unknown because imports were not performed")]
    loaded = _stringify_names(_observation_value(observation, "loaded", []))
    errors = _stringify_names(_observation_value(observation, "errors", []))
    mcp_tools = _stringify_names(
        _observation_value(observation, "mcp_tools", {}))
    queries = _stringify_names(
        _observation_value(observation, "graph_queries", {}))
    engines = _stringify_names(
        _observation_value(observation, "edit_engines", {}))
    surfaces: list[dict[str, Any]] = []
    gaps: list[dict[str, str]] = []

    def add(surface_id: str, name: str, details: Mapping[str, Any]) -> None:
        surfaces.append(_surface(
            surface_id=surface_id,
            kind="adapter",
            name=name,
            source="adapter-registry",
            version="unknown",
            declared=True,
            observed=True,
            coverage=COVERAGE_UNKNOWN,
            details=details,
        ))
        gaps.append(_gap(surface_id, "adapter", name,
                         "adapter-supplied surface; coverage is unknown"))

    for name in loaded:
        add(f"adapter:module:{name}", name, {"surface": "module"})
    for name in mcp_tools:
        add(f"adapter:mcp:{name}", name,
            {"surface": "mcp", "module": None})
    for name in queries:
        add(f"adapter:query:{name}", name,
            {"surface": "graph_query", "module": None})
    for name in engines:
        add(f"adapter:engine:{name}", name,
            {"surface": "edit_engine", "module": None})
    for index, error in enumerate(sorted(errors)):
        gaps.append(_gap(
            f"adapter:error:{index}", "adapter", "adapter-error",
            f"adapter load error: {error}"))
    return surfaces, gaps


def _declared_adapter_surfaces(root: str | Path) -> tuple[
        list[dict[str, Any]], list[dict[str, str]]]:
    """List adapter declarations read-only; never import a declared module."""
    from ozagia.config import load_project_config

    try:
        config = load_project_config(root)
    except Exception as exc:  # noqa: BLE001 - a target config must not fail us
        surfaces, gaps = _adapter_surfaces(None)
        gaps.append(_gap(
            "adapter:config", "adapter", "adapter-config",
            f"cannot read adapter declarations: {type(exc).__name__}: {exc}"))
        return surfaces, gaps
    refs = tuple(dict.fromkeys(str(ref) for ref in config.adapters))
    if not refs:
        return _adapter_surfaces(None)
    source = config.source or config.adapter_base
    source_text = (str(source) if source is not None
                   else str(Path(root).resolve()))
    surfaces: list[dict[str, Any]] = []
    gaps: list[dict[str, str]] = []
    for ref in refs:
        surface_id = f"adapter:declared:{ref}"
        surfaces.append(_surface(
            surface_id=surface_id,
            kind="adapter",
            name=ref,
            source=source_text,
            version="unknown",
            declared=True,
            observed=False,
            coverage=COVERAGE_UNKNOWN,
            details={
                "imported": False,
                "surface": "declared-module",
                "source": source_text,
                "reason": ("declared but not imported; importing would "
                           "execute target-project code"),
            },
        ))
        gaps.append(_gap(
            surface_id, "adapter", ref,
            "declared but not imported; coverage is unknown"))
    return surfaces, gaps


# ---------------------------------------------------------------------------
# Report assembly
# ---------------------------------------------------------------------------


def build_inventory(*, tool_version: str, tool_source: str,
                    surfaces: Iterable[Mapping[str, Any]],
                    declared_not_observed: Iterable[Mapping[str, Any]] = (),
                    observed_not_declared: Iterable[Mapping[str, Any]] = (),
                    unverified: Iterable[Mapping[str, Any]] = ()) -> dict[str, Any]:
    """Assemble a sorted inventory report from already-collected surfaces."""
    by_id: dict[str, dict[str, Any]] = {}
    duplicate_gaps: list[dict[str, str]] = []
    for surface in surfaces:
        item = dict(surface)
        surface_id = item["id"]
        if surface_id in by_id:
            duplicate_gaps.append(_gap(
                surface_id, str(item.get("kind", "unknown")),
                str(item.get("name", surface_id)),
                "duplicate surface declaration; first one kept"))
            continue
        by_id[surface_id] = item
    ordered = [by_id[key] for key in sorted(by_id, key=lambda key: (
        str(by_id[key].get("kind", "")), key))]

    def normalize_gaps(items: Iterable[Mapping[str, Any]]
                       ) -> list[dict[str, Any]]:
        result = [dict(item) for item in items]
        result.sort(key=lambda item: (
            str(item.get("kind", "")), str(item.get("id", "")),
            str(item.get("reason", ""))))
        return result

    gaps = {
        "declared_not_observed": normalize_gaps(declared_not_observed),
        "observed_not_declared": normalize_gaps(observed_not_declared),
        "unverified": normalize_gaps([*unverified, *duplicate_gaps]),
    }
    counts_by_kind: dict[str, int] = {}
    coverage_by_kind: dict[str, dict[str, int]] = {}
    for surface in ordered:
        kind = str(surface["kind"])
        counts_by_kind[kind] = counts_by_kind.get(kind, 0) + 1
        bucket = coverage_by_kind.setdefault(kind, {
            COVERAGE_OBSERVED: 0, COVERAGE_UNKNOWN: 0})
        bucket[str(surface["coverage"])] += 1
    return {
        "schema": {"namespace": SCHEMA_NAMESPACE, "version": SCHEMA_VERSION},
        "inventory_version": INVENTORY_VERSION,
        "tool": {"name": "ozagia", "version": tool_version,
                 "source": tool_source},
        "surfaces": ordered,
        "gaps": gaps,
        "coverage": {
            "exhaustive": False,
            "note": ("Generated from declared and statically observed "
                     "surfaces; MCP handler maps, adapters and daemon "
                     "dispatch coverage can be unknown."),
            "by_kind": {kind: coverage_by_kind[kind]
                        for kind in sorted(coverage_by_kind)},
        },
        "counts": {
            "surfaces": len(ordered),
            "by_kind": {kind: counts_by_kind[kind]
                        for kind in sorted(counts_by_kind)},
            "gaps": {name: len(items) for name, items in gaps.items()},
        },
    }


def collect_inventory(root: str | Path | None = None, *,
                      mcp_tools: Iterable[Any] | None = None,
                      mcp_handler_keys: Iterable[str] | None = None,
                      adapter_registry_result: Any = None) -> dict[str, Any]:
    """Collect the deterministic surface inventory.

    The inventory always describes the running Ozagia package.  ``root`` is
    used only to parse adapter *declarations* read-only; declared modules are
    never imported because that would execute project-supplied code.  With no
    root, or with no declared adapters, one blanket ``adapter:registry``
    unknown surface is reported.  The keyword arguments are injection points
    for tests and diagnostics: a fake MCP tool list/handler map or a pre-built
    adapter registry observation.  An injected adapter observation is a
    caller-supplied runtime view and takes precedence over declarations; the
    two are never merged.
    """
    package = _package_root()
    pyproject = _read_pyproject(package)
    tool_version = _tool_version(pyproject)
    tool_source = _tool_source(package)

    surfaces: list[dict[str, Any]] = []
    surfaces.extend(_cli_surfaces(tool_version))
    surfaces.extend(_settings_surfaces(tool_version))
    surfaces.extend(_entrypoint_surfaces(pyproject))

    daemon_surfaces, daemon_gaps = _daemon_surfaces(
        package / _DAEMON_PROTOCOL)
    surfaces.extend(daemon_surfaces)

    handler_error: str | None = None
    if mcp_tools is None or mcp_handler_keys is None:
        default_tools, default_handlers, handler_error = _mcp_default_parts()
        tools = default_tools if mcp_tools is None else mcp_tools
        handlers = (default_handlers if mcp_handler_keys is None
                    else mcp_handler_keys)
    else:
        tools, handlers = mcp_tools, mcp_handler_keys
    mcp_surface_list, missing, undeclared = _mcp_surfaces(
        tools, handlers, tool_version, surface="full", id_prefix="mcp:tool:",
        source=_MCP_SERVER)
    if handler_error:
        missing.append(_gap(
            "mcp:handlers", "mcp", "handler-map",
            f"handler map could not be read: {handler_error}"))
    surfaces.extend(mcp_surface_list)

    graph_tools, graph_handlers, graph_error = _graph_default_parts()
    graph_surfaces, graph_missing, graph_undeclared = _mcp_surfaces(
        graph_tools, graph_handlers, tool_version, surface="graph",
        id_prefix="mcp:graph:", source=_MCP_GRAPH)
    if graph_error:
        graph_missing.append(_gap(
            "mcp:graph:handlers", "mcp", "graph-handler-map",
            f"graph handler map could not be read: {graph_error}"))
    surfaces.extend(graph_surfaces)
    missing.extend(graph_missing)
    undeclared.extend(graph_undeclared)

    engine_tools, engine_handlers, engine_error = _engine_default_parts()
    engine_surfaces, engine_missing, engine_undeclared = _mcp_surfaces(
        engine_tools, engine_handlers, tool_version, surface="engine",
        id_prefix="mcp:tool:", source=_MCP_ENGINE)
    if engine_error:
        engine_missing.append(_gap(
            "mcp:tool:graph", "mcp", "graph",
            f"engine tool schema could not be read: {engine_error}"))
    surfaces.extend(engine_surfaces)
    missing.extend(engine_missing)
    undeclared.extend(engine_undeclared)

    if adapter_registry_result is not None:
        adapter_surfaces, adapter_gaps = _adapter_surfaces(
            adapter_registry_result)
    elif root is not None:
        adapter_surfaces, adapter_gaps = _declared_adapter_surfaces(root)
    else:
        adapter_surfaces, adapter_gaps = _adapter_surfaces(None)
    surfaces.extend(adapter_surfaces)

    entrypoint_gaps = [
        _gap(surface["id"], "entrypoint", surface["name"],
             "declared entry point target was not imported")
        for surface in surfaces if surface["kind"] == "entrypoint"
    ]
    return build_inventory(
        tool_version=tool_version,
        tool_source=tool_source,
        surfaces=surfaces,
        declared_not_observed=missing,
        observed_not_declared=undeclared,
        unverified=[*daemon_gaps, *adapter_gaps, *entrypoint_gaps],
    )


def inventory_hash(report: Mapping[str, Any]) -> str:
    """SHA-256 of the canonical inventory report."""
    return content_hash(dict(report))


__all__ = [
    "COVERAGE_OBSERVED", "COVERAGE_UNKNOWN", "INVENTORY_VERSION",
    "SCHEMA_NAMESPACE", "SCHEMA_VERSION", "SURFACE_KINDS", "build_inventory",
    "collect_inventory", "inventory_hash",
]
