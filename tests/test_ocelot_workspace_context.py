"""Tests for the compact Ocelot agent context and its MCP surface."""
from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest

from ozagia.core.memory import DevMemory, build_index


def _load_fixture_builder() -> Any:
    path = Path(__file__).resolve().parent / "fixtures" / "ocelot_workspace_repo.py"
    spec = importlib.util.spec_from_file_location(
        "_ocelot_workspace_context_fixture", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


FIXTURE = _load_fixture_builder()
RESEARCH_PREREG = "docs/research/block371000/block371000_PREREG.md"
RESEARCH_RESULTS = "docs/research/block371000/block371000_RESULTS.md"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, DevMemory]:
    monkeypatch.delenv("OZAGIA_CONFIG", raising=False)
    monkeypatch.delenv("OZAGIA_ADAPTERS", raising=False)
    root = tmp_path / "ocelot-context"
    FIXTURE.write_workspace(root)
    _write(root / RESEARCH_PREREG, """\
# Block 371000 prereg

Block 371000 uses `seeds/shared.json` and gates on
`tests/test_shared_migrated.py`.
""")
    _write(root / RESEARCH_RESULTS, """\
# Block 371000 results

Block 371000 measured the shared seed; the gate is
`tests/test_shared_migrated.py`.
""")
    out = root / FIXTURE.OUT_REL
    journal = root / FIXTURE.JOURNAL_REL
    build_index(root, out, include_docs="features", include_git=False)
    return root, DevMemory(out, journal, root=root)


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, DevMemory]:
    return _build(tmp_path, monkeypatch)


@pytest.fixture
def context_api() -> Any:
    return pytest.importorskip(
        "ozagia.integrations.ocelot.workspace_context",
        reason="the Ocelot agent context has not been written yet")


def test_context_compacts_doctor_plan_and_seed(
        workspace: tuple[Path, DevMemory], context_api: Any) -> None:
    _root, mem = workspace
    result = context_api.context_summary(mem, seeds=["shared"], max_tests=5)

    assert result["schema"] == context_api.SCHEMA
    assert result["ok"] is True
    assert result["state"] in {"ready", "warn"}
    doctor = result["doctor"]
    assert doctor["worktree"]["available"] in {True, False}
    assert doctor["engine"]["native"] is True
    assert doctor["datasets"]["data"] is True
    assert doctor["datasets"]["baseline"] is True

    plan = result["plan"]
    assert plan["mode"] == "full"
    assert plan["full_gate"] is True
    assert plan["test_count"] > 0
    assert len(plan["tests"]) <= 5
    assert plan["full_reasons"]

    assert result["seeds"], result
    seed = result["seeds"][0]
    assert seed["id"] == "shared"
    assert seed["blast_radius"] in {"shared", "core"}
    assert "local" in seed["direct_dependents"]
    assert "tests/test_shared_migrated.py" in seed["related_tests"]

    assert "full migrated gate required; never -n auto" in result["agent_note"]
    assert any("tests -k migrated" in command
               for command in result["next_commands"])


def test_context_caps_selected_tests(
        workspace: tuple[Path, DevMemory], context_api: Any) -> None:
    _root, mem = workspace
    changed = [
        "tests/test_local_migrated.py",
        "tests/test_shared_migrated.py",
        "tests/test_world.py",
    ]
    result = context_api.context_summary(mem, changed=changed, max_tests=2)
    plan = result["plan"]
    assert plan["mode"] == "affected"
    assert plan["test_count"] >= 3
    assert plan["tests_shown"] == 2
    assert len(plan["tests"]) == 2
    assert plan["tests_truncated"] is True


def test_context_engine_rebuild_note(
        workspace: tuple[Path, DevMemory], context_api: Any) -> None:
    _root, mem = workspace
    result = context_api.context_summary(mem, changed=[FIXTURE.RUST_SOURCE])
    assert result["plan"]["engine_rebuild"] is True
    assert "engine rebuild required; do not measure" in result["agent_note"]
    assert any("engine_cargo_test.sh" in command
               for command in result["plan"]["commands"])


def test_context_docs_and_research_chain(
        workspace: tuple[Path, DevMemory], context_api: Any) -> None:
    _root, mem = workspace
    result = context_api.context_summary(mem, changed=[RESEARCH_PREREG])

    assert result["plan"]["mode"] == "none"
    assert result["plan"]["tests"] == []
    assert "documentation-only change; no pytest selected" in result["agent_note"]

    assert result["research"], result
    research = result["research"][0]
    assert research["path"] == RESEARCH_PREREG
    assert research["available"] is True
    assert RESEARCH_PREREG in research["chain"]["prereg"]
    assert RESEARCH_RESULTS in research["chain"]["results"]
    assert "tests/test_shared_migrated.py" in research["tests"]
    assert "shared" in research["seeds"]

    text = context_api.format_text(result)
    assert text.startswith("Ocelot context:")
    assert "research " + RESEARCH_PREREG in text
    assert "note:" in text


def test_context_tool_spec_and_registration(
        workspace: tuple[Path, DevMemory], context_api: Any) -> None:
    _root, mem = workspace

    class Registry:
        def __init__(self) -> None:
            self.queries: dict[str, Any] = {}
            self.tools: dict[str, Any] = {}

        def add_graph_query(self, name: str, fn: Any) -> None:
            self.queries[name] = fn

        def add_mcp_tool(self, spec: dict[str, Any], fn: Any) -> None:
            self.tools[spec["name"]] = fn

    registry = Registry()
    context_api.register_context(registry)

    assert "ocelot_context" in registry.queries
    assert "ocelot_context" in registry.tools
    spec = context_api.CONTEXT_TOOL
    assert spec["name"] == "ocelot_context"
    properties = spec["inputSchema"]["properties"]
    assert set(properties) == {"changed", "base", "head", "seeds", "max_tests"}
    assert spec["inputSchema"]["additionalProperties"] is False

    payload = registry.queries["ocelot_context"](
        mem, seeds=["local"], max_tests=1)
    assert payload["plan"]["mode"] == "affected"
    assert len(payload["plan"]["tests"]) <= 1
    assert payload["seeds"][0]["id"] == "local"


def test_format_text_for_a_full_gate(
        workspace: tuple[Path, DevMemory], context_api: Any) -> None:
    _root, mem = workspace
    result = context_api.context_summary(mem, seeds=["shared"], max_tests=3)
    text = context_api.format_text(result)
    assert "plan: full" in text
    assert "full gate" in text
    assert "full migrated gate required; never -n auto" in text
    assert "next:" in text


def test_ocelot_task_spec_loads_through_the_library() -> None:
    from ozagia.core import task_library as library
    from ozagia.core import tasks

    path = library.builtin_dir() / "coding" / "ocelot_workspace.toml"
    assert path.is_file(), path
    spec = tasks.load_spec(path)
    assert spec.id == "ocelot_workspace"
    assert spec.version == 1
    assert spec.requires_graph is True
    assert spec.test_policy == {
        "mode": "affected",
        "fallback": "all",
        "max_tests": 20,
        "timeout_s": 7200,
    }
    assert spec.gates.before == ("resources_fit", "snapshot_fresh")
    assert spec.gates.after == ("schema_valid", "evidence_present")
    text = " ".join([spec.objective, *(step.goal for step in spec.steps)]).lower()
    assert "engine" in text
    assert "full" in text
    assert "-n auto" in text

    task = library.get_task("ocelot_workspace")
    assert task.id == "ocelot_workspace"
    assert task.domain == "coding"
    assert task.source == "builtin"
    defaults = task.entry()["defaults"]
    assert defaults["gates"]["after"] == ["schema_valid", "evidence_present"]
    assert defaults["requires_graph"] is True
