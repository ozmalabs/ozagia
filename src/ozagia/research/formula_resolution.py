"""Resolve formula notation only when the scoped research graph supports it.

This is a proposal helper, not a parser or a source of mathematical facts. It
returns graph-backed alternatives and an executable ``implicit_products`` map
for the existing formula translator when a unique decomposition is supported.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from ozagia.research.graph import ResearchError


def _state(graph: Any) -> Mapping[str, Any]:
    state = graph.state() if hasattr(graph, "state") else graph
    if not isinstance(state, Mapping):
        raise ResearchError("expected a ResearchGraph or graph state")
    return state


def _scope_edges(state: Mapping[str, Any], scope_id: str) -> list[dict[str, Any]]:
    nodes = {n.get("id"): dict(n) for n in state.get("nodes", []) if isinstance(n, Mapping)}
    edges = [dict(e) for e in state.get("edges", []) if isinstance(e, Mapping)]
    if scope_id not in nodes or nodes[scope_id].get("type") != "Frame":
        raise ResearchError("scope_id must identify an existing Frame")
    return edges


def _notation(node: Mapping[str, Any]) -> str | None:
    value = node.get("notation") or node.get("name")
    return value.strip() if isinstance(value, str) and value.strip() else None


def resolve_formula_ambiguity(graph: Any, formula: str, *, scope_id: str) -> dict[str, Any]:
    """Return scoped symbol interpretations and select only a unique supported one.

    ``implicit_products`` can be passed directly to
    ``ozagia.research.formula.translate_formula``. For example, if the
    scoped context chain contains symbols ``m`` and ``c`` but not ``mc``, ``E=mc^2``
    yields the graph-supported map ``{"mc": ["m", "c"]}``. If both ``mc`` and
    the factors are scoped, the result is explicitly tied. Function applications
    are reported as notation evidence; the current Term translator does not
    encode uninterpreted functions, so this helper never invents an executable
    function Term.
    """
    if not isinstance(formula, str) or not formula.strip():
        raise ResearchError("formula must be a non-empty string")
    state = _state(graph)
    edges = _scope_edges(state, scope_id)
    # The native substrate query is part of candidate admission. A child Frame
    # may inherit from its scoped_by parent; walk only that directed chain so a
    # sibling source context cannot leak into the interpretation.
    from ozagia.research.native import project_research_graph

    projection = project_research_graph(state)
    native_to_research = {
        native_id: research_id for research_id, native_id in projection["node_id_map"].items()
    }
    frame_by_id = {
        n.get("id"): dict(n)
        for n in state.get("nodes", [])
        if isinstance(n, Mapping) and n.get("type") == "Frame"
    }
    context_frames: list[str] = [scope_id]
    visited = {scope_id}
    frame_query = {
        "type": "Filter",
        "source": {"type": "NodesOfType", "node_type": "Frame"},
        "var_name": "p",
        "predicate": {
            "type": "Exists",
            "var_name": "c",
            "source": {
                "type": "InNeighbours",
                "node": {"type": "Var", "name": "p"},
                "edge_type": "scoped_by",
                "any_type": False,
            },
            "predicate": {
                "type": "Eq",
                "a": {
                    "type": "Attr",
                    "node": {"type": "Var", "name": "c"},
                    "key": "projection_provenance",
                },
            },
        },
    }
    queue = [scope_id]
    while queue:
        child_id = queue.pop(0)
        child_provenance = {
            "engine": projection["engine_provenance"]["engine"],
            "projection_version": projection["engine_provenance"]["projection_version"],
            "investigation_id": state.get("investigation_id"),
            "research_node_id": child_id,
        }
        query = {
            **frame_query,
            "predicate": {
                **frame_query["predicate"],
                "predicate": {
                    "type": "Eq",
                    "a": {
                        "type": "Attr",
                        "node": {"type": "Var", "name": "c"},
                        "key": "projection_provenance",
                    },
                    "b": {"type": "Lit", "value": child_provenance},
                },
            },
        }
        projected = project_research_graph(state, query=query)
        for row in projected["query_result"]:
            if not isinstance(row, Mapping) or row.get("kind") != "NodeID":
                continue
            parent_id = native_to_research.get(row.get("value"))
            if parent_id in frame_by_id and parent_id not in visited:
                visited.add(parent_id)
                context_frames.append(parent_id)
                queue.append(parent_id)

    symbols: dict[str, dict[str, Any]] = {}
    symbol_scopes: dict[str, str] = {}
    scoped_symbol_queries: list[dict[str, Any]] = []
    for context_frame_id in context_frames:
        frame_provenance = {
            "engine": projection["engine_provenance"]["engine"],
            "projection_version": projection["engine_provenance"]["projection_version"],
            "investigation_id": state.get("investigation_id"),
            "research_node_id": context_frame_id,
        }
        symbol_query = {
            "type": "Filter",
            "source": {"type": "NodesOfType", "node_type": "MathematicalSymbol"},
            "var_name": "s",
            "predicate": {
                "type": "Exists",
                "var_name": "f",
                "source": {
                    "type": "Neighbours",
                    "node": {"type": "Var", "name": "s"},
                    "edge_type": "scoped_by",
                    "any_type": False,
                },
                "predicate": {
                    "type": "Eq",
                    "a": {
                        "type": "Attr",
                        "node": {"type": "Var", "name": "f"},
                        "key": "projection_provenance",
                    },
                    "b": {"type": "Lit", "value": frame_provenance},
                },
            },
        }
        scoped_symbol_queries.append(symbol_query)
        projected = project_research_graph(state, query=symbol_query)
        ids = {
            row.get("value")
            for row in projected["query_result"]
            if isinstance(row, Mapping) and row.get("kind") == "NodeID"
        }
        for nid in ids:
            research_id = native_to_research.get(nid)
            node = next(
                (
                    n
                    for n in state.get("nodes", [])
                    if isinstance(n, Mapping) and n.get("id") == research_id
                ),
                None,
            )
            if node is not None and node.get("type") == "MathematicalSymbol":
                symbols[research_id] = dict(node)
                symbol_scopes[research_id] = context_frame_id
    by_name: dict[str, list[dict[str, Any]]] = {}
    by_notation: dict[str, list[dict[str, Any]]] = {}
    for symbol in symbols.values():
        name = _notation(symbol)
        if name and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", name):
            by_notation.setdefault(name, []).append(symbol)
            if symbol.get("arity", 0) == 0:
                by_name.setdefault(name, []).append(symbol)

    # Candidate compact names are found from identifier tokens, excluding call
    # heads and known translator operators. Only letter-only names can be
    # implicit products under the translator's current syntax.
    tokens = set(re.findall(r"(?<!\\)\b[A-Za-z][A-Za-z0-9_]*\b", formula))
    call_heads = set(re.findall(r"\b([A-Za-z_]\w*)\s*\(", formula))
    macro_tokens = set(re.findall(r"\\[A-Za-z]+", formula))
    builtin = {"abs", "min", "max", "True", "False"}
    products: list[dict[str, Any]] = []
    interpretations: list[dict[str, Any]] = []
    for token in sorted(tokens - call_heads - builtin):
        if not token.isalpha() or len(token) < 2:
            continue
        direct = by_name.get(token, [])
        for node in direct:
            interpretations.append(
                {
                    "notation": token,
                    "kind": "single_symbol",
                    "symbol_ids": [node["id"]],
                    "scope_frame_ids": [symbol_scopes[node["id"]]],
                }
            )
        # Enumerate splits that are themselves sourced, scoped symbols. Keep
        # all supported splits: choosing among them by lexical convention would
        # be an unsupported mathematical decision.
        for cut in range(1, len(token)):
            left, right = token[:cut], token[cut:]
            left_nodes, right_nodes = by_name.get(left, []), by_name.get(right, [])
            if left_nodes and right_nodes:
                for left_node in left_nodes:
                    for right_node in right_nodes:
                        product = {
                            "notation": token,
                            "kind": "implicit_product",
                            "factors": [left, right],
                            "symbol_ids": [left_node["id"], right_node["id"]],
                            "scope_frame_ids": [
                                symbol_scopes[left_node["id"]],
                                symbol_scopes[right_node["id"]],
                            ],
                        }
                        products.append(product)
                        interpretations.append(product)

    function_evidence = []
    for name in sorted(call_heads):
        candidates = [
            node
            for node in by_notation.get(name, [])
            if node.get("arity") is not None and node.get("arity") > 0
        ]
        if candidates:
            function_evidence.append(
                {
                    "notation": name,
                    "symbol_ids": [n["id"] for n in candidates],
                    "arities": [n.get("arity") for n in candidates],
                }
            )

    macro_evidence = []
    for symbol in symbols.values():
        name = symbol.get("name") or symbol.get("notation")
        if name in macro_tokens:
            macro_evidence.append(
                {
                    "notation": name,
                    "symbol_id": symbol["id"],
                    "scope_frame_id": symbol_scopes[symbol["id"]],
                    "expansion": symbol.get("expansion"),
                    "locator": symbol.get("locator"),
                    "interpretation_status": symbol.get("interpretation_status"),
                    "semantics_verified": symbol.get("semantics_verified", False),
                }
            )

    # An exact scoped symbol is a supported reading. A product reading is only
    # selected if it is the sole supported reading for that compact token.
    selected_products: dict[str, list[str]] = {}
    selected_symbols: dict[str, dict[str, Any]] = {}
    tied = []
    for token in sorted({x["notation"] for x in interpretations}):
        options = [x for x in interpretations if x["notation"] == token]
        if len(options) == 1 and options[0]["kind"] == "implicit_product":
            selected_products[token] = list(options[0]["factors"])
        elif len(options) == 1 and options[0]["kind"] == "single_symbol":
            symbol_id = options[0]["symbol_ids"][0]
            selected_symbols[token] = {
                "symbol_id": symbol_id,
                "scope_frame_id": options[0]["scope_frame_ids"][0],
            }
        elif len(options) > 1:
            tied.append({"notation": token, "candidates": options})

    # Definition provenance is included so callers can explain why each node
    # is evidence. Definitions remain authored assertions, not validated truth.
    definition_edges = [e for e in edges if e.get("type") == "defines"]
    definitions = []
    for edge in definition_edges:
        symbol = symbols.get(edge.get("tgt"))
        definition = next(
            (
                n
                for n in state.get("nodes", [])
                if isinstance(n, Mapping)
                and n.get("id") == edge.get("src")
                and n.get("type") == "MathematicalDefinition"
            ),
            None,
        )
        if symbol and definition:
            definitions.append(
                {
                    "symbol_id": symbol["id"],
                    "definition_id": definition["id"],
                    "wording": definition.get("wording"),
                    "status": definition.get("status"),
                }
            )

    has_selection = bool(selected_products or selected_symbols)
    status = "selected" if has_selection and not tied else "tied" if tied else "unresolved"
    unresolved_names = sorted(
        token
        for token in tokens - call_heads - builtin
        if token.isalpha()
        and len(token) > 1
        and not any(x["notation"] == token for x in interpretations)
    )
    if unresolved_names and status == "selected":
        status = "partially_resolved"
    if function_evidence and status == "unresolved":
        status = "function_evidence_only"
    selected_ids = sorted(
        {node_id for reading in selected_symbols.values() for node_id in [reading["symbol_id"]]}
        | {
            sid
            for token, factors in selected_products.items()
            for option in interpretations
            if option["notation"] == token and option["kind"] == "implicit_product"
            for sid in option["symbol_ids"]
        }
    )
    residuals = []
    if selected_products and not tied:
        for compact in selected_products:
            residuals.append(
                f"Selected {compact!r} as a graph-supported scalar product; no scoped single-symbol "
                "assertion for the compact spelling was found, but the graph may be incomplete."
            )
    selected_interpretation = None
    if has_selection and not tied:
        if selected_products and selected_symbols:
            selected_interpretation = {
                "kind": "mixed_symbol_and_product_readings",
                "symbols": selected_symbols,
                "implicit_products": selected_products,
            }
        elif selected_products:
            selected_interpretation = {
                "kind": "implicit_products",
                "implicit_products": selected_products,
            }
        else:
            selected_interpretation = {
                "kind": "scoped_symbol_readings",
                "symbols": selected_symbols,
            }
    return {
        "status": status,
        "scope_id": scope_id,
        "formula": formula,
        "selected_interpretation": selected_interpretation,
        "implicit_products": selected_products if selected_products and not tied else {},
        "selected_symbols": selected_symbols if selected_symbols and not tied else {},
        "tied": tied,
        "interpretations": interpretations,
        "unresolved_notation": unresolved_names,
        "selected_symbol_ids": selected_ids,
        "residuals": residuals,
        "function_evidence": function_evidence,
        "macro_evidence": macro_evidence,
        "definitions": definitions,
        "provenance": {
            "basis": "existing scoped MathematicalSymbol and MathematicalDefinition graph assertions",
            "native_query_engine": projected["engine_provenance"],
            "native_queries": {
                "ancestor_frames": frame_query,
                "scoped_symbols": scoped_symbol_queries,
            },
            "context_frame_ids": context_frames,
            "context_frames": [frame_by_id[fid] for fid in context_frames],
            "investigation_id": state.get("investigation_id"),
            "scope_frame": dict(
                next(
                    n
                    for n in state.get("nodes", [])
                    if isinstance(n, Mapping) and n.get("id") == scope_id
                )
            ),
            "graph_assertions_are_source_provenance_not_truth_verification": True,
        },
    }


__all__ = ["resolve_formula_ambiguity"]
