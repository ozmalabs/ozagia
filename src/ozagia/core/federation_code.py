"""Read-only code search across the current source of federation members.

This is the opt-in code arm beside :func:`ozagia.core.federation.search`.
Graph search reads member snapshots; code search reads each existing member
checkout with :func:`ozagia.core.search.code_search`. Both use the same member
resolution rules and qualified-key prefixes. Code mode is relevance-ranked
only: it has no family boost. Graph-mode family ranking is unchanged.

The only writes are tag/index caches under the anchor's state directory (or a
temporary directory when that is not writable). A cache file is never placed
under another member checkout or its ``.ozagia`` directory. No member index is
refreshed, no adapter is imported, and no project code is imported from a
member.
"""
from __future__ import annotations

import hashlib
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Iterable

from ozagia.config import state_path
from ozagia.core.search import code_search as _member_code_search

# Normalized scores are in (0, 1]; this keeps an all-zero member finite.
EPSILON = 1e-9

# One member may need to contribute more than the global page, but an
# unbounded per-member request would make a large checkout dominate the merge.
MAX_PER_MEMBER_LIMIT = 50

_MEMBER_FILE_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _anchor_state(anchor: Path) -> Path:
    return state_path(anchor).expanduser().resolve()


def _allowed_cache_dir(candidate: Path, anchor: Path,
                       members: list[dict[str, Any]]) -> bool:
    """Allow the anchor state directory, or any path outside member roots.

    The anchor's own state directory is the documented cache location even
    when it sits inside the anchor checkout. Every other member root, and the
    anchor outside its state directory, is off limits.
    """
    state = _anchor_state(anchor)
    for item in members:
        root = item["root"]
        if not _inside(candidate, root):
            continue
        if root == anchor and _inside(candidate, state):
            continue
        return False
    return True


def _prepare_cache_dir(anchor: Path, members: list[dict[str, Any]],
                       cache_dir: str | Path | None
                       ) -> tuple[Path, dict[str, Any]]:
    """Return a writable cache directory and its report.

    The default is ``<anchor state>/federation-search``. An explicit directory
    that would fall inside a member checkout, or that is not writable, falls
    back to a fresh temporary directory. The fallback is reported; it is never
    silent.
    """
    if cache_dir is None:
        candidate = _anchor_state(anchor) / "federation-search"
        origin = "anchor-state"
    else:
        candidate = Path(cache_dir).expanduser()
        if not candidate.is_absolute():
            candidate = anchor / candidate
        candidate = candidate.resolve()
        origin = "explicit"

    note = None
    if not _allowed_cache_dir(candidate, anchor, members):
        note = ("cache directory is inside a member checkout; "
                "using a temporary directory instead")
    else:
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            fd, probe = tempfile.mkstemp(dir=str(candidate),
                                         prefix=".ozagia-search-probe-")
            os.close(fd)
            try:
                os.unlink(probe)
            except OSError:
                pass
        except OSError as exc:
            note = (f"cache directory is not writable: {type(exc).__name__}: "
                    f"{exc}; using a temporary directory instead")
        else:
            report = {"dir": str(candidate), "state": origin,
                      "fallback": False, "files": {},
                      "scope": ("anchor state directory"
                                if origin == "anchor-state"
                                else "explicit cache directory")}
            return candidate, report

    fallback = Path(tempfile.mkdtemp(prefix="ozagia-federation-search-"))
    report = {"dir": str(fallback), "state": "temporary", "fallback": True,
              "files": {}, "scope": "temporary directory", "note": note}
    return fallback, report


def _cache_file(cache_dir: Path, alias: str, root: Path) -> Path:
    """Stable member cache filename: readable alias plus the root path hash."""
    safe = _MEMBER_FILE_SAFE.sub("-", alias).strip("-") or "member"
    digest = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:16]
    return cache_dir / f"{safe}-{digest}.tags.json"


def _member_source_state(root: Path) -> tuple[str, str | None]:
    if not root.exists():
        return "missing", "member source checkout does not exist"
    if not root.is_dir():
        return "not-a-directory", "member source path is not a directory"
    if not os.access(root, os.R_OK | os.X_OK):
        return "unreadable", "member source checkout is not readable"
    return "ok", None


def code_search(root: str | Path | None, query: str,
                roots: Iterable[str | Path] | None = None, limit: int = 20,
                include_snippet: bool = False,
                cache_dir: str | Path | None = None) -> dict[str, Any]:
    """Search current member source and merge per-member normalized scores.

    ``roots`` selects members with the same rules as graph federation. A
    member without a snapshot is still reported but is skipped by automatic
    discovery, so use explicit roots to search an unindexed checkout. Member
    results are normalized by that member's best score, then sorted by rank
    score, member id and path. The sort is deterministic and relevance-only;
    code mode has no family preference.

    ``include_snippet`` defaults to false. Cache writes go under the anchor
    state directory (or a temporary fallback), never under another member.
    Missing or unreadable member roots are reported and skipped, not fatal.
    """
    from ozagia.core import federation as F

    anchor = F._root(root)
    if roots is not None:
        roots = tuple(roots)
    limit = max(0, int(limit))
    per_member_limit = min(MAX_PER_MEMBER_LIMIT, max(1, limit)) if limit else 0
    items = F._members(anchor, roots)
    cache_path, cache_report = _prepare_cache_dir(anchor, items, cache_dir)

    members: list[dict[str, Any]] = []
    hits: list[dict[str, Any]] = []
    for item in items:
        alias = item["alias"]
        member = dict(F._public_member(item))
        member["mode"] = "code"
        source_state, source_detail = _member_source_state(item["root"])
        member["source_state"] = source_state
        if source_detail:
            member["source_detail"] = source_detail
        if source_state != "ok":
            member["search"] = {"state": "skipped", "results": 0,
                                "detail": source_detail}
            members.append(member)
            continue
        cache_file = _cache_file(cache_path, alias, item["root"])
        cache_report["files"][alias] = str(cache_file)
        member["cache"] = str(cache_file)
        try:
            found = _member_code_search(
                item["root"], query, limit=per_member_limit,
                cache_path=cache_file, include_snippet=bool(include_snippet))
        except Exception as exc:  # A member query must not fail the federation.
            member["search"] = {
                "state": "error", "results": 0,
                "detail": f"{type(exc).__name__}: {exc}"}
            members.append(member)
            continue
        if not found.get("ok"):
            member["search"] = {
                "state": "error", "results": 0,
                "detail": found.get("error") or "member code search failed"}
            members.append(member)
            continue
        rows = [row for row in found.get("results", [])
                if isinstance(row, dict) and isinstance(row.get("path"), str)]
        best = max((float(row.get("score") or 0.0) for row in rows), default=0.0)
        best = max(best, EPSILON)
        for rank, row in enumerate(rows, 1):
            score = float(row.get("score") or 0.0)
            normalized = round(score / best, 6)
            path = row["path"]
            hit: dict[str, Any] = {
                "qualified_key": f"{alias}::file:{path}",
                "key": f"file:{path}",
                "path": path,
                "file": path,
                "score": row.get("score"),
                "normalized_score": normalized,
                "rank_score": normalized,
                "per_member_rank": rank,
                "source": {"id": alias, "root": str(item["root"])},
            }
            if include_snippet and "line" in row:
                hit["line"] = row.get("line")
            if include_snippet and "snippet" in row:
                hit["snippet"] = row.get("snippet")
            if row.get("symbols"):
                hit["symbols"] = row["symbols"]
            hits.append(hit)
        member["search"] = {"state": "ok", "results": len(rows),
                            "index": found.get("index")}
        members.append(member)

    hits.sort(key=lambda hit: (
        -hit["rank_score"],
        str(hit["source"]["id"]).casefold(),
        hit["path"],
    ))
    notes = [
        "code mode reads current member source; member snapshots are not "
        "refreshed",
        "per-member scores are normalized before the global merge",
        "code mode is relevance-ranked only; there is no family boost",
        "cache files live under the anchor state directory or a temporary "
        "fallback, never under another member checkout",
    ]
    if not query.strip():
        notes.append("empty query; no member source was searched")
    missing = [member["id"] for member in members
               if member.get("source_state") != "ok"]
    if missing:
        notes.append("members skipped for missing or unreadable source: "
                     + ", ".join(missing))
    return {
        "ok": True,
        "mode": "code",
        "query": query,
        "results": hits[:limit],
        "n": len(hits),
        "members": members,
        "cache": cache_report,
        "per_member_limit": per_member_limit,
        "notes": notes,
        "limits": [
            "relevance-ranked only; code mode has no family boost",
            "per-member results are capped at "
            f"{MAX_PER_MEMBER_LIMIT} before the global merge",
            "snippets are opt-in and bounded by the code-search adapter",
            "missing or unreadable member source roots are skipped and reported",
            "no target import, adapter execution, index refresh or member write",
            "snapshot state is reported separately; current source is not proof "
            "that a snapshot is fresh",
        ],
    }


__all__ = ["code_search", "MAX_PER_MEMBER_LIMIT"]
