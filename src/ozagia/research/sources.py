"""Explicit, read-only source discovery for research sessions.

The index is deliberately small: callers name roots and selected files.  Every
returned passage carries a reopenable locator and a content hash; prose links
are retained as navigation hints and are never interpreted as support edges.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import subprocess
from typing import Any, Iterable, Mapping, Sequence

SCHEMA_VERSION = 1
DEFAULT_SELECTED_FILES = (
    "docs/SCIENCE_RESEARCH.md",
    "docs/features/RESEARCH_ASSISTANT_PLAN.md",
)


class SourceError(ValueError):
    """Invalid or unsafe source configuration/locator."""


@dataclass(frozen=True)
class RootManifest:
    root_id: str
    path: str | os.PathLike[str]
    files: Sequence[str]
    repository: str | None = None


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _safe_file(root: Path, relative: str) -> Path:
    rel = Path(relative)
    if rel.is_absolute() or ".." in rel.parts:
        raise SourceError(f"source path must stay inside its root: {relative}")
    candidate = root / rel
    try:
        resolved_root = root.resolve(strict=True)
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise FileNotFoundError(relative) from exc
    if not resolved.is_relative_to(resolved_root):
        raise SourceError(f"source path escapes root (including symlinks): {relative}")
    if not resolved.is_file():
        raise SourceError(f"source is not a regular file: {relative}")
    return resolved


def _git_state(root: Path, relative_paths: Iterable[str]) -> dict[str, Any]:
    """Best-effort commit and selected-file dirty status; works without git."""
    result: dict[str, Any] = {"revision": None, "available": False, "dirty_files": []}
    try:
        rev = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            text=True,
            capture_output=True,
            check=True,
            timeout=3,
        )
        result["revision"] = rev.stdout.strip()
        result["available"] = True
        paths = sorted(set(relative_paths))
        if paths:
            status = subprocess.run(
                ["git", "-C", str(root), "status", "--porcelain", "--", *paths],
                text=True,
                capture_output=True,
                check=True,
                timeout=3,
            )
            result["dirty_files"] = [
                line[3:] for line in status.stdout.splitlines() if len(line) >= 4
            ]
    except (OSError, subprocess.SubprocessError):
        pass
    return result


def _sections(text: str) -> list[tuple[int, int, str, str]]:
    """Split Markdown at headings; return start/end lines, heading, body."""
    lines = text.splitlines()
    headings = [
        (i, m.group(1).strip())
        for i, line in enumerate(lines)
        if (m := re.match(r"^#{1,6}\s+(.+?)\s*#*\s*$", line))
    ]
    if not headings:
        return [(1, max(1, len(lines)), "Document", text)]
    chunks = []
    if headings[0][0] > 0:
        chunks.append((1, headings[0][0], "Introduction", "\n".join(lines[: headings[0][0]])))
    for n, (at, title) in enumerate(headings):
        end = headings[n + 1][0] if n + 1 < len(headings) else len(lines)
        chunks.append((at + 1, max(at + 1, end), title, "\n".join(lines[at:end])))
    return chunks


def build_index(manifests: Sequence[RootManifest | Mapping[str, Any]]) -> dict[str, Any]:
    """Index only explicitly selected files under explicitly named roots."""
    roots, passages, coverage = [], [], []
    seen_ids: set[str] = set()
    for raw in manifests:
        m = raw if isinstance(raw, RootManifest) else RootManifest(**raw)
        if not m.root_id or m.root_id in seen_ids:
            raise SourceError(f"root_id must be unique and nonempty: {m.root_id!r}")
        seen_ids.add(m.root_id)
        root = Path(m.path).expanduser().resolve(strict=True)
        file_state, selected = {}, []
        for rel in m.files:
            rel = str(rel)
            try:
                path = _safe_file(root, rel)
                raw_bytes = path.read_bytes()
                content = raw_bytes.decode("utf-8")
            except FileNotFoundError:
                coverage.append({"root_id": m.root_id, "path": rel, "status": "missing"})
                continue
            digest = hashlib.sha256(raw_bytes).hexdigest()
            selected.append(rel)
            file_state[rel] = digest
            links = re.findall(r"(?<!!)\[[^\]]*\]\(([^)]+)\)", content)
            for start, end, heading, body in _sections(content):
                if not body.strip():
                    continue
                pid = f"{m.root_id}:{rel}:{start}:{digest[:12]}"
                passages.append(
                    {
                        "id": pid,
                        "root_id": m.root_id,
                        "repository": m.repository or m.root_id,
                        "path": rel,
                        "start_line": start,
                        "end_line": end,
                        "heading": heading,
                        "text": body.strip(),
                        "sha256": digest,
                        "links": links,
                    }
                )
            coverage.append(
                {"root_id": m.root_id, "path": rel, "status": "indexed", "sha256": digest}
            )
        git = _git_state(root, selected)
        dirty = set(git["dirty_files"])
        roots.append(
            {
                "root_id": m.root_id,
                "path": str(root),
                "repository": m.repository or m.root_id,
                "revision": git["revision"],
                "git_available": git["available"],
                "files": file_state,
                "dirty_files": sorted(dirty),
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "roots": roots,
        "passages": passages,
        "coverage": coverage,
    }


def open_locator(index: Mapping[str, Any], locator: Mapping[str, Any]) -> dict[str, Any]:
    """Reopen a passage and report current hash/freshness without changing history."""
    root_info = next(
        (r for r in index.get("roots", []) if r["root_id"] == locator.get("root_id")), None
    )
    if root_info is None:
        return {"status": "unavailable", "reason": "unknown root"}
    path_value = locator.get("path")
    if not isinstance(path_value, str) or path_value not in root_info.get("files", {}):
        return {"status": "unavailable", "reason": "file is not in the selected source manifest"}
    try:
        path = _safe_file(Path(root_info["path"]), path_value)
        raw_bytes = path.read_bytes()
        content = raw_bytes.decode("utf-8")
    except (FileNotFoundError, UnicodeError, SourceError, OSError):
        return {"status": "unavailable", "reason": "file missing, unreadable, or outside root"}
    digest = hashlib.sha256(raw_bytes).hexdigest()
    expected = locator.get("sha256")
    start = int(locator.get("start_line", 1))
    end = int(locator.get("end_line", start))
    lines = content.splitlines()
    dirty = str(locator["path"]) in root_info.get("dirty_files", [])
    revision_changed = bool(
        locator.get("revision") and locator["revision"] != root_info.get("revision")
    )
    return {
        "status": "stale"
        if digest != expected or revision_changed
        else ("dirty" if dirty else "current"),
        "sha256": digest,
        "expected_sha256": expected,
        "text": "\n".join(lines[max(0, start - 1) : end]),
        "revision": root_info.get("revision"),
        "dirty": dirty,
    }


def retrieve(index: Mapping[str, Any], query: str, *, limit: int = 8) -> dict[str, Any]:
    """Return lexical-match passages with citations and explicit coverage/freshness."""
    if not isinstance(limit, int) or isinstance(limit, bool) or not 0 <= limit <= 100:
        raise ValueError("limit must be between 0 and 100")
    terms = {x.casefold() for x in re.findall(r"[\w-]+", query) if len(x) > 1}
    ranked = []
    for p in index.get("passages", []):
        haystack = (p["heading"] + "\n" + p["text"]).casefold()
        score = sum(haystack.count(term) for term in terms)
        if score:
            freshness = open_locator(index, p)
            ranked.append((score, p, freshness))
    ranked.sort(key=lambda x: (-x[0], x[1]["root_id"], x[1]["path"], x[1]["start_line"]))
    results = []
    for score, p, fresh in ranked[:limit]:
        results.append(
            {
                "score": score,
                "citation": {
                    k: p[k]
                    for k in (
                        "root_id",
                        "repository",
                        "path",
                        "start_line",
                        "end_line",
                        "heading",
                        "sha256",
                    )
                }
                | {
                    "revision": next(
                        (r["revision"] for r in index["roots"] if r["root_id"] == p["root_id"]),
                        None,
                    )
                },
                "excerpt": p["text"][:4000],
                "excerpt_truncated": len(p["text"]) > 4000,
                "freshness": fresh["status"],
                "revision": next(
                    (r["revision"] for r in index["roots"] if r["root_id"] == p["root_id"]), None
                ),
                "links": p["links"],
                "support_edges": [],
            }
        )
    roots = [
        {"root_id": r["root_id"], "revision": r["revision"], "dirty_files": r["dirty_files"]}
        for r in index.get("roots", [])
    ]
    return {
        "query": query,
        "results": results,
        "coverage": index.get("coverage", []),
        "roots": roots,
        "reason": "lexical matches" if results else "no selected passage matched query terms",
    }


def default_manifest(root: str | os.PathLike[str]) -> RootManifest:
    """Small local default; callers should add explicit sibling manifests."""
    return RootManifest("ocelot", root, DEFAULT_SELECTED_FILES, "ocelot")
