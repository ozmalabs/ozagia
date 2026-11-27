"""Tests for the Ocelot session-start hint hook."""
from __future__ import annotations

import json
import threading
import types
from typing import Any

import pytest

import ozagia.adapters as adapters
from ozagia.daemon.server import DaemonState
from ozagia.integrations.ocelot import workspace_hook as hook

MARKER = "[ozagia ocelot]"


class _Memory:
    def __init__(self, root, *, result: Any = None,
                 error: BaseException | None = None,
                 missing: bool = False) -> None:
        self.raw_repo_root = root
        self.result = result
        self.error = error
        self.missing = missing
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def adapter_query(self, name: str, **kwargs: Any) -> Any:
        self.calls.append((name, kwargs))
        if self.error is not None:
            raise self.error
        if self.missing:
            return {"ok": False,
                    "error": f"no adapter graph query {name!r}; known: none"}
        return self.result


def _install_registry(monkeypatch: pytest.MonkeyPatch, *,
                      present: bool = True) -> None:
    queries = {"ocelot_context": object()} if present else {}
    monkeypatch.setattr(
        adapters, "registry",
        lambda root=None, **kwargs: types.SimpleNamespace(
            graph_queries=queries))


def _payload(root) -> dict[str, Any]:
    return {
        "schema": "ozagia.ocelot.context.v1",
        "ok": True,
        "state": "warn",
        "root": str(root),
        "doctor": {
            "worktree": {"available": True, "branch": "main",
                         "head": "abcdef123456", "dirty": 2,
                         "worktree_count": 1},
            "engine": {"state": "warn", "native": True,
                       "sha256": "b" * 64, "source_matches_record": True},
            "datasets": {"data": True, "baseline": False},
            "warnings": ["engine build is stale"],
        },
        "plan": {
            "mode": "affected",
            "test_count": 7,
            "tests": [f"tests/test_{index}.py" for index in range(4)],
            "tests_truncated": True,
            "full_gate": False,
            "full_reasons": [],
            "engine_rebuild": False,
        },
        "seeds": [],
        "research": [],
        "next_commands": ["pytest -q tests/test_0.py"],
        "warnings": ["engine build is stale", "baseline missing"],
        "agent_note": "verify the selected tests",
    }


def _hint(context: str = "candidate context") -> dict[str, Any]:
    return {"context": context,
            "meta": {"intent": "hint", "keys": ["file:a.py"]}}


def test_hint_context_is_unavailable_without_the_adapter(
        tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    _install_registry(monkeypatch, present=False)
    memory = _Memory(tmp_path, missing=True)

    result = hook.ocelot_hint_context(memory)

    assert result["available"] is False
    assert result["context"] == "" and result["chars"] == 0
    assert memory.calls == [("ocelot_context", {"max_tests": 5})]

    # A memory with no adapter-query API at all is also safe.
    bare = hook.ocelot_hint_context(object())
    assert bare["available"] is False
    assert bare["context"] == ""


def test_hint_context_formats_bounds_and_reports_the_plan(
        tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    _install_registry(monkeypatch)
    memory = _Memory(tmp_path, result=_payload(tmp_path))

    result = hook.ocelot_hint_context(memory, max_chars=300)

    assert memory.calls == [("ocelot_context", {"max_tests": 5})]
    assert result["available"] is True
    assert result["context"].startswith("Ocelot context: warn")
    assert "plan: affected" in result["context"]
    assert len(result["context"]) <= 300
    assert result["chars"] == len(result["context"])
    assert result["state"] == "warn"
    assert result["plan_mode"] == "affected"
    assert result["test_count"] == 7
    assert result["warnings"] == ["engine build is stale", "baseline missing"]
    assert result["meta"]["truncated"] is True
    assert result["meta"]["raw_chars"] > result["chars"]


def test_augment_hint_is_opencode_only_and_isolates_failures(
        tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    _install_registry(monkeypatch)
    memory = _Memory(tmp_path, result=_payload(tmp_path))

    # Not OpenCode: the hint is returned untouched.
    other = _hint()
    assert hook.augment_hint(memory, other, harness="claude",
                             session="s1") is other
    assert MARKER not in other["context"]

    # No adapter: the hint is returned untouched, with no Ocelot metadata.
    _install_registry(monkeypatch, present=False)
    unavailable_memory = _Memory(tmp_path, missing=True)
    plain = _hint()
    unchanged = hook.augment_hint(unavailable_memory, plain, harness="opencode",
                                  session="s1")
    assert unchanged == plain
    assert MARKER not in unchanged["context"]
    assert "ocelot" not in unchanged["meta"]

    # Available: append exactly one marked block, preserving the original.
    _install_registry(monkeypatch)
    base = _hint()
    result = hook.augment_hint(memory, base, harness="opencode", session="s1")
    assert result["context"].startswith("candidate context\n\n" + MARKER)
    assert "read-only" in result["context"].lower()
    assert "ocelot_*" in result["context"]
    assert result["context"].count(MARKER) == 1
    assert result["meta"]["intent"] == "hint"
    assert result["meta"]["keys"] == ["file:a.py"]
    ocelot = result["meta"]["ocelot"]
    assert ocelot["available"] is True
    assert ocelot["delivered"] is True
    assert ocelot["state"] == "warn"
    assert ocelot["mode"] == "affected"
    assert ocelot["test_count"] == 7
    assert ocelot["truncated"] in {True, False}
    assert len(result["context"]) <= len(base["context"]) + 2 + hook.MAX_HINT_CHARS

    # Calling again on the augmented hint is a no-op.
    again = hook.augment_hint(memory, result, harness="opencode", session="s1")
    assert again["context"] == result["context"]
    assert again["context"].count(MARKER) == 1

    # A failing query keeps the bytes and records a bounded error.
    failing = _Memory(tmp_path, error=RuntimeError("x" * 1000))
    before = _hint()
    failed = hook.augment_hint(failing, before, harness="opencode",
                               session="s2")
    assert failed["context"] == before["context"]
    failed_meta = failed["meta"]["ocelot"]
    assert failed_meta["available"] is True
    assert failed_meta["delivered"] is False
    assert "RuntimeError" in failed_meta["error"]
    assert len(failed_meta["error"]) <= hook.MAX_ERROR_CHARS


def test_daemon_hint_delivers_ocelot_once_per_session(
        tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    from ozagia.core import proactive_context
    monkeypatch.setattr(
        proactive_context, "build_context",
        lambda memory, prompt, **kwargs: {
            "context": "candidate context", "keys": ["file:a.py"]})
    _install_registry(monkeypatch)
    memory = _Memory(tmp_path, result=_payload(tmp_path))
    state = DaemonState(tmp_path, memory, tmp_path / "journal.jsonl")

    def hint(session: str, harness: str = "opencode") -> dict[str, Any]:
        return state.dispatch({"op": "hint", "prompt": "x",
                               "harness": harness, "session": session})

    first = hint("s1")
    assert first["ok"] is True
    assert first["context"].startswith("candidate context\n\n" + MARKER)
    assert first["context"].count(MARKER) == 1
    ocelot = first["meta"]["ocelot"]
    assert ocelot["delivered"] is True
    assert ocelot["test_count"] == 7
    assert len(json.dumps(first["meta"])) < 1000
    assert len(memory.calls) == 1

    # The same session never gets a second block or another adapter call.
    second = hint("s1")
    assert second["context"] == "candidate context"
    assert "ocelot" not in second["meta"]
    assert first["context"].count(MARKER) + second["context"].count(MARKER) == 1
    assert len(memory.calls) == 1

    # A different session gets exactly one block of its own.
    other_session = hint("s2")
    assert other_session["context"].count(MARKER) == 1
    assert len(memory.calls) == 2

    # Other harnesses stay unchanged and do not consume a session slot.
    other_harness = hint("s3", harness="claude")
    assert other_harness["context"] == "candidate context"
    assert "ocelot" not in other_harness["meta"]
    assert len(memory.calls) == 2

    # The delivered-session set is bounded.
    for index in range(270):
        hint(f"bulk-{index}")
    assert len(state.ocelot_hint_sessions) <= 256
    assert state._ocelot_hint_pending == set()


def test_concurrent_first_hints_deliver_the_block_once(
        tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    _install_registry(monkeypatch)
    memory = _Memory(tmp_path, result=_payload(tmp_path))
    state = DaemonState(tmp_path, memory, tmp_path / "journal.jsonl")
    entered = threading.Event()
    release = threading.Event()
    original = hook.augment_hint
    results: list[dict[str, Any]] = []
    errors: list[BaseException] = []

    def slow_augment(memory_arg: Any, hint_arg: dict[str, Any], *,
                     harness: str, session: Any = None) -> dict[str, Any]:
        entered.set()
        assert release.wait(5), "concurrent test did not release the augment"
        return original(memory_arg, hint_arg, harness=harness, session=session)

    monkeypatch.setattr(hook, "augment_hint", slow_augment)

    def first() -> None:
        try:
            results.append(state._ocelot_hint(_hint(), "opencode", "race"))
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    thread = threading.Thread(target=first)
    thread.start()
    try:
        assert entered.wait(5)
        # This call starts while the first one is still inside augment_hint.
        second = state._ocelot_hint(_hint(), "opencode", "race")
    finally:
        release.set()
    thread.join(5)

    assert not thread.is_alive()
    assert errors == []
    assert len(results) == 1
    assert results[0]["context"].count(MARKER) == 1
    assert results[0]["meta"]["ocelot"]["delivered"] is True
    assert second == _hint()
    assert MARKER not in second["context"]
    assert "ocelot" not in second["meta"]
    assert len(memory.calls) == 1
    assert "race" in state.ocelot_hint_sessions
    assert state._ocelot_hint_pending == set()


def test_daemon_hint_without_a_session_uses_one_anonymous_slot(
        tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    from ozagia.core import proactive_context
    monkeypatch.setattr(
        proactive_context, "build_context",
        lambda memory, prompt, **kwargs: {
            "context": "candidate context", "keys": ["file:a.py"]})
    _install_registry(monkeypatch)
    memory = _Memory(tmp_path, result=_payload(tmp_path))
    state = DaemonState(tmp_path, memory, tmp_path / "journal.jsonl")

    def hint(**extra: Any) -> dict[str, Any]:
        return state.dispatch({"op": "hint", "prompt": "x",
                               "harness": "opencode", **extra})

    first = hint()
    assert first["context"].count(MARKER) == 1
    assert first["meta"]["ocelot"]["delivered"] is True

    # No session, a non-string session and an empty session all share the
    # one anonymous slot, so none of them gets another block.
    for extra in ({}, {"session": None}, {"session": {"bad": 1}},
                  {"session": ""}):
        again = hint(**extra)
        assert again["context"] == "candidate context"
        assert "ocelot" not in again["meta"]
    assert len(memory.calls) == 1
    assert state.ocelot_hint_sessions == {}

    # The anonymous slot never consumes a named session's own delivery.
    named = hint(session="named")
    assert named["context"].count(MARKER) == 1
    assert named["meta"]["ocelot"]["delivered"] is True
    assert len(memory.calls) == 2


def test_failed_augment_does_not_poison_the_session_marker(
        tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    _install_registry(monkeypatch)
    memory = _Memory(tmp_path, result=_payload(tmp_path))
    state = DaemonState(tmp_path, memory, tmp_path / "journal.jsonl")
    original = hook.augment_hint

    def broken(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("augment failed")

    monkeypatch.setattr(hook, "augment_hint", broken)
    failed = state._ocelot_hint(_hint(), "opencode", "retry")
    assert failed == _hint()
    assert "retry" not in state.ocelot_hint_sessions
    assert "retry" not in state._ocelot_hint_pending

    # An adapter failure (delivered False) also clears the claim.
    monkeypatch.setattr(hook, "augment_hint", original)
    memory.error = RuntimeError("adapter failed")
    skipped = state._ocelot_hint(_hint(), "opencode", "retry")
    assert skipped["meta"]["ocelot"]["delivered"] is False
    assert "retry" not in state.ocelot_hint_sessions
    assert "retry" not in state._ocelot_hint_pending

    # Once the failure clears, the same session can still receive its block.
    memory.error = None
    retried = state._ocelot_hint(_hint(), "opencode", "retry")
    assert retried["context"].count(MARKER) == 1
    assert retried["meta"]["ocelot"]["delivered"] is True
    assert "retry" in state.ocelot_hint_sessions
    assert state._ocelot_hint_pending == set()
    assert len(memory.calls) == 2


def test_hint_without_ocelot_is_unchanged(
        tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    from ozagia.core import proactive_context
    monkeypatch.setattr(
        proactive_context, "build_context",
        lambda memory, prompt, **kwargs: {
            "context": "candidate context", "keys": ["file:a.py"]})
    _install_registry(monkeypatch, present=False)
    memory = _Memory(tmp_path, missing=True)
    state = DaemonState(tmp_path, memory, tmp_path / "journal.jsonl")

    result = state.dispatch({"op": "hint", "prompt": "x",
                             "harness": "opencode", "session": "plain"})

    assert result["ok"] is True
    assert result["context"] == "candidate context"
    assert result["meta"] == {
        "intent": "hint",
        "keys": ["file:a.py"],
        "structural_intent": None,
        "freshness": "unknown",
        "truncated": False,
        "estimated_tokens": None,
        "token_estimate_method": None,
        "allocation": None,
        "infra_results": 0,
        "infra_edges": 0,
    }
    assert state.ocelot_hint_sessions == {}


def test_hint_context_through_the_registered_fixture_adapter(
        tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib.util
    from pathlib import Path

    monkeypatch.delenv("OZAGIA_CONFIG", raising=False)
    monkeypatch.delenv("OZAGIA_ADAPTERS", raising=False)
    path = Path(__file__).resolve().parent / "fixtures" / "ocelot_workspace_repo.py"
    spec = importlib.util.spec_from_file_location(
        "_ocelot_workspace_hook_fixture", path)
    assert spec is not None and spec.loader is not None
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    _root, memory = fixture.build_workspace(tmp_path / "ocelot")

    result = hook.ocelot_hint_context(memory)

    assert result["available"] is True
    assert result["context"].startswith("Ocelot context:")
    assert result["state"] in {"ready", "warn"}
    assert result["chars"] == len(result["context"])
    assert len(result["context"]) <= hook.MAX_HINT_CHARS
