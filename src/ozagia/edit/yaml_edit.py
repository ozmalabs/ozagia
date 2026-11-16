"""Round-trip, path-addressed YAML edits.

The optional ruamel.yaml parser is required. There is deliberately no textual
fallback: without the parser the edit is refused. Aliases and anchors are
refused to avoid implicit shared-object mutation or expansion surprises.
"""
from __future__ import annotations

import difflib
import io
import math
from pathlib import Path
from typing import Any

from ozagia.edit.code_edit import CodeEditError, FORBIDDEN_SCRIPT_KEYS, MAX_SOURCE_BYTES
from ozagia.edit.lossless import LosslessText
from ozagia.edit.txn import (
    check_base,
    encode_text,
    normalize_newlines,
    read_source,
    sha256_bytes,
    write_source,
)

_NEWLINE_NAMES = {"\n": "lf", "\r\n": "crlf", "\r": "cr"}
MAX_DEPTH = 64
MAX_NODES = 100_000


def _yaml_parser():
    try:
        from ruamel.yaml import YAML
        from ruamel.yaml.tokens import AliasToken
    except ImportError as exc:
        raise CodeEditError(
            "YAML structural editing is unavailable; install ozagia[edit-yaml]"
        ) from exc
    yaml = YAML(typ="rt")
    yaml.preserve_quotes = True
    yaml.allow_duplicate_keys = False
    return yaml, AliasToken


def _bounded(value: Any, *, allow_roundtrip_types: bool = False) -> None:
    count = 0
    stack = [(value, 0)]
    seen: set[int] = set()
    while stack:
        item, depth = stack.pop()
        count += 1
        if count > MAX_NODES or depth > MAX_DEPTH:
            raise CodeEditError("YAML structure exceeds depth or node limit")
        if isinstance(item, dict):
            identity = id(item)
            if identity in seen:
                raise CodeEditError("YAML aliases or cyclic values are unsupported")
            seen.add(identity)
            stack.extend((key, depth + 1) for key in item)
            stack.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            identity = id(item)
            if identity in seen:
                raise CodeEditError("YAML aliases or cyclic values are unsupported")
            seen.add(identity)
            stack.extend((child, depth + 1) for child in item)
        elif item is None or isinstance(item, (str, bool, int, float)):
            if isinstance(item, float) and not math.isfinite(item):
                raise CodeEditError("non-finite YAML numbers are unsupported")
        elif allow_roundtrip_types:
            # ruamel's round-trip scalar wrappers subclass ordinary primitives;
            # unknown tagged/custom objects remain unsupported.
            raise CodeEditError("YAML custom tags or values are unsupported")
        else:
            raise CodeEditError("YAML edit values must use JSON-compatible types")


def _load(source: str):
    scanner, alias_token = _yaml_parser()
    yaml, _ = _yaml_parser()
    try:
        for token in scanner.scan(source):
            if isinstance(token, alias_token):
                raise CodeEditError("YAML aliases are unsupported")
        document = yaml.load(source)
    except CodeEditError:
        raise
    except Exception as exc:
        # Parser diagnostics may contain source snippets or scalar values.
        raise CodeEditError("malformed or duplicate-key YAML") from exc
    _bounded(document, allow_roundtrip_types=True)
    return yaml, document


def _value(value: Any) -> Any:
    _bounded(value)
    return value


def _resolve_parent(root: Any, path: list[Any]) -> tuple[Any, Any]:
    if not path:
        raise CodeEditError("YAML path must be non-empty")
    current = root
    for part in path[:-1]:
        if isinstance(current, dict):
            if not isinstance(part, str) or part not in current:
                raise CodeEditError("YAML path does not exist")
            current = current[part]
        elif isinstance(current, list):
            if (not isinstance(part, int) or isinstance(part, bool)
                    or part < 0 or part >= len(current)):
                raise CodeEditError("YAML list index is invalid")
            current = current[part]
        else:
            raise CodeEditError("YAML path cannot descend through a scalar")
    return current, path[-1]


def _apply_ops(document: Any, ops: list[dict[str, Any]]) -> list[dict[str, str]]:
    changes: list[dict[str, str]] = []
    for op in ops:
        if not isinstance(op, dict):
            raise CodeEditError("each YAML op must be an object")
        kind = str(op.get("op") or "").strip()
        path = op.get("path")
        if not isinstance(path, list):
            raise CodeEditError("YAML op path must be a list")
        if kind == "merge":
            target = document
            for part in path:
                if isinstance(target, dict):
                    if not isinstance(part, str) or part not in target:
                        raise CodeEditError("YAML path does not exist")
                    target = target[part]
                elif isinstance(target, list):
                    if (not isinstance(part, int) or isinstance(part, bool)
                            or part < 0 or part >= len(target)):
                        raise CodeEditError("YAML list index is invalid")
                    target = target[part]
                else:
                    raise CodeEditError("YAML path cannot descend through a scalar")
            value = op.get("value")
            if not isinstance(target, dict) or not isinstance(value, dict):
                raise CodeEditError("YAML merge target and value must be mappings")
            _value(value)
            for key, child in value.items():
                if not isinstance(key, str):
                    raise CodeEditError("YAML merge keys must be strings")
                target[key] = child
            changes.append({"op": kind, "summary": "merged YAML mapping"})
            continue
        parent, last = _resolve_parent(document, path)
        if kind == "set":
            value = _value(op.get("value"))
            if isinstance(parent, dict):
                if not isinstance(last, str) or last not in parent:
                    raise CodeEditError("YAML path does not exist")
                parent[last] = value
            elif isinstance(parent, list):
                if (not isinstance(last, int) or isinstance(last, bool)
                        or last < 0 or last >= len(parent)):
                    raise CodeEditError("YAML list index is invalid")
                parent[last] = value
            else:
                raise CodeEditError("YAML path parent is not a mapping or list")
            changes.append({"op": kind, "summary": "set YAML value"})
        elif kind == "delete":
            if isinstance(parent, dict):
                if not isinstance(last, str) or last not in parent:
                    raise CodeEditError("YAML path does not exist")
                del parent[last]
            elif isinstance(parent, list):
                if (not isinstance(last, int) or isinstance(last, bool)
                        or last < 0 or last >= len(parent)):
                    raise CodeEditError("YAML list index is invalid")
                del parent[last]
            else:
                raise CodeEditError("YAML path parent is not a mapping or list")
            changes.append({"op": kind, "summary": "deleted YAML value"})
        else:
            raise CodeEditError("unsupported YAML op; supported: set, delete, merge")
    return changes


def apply_yaml_edit_script(root: str | Path, script: dict[str, Any], *,
                           write: bool = False) -> dict[str, Any]:
    if not isinstance(script, dict):
        raise CodeEditError("edit script must be an object")
    for key in FORBIDDEN_SCRIPT_KEYS:
        if key in script:
            raise CodeEditError(f"raw {key!r} input is refused; use semantic YAML ops")
    root = Path(root).resolve()
    file = str(script.get("file") or "").strip()
    if not file:
        raise CodeEditError("edit script needs file")
    path = (root / file).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise CodeEditError("file escapes the repo root") from exc
    src = read_source(root, file, suffixes=(".yaml", ".yml"),
                      max_bytes=MAX_SOURCE_BYTES, error_cls=CodeEditError)
    check_base(src, script.get("base_sha256"), CodeEditError)
    ops = script.get("ops")
    if not isinstance(ops, list) or not ops:
        raise CodeEditError("edit script needs a non-empty ops list")
    yaml, document = _load(src.text)
    changes = _apply_ops(document, ops)
    _bounded(document, allow_roundtrip_types=True)
    stream = io.StringIO()
    try:
        yaml.dump(document, stream)
    except Exception as exc:
        raise CodeEditError("YAML edit could not be rendered safely") from exc
    new_source = normalize_newlines(stream.getvalue(), src.newline)
    if not src.text.endswith(("\n", "\r")) and new_source.endswith(src.newline):
        new_source = new_source[:-len(src.newline)]
    # Validate the actual rendered document and refuse aliases/duplicate keys.
    _load(new_source)
    new_sha = sha256_bytes(encode_text(src, new_source))
    diff_lines = list(difflib.unified_diff(
        src.text.splitlines(keepends=True), new_source.splitlines(keepends=True),
        fromfile=f"a/{file}", tofile=f"b/{file}",
    ))
    backup = write_source(root, src, new_source, error_cls=CodeEditError) if write else None
    return {
        "ok": True,
        "file": file,
        "applied": bool(write),
        "format": "yaml",
        "base_sha256": src.sha256,
        "new_sha256": new_sha,
        "before": {"lines": len(src.text.splitlines()), "sha256": src.sha256},
        "after": {"lines": len(new_source.splitlines()), "sha256": new_sha},
        "changes": changes,
        # The raw script/diff may carry secrets; only metadata is returned.
        "diff_view": "",
        "diff_note": "content omitted; inspect the requested file locally",
        "diff_line_count": len(diff_lines),
        "backup": str(backup) if backup else None,
        "newline": _NEWLINE_NAMES[src.newline],
        "bom": src.bom,
        "lossless": {
            "line_leaves": True,
            "roundtrip": LosslessText.from_lines(src.text).render() == src.text,
        },
        "graph_stale": bool(write),
    }
