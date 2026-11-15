"""Stable node keys: the names an agent uses for graph nodes.

A key names a node by what it is, not by build order, so it survives edits
and rebuilds. Node ids stay internal to one snapshot.

    file:<rel>                       file:src/pkg/core.py
    sym:<rel>::<qualname>            sym:src/pkg/core.py::Retry.backoff
    test:<rel>::<qualname>           test:tests/test_core.py::test_retry
    doc:<rel>                        doc:README.md
    text:<rel>#L<start>-L<end>       text:deploy/app.yaml#L10-L42
    commit:<sha>                     commit:5010d75...
    failure:<id>                     failure:tests/test_x.py::test_y
    repo:                            the Repo node
    <kind>:<part>::<part>...         any other kind (adapter nodes)

`<rel>` is the repository-relative POSIX path; paths never contain `::`.
A `sym:`/`test:` qualname may (a Rust `impl fmt::Display for X`), so those keys
split only at the first `::`. Builder keys are tuples whose first item is the
kind; `format_key` and `parse_key` convert between the two forms.
"""
from __future__ import annotations

# Builder tuple kind -> key prefix, where they differ.
_ALIASES = {"symbol": "sym"}
_REVERSE = {v: k for k, v in _ALIASES.items()}
SEP = "::"
# Kinds whose last part (a qualname) may itself contain SEP: (path, qualname).
_PATH_QUALNAME_KINDS = {"symbol", "test"}


def format_key(key: tuple) -> str:
    """The string key for a builder key tuple."""
    if not key:
        raise ValueError("empty key")
    kind, *parts = key
    prefix = _ALIASES.get(str(kind), str(kind))
    return f"{prefix}:" + SEP.join(str(p) for p in parts)


def parse_key(key: str) -> tuple:
    """The builder key tuple for a string key (inverse of `format_key`).

    Parts are returned as strings. `text:` keys keep their `#L..` suffix in the
    path part.
    """
    prefix, sep, rest = key.partition(":")
    if not sep:
        raise ValueError(f"not a node key: {key!r}")
    kind = _REVERSE.get(prefix, prefix)
    if not rest:
        return (kind,)
    if kind in _PATH_QUALNAME_KINDS:
        return (kind, *rest.split(SEP, 1))
    return (kind, *rest.split(SEP))


def file_key(rel: str) -> str:
    return format_key(("file", rel))


def symbol_key(rel: str, qualname: str) -> str:
    return format_key(("symbol", rel, qualname))


def test_key(rel: str, qualname: str) -> str:
    return format_key(("test", rel, qualname))


def text_key(rel: str, start_line: int, end_line: int) -> str:
    return f"text:{rel}#L{int(start_line)}-L{int(end_line)}"


def key_file(key: str) -> str | None:
    """The repository file a key belongs to, or None (commits, repo, ...)."""
    prefix, _, rest = key.partition(":")
    if prefix in ("file", "doc"):
        return rest or None
    if prefix in ("sym", "test"):
        return rest.split(SEP, 1)[0] or None
    if prefix == "text":
        return rest.split("#", 1)[0] or None
    return None
