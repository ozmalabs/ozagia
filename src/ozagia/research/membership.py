"""Translate selected scalar membership expressions into native Boolean Terms.

This narrowly scoped adapter covers statements such as ``x,y \\in \\{0,1\\}``.
The finite set must be written explicitly and contain only Boolean or i64
integer literals of one sort. Natural-number membership additionally requires
an explicit minimum and is represented only over native signed integers. No
unbounded set, macro expansion, or arbitrary predicate is inferred.
"""

from __future__ import annotations
import re
from ozagia.research.formula import FormulaTranslationError, Translation

_GREEK = frozenset(
    "alpha beta gamma delta epsilon varepsilon zeta eta theta vartheta iota kappa lambda mu nu xi omicron pi varpi rho varrho sigma varsigma tau upsilon phi varphi chi psi omega Gamma Delta Theta Lambda Xi Pi Sigma Upsilon Phi Psi Omega".split()
)
_IDENT = re.compile(r"(?:[A-Za-z][A-Za-z0-9_]*|\\[A-Za-z]+)(?:_\{[A-Za-z0-9]+\})?\Z")
_I64_MIN, _I64_MAX = -(2**63), 2**63 - 1


def _symbol(text: str) -> str:
    token = text.strip()
    if not _IDENT.fullmatch(token):
        raise FormulaTranslationError("membership element must be a scalar identifier", (token,))
    m = re.match(r"\\([A-Za-z]+)", token)
    if m and m.group(1) not in _GREEK:
        raise FormulaTranslationError("unknown LaTeX symbol; provide an explicit mapping", (token,))
    name = token.replace("\\", "")
    name = re.sub(r"_\{([A-Za-z0-9]+)\}", r"_\1", name)
    return name


def translate_membership(source: str, *, natural_minimum: int | None = None) -> Translation:
    """Compile explicit finite membership to Eq/Or/And substrate terms."""
    if not isinstance(source, str) or not source.strip():
        raise FormulaTranslationError("formula must be a nonempty string", ("empty",))
    text = source.strip()
    if text.startswith("$") and text.endswith("$") and not text.startswith("$$"):
        text = text[1:-1].strip()
    parts = re.split(r"\\in(?![A-Za-z_])|∈", text)
    if len(parts) != 2:
        raise FormulaTranslationError("expected one finite membership relation", (source,))
    lhs, rhs = (part.strip() for part in parts)
    if rhs in {r"\NN", r"\mathbb{N}", "ℕ"}:
        if type(natural_minimum) is not int or natural_minimum not in (0, 1):
            raise FormulaTranslationError(
                "natural-number notation needs an explicit minimum of 0 or 1", (rhs,)
            )
        names_raw = [x.strip() for x in lhs.split(",")]
        if not 1 <= len(names_raw) <= 16:
            raise FormulaTranslationError("natural membership exceeds variable bounds", (lhs,))
        names = [_symbol(x) for x in names_raw]
        if len(set(names)) != len(names):
            raise FormulaTranslationError(
                "membership variables are duplicated or normalize to the same name",
                tuple(names_raw),
            )
        clauses = [
            {
                "type": "Gte",
                "a": {"type": "Var", "name": name},
                "b": {"type": "Lit", "value": natural_minimum},
            }
            for name in names
        ]
        term = clauses[0] if len(clauses) == 1 else {"type": "And", "items": clauses}
        return Translation(
            term=term,
            variables=tuple(sorted(names)),
            variable_sorts={n: "int" for n in sorted(names)},
            result_sort="bool",
            equality_sort_constraints=(),
            residuals=(
                f"Natural numbers are explicitly modelled as integers >= {natural_minimum}; verify the paper uses that convention.",
                "Native integer inputs are limited to signed i64; this does not represent an unbounded natural-number domain.",
                "Verify that any paper-specific natural-number macro denotes this domain.",
            ),
            preconditions=(),
        )
    if rhs.startswith(r"\{") and rhs.endswith(r"\}"):
        inner = rhs[2:-2]
    elif rhs.startswith("{") and rhs.endswith("}"):
        inner = rhs[1:-1]
    else:
        raise FormulaTranslationError(
            "membership domain must be an explicit finite literal set", (rhs,)
        )
    names_raw = [x.strip() for x in lhs.split(",")]
    members_raw = [x.strip() for x in inner.split(",")]
    if not 1 <= len(names_raw) <= 16 or not 1 <= len(members_raw) <= 64:
        raise FormulaTranslationError("finite membership exceeds bounds or is empty", (source,))
    names = [_symbol(x) for x in names_raw]
    if len(set(names)) != len(names):
        raise FormulaTranslationError(
            "membership variables are duplicated or normalize to the same name", tuple(names_raw)
        )
    members = []
    for raw in members_raw:
        if raw in {"true", "True", r"\top"}:
            value = True
        elif raw in {"false", "False", r"\bot"}:
            value = False
        elif re.fullmatch(r"[+-]?[0-9]+", raw):
            value = int(raw)
            if not _I64_MIN <= value <= _I64_MAX:
                raise FormulaTranslationError("integer literal outside native i64 range", (raw,))
        else:
            raise FormulaTranslationError(
                "finite membership elements must be integer or Boolean literals", (raw,)
            )
        members.append(value)
    if len({type(v) for v in members}) != 1:
        raise FormulaTranslationError(
            "finite set elements must have one scalar sort", tuple(members_raw)
        )
    sort = "bool" if type(members[0]) is bool else "int"
    clauses = []
    for name in names:
        options = [
            {"type": "Eq", "a": {"type": "Var", "name": name}, "b": {"type": "Lit", "value": value}}
            for value in members
        ]
        clauses.append(options[0] if len(options) == 1 else {"type": "Or", "items": options})
    term = clauses[0] if len(clauses) == 1 else {"type": "And", "items": clauses}
    residuals = (
        "Finite membership denotes only the explicitly listed scalar values; review source notation and variable mapping.",
    )
    if any("\\" in name for name in names_raw):
        residuals += ("LaTeX Greek symbols are mapped to their command names as scalar variables.",)
    # Construct the same translation contract as the existing scalar translator.
    return Translation(
        term=term,
        variables=tuple(sorted(names)),
        variable_sorts={n: sort for n in sorted(names)},
        result_sort="bool",
        equality_sort_constraints=(),
        residuals=residuals,
        preconditions=(),
    )
