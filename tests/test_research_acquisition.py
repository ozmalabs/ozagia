from __future__ import annotations

import hashlib
import json

import pytest

from ozagia.research import fetch, providers, references as refs, tei, zotero
from ozagia.research.graph import ResearchGraph
from ozagia.research.service import ResearchService


def _json_response(payload, url):
    return {
        "content": json.dumps(payload).encode("utf-8"),
        "media_type": "application/json",
        "final_url": url,
        "headers": {},
    }


CROSSREF = {
    "message": {
        "title": ["A Graph Paper"],
        "author": [{"family": "Smith", "given": "Jane"}],
        "issued": {"date-parts": [[2024]]},
        "container-title": ["Journal of Tests"],
        "DOI": "10.1234/abc.1",
        "URL": "https://doi.org/10.1234/abc.1",
    }
}
OPENALEX = {
    "title": "A Graph Paper",
    "authorships": [{"author": {"display_name": "Jane Smith"}}],
    "publication_year": 2024,
    "primary_location": {"source": {"display_name": "Journal of Tests"}},
    "id": "https://openalex.org/W1",
    "doi": "https://doi.org/10.1234/abc.1",
    "is_retracted": False,
    "open_access": {
        "is_oa": True,
        "oa_status": "gold",
        "oa_url": "https://example.org/paper.pdf",
    },
}
ARXIV = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <title>An arXiv Paper</title>
    <summary>An abstract.</summary>
    <author><name>Ada Doe</name></author>
    <published>2024-01-01T00:00:00Z</published>
    <arxiv:journal_ref>Journal of Preprints</arxiv:journal_ref>
  </entry>
</feed>
"""
UNPAYWALL = {
    "oa_locations": [{
        "url_for_pdf": "https://example.org/oa.pdf",
        "url": "https://example.org/oa",
        "host_type": "publisher",
        "version": "publishedVersion",
        "license": "cc-by",
    }]
}


def fake_fetch(url, **_kwargs):
    if "api.crossref.org" in url:
        return _json_response(CROSSREF, url)
    if "api.openalex.org" in url:
        return _json_response(OPENALEX, url)
    if "export.arxiv.org" in url:
        return {"content": ARXIV.encode(), "media_type": "application/atom+xml",
                "final_url": url, "headers": {}}
    if "api.unpaywall.org" in url:
        return _json_response(UNPAYWALL, url)
    if url.endswith(".pdf"):
        return {"content": b"%PDF-1.7 fixture", "media_type": "application/pdf",
                "final_url": url, "headers": {}}
    raise AssertionError(url)


def test_resolve_reference_merges_open_metadata():
    reference = fetch.resolve_reference("10.1234/abc.1", fetcher=fake_fetch)
    assert reference["title"] == "A Graph Paper"
    assert reference["authors"] == ["Smith, Jane"]
    assert reference["identifiers"]["doi"] == "10.1234/abc.1"
    assert reference["identifiers"]["openalex"] == "W1"
    assert reference["retraction"]["status"] == "none"
    assert reference["open_access"]["is_oa"] is True
    assert len(reference["metadata_provenance"]) == 2


def test_preview_fetch_prefers_arxiv_source_and_pdf():
    result = fetch.preview_fetch("2401.12345", fetcher=fake_fetch)
    assert result["status"] == "candidates"
    urls = [candidate["url"] for candidate in result["candidates"]]
    assert urls[0].endswith("/e-print/2401.12345")
    assert any(url.endswith("/pdf/2401.12345") for url in urls)
    assert result["reference"]["identifiers"]["arxiv"] == "2401.12345"


def test_preview_fetch_uses_unpaywall_when_email_supplied():
    reference = {"title": "A Graph Paper", "citation_key": "smith2024",
                 "identifiers": {"doi": "10.1234/abc.1"}}
    without = fetch.preview_fetch(reference, fetcher=fake_fetch)
    assert without["status"] == "no_open_access_candidate"
    assert any("email" in note for note in without["notes"])

    result = fetch.preview_fetch(reference, email="reader@example.org", fetcher=fake_fetch)
    assert any(candidate["provider"] == "unpaywall" for candidate in result["candidates"])


def test_apply_fetch_hashes_caches_and_links_source(tmp_path):
    graph = ResearchGraph.create("Fetch", root=tmp_path, investigation_id="fetch-test")
    reference = refs.record_reference(graph, {
        "title": "A Graph Paper",
        "citation_key": "smith2024",
        "identifiers": {"doi": "10.1234/abc.1"},
    })["reference"]
    result = fetch.apply_fetch(
        graph,
        reference_id=reference["id"],
        candidate={"provider": "unpaywall", "url": "https://example.org/paper.pdf",
                   "media_type": "application/pdf", "license": "cc-by"},
        cache_dir=tmp_path / "cache",
        fetcher=fake_fetch,
    )
    artifact = result["artifact"]
    assert artifact["sha256"] == hashlib.sha256(b"%PDF-1.7 fixture").hexdigest()
    assert artifact["path"].endswith(".pdf")
    assert result["source"]["locator"]["reference_id"] == reference["id"]
    edges = graph.state()["edges"]
    assert any(edge["type"] == "manifestation_of"
               and edge["src"] == result["source"]["id"]
               and edge["tgt"] == reference["id"] for edge in edges)

    again = fetch.apply_fetch(
        graph,
        reference_id=reference["id"],
        candidate={"provider": "unpaywall", "url": "https://example.org/paper.pdf",
                   "media_type": "application/pdf"},
        cache_dir=tmp_path / "cache",
        fetcher=fake_fetch,
    )
    assert again["source"]["id"] == result["source"]["id"]


def test_url_guard_rejects_private_and_non_http():
    with pytest.raises(fetch.FetchError, match="http"):
        fetch.validate_url("file:///etc/passwd")
    with pytest.raises(fetch.FetchError, match="private"):
        fetch.validate_url("http://127.0.0.1/paper.pdf")
    with pytest.raises(fetch.FetchError, match="private"):
        fetch.validate_url("http://localhost/paper.pdf")


def test_zotero_item_mapping_and_local_items():
    item = {"data": {
        "title": "Zotero Paper",
        "creators": [{"firstName": "Ada", "lastName": "Doe"}],
        "date": "2022-04-01",
        "publicationTitle": "Journal of Zotero",
        "DOI": "10.5555/zotero.1",
        "itemType": "journalArticle",
        "extra": "Citation Key: doe2022",
    }}
    reference = zotero.item_to_reference(item)
    assert reference["title"] == "Zotero Paper"
    assert reference["authors"] == ["Doe, Ada"]
    assert reference["year"] == "2022"
    assert reference["citation_key"] == "doe2022"
    assert reference["identifiers"]["doi"] == "10.5555/zotero.1"

    items = zotero.local_items(get_json=lambda url: [item])
    assert items[0]["citation_key"] == "doe2022"


TEI_XML = b"""<?xml version="1.0"?>
<TEI xmlns="http://www.tei-c.org/ns/1.0">
  <teiHeader>
    <fileDesc>
      <titleStmt><title>A TEI Paper</title><author>A. Author</author></titleStmt>
      <sourceDesc>
        <listBibl>
          <biblStruct xml:id="b0">
            <analytic><title>Cited Work</title></analytic>
            <idno type="DOI">10.1234/cited.1</idno>
            <date>2020</date>
          </biblStruct>
        </listBibl>
      </sourceDesc>
    </fileDesc>
    <profileDesc><abstract><p>An abstract.</p></abstract></profileDesc>
  </teiHeader>
  <text><body>
    <div xml:id="s1"><head>Introduction</head>
      <p>Evidence text <ref type="bibr" target="#b0">[1]</ref>.</p>
    </div>
  </body></text>
</TEI>
"""


def test_tei_parser_extracts_sections_references_and_citation_targets():
    parsed = tei.parse_tei(TEI_XML)
    assert parsed["title"] == "A TEI Paper"
    assert parsed["abstract"] == "An abstract."
    assert parsed["sections"][0]["heading"] == "Introduction"
    assert parsed["references"][0]["title"] == "Cited Work"
    assert parsed["references"][0]["identifiers"]["doi"] == "10.1234/cited.1"
    assert parsed["citations"][0]["citation_keys"] == ["b0"]
    assert parsed["citations"][0]["truth_assessed"] is False


def test_tei_rejects_entity_declarations():
    with pytest.raises(tei.TEIError, match="DTD/entity"):
        tei.parse_tei(b"<!DOCTYPE x [<!ENTITY e 'boom'>]><x/>")


def test_external_provider_is_opt_in_and_named_only():
    providers.reset_providers()
    try:
        providers.register_provider("fixture-scholar", {
            "search": lambda query, limit: [{
                "title": f"Result for {query}",
                "citation_key": "scholar2024",
            }],
            "fulltext": lambda reference: [{
                "url": "https://fixture.example/paper.pdf",
                "media_type": "application/pdf",
            }],
            "unofficial": True,
        })
        status = providers.provider_status()
        assert status["enabled_by_default"] == []
        assert status["providers"][0]["unofficial"] is True

        result = fetch.preview_fetch(
            {"title": "A Graph Paper", "citation_key": "smith2024"},
            provider="fixture-scholar",
        )
        assert result["candidates"][0]["unofficial"] is True
        assert result["candidates"][0]["external"] is True

        search_result = fetch.search_external(
            "graph methods", provider="fixture-scholar", limit=3)
        assert search_result["results"][0]["title"] == "Result for graph methods"
        assert search_result["results"][0]["unofficial"] is True
        assert search_result["truth_assessed"] is False
    finally:
        providers.reset_providers()


def test_external_provider_package_loads_only_when_explicitly_configured(tmp_path, monkeypatch):
    module = tmp_path / "fixture_scholar_provider.py"
    module.write_text(
        "def provider():\n"
        "    return {'fulltext': lambda reference: [], 'unofficial': True}\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv("OZAGIA_RESEARCH_PROVIDERS", "fixture_scholar_provider:provider")
    providers.reset_providers()
    try:
        status = providers.provider_status()
        assert any(item["name"] == "provider" for item in status["providers"])
        assert status["enabled_by_default"] == []
    finally:
        providers.reset_providers()


def test_service_ref_and_fetch_operations(tmp_path):
    source_root = tmp_path / "sources"
    source_root.mkdir()
    (source_root / "paper.tex").write_text(
        r"\begin{theorem}Result \cite{smith2024}.\end{theorem}", encoding="utf-8")
    (source_root / "refs.bib").write_text(
        "@article{smith2024, title={A Graph Paper}, author={Smith, Jane}, year={2024}, doi={10.1234/abc.1}}",
        encoding="utf-8")
    svc = ResearchService(tmp_path, fetcher=fake_fetch)
    sid = svc.call("init", {
        "question": "Check acquisition",
        "roots": [{"root_id": "papers", "path": str(source_root),
                   "files": ["paper.tex", "refs.bib"]}],
    })["investigation_id"]
    resolved = svc.call("ref_resolve", {
        "session_id": sid, "identifier": "10.1234/abc.1", "record": True})
    assert resolved["recorded"]["created"] is True
    preview = svc.call("fetch_preview", {
        "session_id": sid, "reference_id": resolved["recorded"]["reference"]["id"],
        "email": "reader@example.org"})
    assert preview["candidates"]
    candidate = preview["candidates"][0]
    applied = svc.call("fetch_apply", {
        "session_id": sid,
        "reference_id": resolved["recorded"]["reference"]["id"],
        "candidate": candidate,
    })
    assert applied["artifact"]["sha256"]
    checked = svc.call("check", {"session_id": sid})
    assert any(item["status"] == "current" for item in checked["citations"])
    assert checked["references"]["counts"]["references"] == 1
