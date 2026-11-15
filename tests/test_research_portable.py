from __future__ import annotations

import pytest

from ozagia.research.graph import ResearchError, ResearchGraph
from ozagia.research.sources import RootManifest, SourceError, build_index, retrieve
from ozagia.research.checks import check_finite_math
from ozagia.research.provenance import verify_engine_provenance


def test_graph_persists_under_ozagia_and_reads_legacy(tmp_path, monkeypatch):
    monkeypatch.delenv("OZAGIA_STATE_DIR", raising=False)
    graph = ResearchGraph.create("Question", root=tmp_path, investigation_id="portable")
    assert graph.path == tmp_path / ".ozagia/research/portable.json"
    assert ResearchGraph.load("portable", root=tmp_path).state() == graph.state()

    legacy_root = tmp_path / "legacy"
    legacy = ResearchGraph.create("Legacy", root=legacy_root, investigation_id="old")
    legacy_path = legacy_root / "var/ocelot_research/old.json"
    legacy_path.parent.mkdir(parents=True)
    legacy.path.replace(legacy_path)
    migrated = ResearchGraph.load("old", root=legacy_root)
    new_path = legacy_root / ".ozagia/research/old.json"
    assert migrated.path == new_path
    assert not new_path.exists()
    migrated.record("Frame", name="migrated")
    assert new_path.is_file()
    assert legacy_path.is_file()
    with pytest.raises(ResearchError, match="already exists"):
        ResearchGraph.create("Collision", root=legacy_root, investigation_id="old")


def test_graph_honors_relative_state_dir_inside_root(tmp_path, monkeypatch):
    monkeypatch.setenv("OZAGIA_STATE_DIR", "private-state")
    graph = ResearchGraph.create("Question", root=tmp_path, investigation_id="relative")
    assert graph.path == tmp_path / "private-state/research/relative.json"
    assert graph.path.is_file()


def test_graph_honors_explicit_absolute_state_dir(tmp_path, monkeypatch):
    state = tmp_path.parent / f"{tmp_path.name}-external-state"
    monkeypatch.setenv("OZAGIA_STATE_DIR", str(state))
    graph = ResearchGraph.create("Question", root=tmp_path, investigation_id="absolute")
    assert graph.path == state / "research/absolute.json"
    assert graph.path.is_file()
    assert not (tmp_path / ".ozagia").exists()


def test_source_index_is_explicit_and_fenced(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "notes.md").write_text("# Findings\n\nA sourced result.\n", encoding="utf-8")
    index = build_index([RootManifest("fixture", tmp_path, ["docs/notes.md"])])
    assert retrieve(index, "sourced result")["results"][0]["citation"]["path"] == "docs/notes.md"
    with pytest.raises(SourceError):
        build_index([RootManifest("fixture", tmp_path, ["../outside.md"])])


def test_project_specific_provenance_is_unavailable_without_target_imports():
    assert verify_engine_provenance()["status"] == "unavailable"
    assert check_finite_math(
        domain="N",
        bounds="0..10",
        oracle_independent=False,
        verification_label="finite_verification",
    )["complete"]
