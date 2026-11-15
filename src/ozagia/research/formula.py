"""Translate a strict numeric and Boolean formula subset to native Terms.

Supports finite decimal and i64 literals, arithmetic + - * /, nonnegative
integer-literal powers, ``abs``/``min``/``max``, Boolean logic and single comparisons, plus selected
Unicode and LaTeX operators and balanced LaTeX fractions. Division by zero
is represented as an unresolved null. Static scalar indexing such as ``x[2]``
maps to variable ``x_2``. Common presentation wrappers (``\\boxed``,
``\\mathrm``, ``\\text`` and math spacing) are normalized; Greek names and
simple symbolic subscripts are retained as opaque identifiers. Quantifiers use the separate finite
formula translator. Unsupported and ambiguous syntax is refused.

Sort metadata is an adapter-side requirement. Floating arithmetic uses native
floating-point semantics; the research execution path checks for i64 overflow
and non-finite intermediates. A translated expression is a model candidate,
not evidence that it captures a paper's intended domains or meaning.
"""

from __future__ import annotations

import ast
import io
import keyword
import math
import re
import tokenize
from dataclasses import dataclass
from collections.abc import Mapping, Sequence
from typing import Any

from ozagia.research.terms import (
    Abs,
    And,
    Div,
    Eq,
    Gt,
    Gte,
    IfThenElse,
    Lit,
    Lt,
    Lte,
    Max,
    Min,
    Minus,
    Not,
    Or,
    Plus,
    Pow,
    Times,
    Var,
    term_to_json,
)


class FormulaTranslationError(ValueError):
    """Malformed or unsupported formula; ``residuals`` names the refusal."""

    def __init__(self, message: str, residuals: tuple[str, ...] = ()):
        super().__init__(message)
        self.residuals = residuals


@dataclass(frozen=True)
class Translation:
    term: dict[str, Any]
    variables: tuple[str, ...]
    variable_sorts: dict[str, str]
    result_sort: str
    equality_sort_constraints: tuple[tuple[str, str], ...]
    residuals: tuple[str, ...] = ()
    preconditions: tuple[dict[str, Any], ...] = ()


_LATEX = (
    (r"\land", " and "),
    (r"\lor", " or "),
    (r"\neg", " not "),
    (r"\times", " * "),
    (r"\leq", " <= "),
    (r"\le", " <= "),
    (r"\geq", " >= "),
    (r"\ge", " >= "),
    (r"\neq", " != "),
    (r"\cdot", " * "),
    (r"\div", " / "),
)
_UNICODE = str.maketrans(
    {"∧": " and ", "∨": " or ", "¬": " not ", "×": " * ", "≤": " <= ", "≥": " >= ", "≠": " != "}
)
_NAME = re.compile(r"^[A-Za-z_]\w*$")
_I64_MIN = -(1 << 63)
_I64_MAX = (1 << 63) - 1


class _IndexedScalarNames(ast.NodeTransformer):
    """Map static scalar subscripts such as ``x[2]`` to the variable ``x_2``."""

    def visit_Subscript(self, node):
        node = self.generic_visit(node)
        if (
            isinstance(node.value, ast.Name)
            and isinstance(node.slice, ast.Constant)
            and type(node.slice.value) is int
            and node.slice.value >= 0
        ):
            return ast.copy_location(
                ast.Name(id=f"{node.value.id}_{node.slice.value}", ctx=ast.Load()), node
            )
        raise FormulaTranslationError(
            "only non-negative literal scalar indices are supported",
            (ast.dump(node, include_attributes=False),),
        )


def _latex_fractions(source: str) -> str:
    """Lower balanced ``\\frac{numerator}{denominator}`` groups recursively."""
    marker = r"\frac"
    while marker in source:
        start = source.find(marker)
        pos = start + len(marker)
        groups = []
        for _ in range(2):
            while pos < len(source) and source[pos].isspace():
                pos += 1
            if pos >= len(source) or source[pos] != "{":
                raise FormulaTranslationError("malformed LaTeX fraction", (source[start:],))
            depth, end = 1, pos + 1
            while end < len(source) and depth:
                depth += (source[end] == "{") - (source[end] == "}")
                end += 1
            if depth:
                raise FormulaTranslationError("unclosed LaTeX fraction group", (source[start:],))
            groups.append(source[pos + 1 : end - 1])
            pos = end
        replacement = f"(({groups[0]}) / ({groups[1]}))"
        source = source[:start] + replacement + source[pos:]
    return source


_GREEK = {
    "alpha": "alpha",
    "beta": "beta",
    "gamma": "gamma",
    "delta": "delta",
    "epsilon": "epsilon",
    "varepsilon": "varepsilon",
    "zeta": "zeta",
    "eta": "eta",
    "theta": "theta",
    "vartheta": "vartheta",
    "iota": "iota",
    "kappa": "kappa",
    "lambda": "lambda",
    "mu": "mu",
    "nu": "nu",
    "xi": "xi",
    "pi": "pi",
    "varpi": "varpi",
    "rho": "rho",
    "varrho": "varrho",
    "sigma": "sigma",
    "tau": "tau",
    "upsilon": "upsilon",
    "phi": "phi",
    "varphi": "varphi",
    "chi": "chi",
    "psi": "psi",
    "omega": "omega",
    "Gamma": "Gamma",
    "Delta": "Delta",
    "Theta": "Theta",
    "Lambda": "Lambda",
    "Xi": "Xi",
    "Pi": "Pi",
    "Sigma": "Sigma",
    "Upsilon": "Upsilon",
    "Phi": "Phi",
    "Psi": "Psi",
    "Omega": "Omega",
}


def _unwrap_outer(source: str, command: str) -> tuple[str, bool]:
    """Unwrap command{...} only when its balanced group covers the whole input."""
    prefix = "\\" + command + "{"
    if not source.startswith(prefix):
        return source, False
    depth = 0
    for pos in range(len(prefix) - 1, len(source)):
        if source[pos] == "{":
            depth += 1
        elif source[pos] == "}":
            depth -= 1
            if depth == 0:
                if pos == len(source) - 1:
                    return source[len(prefix) : pos], True
                return source, False
    raise FormulaTranslationError(f"unclosed \\{command} group", (source,))


def _normalize_latex(source: str) -> tuple[str, bool]:
    """Normalize presentation-only LaTeX while preserving symbolic tokens."""
    text = source.strip()
    # Unwrap exactly one complete math delimiter pair; reject unmatched forms.
    for left, right in (("$$", "$$"), ("$", "$"), (r"\[", r"\]"), (r"\(", r"\)")):
        if text.startswith(left) or text.endswith(right):
            if not (
                text.startswith(left)
                and text.endswith(right)
                and len(text) >= len(left) + len(right)
            ):
                raise FormulaTranslationError("unbalanced math delimiters", (text,))
            text = text[len(left) : -len(right)].strip()
            break
    text = re.sub(r"\.\s*$", "", text).strip()
    if text.startswith(r"\boxed{") and text.endswith("},"):
        text = text[:-1].rstrip()
    was_boxed = False
    while True:
        text, unwrapped = _unwrap_outer(text, "boxed")
        was_boxed |= unwrapped
        if not unwrapped:
            break
    text = _latex_fractions(text)
    if re.search(r"\\(?:" + "|".join(map(re.escape, _GREEK)) + r")\s*\^\s*\{?[A-Za-z_]", text):
        raise FormulaTranslationError(
            "symbolic Greek superscript is ambiguous between an index and exponent",
            (text, "provide an explicit index or exponent interpretation"),
        )

    # These macros change typography only. Their contents become a string literal,
    # keeping domain labels distinct from variables and Python identifiers.
    def label(match: re.Match[str]) -> str:
        value = match.group(1).strip()
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9 _-]*", value):
            raise FormulaTranslationError("unsupported text/math label", (value,))
        return repr(value)

    text = re.sub(r"\\(?:mathrm|text)\{([^{}]*)\}", label, text)
    text = re.sub(r"\\operatorname\{([A-Za-z]+)\}", r"\1", text)
    text = re.sub(r"\\mathcal\s*\{([A-Za-z])\}", r"\1", text)
    text = re.sub(r"\\mathcal\s+(?=[A-Za-z])", "", text)
    # LaTeX spacing commands carry no mathematical operation.
    text = re.sub(r"\\(?:,|;|:|!|quad|qquad|enspace|thinspace)(?![A-Za-z])", "", text)
    # Greek control words are symbolic identifiers, not Python escapes.
    for macro, name in sorted(_GREEK.items(), key=lambda kv: -len(kv[0])):
        text = re.sub(r"\\" + re.escape(macro) + r"(?![A-Za-z])", name, text)
    # A simple braced subscript is an identifier suffix: U_{F} -> U_F.
    text = re.sub(r"\b([A-Za-z_]\w*)_\{([A-Za-z_]\w*|[0-9]+)\}", r"\1_\2", text)
    # Existing aliases, including LaTeX relation operators.
    text = text.translate(_UNICODE)
    for latex, replacement in _LATEX:
        text = re.sub(re.escape(latex) + r"(?![A-Za-z_])", replacement, text)
    if was_boxed:
        text = re.sub(r",\s*$", "", text)
    # TeX punctuation outside an expression is editorial. Periods are safe at
    # end; commas are stripped only from a complete boxed formula above.
    text = re.sub(r"\.\s*$", "", text).strip()
    return text, was_boxed


def _node_depth(node: ast.AST) -> int:
    children = list(ast.iter_child_nodes(node))
    return 0 if not children else 1 + max(_node_depth(child) for child in children)


def _implicit_product_tokens(
    source: str, mappings: Mapping[str, Sequence[str]] | None
) -> tuple[str, tuple[str, ...]]:
    """Expand selected whole NAME tokens to explicit multiplication chains."""
    if mappings is None:
        return source, ()
    if not isinstance(mappings, Mapping):
        raise FormulaTranslationError(
            "implicit_products must be a mapping of names to factor-name sequences",
            (type(mappings).__name__,),
        )
    checked: dict[str, tuple[str, ...]] = {}
    reserved = {"True", "False", "abs", "min", "max"}
    for name, factors in mappings.items():
        if (
            not isinstance(name, str)
            or not _NAME.fullmatch(name)
            or keyword.iskeyword(name)
            or name in reserved
        ):
            raise FormulaTranslationError(
                "implicit product keys must be ordinary variable names", (repr(name),)
            )
        if isinstance(factors, (str, bytes)) or not isinstance(factors, Sequence):
            raise FormulaTranslationError(
                "implicit product factors must be a sequence of variable names", (name,)
            )
        factor_tuple = tuple(factors)
        if len(factor_tuple) < 2 or any(
            not isinstance(f, str)
            or not _NAME.fullmatch(f)
            or keyword.iskeyword(f)
            or f in reserved
            for f in factor_tuple
        ):
            raise FormulaTranslationError(
                "implicit products require at least two ordinary variable-name factors",
                (name, repr(factors)),
            )
        if name in factor_tuple or any(f in mappings for f in factor_tuple):
            raise FormulaTranslationError(
                "recursive or nested implicit product mappings are not supported",
                (name, repr(factor_tuple)),
            )
        checked[name] = factor_tuple
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except tokenize.TokenError as exc:
        raise FormulaTranslationError(
            f"invalid token stream: {exc.args[0]}", (str(exc.args),)
        ) from exc
    rewritten: list[tuple[int, str]] = []
    used: list[str] = []
    for token in tokens:
        if token.type == tokenize.NAME and token.string in checked:
            name = token.string
            used.append(name)
            replacement = "*".join(checked[name])
            replacement_tokens = tokenize.generate_tokens(io.StringIO(replacement).readline)
            rewritten.extend(
                (part.type, part.string)
                for part in replacement_tokens
                if part.type not in (tokenize.ENDMARKER, tokenize.NEWLINE)
            )
        else:
            rewritten.append((token.type, token.string))
    return tokenize.untokenize(rewritten), tuple(sorted(set(used)))


def _emit(node: ast.AST, variables: set[str]) -> Any:
    if isinstance(node, ast.Name):
        if node.id in {"True", "False"}:
            return Lit(node.id == "True")
        if not _NAME.fullmatch(node.id):
            raise FormulaTranslationError(f"unsupported variable name: {node.id}", (node.id,))
        variables.add(node.id)
        return Var(node.id)
    if isinstance(node, ast.Constant) and type(node.value) in (int, float, bool, str):
        return Lit(node.value)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and not node.keywords:
        if not node.args or (node.func.id == "abs" and len(node.args) != 1):
            raise FormulaTranslationError(
                f"{node.func.id} has unsupported arity", (ast.dump(node, include_attributes=False),)
            )
        if node.func.id == "abs" and len(node.args) == 1:
            return Abs(_emit(node.args[0], variables))
        if node.func.id in {"min", "max"} and len(node.args) >= 2:
            items = tuple(_emit(arg, variables) for arg in node.args)
            return (Min if node.func.id == "min" else Max)(items)
        raise FormulaTranslationError(
            "uninterpreted function application has no native Term semantics",
            (ast.dump(node, include_attributes=False),),
        )
    if isinstance(node, ast.BoolOp) and isinstance(node.op, (ast.And, ast.Or)):
        items = tuple(_emit(v, variables) for v in node.values)
        return And(items) if isinstance(node.op, ast.And) else Or(items)
    if isinstance(node, ast.UnaryOp):
        if isinstance(node.op, ast.Not):
            return Not(_emit(node.operand, variables))
        if isinstance(node.op, ast.USub):
            if isinstance(node.operand, ast.Constant) and type(node.operand.value) in (int, float):
                return Lit(-node.operand.value)
            return Minus(Lit(0), _emit(node.operand, variables))
    if isinstance(node, ast.BinOp) and isinstance(
        node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow)
    ):
        ctor = {ast.Add: Plus, ast.Sub: Minus, ast.Mult: Times, ast.Div: Div, ast.Pow: Pow}[
            type(node.op)
        ]
        left, right = _emit(node.left, variables), _emit(node.right, variables)
        if ctor is Minus:
            return Minus(left, right)
        if ctor in (Div, Pow):
            return ctor(left, right)
        return ctor((left, right))
    if isinstance(node, ast.Compare) and len(node.ops) == len(node.comparators) == 1:
        ctor = {
            ast.Eq: Eq,
            ast.NotEq: lambda a, b: Not(Eq(a, b)),
            ast.Lt: Lt,
            ast.LtE: Lte,
            ast.Gt: Gt,
            ast.GtE: Gte,
        }.get(type(node.ops[0]))
        if ctor is not None:
            return ctor(_emit(node.left, variables), _emit(node.comparators[0], variables))
    raise FormulaTranslationError(
        f"unsupported syntax node: {type(node).__name__}",
        (ast.dump(node, include_attributes=False),),
    )


def _sorts(
    root: ast.AST, names: set[str]
) -> tuple[dict[str, str], str, tuple[tuple[str, str], ...]]:
    constraints: dict[str, str] = {}
    equal_names: list[tuple[str, str]] = []

    def intrinsic(node: ast.AST) -> str:
        if isinstance(node, ast.Name):
            if node.id in {"True", "False"}:
                return "bool"
            return "unknown"
        if isinstance(node, ast.Constant) and type(node.value) is bool:
            return "bool"
        if isinstance(node, ast.Constant) and type(node.value) is str:
            return "symbol"
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            return "float" if type(node.value) is float else "int"
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in {"abs", "min", "max"}
        ):
            return "number"
        if (
            isinstance(node, (ast.BinOp,))
            or isinstance(node, ast.UnaryOp)
            and isinstance(node.op, ast.USub)
        ):
            return "number"
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            return "bool"
        if isinstance(node, (ast.BoolOp, ast.Compare)):
            return "bool"
        return "unknown"

    def visit(node: ast.AST, expected: str = "unknown") -> str:
        def checked(actual: str) -> str:
            if expected == "number" and actual in {"int", "float", "number"}:
                return actual
            if expected != "unknown" and actual != expected:
                raise FormulaTranslationError(
                    f"expression requires {expected}, but has sort {actual}",
                    (ast.dump(node, include_attributes=False), expected, actual),
                )
            return actual

        if isinstance(node, ast.Name):
            if node.id in names and expected != "unknown":
                old = constraints.setdefault(node.id, expected)
                if old != expected:
                    raise FormulaTranslationError(
                        f"variable {node.id!r} is required as both {old} and {expected}",
                        (node.id, old, expected),
                    )
            return checked(constraints.get(node.id, "unknown"))
        if isinstance(node, ast.Constant):
            actual = (
                "bool"
                if type(node.value) is bool
                else "symbol"
                if type(node.value) is str
                else "float"
                if type(node.value) is float
                else "int"
            )
            if expected == "number" and actual in {"int", "float"}:
                return actual
            if expected != "unknown" and actual != expected:
                raise FormulaTranslationError(
                    f"expected {expected} literal, got {actual}", (repr(node.value),)
                )
            return checked(actual)
        if isinstance(node, ast.BinOp):
            left_sort = visit(node.left, "number")
            right_sort = visit(node.right, "number")
            result_sort = (
                "float"
                if isinstance(node.op, ast.Div) or "float" in (left_sort, right_sort)
                else "number"
            )
            return checked(result_sort)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id not in {"abs", "min", "max"}:
                return "unknown"
            for arg in node.args:
                visit(arg, "number")
            return checked("number")
        if isinstance(node, ast.UnaryOp):
            want = "bool" if isinstance(node.op, ast.Not) else "number"
            visit(node.operand, want)
            return checked(want)
        if isinstance(node, ast.BoolOp):
            for child in node.values:
                visit(child, "bool")
            return checked("bool")
        if isinstance(node, ast.Compare):
            op = node.ops[0]
            if isinstance(op, (ast.Eq, ast.NotEq)):
                left_sort, right_sort = intrinsic(node.left), intrinsic(node.comparators[0])
                numeric = {"int", "float", "number"}
                if left_sort in numeric and right_sort in numeric:
                    required = "number"
                else:
                    required = left_sort if left_sort != "unknown" else right_sort
                if (
                    left_sort != "unknown"
                    and right_sort != "unknown"
                    and left_sort != right_sort
                    and not (left_sort in numeric and right_sort in numeric)
                ):
                    raise FormulaTranslationError(
                        "equality operands have incompatible sorts", (left_sort, right_sort)
                    )
                if (
                    isinstance(node.left, ast.Name)
                    and node.left.id in names
                    and isinstance(node.comparators[0], ast.Name)
                    and node.comparators[0].id in names
                ):
                    equal_names.append((node.left.id, node.comparators[0].id))
            else:
                required = "number"
            visit(node.left, required)
            visit(node.comparators[0], required)
            return checked("bool")
        return "unknown"

    result = visit(root)
    # Equality between variables requires equal runtime sorts. Preserve that
    # constraint even when one side only becomes known elsewhere in the formula.
    changed = True
    while changed:
        changed = False
        for left, right in equal_names:
            lsort, rsort = constraints.get(left), constraints.get(right)
            if lsort and rsort and lsort != rsort:
                raise FormulaTranslationError(
                    "equality operands have incompatible inferred sorts",
                    (left, lsort, right, rsort),
                )
            if lsort and not rsort:
                constraints[right] = lsort
                changed = True
            elif rsort and not lsort:
                constraints[left] = rsort
                changed = True
    constraints_out = tuple(sorted(set((a, b) for a, b in equal_names if a != b)))
    return (
        {name: constraints.get(name, "unknown") for name in sorted(names)},
        result,
        constraints_out,
    )


def translate_formula(
    source: str, *, implicit_products: Mapping[str, Sequence[str]] | None = None
) -> Translation:
    """Translate a supported formula to Term JSON, reporting variables/residuals."""
    if not isinstance(source, str) or not source.strip():
        raise FormulaTranslationError("formula must be a non-empty string", ("empty input",))
    text, was_boxed = _normalize_latex(source)
    normalized = text.translate(_UNICODE)
    normalized = normalized.replace("^", "**")
    # A single math equality is accepted; preserve == and comparison operators.
    normalized = re.sub(r"(?<![<>=!])=(?!=)", "==", normalized).strip()
    normalized, used_products = _implicit_product_tokens(normalized, implicit_products)
    try:
        tree = ast.parse(normalized, mode="eval")
    except SyntaxError as exc:
        residual = text[exc.offset - 1 : exc.offset] if exc.offset else text
        raise FormulaTranslationError(f"invalid formula syntax: {exc.msg}", (residual,)) from exc
    tree = ast.fix_missing_locations(_IndexedScalarNames().visit(tree))
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and type(node.value) is float
            and not math.isfinite(node.value)
        ):
            raise FormulaTranslationError("float literal must be finite", (repr(node.value),))
        if isinstance(node, ast.Constant) and type(node.value) is int:
            parent = parents.get(node)
            allowed_negative_min = (
                isinstance(parent, ast.UnaryOp)
                and isinstance(parent.op, ast.USub)
                and node.value == (1 << 63)
            )
            if node.value > _I64_MAX and not allowed_negative_min:
                raise FormulaTranslationError(
                    "integer literal is outside the signed 64-bit range", (str(node.value),)
                )
    for node in ast.walk(tree.body):
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Pow):
            if not (
                isinstance(node.right, ast.Constant)
                and type(node.right.value) is int
                and node.right.value >= 0
            ):
                raise FormulaTranslationError(
                    "power exponent must be a non-negative integer literal",
                    (ast.dump(node, include_attributes=False),),
                )
    variables: set[str] = set()
    term = _emit(tree.body, variables)
    variable_sorts, result_sort, equality_constraints = _sorts(tree.body, variables)
    # Postorder is required: evaluating a denominator may itself require an
    # earlier division guard (for example x / (y / z)).
    divisions = [
        n for n in ast.walk(tree.body) if isinstance(n, ast.BinOp) and isinstance(n.op, ast.Div)
    ]
    divisions.sort(key=_node_depth)
    if divisions:
        guards = tuple(Not(Eq(_emit(n.right, set()), Lit(0))) for n in divisions)
        term = IfThenElse(And(guards), term, Lit(None))
    scalar_index_used = bool(
        re.search(r"_\s*\{[^{}]+\}|\b[A-Za-z_]\w*\[\s*[0-9]+\s*\]|\\[A-Za-z]+_[A-Za-z0-9]", source)
    )
    residuals = (
        [
            "Subscript notation is represented as a fixed scalar variable name, not collection indexing; verify this interpretation."
        ]
        if scalar_index_used
        else []
    )
    residuals += [
        f"variable {name!r} has unknown required sort; executor must validate its runtime sort"
        for name, sort in variable_sorts.items()
        if sort == "unknown"
    ]
    residuals.extend(
        f"variables {left!r} and {right!r} must have the same runtime sort"
        for left, right in equality_constraints
    )
    if re.search(r"\\(?:" + "|".join(map(re.escape, _GREEK)) + r")(?![A-Za-z])", source):
        residuals.append(
            "LaTeX Greek symbols map to named scalar variables; verify names and intended domain."
        )
    if divisions:
        residuals.append(
            "zero divisor produces unresolved null; executor must treat null and any non-finite intermediate as unresolved"
        )
    if any(isinstance(n, ast.BinOp) and isinstance(n.op, ast.Pow) for n in ast.walk(tree.body)):
        residuals.append("executor must treat non-finite power results as unresolved")
    if any(isinstance(n, ast.Constant) and type(n.value) is str for n in ast.walk(tree.body)):
        residuals.append(
            "\\mathrm/\\text labels are opaque string symbols; they carry no special domain semantics"
        )
    ambiguous_names = sorted(
        {
            n.id
            for n in ast.walk(tree.body)
            if isinstance(n, ast.Name)
            and n.id.isalpha()
            and len(n.id) > 1
            and n.id not in {"True", "False", *_GREEK.values()}
        }
    )
    if ambiguous_names:
        residuals.append(
            "adjacent letters are retained as single variable names; verify they are not implicit products: "
            + ", ".join(ambiguous_names)
        )
    for name in used_products:
        residuals.append(
            f"user-selected implicit product mapping {name!r} -> "
            + " * ".join(implicit_products[name])
            + "; this interpretation was supplied by the caller"
        )
    if any(
        isinstance(node, (ast.BinOp,))
        or isinstance(node, ast.UnaryOp)
        and isinstance(node.op, ast.USub)
        or isinstance(node, ast.Compare)
        and any(not isinstance(op, (ast.Eq, ast.NotEq)) for op in node.ops)
        for node in ast.walk(tree.body)
    ):
        residuals.append(
            "numeric arithmetic and ordering use native runtime semantics (including float precision)"
        )
    return Translation(
        term_to_json(term),
        tuple(sorted(variables)),
        variable_sorts,
        result_sort,
        equality_constraints,
        tuple(residuals),
        tuple(term_to_json(g) for g in guards) if divisions else (),
    )


__all__ = ["FormulaTranslationError", "Translation", "translate_formula"]
