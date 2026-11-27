from __future__ import annotations

from pathlib import Path

from ozagia.core.memory import DevMemory, build_index
from ozagia.core.project_invariants import declarations


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_invariants_are_source_backed_and_ranked_for_context(tmp_path: Path) -> None:
    _write(tmp_path / "src" / "world.py", "def validate(action): return True\n")
    _write(tmp_path / "tests" / "test_world.py", "def test_world(): pass\n")
    _write(tmp_path / ".ozagia.toml", '''
[invariants.backend_world_boundary]
statement = "Backend proposes; world validates before applying."
sources = ["src/world.py"]
checks = ["tests/test_world.py"]

[invariants.seeded_replay]
statement = "A seeded run replays deterministically."
sources = ["src/world.py"]
checks = ["tests/test_world.py"]
''')

    result = declarations(tmp_path, "world must validate backend proposals")

    assert result["ok"] is True
    assert [row["id"] for row in result["invariants"]] == [
        "backend_world_boundary"]
    row = result["invariants"][0]
    assert row["sources"] == ["src/world.py"]
    assert row["checks"] == ["tests/test_world.py"]
    assert row["source_exists"] == [True]
    assert row["check_exists"] == [True]
    assert row["evidence"] == "declared"


def test_memory_context_surfaces_relevant_invariants(tmp_path: Path) -> None:
    _write(tmp_path / "src" / "world.py", "def validate(action): return True\n")
    _write(tmp_path / "tests" / "test_world.py", "def test_world(): pass\n")
    _write(tmp_path / ".ozagia.toml", '''
[index]
source_dirs = ["src", "tests"]
test_dirs = ["tests"]

[invariants.backend_world_boundary]
statement = "Backend proposes; world validates before applying."
sources = ["src/world.py"]
checks = ["tests/test_world.py"]
''')
    graph = tmp_path / ".ozagia" / "graph.json"
    build_index(tmp_path, graph, include_docs="none", include_git=False,
                include_scripts=False)
    memory = DevMemory(graph, tmp_path / ".ozagia" / "journal.jsonl",
                       root=tmp_path)

    context = memory.context("preserve world validation")

    assert context["invariants"]["evidence"] == "declared"
    assert context["invariants"]["invariants"][0]["id"] == \
        "backend_world_boundary"
    assert context["invariants"]["invariants"][0]["checks"] == \
        ["tests/test_world.py"]


def test_invalid_invariant_declarations_are_reported(tmp_path: Path) -> None:
    _write(tmp_path / ".ozagia.toml", '''
[invariants.no_source]
statement = "This claim has no source path."
checks = ["../outside.py"]
''')

    result = declarations(tmp_path)

    assert result["ok"] is False
    assert result["invariants"] == []
    assert any("sources" in error for error in result["errors"])


def test_stopword_only_task_does_not_match_invariant(tmp_path: Path) -> None:
    _write(tmp_path / "src" / "world.py", "def validate(action): return True\n")
    _write(tmp_path / ".ozagia.toml", '''
[invariants.world_boundary]
statement = "The world validates each proposed action."
sources = ["src/world.py"]
''')

    result = declarations(tmp_path, "ensure it is valid")

    assert result["ok"] is True
    assert result["invariants"] == []


def test_context_counts_invariants_inside_the_reported_budget(
        tmp_path: Path) -> None:
    _write(tmp_path / "src" / "world.py", "def validate(action): return True\n")
    _write(tmp_path / "tests" / "test_world.py", "def test_world(): pass\n")
    _write(tmp_path / ".ozagia.toml", '''
[index]
source_dirs = ["src", "tests"]
test_dirs = ["tests"]

[invariants.backend_world_boundary]
statement = "Backend proposes actions, and the world validates each proposal before application."
sources = ["src/world.py"]
checks = ["tests/test_world.py"]
''')
    graph = tmp_path / ".ozagia" / "graph.json"
    build_index(tmp_path, graph, include_docs="none", include_git=False,
                include_scripts=False)
    memory = DevMemory(graph, tmp_path / ".ozagia" / "journal.jsonl",
                       root=tmp_path)

    context = memory.context("preserve backend world proposal validation",
                             budget_tokens=2000)

    budget = context["budget"]
    invariant_info = context["invariants"]
    snippet_tokens = sum(item["snippet"]["estimated_tokens"]
                         for item in context["items"])
    assert invariant_info["invariants"]
    assert invariant_info["estimated_tokens"] > 0
    assert budget["estimated_tokens"] == (
        snippet_tokens + invariant_info["estimated_tokens"])
    assert budget["estimated_tokens"] <= budget["budget_tokens"]


def test_context_prefers_task_matching_configured_current_doc(
        tmp_path: Path) -> None:
    _write(tmp_path / "docs" / "FEATURE_PHASES.md", """# Current feature phases

Current status: the seeded deterministic replay contract is in force.
""")
    _write(tmp_path / "docs" / "phases" / "phase001" / "PHASE001_REPORT.md",
           """# Phase 001 report

The seeded deterministic replay contract is in force.
""")
    _write(tmp_path / ".ozagia.toml", '''
[index]
key_docs = ["docs/FEATURE_PHASES.md"]
''')
    graph = tmp_path / ".ozagia" / "graph.json"
    build_index(tmp_path, graph, include_docs="all", include_git=False,
                include_scripts=False)
    memory = DevMemory(graph, tmp_path / ".ozagia" / "journal.jsonl",
                       root=tmp_path)

    context = memory.context("seeded deterministic replay contract")

    docs = [item for item in context["items"]
            if item["node"]["type"] == "Doc"]
    assert docs
    assert docs[0]["attrs"]["path"] == "docs/FEATURE_PHASES.md"
    assert "listed in [index] key_docs" in docs[0]["why"]

    historical_context = memory.context("phase001 report")
    assert any(item["attrs"]["path"].endswith("PHASE001_REPORT.md")
               for item in historical_context["items"]
               if item["node"]["type"] == "Doc")

    stopword_context = memory.context("ensure it is valid")
    assert not any("listed in [index] key_docs" in item["why"]
                   for item in stopword_context["items"])


def test_oversized_first_context_hit_is_omitted_from_small_budget(
        tmp_path: Path) -> None:
    _write(tmp_path / "src" / "large.py",
           "def important_behavior():\n    return '" + "detail " * 300 + "'\n")
    graph = tmp_path / ".ozagia" / "graph.json"
    build_index(tmp_path, graph, include_docs="none", include_git=False,
                include_scripts=False)
    memory = DevMemory(graph, tmp_path / ".ozagia" / "journal.jsonl",
                       root=tmp_path)

    context = memory.context("important behavior", budget_tokens=1)

    assert context["items"] == []
    assert context["budget"]["estimated_tokens"] <= 1


def test_context_skips_oversized_hit_and_keeps_later_compact_hit(
        tmp_path: Path) -> None:
    large_lines = ["# important behavior with detailed implementation notes"
                   for _ in range(100)]
    _write(tmp_path / "src" / "important_behavior_large.py",
           "\n".join(large_lines) + "\n")
    _write(tmp_path / "src" / "important_behavior_small.py",
           "def important_behavior():\n    return True\n")
    graph = tmp_path / ".ozagia" / "graph.json"
    build_index(tmp_path, graph, include_docs="none", include_git=False,
                include_scripts=False)
    memory = DevMemory(graph, tmp_path / ".ozagia" / "journal.jsonl",
                       root=tmp_path)

    context = memory.context("important behavior", budget_tokens=40)

    paths = [item["attrs"].get("path") for item in context["items"]]
    assert "src/important_behavior_small.py" in paths
    assert "src/important_behavior_large.py" not in paths
    assert context["budget"]["estimated_tokens"] <= 40
