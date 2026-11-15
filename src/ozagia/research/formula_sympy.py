"""Optional SymPy/Lark formula interpretation candidates.

This adapter is deliberately separate from paper ingestion: parsing a formula
does not establish what its author meant, nor does it verify a mathematical
claim. SymPy is imported only when the adapter is called.
"""

from __future__ import annotations

import re
from importlib import metadata
from typing import Any


MAX_FORMULA_CHARS = 10_000
MAX_REPRESENTATION_CHARS = 20_000
MAX_ERROR_CHARS = 1_200


def _versions() -> dict[str, str | None]:
    found = {}
    for package in ("sympy", "lark"):
        try:
            found[package] = metadata.version(package)
        except metadata.PackageNotFoundError:
            found[package] = None
    return found


def _error_fields(exc: Exception) -> dict[str, str | bool]:
    detail = f"{type(exc).__name__}: {exc}"
    return {
        "exception_type": type(exc).__name__,
        "reason": detail[:MAX_ERROR_CHARS],
        "reason_truncated": len(detail) > MAX_ERROR_CHARS,
    }


def sympy_lark_candidate(formula: str) -> dict[str, Any]:
    """Return a bounded, explicitly unverified SymPy/Lark parse candidate.

    Missing optional dependencies and parser failures are normal outcomes.
    No Python evaluation is performed. SymPy may normalize/simplify parts of
    its expression tree, so ``srepr`` is a parser artifact, not source truth.
    """
    base: dict[str, Any] = {
        "adapter": "sympy_lark",
        "status": "unavailable",
        "verified": False,
        "is_claim": False,
        "semantics": "unverified_parser_candidate",
        "limitations": [
            "LaTeX parsing is heuristic and does not establish authorial meaning.",
            "SymPy may normalize expression structure; this output is not a proof or evaluation.",
        ],
    }
    if not isinstance(formula, str):
        return {**base, "status": "rejected", "reason": "formula must be text"}
    if len(formula) > MAX_FORMULA_CHARS:
        return {**base, "status": "rejected", "reason": "formula exceeds parser input limit"}
    if not formula.strip():
        return {**base, "status": "rejected", "reason": "formula is empty"}

    try:
        from sympy.parsing.latex import parse_latex
    except (ImportError, ModuleNotFoundError) as exc:
        return {
            **base,
            "reason": f"optional SymPy parser unavailable: {type(exc).__name__}",
            "parser_versions": _versions(),
        }
    versions = _versions()
    try:
        expression = parse_latex(formula, backend="lark")
    except Exception as exc:
        return {**base, "status": "unparsed", **_error_fields(exc), "parser_versions": versions}

    # Keep parser output bounded; this is data for review, never executable source.
    try:
        from sympy import srepr
        from lark import Tree

        ambiguous = isinstance(expression, Tree) and str(expression.data) == "_ambig"
        if ambiguous:
            alternatives = [srepr(item) for item in expression.children]
            alternatives = alternatives[:64]
            result_representation = None
        else:
            result_representation = srepr(expression)
            alternatives = []
    except Exception as exc:
        return {
            **base,
            "status": "representation_failed",
            **_error_fields(exc),
            "parser_versions": versions,
            "parse_succeeded": True,
        }

    ambiguities = []
    if re.search(r"(?<![A-Za-z\\])[A-Za-z]{2,}(?![A-Za-z])", formula):
        ambiguities.append("multi-letter names may be products or single symbols")
    if re.search(r"(?<!\\)[A-Za-z](?:\s*)[A-Za-z](?![A-Za-z])", formula):
        ambiguities.append("adjacent symbols may imply multiplication")
    parse_ambiguous = ambiguous
    if parse_ambiguous:
        ambiguities.append("Lark returned multiple parses; each alternative is preserved below")
    status = "ambiguous" if ambiguities else "parsed"
    output = {
        **base,
        "status": status,
        "parser_versions": versions,
        "ambiguity": ambiguities,
        "has_alternative_parse_tree": parse_ambiguous,
    }
    if parse_ambiguous:
        output["alternatives"] = [item[:MAX_REPRESENTATION_CHARS] for item in alternatives]
        output["alternatives_truncated"] = len(expression.children) > len(alternatives) or any(
            len(item) > MAX_REPRESENTATION_CHARS for item in alternatives
        )
    else:
        output["expression_repr"] = result_representation[:MAX_REPRESENTATION_CHARS]
        output["representation_truncated"] = len(result_representation) > MAX_REPRESENTATION_CHARS
    return output
