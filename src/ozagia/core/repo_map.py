"""ozagia.core.repo_map — ranked, token-budgeted repo maps and impact queries.

This is the graph/tree enhancement distilled from the current coding-agent
landscape (Aider's repo map, RepoGraph, CodexGraph, Codebase-Memory, arbor,
context-link, Sourcegraph/SCIP):

  * tree-sitter `tags.scm` extracts definitions and references per file;
  * files are nodes in a weighted reference graph (referencer -> definer);
  * personalized PageRank ranks files for the current task;
  * a token budget selects a three-tier map: full outlines for the hottest
    files, public symbols for the middle, a file listing for the tail;
  * the tag cache is content-hash incremental (Merkle-like file sync), so
    unchanged files are never re-parsed.

The map is a reading aid; it never edits. The canonical edit path is the op
list in `ozagia.edit.dispatch`. The scanned directories come from the project
configuration (`map_dirs`, `skip_dirs`; see `ozagia.config`).
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import json
import math
import os
import re
import tempfile
import time
from collections import OrderedDict, defaultdict, deque
from pathlib import Path
from typing import Any, Iterable
from ozagia.config import (GENERIC_SKIP_DIRS, load_project_config, repo_root,
                           state_path, state_rel)

ROOT = repo_root()
CACHE_VERSION = 5
# Process-local memos. The document memo keeps parsed tag caches keyed by
# path and validated by (mtime_ns, size); the graph memo keeps reference
# graphs and derived indexes keyed by the cache view's content.
DOC_MEMO_SIZE = 4
GRAPH_MEMO_SIZE = 4
# Cold extraction runs in a process pool once this many files need parsing.
PARALLEL_MIN_FILES = 64
PARALLEL_CHUNK = 64
INDEX_WORKERS_ENV = "OZAGIA_INDEX_WORKERS"
DEFAULT_INDEX_WORKERS = 4
# A file whose mtime is within this window of the moment it was hashed may
# change again without a visible size or mtime change at the same second
# (racy-git rule). Such entries are re-hashed instead of trusted by mtime.
RACY_NS = 2_000_000_000
from ozagia.index.tag_languages import (  # noqa: E402
    LANGUAGES as _TAG_LANGUAGES,
    extract_tags as _registry_extract_tags,
    extract_tags_status,
    extractor_fingerprint,
)
LANGUAGES = {suffix: entry["module"]
             for suffix, entry in _TAG_LANGUAGES.items()
             if entry.get("module")}
# Compatibility aliases for callers that imported these historical constants.
# Scans always load configuration for the root passed to _source_files.
SOURCE_DIRS = (".",)
SKIP_PARTS = set(GENERIC_SKIP_DIRS)
MAX_FILE_BYTES = 2 * 1024 * 1024


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _rel(root: Path, path: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def _skipped(rel: str, skip_parts: set[str]) -> bool:
    return any(part in skip_parts for part in rel.split("/"))


def _source_files(root: Path) -> list[str]:
    """Repo-relative source files under this root's configured map_dirs.

    Same result as the earlier ``sorted(base.rglob("*"))`` walk, without a
    ``resolve()`` per file:

    * directories are walked with ``os.scandir``; directory symlinks are not
      followed, and directories named in SKIP_PARTS are not entered;
    * entries within one SOURCE_DIR are ordered by their path parts, which is
      the ordering ``Path`` comparison gives;
    * a regular file's rel is the SOURCE_DIR's resolved prefix plus its walked
      parts. That equals ``resolve()`` because no walked directory is a
      symlink;
    * a symlinked file is resolved, and kept only when its target is under
      root. Its rel is the target's rel, as before.
    """
    config = load_project_config(root)
    source_dirs = config.map_dirs
    skip_parts = set(config.skip_dirs)
    root_real = Path(root).resolve()
    srel = state_rel(root_real)
    out: list[str] = []
    for d in source_dirs:
        base = Path(root) / d if d not in ("", ".") else Path(root)
        if not base.exists():
            continue
        try:
            prefix: tuple[str, ...] | None = base.resolve().relative_to(
                root_real).parts
        except ValueError:
            prefix = None
        found: list[tuple[tuple[str, ...], str, bool]] = []
        stack: list[tuple[str, tuple[str, ...]]] = [(str(base), ())]
        while stack:
            current, parts = stack.pop()
            try:
                with os.scandir(current) as it:
                    entries = list(it)
            except OSError:
                continue
            for entry in entries:
                child_parts = parts + (entry.name,)
                try:
                    if entry.is_dir(follow_symlinks=False):
                        if entry.name not in skip_parts:
                            stack.append((entry.path, child_parts))
                        continue
                    if not entry.is_file():
                        continue
                except OSError:
                    continue
                if Path(entry.name).suffix not in LANGUAGES:
                    continue
                found.append((child_parts, entry.path, entry.is_symlink()))
        found.sort(key=lambda item: item[0])
        for child_parts, abs_path, is_link in found:
            if is_link:
                try:
                    rel = Path(abs_path).resolve().relative_to(
                        root_real).as_posix()
                except (ValueError, OSError, RuntimeError):
                    continue
            elif prefix is None:
                continue
            else:
                rel = "/".join(prefix + child_parts)
            if _skipped(rel, skip_parts):
                continue
            if srel and (rel == srel or rel.startswith(srel + "/")):
                continue
            try:
                if os.stat(abs_path).st_size > MAX_FILE_BYTES:
                    continue
            except OSError:
                continue
            out.append(rel)
    return out


def _grammar(language: str):
    import importlib
    return importlib.import_module(LANGUAGES[language])


def _tag_query_path(grammar_module) -> Path | None:
    p = Path(grammar_module.__file__).resolve().parent / "queries" / "tags.scm"
    return p if p.exists() else None


_GENERIC_SYMBOLS = {
    "new", "default", "eq", "ne", "hash", "fmt", "drop", "clone", "from",
    "into", "as_ref", "as_mut", "deref", "index", "next", "add", "sub",
    "mul", "div", "shl", "shr", "bitand", "bitor", "bitxor", "not", "neg",
}

_GENERIC_IDENTS = {
    "get", "set", "data", "name", "value", "self", "init", "main", "run",
    "test", "read", "write", "load", "save", "open", "close", "update",
    "create", "delete", "true", "false", "none", "str", "int", "list",
    "dict", "set", "len", "type", "object", "result", "args", "kwargs",
    "item", "items", "key", "keys", "path", "file", "node", "nodes", "id",
}


def extract_tags(path: str | Path, source: str) -> list[dict[str, Any]]:
    """Definition/reference tags via the shared multi-language registry.

    The registry (`ozagia.index.tag_languages`) is the only extractor: Python on the
    stdlib-AST fast path, other languages on each grammar's `queries/tags.scm`.
    A missing grammar yields an empty list for that file, never an exception.
    """
    return _registry_extract_tags(path, source)


# key -> (st_mtime_ns, st_size, document). Documents here are shared with
# callers of _load_cache, so they must never be mutated in place.
_DOC_MEMO: dict[str, tuple[int, int, dict[str, Any]]] = {}


def _remember_doc(key: str, st: os.stat_result, data: dict[str, Any]) -> None:
    _DOC_MEMO.pop(key, None)
    _DOC_MEMO[key] = (st.st_mtime_ns, st.st_size, data)
    while len(_DOC_MEMO) > DOC_MEMO_SIZE:
        _DOC_MEMO.pop(next(iter(_DOC_MEMO)))


def _load_cache(path: Path) -> dict[str, Any]:
    empty = {"version": CACHE_VERSION, "files": {}}
    key = str(path.resolve())
    try:
        st = path.stat()
    except OSError:
        return empty
    hit = _DOC_MEMO.get(key)
    if hit is not None and hit[0] == st.st_mtime_ns and hit[1] == st.st_size:
        return hit[2]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return empty
    if (isinstance(data, dict) and data.get("version") == CACHE_VERSION
            and isinstance(data.get("files"), dict)):
        _remember_doc(key, st, data)
        return data
    return empty


def _save_cache(path: Path, data: dict[str, Any]) -> None:
    """Atomic write: a unique temp file in the target directory, then rename.

    The temp file's stat is taken before the rename. A rename keeps inode,
    size and mtime, so that stat describes the file now at ``path`` without a
    second stat that a concurrent writer could race.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent),
                               prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(data, separators=(",", ":")))
        st = os.stat(tmp)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    _remember_doc(str(path.resolve()), st, data)


def _root_hash(entries: dict[str, Any], rels: Iterable[str]) -> str:
    text = "|".join(f"{rel}:{entries[rel].get('sha256', '')}"
                    for rel in sorted(rels))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _entry_is_fresh(old: Any, st: os.stat_result, fingerprint: str) -> bool:
    """True when mtime and size match and the entry is outside the racy window."""
    if not isinstance(old, dict) or old.get("fp") != fingerprint:
        return False
    if old.get("mtime_ns") != st.st_mtime_ns or old.get("size") != st.st_size:
        return False
    try:
        return int(old["checked_ns"]) - int(old["mtime_ns"]) >= RACY_NS
    except (KeyError, TypeError, ValueError):
        return False


def update_tag_cache(root: str | Path = ROOT, *,
                     cache_path: str | Path | None = None,
                     files: Iterable[str] | None = None,
                     force: bool = False) -> tuple[dict[str, Any], dict[str, Any]]:
    """Content-hash incremental tag cache. Returns (cache, stats).

    The on-disk document holds every entry. A full scan (``files`` is None)
    also prunes entries for files that no longer exist; a subset call never
    removes entries outside its subset. The returned ``cache`` is a view
    restricted to the requested or scanned paths, with its own ``root_hash``.
    """
    root = Path(root).resolve()
    cache_path = state_path(root, "tags.json") if cache_path is None else cache_path
    cache_path = Path(cache_path)
    existed = cache_path.exists()
    cache = _load_cache(cache_path)
    # Copy-on-write: the loaded document may be the memoised one. Entries
    # this call changes are replaced, never edited in place.
    entries: dict[str, Any] = dict(cache["files"])
    full_scan = files is None
    if full_scan:
        paths = _source_files(root)
    else:
        paths = list(dict.fromkeys(
            str(f).replace("\\", "/") for f in files))
    stats: dict[str, Any] = {"files": len(paths), "parsed": 0, "reused": 0,
                             "failed": 0, "errors": [], "parallel": 1}
    dirty = False

    def _fail(rel: str, message: str) -> None:
        nonlocal dirty
        stats["failed"] += 1
        if len(stats["errors"]) < 5:
            stats["errors"].append(f"{rel}: {message}")
        if entries.pop(rel, None) is not None:
            dirty = True

    # Phase 1: stat every path. Fresh entries are decided by stat alone and
    # are never read. The rest need a read, a hash and maybe an extraction.
    stat_of: dict[str, os.stat_result | None] = {}
    fresh: set[str] = set()
    fingerprint_of: dict[str, str] = {}
    todo: list[tuple[str, str | None]] = []
    for rel in paths:
        try:
            st = (root / rel).stat()
        except OSError:
            stat_of[rel] = None
            continue
        stat_of[rel] = st
        fingerprint = extractor_fingerprint(rel)
        fingerprint_of[rel] = fingerprint
        old = entries.get(rel)
        if not force and _entry_is_fresh(old, st, fingerprint):
            fresh.add(rel)
            continue
        old_sha = None
        if isinstance(old, dict) and old.get("fp") == fingerprint:
            old_sha = old.get("sha256")
        todo.append((rel, old_sha))
    outcomes = _scan_files(root, todo, force, stats)

    # Phase 2: apply the outcomes in path order.
    for rel in paths:
        st = stat_of[rel]
        if st is None:
            _fail(rel, "file not found")
            continue
        if rel in fresh:
            stats["reused"] += 1
            continue
        digest, reuse, tags, error = outcomes[rel]
        if digest is None:
            _fail(rel, error or "read failed")
            continue
        now = time.time_ns()
        if reuse:
            entries[rel] = {**entries[rel], "mtime_ns": st.st_mtime_ns,
                            "size": st.st_size, "checked_ns": now}
            stats["reused"] += 1
            dirty = True
            continue
        if error is not None:
            # Failures are never cached: the next call tries the file again.
            _fail(rel, error)
            continue
        entries[rel] = {
            "sha256": digest,
            "mtime_ns": st.st_mtime_ns,
            "size": st.st_size,
            "checked_ns": now,
            "fp": fingerprint_of[rel],
            "tags": tags,
        }
        stats["parsed"] += 1
        dirty = True
    if full_scan:
        known = set(paths)
        for rel in list(entries):
            if rel not in known:
                entries.pop(rel, None)
                dirty = True
    view_rels = [rel for rel in dict.fromkeys(paths) if rel in entries]
    view = {rel: entries[rel] for rel in view_rels}
    view_hash = _root_hash(entries, view_rels)
    stored_hash = _root_hash(entries, entries)
    document = {"version": CACHE_VERSION, "files": entries,
                "root": str(root), "root_hash": stored_hash,
                "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    if stats["parsed"] or dirty or force or not existed:
        _save_cache(cache_path, document)
    stats["root_hash"] = view_hash
    stats["cached_files"] = len(entries)
    result = {"version": CACHE_VERSION, "files": view,
              "root": str(root), "root_hash": view_hash}
    return result, stats


def _scan_one(root: str, rel: str, old_sha: str | None,
              force: bool) -> tuple[str, str | None, bool, list | None,
                                    str | None]:
    """Read, hash and, when the content changed, extract one file.

    Returns (rel, digest, reuse, tags, error). ``digest`` is None when the
    read failed. ``reuse`` is True when the content hash matches the cached
    entry, so no extraction ran. Top-level so a process pool can run it.
    """
    try:
        source = (Path(root) / rel).read_text(encoding="utf-8",
                                              errors="replace")
    except OSError as exc:
        return rel, None, False, None, f"read failed: {exc}"
    digest = _sha256(source)
    if not force and old_sha is not None and old_sha == digest:
        return rel, digest, True, None, None
    tags, error = extract_tags_status(rel, source)
    return rel, digest, False, tags, error


def _scan_chunk(root: str, items: list[tuple[str, str | None]],
                force: bool) -> list[tuple[str, str | None, bool, list | None,
                                           str | None]]:
    return [_scan_one(root, rel, old_sha, force) for rel, old_sha in items]


def _index_workers() -> int:
    raw = os.environ.get(INDEX_WORKERS_ENV, str(DEFAULT_INDEX_WORKERS))
    try:
        requested = int(raw)
    except ValueError:
        requested = DEFAULT_INDEX_WORKERS
    return max(1, min(requested, os.cpu_count() or 1))


def _scan_files(root: Path, todo: list[tuple[str, str | None]], force: bool,
                stats: dict[str, Any]) -> dict[str, tuple[Any, ...]]:
    """Run `_scan_one` for every file that needs a read, serially or pooled.

    Both paths call the same functions, so the cache document does not depend
    on the worker count. A pool that cannot start, or breaks, falls back to
    the serial path and records the reason in ``stats["parallel"]``.
    """
    workers = _index_workers()
    results = None
    if workers > 1 and len(todo) >= PARALLEL_MIN_FILES:
        try:
            chunks = [todo[i:i + PARALLEL_CHUNK]
                      for i in range(0, len(todo), PARALLEL_CHUNK)]
            with concurrent.futures.ProcessPoolExecutor(
                    max_workers=workers) as pool:
                futures = [pool.submit(_scan_chunk, str(root), chunk, force)
                           for chunk in chunks]
                results = [row for fut in futures for row in fut.result()]
            stats["parallel"] = workers
        except (OSError, concurrent.futures.BrokenExecutor) as exc:
            stats["parallel"] = f"fallback: {type(exc).__name__}: {exc}"
            results = None
    if results is None:
        results = [_scan_one(str(root), rel, old_sha, force)
                   for rel, old_sha in todo]
    return {rel: (digest, reuse, tags, error)
            for rel, digest, reuse, tags, error in results}


def build_reference_graph(cache: dict[str, Any]) -> dict[str, Any]:
    """defines[name] -> files; references[name] -> files; edges referencer->definer.

    Iteration is over sorted names and files, and the returned edges are sorted
    by (src, tgt), so the caps and the output do not depend on set order.
    """
    defines: dict[str, set[str]] = defaultdict(set)
    references: dict[str, set[str]] = defaultdict(set)
    definitions: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    import_edges: dict[tuple[str, str], float] = defaultdict(float)
    rust = None
    root = cache.get("root")
    files = cache.get("files") or {}
    if root and any(str(rel).endswith(".rs") for rel in files):
        try:
            from ozagia.core import rust_modules
            rust = rust_modules.discover(root)
        except Exception:
            rust = None
    for rel, entry in files.items():
        for tag in entry.get("tags") or []:
            name = str(tag.get("name") or "")
            if not name:
                continue
            if tag.get("kind") == "def":
                defines[name].add(rel)
                definitions[(rel, name)].append(tag)
            elif tag.get("kind") == "ref":
                references[name].add(rel)
            elif (rust is not None and tag.get("kind") == "import"
                  and tag.get("detail") == "import.use"):
                target = rust.import_file(rel, tag)
                if target and target != rel:
                    import_edges[(rel, target)] += 1.0
    edges: dict[tuple[str, str], float] = defaultdict(float)
    edges.update(import_edges)
    MAX_DEFINERS = 12
    MAX_REFERENCERS = 120
    MAX_EDGES = 250_000
    for name in sorted(set(defines) & set(references)):
        if (name in _GENERIC_IDENTS or len(name) < 4
                or len(defines[name]) > MAX_DEFINERS
                or len(references[name]) > MAX_REFERENCERS):
            continue
        for src in sorted(references[name])[:MAX_REFERENCERS]:
            for dst in sorted(defines[name])[:MAX_DEFINERS]:
                if src == dst:
                    continue
                edges[(src, dst)] += 1.0
                if len(edges) >= MAX_EDGES:
                    break
            if len(edges) >= MAX_EDGES:
                break
        if len(edges) >= MAX_EDGES:
            break
    # Self-edge for definitions with no references keeps isolated files alive.
    for name in sorted(defines):
        if name in references:
            continue
        for f in sorted(defines[name]):
            edges[(f, f)] = max(edges.get((f, f), 0.0), 0.1)
    ordered = sorted(edges.items())
    return {"defines": defines, "references": references,
            "definitions": definitions,
            "edges": [{"src": s, "tgt": t, "weight": w}
                      for (s, t), w in ordered]}



def _task_terms(task: str) -> list[str]:
    raw = re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", (task or "").lower())
    stop = {"the", "and", "for", "with", "from", "into", "this", "that",
            "add", "fix", "use", "make", "new", "code", "file", "test"}
    return [t for t in raw if t not in stop]


# Issue templates, URLs and code samples carry many repeated words ("python",
# "version", "example") that are not task evidence. De-duplicating and dropping
# them stops a long boilerplate paragraph from outweighing the one identifier
# the issue is actually about.
_CONTENT_STOP = {
    "the", "and", "for", "with", "from", "into", "this", "that", "add",
    "fix", "use", "make", "new", "code", "file", "test", "when", "you",
    "not", "need", "are", "does", "can", "has", "have", "was", "were",
    "will", "would", "should", "could", "them", "they", "their", "there",
    "here", "also", "but", "however", "please", "sure", "comments",
    "submit", "issue", "hidden", "remove", "contributing", "guidelines",
    "github", "www", "org", "https", "http", "actual", "behavior",
    "behaviour", "description", "steps", "reproduce", "output", "version",
    "following", "example", "expected", "value", "values", "import",
    "from", "class", "def", "return", "self", "none", "true", "false",
    "object", "string", "print", "assert", "lambda", "const", "function",
    "module", "python",
}


def _content_task_terms(task: str) -> list[str]:
    """Task terms for content-aware ranking, without template noise.

    HTML comments, URLs and duplicate words are removed. Code-block
    identifiers are kept: they often name the failing API.
    """
    text = re.sub(r"<!--.*?-->", " ", task or "", flags=re.S)
    text = re.sub(r"https?://\S+", " ", text)
    raw = re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", text.lower())
    seen: set[str] = set()
    terms: list[str] = []
    for term in raw:
        if term in _CONTENT_STOP or term in seen:
            continue
        seen.add(term)
        terms.append(term)
    return terms


_AUXILIARY_SEGMENTS = ("/tests/", "/test/", "/testing/", "/docs/",
                       "/doc/", "/examples/", "/example/", "/vendor/",
                       "/extern/", "/third_party/", "/thirdparty/")


def _is_auxiliary_path(path: str) -> bool:
    """Whether a path is test/documentation/vendored support code."""
    low = "/" + str(path).lower().lstrip("/")
    name = low.rsplit("/", 1)[-1]
    if name.startswith("test_") or name.endswith("_test.py"):
        return True
    return any(segment in low for segment in _AUXILIARY_SEGMENTS)


def _file_names(graph: dict[str, Any]) -> tuple[dict[str, set[str]],
                                                dict[str, set[str]]]:
    file_defs: dict[str, set[str]] = defaultdict(set)
    file_refs: dict[str, set[str]] = defaultdict(set)
    for name, files in (graph.get("defines") or {}).items():
        low = str(name).lower()
        for f in files:
            file_defs[f].add(low)
    for name, files in (graph.get("references") or {}).items():
        low = str(name).lower()
        for f in files:
            file_refs[f].add(low)
    return file_defs, file_refs


class _NameIndex:
    """Per-file name blobs and the sorted edge list used by relevance.

    A blob is the file's lower-cased names joined by NUL. Task terms are
    ``[a-z0-9_]`` only, so ``term in blob`` is true exactly when some name
    contains the term. This matches ``any(term in name for name in names)``.
    """

    def __init__(self, file_defs: dict[str, set[str]],
                 file_refs: dict[str, set[str]],
                 nodes: set[str], sorted_edges: list[dict[str, Any]],
                 defs_blob: dict[str, str], refs_blob: dict[str, str],
                 names_blob: dict[str, str]) -> None:
        self.file_defs = file_defs
        self.file_refs = file_refs
        self.nodes = nodes
        self.sorted_edges = sorted_edges
        self.defs_blob = defs_blob
        self.refs_blob = refs_blob
        self.names_blob = names_blob


def _name_index(graph: dict[str, Any]) -> _NameIndex:
    file_defs, file_refs = _file_names(graph)
    sorted_edges = sorted(graph.get("edges") or [],
                          key=lambda e: (str(e["src"]), str(e["tgt"])))
    nodes = set(file_defs) | set(file_refs)
    for e in sorted_edges:
        nodes.add(str(e["src"]))
        nodes.add(str(e["tgt"]))
    defs_blob = {f: "\x00".join(sorted(names))
                 for f, names in file_defs.items()}
    refs_blob = {f: "\x00".join(sorted(names))
                 for f, names in file_refs.items()}
    names_blob = {f: "\x00".join(sorted(file_defs.get(f, set())
                                        | file_refs.get(f, set())))
                  for f in set(file_defs) | set(file_refs)}
    return _NameIndex(file_defs, file_refs, nodes, sorted_edges,
                      defs_blob, refs_blob, names_blob)


def _task_relevance(graph: dict[str, Any], task: str,
                    focus_files: Iterable[str],
                    index: _NameIndex | None = None,
                    *, content_aware: bool = False) -> tuple[dict[str, float],
                                                             set[str]]:
    """Term-weighted relevance + a candidate subgraph.

    The candidate restriction is what stops a globally central file (e.g. a
    py.rs with hundreds of references) from dominating every task. Terms are
    weighted by inverse file frequency; the candidates are the top matching
    files plus their one-hop reference neighbours.

    With ``content_aware`` the score distinguishes evidence quality: an exact
    definition name or path segment is worth much more than a substring hit,
    and test/documentation/vendored files are demoted. This stops a long issue
    template that repeats generic words from outranking the one file that
    defines the identifier named in the title.
    """
    idx = index if index is not None else _name_index(graph)
    terms = _content_task_terms(task) if content_aware else _task_terms(task)
    nodes = idx.nodes
    if not nodes:
        return {}, set()
    scores: dict[str, float] = {}
    if terms:
        n = max(1, len(nodes))
        df = {t: 0 for t in terms}
        for f in nodes:
            lowpath = f.lower()
            names_blob = idx.names_blob.get(f, "")
            for t in terms:
                if t in lowpath or t in names_blob:
                    df[t] += 1
        weight = {t: math.log((n + 1) / (df[t] + 1)) + 1.0 for t in terms}
        for f in nodes:
            lowpath = f.lower()
            defs_blob = idx.defs_blob.get(f, "")
            refs_blob = idx.refs_blob.get(f, "")
            if content_aware:
                defs = idx.file_defs.get(f, set())
                segments = set(re.split(r"[/_.\-]", lowpath))
                score = 0.0
                for t in terms:
                    signal = 0.0
                    if t in segments:
                        signal = 6.0
                    elif t in lowpath:
                        signal = 2.0
                    if t in defs:
                        signal = 10.0
                    elif t in defs_blob:
                        signal = max(signal, 4.0)
                    if t in refs_blob:
                        signal = max(signal, 1.0)
                    if signal:
                        score += signal * weight[t]
            else:
                score = 0.0
                for t in terms:
                    if t in lowpath:
                        score += 3.0 * weight[t]
                    if t in defs_blob:
                        score += 4.0 * weight[t]
                    if t in refs_blob:
                        score += 1.5 * weight[t]
            if score > 0:
                if content_aware and _is_auxiliary_path(f):
                    score *= 0.5
                elif f.startswith("tests/"):
                    score *= 0.35
                scores[f] = score
    for f in focus_files:
        scores[str(f)] = scores.get(str(f), 0.0) + 100.0
    if not scores:
        return {f: 1.0 for f in nodes}, set(nodes)
    top = sorted(scores, key=lambda f: (-scores[f], f))[:30]
    top_set = set(top)
    candidates = set(top)
    for e in idx.sorted_edges:
        src, tgt = str(e["src"]), str(e["tgt"])
        if src not in top_set:
            continue
        candidates.add(src)
        candidates.add(tgt)
        if len(candidates) >= 300:
            break
    return scores, candidates


PPR_TOLERANCE = 1e-10
PPR_MAX_ITERATIONS = 100

# Confidence-weighted rank-fusion horizons. A candidate's contribution decays
# exponentially with its rank in each signal; the horizons say how many
# positions that signal is trusted over. Lexical search is noisy past its
# first handful of hits, so its horizon is shorter than the graph's.
GRAPH_RANK_HORIZON = 20.0
LEXICAL_RANK_HORIZON = 8.0
GRAPH_FUSION_WEIGHT = 0.60
LEXICAL_FUSION_WEIGHT = 0.40
# Stop rendering candidates below this fraction of the top fused score, and
# never render more than this many. This keeps the map a confident reading
# list instead of an 80-90 path dump.
CONTENT_TAIL_RATIO = 0.18
CONTENT_MAX_FILES = 32
# Below this share of the top fused score, a file is kept only when it adds
# task evidence not already covered by the files above it. High-confidence
# files are always kept; low-confidence duplicates are redundant context.
# Small budgets have fewer slots, so they use the stricter threshold.
CONTENT_MIN_CONFIDENCE = 0.30
CONTENT_MIN_CONFIDENCE_TIGHT = 0.35


def personalized_pagerank(graph: dict[str, Any], *,
                          task: str = "",
                          focus_files: Iterable[str] = (),
                          damping: float | None = None,
                          iterations: int = PPR_MAX_ITERATIONS,
                          prepared_relevance: tuple[dict[str, float],
                                                    set[str]] | None = None,
                          ) -> dict[str, float]:
    """Personalized PageRank over the candidate subgraph.

    Mass at dangling nodes (no out-weight inside the subgraph) is handed back
    through the personalization vector, so the ranks sum to 1. Iteration stops
    when the L1 change drops below PPR_TOLERANCE or after `iterations` rounds.
    `prepared_relevance` is the (relevance, candidates) pair from
    `_task_relevance`, passed in when the caller already computed it.
    """
    if prepared_relevance is None:
        relevance, candidates = _task_relevance(graph, task, focus_files)
    else:
        relevance, candidates = prepared_relevance
    nodes = sorted(candidates)
    if not nodes:
        return {}
    edges = sorted(
        (e for e in (graph.get("edges") or [])
         if str(e["src"]) in candidates and str(e["tgt"]) in candidates),
        key=lambda e: (str(e["src"]), str(e["tgt"])))
    d = 0.30 if task.strip() else 0.85
    if damping is not None:
        d = float(damping)
    terms = _task_terms(task)
    personal: dict[str, float] = {}
    for n in nodes:
        w = 1.0 + 20.0 * relevance.get(n, 0.0)
        for t in terms:
            if t in n.lower():
                w += 2.0
        if n.startswith("tests/") and task.strip():
            w *= 0.5
        personal[n] = w
    total = sum(personal[n] for n in nodes) or 1.0
    teleport = {n: personal[n] / total for n in nodes}
    rank = dict(teleport)
    out_weight: dict[str, float] = defaultdict(float)
    for e in edges:
        out_weight[str(e["src"])] += float(e.get("weight", 1.0))
    dangling = [n for n in nodes if out_weight.get(n, 0.0) <= 0.0]
    for _ in range(max(1, int(iterations))):
        dangling_mass = sum(rank[n] for n in dangling)
        new = {n: (1.0 - d) * teleport[n] + d * dangling_mass * teleport[n]
               for n in nodes}
        for e in edges:
            src, tgt = str(e["src"]), str(e["tgt"])
            w = float(e.get("weight", 1.0))
            if out_weight.get(src, 0.0) > 0:
                new[tgt] += d * rank[src] * w / out_weight[src]
        delta = sum(abs(new[n] - rank[n]) for n in nodes)
        rank = new
        if delta < PPR_TOLERANCE:
            break
    return dict(sorted(rank.items(), key=lambda kv: (-kv[1], kv[0])))



class _Prepared:
    """Everything derived from one reference graph, built once per content."""

    def __init__(self, graph: dict[str, Any]) -> None:
        self.graph = graph
        self.index = _name_index(graph)
        self.symbols_by_file = _symbols_by_file(graph)
        self.reverse, self.forward = _adjacency(graph)
        self._prior: dict[str, float] | None = None

    def prior(self) -> dict[str, float]:
        """Task-independent PageRank, memoised with the graph."""
        if self._prior is None:
            relevance = _task_relevance(self.graph, "", (), index=self.index)
            self._prior = personalized_pagerank(
                self.graph, task="", prepared_relevance=relevance)
        return self._prior


# key -> _Prepared, least recently used first. The key is the cache view's
# root hash plus extractor fingerprints, because root_hash covers file content
# but not the fingerprints that decide which tags exist.
_GRAPH_MEMO: "OrderedDict[str, _Prepared]" = OrderedDict()


def clear_memos() -> None:
    """Drop the document and graph memos. Tests use this between fixtures."""
    _DOC_MEMO.clear()
    _GRAPH_MEMO.clear()


def _graph_key(cache: dict[str, Any]) -> str:
    files = cache.get("files") or {}
    fp_text = "|".join(f"{rel}:{(files[rel] or {}).get('fp', '')}"
                       for rel in sorted(files))
    fp_digest = hashlib.sha256(fp_text.encode("utf-8")).hexdigest()
    return f"{cache.get('root', '')}|{cache.get('root_hash', '')}|{fp_digest}"


def _prepared(cache: dict[str, Any]) -> _Prepared:
    """Memoised preparation for a cache view, without SCIP edges."""
    key = _graph_key(cache)
    hit = _GRAPH_MEMO.get(key)
    if hit is not None:
        _GRAPH_MEMO.move_to_end(key)
        return hit
    prepared = _Prepared(build_reference_graph(cache))
    _GRAPH_MEMO[key] = prepared
    while len(_GRAPH_MEMO) > GRAPH_MEMO_SIZE:
        _GRAPH_MEMO.popitem(last=False)
    return prepared


def _prepared_with_edges(cache: dict[str, Any],
                         extra_edges: list[dict[str, Any]]) -> _Prepared:
    """Uncached preparation with extra (SCIP) edges appended to the graph."""
    graph = build_reference_graph(cache)
    graph["edges"] = list(graph.get("edges") or []) + extra_edges
    return _Prepared(graph)


def graph_prior(cache: dict[str, Any]) -> dict[str, float]:
    """Query-independent PageRank for a cache view (memoised)."""
    return _prepared(cache).prior()


def _symbols_by_file(graph: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    symbols_by_file: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for (f, name), tags in (graph.get("definitions") or {}).items():
        for tag in tags:
            symbols_by_file[f].append({"name": name,
                                        "line": tag.get("line", 0),
                                        "detail": tag.get("detail", "")})
    for f in symbols_by_file:
        seen = set()
        unique = []
        for sym in symbols_by_file[f]:
            key = (sym["name"], sym["line"])
            if key in seen:
                continue
            seen.add(key)
            unique.append(sym)
        symbols_by_file[f] = sorted(unique, key=lambda x: (x["line"], x["name"]))
    return dict(symbols_by_file)


def _adjacency(graph: dict[str, Any]) -> tuple[dict[str, set[str]],
                                               dict[str, set[str]]]:
    reverse: dict[str, set[str]] = defaultdict(set)
    forward: dict[str, set[str]] = defaultdict(set)
    for e in graph["edges"]:
        src, tgt = str(e["src"]), str(e["tgt"])
        if src != tgt:
            reverse[tgt].add(src)
            forward[src].add(tgt)
    return reverse, forward


def _line_text(root: Path, rel: str, line: int) -> str:
    return _line_text_cached(root, rel, line, {})


def _line_text_cached(root: Path, rel: str, line: int,
                      cache: dict[str, list[str]]) -> str:
    if rel not in cache:
        try:
            cache[rel] = (root / rel).read_text(
                encoding="utf-8", errors="replace").splitlines()
        except OSError:
            cache[rel] = []
    lines = cache[rel]
    if 1 <= line <= len(lines):
        return lines[line - 1].strip()
    return ""


def _symbols_for(graph: dict[str, Any], rel: str) -> list[dict[str, Any]]:
    out = []
    for (f, name), tags in (graph.get("definitions") or {}).items():
        if f != rel:
            continue
        for tag in tags:
            out.append({"name": name, "line": tag.get("line", 0),
                        "detail": tag.get("detail", "")})
    out.sort(key=lambda x: (x["line"], x["name"]))
    return out


def _scip_edges(root: Path, scip_path: str | Path | None) -> list[dict[str, Any]]:
    if not scip_path:
        return []
    try:
        from ozagia.index.scip import scip_import
        payload = scip_import(scip_path, root=root)
        return list(payload.get("edges") or [])
    except Exception:
        return []


def _content_rank(root: Path, task: str,
                  cache_path: str | Path | None
                  ) -> tuple[dict[str, float],
                             dict[str, tuple[int, str]]]:
    """Bounded lexical candidates and their best matching line.

    The repo-map graph knows paths and symbol names, but not what a file says.
    Reusing the existing BM25 search adds content evidence without a second
    lexical implementation. A missing/failed search is a silent fallback: the
    map still works from the graph alone.
    """
    try:
        from ozagia.core.search import code_search
        result = code_search(
            root, task, limit=80, cache_path=cache_path,
            include_snippet=True)
    except Exception:  # a map remains useful when search is unavailable
        return {}, {}
    if not isinstance(result, dict) or not result.get("ok"):
        return {}, {}
    scores: dict[str, float] = {}
    snippets: dict[str, tuple[int, str]] = {}
    for item in result.get("results") or []:
        if not isinstance(item, dict):
            continue
        path = item.get("path")
        score = item.get("score")
        if not isinstance(path, str) or not isinstance(score, (int, float)):
            continue
        scores[path] = float(score)
        line = item.get("line")
        snippet = item.get("snippet")
        if isinstance(line, int) and isinstance(snippet, str) and snippet:
            snippets[path] = (line, snippet)
    return scores, snippets


def repo_map(root: str | Path = ROOT, *, task: str = "",
             budget_tokens: int = 1500,
             cache_path: str | Path | None = None,
             focus_files: Iterable[str] = (),
             full_tier: int = 3, outline_tier: int = 12,
             force_refresh: bool = False,
             files: Iterable[str] | None = None,
             scip_path: str | Path | None = None,
             content_search: bool = True,
             min_confidence: float | None = None
             ) -> dict[str, Any]:
    """Ranked, token-budgeted repo map (Aider-style, graph-native).

    With a non-empty task and ``content_search=True``, bounded BM25 results
    from :mod:`ozagia.core.search` are fused with the graph/name relevance.
    This lets a file whose content matches the issue terms enter the candidate
    set even when its path and symbol names do not.

    ``min_confidence`` is the share of the top fused score below which a file
    must add new task evidence to be kept. High-confidence files are always
    kept; low-confidence duplicates are dropped. Set it to 0 to keep every
    candidate above the tail floor. ``None`` uses 0.35 for budgets below 700
    tokens and 0.30 above, because small budgets have fewer slots to spend on
    uncertain candidates.
    """
    root = Path(root).resolve()
    cache, stats = update_tag_cache(root, cache_path=cache_path,
                                    files=files,
                                    force=force_refresh)
    known_files = set((cache.get("files") or {}))
    focus_files = [str(f) for f in focus_files if str(f) in known_files]
    scip_edges = _scip_edges(root, scip_path)
    prep = (_prepared_with_edges(cache, scip_edges) if scip_edges
            else _prepared(cache))
    graph = prep.graph
    content_aware = bool(content_search and task.strip())
    relevance, candidates = _task_relevance(graph, task, focus_files,
                                            index=prep.index,
                                            content_aware=content_aware)
    lexical, lexical_snippets = (
        _content_rank(root, task, cache_path) if content_aware else ({}, {}))
    lexical = {path: score for path, score in lexical.items()
               if path in known_files}
    lexical_snippets = {path: snippet
                        for path, snippet in lexical_snippets.items()
                        if path in lexical}
    if lexical:
        # Content hits must be candidates before PageRank runs; otherwise the
        # candidate restriction would hide the very files search found.
        candidates.update(lexical)
    ranked = personalized_pagerank(graph, task=task,
                                   focus_files=focus_files,
                                   prepared_relevance=(relevance, candidates))
    if task.strip() and ranked:
        # PageRank supplies centrality; task relevance and BM25 content
        # evidence supply fit. Relevance leads more strongly when content
        # evidence is available because PageRank is query-independent.
        max_rel = max(relevance.values()) or 1.0
        max_rank = max(ranked.values()) or 1.0
        rel_weight = 0.80 if content_aware else 0.70
        if lexical:
            # Confidence-weighted rank fusion. Reciprocal-rank fusion's
            # 1/(k+rank) decays too slowly when the lexical list is much
            # longer than the graph list: an 80th lexical hit could outrank a
            # top-10 graph hit. An exponential horizon makes each list's tail
            # vanish, so a file must be near the top of at least one signal.
            graph_order = sorted(
                ranked,
                key=lambda path: (-(rel_weight * relevance.get(path, 0.0)
                                    / max_rel
                                    + (1.0 - rel_weight) * ranked[path]
                                    / max_rank), path))
            graph_rank = {path: index + 1
                          for index, path in enumerate(graph_order)}
            lexical_order = sorted(lexical,
                                   key=lambda path: (-lexical[path], path))
            lexical_rank = {path: index + 1
                            for index, path in enumerate(lexical_order)}
            fused = {}
            for path in set(graph_order) | set(lexical_order):
                score = 0.0
                if path in graph_rank:
                    score += GRAPH_FUSION_WEIGHT * math.exp(
                        -(graph_rank[path] - 1) / GRAPH_RANK_HORIZON)
                if path in lexical_rank:
                    score += LEXICAL_FUSION_WEIGHT * math.exp(
                        -(lexical_rank[path] - 1) / LEXICAL_RANK_HORIZON)
                fused[path] = score
            ranked = dict(sorted(fused.items(),
                                 key=lambda kv: (-kv[1], kv[0])))
            # The map is a reading aid, not an exhaustive path dump. Keep the
            # confident head and drop the flat tail; the old 80-90 file tail
            # added little recall and made precision collapse.
            floor = (max(ranked.values()) or 1.0) * CONTENT_TAIL_RATIO
            ranked = {path: score for path, score in ranked.items()
                      if score >= floor}
            if len(ranked) > CONTENT_MAX_FILES:
                ranked = dict(list(ranked.items())[:CONTENT_MAX_FILES])
        else:
            def _score(item: tuple[str, float]) -> float:
                rel, rank = item[0], item[1]
                return (rel_weight * relevance.get(rel, 0.0) / max_rel
                        + (1.0 - rel_weight) * rank / max_rank)
            ranked = dict(sorted(
                ranked.items(),
                key=lambda kv: (-_score(kv), kv[0])))
    budget = max(200, int(budget_tokens))
    if min_confidence is None:
        min_confidence = (CONTENT_MIN_CONFIDENCE_TIGHT if budget < 700
                          else CONTENT_MIN_CONFIDENCE)
    min_confidence = max(0.0, min(1.0, float(min_confidence)))
    if content_aware:
        # Lexical candidates tend to arrive as many medium-confidence files.
        # Keep one full body and more compact outlines, then spend the rest on
        # cheap listings so the confident head is shown with content.
        full_tier = min(full_tier, 1)
        outline_tier = min(max(outline_tier, 16), 20)
    full_tier = max(1, min(int(full_tier), max(1, budget // 300)))
    outline_tier = max(full_tier, min(int(outline_tier),
                                      max(2, budget // 80)))
    symbols_by_file = prep.symbols_by_file
    # A snippet costs more than a listing but far less than an outline. Spend
    # a bounded slice of the budget on the best lexical evidence so files just
    # outside the outline tier still arrive with a matching line. At 500
    # tokens there is no spare room: a snippet would displace a candidate and
    # cost more recall than the extra content returns.
    snippet_limit = (min(8, max(2, budget // 150))
                     if content_aware and budget >= 700 else 0)
    snippets_used = 0
    line_cache: dict[str, list[str]] = {}
    lines: list[str] = []
    used_files: list[dict[str, Any]] = []
    top_score = max((score for _, score in ranked.items()), default=1.0) or 1.0
    task_evidence = _content_task_terms(task) if content_aware else []
    seen_evidence: set[str] = set()
    suppressed = 0
    gate_low_confidence = bool(
        content_aware and lexical and min_confidence > 0)
    if task:
        # The task drives ranking, but the map header must not spend the file
        # budget on a long issue description. One bounded line is enough to
        # identify the map; the caller still has the full prompt.
        headline = " ".join(str(task).split())[:200]
        if len(" ".join(str(task).split())) > 200:
            headline += "..."
        lines.append(f"# Repo map: {headline}")
    else:
        lines.append("# Repo map")
    lines.append(f"# files: {len(ranked)}  cache: {stats['cached_files']} "
                 f"(parsed {stats['parsed']}, reused {stats['reused']})")
    lines.append("")
    for i, (rel, score) in enumerate(ranked.items()):
        syms = symbols_by_file.get(rel, [])
        rendered_symbols: list[str] = []
        if i < full_tier:
            tier = "full"
            body = [f"{rel}  score={score:.4f}"]
            shown = 0
            symbol_limit = 8 if content_aware else 16
            for s in syms:
                if shown >= symbol_limit:
                    break
                if (s["name"].lower() in _GENERIC_SYMBOLS
                        and len(syms) > 8):
                    continue
                line = f"  L{s['line']} {s['name']}"
                src = _line_text_cached(root, rel, int(s["line"]), line_cache)
                if src:
                    line += f"  | {src[:120]}"
                body.append(line)
                rendered_symbols.append(str(s["name"]))
                shown += 1
        elif i < outline_tier:
            tier = "outline"
            public = [s for s in syms
                      if not s["name"].startswith("_")
                      and s["name"].lower() not in _GENERIC_SYMBOLS]
            if not public:
                public = syms
            name_limit = 6 if content_aware else 12
            shown_symbols = public[:name_limit]
            rendered_symbols = [str(s["name"]) for s in shown_symbols]
            names = ", ".join(f"{s['name']}@L{s['line']}"
                              for s in shown_symbols)
            body = [f"{rel}  score={score:.4f}  {names}"]
        elif (snippets_used < snippet_limit
              and rel in lexical_snippets):
            tier = "snippet"
            line_no, text = lexical_snippets[rel]
            if len(text) > 140:
                text = text[:137].rstrip() + "..."
            body = [f"{rel}  score={score:.4f}  L{line_no}",
                    f"  {text}"]
            snippets_used += 1
        else:
            tier = "listing"
            body = [f"{rel}  ({len(syms)} symbols)"]
        body_low = "\n".join(body).lower()
        body_terms = {term for term in task_evidence if term in body_low}
        confidence = score / top_score
        if (gate_low_confidence and used_files
                and confidence < min_confidence
                and not (body_terms - seen_evidence)):
            # Below the confidence line, redundant context is not worth its
            # tokens. High-confidence files are always kept.
            if tier == "snippet":
                snippets_used -= 1
            suppressed += 1
            continue
        seen_evidence.update(body_terms)
        if not used_files:
            # Always return at least one file: trim a too-large first body
            # rather than returning an empty map.
            while body and len("\n".join(lines + body + [""])) // 4 > budget:
                body = body[:-1] or [body[0]]
                if len(body) == 1:
                    break
            lines.extend(body)
            lines.append("")
            entry = {"path": rel, "score": round(score, 6),
                     "tier": tier, "symbols": len(syms)}
            if content_aware:
                entry["confidence"] = round(confidence, 6)
                entry["evidence_symbols"] = sorted({
                    name for name in rendered_symbols
                    if any(term in name.lower() for term in task_evidence)
                })[:12]
            used_files.append(entry)
            continue
        candidate = "\n".join(lines + body + [""])
        if len(candidate) // 4 > budget:
            if tier == "snippet":
                # A snippet must not crowd out the rest of the tail; degrade
                # it to a listing and keep going.
                snippets_used -= 1
                tier = "listing"
                body = [f"{rel}  ({len(syms)} symbols)"]
                candidate = "\n".join(lines + body + [""])
            if len(candidate) // 4 > budget:
                break
        lines.extend(body)
        lines.append("")
        entry = {"path": rel, "score": round(score, 6),
                 "tier": tier, "symbols": len(syms)}
        if content_aware:
            entry["confidence"] = round(confidence, 6)
            entry["evidence_symbols"] = sorted({
                name for name in rendered_symbols
                if any(term in name.lower() for term in task_evidence)
            })[:12]
        used_files.append(entry)
    markdown = "\n".join(lines).strip() + "\n"
    result = {
        "ok": True,
        "task": task,
        "budget_tokens": budget,
        "estimated_tokens": max(1, len(markdown) // 4),
        "markdown": markdown,
        "files": used_files,
        "cache": stats,
        "graph": {"files": len(ranked),
                  "edges": len(graph["edges"]),
                  "scip_edges": len(scip_edges),
                  "definitions": sum(len(v) for v in graph["defines"].values())},
    }
    if content_aware:
        result["suppressed_files"] = suppressed
        result["evidence_terms"] = len(seen_evidence)
    return result


def impact(root: str | Path = ROOT, *, file: str, depth: int = 2,
           cache_path: str | Path | None = None,
           files: Iterable[str] | None = None,
           scip_path: str | Path | None = None) -> dict[str, Any]:
    """Transitive dependents of a file over the reference graph."""
    root = Path(root).resolve()
    cache, stats = update_tag_cache(root, cache_path=cache_path, files=files)
    scip_edges = _scip_edges(root, scip_path)
    prep = (_prepared_with_edges(cache, scip_edges) if scip_edges
            else _prepared(cache))
    reverse, forward = prep.reverse, prep.forward
    target = str(file).replace("\\", "/")
    if target not in (cache.get("files") or {}):
        return {"ok": False, "error": f"no indexed file {file!r}"}
    seen = {target}
    frontier: deque[tuple[str, int]] = deque([(target, 0)])
    impacted = []
    while frontier:
        cur, d = frontier.popleft()
        if d >= max(1, int(depth)):
            continue
        for nxt in sorted(reverse.get(cur, ())):
            if nxt in seen:
                continue
            seen.add(nxt)
            impacted.append({"path": nxt, "depth": d + 1})
            frontier.append((nxt, d + 1))
    return {"ok": True, "file": target, "depth": int(depth),
            "impacted": impacted, "n": len(impacted),
            "direct_dependencies": sorted(forward.get(target, ())),
            "scip_edges": len(scip_edges), "cache": stats}
