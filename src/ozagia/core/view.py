"""A deterministic, inspectable view model of a graph.

``build_view`` is a projection, not an analysis: it applies an aperture to a
graph bundle and returns plain JSON data. It does not assess truth, support,
consensus or freshness, and it does not call a model or the network.

Two input shapes are understood:

Dev-memory snapshot (``.ozagia/graph.json``)::

    {"version": 2, "nodes": [{"id": 1, "type": "File", "key": "file:a.py",
                              "attrs": {"name": "a.py", "path": "a.py"}}],
     "edges": [{"src": 1, "type": "contains", "tgt": 2, "attrs": {}}]}

Research graph bundle (``ResearchGraph.export()["bundle"]``)::

    {"schema_version": 6, "investigation_id": "research-...",
     "nodes": [{"id": "claim-0001", "type": "Claim", ...}],
     "edges": [{"id": "edge-000001", "src": "...", "tgt": "...",
                "type": "supports", ...}], ...}

The output is stable for a given input and aperture: node and edge order is
sorted, layout seeds are fixed, and JSON serialisation uses sorted keys. A
node type or edge type the aperture does not resolve becomes ``"other"``; it
is never dropped. Limits report what was omitted and why.
"""
from __future__ import annotations

import json
import math
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from ozagia.core.apertures import (
    UNRESOLVED,
    as_aperture,
    group_colors,
)

SCHEMA_VERSION = 1
GRAPH_KINDS = ("dev-memory", "research")

_RESERVED_NODE = frozenset({"id", "type", "attrs", "key"})
_RESERVED_EDGE = frozenset({"id", "src", "tgt", "type", "attrs"})
_LABEL_FIELDS = ("label", "name", "title", "wording", "question", "summary",
                 "path", "qualname")
_DATE_FIELDS = ("date", "day", "created_at", "updated_at", "at", "timestamp",
                "head_date")


class ViewError(ValueError):
    """A graph bundle or view request that cannot be projected."""


def _json_safe(value: Any) -> Any:
    """A deterministic JSON-safe copy of a graph attribute value."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (set, frozenset)):
        return [_json_safe(item) for item in sorted(value, key=str)]
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return str(value)


def _unwrap(graph: Any) -> dict[str, Any]:
    if not isinstance(graph, dict):
        raise ViewError("graph must be a JSON object")
    bundle = graph.get("bundle")
    if isinstance(bundle, dict):
        return bundle
    return graph


def graph_kind(graph: Any) -> str:
    """``"dev-memory"`` or ``"research"`` for a graph bundle."""
    data = _unwrap(graph)
    if ("investigation_id" in data
            or ("schema_version" in data and "events" in data)):
        return "research"
    if "version" in data or "nodes" in data:
        return "dev-memory"
    raise ViewError(
        "unrecognised graph: expected a dev-memory snapshot or a research "
        "graph bundle with nodes and edges")


def read_graph(path: str | Path) -> dict[str, Any]:
    """Read a graph JSON file, with a clear error when it is absent or bad."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ViewError(
            f"no graph at {path}; build the index first or pass --graph") from None
    except (OSError, UnicodeError) as exc:
        raise ViewError(f"cannot read graph at {path}: {exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ViewError(f"graph at {path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ViewError(f"graph at {path} must be a JSON object")
    return data


def _id_key(value: str) -> tuple:
    text = str(value)
    return (0, int(text)) if text.isdigit() else (1, text)


def _unique_id(base: str, used: set[str]) -> str:
    candidate = base
    suffix = 2
    while candidate in used:
        candidate = f"{base}#{suffix}"
        suffix += 1
    used.add(candidate)
    return candidate


def _node_attrs(raw: dict[str, Any]) -> dict[str, Any]:
    attrs = raw.get("attrs")
    out = dict(attrs) if isinstance(attrs, dict) else {}
    for key, value in raw.items():
        if key not in _RESERVED_NODE and key not in out:
            out[key] = value
    return _json_safe(out)


def _edge_attrs(raw: dict[str, Any]) -> dict[str, Any]:
    attrs = raw.get("attrs")
    out = dict(attrs) if isinstance(attrs, dict) else {}
    for key, value in raw.items():
        if key not in _RESERVED_EDGE and key not in out:
            out[key] = value
    return _json_safe(out)


def _label(attrs: dict[str, Any], raw_type: str, node_id: str) -> str:
    for key in _LABEL_FIELDS:
        value = attrs.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return str(value)
    return f"{raw_type} {node_id}".strip() or str(node_id)


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) and number >= 0 else None


def _group_for(raw_type: str, display_type: str, attrs: dict[str, Any],
               collapse: dict[str, Any]) -> tuple[str, bool]:
    """The display group and whether a collapse rule produced it."""
    if display_type == UNRESOLVED:
        return collapse["unresolved"], False
    by_attr = collapse["by_attr"]
    if raw_type in by_attr:
        value = attrs.get(by_attr[raw_type])
        if value is None or not str(value).strip():
            return collapse["unresolved"], True
        return str(value).strip(), True
    groups = collapse["groups"]
    if raw_type in groups:
        group = groups[raw_type]
        return group, group != raw_type.lower()
    return raw_type.lower(), False


def _normalise_nodes(graph: dict[str, Any], aperture: dict[str, Any]
                     ) -> list[dict[str, Any]]:
    raw_nodes = graph.get("nodes", [])
    if not isinstance(raw_nodes, list):
        raise ViewError("graph.nodes must be a list")
    resolved = set(aperture["resolves"]["nodes"])
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_nodes):
        if not isinstance(raw, dict):
            raise ViewError(f"graph.nodes[{index}] must be an object")
        raw_id = raw.get("id")
        if raw_id is None or str(raw_id).strip() == "":
            raise ViewError(f"graph.nodes[{index}] has no id")
        node_id = str(raw_id)
        if node_id in seen:
            raise ViewError(f"duplicate node id: {node_id}")
        seen.add(node_id)
        attrs = _node_attrs(raw)
        raw_type = str(raw.get("type") or UNRESOLVED)
        out.append({
            "id": node_id,
            "raw_type": raw_type,
            "type": raw_type if raw_type in resolved else UNRESOLVED,
            "label": _label(attrs, raw_type, node_id),
            "attrs": attrs,
            "key": str(raw.get("key") or node_id),
        })
    out.sort(key=lambda node: _id_key(node["id"]))
    return out


def _normalise_edges(graph: dict[str, Any], aperture: dict[str, Any],
                     node_ids: set[str]) -> tuple[list[dict[str, Any]], int]:
    raw_edges = graph.get("edges", [])
    if not isinstance(raw_edges, list):
        raise ViewError("graph.edges must be a list")
    resolved = set(aperture["resolves"]["edges"])
    prepared: list[dict[str, Any]] = []
    dangling = 0
    for index, raw in enumerate(raw_edges):
        if not isinstance(raw, dict):
            raise ViewError(f"graph.edges[{index}] must be an object")
        src = raw.get("src")
        tgt = raw.get("tgt")
        if src is None or tgt is None:
            dangling += 1
            continue
        src, tgt = str(src), str(tgt)
        if src not in node_ids or tgt not in node_ids:
            dangling += 1
            continue
        raw_type = str(raw.get("type") or UNRESOLVED)
        raw_edge_id = raw.get("id")
        prepared.append({
            "base": (str(raw_edge_id) if raw_edge_id is not None
                     else f"e:{src}:{raw_type}:{tgt}"),
            "src": src,
            "tgt": tgt,
            "raw_type": raw_type,
            "type": raw_type if raw_type in resolved else UNRESOLVED,
            "attrs": _edge_attrs(raw),
        })
    # Assign suffixes for duplicate ids in a stable order, not input order.
    prepared.sort(key=lambda edge: (
        edge["src"], edge["raw_type"], edge["tgt"], edge["base"],
        json.dumps(edge["attrs"], sort_keys=True, ensure_ascii=False,
                   default=str)))
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for edge in prepared:
        edge_id = _unique_id(edge.pop("base"), seen)
        out.append({"id": edge_id, **edge})
    out.sort(key=lambda edge: _id_key(edge["id"]))
    return out, dangling


def _scope_nodes(nodes: list[dict[str, Any]], edges: list[dict[str, Any]],
                 scope: dict[str, Any], depth: int | None,
                 scope_root: str | None) -> tuple[list[dict[str, Any]], set[str]]:
    """Nodes inside the aperture's scope (or all nodes without one)."""
    allowed_types = set(scope["node_types"])
    if scope_root is None:
        selected = [node for node in nodes
                    if not allowed_types or node["raw_type"] in allowed_types]
        return selected, {node["id"] for node in selected}

    if scope_root not in {node["id"] for node in nodes}:
        raise ViewError(f"scope root {scope_root!r} is not in the graph")
    root_node = next(node for node in nodes if node["id"] == scope_root)
    if allowed_types and root_node["raw_type"] not in allowed_types:
        raise ViewError(
            f"scope root {scope_root!r} is excluded by scope.node_types")
    allowed_edges = set(scope["edge_types"])
    adjacency: dict[str, set[str]] = {node["id"]: set() for node in nodes}
    for edge in edges:
        if allowed_edges and edge["raw_type"] not in allowed_edges:
            continue
        adjacency[edge["src"]].add(edge["tgt"])
        adjacency[edge["tgt"]].add(edge["src"])
    # A root without an explicit depth means its immediate neighbourhood; a
    # depth of 0 means the root alone.
    limit = 1 if depth is None else depth
    reached = {scope_root}
    frontier = {scope_root}
    for _ in range(limit):
        frontier = {other for node_id in frontier
                    for other in adjacency.get(node_id, ())
                    if other not in reached}
        if not frontier:
            break
        reached.update(frontier)
    selected = [node for node in nodes if node["id"] in reached
                and (not allowed_types or node["raw_type"] in allowed_types)]
    return selected, {node["id"] for node in selected}


def _snapshot(graph: dict[str, Any], kind: str, nodes: list[dict[str, Any]],
              root: str | None) -> dict[str, Any]:
    snapshot: dict[str, Any] = {
        "root": root,
        "head": None,
        "freshness": None,
        "generated_at": None,
    }
    repo = next((node for node in nodes if node["raw_type"] == "Repo"), None)
    if repo is not None:
        attrs = repo["attrs"]
        for key in ("root", "head", "freshness", "generated_at"):
            value = attrs.get(key)
            if value is not None and str(value).strip():
                snapshot[key] = str(value)
    elif kind == "research" and graph.get("root") is not None:
        snapshot["root"] = str(graph["root"])
    if snapshot["root"] is not None:
        snapshot["root"] = str(snapshot["root"])
    return snapshot


def _provenance(graph: dict[str, Any], kind: str, source: str | None
                ) -> dict[str, Any]:
    sources: list[str] = []
    raw = graph.get("provenance")
    if isinstance(raw, dict) and isinstance(raw.get("sources"), list):
        sources.extend(str(item) for item in raw["sources"])
    if source:
        sources.append(str(source))
    result: dict[str, Any] = {
        "sources": sorted(set(sources)),
        "derived": True,
        "graph": kind,
        "graph_schema": graph.get("version", graph.get("schema_version")),
    }
    if graph.get("investigation_id"):
        result["investigation"] = str(graph["investigation_id"])
    return result


def build_view(graph: dict[str, Any], aperture: str | dict[str, Any], *,
               root: str | Path | None = None, depth: int | None = None,
               scope_root: str | int | None = None,
               source: str | Path | None = None) -> dict[str, Any]:
    """Project ``graph`` through ``aperture`` into a versioned view model.

    ``root`` is only a fallback for snapshot metadata; it is not used to read
    the checkout. ``scope_root`` and ``depth`` override the aperture's scope.
    ``source`` is an optional graph path recorded honestly in provenance.
    """
    definition = as_aperture(aperture, root=root)
    if depth is not None and (isinstance(depth, bool)
                              or not isinstance(depth, int) or depth < 0):
        raise ViewError("depth must be an integer >= 0 or null")
    data = _unwrap(graph)
    kind = graph_kind(data)
    nodes = _normalise_nodes(data, definition)
    node_ids = {node["id"] for node in nodes}
    edges, dangling = _normalise_edges(data, definition, node_ids)

    scope = definition["scope"]
    effective_root = (str(scope_root) if scope_root is not None
                      else scope["root"])
    effective_depth = depth if depth is not None else scope["depth"]
    scoped_nodes, scoped_ids = _scope_nodes(
        nodes, edges, scope, effective_depth, effective_root)

    allowed_edge_types = set(scope["edge_types"])
    scoped_edges = [
        edge for edge in edges
        if edge["src"] in scoped_ids and edge["tgt"] in scoped_ids
        and (not allowed_edge_types or edge["raw_type"] in allowed_edge_types)
    ]
    scope_node_omitted = len(nodes) - len(scoped_nodes)
    scope_edge_omitted = len(edges) - len(scoped_edges)

    # Degree is a visual property only. It is computed on the scoped graph so
    # a cap keeps the connected part of the view rather than an arbitrary
    # lexical slice.
    degree: Counter[str] = Counter()
    for edge in scoped_edges:
        degree[edge["src"]] += 1
        degree[edge["tgt"]] += 1
    limits = definition["limits"]
    if limits["truncate"] == "id":
        priority = sorted(scoped_nodes, key=lambda node: _id_key(node["id"]))
    else:
        priority = sorted(
            scoped_nodes,
            key=lambda node: (-degree.get(node["id"], 0), _id_key(node["id"])))
    kept_nodes = priority[:limits["max_nodes"]]
    kept_ids = {node["id"] for node in kept_nodes}
    node_cap_omitted = len(scoped_nodes) - len(kept_nodes)

    capped_edges = [edge for edge in scoped_edges
                    if edge["src"] in kept_ids and edge["tgt"] in kept_ids]
    edge_endpoint_omitted = len(scoped_edges) - len(capped_edges)
    kept_edges = sorted(capped_edges,
                        key=lambda edge: _id_key(edge["id"])
                        )[:limits["max_edges"]]
    edge_cap_omitted = len(capped_edges) - len(kept_edges)

    final_degree: Counter[str] = Counter()
    for edge in kept_edges:
        final_degree[edge["src"]] += 1
        final_degree[edge["tgt"]] += 1
    max_degree = max(final_degree.values(), default=0)

    output_nodes: list[dict[str, Any]] = []
    group_counts: Counter[str] = Counter()
    group_collapsed: Counter[str] = Counter()
    for node in sorted(kept_nodes, key=lambda item: _id_key(item["id"])):
        group, collapsed = _group_for(
            node["raw_type"], node["type"], node["attrs"],
            definition["collapse"])
        group_counts[group] += 1
        if collapsed:
            group_collapsed[group] += 1
        weight = _number(node["attrs"].get("weight"))
        if weight is None:
            weight = (final_degree.get(node["id"], 0) / max_degree
                      if max_degree else 0.0)
        output_nodes.append({
            "id": node["id"],
            "type": node["type"],
            "raw_type": node["raw_type"],
            "label": node["label"],
            "group": group,
            "attrs": node["attrs"],
            "weight": round(float(weight), 6),
            "key": node["key"],
        })

    output_edges = [{
        "id": edge["id"],
        "src": edge["src"],
        "tgt": edge["tgt"],
        "type": edge["type"],
        "raw_type": edge["raw_type"],
        "attrs": edge["attrs"],
    } for edge in kept_edges]

    labels = definition["collapse"]["labels"]
    groups = [{
        "id": group,
        "label": labels.get(group, group),
        "count": group_counts[group],
        "collapsed": group_collapsed[group],
    } for group in sorted(group_counts)]
    colors = group_colors(group_counts)

    omitted_by = {
        "scope_nodes": scope_node_omitted,
        "scope_edges": scope_edge_omitted,
        "node_cap": node_cap_omitted,
        "edge_endpoints": edge_endpoint_omitted,
        "edge_cap": edge_cap_omitted,
        "dangling_edges": dangling,
    }
    omitted_nodes = scope_node_omitted + node_cap_omitted
    omitted_edges = (scope_edge_omitted + edge_endpoint_omitted
                     + edge_cap_omitted + dangling)
    reasons: list[str] = []
    if scope_node_omitted or scope_edge_omitted:
        reasons.append("scope filter")
    if node_cap_omitted or edge_endpoint_omitted:
        reasons.append("node cap")
    if edge_cap_omitted:
        reasons.append("edge cap")
    if dangling:
        reasons.append("dangling edges")
    omitted = {
        "nodes": omitted_nodes,
        "edges": omitted_edges,
        "reason": "; ".join(reasons) if reasons else "none",
        "by": {key: value for key, value in omitted_by.items() if value},
    }

    return {
        "schema": SCHEMA_VERSION,
        "aperture": definition["id"],
        "snapshot": _snapshot(data, kind, nodes,
                              str(root) if root is not None else None),
        "nodes": output_nodes,
        "edges": output_edges,
        "groups": groups,
        "legend": {
            "color": colors,
            "encoding": dict(definition["render"]["encoding"]),
        },
        "layout": {
            "kind": definition["render"]["layout"],
            "seed": definition["render"]["seed"],
        },
        "provenance": _provenance(
            data, kind, str(source) if source is not None else None),
        "omitted": omitted,
    }


def render_json(view: dict[str, Any]) -> str:
    """Serialise a view deterministically (sorted keys, fixed spacing)."""
    return json.dumps(view, sort_keys=True, indent=2, ensure_ascii=False,
                      allow_nan=False) + "\n"


def node_dates(nodes: Iterable[dict[str, Any]]) -> dict[str, str]:
    """The first plausible date string per node, for timeline layouts."""
    out: dict[str, str] = {}
    for node in nodes:
        attrs = node.get("attrs") or {}
        for key in _DATE_FIELDS:
            value = attrs.get(key)
            if value is None:
                continue
            text = str(value)
            if len(text) >= 10 and text[4:5] == "-" and text[7:8] == "-":
                out[node["id"]] = text[:10]
                break
            if text.isdigit() and len(text) == 8:
                out[node["id"]] = f"{text[:4]}-{text[4:6]}-{text[6:8]}"
                break
    return out


__all__ = [
    "GRAPH_KINDS", "SCHEMA_VERSION", "ViewError", "build_view", "graph_kind",
    "node_dates", "read_graph", "render_json",
]
