"""The format-dispatching graph edit entry point.

One edit-script contract, multiple format adapters:

    .py                 -> ozagia.edit.code_edit (AST/symbol ops), or an
                           adapter edit engine (`engine: <name>`, or one of
                           its ops; see `ozagia.adapters`)
    .md / .markdown     -> ozagia.edit.markdown (section ops)
    .json               -> ozagia.edit.json_edit (span-preserving path ops)
    .yaml / .yml / ...  -> refused in v0 (no silent text-patch fallback)
    .rs / .lua         -> ozagia.edit.cst (CstNode subtree/leaf ops;
                           tree-sitter backend or a supplied synthetic graph)

The adapters share the transaction contract: preview by default, optional
`base_sha256` staleness check, parse/structural validation, atomic write,
backup under `.ozagia/edits/backups/`, and a `diff_view` that is only a
rendering. Raw diff/patch input is refused everywhere.

The point is that adding a format means adding a parser + renderer for that
format's block graph, not adding a new text-patch convention. The eventual
Rust/hub-IR adapter is the same interface with the graph renderer on the other
side.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ozagia.edit.code_edit import CodeEditError
from ozagia.edit.code_edit import apply_edit_script as apply_python_edit
from ozagia.edit.cst import apply_cst_edit_script
from ozagia.edit.json_edit import apply_json_edit_script
from ozagia.edit.markdown import apply_markdown_edit_script
from ozagia.adapters import registry

__all__ = ["CodeEditError", "apply_edit_script", "adapter_for"]


def adapter_for(file: str) -> str:
    suffix = Path(str(file or "")).suffix.lower()
    if suffix == ".py":
        return "python"
    if suffix in (".md", ".markdown"):
        return "markdown"
    if suffix == ".json":
        return "json"
    if suffix in (".yaml", ".yml"):
        return "yaml-refused"
    if suffix == ".rs":
        return "rust"
    if suffix == ".lua":
        return "lua"
    if suffix in (".toml", ".cfg", ".ini"):
        return "source-refused"
    return "unknown"


def _python_engine(root: str | Path, script: dict[str, Any]) -> Any:
    """The adapter engine a Python script asks for, or None for the built-in.

    The engine is named (`engine: <name>`) or implied by one of its op names.
    A named engine that no adapter registered is an error, not a fallback.
    """
    engines = registry(root).edit_engines
    named = str(script.get("engine") or "").strip().lower()
    if named and named not in ("python", "ast"):
        if named not in engines:
            raise CodeEditError(
                f"no edit engine {named!r} is registered; "
                f"known: {sorted(engines) or 'none'} (see ozagia.adapters)")
        return engines[named][1]
    ops = [op.get("op") for op in script.get("ops") or [] if isinstance(op, dict)]
    for name, (engine_ops, apply) in sorted(engines.items()):
        if any(op in engine_ops for op in ops):
            return apply
    return None


def apply_edit_script(root: str | Path, script: dict[str, Any], *,
                      write: bool = False) -> dict[str, Any]:
    if not isinstance(script, dict):
        raise CodeEditError("edit script must be an object")
    file = str(script.get("file") or "").strip()
    if not file:
        raise CodeEditError("edit script needs file")
    kind = adapter_for(file)
    if kind == "python":
        engine = _python_engine(root, script)
        if engine is not None:
            return engine(root, script, write=write)
        return apply_python_edit(root, script, write=write)
    if kind == "markdown":
        return apply_markdown_edit_script(root, script, write=write)
    if kind == "json":
        return apply_json_edit_script(root, script, write=write)
    if kind in ("rust", "lua"):
        return apply_cst_edit_script(root, script, write=write)
    if kind in ("yaml-refused", "source-refused"):
        raise CodeEditError(
            f"no graph edit adapter for {Path(file).suffix!r} yet; supported "
            f"in v0: .py (symbol/AST ops, plus adapter engines), .md/.markdown (section ops), "
            f".json (path ops), and .rs/.lua (CST ops). "
            f"Refusing rather than falling back to a text patch.")
    raise CodeEditError(f"unsupported file type: {file}")
