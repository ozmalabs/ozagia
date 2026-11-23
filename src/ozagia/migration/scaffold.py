"""Bounded scaffold preview for a simple dataclass or function.

The renderer is deliberately conservative. It accepts a public top-level
Python symbol with one of two shapes:

* a dataclass with no bases, no methods, at least one annotated field and only
  simple scalar field types;
* a synchronous or asynchronous function with no decorators, no ``*args`` or
  ``**kwargs``, every parameter annotated and a return annotation, using only
  simple scalar types.

Everything else is refused with a reason. The result is a suggested skeleton,
never claimed to be compilable: imports, lifetimes, derives, error handling and
runtime choices are left to the caller.

Preview is the default. ``write=True`` appends the skeleton to an existing
``.rs`` file through :mod:`ozagia.edit.txn`: base hash optional, atomic write,
backup under the state directory, tree-sitter parse gate when the grammar is
installed.
"""
from __future__ import annotations

import difflib
import re
from pathlib import Path
from typing import Any

from ozagia.config import MigrationConfig
from ozagia.edit.txn import (
    check_base,
    encode_text,
    normalize_newlines,
    read_source,
    sha256_bytes,
    write_source,
)
from ozagia.index.rust_symbols import parse_rust
from ozagia.migration.hints import hints_for
from ozagia.migration.inventory import PythonSymbol, inventory_python

MAX_TARGET_BYTES = 2 * 1024 * 1024
MAX_TARGET_CHARS = 256
MAX_SYMBOL_CHARS = 256
MAX_DIFF_LINES = 200
_SIMPLE_TYPES = {
    "int": "i64",
    "float": "f64",
    "bool": "bool",
    "str": "String",
    "bytes": "Vec<u8>",
    "None": "()",
    "NoneType": "()",
}
# Rust keywords and reserved words; a raw identifier would be possible but a
# scaffold that silently changes the name is worse than a refusal.
_RUST_KEYWORDS = frozenset({
    "as", "break", "const", "continue", "crate", "dyn", "else", "enum",
    "extern", "false", "fn", "for", "if", "impl", "in", "let", "loop",
    "match", "mod", "move", "mut", "pub", "ref", "return", "self", "Self",
    "static", "struct", "super", "trait", "true", "type", "unsafe", "use",
    "where", "while", "async", "await", "try", "abstract", "become", "box",
    "do", "final", "macro", "override", "priv", "typeof", "unsized",
    "virtual", "yield",
})


def _rust_ident(name: str, *, where: str) -> str:
    if (not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name)
            or name in _RUST_KEYWORDS):
        raise ScaffoldError(
            f"{where} {name!r} is not a Rust identifier; choose a different "
            "name or scaffold manually")
    return name


class ScaffoldError(ValueError):
    """The symbol or target is not a supported scaffold shape."""


def _decorator_names(values: tuple[str, ...]) -> list[str]:
    names: list[str] = []
    for value in values:
        text = str(value).strip().lstrip("@").split("(", 1)[0].strip()
        names.append(text.rsplit(".", 1)[-1])
    return names


def _simple_type(annotation: str, *, where: str) -> str:
    text = (annotation or "").strip().strip("'\"")
    if text.startswith("typing."):
        text = text[len("typing."):]
    if text in _SIMPLE_TYPES:
        return _SIMPLE_TYPES[text]
    raise ScaffoldError(
        f"{where} uses type {annotation!r}, which is not one of the simple "
        "scaffold types (int, float, bool, str, bytes, None); resolve the "
        "type decision manually")


def _find_symbol(inventory: Any, name: str) -> PythonSymbol:
    exact = [item for item in inventory.api if item.key == name]
    if exact:
        return exact[0]
    suffix = [item for item in inventory.api
              if item.key.endswith("." + name)]
    if len(suffix) == 1:
        return suffix[0]
    if len(suffix) > 1:
        raise ScaffoldError(
            "ambiguous Python symbol: "
            + ", ".join(item.key for item in suffix[:10]))
    raise ScaffoldError(f"no public Python symbol matches {name!r}")


def _hints_payload(symbol: PythonSymbol) -> list[dict[str, Any]]:
    return [hint.to_dict() for hint in hints_for(symbol)]


def _unsupported_hints(symbol: PythonSymbol) -> list[str]:
    return [hint.reason for hint in hints_for(symbol)
            if hint.support == "unsupported"]


def _class_skeleton(symbol: PythonSymbol, *, notes: list[str]) -> tuple[str, list[str]]:
    if symbol.kind != "dataclass":
        raise ScaffoldError(
            f"{symbol.key} is a {symbol.kind}, not a dataclass; complex "
            "classes, protocols, enums and exception types are refused")
    decorators = _decorator_names(list(symbol.decorators))
    unsupported = [name for name in decorators
                   if name not in ("dataclass",)]
    if unsupported:
        raise ScaffoldError(
            f"{symbol.key} has decorator(s) {', '.join(unsupported)}; "
            "decorated shapes are refused")
    if symbol.bases:
        raise ScaffoldError(
            f"{symbol.key} has base(s) {', '.join(symbol.bases)}; a complex "
            "class is refused")
    methods = [name for name in symbol.methods]
    if methods:
        raise ScaffoldError(
            f"{symbol.key} has method(s) {', '.join(methods[:5])}; only a "
            "field-only dataclass can be scaffolded")
    if not symbol.fields:
        raise ScaffoldError(
            f"{symbol.key} has no annotated fields; an empty dataclass is "
            "not scaffolded")
    reasons = _unsupported_hints(symbol)
    if reasons:
        raise ScaffoldError(f"{symbol.key}: {reasons[0]}")
    fields: list[tuple[str, str]] = []
    for item in symbol.fields:
        rust_type = _simple_type(item.annotation, where=f"{symbol.key}.{item.name}")
        fields.append((_rust_ident(item.name, where=f"{symbol.key} field"),
                       rust_type))
    struct_name = _rust_ident(symbol.name, where=f"{symbol.key} name")
    lines: list[str] = []
    if symbol.doc:
        lines.append(f"/// {symbol.doc}")
    lines.append("#[derive(Debug, Clone)]")
    lines.append(f"pub struct {struct_name} {{")
    for name, rust_type in fields:
        lines.append(f"    pub {name}: {rust_type},")
    lines.append("}")
    notes.append(
        "derives are limited to Debug + Clone; add Eq/Hash/Serialize or "
        "field-specific derives only if the Python class really had them")
    if any(item.annotation.strip().endswith("str") for item in symbol.fields):
        notes.append(
            "str fields are rendered as owned String; choose &str with a "
            "lifetime if the struct should borrow")
    if any(item.default for item in symbol.fields):
        notes.append(
            "Python field defaults are not translated; add a constructor or "
            "default policy deliberately")
    if any("frozen=True" in item for item in symbol.decorators):
        notes.append(
            "the Python dataclass is frozen; consider Eq/PartialEq and a "
            "constructor policy for the Rust struct")
    return "\n".join(lines), notes


def _function_skeleton(symbol: PythonSymbol, *,
                       notes: list[str]) -> tuple[str, list[str]]:
    if symbol.kind not in ("function", "async_function"):
        raise ScaffoldError(
            f"{symbol.key} is a {symbol.kind}, not a function; only a simple "
            "function or dataclass can be scaffolded")
    if symbol.decorators:
        raise ScaffoldError(
            f"{symbol.key} is decorated ({', '.join(symbol.decorators)}); "
            "decorated functions are refused")
    if symbol.varargs or symbol.kwargs:
        raise ScaffoldError(
            f"{symbol.key} uses *args/**kwargs; that shape is refused")
    if symbol.keyword_only:
        raise ScaffoldError(
            f"{symbol.key} has keyword-only parameters; Rust has no direct "
            "equivalent and the call shape would change")
    if getattr(symbol, "defaults", False):
        raise ScaffoldError(
            f"{symbol.key} has default parameter values; the scaffold does "
            "not translate defaults, so the call shape would change")
    reasons = _unsupported_hints(symbol)
    if reasons:
        raise ScaffoldError(f"{symbol.key}: {reasons[0]}")
    annotations = dict(symbol.arg_annotations)
    params: list[str] = []
    for name in symbol.args:
        annotation = annotations.get(name, "")
        if not annotation:
            raise ScaffoldError(
                f"{symbol.key}({name}) has no annotation; unannotated "
                "parameters are refused")
        rust_name = _rust_ident(name, where=f"{symbol.key} parameter")
        params.append(
            f"{rust_name}: "
            f"{_simple_type(annotation, where=f'{symbol.key}({name})')}")
    if not symbol.returns:
        raise ScaffoldError(
            f"{symbol.key} has no return annotation; unknown return types "
            "are refused")
    return_type = _simple_type(symbol.returns, where=f"{symbol.key} return")
    func_name = _rust_ident(symbol.name, where=f"{symbol.key} name")
    lines: list[str] = []
    if symbol.doc:
        lines.append(f"/// {symbol.doc}")
    prefix = "pub async fn" if symbol.async_ else "pub fn"
    ret = "" if return_type == "()" else f" -> {return_type}"
    lines.append(f"{prefix} {func_name}({', '.join(params)}){ret} {{")
    lines.append(f'    todo!("port from {symbol.key}")')
    lines.append("}")
    if symbol.async_:
        notes.append(
            "async fn renders without choosing a runtime; add the project's "
            "executor and error type")
    if "i64" in {part.split(": ", 1)[-1] for part in params}:
        notes.append(
            "int is rendered as i64; the integer width and overflow policy "
            "are a project decision")
    if return_type == "String":
        notes.append(
            "str return values are rendered as owned String; a borrowed "
            "&str may be cheaper at the call site")
    notes.append(
        "the body is todo!(); this scaffold does not translate behaviour, "
        "errors, logging or tests")
    return "\n".join(lines), notes


def scaffold(root: str | Path, symbol_name: str, target: str, *,
             config: MigrationConfig | None = None,
             write: bool = False,
             base_sha256: str | None = None) -> dict[str, Any]:
    """Render and optionally write one suggested Rust skeleton."""
    root_p = Path(root).resolve()
    name = (symbol_name or "").strip()
    if not name or len(name) > MAX_SYMBOL_CHARS:
        raise ScaffoldError("a bounded Python symbol name is required")
    target_text = (target or "").strip()
    if not target_text or len(target_text) > MAX_TARGET_CHARS:
        raise ScaffoldError("a bounded --target path is required")
    if config is None:
        from ozagia.config import load_project_config
        config = load_project_config(root_p).migration
    if not config.configured:
        raise ScaffoldError(
            "no [migration] section with python and rust trees is configured")
    target_path = (root_p / target_text).resolve()
    try:
        rel_target = target_path.relative_to(root_p).as_posix()
    except ValueError as exc:
        raise ScaffoldError("target escapes the repository root") from exc
    if target_path.suffix.lower() != ".rs":
        raise ScaffoldError("--target must be a .rs file")
    rust_tree = (root_p / str(config.rust)).resolve()
    try:
        target_path.relative_to(rust_tree)
    except ValueError as exc:
        raise ScaffoldError(
            "--target must be inside the configured Rust tree "
            f"{config.rust!r}") from exc
    if not target_path.is_file():
        raise ScaffoldError(
            "target must already exist; scaffold appends through the edit "
            "transaction and never creates files implicitly")

    inventory = inventory_python(root_p, config)
    symbol = _find_symbol(inventory, name)
    notes: list[str] = []
    if symbol.kind == "dataclass":
        skeleton, notes = _class_skeleton(symbol, notes=notes)
    elif symbol.kind in ("function", "async_function"):
        skeleton, notes = _function_skeleton(symbol, notes=notes)
    else:
        raise ScaffoldError(
            f"{symbol.key} is a {symbol.kind}; only a simple dataclass or "
            "function can be scaffolded")
    header = (f"// ozagia migration scaffold from {symbol.key}: suggested "
              "skeleton, not compiled or verified")
    skeleton = header + "\n" + skeleton

    src = read_source(root_p, rel_target, suffixes=(".rs",),
                      max_bytes=MAX_TARGET_BYTES, error_cls=ScaffoldError)
    check_base(src, base_sha256, ScaffoldError)
    existing = parse_rust(target_path, src.text)
    if any(item.get("name") == symbol.name for item in
           existing.get("symbols") or ()):
        raise ScaffoldError(
            f"{rel_target} already defines {symbol.name!r}; refusing a "
            "duplicate scaffold")
    separator = "" if src.text.endswith(("\n", "\r")) else src.newline
    skeleton_text = separator + src.newline + normalize_newlines(
        skeleton, src.newline) + src.newline
    new_text = src.text + skeleton_text
    checks = parse_rust(target_path, new_text)
    backend = str(checks.get("backend") or "unavailable")
    if backend == "tree-sitter" and checks.get("has_errors"):
        raise ScaffoldError(
            "the scaffold would not parse as Rust; the target file was not "
            "changed")
    parse_note = (
        "tree-sitter parse gate passed"
        if backend == "tree-sitter"
        else f"parse gate unavailable ({backend}); tree-sitter-rust is an "
             "optional extra")
    new_sha = sha256_bytes(encode_text(src, new_text))
    diff = list(difflib.unified_diff(
        src.text.splitlines(keepends=True),
        new_text.splitlines(keepends=True),
        fromfile=f"a/{rel_target}", tofile=f"b/{rel_target}"))
    backup = None
    if write:
        backup = write_source(root_p, src, new_text, error_cls=ScaffoldError)
    return {
        "ok": True,
        "applied": bool(write),
        "preview": not write,
        "symbol": symbol.key,
        "target": rel_target,
        "base_sha256": src.sha256,
        "new_sha256": new_sha,
        "candidate": skeleton,
        "diff_view": "".join(diff[:MAX_DIFF_LINES]),
        "diff_note": "a unified diff is a rendered view; the edit goes "
                     "through ozagia.edit.txn",
        "hints": _hints_payload(symbol),
        "notes": notes,
        "parse_gate": {"backend": backend, "checked": True,
                       "note": parse_note},
        "backup": backup,
        "written": bool(write),
        "executed": False,
    }


__all__ = ["MAX_TARGET_BYTES", "ScaffoldError", "scaffold"]
