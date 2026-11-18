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

MAX_FILES = 2000
MAX_FILE_BYTES = 512_000
MAX_TOTAL_BYTES = 12_000_000
MAX_ITEMS = 400
SKIP_DIRS = {".git", ".ozagia", ".venv", "venv", "node_modules", "__pycache__", ".tox", "target", "dist"}
_STRUCTURED = {".json", ".toml", ".yaml", ".yml"}


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


def _keys(value, prefix="", cap=MAX_ITEMS):
    found = []
    if isinstance(value, dict):
        for key in sorted(value, key=lambda item: str(item)):
            path = f"{prefix}/{key}"
            found.append(path)
            if len(found) < cap:
                found.extend(_keys(value[key], path, cap - len(found)))
            if len(found) >= cap:
                break
    elif isinstance(value, list) and value:
        found.extend(_keys(value[0], f"{prefix}/*", cap))
    return found[:cap]


def analyze(root: str | Path, *, budget_s: float = 10.0) -> dict:
    """Inventory Python records/schema declarations and JSON/TOML keys.

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
                size = path.stat().st_size
                if size > MAX_FILE_BYTES or size > MAX_TOTAL_BYTES - total:
                    unknown.append({"path": rel, "reason": "file or total byte limit reached"})
                    continue
                source = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                unknown.append({"path": rel, "reason": "unreadable or non-UTF-8 file"})
                continue
            total += len(source.encode("utf-8"))
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
                unknown.append({"path": rel, "reason": "YAML key inventory is unsupported without an optional parser"})
                continue
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
