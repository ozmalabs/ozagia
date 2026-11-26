"""Read-only structural validation for Ocelot seed manifests.

This module deliberately does not import Ocelot or execute its seed loader:
the manifest is checked as JSON against the documented structural shape, and
Ocelot's own loader remains the authority on whether a seed loads.  The
validator only reads files and, when a graph memory is supplied, reads the
already-indexed ``OcelotSeed`` context.  It never builds the engine, runs the
target's tests, calls a model or touches the network.

The adapter adds ``ocelot_seed_validate`` as both a graph query and an MCP
tool.  Register it alongside the workspace adapter::

    [adapters]
    modules = [
      "ozagia.integrations.ocelot.workspace:register",
      "ozagia.integrations.ocelot.workspace_validate:register_validate",
    ]
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable, Mapping

SEED_DIR = "seeds"
BUILTIN_SEEDS = frozenset({"core"})
KNOWN_ROLES = frozenset({"agent_root"})
MAX_VALIDATE_BYTES = 32 * 1024 * 1024
MAX_VALIDATE_HEAD_BYTES = 2 * 1024 * 1024
MAX_DEPENDENCY_FILES = 512
MAX_DEPENDENCY_DEPTH = 64
MAX_ISSUES = 200
MAX_MISSING_PRODUCTION_WARNINGS = 50
SAFE_SEED_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_MISSING = object()


class _Report:
    """Bounded error/warning collector."""

    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self._dropped_errors = 0
        self._dropped_warnings = 0

    def error(self, message: str) -> None:
        if len(self.errors) < MAX_ISSUES:
            self.errors.append(str(message))
        else:
            self._dropped_errors += 1

    def warning(self, message: str) -> None:
        if len(self.warnings) < MAX_ISSUES:
            self.warnings.append(str(message))
        else:
            self._dropped_warnings += 1

    def finish(self) -> None:
        if self._dropped_errors:
            self.errors.append(
                f"{self._dropped_errors} additional error(s) omitted")
        if self._dropped_warnings:
            self.warnings.append(
                f"{self._dropped_warnings} additional warning(s) omitted")


def _check(checks: list[dict[str, Any]], name: str, ok: bool,
           detail: str = "") -> None:
    checks.append({"name": name, "ok": bool(ok), "detail": str(detail)})


def _is_safe_seed_id(seed_id: str) -> bool:
    return bool(seed_id and SAFE_SEED_ID_RE.fullmatch(seed_id)
                and seed_id not in (".", ".."))


def _display_path(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root).as_posix()
    except (OSError, ValueError):
        return str(path)


def _seed_path(root: Path, seed_id: str, memory: Any | None) -> Path:
    """Prefer the indexed graph path, then the conventional seed path."""
    default = root / SEED_DIR / f"{seed_id}.json"
    if memory is None:
        return default
    try:
        nid = memory.node_by_key("ocelot_seed:" + str(seed_id))
        node = memory.node(int(nid)) if nid is not None else None
        attrs = (node or {}).get("attrs") or {}
        rel = attrs.get("path")
        if isinstance(rel, str) and rel:
            candidate = Path(rel)
            if not candidate.is_absolute():
                candidate = root / candidate
            if candidate.is_file():
                return candidate
    except Exception:  # noqa: BLE001 - graph context is optional
        pass
    return default


def _head_metadata(path: Path) -> dict[str, Any]:
    """Best-effort top-level metadata from a bounded manifest head."""
    with path.open("r", encoding="utf-8", errors="replace") as stream:
        head = stream.read(MAX_VALIDATE_HEAD_BYTES)

    def string(key: str) -> str | None:
        match = re.search(
            rf'"{re.escape(key)}"\s*:\s*("(?:[^"\\]|\\.)*")', head, re.S)
        if not match:
            return None
        try:
            value = json.loads(match.group(1))
        except json.JSONDecodeError:
            return None
        return value if isinstance(value, str) else None

    data: dict[str, Any] = {}
    for key in ("id", "version"):
        value = string(key)
        if value is not None:
            data[key] = value

    depends = re.search(r'"depends_on"\s*:\s*\[([^\]]*)\]', head, re.S)
    if depends:
        data["depends_on"] = re.findall(r'"([^"]+)"', depends.group(1))

    self_load = re.search(r'"self_load_only"\s*:\s*(true|false)', head)
    if self_load:
        data["self_load_only"] = self_load.group(1) == "true"

    capabilities = re.search(
        r'"capability_keys"\s*:\s*\[([^\]]*)\]', head, re.S)
    if capabilities:
        data["capability_keys"] = re.findall(r'"([^"]+)"',
                                             capabilities.group(1))
    return data


def _read_manifest(path: Path) -> tuple[dict[str, Any], bool, str | None]:
    """Return ``(data, partial, error)`` without reading an oversized file."""
    try:
        size = path.stat().st_size
    except OSError as exc:
        return {}, False, f"cannot stat manifest: {exc}"
    if size > MAX_VALIDATE_BYTES:
        try:
            return _head_metadata(path), True, None
        except OSError as exc:
            return {}, True, f"cannot read manifest head: {exc}"
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except OSError as exc:
        return {}, False, f"cannot read manifest: {exc}"
    except json.JSONDecodeError as exc:
        return {}, False, f"invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}"
    if not isinstance(data, dict):
        return {}, False, "manifest is not a JSON object"
    return data, False, None


def _empty(value: Any) -> bool:
    return value is None or value == [] or value == {} or value == ""


def _string_list(value: Any) -> bool:
    return (isinstance(value, list)
            and all(isinstance(item, str) and item.strip() for item in value))


def _validate_metadata(
    data: Mapping[str, Any], *, root: Path, path: Path, seed_id: str,
    rel: str, report: _Report, checks: list[dict[str, Any]],
    partial: bool,
) -> list[str]:
    """Validate identity, version, flags and dependencies.

    Returns the direct dependency names that are syntactically usable.
    """
    actual_id = data.get("id", _MISSING)
    if actual_id is _MISSING:
        if partial:
            report.warning(
                f"{rel}: manifest head does not expose 'id' for partial validation")
            _check(checks, "filename", True, "id is outside the validated head")
        else:
            report.warning(
                f"{rel}: id is absent; the filename supplies the seed id")
            _check(checks, "filename", True,
                   f"id absent; filename is {path.stem!r}")
    elif not isinstance(actual_id, str) or not actual_id.strip():
        report.error(f"{rel}: id must be a non-empty string")
        _check(checks, "filename", False, "invalid id")
    elif actual_id != path.stem or actual_id != seed_id:
        report.error(
            f"{rel}: id {actual_id!r} differs from filename {path.stem!r}"
            + ("" if actual_id == seed_id else f" and requested seed {seed_id!r}"))
        _check(checks, "filename", False,
               f"id {actual_id!r} != filename {path.stem!r}")
    else:
        _check(checks, "filename", True, f"id matches {path.name}")

    version = data.get("version", _MISSING)
    if version is _MISSING:
        if partial:
            report.warning(
                f"{rel}: manifest head does not expose 'version' for partial validation")
            _check(checks, "version", True, "version is outside the validated head")
        else:
            report.warning(
                f"{rel}: version is absent; Ocelot's loader does not require it")
            _check(checks, "version", True, "version absent")
    elif not isinstance(version, str) or not version.strip():
        report.error(f"{rel}: version must be a non-empty string")
        _check(checks, "version", False, "invalid version")
    else:
        _check(checks, "version", True, version)

    self_load = data.get("self_load_only", _MISSING)
    if self_load is _MISSING:
        _check(checks, "self_load_only", True,
               "absent (Ocelot defaults it to false)"
               if not partial else "outside the validated head")
    elif not isinstance(self_load, bool):
        report.error(f"{rel}: self_load_only must be a boolean")
        _check(checks, "self_load_only", False, "not a boolean")
    else:
        _check(checks, "self_load_only", True, str(self_load).lower())

    capabilities = data.get("capability_keys", _MISSING)
    if capabilities is _MISSING:
        _check(checks, "capability_keys", True, "absent")
    elif not isinstance(capabilities, list):
        report.error(f"{rel}: capability_keys must be a list")
        _check(checks, "capability_keys", False, "not a list")
    else:
        bad = [index for index, item in enumerate(capabilities)
               if not isinstance(item, str) or not item.strip()]
        for index in bad:
            report.error(
                f"{rel}: capability_keys[{index}] must be a non-empty string")
        _check(checks, "capability_keys", not bad,
               "all entries are strings" if not bad else "invalid entries")

    depends = data.get("depends_on", _MISSING)
    deps: list[str] = []
    if depends is _MISSING:
        if partial:
            report.warning(
                f"{rel}: manifest head does not expose 'depends_on' "
                "for partial validation")
            _check(checks, "dependencies", True,
                   "depends_on is outside the validated head")
        else:
            report.warning(
                f"{rel}: depends_on is absent; treating it as no dependencies")
            _check(checks, "dependencies", True, "depends_on absent")
    elif not isinstance(depends, list):
        report.error(f"{rel}: depends_on must be a list of strings")
        _check(checks, "dependencies", False, "not a list")
    else:
        dep_ok = True
        seen: set[str] = set()
        for index, dep in enumerate(depends):
            if not isinstance(dep, str) or not dep.strip():
                report.error(
                    f"{rel}: depends_on[{index}] must be a non-empty string")
                dep_ok = False
                continue
            if dep in seen:
                report.warning(
                    f"{rel}: depends_on lists {dep!r} more than once")
                continue
            seen.add(dep)
            deps.append(dep)
            if not _is_safe_seed_id(dep):
                report.error(
                    f"{rel}: depends_on[{index}] {dep!r} is not a valid seed id")
                dep_ok = False
                continue
            if dep in BUILTIN_SEEDS:
                continue
            if not (root / SEED_DIR / f"{dep}.json").is_file():
                report.error(
                    f"{rel}: depends_on references missing seed {dep!r} "
                    f"(expected {SEED_DIR}/{dep}.json)")
                dep_ok = False
        _check(checks, "dependencies", dep_ok,
               f"{len(deps)} dependency reference(s) resolved"
               if dep_ok else "unresolved or malformed dependency")
    return deps


def _dependency_names(root: Path, seed_id: str, report: _Report) -> list[str]:
    path = root / SEED_DIR / f"{seed_id}.json"
    try:
        data, _partial, error = _read_manifest(path)
        if error:
            report.warning(
                f"{_display_path(path, root)}: cannot read dependency "
                f"manifest for cycle check: {error}")
            return []
        depends = data.get("depends_on")
        if isinstance(depends, list):
            return [dep for dep in depends if isinstance(dep, str) and dep.strip()]
    except Exception as exc:  # noqa: BLE001 - cycle checks are best effort
        report.warning(
            f"{_display_path(path, root)}: cannot read dependency manifest "
            f"for cycle check: {exc}")
    return []


def _dependency_cycles(
    root: Path, seed_id: str, *, rel: str, report: _Report,
) -> list[list[str]]:
    """Dependency cycles reachable from *seed_id*'s manifests."""
    cache: dict[str, list[str]] = {}
    state: dict[str, int] = {}
    stack: list[str] = []
    cycles: list[list[str]] = []
    reads = 0

    def names(current: str) -> list[str]:
        nonlocal reads
        if current in cache:
            return cache[current]
        if reads >= MAX_DEPENDENCY_FILES:
            report.warning(
                f"{rel}: dependency cycle check stopped after "
                f"{MAX_DEPENDENCY_FILES} manifest reads")
            return []
        reads += 1
        values = _dependency_names(root, current, report)
        cache[current] = values
        return values

    def visit(current: str) -> None:
        if len(stack) >= MAX_DEPENDENCY_DEPTH:
            report.warning(
                f"{rel}: dependency cycle check stopped at depth "
                f"{MAX_DEPENDENCY_DEPTH}")
            return
        state[current] = 1
        stack.append(current)
        for dep in names(current):
            if dep in BUILTIN_SEEDS or not _is_safe_seed_id(dep):
                continue
            if not (root / SEED_DIR / f"{dep}.json").is_file():
                continue
            if dep in stack:
                cycle = stack[stack.index(dep):] + [dep]
                if cycle not in cycles:
                    cycles.append(cycle)
                continue
            if state.get(dep) == 2:
                continue
            visit(dep)
        stack.pop()
        state[current] = 2

    visit(seed_id)
    return cycles


def _validate_node(node: Any, index: int, *, rel: str, report: _Report,
                   local_ids: dict[int, int]) -> bool:
    if not isinstance(node, dict):
        report.error(f"{rel}: nodes[{index}] must be an object")
        return False
    ok = True
    local_id = node.get("local_id")
    if isinstance(local_id, bool) or not isinstance(local_id, int):
        report.error(f"{rel}: nodes[{index}].local_id must be an integer")
        ok = False
    elif local_id in local_ids:
        report.error(
            f"{rel}: nodes[{index}].local_id {local_id} duplicates "
            f"nodes[{local_ids[local_id]}]")
        ok = False
    else:
        local_ids[local_id] = index
    node_type = node.get("type")
    if not isinstance(node_type, str) or not node_type.strip():
        report.error(f"{rel}: nodes[{index}].type must be a non-empty string")
        ok = False
    attrs = node.get("attrs")
    if not isinstance(attrs, dict):
        report.error(f"{rel}: nodes[{index}].attrs must be an object")
        ok = False
    return ok


def _validate_ref(
    ref: Any, *, rel: str, where: str, local_ids: Mapping[int, int],
    report: _Report,
) -> bool:
    if not isinstance(ref, dict):
        report.error(f"{rel}: {where} must be an object")
        return False
    has_local = "local" in ref
    has_role = "role" in ref
    has_type = "type" in ref
    modes = sum((has_local, has_role, has_type))
    if modes == 0:
        report.error(
            f"{rel}: {where} must contain 'local', 'role', or a cross-seed "
            "'type' with a string 'name' or 'id'")
        return False
    if modes > 1:
        report.error(
            f"{rel}: {where} is ambiguous; use only one of 'local', 'role' "
            "or cross-seed 'type'")
        return False
    if has_local:
        local = ref.get("local")
        if isinstance(local, bool) or not isinstance(local, int):
            report.error(f"{rel}: {where}.local must be an integer local_id")
            return False
        if local not in local_ids:
            report.error(
                f"{rel}: {where}.local {local} does not resolve to a node local_id")
            return False
        return True
    if has_role:
        role = ref.get("role")
        if not isinstance(role, str) or not role.strip():
            report.error(f"{rel}: {where}.role must be a non-empty string")
            return False
        if role not in KNOWN_ROLES:
            report.warning(
                f"{rel}: {where}.role {role!r} is not a known Ocelot role "
                f"({', '.join(sorted(KNOWN_ROLES))})")
        return True
    node_type = ref.get("type")
    if not isinstance(node_type, str) or not node_type.strip():
        report.error(f"{rel}: {where}.type must be a non-empty string")
        return False
    name = ref.get("name")
    ident = ref.get("id")
    if ((isinstance(name, str) and name.strip())
            or (isinstance(ident, str) and ident.strip())):
        return True
    report.error(
        f"{rel}: {where} is a cross-seed reference without a non-empty "
        "string 'name' or 'id'")
    return False


def _validate_edges(
    edges: Any, *, rel: str, report: _Report, local_ids: Mapping[int, int],
) -> tuple[bool, list[str]]:
    if not isinstance(edges, list):
        report.error(f"{rel}: edges must be a list")
        return False, []
    ok = True
    types: list[str] = []
    for index, edge in enumerate(edges):
        if not isinstance(edge, dict):
            report.error(f"{rel}: edges[{index}] must be an object")
            ok = False
            continue
        edge_type = edge.get("type")
        if not isinstance(edge_type, str) or not edge_type.strip():
            report.error(
                f"{rel}: edges[{index}].type must be a non-empty string")
            ok = False
        else:
            types.append(edge_type)
        for side in ("src", "tgt"):
            if not _validate_ref(
                    edge.get(side), rel=rel, where=f"edges[{index}].{side}",
                    local_ids=local_ids, report=report):
                ok = False
    return ok, types


def _rule_edge_types(rule: Mapping[str, Any]) -> set[str]:
    """Edge types read or written by a rule."""
    out: set[str] = set()
    match = rule.get("match")
    if isinstance(match, list):
        for clause in match:
            if (isinstance(clause, list) and len(clause) >= 3
                    and isinstance(clause[1], str) and clause[1].strip()):
                out.add(clause[1])
    effects = rule.get("effects")
    if isinstance(effects, list):
        for effect in effects:
            if isinstance(effect, list) and len(effect) >= 3:
                if (isinstance(effect[0], str)
                        and effect[0] in ("add_edge", "remove_edge")
                        and isinstance(effect[2], str)
                        and effect[2].strip()):
                    out.add(effect[2])
            elif isinstance(effect, dict):
                op = effect.get("op") or effect.get("kind")
                edge = effect.get("edge_type") or effect.get("edge")
                if (op in ("add_edge", "remove_edge")
                        and isinstance(edge, str) and edge.strip()):
                    out.add(edge)
    return out


def _validate_productions(
    productions: Any, *, rel: str, report: _Report,
) -> tuple[bool, set[str]]:
    if not isinstance(productions, list):
        report.error(f"{rel}: productions must be a list")
        return False, set()
    ok = True
    edge_types: set[str] = set()
    for index, production in enumerate(productions):
        if isinstance(production, dict):
            edge_type = production.get("edge_type")
            if not isinstance(edge_type, str) or not edge_type.strip():
                report.error(
                    f"{rel}: productions[{index}].edge_type must be a "
                    "non-empty string")
                ok = False
            else:
                edge_types.add(edge_type)
        elif isinstance(production, str):
            report.warning(
                f"{rel}: productions[{index}] is a legacy string entry")
            if production.strip():
                edge_types.add(production)
        elif isinstance(production, list):
            report.warning(
                f"{rel}: productions[{index}] is a legacy list entry")
        else:
            report.error(
                f"{rel}: productions[{index}] must be an object, list, or string")
            ok = False
    return ok, edge_types


def _validate_rules(
    rules: Any, *, rel: str, report: _Report,
) -> tuple[bool, set[str]]:
    if not isinstance(rules, list):
        report.error(f"{rel}: rules must be a list")
        return False, set()
    ok = True
    edge_types: set[str] = set()
    for index, rule in enumerate(rules):
        if not isinstance(rule, dict):
            report.error(f"{rel}: rules[{index}] must be an object")
            ok = False
            continue
        name = rule.get("name")
        label = f"rules[{index}]"
        if not isinstance(name, str) or not name.strip():
            report.error(f"{rel}: {label}.name must be a non-empty string")
            ok = False
        else:
            label = f"rules[{index}] ({name!r})"
        edge_types.update(_rule_edge_types(rule))

        legacy = (("if" in rule and "then" in rule)
                  or ("body" in rule and "head" in rule))
        if legacy:
            shape = "if/then" if "if" in rule else "body/head"
            report.warning(
                f"{rel}: {label} is a legacy declarative {shape} rule; "
                "accepted without match/effects")
            continue

        match = rule.get("match", _MISSING)
        if match is _MISSING or _empty(match):
            report.warning(f"{rel}: {label} has no match conditions")
        elif not isinstance(match, (list, dict)):
            report.error(f"{rel}: {label}.match must be a list or object")
            ok = False

        effects = rule.get("effects", _MISSING)
        if effects is _MISSING:
            report.error(f"{rel}: {label} is missing effects")
            ok = False
        elif not isinstance(effects, list):
            report.error(f"{rel}: {label}.effects must be a list")
            ok = False
        elif not effects:
            report.warning(f"{rel}: {label}.effects is empty")
    return ok, edge_types


def _looks_like_reflex(value: Mapping[str, Any]) -> bool:
    return any(key in value for key in ("name", "stimulus", "response"))


def _validate_reflex_entry(
    entry: Any, *, rel: str, where: str, report: _Report,
    fallback_name: str | None = None,
) -> bool:
    if not isinstance(entry, dict):
        report.error(f"{rel}: {where} must be an object")
        return False
    ok = True
    name = entry.get("name", fallback_name)
    if not isinstance(name, str) or not name.strip():
        report.error(f"{rel}: {where}.name must be a non-empty string")
        ok = False
    if "stimulus" not in entry:
        report.error(f"{rel}: {where} is missing stimulus")
        ok = False
    response = entry.get("response")
    if not isinstance(response, str) or not response.strip():
        report.error(f"{rel}: {where}.response must be a non-empty string")
        ok = False
    ticks = entry.get("refractory_ticks", _MISSING)
    if (ticks is not _MISSING
            and (isinstance(ticks, bool) or not isinstance(ticks, int))):
        report.error(f"{rel}: {where}.refractory_ticks must be an integer")
        ok = False
    return ok


def _validate_reflexes(
    reflexes: Any, *, rel: str, report: _Report,
) -> tuple[bool, int]:
    if not isinstance(reflexes, (list, dict)):
        report.error(f"{rel}: reflexes must be a list or object")
        return False, 0
    ok = True
    if isinstance(reflexes, list):
        for index, reflex in enumerate(reflexes):
            if not _validate_reflex_entry(
                    reflex, rel=rel, where=f"reflexes[{index}]",
                    report=report):
                ok = False
        return ok, len(reflexes)

    if _looks_like_reflex(reflexes):
        report.warning(
            f"{rel}: reflexes is a single object; treating it as one reflex")
        ok = _validate_reflex_entry(
            reflexes, rel=rel, where="reflexes", report=report)
        return ok, 1

    report.warning(
        f"{rel}: reflexes is an object rather than a list; "
        "validating its mapping values")
    for name, reflex in reflexes.items():
        if not _validate_reflex_entry(
                reflex, rel=rel, where=f"reflexes[{name!r}]",
                report=report,
                fallback_name=str(name) if isinstance(name, str) else None):
            ok = False
    return ok, len(reflexes)


def _missing_production_warnings(
    *, rel: str, report: _Report, edge_types: Iterable[str],
    rule_types: Iterable[str], production_types: set[str],
) -> int:
    usages: dict[str, list[str]] = {}
    for edge_type in edge_types:
        usages.setdefault(edge_type, []).append("edges")
    for edge_type in rule_types:
        usages.setdefault(edge_type, []).append("rules")
    missing = sorted(set(usages) - production_types)
    for edge_type in missing[:MAX_MISSING_PRODUCTION_WARNINGS]:
        sources = ", ".join(sorted(set(usages[edge_type])))
        report.warning(
            f"{rel}: edge type {edge_type!r} has no production "
            f"(referenced by the seed's {sources})")
    if len(missing) > MAX_MISSING_PRODUCTION_WARNINGS:
        report.warning(
            f"{rel}: {len(missing) - MAX_MISSING_PRODUCTION_WARNINGS} more "
            "edge type(s) have no production")
    return len(missing)


def _memory_context(
    memory: Any, *, seed_id: str, root: Path, path: Path, rel: str,
    report: _Report, checks: list[dict[str, Any]],
) -> dict[str, Any]:
    """Add graph context when the seed is indexed; never a hard requirement."""
    try:
        from . import workspace
    except Exception as exc:  # noqa: BLE001 - optional adapter
        report.warning(f"{rel}: graph context unavailable: {exc}")
        return {"indexed": False}

    try:
        summary = workspace.seed_summary(memory, seed_id, include="all")
    except Exception as exc:  # noqa: BLE001 - graph context is optional
        report.warning(f"{rel}: graph context unavailable: {exc}")
        _check(checks, "graph", True, "unavailable; manifest validation used")
        return {"indexed": False}

    if not isinstance(summary, dict) or not summary.get("ok"):
        report.warning(
            f"{rel}: seed {seed_id!r} is not indexed in the graph; "
            "manifest validation used")
        _check(checks, "graph", True, "seed is not indexed")
        return {"indexed": False}

    indexed_path = summary.get("path")
    if isinstance(indexed_path, str) and indexed_path and indexed_path != rel:
        report.warning(
            f"{rel}: graph path {indexed_path!r} differs from the "
            "validated manifest path")
    _check(checks, "graph", True,
           f"indexed at {indexed_path or rel}")

    cycles: list[list[str]] = []
    try:
        census = workspace.seed_census(memory)
        cycles = [list(cycle) for cycle in census.get("cycles") or []
                  if seed_id in cycle]
    except Exception:  # noqa: BLE001 - census is extra context
        pass

    context = {
        "indexed": True,
        "path": indexed_path,
        "blast_radius": summary.get("blast_radius"),
        "fanout": summary.get("fanout"),
        "dependencies": list(summary.get("dependencies") or []),
        "dependents": list(summary.get("dependents") or []),
        "boot_files": list(summary.get("boot_files") or []),
        "tests": list(summary.get("tests") or []),
        "cycles": cycles,
    }
    for cycle in cycles:
        message = "dependency cycle: " + " -> ".join(cycle)
        if message not in report.errors:
            report.error(f"{rel}: {message}")
    return context


def validate_seed(root: str | Path, seed_id: str, *,
                  memory: Any | None = None) -> dict[str, Any]:
    """Structurally validate one Ocelot seed manifest without loading it.

    ``ok`` is false only for structural errors.  Warnings (legacy rule forms,
    unknown roles, missing productions, absent graph context) do not fail the
    validation.  Ocelot's loader is still authoritative for runtime loading.
    """
    seed_id = str(seed_id or "").strip()
    root_path = Path(root).expanduser().resolve()
    report = _Report()
    checks: list[dict[str, Any]] = []
    counts = {"nodes": 0, "edges": 0, "productions": 0, "rules": 0,
              "reflexes": 0}
    result: dict[str, Any] = {
        "ok": False,
        "seed": seed_id,
        "path": f"{SEED_DIR}/{seed_id}.json" if _is_safe_seed_id(seed_id)
        else str(seed_id),
        "errors": report.errors,
        "warnings": report.warnings,
        "checks": checks,
        "counts": counts,
        "partial": False,
    }

    if not _is_safe_seed_id(seed_id):
        report.error(
            f"invalid seed id {seed_id!r}; expected a filename stem such as "
            "'my_seed'")
        _check(checks, "path", False, "invalid seed id")
        report.finish()
        return result

    path = _seed_path(root_path, seed_id, memory)
    rel = _display_path(path, root_path)
    result["path"] = rel
    if not path.is_file():
        report.error(f"{rel}: seed manifest not found")
        _check(checks, "path", False, "manifest not found")
        if memory is not None:
            result["graph"] = _memory_context(
                memory, seed_id=seed_id, root=root_path, path=path,
                rel=rel, report=report, checks=checks)
        report.finish()
        return result
    _check(checks, "path", True, rel)

    data, partial, error = _read_manifest(path)
    result["partial"] = partial
    if partial:
        report.warning(
            f"{rel}: manifest exceeds full validation size "
            f"({MAX_VALIDATE_BYTES} bytes); validated head metadata only")
        _check(checks, "size", False, "full validation skipped")
    else:
        _check(checks, "size", True, "within the full-validation size cap")
    if error:
        report.error(f"{rel}: {error}")
        _check(checks, "json", False, error)
        if memory is not None:
            result["graph"] = _memory_context(
                memory, seed_id=seed_id, root=root_path, path=path,
                rel=rel, report=report, checks=checks)
        report.finish()
        return result
    _check(checks, "json", True, "manifest parsed")

    deps = _validate_metadata(
        data, root=root_path, path=path, seed_id=seed_id, rel=rel,
        report=report, checks=checks, partial=partial)
    if deps:
        cycles = _dependency_cycles(root_path, seed_id, rel=rel, report=report)
        for cycle in cycles:
            report.error(f"{rel}: dependency cycle: " + " -> ".join(cycle))
        _check(checks, "dependency_cycle", not cycles,
               "no dependency cycle" if not cycles
               else " -> ".join(cycles[0]))
    else:
        _check(checks, "dependency_cycle", True, "no dependencies to walk")

    if partial:
        _check(checks, "nodes", True, "outside the validated head")
        _check(checks, "edges", True, "outside the validated head")
        _check(checks, "productions", True, "outside the validated head")
        _check(checks, "rules", True, "outside the validated head")
        _check(checks, "reflexes", True, "outside the validated head")
    else:
        nodes = data.get("nodes", [])
        local_ids: dict[int, int] = {}
        nodes_ok = True
        if not isinstance(nodes, list):
            report.error(f"{rel}: nodes must be a list")
            nodes_ok = False
            nodes = []
        else:
            for index, node in enumerate(nodes):
                if not _validate_node(
                        node, index, rel=rel, report=report,
                        local_ids=local_ids):
                    nodes_ok = False
        counts["nodes"] = len(nodes)
        _check(checks, "nodes", nodes_ok,
               f"{len(nodes)} node(s), {len(local_ids)} unique local_id(s)")

        edges = data.get("edges", [])
        if isinstance(edges, list):
            edges_ok, edge_types = _validate_edges(
                edges, rel=rel, report=report, local_ids=local_ids)
            counts["edges"] = len(edges)
        else:
            edges_ok, edge_types = _validate_edges(
                edges, rel=rel, report=report, local_ids=local_ids)
            counts["edges"] = 0
        _check(checks, "edges", edges_ok, f"{counts['edges']} edge(s)")

        productions = data.get("productions", [])
        productions_ok, production_types = _validate_productions(
            productions, rel=rel, report=report)
        counts["productions"] = (
            len(productions) if isinstance(productions, list) else 0)
        _check(checks, "productions", productions_ok,
               f"{counts['productions']} production(s)")

        rules = data.get("rules", [])
        rules_ok, rule_types = _validate_rules(
            rules, rel=rel, report=report)
        counts["rules"] = len(rules) if isinstance(rules, list) else 0
        _check(checks, "rules", rules_ok, f"{counts['rules']} rule(s)")

        reflexes = data.get("reflexes", [])
        reflexes_ok, reflex_count = _validate_reflexes(
            reflexes, rel=rel, report=report)
        counts["reflexes"] = reflex_count
        _check(checks, "reflexes", reflexes_ok,
               f"{reflex_count} reflex/reflexes")

        missing = _missing_production_warnings(
            rel=rel, report=report, edge_types=edge_types,
            rule_types=rule_types, production_types=production_types)
        if missing:
            _check(checks, "production_coverage", False,
                   f"{missing} edge type(s) lack a production (warning)")
        else:
            _check(checks, "production_coverage", True,
                   "every referenced edge type has a production")

    if memory is not None:
        result["graph"] = _memory_context(
            memory, seed_id=seed_id, root=root_path, path=path, rel=rel,
            report=report, checks=checks)

    report.finish()
    result["ok"] = not report.errors
    return result


def seed_validate(memory: Any, seed_id: str) -> dict[str, Any]:
    """Adapter helper: validate a seed in the memory's repository."""
    root = getattr(memory, "raw_repo_root", "")
    if not root:
        return {
            "ok": False,
            "seed": str(seed_id or ""),
            "path": "",
            "errors": ["memory has no raw_repo_root"],
            "warnings": [],
            "checks": [{"name": "path", "ok": False,
                        "detail": "memory has no raw_repo_root"}],
            "counts": {"nodes": 0, "edges": 0, "productions": 0,
                       "rules": 0, "reflexes": 0},
            "partial": False,
        }
    return validate_seed(root, str(seed_id or ""), memory=memory)


def _tool_validate(memory: Any, args: Mapping[str, Any]) -> Any:
    return seed_validate(memory, str(args.get("id") or ""))


SEED_VALIDATE_TOOL = {
    "name": "ocelot_seed_validate",
    "description": (
        "Read-only structural validation of an Ocelot seed manifest. "
        "Checks ids, version, dependencies, cycles, nodes, edge references, "
        "productions, rules and reflexes. Warnings do not fail; Ocelot's "
        "loader remains authoritative."),
    "inputSchema": {
        "type": "object",
        "properties": {"id": {"type": "string"}},
        "required": ["id"],
        "additionalProperties": False,
    },
}


def register_validate(registry: Any) -> None:
    """Register the read-only seed validator as a query and MCP tool."""
    if getattr(registry, "_ocelot_validate_registered", False):
        return
    setattr(registry, "_ocelot_validate_registered", True)
    registry.add_graph_query(
        "ocelot_seed_validate",
        lambda memory, **kw: seed_validate(memory, str(kw.get("id") or "")))
    registry.add_mcp_tool(SEED_VALIDATE_TOOL, _tool_validate)


__all__ = [
    "MAX_VALIDATE_BYTES",
    "MAX_VALIDATE_HEAD_BYTES",
    "SEED_VALIDATE_TOOL",
    "register_validate",
    "seed_validate",
    "validate_seed",
]
