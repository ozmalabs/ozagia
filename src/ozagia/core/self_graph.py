"""Read-only projection joining the capability catalogue to observed surfaces.

This is the M1 self-graph: a *logical view* over owners that already exist.  It
does not replace a store, refresh a snapshot, start a daemon, probe a tool or
import a target project.  It reads:

* the explicit catalogue in :mod:`ozagia.core.capabilities`;
* the generated surface inventory in :mod:`ozagia.core.inventory`;
* the static toolchain registry (data only, no probing);
* the section names emitted by ``ozagia.core.status`` (source AST only; the
  function is not called because it may probe a daemon);
* the daemon's :data:`ozagia.daemon.protocol.OPERATIONS` vocabulary.

Everything is joined into capability -> implementation -> surface -> dependency
-> test/doc nodes and edges with an explicit state.  Partial and error states
survive: an unreadable handler map is ``unknown`` and is never reported as an
absence.

Public entry points
-------------------

``collect_observations(root=None)``
    The observed half of the join, with one emitter envelope per source.
``build_self_graph(...)``
    The full deterministic projection (nodes, edges, observations, audit).
``capability_list(...)`` / ``capability_show(...)`` / ``audit_self_graph(...)``
    Bounded, paginated views used by the CLI and the ``dev_capabilities`` MCP
    tool.

Canonical JSON, content hashes and the contract truncation envelope are used
throughout.  No function reads the clock.
"""

from __future__ import annotations

import ast
import hashlib
import threading
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from ozagia.core import capabilities as K
from ozagia.core import contracts as C
from ozagia.core import inventory as I

SCHEMA_NAMESPACE = "ozagia.self_graph.capabilities"
SCHEMA_VERSION = "1.0"
OBSERVATION_NAMESPACE = "ozagia.self_graph.observation"
REPORT_VERSION = "1.0"

SURFACE_KINDS = (
    "adapter", "cli", "daemon", "entrypoint", "mcp", "settings", "status",
    "toolchain",
)
REQUIRED_SURFACE_KINDS = ("cli", "daemon", "mcp", "status", "toolchain")
EXCLUDED_SURFACE_KINDS = ("adapter", "entrypoint", "settings")
NODE_KINDS = ("capability", "dependency", "doc", "implementation", "surface",
              "test")
EDGE_TYPES = (
    "backs", "documented_by", "exposes", "implements", "requires", "tested_by",
    "verified_by",
)
EDGE_STATES = ("observed", "declared", "missing", "undeclared", "unknown",
               "absent")
AUDIT_STATES = ("current", "partial", "unknown")
OBSERVATION_STATES = ("observed", "unknown")

EDGE_STATE_RANK = {
    "observed": 0, "declared": 1, "absent": 2, "missing": 3,
    "undeclared": 4, "unknown": 5,
}

DEFAULT_LIMIT = 25
MAX_LIMIT = 200
MAX_GRAPH_NODES = 2_000
MAX_GRAPH_EDGES = 4_000
MAX_ERROR_TEXT = 300
MAX_CURSOR = 64
MAX_AST_SYMBOL_CACHE = 256

_STATUS_SOURCE = "src/ozagia/core/status.py"
_TOOLCHAIN_SOURCE = "src/ozagia/core/toolchain.py"
_DAEMON_SOURCE = "src/ozagia/daemon/protocol.py"
_CATALOGUE_SOURCE = "src/ozagia/core/capabilities.py"
_INVENTORY_SOURCE = "src/ozagia/core/inventory.py"
_DISPATCH_SOURCE = "src/ozagia/daemon/server.py"

_STATUS_ENVELOPE = frozenset({
    "schema", "checked_at", "root", "ok", "errors", "degraded",
})

# Source-content keyed by a fresh digest on every read. This avoids reparsing
# the same package files across repeated self-graph requests while ensuring a
# source edit immediately invalidates the cached symbol list.
_AST_SYMBOL_CACHE: dict[str, tuple[str, frozenset[str], str | None]] = {}
_AST_SYMBOL_CACHE_LOCK = threading.Lock()


class SelfGraphError(ValueError):
    """A malformed request or an unreadable projection input."""


# ---------------------------------------------------------------------------
# Small deterministic helpers
# ---------------------------------------------------------------------------


def _clean_error(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}".replace("\n", " ").replace("\r", " ")
    return text[:MAX_ERROR_TEXT]


def _source_label(path: Path, root: Path | None) -> str:
    if root is not None:
        try:
            return path.resolve().relative_to(root.resolve()).as_posix()
        except (OSError, ValueError):
            pass
    return path.as_posix()


def package_dir() -> Path:
    """The installed/checked-out ``ozagia`` package directory."""
    return Path(__file__).resolve().parents[1]


def repo_root() -> Path | None:
    """The Ozagia source repository, when the package is a source checkout.

    This is the *Ozagia* repository, never a target project.  Returns ``None``
    for an installed package with no ``pyproject.toml`` above it, in which case
    test/doc references are reported ``unknown`` rather than missing.
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").is_file() and (
                parent / "src" / "ozagia").is_dir():
            return parent
    return None


def _package_source(relative: str) -> Path:
    return package_dir() / relative


# ---------------------------------------------------------------------------
# Implementation resolution (static, no import)
# ---------------------------------------------------------------------------


def _module_file(parts: Sequence[str]) -> tuple[Path | None, tuple[str, ...]]:
    """Resolve the longest importable module prefix to a source file."""
    base = package_dir()
    for count in range(len(parts), -1, -1):
        candidate = base.joinpath(*parts[:count])
        module = candidate.with_suffix(".py")
        if module.is_file():
            return module, tuple(parts[count:])
        package = candidate / "__init__.py"
        if package.is_file():
            return package, tuple(parts[count:])
    return None, tuple(parts)


def _ast_symbols(path: Path) -> tuple[set[str], str | None]:
    try:
        source = path.read_bytes()
        digest = hashlib.sha256(source).hexdigest()
    except (OSError, SyntaxError, UnicodeError) as exc:
        return set(), _clean_error(exc)
    cache_key = str(path.resolve())
    with _AST_SYMBOL_CACHE_LOCK:
        cached = _AST_SYMBOL_CACHE.get(cache_key)
    if cached is not None and cached[0] == digest:
        return set(cached[1]), cached[2]
    try:
        tree = ast.parse(source.decode("utf-8"))
    except (OSError, SyntaxError, UnicodeError) as exc:
        error = _clean_error(exc)
        _cache_ast_symbols(cache_key, digest, (), error)
        return set(), error
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            names.add(node.name)
            if isinstance(node, ast.ClassDef):
                for child in node.body:
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        names.add(child.name)
    _cache_ast_symbols(cache_key, digest, names, None)
    return set(names), None


def _cache_ast_symbols(key: str, digest: str, names: Iterable[str],
                       error: str | None) -> None:
    """Insert one parse result and evict atomically within the cache bound."""
    with _AST_SYMBOL_CACHE_LOCK:
        if (key not in _AST_SYMBOL_CACHE
                and len(_AST_SYMBOL_CACHE) >= MAX_AST_SYMBOL_CACHE):
            _AST_SYMBOL_CACHE.pop(next(iter(_AST_SYMBOL_CACHE)))
        _AST_SYMBOL_CACHE[key] = (digest, frozenset(names), error)


def _probe_implementation(impl_id: str) -> dict[str, Any]:
    """Check that an ``impl:`` reference names a source module/symbol.

    ``impl:ozagia.core.memory.context`` resolves the longest module prefix
    (``ozagia/core/memory.py``) and then looks for ``context`` as a top-level
    function/class or a class method.  A module-only reference
    (``impl:ozagia.core.session``) is observed when the module file exists.
    """
    try:
        parsed = C.parse_id(impl_id)
    except C.ContractError as exc:
        return {"id": impl_id, "state": "unknown", "module": None,
                "symbol": None, "source": None,
                "reason": f"invalid implementation id: {exc}"}
    body = parsed.canonical.split(":", 1)[1]
    parts = body.split(".")
    if not parts or parts[0] != "ozagia":
        return {"id": parsed.canonical, "state": "unknown", "module": None,
                "symbol": None, "source": None,
                "reason": "implementation is outside the ozagia package"}
    module_file, symbol_parts = _module_file(tuple(parts[1:]))
    if module_file is None:
        return {"id": parsed.canonical, "state": "missing", "module": body,
                "symbol": ".".join(symbol_parts) or None, "source": None,
                "reason": "no source file matches the implementation module"}
    source = _source_label(module_file, repo_root())
    if not symbol_parts:
        return {"id": parsed.canonical, "state": "observed", "module": body,
                "symbol": None, "source": source, "reason": None}
    names, error = _ast_symbols(module_file)
    if error is not None:
        return {"id": parsed.canonical, "state": "unknown", "module": body,
                "symbol": ".".join(symbol_parts), "source": source,
                "reason": f"cannot parse implementation module: {error}"}
    symbol = symbol_parts[-1]
    if symbol not in names:
        return {"id": parsed.canonical, "state": "missing", "module": body,
                "symbol": ".".join(symbol_parts), "source": source,
                "reason": f"symbol {symbol!r} is not defined in {source}"}
    return {"id": parsed.canonical, "state": "observed", "module": body,
            "symbol": ".".join(symbol_parts), "source": source, "reason": None}


def _ref_path(ref: str) -> str:
    body = ref.split(":", 1)[1]
    for separator in ("::", "#"):
        if separator in body:
            body = body.split(separator, 1)[0]
    return body


def _probe_ref(ref: str) -> dict[str, Any]:
    """Check a ``test:`` or ``doc:`` reference as a relative repository path."""
    root = repo_root()
    if root is None:
        return {"id": ref, "state": "unknown", "path": _ref_path(ref),
                "reason": "the Ozagia source tree is not available"}
    path = _ref_path(ref)
    if path.startswith("/") or ".." in path.split("/"):
        return {"id": ref, "state": "missing", "path": path,
                "reason": "reference escapes the repository"}
    full = root / path
    if full.is_file():
        return {"id": ref, "state": "observed", "path": path, "reason": None}
    return {"id": ref, "state": "missing", "path": path,
            "reason": f"referenced file does not exist: {path}"}


# ---------------------------------------------------------------------------
# Observations
# ---------------------------------------------------------------------------


def _normalize_surface(raw: Any, origin: str,
                       errors: list[dict[str, str]]) -> dict[str, Any] | None:
    if not isinstance(raw, Mapping):
        errors.append({"source": origin,
                       "reason": "surface is not a mapping"})
        return None
    surface_id = raw.get("id")
    kind = raw.get("kind")
    name = raw.get("name")
    if (not isinstance(surface_id, str) or not surface_id
            or not isinstance(kind, str) or kind not in SURFACE_KINDS
            or not isinstance(name, str) or not name):
        errors.append({"source": origin,
                       "reason": f"malformed surface: {surface_id!r}"})
        return None
    coverage = raw.get("coverage", "unknown")
    if coverage not in (I.COVERAGE_OBSERVED, I.COVERAGE_UNKNOWN):
        coverage = I.COVERAGE_UNKNOWN
    details = raw.get("details")
    if not isinstance(details, Mapping):
        details = {}
    declared = raw.get("declared")
    observed = raw.get("observed")
    return {
        "id": surface_id,
        "kind": kind,
        "name": name,
        "source": str(raw.get("source") or ""),
        "version": str(raw.get("version") or "unknown"),
        "declared": bool(declared),
        "observed": bool(observed),
        "coverage": coverage,
        "origin": origin,
        "details": dict(details),
    }


def _inventory_gaps(report: Mapping[str, Any]) -> dict[str, list[dict[str, Any]]]:
    raw = report.get("gaps")
    result = {"declared_not_observed": [], "observed_not_declared": [],
              "unverified": []}
    if not isinstance(raw, Mapping):
        return result
    for name in result:
        items = raw.get(name)
        if not isinstance(items, Sequence) or isinstance(items, (str, bytes)):
            continue
        clean: list[dict[str, Any]] = []
        for item in items:
            if isinstance(item, Mapping):
                clean.append({key: str(item.get(key, ""))
                              for key in ("id", "kind", "name", "reason")})
        result[name] = sorted(clean, key=lambda row: (
            row["kind"], row["id"], row["reason"]))
    return result


def _inventory_observation(root: str | Path | None) -> dict[str, Any]:
    try:
        report = I.collect_inventory(root)
    except Exception as exc:  # noqa: BLE001 - partial states must survive
        return {"state": "unknown", "error": _clean_error(exc),
                "surfaces": [], "gaps": _inventory_gaps({}),
                "hash": None, "counts": {}, "coverage": {}}
    return {
        "state": "observed",
        "error": None,
        "surfaces": list(report.get("surfaces") or []),
        "gaps": _inventory_gaps(report),
        "hash": I.inventory_hash(report),
        "counts": dict(report.get("counts") or {}),
        "coverage": dict(report.get("coverage") or {}),
    }


def _toolchain_observation() -> dict[str, Any]:
    try:
        from ozagia.core import toolchain as T

        registry = T.registry()
        capabilities = sorted(registry.get("capabilities") or {})
        tools = sorted(registry.get("tools") or {})
        surface = {
            "id": "toolchain:registry",
            "kind": "toolchain",
            "name": "registry",
            "source": _TOOLCHAIN_SOURCE,
            "version": str(registry.get("registry_version") or "unknown"),
            "declared": True,
            "observed": True,
            "coverage": I.COVERAGE_OBSERVED,
            "details": {
                "observation": "runtime-data",
                "capabilities": capabilities,
                "tools": tools,
                "registry_hash": C.content_hash(registry),
            },
        }
        return {"state": "observed", "error": None, "surfaces": [surface],
                "registry_version": surface["version"],
                "capabilities": capabilities, "tools": tools,
                "hash": surface["details"]["registry_hash"]}
    except Exception as exc:  # noqa: BLE001 - a missing extra is not a crash
        return {"state": "unknown", "error": _clean_error(exc),
                "surfaces": [], "registry_version": "unknown",
                "capabilities": [], "tools": [], "hash": None}


def _status_sections_source() -> tuple[list[str], str | None]:
    """Read the section keys of ``status()`` from source, without calling it."""
    path = _package_source("core/status.py")
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeError) as exc:
        return [], _clean_error(exc)
    function = next((node for node in tree.body
                     if isinstance(node, ast.FunctionDef)
                     and node.name == "status"), None)
    if function is None:
        return [], "status() not found"
    for node in ast.walk(function):
        if not isinstance(node, ast.Return) or not isinstance(node.value, ast.Dict):
            continue
        keys: list[str] = []
        for key in node.value.keys:
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                if key.value not in _STATUS_ENVELOPE:
                    keys.append(key.value)
        if keys:
            return sorted(set(keys)), None
    return [], "status() return mapping not found"


def _status_observation() -> dict[str, Any]:
    sections, error = _status_sections_source()
    if error is not None:
        return {"state": "unknown", "error": error, "surfaces": [],
                "sections": []}
    surfaces = [{
        "id": f"status:{section}",
        "kind": "status",
        "name": section,
        "source": _STATUS_SOURCE,
        "version": "1",
        "declared": True,
        "observed": True,
        "coverage": I.COVERAGE_UNKNOWN,
        "details": {
            "observation": "static-ast",
            "reason": "status() is not invoked; it may probe the daemon",
        },
    } for section in sections]
    return {"state": "observed", "error": None, "surfaces": surfaces,
            "sections": sections}


def _daemon_observation(inventory_surfaces: Sequence[Mapping[str, Any]]
                        ) -> dict[str, Any]:
    try:
        from ozagia.daemon import protocol

        operations = tuple(sorted(set(protocol.OPERATIONS)))
    except Exception as exc:  # noqa: BLE001 - partial states must survive
        return {"state": "unknown", "error": _clean_error(exc),
                "operations": [], "hash": None,
                "inventory_missing": [], "inventory_extra": []}
    documented = sorted({
        str(surface.get("name"))
        for surface in inventory_surfaces
        if surface.get("kind") == "daemon" and surface.get("name")
    })
    missing = sorted(set(operations) - set(documented))
    extra = sorted(set(documented) - set(operations))
    return {
        "state": "observed",
        "error": None,
        "operations": list(operations),
        "hash": C.content_hash(list(operations)),
        "inventory_missing": missing,
        "inventory_extra": extra,
    }


def _daemon_surfaces(daemon: Mapping[str, Any],
                     inventory_surfaces: Sequence[Mapping[str, Any]]
                     ) -> list[dict[str, Any]]:
    """The observed daemon operations, cross-checked against OPERATIONS.

    The protocol docstring may omit an operation that the server dispatches;
    ``protocol.OPERATIONS`` is the code-level declaration.  A documented
    surface is kept as-is, an operation missing from the docstring is
    synthesized with ``documented=false`` and ``cross_checked=true``.
    """
    existing = {
        str(surface.get("id")): dict(surface)
        for surface in inventory_surfaces if surface.get("kind") == "daemon"
    }
    result: list[dict[str, Any]] = []
    for operation in daemon.get("operations") or []:
        surface_id = f"daemon:op:{operation}"
        surface = existing.get(surface_id)
        if surface is None:
            surface = {
                "id": surface_id,
                "kind": "daemon",
                "name": operation,
                "source": _DAEMON_SOURCE,
                "version": "1",
                "declared": True,
                "observed": True,
                "coverage": I.COVERAGE_UNKNOWN,
                "details": {
                    "documented": False,
                    "cross_checked": True,
                    "observation": "protocol-operations",
                },
            }
        else:
            details = dict(surface.get("details") or {})
            details.update({"cross_checked": True, "documented": True,
                            "observation": details.get("observation",
                                                       "docstring")})
            surface["details"] = details
            surface["observed"] = True
        result.append(surface)
    for surface_id, surface in existing.items():
        if surface_id not in {item["id"] for item in result}:
            result.append(surface)
    return result


def collect_observations(root: str | Path | None = None) -> dict[str, Any]:
    """Collect the observed half of the self-graph without side effects.

    ``root`` is passed only to the inventory adapter-declaration reader (a
    read-only parse).  The projection itself is always about the running
    Ozagia package, never a target project.
    """
    errors: list[dict[str, str]] = []
    inventory = _inventory_observation(root)
    toolchain = _toolchain_observation()
    status = _status_observation()

    inventory_surfaces = [
        item for item in (
            _normalize_surface(raw, "inventory", errors)
            for raw in inventory.get("surfaces", []))
        if item is not None
    ]
    toolchain_surfaces = [
        item for item in (
            _normalize_surface(raw, "toolchain", errors)
            for raw in toolchain.get("surfaces", []))
        if item is not None
    ]
    status_surfaces = [
        item for item in (
            _normalize_surface(raw, "status", errors)
            for raw in status.get("surfaces", []))
        if item is not None
    ]
    daemon = _daemon_observation(inventory_surfaces)
    daemon_surfaces = [
        item for item in (
            _normalize_surface(raw, "daemon", errors)
            for raw in _daemon_surfaces(daemon, inventory_surfaces))
        if item is not None
    ]

    surfaces = sorted(
        [*[surface for surface in inventory_surfaces
           if surface["kind"] != "daemon"],
         *toolchain_surfaces, *status_surfaces, *daemon_surfaces],
        key=lambda item: (item["kind"], item["id"]))
    duplicates: list[dict[str, str]] = []
    unique_surfaces: list[dict[str, Any]] = []
    seen: dict[str, str] = {}
    for surface in surfaces:
        previous = seen.get(surface["id"])
        if previous is not None:
            duplicates.append({
                "source": "observations",
                "reason": f"duplicate surface {surface['id']!r} from "
                          f"{previous} and {surface['origin']}",
            })
            continue
        seen[surface["id"]] = surface["origin"]
        unique_surfaces.append(surface)
    if duplicates:
        errors.extend(duplicates)

    gaps = dict(inventory.get("gaps") or _inventory_gaps({}))
    emitters = {
        "inventory": {
            "state": inventory["state"], "error": inventory["error"],
            "hash": inventory["hash"], "counts": inventory["counts"],
            "coverage": inventory["coverage"],
        },
        "toolchain": {
            "state": toolchain["state"], "error": toolchain["error"],
            "hash": toolchain["hash"],
            "registry_version": toolchain["registry_version"],
            "capabilities": toolchain["capabilities"],
            "tools": toolchain["tools"],
        },
        "status": {
            "state": status["state"], "error": status["error"],
            "sections": status["sections"],
        },
        "daemon": {
            "state": daemon["state"], "error": daemon["error"],
            "hash": daemon["hash"], "operations": daemon["operations"],
            "inventory_missing": daemon["inventory_missing"],
            "inventory_extra": daemon["inventory_extra"],
        },
    }
    emitter_states = [emitters[name]["state"] for name in sorted(emitters)]
    if all(state == "unknown" for state in emitter_states):
        state = "unknown"
    elif any(value == "unknown" for value in emitter_states):
        state = "partial"
    else:
        state = "current"

    report = {
        "schema": {"namespace": OBSERVATION_NAMESPACE,
                   "version": SCHEMA_VERSION},
        "observation_version": REPORT_VERSION,
        "state": state,
        "sources": {
            "catalogue": _CATALOGUE_SOURCE,
            "inventory": _INVENTORY_SOURCE,
            "toolchain": _TOOLCHAIN_SOURCE,
            "status": _STATUS_SOURCE,
            "daemon": _DAEMON_SOURCE,
            "dispatch": _DISPATCH_SOURCE,
        },
        "surfaces": unique_surfaces,
        "gaps": gaps,
        "errors": sorted(errors, key=lambda item: (
            item.get("source", ""), item.get("reason", ""))),
        "emitters": emitters,
    }
    report["hash"] = C.content_hash(report)
    return report


# ---------------------------------------------------------------------------
# Surface resolution
# ---------------------------------------------------------------------------


def _surface_matches(declared: str, observed: Mapping[str, Any]) -> list[str]:
    """Observed ids covered by one declaration.

    Exact ids match themselves.  A CLI declaration also covers its dotted
    descendants, so ``cli:ozagia.goal`` covers ``cli:ozagia.goal.add`` without
    pretending the parent is a separate capability.  A trailing ``:*`` family
    declaration is accepted for future adapters.
    """
    if declared.endswith(":*"):
        prefix = declared[:-1]
        return sorted(surface_id for surface_id in observed
                      if surface_id.startswith(prefix))
    matches: set[str] = set()
    if declared in observed:
        matches.add(declared)
    if declared.startswith("cli:"):
        prefix = declared + "."
        matches.update(surface_id for surface_id in observed
                       if surface_id.startswith(prefix))
    return sorted(matches)


def _surface_family_unknown(observations: Mapping[str, Any],
                            declared: str) -> str | None:
    """Return a reason when absence cannot be claimed for this family."""
    emitters = observations.get("emitters")
    if not isinstance(emitters, Mapping):
        return "observations are incomplete"
    inventory = emitters.get("inventory") or {}
    if inventory.get("state") == "unknown":
        return "the inventory could not be collected"
    gaps = observations.get("gaps")
    entries: list[Mapping[str, Any]] = []
    if isinstance(gaps, Mapping):
        for name in ("declared_not_observed", "observed_not_declared",
                     "unverified"):
            items = gaps.get(name)
            if isinstance(items, Sequence) and not isinstance(
                    items, (str, bytes)):
                entries.extend(item for item in items
                               if isinstance(item, Mapping))
    if declared.startswith("mcp:tool:"):
        for gap in entries:
            if gap.get("id") == "mcp:handlers":
                return str(gap.get("reason")
                           or "the MCP handler map is unreadable")
    if declared.startswith("mcp:graph:"):
        for gap in entries:
            if gap.get("id") == "mcp:graph:handlers":
                return str(gap.get("reason")
                           or "the graph handler map is unreadable")
    if declared.startswith("daemon:op:"):
        daemon = emitters.get("daemon") or {}
        if daemon.get("state") == "unknown":
            return "daemon operations could not be read"
        operation = declared.split(":", 2)[2]
        operations = daemon.get("operations") or []
        if operation not in operations:
            return f"operation {operation!r} is not in protocol.OPERATIONS"
    if declared.startswith("toolchain:"):
        toolchain = emitters.get("toolchain") or {}
        if toolchain.get("state") == "unknown":
            return "the toolchain registry could not be read"
    if declared.startswith("status:"):
        status = emitters.get("status") or {}
        if status.get("state") == "unknown":
            return "status sections could not be read"
    return None


def _explicit_gap(observations: Mapping[str, Any], declared: str
                  ) -> dict[str, Any] | None:
    gaps = observations.get("gaps")
    if not isinstance(gaps, Mapping):
        return None
    for name in ("declared_not_observed", "unverified"):
        items = gaps.get(name)
        if not isinstance(items, Sequence) or isinstance(items, (str, bytes)):
            continue
        for gap in items:
            if isinstance(gap, Mapping) and gap.get("id") == declared:
                return dict(gap)
    return None


def _resolve_declared(declared: str, observations: Mapping[str, Any],
                      observed: Mapping[str, Any]) -> dict[str, Any]:
    matches = _surface_matches(declared, observed)
    if matches:
        return {"state": "observed", "matches": matches, "reason": None,
                "provenance": [observed[surface]["origin"] for surface in matches]}
    family = _surface_family_unknown(observations, declared)
    if family is not None:
        return {"state": "unknown", "matches": [], "reason": family,
                "provenance": ["partial-observation"]}
    gap = _explicit_gap(observations, declared)
    if gap is not None:
        return {"state": "missing", "matches": [],
                "reason": str(gap.get("reason") or "not observed"),
                "provenance": ["inventory-gap"]}
    if declared.startswith("mcp:"):
        return {"state": "missing", "matches": [],
                "reason": "the static MCP surface inventory does not list this tool",
                "provenance": ["inventory"]}
    return {"state": "missing", "matches": [],
            "reason": "the declared surface was not observed",
            "provenance": ["inventory"]}


def _proxy_state(desired: str) -> str:
    return desired if desired in EDGE_STATES else "unknown"


def _freshness(state: str) -> str:
    return {"observed": "current", "declared": "current", "missing": "absent",
            "absent": "absent", "undeclared": "unknown",
            "unknown": "unknown"}.get(state, "unknown")


def _evidence(state: str) -> str:
    return {"observed": "observed", "declared": "declared",
            "missing": "inferred", "absent": "observed",
            "undeclared": "observed", "unknown": "inferred"}.get(
                state, "inferred")


# ---------------------------------------------------------------------------
# Projection
# ---------------------------------------------------------------------------


class _Edges:
    def __init__(self) -> None:
        self._items: dict[tuple[str, str, str], dict[str, Any]] = {}

    def add(self, edge_type: str, from_id: str, to_id: str, state: str,
            *, capability: str, source: str, method: str,
            observation: str | None = None) -> None:
        if edge_type not in EDGE_TYPES:
            raise SelfGraphError(f"unknown edge type {edge_type!r}")
        state = _proxy_state(state)
        key = (edge_type, from_id, to_id)
        item = self._items.get(key)
        if item is None:
            item = {
                "type": edge_type,
                "from": from_id,
                "to": to_id,
                "state": state,
                "freshness": _freshness(state),
                "evidence": _evidence(state),
                "via": [],
                "provenance": [],
            }
            self._items[key] = item
        elif EDGE_STATE_RANK[state] > EDGE_STATE_RANK[item["state"]]:
            item["state"] = state
            item["freshness"] = _freshness(state)
            item["evidence"] = _evidence(state)
        item["via"].append(capability)
        provenance = {
            "source": source,
            "method": method,
            "capability": capability,
        }
        if observation is not None:
            provenance["observation"] = observation
        if provenance not in item["provenance"]:
            item["provenance"].append(provenance)

    def finish(self) -> list[dict[str, Any]]:
        items = []
        for item in self._items.values():
            item["via"] = sorted(set(item["via"]))
            item["provenance"] = sorted(
                item["provenance"],
                key=lambda row: C.canonical_json(row))
            items.append(item)
        return sorted(items, key=lambda row: (
            row["type"], row["from"], row["to"]))


def _node(*, node_id: str, kind: str, state: str, label: str,
          provenance: Mapping[str, Any], **details: Any) -> dict[str, Any]:
    if kind not in NODE_KINDS:
        raise SelfGraphError(f"unknown node kind {kind!r}")
    if state not in EDGE_STATES:
        raise SelfGraphError(f"unknown node state {state!r}")
    return {
        "id": node_id,
        "kind": kind,
        "state": state,
        "label": label,
        "provenance": dict(provenance),
        "details": details,
    }


def _dependency_id(text: str) -> str:
    return "dep:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def _findings(observations: Mapping[str, Any],
              capabilities: Sequence[K.Capability],
              observed: Mapping[str, dict[str, Any]],
              surface_states: Sequence[Mapping[str, Any]],
              impl_probes: Mapping[str, Mapping[str, Any]],
              ref_probes: Mapping[str, Mapping[str, Any]],
              covered: set[str],
              duplicates: Mapping[str, Sequence[str]]
              ) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    by_key: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}

    def add(kind: str, severity: str, state: str, subject: str,
            reason: str, provenance: Mapping[str, Any]) -> None:
        key = (kind, severity, state, subject, reason)
        existing = by_key.get(key)
        capability = provenance.get("capability")
        if existing is not None:
            if isinstance(capability, str):
                owners = existing["provenance"].setdefault("capabilities", [])
                if capability not in owners:
                    owners.append(capability)
                    owners.sort()
            return
        item = {
            "kind": kind,
            "severity": severity,
            "state": state,
            "subject": subject,
            "reason": reason,
            "provenance": dict(provenance),
        }
        if isinstance(capability, str):
            item["provenance"]["capabilities"] = [capability]
        by_key[key] = item
        findings.append(item)

    catalogue_source = "catalogue:" + _CATALOGUE_SOURCE
    for row in surface_states:
        if row["state"] == "observed":
            continue
        kind = ("declared_surface_unknown" if row["state"] == "unknown"
                else "declared_surface_not_observed")
        severity = "unknown" if row["state"] == "unknown" else "error"
        add(kind, severity, row["state"], row["surface"], row["reason"], {
            "catalogue": catalogue_source,
            "capability": row["capability"],
            "surface": row["surface"],
            "observation": ",".join(row["provenance"]),
        })

    for surface_id in sorted(observed):
        surface = observed[surface_id]
        if surface_id in covered:
            continue
        kind = surface["kind"]
        is_namespace = (kind == "cli"
                        and bool(surface.get("details", {}).get(
                            "takes_subcommand")))
        if kind in EXCLUDED_SURFACE_KINDS or is_namespace:
            continue
        add("observed_surface_undeclared", "coverage", "undeclared",
            surface_id,
            "the surface is observed but no capability declares it",
            {"catalogue": catalogue_source, "observation": surface["origin"],
             "source": surface["source"], "surface_kind": kind,
             "coverage": surface["coverage"]})

    # The inventory also compares a tool schema with its handler map.  Keep
    # those two independent contradictions visible even when a capability
    # declaration already covers the surface id.
    gaps = observations.get("gaps")
    if isinstance(gaps, Mapping):
        for gap in gaps.get("observed_not_declared") or []:
            if not isinstance(gap, Mapping):
                continue
            subject = str(gap.get("id") or "")
            if not subject:
                continue
            add("handler_without_tool_declaration", "error", "undeclared",
                subject,
                str(gap.get("reason") or "handler has no declared tool entry"),
                {"observation": "inventory-gap", "source": _INVENTORY_SOURCE,
                 "surface_kind": str(gap.get("kind") or "")})
        for gap in gaps.get("declared_not_observed") or []:
            if not isinstance(gap, Mapping):
                continue
            subject = str(gap.get("id") or "")
            if (not subject
                    or subject in ("mcp:handlers", "mcp:graph:handlers")):
                continue
            add("tool_without_handler", "error", "missing", subject,
                str(gap.get("reason") or "tool has no runtime handler"),
                {"observation": "inventory-gap", "source": _INVENTORY_SOURCE,
                 "surface_kind": str(gap.get("kind") or "")})

    for capability in capabilities:
        for impl_id in capability.impl:
            probe = impl_probes.get(impl_id) or {}
            state = probe.get("state", "unknown")
            if state == "observed":
                continue
            add("implementation_" + ("unknown" if state == "unknown"
                                     else "missing"),
                "unknown" if state == "unknown" else "error",
                state, impl_id, str(probe.get("reason") or state), {
                    "catalogue": catalogue_source,
                    "capability": capability.id,
                    "observation": str(probe.get("source") or ""),
                })
        for ref_prefix, kind in (("test:", "test"), ("doc:", "doc")):
            refs = capability.tests if kind == "test" else capability.docs
            for ref in refs:
                probe = ref_probes.get(ref) or {}
                state = probe.get("state", "unknown")
                if state == "observed":
                    continue
                add(f"{kind}_ref_" + ("unknown" if state == "unknown"
                                      else "missing"),
                    "unknown" if state == "unknown" else "coverage",
                    state, ref, str(probe.get("reason") or state), {
                        "catalogue": catalogue_source,
                        "capability": capability.id,
                        "observation": "source-tree",
                    })

    for surface, owners in duplicates.items():
        add("duplicate_surface_declaration", "coverage", "declared", surface,
            "the same surface is declared by more than one capability",
            {"catalogue": catalogue_source, "capabilities": list(owners)})

    emitters = observations.get("emitters")
    if isinstance(emitters, Mapping):
        for name in sorted(emitters):
            emitter = emitters[name]
            if emitter.get("state") == "unknown":
                add("observation_unavailable", "unknown", "unknown",
                    f"emitter:{name}", str(emitter.get("error") or "unknown"), {
                        "observation": name,
                        "source": (observations.get("sources") or {}).get(
                            name, ""),
                    })
    for error in observations.get("errors", []) or []:
        add("observation_error", "unknown", "unknown",
            str(error.get("source") or "observations"),
            str(error.get("reason") or "unknown"), {"observation": "normalize"})

    daemon = (observations.get("emitters") or {}).get("daemon") or {}
    for name, kind, severity, reason in (
        ("inventory_missing", "daemon_operation_undocumented", "coverage",
         "protocol.OPERATIONS lists this operation but the protocol docstring "
         "does not"),
        ("inventory_extra", "daemon_operation_undeclared", "error",
         "the protocol docstring lists this operation but protocol.OPERATIONS "
         "does not"),
    ):
        for operation in daemon.get(name) or []:
            add(kind, severity, "missing", f"daemon:op:{operation}",
                reason, {"observation": "daemon", "source": _DAEMON_SOURCE,
                         "operation": operation})
    return sorted(findings, key=lambda row: (
        row["kind"], row["subject"], row["reason"]))


def _audit_state(observations: Mapping[str, Any],
                 findings: Sequence[Mapping[str, Any]]) -> str:
    emitters = observations.get("emitters")
    states = []
    if isinstance(emitters, Mapping):
        states = [emitters[name].get("state") for name in emitters]
    if states and all(state == "unknown" for state in states):
        return "unknown"
    if (any(state == "unknown" for state in states)
            or any(finding["severity"] == "unknown" for finding in findings)):
        return "partial"
    return "current"


def _summarize_states(items: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        state = str(item.get("state") or "unknown")
        counts[state] = counts.get(state, 0) + 1
    return {state: counts[state] for state in sorted(counts)}


def build_self_graph(*, capabilities: Sequence[K.Capability] | None = None,
                     observations: Mapping[str, Any] | None = None,
                     root: str | Path | None = None) -> dict[str, Any]:
    """Build the deterministic read-only self-graph.

    ``capabilities`` and ``observations`` are injection points for tests and
    diagnostics.  The default catalogue is the built-in authority; the default
    observations are collected fresh from the documented read-only sources.
    """
    caps = tuple(K.catalogue() if capabilities is None else capabilities)
    obs = (collect_observations(root) if observations is None
           else dict(observations))
    present: dict[str, dict[str, Any]] = {}
    normalization_errors: list[dict[str, str]] = []
    for raw in obs.get("surfaces", []) or []:
        surface = _normalize_surface(raw, str(raw.get("origin") or "observations")
                                     if isinstance(raw, Mapping) else "observations",
                                     normalization_errors)
        if surface is None:
            continue
        surface["origin"] = str(raw.get("origin") or "observations")
        present[surface["id"]] = surface
    observed = {surface_id: surface for surface_id, surface in present.items()
                if surface["observed"]}

    nodes: dict[str, dict[str, Any]] = {}
    edges = _Edges()
    implementation_probes: dict[str, dict[str, Any]] = {}
    ref_probes: dict[str, dict[str, Any]] = {}
    surface_states: list[dict[str, Any]] = []
    covered: set[str] = set()
    duplicates = K.duplicate_surfaces(caps)
    catalogue_hash = K.catalogue_hash(caps)

    for capability in caps:
        nodes[capability.id] = _node(
            node_id=capability.id, kind="capability", state="declared",
            label=capability.id,
            provenance={"source": "catalogue:" + _CATALOGUE_SOURCE,
                        "method": "declared"},
            domain=capability.domain, owner=capability.owner,
            side_effect=capability.side_effect,
            exposure=list(capability.exposure))

    for capability in caps:
        for impl_id in capability.impl:
            probe = implementation_probes.get(impl_id)
            if probe is None:
                probe = _probe_implementation(impl_id)
                implementation_probes[impl_id] = probe
            nodes.setdefault(impl_id, _node(
                node_id=impl_id, kind="implementation",
                state=str(probe.get("state") or "unknown"),
                label=impl_id,
                provenance={"source": str(probe.get("source") or ""),
                            "method": "static-ast"},
                module=probe.get("module"), symbol=probe.get("symbol"),
                reason=probe.get("reason")))
            edges.add("implements", capability.id, impl_id,
                      str(probe.get("state") or "unknown"),
                      capability=capability.id,
                      source="catalogue:" + _CATALOGUE_SOURCE,
                      method="declared", observation="static-ast")

        surface_probes: list[tuple[str, str, list[str]]] = []
        for declared in capability.surfaces:
            resolution = _resolve_declared(declared, obs, observed)
            state = str(resolution["state"])
            surface_probes.append((declared, state,
                                   list(resolution.get("matches") or [])))
            surface_states.append({
                "capability": capability.id,
                "surface": declared,
                "state": state,
                "reason": resolution.get("reason"),
                "provenance": list(resolution.get("provenance") or []),
                "matches": list(resolution.get("matches") or []),
            })
            if state == "observed":
                covered.update(resolution["matches"])
                for match in resolution["matches"]:
                    surface = observed[match]
                    nodes.setdefault(match, _node(
                        node_id=match, kind="surface", state="observed",
                        label=match,
                        provenance={"source": surface["source"],
                                    "method": surface["origin"]},
                        surface_kind=surface["kind"],
                        coverage=surface["coverage"],
                        declared_by=capability.id))
                    edges.add("exposes", capability.id, match, "observed",
                              capability=capability.id,
                              source="inventory:" + surface["source"],
                              method="declared",
                              observation=surface["origin"])
            else:
                nodes.setdefault(declared, _node(
                    node_id=declared, kind="surface", state=state,
                    label=declared,
                    provenance={"source": "catalogue:" + _CATALOGUE_SOURCE,
                                "method": "declared"},
                    surface_kind=declared.split(":", 1)[0],
                    declared_by=capability.id,
                    reason=resolution.get("reason")))
                edges.add("exposes", capability.id, declared, state,
                          capability=capability.id,
                          source="catalogue:" + _CATALOGUE_SOURCE,
                          method="declared",
                          observation=str(resolution.get("provenance") or ""))

        for impl_id in capability.impl:
            impl_state = str(
                (implementation_probes.get(impl_id) or {}).get("state")
                or "unknown")
            for declared, surface_state, matches in surface_probes:
                targets = matches or [declared]
                for target in targets:
                    combined = surface_state
                    if EDGE_STATE_RANK[impl_state] > EDGE_STATE_RANK[combined]:
                        combined = impl_state
                    edges.add("backs", impl_id, target, combined,
                              capability=capability.id,
                              source="catalogue:" + _CATALOGUE_SOURCE,
                              method="inferred",
                              observation="static-ast")

        for prerequisite in capability.prerequisites:
            dep_id = _dependency_id(prerequisite)
            nodes.setdefault(dep_id, _node(
                node_id=dep_id, kind="dependency", state="declared",
                label=prerequisite,
                provenance={"source": "catalogue:" + _CATALOGUE_SOURCE,
                            "method": "declared"},
                declared_by=capability.id))
            edges.add("requires", capability.id, dep_id, "declared",
                      capability=capability.id,
                      source="catalogue:" + _CATALOGUE_SOURCE,
                      method="declared")
            for declared, surface_state, matches in surface_probes:
                for target in (matches or [declared]):
                    combined = surface_state
                    if EDGE_STATE_RANK["declared"] > EDGE_STATE_RANK[combined]:
                        combined = "declared"
                    edges.add("requires", target, dep_id, combined,
                              capability=capability.id,
                              source="catalogue:" + _CATALOGUE_SOURCE,
                              method="inferred")

        for kind, refs in (("test", capability.tests),
                           ("doc", capability.docs)):
            for ref in refs:
                probe = ref_probes.get(ref)
                if probe is None:
                    probe = _probe_ref(ref)
                    ref_probes[ref] = probe
                nodes.setdefault(ref, _node(
                    node_id=ref, kind=kind,
                    state=str(probe.get("state") or "unknown"),
                    label=ref,
                    provenance={"source": "source-tree",
                                "method": "static-path"},
                    path=probe.get("path"), reason=probe.get("reason")))
                edge_type = "verified_by" if kind == "test" else "documented_by"
                edges.add(edge_type, capability.id, ref,
                          str(probe.get("state") or "unknown"),
                          capability=capability.id,
                          source="catalogue:" + _CATALOGUE_SOURCE,
                          method="declared", observation="static-path")
                for impl_id in capability.impl:
                    impl_state = str(
                        (implementation_probes.get(impl_id) or {}).get("state")
                        or "unknown")
                    probe_state = str(probe.get("state") or "unknown")
                    combined = (impl_state
                                if EDGE_STATE_RANK[impl_state]
                                > EDGE_STATE_RANK[probe_state]
                                else probe_state)
                    second = "tested_by" if kind == "test" else "documented_by"
                    edges.add(second, impl_id, ref, combined,
                              capability=capability.id,
                              source="catalogue:" + _CATALOGUE_SOURCE,
                              method="inferred", observation="static-path")

    edge_items = edges.finish()
    node_items = sorted(nodes.values(), key=lambda item: (
        item["kind"], item["id"]))
    graph_truncation = C.truncation()
    if (len(node_items) > MAX_GRAPH_NODES
            or len(edge_items) > MAX_GRAPH_EDGES):
        omitted = (max(0, len(node_items) - MAX_GRAPH_NODES)
                   + max(0, len(edge_items) - MAX_GRAPH_EDGES))
        node_items = node_items[:MAX_GRAPH_NODES]
        edge_items = edge_items[:MAX_GRAPH_EDGES]
        graph_truncation = C.truncation(
            True, omitted=omitted, reason="limit")

    findings = _findings(obs, caps, observed, surface_states,
                         implementation_probes, ref_probes, covered, duplicates)
    audit_state = _audit_state(obs, findings)
    graph_body = {"nodes": node_items, "edges": edge_items}
    graph_hash = C.content_hash(graph_body)
    audit_body = {
        "state": audit_state,
        "findings": findings,
        "counts": _audit_counts(caps, present, observed, surface_states,
                                covered, findings, node_items, edge_items,
                                implementation_probes, ref_probes, duplicates),
    }
    audit_hash = C.content_hash(audit_body)
    return {
        "schema": {"namespace": SCHEMA_NAMESPACE, "version": SCHEMA_VERSION},
        "report_version": REPORT_VERSION,
        "catalogue": {
            "version": K.CATALOGUE_VERSION,
            "hash": catalogue_hash,
            "capabilities": len(caps),
            "domains": K.domain_counts(caps),
            "surfaces": sum(len(capability.surfaces) for capability in caps),
        },
        "observations": obs,
        "graph": {
            "hash": graph_hash,
            "nodes": node_items,
            "edges": edge_items,
            "counts": {
                "nodes": len(node_items),
                "edges": len(edge_items),
                "by_node_kind": _count_by(node_items, "kind"),
                "by_node_state": _summarize_states(node_items),
                "by_edge_type": _count_by(edge_items, "type"),
                "by_edge_state": _summarize_states(edge_items),
            },
            "truncated": graph_truncation.to_dict(),
        },
        "audit": {**audit_body, "hash": audit_hash},
        "surface_states": sorted(surface_states, key=lambda row: (
            row["capability"], row["surface"])),
        "implementation_probes": {
            key: implementation_probes[key]
            for key in sorted(implementation_probes)},
        "ref_probes": {key: ref_probes[key] for key in sorted(ref_probes)},
        "duplicate_surfaces": {
            key: list(value) for key, value in sorted(duplicates.items())},
        "coverage": _coverage(obs, present, observed, covered, surface_states,
                              findings),
    }


def _count_by(items: Iterable[Mapping[str, Any]], field: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        key = str(item.get(field) or "unknown")
        counts[key] = counts.get(key, 0) + 1
    return {key: counts[key] for key in sorted(counts)}


def _audit_counts(capabilities: Sequence[K.Capability],
                  present: Mapping[str, Any],
                  observed: Mapping[str, Any],
                  surface_states: Sequence[Mapping[str, Any]],
                  covered: set[str],
                  findings: Sequence[Mapping[str, Any]],
                  nodes: Sequence[Mapping[str, Any]],
                  edges: Sequence[Mapping[str, Any]],
                  impl_probes: Mapping[str, Any],
                  ref_probes: Mapping[str, Any],
                  duplicates: Mapping[str, Sequence[str]]) -> dict[str, Any]:
    findings_by_kind = _count_by(findings, "kind")
    findings_by_severity = _count_by(findings, "severity")
    declared_surfaces = sum(len(capability.surfaces)
                            for capability in capabilities)
    with_surfaces = sum(1 for capability in capabilities if capability.surfaces)
    return {
        "capabilities": len(capabilities),
        "capabilities_with_surfaces": with_surfaces,
        "capabilities_without_surfaces": len(capabilities) - with_surfaces,
        "declared_surfaces": declared_surfaces,
        "present_surfaces": len(present),
        "observed_surfaces": len(observed),
        "covered_surfaces": len(covered),
        "undeclared_surfaces": len([
            surface for surface in observed
            if surface not in covered
            and observed[surface]["kind"] in REQUIRED_SURFACE_KINDS
            and not (observed[surface]["kind"] == "cli"
                     and observed[surface].get("details", {}).get(
                         "takes_subcommand"))
        ]),
        "excluded_surfaces": len([
            surface for surface in present
            if present[surface]["kind"] in EXCLUDED_SURFACE_KINDS
            and surface not in covered
        ]),
        "duplicate_declarations": len(duplicates),
        "surface_states": _summarize_states(surface_states),
        "implementations": _summarize_states(impl_probes.values()),
        "references": _summarize_states(ref_probes.values()),
        "findings": len(findings),
        "findings_by_kind": findings_by_kind,
        "findings_by_severity": findings_by_severity,
        "nodes": len(nodes),
        "edges": len(edges),
        "edge_states": _summarize_states(edges),
    }


def _coverage(observations: Mapping[str, Any],
              present: Mapping[str, Any],
              observed: Mapping[str, Any],
              covered: set[str],
              surface_states: Sequence[Mapping[str, Any]],
              findings: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    present_by_kind: dict[str, int] = {}
    observed_by_kind: dict[str, int] = {}
    excluded: dict[str, int] = {}
    for surface in present.values():
        kind = surface["kind"]
        present_by_kind[kind] = present_by_kind.get(kind, 0) + 1
        if kind in EXCLUDED_SURFACE_KINDS and surface["id"] not in covered:
            excluded[kind] = excluded.get(kind, 0) + 1
    for surface in observed.values():
        kind = surface["kind"]
        observed_by_kind[kind] = observed_by_kind.get(kind, 0) + 1
    emitters = observations.get("emitters")
    unknowns = []
    if isinstance(emitters, Mapping):
        for name in sorted(emitters):
            emitter = emitters[name]
            if emitter.get("state") == "unknown":
                unknowns.append(f"{name}: {emitter.get('error') or 'unknown'}")
    if observations.get("state") in ("partial", "unknown"):
        unknowns.append(
            "observations are partial; absence is not claimed where an "
            "emitter is unknown")
    return {
        "exhaustive": False,
        "audited_surface_kinds": list(REQUIRED_SURFACE_KINDS),
        "count_semantics": (
            "per-kind maps cover every inventory kind; audited totals include "
            "only audited_surface_kinds"),
        "counts_source": "generated surface inventory",
        "audited_present_surfaces": sum(
            count for kind, count in present_by_kind.items()
            if kind in REQUIRED_SURFACE_KINDS),
        "audited_observed_surfaces": sum(
            count for kind, count in observed_by_kind.items()
            if kind in REQUIRED_SURFACE_KINDS),
        "all_present_surfaces": sum(present_by_kind.values()),
        "all_observed_surfaces": sum(observed_by_kind.values()),
        "excluded_surface_kinds": {
            kind: {
                "count": excluded[kind],
                "reason": "configuration, packaging or adapter surfaces are "
                          "not capability declarations",
            }
            for kind in sorted(excluded)
        },
        "present_by_kind": {
            kind: present_by_kind[kind] for kind in sorted(present_by_kind)},
        "observed_by_kind": {
            kind: observed_by_kind[kind] for kind in sorted(observed_by_kind)},
        "declared_by_state": _summarize_states(surface_states),
        "unknowns": unknowns,
        "findings": len(findings),
    }


# ---------------------------------------------------------------------------
# Pagination and bounded views
# ---------------------------------------------------------------------------


def _check_limit(limit: Any) -> int:
    if limit is None:
        return DEFAULT_LIMIT
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise SelfGraphError("limit must be an integer")
    if limit < 1:
        raise SelfGraphError("limit must be at least 1")
    if limit > MAX_LIMIT:
        raise SelfGraphError(f"limit must be at most {MAX_LIMIT}")
    return limit


def _check_cursor(cursor: Any) -> int:
    if cursor is None:
        return 0
    if not isinstance(cursor, str) or not cursor:
        raise SelfGraphError("cursor must be a non-empty string")
    if len(cursor) > MAX_CURSOR or not cursor.isdigit():
        raise SelfGraphError("cursor must be a decimal offset")
    return int(cursor)


def _page(items: Sequence[Any], limit: Any = None,
          cursor: Any = None) -> tuple[list[Any], C.Truncation]:
    limit_value = _check_limit(limit)
    start = _check_cursor(cursor)
    total = len(items)
    page = list(items[start:start + limit_value])
    consumed = start + len(page)
    truncated = consumed < total
    if not truncated:
        return page, C.truncation()
    return page, C.truncation(
        True, omitted=total - consumed, next_cursor=str(consumed),
        reason="limit")


def _report_common(report: Mapping[str, Any], action: str) -> dict[str, Any]:
    return {
        "schema": {"namespace": SCHEMA_NAMESPACE, "version": SCHEMA_VERSION},
        "report_version": REPORT_VERSION,
        "action": action,
        "catalogue": dict(report["catalogue"]),
        "coverage": dict(report["coverage"]),
    }


def _surface_state_map(report: Mapping[str, Any]) -> dict[str, dict[str, int]]:
    counts: dict[str, dict[str, int]] = {}
    for row in report.get("surface_states", []) or []:
        capability = str(row.get("capability"))
        state = str(row.get("state") or "unknown")
        bucket = counts.setdefault(capability, {})
        bucket[state] = bucket.get(state, 0) + 1
    return counts


def capability_list(*, capabilities: Sequence[K.Capability] | None = None,
                    observations: Mapping[str, Any] | None = None,
                    limit: Any = None, cursor: Any = None,
                    root: str | Path | None = None) -> dict[str, Any]:
    """Paginated capability list with surface and finding counts."""
    report = build_self_graph(capabilities=capabilities,
                              observations=observations, root=root)
    caps = tuple(K.catalogue() if capabilities is None else capabilities)
    states = _surface_state_map(report)
    findings = report["audit"]["findings"]
    finding_counts: dict[str, int] = {}
    for finding in findings:
        provenance = finding.get("provenance") or {}
        owners = {provenance.get("capability")}
        owners.update(provenance.get("capabilities") or [])
        for capability in owners:
            if capability:
                finding_counts[capability] = finding_counts.get(
                    capability, 0) + 1
    items = [{
        "id": capability.id,
        "domain": capability.domain,
        "purpose": capability.purpose,
        "owner": capability.owner,
        "side_effect": capability.side_effect,
        "exposure": list(capability.exposure),
        "surfaces": len(capability.surfaces),
        "implementations": len(capability.impl),
        "tests": len(capability.tests),
        "docs": len(capability.docs),
        "surface_states": dict(states.get(capability.id, {})),
        "findings": finding_counts.get(capability.id, 0),
    } for capability in caps]
    page, truncation = _page(items, limit, cursor)
    return {
        **_report_common(report, "list"),
        "items": page,
        "truncation": truncation.to_dict(),
        "counts": {
            "capabilities": len(items),
            "returned": len(page),
            "domains": dict(report["catalogue"]["domains"]),
        },
    }


def capability_show(capability_id: str, *,
                    capabilities: Sequence[K.Capability] | None = None,
                    observations: Mapping[str, Any] | None = None,
                    limit: Any = None, cursor: Any = None,
                    root: str | Path | None = None) -> dict[str, Any]:
    """One capability with the bounded nodes and edges that mention it."""
    caps = tuple(K.catalogue() if capabilities is None else capabilities)
    try:
        parsed = C.parse_id(capability_id)
    except C.ContractError as exc:
        raise SelfGraphError(f"unknown capability {capability_id!r}") from exc
    if parsed.kind != "cap":
        raise SelfGraphError(f"not a capability id: {capability_id!r}")
    selected = next((capability for capability in caps
                     if capability.id == parsed.canonical), None)
    if selected is None:
        raise SelfGraphError(f"unknown capability {parsed.canonical!r}")
    report = build_self_graph(capabilities=caps, observations=observations,
                              root=root)
    edges = [edge for edge in report["graph"]["edges"]
             if selected.id in edge.get("via", [])
             or selected.id in (edge.get("from"), edge.get("to"))]
    page, truncation = _page(edges, limit, cursor)
    node_ids: set[str] = {selected.id}
    for edge in page:
        node_ids.add(edge["from"])
        node_ids.add(edge["to"])
    nodes = [node for node in report["graph"]["nodes"]
             if node["id"] in node_ids]
    findings = []
    for finding in report["audit"]["findings"]:
        provenance = finding.get("provenance") or {}
        owners = {provenance.get("capability")}
        owners.update(provenance.get("capabilities") or [])
        if selected.id in owners or finding.get("subject") == selected.id:
            findings.append(finding)
    return {
        **_report_common(report, "show"),
        "capability": selected.to_dict(),
        "nodes": nodes,
        "edges": page,
        "truncation": truncation.to_dict(),
        "findings": findings,
        "counts": {
            "edges": len(edges),
            "returned_edges": len(page),
            "nodes": len(nodes),
            "findings": len(findings),
        },
    }


def audit_self_graph(*, capabilities: Sequence[K.Capability] | None = None,
                     observations: Mapping[str, Any] | None = None,
                     limit: Any = None, cursor: Any = None,
                     root: str | Path | None = None) -> dict[str, Any]:
    """The bounded audit view: findings, coverage and a graph summary."""
    report = build_self_graph(capabilities=capabilities,
                              observations=observations, root=root)
    findings = report["audit"]["findings"]
    page, truncation = _page(findings, limit, cursor)
    return {
        **_report_common(report, "audit"),
        "state": report["audit"]["state"],
        "hash": report["audit"]["hash"],
        "findings": page,
        "truncation": truncation.to_dict(),
        "counts": {
            **dict(report["audit"]["counts"]),
            "returned_findings": len(page),
        },
        "graph": {
            "hash": report["graph"]["hash"],
            "counts": dict(report["graph"]["counts"]),
            "truncated": dict(report["graph"]["truncated"]),
        },
        "observations": {
            "state": report["observations"]["state"],
            "hash": report["observations"]["hash"],
            "emitters": {
                name: {
                    "state": emitter.get("state"),
                    "error": emitter.get("error"),
                }
                for name, emitter in sorted(
                    report["observations"]["emitters"].items())
            },
        },
    }


def self_graph_hash(report: Mapping[str, Any]) -> str:
    """SHA-256 over the canonical node/edge body of a built report."""
    body = {
        "nodes": report.get("graph", {}).get("nodes", []),
        "edges": report.get("graph", {}).get("edges", []),
    }
    return C.content_hash(body)


__all__ = [
    "AUDIT_STATES", "DEFAULT_LIMIT", "EDGE_STATES", "EDGE_TYPES",
    "EXCLUDED_SURFACE_KINDS", "MAX_LIMIT", "NODE_KINDS",
    "OBSERVATION_NAMESPACE", "REPORT_VERSION", "REQUIRED_SURFACE_KINDS",
    "SCHEMA_NAMESPACE", "SCHEMA_VERSION", "SelfGraphError", "SURFACE_KINDS",
    "audit_self_graph", "build_self_graph", "capability_list",
    "capability_show", "collect_observations", "package_dir", "repo_root",
    "self_graph_hash",
]
