"""ozagia.edit.span_edit — exact text-span edits, mapped to graph nodes.

The backend for an agent's ordinary `edit(path, old_string, new_string)`
call: the agent names text, ozagia finds it, names the graph node that
encloses it, guards the file's parse, writes through `ozagia.edit.txn` and
reports the graph delta and the references the edit breaks.

Matching (`locate`). `old_string` is searched in the decoded file text (BOM
stripped, the file's own newlines kept). When it does not occur as given, it
is searched again with its newlines converted to the file's newline style.
Matches do not overlap. No match is an error listing the nearest lines
(difflib ratio against the first non-blank line of `old_string`); more than
one match without `replace_all` is an error listing every match (OpenCode's
uniqueness rule). Both errors are returned as `{ok: False, error, ...}`;
read failures (missing, binary, oversized, non-UTF-8, stale base) raise
`CodeEditError`.

Enclosing key per match, by suffix:

    .py              innermost def/class containing the matched lines
                     (`ozagia.check.symbol_at`): sym:/test:, else file:
    .md, .markdown   doc:<rel>, plus `section`: the heading path of the
                     innermost section containing the span ([] before the
                     first heading)
    .json            file:<rel>, plus `pointer`: the JSON pointer of the
                     innermost value containing the span (None when the file
                     does not parse)
    .rs, .lua and the other tag-language suffixes
                     file:<rel>
    anything else    text:<rel>#L<a>-L<b>, the `text_nodes` chunk holding
                     the first matched line

Parse guard. Before and after the edit the file is parsed: `.py` with `ast`,
`.json` with `json.loads`, `.rs`/`.lua` with tree-sitter (`has_error` on the
root) when the grammar is importable. If the old text parsed and the new
text does not, nothing is written and the result is `{ok: False, error,
parse_error: {line, col, message}, key}`. `parse_check` is "ok", "refused",
"was_broken" (old text did not parse; not guarded), "unavailable" (no
grammar) or "none" (no parser for the suffix).

Broken references (Python only, on the new text, no symbol-graph build).
F is the edited file. Compared before and after: F's module-level names
(defs, classes, assignments, imports, including those inside module-level
if/try/with/for blocks) and the signatures of its module-level functions and
of methods defined directly in its classes. A signature is judged only for
an undecorated def or a staticmethod/classmethod, defined once. Checked
files: F itself, and with a `DevMemory`, every file with an `imports` edge to
`file:F` whose text spells a removed name or a changed function's name (the
others cannot hold a reported reference and are not parsed). Reported:

  * `missing_name`: `from M import name` of a removed name (at the import
    line); `mod.name` of a removed name where `mod` is F imported as a module;
    in F, a call `name(...)` of a removed module-level name.
  * `arity`: a call `name(...)` (imported from F, or in F itself) or
    `mod.name(...)` of a function whose signature changed, and in F a
    `self.m(...)` call inside a method of the class that defines `m`, that
    does not bind to the new signature (too many or missing positional
    arguments, unexpected, duplicate or missing keyword arguments).

A call with `*args` or `**kwargs` at the call site is skipped, as is a name
bound locally in an enclosing function (shadowing). Lists stop at
`MAX_BROKEN_REFERENCES` with `broken_references_truncated: True`.
"""
from __future__ import annotations

import ast
import difflib
import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from ozagia.core.keys import file_key, format_key
from ozagia.edit.code_edit import MAX_SOURCE_BYTES, CodeEditError
from ozagia.edit.txn import (
    SourceFile,
    check_base,
    encode_text,
    in_git_dir,
    normalize_newlines,
    read_source,
    sha256_bytes,
    write_source,
)

MAX_BROKEN_REFERENCES = 50
NEAREST_LINES = 3
BINARY_SNIFF_BYTES = 8 * 1024
_DIFF_MAX_LINES = 400

_MD_SUFFIXES = frozenset({".md", ".markdown"})
_TS_LANGUAGES = {".rs": "rust", ".lua": "lua"}
_JUDGED_DECORATORS = frozenset({"staticmethod", "classmethod"})


# --------------------------------------------------------------------------- #
# Reading and matching
# --------------------------------------------------------------------------- #

def _read(root: str | Path, rel: str) -> SourceFile:
    rel = str(rel or "").strip()
    if in_git_dir(rel):
        raise CodeEditError(f"{rel} is inside .git; refused")
    suffix = PurePosixPath(rel).suffix.lower()
    src = read_source(root, rel, suffixes=(suffix,), max_bytes=MAX_SOURCE_BYTES,
                      error_cls=CodeEditError)
    if in_git_dir(src.rel):
        raise CodeEditError(f"{src.rel} is inside .git; refused")
    if b"\0" in src.raw[:BINARY_SNIFF_BYTES]:
        raise CodeEditError(f"{src.rel} is binary")
    return src


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _find_all(text: str, needle: str) -> list[int]:
    out: list[int] = []
    i = text.find(needle)
    while i >= 0:
        out.append(i)
        i = text.find(needle, i + len(needle))
    return out


def _md_section(text: str, start: int, end: int) -> list[str]:
    from ozagia.edit.markdown import headings
    chain = [h for h in headings(text)
             if h.start <= start and end <= h.section_end]
    chain.sort(key=lambda h: (h.start, h.level))
    return [h.title for h in chain]


def _json_pointer(tree: Any, start: int, end: int) -> str | None:
    if tree is None:
        return None
    node, parts = tree, []
    while True:
        nxt = None
        if node.kind == "object":
            for m in node.members:
                v = m["value"]
                if v.start <= start and end <= v.end:
                    nxt, tok = v, str(m["key"])
                    break
        elif node.kind == "array":
            for i, v in enumerate(node.items):
                if v.start <= start and end <= v.end:
                    nxt, tok = v, str(i)
                    break
        if nxt is None:
            break
        parts.append(tok.replace("~", "~0").replace("/", "~1"))
        node = nxt
    return "".join("/" + p for p in parts)


class _Keyer:
    """Enclosing keys for spans of one file text."""

    def __init__(self, root: str | Path, rel: str, text: str, *,
                 py_tree: ast.Module | None = None) -> None:
        self.root, self.rel, self.text = root, rel, text
        self.py_tree = py_tree
        self.suffix = PurePosixPath(rel).suffix.lower()
        self._json: Any = False
        self._chunks: list[dict[str, Any]] | None = None

    def key(self, start: int, end: int, start_line: int,
            end_line: int) -> dict[str, Any]:
        from ozagia.index.text_nodes import is_structured
        if self.suffix == ".py":
            from ozagia.check import symbol_at
            return {"key": symbol_at(self.root, self.rel, start_line,
                                     end_line=end_line, source=self.text,
                                     tree=self.py_tree)}
        if self.suffix in _MD_SUFFIXES:
            return {"key": format_key(("doc", self.rel)),
                    "section": _md_section(self.text, start, end)}
        if self.suffix == ".json":
            if self._json is False:
                from ozagia.edit.json_edit import _Parser
                try:
                    self._json = _Parser(self.text).parse()
                except CodeEditError:
                    self._json = None
            return {"key": file_key(self.rel),
                    "pointer": _json_pointer(self._json, start, end)}
        if is_structured(self.rel):
            return {"key": file_key(self.rel)}
        from ozagia.index.text_nodes import locate_chunk, text_chunks
        if self._chunks is None:
            self._chunks = text_chunks(self.rel, self.text)
        chunk = locate_chunk(self._chunks, start_line)
        return {"key": chunk["key"] if chunk else file_key(self.rel)}


def _nearest(text: str, old: str, keyer: _Keyer) -> list[dict[str, Any]]:
    target = next((ln.strip() for ln in old.splitlines() if ln.strip()), "")
    if not target:
        return []
    sm = difflib.SequenceMatcher(autojunk=False)
    sm.set_seq2(target)
    ranked: list[tuple[float, int, str]] = []
    for i, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        sm.set_seq1(stripped)
        floor = ranked[-1][0] if len(ranked) >= NEAREST_LINES else -1.0
        if sm.real_quick_ratio() <= floor or sm.quick_ratio() <= floor:
            continue
        r = sm.ratio()
        if r > floor:
            ranked.append((r, i, line))
            ranked.sort(key=lambda t: (-t[0], t[1]))
            del ranked[NEAREST_LINES:]
    offsets = _offsets(text)
    out = []
    for r, i, line in ranked:
        start = offsets[i - 1]
        out.append({"line": i, "text": line, "ratio": round(r, 3),
                    **keyer.key(start, start + len(line), i, i)})
    return out


def _offsets(text: str) -> list[int]:
    out = [0]
    i = text.find("\n")
    while i >= 0:
        out.append(i + 1)
        i = text.find("\n", i + 1)
    return out


def _locate(root: str | Path, src: SourceFile, old: str, *,
            replace_all: bool, keyer: _Keyer | None = None) -> dict[str, Any]:
    if not isinstance(old, str) or old == "":
        raise CodeEditError("old_string must be a non-empty string")
    text = src.text
    needle = old
    starts = _find_all(text, needle)
    converted = False
    if not starts:
        alt = normalize_newlines(old, src.newline)
        if alt != old:
            starts = _find_all(text, alt)
            if starts:
                needle, converted = alt, True
    keyer = keyer or _Keyer(root, src.rel, text)
    if not starts:
        return {"ok": False, "file": src.rel,
                "error": f"old_string not found in {src.rel}",
                "nearest": _nearest(text, old, keyer)}
    matches = []
    for s in starts:
        e = s + len(needle)
        sl = _line_of(text, s)
        el = _line_of(text, e - 1)
        matches.append({"start": s, "end": e, "start_line": sl,
                        "end_line": el, **keyer.key(s, e, sl, el)})
    out = {"file": src.rel, "matches": matches,
           "keys": list(dict.fromkeys(m["key"] for m in matches)),
           "newline_converted": converted}
    if len(matches) > 1 and not replace_all:
        where = ", ".join(f"L{m['start_line']} ({m['key']})" for m in matches)
        return {"ok": False, **out,
                "error": (f"old_string occurs {len(matches)} times in "
                          f"{src.rel}: {where}; add surrounding context to "
                          f"make it unique, or set replace_all")}
    return {"ok": True, **out}


def locate(root: str | Path, rel: str, old_string: str, *,
           replace_all: bool = False) -> dict[str, Any]:
    """Find `old_string` in `rel` and name the node enclosing each match.

    Returns `{ok: True, file, matches: [{start, end, start_line, end_line,
    key, ...}], keys, newline_converted}`, or `{ok: False, error, ...}` with
    `nearest` (no match) or `matches` (ambiguous without `replace_all`).
    """
    return _locate(root, _read(root, rel), old_string, replace_all=replace_all)


# --------------------------------------------------------------------------- #
# Parse guard
# --------------------------------------------------------------------------- #

def _ts_first_error(node: Any) -> Any:
    stack = [node]
    while stack:
        n = stack.pop()
        if n.type == "ERROR" or n.is_missing:
            return n
        if n.has_error:
            stack.extend(reversed(n.children))
    return node


def _parse_py(text: str) -> tuple[str, dict[str, Any] | None,
                                  ast.Module | None]:
    try:
        return "ok", None, ast.parse(text)
    except SyntaxError as e:
        return "error", {"line": e.lineno, "col": e.offset,
                         "message": e.msg}, None
    except ValueError as e:
        return "error", {"line": None, "col": None, "message": str(e)}, None


def _parse_error(rel: str, text: str) -> tuple[str, dict[str, Any] | None]:
    """("ok" | "none" | "unavailable", None) or ("error", {line, col, message})."""
    suffix = PurePosixPath(rel).suffix.lower()
    if suffix == ".py":
        return _parse_py(text)[:2]
    if suffix == ".json":
        try:
            json.loads(text)
        except json.JSONDecodeError as e:
            return "error", {"line": e.lineno, "col": e.colno,
                             "message": e.msg}
        return "ok", None
    language = _TS_LANGUAGES.get(suffix)
    if language:
        from ozagia.edit.cst import _make_parser
        parser, _reason = _make_parser(language)
        if parser is None:
            return "unavailable", None
        try:
            tree = parser.parse(text.encode("utf-8"))
        except Exception:  # noqa: BLE001 - a parser failure is not a verdict
            return "unavailable", None
        if not tree.root_node.has_error:
            return "ok", None
        bad = _ts_first_error(tree.root_node)
        row, col = bad.start_point
        what = f"missing {bad.type}" if bad.is_missing else "syntax error"
        return "error", {"line": row + 1, "col": col + 1,
                         "message": f"tree-sitter: {what}"}
    return "none", None


# --------------------------------------------------------------------------- #
# Broken references (Python)
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class _Sig:
    params: tuple[str, ...]          # positional parameters, in order
    posonly: int                      # how many of them are positional-only
    required: int                     # positional parameters without default
    vararg: bool
    kwonly: tuple[tuple[str, bool], ...]  # (name, required)
    kwarg: bool
    text: str

    def shape(self) -> tuple:
        return (self.params, self.posonly, self.required, self.vararg,
                self.kwonly, self.kwarg)

    def max_text(self) -> str:
        return "inf" if self.vararg else str(len(self.params))


def _sig(fn: ast.FunctionDef | ast.AsyncFunctionDef, *, drop_first: bool) -> _Sig:
    a = fn.args
    pos = [p.arg for p in (*a.posonlyargs, *a.args)]
    required = len(pos) - len(a.defaults)
    posonly = len(a.posonlyargs)
    if drop_first and pos:
        pos = pos[1:]
        required = max(0, required - 1)
        posonly = max(0, posonly - 1)
    kwonly = tuple((p.arg, d is None) for p, d in zip(a.kwonlyargs, a.kw_defaults))
    return _Sig(tuple(pos), posonly, required, a.vararg is not None, kwonly,
                a.kwarg is not None, f"{fn.name}({ast.unparse(a)})")


def _decorators(fn: ast.AST) -> set[str]:
    return {ast.unparse(d) for d in getattr(fn, "decorator_list", [])}


def _target_names(t: ast.AST) -> Iterable[str]:
    if isinstance(t, ast.Name):
        yield t.id
    elif isinstance(t, (ast.Tuple, ast.List)):
        for e in t.elts:
            yield from _target_names(e)
    elif isinstance(t, ast.Starred):
        yield from _target_names(t.value)


def _module_names(tree: ast.Module) -> tuple[set[str], bool]:
    """Module-level names of a module, and whether they are open-ended
    (a star import or a module `__getattr__`)."""
    names: set[str] = set()
    opaque = False

    def walk(body: list[ast.stmt]) -> None:
        nonlocal opaque
        for st in body:
            if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef,
                               ast.ClassDef)):
                names.add(st.name)
                if st.name == "__getattr__":
                    opaque = True
            elif isinstance(st, ast.Assign):
                for t in st.targets:
                    names.update(_target_names(t))
            elif isinstance(st, (ast.AnnAssign, ast.AugAssign)):
                names.update(_target_names(st.target))
            elif isinstance(st, ast.Import):
                for al in st.names:
                    names.add(al.asname or al.name.split(".")[0])
            elif isinstance(st, ast.ImportFrom):
                for al in st.names:
                    if al.name == "*":
                        opaque = True
                    else:
                        names.add(al.asname or al.name)
            elif isinstance(st, (ast.For, ast.AsyncFor)):
                names.update(_target_names(st.target))
                walk(st.body)
                walk(st.orelse)
            elif isinstance(st, (ast.With, ast.AsyncWith)):
                for item in st.items:
                    if item.optional_vars is not None:
                        names.update(_target_names(item.optional_vars))
                walk(st.body)
            elif isinstance(st, (ast.If, ast.While)):
                walk(st.body)
                walk(st.orelse)
            elif isinstance(st, ast.Try) or type(st).__name__ == "TryStar":
                walk(st.body)
                for h in st.handlers:
                    if h.name:
                        names.add(h.name)
                    walk(h.body)
                walk(st.orelse)
                walk(st.finalbody)
    walk(tree.body)
    return names, opaque


def _signatures(tree: ast.Module) -> tuple[dict[str, _Sig], dict[str, _Sig]]:
    """(module-level function name -> signature for a direct call,
    "Class.method" -> signature for a `self.method(...)` call), for the
    defs that are judged (see the module docstring)."""
    funcs: dict[str, _Sig] = {}
    methods: dict[str, _Sig] = {}
    seen: dict[str, int] = {}
    fn_types = (ast.FunctionDef, ast.AsyncFunctionDef)
    for st in tree.body:
        if isinstance(st, fn_types):
            seen[st.name] = seen.get(st.name, 0) + 1
            if not _decorators(st):
                funcs[st.name] = _sig(st, drop_first=False)
        elif isinstance(st, ast.ClassDef):
            seen[st.name] = seen.get(st.name, 0) + 1
            for m in st.body:
                if not isinstance(m, fn_types):
                    continue
                q = f"{st.name}.{m.name}"
                seen[q] = seen.get(q, 0) + 1
                decos = _decorators(m)
                if decos - _JUDGED_DECORATORS:
                    continue
                methods[q] = _sig(m, drop_first="staticmethod" not in decos)
    funcs = {k: v for k, v in funcs.items() if seen.get(k) == 1}
    methods = {k: v for k, v in methods.items() if seen.get(k) == 1}
    return funcs, methods


def _changed(old: dict[str, _Sig], new: dict[str, _Sig]) -> dict[str, _Sig]:
    return {q: s for q, s in new.items()
            if q in old and old[q].shape() != s.shape()}


def _call_problem(call: ast.Call, sig: _Sig) -> str | None:
    n = len(call.args)
    kws = [k.arg for k in call.keywords]
    problems: list[str] = []
    if not sig.vararg and n > len(sig.params):
        problems.append(f"{n} positional given, at most {len(sig.params)}")
    filled = set(sig.params[:n])
    by_kw = set(sig.params[sig.posonly:])
    kwonly = {k for k, _ in sig.kwonly}
    for k in kws:
        if k in filled and k in by_kw:
            problems.append(f"multiple values for {k!r}")
        elif k in by_kw or k in kwonly:
            continue
        elif not sig.kwarg:
            problems.append(f"unexpected keyword {k!r}")
    missing = [p for i, p in enumerate(sig.params)
               if n <= i < sig.required and (i < sig.posonly or p not in kws)]
    missing += [k for k, req in sig.kwonly if req and k not in kws]
    if missing:
        problems.append("missing " + ", ".join(repr(m) for m in missing))
    return "; ".join(problems) or None


def _call_text(call: ast.Call) -> str:
    kws = [k.arg for k in call.keywords if k.arg]
    text = f"{len(call.args)} positional"
    return text + (f", kw {', '.join(kws)}" if kws else "")


def _dotted(node: ast.AST) -> str | None:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return ".".join(reversed(parts))


def _local_bindings(fn: ast.AST, skip: set[int]) -> set[str]:
    """Names bound anywhere inside a def (parameters included), nested defs
    included: an over-approximation, so shadowing is never missed. Import
    statements whose id is in `skip` (imports from F) do not shadow."""
    out: set[str] = set()
    for n in ast.walk(fn):
        if id(n) in skip:
            continue
        if isinstance(n, ast.arg):
            out.add(n.arg)
        elif isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            out.add(n.id)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef,
                            ast.ClassDef)) and n is not fn:
            out.add(n.name)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            for al in n.names:
                out.add(al.asname or al.name.split(".")[0])
    return out


def _f_module_names(rel: str) -> set[str]:
    from ozagia.core import memory as M
    p = PurePosixPath(rel)
    if p.suffix != ".py":
        return set()
    out: set[str] = set()
    for r in [None, *[r for r in M.IMPORT_ROOTS if r != "."]]:
        if r is None:
            sub = p
        else:
            try:
                sub = p.relative_to(r)
            except ValueError:
                continue
        parts = list(sub.with_suffix("").parts)
        if parts and parts[-1] == "__init__":
            parts = parts[:-1]
        if parts:
            out.add(".".join(parts))
    return out


class _RefScan:
    """Walks one file's AST and collects broken references to F."""

    def __init__(self, ctx: "_RefContext", rel: str, tree: ast.Module,
                 source: str | None) -> None:
        # `source`: the text `tree` was parsed from when it is not on disk.
        self.ctx, self.rel, self.tree, self.source = ctx, rel, tree, source
        self.in_f = rel == ctx.rel
        self.names: dict[str, str] = {}     # local name -> F module-level name
        self.mods: set[str] = set()         # dotted expressions naming F
        self.import_lines: list[tuple[int, str]] = []
        self.f_imports: set[int] = set()    # ids of import statements from F

    # -- import resolution -------------------------------------------------- #
    def _is_f(self, module: str, level: int) -> bool:
        if level == 0:
            return module in self.ctx.module_names
        from ozagia.core import memory as M
        return self.ctx.rel in M._import_candidates(self.rel, module, level)

    def collect_imports(self) -> None:
        ctx = self.ctx
        wanted = ctx.removed | set(ctx.changed_funcs)
        if self.in_f:
            for name in (ctx.after_names - ctx.after_imported_only) | ctx.removed:
                if name in wanted:
                    self.names[name] = name
            return
        for node in _statements(self.tree.body):
            if isinstance(node, ast.Import):
                for al in node.names:
                    if al.name in ctx.module_names:
                        self.mods.add(al.asname or al.name)
                        self.f_imports.add(id(node))
            elif isinstance(node, ast.ImportFrom):
                module, level = node.module or "", int(node.level or 0)
                from_f = self._is_f(module, level)
                for al in node.names:
                    if al.name == "*":
                        continue
                    local = al.asname or al.name
                    if from_f:
                        self.f_imports.add(id(node))
                        if al.name in wanted:
                            self.names[local] = al.name
                        line = int(getattr(al, "lineno", None) or node.lineno)
                        self.import_lines.append((line, al.name))
                    elif self._is_f(f"{module}.{al.name}" if module
                                    else al.name, level):
                        self.f_imports.add(id(node))
                        self.mods.add(local)

    # -- reporting ----------------------------------------------------------- #
    def _key(self, line: int) -> str:
        from ozagia.check import symbol_at
        return symbol_at(self.ctx.root, self.rel, line, source=self.source,
                         tree=self.tree if self.source is not None else None)

    def _add(self, kind: str, line: int, name: str, **extra: Any) -> None:
        self.ctx.add({"kind": kind, "key": self._key(line), "file": self.rel,
                      "line": line, "name": name, **extra})

    def _arity(self, call: ast.Call, name: str, sig: _Sig) -> None:
        if any(isinstance(a, ast.Starred) for a in call.args) or any(
                k.arg is None for k in call.keywords):
            self.ctx.skipped_calls += 1
            return
        problem = _call_problem(call, sig)
        if problem:
            self._add("arity", call.lineno, name, call=_call_text(call),
                      signature=sig.text, problem=problem)

    def run(self) -> None:
        ctx = self.ctx
        self.collect_imports()
        for line, name in self.import_lines:
            if name in ctx.removed:
                self._add("missing_name", line, name)
        mod_attrs = (ctx.removed | set(ctx.changed_funcs)) if self.mods else set()
        # In F: changed methods, by method name, for `self.m(...)` calls.
        method_names = ({q.rsplit(".", 1)[1] for q in ctx.changed_methods}
                        if self.in_f else set())
        if not (self.names or mod_attrs or method_names):
            return
        # One walk: candidate nodes (by the name they spell) and the defs.
        fn_types = (ast.FunctionDef, ast.AsyncFunctionDef)
        defs: list[ast.AST] = []
        cands: list[ast.AST] = []
        for node in ast.walk(self.tree):
            if isinstance(node, fn_types):
                defs.append(node)
            elif isinstance(node, ast.Call):
                f = node.func
                if isinstance(f, ast.Name):
                    if f.id in self.names:
                        cands.append(node)
                elif isinstance(f, ast.Attribute) and (
                        f.attr in mod_attrs or f.attr in method_names):
                    cands.append(node)
            elif (isinstance(node, ast.Attribute) and node.attr in ctx.removed
                  and self.mods and isinstance(node.ctx, ast.Load)):
                cands.append(node)
        if not cands:
            return
        # Methods of F's top-level classes: id -> (class, receiver name).
        methods: dict[int, tuple[str, str]] = {}
        if method_names:
            for c in self.tree.body:
                if not isinstance(c, ast.ClassDef):
                    continue
                for m in c.body:
                    if not isinstance(m, fn_types):
                        continue
                    pos = [*m.args.posonlyargs, *m.args.args]
                    if pos and "staticmethod" not in _decorators(m):
                        methods[id(m)] = (c.name, pos[0].arg)
        bindings: dict[int, set[str]] = {}

        def scope(line: int) -> list[ast.AST]:
            # Defs whose body holds `line`; decorators and defaults are in
            # the enclosing scope.
            return [d for d in defs
                    if d.body[0].lineno <= line <= (d.end_lineno or d.lineno)]

        def shadowed(name: str, chain: list[ast.AST]) -> bool:
            for d in chain:
                if id(d) not in bindings:
                    bindings[id(d)] = _local_bindings(d, self.f_imports)
                if name in bindings[id(d)]:
                    return True
            return False

        cands.sort(key=lambda n: (n.lineno, n.col_offset))
        for node in cands:
            if ctx.truncated:
                return
            chain = scope(node.lineno)
            if isinstance(node, ast.Attribute):
                base = _dotted(node.value)
                if base in self.mods and not shadowed(base.split(".")[0], chain):
                    self._add("missing_name", node.lineno, node.attr)
                continue
            func = node.func
            if isinstance(func, ast.Name):
                if shadowed(func.id, chain):
                    continue
                name = self.names[func.id]
                if self.in_f and name in ctx.removed:
                    self._add("missing_name", node.lineno, name)
                elif name in ctx.changed_funcs:
                    self._arity(node, name, ctx.changed_funcs[name])
                continue
            base = _dotted(func.value)
            if base in self.mods:
                if (func.attr in ctx.changed_funcs
                        and not shadowed(base.split(".")[0], chain)):
                    self._arity(node, func.attr, ctx.changed_funcs[func.attr])
            elif isinstance(func.value, ast.Name):
                owner = next((methods[id(d)] for d in chain
                              if id(d) in methods), None)
                if owner and func.value.id == owner[1]:
                    q = f"{owner[0]}.{func.attr}"
                    if q in ctx.changed_methods:
                        self._arity(node, q, ctx.changed_methods[q])


def _statements(body: list[ast.stmt]) -> Iterable[ast.stmt]:
    """Every statement in `body`, nested bodies included (defs, classes,
    if/for/while/with/try/match)."""
    stack = list(reversed(body))
    while stack:
        st = stack.pop()
        yield st
        nested: list[ast.stmt] = []
        for attr in ("body", "orelse", "finalbody"):
            part = getattr(st, attr, None)
            if isinstance(part, list):
                nested.extend(part)
        for h in getattr(st, "handlers", None) or []:
            nested.extend(h.body)
        for case in getattr(st, "cases", None) or []:
            nested.extend(case.body)
        stack.extend(reversed([n for n in nested if isinstance(n, ast.stmt)]))


class _RefContext:
    def __init__(self, root: str | Path, rel: str, old: ast.Module,
                 new: ast.Module) -> None:
        self.root, self.rel = root, rel
        before, before_open = _module_names(old)
        after, after_open = _module_names(new)
        self.after_names = after
        # Names F only imports: a bare call `name(...)` in F is not F's def.
        defined = {st.name for st in new.body
                   if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef))}
        self.after_imported_only = after - defined
        removed = set() if (before_open or after_open) else before - after
        if PurePosixPath(rel).name == "__init__.py":
            pkg = Path(root) / PurePosixPath(rel).parent
            removed = {n for n in removed
                       if not (pkg / f"{n}.py").is_file()
                       and not (pkg / n / "__init__.py").is_file()}
        self.removed = removed
        of, om = _signatures(old)
        nf, nm = _signatures(new)
        self.changed_funcs = _changed(of, nf)
        self.changed_methods = _changed(om, nm)
        self.module_names = _f_module_names(rel)
        self.refs: list[dict[str, Any]] = []
        self.truncated = False
        self.skipped_calls = 0

    def add(self, ref: dict[str, Any]) -> None:
        if len(self.refs) >= MAX_BROKEN_REFERENCES:
            self.truncated = True
            return
        self.refs.append(ref)


def _importers(memory: Any, rel: str) -> list[str]:
    if memory is None:
        return []
    nid = memory.node_by_key(file_key(rel))
    if nid is None:
        return []
    out = []
    for e in memory._in.get(nid, []):
        if e.get("type") != "imports":
            continue
        key = memory.key_of(int(e["src"])) or ""
        if key.startswith("file:") and key[5:] != rel:
            out.append(key[5:])
    return sorted(set(out))


def broken_references(root: str | Path, rel: str, old_text: str,
                      new_text: str, *, memory: Any = None,
                      old_tree: ast.Module | None = None,
                      new_tree: ast.Module | None = None) -> dict[str, Any]:
    """Broken references of replacing `old_text` by `new_text` in `rel`.

    Returns `{refs, truncated, info}`. See the module docstring for the rules.
    `old_tree`/`new_tree` are the parsed texts, when the caller has them.
    """
    if PurePosixPath(rel).suffix.lower() != ".py":
        return {"refs": [], "truncated": False, "info": {"checked": False,
                "reason": "python only"}}
    old = old_tree or _parse_py(old_text)[2]
    new = new_tree or _parse_py(new_text)[2]
    if old is None or new is None:
        return {"refs": [], "truncated": False, "info": {"checked": False,
                "reason": "the old or new text does not parse"}}
    ctx = _RefContext(root, rel, old, new)
    importers = _importers(memory, rel)
    info: dict[str, Any] = {
        "checked": True,
        "removed_names": sorted(ctx.removed),
        "changed_signatures": sorted([*ctx.changed_funcs,
                                      *ctx.changed_methods]),
        "importers": len(importers),
        "importers_unreadable": [],
    }
    _RefScan(ctx, rel, new, new_text).run()
    # Every reference this scan reports spells the name, so an importer whose
    # text does not contain one of the names cannot have one: it is not parsed.
    names = sorted({*ctx.removed, *ctx.changed_funcs})
    # Not preceded or followed by an identifier byte (UTF-8 bytes included).
    needle = re.compile(
        rb"(?<![A-Za-z0-9_\x80-\xff])(?:"
        + b"|".join(re.escape(n.encode("utf-8")) for n in names)
        + rb")(?![A-Za-z0-9_\x80-\xff])") if names else None
    parsed = 0
    for imp in importers if needle else []:
        if ctx.truncated:
            break
        try:
            data = (Path(root) / imp).read_bytes()
        except OSError:
            info["importers_unreadable"].append(imp)
            continue
        if not needle.search(data):
            continue
        try:
            tree = ast.parse(data)
        except (SyntaxError, ValueError):
            info["importers_unreadable"].append(imp)
            continue
        parsed += 1
        _RefScan(ctx, imp, tree, None).run()
    info["importers_parsed"] = parsed
    info["calls_skipped_star_args"] = ctx.skipped_calls
    return {"refs": ctx.refs, "truncated": ctx.truncated, "info": info}


# --------------------------------------------------------------------------- #
# The edit
# --------------------------------------------------------------------------- #

def apply_span_edit(root: str | Path, rel: str, old_string: str,
                    new_string: str, *, replace_all: bool = False,
                    base_sha256: str | None = None, write: bool = False,
                    memory: Any = None) -> dict[str, Any]:
    """Replace `old_string` by `new_string` in `rel` (see the module
    docstring). Preview by default; `write=True` writes through
    `ozagia.edit.txn`, and with a `DevMemory` reingests the file and returns
    `graph_delta`."""
    if not isinstance(new_string, str):
        raise CodeEditError("new_string must be a string")
    src = _read(root, rel)
    check_base(src, base_sha256, CodeEditError)
    if old_string == new_string:
        raise CodeEditError("old_string and new_string are identical")
    is_py = PurePosixPath(src.rel).suffix.lower() == ".py"
    if is_py:
        before_status, _, old_tree = _parse_py(src.text)
    else:
        (before_status, _), old_tree = _parse_error(src.rel, src.text), None
    loc = _locate(root, src, old_string, replace_all=replace_all,
                  keyer=_Keyer(root, src.rel, src.text, py_tree=old_tree))
    if not loc["ok"]:
        return {**loc, "applied": False}
    matches = loc["matches"]
    replacement = normalize_newlines(new_string, src.newline)
    text = src.text
    parts: list[str] = []
    cursor = 0
    for m in matches:
        parts.append(text[cursor:m["start"]])
        parts.append(replacement)
        cursor = m["end"]
    parts.append(text[cursor:])
    new_text = "".join(parts)

    if is_py:
        after_status, after_error, new_tree = _parse_py(new_text)
    else:
        (after_status, after_error), new_tree = (
            _parse_error(src.rel, new_text), None)
    if before_status == "error":
        parse_check = "was_broken"
    elif after_status == "error":
        return {"ok": False, "applied": False, "file": src.rel,
                "error": (f"the edit breaks the parse of {src.rel} at line "
                          f"{after_error['line']}: {after_error['message']}"),
                "parse_error": after_error, "parse_check": "refused",
                "key": matches[0]["key"], "keys": loc["keys"],
                "matches": matches}
    else:
        parse_check = after_status

    refs = broken_references(root, src.rel, text, new_text, memory=memory,
                             old_tree=old_tree, new_tree=new_tree)
    new_sha = sha256_bytes(encode_text(src, new_text))
    diff_lines = list(difflib.unified_diff(
        text.splitlines(keepends=True), new_text.splitlines(keepends=True),
        fromfile=f"a/{src.rel}", tofile=f"b/{src.rel}"))
    backup = None
    applied = False
    if write:
        backup = write_source(root, src, new_text, error_cls=CodeEditError)
        applied = True
    result: dict[str, Any] = {
        "ok": True,
        "applied": applied,
        "format": "span",
        "file": src.rel,
        "matches": matches,
        "keys": loc["keys"],
        "newline_converted": loc["newline_converted"],
        "base_sha256": src.sha256,
        "new_sha256": new_sha,
        "before": {"sha256": src.sha256, "lines": len(text.splitlines())},
        "after": {"sha256": new_sha, "lines": len(new_text.splitlines())},
        "backup": backup,
        "diff_view": "".join(diff_lines[:_DIFF_MAX_LINES]),
        "parse_check": parse_check,
        "broken_references": refs["refs"],
        "broken_references_truncated": refs["truncated"],
        "broken_references_info": refs["info"],
        "graph_stale": applied and memory is None,
    }
    if applied and memory is not None:
        # A reingest failure is reported, not raised: the edit is written.
        try:
            result["graph_delta"] = memory.reingest([src.rel]).to_dict()
        except Exception as e:  # noqa: BLE001 - recorded, not raised
            result["graph_delta"] = {"error": f"{type(e).__name__}: {e}"}
    return result


__all__ = [
    "MAX_BROKEN_REFERENCES",
    "apply_span_edit",
    "broken_references",
    "locate",
]
