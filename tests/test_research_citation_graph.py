from __future__ import annotations

import hashlib
import json
from urllib.parse import parse_qs, urlsplit

import pytest

from ozagia.research import citation_graph, references as refs
from ozagia.research.citation_graph import CitationGraphError
from ozagia.research.graph import ResearchGraph


def _json_response(payload, url):
    return {
        "content": json.dumps(payload).encode("utf-8"),
        "media_type": "application/json",
        "final_url": url,
        "headers": {},
    }


def _work(work_id, title, doi, year=2020):
    return {
        "id": f"https://openalex.org/{work_id}",
        "doi": f"https://doi.org/{doi}",
        "title": title,
        "type": "article",
        "publication_year": year,
        "authorships": [{"author": {"display_name": "Ada Doe"}}],
        "primary_location": {"source": {"display_name": "Journal of Citations"}},
        "is_retracted": False,
        "open_access": {"is_oa": True, "oa_status": "green",
                        "oa_url": f"https://example.org/{work_id}.pdf"},
    }


SOURCE_WORK = {
    **_work("W1", "Source Work", "10.1234/source", 2024),
    "referenced_works": [
        "https://openalex.org/W2",
        "https://openalex.org/W3",
        "https://openalex.org/W4",
    ],
}
DOI_SOURCE_WORK = {
    **_work("W8", "DOI Source Work", "10.1234/doi-source", 2023),
    "referenced_works": ["https://openalex.org/W2"],
}
WORKS = {
    "W1": SOURCE_WORK,
    "W2": _work("W2", "Referenced Two", "10.1234/two", 2001),
    "W3": _work("W3", "Referenced Three", "10.1234/three", 2002),
    "W4": _work("W4", "Referenced Four", "10.1234/four", 2003),
    "W5": _work("W5", "Citing Five", "10.1234/five", 2005),
    "W6": _work("W6", "Citing Six", "10.1234/six", 2006),
    "W7": _work("W7", "Citing Seven", "10.1234/seven", 2007),
    "W8": DOI_SOURCE_WORK,
    "W9": _work("W9", "Citing Nine", "10.1234/nine", 2009),
}
DOI_WORKS = {
    "10.1234/source": SOURCE_WORK,
    "10.1234/doi-source": DOI_SOURCE_WORK,
}
CITATIONS = {
    "W1": [WORKS["W5"], WORKS["W6"], WORKS["W7"]],
    "W8": [WORKS["W9"]],
}


class FakeOpenAlex:
    def __init__(self):
        self.urls: list[str] = []

    def __call__(self, url, **_kwargs):
        self.urls.append(url)
        if url.startswith("https://api.openalex.org/works?filter=cites:"):
            query = parse_qs(urlsplit(url).query)
            work_id = query["filter"][0].split(":", 1)[1]
            limit = int(query["per-page"][0])
            return _json_response({"results": CITATIONS[work_id][:limit]}, url)
        if url.startswith("https://api.openalex.org/works/"):
            target = url.rsplit("/works/", 1)[1]
            if target.startswith("https://doi.org/"):
                return _json_response(DOI_WORKS[target.removeprefix("https://doi.org/")], url)
            return _json_response(WORKS[target], url)
        raise AssertionError(url)


def _graph(tmp_path):
    return ResearchGraph.create(
        "Citation graph", root=tmp_path, investigation_id="citation-test")


def _record(graph, title, *, doi=None, openalex=None):
    return refs.record_reference(graph, {
        "title": title,
        "authors": ["Doe, Ada"],
        "year": "2024",
        "identifiers": {
            "doi": doi,
            "openalex": openalex,
        },
    })["reference"]


def _openalex_works(fake):
    return [
        url for url in fake.urls
        if url.startswith("https://api.openalex.org/works/W")
    ]


def test_fetch_references_is_bounded_directed_and_provenanced(tmp_path):
    graph = _graph(tmp_path)
    source = _record(
        graph, "Source Work", doi="10.1234/source", openalex="https://openalex.org/W1")
    fake = FakeOpenAlex()

    result = citation_graph.fetch_related(
        graph, source["id"], direction="references", limit=2, fetcher=fake)

    assert result["provider"] == "openalex"
    assert result["truth_assessed"] is False
    assert result["count"] == 2
    assert result["limit"] == 2
    assert [candidate["identifiers"]["openalex"] for candidate in result["candidates"]] == [
        "W2", "W3"]
    assert [candidate["title"] for candidate in result["candidates"]] == [
        "Referenced Two", "Referenced Three"]
    assert _openalex_works(fake) == [
        "https://api.openalex.org/works/W1",
        "https://api.openalex.org/works/W2",
        "https://api.openalex.org/works/W3",
    ]
    assert all("W4" not in url for url in fake.urls)

    expected_sha = hashlib.sha256(json.dumps(SOURCE_WORK).encode("utf-8")).hexdigest()
    state = graph.state()
    edges = [edge for edge in state["edges"] if edge["type"] == "cites_work"]
    assert len(edges) == 2
    assert len(result["edges"]) == 2
    assert len(result["recorded"]) == 2
    for recorded, candidate in zip(result["recorded"], result["candidates"], strict=True):
        assert recorded["created"] is True
        assert recorded["edge_created"] is True
        assert recorded["reference"]["identifiers"]["openalex"] == candidate["identifiers"]["openalex"]
        edge = recorded["edge"]
        assert edge["src"] == source["id"]
        assert edge["tgt"] == recorded["reference"]["id"]
        provenance = edge["provenance"]
        assert provenance["provider"] == "openalex"
        assert provenance["retrieved_at"].endswith("Z")
        assert provenance["response_sha256"] == expected_sha
        assert provenance["direction"] == "references"
        assert provenance["source_reference_id"] == source["id"]
    assert not any(edge["type"] == "supports" for edge in state["edges"])


def test_fetch_citations_points_edges_at_the_source(tmp_path):
    graph = _graph(tmp_path)
    source = _record(
        graph, "Source Work", doi="10.1234/source", openalex="https://openalex.org/W1")
    fake = FakeOpenAlex()

    result = citation_graph.fetch_related(
        graph, source["id"], direction="citations", limit=2, fetcher=fake)

    assert [candidate["title"] for candidate in result["candidates"]] == [
        "Citing Five", "Citing Six"]
    assert result["count"] == 2
    assert any("filter=cites:W1" in url and "per-page=2" in url for url in fake.urls)

    expected_sha = hashlib.sha256(
        json.dumps({"results": CITATIONS["W1"][:2]}).encode("utf-8")).hexdigest()
    for recorded in result["recorded"]:
        edge = recorded["edge"]
        assert edge["src"] == recorded["reference"]["id"]
        assert edge["tgt"] == source["id"]
        assert edge["provenance"]["direction"] == "citations"
        assert edge["provenance"]["response_sha256"] == expected_sha
        assert edge["provenance"]["source_reference_id"] == source["id"]


def test_fetch_citations_resolves_a_doi_only_source(tmp_path):
    graph = _graph(tmp_path)
    source = _record(graph, "DOI Source Work", doi="10.1234/doi-source")
    fake = FakeOpenAlex()

    result = citation_graph.fetch_related(
        graph, source["id"], direction="citations", limit=5, fetcher=fake)

    assert [candidate["identifiers"]["openalex"] for candidate in result["candidates"]] == ["W9"]
    assert any(
        url == "https://api.openalex.org/works/https://doi.org/10.1234/doi-source"
        for url in fake.urls)
    assert any("filter=cites:W8" in url for url in fake.urls)
    assert result["recorded"][0]["edge"]["src"] == result["recorded"][0]["reference"]["id"]
    assert result["recorded"][0]["edge"]["tgt"] == source["id"]


def test_apply_false_writes_nothing(tmp_path):
    graph = _graph(tmp_path)
    source = _record(graph, "Source Work", doi="10.1234/source")
    before = graph.state()
    fake = FakeOpenAlex()

    result = citation_graph.fetch_related(
        graph, source["id"], direction="references", limit=2, fetcher=fake, apply=False)

    assert result["count"] == 2
    assert len(result["candidates"]) == 2
    assert result["recorded"] == []
    assert result["edges"] == []
    assert graph.state() == before
    assert not [edge for edge in graph.state()["edges"] if edge["type"] == "cites_work"]


def test_repeated_fetch_does_not_duplicate_edges(tmp_path):
    graph = _graph(tmp_path)
    source = _record(graph, "Source Work", doi="10.1234/source")
    fake = FakeOpenAlex()

    first = citation_graph.fetch_related(
        graph, source["id"], direction="references", limit=2, fetcher=fake)
    assert all(recorded["edge_created"] is True for recorded in first["recorded"])
    second = citation_graph.fetch_related(
        graph, source["id"], direction="references", limit=2, fetcher=fake)

    assert all(recorded["created"] is False for recorded in second["recorded"])
    assert all(recorded["edge_created"] is False for recorded in second["recorded"])
    assert all(
        len(recorded["reference"]["metadata_provenance"]) == 1
        for recorded in second["recorded"])
    assert [edge["id"] for edge in second["edges"]] == [
        edge["id"] for edge in first["edges"]]
    state = graph.state()
    edges = [edge for edge in state["edges"] if edge["type"] == "cites_work"]
    assert len(edges) == 2
    assert len({(edge["src"], edge["tgt"]) for edge in edges}) == 2


def test_limit_is_respected_for_references_and_citations(tmp_path):
    graph = _graph(tmp_path)
    source = _record(graph, "Source Work", doi="10.1234/source")
    fake = FakeOpenAlex()

    references = citation_graph.fetch_related(
        graph, source["id"], direction="references", limit=1, fetcher=fake)
    citations = citation_graph.fetch_related(
        graph, source["id"], direction="citations", limit=1, fetcher=fake)

    assert references["count"] == 1
    assert references["limit"] == 1
    assert len(references["recorded"]) == 1
    assert citations["count"] == 1
    assert citations["limit"] == 1
    assert len(citations["recorded"]) == 1


def test_neighborhood_is_local_bounded_and_deterministic(tmp_path):
    graph = _graph(tmp_path)
    alpha = _record(graph, "Alpha", doi="10.1000/alpha")
    beta = _record(graph, "Beta", doi="10.1000/beta")
    gamma = _record(graph, "Gamma", doi="10.1000/gamma")
    delta = _record(graph, "Delta", doi="10.1000/delta")
    alpha_beta = graph.link(
        alpha["id"], "cites_work", beta["id"], provenance={"provider": "fixture"})
    beta_gamma = graph.link(
        beta["id"], "cites_work", gamma["id"], provenance={"provider": "fixture"})
    delta_alpha = graph.link(
        delta["id"], "cites_work", alpha["id"], provenance={"provider": "fixture"})
    state = graph.state()

    result = citation_graph.neighborhood(
        state, alpha["id"], depth=2, direction="both")

    assert result["truth_assessed"] is False
    assert result["depth"] == 2
    assert result["reference_id"] == alpha["id"]
    assert [node["id"] for node in result["nodes"]] == sorted(
        [alpha["id"], beta["id"], gamma["id"], delta["id"]])
    assert result["counts"] == {"nodes": 4, "edges": 3}
    assert {edge["id"] for edge in result["edges"]} == {
        alpha_beta["id"], beta_gamma["id"], delta_alpha["id"]}
    assert [path["target"] for path in result["paths"]] == sorted(
        [alpha["id"], beta["id"], gamma["id"], delta["id"]])
    assert result["paths"][0] == {
        "target": alpha["id"], "nodes": [alpha["id"]], "length": 0}
    assert result == citation_graph.neighborhood(
        state, alpha["id"], depth=2, direction="both")

    outgoing = citation_graph.neighborhood(
        state, alpha["id"], depth=1, direction="references")
    assert [node["id"] for node in outgoing["nodes"]] == sorted([alpha["id"], beta["id"]])
    assert [edge["id"] for edge in outgoing["edges"]] == [alpha_beta["id"]]

    incoming = citation_graph.neighborhood(
        state, alpha["id"], depth=1, direction="citations")
    assert [node["id"] for node in incoming["nodes"]] == sorted([alpha["id"], delta["id"]])
    assert [edge["id"] for edge in incoming["edges"]] == [delta_alpha["id"]]

    with pytest.raises(CitationGraphError, match="depth"):
        citation_graph.neighborhood(state, alpha["id"], depth=5)
    with pytest.raises(CitationGraphError, match="direction"):
        citation_graph.neighborhood(state, alpha["id"], direction="sideways")


def test_citation_path_finds_shortest_directed_path(tmp_path):
    graph = _graph(tmp_path)
    alpha = _record(graph, "Alpha", doi="10.1000/alpha")
    beta = _record(graph, "Beta", doi="10.1000/beta")
    gamma = _record(graph, "Gamma", doi="10.1000/gamma")
    delta = _record(graph, "Delta", doi="10.1000/delta")
    epsilon = _record(graph, "Epsilon", doi="10.1000/epsilon")
    zeta = _record(graph, "Zeta", doi="10.1000/zeta")
    alpha_gamma = graph.link(
        alpha["id"], "cites_work", gamma["id"], provenance={"provider": "fixture"})
    alpha_beta = graph.link(
        alpha["id"], "cites_work", beta["id"], provenance={"provider": "fixture"})
    beta_gamma = graph.link(
        beta["id"], "cites_work", gamma["id"], provenance={"provider": "fixture"})
    delta_epsilon = graph.link(
        delta["id"], "cites_work", epsilon["id"], provenance={"provider": "fixture"})
    epsilon_zeta = graph.link(
        epsilon["id"], "cites_work", zeta["id"], provenance={"provider": "fixture"})
    state = graph.state()

    shortest = citation_graph.citation_path(state, alpha["id"], gamma["id"])
    assert shortest["found"] is True
    assert shortest["path"] == [alpha["id"], gamma["id"]]
    assert shortest["length"] == 1
    assert [edge["id"] for edge in shortest["edges"]] == [alpha_gamma["id"]]
    assert shortest["truth_assessed"] is False

    reverse = citation_graph.citation_path(state, gamma["id"], alpha["id"])
    assert reverse["found"] is False
    assert reverse["path"] == []

    direct = citation_graph.citation_path(
        state, alpha["id"], gamma["id"], max_depth=1)
    assert direct["found"] is True
    assert [edge["id"] for edge in direct["edges"]] == [alpha_gamma["id"]]

    chain = citation_graph.citation_path(state, delta["id"], zeta["id"])
    assert chain["found"] is True
    assert chain["path"] == [delta["id"], epsilon["id"], zeta["id"]]
    assert chain["length"] == 2
    assert [edge["id"] for edge in chain["edges"]] == [
        delta_epsilon["id"], epsilon_zeta["id"]]

    limited_chain = citation_graph.citation_path(
        state, delta["id"], zeta["id"], max_depth=1)
    assert limited_chain["found"] is False
    assert "max_depth" in limited_chain["reason"]

    # The direct edge is shorter than the two-hop route through beta.
    assert citation_graph.citation_path(state, alpha["id"], gamma["id"])["length"] == 1
    assert alpha_beta["id"] != alpha_gamma["id"]
    assert beta_gamma["id"] != alpha_gamma["id"]

    same = citation_graph.citation_path(state, alpha["id"], alpha["id"])
    assert same["found"] is True
    assert same["path"] == [alpha["id"]]
    assert same["length"] == 0

    missing = citation_graph.citation_path(state, alpha["id"], "reference-9999")
    assert missing["found"] is False
    assert "not found" in missing["reason"]

    with pytest.raises(CitationGraphError, match="max_depth"):
        citation_graph.citation_path(state, alpha["id"], gamma["id"], max_depth=0)


def test_fetch_related_validates_direction_limit_and_source(tmp_path):
    graph = _graph(tmp_path)
    source = _record(graph, "Source Work", doi="10.1234/source")
    no_identifiers = refs.record_reference(graph, {
        "title": "No Identifiers",
        "citation_key": "noidentifiers",
    })["reference"]
    fake = FakeOpenAlex()

    with pytest.raises(CitationGraphError, match="direction"):
        citation_graph.fetch_related(graph, source["id"], direction="supports", fetcher=fake)
    with pytest.raises(CitationGraphError, match="limit"):
        citation_graph.fetch_related(graph, source["id"], limit=0, fetcher=fake)
    with pytest.raises(CitationGraphError, match="unknown Reference"):
        citation_graph.fetch_related(graph, "reference-9999", fetcher=fake)
    with pytest.raises(CitationGraphError, match="DOI|OpenAlex"):
        citation_graph.fetch_related(graph, no_identifiers["id"], fetcher=fake)
    assert fake.urls == []


def test_invalid_provider_json_is_a_typed_error(tmp_path):
    graph = _graph(tmp_path)
    source = _record(graph, "Source Work", doi="10.1234/source")

    def bad_fetch(url, **_kwargs):
        return {"content": b"not json", "media_type": "application/json",
                "final_url": url, "headers": {}}

    with pytest.raises(CitationGraphError, match="invalid JSON"):
        citation_graph.fetch_related(graph, source["id"], fetcher=bad_fetch)
