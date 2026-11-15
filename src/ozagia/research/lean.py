"""Export an explicit Boolean and bounded-integer subset as Lean 4 definitions.

This is a syntax translator for a deliberately small subset of the substrate
Term JSON format. It emits Bool/Int definitions and provenance strings; it does
not prove claims, add axioms, or treat a computed Bool as domain truth. Float,
rational, and finite-quantifier forms are rejected when their Lean semantics
cannot be shown to match the native evaluator.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any


class LeanExportError(ValueError):
    """A research graph cannot be represented by the supported Lean subset."""


_I64_MIN = -(1 << 63)
_I64_MAX = (1 << 63) - 1


def _ident(value: str) -> str:
    """Encode an arbitrary graph id as a stable Lean identifier component."""
    safe = re.sub(r"[^A-Za-z0-9_']", "_", value)
    if not safe or safe[0].isdigit():
        safe = "n_" + safe
    return safe


def _lean_string(value: str) -> str:
    # Lean string literals use familiar escapes and `\u{...}` for controls.
    escaped: list[str] = []
    for char in value:
        code = ord(char)
        if char == "\\":
            escaped.append("\\\\")
        elif char == '"':
            escaped.append('\\"')
        elif char == "\n":
            escaped.append("\\n")
        elif char == "\r":
            escaped.append("\\r")
        elif char == "\t":
            escaped.append("\\t")
        elif code < 0x20 or code == 0x7F:
            escaped.append(f"\\u{{{code:x}}}")
        else:
            escaped.append(char)
    return '"' + "".join(escaped) + '"'


def _node_index(graph: Any) -> dict[str, dict[str, Any]]:
    state = graph.state() if hasattr(graph, "state") else graph
    if not isinstance(state, Mapping) or not isinstance(state.get("nodes"), list):
        raise LeanExportError("expected a ResearchGraph or graph state with nodes")
    nodes = state["nodes"]
    if any(not isinstance(n, Mapping) or not isinstance(n.get("id"), str) for n in nodes):
        raise LeanExportError("malformed research node")
    return {n["id"]: dict(n) for n in nodes}


def _require_type(actual: str, expected: str | None, node_id: str) -> str:
    if expected is not None and actual != expected:
        raise LeanExportError(f"node {node_id}: expected {expected} term, got {actual}")
    return actual


def _infer_term(
    term: Any,
    *,
    node_id: str,
    bindings: Mapping[str, str],
    dependency_types: Mapping[str, str],
    input_types: dict[str, str],
    expected_nodes: dict[str, str],
    expected: str | None = None,
) -> str | None:
    if not isinstance(term, Mapping):
        raise LeanExportError(f"node {node_id}: term must be a Term JSON object")
    kind = term.get("type")
    if kind == "Lit":
        value = term.get("value")
        if type(value) is bool:
            return _require_type("Bool", expected, node_id)
        if type(value) is int:
            if not _I64_MIN <= value <= _I64_MAX:
                raise LeanExportError(
                    f"node {node_id}: integer literal is outside the native i64 range"
                )
            return _require_type("Int", expected, node_id)
        raise LeanExportError(
            f"node {node_id}: only Boolean and signed i64 Lit values are supported"
        )
    if kind == "Var":
        alias = term.get("name")
        if not isinstance(alias, str):
            raise LeanExportError(f"node {node_id}: Var.name must be a string")
        if alias in bindings:
            raise LeanExportError(
                f"node {node_id}: graph binding {alias!r} is a NodeID; use EvalSubgraph(node=Var(...))"
            )
        if expected is None:
            return None
        prior = input_types.get(alias)
        if prior is not None and prior != expected:
            raise LeanExportError(
                f"external variable {alias!r} is used as both {prior} and {expected}"
            )
        input_types[alias] = expected
        return expected
    if kind in {"And", "Or"}:
        items = term.get("items")
        if not isinstance(items, list):
            raise LeanExportError(f"node {node_id}: {kind}.items must be a list")
        for item in items:
            _infer_term(
                item,
                node_id=node_id,
                bindings=bindings,
                dependency_types=dependency_types,
                input_types=input_types,
                expected_nodes=expected_nodes,
                expected="Bool",
            )
        return _require_type("Bool", expected, node_id)
    if kind == "Not":
        _infer_term(
            term.get("arg"),
            node_id=node_id,
            bindings=bindings,
            dependency_types=dependency_types,
            input_types=input_types,
            expected_nodes=expected_nodes,
            expected="Bool",
        )
        return _require_type("Bool", expected, node_id)
    if kind == "IfThenElse":
        _infer_term(
            term.get("cond"),
            node_id=node_id,
            bindings=bindings,
            dependency_types=dependency_types,
            input_types=input_types,
            expected_nodes=expected_nodes,
            expected="Bool",
        )
        then_kind = _infer_term(
            term.get("then"),
            node_id=node_id,
            bindings=bindings,
            dependency_types=dependency_types,
            input_types=input_types,
            expected_nodes=expected_nodes,
            expected=expected,
        )
        other_kind = _infer_term(
            term.get("other"),
            node_id=node_id,
            bindings=bindings,
            dependency_types=dependency_types,
            input_types=input_types,
            expected_nodes=expected_nodes,
            expected=then_kind if then_kind is not None else expected,
        )
        if then_kind is None and other_kind is not None:
            then_kind = _infer_term(
                term.get("then"),
                node_id=node_id,
                bindings=bindings,
                dependency_types=dependency_types,
                input_types=input_types,
                expected_nodes=expected_nodes,
                expected=other_kind,
            )
        if then_kind is None or other_kind is None:
            return None
        _require_type(other_kind, then_kind, node_id)
        return _require_type(then_kind, expected, node_id)
    if kind == "Eq":
        left, right = term.get("a"), term.get("b")
        left_kind = _infer_term(
            left,
            node_id=node_id,
            bindings=bindings,
            dependency_types=dependency_types,
            input_types=input_types,
            expected_nodes=expected_nodes,
        )
        right_kind = _infer_term(
            right,
            node_id=node_id,
            bindings=bindings,
            dependency_types=dependency_types,
            input_types=input_types,
            expected_nodes=expected_nodes,
            expected=left_kind,
        )
        if left_kind is None and right_kind is not None:
            left_kind = _infer_term(
                left,
                node_id=node_id,
                bindings=bindings,
                dependency_types=dependency_types,
                input_types=input_types,
                expected_nodes=expected_nodes,
                expected=right_kind,
            )
        if left_kind is None or right_kind is None:
            raise LeanExportError(
                f"node {node_id}: Eq operands do not determine an unambiguous Bool or Int type"
            )
        _require_type(right_kind, left_kind, node_id)
        return _require_type("Bool", expected, node_id)
    if kind == "EvalSubgraph":
        ref = term.get("node")
        if (
            not isinstance(ref, Mapping)
            or ref.get("type") != "Var"
            or not isinstance(ref.get("name"), str)
        ):
            raise LeanExportError(f"node {node_id}: EvalSubgraph requires node=Var(alias)")
        alias = ref["name"]
        if alias not in bindings:
            raise LeanExportError(
                f"node {node_id}: EvalSubgraph alias {alias!r} has no graph binding"
            )
        target = bindings[alias]
        if target in dependency_types:
            return _require_type(dependency_types[target], expected, node_id)
        if expected is not None:
            prior = expected_nodes.get(target)
            if prior is not None and prior != expected:
                raise LeanExportError(
                    f"dependency {target!r} is constrained as both {prior} and {expected}"
                )
            expected_nodes[target] = expected
            return expected
        return None
    if kind in {"Plus", "Times"}:
        items = term.get("items")
        if not isinstance(items, list):
            raise LeanExportError(f"node {node_id}: {kind}.items must be a list")
        for item in items:
            _infer_term(
                item,
                node_id=node_id,
                bindings=bindings,
                dependency_types=dependency_types,
                input_types=input_types,
                expected_nodes=expected_nodes,
                expected="Int",
            )
        return _require_type("Int", expected, node_id)
    if kind == "Minus":
        _infer_term(
            term.get("a"),
            node_id=node_id,
            bindings=bindings,
            dependency_types=dependency_types,
            input_types=input_types,
            expected_nodes=expected_nodes,
            expected="Int",
        )
        _infer_term(
            term.get("b"),
            node_id=node_id,
            bindings=bindings,
            dependency_types=dependency_types,
            input_types=input_types,
            expected_nodes=expected_nodes,
            expected="Int",
        )
        return _require_type("Int", expected, node_id)
    if kind == "Pow":
        exponent = term.get("b")
        if (
            not isinstance(exponent, Mapping)
            or exponent.get("type") != "Lit"
            or type(exponent.get("value")) is not int
        ):
            raise LeanExportError(
                f"node {node_id}: Pow is supported only with a literal integer exponent in [0, 63]"
            )
        power = exponent["value"]
        if not 0 <= power < 64:
            raise LeanExportError(
                f"node {node_id}: native integer Pow requires exponent in [0, 63]"
            )
        _infer_term(
            term.get("a"),
            node_id=node_id,
            bindings=bindings,
            dependency_types=dependency_types,
            input_types=input_types,
            expected_nodes=expected_nodes,
            expected="Int",
        )
        return _require_type("Int", expected, node_id)
    if kind in {"Abs", "Neg"}:
        _infer_term(
            term.get("arg"),
            node_id=node_id,
            bindings=bindings,
            dependency_types=dependency_types,
            input_types=input_types,
            expected_nodes=expected_nodes,
            expected="Int",
        )
        return _require_type("Int", expected, node_id)
    if kind in {"Lt", "Lte", "Gt", "Gte"}:
        _infer_term(
            term.get("a"),
            node_id=node_id,
            bindings=bindings,
            dependency_types=dependency_types,
            input_types=input_types,
            expected_nodes=expected_nodes,
            expected="Int",
        )
        _infer_term(
            term.get("b"),
            node_id=node_id,
            bindings=bindings,
            dependency_types=dependency_types,
            input_types=input_types,
            expected_nodes=expected_nodes,
            expected="Int",
        )
        return _require_type("Bool", expected, node_id)
    raise LeanExportError(f"node {node_id}: unsupported term constructor {kind!r}")


def _compile_term(
    term: Any,
    *,
    node_id: str,
    bindings: Mapping[str, str],
    dependency_defs: Mapping[str, str],
    dependency_types: Mapping[str, str],
    input_types: Mapping[str, str],
) -> str:
    kind = term.get("type")
    if kind == "Lit":
        value = term["value"]
        if type(value) is bool:
            return "true" if value else "false"
        return str(value)
    if kind == "Var":
        alias = term["name"]
        return "input_" + _ident(alias)
    if kind in {"And", "Or"}:
        items = term["items"]
        if not items:
            return "true" if kind == "And" else "false"
        op = "&&" if kind == "And" else "||"
        return (
            "("
            + f" {op} ".join(
                _compile_term(
                    x,
                    node_id=node_id,
                    bindings=bindings,
                    dependency_defs=dependency_defs,
                    dependency_types=dependency_types,
                    input_types=input_types,
                )
                for x in items
            )
            + ")"
        )
    if kind == "Not":
        return (
            "(! "
            + _compile_term(
                term["arg"],
                node_id=node_id,
                bindings=bindings,
                dependency_defs=dependency_defs,
                dependency_types=dependency_types,
                input_types=input_types,
            )
            + ")"
        )
    if kind == "IfThenElse":
        cond = _compile_term(
            term["cond"],
            node_id=node_id,
            bindings=bindings,
            dependency_defs=dependency_defs,
            dependency_types=dependency_types,
            input_types=input_types,
        )
        then = _compile_term(
            term["then"],
            node_id=node_id,
            bindings=bindings,
            dependency_defs=dependency_defs,
            dependency_types=dependency_types,
            input_types=input_types,
        )
        other = _compile_term(
            term["other"],
            node_id=node_id,
            bindings=bindings,
            dependency_defs=dependency_defs,
            dependency_types=dependency_types,
            input_types=input_types,
        )
        return f"(if {cond} then {then} else {other})"
    if kind == "Eq":
        left = _compile_term(
            term["a"],
            node_id=node_id,
            bindings=bindings,
            dependency_defs=dependency_defs,
            dependency_types=dependency_types,
            input_types=input_types,
        )
        right = _compile_term(
            term["b"],
            node_id=node_id,
            bindings=bindings,
            dependency_defs=dependency_defs,
            dependency_types=dependency_types,
            input_types=input_types,
        )
        return f"({left} == {right})"
    if kind == "EvalSubgraph":
        alias = term["node"]["name"]
        target = bindings[alias]
        arg_names = []
        for name in sorted(input_types):
            arg_names.append("input_" + _ident(name))
            if input_types[name] == "Int":
                arg_names.append("input_" + _ident(name) + "_range")
        args = " " + " ".join(arg_names) if arg_names else ""
        return f"({dependency_defs[target]}{args})"
    if kind == "Plus":
        acc = "0"
        for item in term["items"]:
            value = _compile_term(
                item,
                node_id=node_id,
                bindings=bindings,
                dependency_defs=dependency_defs,
                dependency_types=dependency_types,
                input_types=input_types,
            )
            acc = f"(ocelotWrapI64 ({acc} + {value}))"
        return acc
    if kind == "Times":
        acc = "1"
        for item in term["items"]:
            value = _compile_term(
                item,
                node_id=node_id,
                bindings=bindings,
                dependency_defs=dependency_defs,
                dependency_types=dependency_types,
                input_types=input_types,
            )
            acc = f"(ocelotWrapI64 ({acc} * {value}))"
        return acc
    if kind == "Minus":
        a = _compile_term(
            term["a"],
            node_id=node_id,
            bindings=bindings,
            dependency_defs=dependency_defs,
            dependency_types=dependency_types,
            input_types=input_types,
        )
        b = _compile_term(
            term["b"],
            node_id=node_id,
            bindings=bindings,
            dependency_defs=dependency_defs,
            dependency_types=dependency_types,
            input_types=input_types,
        )
        return f"(ocelotWrapI64 ({a} - {b}))"
    if kind == "Pow":
        a = _compile_term(
            term["a"],
            node_id=node_id,
            bindings=bindings,
            dependency_defs=dependency_defs,
            dependency_types=dependency_types,
            input_types=input_types,
        )
        exponent = term["b"]["value"]
        return f"(ocelotWrapI64 ({a} ^ {exponent}))"
    if kind == "Neg":
        value = _compile_term(
            term["arg"],
            node_id=node_id,
            bindings=bindings,
            dependency_defs=dependency_defs,
            dependency_types=dependency_types,
            input_types=input_types,
        )
        return f"(ocelotWrapI64 (-{value}))"
    if kind == "Abs":
        value = _compile_term(
            term["arg"],
            node_id=node_id,
            bindings=bindings,
            dependency_defs=dependency_defs,
            dependency_types=dependency_types,
            input_types=input_types,
        )
        return f"(if {value} < 0 then ocelotWrapI64 (-{value}) else {value})"
    if kind in {"Lt", "Lte", "Gt", "Gte"}:
        a = _compile_term(
            term["a"],
            node_id=node_id,
            bindings=bindings,
            dependency_defs=dependency_defs,
            dependency_types=dependency_types,
            input_types=input_types,
        )
        b = _compile_term(
            term["b"],
            node_id=node_id,
            bindings=bindings,
            dependency_defs=dependency_defs,
            dependency_types=dependency_types,
            input_types=input_types,
        )
        op = {"Lt": "<", "Lte": "≤", "Gt": ">", "Gte": "≥"}[kind]
        return f"(decide ({a} {op} {b}))"
    raise LeanExportError(f"node {node_id}: unsupported term constructor {kind!r}")


def export_lean(graph: Any, target: str | None = None) -> str:
    """Render terms in ``graph`` as Lean definitions.

    With ``target``, emit only that node and its transitive ``derived_from``
    dependencies. Without a target, emit every term-bearing node. Each node's
    ``bindings`` map is alias -> research node id; every binding must also have
    a directed ``derived_from`` edge from the using node to the referenced node.
    """
    state = graph.state() if hasattr(graph, "state") else graph
    nodes = _node_index(state)
    edges = state.get("edges", []) if isinstance(state, Mapping) else []
    derived = {
        (e.get("src"), e.get("tgt"))
        for e in edges
        if isinstance(e, Mapping) and e.get("type") == "derived_from"
    }
    term_nodes = {nid for nid, node in nodes.items() if "term" in node}
    if target is not None and target not in nodes:
        raise LeanExportError(f"unknown target research node {target!r}")
    if target is not None and target not in term_nodes:
        raise LeanExportError(f"target research node {target!r} has no explicit Boolean term")

    if target is None:
        selected = set(term_nodes)
    else:
        selected: set[str] = set()
        pending = [target]
        while pending:
            nid = pending.pop()
            if nid in selected:
                continue
            selected.add(nid)
            node = nodes[nid]
            bindings = node.get("bindings", {})
            if not isinstance(bindings, Mapping):
                raise LeanExportError(f"node {nid}: bindings must be an alias-to-node-id mapping")
            for dep in bindings.values():
                if not isinstance(dep, str) or dep not in nodes:
                    raise LeanExportError(
                        f"node {nid}: binding references unknown research node {dep!r}"
                    )
                pending.append(dep)
        selected = {nid for nid in selected if nid in term_nodes}

    # Topologically order dependencies before users and reject cycles.
    ordered: list[str] = []
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(nid: str) -> None:
        if nid in visited:
            return
        if nid in visiting:
            raise LeanExportError(f"dependency cycle reaches research node {nid}")
        visiting.add(nid)
        node = nodes[nid]
        bindings = node.get("bindings", {})
        if not isinstance(bindings, Mapping):
            raise LeanExportError(f"node {nid}: bindings must be an alias-to-node-id mapping")
        for alias, dep in bindings.items():
            if not isinstance(alias, str) or not isinstance(dep, str):
                raise LeanExportError(f"node {nid}: bindings must map string aliases to node ids")
            if (nid, dep) not in derived:
                raise LeanExportError(
                    f"node {nid}: binding {alias!r} to {dep!r} lacks derived_from edge"
                )
            if dep not in nodes:
                raise LeanExportError(
                    f"node {nid}: binding {alias!r} references unknown node {dep!r}"
                )
            if dep not in term_nodes:
                raise LeanExportError(f"node {nid}: bound dependency {dep!r} has no explicit term")
            if dep in selected:
                visit(dep)
        visiting.remove(nid)
        visited.add(nid)
        ordered.append(nid)

    for nid in sorted(selected):
        visit(nid)

    input_types: dict[str, str] = {}
    node_types: dict[str, str] = {}
    expected_node_types: dict[str, str] = {}
    bound_aliases: set[str] = set()
    for nid in ordered:
        bound_aliases.update(nodes[nid].get("bindings", {}))
    # Iterate from users toward dependencies. A context such as
    # `Not(EvalSubgraph(first))` constrains `first`; an outer bare
    # `EvalSubgraph(middle)` can then learn its type on the next pass.
    unresolved = set(ordered)
    while unresolved:
        progress = False
        for nid in reversed(ordered):
            if nid not in unresolved:
                continue
            node = nodes[nid]
            bindings = node.get("bindings", {})
            inferred_type = _infer_term(
                node["term"],
                node_id=nid,
                bindings=bindings,
                dependency_types=node_types,
                input_types=input_types,
                expected_nodes=expected_node_types,
                expected=expected_node_types.get(nid),
            )
            if inferred_type is not None:
                node_types[nid] = inferred_type
                unresolved.remove(nid)
                progress = True
        if not progress:
            unresolved_names = ", ".join(sorted(unresolved))
            raise LeanExportError(
                f"nodes have ambiguous standalone variable or dependency types: {unresolved_names}"
            )
    input_names = set(input_types)
    ambiguous_aliases = sorted(input_names.intersection(bound_aliases))
    if ambiguous_aliases:
        raise LeanExportError(
            "external aliases collide with graph binding aliases in the dependency chain: "
            + ", ".join(repr(alias) for alias in ambiguous_aliases)
        )
    lean_input_names: dict[str, str] = {}
    for alias in sorted(input_names):
        lean_name = _ident(alias)
        previous = lean_input_names.get(lean_name)
        if previous is not None and previous != alias:
            raise LeanExportError(
                f"external aliases {previous!r} and {alias!r} collide after Lean identifier sanitization"
            )
        lean_input_names[lean_name] = alias
    inputs = tuple(sorted(input_names))

    definitions: dict[str, str] = {}
    lines = [
        "-- Generated by ozagia.research.lean.",
        "-- These are translations of recorded Boolean/integer syntax, not domain proofs.",
        "-- Integer arithmetic definitions model signed i64 wrapping.",
        "-- Pow is translated only for Int bases and literal exponents in [0, 63].",
        "-- IfThenElse is translated only with a Bool guard and equal branch types.",
        "-- Native IEEE Float and exact Rational terms are not coerced to Lean Float or Real.",
        "-- The research helper reports integer overflow as unresolved; prove",
        "-- overflow-free intermediate bounds before comparing to helper runs.",
        "-- This syntax may be broader than the research helper's accepted input domain.",
        "-- Establish overflow-free preconditions before claiming equivalence on a domain.",
        "-- Graph status, evidence, and truth are not assessed by this export.",
        "",
    ]

    def has_numeric_operation(value: Any) -> bool:
        if isinstance(value, Mapping):
            if value.get("type") in {"Plus", "Times", "Minus", "Pow", "Neg", "Abs"}:
                return True
            return any(has_numeric_operation(v) for v in value.values())
        return isinstance(value, list) and any(has_numeric_operation(v) for v in value)

    if (
        "Int" in node_types.values()
        or "Int" in input_types.values()
        or any(has_numeric_operation(nodes[nid]["term"]) for nid in ordered)
    ):
        lines.extend(
            [
                "def ocelotWrapI64 (value : Int) : Int :=",
                "  let modulus : Int := 18446744073709551616",
                "  let unsigned := value % modulus",
                "  if unsigned >= 9223372036854775808 then unsigned - modulus else unsigned",
                "",
            ]
        )
    for ordinal, nid in enumerate(ordered):
        node = nodes[nid]
        bindings = node.get("bindings", {})
        # Graph ids form stable, unique names even if their sanitized forms collide.
        name = f"researchTerm_{ordinal}_{_ident(nid)}"
        expr = _compile_term(
            node["term"],
            node_id=nid,
            bindings=bindings,
            dependency_defs=definitions,
            dependency_types=node_types,
            input_types=input_types,
        )
        definitions[nid] = name
        params = " ".join(
            f"(input_{_ident(item)} : {input_types[item]})"
            + (
                f" (input_{_ident(item)}_range : ({_I64_MIN} : Int) ≤ input_{_ident(item)} ∧ input_{_ident(item)} ≤ {_I64_MAX})"
                if input_types[item] == "Int"
                else ""
            )
            for item in inputs
        )
        param_prefix = (" " + params) if params else ""
        lines.extend(
            [
                f"-- research_node_id: {_lean_string(nid)}",
                f"def {name}{param_prefix} : {node_types[nid]} := {expr}",
                f"def {name}_researchNodeId : String := {_lean_string(nid)}",
            ]
        )
        if bindings:
            pairs = ", ".join(
                f"({_lean_string(alias)}, {_lean_string(dep)})"
                for alias, dep in sorted(bindings.items())
            )
            lines.append(f"def {name}_bindings : List (String × String) := [{pairs}]")
        lines.append("")
    return "\n".join(lines)
