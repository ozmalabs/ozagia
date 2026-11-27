"""Repository-level relationships across federation members.

Federation membership answers which checkouts are in scope. This module adds
the relationships *between* those checkouts:

- explicit ``relationships`` and ``families`` declarations in the anchor's
  ``.ozagia/federation.json``;
- directed dependency edges rolled up from declared package manifests, using
  :mod:`ozagia.core.dependency_links` evidence;
- explicit ``ApiMapping`` nodes already in member snapshots, using
  :mod:`ozagia.core.api_relationships`; and
- explicit ``RepoBridge`` nodes emitted by adapters, validated by
  :mod:`ozagia.core.repo_bridges`.

It never infers a relationship from repository names, symbol names, search
results, dynamic imports or Git remotes, and it never writes to a member.
Dynamic imports become edges only when an adapter declares a ``RepoBridge``
with source evidence and both aliases are selected. Only dependency relations
(``depends_on``, ``dev_depends_on`` and declared package dependencies) are used
for cycle and reverse-dependency analysis; lineage, integration, API and
bridge edges remain descriptive.
"""
from __future__ import annotations

import heapq
import json
import re
from pathlib import Path
from typing import Any, Iterable, Mapping

from ozagia.config import state_path
from ozagia.core import api_relationships, repo_bridges

MAX_RELATIONSHIPS = 100
MAX_FAMILIES = 32
MAX_FAMILY_MEMBERS = 64
MAX_EVIDENCE = 10
MAX_TEXT = 500
MAX_RELATION = 80
MAX_PACKAGES_PER_EDGE = 20
MAX_REVERSE_PER_MEMBER = 64
MAX_BRIDGE_EDGES = 100
MAX_BRIDGE_UNPAIRED = 100
MAX_BRIDGE_ERRORS = 100
MAX_BRIDGE_EVIDENCE = 10
MAX_BRIDGE_PROVENANCE = 10

_FAMILY_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}\Z")
_DEPENDENCY_RELATIONS = frozenset({"depends_on", "dev_depends_on"})
_RELATION_KINDS = {
    "depends_on": "dependency",
    "dev_depends_on": "dependency",
    "forked_from": "lineage",
    "upstream_of": "lineage",
    "sibling_of": "lineage",
    "part_of": "grouping",
    "documents": "documentation",
    "documented_by": "documentation",
    "uses": "integration",
    "integrates_with": "integration",
    "plugin_for": "integration",
}
_STATUS_ORDER = ("mismatch", "unknown", "satisfied")


def _manifest(root: Path) -> tuple[dict[str, Any] | None, list[str]]:
    path = state_path(root, "federation.json")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, []
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return None, [f"cannot read manifest: {type(exc).__name__}: {exc}"]
    if (not isinstance(raw, dict) or raw.get("version") != 1
            or not isinstance(raw.get("links"), list)):
        return None, ["expected {version: 1, links: [...]} manifest"]
    return raw, []


def _bounded(value: Any, field: str, limit: int = MAX_TEXT) -> tuple[str | None, str | None]:
    if not isinstance(value, str) or not value.strip():
        return None, f"{field} must be a non-empty string"
    text = value.strip()
    if len(text) > limit:
        return None, f"{field} exceeds {limit} characters"
    return text, None


def _member_ref(value: Any, members: Mapping[str, dict[str, Any]],
                field: str) -> tuple[str | None, str | None]:
    if not isinstance(value, str) or not value:
        return None, f"{field} must be a non-empty member id"
    if value not in members:
        return None, f"{field} is not a selected member: {value}"
    return value, None


def _evidence(value: Any, field: str) -> tuple[list[str] | None, str | None]:
    if value is None:
        return [], None
    if not isinstance(value, list):
        return None, f"{field} must be an array of non-empty strings"
    if len(value) > MAX_EVIDENCE:
        return None, f"{field} has more than {MAX_EVIDENCE} entries"
    items: list[str] = []
    for index, item in enumerate(value):
        text, error = _bounded(item, f"{field}[{index}]")
        if error:
            return None, error
        items.append(text)
    return items, None


def _declared(root: Path, members: list[dict[str, Any]]
              ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str], bool]:
    """Read optional repository relationships and families from the manifest."""
    raw, errors = _manifest(root)
    if raw is None:
        return [], [], errors, False
    by_alias = {item["alias"]: item for item in members}
    relationships: list[dict[str, Any]] = []
    families: list[dict[str, Any]] = []
    truncated = False

    values = raw.get("relationships", [])
    if values is None:
        values = []
    if not isinstance(values, list):
        errors.append("relationships must be an array of objects")
        values = []
    if len(values) > MAX_RELATIONSHIPS:
        truncated = True
        errors.append(f"relationships is limited to the first {MAX_RELATIONSHIPS} entries")
        values = values[:MAX_RELATIONSHIPS]
    seen: set[tuple[str, str, str]] = set()
    for index, value in enumerate(values):
        if not isinstance(value, dict):
            errors.append(f"relationships[{index}] must be an object")
            continue
        extra = set(value) - {"source", "target", "relation", "provenance", "evidence"}
        if extra:
            errors.append(f"relationships[{index}] has unknown fields: "
                          + ", ".join(sorted(extra)))
            continue
        source, error = _member_ref(value.get("source"), by_alias,
                                    f"relationships[{index}].source")
        if error:
            errors.append(error)
            continue
        target, error = _member_ref(value.get("target"), by_alias,
                                    f"relationships[{index}].target")
        if error:
            errors.append(error)
            continue
        relation, error = _bounded(value.get("relation"),
                                   f"relationships[{index}].relation", MAX_RELATION)
        if error:
            errors.append(error)
            continue
        provenance, error = _bounded(value.get("provenance"),
                                     f"relationships[{index}].provenance")
        if error:
            errors.append(error)
            continue
        evidence, error = _evidence(value.get("evidence"),
                                    f"relationships[{index}].evidence")
        if error:
            errors.append(error)
            continue
        if source == target:
            errors.append(f"relationships[{index}] names the same member: {source}")
            continue
        key = (source, target, relation)
        if key in seen:
            errors.append(f"relationships[{index}] duplicates an earlier relationship")
            continue
        seen.add(key)
        relationships.append({
            "source": source, "target": target, "relation": relation,
            "kind": _RELATION_KINDS.get(relation, "opaque"),
            "origin": "declared", "provenance": provenance,
            "evidence": evidence,
        })

    values = raw.get("families", [])
    if values is None:
        values = []
    if not isinstance(values, list):
        errors.append("families must be an array of objects")
        values = []
    if len(values) > MAX_FAMILIES:
        truncated = True
        errors.append(f"families is limited to the first {MAX_FAMILIES} entries")
        values = values[:MAX_FAMILIES]
    family_ids: set[str] = set()
    for index, value in enumerate(values):
        if not isinstance(value, dict):
            errors.append(f"families[{index}] must be an object")
            continue
        extra = set(value) - {"id", "label", "members", "provenance"}
        if extra:
            errors.append(f"families[{index}] has unknown fields: "
                          + ", ".join(sorted(extra)))
            continue
        family_id, error = _bounded(value.get("id"), f"families[{index}].id", 64)
        if error or not _FAMILY_ID.fullmatch(family_id or ""):
            errors.append(f"families[{index}].id must be 1-64 characters, start "
                          "with a lowercase letter or digit, and use only "
                          "[a-z0-9._-]")
            continue
        if family_id in family_ids:
            errors.append(f"families[{index}].id duplicates another family id: {family_id}")
            continue
        family_ids.add(family_id)
        provenance, error = _bounded(value.get("provenance"),
                                     f"families[{index}].provenance")
        if error:
            errors.append(error)
            continue
        label = family_id
        if "label" in value:
            label, error = _bounded(value["label"], f"families[{index}].label", 200)
            if error:
                errors.append(error)
                continue
        declared_members = value.get("members")
        if not isinstance(declared_members, list) or not declared_members:
            errors.append(f"families[{index}].members must be a non-empty array")
            continue
        if len(declared_members) > MAX_FAMILY_MEMBERS:
            truncated = True
            errors.append(f"families[{index}].members is limited to "
                          f"{MAX_FAMILY_MEMBERS} entries")
            declared_members = declared_members[:MAX_FAMILY_MEMBERS]
        resolved: list[str] = []
        unresolved: list[str] = []
        seen_members: set[str] = set()
        for position, item in enumerate(declared_members):
            member, error = _member_ref(item, by_alias,
                                        f"families[{index}].members[{position}]")
            if error:
                errors.append(error)
                if isinstance(item, str) and item:
                    unresolved.append(item)
                continue
            if member in seen_members:
                errors.append(f"families[{index}].members[{position}] duplicates "
                              f"a member: {member}")
                continue
            seen_members.add(member)
            resolved.append(member)
        if not resolved:
            errors.append(f"families[{index}] has no selected members")
            continue
        families.append({"id": family_id, "label": label, "provenance": provenance,
                         "members": resolved, "unresolved": unresolved})
    return relationships, families, errors, truncated


def family_index(root: str | Path, members: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Return declared family membership by member alias.

    This is the lightweight lookup used by federated search: it reads only the
    anchor's ``families`` declarations and does not run dependency, API or
    adapter-bridge analysis. ``members`` are the records returned by
    ``federation._members``. The ``by_member`` map includes every selected
    member alias, with family ids in declaration order; members in no family
    map to an empty list.

    Invalid declarations never raise. Valid families are kept and reported in
    ``families``, errors are normalized into ``errors``, and ``state`` is:

    - ``absent``: no family is declared;
    - ``ok``: every declaration resolved;
    - ``partial``: some declarations failed but valid families remain; or
    - ``invalid``: errors left no usable family.
    """
    selected = Path(root).expanduser().resolve()
    records = [item for item in members if isinstance(item, dict)]
    by_member: dict[str, list[str]] = {}
    for item in records:
        alias = item.get("alias")
        if isinstance(alias, str):
            by_member.setdefault(alias, [])
    try:
        _, declared, errors, truncated = _declared(selected, records)
    except Exception as exc:  # Defensive: a query must never fail on bad declarations.
        return {"state": "invalid", "by_member": by_member, "families": [],
                "errors": [f"cannot resolve families: {type(exc).__name__}: {exc}"],
                "truncated": False}
    for family in declared:
        for member_id in family["members"]:
            by_member.setdefault(member_id, []).append(family["id"])
    if errors:
        state = "partial" if declared else "invalid"
    else:
        state = "ok" if declared else "absent"
    return {"state": state, "by_member": by_member, "families": declared,
            "errors": errors, "truncated": truncated}


def _aggregate_status(packages: Iterable[Mapping[str, Any]]) -> str:
    statuses = {str(item.get("status")) for item in packages}
    for status in _STATUS_ORDER:
        if status in statuses:
            return status
    return "unknown"


def _derived_edges(report: Mapping[str, Any] | None,
                   members: Mapping[str, dict[str, Any]]
                   ) -> tuple[list[dict[str, Any]], list[str]]:
    """Collapse package-link rows into one edge per consumer/provider pair."""
    if not isinstance(report, Mapping):
        return [], []
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for link in report.get("links") or []:
        if not isinstance(link, Mapping):
            continue
        source, target = link.get("consumer"), link.get("provider")
        if not isinstance(source, str) or not isinstance(target, str):
            continue
        if source not in members or target not in members or source == target:
            continue
        groups.setdefault((source, target), []).append(dict(link))
    edges: list[dict[str, Any]] = []
    notes: list[str] = []
    for (source, target), links in sorted(groups.items()):
        packages = [{
            "package": link.get("package"),
            "ecosystem": link.get("ecosystem"),
            "requirement": link.get("requirement"),
            "provided_version": link.get("provided_version"),
            "status": link.get("status"),
            "source_manifest": link.get("source_manifest"),
            "source_line": link.get("source_line"),
            "provider_manifest": link.get("provider_manifest"),
            "provider_line": link.get("provider_line"),
            "optional": bool(link.get("optional")),
            "group": link.get("group"),
        } for link in sorted(links, key=lambda item: (
            str(item.get("ecosystem")), str(item.get("package")),
            str(item.get("source_manifest"))))]
        packages_truncated = len(packages) > MAX_PACKAGES_PER_EDGE
        if packages_truncated:
            notes.append(f"{source} -> {target}: package list truncated")
        edges.append({
            "source": source, "target": target, "relation": "depends_on",
            "kind": "dependency", "origin": "package_manifest",
            "status": _aggregate_status(links),
            "provenance": "declared_dependency",
            "packages": packages[:MAX_PACKAGES_PER_EDGE],
            "package_count": len(packages),
            "packages_truncated": packages_truncated,
            "groups": sorted({str(link["group"]) for link in links
                              if link.get("group")}),
            "source_manifests": sorted({str(link["source_manifest"])
                                        for link in links
                                        if link.get("source_manifest")}),
            "provider_manifests": sorted({str(link["provider_manifest"])
                                          for link in links
                                          if link.get("provider_manifest")}),
        })
    return edges, notes


def _bridge_evidence_key(item: Any) -> tuple[Any, ...]:
    if isinstance(item, Mapping):
        return ("location", str(item.get("path")), item.get("line") or 0)
    return ("text", str(item))


def _bridge_section(members: Iterable[Mapping[str, Any]],
                    by_alias: Mapping[str, dict[str, Any]]) -> dict[str, Any]:
    """Roll up explicit ``RepoBridge`` nodes into bounded integration edges.

    A bridge node is advisory until both endpoints are selected member aliases.
    Unknown or unselected endpoints are reported as unpaired; they are never
    turned into a member, a guessed alias or an edge.

    Merged provenance and evidence are sorted lexicographically before the
    per-edge cap is applied. Sorting makes the retained entries independent of
    snapshot, member and declaration order; truncation keeps the first
    ``MAX_BRIDGE_PROVENANCE``/``MAX_BRIDGE_EVIDENCE`` entries of that sorted
    order and is reported in ``notes`` and ``truncated``.
    """
    errors: list[str] = []
    notes: list[str] = []
    unpaired: list[dict[str, Any]] = []
    groups: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    nodes_seen = 0
    truncated = False
    for item in sorted(members, key=lambda record: str(record.get("alias"))):
        member = str(item.get("alias") or "repo")
        snapshot = item.get("snapshot")
        if not isinstance(snapshot, Mapping):
            continue
        nodes = snapshot.get("nodes")
        if not isinstance(nodes, list):
            continue
        for node in nodes:
            if not isinstance(node, Mapping) or node.get("type") != repo_bridges.BRIDGE_TYPE:
                continue
            nodes_seen += 1
            raw_attrs = node.get("attrs")
            attrs, error = repo_bridges.normalize_bridge_attrs(
                raw_attrs if isinstance(raw_attrs, Mapping) else {})
            if error:
                label = node.get("key")
                if label in (None, ""):
                    label = node.get("id", "?")
                errors.append(f"{member}:repo_bridge:{label}:{error}")
                continue
            assert attrs is not None
            source, target = attrs["source_repo"], attrs["target_repo"]
            missing = []
            if source not in by_alias:
                missing.append("source")
            if target not in by_alias:
                missing.append("target")
            if missing:
                record = {
                    "member": member,
                    "source": source,
                    "target": target,
                    "relation": attrs["relation"],
                    "reason": "_and_".join(missing) + "_not_selected",
                    "provenance": attrs["provenance"],
                    "evidence": attrs["evidence"],
                }
                if "symbol" in attrs:
                    record["symbol"] = attrs["symbol"]
                unpaired.append(record)
                continue
            key = (source, target, attrs["relation"],
                   str(attrs.get("symbol") or ""))
            group = groups.get(key)
            if group is None:
                group = {
                    "source": source, "target": target,
                    "relation": attrs["relation"], "kind": "integration",
                    "origin": "adapter_bridge", "provenance": set(),
                    "evidence": {}, "members": set(),
                }
                if "symbol" in attrs:
                    group["symbol"] = attrs["symbol"]
                groups[key] = group
            group["provenance"].add(attrs["provenance"])
            for entry in attrs["evidence"]:
                group["evidence"][_bridge_evidence_key(entry)] = entry
            group["members"].add(member)

    edges: list[dict[str, Any]] = []
    for key in sorted(groups):
        group = groups[key]
        # Sort before capping. The retained entries are the lexicographically
        # first N, independent of member, snapshot and declaration order.
        provenance = sorted(group["provenance"])
        if len(provenance) > MAX_BRIDGE_PROVENANCE:
            truncated = True
            notes.append(f"{group['source']} -> {group['target']}: "
                         f"provenance list truncated from {len(provenance)} "
                         f"to {MAX_BRIDGE_PROVENANCE} entries (sorted)")
            provenance = provenance[:MAX_BRIDGE_PROVENANCE]
        evidence_keys = sorted(group["evidence"])
        if len(evidence_keys) > MAX_BRIDGE_EVIDENCE:
            truncated = True
            notes.append(f"{group['source']} -> {group['target']}: "
                         f"evidence list truncated from {len(evidence_keys)} "
                         f"to {MAX_BRIDGE_EVIDENCE} entries (sorted)")
            evidence_keys = evidence_keys[:MAX_BRIDGE_EVIDENCE]
        edge = {
            "source": group["source"], "target": group["target"],
            "relation": group["relation"], "kind": "integration",
            "origin": "adapter_bridge", "explicit": True,
            "provenance": provenance,
            "evidence": [group["evidence"][item] for item in evidence_keys],
            "members": sorted(group["members"]),
        }
        if "symbol" in group:
            edge["symbol"] = group["symbol"]
        edges.append(edge)
    if len(edges) > MAX_BRIDGE_EDGES:
        truncated = True
        notes.append(f"adapter bridge edges are limited to the first "
                     f"{MAX_BRIDGE_EDGES}")
        edges = edges[:MAX_BRIDGE_EDGES]
    unpaired.sort(key=lambda item: (str(item["source"]), str(item["target"]),
                                    str(item["relation"]), str(item["member"]),
                                    str(item["reason"])))
    if len(unpaired) > MAX_BRIDGE_UNPAIRED:
        truncated = True
        notes.append(f"unpaired adapter bridges are limited to the first "
                     f"{MAX_BRIDGE_UNPAIRED}")
        unpaired = unpaired[:MAX_BRIDGE_UNPAIRED]
    if len(errors) > MAX_BRIDGE_ERRORS:
        truncated = True
        errors = errors[:MAX_BRIDGE_ERRORS - 1]
        errors.append(f"adapter bridge errors are limited to the first "
                      f"{MAX_BRIDGE_ERRORS}")
    if not nodes_seen:
        state = "absent"
    elif errors or unpaired or truncated:
        state = "partial"
    else:
        state = "ok"
    return {
        "state": state,
        "edges": edges,
        "unpaired": unpaired,
        "errors": errors,
        "notes": notes,
        "truncated": truncated,
        "counts": {"nodes": nodes_seen, "edges": len(edges),
                   "unpaired": len(unpaired), "errors": len(errors)},
        "note": ("Adapter-declared `RepoBridge` nodes only. An edge requires "
                 "`explicit: true`, non-empty provenance and both aliases "
                 "among the selected members; no dynamic import, module name, "
                 "repository name or runtime call is inferred."),
    }


def _dependency_pairs(edges: Iterable[Mapping[str, Any]]
                      ) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for edge in edges:
        if edge.get("kind") != "dependency":
            continue
        if edge.get("origin") == "declared" and edge.get("relation") not in _DEPENDENCY_RELATIONS:
            continue
        source, target = str(edge["source"]), str(edge["target"])
        item = grouped.setdefault((source, target), {
            "source": source, "target": target, "origins": set(),
            "relations": set(), "statuses": set(), "provenance": set(),
        })
        item["origins"].add(str(edge.get("origin")))
        item["relations"].add(str(edge.get("relation")))
        if edge.get("status"):
            item["statuses"].add(str(edge["status"]))
        if edge.get("provenance"):
            item["provenance"].add(str(edge["provenance"]))
    result: list[dict[str, Any]] = []
    for item in sorted(grouped.values(), key=lambda value: (value["source"],
                                                            value["target"])):
        statuses = item["statuses"]
        if "mismatch" in statuses:
            status = "mismatch"
        elif "unknown" in statuses:
            status = "unknown"
        elif "satisfied" in statuses:
            status = "satisfied"
        elif statuses:
            status = sorted(statuses)[0]
        else:
            status = "declared"
        result.append({
            "source": item["source"], "target": item["target"],
            "origins": sorted(item["origins"]),
            "relations": sorted(item["relations"]),
            "status": status,
            "provenance": sorted(item["provenance"]),
        })
    return result


def _finish_order(nodes: list[str], adjacency: Mapping[str, set[str]]) -> list[str]:
    seen: set[str] = set()
    order: list[str] = []
    for start in nodes:
        if start in seen:
            continue
        stack: list[tuple[str, bool]] = [(start, False)]
        while stack:
            node, done = stack.pop()
            if done:
                order.append(node)
                continue
            if node in seen:
                continue
            seen.add(node)
            stack.append((node, True))
            for target in sorted(adjacency.get(node, ())):
                if target not in seen:
                    stack.append((target, False))
    return order


def _components(nodes: list[str], adjacency: Mapping[str, set[str]],
                reverse: Mapping[str, set[str]]
                ) -> tuple[list[list[str]], list[str], dict[str, int]]:
    """Return cycles (strongly connected components), topological order and levels."""
    order = _finish_order(nodes, adjacency)
    assigned: set[str] = set()
    components: list[list[str]] = []
    for start in reversed(order):
        if start in assigned:
            continue
        component: list[str] = []
        stack = [start]
        while stack:
            node = stack.pop()
            if node in assigned:
                continue
            assigned.add(node)
            component.append(node)
            stack.extend(sorted(reverse.get(node, ())))
        components.append(sorted(component))
    cycles = sorted((component for component in components if len(component) > 1),
                    key=lambda component: component[0])
    if cycles:
        return cycles, [], {}
    indegree = {node: 0 for node in nodes}
    for source in nodes:
        for target in adjacency.get(source, ()):
            indegree[target] = indegree.get(target, 0) + 1
    ready = [node for node in nodes if indegree[node] == 0]
    heapq.heapify(ready)
    topological: list[str] = []
    levels = {node: 0 for node in nodes}
    while ready:
        node = heapq.heappop(ready)
        topological.append(node)
        for target in sorted(adjacency.get(node, ())):
            levels[target] = max(levels[target], levels[node] + 1)
            indegree[target] -= 1
            if indegree[target] == 0:
                heapq.heappush(ready, target)
    return [], topological, levels


def analyze(root: str | Path, members: Iterable[dict[str, Any]],
            dependency_report: Mapping[str, Any] | None = None,
            member_states: Mapping[str, str] | None = None,
            api_report: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Build a repository-level relationship report for selected members.

    ``members`` are the records returned by ``federation._members``. The
    optional ``dependency_report`` is :func:`ozagia.core.dependency_links.analyze`
    output; when absent, only declared relationships and families are reported.
    ``member_states`` lets the caller reuse an existing freshness evaluation.
    ``api_report`` is :func:`ozagia.core.api_relationships.analyze` output; its
    explicit API mappings are included as repository integration edges.
    Explicit ``RepoBridge`` nodes already in member snapshots are included as
    adapter-declared integration edges when both aliases are selected.
    """
    records = list(members)
    selected = Path(root).expanduser().resolve()
    by_alias = {item["alias"]: item for item in records}
    states = dict(member_states or {})
    if any(item["alias"] not in states for item in records):
        from ozagia.core import federation
        for item in records:
            if item["alias"] not in states:
                states[item["alias"]] = federation._freshness(item)[0]
    relationships, families, errors, truncated = _declared(selected, records)
    derived, notes = _derived_edges(dependency_report, by_alias)
    api = (dict(api_report) if api_report is not None else {
        "state": "absent", "source_order": [], "references": [],
        "mappings": [], "edges": [], "unpaired": [], "errors": [],
        "truncated": False, "counts": {},
    })
    api_edges = [edge for edge in (api.get("edges") or [])
                 if isinstance(edge, Mapping)]
    bridges = _bridge_section(records, by_alias)
    bridge_edges = bridges["edges"]
    if bridges.get("truncated"):
        truncated = True
    notes.extend(str(note) for note in bridges.get("notes") or [])
    edges = relationships + derived + api_edges + bridge_edges
    dependencies = _dependency_pairs(edges)
    ids = [item["alias"] for item in records]
    adjacency: dict[str, set[str]] = {member_id: set() for member_id in ids}
    reverse: dict[str, set[str]] = {member_id: set() for member_id in ids}
    for pair in dependencies:
        if pair["source"] in adjacency and pair["target"] in adjacency:
            adjacency[pair["source"]].add(pair["target"])
            reverse[pair["target"]].add(pair["source"])
    cycles, order, levels = _components(ids, adjacency, reverse)
    family_by_member: dict[str, list[str]] = {member_id: [] for member_id in ids}
    for family in families:
        for member_id in family["members"]:
            family_by_member.setdefault(member_id, []).append(family["id"])
    reverse_dependencies = {
        member_id: sorted(reverse.get(member_id, ()))[:MAX_REVERSE_PER_MEMBER]
        for member_id in ids
    }
    if any(len(reverse.get(member_id, ())) > MAX_REVERSE_PER_MEMBER
           for member_id in ids):
        truncated = True
        notes.append(f"reverse dependency lists are limited to "
                     f"{MAX_REVERSE_PER_MEMBER} entries per member")
    public_members = [{
        "id": item["alias"], "name": item["label"], "root": str(item["root"]),
        "state": states.get(item["alias"]),
        "identity_source": item.get("identity_source", "derived"),
        "families": family_by_member.get(item["alias"], []),
    } for item in records]
    for public, item in zip(public_members, records):
        if item.get("identity_error"):
            public["identity_error"] = item["identity_error"]
        if item.get("reference"):
            public["reference"] = True
            public["pin"] = item.get("pin")
            public["sync"] = item.get("sync") or {
                "state": "not_checked", "updated": False}
    api_state = str(api.get("state") or "absent")
    api_partial = api_state == "partial"
    bridge_state = str(bridges.get("state") or "absent")
    if errors or truncated or api_partial or bridge_state == "partial":
        state = "partial"
    elif (not relationships and not families and not derived
          and api_state == "absent" and bridge_state == "absent"):
        state = "absent"
    else:
        state = "ok"
    counts = {
        "members": len(records),
        "families": len(families),
        "edges": len(edges),
        "declared_edges": len(relationships),
        "derived_edges": len(derived),
        "api_edges": len(api_edges),
        "bridge_edges": len(bridge_edges),
        "dependency_edges": len(dependencies),
        "api_references": len(api.get("references") or []),
        "api_mappings": len(api.get("mappings") or []),
        "cycles": len(cycles),
    }
    return {
        "ok": True,
        "root": str(selected),
        "state": state,
        "members": public_members,
        "families": families,
        "edges": edges,
        "dependencies": dependencies,
        "apis": api,
        "bridges": bridges,
        "cycles": cycles,
        "order": order,
        "levels": levels,
        "reverse_dependencies": reverse_dependencies,
        "errors": errors,
        "notes": notes,
        "truncated": truncated,
        "counts": counts,
        "note": ("Explicit `relationships`/`families` declarations, declared "
                 "package manifests, explicit API mappings, and explicit adapter "
                 "`RepoBridge` nodes only. Repository names, symbol names, "
                 "dynamic imports, Git remotes, matching API operation names, "
                 "and transitive package resolution are not used to infer "
                 "edges."),
    }


def check(root: str | Path | None = None,
          roots: Iterable[str | Path] | None = None) -> dict[str, Any]:
    """Read-only relationship report over selected federation members.

    Member ``state`` values are federation freshness states only. The
    ``repository_relations`` copy embedded by
    :func:`ozagia.core.cross_repo.check` can replace them with reference sync
    states (``sync_error``, ``sync_pending``, ``index_stale``,
    ``sync_unknown``); use this function or
    :func:`ozagia.core.cross_repo.relations` for the standalone
    freshness-only report.

    Reference synchronization follows the normal federation query rules; use
    :func:`ozagia.core.federation.without_reference_sync` to inspect cached
    state without updating designated references.
    """
    from ozagia.core import dependency_links, federation

    selected = federation._root(root)
    if roots is not None:
        roots = tuple(roots)
    members = federation._members(selected, roots)
    states = {item["alias"]: federation._freshness(item)[0] for item in members}
    dependencies = dependency_links.analyze(members, states)
    apis = api_relationships.analyze(members)
    return analyze(selected, members, dependencies, states, apis)


__all__ = ["analyze", "check", "family_index"]
