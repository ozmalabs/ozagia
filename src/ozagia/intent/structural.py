"""Conservative structural hints for captured shell searches.

Only a grep pattern that explicitly asks for a Python ``def`` or ``class``
definition is promoted to a graph lookup. The original grep records remain
the primary result; graph data is appended as advisory context.
"""
from __future__ import annotations

import re
from typing import Any

_DEFINITION = re.compile(r"^\s*(?:def|class)\s+([A-Za-z_]\w*)\s*(?:\(|:|$)")
_BRE_DEFINITION = re.compile(
    r"(?:^|\\\|)\^?(?:def|class)\s+([A-Za-z_]\w*)\s*(?:\\\(|\(|:|$)")


def _tag_cache_refresh(stats: dict[str, Any]) -> str:
    """Describe cache work for this refresh without presenting it as coverage."""
    fields = ("parsed", "reused", "failed", "files", "cached_files")
    values = {field: stats.get(field, "unknown") for field in fields}
    return (
        "tag cache refresh: "
        f"{values['parsed']} parsed, {values['reused']} reused, "
        f"{values['failed']} failed of {values['files']} scanned files; "
        f"{values['cached_files']} cached entries"
    )


def definition_hint(memory: Any, pattern: Any, *, paths: list[str] | None = None,
                    matched_names: list[str] | None = None
                    ) -> tuple[str, list[str]]:
    """Return an additive definition hint for an explicit definition grep.

    The graph snapshot must be fresh. Results include tag-cache refresh
    counters, and an unhandled or stale lookup produces no hint rather than
    changing the shell result.
    """
    if not isinstance(pattern, str):
        return "", []
    match = _DEFINITION.fullmatch(pattern) or _BRE_DEFINITION.fullmatch(pattern)
    if match is None and not re.search(r"(?:^|\\\|)\^?(?:def|class)\s+", pattern):
        return "", []
    names = [match.group(1)] if match else list(dict.fromkeys(matched_names or []))
    if not names:
        return "", []
    try:
        freshness = memory.staleness()
        state = str(freshness.get("freshness") or "unknown")
    except Exception:  # noqa: BLE001 - this hint is advisory
        return "", []
    if state != "fresh":
        return f"[ozagia] structural lookup unavailable: graph freshness is {state}", []
    try:
        resolved: list[tuple[str, list[str]]] = []
        cache_stats: dict[str, Any] = {}
        for name in names[:10]:
            result = memory.symbol_graph("resolve", name)
            if isinstance(result, dict):
                resolved.append((name, result.get("matches") or []))
                cache_stats = result.get("tag_cache") or cache_stats
    except Exception:  # noqa: BLE001 - this hint is advisory
        return "", []
    if not resolved:
        return "", []
    tag_cache = cache_stats
    refresh = _tag_cache_refresh(tag_cache)
    rows: list[str] = []
    keys: list[str] = []
    total = 0
    for name, matches in resolved:
        if not isinstance(matches, list):
            continue
        total += len(matches)
        for item in matches:
            if not isinstance(item, str) or "::" not in item:
                continue
            rel, qualname = item.split("::", 1)
            if paths is not None and rel not in paths:
                continue
            key = f"sym:{rel}::{qualname}"
            if key not in keys:
                keys.append(key)
                rows.append(key)
            if len(rows) >= 10:
                break
    if not rows:
        return "", []
    lead = "[ozagia] structural definitions (fresh snapshot; " + refresh + "):"
    detail = "\n".join(f"  {row}" for row in rows)
    guidance = "[ozagia] read a definition by passing its sym: key to read."
    if total > len(rows):
        detail += f"\n  +{total - len(rows)} more matches (outside matched grep paths or display cap)"
    return "\n".join((lead, detail, guidance)), keys


def query(memory: Any, kind: Any, target: Any, *, depth: Any = 1,
          budget_tokens: Any = 240) -> dict[str, Any]:
    """Bounded structural answer for native OpenCode read-only tools.

    Results are projected onto a small JSON/text shape. Source paths in
    ``resources`` must be approved by the caller before the result is shown.
    """
    if not isinstance(kind, str) or kind not in {
            "resolve", "callers", "callees", "tests", "outline"}:
        return {"ok": False, "error": "unsupported structural operation"}
    if not isinstance(target, str) or not target.strip() or len(target) > 256:
        return {"ok": False, "error": "target must be a non-empty string of at most 256 characters"}
    try:
        freshness = memory.staleness()
    except Exception:
        freshness = {"freshness": "unknown"}
    state = str(freshness.get("freshness") or "unknown")
    if state != "fresh":
        return {"ok": False, "error": f"graph freshness is {state}; structural results withheld",
                "freshness": state, "coverage": "unavailable", "resources": []}
    resources: list[str] = []
    coverage = "graph snapshot: fresh"
    if kind in ("resolve", "callers", "callees"):
        try:
            result = memory.symbol_graph(kind, target,
                                         depth=max(1, min(3, int(depth))))
        except Exception as exc:  # noqa: BLE001 - bounded user-facing service
            return {"ok": False, "error": f"symbol graph failed: {type(exc).__name__}",
                    "freshness": state, "coverage": "unavailable", "resources": []}
        stats = result.get("tag_cache") or {}
        coverage += "; " + _tag_cache_refresh(stats)
        if kind == "resolve":
            rows = [{"id": item} for item in result.get("matches", [])[:20]
                    if isinstance(item, str)]
        elif isinstance(result.get("ambiguous"), list):
            rows = [{"id": item} for item in result["ambiguous"][:20]
                    if isinstance(item, str)]
        else:
            rows = [{key: row.get(key) for key in
                     ("id", "file", "qualname", "depth", "resolution", "confidence")}
                    for row in result.get("results", [])[:40]
                    if isinstance(row, dict)]
        resources = _safe_resources(memory, sorted({str(row["file"]) if "file" in row else
                            str(row["id"]).split("::", 1)[0]
                            for row in rows
                            if isinstance(row.get("file"), str)
                            or isinstance(row.get("id"), str) and "::" in row["id"]}))
        rows = [row for row in rows
                if (isinstance(row.get("file"), str) and row["file"] in resources)
                or (isinstance(row.get("id"), str)
                    and row["id"].split("::", 1)[0] in resources)]
        return {"ok": bool(result.get("ok")), "kind": kind, "target": target,
                "results": rows, "freshness": state, "coverage": coverage,
                "resources": resources, "truncated": len(result.get(
                    "results", result.get("matches", result.get("ambiguous", [])))
                    or []) > len(rows),
                **({"error": str(result["error"])[:500]}
                   if result.get("error") else {})}
    if kind == "tests":
        try:
            result = memory.tests_for(target)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"test graph failed: {type(exc).__name__}",
                    "freshness": state, "coverage": "graph snapshot: fresh", "resources": []}
        tests = result.get("tests", [])
        rows = [{"key": (test.get("test") or {}).get("key"),
                 "file": test.get("file"), "line": test.get("line"),
                 "why": test.get("why")}
                for test in tests[:40] if isinstance(test, dict)]
        resources = _safe_resources(memory, sorted({row["file"] for row in rows
                                     if isinstance(row.get("file"), str)}))
        rows = [row for row in rows if row.get("file") in resources]
        stats = memory.stats()
        coverage += f"; snapshot nodes {stats.get('nodes', 'unknown')}"
        return {"ok": bool(result.get("ok")), "kind": kind, "target": target,
                "results": rows, "freshness": state, "coverage": coverage,
                "resources": resources, "truncated": len(tests) > len(rows),
                **({"error": str(result["error"])[:500]}
                   if result.get("error") else {})}
    if target not in (".", "repo", "repository"):
        try:
            result = memory.outline_target(target)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"outline failed: {type(exc).__name__}",
                    "freshness": state, "coverage": "unavailable", "resources": []}
        path = result.get("path")
        resources = _safe_resources(memory, [path] if isinstance(path, str) and path else [])
        stats = memory.stats()
        coverage = f"graph snapshot: fresh; {stats.get('nodes', 'unknown')} snapshot nodes"
        safe_text = str(result.get("text") or "") if resources else ""
        return {"ok": bool(result.get("ok") and resources), "kind": kind, "target": target,
                "text": safe_text[:6000],
                "freshness": state, "coverage": coverage, "resources": resources,
                "truncated": len(safe_text) > 6000,
                **({"error": "outline omitted because it has no safe in-checkout citation"}
                   if not resources and result.get("ok") else {}),
                **({"error": str(result["error"])[:500]}
                   if result.get("error") else {})}
    try:
        budget = max(100, min(400, int(budget_tokens)))
        result = memory.repo_map(target, budget_tokens=budget)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"repository map failed: {type(exc).__name__}",
                "freshness": state, "coverage": "unavailable", "resources": []}
    files = result.get("files", [])
    resources = _safe_resources(memory, sorted({f["path"] for f in files
                        if isinstance(f, dict) and isinstance(f.get("path"), str)}))
    stats = result.get("cache") or {}
    coverage += "; " + _tag_cache_refresh(stats)
    all_cited = len(resources) == len(files)
    map_text = str(result.get("markdown") or "")
    return {"ok": bool(result.get("ok")), "kind": kind, "target": target,
            "text": (map_text[:6000] if all_cited and (resources or not files) else
                     "Outline omitted because one or more citations were outside the checkout."),
            "freshness": state, "coverage": coverage, "resources": resources,
            "truncated": len(map_text) > 6000,
            **({"error": str(result["error"])[:500]}
               if result.get("error") else {})}


def _safe_resources(memory: Any, resources: list[str]) -> list[str]:
    """Keep citations inside the checkout and outside private state folders."""
    from pathlib import Path
    root = Path(memory.raw_repo_root).resolve()
    safe: list[str] = []
    for raw in resources:
        if not isinstance(raw, str) or not raw or "\x00" in raw:
            continue
        path = Path(raw)
        if path.is_absolute() or ".." in path.parts:
            continue
        try:
            real = (root / path).resolve()
            rel = real.relative_to(root).as_posix()
        except (OSError, ValueError):
            continue
        if rel == ".git" or rel.startswith(".git/") or rel == ".ozagia" or rel.startswith(".ozagia/"):
            continue
        safe.append(rel)
    return sorted(set(safe))


__all__ = ["definition_hint", "query"]
