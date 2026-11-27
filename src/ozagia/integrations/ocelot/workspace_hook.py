"""Append the compact Ocelot context to an OpenCode session-start hint.

OpenCode asks the daemon for a ``hint`` on each user prompt and the plugin
caches the answer per session, so the first hint is effectively the session
start.  When the Ocelot workspace adapter registered the ``ocelot_context``
graph query, :func:`augment_hint` appends one bounded, clearly marked
read-only block to that hint.  Without the adapter it adds nothing.

Everything here is read-only: the adapter query runs the existing workspace
planner and the doctor's local git/file checks.  This module never imports the
target repository, builds the engine, runs pytest, reaches the network or
calls a model.
"""
from __future__ import annotations

from typing import Any, Mapping

# Character cap for the marked Ocelot block, including its marker and the
# read-only instruction.  The base hint keeps its own independent budget.
MAX_HINT_CHARS = 1800
# Default and maximum selected test files in the adapter query.
DEFAULT_MAX_TESTS = 5
# Bounds on the advisory lists copied into the return value.
MAX_WARNINGS = 5
MAX_WARNING_CHARS = 200
MAX_ERROR_CHARS = 200
# The adapter query is named consistently across the graph and MCP surfaces.
QUERY_NAME = "ocelot_context"
MARKER = "[ozagia ocelot]"
INSTRUCTION = (
    "Read-only Ocelot context. Verify every path, seed and test command with "
    "the ocelot_* tools before acting; this block may be stale or truncated."
)
_MISSING_QUERY = "no adapter graph query"
_TRUNCATED = "[truncated]"


def _cap(value: Any, default: int = MAX_HINT_CHARS) -> int:
    try:
        limit = int(value)
    except (TypeError, ValueError):
        limit = default
    return max(0, min(limit, 20000))


def _error_text(exc: BaseException) -> str:
    detail = str(exc).strip()
    text = f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__
    return text[:MAX_ERROR_CHARS]


def _clip(text: str, limit: int) -> tuple[str, bool]:
    """Bound *text*, preferring a line boundary, and mark a truncation."""
    if limit <= 0:
        return "", bool(text)
    if len(text) <= limit:
        return text, False
    marker = "\n... " + _TRUNCATED
    budget = limit - len(marker)
    if budget <= 0:
        return text[:limit], True
    cut = text[:budget]
    newline = cut.rfind("\n")
    if newline > 0:
        cut = cut[:newline]
    return (cut.rstrip() + marker)[:limit], True


def _registered(memory: Any) -> bool | None:
    """Whether the adapter registry knows the query; ``None`` if unknown."""
    try:
        from ozagia.adapters import registry
        queries = registry(getattr(memory, "raw_repo_root", None)).graph_queries
    except Exception:  # noqa: BLE001 - a broken config must not block the hint
        return None
    return QUERY_NAME in queries


def _missing_query(result: Any) -> bool:
    """Whether ``DevMemory.adapter_query`` said the query is not registered."""
    return (isinstance(result, Mapping)
            and result.get("ok") is False
            and _MISSING_QUERY in str(result.get("error") or ""))


def _empty(*, available: bool, cap: int,
           error: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "available": available,
        "context": "",
        "chars": 0,
        "state": None,
        "plan_mode": None,
        "test_count": 0,
        "warnings": [],
        "meta": {"max_chars": cap, "raw_chars": 0, "truncated": False},
    }
    if error:
        out["error"] = error[:MAX_ERROR_CHARS]
    return out


def _test_count(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _warnings(result: Mapping[str, Any]) -> list[str]:
    out: list[str] = []
    for item in result.get("warnings") or []:
        text = str(item or "").strip()
        if text:
            out.append(text[:MAX_WARNING_CHARS])
        if len(out) >= MAX_WARNINGS:
            break
    return out


def ocelot_hint_context(memory: Any, *,
                        max_chars: int = MAX_HINT_CHARS) -> dict[str, Any]:
    """Run ``ocelot_context`` and return a bounded, read-only summary.

    A memory without an adapter query (or one whose ``DevMemory.adapter_query``
    reports the query is not registered) returns ``available: False`` without
    raising.  A query that is registered but fails returns ``available: True``
    plus a bounded ``error`` so the caller can report it in hint metadata.
    """
    cap = _cap(max_chars)
    query = getattr(memory, "adapter_query", None)
    if not callable(query):
        return _empty(available=False, cap=cap)
    registered = _registered(memory)
    try:
        result = query(QUERY_NAME, max_tests=DEFAULT_MAX_TESTS)
    except Exception as exc:  # noqa: BLE001 - a hint must never block the agent
        return _empty(available=registered is True, cap=cap,
                      error=_error_text(exc))
    if _missing_query(result):
        return _empty(available=False, cap=cap)
    if not isinstance(result, Mapping):
        error = f"TypeError: adapter query returned {type(result).__name__}"
        return _empty(available=registered is True, cap=cap, error=error)
    if (result.get("ok") is False and result.get("error")
            and "state" not in result):
        return _empty(available=registered is True, cap=cap,
                      error=str(result.get("error"))[:MAX_ERROR_CHARS])

    try:
        from . import workspace_context
        text = workspace_context.format_text(result)
    except Exception as exc:  # noqa: BLE001 - a hint must never block the agent
        return _empty(available=True, cap=cap, error=_error_text(exc))

    context, truncated = _clip(text, cap)
    context = context.strip()
    plan = result.get("plan")
    if not isinstance(plan, Mapping):
        plan = {}
    return {
        "available": True,
        "context": context,
        "chars": len(context),
        "state": str(result.get("state") or "unknown"),
        "plan_mode": str(plan.get("mode") or "unknown"),
        "test_count": _test_count(plan.get("test_count")),
        "warnings": _warnings(result),
        "meta": {
            "max_chars": cap,
            "raw_chars": len(text),
            "truncated": truncated,
            "schema": str(result.get("schema") or ""),
        },
    }


def _hint_meta(hint: Mapping[str, Any]) -> dict[str, Any]:
    meta = hint.get("meta")
    return dict(meta) if isinstance(meta, Mapping) else {}


def _with_ocelot(hint: dict[str, Any], payload: dict[str, Any],
                 context: str | None = None) -> dict[str, Any]:
    out = dict(hint)
    out["meta"] = {**_hint_meta(hint), "ocelot": payload}
    if context is not None:
        out["context"] = context
    return out


def _block(context: str) -> tuple[str, bool]:
    """The marked block, bounded to :data:`MAX_HINT_CHARS` in total."""
    prefix = f"{MARKER}\n{INSTRUCTION}\n"
    if len(prefix) >= MAX_HINT_CHARS:
        return _clip(prefix, MAX_HINT_CHARS)
    body, truncated = _clip(context, MAX_HINT_CHARS - len(prefix))
    return prefix + body, truncated


def augment_hint(memory: Any, hint: dict[str, Any], *, harness: str,
                 session: Any = None) -> dict[str, Any]:
    """Append one bounded ``[ozagia ocelot]`` block to an OpenCode hint.

    Other harnesses, an unavailable adapter and a hint that already carries
    the block are returned unchanged.  The existing ``context`` and ``meta``
    keys keep their order and are never overwritten.  Any failure leaves the
    context untouched and records a bounded ``meta.ocelot.error`` when the
    adapter is present.
    """
    if harness != "opencode" or not isinstance(hint, dict):
        return hint
    meta = hint.get("meta")
    existing = meta.get("ocelot") if isinstance(meta, Mapping) else None
    if isinstance(existing, Mapping) and existing.get("delivered") is True:
        return hint
    original = hint.get("context")
    original_text = original if isinstance(original, str) else ""
    if MARKER in original_text:
        return hint

    try:
        info = ocelot_hint_context(memory)
    except Exception as exc:  # noqa: BLE001 - a hint must never block the agent
        return _with_ocelot(hint, {
            "available": False, "delivered": False,
            "error": _error_text(exc),
        })
    if not info.get("available"):
        return hint

    state = info.get("state")
    mode = info.get("plan_mode")
    count = info.get("test_count")
    truncated = bool((info.get("meta") or {}).get("truncated"))
    if info.get("error"):
        return _with_ocelot(hint, {
            "available": True, "delivered": False,
            "state": state, "mode": mode, "test_count": count,
            "truncated": truncated,
            "error": str(info["error"])[:MAX_ERROR_CHARS],
        })

    body = str(info.get("context") or "").strip()
    if not body:
        return _with_ocelot(hint, {
            "available": True, "delivered": False,
            "state": state, "mode": mode, "test_count": count,
            "truncated": truncated,
        })
    block, block_truncated = _block(body)
    if not block:
        return _with_ocelot(hint, {
            "available": True, "delivered": False,
            "state": state, "mode": mode, "test_count": count,
            "truncated": truncated,
        })
    separator = "\n\n" if original_text else ""
    payload: dict[str, Any] = {
        "available": True,
        "delivered": True,
        "state": state,
        "mode": mode,
        "test_count": count,
        "truncated": bool(truncated or block_truncated),
        "chars": len(block),
    }
    if info.get("warnings"):
        payload["warnings"] = len(info["warnings"])
    return _with_ocelot(hint, payload, f"{original_text}{separator}{block}")
