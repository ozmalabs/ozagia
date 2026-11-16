"""ozagia.core.jq — a small, exact subset of jq for reading JSON files.

`parse(filter)` accepts a pipeline of up to `MAX_STAGES` stages joined by
`|`. A stage is a path or one of `keys`, `length`, `type`:

    .                 identity
    .a  ."a b"        object member (missing: null)
    .a.b  .a[0]       chained
    .[N]  .[-N]       array element (out of range: null)
    .[N:M] .[N:] .[:M] array or string slice
    .[]  .a[]         every element of an array, every value of an object
    .["a b"]          object member

Keys in quotes may not contain a backslash. Anything else (`..`, `?`,
`,`, functions, operators, variables, `select`, ...) is `Unsupported`.

`run(value, program)` evaluates on a value loaded with
`structured.load_json` (numbers kept as `Num`); a case whose result jq
would report as an error (`.a` of an array, `.[]` of null, `keys` of a
string, `length` of a boolean) raises `Refused`, so the real jq
reports it; so does `length` of a number other than an integer below 2**53
(jq prints the double).

`render(results, ...)` writes jq's stdout: one value per line, 2-space
indent by default, `compact` as `-c`, `raw` as `-r`, `sort_keys` as `-S`.
jq 1.7+ prints a number literal as written unless it has an exponent
(`1e3` prints `1E+3`); `numbers="literal"` follows that and refuses
exponents. jq 1.6 prints numbers as doubles; `numbers="integers"` accepts
only integers below 2**53 (exact as doubles) and refuses the rest. An
integer literal longer than 20 digits is never converted (Python's `int()`
refuses over 4,300 digits); it is printed as written or refused.

`load` reads the text as jq would see it: a BOM, a lone surrogate
escape, `NaN`/`Infinity`, several JSON values, an empty input or input
nested deeper than jq 1.7's parser allows (`MAX_DEPTH`) are `Refused`.

`number_policy()` picks the number output from the `jq` on PATH: jq 1.7
and 1.8 print literals (1.8.1 checked against the same differential
cases), older and newer versions get `integers`, the output every version
shares. jq 1.8 raised the parse depth limit to 10000; the 256 limit is
kept for every version, which only hands more inputs to the real jq.
"""
from __future__ import annotations

import json
import re
from typing import Any

from ozagia.core.structured import Num, json_string

MAX_STAGES = 6
# Largest output written; above it the real jq runs.
MAX_OUTPUT_BYTES = 1024 * 1024
MAX_RESULTS = 100_000
# jq 1.7's parser stack limit: each open array or object takes one slot and
# an object's key takes one more while its value is parsed; opening a
# container that would make the stack deeper than this is a parse error.
MAX_DEPTH = 256
# An integer literal with more digits than this is not converted to int.
_INT_DIGITS = 20
BUILTINS = frozenset({"keys", "length", "type"})
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_INT_RE = re.compile(r"-?(?:0|[1-9][0-9]*)")
# A lone surrogate escape (jq rejects it; json.loads keeps it). An escaped
# backslash before `u` also matches, which only refuses more.
_LONE_SURROGATE_RE = re.compile(
    r"\\u[dD][89abAB][0-9a-fA-F]{2}(?!\\u[dD][c-fC-F][0-9a-fA-F]{2})"
    r"|(?<!\\u[dD][89abAB][0-9a-fA-F]{2})\\u[dD][c-fC-F][0-9a-fA-F]{2}")
_KEYWORDS = frozenset({
    "and", "or", "not", "if", "then", "elif", "else", "end", "as", "def",
    "reduce", "foreach", "try", "catch", "label", "import", "include",
    "__loc__"})


class Unsupported(ValueError):
    """The filter is outside the subset."""


class Refused(ValueError):
    """The input or a result is a case this module does not reproduce."""


def _not_mapped(text: str) -> Unsupported:
    return Unsupported(f"jq filter {text!r} is not mapped")


def _split_pipes(text: str) -> list[str]:
    parts, cur, quote = [], [], False
    for ch in text:
        if ch == '"':
            quote = not quote
        if ch == "|" and not quote:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    if quote:
        raise _not_mapped(text)
    parts.append("".join(cur))
    return parts


def _string(s: str, i: int, whole: str) -> tuple[str, int]:
    end = s.find('"', i + 1)
    if end < 0:
        raise _not_mapped(whole)
    body = s[i + 1:end]
    if "\\" in body or any(c < " " for c in body):
        raise _not_mapped(whole)
    return body, end + 1


def _member(s: str, i: int, whole: str) -> tuple[str, int]:
    """A member name at `i` (an identifier or a quoted key)."""
    if i < len(s) and s[i] == '"':
        return _string(s, i, whole)
    m = _IDENT_RE.match(s, i)
    if not m or m.group(0) in _KEYWORDS:
        raise _not_mapped(whole)
    return m.group(0), m.end()


def _bracket(s: str, i: int, whole: str) -> tuple[tuple, int]:
    close = s.find("]", i)
    if close < 0:
        raise _not_mapped(whole)
    inner = s[i + 1:close]
    if inner.startswith('"'):
        key, j = _string(s, i + 1, whole)
        if j != close:
            raise _not_mapped(whole)
        return ("key", key), close + 1
    if inner == "":
        return ("iter",), close + 1
    if ":" in inner:
        a, _, b = inner.partition(":")
        if not (a or b) or (a and not _INT_RE.fullmatch(a)) \
                or (b and not _INT_RE.fullmatch(b)):
            raise _not_mapped(whole)
        return ("slice", int(a) if a else None, int(b) if b else None), close + 1
    if _INT_RE.fullmatch(inner):
        return ("index", int(inner)), close + 1
    raise _not_mapped(whole)


def _path(s: str, whole: str) -> list[tuple]:
    if not s.startswith("."):
        raise _not_mapped(whole)
    ops: list[tuple] = []
    n = len(s)
    i = 1
    if i < n and s[i] != "[":
        key, i = _member(s, i, whole)
        ops.append(("key", key))
    while i < n:
        if s[i] == ".":
            key, i = _member(s, i + 1, whole)
            ops.append(("key", key))
        elif s[i] == "[":
            op, i = _bracket(s, i, whole)
            ops.append(op)
        else:
            raise _not_mapped(whole)
    return ops


def parse(text: str) -> list[tuple]:
    """The program of a filter: [("path", ops) | ("builtin", name)]."""
    if not isinstance(text, str) or not text.strip():
        raise _not_mapped(str(text))
    stages = _split_pipes(text)
    if len(stages) > MAX_STAGES:
        raise _not_mapped(text)
    prog: list[tuple] = []
    for st in stages:
        st = st.strip(" ")
        if not st or any(c in st for c in "\t\n\r"):
            raise _not_mapped(text)
        if st in BUILTINS:
            prog.append(("builtin", st))
        else:
            if _has_bare_space(st):
                raise _not_mapped(text)
            prog.append(("path", _path(st, text)))
    return prog


def _has_bare_space(s: str) -> bool:
    quote = False
    for ch in s:
        if ch == '"':
            quote = not quote
        elif ch == " " and not quote:
            return True
    return False


def pointer_of(program: list[tuple]) -> list[Any] | None:
    """The JSON pointer tokens a program reads, when it is one path of
    keys and non-negative indices; else None."""
    if len(program) != 1 or program[0][0] != "path":
        return None
    out: list[Any] = []
    for op in program[0][1]:
        if op[0] == "key":
            out.append(op[1])
        elif op[0] == "index" and op[1] >= 0:
            out.append(op[1])
        else:
            return None
    return out


# --------------------------------------------------------------------------- #
# evaluation
# --------------------------------------------------------------------------- #
def _type(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "boolean"
    if isinstance(v, (Num, int)):
        return "number"
    if isinstance(v, str):
        return "string"
    if isinstance(v, list):
        return "array"
    if isinstance(v, dict):
        return "object"
    raise Refused(f"not a JSON value: {type(v).__name__}")


def _op(v: Any, op: tuple) -> list[Any]:
    kind = op[0]
    if kind == "key":
        if v is None:
            return [None]
        if isinstance(v, dict):
            return [v.get(op[1])]
        raise Refused(f"cannot index {_type(v)} with a string")
    if kind == "index":
        if v is None:
            return [None]
        if isinstance(v, list):
            i = op[1] + len(v) if op[1] < 0 else op[1]
            return [v[i] if 0 <= i < len(v) else None]
        raise Refused(f"cannot index {_type(v)} with a number")
    if kind == "slice":
        if v is None:
            return [None]
        if isinstance(v, (list, str)):
            return [v[op[1]:op[2]]]
        raise Refused(f"cannot slice {_type(v)}")
    if kind == "iter":
        if isinstance(v, list):
            return list(v)
        if isinstance(v, dict):
            return list(v.values())
        raise Refused(f"cannot iterate over {_type(v)}")
    raise Refused(f"unknown op {kind}")


def _builtin(v: Any, name: str) -> Any:
    if name == "type":
        return _type(v)
    if name == "keys":
        if isinstance(v, dict):
            return sorted(v)
        if isinstance(v, list):
            return list(range(len(v)))
        raise Refused(f"{_type(v)} has no keys")
    if name == "length":
        if v is None:
            return 0
        if isinstance(v, (str, list, dict)):
            return len(v)
        # A number's length is its absolute value as a double: exact (and
        # printed as an integer) for integers below 2**53.
        n = _small_int(v.text) if isinstance(v, Num) else None
        if n is not None:
            return abs(n)
        raise Refused(f"length of {_type(v)} {getattr(v, 'text', '')} is "
                      f"not reproduced")
    raise Refused(f"unknown builtin {name}")


def run(value: Any, program: list[tuple]) -> list[Any]:
    vals = [value]
    for stage in program:
        nxt: list[Any] = []
        for v in vals:
            if stage[0] == "builtin":
                nxt.append(_builtin(v, stage[1]))
            else:
                cur = [v]
                for op in stage[1]:
                    cur = [r for x in cur for r in _op(x, op)]
                    if len(cur) > MAX_RESULTS:
                        raise Refused("too many results")
                nxt.extend(cur)
            if len(nxt) > MAX_RESULTS:
                raise Refused("too many results")
        vals = nxt
    return vals


def _small_int(text: str) -> int | None:
    """The integer literal `text` if its magnitude is below 2**53 (exact as
    a double), else None. Long literals are not converted."""
    if len(text) > _INT_DIGITS + 1 or not re.fullmatch(r"-?[0-9]+", text):
        return None
    n = int(text)
    return n if abs(n) < 2 ** 53 else None


# --------------------------------------------------------------------------- #
# output
# --------------------------------------------------------------------------- #
def _number(v: Any, numbers: str) -> str:
    if isinstance(v, int):
        return str(v)
    text = v.text
    if numbers == "literal":
        if "e" in text or "E" in text:
            raise Refused(f"number {text} is printed in jq's own notation")
        return text
    n = _small_int(text) if text != "-0" else None
    if n is not None:
        return str(n)
    raise Refused(f"number {text} is printed as a double by this jq")


def _dump(v: Any, out: list[str], indent: int, level: int, sort: bool,
          numbers: str) -> None:
    if v is None:
        out.append("null")
    elif v is True:
        out.append("true")
    elif v is False:
        out.append("false")
    elif isinstance(v, (Num, int)):
        out.append(_number(v, numbers))
    elif isinstance(v, str):
        out.append(json_string(v))
    elif isinstance(v, (list, dict)):
        if not v:
            out.append("[]" if isinstance(v, list) else "{}")
            return
        is_obj = isinstance(v, dict)
        out.append("{" if is_obj else "[")
        items = (sorted(v.items()) if sort else list(v.items())) if is_obj \
            else v
        pad = " " * (indent * (level + 1)) if indent else ""
        for n, item in enumerate(items):
            if n:
                out.append(",")
            if indent:
                out.append("\n" + pad)
            if is_obj:
                out.append(json_string(item[0]))
                out.append(": " if indent else ":")
                _dump(item[1], out, indent, level + 1, sort, numbers)
            else:
                _dump(item, out, indent, level + 1, sort, numbers)
        if indent:
            out.append("\n" + " " * (indent * level))
        out.append("}" if is_obj else "]")
    else:
        raise Refused(f"not a JSON value: {type(v).__name__}")


def render(results: list[Any], *, raw: bool = False, compact: bool = False,
           sort_keys: bool = False, numbers: str = "literal") -> str:
    out: list[str] = []
    size = 0
    try:
        for v in results:
            if raw and isinstance(v, str):
                out.append(v)
            else:
                _dump(v, out, 0 if compact else 2, 0, sort_keys, numbers)
            out.append("\n")
            size += 1
            if size % 1000 == 0 and sum(map(len, out)) > MAX_OUTPUT_BYTES:
                raise Refused("output is too large")
    except RecursionError:
        raise Refused("value nested too deeply") from None
    text = "".join(out)
    if len(text.encode("utf-8")) > MAX_OUTPUT_BYTES:
        raise Refused("output is too large")
    return text


def load(text: str) -> Any:
    """`text` as jq reads one JSON value, or Refused."""
    if text.startswith("\ufeff"):
        raise Refused("input starts with a byte order mark")
    if not text.strip(" \t\r\n"):
        raise Refused("empty input")
    if _LONE_SURROGATE_RE.search(text):
        raise Refused("input has a lone surrogate escape")

    def constant(name: str) -> Any:
        raise Refused(f"{name} is not JSON")

    dup = []

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        d = dict(items)
        if len(d) != len(items):
            dup.append(True)
        return d

    try:
        value = json.loads(text, parse_int=Num, parse_float=Num,
                           parse_constant=constant, object_pairs_hook=pairs)
    except json.JSONDecodeError as e:
        raise Refused(f"not one JSON value: {e}") from None
    except RecursionError:
        raise Refused("input nested too deeply") from None
    # A duplicate key drops a value, which may be the deep one: scan the text.
    if not (_text_depth_ok(text) if dup else _depth_ok(value)):
        raise Refused(f"input is nested deeper than jq's parse limit "
                      f"({MAX_DEPTH})")
    return value


def _depth_ok(value: Any) -> bool:
    """Whether jq's parser stack stays within MAX_DEPTH for `value`: a
    container opens at the stack size of its parent plus one (array) or
    two (object: the key)."""
    todo = [(value, 0)]
    while todo:
        x, d = todo.pop()
        if d >= MAX_DEPTH:
            return False
        if type(x) is list:
            todo.extend((c, d + 1) for c in x if type(c) in (list, dict))
        elif type(x) is dict:
            todo.extend((c, d + 2) for c in x.values()
                        if type(c) in (list, dict))
    return True


_DEPTH_TOKEN_RE = re.compile(r'"(?:[^"\\]|\\.)*"|[\[\]{}:,]')


def _text_depth_ok(text: str) -> bool:
    """`_depth_ok` on the text of valid JSON (slower; sees every value of
    a duplicate key)."""
    d = 0
    stack: list[int] = []  # 0 array, 1 object, 2 object inside a value
    for m in _DEPTH_TOKEN_RE.finditer(text):
        c = m.group()
        if c[0] == '"':
            continue
        if c in "[{":
            if d >= MAX_DEPTH:
                return False
            stack.append(1 if c == "{" else 0)
            d += 1
        elif c == ":":
            stack[-1] = 2
            d += 1
        elif c == ",":
            if stack and stack[-1] == 2:
                stack[-1] = 1
                d -= 1
        else:
            if stack and stack.pop() == 2:
                d -= 1
            d -= 1
    return True


def run_text(text: str, program: list[tuple], *, raw: bool = False,
             compact: bool = False, sort_keys: bool = False,
             numbers: str = "literal") -> str:
    return render(run(load(text), program), raw=raw, compact=compact,
                  sort_keys=sort_keys, numbers=numbers)


_VERSION: dict[tuple, tuple[int, int] | None] = {}


def jq_version() -> tuple[int, int] | None:
    """(major, minor) of the `jq` on the daemon's PATH, or None when there
    is none. Cached by PATH, the binary's path and its mtime, so installing
    or upgrading jq is seen on the next call."""
    import os
    import shutil
    import subprocess
    exe = shutil.which("jq")
    if exe is None:
        return None
    try:
        st = os.stat(exe)
    except OSError:
        return None
    ck = (os.environ.get("PATH", ""), exe, st.st_mtime_ns, st.st_size)
    if ck in _VERSION:
        return _VERSION[ck]
    got = None
    try:
        proc = subprocess.run([exe, "--version"], capture_output=True,
                              text=True, timeout=5,
                              stdin=subprocess.DEVNULL, check=False)
        m = re.search(r"(\d+)\.(\d+)", proc.stdout or "")
        if m:
            got = (int(m.group(1)), int(m.group(2)))
    except (OSError, subprocess.SubprocessError):
        got = None
    _VERSION.clear()
    _VERSION[ck] = got
    return got


def number_policy() -> str:
    """`literal` for jq 1.7 and 1.8 (checked), `integers` (the output every
    version shares) for older or newer versions and when jq is absent."""
    v = jq_version()
    return "literal" if v is not None and (1, 7) <= v <= (1, 8) else "integers"


__all__ = ["BUILTINS", "MAX_DEPTH", "Refused", "Unsupported", "jq_version",
           "load", "number_policy", "parse", "pointer_of", "render", "run",
           "run_text"]
