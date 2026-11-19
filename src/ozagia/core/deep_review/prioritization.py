"""Conservative graph based finding priority annotations.

Only a caller supplied graph explicitly marked fresh is considered. The
function is intentionally independent of collection and report writing so a
caller can opt into these annotations without changing finding evidence.
"""
from __future__ import annotations

from collections.abc import Mapping

MAX_NODES = 20_000
MAX_EDGES = 100_000
MAX_DEPTH = 3
MAX_RELATED = 20

_SEVERITY = {"blocking": 100, "major": 70, "minor": 40, "note": 10}
_CONFIDENCE = {"high": 3, "medium": 2, "low": 1, "unknown": 0}


def prioritize_findings(findings, graph, *, freshness="unknown", max_nodes=MAX_NODES,
                        max_edges=MAX_EDGES, max_depth=MAX_DEPTH,
                        churn_hotspots=None, focus_subjects=()):
    """Return copied findings with deterministic priority and blast radius.

    ``graph`` is a snapshot mapping with ``nodes`` and ``edges`` lists. Callers
    must pass ``freshness='fresh'`` only after verifying the snapshot against
    the current checkout. No graph facts are inferred from stale/unknown data.
    Locations must identify both a path and symbol; file-only matches are
    deliberately not expanded to every symbol in a file.
    """
    limits = (max_nodes, max_edges, max_depth)
    if any(not isinstance(v, int) or isinstance(v, bool) or v < 0
           for v in limits):
        raise ValueError("graph bounds must be non-negative integers")
    if max_nodes > MAX_NODES or max_edges > MAX_EDGES or max_depth > MAX_DEPTH:
        raise ValueError("graph bounds exceed conservative hard limits")
    node_rows = graph.get("nodes", []) if isinstance(graph, Mapping) else []
    edge_rows = graph.get("edges", []) if isinstance(graph, Mapping) else []
    valid = freshness == "fresh" and isinstance(node_rows, list) and isinstance(edge_rows, list)
    truncated = bool(valid and (len(node_rows) > max_nodes or len(edge_rows) > max_edges))
    nodes = {}
    incoming = {}
    degree = {}
    if valid and not truncated:
        for row in node_rows:
            if not isinstance(row, Mapping):
                continue
            try:
                ident = int(row.get("id"))
            except (TypeError, ValueError):
                continue
            attrs = row.get("attrs") if isinstance(row.get("attrs"), Mapping) else {}
            nodes[ident] = {"type": row.get("type"), "file": attrs.get("file"),
                            "name": attrs.get("name"),
                            "qualname": attrs.get("qualname")}
        for edge in edge_rows:
            if not isinstance(edge, Mapping):
                continue
            try:
                source, target = int(edge.get("src")), int(edge.get("tgt"))
            except (TypeError, ValueError):
                continue
            if source in nodes and target in nodes:
                degree.setdefault(source, set()).add((target, str(edge.get("type"))))
                degree.setdefault(target, set()).add((source, str(edge.get("type"))))
                if edge.get("type") == "calls":
                    incoming.setdefault(target, set()).add(source)

    # Only explicitly recorded hotspot counts are evidence; absence from a
    # truncated hotspot list does not mean a file has zero churn.
    churn = {}
    if isinstance(churn_hotspots, list):
        for row in churn_hotspots[:1000]:
            if not isinstance(row, Mapping):
                continue
            path, count = row.get("file"), row.get("commits")
            if (isinstance(path, str) and isinstance(count, int)
                    and not isinstance(count, bool) and 0 <= count <= 1_000_000):
                churn[path] = max(churn.get(path, 0), count)
    focus = set(focus_subjects)
    results = []
    for original in findings:
        item = dict(original)
        locations = item.get("locations") or []
        seeds = set()
        if valid and not truncated:
            for location in locations:
                if not isinstance(location, Mapping):
                    continue
                path, symbol = location.get("path"), location.get("symbol")
                if not path or not symbol:
                    continue
                wanted = str(symbol).split(".")[-1]
                for ident, node in nodes.items():
                    if (node["type"] == "Symbol" and node["file"] == path and
                            (node["qualname"] == symbol or
                             ("." not in str(symbol) and node["name"] == wanted))):
                        seeds.add(ident)
        omission = None
        if freshness != "fresh":
            omission = f"graph evidence omitted: snapshot freshness is {freshness}"
        elif truncated:
            omission = "graph evidence omitted: snapshot exceeds node or edge bound"
        elif not seeds:
            omission = "graph evidence omitted: no exact path and symbol match"
        reached = set(seeds)
        frontier = set(seeds)
        for _ in range(max_depth):
            next_frontier = set()
            for target in frontier:
                next_frontier.update(incoming.get(target, ()))
            next_frontier -= reached
            reached.update(next_frontier)
            frontier = next_frontier
        callers = reached - seeds
        related = sorted({str(nodes[i]["file"]) for i in callers if nodes[i]["file"]})
        base = _SEVERITY.get(str(item.get("severity", "note")), 10)
        graph_degree = sum(len(degree.get(seed, ())) for seed in seeds) if seeds else None
        path_counts = [churn[loc.get("path")] for loc in locations
                       if isinstance(loc, Mapping) and isinstance(loc.get("path"), str)
                       and loc["path"] in churn]
        churn_count = max(path_counts) if path_counts else None
        focused = bool(focus.intersection(item.get("subjects") or [item.get("subject")]))
        confidence_points = _CONFIDENCE.get(str(item.get("confidence", "unknown")), 0)
        # These are bounded triage signals, not evidence of severity or truth.
        score = (base + confidence_points + min(len(callers), 10)
                 + min(graph_degree or 0, 5) + min(churn_count or 0, 5)
                 + (5 if focused else 0))
        item["priority"] = {"score": score,
                            "basis": "severity, confidence, bounded fresh graph callers/degree, recorded churn and requested focus",
                            "signals": {"confidence": item.get("confidence", "unknown"),
                                        "graph_degree": graph_degree,
                                        "churn_commits": churn_count,
                                        "focus_match": focused if focus else None},
                            "blast_radius": {"direct_symbols": len(seeds),
                                             "reachable_callers": len(callers),
                                             "caller_files": related[:MAX_RELATED],
                                             "depth_limit": max_depth,
                                             "truncated": len(related) > MAX_RELATED},
                            "omission": omission}
        results.append(item)
    results.sort(key=lambda item: (-item["priority"]["score"],
                                   str(item.get("id", ""))))
    return results
