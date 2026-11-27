"""Compact Ocelot agent context built from the workspace graph and doctor.

This is the session-start surface for an Ocelot checkout: one bounded payload
that answers "where am I, is the engine trustworthy, what should I run, and
which seed or research chain did I touch?".  It composes the existing
:mod:`ozagia.integrations.ocelot.workspace` planner, the read-only
:mod:`ozagia.integrations.ocelot.workspace_doctor` and (when indexed) the
research overlay.  It never imports Ocelot, builds the engine, runs pytest,
reaches the network or calls a model.

The workspace adapter registers this module lazily from ``workspace:register``;
a partial install can also register it directly::

    [adapters]
    modules = [
        "ozagia.integrations.ocelot.workspace:register",
        "ozagia.integrations.ocelot.workspace_context:register_context",
    ]

It registers graph query ``ocelot_context`` and MCP tool ``ocelot_context``.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Mapping

MAX_TESTS_DEFAULT = 20
MAX_TESTS_LIMIT = 200
MAX_SEED_DEPENDENTS = 10
MAX_SEED_ENTRIES = 10
MAX_RESEARCH_ENTRIES = 5
MAX_RESEARCH_ITEMS = 10
MAX_REASONS = 20
MAX_COMMANDS = 12
MAX_WARNINGS = 20
MAX_NOTE = 400

SCHEMA = "ozagia.ocelot.context.v1"


def _root(memory: Any) -> Path:
    return Path(memory.raw_repo_root).resolve()


def _limit(max_tests: Any) -> int:
    try:
        value = int(max_tests)
    except (TypeError, ValueError):
        return MAX_TESTS_DEFAULT
    return min(max(value, 1), MAX_TESTS_LIMIT)


def _bounded(values: Iterable[Any], limit: int) -> list[Any]:
    return list(values)[:limit]


def _doctor(root: Path) -> dict[str, Any]:
    try:
        from .workspace_doctor import status
        result = status(root)
    except Exception as exc:  # noqa: BLE001 - a broken doctor must not hide context
        return {
            "state": "unknown",
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "warnings": [],
            "blockers": [],
            "next_commands": [],
        }
    return result


def _compact_doctor(result: Mapping[str, Any]) -> dict[str, Any]:
    git = result.get("git") or {}
    dirty = git.get("dirty") or {}
    engine = result.get("engine") or {}
    native = engine.get("native") or {}
    pth = result.get("pth") or {}
    data = result.get("data") or {}
    baseline = result.get("baseline") or {}
    return {
        "state": str(result.get("state") or "unknown"),
        "ok": bool(result.get("ok")),
        "error": result.get("error"),
        "worktree": {
            "available": bool(git.get("available")),
            "branch": git.get("branch"),
            "head": (str(git.get("head") or "")[:12] or None),
            "dirty": dirty.get("count"),
            "dirty_count": dirty.get("count"),
            "worktree_count": git.get("worktree_count", 0),
            "is_main_worktree": git.get("is_main_worktree"),
        },
        "engine": {
            "state": str(engine.get("state") or "unknown"),
            "native": bool(native.get("exists")),
            "sha256": native.get("sha256"),
            "source_matches_record": engine.get("source_matches_record"),
            "engine_hash_matches_record": engine.get("engine_hash_matches_record"),
            "build_is_stale": bool(engine.get("build_is_stale")),
            "headless_package": bool(engine.get("headless_package")),
            "blockers": _bounded(engine.get("blockers") or [], MAX_REASONS),
        },
        "data": {
            "exists": bool(data.get("exists")),
            "path": data.get("path"),
        },
        "baseline": {
            "exists": bool(baseline.get("exists")),
            "path": baseline.get("path"),
        },
        "datasets": {
            "data": bool(data.get("exists")),
            "data_path": data.get("path"),
            "baseline": bool(baseline.get("exists")),
            "baseline_path": baseline.get("path"),
        },
        "pth_outside_root": bool(pth.get("outside_root")),
        "warnings": _bounded(result.get("warnings") or [], MAX_WARNINGS),
        "blockers": _bounded(result.get("blockers") or [], MAX_WARNINGS),
        "next_commands": _bounded(result.get("next_commands") or [], MAX_COMMANDS),
    }


def _compact_plan(plan: Mapping[str, Any], max_tests: int) -> dict[str, Any]:
    tests = [str(path) for path in plan.get("tests") or []]
    shown = tests[:max_tests]
    return {
        "ok": plan.get("ok", True) is not False,
        "mode": str(plan.get("mode") or "unknown"),
        "test_count": int(plan.get("test_count") or len(tests)),
        "tests": shown,
        "tests_shown": len(shown),
        "tests_truncated": len(tests) > len(shown),
        "commands": _bounded(plan.get("commands") or [], MAX_COMMANDS),
        "engine_rebuild": bool(plan.get("engine_rebuild")),
        "full_gate": bool(plan.get("full_gate")),
        "full_reasons": _bounded(plan.get("full_reasons") or [], MAX_REASONS),
        "reasons": _bounded(plan.get("reasons") or [], MAX_REASONS),
        "warnings": _bounded(plan.get("warnings") or [], MAX_WARNINGS),
    }


def _seed_entries(memory: Any, changes: Iterable[Any],
                  max_tests: int) -> list[dict[str, Any]]:
    from .workspace import seed_summary

    entries: list[dict[str, Any]] = []
    for raw in list(changes)[:MAX_SEED_ENTRIES]:
        if not isinstance(raw, Mapping):
            continue
        seed_id = str(raw.get("id") or "").strip()
        if not seed_id:
            continue
        summary = seed_summary(memory, seed_id, include="summary")
        if not summary.get("ok"):
            entries.append({
                "id": seed_id,
                "path": raw.get("path"),
                "blast_radius": raw.get("blast_radius"),
                "shared": bool(raw.get("shared")),
                "error": summary.get("error") or "seed is not indexed",
            })
            continue
        tests = [
            str(item.get("file"))
            for item in summary.get("tests") or []
            if isinstance(item, Mapping) and item.get("file")
        ]
        exact = summary.get("exact_test")
        if isinstance(exact, str) and exact and exact not in tests:
            tests.append(exact)
        tests = sorted(set(tests))
        dependents = [str(value) for value in summary.get("dependents") or []]
        entries.append({
            "id": seed_id,
            "path": raw.get("path") or summary.get("path"),
            "blast_radius": summary.get("blast_radius") or raw.get("blast_radius"),
            "shared": bool(summary.get("shared") or raw.get("shared")),
            "fanout": int(summary.get("fanout") or 0),
            "direct_dependents": dependents[:MAX_SEED_DEPENDENTS],
            "dependents_truncated": len(dependents) > MAX_SEED_DEPENDENTS,
            "related_tests": tests[:max_tests],
            "tests": tests[:max_tests],
            "tests_truncated": len(tests) > max_tests,
            "test_count": len(tests),
        })
    return entries


def _research_entries(memory: Any, changed: Iterable[str],
                      max_tests: int) -> list[dict[str, Any]]:
    try:
        from .workspace_research import research_summary
    except ImportError:  # pragma: no cover - partial source tree
        return []
    entries: list[dict[str, Any]] = []
    for rel in changed:
        if not (rel.startswith("docs/research/") and rel.endswith(".md")):
            continue
        if len(entries) >= MAX_RESEARCH_ENTRIES:
            break
        try:
            result = research_summary(memory, rel)
        except Exception as exc:  # noqa: BLE001 - the overlay is best-effort
            entries.append({
                "path": rel,
                "available": False,
                "error": f"{type(exc).__name__}: {exc}",
            })
            continue
        if not result.get("ok"):
            entries.append({
                "path": rel,
                "available": False,
                "error": result.get("error") or "no research chain indexed",
            })
            continue
        chain = result.get("chain") or {}
        entries.append({
            "path": rel,
            "available": True,
            "target": result.get("target"),
            "chain": {
                key: _bounded(chain.get(key) or [], MAX_RESEARCH_ITEMS)
                for key in ("prereg", "amendments", "results", "indexes")
            },
            "scripts": _bounded(result.get("scripts") or [], MAX_RESEARCH_ITEMS),
            "tests": _bounded(result.get("tests") or [], max_tests),
            "seeds": _bounded(result.get("seeds") or [], MAX_RESEARCH_ITEMS),
            "engine_hashes": _bounded(
                result.get("engine_hashes") or [], 3),
            "missing_gate": bool(result.get("missing_gate")),
            "stale": bool(result.get("stale")),
            "reasons": _bounded(result.get("reasons") or [], MAX_REASONS),
        })
    return entries


def _next_commands(doctor: Mapping[str, Any],
                   plan: Mapping[str, Any]) -> list[str]:
    commands: list[str] = []
    candidates = list(doctor.get("next_commands") or [])
    candidates.extend(plan.get("commands") or [])
    for command in candidates:
        text = str(command or "").strip()
        if text and text not in commands:
            commands.append(text)
        if len(commands) >= MAX_COMMANDS:
            break
    return commands


def _agent_note(doctor: Mapping[str, Any], plan: Mapping[str, Any],
                research: Iterable[Mapping[str, Any]]) -> str:
    notes: list[str] = []
    if doctor.get("state") == "blocked":
        notes.append("workspace blocked; fix doctor blockers before measuring")
    if plan.get("engine_rebuild"):
        notes.append("engine rebuild required; do not measure")
    if plan.get("full_gate"):
        notes.append("full migrated gate required; never -n auto")
    elif plan.get("mode") == "none":
        notes.append("documentation-only change; no pytest selected")
    elif plan.get("mode") == "affected":
        notes.append("affected slice only; do not claim full coverage")
    if any(bool(item.get("stale")) for item in research):
        notes.append("research records a stale engine hash")
    text = "; ".join(notes)
    return text[:MAX_NOTE]


def context_summary(memory: Any, changed: Iterable[str] | None = None, *,
                    base: str | None = None, head: str | None = None,
                    seeds: Iterable[str] | None = None,
                    max_tests: int = MAX_TESTS_DEFAULT) -> dict[str, Any]:
    """A bounded, read-only Ocelot context for an agent session.

    ``changed``, ``base``, ``head`` and ``seeds`` are passed to the test
    planner.  ``max_tests`` bounds how many selected test paths and per-seed
    related paths are shown; the planner's full count is still reported.
    """
    from .workspace import test_plan

    root = _root(memory)
    limit = _limit(max_tests)
    doctor_full = _doctor(root)
    doctor = _compact_doctor(doctor_full)

    plan_error: str | None = None
    try:
        plan_full = test_plan(memory, changed=changed, base=base, head=head,
                              seeds=seeds)
    except Exception as exc:  # noqa: BLE001 - degrade to an explicit error
        plan_error = f"{type(exc).__name__}: {exc}"
        plan_full = {
            "ok": False,
            "mode": "error",
            "tests": [],
            "test_count": 0,
            "commands": [],
            "engine_rebuild": False,
            "full_gate": False,
            "full_reasons": [],
            "reasons": [],
            "warnings": [f"test planner failed: {plan_error}"],
            "changed": [],
            "seeds": [],
        }
    plan = _compact_plan(plan_full, limit)

    seed_entries = _seed_entries(memory, plan_full.get("seeds") or [], limit)
    research_entries = _research_entries(
        memory, plan_full.get("changed") or [], limit)
    next_commands = _next_commands(doctor, plan)

    warnings: list[str] = []
    candidates = list(doctor.get("warnings") or [])
    candidates.extend(plan.get("warnings") or [])
    for item in candidates:
        text = str(item or "").strip()
        if text and text not in warnings:
            warnings.append(text)
        if len(warnings) >= MAX_WARNINGS:
            break
    if plan_error and plan_error not in warnings:
        warnings.append(plan_error)

    state = str(doctor.get("state") or "unknown")
    result: dict[str, Any] = {
        "schema": SCHEMA,
        "ok": plan["ok"] and state != "blocked",
        "state": state,
        "root": str(root),
        "doctor": doctor,
        "plan": plan,
        "seeds": seed_entries,
        "research": research_entries,
        "next_commands": next_commands,
        "warnings": warnings,
        "agent_note": _agent_note(doctor, plan, research_entries),
    }
    if plan_error:
        result["error"] = plan_error
    return result


def _tool_context(memory: Any, args: Mapping[str, Any]) -> Any:
    return context_summary(
        memory,
        changed=args.get("changed"),
        base=args.get("base"),
        head=args.get("head"),
        seeds=args.get("seeds"),
        max_tests=args.get("max_tests", MAX_TESTS_DEFAULT),
    )


CONTEXT_TOOL = {
    "name": "ocelot_context",
    "description": (
        "Compact Ocelot session context: worktree/engine/dataset doctor, a "
        "conservative test plan, seed blast radius, research chain links and "
        "the next commands. Read-only; never runs tests or builds."),
    "inputSchema": {
        "type": "object",
        "properties": {
            "changed": {
                "type": "array",
                "items": {"type": "string"},
                "description": "changed repository-relative paths",
            },
            "base": {"type": "string", "description": "git base revision"},
            "head": {"type": "string", "description": "git head revision"},
            "seeds": {
                "type": "array",
                "items": {"type": "string"},
                "description": "changed seed ids",
            },
            "max_tests": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_TESTS_LIMIT,
                "default": MAX_TESTS_DEFAULT,
            },
        },
        "additionalProperties": False,
    },
}
context_tool = CONTEXT_TOOL


def register_context(registry: Any) -> None:
    """Register the ``ocelot_context`` graph query and MCP tool."""
    registry.add_graph_query(
        "ocelot_context",
        lambda memory, **kw: context_summary(
            memory,
            changed=kw.get("changed"),
            base=kw.get("base"),
            head=kw.get("head"),
            seeds=kw.get("seeds"),
            max_tests=kw.get("max_tests", MAX_TESTS_DEFAULT),
        ),
    )
    registry.add_mcp_tool(CONTEXT_TOOL, _tool_context)


def format_text(result: Mapping[str, Any]) -> str:
    """A compact agent-facing rendering of :func:`context_summary`."""
    lines = [f"Ocelot context: {result.get('state', 'unknown')}"]
    lines.append(f"root: {result.get('root', '')}")

    doctor = result.get("doctor") or {}
    worktree = doctor.get("worktree") or {}
    if worktree.get("available"):
        lines.append(
            f"worktree: {worktree.get('branch') or '(detached)'} @ "
            f"{worktree.get('head') or '-'} | dirty: {worktree.get('dirty')} | "
            f"worktrees: {worktree.get('worktree_count', 0)}")
    else:
        lines.append("worktree: git unavailable")

    engine = doctor.get("engine") or {}
    lines.append(
        f"engine: {engine.get('state', 'unknown')} | "
        f"native: {'yes' if engine.get('native') else 'no'} | "
        f"sha256: {(engine.get('sha256') or '-')[:12]} | "
        f"source match: {engine.get('source_matches_record')}")
    datasets = doctor.get("datasets") or {}
    lines.append(
        f"datasets: data {'yes' if datasets.get('data') else 'no'} | "
        f"baseline {'yes' if datasets.get('baseline') else 'no'}")

    plan = result.get("plan") or {}
    flags: list[str] = []
    if plan.get("engine_rebuild"):
        flags.append("engine rebuild")
    if plan.get("full_gate"):
        flags.append("full gate")
    suffix = (" | " + ", ".join(flags)) if flags else ""
    lines.append(
        f"plan: {plan.get('mode', 'unknown')} | "
        f"{plan.get('test_count', 0)} test file(s){suffix}")
    for path in plan.get("tests") or []:
        lines.append(f"  - {path}")
    if plan.get("tests_truncated"):
        lines.append("  ... more selected tests not shown")
    for reason in plan.get("full_reasons") or []:
        lines.append(f"  full: {reason}")

    for seed in result.get("seeds") or []:
        if seed.get("error"):
            lines.append(f"seed {seed.get('id')}: {seed.get('error')}")
            continue
        dependents = ", ".join(seed.get("direct_dependents") or []) or "-"
        lines.append(
            f"seed {seed.get('id')}: {seed.get('blast_radius')} | "
            f"fan-out {seed.get('fanout', 0)} | dependents: {dependents}")
        for path in seed.get("related_tests") or []:
            lines.append(f"  test: {path}")

    for research in result.get("research") or []:
        if not research.get("available"):
            lines.append(f"research {research.get('path')}: "
                         f"{research.get('error') or 'not indexed'}")
            continue
        chain = research.get("chain") or {}
        documents = sum(len(chain.get(key) or []) for key in chain)
        flags_text = []
        if research.get("stale"):
            flags_text.append("stale engine")
        if research.get("missing_gate"):
            flags_text.append("missing gate")
        suffix = (" (" + ", ".join(flags_text) + ")") if flags_text else ""
        lines.append(
            f"research {research.get('path')}: {documents} linked "
            f"document(s){suffix}")

    if result.get("next_commands"):
        lines.append("next:")
        lines.extend(f"  - {command}" for command in result["next_commands"])
    for warning in _bounded(result.get("warnings") or [], 5):
        lines.append(f"warning: {warning}")
    if result.get("agent_note"):
        lines.append(f"note: {result['agent_note']}")
    return "\n".join(lines)


__all__ = [
    "CONTEXT_TOOL",
    "SCHEMA",
    "context_summary",
    "context_tool",
    "format_text",
    "register_context",
]
