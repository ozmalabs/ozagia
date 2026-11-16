"""Advisory repository scan: ranked development opportunities, never edits.

This is the first stage of plan item 15 ("scan repo and build modules"). It
loads an *existing* ``.ozagia/graph.json`` read-only, adds bounded git,
filesystem and state-directory reads, and ranks opportunities such as missing
tests, hotspots, duplicates, weak boundaries, orphaned files, stale notes,
generated clutter and unindexed languages.

It deliberately does not build, refresh or rewrite the graph. A missing
snapshot is reported as ``unavailable``; a stale, unreadable or unverified one
is reported as ``degraded``. Filesystem and configuration checks still run in
either case, and the report says which categories were not assessed.

Every opportunity carries evidence (path, line, graph key, command or metric),
an expected effect, effort (S/M/L), risk, confidence, a proposed module and an
action, plus what was not assessed. A scan is advice, not a verdict:
``truth_assessed`` is always false. Nothing is executed, deleted, sent or
written except an optional caller-requested report file in the CLI.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from ozagia.config import load_project_config, state_path, state_rel

SCHEMA = "ozagia.repo_scan.report.v1"
SCHEMA_VERSION = 1
ADVISORY = (
    "advisory only: this scan does not build, edit, run tests, call a model, "
    "use the network or claim correctness"
)

DEFAULT_TOP = 25
MAX_TOP = 200
DEFAULT_PER_CATEGORY = 10
MAX_PER_CATEGORY = 50
DEFAULT_SINCE_DAYS = 30
MAX_SINCE_DAYS = 3650

MAX_GRAPH_BYTES = 128 * 1024 * 1024
MAX_STATUS_ENTRIES = 1000
MAX_WALK_FILES = 5000
MAX_WALK_DEPTH = 12
MAX_CLUTTER_FILES = 5000
MAX_TEXT_BYTES = 256 * 1024
MAX_PAIR_FILES = 160
MAX_PAIR_RESULTS = 20
MAX_CANDIDATES = 300
MAX_REVIEW_FILES = 20
MAX_REVIEW_BYTES = 2 * 1024 * 1024
MAX_DOC_WALK_FILES = 500
MAX_HISTORY_FILES = 2000

GIT_TIMEOUT_S = 20

#: Extensions the built-in memory index parses today (see memory.build_index).
INDEXED_SUFFIXES = frozenset({".py", ".rs", ".lua"})

#: Source languages present in repositories but outside the built-in index.
SOURCE_LANGUAGES = {
    ".ts": "typescript",
    ".tsx": "tsx",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".go": "go",
    ".java": "java",
    ".kt": "kotlin",
    ".kts": "kotlin",
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
    ".swift": "swift",
    ".scala": "scala",
    ".ex": "elixir",
    ".exs": "elixir",
    ".erl": "erlang",
    ".clj": "clojure",
    ".dart": "dart",
    ".vue": "vue",
    ".svelte": "svelte",
}

CLUTTER_DIRS = frozenset({
    "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache",
    ".hypothesis", ".tox", ".nox", "node_modules", ".next", ".cache",
    ".parcel-cache", ".turbo", "htmlcov", "coverage", "target", "build",
    "dist", ".venv", "venv",
})

ENTRYPOINT_NAMES = frozenset({
    "__init__.py", "__main__.py", "main.py", "main.rs", "main.lua",
    "setup.py", "conftest.py", "wsgi.py", "asgi.py", "manage.py", "app.py",
})

TEST_EDGE_TYPES = ("tests_module", "mentions_file")
IMPORT_EDGE = "imports"

CATEGORIES: tuple[dict[str, str], ...] = (
    {
        "id": "missing_tests",
        "title": "Missing tests",
        "description": "Source files with no test linked by the graph, "
                       "prioritising changed and hotspot files.",
    },
    {
        "id": "hotspots",
        "title": "Hotspots",
        "description": "Files with large symbols, high import degree or "
                       "recent churn (syntactic proxies, not runtime data).",
    },
    {
        "id": "duplicate_modules",
        "title": "Duplicate modules",
        "description": "Identical or near-identical source files by content "
                       "hash or token shingles.",
    },
    {
        "id": "duplicate_docs",
        "title": "Duplicate Markdown",
        "description": "Identical or near-identical documents, including "
                       "notes under history/.",
    },
    {
        "id": "weak_boundaries",
        "title": "Weak boundaries",
        "description": "Files with unusually high fan-in or fan-out in the "
                       "import graph.",
    },
    {
        "id": "orphaned_files",
        "title": "Orphaned files",
        "description": "Source files no indexed file imports, plus created "
                       "files the ledger cannot tie to a reference.",
    },
    {
        "id": "stale_history",
        "title": "Stale history notes",
        "description": "Files under history/ older than the scan window.",
    },
    {
        "id": "generated_clutter",
        "title": "Generated clutter",
        "description": "Cache, build and environment directories with a "
                       "measured size; cleanup always needs confirmation.",
    },
    {
        "id": "unindexed_languages",
        "title": "Unindexed languages",
        "description": "Source files in languages the built-in graph does "
                       "not parse.",
    },
    {
        "id": "missing_adapters",
        "title": "Missing adapters",
        "description": "No project adapter despite unindexed languages, or "
                       "a configured adapter that contributed nothing.",
    },
    {
        "id": "test_policy",
        "title": "Test policy signals",
        "description": "Recorded failures/flaky tests and test-selection "
                       "policy observations.",
    },
    {
        "id": "code_review",
        "title": "Code review signals",
        "description": "Blocking or major findings from existing review "
                       "packets, plus known-failure records.",
    },
)
CATEGORY_IDS = tuple(item["id"] for item in CATEGORIES)
CATEGORY_RANK = {name: rank for rank, name in enumerate(CATEGORY_IDS)}
CATEGORY_TITLE = {item["id"]: item["title"] for item in CATEGORIES}

EFFORTS = ("S", "M", "L")
RISKS = ("low", "medium", "high")
CONFIDENCES = ("high", "medium", "low")
_CONFIDENCE_DOWN = {"high": "medium", "medium": "low", "low": "low"}


class RepoScanError(ValueError):
    """The scan request is invalid or an output cannot be written."""


# --------------------------------------------------------------------------- #
# small evidence and opportunity builders
# --------------------------------------------------------------------------- #
def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()


def _short(value: str, limit: int = 12) -> str:
    return _sha(value)[:limit]


def _file_ev(path: str, line: int | None = None,
             detail: str | None = None) -> dict[str, Any]:
    item: dict[str, Any] = {"kind": "file", "path": path}
    if line is not None:
        item["line"] = int(line)
    if detail:
        item["detail"] = str(detail)
    return item


def _key_ev(key: str, detail: str | None = None) -> dict[str, Any]:
    item: dict[str, Any] = {"kind": "graph_key", "key": key}
    if detail:
        item["detail"] = str(detail)
    return item


def _cmd_ev(command: str, detail: str | None = None) -> dict[str, Any]:
    item: dict[str, Any] = {"kind": "command", "command": command}
    if detail:
        item["detail"] = str(detail)
    return item


def _metric_ev(name: str, value: Any,
               detail: str | None = None) -> dict[str, Any]:
    item: dict[str, Any] = {"kind": "metric", "name": name, "value": value}
    if detail:
        item["detail"] = str(detail)
    return item


def _dedupe_evidence(items: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        text = json.dumps(dict(item), sort_keys=True)
        if text in seen:
            continue
        seen.add(text)
        out.append(dict(item))
    return out


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, int(value)))


def _confidence_down(level: str) -> str:
    return _CONFIDENCE_DOWN.get(level, "low")


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _node_key(node: Mapping[str, Any]) -> str | None:
    key = node.get("key")
    if isinstance(key, str) and key:
        return key
    attrs = node.get("attrs")
    if not isinstance(attrs, Mapping):
        attrs = {}
    typ = str(node.get("type") or "")
    if typ == "Repo":
        return "repo:"
    if typ == "File" and attrs.get("path"):
        return f"file:{attrs['path']}"
    if typ == "Doc" and attrs.get("path"):
        return f"doc:{attrs['path']}"
    if typ == "Symbol" and attrs.get("file") and attrs.get("qualname"):
        return f"sym:{attrs['file']}::{attrs['qualname']}"
    if typ == "Test" and attrs.get("file") and attrs.get("qualname"):
        return f"test:{attrs['file']}::{attrs['qualname']}"
    if typ == "Commit" and attrs.get("hash"):
        return f"commit:{attrs['hash']}"
    return None


def _join(parts: Sequence[str]) -> str:
    return "|".join(str(part) for part in parts)


# --------------------------------------------------------------------------- #
# git and filesystem reads (bounded, read-only)
# --------------------------------------------------------------------------- #
def _git(root: Path, args: Sequence[str], *,
         timeout: int = GIT_TIMEOUT_S) -> subprocess.CompletedProcess[str] | None:
    env = dict(os.environ)
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env.setdefault("LC_ALL", "C")
    try:
        return subprocess.run(
            ["git", "-C", str(root), *args], capture_output=True, text=True,
            errors="replace", timeout=timeout, env=env,
            stdin=subprocess.DEVNULL, check=False)
    except (OSError, subprocess.SubprocessError):
        return None


def _git_text(root: Path, args: Sequence[str], *,
              timeout: int = GIT_TIMEOUT_S) -> str | None:
    proc = _git(root, args, timeout=timeout)
    if proc is None or proc.returncode != 0:
        return None
    return proc.stdout.strip()


def _current_head(root: Path) -> str | None:
    return _git_text(root, ["rev-parse", "HEAD"])


def _skip_prefixes(cfg: Any, root: Path) -> tuple[str, ...]:
    prefixes = []
    if cfg is not None:
        prefixes.extend(str(item).rstrip("/") for item in cfg.changed_exclude)
    rel = state_rel(root)
    if rel:
        prefixes.append(rel)
    return tuple(prefixes)


def _status_paths(root: Path, cfg: Any,
                  warnings: list[str]) -> tuple[list[str], bool, bool]:
    """(changed paths, dirty, status_ok) from one bounded git status call."""
    proc = _git(root, ["status", "--porcelain", "-uall"])
    if proc is None or proc.returncode != 0:
        warnings.append("git status could not run; changed-file signals are "
                        "unavailable")
        return [], False, False
    exclude = _skip_prefixes(cfg, root)
    changed: list[str] = []
    lines = (proc.stdout or "").splitlines()
    for line in lines[:MAX_STATUS_ENTRIES]:
        if len(line) < 4:
            continue
        path = line[3:].strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        if any(path == prefix or path.startswith(prefix + "/")
               for prefix in exclude):
            continue
        changed.append(path)
    if len(lines) > MAX_STATUS_ENTRIES:
        warnings.append(
            f"git status reported more than {MAX_STATUS_ENTRIES} entries; "
            "changed-file signals are truncated")
    return sorted(set(changed)), bool(lines), True


def _not_skipped(rel: str, cfg: Any, root: Path) -> bool:
    parts = set(Path(rel).parts)
    skipped = set((".git", "__pycache__", "node_modules", ".venv", "venv"))
    if cfg is not None:
        skipped |= set(cfg.skip_dirs)
    if parts & skipped:
        return False
    rel_state = state_rel(root)
    return not (rel_state and (rel == rel_state
                               or rel.startswith(rel_state + "/")))


def _walk_files(root: Path, rel_dirs: Sequence[str], *,
                cfg: Any = None,
                suffixes: Iterable[str] | None = None,
                max_files: int = MAX_WALK_FILES,
                max_depth: int = MAX_WALK_DEPTH,
                warnings: list[str] | None = None) -> list[str]:
    """Bounded, sorted walk; symlinks are never followed."""
    wanted = {str(suffix).lower() for suffix in suffixes} if suffixes else None
    skip = set((".git", "__pycache__", "node_modules", ".venv", "venv"))
    if cfg is not None:
        skip |= set(cfg.skip_dirs)
    found: list[str] = []
    truncated = False
    for rel_dir in rel_dirs:
        base = root / rel_dir
        if not base.is_dir() or base.is_symlink():
            continue
        stack: list[tuple[Path, int]] = [(base, 0)]
        while stack:
            directory, depth = stack.pop()
            try:
                entries = _entries(directory)
            except OSError:
                continue
            for entry in entries:
                if len(found) >= max_files:
                    truncated = True
                    break
                try:
                    if entry.is_symlink():
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        if entry.name in skip or depth >= max_depth:
                            continue
                        stack.append((Path(entry.path), depth + 1))
                    elif entry.is_file(follow_symlinks=False):
                        suffix = Path(entry.name).suffix.lower()
                        if wanted is not None and suffix not in wanted:
                            continue
                        try:
                            rel = Path(entry.path).relative_to(root).as_posix()
                        except ValueError:
                            continue
                        if not _not_skipped(rel, cfg, root):
                            continue
                        found.append(rel)
                except OSError:
                    continue
            if truncated:
                break
        if truncated:
            break
    if truncated and warnings is not None:
        warnings.append(
            f"filesystem walk stopped at {max_files} files; filesystem-based "
            "categories are truncated")
    return sorted(set(found))


def _read_text(path: Path, limit: int = MAX_TEXT_BYTES) -> str | None:
    try:
        if path.stat().st_size > limit:
            return None
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None


def _entries(path: Path) -> list[os.DirEntry[str]]:
    """Sorted directory entries; an unreadable directory is empty."""
    try:
        with os.scandir(path) as handle:
            return sorted(handle, key=lambda entry: entry.name)
    except OSError:
        return []


def _dir_usage(path: Path, *, max_files: int = MAX_CLUTTER_FILES
               ) -> dict[str, Any]:
    """(bytes, files, truncated) for one tree; symlinks are not followed."""
    total = 0
    files = 0
    truncated = False
    stack = [path]
    while stack:
        directory = stack.pop()
        try:
            entries = _entries(directory)
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    stack.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    total += int(entry.stat(follow_symlinks=False).st_size)
                    files += 1
                    if files >= max_files:
                        truncated = True
                        break
            except OSError:
                continue
        if truncated:
            break
    return {"bytes": total, "files": files, "truncated": truncated}


def _measure_clutter(root: Path, warnings: list[str]) -> list[dict[str, Any]]:
    """Known generated/cache directories under the root, measured once each."""
    found: list[dict[str, Any]] = []
    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack:
        directory, depth = stack.pop()
        try:
            entries = _entries(directory)
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                    continue
                try:
                    rel = Path(entry.path).relative_to(root).as_posix()
                except ValueError:
                    continue
                if entry.name == ".git":
                    continue
                if rel == state_rel(root):
                    trash = Path(entry.path) / "trash"
                    if trash.is_dir():
                        found.append(
                            {"path": ".ozagia/trash", **_dir_usage(trash)})
                    continue
                if entry.name == ".ozagia":
                    continue
                if entry.name in CLUTTER_DIRS:
                    found.append({"path": rel, **_dir_usage(Path(entry.path))})
                    continue
                if depth < MAX_WALK_DEPTH:
                    stack.append((Path(entry.path), depth + 1))
            except OSError:
                continue
    coverage = root / ".coverage"
    if coverage.is_file():
        try:
            size = int(coverage.stat().st_size)
        except OSError:
            size = 0
        found.append({"path": ".coverage", "bytes": size, "files": 1,
                      "truncated": False})
    found.sort(key=lambda item: (-int(item["bytes"]), str(item["path"])))
    if len(found) > MAX_PER_CATEGORY:
        warnings.append(
            f"generated-cache measurement reports the largest "
            f"{MAX_PER_CATEGORY} directories")
        found = found[:MAX_PER_CATEGORY]
    return found


def _history_stats(root: Path, since_days: int,
                   warnings: list[str]) -> dict[str, Any] | None:
    base = root / "history"
    if not base.is_dir():
        return None
    cutoff = datetime.now(timezone.utc) - timedelta(days=since_days)
    files = 0
    old = 0
    old_bytes = 0
    oldest: str | None = None
    samples: list[tuple[float, str]] = []
    stack: list[tuple[Path, int]] = [(base, 0)]
    truncated = False
    while stack:
        directory, depth = stack.pop()
        try:
            entries = _entries(directory)
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if depth < 8:
                        stack.append((Path(entry.path), depth + 1))
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue
                stat = entry.stat(follow_symlinks=False)
                files += 1
                stamp = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
                day = stamp.date().isoformat()
                if stamp < cutoff:
                    old += 1
                    old_bytes += int(stat.st_size)
                    if oldest is None or day < oldest:
                        oldest = day
                    samples.append((stat.st_mtime, Path(entry.path).relative_to(
                        root).as_posix()))
                if files >= MAX_HISTORY_FILES:
                    truncated = True
                    break
            except (OSError, ValueError, OverflowError):
                continue
        if truncated:
            break
    if truncated:
        warnings.append(
            f"history/ walk stopped at {MAX_HISTORY_FILES} files; stale-note "
            "counts are a lower bound")
    samples.sort(key=lambda item: (item[0], item[1]))
    return {
        "files": files,
        "old_files": old,
        "old_bytes": old_bytes,
        "oldest": oldest,
        "samples": [path for _stamp, path in samples[:5]],
        "truncated": truncated,
    }


# --------------------------------------------------------------------------- #
# graph load (read-only) and preparation
# --------------------------------------------------------------------------- #
@dataclass
class _ScanContext:
    root: Path
    cfg: Any
    graph: dict[str, Any]
    graph_info: dict[str, Any]
    since_days: int
    warnings: list[str]
    changed: set[str] = field(default_factory=set)
    changed_all: list[str] = field(default_factory=list)
    roles: dict[str, str] = field(default_factory=dict)
    files: dict[str, dict[str, Any]] = field(default_factory=dict)
    file_keys: dict[str, str] = field(default_factory=dict)
    symbols: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    covered: set[str] = field(default_factory=set)
    test_files: set[str] = field(default_factory=set)
    doc_paths: list[str] = field(default_factory=list)
    in_degree: dict[str, int] = field(default_factory=dict)
    out_degree: dict[str, int] = field(default_factory=dict)
    churn: dict[str, int] = field(default_factory=dict)
    commits: list[dict[str, Any]] = field(default_factory=list)
    adapter_info: dict[str, Any] = field(default_factory=dict)
    unindexed: dict[str, list[str]] = field(default_factory=dict)
    clutter: list[dict[str, Any]] = field(default_factory=list)
    history: dict[str, Any] | None = None

    @property
    def has_graph(self) -> bool:
        return bool(self.graph.get("nodes") is not None
                    and self.graph_info.get("available")
                    and not self.graph_info.get("empty"))

    @property
    def graph_fresh(self) -> bool:
        return str(self.graph_info.get("freshness")) == "fresh"

    def confidence(self, level: str, *, graph_derived: bool) -> str:
        if graph_derived and not self.graph_fresh:
            return _confidence_down(level)
        return level

    def path(self, rel: str) -> Path:
        return self.root / rel


def _empty_graph_info(path: Path) -> dict[str, Any]:
    return {
        "path": str(path),
        "available": False,
        "freshness": "missing",
        "usable": False,
        "used": False,
        "empty": False,
        "reason": None,
        "nodes": 0,
        "edges": 0,
        "snapshot_head": None,
        "head": None,
        "snapshot_root": None,
        "root_matches": None,
        "working_tree_dirty": None,
        "format_version": None,
    }


def _load_graph(root: Path, graph_path: Path | None,
                head: str | None, dirty: bool | None
                ) -> tuple[dict[str, Any], dict[str, Any]]:
    path = graph_path if graph_path is not None else state_path(
        root, "graph.json")
    info = _empty_graph_info(path)
    info["head"] = head
    info["working_tree_dirty"] = dirty
    if not path.is_file():
        info["reason"] = f"no graph snapshot at {path}"
        return {}, info
    try:
        if path.stat().st_size > MAX_GRAPH_BYTES:
            info["reason"] = (
                f"graph snapshot is larger than {MAX_GRAPH_BYTES} bytes")
            return {}, info
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        info["available"] = True
        info["freshness"] = "error"
        info["reason"] = f"cannot read graph snapshot: {exc}"
        return {}, info
    if not isinstance(raw, dict):
        info["available"] = True
        info["freshness"] = "error"
        info["reason"] = "graph snapshot is not a JSON object"
        return {}, info
    nodes = raw.get("nodes")
    edges = raw.get("edges")
    if not isinstance(nodes, list) or not isinstance(edges, list):
        info["available"] = True
        info["freshness"] = "error"
        info["reason"] = "graph snapshot has no nodes/edges lists"
        return {}, info
    info["available"] = True
    info["format_version"] = raw.get("version")
    info["nodes"] = len(nodes)
    info["edges"] = len(edges)
    repo_attrs: dict[str, Any] = {}
    for node in nodes:
        if isinstance(node, dict) and node.get("type") == "Repo":
            attrs = node.get("attrs")
            repo_attrs = dict(attrs) if isinstance(attrs, Mapping) else {}
            break
    snapshot_head = repo_attrs.get("head")
    snapshot_root = repo_attrs.get("root")
    info["snapshot_head"] = snapshot_head
    info["snapshot_root"] = snapshot_root
    info["generated_at"] = repo_attrs.get("generated_at")
    reasons: list[str] = []
    if snapshot_root:
        try:
            info["root_matches"] = (
                Path(str(snapshot_root)).resolve() == root.resolve())
        except (OSError, RuntimeError, ValueError):
            info["root_matches"] = None
        if info["root_matches"] is False:
            reasons.append(f"snapshot root {snapshot_root} is not {root}")
    else:
        reasons.append("snapshot does not record its repository root")
    if not snapshot_head:
        reasons.append("snapshot does not record git HEAD")
    elif head is None:
        reasons.append("cannot read current git HEAD")
    elif str(snapshot_head) != head:
        reasons.append(
            f"snapshot head {str(snapshot_head)[:12]} does not match current "
            f"HEAD {head[:12]}")
    if reasons:
        info["freshness"] = "stale" if info["root_matches"] is False or (
            snapshot_head and head and str(snapshot_head) != head) else "unknown"
        info["reason"] = "; ".join(reasons)
    elif dirty is None:
        info["freshness"] = "unknown"
        info["reason"] = (
            "working-tree state could not be verified; the snapshot may not "
            "match the checkout")
    elif dirty:
        info["freshness"] = "unknown"
        info["reason"] = (
            "working tree has uncommitted changes; the snapshot may not "
            "include them")
    else:
        info["freshness"] = "fresh"
        info["reason"] = "snapshot root and HEAD match; working tree is clean"
    info["empty"] = not any(
        isinstance(node, Mapping) and node.get("type") != "Repo"
        for node in nodes)
    info["usable"] = bool(info["freshness"] == "fresh" and not info["empty"])
    info["used"] = bool(info["available"] and not info["empty"])
    if info["empty"]:
        info["reason"] = (info["reason"] or "") + (
            "; graph has no nodes" if info["reason"] else "graph has no nodes")
    return raw, info


def _infer_role(path: str, cfg: Any) -> str:
    name = Path(path).name
    suffix = Path(path).suffix.lower()
    test_dirs = tuple(cfg.test_dirs) if cfg is not None else ("tests",)
    if any(part == "tests" or part == "test" for part in Path(path).parts[:-1]):
        return "test"
    if any(path.startswith(str(item).rstrip("/") + "/") for item in test_dirs
           if str(item) not in ("", ".")):
        return "test"
    if name.startswith("test_") or name.endswith("_test.py"):
        return "test"
    if suffix in (".md", ".markdown"):
        return "doc"
    return "source"


def _prepare(root: Path, cfg: Any, graph: dict[str, Any],
             info: dict[str, Any], since_days: int,
             changed: list[str], warnings: list[str]) -> _ScanContext:
    ctx = _ScanContext(root=root, cfg=cfg, graph=graph, graph_info=info,
                       since_days=since_days, warnings=warnings,
                       changed=set(changed), changed_all=list(changed))
    nodes_raw = graph.get("nodes") if isinstance(graph.get("nodes"), list) else []
    edges_raw = graph.get("edges") if isinstance(graph.get("edges"), list) else []
    nodes: dict[int, dict[str, Any]] = {}
    for node in nodes_raw:
        if not isinstance(node, Mapping):
            continue
        try:
            nid = int(node.get("id"))
        except (TypeError, ValueError):
            continue
        nodes[nid] = dict(node)
    edges: list[dict[str, Any]] = [
        dict(edge) for edge in edges_raw if isinstance(edge, Mapping)]
    graph["nodes"] = list(nodes.values())
    graph["edges"] = edges

    table = graph.get("files")
    if not isinstance(table, Mapping):
        table = {}
    for path, entry in table.items():
        if not isinstance(path, str):
            continue
        role = entry.get("role") if isinstance(entry, Mapping) else None
        if role in ("source", "test", "doc"):
            ctx.roles[path] = str(role)

    docs: set[str] = set()
    for node in nodes.values():
        typ = node.get("type")
        attrs = node.get("attrs") if isinstance(node.get("attrs"), Mapping) else {}
        if typ == "File":
            path = attrs.get("path")
            if isinstance(path, str) and path:
                ctx.files[path] = dict(attrs)
                key = _node_key(node)
                if key:
                    ctx.file_keys[path] = key
                ctx.roles.setdefault(path, _infer_role(path, cfg))
        elif typ == "Doc":
            path = attrs.get("path")
            if isinstance(path, str) and path:
                docs.add(path)
    for path in docs:
        ctx.roles.setdefault(path, "doc")
    if cfg is not None:
        ctx.doc_paths = sorted(
            set(docs) | {path for path, role in ctx.roles.items()
                         if role == "doc"})
    else:
        ctx.doc_paths = sorted(docs)
    if not ctx.doc_paths:
        # A missing graph does not remove duplicate-Markdown evidence: walk
        # the docs the configuration names, or the repo root, read-only.
        ctx.doc_paths = _walk_files(
            root, ["."], cfg=cfg, suffixes=(".md", ".markdown"),
            max_files=MAX_DOC_WALK_FILES, warnings=warnings)

    ctx.test_files = {path for path, role in ctx.roles.items() if role == "test"}

    for nid, node in nodes.items():
        if node.get("type") != "Symbol":
            continue
        attrs = node.get("attrs") if isinstance(node.get("attrs"), Mapping) else {}
        path = attrs.get("file")
        if not isinstance(path, str) or not path:
            continue
        start = _safe_int(attrs.get("start_line"))
        end = _safe_int(attrs.get("end_line"))
        ctx.symbols.setdefault(path, []).append({
            "qualname": str(attrs.get("qualname") or ""),
            "start_line": start,
            "end_line": end,
            "span": max(0, end - start + 1),
        })
    for path in ctx.symbols:
        ctx.symbols[path].sort(key=lambda item: (
            item["start_line"], item["qualname"]))

    for edge in edges:
        try:
            src, tgt = int(edge.get("src")), int(edge.get("tgt"))
        except (TypeError, ValueError):
            continue
        etype = str(edge.get("type") or "")
        if etype in TEST_EDGE_TYPES:
            source_node = nodes.get(src)
            target_node = nodes.get(tgt)
            if (source_node and source_node.get("type") == "Test"
                    and target_node and target_node.get("type") == "File"):
                target_attrs = target_node.get("attrs")
                if isinstance(target_attrs, Mapping):
                    path = target_attrs.get("path")
                    if isinstance(path, str) and path:
                        ctx.covered.add(path)
        elif etype == IMPORT_EDGE:
            source_node = nodes.get(src)
            target_node = nodes.get(tgt)
            if (source_node and source_node.get("type") == "File"
                    and target_node and target_node.get("type") == "File"):
                source_attrs = source_node.get("attrs")
                target_attrs = target_node.get("attrs")
                source_path = (source_attrs.get("path")
                               if isinstance(source_attrs, Mapping) else None)
                target_path = (target_attrs.get("path")
                               if isinstance(target_attrs, Mapping) else None)
                if isinstance(source_path, str) and isinstance(target_path, str):
                    ctx.out_degree[source_path] = (
                        ctx.out_degree.get(source_path, 0) + 1)
                    ctx.in_degree[target_path] = (
                        ctx.in_degree.get(target_path, 0) + 1)

    cutoff = (date.today() - timedelta(days=since_days)).isoformat()
    for nid, node in nodes.items():
        if node.get("type") != "Commit":
            continue
        attrs = node.get("attrs") if isinstance(node.get("attrs"), Mapping) else {}
        stamp = str(attrs.get("date") or "")
        commit_hash = str(attrs.get("hash") or attrs.get("name") or "")
        files = []
        for edge in edges:
            if (str(edge.get("type")) == "changes"
                    and _safe_int(edge.get("src"), -1) == nid):
                target = nodes.get(_safe_int(edge.get("tgt"), -1))
                if target and target.get("type") == "File":
                    target_attrs = target.get("attrs")
                    path = (target_attrs.get("path")
                            if isinstance(target_attrs, Mapping) else None)
                    if isinstance(path, str) and path:
                        files.append(path)
        files = sorted(set(files))
        ctx.commits.append({"hash": commit_hash, "date": stamp,
                            "subject": str(attrs.get("subject") or ""),
                            "files": files})
        if stamp and stamp < cutoff:
            continue
        for path in files:
            ctx.churn[path] = ctx.churn.get(path, 0) + 1
    ctx.commits.sort(key=lambda item: (str(item["date"]), str(item["hash"])),
                     reverse=True)

    adapter = graph.get("adapter")
    ctx.adapter_info = dict(adapter) if isinstance(adapter, Mapping) else {}
    return ctx


# --------------------------------------------------------------------------- #
# opportunity ranking
# --------------------------------------------------------------------------- #
def _opportunity(ctx: _ScanContext, category: str, ident: str, title: str, *,
                 evidence: Iterable[Mapping[str, Any]],
                 expected_effect: str, effort: str, risk: str,
                 confidence: str, proposed_module: str, action: str,
                 not_assessed: Iterable[str], score: int,
                 graph_derived: bool) -> dict[str, Any]:
    if category not in CATEGORY_RANK:
        raise RepoScanError(f"unknown category {category!r}")
    if effort not in EFFORTS:
        raise RepoScanError(f"effort must be one of {EFFORTS}")
    if risk not in RISKS:
        raise RepoScanError(f"risk must be one of {RISKS}")
    if confidence not in CONFIDENCES:
        raise RepoScanError(f"confidence must be one of {CONFIDENCES}")
    return {
        "id": ident,
        "category": category,
        "title": title[:240],
        "evidence": _dedupe_evidence(evidence),
        "expected_effect": expected_effect,
        "effort": effort,
        "risk": risk,
        "confidence": ctx.confidence(confidence, graph_derived=graph_derived),
        "proposed_module": proposed_module,
        "action": action,
        "not_assessed": list(dict.fromkeys(str(item) for item in not_assessed)),
        "score": _clamp(score, 0, 100),
    }


def _source_paths(ctx: _ScanContext) -> list[str]:
    return sorted(path for path, role in ctx.roles.items()
                  if role == "source" and path in ctx.files)


def _is_entrypoint(path: str, ctx: _ScanContext) -> bool:
    name = Path(path).name
    if name in ENTRYPOINT_NAMES:
        return True
    if ctx.cfg is not None:
        for prefix in ctx.cfg.entrypoint_prefixes:
            clean = str(prefix).rstrip("/")
            if clean and path.startswith(clean + "/"):
                return True
    return False


def _hotspot_score(ctx: _ScanContext, path: str) -> int:
    symbols = ctx.symbols.get(path) or []
    max_span = max((item["span"] for item in symbols), default=0)
    return (min(20, len(symbols)) + min(20, max_span // 10)
            + min(20, ctx.churn.get(path, 0) * 4)
            + min(20, ctx.in_degree.get(path, 0)
                + ctx.out_degree.get(path, 0)))


def _test_target(path: str, ctx: _ScanContext) -> str:
    stem = Path(path).stem
    test_dir = "tests"
    if ctx.cfg is not None and ctx.cfg.test_dirs:
        test_dir = str(ctx.cfg.test_dirs[0]).strip("/") or "tests"
    if test_dir == ".":
        test_dir = "tests"
    return f"{test_dir}/test_{stem}.py"


def _config_display(ctx: _ScanContext) -> str:
    source = getattr(ctx.cfg, "source", None) if ctx.cfg is not None else None
    if source is None:
        return ".ozagia.toml"
    try:
        return Path(str(source)).resolve().relative_to(ctx.root).as_posix()
    except (OSError, RuntimeError, ValueError):
        return str(source)


# --------------------------------------------------------------------------- #
# detectors
# --------------------------------------------------------------------------- #
def _detect_missing_tests(ctx: _ScanContext) -> list[dict[str, Any]]:
    if not ctx.has_graph:
        return []
    out: list[dict[str, Any]] = []
    candidates: list[tuple[int, str, int]] = []
    for path in _source_paths(ctx):
        if _is_entrypoint(path, ctx):
            continue
        if path in ctx.covered:
            continue
        symbols = ctx.symbols.get(path) or []
        size = _safe_int((ctx.files.get(path) or {}).get("size"))
        if not symbols and size == 0:
            continue
        changed = path in ctx.changed
        score = (58 + (22 if changed else 0)
                 + min(15, _hotspot_score(ctx, path) // 3))
        candidates.append((score, path, len(symbols)))
    candidates.sort(key=lambda item: (-item[0], item[1]))
    for score, path, symbol_count in candidates[:MAX_CANDIDATES]:
        changed = path in ctx.changed
        title = (f"Changed source file has no linked test: {path}"
                 if changed else f"No linked test for {path}")
        evidence = [
            _file_ev(path),
            _key_ev(ctx.file_keys.get(path) or f"file:{path}"),
            _metric_ev("symbols", symbol_count),
        ]
        if changed:
            evidence.append(_cmd_ev("git status --porcelain",
                                    "file is changed in the working tree"))
        not_assessed = [
            "whether an unlinked test exercises this file through fixtures, "
            "subprocesses or dynamic imports",
            "coverage quality; the graph records links, not executed lines",
        ]
        out.append(_opportunity(
            ctx, "missing_tests", f"missing-tests:{path}", title,
            evidence=evidence,
            expected_effect=("a regression in this file would fail a targeted "
                             "test instead of going unnoticed"),
            effort="S" if symbol_count <= 5 else "M",
            risk="low",
            confidence="medium",
            proposed_module=_test_target(path, ctx),
            action=("Add focused tests for the public symbols, or record why "
                    "the file is exercised indirectly."),
            not_assessed=not_assessed, score=score, graph_derived=True))
    return out


def _detect_hotspots(ctx: _ScanContext) -> list[dict[str, Any]]:
    if not ctx.has_graph:
        return []
    out: list[dict[str, Any]] = []
    for path in _source_paths(ctx):
        symbols = ctx.symbols.get(path) or []
        max_span = max((item["span"] for item in symbols), default=0)
        degree = ctx.in_degree.get(path, 0) + ctx.out_degree.get(path, 0)
        churn = ctx.churn.get(path, 0)
        size = _safe_int((ctx.files.get(path) or {}).get("size"))
        triggers: list[str] = []
        if max_span >= 80:
            triggers.append(f"largest symbol spans {max_span} lines")
        if len(symbols) >= 12:
            triggers.append(f"{len(symbols)} symbols in one file")
        if churn >= 3:
            triggers.append(f"{churn} changes in the last {ctx.since_days} days")
        if degree >= 10:
            triggers.append(f"import degree {degree}")
        if size >= 100_000:
            triggers.append(f"{size} bytes")
        if not triggers:
            continue
        score = (50 + 5 * len(triggers) + min(20, churn * 3)
                 + min(15, max_span // 15))
        title = f"Hotspot: {path} (" + "; ".join(triggers) + ")"
        effort = "L" if max_span >= 200 or size >= 200_000 else "M"
        evidence = [
            _file_ev(path),
            _key_ev(ctx.file_keys.get(path) or f"file:{path}"),
            _metric_ev("symbols", len(symbols)),
            _metric_ev("largest_symbol_lines", max_span),
            _metric_ev("import_degree", degree),
            _metric_ev("churn_commits", churn,
                       f"commit nodes in the last {ctx.since_days} days"),
            _metric_ev("bytes", size),
            _cmd_ev("git log --name-only",
                    "churn comes from the graph's commit nodes"),
        ]
        if symbols:
            largest = max(symbols, key=lambda item: (
                item["span"], item["qualname"]))
            evidence.append(_file_ev(path, largest["start_line"],
                                     "largest symbol starts here"))
        out.append(_opportunity(
            ctx, "hotspots", f"hotspot:{path}", title,
            evidence=evidence,
            expected_effect=("reduce the change surface and make the file "
                             "easier to test and review"),
            effort=effort, risk="medium", confidence="medium",
            proposed_module=path,
            action=("Add tests around the largest symbol, then extract "
                    "cohesive helpers behind a smaller interface."),
            not_assessed=[
                "runtime complexity and hot-path cost were not measured",
                "whether the size is generated or intentionally data-heavy",
                "whether churn reflects one mechanical edit sequence",
            ],
            score=score, graph_derived=True))
    return out


def _exact_duplicates(items: Iterable[tuple[str, str]]
                      ) -> list[tuple[str, list[str]]]:
    groups: dict[str, list[str]] = {}
    for path, digest in items:
        groups.setdefault(digest, []).append(path)
    out = [(digest, sorted(paths)) for digest, paths in groups.items()
           if len(paths) > 1]
    out.sort(key=lambda item: (-len(item[1]), item[1][0]))
    return out


def _exact_pair_keys(groups: Iterable[tuple[str, list[str]]]) -> set[tuple[str, str]]:
    keys: set[tuple[str, str]] = set()
    for _digest, paths in groups:
        for left in range(len(paths)):
            for right in range(left + 1, len(paths)):
                keys.add(tuple(sorted((paths[left], paths[right]))))
    return keys


def _shingles(text: str, size: int = 5) -> set[str]:
    words = re.findall(r"[a-z0-9_]+", text.lower())
    if len(words) < size:
        return set(words)
    return {" ".join(words[index:index + size])
            for index in range(len(words) - size + 1)}


def _near_duplicates(pairs: Sequence[tuple[str, set[str]]],
                     threshold: float) -> list[tuple[float, str, str]]:
    out: list[tuple[float, str, str]] = []
    for left in range(len(pairs)):
        left_path, left_set = pairs[left]
        if not left_set:
            continue
        for right in range(left + 1, len(pairs)):
            right_path, right_set = pairs[right]
            if not right_set:
                continue
            union = len(left_set | right_set)
            if not union:
                continue
            score = len(left_set & right_set) / union
            if score >= threshold:
                out.append((score, left_path, right_path))
    out.sort(key=lambda item: (-item[0], item[1], item[2]))
    return out


def _duplicate_source_items(ctx: _ScanContext
                            ) -> tuple[list[tuple[str, str]],
                                       list[tuple[str, set[str]]]]:
    exact: list[tuple[str, str]] = []
    near: list[tuple[str, set[str]]] = []
    if not ctx.has_graph:
        return exact, near
    paths = [path for path in _source_paths(ctx)
             if Path(path).suffix.lower() in INDEXED_SUFFIXES]
    for path in paths:
        attrs = ctx.files.get(path) or {}
        digest = attrs.get("sha256")
        if isinstance(digest, str) and digest and _safe_int(attrs.get("size")):
            exact.append((path, digest))
    for path in paths[:MAX_PAIR_FILES]:
        text = _read_text(ctx.path(path))
        if text is None:
            continue
        shingles = _shingles(text, size=6)
        if len(shingles) >= 40:
            near.append((path, shingles))
    return exact, near


def _detect_duplicate_modules(ctx: _ScanContext) -> list[dict[str, Any]]:
    exact, near = _duplicate_source_items(ctx)
    exact_groups = _exact_duplicates(exact)
    exact_keys = _exact_pair_keys(exact_groups)
    out: list[dict[str, Any]] = []
    for digest, paths in exact_groups[:MAX_PAIR_RESULTS]:
        score = 66 + min(10, len(paths))
        out.append(_opportunity(
            ctx, "duplicate_modules",
            f"duplicate-modules:{_short(digest)}",
            f"Identical source files: {', '.join(paths[:4])}",
            evidence=[_file_ev(path) for path in paths[:8]] + [
                _metric_ev("files", len(paths)),
                _metric_ev("sha256", digest),
            ],
            expected_effect="one implementation instead of divergent copies",
            effort="M", risk="medium", confidence="high",
            proposed_module=paths[0],
            action=("Keep one module and re-export or delete the duplicate; "
                    "confirm callers before removing anything."),
            not_assessed=[
                "whether the copies are intentionally frozen or vendored",
                "whether external users import the duplicate path",
            ], score=score, graph_derived=True))
    for score, left, right in _near_duplicates(near, 0.82)[:MAX_PAIR_RESULTS]:
        if tuple(sorted((left, right))) in exact_keys:
            continue
        score_int = 50 + int(score * 30)
        out.append(_opportunity(
            ctx, "duplicate_modules",
            f"near-duplicate-modules:{_short(_join((left, right)))}",
            f"Near-duplicate source files: {left} and {right}",
            evidence=[
                _file_ev(left), _file_ev(right),
                _metric_ev("token_jaccard", round(score, 3)),
                _metric_ev("shingle_size", 6),
            ],
            expected_effect="less duplicated logic and one place to fix bugs",
            effort="M", risk="medium", confidence="medium",
            proposed_module=left,
            action="Compare the two modules and extract the shared logic.",
            not_assessed=[
                "whether the similarity is boilerplate rather than duplicated "
                "behaviour",
                "semantic equivalence; token overlap is not a proof",
            ], score=score_int, graph_derived=True))
    return out


def _detect_duplicate_docs(ctx: _ScanContext) -> list[dict[str, Any]]:
    exact: list[tuple[str, str]] = []
    near: list[tuple[str, set[str]]] = []
    for path in ctx.doc_paths[:MAX_PAIR_FILES]:
        text = _read_text(ctx.path(path))
        if text is None or not text.strip():
            continue
        digest = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()
        exact.append((path, digest))
        shingles = _shingles(text, size=5)
        if len(shingles) >= 30:
            near.append((path, shingles))
    out: list[dict[str, Any]] = []
    exact_groups = _exact_duplicates(exact)
    exact_keys = _exact_pair_keys(exact_groups)
    for digest, paths in exact_groups[:MAX_PAIR_RESULTS]:
        score = 58 + min(10, len(paths))
        out.append(_opportunity(
            ctx, "duplicate_docs",
            f"duplicate-docs:{_short(digest)}",
            f"Identical Markdown: {', '.join(paths[:4])}",
            evidence=[_file_ev(path) for path in paths[:8]] + [
                _metric_ev("files", len(paths)),
                _metric_ev("sha256", digest),
            ],
            expected_effect="one canonical page and less contradictory guidance",
            effort="S", risk="low", confidence="high",
            proposed_module=paths[0],
            action=("Keep one canonical document, cross-link from the other "
                    "or move an obsolete note to history/ after confirmation."),
            not_assessed=[
                "which copy is authoritative",
                "whether external links point at the duplicate path",
            ], score=score, graph_derived=False))
    for score, left, right in _near_duplicates(near, 0.80)[:MAX_PAIR_RESULTS]:
        if tuple(sorted((left, right))) in exact_keys:
            continue
        out.append(_opportunity(
            ctx, "duplicate_docs",
            f"near-duplicate-docs:{_short(_join((left, right)))}",
            f"Near-duplicate Markdown: {left} and {right}",
            evidence=[
                _file_ev(left), _file_ev(right),
                _metric_ev("word_shingle_jaccard", round(score, 3)),
            ],
            expected_effect="one maintained explanation instead of drift",
            effort="S", risk="low", confidence="medium",
            proposed_module=left,
            action="Merge the unique sections into one page and cross-link.",
            not_assessed=[
                "whether the pages target different audiences",
                "whether overlapping wording is unavoidable reference text",
            ], score=45 + int(score * 30), graph_derived=False))
    return out


def _detect_weak_boundaries(ctx: _ScanContext) -> list[dict[str, Any]]:
    if not ctx.has_graph:
        return []
    out: list[dict[str, Any]] = []
    for path in _source_paths(ctx):
        fan_in = ctx.in_degree.get(path, 0)
        fan_out = ctx.out_degree.get(path, 0)
        reasons = []
        if fan_in >= 5:
            reasons.append(f"{fan_in} importers")
        if fan_out >= 10:
            reasons.append(f"{fan_out} imports")
        if not reasons:
            continue
        score = (52 + min(20, fan_in * 2) + min(15, fan_out)
                 + (10 if path in ctx.changed else 0))
        out.append(_opportunity(
            ctx, "weak_boundaries", f"weak-boundary:{path}",
            f"Weak boundary: {path} has " + " and ".join(reasons),
            evidence=[
                _file_ev(path),
                _key_ev(ctx.file_keys.get(path) or f"file:{path}"),
                _metric_ev("fan_in", fan_in),
                _metric_ev("fan_out", fan_out),
                _cmd_ev("python -m ozagia.core.architecture",
                        "imports come from the graph's File -imports-> File "
                        "edges"),
            ],
            expected_effect=("a smaller, explicit interface and fewer "
                             "surprising cross-module changes"),
            effort="M", risk="medium", confidence="medium",
            proposed_module=path,
            action=("Review the exported surface, group callers behind a "
                    "facade or split independent responsibilities."),
            not_assessed=[
                "whether the hub is an intentional stable interface",
                "runtime coupling through globals, plugins or data files",
                "cycle detection; this detector counts direct import edges",
            ], score=score, graph_derived=True))
    return out


def _detect_orphaned_files(ctx: _ScanContext) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if ctx.has_graph:
        for path in _source_paths(ctx):
            if _is_entrypoint(path, ctx):
                continue
            if path in ctx.covered or path in ctx.test_files:
                continue
            if ctx.in_degree.get(path, 0) != 0:
                continue
            if ctx.out_degree.get(path, 0) == 0 and not ctx.symbols.get(path):
                continue
            score = 45 + min(15, ctx.churn.get(path, 0) * 3)
            out.append(_opportunity(
                ctx, "orphaned_files", f"orphaned-file:{path}",
                f"No indexed file imports {path}",
                evidence=[
                    _file_ev(path),
                    _key_ev(ctx.file_keys.get(path) or f"file:{path}"),
                    _metric_ev("in_degree", 0),
                    _metric_ev("out_degree", ctx.out_degree.get(path, 0)),
                    _cmd_ev("python -m ozagia.core.architecture",
                            "dead-code candidate from the import graph"),
                ],
                expected_effect=("remove an unused file or document why it is "
                                 "an entrypoint"),
                effort="S", risk="medium", confidence="low",
                proposed_module=path,
                action=("Confirm it is not loaded dynamically, then document "
                        "it as an entrypoint or remove it after review."),
                not_assessed=[
                    "dynamic imports, plugin discovery and external callers",
                    "whether a test loads this file without a graph edge",
                ], score=score, graph_derived=True))
    # The created-file ledger is a second, independent orphan signal.
    try:
        from ozagia.core import files as ledger
        for item in ledger.orphans(ctx.root)[:5]:
            path = str(item.get("path") or "")
            if not path:
                continue
            out.append(_opportunity(
                ctx, "orphaned_files", f"orphaned-ledger:{path}",
                f"Created file has no recorded reference: {path}",
                evidence=[
                    _file_ev(path),
                    _cmd_ev("python -m ozagia.core.files list",
                            "file ledger metadata"),
                    _metric_ev("size", _safe_int(item.get("size"))),
                ],
                expected_effect="less untracked scratch output",
                effort="S", risk="low", confidence="medium",
                proposed_module=path,
                action=("Reference the file from a durable page, or move it "
                        "to .ozagia/trash after explicit confirmation."),
                not_assessed=[
                    "whether a person still needs the file",
                    "whether the ledger missed a reference",
                ], score=38, graph_derived=False))
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return out


def _detect_stale_history(ctx: _ScanContext) -> list[dict[str, Any]]:
    stats = ctx.history
    if not stats or not stats.get("old_files"):
        return []
    count = int(stats["old_files"])
    size = int(stats["old_bytes"])
    score = 34 + min(20, count) + min(10, size // (1024 * 1024) * 2)
    evidence = [
        _metric_ev("old_files", count),
        _metric_ev("old_bytes", size),
        _cmd_ev(f"find history -type f -mtime +{ctx.since_days}"),
    ]
    if stats.get("oldest"):
        evidence.append(_metric_ev("oldest_date", stats["oldest"]))
    for path in stats.get("samples") or []:
        evidence.append(_file_ev(str(path)))
    return [_opportunity(
        ctx, "stale_history", "stale-history:history",
        f"history/ holds {count} file(s) older than {ctx.since_days} days",
        evidence=evidence,
        expected_effect="less review noise and a smaller working tree",
        effort="S", risk="medium", confidence="high",
        proposed_module="history/README.md",
        action=("Review the notes, keep anything still cited, and move expired "
                "ones to .ozagia/trash only after confirmation."),
        not_assessed=[
            "whether the notes remain useful outside the repository",
            "whether they are linked from an external tracker",
        ], score=score, graph_derived=False)]


def _detect_generated_clutter(ctx: _ScanContext) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in ctx.clutter:
        path = str(item.get("path") or "")
        size = _safe_int(item.get("bytes"))
        if not path or size <= 0:
            continue
        files = _safe_int(item.get("files"))
        score = 30 + min(30, (size // (1024 * 1024)) * 5
                         + (5 if files else 0))
        evidence = [
            _file_ev(path, detail="generated/cache directory"),
            _metric_ev("bytes", size),
            _metric_ev("files", files),
            _cmd_ev(f"du -sb {path}"),
        ]
        if item.get("truncated"):
            evidence.append(_metric_ev("measurement_truncated", True))
        out.append(_opportunity(
            ctx, "generated_clutter", f"generated-clutter:{path}",
            f"Generated/cache clutter: {path} ({size} bytes)",
            evidence=evidence,
            expected_effect=f"reclaim up to {size} bytes when the cache is "
                            "no longer needed",
            effort="S", risk="medium",
            confidence=("low" if item.get("truncated") else "high"),
            proposed_module=".ozagia/trash",
            action=("Clean only with an explicit confirmation; prefer the "
                    "tool's own cache command, then move leftovers to "
                    ".ozagia/trash."),
            not_assessed=[
                "whether the cache is active and regenerating it is expensive",
                "exact per-process attribution of the on-disk bytes",
                "other filesystems, snapshots and excluded directories",
            ], score=score, graph_derived=False))
    return out


def _unindexed_files(ctx: _ScanContext) -> dict[str, list[str]]:
    dirs = list(ctx.cfg.source_dirs) if ctx.cfg is not None else ["."]
    found = _walk_files(
        ctx.root, dirs, cfg=ctx.cfg, suffixes=SOURCE_LANGUAGES.keys(),
        max_files=MAX_WALK_FILES, warnings=ctx.warnings)
    grouped: dict[str, list[str]] = {}
    for path in found:
        language = SOURCE_LANGUAGES.get(Path(path).suffix.lower())
        if language:
            grouped.setdefault(language, []).append(path)
    # Also count graph File nodes in unindexed languages; an adapter may have
    # added them without the built-in index parsing them.
    for path, attrs in ctx.files.items():
        language = SOURCE_LANGUAGES.get(Path(path).suffix.lower())
        if language and path not in grouped.get(language, []):
            grouped.setdefault(language, []).append(path)
    return {language: sorted(set(paths))
            for language, paths in sorted(grouped.items())}


def _detect_unindexed_languages(ctx: _ScanContext) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for language, paths in ctx.unindexed.items():
        count = len(paths)
        score = 48 + min(15, count)
        evidence = [
            _metric_ev("files", count),
            _metric_ev("language", language),
            _cmd_ev("find <source_dirs> -type f",
                    "walked read-only, bounded and without following symlinks"),
        ]
        for path in paths[:5]:
            evidence.append(_file_ev(path))
        out.append(_opportunity(
            ctx, "unindexed_languages", f"unindexed-language:{language}",
            f"{count} {language} source file(s) outside the built-in index",
            evidence=evidence,
            expected_effect=("graph queries and symbol search can see these "
                             "files instead of silently omitting them"),
            effort="M", risk="low", confidence="medium",
            proposed_module=".ozagia.toml",
            action=("If these files matter, add a project adapter indexer "
                    "([adapters] modules); otherwise ignore this category."),
            not_assessed=[
                "whether the files are generated, vendored or build output",
                "semantic quality of any future adapter",
                "the built-in index parses only Python, Rust and Lua today",
            ], score=score, graph_derived=False))
    return out


def _detect_missing_adapters(ctx: _ScanContext) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    configured = list(ctx.cfg.adapters) if ctx.cfg is not None else []
    unindexed = sum(len(paths) for paths in ctx.unindexed.values())
    config_path = _config_display(ctx)
    if configured:
        contributed = (
            bool(ctx.adapter_info.get("files"))
            or bool(ctx.adapter_info.get("edges"))
            or _safe_int(ctx.adapter_info.get("unkeyed")) > 0)
        if ctx.graph_info.get("available") and not contributed:
            out.append(_opportunity(
                ctx, "missing_adapters", "missing-adapter:configured",
                "Configured adapter(s) contributed no graph nodes",
                evidence=[
                    _file_ev(config_path),
                    _metric_ev("adapter_modules", len(configured)),
                    _metric_ev("adapter_files", len(
                        ctx.adapter_info.get("files") or [])),
                    _metric_ev("adapter_edges", len(
                        ctx.adapter_info.get("edges") or [])),
                    _metric_ev("adapter_unkeyed",
                               _safe_int(ctx.adapter_info.get("unkeyed"))),
                ],
                expected_effect="adapter-owned knowledge appears in graph "
                                "queries",
                effort="M", risk="low", confidence="medium",
                proposed_module=".ozagia.toml",
                action=("Check the adapter module path and its indexer; a "
                        "manual graph rebuild is needed only after the fix."),
                not_assessed=[
                    "whether the adapter intentionally adds nothing for this "
                    "checkout",
                    "whether the graph predates the adapter configuration",
                ], score=56, graph_derived=True))
        leftover = ctx.adapter_info.get("unindexed") or []
        if leftover:
            out.append(_opportunity(
                ctx, "missing_adapters", "missing-adapter:unindexed",
                f"Adapter left {len(leftover)} file(s) unindexed",
                evidence=[_file_ev(str(path)) for path in leftover[:8]] + [
                    _metric_ev("files", len(leftover)),
                    _metric_ev("adapter_modules", len(configured)),
                ],
                expected_effect="adapter coverage that matches its write scope",
                effort="M", risk="low", confidence="medium",
                proposed_module=".ozagia.toml",
                action="Review the adapter's unindexed files and extend or "
                       "narrow its indexer.",
                not_assessed=[
                    "whether the listed files are intentionally opaque",
                    "whether the adapter's own tests cover these paths",
                ], score=52, graph_derived=True))
    elif unindexed:
        out.append(_opportunity(
            ctx, "missing_adapters", "missing-adapter:none",
            f"No project adapter configured for {unindexed} unindexed "
            "source file(s)",
            evidence=[
                _file_ev(config_path),
                _metric_ev("unindexed_files", unindexed),
                _metric_ev("languages", len(ctx.unindexed)),
                _cmd_ev("python -m ozagia.core.memory stats"),
            ],
            expected_effect="project-specific symbols and edges become "
                            "queryable",
            effort="M", risk="low", confidence="medium",
            proposed_module=".ozagia.toml",
            action=("Add a read-only adapter indexer under [adapters] modules "
                    "if the languages are part of the development surface."),
            not_assessed=[
                "whether the unindexed files are vendored or generated",
                "whether an adapter's added edges would be worth the cost",
            ], score=54, graph_derived=False))
    return out


def _result_state(ctx: _ScanContext) -> dict[str, Any]:
    path = state_path(ctx.root, "tests", "results.json")
    info: dict[str, Any] = {"path": path, "available": False, "failing": [],
                            "flaky": [], "version": None}
    if not path.is_file():
        return info
    try:
        if path.stat().st_size > MAX_REVIEW_BYTES:
            return info
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return info
    if not isinstance(raw, Mapping):
        return info
    info["available"] = True
    info["version"] = raw.get("version")
    failing: set[str] = set()
    suites = raw.get("failing")
    if isinstance(suites, Mapping):
        for suite in suites.values():
            if not isinstance(suite, Mapping):
                continue
            files = suite.get("files")
            if isinstance(files, Mapping):
                for rel in files:
                    if isinstance(rel, str) and rel:
                        failing.add(rel)
    flaky = raw.get("flaky")
    if isinstance(flaky, Mapping):
        for nodeid in flaky:
            if isinstance(nodeid, str) and "::" in nodeid:
                rel = nodeid.split("::", 1)[0]
                if rel:
                    info["flaky"].append(rel)
    info["failing"] = sorted(failing)
    info["flaky"] = sorted(set(info["flaky"]))
    return info


def _detect_test_policy(ctx: _ScanContext) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    config_path = _config_display(ctx)
    if (ctx.cfg is not None and ctx.cfg.tests_select == "all"
            and ctx.changed):
        out.append(_opportunity(
            ctx, "test_policy", "test-policy:select-all",
            "Test selection is 'all' while the working tree has local changes",
            evidence=[
                _file_ev(config_path),
                _metric_ev("tests_select", ctx.cfg.tests_select),
                _metric_ev("changed_files", len(ctx.changed)),
                _cmd_ev("git status --porcelain"),
            ],
            expected_effect="shorter local test runs without changing what CI "
                            "runs",
            effort="S", risk="medium", confidence="low",
            proposed_module=".ozagia.toml",
            action=("Consider [tests] select = \"affected\" only if the "
                    "affected planner is trusted for this repository; this "
                    "scan does not change it."),
            not_assessed=[
                "whether affected selection is safe enough for this project",
                "how long the full suite takes on the current host",
            ], score=40, graph_derived=False))
    results = _result_state(ctx)
    for path in results["failing"][:5]:
        out.append(_opportunity(
            ctx, "test_policy", f"test-policy:failure:{path}",
            f"Last recorded test run failed for {path}",
            evidence=[
                _file_ev(path),
                _file_ev(str(results["path"]), detail="test result state"),
                _cmd_ev(f"python -m pytest {path}"),
            ],
            expected_effect="restore a trustworthy test signal",
            effort="M", risk="low", confidence="high",
            proposed_module=path,
            action="Reproduce the failure, fix it or quarantine it with a "
                   "tracked reason.",
            not_assessed=[
                "whether the recorded result still applies to this revision",
                "whether the failure is environmental",
            ], score=62, graph_derived=False))
    if results["flaky"]:
        out.append(_opportunity(
            ctx, "test_policy", "test-policy:flaky",
            f"{len(results['flaky'])} test file(s) contain flaky tests",
            evidence=[
                _file_ev(str(results["path"]), detail="test result state"),
                _metric_ev("flaky_files", len(results["flaky"])),
            ] + [_file_ev(path) for path in results["flaky"][:5]],
            expected_effect="a deterministic test signal",
            effort="M", risk="low", confidence="high",
            proposed_module="tests/",
            action="Stabilise or quarantine the flaky tests; record the cause.",
            not_assessed=[
                "whether the flakiness is timing, ordering or environment",
                "whether the last known result still applies",
            ], score=58, graph_derived=False))
    if ctx.has_graph and not any(node_type == "Test"
                                 for node_type in _node_types(ctx)):
        sources = _source_paths(ctx)
        if sources:
            out.append(_opportunity(
                ctx, "test_policy", "test-policy:no-test-nodes",
                "Graph records source files but no Test nodes",
                evidence=[
                    _metric_ev("source_files", len(sources)),
                    _metric_ev("test_nodes", 0),
                    _cmd_ev("python -m ozagia.core.memory stats"),
                ],
                expected_effect="test impact and coverage queries become "
                                "usable",
                effort="S", risk="low", confidence="medium",
                proposed_module="tests/",
                action=("Check [index] test_dirs and test naming; the graph "
                        "builder indexes tests/test_*.py by default."),
                not_assessed=[
                    "whether tests live outside the configured test dirs",
                    "whether test files use a non-default naming convention",
                ], score=50, graph_derived=True))
    return out


def _node_types(ctx: _ScanContext) -> set[str]:
    nodes = ctx.graph.get("nodes")
    if not isinstance(nodes, list):
        return set()
    return {str(node.get("type")) for node in nodes if isinstance(node, Mapping)}


def _review_packets(ctx: _ScanContext) -> list[Path]:
    base = state_path(ctx.root, "reviews")
    if not base.is_dir():
        return []
    try:
        paths = sorted(path for path in base.glob("*.json") if path.is_file())
    except OSError:
        return []
    if len(paths) > MAX_REVIEW_FILES:
        ctx.warnings.append(
            f"review packet scan reads the newest {MAX_REVIEW_FILES} files "
            f"of {len(paths)}")
        paths = paths[-MAX_REVIEW_FILES:]
    return paths


def _detect_code_review(ctx: _ScanContext) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for path in _review_packets(ctx):
        try:
            if path.stat().st_size > MAX_REVIEW_BYTES:
                ctx.warnings.append(f"review packet too large: {path}")
                continue
            packet = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            ctx.warnings.append(f"cannot read review packet {path}: {exc}")
            continue
        if not isinstance(packet, Mapping):
            continue
        if packet.get("schema") != "ozagia.code_review.packet.v1":
            continue
        findings = packet.get("findings")
        if not isinstance(findings, list):
            continue
        try:
            rel_packet = path.relative_to(ctx.root).as_posix()
        except ValueError:
            rel_packet = str(path)
        for finding in findings:
            if not isinstance(finding, Mapping):
                continue
            severity = str(finding.get("severity") or "")
            if severity not in ("blocking", "major"):
                continue
            file_path = finding.get("file")
            line = finding.get("line")
            message = str(finding.get("message") or "review finding")
            fid = str(finding.get("id") or _short(
                _join((severity, str(file_path), str(line), message))))
            evidence = [
                _file_ev(rel_packet, detail=f"review packet ({severity})"),
                _metric_ev("severity", severity),
                _metric_ev("finding_type", str(finding.get("type") or "unknown")),
            ]
            if isinstance(file_path, str) and file_path:
                evidence.append(_file_ev(
                    file_path, _safe_int(line) or None,
                    "finding location"))
            for key in finding.get("graph_keys") or []:
                if isinstance(key, str) and key:
                    evidence.append(_key_ev(key))
            base_value = packet.get("base") or packet.get("base_commit")
            head_value = packet.get("head") or packet.get("head_commit")
            if base_value and head_value:
                evidence.append(_cmd_ev(
                    "python -m ozagia.code_review_cli --range "
                    f"{base_value}..{head_value}",
                    "re-run the deterministic review"))
            score = 82 if severity == "blocking" else 70
            confidence = str(finding.get("confidence") or "medium")
            if confidence not in CONFIDENCES:
                confidence = "medium"
            out.append(_opportunity(
                ctx, "code_review",
                f"code-review:{rel_packet}:{fid}",
                f"Review finding ({severity}): {message}",
                evidence=evidence,
                expected_effect="resolve the finding before the change lands",
                effort="S" if file_path else "M",
                risk="high" if severity == "blocking" else "medium",
                confidence=confidence,
                proposed_module=str(file_path or rel_packet),
                action=("Address the finding, then re-run the same review "
                        "range; the scan does not re-run it."),
                not_assessed=[
                    "whether the finding is still open on the current tree",
                    "independent model review and semantic correctness",
                ], score=score, graph_derived=False))
    if ctx.has_graph:
        for nid in ctx.graph.get("nodes") or []:
            if not isinstance(nid, Mapping) or nid.get("type") != "Failure":
                continue
            attrs = nid.get("attrs") if isinstance(nid.get("attrs"), Mapping) else {}
            name = str(attrs.get("name") or attrs.get("node") or "")
            file_path = str(attrs.get("file") or "")
            evidence = [
                _key_ev(_node_key(nid) or f"failure:{name}"),
                _metric_ev("kind", str(attrs.get("kind") or "")),
                _metric_ev("last_green_commit",
                           str(attrs.get("last_green_commit") or "")),
                _cmd_ev("python -m ozagia.core.memory failures"),
            ]
            if file_path:
                evidence.append(_file_ev(file_path))
            out.append(_opportunity(
                ctx, "code_review",
                f"code-review:known-failure:{_short(name)}",
                f"Known failure record: {name or 'unnamed'}",
                evidence=evidence,
                expected_effect="a known failure is reproduced or cleared",
                effort="M", risk="medium", confidence="medium",
                proposed_module=file_path or "results/failure_baseline.json",
                action=("Reproduce the recorded failure and update the "
                        "baseline only with evidence."),
                not_assessed=[
                    "whether the baseline entry is still current",
                    "whether the failure is environmental or a real defect",
                ], score=65, graph_derived=True))
    return out


# --------------------------------------------------------------------------- #
# scan orchestration
# --------------------------------------------------------------------------- #
_DETECTORS = (
    ("missing_tests", _detect_missing_tests),
    ("hotspots", _detect_hotspots),
    ("duplicate_modules", _detect_duplicate_modules),
    ("duplicate_docs", _detect_duplicate_docs),
    ("weak_boundaries", _detect_weak_boundaries),
    ("orphaned_files", _detect_orphaned_files),
    ("stale_history", _detect_stale_history),
    ("generated_clutter", _detect_generated_clutter),
    ("unindexed_languages", _detect_unindexed_languages),
    ("missing_adapters", _detect_missing_adapters),
    ("test_policy", _detect_test_policy),
    ("code_review", _detect_code_review),
)


def _global_not_assessed(ctx: _ScanContext) -> list[str]:
    items = [
        "correctness and completeness: the scan ranks candidates, it does "
        "not prove any file is wrong, unused or untested",
        "runtime performance, memory use and production behaviour",
        "line or branch coverage; the graph records links, not execution",
        "semantic equivalence of duplicate candidates",
        "security, dependency, licence and supply-chain properties",
        "anything requiring a model, the network or a live service; none was "
        "used",
        "cleanup or edits; the scan never deletes, moves or writes into the "
        "repository",
    ]
    if not ctx.has_graph:
        items.append(
            "every graph-derived category (missing tests, hotspots, weak "
            "boundaries, graph orphans, duplicate modules)")
    elif not ctx.graph_fresh:
        items.append(
            "graph freshness: results may reflect an older checkout; "
            "graph-derived confidence is reduced")
    if ctx.graph_info.get("empty"):
        items.append("the graph is empty, so graph-derived categories were "
                     "not assessed")
    return items


def _report_json(report: Mapping[str, Any]) -> str:
    return json.dumps(report, indent=2, sort_keys=True) + "\n"


def _format_evidence(item: Mapping[str, Any]) -> str:
    kind = str(item.get("kind") or "?")
    if kind == "file":
        line = item.get("line")
        return f"file {item.get('path')}" + (
            f":{line}" if line is not None else "")
    if kind == "graph_key":
        return f"graph {item.get('key')}"
    if kind == "command":
        return f"command `{item.get('command')}`"
    if kind == "metric":
        return f"{item.get('name')}={item.get('value')}"
    return kind


def format_report(report: Mapping[str, Any]) -> str:
    """A compact text summary of a scan report."""
    graph = report.get("graph") or {}
    lines = [
        "Repository scan (advisory)",
        f"root: {report.get('root')}",
        f"status: {report.get('status')}",
        f"graph: {graph.get('freshness', 'missing')} "
        f"({graph.get('nodes', 0)} nodes, {graph.get('edges', 0)} edges) "
        f"- {graph.get('reason') or 'no detail'}",
        "nothing was built, edited, executed, deleted or sent",
        "",
    ]
    inputs = report.get("inputs") or {}
    lines.append(
        f"window: {inputs.get('since_days', 0)} days; top: "
        f"{inputs.get('top', 0)}; changed: "
        f"{inputs.get('changed_count', 0)}")
    summary = report.get("summary") or {}
    omitted = report.get("omitted") or {}
    lines.append(
        f"opportunities: {summary.get('opportunities', 0)} shown "
        f"({omitted.get('total', 0)} omitted of "
        f"{summary.get('candidates', 0)} candidates)")
    by_category = summary.get("by_category") or {}
    if by_category:
        lines.append("by category: " + ", ".join(
            f"{name} {count}" for name, count in sorted(by_category.items())))
    lines.append("")
    for item in report.get("opportunities") or []:
        lines.append(
            f"[{item.get('rank')}] {CATEGORY_TITLE.get(str(item.get('category')), item.get('category'))}"
            f" - {item.get('title')}")
        lines.append(
            f"    score {item.get('score')} | effort {item.get('effort')} | "
            f"risk {item.get('risk')} | confidence {item.get('confidence')}")
        lines.append(
            f"    proposed module: {item.get('proposed_module')}")
        lines.append(f"    action: {item.get('action')}")
        if item.get("evidence"):
            lines.append("    evidence: " + "; ".join(
                _format_evidence(entry) for entry in item["evidence"]))
        if item.get("not_assessed"):
            lines.append("    not assessed: " + "; ".join(
                str(value) for value in item["not_assessed"]))
        lines.append("")
    if report.get("warnings"):
        lines.append("Warnings:")
        for warning in report["warnings"]:
            lines.append(f"- {warning}")
        lines.append("")
    if report.get("not_assessed"):
        lines.append("Global not assessed:")
        for item in report["not_assessed"]:
            lines.append(f"- {item}")
        lines.append("")
    if omitted.get("by_category"):
        lines.append("Omitted by category: " + ", ".join(
            f"{name} {count}"
            for name, count in sorted(omitted["by_category"].items())))
    return "\n".join(lines).rstrip() + "\n"


def format_categories() -> str:
    """List the scan categories for ``list-categories``."""
    lines = ["Repository scan categories:"]
    for item in CATEGORIES:
        lines.append(f"- {item['id']}: {item['title']}")
        lines.append(f"  {item['description']}")
    return "\n".join(lines) + "\n"


def scan(root: str | Path = ".", *, graph_path: str | Path | None = None,
         since_days: int = DEFAULT_SINCE_DAYS, top: int = DEFAULT_TOP,
         per_category: int = DEFAULT_PER_CATEGORY) -> dict[str, Any]:
    """Scan one checkout read-only and return a deterministic report.

    `since_days` bounds the churn window and the stale-note age. `top` bounds
    the opportunities shown; `per_category` bounds each category before the
    global cap. A missing graph is reported, not rebuilt.
    """
    root_path = Path(root).expanduser()
    try:
        root_path = root_path.resolve()
    except (OSError, RuntimeError) as exc:
        raise RepoScanError(f"cannot resolve root {root!r}: {exc}") from exc
    if not root_path.is_dir():
        raise RepoScanError(f"root is not a directory: {root_path}")
    since = _clamp(_safe_int(since_days, DEFAULT_SINCE_DAYS), 1,
                   MAX_SINCE_DAYS)
    top_n = _clamp(_safe_int(top, DEFAULT_TOP), 1, MAX_TOP)
    per_category = _clamp(_safe_int(per_category, DEFAULT_PER_CATEGORY), 1,
                          MAX_PER_CATEGORY)

    warnings: list[str] = []
    try:
        cfg = load_project_config(root_path)
    except (OSError, ValueError) as exc:
        cfg = None
        warnings.append(f"project configuration could not be read: {exc}")

    head = _current_head(root_path)
    if head is None:
        warnings.append("git HEAD could not be read; graph freshness is "
                        "unknown")
    changed, dirty, status_ok = _status_paths(root_path, cfg, warnings)
    if status_ok and not dirty:
        dirty = False
    graph_file: Path | None = None
    if graph_path is not None:
        candidate = Path(graph_path).expanduser()
        if not candidate.is_absolute():
            candidate = root_path / candidate
        graph_file = candidate.resolve()
    graph, graph_info = _load_graph(root_path, graph_file, head,
                                    dirty if status_ok else None)
    ctx = _prepare(root_path, cfg, graph, graph_info, since, changed, warnings)
    if graph_info.get("available") and graph_info.get("empty"):
        warnings.append("graph snapshot is empty; graph-derived categories "
                        "were not assessed")
    ctx.unindexed = _unindexed_files(ctx)
    ctx.clutter = _measure_clutter(root_path, warnings)
    ctx.history = _history_stats(root_path, since, warnings)

    raw: list[dict[str, Any]] = []
    for name, detector in _DETECTORS:
        try:
            raw.extend(detector(ctx))
        except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
            warnings.append(f"category {name} could not be assessed: "
                            f"{type(exc).__name__}: {exc}")

    unique: dict[str, dict[str, Any]] = {}
    for item in raw:
        unique.setdefault(str(item["id"]), item)
    ordered = sorted(
        unique.values(),
        key=lambda item: (-int(item["score"]),
                          CATEGORY_RANK.get(str(item["category"]), 99),
                          str(item["id"])))
    per_counts: Counter[str] = Counter()
    capped: list[dict[str, Any]] = []
    for item in ordered:
        category = str(item["category"])
        if per_counts[category] >= per_category:
            continue
        per_counts[category] += 1
        capped.append(item)
    shown = capped[:top_n]
    for rank, item in enumerate(shown, start=1):
        item["rank"] = rank

    candidate_counts = Counter(str(item["category"]) for item in unique.values())
    shown_counts = Counter(str(item["category"]) for item in shown)
    omitted_by_category = {
        category: candidate_counts[category] - shown_counts.get(category, 0)
        for category in sorted(candidate_counts)
        if candidate_counts[category] - shown_counts.get(category, 0) > 0
    }
    omitted_total = len(unique) - len(shown)
    status = ("unavailable" if not graph_info.get("available")
              else "degraded" if (not graph_info.get("usable")
                                  or graph_info.get("empty"))
              else "ok")
    summary = {
        "opportunities": len(shown),
        "candidates": len(unique),
        "by_category": dict(sorted(shown_counts.items())),
        "high_confidence": sum(
            1 for item in shown if item.get("confidence") == "high"),
        "effort": dict(sorted(Counter(
            str(item.get("effort")) for item in shown).items())),
        "risk": dict(sorted(Counter(
            str(item.get("risk")) for item in shown).items())),
        "omitted": omitted_total,
    }
    report = {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "advisory": True,
        "truth_assessed": False,
        "status": status,
        "root": str(root_path),
        "graph": dict(graph_info),
        "inputs": {
            "since_days": since,
            "top": top_n,
            "per_category_cap": per_category,
            "head": head,
            "working_tree_dirty": bool(dirty),
            "changed_count": len(changed),
            "changed_files": changed[:50],
            "measurements": {
                "clutter_directories": len(ctx.clutter),
                "clutter_bytes": sum(
                    _safe_int(item.get("bytes")) for item in ctx.clutter),
                "clutter_truncated": any(
                    bool(item.get("truncated")) for item in ctx.clutter),
                "history_files": (
                    _safe_int(ctx.history.get("files"))
                    if ctx.history else 0),
                "history_old_files": (
                    _safe_int(ctx.history.get("old_files"))
                    if ctx.history else 0),
            },
            "categories": list(CATEGORY_IDS),
            "limits": {
                "max_graph_bytes": MAX_GRAPH_BYTES,
                "max_walk_files": MAX_WALK_FILES,
                "max_status_entries": MAX_STATUS_ENTRIES,
                "max_text_bytes": MAX_TEXT_BYTES,
                "max_pair_files": MAX_PAIR_FILES,
            },
        },
        "summary": summary,
        "omitted": {
            "total": omitted_total,
            "by_category": omitted_by_category,
            "cap": top_n,
            "per_category_cap": per_category,
        },
        "opportunities": shown,
        "not_assessed": _global_not_assessed(ctx),
        "warnings": list(dict.fromkeys(warnings)),
    }
    return report


def report_json(report: Mapping[str, Any]) -> str:
    """Deterministic JSON for a scan report."""
    return _report_json(report)


def atomic_write(path: str | Path, text: str) -> None:
    """Write a report atomically; the caller chose the path."""
    import tempfile
    try:
        target = Path(path).expanduser()
    except (OSError, RuntimeError) as exc:
        raise RepoScanError(f"cannot resolve output path {path!r}: {exc}") from exc
    parent = target.parent
    if not parent.is_dir():
        raise RepoScanError(f"output directory does not exist: {parent}")
    handle: int | None = None
    tmp: str | None = None
    try:
        handle, tmp = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".tmp", dir=str(parent))
        with os.fdopen(handle, "w", encoding="utf-8", newline="") as stream:
            handle = None
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, target)
        tmp = None
    except OSError as exc:
        raise RepoScanError(f"cannot write {target}: {exc}") from exc
    finally:
        if handle is not None:
            os.close(handle)
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass


__all__ = [
    "ADVISORY",
    "CATEGORIES",
    "CATEGORY_IDS",
    "DEFAULT_PER_CATEGORY",
    "DEFAULT_SINCE_DAYS",
    "DEFAULT_TOP",
    "RepoScanError",
    "SCHEMA",
    "atomic_write",
    "format_categories",
    "format_report",
    "report_json",
    "scan",
]
