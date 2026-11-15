"""Strict translator for formulas with finite-domain quantifiers.

Quantifiers use ``forall(x, D, P)`` and ``exists(x, D, P)``. The strict
Unicode/LaTeX form ``∀x ∈ D: (P)`` / ``\\forall x \\in D: (P)`` is also
accepted; its predicate must be parenthesized. ``D`` names a finite collection in
the runtime environment. The third argument is a Boolean formula. No calls
other than these two binder forms are accepted and source text is never run.

All quantification is over the supplied collection as it exists at evaluation
time. Universal quantification uses the substrate's native ``Exists`` term
under negation, so its empty-domain result is true.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from typing import Any

from ozagia.research.terms import (
    And,
    Eq,
    Exists,
    Gt,
    Gte,
    Lit,
    Lt,
    Lte,
    Minus,
    Not,
    Or,
    Plus,
    Times,
    Var,
    term_to_json,
)


class FiniteFormulaError(ValueError):
    """Unsupported finite formula syntax."""


@dataclass(frozen=True)
class FiniteTranslation:
    term: dict[str, Any]
    variables: tuple[str, ...]
    variable_sorts: dict[str, str]
    equality_sort_constraints: tuple[tuple[str, str], ...]
    domains: tuple[str, ...]
    domain_sorts: dict[str, str]
    quantifier_domains: tuple[str, ...]
    normalized_source: str
    result_sort: str = "bool"
    residuals: tuple[str, ...] = ()
    semantics: str = "quantifiers range over runtime finite collections named by domain variables"


_NAME = re.compile(r"^[A-Za-z_]\w*$")
_ALIASES = (
    (r"\land", " and "),
    (r"\lor", " or "),
    (r"\neg", " not "),
    (r"\forall", "forall"),
    (r"\exists", "exists"),
    (r"\in", " in "),
    (r"\leq", " <= "),
    (r"\geq", " >= "),
    (r"\neq", " != "),
    (r"\times", " * "),
)


def _emit(
    node: ast.AST,
    free: set[str],
    domains: set[str],
    binders: dict[str, tuple[str, str]] | None = None,
):
    binders = binders or {}
    if isinstance(node, ast.Name):
        if node.id in ("True", "False"):
            return Lit(node.id == "True")
        if not _NAME.fullmatch(node.id):
            raise FiniteFormulaError(f"unsupported name: {node.id}")
        if node.id not in binders:
            free.add(node.id)
        return Var(node.id)
    if isinstance(node, ast.Constant) and type(node.value) in (int, bool):
        return Lit(node.value)
    if isinstance(node, ast.BoolOp) and isinstance(node.op, (ast.And, ast.Or)):
        terms = tuple(_emit(n, free, domains, binders) for n in node.values)
        return And(terms) if isinstance(node.op, ast.And) else Or(terms)
    if isinstance(node, ast.UnaryOp):
        if isinstance(node.op, ast.Not):
            return Not(_emit(node.operand, free, domains, binders))
        if isinstance(node.op, ast.USub):
            return Minus(Lit(0), _emit(node.operand, free, domains, binders))
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult)):
        a, b = _emit(node.left, free, domains, binders), _emit(node.right, free, domains, binders)
        return (
            Minus(a, b)
            if isinstance(node.op, ast.Sub)
            else (Plus((a, b)) if isinstance(node.op, ast.Add) else Times((a, b)))
        )
    if isinstance(node, ast.Compare) and len(node.ops) == len(node.comparators) == 1:
        a, b = (
            _emit(node.left, free, domains, binders),
            _emit(node.comparators[0], free, domains, binders),
        )
        op = node.ops[0]
        if isinstance(op, (ast.Eq, ast.NotEq)):
            eq = Eq(a, b)
            return Not(eq) if isinstance(op, ast.NotEq) else eq
        ctor = {ast.Lt: Lt, ast.LtE: Lte, ast.Gt: Gt, ast.GtE: Gte}.get(type(op))
        if ctor:
            return ctor(a, b)
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in ("forall", "exists")
    ):
        if len(node.args) != 3 or node.keywords:
            raise FiniteFormulaError(
                "quantifier syntax is forall(variable, domain, predicate) or exists(variable, domain, predicate)"
            )
        binder, domain, pred = node.args
        if not isinstance(binder, ast.Name) or not _NAME.fullmatch(binder.id):
            raise FiniteFormulaError("quantifier binder must be a simple variable name")
        if not isinstance(domain, ast.Name) or not _NAME.fullmatch(domain.id):
            raise FiniteFormulaError("quantifier domain must be a named finite collection")
        if domain.id in binders:
            raise FiniteFormulaError("a quantifier domain cannot refer to a bound variable")
        domains.add(domain.id)
        source = Var(domain.id)
        scope = dict(binders)
        scope[binder.id] = (domain.id, binder.id)
        predicate = _emit(pred, free, domains, scope)
        exists = Exists(source, binder.id, predicate)
        return (
            Not(Exists(source, binder.id, Not(predicate))) if node.func.id == "forall" else exists
        )
    raise FiniteFormulaError(
        f"unsupported syntax: {type(node).__name__}: {ast.dump(node, include_attributes=False)}"
    )


def translate_finite_formula(source: str) -> FiniteTranslation:
    """Compile supported finite-domain formula syntax to executable Term JSON."""
    if not isinstance(source, str) or not source.strip():
        raise FiniteFormulaError("formula must be a non-empty string")
    text = source.strip()
    if text.startswith("$") or text.endswith("$"):
        if not (text.startswith("$") and text.endswith("$") and len(text) >= 2):
            raise FiniteFormulaError("unbalanced math delimiters")
        text = text[1:-1]
    text = _normalize(text)
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError as exc:
        raise FiniteFormulaError(f"invalid formula syntax: {exc.msg}") from exc
    for literal in ast.walk(tree.body):
        if (
            isinstance(literal, ast.Constant)
            and type(literal.value) is int
            and not -(2**63) <= literal.value < 2**63
        ):
            raise FiniteFormulaError("integer literal is outside native i64 range")
    free: set[str] = set()
    domains: set[str] = set()
    quantifier_domains: list[str] = []

    def record_quantifiers(n: ast.AST) -> None:
        if (
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id in ("forall", "exists")
            and len(n.args) == 3
            and isinstance(n.args[1], ast.Name)
        ):
            quantifier_domains.append(n.args[1].id)
        for child in ast.iter_child_nodes(n):
            record_quantifiers(child)

    record_quantifiers(tree.body)
    term = _emit(tree.body, free, domains)
    # Infer scalar requirements, including the element sort required of each
    # finite domain. Unknown equality operands remain explicit constraints.
    sorts: dict[str, str] = {}
    domain_sorts: dict[str, str] = {}
    equalities: list[tuple[str, str]] = []

    def infer(
        n: ast.AST, expected: str = "unknown", bind: dict[str, tuple[str, str]] | None = None
    ) -> str:
        bind = bind or {}

        def req(actual: str) -> str:
            if expected != "unknown" and actual != "unknown" and actual != expected:
                raise FiniteFormulaError(
                    f"expression requires {expected}, but has sort {actual}: {ast.dump(n, include_attributes=False)}"
                )
            return actual

        if isinstance(n, ast.Name):
            if n.id in ("True", "False"):
                return req("bool")
            if n.id in bind:
                dom, binder = bind[n.id]
                if expected != "unknown":
                    old = domain_sorts.setdefault(dom, expected)
                    if old != expected:
                        raise FiniteFormulaError(
                            f"domain {dom!r} is required to contain both {old} and {expected} values"
                        )
                return req(domain_sorts.get(dom, "unknown"))
            if expected != "unknown":
                old = sorts.setdefault(n.id, expected)
                if old != expected:
                    raise FiniteFormulaError(
                        f"variable {n.id!r} is required as both {old} and {expected}"
                    )
            return req(sorts.get(n.id, "unknown"))
        if isinstance(n, ast.Constant):
            return req("bool" if type(n.value) is bool else "int")
        if isinstance(n, ast.UnaryOp):
            want = "bool" if isinstance(n.op, ast.Not) else "int"
            infer(n.operand, want, bind)
            return req(want)
        if isinstance(n, ast.BinOp):
            if not isinstance(n.op, (ast.Add, ast.Sub, ast.Mult)):
                raise FiniteFormulaError("unsupported arithmetic operator")
            infer(n.left, "int", bind)
            infer(n.right, "int", bind)
            return req("int")
        if isinstance(n, ast.BoolOp):
            for x in n.values:
                infer(x, "bool", bind)
            return req("bool")
        if isinstance(n, ast.Compare):
            op = n.ops[0]
            if isinstance(op, (ast.Eq, ast.NotEq)):
                a, b = n.left, n.comparators[0]
                sa = infer(a, "unknown", bind)
                sb = infer(b, "unknown", bind)
                if sa != "unknown" and sb != "unknown" and sa != sb:
                    raise FiniteFormulaError("equality operands have incompatible sorts")
                want = sa if sa != "unknown" else sb
                infer(a, want, bind)
                infer(b, want, bind)
                if isinstance(a, ast.Name) and isinstance(b, ast.Name):
                    if a.id in bind and b.id in bind:
                        da, db = (
                            domain_sorts.get(bind[a.id][0], "unknown"),
                            domain_sorts.get(bind[b.id][0], "unknown"),
                        )
                        if da == "unknown" or db == "unknown":
                            raise FiniteFormulaError(
                                "ambiguous equality between quantified variables; constrain their sorts"
                            )
                    elif a.id in bind or b.id in bind:
                        bound_name, free_name = (a, b) if a.id in bind else (b, a)
                        ds, fs = (
                            domain_sorts.get(bind[bound_name.id][0], "unknown"),
                            sorts.get(free_name.id, "unknown"),
                        )
                        if ds == "unknown" and fs == "unknown":
                            raise FiniteFormulaError(
                                "ambiguous equality between quantified and free variables; constrain their sorts"
                            )
                        if ds != "unknown" and fs != "unknown" and ds != fs:
                            raise FiniteFormulaError(
                                "equality operands have incompatible inferred sorts"
                            )
                        if ds == "unknown":
                            domain_sorts[bind[bound_name.id][0]] = fs
                        if fs == "unknown":
                            sorts[free_name.id] = ds
                    elif a.id in free and b.id in free:
                        equalities.append((a.id, b.id))
            else:
                infer(n.left, "int", bind)
                infer(n.comparators[0], "int", bind)
            return req("bool")
        if (
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id in ("forall", "exists")
        ):
            binder, domain, pred = n.args
            scope = dict(bind)
            scope[binder.id] = (domain.id, binder.id)
            infer(pred, "bool", scope)
            return req("bool")
        raise FiniteFormulaError("unsupported sort expression")

    infer(tree.body, "bool")
    # Equality between free variables requires equal runtime sorts.
    for a, b in equalities:
        if a in sorts and b in sorts and sorts[a] != sorts[b]:
            raise FiniteFormulaError("equality operands have incompatible inferred sorts")
    constraints = tuple(sorted(set((a, b) for a, b in equalities if a != b)))
    residuals = tuple(
        f"variable {v!r} has unknown required sort"
        for v in sorted(free)
        if sorts.get(v, "unknown") == "unknown"
    )
    residuals += tuple(
        f"domain {d!r} must contain homogeneous {domain_sorts.get(d, 'unknown')} values and at most {MAX_DOMAIN_SIZE} items"
        for d in sorted(domains)
    )
    if any(
        isinstance(n, ast.BinOp) or isinstance(n, ast.UnaryOp) and isinstance(n.op, ast.USub)
        for n in ast.walk(tree.body)
    ):
        residuals += (
            "call validate_environment before native evaluation to prove quantified integer arithmetic stays within i64",
        )
    return FiniteTranslation(
        term_to_json(term),
        tuple(sorted(free)),
        {v: sorts.get(v, "unknown") for v in sorted(free)},
        constraints,
        tuple(sorted(domains)),
        {d: domain_sorts.get(d, "unknown") for d in sorted(domains)},
        tuple(quantifier_domains),
        text,
        "bool",
        residuals,
    )


def _normalize(text: str) -> str:
    for token, replacement in _ALIASES:
        text = re.sub(re.escape(token) + r"(?![A-Za-z_])", replacement, text)
    text = text.translate(
        str.maketrans(
            {
                "∧": " and ",
                "∨": " or ",
                "¬": " not ",
                "≤": " <= ",
                "≥": " >= ",
                "≠": " != ",
                "∀": "forall ",
                "∃": "exists ",
                "∈": " in ",
            }
        )
    )
    text = re.sub(r"(?<![<>=!])=(?!=)", "==", text)
    return _rewrite_binders(text)


def _rewrite_binders(text: str) -> str:
    """Rewrite strict ``forall x in D: (P)`` notation to call syntax."""
    prefix = re.compile(r"\b(forall|exists)\s+([A-Za-z_]\w*)\s+in\s+([A-Za-z_]\w*)\s*:\s*\(")
    while match := prefix.search(text):
        depth = 1
        i = match.end()
        start = i
        while i < len(text) and depth:
            if text[i] == "(":
                depth += 1
            elif text[i] == ")":
                depth -= 1
            i += 1
        if depth:
            raise FiniteFormulaError("quantifier predicate parentheses are unbalanced")
        body = _rewrite_binders(text[start : i - 1])
        replacement = f"{match.group(1)}({match.group(2)}, {match.group(3)}, ({body}))"
        text = text[: match.start()] + replacement + text[i:]
    return text


MAX_DOMAIN_SIZE = 10_000
MAX_EVALUATION_WORK = 10_000
MIN_I64, MAX_I64 = -(2**63), 2**63 - 1


def validate_environment(translation: FiniteTranslation, env: dict[str, Any]) -> None:
    """Validate finite-domain inputs before native evaluation; raises ValueError."""
    required = set(translation.variables) | set(translation.domains)
    missing = sorted(required - set(env))
    if missing:
        raise ValueError(f"missing formula inputs: {', '.join(missing)}")
    for name, expected in translation.variable_sorts.items():
        value = env[name]
        actual = "bool" if type(value) is bool else "int" if type(value) is int else "other"
        if actual not in ("bool", "int"):
            raise ValueError(f"variable {name!r} must be Boolean or integer")
        if expected != "unknown" and actual != expected:
            raise ValueError(f"variable {name!r} must have sort {expected}")
        if actual == "int" and not MIN_I64 <= value <= MAX_I64:
            raise ValueError(f"variable {name!r} is outside native i64 range")
    for left, right in translation.equality_sort_constraints:
        lval, rval = env[left], env[right]
        if type(lval) is not type(rval):
            raise ValueError(f"variables {left!r} and {right!r} must have the same runtime sort")
    cardinalities = {}
    for domain, expected in translation.domain_sorts.items():
        values = env.get(domain)
        if not isinstance(values, (tuple, list, set, frozenset)):
            raise ValueError(f"domain {domain!r} must be a finite tuple, list, or set")
        if len(values) > MAX_DOMAIN_SIZE:
            raise ValueError(f"domain {domain!r} exceeds {MAX_DOMAIN_SIZE} items")
        cardinalities[domain] = len(values)
        actual = {
            "bool" if type(v) is bool else "int" if type(v) is int else "other" for v in values
        }
        if len(actual) > 1 or (actual and next(iter(actual)) not in ("int", "bool")):
            raise ValueError(
                f"domain {domain!r} must contain homogeneous Boolean or integer values"
            )
        if expected != "unknown" and actual and next(iter(actual)) != expected:
            raise ValueError(f"domain {domain!r} must contain {expected} values")
        if any(type(value) is int and not MIN_I64 <= value <= MAX_I64 for value in values):
            raise ValueError(f"domain {domain!r} contains an integer outside native i64 range")
    work = 1
    for domain in translation.quantifier_domains:
        work *= cardinalities[domain]
        if work > MAX_EVALUATION_WORK:
            raise ValueError(f"quantified evaluation work exceeds {MAX_EVALUATION_WORK}")
    # The native integer terms use wrapping i64 arithmetic. Prove that every
    # arithmetic subexpression stays in range over the supplied scalar inputs
    # and each finite binder's value interval before allowing execution.
    tree = ast.parse(translation.normalized_source, mode="eval").body

    def interval(node: ast.AST, bind: dict[str, str]) -> tuple[int, int]:
        if isinstance(node, ast.Constant) and type(node.value) is int:
            return node.value, node.value
        if isinstance(node, ast.Name):
            values = env[bind[node.id]] if node.id in bind else (env[node.id],)
            if not values or any(type(v) is not int for v in values):
                raise ValueError("integer arithmetic requires non-empty integer inputs")
            return min(values), max(values)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            lo, hi = interval(node.operand, bind)
            return -hi, -lo
        if isinstance(node, ast.BinOp):
            alo, ahi = interval(node.left, bind)
            blo, bhi = interval(node.right, bind)
            if isinstance(node.op, ast.Add):
                return alo + blo, ahi + bhi
            if isinstance(node.op, ast.Sub):
                return alo - bhi, ahi - blo
            if isinstance(node.op, ast.Mult):
                vals = (alo * blo, alo * bhi, ahi * blo, ahi * bhi)
                return min(vals), max(vals)
        if isinstance(node, ast.BoolOp):
            for child in node.values:
                check(child, bind)
        elif isinstance(node, ast.UnaryOp):
            check(node.operand, bind)
        elif isinstance(node, ast.Compare):
            check(node.left, bind)
            for child in node.comparators:
                check(child, bind)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            binder, domain, predicate = node.args
            values = env[domain.id]
            if values:
                scope = dict(bind)
                scope[binder.id] = domain.id
                check(predicate, scope)
        return 0, 0

    def check(node: ast.AST, bind: dict[str, str]) -> None:
        if isinstance(node, (ast.BinOp, ast.UnaryOp)) and (
            isinstance(node, ast.BinOp) or isinstance(node.op, ast.USub)
        ):
            lo, hi = interval(node, bind)
            if lo < MIN_I64 or hi > MAX_I64:
                raise ValueError("integer arithmetic may overflow native i64")
            if isinstance(node, ast.BinOp):
                check(node.left, bind)
                check(node.right, bind)
            else:
                check(node.operand, bind)
        elif isinstance(node, ast.BoolOp):
            for child in node.values:
                check(child, bind)
        elif isinstance(node, ast.UnaryOp):
            check(node.operand, bind)
        elif isinstance(node, ast.Compare):
            check(node.left, bind)
            for child in node.comparators:
                check(child, bind)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            binder, domain, predicate = node.args
            if env[domain.id]:
                scope = dict(bind)
                scope[binder.id] = domain.id
                check(predicate, scope)

    check(tree, {})


__all__ = [
    "FiniteFormulaError",
    "FiniteTranslation",
    "translate_finite_formula",
    "validate_environment",
    "MAX_DOMAIN_SIZE",
    "MAX_EVALUATION_WORK",
]
