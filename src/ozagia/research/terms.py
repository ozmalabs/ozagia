"""Small substrate-compatible Term JSON constructors.

The research translators describe candidate expressions in the stable Term
wire format. This module keeps formula parsing independent of Ocelot's optional
native substrate package; it does not evaluate terms or imply a domain model.
"""

from __future__ import annotations

from typing import Any


def _term(kind: str, **fields: Any) -> dict[str, Any]:
    return {"type": kind, **fields}


def Lit(value: Any) -> dict[str, Any]:
    return _term("Lit", value=value)


def Var(name: str) -> dict[str, Any]:
    return _term("Var", name=name)


def Abs(arg: Any) -> dict[str, Any]:
    return _term("Abs", arg=arg)


def And(items: Any) -> dict[str, Any]:
    return _term("And", items=list(items))


def Or(items: Any) -> dict[str, Any]:
    return _term("Or", items=list(items))


def Plus(items: Any) -> dict[str, Any]:
    return _term("Plus", items=list(items))


def Times(items: Any) -> dict[str, Any]:
    return _term("Times", items=list(items))


def Max(items: Any) -> dict[str, Any]:
    return _term("Max", items=list(items))


def Min(items: Any) -> dict[str, Any]:
    return _term("Min", items=list(items))


def Div(a: Any, b: Any) -> dict[str, Any]:
    return _term("Div", a=a, b=b)


def Eq(a: Any, b: Any) -> dict[str, Any]:
    return _term("Eq", a=a, b=b)


def Gt(a: Any, b: Any) -> dict[str, Any]:
    return _term("Gt", a=a, b=b)


def Gte(a: Any, b: Any) -> dict[str, Any]:
    return _term("Gte", a=a, b=b)


def Lt(a: Any, b: Any) -> dict[str, Any]:
    return _term("Lt", a=a, b=b)


def Lte(a: Any, b: Any) -> dict[str, Any]:
    return _term("Lte", a=a, b=b)


def Minus(a: Any, b: Any) -> dict[str, Any]:
    return _term("Minus", a=a, b=b)


def Not(arg: Any) -> dict[str, Any]:
    return _term("Not", arg=arg)


def Pow(a: Any, b: Any) -> dict[str, Any]:
    return _term("Pow", a=a, b=b)


def IfThenElse(cond: Any, then: Any, other: Any) -> dict[str, Any]:
    return _term("IfThenElse", cond=cond, then=then, other=other)


def Exists(source: Any, var_name: str, predicate: Any) -> dict[str, Any]:
    return _term("Exists", source=source, var_name=var_name, predicate=predicate)


def term_to_json(term: Any) -> dict[str, Any]:
    """Return a JSON-compatible copy of an already encoded Term tree."""
    if not isinstance(term, dict) or not isinstance(term.get("type"), str):
        raise TypeError("expected a Term JSON object")
    return term
