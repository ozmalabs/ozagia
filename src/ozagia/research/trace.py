"""Trace research derivation links and evaluate explicitly bound native Terms.

The research graph remains an authored ledger. ``derived_from`` links describe
the chain a researcher recorded; native Term evaluation computes a value for a
bound Term node. Neither the links nor a node's ``assertion``/``status`` fields
are treated as evidence that a proposition is true.
"""

from __future__ import annotations

import json
import math
from typing import Any, Mapping

from ozagia.research.graph import ResearchError


def trace_chain(
    state: Mapping[str, Any], start: str, *, max_depth: int = 64, max_paths: int = 256
) -> dict[str, Any]:
    """Walk the full directed ``derived_from`` DAG from *start* deterministically.

    ``path`` remains populated for linear traces; branched graphs additionally
    include every leaf path. Shared dependencies are represented once in
    ``nodes``/``edges`` and remain present in each applicable leaf path.
    """
    if not isinstance(max_depth, int) or isinstance(max_depth, bool) or max_depth < 1:
        raise ResearchError("max_depth must be a positive integer")
    if not isinstance(max_paths, int) or isinstance(max_paths, bool) or max_paths < 1:
        raise ResearchError("max_paths must be a positive integer")
    nodes = {n.get("id"): n for n in state.get("nodes", []) if isinstance(n, dict)}
    edges = [e for e in state.get("edges", []) if isinstance(e, dict)]
    if start not in nodes:
        raise ResearchError(f"unknown node id: {start}")
    outgoing_by_src: dict[str, list[dict[str, Any]]] = {}
    for edge in edges:
        if edge.get("type") == "derived_from":
            outgoing_by_src.setdefault(edge.get("src"), []).append(edge)
    for group in outgoing_by_src.values():
        group.sort(key=lambda e: str(e.get("id", "")))
    reached: list[str] = []
    reached_set: set[str] = set()
    reached_edges: list[dict[str, Any]] = []
    edge_ids: set[str] = set()
    leaf_paths: list[list[str]] = []
    cycle = missing = bound_exceeded = branched = False

    def visit(current: str, current_path: list[str], active: set[str]) -> None:
        nonlocal cycle, missing, bound_exceeded, branched
        if current not in reached_set:
            reached_set.add(current)
            reached.append(current)
        outgoing = outgoing_by_src.get(current, [])
        if not outgoing:
            leaf_paths.append(current_path)
            return
        if len(outgoing) > 1:
            branched = True
        if len(current_path) - 1 >= max_depth:
            bound_exceeded = True
            leaf_paths.append(current_path)
            return
        for edge in outgoing:
            if len(leaf_paths) >= max_paths:
                bound_exceeded = True
                return
            if edge.get("id") not in edge_ids:
                reached_edges.append(edge)
                edge_ids.add(edge.get("id"))
            nxt = edge.get("tgt")
            if nxt not in nodes:
                missing = True
                leaf_paths.append(current_path)
            elif nxt in active:
                cycle = True
                leaf_paths.append(current_path + [nxt])
            else:
                visit(nxt, current_path + [nxt], active | {nxt})

    visit(start, [start], {start})
    if cycle:
        status = "cycle"
    elif missing:
        status = "missing"
    elif bound_exceeded:
        status = "bound_exceeded"
    elif branched:
        status = "branch"
    elif not reached_edges:
        status = "missing"
    else:
        status = "complete"
    linear_path = leaf_paths[0] if len(leaf_paths) == 1 else [start]
    return {
        "status": status,
        "path": linear_path,
        "paths": leaf_paths,
        "nodes": [
            {
                "id": nid,
                "type": nodes[nid].get("type"),
                "wording": nodes[nid].get("wording"),
                "provenance": nodes[nid].get("provenance"),
            }
            for nid in reached
        ],
        "edges": reached_edges,
        "truth_assessed": False,
    }


def bind_term(substrate: Any, term_json: Mapping[str, Any]) -> Any:
    """Materialize a native DSL Term subgraph through the Rust substrate API."""
    if not isinstance(term_json, Mapping) or not isinstance(term_json.get("type"), str):
        raise ResearchError("term_json must be a native Term JSON object")
    root = substrate._inner.add_term_subgraph(dict(term_json))
    reflected = json.loads(substrate._inner.term_subgraph_json(root))
    canonical = lambda value: json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    if canonical(reflected) != canonical(dict(term_json)):
        raise ResearchError("native Term subgraph round-trip mismatch")
    return root


def execute(
    state: Mapping[str, Any],
    target: str,
    substrate: Any,
    env: Mapping[str, Any] | None = None,
    *,
    max_depth: int = 32,
    max_nodes: int = 64,
) -> dict[str, Any]:
    """Evaluate a target node's recorded native Term using Rust ``EvalSubgraph``.

    Nodes carry ``term`` Term JSON and optional ``bindings`` (alias -> research
    node id). Every binding must have a direct ``derived_from`` edge. Referenced
    terms are materialized as native Term subgraphs and exposed to the target as
    NodeIDs; the target Term decides how to consume each alias (usually through
    ``EvalSubgraph``). This function does not inspect assertion/status fields.
    """
    if not isinstance(max_depth, int) or isinstance(max_depth, bool) or max_depth < 1:
        raise ResearchError("max_depth must be a positive integer")
    if not isinstance(max_nodes, int) or isinstance(max_nodes, bool) or max_nodes < 1:
        raise ResearchError("max_nodes must be a positive integer")
    nodes = {n.get("id"): n for n in state.get("nodes", []) if isinstance(n, dict)}
    if target not in nodes:
        raise ResearchError(f"unknown node id: {target}")
    edges = [e for e in state.get("edges", []) if isinstance(e, dict)]
    by_id: dict[str, Any] = {}
    visiting: set[str] = set()

    def materialize(node_id: str, depth: int):
        if depth > max_depth:
            raise ResearchError("execution depth bound exceeded")
        if node_id in by_id:
            return by_id[node_id]
        if node_id in visiting:
            raise ResearchError(f"cyclic term binding at {node_id}")
        if len(by_id) + len(visiting) >= max_nodes:
            raise ResearchError("execution node bound exceeded")
        node = nodes.get(node_id)
        if node is None:
            raise ResearchError(f"binding references missing node: {node_id}")
        term = node.get("term")
        if not isinstance(term, dict) or not isinstance(term.get("type"), str):
            raise ResearchError(f"node {node_id} has no native Term JSON in `term`")
        bindings = node.get("bindings", {})
        if not isinstance(bindings, dict) or any(
            not isinstance(k, str) or not isinstance(v, str) for k, v in bindings.items()
        ):
            raise ResearchError(f"node {node_id} bindings must map aliases to node ids")
        if "target_term" in bindings:
            raise ResearchError("binding alias 'target_term' is reserved")
        direct = {
            e.get("tgt")
            for e in edges
            if e.get("type") == "derived_from" and e.get("src") == node_id
        }
        for alias, ref in bindings.items():
            if ref not in direct:
                raise ResearchError(
                    f"binding {alias!r} on {node_id} lacks a derived_from edge to {ref}"
                )
        visiting.add(node_id)
        # Recursively validate/materialize dependencies. Native Term references
        # remain explicit NodeIDs; we don't interpret them in Python.
        for ref in bindings.values():
            materialize(ref, depth + 1)
        root = bind_term(substrate, term)
        visiting.remove(node_id)
        by_id[node_id] = root
        return root

    target_node = nodes[target]
    target_bindings = target_node.get("bindings", {})
    if not isinstance(target_bindings, dict):
        raise ResearchError(f"node {target} bindings must be an object")
    term_root = materialize(target, 0)
    runtime_env = dict(env or {})
    recorded_env: dict[str, Any] = {}
    for node_id in sorted(by_id):
        for alias, ref in nodes[node_id].get("bindings", {}).items():
            value = by_id[ref]
            if alias in recorded_env and recorded_env[alias] != value:
                raise ResearchError(f"ambiguous binding alias {alias!r} refers to multiple nodes")
            recorded_env[alias] = value
    if "target_term" in runtime_env or "target_term" in recorded_env:
        raise ResearchError("env key 'target_term' is reserved")
    if any(alias in runtime_env for alias in recorded_env):
        raise ResearchError("external env cannot override a recorded binding alias")
    runtime_env.update(recorded_env)
    # A binding supplies a computed value rather than a caller-provided scalar.
    # Validate it against the formula's inferred input sort at the native boundary.
    for node_id in sorted(by_id):
        metadata = nodes[node_id].get("candidate_provenance", {}).get("translation", {})
        for alias, ref in nodes[node_id].get("bindings", {}).items():
            expected = metadata.get("variable_sorts", {}).get(alias, "unknown")
            if expected == "unknown":
                continue
            bound_value = substrate._inner.evaluate(
                {"type": "EvalSubgraph", "node": {"type": "Var", "name": "target_term"}},
                {**runtime_env, "target_term": by_id[ref]},
            )
            valid = (
                (expected == "bool" and type(bound_value) is bool)
                or (expected == "symbol" and type(bound_value) is str)
                or (expected == "int" and type(bound_value) is int)
                or (expected in {"number", "float"} and type(bound_value) in (int, float))
            )
            if not valid:
                raise ResearchError(f"binding {alias!r} requires {expected} result")
        for left, right in metadata.get("equality_sort_constraints", []):

            def bound_or_input(name):
                ref = nodes[node_id].get("bindings", {}).get(name)
                if ref is None:
                    return runtime_env.get(name)
                return substrate._inner.evaluate(
                    {"type": "EvalSubgraph", "node": {"type": "Var", "name": "target_term"}},
                    {**runtime_env, "target_term": by_id[ref]},
                )

            a, b = bound_or_input(left), bound_or_input(right)
            if type(a) is not type(b):
                raise ResearchError(
                    f"formula equality inputs {left!r} and {right!r} require compatible sorts"
                )

    # Formula Terms use mathematical surface notation; the native runtime's
    # integer operators wrap i64 and floats can overflow. Inspect each numeric
    # subexpression before accepting a result. Python's unbounded integers are
    # used only as an overflow witness; Rust still computes the actual result.
    def numeric_nodes(term):
        if isinstance(term, dict):
            for child in term.values():
                yield from numeric_nodes(child)
            if term.get("type") in {"Plus", "Times", "Minus", "Div", "Pow", "Abs", "Min", "Max"}:
                yield term
        elif isinstance(term, list):
            for child in term:
                yield from numeric_nodes(child)

    guard_reason = None
    for node_id in sorted(by_id):
        metadata = nodes[node_id].get("candidate_provenance", {}).get("translation", {})
        if not metadata or metadata.get("domains"):
            continue
        for numeric in numeric_nodes(nodes[node_id]["term"]):
            kind = numeric["type"]
            if kind in {"Plus", "Times", "Min", "Max"}:
                operand_terms = numeric.get("items", [])
            elif kind == "Abs":
                operand_terms = [numeric.get("arg")]
            else:
                operand_terms = [numeric.get("a"), numeric.get("b")]
            operands = [substrate._inner.evaluate(part, runtime_env) for part in operand_terms]
            if any(isinstance(x, float) and not math.isfinite(x) for x in operands):
                guard_reason = "non-finite numeric intermediate"
                break
            if (
                kind in {"Plus", "Times", "Minus", "Pow", "Abs"}
                and operands
                and all(type(x) is int for x in operands)
            ):
                if kind == "Plus":
                    exact = sum(operands)
                elif kind == "Times":
                    exact = 1
                    for operand in operands:
                        exact *= operand
                elif kind == "Minus":
                    exact = operands[0] - operands[1]
                elif kind == "Abs":
                    exact = abs(operands[0])
                elif operands[1] >= 64 and abs(operands[0]) > 1:
                    guard_reason = "integer operation exceeds native i64 range"
                    break
                else:
                    exact = pow(operands[0], operands[1]) if operands[1] >= 0 else None
                if exact is not None and not -(2**63) <= exact < 2**63:
                    guard_reason = "integer operation exceeds native i64 range"
                    break
            intermediate = substrate._inner.evaluate(numeric, runtime_env)
            if isinstance(intermediate, float) and not math.isfinite(intermediate):
                guard_reason = "non-finite numeric intermediate"
                break
        if guard_reason:
            break
    if guard_reason:
        return {
            "status": "unresolved",
            "target": target,
            "value": None,
            "trace": trace_chain(state, target, max_depth=max_depth),
            "materialized_nodes": sorted(by_id),
            "truth_assessed": False,
            "interpretation": guard_reason,
        }
    value = substrate._inner.evaluate(
        {"type": "EvalSubgraph", "node": {"type": "Var", "name": "target_term"}},
        {**runtime_env, "target_term": term_root},
    )
    trace = trace_chain(state, target, max_depth=max_depth)
    # The native evaluator uses None for an unresolved Var; it can also
    # represent a legitimate null-like value, so leave the result ambiguous.
    result_status = "unresolved" if value is None else "computed"
    return {
        "status": result_status,
        "target": target,
        "value": value,
        "trace": trace,
        "materialized_nodes": sorted(by_id),
        "truth_assessed": False,
        "interpretation": (
            "native evaluator returned None; unresolved input or null-like value is ambiguous"
            if value is None
            else "native computation result; assertions and claim truth are not assessed"
        ),
    }
