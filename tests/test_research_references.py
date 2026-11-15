from __future__ import annotations

import json

import pytest

from ozagia.research import references as refs
from ozagia.research.graph import ResearchError, ResearchGraph
from ozagia.research.paper import ingest_paper, preview_paper


def _graph(tmp_path, files: dict[str, str], investigation_id: str = "refs-test"):
    source_root = tmp_path / "sources"
    source_root.mkdir(exist_ok=True)
    for name, content in files.items():
        path = source_root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return ResearchGraph.create(
        "Review references",
        root=tmp_path,
        investigation_id=investigation_id,
        roots=[{
            "root_id": "papers",
            "path": str(source_root),
            "repository": "fixture",
            "files": {name: "selected" for name in files},
        }],
    )


def test_bibtex_parser_handles_strings_crossref_and_identifiers():
    entries = refs.parse_bibtex(r"""
@string{venue = "Proceedings of Test"}
@article{smith2024,
  title = {A {Graph} Study},
  author = {Smith, Jane and Doe, John},
  year = {2024},
  journal = venue,
  doi = {https://doi.org/10.1234/ABC.1},
}
@inproceedings{lee2023,
  title = "Other Work",
  author = "Lee, Ann",
  year = 2023,
  crossref = {smith2024},
  eprint = {2401.12345v2},
  archivePrefix = {arXiv},
}
""")
    assert [entry["citation_key"] for entry in entries] == ["smith2024", "lee2023"]
    first, second = entries
    assert first["title"] == "A Graph Study"
    assert first["authors"] == ["Smith, Jane", "Doe, John"]
    assert first["identifiers"]["doi"] == "10.1234/abc.1"
    assert first["year"] == "2024"
    assert second["citation_key"] == "lee2023"
    assert second["identifiers"]["arxiv"] == "2401.12345"
    assert second["venue"] == "Proceedings of Test"


def test_bbl_csl_and_ris_parsers_keep_unverified_metadata():
    bbl = refs.parse_bbl(r"""
\begin{thebibliography}{9}
\bibitem{smith2024} J. Smith. \emph{A Graph Study}. Journal, 2024.
doi:10.1234/abc.1
\bibitem{lee2023} A. Lee. Other Work. 2023. arXiv:2401.12345v2
\end{thebibliography}
""")
    assert bbl[0]["title"] == "A Graph Study"
    assert bbl[0]["identifiers"]["doi"] == "10.1234/abc.1"
    assert bbl[0]["metadata_status"] == "parsed_bbl_unverified"
    assert bbl[1]["identifiers"]["arxiv"] == "2401.12345"

    csl = refs.parse_csl_json(json.dumps([{
        "id": "doe-2022",
        "type": "article-journal",
        "title": "A CSL Paper",
        "author": [{"family": "Doe", "given": "Ada"}],
        "issued": {"date-parts": [[2022, 3]]},
        "DOI": "10.5555/CSL.2",
        "container-title": "Journal of Tests",
    }]))
    assert csl[0]["citation_key"] == "doe-2022"
    assert csl[0]["identifiers"]["doi"] == "10.5555/csl.2"
    assert csl[0]["authors"] == ["Doe, Ada"]

    ris = refs.parse_ris("TY  - JOUR\nTI  - RIS Paper\nAU  - Roe, R.\nPY  - 2021\nDO  - 10.7777/ris.1\nER  - \n")
    assert ris[0]["title"] == "RIS Paper"
    assert ris[0]["year"] == "2021"
    assert ris[0]["identifiers"]["doi"] == "10.7777/ris.1"


def test_record_amend_dedupe_and_export_round_trip(tmp_path):
    graph = _graph(tmp_path, {})
    first = refs.record_reference(graph, {
        "title": "A Graph Study",
        "authors": ["Smith, Jane"],
        "year": "2024",
        "reference_type": "article",
        "citation_key": "smith2024",
        "identifiers": {"doi": "10.1234/abc.1"},
        "metadata_status": "parsed_bibtex",
    })
    assert first["created"] is True
    reference = first["reference"]
    assert reference["citation_key"] == "smith2024"

    enriched = refs.record_reference(graph, {
        "title": "A Graph Study",
        "authors": ["Smith, Jane", "Doe, John"],
        "year": "2024",
        "venue": "Journal of Tests",
        "identifiers": {"doi": "10.1234/ABC.1", "arxiv": "2401.12345"},
        "retraction": {"status": "none", "provider": "crossref"},
        "metadata_status": "parsed_csl_json",
    })
    assert enriched["created"] is False
    reference = enriched["reference"]
    assert reference["id"] == first["reference"]["id"]
    assert reference["authors"] == ["Smith, Jane"]
    assert reference["identifiers"]["arxiv"] == "2401.12345"
    assert reference["venue"] == "Journal of Tests"
    assert reference["updated_at"]

    exported = refs.export_bibtex(graph.state())
    assert "@article{smith2024" in exported
    assert "10.1234/abc.1" in exported
    round_trip = refs.parse_bibtex(exported)
    assert round_trip[0]["identifiers"]["doi"] == "10.1234/abc.1"

    csl = json.loads(refs.export_csl_json(graph.state()))
    assert csl[0]["DOI"] == "10.1234/abc.1"


def test_reference_checks_report_unresolved_and_retracted(tmp_path):
    graph = _graph(tmp_path, {})
    refs.record_reference(graph, {
        "title": "Retracted Work",
        "citation_key": "bad2020",
        "identifiers": {"doi": "10.9999/bad"},
        "retraction": {"status": "retracted", "provider": "openalex"},
    })
    graph.record("Claim", wording="A claim", claim_type="theorem", status="open",
                 citations=["missing2024", "bad2020"])
    checks = refs.check_references(graph.state())
    codes = {issue["code"] for issue in checks["issues"]}
    assert "unresolved_citation" in codes
    assert "reference_retracted" in codes
    assert checks["counts"]["references"] == 1
    assert checks["truth_assessed"] is False


def test_graph_amendment_replays_and_version_one_loads(tmp_path):
    graph = ResearchGraph.create("Amend", root=tmp_path, investigation_id="amend-test")
    node = graph.record("Reference", title="Old", citation_key="old2020")
    graph.amend(node["id"], title="New")
    state = graph.state()
    amended = next(item for item in state["nodes"] if item["id"] == node["id"])
    assert amended["title"] == "New"
    assert state["events"][-1]["type"] == "node_amended"

    legacy = state | {"schema_version": 1, "investigation_id": "legacy"}
    path = tmp_path / ".ozagia" / "research" / "legacy.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(legacy), encoding="utf-8")
    loaded = ResearchGraph.load("legacy", root=tmp_path)
    loaded_ref = next(item for item in loaded.state()["nodes"] if item["id"] == node["id"])
    assert loaded_ref["title"] == "New"


def test_paper_ingest_links_cite_keys_to_references(tmp_path):
    tex = (
        r"\begin{theorem}\label{thm:main} For every x, P(x) \cite{smith2024}.\end{theorem}"
        "\n"
        r"\begin{thebibliography}{9}"
        "\n"
        r"\bibitem{smith2024} J. Smith. \emph{A Graph Study}. 2024. doi:10.1234/abc.1"
        "\n"
        r"\end{thebibliography}"
        "\n"
    )
    graph = _graph(tmp_path, {"paper.tex": tex})
    preview = preview_paper(graph, root_id="papers", path="paper.tex")
    assert preview["bibliography"][0]["citation_key"] == "smith2024"
    assert preview["unresolved_citations"] == []
    result = ingest_paper(graph, root_id="papers", path="paper.tex")
    assert result["references"][0]["title"] == "A Graph Study"
    claim = result["claims"][0]
    assert claim["citations"] == ["smith2024"]
    citation_edges = [
        edge for edge in graph.state()["edges"]
        if edge["type"] == "cites" and edge["src"] == claim["id"]
        and edge["tgt"] == result["references"][0]["id"]
    ]
    assert citation_edges
    assert citation_edges[0]["provenance"]["kind"] == "author_citation_key"
    assert refs.check_references(graph.state())["ok"] is True


def test_explicit_bibliography_path_is_selected_and_used(tmp_path):
    tex = r"\begin{theorem}Result \cite{lee2023}.\end{theorem}"
    bib = "@article{lee2023, title={Other Work}, author={Lee, Ann}, year={2023}}"
    graph = _graph(tmp_path, {"paper.tex": tex, "refs.bib": bib})
    result = ingest_paper(
        graph, root_id="papers", path="paper.tex", bibliography_path="refs.bib")
    assert result["references"][0]["citation_key"] == "lee2023"
    assert result["claims"][0]["citations"] == ["lee2023"]

    with pytest.raises(ResearchError):
        ingest_paper(graph, root_id="papers", path="paper.tex",
                     bibliography_path="not-selected.bib")
