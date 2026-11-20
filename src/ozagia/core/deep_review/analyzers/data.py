"""Bounded offline inventory of source data models and structured files.

This is a descriptive pass: it never imports target code and does not infer
runtime invariants from annotations. Unsupported syntax and formats are listed
as unknown coverage instead of being treated as empty.
"""
from __future__ import annotations

import ast
import json
import math
import os
import time
import tomllib
from pathlib import Path

from ._bounded_read import read_text as _read_text

MAX_FILES = 2000
MAX_FILE_BYTES = 512_000
MAX_TOTAL_BYTES = 12_000_000
MAX_ITEMS = 400
MAX_YAML_NODES = 50_000
MAX_YAML_DEPTH = 64
SKIP_DIRS = {".git", ".ozagia", ".venv", "venv", "node_modules", "__pycache__", ".tox", "target", "dist"}
_STRUCTURED = {".json", ".toml", ".yaml", ".yml"}


def _yaml_value(source: str):
    """Load one bounded, unambiguous YAML document without object tags."""
    import yaml

    class BoundedSafeLoader(yaml.SafeLoader):
        def __init__(self, stream):
            super().__init__(stream)
            self._oz_nodes = 0
            self._oz_depth = 0

        def compose_node(self, parent, index):
            self._oz_nodes += 1
            self._oz_depth += 1
            if self._oz_nodes > MAX_YAML_NODES or self._oz_depth > MAX_YAML_DEPTH:
                self._oz_depth -= 1
                raise yaml.YAMLError("YAML structure exceeded analysis bounds")
            try:
                return super().compose_node(parent, index)
            finally:
                self._oz_depth -= 1

        def construct_mapping(self, node, deep=False):
            # YAML merge keys intentionally allow an explicit entry to
            # override an inherited one. Keep rejecting repeated explicit
            # keys, while preserving that standard merge behavior.
            explicit_entries = {
                id(entry) for entry in node.value
                if entry[0].tag != "tag:yaml.org,2002:merge"
            }
            self.flatten_mapping(node)
            mapping = {}
            explicit_keys = set()
            for entry in node.value:
                key_node, value_node = entry
                key = self.construct_object(key_node, deep=deep)
                try:
                    duplicate = key in mapping
                except TypeError as exc:
                    raise yaml.YAMLError("unhashable YAML mapping key") from exc
                # Track the key/value entry, not just its key node: a YAML
                # alias may reuse the same scalar node for both a merged key
                # and an explicit override.
                explicit = id(entry) in explicit_entries
                if duplicate and explicit and key in explicit_keys:
                    raise yaml.YAMLError(f"duplicate YAML mapping key {key!r}")
                mapping[key] = self.construct_object(value_node, deep=deep)
                if explicit:
                    explicit_keys.add(key)
            return mapping

    loader = BoundedSafeLoader(source)
    try:
        value = loader.get_single_data()
    finally:
        loader.dispose()
    return value


def _decorator_name(node: ast.expr) -> str:
    if isinstance(node, ast.Call):
        node = node.func
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _annotation(node: ast.expr | None) -> str | None:
    if node is None:
        return None
    try:
        return ast.unparse(node)[:240]
    except (ValueError, TypeError):
        return None


def _python_models(path: str, source: str):
    tree = ast.parse(source, filename=path)
    models = []
    schemas = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            decorators = [_decorator_name(item) for item in node.decorator_list]
            bases = [_annotation(base) for base in node.bases]
            fields = []
            for member in node.body:
                if isinstance(member, ast.AnnAssign) and isinstance(member.target, ast.Name):
                    fields.append({"name": member.target.id,
                                   "annotation": _annotation(member.annotation),
                                   "defaulted": member.value is not None,
                                   "default_factory": bool(member.value and isinstance(member.value, ast.Call)
                                                            and _decorator_name(member.value.func) == "field")})
            if fields or any(name in {"dataclass", "attrs", "define"} for name in decorators) or \
                    any(str(base).split(".")[-1] in {"TypedDict", "NamedTuple", "Enum", "IntEnum", "StrEnum"}
                        for base in bases if base):
                models.append({"name": node.name, "line": node.lineno,
                               "kind": "dataclass" if "dataclass" in decorators else
                                       "typed_record" if any(str(base).endswith("TypedDict") for base in bases if base)
                                       else "class",
                               "decorators": decorators, "bases": [b for b in bases if b],
                               "fields": fields[:MAX_ITEMS], "fields_truncated": len(fields) > MAX_ITEMS})
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            value = node.value
            if value is None:
                continue
            for target in targets:
                if isinstance(target, ast.Name) and "SCHEMA" in target.id.upper():
                    schemas.append({"name": target.id, "line": node.lineno,
                                    "literal": isinstance(value, (ast.Dict, ast.Constant)),
                                    "has_version_marker": "VERSION" in target.id.upper()})
    return models, schemas


def _keys(value, prefix="", cap=MAX_ITEMS, ancestors=frozenset()):
    found = []
    # Use an explicit stack so both pathological nesting and YAML aliases are
    # bounded by the item cap rather than Python's recursion limit.
    stack = [(value, prefix, ancestors)]
    while stack and len(found) < cap:
        current, current_prefix, seen = stack.pop()
        if isinstance(current, dict):
            identity = id(current)
            if identity in seen:
                continue
            next_seen = seen | {identity}
            children = []
            for key in sorted(current, key=lambda item: str(item)):
                path = f"{current_prefix}/{key}"
                found.append(path)
                children.append((current[key], path, next_seen))
                if len(found) >= cap:
                    break
            stack.extend(reversed(children))
        elif isinstance(current, list) and current:
            identity = id(current)
            if identity not in seen:
                stack.append((current[0], f"{current_prefix}/*", seen | {identity}))
    return found[:cap]


def analyze(root: str | Path, *, budget_s: float = 10.0) -> dict:
    """Inventory Python records/schema declarations and structured document keys.

    The result contains bounded ``inventory``, ``findings`` (currently
    descriptive checks only), and ``not_checked`` reasons for unsupported or
    incomplete coverage.
    """
    try:
        budget = float(budget_s)
    except (TypeError, ValueError) as exc:
        raise ValueError("budget_s must be finite and non-negative") from exc
    if not math.isfinite(budget) or budget < 0:
        raise ValueError("budget_s must be finite and non-negative")
    root = Path(root).resolve()
    started = time.monotonic()
    deadline = started + min(budget, 60.0)
    models, schemas, documents, unknown = [], [], [], []
    checked, count, total = [], 0, 0
    stop_reason = None
    for base, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not (Path(base) / d).is_symlink())
        for name in sorted(names):
            if time.monotonic() >= deadline:
                stop_reason = "time budget exhausted"
                break
            suffix = Path(name).suffix.lower()
            if suffix != ".py" and suffix not in _STRUCTURED:
                continue
            path = Path(base) / name
            if path.is_symlink():
                unknown.append({"path": path.relative_to(root).as_posix(), "reason": "symlink outside scan scope"})
                continue
            if count >= MAX_FILES:
                stop_reason = f"file limit reached ({MAX_FILES})"
                break
            count += 1
            rel = path.relative_to(root).as_posix()
            try:
                source, size = _read_text(
                    path, min(MAX_FILE_BYTES, MAX_TOTAL_BYTES - total), root=root)
            except ValueError:
                unknown.append({"path": rel, "reason": "file or total byte limit reached"})
                continue
            except (OSError, UnicodeError):
                unknown.append({"path": rel, "reason": "unreadable or non-UTF-8 file"})
                continue
            total += size
            if suffix == ".py":
                try:
                    found_models, found_schemas = _python_models(rel, source)
                except (SyntaxError, ValueError):
                    unknown.append({"path": rel, "reason": "Python syntax could not be parsed"})
                    continue
                models.extend({"file": rel, **item} for item in found_models)
                schemas.extend({"file": rel, **item} for item in found_schemas)
            elif suffix in {".json", ".toml"}:
                try:
                    value = json.loads(source) if suffix == ".json" else tomllib.loads(source)
                except (ValueError, tomllib.TOMLDecodeError):
                    unknown.append({"path": rel, "reason": "structured document is invalid"})
                    continue
                keys = _keys(value)
                documents.append({"file": rel, "format": suffix[1:], "top_level_type": type(value).__name__,
                                  "keys": keys, "keys_truncated": len(keys) >= MAX_ITEMS})
            else:
                try:
                    import yaml
                except ImportError:
                    unknown.append({"path": rel, "reason": "YAML key inventory is unsupported without optional PyYAML"})
                    continue
                try:
                    value = _yaml_value(source)
                    keys = _keys(value)
                except (yaml.YAMLError, ValueError, RecursionError):
                    unknown.append({"path": rel, "reason": "structured document is invalid"})
                    continue
                documents.append({"file": rel, "format": suffix[1:], "top_level_type": type(value).__name__,
                                  "keys": keys, "keys_truncated": len(keys) >= MAX_ITEMS})
            checked.append(rel)
        if stop_reason:
            break
    if stop_reason:
        unknown.append({"reason": stop_reason})
    if not checked and not unknown:
        unknown.append({"reason": "no supported source records or structured documents found"})
    inventory = {"models": models[:MAX_ITEMS], "models_truncated": len(models) > MAX_ITEMS,
                 "schema_declarations": schemas[:MAX_ITEMS], "schemas_truncated": len(schemas) > MAX_ITEMS,
                 "documents": documents[:MAX_ITEMS], "documents_truncated": len(documents) > MAX_ITEMS,
                 "files_seen": count, "bytes_read": total,
                 "duration_s": round(time.monotonic() - started, 6)}
    return {"inventory": inventory, "findings": [], "checked": checked,
            "not_checked": sorted(unknown, key=lambda item: (item.get("path", ""), item["reason"]))}
