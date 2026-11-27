"""Read-only Ozagia adapter for Bigville scenarios and their seed links.

The adapter reads ``scenarios/*.json`` and connects scenario manifests to the
seed manifests they name. It does not import Bigville or execute simulations.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any


_SCENARIO_REF = re.compile(r"(scenarios/[A-Za-z0-9_./-]+\.json)")
_ADAPTER_TEXT_REF = re.compile(
    r"(?:scenarios/[A-Za-z0-9_./-]+\.json|"
    r"(?:bigville/)?seeds/[A-Za-z0-9_.-]+\.json|"
    r"packs/[A-Za-z0-9_./-]+\.json)")
_ROUTE_CHECK = re.compile(r"(?:parsed\.path|path)\s*==\s*['\"]([^'\"]+)['\"]")
_API_ROUTE_CHECK = re.compile(r'path\s*==\s*f"\{API_PREFIX\}([^\"]+)"')
_API_STRING_SUFFIX = re.compile(r'f"\{API_PREFIX\}([^\"]+)"')
_FETCH_LITERAL = re.compile(r"fetch\(\s*['\"]([^'\"]+)['\"]")
_REQUEST_PATH_LITERAL = re.compile(r"requestJson\(\s*['\"]([^'\"]+)['\"]")
_REQUEST_PATH_TEMPLATE = re.compile(r"requestJson\(\s*`([^`]+)`")
_JS_METHOD = re.compile(r"method\s*:\s*['\"](GET|POST|PUT|DELETE|PATCH)['\"]")
_API_PREFIX = re.compile(r"API_PREFIX\s*=\s*['\"]([^'\"]+)['\"]")
_JS_API_ROOT = re.compile(r"const\s+API\s*=\s*['\"]([^'\"]+)['\"]")
_SEED_PATH_REF = re.compile(r"((?:bigville/)?seeds/[A-Za-z0-9_.-]+\.json)")
_ROUTE_SOURCES = (
    "bigville/server.py",
    "bigville/management/http.py",
    "bigville/game/game.js",
    "bigville/management/static/management.js",
    "bigville/management/static/readonly-renderer.js",
)


def register(registry: Any) -> None:
    """Register the Bigville scenario indexer and read-only query."""
    try:
        registry.add_indexer(index_scenarios, watch=("scenarios/**/*.json",),
                             texts=_SCENARIO_REF)
    except TypeError:  # Compatibility with older Ozagia adapter interfaces.
        registry.add_indexer(index_scenarios)
    registry.add_graph_query("bigville_scenarios", list_scenarios)
    try:
        registry.add_indexer(index_navigation,
                             watch=("seeds/*.json", "bigville/seeds/*.json",
                                    "packs/**/*.json", *_ROUTE_SOURCES),
                             texts=_ADAPTER_TEXT_REF)
    except TypeError:
        registry.add_indexer(index_navigation)
    registry.add_graph_query("bigville_routes", list_routes)
    registry.add_graph_query("bigville_seed_manifests", list_seed_manifests)
    registry.add_graph_query("bigville_data_test_evidence", list_data_test_evidence)


def index_scenarios(ctx: Any) -> None:
    """Index scenario declarations and their references, without writes."""
    b = ctx.builder
    root = Path(ctx.root).resolve()
    paths = _safe_json_paths(root, root / "scenarios")
    records: list[tuple[Path, dict[str, Any], str, list[str]]] = []
    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        scenario_id = str(data.get("id") or data.get("scenario_id") or path.stem)
        seed_ids = data.get("seeds") or []
        if isinstance(seed_ids, str):
            seed_ids = [seed_ids]
        if not isinstance(seed_ids, list):
            seed_ids = []
        records.append((path, data, scenario_id,
                        sorted({str(seed) for seed in seed_ids if seed is not None})))
    id_counts = Counter(scenario_id for _, _, scenario_id, _ in records)
    scenario_by_path: dict[str, int] = {}
    for path, data, scenario_id, seed_ids in records:
        rel = path.relative_to(root).as_posix()
        actors = data.get("actors")
        attrs = {
            "id": scenario_id,
            "name": str(data.get("name") or scenario_id),
            "description": str(data.get("description") or "")[:1000],
            "path": rel,
            "actor_count": len(actors) if isinstance(actors, list) else 0,
            "actor_names": [str(a.get("name", "")) for a in actors
                            if isinstance(a, dict)] if isinstance(actors, list) else [],
            "seed_ids": seed_ids,
            "unresolved_seed_ids": [],
        }
        # Keep the historical ID key for unique IDs. When IDs collide, add
        # the source path to the key so both authored manifests remain distinct.
        key = ("bigville_scenario", scenario_id)
        if id_counts[scenario_id] > 1:
            key = (*key, rel)
        sid = b.node("Scenario", attrs, key=key)
        scenario_by_path[rel] = sid
        fid = ctx.file_node(rel)
        b.edge(ctx.repo, "has_scenario", sid)
        b.edge(fid, "defines", sid)
        b.edge(sid, "defined_in", fid)
    # Connect source, test and documentation nodes that name scenario paths.
    for group in (ctx.sources, ctx.tests, ctx.docs):
        for item in group:
            for scenario_path in set(_SCENARIO_REF.findall(item.text)):
                target = scenario_by_path.get(Path(scenario_path).as_posix())
                if target is None:
                    continue
                b.edge(item.node, "mentions_scenario", target)
                for test_id in getattr(item, "tests", ()):
                    b.edge(test_id, "mentions_scenario", target)

    b.attrs(ctx.repo)["scenarios"] = b.count("Scenario")


def _safe_json_paths(root: Path, directory: Path) -> list[Path]:
    """Enumerate JSON files but do not read symlinks outside the checkout."""
    if not directory.is_dir():
        return []
    paths = []
    for path in directory.rglob("*.json"):
        try:
            path.resolve().relative_to(root)
        except (OSError, ValueError):
            continue
        if path.is_file():
            paths.append(path)
    return sorted(paths)


def _load_object(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def index_navigation(ctx: Any) -> None:
    """Index real Bigville seed/pack manifests and bounded route evidence."""
    b = ctx.builder
    root = Path(ctx.root).resolve()
    seed_paths = [*_safe_json_paths(root, root / "seeds"),
                  *_safe_json_paths(root, root / "bigville" / "seeds")]
    manifests: list[tuple[Path, dict[str, Any], str]] = []
    for path in seed_paths:
        data = _load_object(path)
        if data is None:
            continue
        seed_id = str(data.get("id") or data.get("seed") or path.stem)
        # A valid seed manifest has a recognized seed payload. Skip unrelated
        # JSON and malformed files instead of inventing seed records.
        if not any(key in data for key in ("rules", "nodes", "edges", "productions", "concepts")):
            continue
        manifests.append((path, data, seed_id))

    candidates_by_id: dict[str, list[int]] = {}
    for path, data, seed_id in manifests:
        rel = path.relative_to(root).as_posix()
        raw_deps = data.get("depends_on") or []
        deps = [str(dep) for dep in raw_deps] if isinstance(raw_deps, list) else []
        generated = data.get("generated_from")
        generated = generated if isinstance(generated, dict) else None
        source_kind = "packaged_runtime_seed" if rel.startswith("bigville/seeds/") else "legacy_root_seed"
        node = b.node("BigvilleSeedManifest", {
            "id": seed_id, "name": seed_id, "version": str(data.get("version", "")),
            "path": rel, "description": str(data.get("description") or data.get("comment") or "")[:1000],
            "depends_on": deps, "rule_count": _dict_count(data.get("rules")),
            "node_count": _dict_count(data.get("nodes")),
            "concept_count": _dict_count(data.get("concepts")),
            "source_kind": source_kind,
            "generated_from": generated,
            "freshness": "unverified_declared_generation" if generated else "not_declared_generated",
            "unresolved_generated_tables": [],
        }, key=("bigville_seed_manifest", rel))
        candidates_by_id.setdefault(seed_id, []).append(node)
        fid = ctx.file_node(rel)
        b.edge(ctx.repo, "has_seed_manifest", node)
        b.edge(fid, "defines", node)
        b.edge(node, "defined_in", fid)
        for item in _seed_item_names(data):
            item_node = b.node("BigvilleSeedItem", {
                "name": item["name"], "seed_id": seed_id, "seed_path": rel,
                "kind": item["kind"], "container": item["container"],
                **item["content"],
            }, key=("bigville_seed_item", rel, item["kind"], item["name"]))
            b.edge(node, "contains_item", item_node)
            b.edge(item_node, "defined_in", fid)

    # Resolve ID-only references to path-distinct candidates. Duplicated ids
    # are explicitly marked ambiguous; no candidate overwrites another.
    for path, data, seed_id in manifests:
        rel = path.relative_to(root).as_posix()
        node = b.find(("bigville_seed_manifest", rel))
        if node is None:
            continue
        deps = data.get("depends_on") if isinstance(data.get("depends_on"), list) else []
        missing, ambiguous = [], []
        for dep in sorted({str(dep) for dep in deps}):
            matches = candidates_by_id.get(dep, [])
            if not matches:
                missing.append(dep)
            elif len(matches) > 1:
                ambiguous.append(dep)
            for match in matches:
                b.edge(node, "declares_dependency_candidate", match,
                       attrs={"seed_id": dep,
                              "resolution": "unique" if len(matches) == 1 else "ambiguous_id_only"})
        attrs = b.attrs(node)
        attrs["unresolved_dependencies"] = missing
        attrs["ambiguous_dependency_ids"] = ambiguous
    for nodes in candidates_by_id.values():
        if len(nodes) > 1:
            paths = sorted(str(b.attrs(node).get("path", "")) for node in nodes)
            for node in nodes:
                attrs = b.attrs(node)
                attrs["id_resolution"] = "ambiguous_duplicate_id"
                attrs["duplicate_manifest_paths"] = paths
        else:
            b.attrs(nodes[0])["id_resolution"] = "unique"

    # Retain aggregate counts for repository-level inspection.
    b.attrs(ctx.repo)["seed_manifests"] = b.count("BigvilleSeedManifest")
    b.attrs(ctx.repo)["seed_manifest_ids"] = len(candidates_by_id)

    # Pack manifests explicitly name their source table files. Resolve paths
    # only within each pack directory and keep missing/escaping paths visible.
    for path in _safe_json_paths(root, root / "packs"):
        if path.name != "pack.json":
            continue
        data = _load_object(path)
        if data is None:
            continue
        pack_id = str(data.get("name") or path.parent.name)
        rel = path.relative_to(root).as_posix()
        pid = b.node("BigvillePack", {
            "id": pack_id, "name": pack_id, "version": str(data.get("version", "")),
            "description": str(data.get("description", ""))[:1000],
            "path": rel, "extends": data.get("extends"),
        }, key=("bigville_pack", pack_id))
        fid = ctx.file_node(rel)
        b.edge(ctx.repo, "has_pack", pid)
        b.edge(fid, "defines", pid)
        for table in data.get("tables", []) if isinstance(data.get("tables"), list) else []:
            if not isinstance(table, dict) or not isinstance(table.get("file"), str):
                continue
            table_path = (path.parent / table["file"]).resolve()
            try:
                table_rel = table_path.relative_to(root).as_posix()
            except ValueError:
                continue
            if not table_path.is_file():
                continue
            table_node = b.node("PackTable", {
                "name": str(table.get("name") or ""), "path": table_rel,
                "pack": pack_id,
            }, key=("bigville_pack_table", pack_id, str(table.get("name", ""))))
            b.edge(pid, "contains_table", table_node)
            b.edge(table_node, "loads_from", ctx.file_node(table_rel))
    b.attrs(ctx.repo)["packs"] = b.count("BigvillePack")

    # Generated seeds declare the pack tables and hash from which they came.
    # Keep these as recorded provenance; this adapter does not regenerate the
    # pack projection or certify that current table bytes still match it.
    table_nodes = {}
    for table_node in b.find_nodes("PackTable"):
        table_attrs = b.attrs(table_node)
        table_nodes[(table_attrs.get("pack"), table_attrs.get("name"))] = table_node
    for path, data, _seed_id in manifests:
        rel = path.relative_to(root).as_posix()
        seed_node = b.find(("bigville_seed_manifest", rel))
        generated = data.get("generated_from")
        if seed_node is None or not isinstance(generated, dict):
            continue
        source_pack = str(generated.get("pack") or "")
        tables = generated.get("tables")
        unresolved = []
        for table_name in tables if isinstance(tables, list) else []:
            table_node = table_nodes.get((source_pack, str(table_name)))
            if table_node is not None:
                b.edge(seed_node, "generated_from_table", table_node,
                       attrs={"recorded_sha256": str(generated.get("sha256") or "")})
            else:
                unresolved.append(str(table_name))
        b.attrs(seed_node)["unresolved_generated_tables"] = sorted(set(unresolved))

    # Literal references to an existing pack table are useful navigation
    # evidence. They are not a claim that the mentioning test covers all users.
    table_nodes_by_path = {
        str(b.attrs(node).get("path") or ""): node
        for node in b.find_nodes("PackTable")
    }
    for group in (ctx.sources, ctx.tests, ctx.docs):
        for item in group:
            for table_path in set(re.findall(
                    r"(packs/[A-Za-z0-9_./-]+\.json)", item.text)):
                table_node = table_nodes_by_path.get(table_path)
                if table_node is not None:
                    b.edge(item.node, "mentions_pack_table", table_node)
                    for test_id in getattr(item, "tests", ()):
                        b.edge(test_id, "mentions_pack_table", table_node)

    # Join explicit scenario `seeds` values only to actual manifests. Unknown
    # values remain visible as unresolved declarations, with no stub node.
    for scenario_node in b.find_nodes("Scenario"):
        attrs = b.attrs(scenario_node)
        missing, ambiguous, candidate_paths = [], [], {}
        for seed_id in attrs.get("seed_ids", []):
            matches = candidates_by_id.get(str(seed_id), [])
            if not matches:
                missing.append(str(seed_id))
                continue
            if len(matches) > 1:
                ambiguous.append(str(seed_id))
            paths = []
            for seed_node in matches:
                seed_path = b.attrs(seed_node).get("path", "")
                paths.append(seed_path)
                b.edge(scenario_node, "references_seed_manifest", seed_node,
                       attrs={"seed_id": str(seed_id),
                              "resolution": "unique" if len(matches) == 1 else "ambiguous_id_only"})
            candidate_paths[str(seed_id)] = sorted(paths)
        attrs["unresolved_seed_ids"] = sorted(missing)
        attrs["ambiguous_seed_ids"] = sorted(ambiguous)
        attrs["seed_manifest_candidates"] = candidate_paths

    index_routes(ctx, _ROUTE_SOURCES)


def _dict_count(value: Any) -> int:
    return len(value) if isinstance(value, (list, dict)) else 0


def _seed_item_names(data: dict[str, Any]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for key, kind in (("rules", "Rule"), ("concepts", "Concept"),
                      ("nodes", "SeedNode")):
        values = data.get(key)
        if not isinstance(values, list):
            continue
        for i, value in enumerate(values):
            if isinstance(value, str):
                name = value
            elif isinstance(value, dict):
                item_attrs = value.get("attrs") if isinstance(value.get("attrs"), dict) else {}
                name = str(value.get("name") or value.get("id") or item_attrs.get("name") or f"{kind}#{i}")
            else:
                continue
            content = {}
            if isinstance(value, dict):
                for field in ("head", "body", "definition", "category", "comment"):
                    text = value.get(field)
                    if isinstance(text, str):
                        content[field] = text[:1000]
            found.append({"kind": kind, "name": name, "container": key,
                          "content": content})
    return found


def index_routes(ctx: Any, source_paths: tuple[str, ...]) -> None:
    """Create routes only from direct literal route checks or client fetches."""
    root = Path(ctx.root).resolve()
    evidence: dict[tuple[str, str], dict[str, set[str]]] = {}
    api_roots: dict[str, str] = {}
    for rel in source_paths:
        path = root / rel
        try:
            path.resolve().relative_to(root)
            text = path.read_text(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            continue
        if not path.is_file():
            continue
        for root_match in _API_PREFIX.findall(text) + _JS_API_ROOT.findall(text):
            api_roots[rel] = root_match.rstrip("/")
        lines = text.splitlines()
        handler_method = None
        for line_index, line in enumerate(lines):
            method_match = re.search(r"def do_(GET|POST|PUT|DELETE|PATCH)\b", line)
            if method_match:
                handler_method = method_match.group(1)
            api_root = api_roots.get(rel)
            for route in _ROUTE_CHECK.findall(line):
                method = handler_method or "UNKNOWN"
                evidence.setdefault((method, route), {"server": set(), "client": set()})["server"].add(rel)
            for suffix in _API_ROUTE_CHECK.findall(line):
                if api_root:
                    method = handler_method or "UNKNOWN"
                    route = api_root + suffix
                    evidence.setdefault((method, route), {"server": set(), "client": set()})["server"].add(rel)
            if handler_method == "POST":
                for suffix in _API_STRING_SUFFIX.findall(line):
                    if api_root and not suffix.endswith("/"):
                        route = api_root + suffix
                        evidence.setdefault((handler_method, route), {"server": set(), "client": set()})["server"].add(rel)
            start_match = re.search(r'path\.startswith\(f"\{API_PREFIX\}([^\"]*)"\)', line)
            end_match = re.search(r'path\.endswith\("([^\"]*)"\)', line)
            if api_root and start_match:
                start = start_match.group(1) if start_match else ""
                end = end_match.group(1) if end_match else ""
                middle = "{param}"
                route = api_root + start + middle + end
                method = handler_method or "UNKNOWN"
                evidence.setdefault((method, route), {"server": set(), "client": set()})["server"].add(rel)
            if api_root and '"/views/" in path' in line:
                route = api_root + "/runs/{param}/views/{param}"
                evidence.setdefault((handler_method or "UNKNOWN", route),
                                    {"server": set(), "client": set()})["server"].add(rel)
            if api_root and "len(parts) in {3, 4}" in line and 'parts[1] == "versions"' in line:
                for suffix in ("/versions/{param}", "/versions/{param}/load"):
                    route = api_root + "/templates/{param}" + suffix
                    evidence.setdefault((handler_method or "UNKNOWN", route),
                                        {"server": set(), "client": set()})["server"].add(rel)
            if api_root and "len(parts) != 4" in line and handler_method == "POST":
                route = api_root + "/runs/{param}/checkpoints/{param}/fork"
                evidence.setdefault((handler_method, route),
                                    {"server": set(), "client": set()})["server"].add(rel)
            if api_root and end_match and not start_match and end_match.group(1) == "/preview":
                route = api_root + "/run-drafts/{param}"
                evidence.setdefault((handler_method or "UNKNOWN", route),
                                    {"server": set(), "client": set()})["server"].add(rel)
            for route in _FETCH_LITERAL.findall(line):
                if route.startswith("/"):
                    client_method = _client_method(lines, line_index)
                    evidence.setdefault((client_method, route), {"server": set(), "client": set()})["client"].add(rel)
            for route in _REQUEST_PATH_LITERAL.findall(line):
                api_root = api_roots.get(rel)
                if api_root:
                    route = api_root + route
                    client_method = _client_method(lines, line_index)
                    evidence.setdefault((client_method, route), {"server": set(), "client": set()})["client"].add(rel)
            for route in _REQUEST_PATH_TEMPLATE.findall(line):
                api_root = api_roots.get(rel)
                if api_root and route.startswith("/"):
                    route = route.split("?", 1)[0]
                    route = re.sub(r"\$\{[^}]+\}", "{param}", route)
                    client_method = _client_method(lines, line_index)
                    evidence.setdefault((client_method, api_root + route), {"server": set(), "client": set()})["client"].add(rel)
    # Join client requests only to handler evidence for the same method and
    # path shape. Calls without a matching handler remain marked unverified.
    normalized: dict[tuple[str, str], dict[str, set[str]]] = {}
    for (method, route), groups in evidence.items():
        if groups["client"]:
            candidates = [(m, p) for m, p in evidence
                          if _route_pattern_matches(p, route)
                          if m == method and evidence[(m, p)]["server"]]
            if candidates:
                for candidate in candidates:
                    normalized.setdefault(candidate, {"server": set(), "client": set()})["client"].update(groups["client"])
            else:
                normalized.setdefault((method, route), {"server": set(), "client": set()})["client"].update(groups["client"])
        if groups["server"]:
            normalized.setdefault((method, route), {"server": set(), "client": set()})["server"].update(groups["server"])
    b = ctx.builder
    for (method, route), groups in sorted(normalized.items()):
        confidence = "explicit_handler_and_client" if groups["server"] and groups["client"] else (
            "explicit_handler_only" if groups["server"] else "client_only_unverified")
        route_node = b.node("BigvilleRoute", {
            "method": method, "path": route, "evidence": confidence,
            "server_files": sorted(groups["server"]),
            "client_files": sorted(groups["client"]),
            "uncertainty": "client URL has no matching literal server route check" if not groups["server"] else "",
        }, key=("bigville_route", method, route))
        for rel in groups["server"]:
            b.edge(ctx.file_node(rel), "handles_route", route_node)
        for rel in groups["client"]:
            b.edge(ctx.file_node(rel), "requests_route", route_node)
    # Literal manifest references add provenance edges from indexed files.
    for collection in (ctx.sources, ctx.tests, ctx.docs):
        for item in collection:
            for seed_path in set(_SEED_PATH_REF.findall(item.text)):
                # Path mentions are provenance-specific; the plain `seeds/`
                # spelling selects the root copy, while `bigville/seeds/`
                # selects the packaged runtime copy.
                for seed_node in b.find_nodes("BigvilleSeedManifest"):
                    attrs = b.attrs(seed_node)
                    if attrs.get("path") == seed_path:
                        b.edge(item.node, "mentions_seed_manifest", seed_node)
                        for test_id in getattr(item, "tests", ()):
                            b.edge(test_id, "mentions_seed_manifest", seed_node)
    b.attrs(ctx.repo)["routes"] = b.count("BigvilleRoute")


def _client_method(lines: list[str], line_index: int, *, fetch: bool = False) -> str:
    """Read an explicit nearby method option; otherwise use HTTP's GET default."""
    first = lines[line_index]
    if fetch:
        match = _JS_METHOD.search(first)
        return match.group(1) if match else "GET"
    window = [first]
    for line in lines[line_index + 1:line_index + 6]:
        if "requestJson(" in line or "fetch(" in line:
            break
        window.append(line)
    match = _JS_METHOD.search("\n".join(window))
    return match.group(1) if match else "GET"


def _route_pattern_matches(pattern: str, path: str) -> bool:
    pattern_parts = pattern.strip("/").split("/")
    path_parts = path.strip("/").split("/")
    return len(pattern_parts) == len(path_parts) and all(
        left == right or (left.startswith("{") and left.endswith("}"))
        for left, right in zip(pattern_parts, path_parts)
    )


def list_scenarios(memory: Any, *, prefix: str = "") -> list[dict[str, Any]]:
    """Return scenario summaries, optionally filtered by id/name prefix."""
    result = []
    for node in memory.nodes.values():
        if node.get("type") != "Scenario":
            continue
        attrs = node.get("attrs", {})
        if not (str(attrs.get("id", "")).startswith(prefix)
                or str(attrs.get("name", "")).lower().startswith(prefix.lower())):
            continue
        result.append({key: attrs.get(key) for key in
                       ("id", "name", "description", "path", "actor_count", "seed_ids")})
    return sorted(result, key=lambda item: (item["id"], item["path"]))


def list_routes(memory: Any, *, prefix: str = "") -> list[dict[str, Any]]:
    """Return literal route evidence, including client-only uncertainty."""
    result = []
    for node in memory.nodes.values():
        if node.get("type") != "BigvilleRoute":
            continue
        attrs = node.get("attrs", {})
        if not str(attrs.get("path", "")).startswith(prefix):
            continue
        result.append({key: attrs.get(key) for key in
                       ("method", "path", "evidence", "server_files",
                        "client_files", "uncertainty")})
    return sorted(result, key=lambda item: (item["path"], item["method"]))


def list_seed_manifests(memory: Any, *, seed_id: str = "",
                        path_prefix: str = "") -> list[dict[str, Any]]:
    """Return path-distinct manifest records, preserving duplicate ids."""
    result = []
    for node in memory.nodes.values():
        if node.get("type") != "BigvilleSeedManifest":
            continue
        attrs = node.get("attrs", {})
        if seed_id and attrs.get("id") != seed_id:
            continue
        if path_prefix and not str(attrs.get("path", "")).startswith(path_prefix):
            continue
        result.append({key: attrs.get(key) for key in (
            "id", "path", "source_kind", "version", "rule_count",
            "depends_on", "ambiguous_dependency_ids", "generated_from",
            "freshness", "unresolved_generated_tables", "id_resolution",
            "duplicate_manifest_paths",
        )})
    return sorted(result, key=lambda item: (item["id"], item["path"]))


def list_data_test_evidence(memory: Any, *, path_prefix: str = "") -> list[dict[str, Any]]:
    """Summarize literal and declared data/test links as advisory evidence.

    This query deliberately does not claim completeness. Only project-owned
    test-coverage configuration can opt the planner into a bounded subset.
    """
    nodes = memory.nodes
    by_id = {int(nid): node for nid, node in nodes.items()}
    test_files: dict[int, str] = {}
    scenarios: dict[int, dict[str, Any]] = {}
    seeds: dict[int, dict[str, Any]] = {}
    tables: dict[int, dict[str, Any]] = {}
    for nid, node in by_id.items():
        attrs = node.get("attrs", {})
        kind = node.get("type")
        if kind == "Test":
            test_files[nid] = str(attrs.get("file") or "")
        elif kind == "Scenario":
            scenarios[nid] = {"path": str(attrs.get("path") or ""), "tests": set(),
                              "evidence": set(), "seed_ids": set()}
        elif kind == "BigvilleSeedManifest":
            seeds[nid] = {"path": str(attrs.get("path") or ""), "id": str(attrs.get("id") or ""),
                          "tests": set(), "evidence": set(),
                          "ambiguity": str(attrs.get("id_resolution") or "unknown")}
        elif kind == "PackTable":
            tables[nid] = {"path": str(attrs.get("path") or ""), "tests": set(),
                           "evidence": set()}
    scenario_seed_edges: list[tuple[int, int, str]] = []
    generated_table_edges: list[tuple[int, int]] = []
    for edge in memory.edges:
        src, tgt = int(edge["src"]), int(edge["tgt"])
        relation = str(edge.get("type") or "")
        if relation == "mentions_scenario" and src in test_files and tgt in scenarios:
            scenarios[tgt]["tests"].add(test_files[src])
            scenarios[tgt]["evidence"].add("literal_scenario_mention")
        elif relation == "mentions_seed_manifest" and src in test_files and tgt in seeds:
            seeds[tgt]["tests"].add(test_files[src])
            seeds[tgt]["evidence"].add("literal_seed_mention")
        elif relation == "mentions_pack_table" and src in test_files and tgt in tables:
            tables[tgt]["tests"].add(test_files[src])
            tables[tgt]["evidence"].add("literal_pack_table_mention")
        elif relation == "references_seed_manifest" and src in scenarios and tgt in seeds:
            resolution = str((edge.get("attrs") or {}).get("resolution") or "unknown")
            scenario_seed_edges.append((src, tgt, resolution))
            scenarios[src]["seed_ids"].add(seeds[tgt]["id"])
        elif relation == "generated_from_table" and src in seeds and tgt in tables:
            generated_table_edges.append((src, tgt))

    for scenario_id, seed_id, _resolution in scenario_seed_edges:
        seeds[seed_id]["tests"].update(scenarios[scenario_id]["tests"])
        if scenarios[scenario_id]["tests"]:
            seeds[seed_id]["evidence"].add("scenario_reference_and_literal_scenario_mention")
    for seed_id, table_id in generated_table_edges:
        tables[table_id]["tests"].update(seeds[seed_id]["tests"])
        if seeds[seed_id]["tests"]:
            tables[table_id]["evidence"].add("declared_generation_provenance_unverified")

    rows: list[dict[str, Any]] = []
    for scenario in scenarios.values():
        if scenario["path"].startswith(path_prefix):
            rows.append({"subject_type": "scenario", "path": scenario["path"],
                         "test_files": sorted(scenario["tests"]),
                         "evidence": sorted(scenario["evidence"]),
                         "ambiguity": "", "completeness": "advisory_only"})
    for seed in seeds.values():
        if seed["path"].startswith(path_prefix):
            rows.append({"subject_type": "seed_manifest", "path": seed["path"],
                         "test_files": sorted(seed["tests"]),
                         "evidence": sorted(seed["evidence"]),
                         "ambiguity": seed["ambiguity"],
                         "completeness": "advisory_only"})
    for table in tables.values():
        if table["path"].startswith(path_prefix):
            rows.append({"subject_type": "pack_table", "path": table["path"],
                         "test_files": sorted(table["tests"]),
                         "evidence": sorted(table["evidence"]),
                         "ambiguity": "generated provenance is not hash-verified",
                         "completeness": "advisory_only"})
    return sorted(rows, key=lambda row: (row["path"], row["subject_type"]))
