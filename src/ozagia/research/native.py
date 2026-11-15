"""Project an inspectable research graph into the native typed substrate.

This is a lossless structural projection: it creates no Rules or scientific
claims. Native node attributes retain the graph's JSON fields, and every
projected node/edge carries projection provenance.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ozagia.research.graph import ResearchError

PROJECTION_VERSION = "1"
ENGINE = "substrate_rs._native.Substrate"


def _json_value(value: Any, label: str) -> Any:
    import json

    try:
        return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise ResearchError(f"{label} must contain only JSON values: {exc}") from exc


def _query_value(value: Any, native_graph: Mapping[str, Any]) -> Any:
    """Serialize evaluator results while keeping NodeID identity and type."""
    id_types = {int(node["id"]): node.get("type") for node in native_graph.get("nodes", [])}
    if hasattr(value, "value") and isinstance(getattr(value, "value"), int):
        node_id = int(value.value)
        if node_id in id_types:
            return {"kind": "NodeID", "value": node_id, "node_type": id_types[node_id]}
    if isinstance(value, Mapping):
        return {str(k): _query_value(v, native_graph) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        converted = [_query_value(v, native_graph) for v in value]
        if isinstance(value, (set, frozenset)):
            converted.sort(key=lambda x: repr(x))
        return converted
    try:
        return _json_value(value, "native query result")
    except ResearchError as exc:
        raise ResearchError(
            f"unsupported native query result {type(value).__name__}: {exc}"
        ) from exc


def project_research_graph(
    graph: Any,
    *,
    substrate: Any = None,
    query: Mapping[str, Any] | None = None,
    env: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Project graph nodes and edges to ``substrate_rs`` and return native data.

    ``query`` optionally supplies read-only native Term JSON for
    ``Substrate.evaluate``. Projection builds only typed nodes, typed edges and
    Γ productions for endpoint type pairs observed in this graph.
    """
    try:
        from substrate_rs import _native
    except (ImportError, ModuleNotFoundError) as exc:
        raise ResearchError(
            "optional native projection backend unavailable; install substrate_rs or supply a substrate"
        ) from exc

    state = graph.state() if hasattr(graph, "state") else graph
    if not isinstance(state, Mapping):
        raise ResearchError("expected a ResearchGraph or graph state mapping")
    nodes = state.get("nodes")
    edges = state.get("edges")
    if not isinstance(nodes, list) or not isinstance(edges, list):
        raise ResearchError("research graph state needs nodes and edges arrays")
    native = substrate if substrate is not None else _native.Substrate()
    initial = native.graph_to_dict()
    if initial.get("nodes") or initial.get("edges"):
        raise ResearchError(
            "projection requires a fresh native substrate with no existing nodes or edges"
        )
    node_by_id = {n.get("id"): n for n in nodes if isinstance(n, Mapping)}
    if len(node_by_id) != len(nodes) or None in node_by_id:
        raise ResearchError("research graph contains malformed or duplicate node IDs")
    edge_specs: set[tuple[str, str, str]] = set()
    for edge in edges:
        if (
            not isinstance(edge, Mapping)
            or edge.get("src") not in node_by_id
            or edge.get("tgt") not in node_by_id
        ):
            raise ResearchError("research graph contains malformed edge endpoints")
        src_type, tgt_type = (
            node_by_id[edge["src"]].get("type"),
            node_by_id[edge["tgt"]].get("type"),
        )
        edge_type = edge.get("type")
        if not all(isinstance(x, str) and x for x in (src_type, tgt_type, edge_type)):
            raise ResearchError("research graph edge and endpoint types must be nonempty strings")
        edge_specs.add((src_type, edge_type, tgt_type))

    for src_type, edge_type, tgt_type in sorted(edge_specs):
        native.add_production(
            {
                "src": {"type": src_type, "var": "src"},
                "edge_type": edge_type,
                "tgt": {"type": tgt_type, "var": "tgt"},
                "where": None,
                "weight": {"type": "Lit", "value": 1.0},
                "provenance": "ocelot_research_native_projection_v1",
            }
        )

    graph_id = state.get("investigation_id")
    id_map: dict[str, Any] = {}
    for node in nodes:
        fields = {k: v for k, v in node.items() if k not in {"id", "type"}}
        attrs = _json_value(fields, f"node {node['id']} attributes")
        attrs["projection_provenance"] = {
            "engine": ENGINE,
            "projection_version": PROJECTION_VERSION,
            "investigation_id": graph_id,
            "research_node_id": node["id"],
        }
        id_map[node["id"]] = native.add_node(node["type"], attrs)

    for edge in edges:
        attrs = {k: v for k, v in edge.items() if k not in {"id", "type", "src", "tgt"}}
        attrs = _json_value(attrs, f"edge {edge.get('id')} attributes")
        attrs["projection_provenance"] = {
            "engine": ENGINE,
            "projection_version": PROJECTION_VERSION,
            "investigation_id": graph_id,
            "research_edge_id": edge.get("id"),
        }
        native.add_edge(id_map[edge["src"]], edge["type"], id_map[edge["tgt"]], attrs)

    evaluated = None
    if query is not None:
        if not isinstance(query, Mapping) or not isinstance(query.get("type"), str):
            raise ResearchError("query must be native Term JSON with a type field")
        before_query = _json_value(native.graph_to_dict(), "native graph before query")
        try:
            evaluated = native.evaluate(
                _json_value(dict(query), "query"), _json_value(dict(env or {}), "query environment")
            )
        except Exception as exc:
            raise ResearchError(f"native read-only query evaluation failed: {exc}") from exc
        after_query = _json_value(native.graph_to_dict(), "native graph after query")
        if after_query != before_query:
            raise ResearchError("native Term evaluation changed the projected graph")
    native_graph = _json_value(native.graph_to_dict(), "native graph snapshot")
    if query is not None:
        evaluated = _query_value(evaluated, native_graph)
    result = {
        "graph": native_graph,
        "node_id_map": {key: value.value for key, value in id_map.items()},
        "engine_provenance": {
            "engine": ENGINE,
            "projection_version": PROJECTION_VERSION,
            "investigation_id": graph_id,
            "node_count": len(nodes),
            "edge_count": len(edges),
        },
        "query_result": evaluated,
    }
    return result


__all__ = ["project_research_graph", "ENGINE", "PROJECTION_VERSION"]
