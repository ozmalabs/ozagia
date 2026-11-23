"""Symbol-addressed structural edits for Rust source.

This is the Rust counterpart to :mod:`ozagia.edit.code_edit`: a script names
symbols or symbol anchors, not raw text patches. Edits are preview-only by
default, check `base_sha256`, preserve line endings/BOM, require the edited
source to reparse with tree-sitter-rust, write atomically and back up under
`.ozagia/edits/backups/`.

Ops::

    replace_symbol          symbol, text
    replace_body            symbol, text
    delete_symbol           symbol
    insert_before_symbol    symbol, text
    insert_after_symbol     symbol, text
    insert_import           text | module + imported/name + alias
    replace_span            start_line/start_col/end_line/end_col or start/end
    rename_symbol           symbol, new_name (same file, conservative)

`rename_symbol` intentionally refuses methods, impl blocks, macros, fields and
names with local bindings/import aliases. Those need type-aware analysis or the
LSP rename preview; guessing would be worse than refusing.
"""
from __future__ import annotations

import bisect
import difflib
import json
import re
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ozagia.edit.code_edit import (
    FORBIDDEN_SCRIPT_KEYS,
    MAX_SOURCE_BYTES,
    CodeEditError,
)
from ozagia.edit.txn import (
    check_base,
    encode_text,
    line_starts,
    read_source,
    sha256_bytes,
    write_source,
)

_NEWLINE_NAMES = {"\n": "lf", "\r\n": "crlf", "\r": "cr"}
_ITEM_KINDS = {
    "struct_item": "struct",
    "enum_item": "enum",
    "union_item": "union",
    "type_item": "type_alias",
    "trait_item": "trait",
    "mod_item": "mod",
    "macro_definition": "macro",
    "const_item": "const",
    "static_item": "static",
}
_RENAME_KINDS = frozenset({"function", "struct", "enum", "union", "trait",
                           "type_alias", "const", "static", "mod"})
_CST_OPS = frozenset({"replace_node", "insert_before_node", "insert_after_node",
                      "delete_node", "wrap_node"})
SYMBOL_OPS = frozenset({
    "replace_symbol", "replace_body", "delete_symbol",
    "insert_before_symbol", "insert_after_symbol", "insert_import",
    "replace_span", "rename_symbol",
})


class RustEditError(CodeEditError):
    """A refused Rust edit: bad symbol, stale base, parse failure, raw diff."""


@dataclass
class RustSymbol:
    name: str
    qualname: str
    kind: str
    start: int                 # character offset, includes leading docs/attrs
    end: int
    start_line: int
    end_line: int
    indent: str
    body_start: int | None = None
    body_end: int | None = None
    body_indent: str = "    "
    inline_body: bool = False
    name_start: int = 0
    name_end: int = 0
    node: Any = None
    body_node: Any = None


@dataclass
class SymbolTable:
    source: str
    tree: Any
    symbols: dict[str, RustSymbol]
    ambiguous: set[str]
    char_index: "_CharIndex"


class _CharIndex:
    """Map UTF-8 byte offsets from tree-sitter to Python character offsets."""

    def __init__(self, source: str) -> None:
        self.offsets = [0]
        total = 0
        for char in source:
            total += len(char.encode("utf-8"))
            self.offsets.append(total)
        self.length = len(source)

    def char(self, byte_offset: int) -> int:
        return min(self.length, bisect.bisect_left(self.offsets, byte_offset))


def _text(node: Any, index: _CharIndex, source: str) -> str:
    try:
        return source[index.char(int(node.start_byte)):
                      index.char(int(node.end_byte))]
    except Exception:
        return ""


def _parser() -> Any:
    try:
        from tree_sitter import Language, Parser
        import tree_sitter_rust
    except Exception as exc:  # optional grammar
        raise RustEditError(
            "tree-sitter-rust is required for Rust symbol edits; install the "
            "treesitter or lang-rust extra") from exc
    return Parser(Language(tree_sitter_rust.language()))


def _parse(source: str) -> Any:
    parser = _parser()
    tree = parser.parse(source.encode("utf-8"))
    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        if getattr(node, "is_error", False) or getattr(node, "is_missing", False):
            raise RustEditError(f"Rust source does not parse at line "
                                f"{int(node.start_point[0]) + 1}")
        stack.extend(node.children)
    return tree


def _line_indent(source: str, char_offset: int) -> str:
    start = source.rfind("\n", 0, char_offset) + 1
    line = source[start:char_offset]
    return line[:len(line) - len(line.lstrip(" \t"))]


def _leading_start(node: Any, index: _CharIndex, source: str) -> int:
    start = int(node.start_byte)
    current = node.prev_named_sibling
    while current is not None and current.type in (
            "attribute_item", "line_comment", "block_comment"):
        text = _text(current, index, source).lstrip()
        if current.type == "line_comment" and not text.startswith("///"):
            break
        if current.type == "block_comment" and not text.startswith("/**"):
            break
        start = int(current.start_byte)
        current = current.prev_named_sibling
    return index.char(start)


def _body_span(node: Any, index: _CharIndex, source: str, item_indent: str
               ) -> tuple[int | None, int | None, str, bool]:
    body = node.child_by_field_name("body")
    if body is None:
        return None, None, item_indent + "    ", False
    body_text = _text(body, index, source)
    if not (body_text.startswith("{") and body_text.endswith("}")):
        return None, None, item_indent + "    ", False
    start = index.char(int(body.start_byte)) + 1
    end = index.char(int(body.end_byte)) - 1
    interior = source[start:end]
    inline = "\n" not in interior
    children = list(getattr(body, "named_children", []))
    if children:
        first = children[0]
        first_char = index.char(int(first.start_byte))
        indent = _line_indent(source, first_char)
        if inline:
            indent = item_indent + "    "
    else:
        indent = item_indent + "    "
    return start, end, indent, inline


def _add_symbol(node: Any, scope: tuple[str, ...], owner: str,
                table: dict[str, RustSymbol], ambiguous: set[str],
                index: _CharIndex, source: str) -> RustSymbol | None:
    kind = node.type
    if kind == "impl_item":
        name_node = node.child_by_field_name("type")
        if name_node is None:
            return None
        type_name = _text(name_node, index, source)
        name = re.split(r"[<{]", type_name, maxsplit=1)[0].strip().lstrip("&")
        name = name or "impl"
        qualname = "impl " + name
        symbol_kind = "impl"
    else:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return None
        name = _text(name_node, index, source)
        if not name:
            return None
        qualname = ""
        symbol_kind = ""
    if kind == "impl_item":
        pass
    elif kind in ("function_item", "function_signature_item"):
        qualname = ".".join((*scope, name))
        symbol_kind = "method" if owner in ("impl", "trait") else "function"
    elif kind in _ITEM_KINDS:
        qualname = ".".join((*scope, name))
        symbol_kind = _ITEM_KINDS[kind]
    else:
        return None
    start = _leading_start(node, index, source)
    end = index.char(int(node.end_byte))
    # Match the Python adapter: a definition span owns its trailing line end.
    if source.startswith("\r\n", end):
        end += 2
    elif source.startswith(("\n", "\r"), end):
        end += 1
    item_indent = _line_indent(source, index.char(int(node.start_byte)))
    body_start, body_end, body_indent, inline = _body_span(
        node, index, source, item_indent)
    symbol = RustSymbol(
        name=name, qualname=qualname, kind=symbol_kind,
        start=start, end=end,
        start_line=source.count("\n", 0, start) + 1,
        end_line=int(node.end_point[0]) + 1,
        indent=item_indent,
        body_start=body_start, body_end=body_end,
        body_indent=body_indent, inline_body=inline,
        name_start=index.char(int(name_node.start_byte)),
        name_end=index.char(int(name_node.end_byte)),
        node=node, body_node=node.child_by_field_name("body"),
    )
    if qualname in table:
        ambiguous.add(qualname)
    else:
        table[qualname] = symbol
    return symbol


def _walk(node: Any, scope: tuple[str, ...], owner: str,
          table: dict[str, RustSymbol], ambiguous: set[str],
          index: _CharIndex, source: str) -> None:
    for child in node.named_children:
        kind = child.type
        if kind == "impl_item":
            symbol = _add_symbol(child, scope, "impl", table, ambiguous,
                                 index, source)
            body = child.child_by_field_name("body")
            if body is not None and symbol is not None:
                name = symbol.qualname[len("impl "):]
                _walk(body, (*scope, name), "impl", table, ambiguous,
                      index, source)
            continue
        if kind in ("trait_item", "mod_item"):
            symbol = _add_symbol(child, scope, owner if kind == "trait_item"
                                 else "mod", table, ambiguous, index, source)
            body = child.child_by_field_name("body")
            if body is not None and symbol is not None:
                _walk(body, (*scope, symbol.name), symbol.kind, table,
                      ambiguous, index, source)
            continue
        if kind in ("function_item", "function_signature_item", *_ITEM_KINDS):
            _add_symbol(child, scope, owner, table, ambiguous, index, source)


def symbol_table(source: str) -> SymbolTable:
    """Rust definitions addressed by qualname, with docs/attrs and body spans."""
    tree = _parse(source)
    index = _CharIndex(source)
    symbols: dict[str, RustSymbol] = {}
    ambiguous: set[str] = set()
    _walk(tree.root_node, (), "", symbols, ambiguous, index, source)
    return SymbolTable(source, tree, symbols, ambiguous, index)


def _ensure_newline(text: str, newline: str = "\n") -> str:
    return text if not text or text.endswith(("\n", "\r")) else text + newline


def _symbol(table: SymbolTable, op: dict[str, Any]) -> RustSymbol:
    name = str(op.get("symbol") or op.get("qualname") or "").strip()
    if not name:
        raise RustEditError(f"{op.get('op')}: missing symbol")
    if name in table.ambiguous:
        raise RustEditError(
            f"{op.get('op')}: symbol {name!r} is ambiguous in this file")
    symbol = table.symbols.get(name)
    if symbol is None:
        available = ", ".join(sorted(table.symbols)[:20])
        raise RustEditError(
            f"{op.get('op')}: no symbol {name!r} in this file; "
            f"available: {available}")
    return symbol


def _replace_body(symbol: RustSymbol, text: str, newline: str) -> tuple[int, int, str]:
    if symbol.body_start is None or symbol.body_end is None:
        raise RustEditError(
            f"replace_body: {symbol.qualname!r} has no body block")
    body = textwrap.dedent(str(text or "")).strip("\n")
    if not body.strip():
        return symbol.body_start, symbol.body_end, ""
    indent = symbol.body_indent or (symbol.indent + "    ")
    rendered = "\n".join(
        (indent + line) if line.strip() else ""
        for line in body.splitlines())
    rendered = newline + rendered + newline + symbol.indent
    return symbol.body_start, symbol.body_end, rendered


def _use_text(op: dict[str, Any]) -> str:
    text = str(op.get("text") or "").strip()
    if text:
        if not text.startswith(("use ", "pub use ")):
            text = "use " + text
        if not text.endswith(";"):
            text += ";"
        return text
    module = str(op.get("module") or "").strip().rstrip(":")
    imported = str(op.get("imported") or op.get("name") or "").strip()
    alias = str(op.get("alias") or "").strip()
    if not imported:
        raise RustEditError("insert_import needs text or a name/imported value")
    path = f"{module}::{imported}" if module else imported
    if alias:
        path += f" as {alias}"
    return f"use {path};"


def _top_level_uses(table: SymbolTable) -> list[Any]:
    return [child for child in table.tree.root_node.named_children
            if child.type == "use_declaration"]


def _first_top_level_item(table: SymbolTable) -> Any | None:
    for child in table.tree.root_node.named_children:
        if child.type in ("line_comment", "block_comment",
                          "attribute_item", "inner_attribute_item"):
            text = _text(child, table.char_index, table.source).lstrip()
            if child.type == "line_comment" and not text.startswith("//!"):
                return child
            if child.type == "attribute_item" and not text.startswith("#!["):
                return child
            continue
        return child
    return None


_GROUP_USE_RE = re.compile(r"^use\s+([A-Za-z0-9_:]+)::\{([^{}]*)\};$")


def _merge_use_group(source: str, table: SymbolTable, op: dict[str, Any]
                     ) -> tuple[int, int, str, str] | None:
    """Insert a simple name into a matching `use prefix::{...};` group."""
    module = str(op.get("module") or "").strip().rstrip(":")
    imported = str(op.get("imported") or op.get("name") or "").strip()
    if (not module or not imported or op.get("alias") or op.get("text")
            or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", imported)):
        return None
    for node in _top_level_uses(table):
        text = re.sub(r"\s+", " ", _text(node, table.char_index, source)).strip()
        match = _GROUP_USE_RE.match(text)
        if not match or match.group(1) != module:
            continue
        names = [part.strip() for part in match.group(2).split(",")
                 if part.strip()]
        if any(" as " in name for name in names):
            continue
        if imported in names:
            return None
        names.append(imported)
        names.sort(key=lambda item: (item != "self", item))
        start = table.char_index.char(int(node.start_byte))
        end = table.char_index.char(int(node.end_byte))
        replacement = f"use {module}::{{{', '.join(names)}}};"
        return start, end, replacement, f"merged {module}::{imported}"
    return None


def _insert_import(source: str, table: SymbolTable, op: dict[str, Any],
                   newline: str) -> tuple[int, str, str]:
    use = _use_text(op)
    normal = re.sub(r"\s+", " ", use).strip()
    existing = {re.sub(r"\s+", " ", _text(node, table.char_index, source)).strip()
                for node in _top_level_uses(table)}
    if normal in existing:
        return 0, "", "already present; skipped"
    uses = _top_level_uses(table)
    if uses:
        at = table.char_index.char(int(max(node.end_byte for node in uses)))
        prefix = "" if at > 0 and source[at - 1:at] == "\n" else newline
        text = prefix + use
        if at >= len(source) or source[at:at + 1] not in ("\n", "\r"):
            text += newline
        return at, text, use
    first = _first_top_level_item(table)
    at = table.char_index.char(int(first.start_byte)) if first is not None else len(source)
    if at > 0 and not source[:at].endswith(("\n", "\r")):
        text = newline + use + newline
    else:
        text = use + newline
    if first is not None:
        text += newline
    return at, text, use


_BINDING_NODES = frozenset({
    "parameter", "let_declaration", "for_expression", "closure_parameters",
    "match_pattern", "struct_pattern", "tuple_pattern", "slice_pattern",
    "or_pattern",
})
_TYPE_NODES = frozenset({
    "type_identifier", "primitive_type", "generic_type", "type_arguments",
    "type_parameters", "scoped_type_identifier", "reference_type",
    "pointer_type", "tuple_type", "array_type", "unit_type",
})


def _binds_name(node: Any, name: str, table: SymbolTable) -> bool:
    if node.type not in _BINDING_NODES:
        return False
    seed = node.child_by_field_name("pattern") or node
    stack = [seed]
    while stack:
        current = stack.pop()
        if current.type in _TYPE_NODES:
            continue
        if current.type == "identifier":
            if _text(current, table.char_index, table.source) == name:
                return True
            continue
        stack.extend(current.children)
    return False


def _rename_spans(table: SymbolTable, symbol: RustSymbol,
                  new_name: str) -> list[tuple[int, int]]:
    if symbol.kind not in _RENAME_KINDS:
        raise RustEditError(
            f"rename_symbol: {symbol.qualname!r} is a {symbol.kind}; only "
            "free items/type names are renamed without type analysis")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", new_name):
        raise RustEditError("rename_symbol: new_name must be an identifier")
    old = symbol.name
    source = table.source
    chars = table.char_index

    # Imports can rebind or re-export the name; update those explicitly or
    # refuse rather than leave a half-renamed graph.
    for node in _top_level_uses(table):
        text = _text(node, chars, source)
        if re.search(rf"\b{re.escape(old)}\b", text):
            raise RustEditError(
                f"rename_symbol: {old!r} appears in a use declaration; "
                "update the import first or use rename preview")

    spans: list[tuple[int, int]] = []
    stack = [table.tree.root_node]
    while stack:
        node = stack.pop()
        if _binds_name(node, old, table):
            raise RustEditError(
                f"rename_symbol: {old!r} is also bound as a local; refusing "
                "an ambiguous same-file rename")
        text = _text(node, chars, source) if node.type in (
            "identifier", "type_identifier") else ""
        if node.type in ("identifier", "type_identifier") and text == old:
            parent = node.parent
            skip = False
            if parent is not None:
                if parent.type in ("field_identifier", "field_declaration",
                                   "field_expression", "shorthand_field_initializer"):
                    skip = True
                if parent.type == "macro_invocation" and (
                        parent.child_by_field_name("macro") == node):
                    skip = True
                if parent.type == "use_declaration":
                    skip = True
                if parent.type == "attribute_item":
                    skip = True
            if not skip:
                spans.append((chars.char(int(node.start_byte)),
                              chars.char(int(node.end_byte))))
        stack.extend(node.children)
    if not spans:
        raise RustEditError(f"rename_symbol: no occurrences of {old!r}")
    return sorted(set(spans))


def _apply_ops(source: str, ops: list[dict[str, Any]], *,
               newline: str) -> tuple[str, list[dict[str, Any]]]:
    table = symbol_table(source)
    edits: list[tuple[int, int, str, dict[str, Any]]] = []
    changes: list[dict[str, Any]] = []

    for op in ops:
        if not isinstance(op, dict):
            raise RustEditError("each op must be an object")
        kind = str(op.get("op") or "").strip()
        if kind == "replace_symbol":
            symbol = _symbol(table, op)
            text = _ensure_newline(str(op.get("text") or ""), newline)
            if text and not text.strip():
                text = ""
            edits.append((symbol.start, symbol.end, text, op))
            changes.append({"op": kind, "symbol": symbol.qualname,
                            "lines": [symbol.start_line, symbol.end_line],
                            "summary": "replace definition"})
        elif kind == "replace_body":
            symbol = _symbol(table, op)
            start, end, text = _replace_body(symbol, str(op.get("text") or ""),
                                             newline)
            edits.append((start, end, text, op))
            changes.append({"op": kind, "symbol": symbol.qualname,
                            "lines": [symbol.start_line, symbol.end_line],
                            "summary": "replace item body"})
        elif kind == "delete_symbol":
            symbol = _symbol(table, op)
            end = symbol.end
            if source.startswith(newline, end):
                end += len(newline)
            edits.append((symbol.start, end, "", op))
            changes.append({"op": kind, "symbol": symbol.qualname,
                            "lines": [symbol.start_line, symbol.end_line],
                            "summary": "delete definition"})
        elif kind in ("insert_before_symbol", "insert_after_symbol"):
            symbol = _symbol(table, op)
            text = _ensure_newline(str(op.get("text") or ""), newline)
            at = symbol.start if kind == "insert_before_symbol" else symbol.end
            if kind == "insert_after_symbol" and at > 0 and source[at - 1] != "\n":
                text = newline + text
            edits.append((at, at, text, op))
            changes.append({"op": kind, "symbol": symbol.qualname,
                            "summary": "insert text at symbol anchor"})
        elif kind == "insert_import":
            merged = _merge_use_group(source, table, op)
            if merged is not None:
                start, end, text, summary = merged
                edits.append((start, end, text, op))
                changes.append({"op": kind, "summary": summary})
            else:
                at, text, summary = _insert_import(source, table, op, newline)
                if text:
                    edits.append((at, at, text, op))
                changes.append({"op": kind, "summary": summary})
        elif kind == "replace_span":
            try:
                if "start" in op:
                    start = int(op["start"])
                    end = int(op["end"])
                else:
                    starts = line_starts(source)
                    start = starts[int(op["start_line"]) - 1] + int(op.get("start_col", 0))
                    end = starts[int(op["end_line"]) - 1] + int(op.get("end_col", 0))
            except (KeyError, IndexError, TypeError, ValueError) as exc:
                raise RustEditError(
                    "replace_span needs start/end or start_line/start_col/"
                    "end_line/end_col") from exc
            if not (0 <= start <= end <= len(source)):
                raise RustEditError("replace_span is outside the file")
            edits.append((start, end, str(op.get("text") or ""), op))
            changes.append({"op": kind, "summary": f"replace {start}:{end}"})
        elif kind == "rename_symbol":
            symbol = _symbol(table, op)
            new_name = str(op.get("new_name") or "").strip()
            if not new_name:
                raise RustEditError("rename_symbol needs new_name")
            spans = _rename_spans(table, symbol, new_name)
            for start, end in spans:
                edits.append((start, end, new_name, op))
            changes.append({"op": kind, "symbol": symbol.qualname,
                            "new_name": new_name,
                            "summary": f"renamed {len(spans)} occurrence(s)"})
        else:
            raise RustEditError(
                f"unsupported Rust symbol op {kind!r}; CST ops must use "
                "edit/cst.py")

    edits.sort(key=lambda item: (item[0], item[1]))
    for previous, current in zip(edits, edits[1:]):
        if previous[1] > current[0]:
            raise RustEditError("overlapping edits are refused")
    new_source = source
    for start, end, text, _op in reversed(edits):
        new_source = new_source[:start] + text + new_source[end:]
    return new_source, changes


def apply_rust_edit_script(root: str | Path, script: dict[str, Any], *,
                           write: bool = False) -> dict[str, Any]:
    """Validate and optionally apply a Rust symbol edit script."""
    if not isinstance(script, dict):
        raise RustEditError("edit script must be an object")
    for key in FORBIDDEN_SCRIPT_KEYS:
        if key in script:
            raise RustEditError(
                f"raw {key!r} input is refused: send semantic ops over graph "
                "targets; a diff is only an output view")
    root_path = Path(root).resolve()
    file = str(script.get("file") or "").strip()
    if not file:
        raise RustEditError("edit script needs file")
    path = (root_path / file).resolve()
    try:
        path.relative_to(root_path)
    except ValueError as exc:
        raise RustEditError("file escapes the repo root") from exc
    if path.suffix.lower() != ".rs":
        raise RustEditError(
            f"rust edit adapter only edits .rs; got {path.suffix or '<no suffix>'!r}")
    src = read_source(root_path, file, suffixes=(".rs",),
                      max_bytes=MAX_SOURCE_BYTES, error_cls=RustEditError)
    source = src.text
    base_sha = src.sha256
    check_base(src, script.get("base_sha256"), RustEditError)
    ops = script.get("ops")
    if not isinstance(ops, list) or not ops:
        raise RustEditError("edit script needs a non-empty ops list")

    new_source, changes = _apply_ops(source, ops, newline=src.newline)
    try:
        _parse(new_source)
        reparse = {"checked": True, "ok": True}
    except RustEditError as exc:
        raise RustEditError(f"edit would not parse as Rust: {exc}") from exc
    new_sha = sha256_bytes(encode_text(src, new_source))
    diff_lines = list(difflib.unified_diff(
        source.splitlines(keepends=True),
        new_source.splitlines(keepends=True),
        fromfile=f"a/{file}", tofile=f"b/{file}",
    ))
    applied = False
    backup = None
    if write:
        backup = write_source(root_path, src, new_source,
                              error_cls=RustEditError)
        applied = True
    return {
        "ok": True,
        "file": file,
        "applied": applied,
        "format": "rust",
        "base_sha256": base_sha,
        "new_sha256": new_sha,
        "before": {"lines": len(source.splitlines()), "sha256": base_sha},
        "after": {"lines": len(new_source.splitlines()), "sha256": new_sha},
        "ops": ops,
        "changes": changes,
        "diff_view": "".join(diff_lines[:400]),
        "diff_note": ("a unified diff is a rendered view for humans; the "
                      "canonical record is the op list above"),
        "backup": str(backup) if backup else None,
        "newline": _NEWLINE_NAMES[src.newline],
        "bom": src.bom,
        "lossless": {"span_edits": True, "reparse": reparse},
        "graph_stale": bool(applied),
    }


def _utf16_to_char(text: str, units: int) -> int:
    count = 0
    for index, char in enumerate(text):
        if count >= units:
            return index
        count += 2 if ord(char) > 0xFFFF else 1
    return len(text)


def apply_lsp_rename_preview(root: str | Path, preview: dict[str, Any], *,
                             write: bool = False) -> dict[str, Any]:
    """Apply a same-file rust-analyzer rename preview through the transaction.

    Cross-file previews and resource operations are review-only: applying them
    here would need a multi-file transaction and conflict handling.
    """
    if not isinstance(preview, dict) or not preview.get("ok", True):
        raise RustEditError("rename preview is not a successful result")
    files = preview.get("files")
    if not isinstance(files, list) or len(files) != 1:
        raise RustEditError(
            "rename preview touches multiple files; cross-file application is "
            "preview-only")
    if preview.get("resource_operations"):
        raise RustEditError("rename preview contains resource operations")
    entry = files[0]
    file = str(entry.get("path") or "").strip()
    if not file:
        raise RustEditError("rename preview has no repo-relative path")
    root_path = Path(root).resolve()
    path = (root_path / file).resolve()
    try:
        path.relative_to(root_path)
    except ValueError as exc:
        raise RustEditError("rename preview escapes the repo root") from exc
    if path.suffix.lower() != ".rs":
        raise RustEditError("rename preview is not for a Rust file")
    src = read_source(root_path, file, suffixes=(".rs",),
                      max_bytes=MAX_SOURCE_BYTES, error_cls=RustEditError)
    check_base(src, preview.get("base_sha256"), RustEditError)
    starts = line_starts(src.text)
    split = src.text.splitlines(keepends=True)
    spans: list[tuple[int, int, str]] = []
    for edit in entry.get("edits") or []:
        if not isinstance(edit, dict):
            continue
        try:
            line = int(edit["line"])
            start_line = starts[line]
            start = start_line + _utf16_to_char(split[line], int(edit["character"]))
            end_line = starts[int(edit["end_line"])]
            end = end_line + _utf16_to_char(split[int(edit["end_line"])],
                                             int(edit["end_character"]))
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise RustEditError("rename preview has an invalid edit range") from exc
        text = edit.get("newText", edit.get("new_text"))
        if not isinstance(text, str):
            raise RustEditError("rename preview edit has no newText")
        if not (0 <= start <= end <= len(src.text)):
            raise RustEditError("rename preview edit is outside the file")
        spans.append((start, end, text))
    if not spans:
        raise RustEditError("rename preview has no edits")
    spans.sort()
    for previous, current in zip(spans, spans[1:]):
        if previous[1] > current[0]:
            raise RustEditError("rename preview has overlapping edits")
    new_source = src.text
    for start, end, text in reversed(spans):
        new_source = new_source[:start] + text + new_source[end:]
    try:
        _parse(new_source)
    except RustEditError as exc:
        raise RustEditError(f"rename preview would not parse: {exc}") from exc
    backup = None
    applied = False
    if write:
        backup = write_source(root_path, src, new_source,
                              error_cls=RustEditError)
        applied = True
    new_sha = sha256_bytes(encode_text(src, new_source))
    return {
        "ok": True,
        "file": file,
        "applied": applied,
        "format": "rust",
        "source": "lsp_rename_preview",
        "base_sha256": src.sha256,
        "new_sha256": new_sha,
        "edits": len(spans),
        "backup": str(backup) if backup else None,
        "graph_stale": bool(applied),
    }


def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description="symbol-addressed Rust edits")
    parser.add_argument("--root", default=str(Path(__file__).resolve().parent.parent))
    parser.add_argument("--script", required=True,
                        help="JSON file: {file, base_sha256?, ops: [...]}")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args(argv)
    script = json.loads(Path(args.script).read_text(encoding="utf-8"))
    try:
        result = apply_rust_edit_script(args.root, script, write=args.apply)
    except RustEditError as exc:
        print(json.dumps({"ok": False, "error": str(exc)},
                         indent=2 if args.pretty else None))
        return 1
    print(json.dumps(result, indent=2 if args.pretty else None))
    return 0


__all__ = ["RustEditError", "RustSymbol", "SYMBOL_OPS",
           "SymbolTable", "apply_lsp_rename_preview",
           "apply_rust_edit_script", "symbol_table"]

if __name__ == "__main__":
    raise SystemExit(main())
