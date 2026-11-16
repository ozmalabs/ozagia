"""ozagia.core.structured — look inside JSON, YAML and TOML files by path.

A structured file is read as a value and addressed with an RFC 6901 JSON
pointer. The key of a node is

    json:<rel>#<pointer>      json:results/run.json#/runs/0/score
    yaml:<rel>#<pointer>      yaml:deploy/app.yaml#/spec/replicas
    toml:<rel>#<pointer>      toml:pyproject.toml#/project/dependencies

`#` alone (or nothing after the path) is the root. In a pointer `~1` is a
`/` and `~0` a `~` inside a key; there is no percent-decoding. Array
indices are decimal without leading zeros.

`query` describes the node at a pointer: an object's keys with their type
and size, an array's length and a slice of its items, or a scalar's value,
capped at `OUTPUT_CAP` bytes. JSON numbers keep the text they were written
with (`Num`). For JSON files up to `SPAN_MAX_BYTES` the line of the node
and of each listed member comes from the span parser of
`ozagia.edit.json_edit`.

`outline` is the shape of a whole document: top-level keys with types,
sizes and array lengths and a preview one level down.

JSON and TOML use the standard library (`json`, `tomllib`, read-only).
YAML needs PyYAML (the `iac-yaml` extra); without it a YAML query is a
`QueryError` that says so.
"""
from __future__ import annotations

import bisect
import difflib
import json
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

FORMATS = ("json", "yaml", "toml")
SUFFIX_FORMAT = {".json": "json", ".yaml": "yaml", ".yml": "yaml",
                 ".toml": "toml"}
# Output cap of one query or outline, in bytes (UTF-8).
OUTPUT_CAP = 8192
# Items shown by default: array items, object keys.
DEFAULT_ITEMS = 20
DEFAULT_KEYS = 50
# Largest file a query reads.
MAX_QUERY_BYTES = 64 * 1024 * 1024
# Largest JSON file whose spans (line numbers) are computed.
SPAN_MAX_BYTES = 1024 * 1024
# A member preview longer than this is cut.
PREVIEW_CHARS = 160
YAML_MISSING = ("YAML queries need PyYAML, which is not installed: install "
                "the iac-yaml extra (pip install 'ozagia[iac-yaml]')")


class QueryError(ValueError):
    """A query that cannot be answered: the message is for the model."""


class Num:
    """A JSON number as written in the file (`1.50` stays `1.50`)."""

    __slots__ = ("text",)

    def __init__(self, text: str) -> None:
        self.text = text

    def __repr__(self) -> str:
        return f"Num({self.text})"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Num) and other.text == self.text

    def __hash__(self) -> int:
        return hash(self.text)


def format_of(path: str | Path) -> str | None:
    """`json`, `yaml` or `toml` from a file name's suffix, or None."""
    return SUFFIX_FORMAT.get(Path(str(path)).suffix.lower())


def yaml_available() -> bool:
    try:
        import yaml  # type: ignore[import-not-found]  # noqa: F401
    except ImportError:
        return False
    return True


# --------------------------------------------------------------------------- #
# pointers and keys
# --------------------------------------------------------------------------- #
def parse_pointer(pointer: str | None) -> list[str]:
    """The reference tokens of a JSON pointer (`#` and `""` are the root)."""
    p = "" if pointer is None else str(pointer)
    if p.startswith("#"):
        p = p[1:]
    if p == "":
        return []
    if not p.startswith("/"):
        raise QueryError(f"a JSON pointer starts with '/': {pointer!r}")
    out = []
    for tok in p[1:].split("/"):
        i = tok.find("~")
        while i >= 0:
            if tok[i + 1:i + 2] not in ("0", "1"):
                raise QueryError(
                    f"bad escape in JSON pointer {pointer!r}: '~' must be "
                    f"followed by 0 or 1")
            i = tok.find("~", i + 2)
        out.append(tok.replace("~1", "/").replace("~0", "~"))
    return out


def format_pointer(tokens: list[Any]) -> str:
    return "".join("/" + str(t).replace("~", "~0").replace("/", "~1")
                   for t in tokens)


def node_key(fmt: str, rel: str, tokens: list[Any]) -> str:
    return f"{fmt}:{rel}#{format_pointer(tokens)}"


def parse_key(key: str) -> tuple[str, str, str] | None:
    """(format, rel, pointer) of a `json:`/`yaml:`/`toml:` key, or None."""
    fmt, sep, rest = key.partition(":")
    if not sep or fmt not in FORMATS or not rest:
        return None
    rel, _, pointer = rest.partition("#")
    if not rel:
        return None
    return fmt, rel, pointer


# --------------------------------------------------------------------------- #
# loading
# --------------------------------------------------------------------------- #
@dataclass
class Doc:
    fmt: str
    rel: str
    value: Any
    note: str = ""                     # "3 documents" for multi-doc YAML
    spans: Any = None                  # json_edit.JNode of the root
    line_starts: list[int] = field(default_factory=list)

    def line(self, offset: int) -> int | None:
        if not self.line_starts:
            return None
        return bisect.bisect_right(self.line_starts, offset)


def load_json(text: str) -> Any:
    try:
        return json.loads(text, parse_int=Num, parse_float=Num)
    except json.JSONDecodeError as e:
        raise QueryError(f"invalid JSON at line {e.lineno} column {e.colno}: "
                         f"{e.msg}") from None
    except RecursionError:
        raise QueryError("JSON nested too deeply") from None


def load_yaml(text: str) -> tuple[Any, str]:
    try:
        import yaml  # type: ignore[import-not-found]
    except ImportError:
        raise QueryError(YAML_MISSING) from None
    loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
    try:
        docs = list(yaml.load_all(text, Loader=loader))
    except yaml.MarkedYAMLError as e:
        mark = e.problem_mark or e.context_mark
        where = (f" at line {mark.line + 1} column {mark.column + 1}"
                 if mark is not None else "")
        raise QueryError(f"invalid YAML{where}: {e.problem or e.context}"
                         ) from None
    except yaml.YAMLError as e:
        raise QueryError(f"invalid YAML: {e}") from None
    if len(docs) == 1:
        return docs[0], ""
    if not docs:
        return None, "no documents"
    return docs, f"{len(docs)} documents; the root is the list of documents"


def load_toml(text: str) -> Any:
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise QueryError(f"invalid TOML: {e}") from None


def load_text(fmt: str, rel: str, text: str) -> Doc:
    if fmt == "json":
        doc = Doc(fmt, rel, load_json(text))
        if len(text) <= SPAN_MAX_BYTES:
            from ozagia.edit.code_edit import CodeEditError
            from ozagia.edit.json_edit import _Parser
            try:
                doc.spans = _Parser(text).parse()
            except (CodeEditError, RecursionError):
                doc.spans = None
            if doc.spans is not None:
                starts = [0]
                i = text.find("\n")
                while i >= 0:
                    starts.append(i + 1)
                    i = text.find("\n", i + 1)
                doc.line_starts = starts
        return doc
    if fmt == "yaml":
        value, note = load_yaml(text)
        return Doc(fmt, rel, value, note)
    if fmt == "toml":
        return Doc(fmt, rel, load_toml(text))
    raise QueryError(f"unknown structured format {fmt!r}; known: "
                     + ", ".join(FORMATS))


def read_file(path: Path) -> str:
    """The UTF-8 text of `path` (a BOM dropped), or QueryError."""
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        raise QueryError(f"no such file: {path}") from None
    except OSError as e:
        raise QueryError(f"cannot read {path}: {e}") from None
    if not path.is_file():
        raise QueryError(f"not a regular file: {path}")
    if size > MAX_QUERY_BYTES:
        raise QueryError(f"{path} is larger than {MAX_QUERY_BYTES} bytes")
    try:
        text = path.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        raise QueryError(f"{path} is not UTF-8") from None
    return text[1:] if text.startswith("\ufeff") else text


def load_file(path: Path, rel: str, fmt: str | None = None) -> Doc:
    fmt = fmt or format_of(path)
    if fmt is None:
        raise QueryError(f"{rel}: not a .json, .yaml, .yml or .toml file; "
                         f"name the format with json:, yaml: or toml:")
    if fmt == "yaml" and not yaml_available():
        raise QueryError(YAML_MISSING)
    return load_text(fmt, rel, read_file(path))


# --------------------------------------------------------------------------- #
# values
# --------------------------------------------------------------------------- #
def type_name(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "boolean"
    if isinstance(v, (Num, int, float)):
        return "number"
    if isinstance(v, str):
        return "string"
    if isinstance(v, dict):
        return "object"
    if isinstance(v, list):
        return "array"
    import datetime as _dt
    if isinstance(v, _dt.datetime):
        return "datetime"
    if isinstance(v, _dt.date):
        return "date"
    if isinstance(v, _dt.time):
        return "time"
    return type(v).__name__


def size_text(v: Any) -> str:
    """`object, 3 keys`, `array, 12 items`, `string, 40 chars`, `number`."""
    t = type_name(v)
    if t == "object":
        return f"object, {len(v)} key{'s' if len(v) != 1 else ''}"
    if t == "array":
        return f"array, {len(v)} item{'s' if len(v) != 1 else ''}"
    if t == "string":
        return f"string, {len(v)} char{'s' if len(v) != 1 else ''}"
    return t


_ESCAPES = {'"': '\\"', "\\": "\\\\", "\b": "\\b", "\f": "\\f",
            "\n": "\\n", "\r": "\\r", "\t": "\\t"}


def json_string(s: str) -> str:
    """`s` as a JSON string the way jq writes it: `"`, `\\`, control
    characters and DEL escaped, everything else as is."""
    out = ['"']
    for ch in s:
        e = _ESCAPES.get(ch)
        if e is not None:
            out.append(e)
        elif ch < " " or ch == "\x7f":
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _scalar(v: Any) -> str:
    if v is None:
        return "null"
    if v is True:
        return "true"
    if v is False:
        return "false"
    if isinstance(v, Num):
        return v.text
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return json.dumps(v)
    if isinstance(v, str):
        return json_string(v)
    if hasattr(v, "isoformat"):
        return json_string(v.isoformat())
    return json_string(str(v))


def _key_text(k: Any) -> str:
    return k if isinstance(k, str) else _scalar(k).strip('"')


def dump(v: Any, *, indent: int | None = None, sort_keys: bool = False,
         _level: int = 0) -> str:
    """`v` as JSON: compact with `indent=None` (`{"a":1}`), else with that
    many spaces per level (`"a": 1`). Values that are not JSON (YAML/TOML
    dates) become strings."""
    if isinstance(v, dict):
        if not v:
            return "{}"
        items = sorted(v.items(), key=lambda kv: _key_text(kv[0])) \
            if sort_keys else v.items()
        sep = ":" if indent is None else ": "
        parts = [json_string(_key_text(k)) + sep
                 + dump(x, indent=indent, sort_keys=sort_keys,
                        _level=_level + 1) for k, x in items]
    elif isinstance(v, list):
        if not v:
            return "[]"
        parts = [dump(x, indent=indent, sort_keys=sort_keys,
                      _level=_level + 1) for x in v]
    else:
        return _scalar(v)
    open_, close = ("{", "}") if isinstance(v, dict) else ("[", "]")
    if indent is None:
        return open_ + ",".join(parts) + close
    pad = " " * (indent * (_level + 1))
    end = " " * (indent * _level)
    return (open_ + "\n" + pad + (",\n" + pad).join(parts) + "\n" + end
            + close)


def _cut(text: str, n: int) -> str:
    return text if len(text) <= n else text[:n - 1] + "…"


def preview(v: Any, n: int = PREVIEW_CHARS) -> str:
    """A one-line view of a value: its compact JSON when that is at most
    `n` characters, else for an object its first keys and for an array
    the shape of its first item."""
    if isinstance(v, (dict, list)):
        try:
            text = dump(v)
        except RecursionError:
            text = ""
        if text and len(text) <= n:
            return text
        if isinstance(v, dict):
            keys = ", ".join(_key_text(k) for k in list(v)[:8])
            return f"{{{keys}{', …' if len(v) > 8 else ''}}}"
        first = v[0]
        if isinstance(first, dict):
            keys = ", ".join(_key_text(k) for k in list(first)[:8])
            shape = f"object {{{keys}{', …' if len(first) > 8 else ''}}}"
        elif isinstance(first, list):
            shape = size_text(first)
        else:
            shape = _cut(_scalar(first), 40)
        return f"[first: {shape}, …]"
    return _cut(_scalar(v), n)


def item_text(v: Any) -> str:
    """An array item: its compact JSON when short, else type, size and a
    preview."""
    if isinstance(v, (dict, list)) and not _fits(v):
        return f"{size_text(v)}  {preview(v)}"
    return preview(v)


def _fits(v: Any) -> bool:
    try:
        return len(dump(v)) <= PREVIEW_CHARS
    except RecursionError:
        return False


# --------------------------------------------------------------------------- #
# resolution
# --------------------------------------------------------------------------- #
def _member_span(span: Any, key: str) -> Any:
    if span is None or span.kind != "object":
        return None
    hit = None
    for m in span.members:
        if m["key"] == key:
            hit = m                  # json.loads keeps the last duplicate
    return hit


def _match_key(obj: dict, tok: str) -> tuple[bool, Any]:
    if tok in obj:
        return True, tok
    for k in obj:                    # YAML/TOML keys that are not strings
        if not isinstance(k, str) and _key_text(k) == tok:
            return True, k
    return False, None


@dataclass
class Node:
    value: Any
    tokens: list[Any]
    span: Any = None                 # JNode, JSON only
    key_start: int | None = None     # offset of the member key, JSON only


def resolve(doc: Doc, tokens: list[str]) -> Node:
    cur = Node(doc.value, [], doc.spans)
    for tok in tokens:
        here = node_key(doc.fmt, doc.rel, cur.tokens)
        v = cur.value
        if isinstance(v, dict):
            ok, k = _match_key(v, tok)
            if not ok:
                keys = [_key_text(x) for x in v]
                near = difflib.get_close_matches(tok, keys, n=3, cutoff=0.5)
                hint = (f"; nearest: {', '.join(near)}" if near else
                        f"; keys: {', '.join(keys[:10])}"
                        + (", …" if len(keys) > 10 else "") if keys else
                        "; the object is empty")
                raise QueryError(f"no key {tok!r} in {here} "
                                 f"({size_text(v)}){hint}")
            m = _member_span(cur.span, tok)
            cur = Node(v[k], cur.tokens + [tok],
                       m["value"] if m else None,
                       m["key_start"] if m else None)
        elif isinstance(v, list):
            if not tok.isdigit() or (len(tok) > 1 and tok[0] == "0"):
                raise QueryError(f"{tok!r} is not an array index in {here} "
                                 f"({size_text(v)}); use 0-{len(v) - 1}"
                                 if v else
                                 f"{tok!r} is not an array index in {here} "
                                 f"(an empty array)")
            i = int(tok)
            if i >= len(v):
                rng = f"0-{len(v) - 1}" if v else "none: it is empty"
                raise QueryError(f"index {i} is out of range in {here} "
                                 f"({size_text(v)}); indices: {rng}")
            span = (cur.span.items[i] if cur.span is not None
                    and cur.span.kind == "array" else None)
            cur = Node(v[i], cur.tokens + [i], span)
        else:
            raise QueryError(f"cannot look up {tok!r} in {here}: it is a "
                             f"{type_name(v)}, not an object or array")
    return cur


# --------------------------------------------------------------------------- #
# query
# --------------------------------------------------------------------------- #
class _Out:
    def __init__(self, cap: int) -> None:
        self.cap = cap
        self.lines: list[str] = []
        self.size = 0
        self.full = False

    def add(self, line: str) -> bool:
        n = len(line.encode("utf-8")) + 1
        if self.size + n > self.cap:
            self.full = True
            return False
        self.lines.append(line)
        self.size += n
        return True


def _int_opt(value: Any, name: str, *, minimum: int) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise QueryError(f"{name} must be an integer, got {value!r}")
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise QueryError(f"{name} must be an integer, got {value!r}") from None
    if n < minimum:
        raise QueryError(f"{name} must be at least {minimum}, got {n}")
    return n


def query(doc: Doc, pointer: str | None, *, offset: Any = None,
          limit: Any = None, cap: int = OUTPUT_CAP) -> dict[str, Any]:
    """The node at `pointer` (see the module docstring). `offset` (0-based)
    and `limit` select the items of an array or the keys of an object.

    Returns {"key", "pointer", "type", "size", "line", "lines" (the body),
    "shown": (first, last) or None, "total", "truncated", "next"}."""
    tokens = parse_pointer(pointer)
    off = _int_opt(offset, "offset", minimum=0) or 0
    lim = _int_opt(limit, "limit", minimum=1)
    node = resolve(doc, tokens)
    v = node.value
    out = _Out(cap)
    line = None
    if node.key_start is not None:
        line = doc.line(node.key_start)
    elif node.span is not None:
        line = doc.line(node.span.start)
    shown = None
    total = None
    nxt = None
    if isinstance(v, dict):
        keys = list(v)
        total = len(keys)
        n = lim or DEFAULT_KEYS
        part = keys[off:off + n]
        last = off - 1
        for k in part:
            m = _member_span(node.span, _key_text(k))
            where = (f"  (line {doc.line(m['key_start'])})"
                     if m is not None and doc.line_starts else "")
            if not out.add(f"{json_string(_key_text(k))}: "
                           f"{_describe(v[k])}{where}"):
                break
            last += 1
        shown = (off, last) if part else None
        if last + 1 < total:
            nxt = last + 1
    elif isinstance(v, list):
        total = len(v)
        n = lim or DEFAULT_ITEMS
        part = v[off:off + n]
        last = off - 1
        for i, item in enumerate(part, start=off):
            where = ""
            if node.span is not None and node.span.kind == "array" \
                    and doc.line_starts:
                where = f"  (line {doc.line(node.span.items[i].start)})"
            if not out.add(f"[{i}] {item_text(item)}{where}"):
                break
            last += 1
        shown = (off, last) if part else None
        if last + 1 < total:
            nxt = last + 1
    else:
        text = _scalar(v)
        if not out.add(text):
            budget = max(16, cap - 64)
            out.lines.append(_cut(text, budget))
            out.full = True
    return {"key": node_key(doc.fmt, doc.rel, node.tokens),
            "pointer": format_pointer(node.tokens),
            "type": type_name(v), "size": size_text(v), "line": line,
            "lines": out.lines, "shown": shown, "total": total,
            "truncated": out.full, "next": nxt}


def _describe(v: Any) -> str:
    """A member's type, size and short preview."""
    if isinstance(v, (dict, list)):
        return f"{size_text(v)}  {preview(v, 100)}"
    if isinstance(v, str):
        return f"{size_text(v)}  {preview(v, 100)}"
    return preview(v, 100)


# --------------------------------------------------------------------------- #
# outline
# --------------------------------------------------------------------------- #
def outline(doc: Doc, *, max_keys: int = 40, max_children: int = 8,
            cap: int = OUTPUT_CAP) -> list[str]:
    """The shape of a document, depth 2 (see the module docstring)."""
    v = doc.value
    out = _Out(cap)
    out.add(f"root: {size_text(v)}" + (f" ({doc.note})" if doc.note else ""))

    def line_of(span: Any, key: str) -> str:
        m = _member_span(span, key)
        if m is None or not doc.line_starts:
            return ""
        return f"  (line {doc.line(m['key_start'])})"

    def children(x: Any, span: Any, pad: str) -> None:
        if isinstance(x, dict):
            for k in list(x)[:max_children]:
                kt = _key_text(k)
                if not out.add(f"{pad}{json_string(kt)}: {_describe(x[k])}"
                               + line_of(span, kt)):
                    return
            if len(x) > max_children:
                out.add(f"{pad}… {len(x) - max_children} more keys")
        elif isinstance(x, list) and x:
            out.add(f"{pad}[0] {item_text(x[0])}")

    if isinstance(v, dict):
        for k in list(v)[:max_keys]:
            kt = _key_text(k)
            if not out.add(f"- {json_string(kt)}: {_describe(v[k])}"
                           + line_of(doc.spans, kt)):
                break
            m = _member_span(doc.spans, kt)
            children(v[k], m["value"] if m else None, "    ")
            if out.full:
                break
        if len(v) > max_keys and not out.full:
            out.add(f"… {len(v) - max_keys} more top-level keys")
    elif isinstance(v, list):
        for i, item in enumerate(v[:max_children]):
            if not out.add(f"- [{i}] {_describe(item)}"):
                break
            span = (doc.spans.items[i] if doc.spans is not None
                    and doc.spans.kind == "array" else None)
            children(item, span, "    ")
            if out.full:
                break
        if len(v) > max_children and not out.full:
            out.add(f"… {len(v) - max_children} more items")
    if out.full:
        out.lines.append(f"… outline truncated at {cap} bytes")
    return out.lines


__all__ = ["DEFAULT_ITEMS", "DEFAULT_KEYS", "Doc", "FORMATS", "Num",
           "OUTPUT_CAP", "QueryError", "YAML_MISSING", "dump", "format_of",
           "format_pointer", "json_string", "load_file", "load_text",
           "node_key", "outline", "parse_key", "parse_pointer", "preview",
           "query", "resolve", "type_name", "yaml_available"]
