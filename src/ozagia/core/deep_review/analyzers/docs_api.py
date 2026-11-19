"""Conservative offline checks for Python API references in Markdown docs.

Literal ``from local.module import name`` and single-module
``import local.module [as alias]`` references are checked. The analyzer reads
text and ASTs; it never imports or executes the repository.
Documentation coverage is returned as explicit facts so a report can state
which local modules and symbols were inspected.
"""
from __future__ import annotations

import ast
import difflib
import math
import os
import re
import time
from pathlib import Path
from typing import Any

from ._bounded_read import read_text as _read_text

MAX_FILES = 3000
MAX_FILE_BYTES = 1_000_000
MAX_TOTAL_BYTES = 20_000_000
MAX_COVERAGE_SYMBOLS = 100
SKIP_DIRS = {".git", ".ozagia", ".venv", "venv", "node_modules", "__pycache__", ".tox", "target", "dist"}
_IMPORT = re.compile(r"^\s*from\s+([A-Za-z_][\w.]*)\s+import\s+(.+?)\s*(?:#.*)?$")
_IMPORT_MODULE = re.compile(
    r"^\s*import\s+([A-Za-z_][\w.]*)"
    r"(?:\s+as\s+[A-Za-z_]\w*)?\s*(?:#.*)?$")
_LIMITATIONS = [
    "only single-line literal `from local.module import name` and `import local.module` statements are checked",
    "comma-separated imports, prose API mentions, and non-Markdown examples are not checked",
    "runtime re-exports and versioned/removed API history are not assessed",
    "a documented name re-exported dynamically may appear missing",
]


def _module_path(root: Path, module: str) -> Path | None:
    parts = module.split(".")
    options = [base.joinpath(*parts).with_suffix(".py") for base in (root, root / "src", root / "lib")]
    options += [base.joinpath(*parts, "__init__.py") for base in (root, root / "src", root / "lib")]
    return next((p for p in options if p.is_file() and not p.is_symlink()), None)


def _looks_local(root: Path, module: str) -> bool:
    """True when a missing module's first component is a local Python unit."""
    first = module.split(".")[0]
    for base in (root, root / "src", root / "lib"):
        if (base / f"{first}.py").is_file() or (base / first / "__init__.py").is_file():
            return True
    return False


def _public_names(path: Path, root: Path) -> set[str] | None:
    try:
        source, _ = _read_text(path, MAX_FILE_BYTES, root=root)
        tree = ast.parse(source, filename=str(path))
    except (OSError, UnicodeError, SyntaxError, ValueError):
        return None
    names = set()
    explicit = None
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and not node.name.startswith("_"):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    if target.id == "__all__" and isinstance(node.value, (ast.List, ast.Tuple)):
                        explicit = {elt.value for elt in node.value.elts if isinstance(elt, ast.Constant) and isinstance(elt.value, str)}
                    elif not target.id.startswith("_"):
                        names.add(target.id)
    return explicit if explicit is not None else names


def _rename_candidates(missing: str, names: set[str]) -> list[str]:
    """Bounded, deterministic near-name evidence; never a rename assertion."""
    return sorted(difflib.get_close_matches(missing, sorted(names), n=3,
                                             cutoff=0.7))


def _missing_finding(rel: str, line_no: int, module: str, symbol: str, *,
                     source_rel: str | None = None,
                     candidates: list[str] | None = None) -> dict[str, Any]:
    if source_rel is None:
        return {
            "section": "documents-and-api",
            "type": "stale_documented_api",
            "api_state": "module_missing",
            "severity": "minor",
            "confidence": "medium",
            "keys": [f"file:{rel}", f"module:{module}"],
            "line": line_no,
            "summary": (f"Documentation imports `{symbol}` from `{module}`, but no "
                        "local Python module exists at that path."),
            "recommendation": "Update the documented import path or restore the module.",
        }
    return {
        "section": "documents-and-api",
        "type": "stale_documented_api",
        "api_state": "removed_or_renamed",
        "severity": "minor",
        "confidence": "medium",
        "keys": [f"file:{rel}", f"file:{source_rel}",
                 f"symbol:{module}.{symbol}"],
        "line": line_no,
        "summary": (f"Documentation imports `{symbol}` from local module `{module}`, "
                    "but that public name was not found."),
        "recommendation": ("Check whether the example is outdated, the symbol was "
                           "renamed, or it is re-exported through another module."),
        "rename_candidates": candidates or [],
    }


def _missing_module_finding(rel: str, line_no: int,
                            module: str) -> dict[str, Any]:
    return {
        "section": "documents-and-api",
        "type": "stale_documented_api",
        "api_state": "module_missing",
        "severity": "minor",
        "confidence": "medium",
        "keys": [f"file:{rel}", f"module:{module}"],
        "line": line_no,
        "summary": (f"Documentation imports local module `{module}`, but no "
                    "local Python module exists at that path."),
        "recommendation": "Update the documented import path or restore the module.",
    }


def analyze_with_coverage(root: str | Path, *, budget_s: float = 10.0) -> dict[str, Any]:
    """Return findings, checked docs, skips, and explicit documentation facts.

    Result shape: ``{"findings", "checked", "skipped", "coverage"}``. Coverage
    is a fact set: it names documents, local modules, and public symbols that
    were compared, plus the limitations of this static check.
    """
    root = Path(root).resolve()
    try:
        budget = float(budget_s)
    except (TypeError, ValueError) as exc:
        raise ValueError("budget_s must be finite and non-negative") from exc
    if not math.isfinite(budget) or budget < 0:
        raise ValueError("budget_s must be finite and non-negative")
    deadline = time.monotonic() + min(budget, 60.0)
    docs = []
    files_seen = total_bytes = 0
    skipped = []
    for base, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not (Path(base) / d).is_symlink())
        for name in sorted(names):
            if time.monotonic() >= deadline:
                skipped.append({"reason": "time budget exhausted; remaining documentation not checked"})
                break
            p = Path(base) / name
            if p.suffix.lower() not in {".md", ".markdown"}:
                continue
            if p.is_symlink():
                skipped.append({"path": p.relative_to(root).as_posix(), "reason": "symlinked documentation is outside scan scope"})
                continue
            if files_seen >= MAX_FILES:
                skipped.append({"reason": f"file limit reached ({MAX_FILES}); remaining documentation not checked"})
                break
            files_seen += 1
            try:
                text, size = _read_text(p, min(MAX_FILE_BYTES, MAX_TOTAL_BYTES - total_bytes), root=root)
            except (OSError, UnicodeError):
                skipped.append({"path": p.relative_to(root).as_posix(), "reason": "documentation unavailable"})
                continue
            except ValueError:
                skipped.append({"path": p.relative_to(root).as_posix(), "reason": "documentation size limit reached"})
                continue
            total_bytes += size
            docs.append((p, text))
        if time.monotonic() >= deadline or files_seen >= MAX_FILES:
            break

    findings = []
    checked = []
    cache: dict[Path, set[str] | None] = {}
    modules: dict[str, dict[str, Any]] = {}
    public_by_module: dict[str, set[str]] = {}
    local_imports = external_imports = 0
    local_module_imports = external_module_imports = stale_local_modules = 0
    documented_modules: list[dict[str, str]] = []
    documented_count = removed_count = stale_local = 0
    unparseable: set[str] = set()
    for doc, text in docs:
        rel = doc.relative_to(root).as_posix()
        checked.append(rel)
        for line_no, line in enumerate(text.splitlines(), 1):
            module_match = _IMPORT_MODULE.match(line)
            if module_match:
                module = module_match.group(1)
                source = _module_path(root, module)
                if source is None:
                    if _looks_local(root, module):
                        stale_local_modules += 1
                        findings.append(_missing_module_finding(
                            rel, line_no, module))
                    else:
                        external_module_imports += 1
                else:
                    local_module_imports += 1
                    documented_modules.append({
                        "documentation": rel,
                        "line": line_no,
                        "module": module,
                        "source": source.relative_to(root).as_posix(),
                    })
            match = _IMPORT.match(line)
            if not match:
                continue
            module, imported = match.groups()
            source = _module_path(root, module)
            if source is None:
                if _looks_local(root, module):
                    stale_local += 1
                    for token in imported.split(","):
                        symbol = token.strip().split()[0] if token.strip() else ""
                        if symbol and symbol != "*":
                            findings.append(_missing_finding(
                                rel, line_no, module, symbol))
                else:
                    external_imports += 1
                continue
            local_imports += 1
            if source not in cache:
                cache[source] = _public_names(source, root)
            names = cache[source]
            module_rel = source.relative_to(root).as_posix()
            if names is None:
                if module_rel not in unparseable:
                    skipped.append({"path": module_rel, "reason": "referenced Python module could not be parsed"})
                    unparseable.add(module_rel)
                modules[module_rel] = {
                    "state": "unparseable", "public_symbols": 0,
                    "documented_symbols": [], "undocumented_symbols": [],
                }
                continue
            row = modules.setdefault(module_rel, {
                "state": "parsed", "public_symbols": len(names),
                "documented_symbols": [], "undocumented_symbols": [],
            })
            public_by_module[module_rel] = names
            for token in imported.split(","):
                symbol = token.strip().split()[0] if token.strip() else ""
                if not symbol or symbol == "*":
                    continue
                if symbol in names:
                    documented_count += 1
                    if symbol not in row["documented_symbols"]:
                        row["documented_symbols"].append(symbol)
                    continue
                removed_count += 1
                findings.append(_missing_finding(
                    rel, line_no, module, symbol, source_rel=module_rel,
                    candidates=_rename_candidates(symbol, names)))
    for module_rel, row in modules.items():
        row["documented_symbols"] = sorted(
            row["documented_symbols"])[:MAX_COVERAGE_SYMBOLS]
        row["undocumented_symbols"] = sorted(
            public_by_module.get(module_rel, set()) -
            set(row["documented_symbols"]))[:MAX_COVERAGE_SYMBOLS]
    if not docs and not skipped:
        skipped.append({"reason": "no Markdown documentation files found"})
    coverage = {
        "docs_checked": checked,
        "docs_found": len(docs),
        "local_imports_checked": local_imports,
        "external_imports_ignored": external_imports,
        "stale_local_imports": stale_local,
        "local_module_imports_checked": local_module_imports,
        "external_module_imports_ignored": external_module_imports,
        "stale_local_module_imports": stale_local_modules,
        "documented_modules": documented_modules,
        "documented_symbols_checked": documented_count,
        "removed_or_renamed_symbols": removed_count,
        "modules": dict(sorted(modules.items())),
        "limitations": list(_LIMITATIONS),
    }
    return {"findings": findings, "checked": checked, "skipped": skipped,
            "coverage": coverage}


def analyze(root, *, budget_s=10.0):
    """Return ``(findings, checked_docs, skipped)`` for local Markdown API imports."""
    result = analyze_with_coverage(root, budget_s=budget_s)
    return result["findings"], result["checked"], result["skipped"]


__all__ = ["analyze", "analyze_with_coverage"]
