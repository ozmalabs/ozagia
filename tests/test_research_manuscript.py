from __future__ import annotations

import json
from pathlib import Path

import pytest

from ozagia.research import manuscript
from ozagia.research import references as refs
from ozagia.research import review
from ozagia.research.graph import ResearchError, ResearchGraph


def _graph(tmp_path, investigation_id: str) -> ResearchGraph:
    return ResearchGraph.create(
        "Manuscript checks",
        root=tmp_path,
        investigation_id=investigation_id,
    )


def _reference(graph, key: str, title: str, **fields):
    return refs.record_reference(graph, {
        "title": title,
        "citation_key": key,
        **fields,
    })["reference"]


def test_check_manuscript_reports_unresolved_retracted_missing_and_unused(tmp_path):
    graph = _graph(tmp_path, "manuscript-check")
    _reference(graph, "known2024", "Known Work")
    retracted = _reference(
        graph, "retracted2020", "Retracted Work",
        retraction={"status": "retracted", "provider": "openalex"},
    )
    concern = _reference(
        graph, "concern2021", "Expression of Concern",
        retraction={"status": "concern"},
    )
    unused = _reference(graph, "unused2019", "Unused Work")
    linked = _reference(graph, "linked2022", "Source-Linked Work")
    source = graph.record("Source", locator={"path": "papers/linked.pdf"})
    graph.link(source["id"], "manifestation_of", linked["id"])

    graph.record(
        "Claim", wording="A sourced claim.", claim_type="empirical_observation",
        status="open", source_locator={"path": "draft.md", "start_line": 3},
    )
    unsourced = graph.record(
        "Claim", wording="A claim without a source.", claim_type="inference",
        status="unresolved",
    )

    body = (
        "Known [@known2024]. Retracted [@retracted2020]. "
        "Concern \\citep[see][p. 2]{concern2021}. Missing [@missing2024].\n"
    )
    checks = manuscript.check_manuscript(graph.state(), body)

    assert checks["unresolved_citations"] == ["missing2024"]
    assert {item["record_id"] for item in checks["retracted_references"]} == {
        retracted["id"], concern["id"]}
    assert {item["retraction_status"] for item in checks["retracted_references"]} == {
        "retracted", "concern"}
    assert {item["record_id"] for item in checks["unused_references"]} == {unused["id"]}
    assert {item["record_id"] for item in checks["missing_claim_sources"]} == {
        unsourced["id"]}
    assert checks["counts"] == {
        "citations": 4,
        "resolved_citations": 3,
        "unresolved_citations": 1,
        "references": 5,
        "cited_references": 3,
        "retracted_references": 2,
        "claims": 2,
        "missing_claim_sources": 1,
        "unused_references": 1,
    }
    assert checks["ok"] is False
    assert checks["truth_assessed"] is False

    relaxed = manuscript.check_manuscript(
        graph.state(), body, require_claim_sources=False)
    assert relaxed["missing_claim_sources"] == []
    assert relaxed["counts"]["missing_claim_sources"] == 0
    assert relaxed["require_claim_sources"] is False
    assert relaxed["ok"] is False


def test_check_manuscript_clean_records_are_ok(tmp_path):
    graph = _graph(tmp_path, "manuscript-clean")
    _reference(graph, "clean2024", "Clean Work")
    graph.record(
        "Claim", wording="A cited claim.", claim_type="inference",
        status="open", source_locator={"path": "draft.md"},
    )

    checks = manuscript.check_manuscript(
        graph.state(), "Only \\cite{clean2024} is needed here.\n")

    assert checks["ok"] is True
    assert checks["unresolved_citations"] == []
    assert checks["retracted_references"] == []
    assert checks["missing_claim_sources"] == []
    assert checks["unused_references"] == []
    assert checks["counts"]["citations"] == 1
    assert checks["counts"]["resolved_citations"] == 1


def test_build_manuscript_writes_deterministic_bundle(tmp_path):
    graph = _graph(tmp_path, "manuscript-bundle")
    _reference(graph, "test2024", "A Test Work")
    graph.record(
        "Claim", wording="A cited claim.", claim_type="empirical_observation",
        status="open", source_locator={"path": "draft.md", "start_line": 2},
    )
    title = "A Test Manuscript"
    body = "First paragraph with [@test2024].\n\nSecond paragraph."
    output_dir = tmp_path / "bundle"

    result = manuscript.build_manuscript(
        graph, title=title, body=body, output_dir=output_dir)

    assert result["title"] == title
    assert result["output_dir"] == str(output_dir)
    assert result["count"] == 4
    assert set(result["bundle"]) == {"manuscript", "references", "claims", "checks"}
    assert result["checks"] == manuscript.check_manuscript(graph.state(), body)
    assert result["pandoc"]["status"] == "not_requested"
    assert result["truth_assessed"] is False

    paths = {name: Path(path) for name, path in result["bundle"].items()}
    assert paths["manuscript"].read_text(encoding="utf-8") == (
        f"# {title}\n\n{body}\n")
    assert paths["references"].read_text(encoding="utf-8") == (
        refs.export_bibtex(graph.state()))
    assert paths["claims"].read_bytes().decode("utf-8") == (
        review.evidence_table(graph.state(), format="csv")["content"])
    assert json.loads(paths["checks"].read_text(encoding="utf-8")) == result["checks"]

    snapshot = {name: path.read_bytes() for name, path in paths.items()}
    again = manuscript.build_manuscript(
        graph, title=title, body=body, output_dir=output_dir)
    assert again["bundle"] == result["bundle"]
    assert again["checks"] == result["checks"]
    assert {name: path.read_bytes() for name, path in
            ((name, Path(path)) for name, path in again["bundle"].items())} == snapshot


def test_default_output_directory_is_under_the_state_directory(tmp_path):
    graph = _graph(tmp_path, "manuscript-default")

    result = manuscript.build_manuscript(
        graph, title="Draft: First Pass!", body="No citations here.")

    expected = tmp_path / ".ozagia" / "research" / "exports" / "draft-first-pass"
    assert Path(result["output_dir"]) == expected
    assert (expected / "manuscript.md").read_text(encoding="utf-8") == (
        "# Draft: First Pass!\n\nNo citations here.\n")
    assert all(Path(path).is_file() for path in result["bundle"].values())


def test_pandoc_runner_receives_list_arguments(tmp_path):
    graph = _graph(tmp_path, "manuscript-pandoc")
    _reference(graph, "test2024", "A Test Work")
    output_dir = tmp_path / "pandoc-bundle"
    calls: list[tuple[list[str], str]] = []

    def runner(argv, cwd):
        calls.append((argv, cwd))
        return {"returncode": 0, "stdout": "converted", "stderr": ""}

    result = manuscript.build_manuscript(
        graph, title="Converted", body="See [@test2024].",
        output_dir=output_dir, run_pandoc=True, runner=runner)

    assert len(calls) == 1
    argv, cwd = calls[0]
    assert isinstance(argv, list)
    assert argv == [
        "pandoc", "manuscript.md", "--citeproc", "--bibliography",
        "references.bib", "-o", "output.docx",
    ]
    assert cwd == str(output_dir)
    assert result["pandoc"]["status"] == "success"
    assert result["pandoc"]["returncode"] == 0
    assert result["pandoc"]["stdout"] == "converted"
    assert result["pandoc"]["output"] == str(output_dir / "output.docx")
    assert all(Path(path).is_file() for path in result["bundle"].values())


def test_pandoc_failure_is_reported_without_losing_the_bundle(tmp_path):
    graph = _graph(tmp_path, "manuscript-pandoc-failure")
    output_dir = tmp_path / "failed-pandoc"

    def runner(argv, cwd):
        return {"returncode": 2, "stdout": "", "stderr": "citation error"}

    result = manuscript.build_manuscript(
        graph, title="Failed", body="No citations.",
        output_dir=output_dir, run_pandoc=True, runner=runner)

    assert result["pandoc"]["status"] == "failed"
    assert result["pandoc"]["returncode"] == 2
    assert result["pandoc"]["stderr"] == "citation error"
    assert (output_dir / "manuscript.md").is_file()


def test_missing_pandoc_is_reported_as_unavailable(tmp_path, monkeypatch):
    graph = _graph(tmp_path, "manuscript-no-pandoc")
    output_dir = tmp_path / "no-pandoc"
    monkeypatch.setattr(manuscript.shutil, "which", lambda name: None)

    result = manuscript.build_manuscript(
        graph, title="No Pandoc", body="No citations.",
        output_dir=output_dir, run_pandoc=True)

    assert result["pandoc"]["requested"] is True
    assert result["pandoc"]["available"] is False
    assert result["pandoc"]["status"] == "unavailable"
    assert "pandoc" in result["pandoc"]["reason"]
    assert (output_dir / "manuscript.md").is_file()
    assert (output_dir / "checks.json").is_file()


def test_manuscript_error_is_research_error_and_rejects_unsafe_format(tmp_path):
    graph = _graph(tmp_path, "manuscript-invalid")
    assert issubclass(manuscript.ManuscriptError, ResearchError)

    with pytest.raises(manuscript.ManuscriptError):
        manuscript.build_manuscript(
            graph, title="Unsafe", body="Body.",
            output_dir=tmp_path / "unsafe", pandoc_format="../escape")
