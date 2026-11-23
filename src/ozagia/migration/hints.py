"""Deterministic construct and type hints for a Python→Rust port.

Every hint is documentation, not code. ``support`` says how much of the
translation is mechanical:

``supported``
    A shape mapping that is usually direct (``list[T]`` → ``Vec<T>``,
    ``@dataclass`` → a struct with derives). The result still needs review.

``manual``
    A human must choose the design (ownership, object safety, error type,
    integer width, ``String`` vs ``&str``). The reason says why.

``unsupported``
    No honest static mapping exists (metaprogramming, monkeypatching, C
    extensions, ``**kwargs``). The caller should keep the symbol out of an
    automated port.

``hints_for`` is intentionally small and deterministic: it classifies one
:class:`ozagia.migration.inventory.PythonSymbol` from its decorators, bases,
annotations, arguments and imports. It never reads source text and never
claims that the suggested Rust is compilable.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable

SUPPORT_LEVELS = ("supported", "manual", "unsupported")


@dataclass(frozen=True)
class Hint:
    """One construct or type mapping.

    ``choice`` marks a mapping where more than one Rust type is reasonable and
    the caller must pick one (``str`` → ``String`` or ``&str``).
    """

    id: str
    construct: str
    python: str
    rust: str
    support: str
    reason: str
    choice: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "construct": self.construct,
            "python": self.python,
            "rust": self.rust,
            "support": self.support,
            "reason": self.reason,
            "choice": self.choice,
            "compilable": False,
        }


def _hint(id: str, construct: str, python: str, rust: str, support: str,
          reason: str, *, choice: bool = False) -> Hint:
    if support not in SUPPORT_LEVELS:  # pragma: no cover - programming error
        raise ValueError(f"unknown support level {support!r}")
    return Hint(id, construct, python, rust, support, reason, choice)


#: The documented construct/type table (``docs/migration.md`` mirrors it).
HINTS: tuple[Hint, ...] = (
    _hint("class", "class", "class", "struct or enum", "manual",
          "a data-only class maps to a struct; stateful behaviour or "
          "inheritance needs a design decision"),
    _hint("dataclass", "dataclass", "@dataclass", "struct + #[derive(...)]",
          "supported",
          "fields become public struct fields; derives Debug/Clone only "
          "cover traits the Python class actually had"),
    _hint("protocol", "Protocol", "class X(Protocol)", "trait", "manual",
          "decide between a generic trait bound, dyn Trait, or an enum; "
          "object safety and method receivers need review"),
    _hint("enum", "Enum", "class X(Enum)", "enum", "supported",
          "simple variants map directly; payload variants and methods need "
          "review"),
    _hint("exception", "exception class", "class X(Exception)",
          "Result<T, E> with an error enum", "manual",
          "Rust has no exceptions; map raise sites and callers to Result"),
    _hint("async", "async def", "async def", "async fn", "supported",
          "the async keyword maps, but the runtime (tokio, async-std, ...) "
          "and executor are a project decision"),
    _hint("optional", "Optional", "Optional[T] / T | None", "Option<T>",
          "manual", "Option changes the call shape; every None path needs a "
          "decision"),
    _hint("union", "Union", "Union[A, B] / A | B", "enum (or a trait object)",
          "manual", "Rust enums need named variants and explicit matches"),
    _hint("list", "list", "list[T]", "Vec<T>", "supported",
          "ownership and borrowing still need review"),
    _hint("dict", "dict", "dict[K, V]", "HashMap<K, V>", "supported",
          "keys must implement Eq + Hash; iteration order is unspecified"),
    _hint("set", "set", "set[T]", "HashSet<T>", "supported",
          "elements must implement Eq + Hash"),
    _hint("tuple", "tuple", "tuple[A, B]", "(A, B)", "supported",
          "fixed-arity tuples map; a variadic tuple maps to Vec<T>"),
    _hint("str", "str", "str", "String or &str", "supported",
          "choose owned String or borrowed &str per field and call site",
          choice=True),
    _hint("callable", "Callable", "Callable[..., T]", "Fn/FnMut/FnOnce or a "
          "trait object", "manual",
          "captures, lifetimes and dyn dispatch are a design decision"),
    _hint("any", "Any", "typing.Any", "unsafe/Box<dyn Any> boundary",
          "manual", "an Any boundary needs an explicit dynamic representation"),
    _hint("metaprogramming", "metaprogramming",
          "metaclasses, __getattr__, dynamic class creation",
          "macro or build script", "unsupported",
          "Rust macros are compile-time and cannot preserve arbitrary Python "
          "runtime metaprogramming"),
    _hint("monkeypatching", "monkeypatching", "runtime attribute replacement",
          "none", "unsupported",
          "Rust methods are resolved statically; tests cannot patch a type "
          "at runtime the Python way"),
    _hint("c-extension", "C extension", "ctypes/cffi/C extension",
          "unsafe FFI or a crate", "unsupported",
          "FFI boundaries, ownership and build linkage are outside this "
          "static overlay"),
    _hint("kwargs", "**kwargs", "**kwargs", "none", "unsupported",
          "there is no direct, typed Rust equivalent; prefer a struct"),
    _hint("keyword-only", "keyword-only parameter", "def f(*, x)",
          "none or a builder", "manual",
          "Rust has no keyword-only parameters; change the call shape "
          "deliberately"),
    _hint("varargs", "*args", "*args", "&[T] or a generic", "manual",
          "choose a slice, iterator or overload set"),
    _hint("decorator", "decorator", "@decorator", "none or a wrapper",
          "manual", "decorator semantics are ordinary Python code; port the "
          "behaviour, not the syntax"),
)


_BY_ID = {item.id: item for item in HINTS}
_OPTIONAL = re.compile(r"^(?:typing\.)?Optional\[(.+)\]\Z", re.S)
_UNION = re.compile(r"^(?:typing\.)?Union\[(.+)\]\Z", re.S)
_GENERIC = re.compile(r"^([A-Za-z_][A-Za-z0-9_.]*)\[(.+)\]\Z", re.S)
_PAREN = re.compile(r"^(?:typing\.)?([A-Za-z_][A-Za-z0-9_.]*)\((.*)\)\Z", re.S)

_SIMPLE_TYPES: dict[str, tuple[str, str, str, bool]] = {
    # python: (rust, support, reason, choice)
    "int": ("i64", "supported",
            "integer width is a choice; check the value range", True),
    "float": ("f64", "supported", "f64 matches Python float precision", False),
    "bool": ("bool", "supported", "direct", False),
    "str": ("String or &str", "supported",
            "choose owned String or borrowed &str per field and call site",
            True),
    "bytes": ("Vec<u8>", "supported", "byte buffers map directly", False),
    "bytearray": ("Vec<u8>", "manual", "mutability and borrowing need review",
                  False),
    "None": ("()", "manual", "only sensible as a unit return type", False),
    "NoneType": ("()", "manual", "only sensible as a unit return type", False),
    "object": ("Box<dyn Any>", "manual",
               "an object boundary needs an explicit dynamic representation",
               False),
    "Any": ("Box<dyn Any>", "manual",
            "an Any boundary needs an explicit dynamic representation", False),
    "Decimal": ("rust_decimal::Decimal", "manual",
                "requires a crate and a rounding-policy decision", False),
    "datetime": ("chrono::DateTime<Utc>", "manual",
                 "requires a crate and a timezone decision", False),
    "date": ("chrono::NaiveDate", "manual", "requires a crate", False),
}


def _split_top(value: str) -> list[str]:
    out: list[str] = []
    depth = 0
    start = 0
    for index, char in enumerate(value):
        if char in "[(":
            depth += 1
        elif char in "])":
            depth = max(0, depth - 1)
        elif char == "," and depth == 0:
            out.append(value[start:index].strip())
            start = index + 1
    tail = value[start:].strip()
    if tail:
        out.append(tail)
    return [part for part in out if part]


def _split_union(value: str) -> list[str]:
    """Split a PEP 604 union at top-level ``|`` characters."""
    out: list[str] = []
    depth = 0
    start = 0
    for index, char in enumerate(value):
        if char in "[(":
            depth += 1
        elif char in "])":
            depth = max(0, depth - 1)
        elif char == "|" and depth == 0:
            out.append(value[start:index].strip())
            start = index + 1
    tail = value[start:].strip()
    if tail:
        out.append(tail)
    return [part for part in out if part]


def annotation_hints(annotation: str, *, depth: int = 0) -> list[Hint]:
    """Classify one annotation text into hints (bounded to a few levels)."""
    text = (annotation or "").strip().strip("'\"")
    if not text:
        return [_hint("unknown", "unknown annotation", "", "unknown",
                      "unsupported", "no annotation was provided")]
    if depth > 4:
        return [_hint("unknown", "nested annotation", text, "unknown",
                      "unsupported", "annotation nesting exceeds the hint "
                      "limit")]
    if text.startswith("typing."):
        text = text[len("typing."):]
    if text.startswith("collections.abc."):
        text = text[len("collections.abc."):]

    if text.startswith(("Callable[", "Callable(")):
        return [_BY_ID["callable"]]

    match = _OPTIONAL.fullmatch(text)
    if match:
        return [_BY_ID["optional"], *annotation_hints(
            match.group(1), depth=depth + 1)]
    match = _UNION.fullmatch(text)
    if match:
        parts = _split_top(match.group(1))
        hints = [_BY_ID["union"]]
        for part in parts:
            hints.extend(annotation_hints(part, depth=depth + 1))
        return hints
    parts = _split_union(text)
    if len(parts) > 1:
        none_parts = [part for part in parts
                      if part in ("None", "NoneType", "type(None)")]
        if len(none_parts) == 1 and len(parts) == 2:
            hints = [_BY_ID["optional"]]
            for part in parts:
                if part not in none_parts:
                    hints.extend(annotation_hints(part, depth=depth + 1))
            return hints
        hints = [_BY_ID["union"]]
        for part in parts:
            hints.extend(annotation_hints(part, depth=depth + 1))
        return hints

    match = _GENERIC.fullmatch(text)
    if match:
        name, inner = match.group(1), match.group(2)
        simple = name.rsplit(".", 1)[-1].lower()
        if simple in ("list", "sequence", "iterable", "iterator"):
            return [_BY_ID["list"], *annotation_hints(inner, depth=depth + 1)]
        if simple in ("dict", "mapping", "mutablemapping"):
            pieces = _split_top(inner)
            hints = [_BY_ID["dict"]]
            for piece in pieces[:2]:
                hints.extend(annotation_hints(piece, depth=depth + 1))
            return hints
        if simple in ("set", "frozenset", "abstractset", "mutableset"):
            return [_BY_ID["set"], *annotation_hints(inner, depth=depth + 1)]
        if simple == "tuple":
            pieces = _split_top(inner)
            hints = [_BY_ID["tuple"]]
            for piece in pieces:
                hints.extend(annotation_hints(piece, depth=depth + 1))
            return hints
        if simple in ("callable",):
            return [_BY_ID["callable"]]
        return [_hint("generic-user-type", "generic user type", text,
                      "manual", "manual",
                      "a project generic has no automatic cross-language "
                      "equivalent")]

    match = _PAREN.fullmatch(text)
    if match:
        name = match.group(1).rsplit(".", 1)[-1]
        if name == "Callable":
            return [_BY_ID["callable"]]
        return [_hint("user-type", "user type", text, "manual", "manual",
                      "a project type has no automatic cross-language "
                      "equivalent")]

    simple = text.rsplit(".", 1)[-1]
    if simple in _SIMPLE_TYPES:
        rust, support, reason, choice = _SIMPLE_TYPES[simple]
        hint_id = simple.lower()
        return [_hint(hint_id, simple, text, rust, support, reason,
                      choice=choice)]
    if simple in ("Any", "object"):
        return [_BY_ID["any"]]
    if simple in ("Optional", "Union"):
        return [_BY_ID[simple.lower()]]
    if simple in ("dict", "list", "set", "tuple"):
        return [_hint(simple, simple, text, {
            "list": "Vec<T>", "dict": "HashMap<K, V>", "set": "HashSet<T>",
            "tuple": "(A, B)",
        }[simple], "manual",
            "no element type was written; choose the Rust element type")]
    return [_hint("unknown-type", "unknown type", text, "unknown",
                  "unsupported",
                  "no documented mapping; a person must choose the Rust "
                  "representation")]


def _decorator_names(values: Iterable[str]) -> list[str]:
    names: list[str] = []
    for value in values:
        text = str(value).strip().lstrip("@")
        text = text.split("(", 1)[0].strip()
        names.append(text.rsplit(".", 1)[-1])
    return names


def _base_names(values: Iterable[str]) -> list[str]:
    return [str(value).strip().split("[", 1)[0].rsplit(".", 1)[-1]
            for value in values]


def hints_for(symbol: Any) -> list[Hint]:
    """Hints for one Python inventory symbol (order is deterministic)."""
    hints: list[Hint] = []
    seen: set[tuple[str, str]] = set()

    def add(items: Iterable[Hint]) -> None:
        for item in items:
            key = (item.id, item.python)
            if key in seen:
                continue
            seen.add(key)
            hints.append(item)

    kind = str(getattr(symbol, "kind", ""))
    decorators = _decorator_names(getattr(symbol, "decorators", ()) or ())
    bases = _base_names(getattr(symbol, "bases", ()) or ())
    imports = {str(item).rsplit(".", 1)[-1]
               for item in (getattr(symbol, "imports", ()) or ())}

    if kind in ("async_function", "async_method") or getattr(
            symbol, "async_", False):
        add([_BY_ID["async"]])
    if kind in ("class", "protocol", "enum", "exception"):
        add([_BY_ID["class"]])
    if "dataclass" in decorators:
        add([_BY_ID["dataclass"]])
    if any("Protocol" in base for base in bases):
        add([_BY_ID["protocol"]])
    if any(base.endswith("Enum") for base in bases):
        add([_BY_ID["enum"]])
    if any(base.endswith(("Exception", "BaseException", "Error"))
           for base in bases):
        add([_BY_ID["exception"]])
    if any(name in ("property", "cached_property") for name in decorators):
        add([_hint("property", "property", "@property",
                   "field or method", "manual",
                   "decide whether the value is stored or computed"),
             _BY_ID["decorator"]])
    if any(name in ("staticmethod", "classmethod") for name in decorators):
        add([_hint("associated", "staticmethod/classmethod",
                   "@staticmethod / @classmethod", "associated function",
                   "manual", "the receiver and Self type need review")])
    unknown_decorators = [
        name for name in decorators
        if name not in ("dataclass", "property", "cached_property",
                        "staticmethod", "classmethod", "override",
                        "final", "abstractmethod")]
    if unknown_decorators:
        add([_BY_ID["decorator"]])
    if getattr(symbol, "varargs", False):
        add([_BY_ID["varargs"]])
    if getattr(symbol, "kwargs", False):
        add([_BY_ID["kwargs"]])
    if getattr(symbol, "keyword_only", False):
        add([_BY_ID["keyword-only"]])
    if any(name in ("__getattr__", "__getattribute__", "__setattr__",
                    "__getattribute__") for name in
           (getattr(symbol, "methods", ()) or ())):
        add([_BY_ID["metaprogramming"]])
    if "mock" in imports or any(
            name in ("patch", "mock", "Mock", "MagicMock")
            for name in decorators):
        add([_BY_ID["monkeypatching"]])
    if imports & {"ctypes", "cffi", "_ctypes"}:
        add([_BY_ID["c-extension"]])

    annotations = [getattr(symbol, "returns", "")]
    for _name, annotation in getattr(symbol, "arg_annotations", ()) or ():
        annotations.append(annotation)
    for field in getattr(symbol, "fields", ()) or ():
        annotations.append(field.annotation)
    for annotation in annotations:
        if annotation:
            add(annotation_hints(str(annotation)))
    return hints


def support_summary(hints: Iterable[Hint]) -> dict[str, int]:
    """Counts per support level, always with all three keys."""
    counts = {level: 0 for level in SUPPORT_LEVELS}
    for hint in hints:
        counts[hint.support] = counts.get(hint.support, 0) + 1
    return counts


__all__ = [
    "HINTS",
    "Hint",
    "SUPPORT_LEVELS",
    "annotation_hints",
    "hints_for",
    "support_summary",
]
