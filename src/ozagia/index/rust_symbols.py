"""Rust structural symbols, modules, `use` imports and tests.

This is the Phase 1 Rust source model. When `tree-sitter-rust` is installed it
walks the CST and returns Python-parity symbol fields (qualified names, spans,
doc comments, attributes, arguments, return type, visibility, test markers),
module declarations and `use` bindings. Without the grammar it falls back to a
small line parser so the graph still has something honest to report.

The module never imports the target project and never edits source. Tree-sitter
is imported lazily; importing this module is safe when the optional grammar is
absent.
"""
from __future__ import annotations

import re
from pathlib import Path, PurePosixPath
from typing import Any

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
_CONTAINER_KINDS = {"impl_item", "trait_item", "mod_item"}
_TEST_ATTR = re.compile(
    r"#\[\s*(?:tokio|async_std|actix_rt|futures|rstest|test_case|quickcheck|"
    r"proptest)::test\b|#\[\s*test\b|#\[\s*rstest\b|#\[\s*quickcheck\b|"
    r"#\[\s*bench\b")
_PATH_ATTR = re.compile(r'#\[\s*path\s*=\s*"([^"]+)"')
_RUST_SYM_RE = re.compile(
    r"^\s*(?:pub(?:\([^)]*\))?\s+)?"
    r"(?:async\s+|unsafe\s+|const\s+|extern\s+\"[^\"]+\"\s+)*"
    r"(fn|struct|enum|trait|impl|mod|type|const|static)\s+"
    r"([A-Za-z_][A-Za-z0-9_]*)")
_RUST_MOD_RE = re.compile(
    r"^\s*(?:pub(?:\([^)]*\))?\s+)?mod\s+([A-Za-z_][A-Za-z0-9_]*)\s*;")
_RUST_USE_RE = re.compile(
    r"^\s*(?:pub(?:\([^)]*\))?\s+)?use\s+(.+);")
_CRATE_ROOT_FILES = frozenset({"lib.rs", "main.rs", "build.rs"})
_TEST_ROOT_DIRS = frozenset({"tests", "examples", "benches"})


def _text(node: Any) -> str:
    try:
        return node.text.decode("utf-8", errors="replace")
    except Exception:
        return ""


def _line(node: Any, *, end: bool = False) -> int:
    point = node.end_point if end else node.start_point
    return int(point[0]) + 1


def _field(node: Any, name: str) -> Any:
    try:
        return node.child_by_field_name(name)
    except Exception:
        return None


def _visibility(node: Any) -> str:
    for child in node.children:
        if child.type == "visibility_modifier":
            return _text(child).strip()
    return ""


def _doc_and_attrs(node: Any) -> tuple[str, list[str]]:
    """Outer doc comments and attributes immediately before an item."""
    attrs: list[str] = []
    docs: list[str] = []
    current = node.prev_named_sibling
    while current is not None and current.type in (
            "attribute_item", "line_comment", "block_comment"):
        text = _text(current).strip()
        if current.type == "attribute_item":
            attrs.append(text)
        elif text.startswith("///"):
            docs.append(text[3:].strip())
        elif text.startswith("/**"):
            body = text[3:]
            if body.endswith("*/"):
                body = body[:-2]
            docs.extend(line.strip().lstrip("*").strip()
                        for line in body.splitlines())
        # `//!` and `/*!` are inner/module docs, not item docs.
        current = current.prev_named_sibling
    attrs.reverse()
    docs.reverse()
    return "\n".join(line for line in docs if line), attrs


def _params(node: Any) -> list[str]:
    params = _field(node, "parameters")
    if params is None:
        return []
    names: list[str] = []
    for child in params.named_children:
        text = _text(child).strip()
        if not text:
            continue
        if child.type == "self_parameter":
            names.append("self")
            continue
        match = re.match(r"(?:mut\s+|ref\s+)*([A-Za-z_][A-Za-z0-9_]*)", text)
        names.append(match.group(1) if match else text[:80])
    return names


def _function_symbol(node: Any, scope: tuple[str, ...], owner: str,
                     doc: str, attrs: list[str]) -> dict[str, Any]:
    name_node = _field(node, "name")
    name = _text(name_node) or "anonymous"
    qualname = ".".join((*scope, name))
    return {
        "name": name,
        "qualname": qualname,
        "kind": "method" if owner in ("impl", "trait") else "function",
        "start_line": _line(node),
        "end_line": _line(node, end=True),
        "doc": doc,
        "decorators": attrs,
        "args": _params(node),
        "returns": _text(_field(node, "return_type")).strip()
                   if _field(node, "return_type") is not None else "",
        "visibility": _visibility(node),
        "is_test": any(_TEST_ATTR.search(item) for item in attrs),
    }


def _simple_symbol(node: Any, name: str, kind: str, scope: tuple[str, ...],
                   doc: str, attrs: list[str]) -> dict[str, Any]:
    return {
        "name": name,
        "qualname": ".".join((*scope, name)),
        "kind": kind,
        "start_line": _line(node),
        "end_line": _line(node, end=True),
        "doc": doc,
        "decorators": attrs,
        "args": [],
        "returns": "",
        "visibility": _visibility(node),
        "is_test": False,
    }


def _impl_name(node: Any) -> str:
    type_node = _field(node, "type")
    text = _text(type_node).strip() if type_node is not None else ""
    # `Foo<T>` and `&Foo` are scoped by the implementing type.
    text = re.split(r"[<{]", text, maxsplit=1)[0].strip().lstrip("&").strip()
    return text or "impl"


def _module_entries(attrs: list[str], name: str, line: int, inline: bool,
                    scope: tuple[str, ...] = ()) -> dict[str, Any]:
    path_attribute = None
    for attr in attrs:
        match = _PATH_ATTR.search(attr)
        if match:
            path_attribute = match.group(1)
    return {"name": name, "line": line, "inline": inline,
            "path_attribute": path_attribute, "parent": list(scope)}


# --------------------------------------------------------------------------- #
# use trees
# --------------------------------------------------------------------------- #

def _split_top(value: str, separator: str = ",") -> list[str]:
    out: list[str] = []
    depth = 0
    start = 0
    index = 0
    while index < len(value):
        char = value[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth = max(0, depth - 1)
        if depth == 0 and value.startswith(separator, index):
            out.append(value[start:index])
            index += len(separator)
            start = index
            continue
        index += 1
    out.append(value[start:])
    return [part.strip() for part in out if part.strip()]


def _join_path(prefix: str, value: str) -> str:
    prefix = prefix.strip().rstrip(":")
    value = value.strip().lstrip(":")
    if not prefix:
        return value
    if not value:
        return prefix
    return f"{prefix}::{value}"


def _use_bindings(expression: str, scope: str, line: int, *,
                  public: bool, path: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []

    def emit(module: str, imported: str, name: str, alias: str | None,
             glob: bool) -> None:
        out.append({
            "name": name,
            "kind": "import",
            "detail": "import.use",
            "line": line,
            "module": module,
            "imported": imported,
            "alias": alias,
            "glob": glob,
            "pub": public,
            "scope": scope,
            "path": path,
        })

    def expand(value: str, prefix: str = "") -> None:
        value = value.strip()
        if not value:
            return
        if value.startswith("{") and value.endswith("}"):
            for part in _split_top(value[1:-1]):
                expand(part, prefix)
            return
        alias: str | None = None
        parts = _split_top(value, " as ")
        if len(parts) == 2:
            value, alias = parts[0], parts[1]
        value = value.strip()
        if value.endswith("::*") or value == "*":
            base = value[:-3] if value.endswith("::*") else ""
            module = _join_path(prefix, base)
            emit(module, "*", "*", None, True)
            return
        if "{" in value:
            head, _, rest = value.partition("{")
            inner, _, tail = rest.rpartition("}")
            child_prefix = _join_path(prefix, head.rstrip(":"))
            for part in _split_top(inner):
                expand(part, child_prefix)
            if tail.strip(":"):
                expand(tail.strip(":"), child_prefix)
            return
        full = _join_path(prefix, value)
        path_parts = [part for part in full.split("::") if part]
        if not path_parts:
            return
        if path_parts[-1] == "self":
            module = "::".join(path_parts[:-1])
            imported = ""
            name = alias or (path_parts[-2] if len(path_parts) > 1 else "self")
        else:
            module = "::".join(path_parts[:-1])
            imported = path_parts[-1]
            name = alias or imported
        emit(module, imported, name, alias, False)

    expand(expression)
    return out


# --------------------------------------------------------------------------- #
# tree-sitter extraction
# --------------------------------------------------------------------------- #

def _collect_use(node: Any, scope: str,
                 imports: list[dict[str, Any]]) -> None:
    text = _text(node).strip()
    public = text.startswith("pub")
    body = re.sub(r"^\s*pub(?:\([^)]*\))?\s+", "", text)
    body = re.sub(r"^\s*use\s+", "", body).rstrip(";").strip()
    imports.extend(_use_bindings(body, scope, _line(node), public=public,
                                 path=text))


def _walk_block_uses(node: Any, scope: str,
                     imports: list[dict[str, Any]]) -> None:
    """Find function-local `use` declarations without indexing nested items."""
    for child in node.named_children:
        if child.type == "use_declaration":
            _collect_use(child, scope, imports)
            continue
        if child.type in ("function_item", "function_signature_item",
                          "impl_item", "trait_item", "mod_item"):
            continue
        _walk_block_uses(child, scope, imports)


def _walk_items(node: Any, scope: tuple[str, ...], owner: str,
                in_test_module: bool,
                symbols: list[dict[str, Any]],
                imports: list[dict[str, Any]],
                modules: list[dict[str, Any]]) -> None:
    for child in node.named_children:
        kind = child.type
        if kind == "use_declaration":
            _collect_use(child, ".".join(scope), imports)
            continue
        if kind in ("line_comment", "block_comment", "attribute_item",
                    "inner_attribute_item", "macro_rule", "enum_variant",
                    "field_declaration"):
            continue
        attrs: list[str] = []
        doc = ""
        if kind in _ITEM_KINDS or kind in _CONTAINER_KINDS or kind in (
                "function_item", "function_signature_item"):
            doc, attrs = _doc_and_attrs(child)
        if kind == "impl_item":
            name = _impl_name(child)
            impl_attrs = attrs
            symbols.append({
                "name": name,
                "qualname": "impl " + name,
                "kind": "impl",
                "start_line": _line(child),
                "end_line": _line(child, end=True),
                "doc": doc,
                "decorators": impl_attrs,
                "args": [],
                "returns": "",
                "visibility": "",
                "is_test": False,
            })
            body = _field(child, "body")
            if body is not None:
                _walk_items(body, (*scope, name), "impl", in_test_module,
                            symbols, imports, modules)
            continue
        if kind == "trait_item":
            name = _text(_field(child, "name")) or "trait"
            symbols.append(_simple_symbol(child, name, "trait", scope, doc, attrs))
            body = _field(child, "body")
            if body is not None:
                _walk_items(body, (*scope, name), "trait", in_test_module,
                            symbols, imports, modules)
            continue
        if kind == "mod_item":
            name = _text(_field(child, "name")) or "mod"
            symbols.append(_simple_symbol(child, name, "mod", scope, doc, attrs))
            body = _field(child, "body")
            if body is not None:
                is_test = in_test_module or any("cfg(test)" in item
                                                for item in attrs)
                modules.append(_module_entries(attrs, name, _line(child), True,
                                               scope))
                _walk_items(body, (*scope, name), "mod", is_test,
                            symbols, imports, modules)
            else:
                modules.append(_module_entries(attrs, name, _line(child), False,
                                               scope))
            continue
        if kind in ("function_item", "function_signature_item"):
            symbol = _function_symbol(child, scope, owner, doc, attrs)
            symbols.append(symbol)
            body = _field(child, "body")
            if body is not None:
                _walk_block_uses(body, symbol["qualname"], imports)
            continue
        if kind in _ITEM_KINDS:
            name_node = _field(child, "name")
            name = _text(name_node) or kind.removesuffix("_item")
            symbols.append(_simple_symbol(child, name, _ITEM_KINDS[kind],
                                          scope, doc, attrs))
            continue
        # Unknown item containers: look inside for declarations (for example a
        # `foreign_mod_item`), without descending into expression bodies.
        if kind in ("declaration_list", "source_file"):
            _walk_items(child, scope, owner, in_test_module,
                        symbols, imports, modules)


def _tree_parse(source: str) -> tuple[dict[str, Any], str | None]:
    from tree_sitter import Language, Parser
    import tree_sitter_rust

    parser = Parser(Language(tree_sitter_rust.language()))
    tree = parser.parse(source.encode("utf-8"))
    symbols: list[dict[str, Any]] = []
    imports: list[dict[str, Any]] = []
    modules: list[dict[str, Any]] = []
    _walk_items(tree.root_node, (), "", False, symbols, imports, modules)
    errors = 0
    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        if getattr(node, "is_error", False) or getattr(node, "is_missing", False):
            errors += 1
        stack.extend(node.children)
    return ({
        "symbols": symbols,
        "imports": imports,
        "tests": [symbol for symbol in symbols if symbol["is_test"]],
        "modules": modules,
        "backend": "tree-sitter",
        "has_errors": errors > 0,
        "error": None,
    }, None)


# --------------------------------------------------------------------------- #
# regex fallback
# --------------------------------------------------------------------------- #

def _regex_parse(source: str) -> dict[str, Any]:
    symbols: list[dict[str, Any]] = []
    modules: list[dict[str, Any]] = []
    imports: list[dict[str, Any]] = []
    attrs: list[str] = []
    for number, line in enumerate(source.splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith("#["):
            attrs.append(stripped)
            continue
        match = _RUST_SYM_RE.match(line)
        if match:
            kind, name = match.group(1), match.group(2)
            if kind == "impl":
                name = line.strip()[4:].strip()[:120]
            is_test = any(_TEST_ATTR.search(item) for item in attrs)
            symbols.append({
                "name": name.split()[0] if name else kind,
                "qualname": name or kind,
                "kind": kind,
                "start_line": number,
                "end_line": number,
                "doc": "",
                "decorators": list(attrs),
                "args": [],
                "returns": "",
                "visibility": "pub" if stripped.startswith("pub") else "",
                "is_test": is_test and kind == "fn",
            })
            attrs = []
        module = _RUST_MOD_RE.match(line)
        if module:
            modules.append(_module_entries(attrs, module.group(1), number, False))
        use = _RUST_USE_RE.match(line)
        if use:
            text = stripped
            public = text.startswith("pub")
            imports.extend(_use_bindings(use.group(1), "", number,
                                         public=public, path=text))
        if stripped and not stripped.startswith(("//", "/*", "*", "#[", "#!")):
            attrs = []
    return {
        "symbols": symbols,
        "imports": imports,
        "tests": [symbol for symbol in symbols if symbol["is_test"]],
        "modules": modules,
        "backend": "regex",
        "has_errors": False,
        "error": None,
    }


def _crate_root(path: str | Path) -> bool:
    rel = PurePosixPath(str(path).replace("\\", "/"))
    if rel.name in _CRATE_ROOT_FILES:
        return True
    parts = rel.parts
    if len(parts) >= 3 and parts[-3] == "src" and parts[-2] == "bin":
        return True
    for i, part in enumerate(parts[:-1]):
        if part in _TEST_ROOT_DIRS and (i == 0 or parts[i - 1] != "src"):
            return True
    return False


def parse_rust(path: str | Path, source: str | None = None, *,
               use_tree_sitter: bool = True) -> dict[str, Any]:
    """Parse one Rust file into symbols, imports, tests and modules.

    ``use_tree_sitter=False`` exercises the regex fallback; it is also used
    automatically when the optional grammar is missing. The result always has
    the same keys and is JSON-safe.
    """
    path = Path(path)
    if source is None:
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return {"symbols": [], "imports": [], "tests": [], "modules": [],
                    "source": "", "backend": "unavailable",
                    "has_errors": False,
                    "error": "file could not be read",
                    "crate_root": _crate_root(path)}
    if use_tree_sitter:
        try:
            result, error = _tree_parse(source)
            if error is None:
                result["source"] = source
                result["crate_root"] = _crate_root(path)
                return result
        except Exception as exc:  # missing grammar, bad grammar, parse crash
            fallback = _regex_parse(source)
            fallback["source"] = source
            fallback["crate_root"] = _crate_root(path)
            fallback["error"] = f"{type(exc).__name__}: {exc}"
            return fallback
    result = _regex_parse(source)
    result["source"] = source
    result["crate_root"] = _crate_root(path)
    return result


__all__ = ["_use_bindings", "parse_rust"]
