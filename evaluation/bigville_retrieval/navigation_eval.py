"""Assert intended Bigville adapter navigation links in an Ozagia snapshot.

This checks graph semantics directly; it does not import or execute Bigville.
The snapshot should be built with ``bigville_adapter:register`` enabled.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


def evaluate(graph_path: str | Path) -> dict[str, Any]:
    """Validate representative scenario, generated-seed, and route links."""
    path = Path(graph_path)
    try:
        graph = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read graph snapshot {path}: {exc}") from exc
    nodes = graph.get("nodes")
    edges = graph.get("edges")
    if not isinstance(nodes, list) or not isinstance(edges, list):
        raise ValueError(f"{path}: expected graph 'nodes' and 'edges' arrays")

    by_type: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_id = {}
    for node in nodes:
        if not isinstance(node, dict) or not isinstance(node.get("id"), int):
            continue
        by_id[node["id"]] = node
        by_type[str(node.get("type") or "")].append(node)
    edge_types = Counter(str(edge.get("type") or "") for edge in edges
                         if isinstance(edge, dict))

    manifests = by_type["BigvilleSeedManifest"]
    seed_ids = Counter(str(node.get("attrs", {}).get("id") or "")
                        for node in manifests)
    duplicate_ids = sorted(seed_id for seed_id, count in seed_ids.items()
                           if count > 1)
    generated = [node for node in manifests
                 if isinstance(node.get("attrs", {}).get("generated_from"), dict)]
    ambiguous_scenarios = [node for node in by_type["Scenario"]
                           if node.get("attrs", {}).get("ambiguous_seed_ids")]

    # Audit every declared generated table relationship: every table is either
    # linked to a real PackTable or reported as unresolved, and edges preserve
    # the recorded hash. This does not validate the hash against current bytes.
    generated_table_audit = []
    for seed in generated:
        attrs = seed.get("attrs", {})
        declared = attrs.get("generated_from") or {}
        pack = str(declared.get("pack") or "")
        tables = declared.get("tables")
        expected = {(pack, str(table)) for table in tables
                    if isinstance(table, str)} if isinstance(tables, list) else set()
        source_edges = [edge for edge in edges
                        if isinstance(edge, dict)
                        and edge.get("src") == seed["id"]
                        and edge.get("type") == "generated_from_table"]
        linked = {
            (str((by_id.get(edge.get("tgt")) or {}).get("attrs", {}).get("pack") or ""),
             str((by_id.get(edge.get("tgt")) or {}).get("attrs", {}).get("name") or ""))
            for edge in source_edges
        }
        unresolved = {(pack, str(table))
                      for table in attrs.get("unresolved_generated_tables", [])}
        _assert(linked <= expected,
                f"{attrs.get('path')}: generated edges must match declared tables")
        _assert(linked.isdisjoint(unresolved),
                f"{attrs.get('path')}: a table cannot be both linked and unresolved")
        _assert(linked | unresolved == expected,
                f"{attrs.get('path')}: every declared table must be linked or unresolved")
        _assert(all(edge.get("attrs", {}).get("recorded_sha256")
                    == str(declared.get("sha256") or "") for edge in source_edges),
                f"{attrs.get('path')}: generated table edges must retain the declared hash")
        generated_table_audit.append({
            "manifest_path": attrs.get("path"),
            "linked_tables": sorted(name for _pack, name in linked),
            "unresolved_tables": sorted(name for _pack, name in unresolved),
        })

    # A scenario's bare seed ID must preserve both path-distinct manifests and
    # label the reference as ambiguous instead of silently selecting one.
    scenario = _one(by_type["Scenario"],
                    lambda node: node.get("attrs", {}).get("id") == "house_argument",
                    "house_argument scenario")
    scenario_attrs = scenario.get("attrs", {})
    scenario_seed_paths = scenario_attrs.get("seed_manifest_candidates", {}).get(
        "conversation", [])
    expected_paths = ["bigville/seeds/conversation.json",
                      "seeds/conversation.json"]
    _assert(sorted(scenario_seed_paths) == sorted(expected_paths),
            "house_argument conversation reference should retain both copies")
    scenario_edges = [edge for edge in edges
                      if isinstance(edge, dict)
                      and edge.get("src") == scenario["id"]
                      and edge.get("type") == "references_seed_manifest"
                      and edge.get("attrs", {}).get("seed_id") == "conversation"]
    scenario_target_paths = sorted(
        str((by_id.get(edge.get("tgt")) or {}).get("attrs", {}).get("path"))
        for edge in scenario_edges)
    _assert(scenario_attrs.get("ambiguous_seed_ids")
            and "conversation" in scenario_attrs["ambiguous_seed_ids"],
            "house_argument must mark the duplicate seed ID ambiguous")
    _assert(scenario_target_paths == sorted(expected_paths),
            "ambiguous scenario edges must point to both real manifest paths")
    _assert(all(edge.get("attrs", {}).get("resolution") == "ambiguous_id_only"
                for edge in scenario_edges),
            "scenario duplicate-ID edges must identify ambiguity")

    # Repeat the ambiguity assertion on a second scenario with two seed IDs.
    # This guards against the first scenario being a one-off fixture shape.
    second_scenario = _one(
        by_type["Scenario"],
        lambda node: node.get("attrs", {}).get("id") == "monopoly_four_players",
        "monopoly_four_players scenario")
    second_attrs = second_scenario.get("attrs", {})
    second_seed_ids = ("conversation", "conversation_reply")
    second_expected_paths = {
        seed_id: [f"bigville/seeds/{seed_id}.json", f"seeds/{seed_id}.json"]
        for seed_id in second_seed_ids
    }
    second_edges = [edge for edge in edges
                    if isinstance(edge, dict)
                    and edge.get("src") == second_scenario["id"]
                    and edge.get("type") == "references_seed_manifest"]
    second_targets = {
        seed_id: sorted(
            str((by_id.get(edge.get("tgt")) or {}).get("attrs", {}).get("path"))
            for edge in second_edges
            if edge.get("attrs", {}).get("seed_id") == seed_id)
        for seed_id in second_seed_ids
    }
    _assert(all(sorted(second_attrs.get("seed_manifest_candidates", {}).get(seed_id, []))
                == sorted(paths)
                for seed_id, paths in second_expected_paths.items()),
            "monopoly scenario should retain both paths for two seed IDs")
    _assert(all(seed_id in second_attrs.get("ambiguous_seed_ids", [])
                for seed_id in second_seed_ids),
            "monopoly scenario should mark both seed IDs ambiguous")
    _assert(all(second_targets[seed_id] == sorted(paths)
                for seed_id, paths in second_expected_paths.items()),
            "monopoly scenario edges should point to all four manifest paths")
    _assert(all(edge.get("attrs", {}).get("resolution") == "ambiguous_id_only"
                for edge in second_edges),
            "monopoly duplicate-ID edges should identify ambiguity")

    # A generated seed's declaration should link to its actual pack table and
    # retain the declared source hash. This is provenance, not a regeneration
    # or current-bytes hash verification.
    generated_seed = _one(
        generated,
        lambda node: node.get("attrs", {}).get("path") ==
        "bigville/seeds/gen_charter_bigville_council_charter.json",
        "generated council charter manifest")
    generated_attrs = generated_seed["attrs"]
    declared = generated_attrs.get("generated_from") or {}
    _assert(generated_attrs.get("freshness") == "unverified_declared_generation",
            "generated source hash must not be represented as verified freshness")
    table_edges = [edge for edge in edges
                   if isinstance(edge, dict)
                   and edge.get("src") == generated_seed["id"]
                   and edge.get("type") == "generated_from_table"]
    table_matches = [edge for edge in table_edges
                     if (by_id.get(edge.get("tgt")) or {}).get("attrs", {}).get("name")
                     in declared.get("tables", [])
                     and (by_id.get(edge.get("tgt")) or {}).get("attrs", {}).get("pack")
                     == declared.get("pack")]
    _assert(table_matches, "generated charter should link to its declared pack table")
    _assert(any(edge.get("attrs", {}).get("recorded_sha256") == declared.get("sha256")
                for edge in table_matches),
            "generated table edge should preserve the manifest's declared hash")

    # Check a different generated seed and a table in another pack domain.
    terrain_seed = _one(
        generated,
        lambda node: node.get("attrs", {}).get("path") ==
        "bigville/seeds/bigville_terrain_movement.json",
        "generated terrain movement manifest")
    terrain_declared = terrain_seed.get("attrs", {}).get("generated_from") or {}
    terrain_edges = [edge for edge in edges
                     if isinstance(edge, dict)
                     and edge.get("src") == terrain_seed["id"]
                     and edge.get("type") == "generated_from_table"]
    terrain_matches = [edge for edge in terrain_edges
                       if (by_id.get(edge.get("tgt")) or {}).get("attrs", {}).get("name")
                       in terrain_declared.get("tables", [])
                       and (by_id.get(edge.get("tgt")) or {}).get("attrs", {}).get("pack")
                       == terrain_declared.get("pack")]
    _assert(terrain_matches,
            "terrain movement seed should link to its declared pack table")
    _assert(any(edge.get("attrs", {}).get("recorded_sha256")
                == terrain_declared.get("sha256") for edge in terrain_matches),
            "terrain movement table edge should retain the declared hash")

    # Dependency IDs are also path-ambiguous. Keep both target manifests and
    # label the resolution honestly rather than selecting one copy.
    dependency_seed = _one(
        manifests,
        lambda node: node.get("attrs", {}).get("path") ==
        "bigville/seeds/bigville_bell_schedule.json",
        "packaged bell schedule manifest")
    dependency_id = "bigville_bells"
    dependency_paths = ["bigville/seeds/bigville_bells.json",
                        "seeds/bigville_bells.json"]
    dependency_attrs = dependency_seed.get("attrs", {})
    dependency_edges = [edge for edge in edges
                        if isinstance(edge, dict)
                        and edge.get("src") == dependency_seed["id"]
                        and edge.get("type") == "declares_dependency_candidate"
                        and edge.get("attrs", {}).get("seed_id") == dependency_id]
    dependency_target_paths = sorted(
        str((by_id.get(edge.get("tgt")) or {}).get("attrs", {}).get("path"))
        for edge in dependency_edges)
    _assert(dependency_id in dependency_attrs.get("depends_on", []),
            "bell schedule should declare the bell knowledge dependency")
    _assert(dependency_id in dependency_attrs.get("ambiguous_dependency_ids", []),
            "duplicate bell knowledge IDs should remain explicitly ambiguous")
    _assert(dependency_target_paths == sorted(dependency_paths),
            "dependency edges should retain both path-distinct target manifests")
    _assert(all(edge.get("attrs", {}).get("resolution") == "ambiguous_id_only"
                for edge in dependency_edges),
            "dependency candidate edges should identify ID-only ambiguity")

    # Browser evidence is accepted only when the same method/path has explicit
    # server and browser source files.
    route = _one(
        by_type["BigvilleRoute"],
        lambda node: (node.get("attrs", {}).get("method") == "GET"
                      and node.get("attrs", {}).get("path") ==
                      "/api/management/v1/backend-profiles"),
        "backend-profiles route")
    route_attrs = route.get("attrs", {})
    _assert(route_attrs.get("evidence") == "explicit_handler_and_client",
            "route should join explicit handler and client evidence")
    _assert("bigville/management/http.py" in route_attrs.get("server_files", []),
            "route should cite the server handler source")
    _assert("bigville/management/static/management.js" in route_attrs.get(
        "client_files", []), "route should cite the browser request source")
    route_edges = [edge for edge in edges
                   if isinstance(edge, dict) and edge.get("tgt") == route["id"]]
    _assert(any(edge.get("type") == "handles_route" for edge in route_edges),
            "route should have a source-to-handler graph edge")
    _assert(any(edge.get("type") == "requests_route" for edge in route_edges),
            "route should have a browser-request graph edge")

    checks = {
        "scenario_duplicate_id_preserved_as_ambiguous": {
            "scenario": scenario_attrs.get("id"), "seed_id": "conversation",
            "candidate_paths": scenario_target_paths,
            "resolution": "ambiguous_id_only",
        },
        "second_scenario_resolves_two_ids_ambiguously": {
            "scenario": second_attrs.get("id"),
            "seed_ids": list(second_seed_ids),
            "candidate_paths": second_targets,
            "resolution": "ambiguous_id_only",
        },
        "generated_manifest_table_provenance": {
            "manifest_path": generated_attrs.get("path"),
            "manifest_id": generated_attrs.get("id"),
            "pack": declared.get("pack"), "tables": declared.get("tables"),
            "declared_sha256": declared.get("sha256"),
            "freshness": generated_attrs.get("freshness"),
            "linked_table_paths": sorted({
                str((by_id.get(edge.get("tgt")) or {}).get("attrs", {}).get("path"))
                for edge in table_matches}),
        },
        "second_generated_manifest_table_provenance": {
            "manifest_path": terrain_seed.get("attrs", {}).get("path"),
            "pack": terrain_declared.get("pack"),
            "tables": terrain_declared.get("tables"),
            "declared_sha256": terrain_declared.get("sha256"),
            "linked_table_paths": sorted({
                str((by_id.get(edge.get("tgt")) or {}).get("attrs", {}).get("path"))
                for edge in terrain_matches}),
        },
        "seed_dependency_duplicate_id_preserved_as_ambiguous": {
            "manifest_path": dependency_attrs.get("path"),
            "seed_id": dependency_id,
            "candidate_paths": dependency_target_paths,
            "resolution": "ambiguous_id_only",
        },
        "all_generated_table_declarations_accounted_for": {
            "manifest_count": len(generated),
            "resolved_table_references": sum(
                len(row["linked_tables"]) for row in generated_table_audit),
            "unresolved_table_references": sum(
                len(row["unresolved_tables"]) for row in generated_table_audit),
            "unresolved_examples": [row for row in generated_table_audit
                                    if row["unresolved_tables"]],
        },
        "browser_route_has_handler_and_client_evidence": {
            "method": route_attrs.get("method"), "path": route_attrs.get("path"),
            "grade": route_attrs.get("evidence"),
            "server_files": route_attrs.get("server_files"),
            "client_files": route_attrs.get("client_files"),
        },
    }
    return {
        "schema": "ozagia.bigville_navigation_eval.v1",
        "ok": True,
        "graph_path": str(path),
        "source_root": _repo_root(by_type["Repo"]),
        "head": _repo_head(by_type["Repo"]),
        "counts": {
            "file_nodes": sum(node.get("type") == "File" for node in nodes),
            "scenarios": len(by_type["Scenario"]),
            "ambiguous_scenarios": len(ambiguous_scenarios),
            "seed_manifests": len(manifests),
            "distinct_seed_ids": len(seed_ids),
            "duplicate_seed_ids": len(duplicate_ids),
            "generated_manifests": len(generated),
            "generated_table_edges": edge_types["generated_from_table"],
            "pack_tables": len(by_type["PackTable"]),
            "routes": len(by_type["BigvilleRoute"]),
        },
        "checks": checks,
        "limitations": [
            "A declared generated_from hash is recorded provenance; this runner does not regenerate manifests or certify current table bytes against it.",
            "Route checks cover explicit bounded route evidence, not every dynamic server route.",
            "Six representative graph paths are asserted; the counts describe this snapshot, not a general benchmark.",
        ],
    }


def _repo_head(repos: list[dict[str, Any]]) -> str | None:
    return str(repos[0].get("attrs", {}).get("head")) if repos else None


def _repo_root(repos: list[dict[str, Any]]) -> str | None:
    return str(repos[0].get("attrs", {}).get("root")) if repos else None


def _one(rows: list[dict[str, Any]], predicate: Any, what: str) -> dict[str, Any]:
    found = [row for row in rows if predicate(row)]
    if len(found) != 1:
        raise ValueError(f"expected one {what}, found {len(found)}")
    return found[0]


def _assert(condition: Any, message: str) -> None:
    if not condition:
        raise ValueError(f"adapter navigation assertion failed: {message}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--graph", required=True,
                        help="Ozagia graph snapshot built with the Bigville adapter")
    parser.add_argument("--out", help="write report JSON here instead of stdout")
    args = parser.parse_args(argv)
    try:
        report = evaluate(args.graph)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    rendered = json.dumps(report, indent=2) + "\n"
    if args.out:
        target = Path(args.out)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
