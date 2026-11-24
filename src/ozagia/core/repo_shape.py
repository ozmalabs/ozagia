"""One deterministic reference page describing a repository's shape.

``ozagia overview`` (alias ``shape``) answers the orientation questions an
agent or a person asks before reading code: what is this repository, what are
its parts, where do I start, how do the docs map to the code, and which
commands and conventions apply. It composes facts that already exist in the
checkout:

* ``README.md`` and ``pyproject.toml`` for identity, prose and package metadata;
* a bounded directory walk (the project configuration's source/test/skip rules)
  for layout, languages and file counts;
* the Python AST for module docstrings, symbol/test counts, ``add_parser``
  command names and test imports;
* the repo-map **tag cache** (read-only) for architecture, when one exists;
* the graph snapshot (``.ozagia/graph.json``, read-only) for test-to-module
  links and recorded counts, when one exists;
* Markdown fences in README/AGENTS/contributing docs for commands.

It never imports the target project, executes its code, starts a process,
opens a socket, calls a model or writes a file. The only write in the feature
is an explicit ``ozagia overview --out PATH`` (atomic) at the CLI edge.

Determinism contract: the report contains no absolute paths, dates or elapsed
times (the snapshot's own recorded ``generated_at`` is input data, not a
clock). Every collection is sorted, and ``content_hash`` is the SHA-256 of the
canonical JSON of the report without the hash field. Two runs over unchanged
inputs produce byte-identical Markdown and JSON.
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import tempfile
import time
import tomllib
from pathlib import Path
from typing import Any, Iterable

from ozagia.config import (
    GENERIC_SKIP_DIRS,
    load_project_config,
    state_path,
)

SCHEMA = "ozagia.repo_shape.v1"
GENERATOR = "ozagia overview"
DEFAULT_BUDGET_S = 10.0
DEFAULT_MAX_MODULES = 40
MAX_WALK_FILES = 50_000
MAX_ANALYZE_FILES = 2_000
MAX_TEST_FILES = 50_000
MAX_FILE_BYTES = 256 * 1024
MAX_README_BYTES = 128 * 1024
MAX_DOC_BYTES = 48 * 1024
MAX_DOC_FILES = 200
MAX_GRAPH_BYTES = 96 * 1024 * 1024
MAX_MARKDOWN_COMMANDS = 40
MAX_COMMANDS_PER_KIND = 8
MAX_DOC_LINKS = 300
MAX_PATH_MENTIONS = 300
MAX_TEST_ROWS = 20
MAX_ENTRY_COMMANDS = 80
MAX_CONVENTIONS = 8

LANGUAGE_BY_SUFFIX = {
    ".py": "python",
    ".pyi": "python",
    ".rs": "rust",
    ".lua": "lua",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".js": "javascript",
    ".jsx": "javascript",
    ".cjs": "javascript",
    ".mjs": "javascript",
    ".go": "go",
    ".java": "java",
    ".kt": "kotlin",
    ".swift": "swift",
    ".c": "c",
    ".h": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cxx": "cpp",
    ".hpp": "cpp",
    ".hh": "cpp",
    ".cs": "csharp",
    ".rb": "ruby",
    ".php": "php",
    ".scala": "scala",
    ".sh": "shell",
    ".bash": "shell",
    ".zsh": "shell",
    ".sql": "sql",
    ".html": "html",
    ".css": "css",
    ".scss": "css",
    ".vue": "vue",
    ".svelte": "svelte",
    ".ex": "elixir",
    ".exs": "elixir",
    ".erl": "erlang",
    ".hs": "haskell",
    ".ml": "ocaml",
    ".clj": "clojure",
    ".zig": "zig",
}
CODE_SUFFIXES = frozenset(LANGUAGE_BY_SUFFIX)
DOC_SUFFIXES = frozenset({".md", ".markdown", ".rst", ".txt", ".adoc"})
KEY_FILE_NAMES = (
    "README.md", "README.rst", "AGENTS.md", "CONTRIBUTING.md", "CHANGELOG.md",
    "LICENSE", "LICENSE.md", "COPYING", "pyproject.toml", "setup.py",
    "setup.cfg", "Cargo.toml", "package.json", "go.mod", "Makefile",
    "justfile", "Dockerfile", "docker-compose.yml", "compose.yaml",
    ".gitignore", ".ozagia.toml",
)
COMMAND_DOCS = (
    "README.md", "AGENTS.md", "CONTRIBUTING.md", "CONTRIBUTING.rst",
    "DEVELOPMENT.md", "docs/contributing.md", "docs/development.md",
    "docs/install.md", "docs/release.md",
)
_ENTRYPOINT_NAMES = frozenset({
    "__main__.py", "main.py", "app.py", "manage.py", "wsgi.py", "asgi.py",
})
_CORE_PARTS = frozenset({"core", "edit", "index", "eval", "intent",
                         "harness", "daemon", "research", "migration",
                         "languages", "model_api"})
_INTEGRATION_PARTS = frozenset({"integrations", "adapters", "plugins"})
_TOOL_PARTS = frozenset({"tools", "scripts", "bin", "cli", "commands"})
_SECRET_RE = re.compile(
    r"(?:gh[pousr]_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9]{20,}|"
    r"AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9-]{10,}|"
    r"glpat-[A-Za-z0-9_-]{20,})"
)
_LINK_RE = re.compile(r"\]\(([^)\s]+)")
_PATH_TOKEN_RE = re.compile(
    r"(?<![\w/.-])((?:src|tests|docs|scripts|tools|examples|addins|"
    r"evaluation|release|ozagia)/[\w./@+-]+\.(?:py|pyi|rs|toml|md|rst|json|"
    r"ya?ml|ts|tsx|js|jsx|go|java|kt|swift|c|cc|cpp|h|hpp|lua|sh))"
)
_IMPORT_RE = re.compile(r"^\s*(?:from\s+([\w.]+)\s+import|import\s+([\w.]+))",
                        re.MULTILINE)
_HASH_RE = re.compile(r"sha256:[0-9a-f]{64}")


# --------------------------------------------------------------------------- #
# Small deterministic helpers
# --------------------------------------------------------------------------- #


def _rel(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except (OSError, ValueError):
        return path.name


def _display_path(root: Path, path: Path) -> str:
    """A root-relative path when inside the checkout, else a stable label."""
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except (OSError, ValueError):
        return f"<state>/{path.name}"


def _one_line(text: str | None, limit: int = 180) -> str:
    if not text:
        return ""
    text = " ".join(str(text).split())
    return text[:limit].rstrip() + ("..." if len(text) > limit else "")


def _redact(text: str) -> str:
    return _SECRET_RE.sub("[redacted]", text)


def _under_prefix(rel: str, prefix: str) -> bool:
    prefix = prefix.replace("\\", "/").rstrip("/")
    return prefix in ("", ".") or rel == prefix or rel.startswith(prefix + "/")


def _test_file(rel: str, test_dirs: Iterable[str]) -> bool:
    parts = rel.split("/")
    name = parts[-1]
    suffix = Path(rel).suffix.lower()
    # Fixture trees frequently contain embedded projects; they are data for
    # this repository, not its tests.
    if any(part in {"fixtures", "fixture", "data", "testdata"}
           for part in parts[:-1]):
        return False
    if suffix not in CODE_SUFFIXES:
        return False
    if (name.startswith("test_") or name.endswith(("_test.py", "_test.rs"))
            or name == "conftest.py"):
        return True
    if not any(_under_prefix(rel, prefix) for prefix in test_dirs):
        return False
    # In a test tree, only conventional test names and Rust integration tests
    # count; helper modules are not test files.
    return suffix == ".rs"


def _doc_file(rel: str) -> bool:
    return (rel.startswith("docs/") or rel.split("/")[-1] in {
        "README.md", "README.rst", "AGENTS.md", "CHANGELOG.md",
        "CONTRIBUTING.md", "LICENSE", "LICENSE.md", "THIRD_PARTY_NOTICES.md",
    }) and Path(rel).suffix.lower() in DOC_SUFFIXES | {""}


def _code_file(rel: str) -> bool:
    return Path(rel).suffix.lower() in CODE_SUFFIXES


def _language(rel: str) -> str:
    return LANGUAGE_BY_SUFFIX.get(Path(rel).suffix.lower(), "other")


def _read_text(root: Path, rel: str, limit: int) -> str | None:
    path = root / rel
    try:
        if path.is_symlink() or not path.is_file():
            return None
        if path.stat().st_size > limit * 4:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                return handle.read(limit)
        return path.read_text(encoding="utf-8", errors="replace")[:limit]
    except OSError:
        return None


def _module_doc(tree: ast.AST) -> tuple[str, str]:
    try:
        doc = ast.get_docstring(tree, clean=True) or ""
    except (TypeError, ValueError):
        doc = ""
    if doc:
        for line in doc.splitlines():
            if line.strip():
                return _one_line(line), "module_docstring"
    return "", "none"


def _leading_comment(text: str) -> str:
    for line in text.splitlines()[:20]:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(("//", "#", "--", "/*", "*")):
            cleaned = stripped.lstrip("/*-# ").strip().rstrip("*/").strip()
            if cleaned:
                return _one_line(cleaned)
            continue
        break
    return ""


def _count_symbols(tree: ast.AST) -> int:
    return sum(1 for node in ast.walk(tree)
               if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                    ast.ClassDef)))


def _count_tests(tree: ast.AST) -> int:
    return sum(1 for node in ast.walk(tree)
               if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
               and node.name.startswith("test_"))


def _is_cli_module(rel: str) -> bool:
    name = rel.rsplit("/", 1)[-1]
    return (name in {"cli.py", "console.py", "manage.py", "__main__.py"}
            or name.endswith("_cli.py"))


def _collect_cli_commands(tree: ast.AST) -> list[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else (
            func.id if isinstance(func, ast.Name) else "")
        if name not in {"add_parser", "add_command"}:
            continue
        if not node.args:
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            if re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,60}", first.value):
                found.add(first.value)
    return sorted(found)


# The committed reference is source-stable. These sections describe the
# checkout itself; every volatile observation (git/snapshot/tag cache/time) is
# under ``environment`` and is deliberately not hashed or checked.
STABLE_KEYS = (
    "schema", "root_name", "identity", "source", "layout", "entry_points",
    "modules", "documentation", "tests", "commands", "conventions", "coverage",
)


def stable_projection(report: dict[str, Any]) -> dict[str, Any]:
    """The deterministic checkout/source shape, without environment facts."""
    return {key: report[key] for key in STABLE_KEYS if key in report}


def _content_hash(report: dict[str, Any]) -> str:
    payload = json.dumps(report, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# Configuration and the bounded walk
# --------------------------------------------------------------------------- #


class _Config:
    """The subset of project configuration this report needs."""

    def __init__(self, root: Path) -> None:
        self.error: str | None = None
        try:
            cfg = load_project_config(root)
        except Exception as exc:  # noqa: BLE001 - reported, never fatal
            cfg = None
            self.error = f"{type(exc).__name__}: {exc}"[:300]
        self.source_dirs = tuple(cfg.source_dirs) if cfg else (".",)
        self.test_dirs = tuple(cfg.test_dirs) if cfg else ("tests",)
        self.skip_dirs = frozenset(cfg.memory_skip_dirs) if cfg else GENERIC_SKIP_DIRS
        self.key_docs = tuple(cfg.key_docs) if cfg else ("README.md",)
        self.command_docs = tuple(cfg.command_docs) if cfg else ("README.md",)
        self.docs = tuple(cfg.docs) if cfg else ("README.md", "docs/**/*.md")


def _walk(root: Path, cfg: _Config, deadline: float | None,
          max_files: int) -> dict[str, Any]:
    files: list[str] = []
    dirs: list[str] = []
    truncated = False
    budget_hit = False
    skip = set(cfg.skip_dirs) | GENERIC_SKIP_DIRS | {".ozagia"}
    for dirpath, dirnames, filenames in os.walk(root):
        if deadline is not None and time.monotonic() > deadline:
            budget_hit = truncated = True
            break
        rel_dir = _rel(Path(dirpath), root)
        dirnames[:] = sorted(
            name for name in dirnames
            if name not in skip and not name.startswith(".")
            and not (Path(dirpath) / name).is_symlink())
        if rel_dir != ".":
            dirs.append(rel_dir)
        for name in sorted(filenames):
            if len(files) >= max_files:
                truncated = True
                break
            if name in {".DS_Store"} or name.endswith((".pyc", ".pyo")):
                continue
            rel = name if rel_dir == "." else f"{rel_dir}/{name}"
            files.append(rel)
            if deadline is not None and time.monotonic() > deadline:
                budget_hit = truncated = True
                break
        if truncated:
            break
    return {"files": files, "dirs": dirs, "truncated": truncated,
            "budget_exceeded": budget_hit}


# --------------------------------------------------------------------------- #
# Source analysis: modules, packages, tests, commands
# --------------------------------------------------------------------------- #


def _analyze_modules(root: Path, source_files: list[str], cfg: _Config,
                     deadline: float | None, max_modules: int
                     ) -> dict[str, Any]:
    modules: list[dict[str, Any]] = []
    cli_commands: list[dict[str, Any]] = []
    symbols_total = 0
    parse_failures: list[str] = []
    read_failures: list[str] = []
    budget_hit = False
    analyzed = 0
    for rel in source_files:
        if analyzed >= MAX_ANALYZE_FILES:
            break
        if deadline is not None and time.monotonic() > deadline:
            budget_hit = True
            break
        analyzed += 1
        text = _read_text(root, rel, MAX_FILE_BYTES)
        if text is None:
            read_failures.append(rel)
            modules.append(_module_row(rel, "", "unread", None))
            continue
        if not rel.endswith(".py"):
            modules.append(_module_row(rel, _leading_comment(text), "comment",
                                       None))
            continue
        try:
            tree = ast.parse(text)
        except (SyntaxError, ValueError):
            parse_failures.append(rel)
            modules.append(_module_row(rel, _leading_comment(text), "comment",
                                       None))
            continue
        responsibility, doc_source = _module_doc(tree)
        symbols = _count_symbols(tree)
        symbols_total += symbols
        modules.append(_module_row(rel, responsibility, doc_source, symbols))
        commands = _collect_cli_commands(tree) if _is_cli_module(rel) else []
        if commands:
            cli_commands.extend(
                {"command": command, "module": rel, "source": "ast:add_parser"}
                for command in commands[:MAX_ENTRY_COMMANDS])
    modules.sort(key=lambda item: (item["path"].count("/"), item["path"]))
    total = len(modules)
    shown = _select_modules(modules, max_modules)
    packages = _packages(modules)
    return {
        "modules": shown,
        "total_modules": total,
        "shown_modules": len(shown),
        "max_modules": max_modules,
        "truncated": total > len(shown),
        "groups": _group_modules(shown),
        "packages": packages,
        "symbols": symbols_total,
        "parse_failures": sorted(parse_failures)[:20],
        "read_failures": sorted(read_failures)[:20],
        "analyzed_files": analyzed,
        "budget_exceeded": budget_hit,
        "cli_commands": cli_commands[:MAX_ENTRY_COMMANDS],
    }


def _module_row(rel: str, responsibility: str, doc_source: str,
                symbols: int | None) -> dict[str, Any]:
    return {
        "path": rel,
        "package": rel.rsplit("/", 1)[0] if "/" in rel else "(root)",
        "name": rel.rsplit("/", 1)[-1],
        "responsibility": _redact(responsibility or "(no docstring)"),
        "doc_source": doc_source,
        "symbols": symbols,
    }


def _packages(modules: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    docs: dict[str, str] = {}
    for module in modules:
        package = module["package"]
        if package == "(root)":
            continue
        counts[package] = counts.get(package, 0) + 1
        if package not in docs and module["name"] == "__init__.py":
            docs[package] = module["responsibility"]
    rows = [
        {"path": package, "modules": count,
         "responsibility": docs.get(package, "(no package docstring)")}
        for package, count in counts.items()
    ]
    rows.sort(key=lambda item: (-item["modules"], item["path"]))
    return rows[:40]


def _group_for(rel: str) -> str:
    parts = {part.lower() for part in rel.split("/")[:-1]}
    name = rel.rsplit("/", 1)[-1].lower()
    if _test_file(rel, ("tests",)) or parts & {"tests", "test"}:
        return "tests"
    if parts & _INTEGRATION_PARTS:
        return "integrations"
    if parts & _CORE_PARTS or name in {"__init__.py", "__main__.py",
                                       "core.py", "memory.py", "config.py"}:
        return "core" if parts else "tools"
    if parts & _TOOL_PARTS or name in {"cli.py", "console.py", "main.py"}:
        return "tools"
    return "other"


_GROUP_ORDER = ("core", "integrations", "tools", "other")


def _select_modules(modules: list[dict[str, Any]], limit: int
                    ) -> list[dict[str, Any]]:
    """Breadth-first across groups, shallowest paths first within each."""
    buckets: dict[str, list[dict[str, Any]]] = {}
    for module in modules:
        buckets.setdefault(_group_for(module["path"]), []).append(module)
    shown: list[dict[str, Any]] = []
    positions = {name: 0 for name in buckets}
    while len(shown) < limit:
        progressed = False
        for name in _GROUP_ORDER:
            bucket = buckets.get(name) or []
            position = positions.get(name, 0)
            if position >= len(bucket):
                continue
            shown.append(bucket[position])
            positions[name] = position + 1
            progressed = True
            if len(shown) >= limit:
                break
        if not progressed:
            break
    return shown


def _group_modules(modules: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[str, list[dict[str, Any]]] = {}
    for module in modules:
        buckets.setdefault(_group_for(module["path"]), []).append(module)
    return [{"name": name, "modules": buckets[name]}
            for name in _GROUP_ORDER if buckets.get(name)]


def _analyze_tests(root: Path, test_files: list[str],
                   deadline: float | None) -> dict[str, Any]:
    tests_total = 0
    imports: dict[str, list[str]] = {}
    analyzed = 0
    unparsed: list[str] = []
    for rel in test_files:
        if analyzed >= MAX_ANALYZE_FILES:
            break
        if deadline is not None and time.monotonic() > deadline:
            break
        analyzed += 1
        if not rel.endswith(".py"):
            continue
        text = _read_text(root, rel, MAX_FILE_BYTES)
        if text is None:
            continue
        try:
            tree = ast.parse(text)
        except (SyntaxError, ValueError):
            unparsed.append(rel)
            continue
        tests_total += _count_tests(tree)
        found: list[str] = []
        for match in _IMPORT_RE.finditer(text):
            module = match.group(1) or match.group(2) or ""
            if module and not module.startswith("."):
                found.append(module)
        imports[rel] = sorted(set(found))
    return {"test_functions": tests_total, "imports": imports,
            "analyzed_files": analyzed, "unparsed": sorted(unparsed)[:20]}


# --------------------------------------------------------------------------- #
# Graph snapshot (read-only) and test links
# --------------------------------------------------------------------------- #


def _load_graph(root: Path) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    path = state_path(root, "graph.json")
    meta: dict[str, Any] = {
        "path": _display_path(root, path),
        "present": path.is_file(),
        "bytes": 0,
        "state": "missing" if not path.is_file() else "unknown",
        "counts": {},
        "head": None,
        "generated_at": None,
        "reason": None,
    }
    if not path.is_file():
        meta["reason"] = "no graph snapshot"
        return None, meta
    try:
        size = path.stat().st_size
        meta["bytes"] = size
        if size > MAX_GRAPH_BYTES:
            meta["reason"] = f"snapshot larger than {MAX_GRAPH_BYTES} bytes"
            return None, meta
        with path.open("rb") as handle:
            graph = json.load(handle)
        if not isinstance(graph, dict):
            meta["reason"] = "snapshot is not an object"
            return None, meta
    except (OSError, ValueError) as exc:
        meta["reason"] = f"{type(exc).__name__}: {exc}"[:300]
        return None, meta
    nodes = graph.get("nodes")
    if not isinstance(nodes, list):
        meta["reason"] = "snapshot has no nodes list"
        return None, meta
    repo_attrs: dict[str, Any] = {}
    for node in nodes:
        if isinstance(node, dict) and node.get("type") == "Repo":
            attrs = node.get("attrs")
            if isinstance(attrs, dict):
                repo_attrs = attrs
                break
    meta["head"] = repo_attrs.get("head")
    meta["generated_at"] = repo_attrs.get("generated_at")
    meta["counts"] = {
        "files": repo_attrs.get("files"),
        "symbols": repo_attrs.get("symbols"),
        "tests": repo_attrs.get("tests"),
        "docs": repo_attrs.get("docs"),
        "commits": repo_attrs.get("commits"),
        "failures": repo_attrs.get("failures"),
    }
    return graph, meta


def _graph_test_links(graph: dict[str, Any] | None) -> dict[str, list[str]]:
    if not graph:
        return {}
    nodes = graph.get("nodes") or []
    edges = graph.get("edges") or []
    by_id: dict[Any, dict[str, Any]] = {}
    for node in nodes:
        if isinstance(node, dict) and "id" in node:
            by_id[node["id"]] = node
    links: dict[str, set[str]] = {}
    for edge in edges:
        if not isinstance(edge, dict) or edge.get("type") not in {
                "tests_module", "mentions_file"}:
            continue
        src = by_id.get(edge.get("src"))
        tgt = by_id.get(edge.get("tgt"))
        if not src or not tgt or src.get("type") != "Test":
            continue
        test_attrs = src.get("attrs") or {}
        file_attrs = tgt.get("attrs") or {}
        test_rel = test_attrs.get("file")
        target_rel = file_attrs.get("path")
        if not isinstance(test_rel, str) or not isinstance(target_rel, str):
            continue
        if not target_rel.endswith(tuple(CODE_SUFFIXES)):
            continue
        links.setdefault(test_rel, set()).add(target_rel)
    return {rel: sorted(targets) for rel, targets in links.items()}


def _module_index(source_files: list[str], source_dirs: Iterable[str]
                  ) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    by_module: dict[str, list[str]] = {}
    by_stem: dict[str, list[str]] = {}
    for rel in source_files:
        stem = Path(rel).stem
        if stem not in {"__init__", "__main__"}:
            by_stem.setdefault(stem, []).append(rel)
        candidates: set[str] = set()
        for prefix in (*source_dirs, "src", "."):
            prefix = prefix.replace("\\", "/").strip("/")
            if prefix in ("", "."):
                candidates.add(rel)
            elif rel.startswith(prefix + "/"):
                candidates.add(rel[len(prefix) + 1:])
        for candidate in candidates:
            if candidate.endswith(".py"):
                dotted = candidate[:-3].replace("/", ".")
                if dotted.endswith(".__init__"):
                    dotted = dotted[:-9]
                by_module.setdefault(dotted, []).append(rel)
    return by_module, by_stem


def _map_tests(test_files: list[str], source_files: list[str],
               source_dirs: Iterable[str], graph_links: dict[str, list[str]],
               test_imports: dict[str, list[str]]) -> dict[str, Any]:
    by_module, by_stem = _module_index(source_files, source_dirs)
    mappings: dict[str, dict[str, Any]] = {}
    for rel in test_files:
        sources: set[str] = set()
        basis: set[str] = set()
        graph_targets = graph_links.get(rel, [])
        if graph_targets:
            sources.update(graph_targets)
            basis.add("graph")
        stem = Path(rel).stem
        names = [stem]
        if stem.startswith("test_"):
            names.append(stem[5:])
        if stem.endswith("_test"):
            names.append(stem[:-5])
        for name in names:
            for candidate in by_stem.get(name, []):
                if candidate != rel:
                    sources.add(candidate)
                    basis.add("filename")
        for module in test_imports.get(rel, []):
            for candidate in by_module.get(module, []):
                if candidate != rel and _code_file(candidate):
                    sources.add(candidate)
                    basis.add("import")
        if sources:
            mappings[rel] = {"sources": sorted(sources)[:8],
                             "basis": sorted(basis)}
    mapped = sorted(mappings)
    unmapped = sorted(set(test_files) - set(mappings))
    return {
        "count": len(test_files),
        "mapped_count": len(mapped),
        "unmapped_count": len(unmapped),
        "mapped": [{"test": rel, **mappings[rel]}
                   for rel in mapped[:MAX_TEST_ROWS]],
        "unmapped": unmapped[:MAX_TEST_ROWS],
        "truncated": len(mapped) > MAX_TEST_ROWS or len(unmapped) > MAX_TEST_ROWS,
    }


# --------------------------------------------------------------------------- #
# Documentation and commands
# --------------------------------------------------------------------------- #


def _title(text: str) -> str:
    for line in text.splitlines()[:80]:
        stripped = line.strip()
        if stripped.startswith("#"):
            return _one_line(stripped.lstrip("#").strip(), 160)
        if stripped and not stripped.startswith(("[!", "<", "|", "```")):
            return _one_line(stripped, 160)
    return ""


def _headings(text: str, limit: int = 12) -> list[str]:
    return [_one_line(line.lstrip("#").strip(), 100)
            for line in text.splitlines()
            if line.startswith("## ")][:limit]


def _highlight_bullets(text: str, limit: int = 8) -> list[str]:
    bullets: list[str] = []
    current: str | None = None
    in_fence = False

    def flush() -> None:
        nonlocal current
        if current:
            value = _one_line(_redact(current), 500)
            if value and value not in bullets:
                bullets.append(value)
        current = None

    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            if current:
                flush()
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if stripped.startswith(("- ", "* ")):
            flush()
            current = stripped[2:].strip()
        elif (current is not None and stripped
              and line[:1] in {" ", "\t"}):
            current += " " + stripped
        else:
            flush()
        if len(bullets) >= limit:
            break
    flush()
    return bullets[:limit]


def _first_prose(text: str, limit: int = 900) -> str:
    paragraphs: list[str] = []
    current: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            if paragraphs or current:
                break
            continue
        if not stripped:
            if current:
                paragraphs.append(" ".join(current))
                current = []
                if len(paragraphs) >= 2:
                    break
            continue
        if stripped.startswith(("[![", "![", "<", "|", "```")):
            continue
        current.append(stripped)
    if current and len(paragraphs) < 2:
        paragraphs.append(" ".join(current))
    return _one_line(_redact(" ".join(paragraphs)), limit)


def _documentation(root: Path, walk: dict[str, Any], cfg: _Config,
                   deadline: float | None,
                   excluded: set[str] | None = None) -> dict[str, Any]:
    excluded = excluded or set()
    readme_rel = ""
    for candidate in (*cfg.key_docs, "README.md", "README.rst"):
        if (root / candidate).is_file():
            readme_rel = candidate
            break
    readme_text = (_read_text(root, readme_rel, MAX_README_BYTES)
                   if readme_rel else None)
    readme = {
        "path": readme_rel or None,
        "title": _redact(_title(readme_text or "")),
        "prose": _first_prose(readme_text or ""),
        "headings": _headings(readme_text or ""),
        "highlights": _highlight_bullets(readme_text or ""),
        "state": "read" if readme_text is not None else "missing",
        "unknown": [] if readme_text is not None else ["README prose"],
    }

    doc_rels = [rel for rel in walk["files"] if _doc_file(rel)]
    doc_rels.sort(key=lambda rel: (
        0 if rel == readme_rel else 1,
        0 if rel.count("/") == 0 else 1,
        rel))
    scanned: list[dict[str, Any]] = []
    links_checked = 0
    links_existing = 0
    links_missing: list[dict[str, str]] = []
    path_mentions = 0
    path_found = 0
    groups: dict[str, int] = {}
    read_count = 0
    for rel in doc_rels:
        kind = _doc_kind(rel)
        groups[kind] = groups.get(kind, 0) + 1
        if len(scanned) >= MAX_DOC_FILES:
            continue
        if deadline is not None and time.monotonic() > deadline:
            continue
        read_count += 1
        text = _read_text(root, rel, MAX_DOC_BYTES)
        if text is None:
            continue
        scanned.append({"path": rel, "title": _redact(_title(text)),
                        "kind": kind, "bytes": (root / rel).stat().st_size
                        if (root / rel).is_file() else None})
        for match in list(_LINK_RE.finditer(text))[:MAX_DOC_LINKS]:
            target = match.group(1).strip()
            if (not target or target.startswith(("#", "http://", "https://",
                                                 "mailto:", "tel:", "data:"))):
                continue
            target = target.split("#", 1)[0]
            if not target or "://" in target:
                continue
            links_checked += 1
            resolved = (root / rel).parent / target
            if _exists(root, resolved, excluded):
                links_existing += 1
            elif len(links_missing) < 8:
                links_missing.append({"doc": rel, "target": target[:180]})
        for match in list(_PATH_TOKEN_RE.finditer(text))[:MAX_PATH_MENTIONS]:
            path_mentions += 1
            token = match.group(1)
            if (_exists(root, root / token, excluded)
                    or _exists(root, (root / rel).parent / token, excluded)):
                path_found += 1

    canonical = {
        "roadmap": "docs/roadmap.md" if (root / "docs/roadmap.md").is_file()
        else None,
        "plans": _dir_count(root, "docs/plans", ".md"),
        "results": _dir_count(root, "docs/results", ".md"),
        "research": _dir_count(root, "docs/research", ".md"),
    }
    scanned.sort(key=lambda item: item["path"])
    return {
        "readme": readme,
        "index": scanned,
        "shown_docs": len(scanned),
        "total_docs": len(doc_rels),
        "truncated": len(doc_rels) > len(scanned),
        "groups": groups,
        "canonical": canonical,
        "coverage": {
            "docs_scanned": read_count,
            "links_checked": links_checked,
            "links_to_existing_files": links_existing,
            "links_missing": len(links_missing) + max(0, links_checked - links_existing - len(links_missing)),
            "missing_examples": links_missing,
            "code_path_mentions": path_mentions,
            "code_paths_found": path_found,
            "note": ("Markdown links and path-like tokens only; a line is not "
                     "proof the documentation is still accurate."),
        },
    }


def _inside(root: Path, path: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _exists(root: Path, path: Path, excluded: set[str]) -> bool:
    """A target counts as existing when it is real or the excluded output."""
    try:
        rel = path.resolve().relative_to(root.resolve()).as_posix()
    except (OSError, ValueError):
        return False
    if rel in excluded:
        return True
    return _inside(root, path) and path.exists()


def _doc_kind(rel: str) -> str:
    parts = rel.split("/")
    if rel in {"README.md", "README.rst"}:
        return "readme"
    if len(parts) > 1 and parts[0] == "docs":
        if len(parts) > 2 and parts[1] == "plans":
            return "plans"
        if len(parts) > 2 and parts[1] == "results":
            return "results"
        if len(parts) > 2 and parts[1] == "research":
            return "research"
        return "docs"
    if rel in {"AGENTS.md", "CONTRIBUTING.md", "CHANGELOG.md"}:
        return "root"
    return "other"


def _dir_count(root: Path, rel: str, suffix: str) -> int | None:
    path = root / rel
    if not path.is_dir():
        return None
    count = 0
    try:
        for dirpath, dirnames, filenames in os.walk(path):
            dirnames[:] = sorted(name for name in dirnames
                                 if not name.startswith("."))
            count += sum(1 for name in filenames
                         if Path(name).suffix == suffix)
            if count > 10_000:
                break
    except OSError:
        return None
    return count


_SHELL_LANGUAGES = frozenset({"sh", "bash", "shell", "console", "zsh", "fish",
                              "powershell", "ps1"})
_COMMAND_NAMES = frozenset({
    "bash", "bun", "cargo", "cd", "cmake", "composer", "ctest", "curl", "deno",
    "docker", "dotnet", "gradle", "go", "hatch", "java", "jest", "just", "lua",
    "make", "mix", "mvn", "node", "npm", "nox", "npx", "ozagia", "ozagia-memory",
    "php", "pip", "pipx", "pnpm", "poetry", "pytest", "python", "python3",
    "ruff", "rustc", "sh", "swift", "tox", "uv", "uvx", "vitest", "yarn",
    "zig",
})


def _plausible_command(line: str) -> bool:
    """A no-language fence is accepted only when the first token is a command.

    Install docs often draw directory trees in an unlabelled fence. Known
    command names (or a path ending in one, such as ``.venv/bin/python``) keep
    those from being reported as runnable commands.
    """
    first = line.split(maxsplit=1)[0] if line.strip() else ""
    if not first or first.endswith("/"):
        return False
    if Path(first).name.lower() not in _COMMAND_NAMES:
        return False
    # A path plus aligned prose, with no comment marker, is a tree annotation.
    if ("/" in first and "  " in line.strip() and "#" not in line):
        return False
    return True


def _extract_commands(text: str, source: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    in_fence = False
    language = ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            if in_fence:
                in_fence = False
                language = ""
            else:
                in_fence = True
                language = stripped[3:].strip().lower()
            continue
        if not in_fence:
            continue
        if language and language not in _SHELL_LANGUAGES:
            continue
        if not language and line[:1].isspace():
            # Indented lines in an unlabelled fence are usually tree contents.
            continue
        if not stripped or stripped.startswith(("#", "//", "<!--")):
            continue
        for prefix in ("$ ", "> ", "% "):
            if stripped.startswith(prefix):
                stripped = stripped[len(prefix):].strip()
        if not stripped or len(stripped) > 240:
            continue
        if stripped.startswith(("{", "}", "<", "...", "|")):
            continue
        if not language and not _plausible_command(stripped):
            continue
        rows.append({"command": _redact(stripped), "source": source,
                     "kind": _command_kind(stripped)})
    return rows


def _command_kind(command: str) -> str:
    # Inline comments describe the command; they should not change its kind.
    low = command.split("#", 1)[0].strip().lower()
    if re.search(r"\b(ruff|eslint|clippy|golangci|lint|mypy|pyright|"
                 r"prettier|fmt|format)\b", low):
        return "lint"
    if re.search(r"\b(pytest|tox|nox|cargo test|go test|npm test|"
                 r"yarn test|pnpm test|make test|ctest|jest|vitest)\b", low):
        return "test"
    if re.search(r"\b(build|install|compile|bundle|uv pip|pip install|"
                 r"npm install|yarn install|pnpm install|cargo build|"
                 r"hatch|wheel)\b", low):
        return "build"
    if re.search(r"\b(run|start|serve|dev|index|agent|preview)\b", low):
        return "run"
    if re.match(r"^(?:ozagia|python|node|go|ruby|\./|make|npm|pnpm|yarn|"
                r"cargo|uv|scripts/)\b", low) or re.search(r"\.(?:sh|bash)\b", low):
        return "run"
    return "other"


def _commands(root: Path, cfg: _Config) -> dict[str, Any]:
    sources = list(dict.fromkeys((*cfg.command_docs, *COMMAND_DOCS)))
    seen: set[str] = set()
    by_kind: dict[str, list[dict[str, str]]] = {
        "build": [], "test": [], "lint": [], "run": [], "other": []}
    sources_read: list[str] = []
    for rel in sources:
        if not (root / rel).is_file():
            continue
        text = _read_text(root, rel, MAX_README_BYTES)
        if text is None:
            continue
        sources_read.append(rel)
        for row in _extract_commands(text, rel):
            if row["command"] in seen:
                continue
            seen.add(row["command"])
            bucket = by_kind[row["kind"]]
            if len(bucket) < MAX_COMMANDS_PER_KIND:
                bucket.append(row)
    return {
        "build": by_kind["build"],
        "test": by_kind["test"],
        "lint": by_kind["lint"],
        "run": by_kind["run"],
        "other": by_kind["other"],
        "sources": sources_read,
        "truncated": any(len(values) >= MAX_COMMANDS_PER_KIND
                         for values in by_kind.values()),
        "note": "commands are extracted from Markdown fences; none is executed",
    }


def _conventions(root: Path, pyproject: dict[str, Any]) -> list[dict[str, str]]:
    notes: list[dict[str, str]] = []
    tool = pyproject.get("tool") if isinstance(pyproject, dict) else None
    if isinstance(tool, dict):
        if "ruff" in tool:
            notes.append({"source": "pyproject.toml",
                          "text": "lint: ruff configured under [tool.ruff]"})
        if "pytest" in tool:
            testpaths = ((tool.get("pytest") or {}).get("ini_options") or {})
            paths = testpaths.get("testpaths") if isinstance(testpaths, dict) else None
            text = "test: pytest configured"
            if paths:
                text += f" (testpaths={', '.join(map(str, paths))})"
            notes.append({"source": "pyproject.toml", "text": text})
    if (root / "AGENTS.md").is_file():
        notes.append({"source": "AGENTS.md",
                      "text": "agent instructions are in AGENTS.md"})
    if (root / "CONTRIBUTING.md").is_file():
        notes.append({"source": "CONTRIBUTING.md",
                      "text": "contribution workflow is in CONTRIBUTING.md"})
    return notes[:MAX_CONVENTIONS]


# --------------------------------------------------------------------------- #
# Git head and architecture
# --------------------------------------------------------------------------- #


def _git_head(root: Path) -> dict[str, Any]:
    git = root / ".git"
    if not git.exists():
        return {"commit": None, "ref": None, "state": "missing",
                "source": ".git"}
    if git.is_file():
        try:
            text = git.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return {"commit": None, "ref": None, "state": "unknown",
                    "source": ".git"}
        if text.startswith("gitdir:"):
            candidate = Path(text.split(":", 1)[1].strip())
            git = candidate if candidate.is_absolute() else (root / candidate)
    head_path = git / "HEAD"
    try:
        head = head_path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return {"commit": None, "ref": None, "state": "unknown",
                "source": ".git/HEAD"}
    if head.startswith("ref:"):
        ref = head.split(":", 1)[1].strip()
        ref_path = git / ref
        try:
            commit = ref_path.read_text(encoding="utf-8").strip()
            return {"commit": commit, "ref": ref, "state": "read",
                    "source": f".git/{ref}"}
        except OSError:
            packed = _packed_ref(git / "packed-refs", ref)
            return {"commit": packed, "ref": ref,
                    "state": "read" if packed else "unknown",
                    "source": ".git/packed-refs" if packed else f".git/{ref}"}
    return {"commit": head or None, "ref": None, "state": "read",
            "source": ".git/HEAD"}


def _packed_ref(path: Path, ref: str) -> str | None:
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith(("#", "^")):
                continue
            parts = line.split()
            if len(parts) == 2 and parts[1] == ref:
                return parts[0]
    except OSError:
        return None
    return None


def _architecture(root: Path, cfg: _Config, source_files: list[str],
                  test_files: list[str], languages: dict[str, int],
                  deadline: float | None) -> dict[str, Any]:
    cache_path = state_path(root, "tags.json")
    cache_rel = _display_path(root, cache_path)
    base: dict[str, Any] = {
        "available": False,
        "source": "none",
        "cache_path": cache_rel,
        "reason": None,
        "hubs": [],
        "cycles": [],
        "coupling": None,
        "modules": None,
        "test_source_ratio": None,
        "dead_code_candidates": None,
        "note": None,
    }
    if not cache_path.is_file():
        base["reason"] = ("no repo-map tag cache; run `ozagia index` or a "
                          "repo-map command to populate it")
        base["file_level"] = _file_level(source_files, test_files, languages,
                                         cfg)
        return base
    if deadline is not None and time.monotonic() > deadline:
        base["reason"] = "budget exhausted before the architecture pass"
        base["file_level"] = _file_level(source_files, test_files, languages,
                                         cfg)
        return base
    try:
        from ozagia.core.architecture import architecture_report
        report = architecture_report(root, cache_path=cache_path, top=10,
                                     update_cache=False)
    except Exception as exc:  # noqa: BLE001 - reported, never fatal
        base["reason"] = f"{type(exc).__name__}: {exc}"[:300]
        base["file_level"] = _file_level(source_files, test_files, languages,
                                         cfg)
        return base
    summary = report.get("summary") or {}
    base.update({
        "available": True,
        "source": "tag_cache",
        "files": summary.get("files"),
        "edges": summary.get("edges"),
        "hubs": (summary.get("hubs") or [])[:10],
        "cycles": (summary.get("cycles") or [])[:10],
        "coupling": summary.get("coupling"),
        "modules": summary.get("modules"),
        "test_source_ratio": summary.get("test_source_ratio"),
        "dead_code_candidates": len(summary.get("dead_code_candidates") or []),
        "note": ("read from the existing tag cache without refreshing it; "
                 "cache freshness is not verified"),
    })
    return base


def _file_level(source_files: list[str], test_files: list[str],
                languages: dict[str, int], cfg: _Config) -> dict[str, Any]:
    source = len(source_files)
    tests = len(test_files)
    return {
        "source_files": source,
        "test_files": tests,
        "doc_files": None,
        "languages": languages,
        "test_source_ratio": {
            "tests": tests,
            "source": source,
            "ratio": round(tests / source, 4) if source else None,
        },
        "note": ("file-level facts only; hubs, cycles and coupling need the "
                 "tag cache"),
    }


# --------------------------------------------------------------------------- #
# Assembly
# --------------------------------------------------------------------------- #


def _pyproject(root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    path = root / "pyproject.toml"
    meta: dict[str, Any] = {"path": "pyproject.toml", "present": path.is_file(),
                            "state": "missing", "error": None}
    if not path.is_file():
        return {}, meta
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        meta["state"] = "invalid"
        meta["error"] = f"{type(exc).__name__}: {exc}"[:300]
        return {}, meta
    meta["state"] = "read"
    return data, meta


def _snapshot_state(snapshot: dict[str, Any],
                    current: str | None) -> tuple[str, str]:
    if not snapshot.get("present"):
        return "missing", "no graph snapshot"
    snap_head = snapshot.get("head")
    if not snap_head or not current:
        return "unknown", "head comparison unavailable"
    if snap_head == current:
        return "fresh", "snapshot head matches .git/HEAD"
    return "stale", "snapshot head differs from .git/HEAD"


def _identity(root: Path, pyproject: dict[str, Any], walk: dict[str, Any],
              modules: dict[str, Any], tests: dict[str, Any],
              docs: dict[str, Any]) -> dict[str, Any]:
    project = pyproject.get("project") if isinstance(pyproject, dict) else None
    if not isinstance(project, dict):
        project = {}
    name = root.name
    version = project.get("version")
    description = project.get("description")
    license_value = project.get("license")
    if isinstance(license_value, dict):
        license_value = license_value.get("text") or license_value.get("file")
    if not license_value:
        for candidate in ("LICENSE", "LICENSE.md", "COPYING"):
            if (root / candidate).is_file():
                first = (_read_text(root, candidate, 500) or "").strip().splitlines()
                license_value = _one_line(first[0], 80) if first else candidate
                break
    languages: dict[str, int] = {}
    for rel in walk["files"]:
        language = _language(rel)
        if language != "other":
            languages[language] = languages.get(language, 0) + 1
    counts = {
        "files": len(walk["files"]),
        "source_files": len(modules["source_files"]),
        "test_files": len(modules["test_files"]),
        "doc_files": docs.get("total_docs", len(docs.get("index") or [])),
        "symbols": modules["analysis"].get("symbols", 0),
        "tests": tests["analysis"].get("test_functions", 0),
        "modules": modules["analysis"].get("total_modules", 0),
        "top_level_dirs": len({rel.split("/", 1)[0] for rel in walk["files"]
                               if "/" in rel}),
    }
    return {
        "name": _redact(name),
        "package": _redact(str(project.get("name") or "")) or None,
        "version": str(version) if version else None,
        "description": _redact(str(description)) if description else None,
        "license": _redact(str(license_value)) if license_value else None,
        "languages": [{"language": language, "files": count}
                      for language, count in sorted(languages.items())],
        "counts": counts,
        "counts_source": "walk+ast",
        "unknown": [label for label, value in (
            ("package name", project.get("name")),
            ("package version", version),
            ("description", description),
            ("license", license_value),
        ) if not value],
    }


def build_shape(root: str | Path = ".", *,
                budget_s: float = DEFAULT_BUDGET_S,
                max_modules: int = DEFAULT_MAX_MODULES,
                exclude: Iterable[str | Path] | None = None) -> dict[str, Any]:
    """Build the deterministic repository-shape report (read-only).

    ``exclude`` names paths the report describes but must not count (the CLI
    passes an explicit ``--out``/``--check`` file so the generated artifact is
    idempotent).
    """
    root_path = Path(root).expanduser().resolve()
    if not root_path.is_dir():
        raise ValueError(f"not a directory: {root}")
    budget_s = max(0.0, float(budget_s))
    max_modules = max(1, min(int(max_modules), 500))
    start = time.monotonic()
    deadline = start + budget_s if budget_s else None
    cfg = _Config(root_path)
    walk = _walk(root_path, cfg, deadline, MAX_WALK_FILES)
    excluded: set[str] = set()
    for item in exclude or ():
        path = Path(item)
        if not path.is_absolute():
            path = root_path / path
        try:
            excluded.add(path.resolve().relative_to(root_path).as_posix())
        except (OSError, ValueError):
            continue
    if excluded:
        walk = {**walk,
                "files": [rel for rel in walk["files"] if rel not in excluded]}

    test_files = [rel for rel in walk["files"]
                  if _test_file(rel, cfg.test_dirs)]
    doc_files = [rel for rel in walk["files"] if _doc_file(rel)]
    source_files = [
        rel for rel in walk["files"]
        if _code_file(rel) and rel not in set(test_files)
        and not _doc_file(rel)
        and any(_under_prefix(rel, prefix) for prefix in cfg.source_dirs)
    ]
    source_files.sort()
    test_files.sort()
    grammar = {
        "source_files": source_files,
        "test_files": test_files,
        "doc_files": doc_files,
        "analysis": {},
    }
    analysis = _analyze_modules(root_path, source_files, cfg, deadline,
                                max_modules)
    grammar["analysis"] = analysis

    tests_analysis = _analyze_tests(root_path, test_files, deadline)
    tests_analysis["link_source"] = "filename+import"
    test_summary = {
        "files": test_files[:50],
        "count": len(test_files),
        "analysis": tests_analysis,
    }

    graph, snapshot = _load_graph(root_path)
    head = _git_head(root_path)
    snapshot["state"], snapshot["reason"] = _snapshot_state(
        snapshot, head.get("commit"))
    graph_links = _graph_test_links(graph)
    # The committed reference must be stable across commits and local graph
    # state: graph-derived links are observation only, never part of the
    # stable test mapping or the content hash.
    mapping = _map_tests(test_files, source_files, cfg.source_dirs,
                         {}, tests_analysis["imports"])
    test_summary.update(mapping)
    test_summary["truncated"] = (test_summary["truncated"]
                                 or len(test_files) > 50)

    docs = _documentation(root_path, walk, cfg, deadline, excluded)
    commands = _commands(root_path, cfg)
    pyproject, pyproject_meta = _pyproject(root_path)
    architecture = _architecture(root_path, cfg, source_files, test_files,
                                 {item["language"]: item["files"]
                                  for item in _identity_languages(walk)},
                                 deadline)
    if architecture.get("file_level"):
        architecture["file_level"]["doc_files"] = len(doc_files)

    project = pyproject.get("project") if isinstance(pyproject, dict) else {}
    if not isinstance(project, dict):
        project = {}
    scripts, entry_groups = _entry_points(project)
    main_modules = sorted(rel for rel in source_files
                          if rel.rsplit("/", 1)[-1] == "__main__.py")[:20]
    cli_commands = analysis.get("cli_commands") or []
    known_commands = sorted({row["command"] for row in cli_commands})
    groups = analysis.get("groups") or []

    layout = _layout(walk, source_files, test_files, doc_files, cfg)
    report: dict[str, Any] = {
        "schema": SCHEMA,
        "root_name": _redact(root_path.name),
        "identity": _identity(root_path, pyproject, walk, grammar, test_summary,
                              docs),
        "source": {
            "readme": docs["readme"],
            "package": {
                **pyproject_meta,
                "name": project.get("name"),
                "version": project.get("version"),
                "description": (_redact(str(project.get("description")))
                                if project.get("description") else None),
                "license": (_redact(str(project.get("license")))
                            if project.get("license") else None),
                "requires_python": project.get("requires-python"),
                "dependencies": _bounded_list(project.get("dependencies")),
                "optional_groups": sorted(
                    (project.get("optional-dependencies") or {}).keys())
                if isinstance(project.get("optional-dependencies"), dict) else [],
            },
            "unknown": (docs["readme"].get("unknown") or [])
            + (["pyproject.toml"] if pyproject_meta["state"] != "read" else []),
        },
        "layout": layout,
        "entry_points": {
            "scripts": scripts,
            "script_groups": entry_groups,
            "main_modules": [{"path": rel, "name": "__main__"} for rel in main_modules],
            "cli_commands": cli_commands,
            "command_names": known_commands[:MAX_ENTRY_COMMANDS],
            "truncated": (len(cli_commands) >= MAX_ENTRY_COMMANDS
                          or len(known_commands) > MAX_ENTRY_COMMANDS),
            "source": "pyproject.toml + Python AST; no module is imported",
        },
        "modules": {
            "groups": groups,
            "packages": analysis["packages"],
            "shown": analysis["shown_modules"],
            "total": analysis["total_modules"],
            "max_modules": analysis["max_modules"],
            "truncated": analysis["truncated"],
            "doc_source": "Python AST module docstrings; leading comments for other languages",
            "parse_failures": analysis["parse_failures"],
            "read_failures": analysis["read_failures"],
        },
        "documentation": docs,
        "architecture": architecture,
        "tests": test_summary,
        "commands": commands,
        "conventions": _conventions(root_path, pyproject),
        "coverage": {
            "files_walked": len(walk["files"]),
            "unknowns": _stable_unknowns(docs, analysis),
            "truncated": [entry for entry in (
                {"section": "walk", "reason": "file cap or budget",
                 "truncated": walk["truncated"]},
                {"section": "modules", "reason": "max_modules",
                 "truncated": analysis["truncated"]},
                {"section": "documentation", "reason": "doc cap",
                 "truncated": docs["truncated"]},
                {"section": "tests", "reason": "row cap",
                 "truncated": test_summary["truncated"]},
                {"section": "commands", "reason": "per-kind cap",
                 "truncated": commands["truncated"]},
            ) if entry["truncated"]],
        },
        "environment": {
            "label": "Generation environment (not checked)",
            "note": ("Informational only. Git HEAD, the local graph snapshot, "
                     "the tag cache and generation time are excluded from "
                     "content_hash and from `--check`."),
            "configuration_error": cfg.error,
            "git": head,
            "snapshot": snapshot,
            "tag_cache": {
                "path": architecture.get("cache_path"),
                "available": bool(architecture.get("available")),
                "reason": architecture.get("reason"),
            },
            "files_read": (analysis.get("analyzed_files", 0)
                           + tests_analysis.get("analyzed_files", 0)
                           + docs.get("coverage", {}).get("docs_scanned", 0)),
            "budget_exceeded": bool(walk["budget_exceeded"]
                                    or analysis["budget_exceeded"]),
            "sources": {
                "readme": docs["readme"].get("state"),
                "pyproject": pyproject_meta["state"],
                "configuration": "error" if cfg.error else "read",
                "graph_snapshot": snapshot.get("state"),
                "tag_cache": ("read" if architecture["available"]
                              else "unavailable"),
                "git": head.get("state"),
            },
            "test_links": {
                "graph_edges_used": bool(graph_links),
                "graph_linked_test_files": len(graph_links),
                "note": ("graph-derived links are observation, not part of "
                         "the stable test mapping"),
            },
            "unknowns": _environment_unknowns(snapshot, architecture),
        },
    }
    report["content_hash"] = _content_hash(stable_projection(report))
    return report


def _identity_languages(walk: dict[str, Any]) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    for rel in walk["files"]:
        language = _language(rel)
        if language != "other":
            counts[language] = counts.get(language, 0) + 1
    return [{"language": key, "files": value}
            for key, value in sorted(counts.items())]


def _bounded_list(value: Any, limit: int = 20) -> list[str]:
    if not isinstance(value, list):
        return []
    return [_redact(str(item))[:200] for item in value[:limit]]


def _entry_points(project: dict[str, Any]) -> tuple[list[dict[str, Any]],
                                                    list[dict[str, Any]]]:
    scripts: list[dict[str, Any]] = []
    tables = [project.get("scripts")]
    entry_points = project.get("entry-points")
    if isinstance(entry_points, dict):
        tables.append(entry_points.get("console_scripts"))
    for table in tables:
        if not isinstance(table, dict):
            continue
        for name, target in sorted(table.items()):
            if not isinstance(target, str):
                continue
            module, _, attr = target.partition(":")
            scripts.append({"name": str(name), "target": _redact(target),
                            "module": module, "attr": attr or None,
                            "kind": "console_script"})
    groups: list[dict[str, Any]] = []
    if isinstance(entry_points, dict):
        for group, values in sorted(entry_points.items()):
            if isinstance(values, dict):
                groups.append({"group": str(group), "entries": len(values),
                               "names": sorted(str(name) for name in values)[:10]})
    return scripts, groups


def _layout(walk: dict[str, Any], source_files: list[str], test_files: list[str],
            doc_files: list[str], cfg: _Config) -> dict[str, Any]:
    source = set(source_files)
    tests = set(test_files)
    docs = set(doc_files)
    top: dict[str, dict[str, Any]] = {}
    for rel in walk["files"]:
        parts = rel.split("/")
        name = parts[0] if len(parts) > 1 else "(root)"
        row = top.setdefault(name, {"path": name, "files": 0, "code": 0,
                                    "tests": 0, "docs": 0})
        row["files"] += 1
        if rel in tests:
            row["tests"] += 1
        elif rel in docs:
            row["docs"] += 1
        elif rel in source:
            row["code"] += 1
    rows = sorted(top.values(),
                  key=lambda item: (-item["files"], item["path"]))
    # `walk["files"]` holds repo-relative paths, so membership is the check.
    key_files = [name for name in KEY_FILE_NAMES if name in walk["files"]]
    return {
        "top_level": rows[:40],
        "key_files": key_files,
        "source_dirs": list(cfg.source_dirs),
        "test_dirs": list(cfg.test_dirs),
        "skipped_dirs": sorted(cfg.skip_dirs)[:30],
        "walked_files": len(walk["files"]),
        "truncated": walk["truncated"] or len(rows) > 40,
    }


def _stable_unknowns(docs: dict[str, Any],
                     analysis: dict[str, Any]) -> list[str]:
    unknown: list[str] = []
    if docs.get("readme", {}).get("state") != "read":
        unknown.append("README prose unavailable")
    if analysis.get("parse_failures"):
        unknown.append(f"{len(analysis['parse_failures'])} Python module(s) "
                       "could not be parsed")
    return unknown


def _environment_unknowns(snapshot: dict[str, Any],
                          architecture: dict[str, Any]) -> list[str]:
    unknown: list[str] = []
    if not snapshot.get("present"):
        unknown.append("no graph snapshot: recorded counts and test links are absent")
    elif snapshot.get("state") != "fresh":
        unknown.append(f"graph snapshot is {snapshot.get('state')}")
    if not architecture.get("available"):
        unknown.append("architecture hubs/cycles/coupling unavailable: "
                       + (architecture.get("reason") or "no tag cache"))
    unknown.append("working-tree changes are not compared with the snapshot")
    return unknown


# --------------------------------------------------------------------------- #
# Renderers
# --------------------------------------------------------------------------- #


def _cell(value: Any, limit: int = 160) -> str:
    text = "-" if value is None or value == "" else str(value)
    text = _redact(text).replace("|", "\\|").replace("\n", " ")
    return text[:limit] + ("..." if len(text) > limit else "")


def _link(path: str | None, label: str | None = None) -> str:
    if not path:
        return "unknown"
    label = label or path
    return f"[{_cell(label)}]({path})"


def render_markdown(shape: dict[str, Any]) -> str:
    """Render one readable reference page from ``build_shape``."""
    identity = shape.get("identity") or {}
    source = shape.get("source") or {}
    layout = shape.get("layout") or {}
    entry = shape.get("entry_points") or {}
    modules = shape.get("modules") or {}
    docs = shape.get("documentation") or {}
    arch = shape.get("architecture") or {}
    tests = shape.get("tests") or {}
    commands = shape.get("commands") or {}
    coverage = shape.get("coverage") or {}
    environment = shape.get("environment") or {}
    counts = identity.get("counts") or {}
    languages = ", ".join(
        f"{item['language']} ({item['files']})"
        for item in identity.get("languages") or []) or "unknown"

    lines: list[str] = [f"# {_cell(identity.get('name') or shape.get('root_name'))}"
                        " — repository shape", ""]
    lines.append("> Generated by `ozagia overview` from the checkout. "
                 "Read-only; no target code is executed. The stable content "
                 "hash covers the checkout/source shape only.")
    lines.append("")
    lines.append(f"Stable content hash: `{shape.get('content_hash')}`")
    lines.append("")

    lines.extend(["## Identity", "",
                  "| Field | Value |", "| --- | --- |"])
    rows = (
        ("Repository", identity.get("name")),
        ("Package", identity.get("package")),
        ("Version", identity.get("version")),
        ("Description", identity.get("description")),
        ("License", identity.get("license")),
        ("Languages", languages),
        ("Files", counts.get("files")),
        ("Source files", counts.get("source_files")),
        ("Test files", counts.get("test_files")),
        ("Doc files", counts.get("doc_files")),
        ("Modules", counts.get("modules")),
        ("Symbols (AST)", counts.get("symbols")),
        ("Tests (AST)", counts.get("tests")),
    )
    for label, value in rows:
        lines.append(f"| {label} | {_cell(value)} |")
    if identity.get("unknown"):
        lines.append("")
        lines.append("Unknown: " + "; ".join(_cell(item, 200)
                                               for item in identity["unknown"]))
    lines.append("")
    lines.append("These are checkout/source facts and are covered by the stable "
                 "content hash. Git HEAD, the local graph snapshot and generation "
                 "time are separate observations (see the environment section).")
    lines.append("")
    lines.append("Sources: `README.md`, `pyproject.toml`, a bounded file walk and "
                 "the Python AST; no target import or execution.")
    lines.append("")

    lines.extend(["## What this is", ""])
    readme = source.get("readme") or {}
    if readme.get("title"):
        lines.append(f"**{_cell(readme['title'], 200)}**")
        lines.append("")
    lines.append(readme.get("prose") or "_No README prose was read._")
    lines.append("")
    if readme.get("headings"):
        lines.append("README sections: " + ", ".join(
            f"`{_cell(item, 80)}`" for item in readme["headings"]) + ".")
        lines.append("")
    if readme.get("highlights"):
        lines.append("README highlights:")
        lines.append("")
        for item in readme["highlights"]:
            lines.append(f"- {item}")
        lines.append("")
    package = source.get("package") or {}
    if package.get("state") == "read":
        description = (package.get("description") or "").rstrip(".")
        lines.append(
            f"Package metadata: `{_cell(package.get('name'))}` "
            f"{_cell(package.get('version'))} — {_cell(description)}. "
            f"Python: `{_cell(package.get('requires_python'))}`; "
            f"dependencies: {_cell(', '.join(package.get('dependencies') or []) or 'none declared')}.")
    else:
        lines.append(f"Package metadata: **{_cell(package.get('state'))}** "
                     f"({_cell(package.get('error'))}).")
    if source.get("unknown"):
        lines.append("")
        lines.append("Unknown/not read: "
                     + "; ".join(_cell(item, 160) for item in source["unknown"]))
    lines.append("")
    lines.append(f"Source: {_link(readme.get('path'))}, "
                 f"{_link(package.get('path'))}.")
    lines.append("")

    start_commands = (commands.get("run") or [])[:4]
    if start_commands:
        lines.extend(["## Start here", ""])
        for row in start_commands:
            lines.append(f"- `{_cell(row['command'], 220)}` "
                         f"(from {_link(row['source'])})")
        lines.append("")

    lines.extend(["## Layout", "",
                  "| Path | Files | Code | Tests | Docs |",
                  "| --- | ---: | ---: | ---: | ---: |"])
    for row in layout.get("top_level") or []:
        lines.append(f"| {_cell(row.get('path'))} | {row.get('files', 0)} | "
                     f"{row.get('code', 0)} | {row.get('tests', 0)} | "
                     f"{row.get('docs', 0)} |")
    lines.append("")
    if layout.get("key_files"):
        lines.append("Key files: " + ", ".join(_link(name)
                                               for name in layout["key_files"]) + ".")
        lines.append("")
    if modules.get("packages"):
        lines.extend(["### Packages", "",
                      "| Package | Modules | Responsibility |",
                      "| --- | ---: | --- |"])
        for row in modules["packages"][:20]:
            lines.append(f"| {_link(row['path'])} | {row.get('modules', 0)} | "
                         f"{_cell(row.get('responsibility'))} |")
        lines.append("")
    lines.append("Sources: bounded checkout walk using the project's source, "
                 "test and skip rules; hidden and skipped directories are not entered.")
    lines.append("")

    lines.extend(["## Entry points", ""])
    scripts = entry.get("scripts") or []
    if scripts:
        lines.extend(["| Command | Target |", "| --- | --- |"])
        for row in scripts[:40]:
            lines.append(f"| `{_cell(row.get('name'))}` | `{_cell(row.get('target'))}` |")
        if len(scripts) > 40:
            lines.append(f"\n({len(scripts) - 40} more console scripts not shown.)")
    else:
        lines.append("**unknown/no console scripts** in `pyproject.toml`.")
    lines.append("")
    mains = entry.get("main_modules") or []
    if mains:
        lines.append("`__main__` modules: " + ", ".join(
            _link(row["path"]) for row in mains) + ".")
        lines.append("")
    names = entry.get("command_names") or []
    if names:
        shown = ", ".join(f"`{_cell(name, 40)}`"
                          for name in names[:MAX_MARKDOWN_COMMANDS])
        more = (f" (+{len(names) - MAX_MARKDOWN_COMMANDS} more)"
                if len(names) > MAX_MARKDOWN_COMMANDS else "")
        lines.append(f"CLI commands found by static AST (`add_parser`): {shown}{more}.")
        lines.append("")
    lines.append("Source: `[project.scripts]` / `[project.entry-points]` in "
                 "`pyproject.toml` and Python AST; no module is imported.")
    lines.append("")

    lines.extend(["## Modules", ""])
    groups = modules.get("groups") or []
    if groups:
        lines.append(f"Showing {modules.get('shown')} of {modules.get('total')} "
                     f"modules (max {modules.get('max_modules')}).")
        lines.append("")
        for group in groups:
            lines.extend([f"### {group['name'].title()}", "",
                          "| Module | Responsibility | Symbols |",
                          "| --- | --- | ---: |"])
            for module in group["modules"]:
                symbols = "-" if module.get("symbols") is None else module["symbols"]
                lines.append(f"| {_link(module['path'])} | "
                             f"{_cell(module.get('responsibility'))} | {symbols} |")
            lines.append("")
    else:
        lines.append("_No Python or supported source modules were found._")
        lines.append("")
    lines.append("Responsibility is the first module-docstring line (Python AST) "
                 "or a leading comment for other languages; `(no docstring)` is "
                 "a fact, not a guess.")
    if modules.get("truncated"):
        lines.append("")
        lines.append(f"Truncated at {modules.get('max_modules')} modules.")
    if modules.get("parse_failures"):
        lines.append("")
        lines.append("Unparsed Python: " + ", ".join(
            _cell(item, 120) for item in modules["parse_failures"]))
    lines.append("")

    lines.extend(["## Architecture (observed; not checked)", ""])
    lines.append("This section depends on the local tag cache. It is shown for "
                 "orientation but excluded from the stable content hash and "
                 "from `--check`.")
    lines.append("")
    if arch.get("available"):
        lines.append("Read from the existing repo-map tag cache (not refreshed by "
                     "this command).")
        lines.append("")
        hubs = arch.get("hubs") or []
        if hubs:
            lines.extend(["| Rank | File | PageRank | In | Out |",
                          "| ---: | --- | ---: | ---: | ---: |"])
            for hub in hubs:
                lines.append(f"| {hub.get('rank')} | {_link(hub.get('path'))} | "
                             f"{hub.get('pagerank')} | {hub.get('in_degree')} | "
                             f"{hub.get('out_degree')} |")
            lines.append("")
        cycles = arch.get("cycles") or []
        if cycles:
            lines.append("Reference cycles (Tarjan SCC): " + "; ".join(
                f"{cycle.get('size')} files: " + ", ".join(
                    _cell(item, 80) for item in (cycle.get("files") or [])[:5])
                for cycle in cycles[:6]) + ".")
        else:
            lines.append("No multi-file reference cycles were found in the cache.")
        coupling = arch.get("coupling") or {}
        if coupling:
            lines.append("")
            lines.append(f"Coupling: {coupling.get('files_with_inbound', 0)} files "
                         f"have inbound references; max in/out degree "
                         f"{coupling.get('max_in_degree', 0)}/"
                         f"{coupling.get('max_out_degree', 0)}.")
        ratio = arch.get("test_source_ratio") or {}
        if ratio:
            lines.append(f"Test/source ratio: {ratio.get('tests')} test files / "
                         f"{ratio.get('source')} source files = {ratio.get('ratio')}.")
        if arch.get("dead_code_candidates") is not None:
            lines.append(f"Dead-code candidates (heuristic, not proof): "
                         f"{arch.get('dead_code_candidates')}.")
    else:
        lines.append(f"**Unavailable.** {_cell(arch.get('reason'))}.")
        lines.append("")
        fallback = arch.get("file_level") or {}
        if fallback:
            lines.append(f"File-level fallback: {fallback.get('source_files')} "
                         f"source files, {fallback.get('test_files')} test files, "
                         f"ratio {((fallback.get('test_source_ratio') or {}).get('ratio'))}.")
    lines.append("")
    lines.append("Source: `core.architecture.architecture_report` over the tag "
                 "cache when it exists; otherwise file-level facts only.")
    lines.append("")

    lines.extend(["## Tests", "",
                  f"- Test files: {tests.get('count')}",
                  f"- Linked to source: {tests.get('mapped_count')}",
                  f"- Without a source link: {tests.get('unmapped_count')}",
                  f"- Link source: {tests.get('analysis', {}).get('link_source', 'filename')}"])
    if tests.get("mapped"):
        lines.extend(["", "| Test | Source(s) | Basis |", "| --- | --- | --- |"])
        for row in tests["mapped"]:
            lines.append(f"| {_link(row.get('test'))} | " + ", ".join(
                _link(item) for item in row.get("sources") or []) +
                f" | {_cell(', '.join(row.get('basis') or []))} |")
    if tests.get("unmapped"):
        lines.extend(["", "Unmapped examples: " + ", ".join(
            _link(item) for item in tests["unmapped"][:8])])
    lines.append("")
    lines.append("The stable mapping uses filename and import signals only. "
                 "Graph-derived links are observed at generation and excluded "
                 "from the stable content hash and `--check`; a link is a "
                 "candidate, not proof of coverage.")
    lines.append("")

    lines.extend(["## Documentation map", ""])
    if readme.get("path"):
        lines.append(f"README: {_link(readme.get('path'))} — "
                     f"{_cell(readme.get('title'))}.")
        lines.append("")
    canonical = docs.get("canonical") or {}
    lines.append(f"Canonical: roadmap {_link(canonical.get('roadmap'))}; "
                 f"plans {canonical.get('plans')} (docs/plans); "
                 f"results {canonical.get('results')} (docs/results); "
                 f"research {canonical.get('research')} (docs/research).")
    lines.append("")
    index = docs.get("index") or []
    if index:
        lines.extend(["| Doc | Title | Kind |",
                      "| --- | --- | --- |"])
        for row in index[:60]:
            lines.append(f"| {_link(row.get('path'))} | {_cell(row.get('title'))} | "
                         f"{_cell(row.get('kind'))} |")
        if len(index) > 60:
            lines.append(f"\n({len(index) - 60} more documents indexed but not shown.)")
    else:
        lines.append("_No documentation files were read._")
    cov = docs.get("coverage") or {}
    lines.append("")
    lines.append(f"Link/path coverage: {cov.get('docs_scanned')} docs scanned; "
                 f"{cov.get('links_to_existing_files')}/{cov.get('links_checked')} "
                 f"relative links resolve to files; "
                 f"{cov.get('code_paths_found')}/{cov.get('code_path_mentions')} "
                 f"path-like tokens name existing files.")
    if cov.get("missing_examples"):
        lines.append("")
        lines.append("Missing-link examples: " + "; ".join(
            f"{_link(item['doc'])} -> `{_cell(item['target'])}`"
            for item in cov["missing_examples"][:5]) + ".")
    lines.append("")
    lines.append("Source: bounded reads of `docs/` and the README; only titles, "
                 "links and path mentions are used, never full document text.")
    lines.append("")

    lines.extend(["## Commands and conventions", ""])
    rows = []
    for kind in ("build", "test", "lint", "run", "other"):
        for row in commands.get(kind) or []:
            rows.append((kind, row))
    if rows:
        lines.extend(["| Kind | Command | Source |", "| --- | --- | --- |"])
        for kind, row in rows:
            lines.append(f"| {kind} | `{_cell(row.get('command'), 220)}` | "
                         f"{_link(row.get('source'))} |")
    else:
        lines.append("_No commands were found in the configured documentation._")
    for note in shape.get("conventions") or []:
        lines.append(f"- {_cell(note.get('text'))} "
                     f"({_link(note.get('source'))})")
    lines.append("")
    lines.append("Commands are extracted from Markdown fences and never executed. "
                 "A command that is not listed here is unknown, not absent.")
    lines.append("")

    lines.extend(["## Coverage, unknowns and truncation", ""])
    lines.append(f"- Files walked: {coverage.get('files_walked')}.")
    for unknown in coverage.get("unknowns") or []:
        lines.append(f"- Unknown: {_cell(unknown, 240)}.")
    for entry in coverage.get("truncated") or []:
        lines.append(f"- Truncated: {entry.get('section')} ({entry.get('reason')}).")
    lines.append("- Environment/snapshot observations are listed separately "
                 "and are not part of the stable hash.")
    lines.append("")

    git = environment.get("git") or {}
    snapshot = environment.get("snapshot") or {}
    tag_cache = environment.get("tag_cache") or {}
    sources = environment.get("sources") or {}
    test_links = environment.get("test_links") or {}
    lines.extend(["## Generation environment (observed; not checked)", "",
                  f"> {_cell(environment.get('note') or 'Informational only.')}"])
    lines.append("")
    lines.append(f"- Git: head `{_cell(git.get('commit'))}`; "
                 f"ref `{_cell(git.get('ref'))}`; state {_cell(git.get('state'))}.")
    lines.append(f"- Snapshot: {_cell(snapshot.get('state'))}; "
                 f"head `{_cell(snapshot.get('head'))}`; "
                 f"generated at `{_cell(snapshot.get('generated_at'))}`; "
                 f"path `{_cell(snapshot.get('path'))}`.")
    counts = snapshot.get("counts") or {}
    if any(value is not None for value in counts.values()):
        lines.append("- Snapshot counts: " + ", ".join(
            f"{key}={_cell(value)}" for key, value in sorted(counts.items())) + ".")
    lines.append(f"- Tag cache: `{_cell(tag_cache.get('path'))}`; "
                 f"available={_cell(tag_cache.get('available'))}"
                 + (f"; {_cell(tag_cache.get('reason'))}."
                    if tag_cache.get("reason") else "."))
    lines.append(f"- Files read: {environment.get('files_read')}; "
                 f"budget exhausted: {_cell(environment.get('budget_exceeded'))}.")
    if environment.get("configuration_error"):
        lines.append("- Project configuration: "
                     f"{_cell(environment['configuration_error'])}.")
    if sources:
        lines.append("- Source states: " + ", ".join(
            f"{key}={value}" for key, value in sorted(sources.items())) + ".")
    lines.append(f"- Observed graph test links: {_cell(test_links.get('graph_edges_used'))}; "
                 f"test files with graph links: "
                 f"{_cell(test_links.get('graph_linked_test_files'))}.")
    for unknown in environment.get("unknowns") or []:
        lines.append(f"- Observed unknown: {_cell(unknown, 240)}.")
    lines.append("")
    lines.append(f"Stable content hash: `{shape.get('content_hash')}`. "
                 "The environment facts above are informational and excluded "
                 "from `--check`.")
    lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_json(shape: dict[str, Any]) -> str:
    """Stable, key-sorted JSON (one trailing newline)."""
    return json.dumps(shape, indent=2, sort_keys=True,
                      ensure_ascii=False) + "\n"


def trim_markdown(text: str, budget_tokens: int) -> tuple[str, bool]:
    """Bound a Markdown view for the MCP transport; tokens are estimated."""
    budget_tokens = max(1, int(budget_tokens))
    limit = max(400, budget_tokens * 4)
    if len(text) <= limit:
        return text, False
    cut = text[:limit].rsplit("\n", 1)[0]
    return (cut + "\n\n... truncated at the requested token budget ...\n", True)


def limit_json(shape: dict[str, Any], budget_tokens: int
               ) -> tuple[str, bool]:
    """Bound a JSON view by row caps, then by an explicit section projection.

    The JSON stays valid and keeps ``content_hash`` for the stable checkout/
    source projection. A ``view`` block says what was dropped; consumers that
    need everything should read ``REPO_SHAPE.md`` or the CLI's unbounded
    ``--json``.
    """
    full = render_json(shape)
    limit = max(2_000, max(1, int(budget_tokens)) * 4)
    if len(full) <= limit:
        return full, False
    view = json.loads(full)
    modules = view.setdefault("modules", {})
    for group in modules.get("groups") or []:
        group["modules"] = (group.get("modules") or [])[:3]
    modules["truncated"] = True
    documentation = view.setdefault("documentation", {})
    documentation["index"] = (documentation.get("index") or [])[:20]
    documentation["truncated"] = True
    tests = view.setdefault("tests", {})
    tests["mapped"] = (tests.get("mapped") or [])[:8]
    tests["unmapped"] = (tests.get("unmapped") or [])[:8]
    tests["truncated"] = True
    coverage = view.setdefault("coverage", {})
    coverage["truncated"] = (coverage.get("truncated") or []) + [
        {"section": "json view", "reason": "mcp budget_tokens",
         "truncated": True}]
    view["view"] = {
        "truncated": True,
        "budget_tokens": int(budget_tokens),
        "note": ("row lists were shortened to fit the MCP budget; "
                 "content_hash still identifies the stable projection"),
    }
    compact = render_json(view)
    if len(compact) <= limit:
        return compact, True
    essential_keys = (
        "schema", "root_name", "identity", "source", "layout", "entry_points",
        "architecture", "commands", "conventions", "coverage", "environment",
        "content_hash",
    )
    essential = {key: view[key] for key in essential_keys if key in view}
    essential["modules"] = {
        "shown": 0,
        "total": modules.get("total"),
        "truncated": True,
        "note": "omitted by the MCP JSON budget",
    }
    essential["documentation"] = {
        "readme": documentation.get("readme"),
        "canonical": documentation.get("canonical"),
        "total_docs": documentation.get("total_docs"),
        "truncated": True,
        "note": "document index omitted by the MCP JSON budget",
    }
    essential["tests"] = {
        "count": tests.get("count"),
        "mapped_count": tests.get("mapped_count"),
        "unmapped_count": tests.get("unmapped_count"),
        "truncated": True,
        "note": "test rows omitted by the MCP JSON budget",
    }
    essential["view"] = view["view"]
    essential["view"]["projection"] = "essential sections only"
    for key in ("documentation", "tests", "architecture", "layout", "commands",
                "conventions", "source", "coverage"):
        if len(render_json(essential)) <= limit:
            break
        essential.pop(key, None)
    return render_json(essential), True


def write_atomic(path: str | Path, text: str) -> None:
    """Write explicitly requested output atomically (the feature's only write)."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=str(target.parent),
                                     prefix=target.name + ".",
                                     suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.replace(temporary, target)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def check_file(path: str | Path, shape: dict[str, Any],
               generated: str | None = None) -> dict[str, Any]:
    """Compare only the stable projection embedded in an existing output.

    Git HEAD, graph-snapshot state, tag-cache availability and generation time
    are environment observations. A reference whose stable hash matches is
    current even when those observations differ. ``generated`` is the freshly
    rendered text, used only to distinguish that informational difference from
    a byte-identical file.
    """
    target = Path(path)
    current_hash = shape.get("content_hash")
    base = {"path": str(path), "exists": False, "same": False,
            "stable_same": False, "volatile_only": False,
            "current_hash": current_hash, "file_hash": None}
    if not target.is_file():
        return {**base, "status": "missing", "reason": "file does not exist"}
    try:
        existing = target.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {**base, "exists": True, "status": "unreadable",
                "reason": f"{type(exc).__name__}: {exc}"}
    file_hash = None
    try:
        document = json.loads(existing)
    except ValueError:
        document = None
    if isinstance(document, dict):
        value = document.get("content_hash")
        if isinstance(value, str) and _HASH_RE.fullmatch(value):
            file_hash = value
    if file_hash is None:
        match = _HASH_RE.search(existing)
        file_hash = match.group(0) if match else None
    if file_hash is None:
        return {**base, "exists": True, "status": "no_hash",
                "reason": "no stable content hash was found in the file"}
    stable_same = file_hash == current_hash
    volatile_only = bool(stable_same and generated is not None
                         and existing.rstrip("\n") != generated.rstrip("\n"))
    if not stable_same:
        status = "drift"
        reason = "stable checkout/source shape differs"
    elif volatile_only:
        status = "environment_diff"
        reason = ("stable shape matches; local snapshot/HEAD, generation time "
                  "or rendered format differs, which is not drift")
    else:
        status = "current"
        reason = None
    return {**base, "exists": True, "same": stable_same,
            "stable_same": stable_same, "volatile_only": volatile_only,
            "file_hash": file_hash, "status": status, "reason": reason}


__all__ = [
    "DEFAULT_BUDGET_S",
    "DEFAULT_MAX_MODULES",
    "SCHEMA",
    "STABLE_KEYS",
    "build_shape",
    "check_file",
    "limit_json",
    "render_json",
    "render_markdown",
    "stable_projection",
    "trim_markdown",
    "write_atomic",
]
