"""Read-only task context for a caller that already owns a DevMemory snapshot.

No target project is imported, no snapshot is built and no model is called.
The caller remains responsible for timeouts and any network transport.
"""
from __future__ import annotations

from typing import Any

from ozagia.core.memory import DevMemory
from ozagia.core import memory as memory_module
from ozagia.config import ConfigError, load_project_config


def task_context(memory: DevMemory, task: str, *, budget_tokens: int = 1500,
                 max_items: int = 8, allow_stale: bool = False) -> dict[str, Any]:
    """Return versioned context with an approximate snippet budget and freshness.

    The budget limits snippet characters to four per estimated token; it does
    not include JSON, metadata or other fields, and is not a tokenizer limit.
    Freshness is a best-effort point-in-time check, not a filesystem lock.
    """
    if not isinstance(task, str) or not task.strip():
        raise ValueError("task must be a non-empty string")
    if isinstance(budget_tokens, bool) or not isinstance(budget_tokens, int) or not 1 <= budget_tokens <= 32000:
        raise ValueError("budget_tokens must be an integer between 1 and 32000")
    if isinstance(max_items, bool) or not isinstance(max_items, int) or not 1 <= max_items <= 100:
        raise ValueError("max_items must be an integer between 1 and 100")
    if not isinstance(allow_stale, bool):
        raise ValueError("allow_stale must be a boolean")

    # Invalidate the short-lived staleness memo at both sides of context
    # assembly. The digest check is not a filesystem lock, but this catches
    # ordinary edits made while snippets are being read.
    memory._staleness_memo = None
    snapshot = memory.staleness()
    freshness = {"fresh": "current", "stale": "stale", "unknown": "unknown"}[
        snapshot["freshness"]]
    # Memory's digest uses module-level configuration chosen at import time.
    # Do not certify another checkout with different digest scope as current.
    try:
        config = load_project_config(memory.raw_repo_root)
        if (tuple(config.source_dirs) != tuple(memory_module.SOURCE_DIRS)
                or tuple(config.test_dirs) != tuple(memory_module.TEST_DIRS)
                or set(config.memory_skip_dirs) != set(memory_module.SKIP_PARTS)):
            snapshot = dict(snapshot)
            snapshot["verification_errors"] = [*snapshot.get("verification_errors", []),
                "repository index configuration differs from the loaded memory configuration"]
            if freshness == "current":
                freshness = "unknown"
                snapshot["freshness"] = "unknown"
    except (ConfigError, OSError) as exc:
        snapshot = dict(snapshot)
        snapshot["verification_errors"] = [*snapshot.get("verification_errors", []),
                                             f"cannot load repository configuration: {exc}"]
        if freshness == "current":
            freshness = "unknown"
            snapshot["freshness"] = "unknown"
    result: dict[str, Any] = {
        "version": 1, "ok": False, "task": task,
        "freshness": freshness, "snapshot": snapshot,
        "freshness_note": "Point-in-time git tree check; changes can occur after the check.",
        "budget": {"snippet_char_limit": budget_tokens * 4,
                   "estimated_tokens_requested": budget_tokens,
                   "scope": "snippet text only; excludes response metadata"},
        "items": [],
    }
    if freshness != "current" and not allow_stale:
        result["error"] = "snapshot is stale or freshness cannot be verified"
        return result

    pack = memory.context(task, max_items=max_items, max_chars=budget_tokens * 4,
                          budget_tokens=budget_tokens)
    memory._staleness_memo = None
    after = memory.staleness()
    identity_fields = ("root", "snapshot_root", "head", "snapshot_head")
    changed_identity = any(snapshot.get(key) != after.get(key)
                           for key in identity_fields)
    changed_tree = snapshot.get("tree_changed") != after.get("tree_changed")
    digest_tracked = ("current_tree_digest" in snapshot
                      or "current_tree_digest" in after)
    before_digest = snapshot.get("current_tree_digest")
    after_digest = after.get("current_tree_digest")
    changed_digest = digest_tracked and before_digest != after_digest
    missing_digest = (digest_tracked and freshness == "current"
                      and (before_digest is None or after_digest is None))
    lost_freshness = freshness == "current" and after.get("freshness") != "fresh"
    if changed_identity or changed_tree or changed_digest or missing_digest or lost_freshness:
        after = dict(after)
        errors = list(after.get("verification_errors", []))
        errors.append("repository changed or could not be verified during context assembly")
        after["verification_errors"] = errors
        after["freshness"] = "unknown"
        result.update(ok=False, freshness="unknown", snapshot=after,
                      error="repository changed during context assembly")
        return result
    remaining = budget_tokens * 4
    items = []
    for item in pack.get("items", []):
        snippet = dict(item.get("snippet") or {})
        text = str(snippet.get("text") or "")
        snippet["text"] = text[:remaining]
        snippet["truncated_by_provider"] = len(text) > remaining
        snippet["estimated_tokens"] = (len(snippet["text"]) + 3) // 4
        remaining -= len(snippet["text"])
        items.append({**item, "snippet": snippet})
        if remaining <= 0:
            break
    result.update(ok=True, items=items)
    result["budget"]["snippet_chars"] = budget_tokens * 4 - remaining
    return result
