from __future__ import annotations

import sys
from pathlib import Path

import pytest

from ozagia.research import source_formats as sf
from ozagia.research.graph import ResearchGraph


def _graph(tmp_path, investigation_id: str = "source-formats-test") -> ResearchGraph:
    return ResearchGraph.create(
        "Register source formats",
        root=tmp_path,
        investigation_id=investigation_id,
    )


def _recorder(calls: list[dict], *, link: bool = True):
    def record(graph, *, source_id, source_type, **overrides):
        calls.append({"source_id": source_id, "source_type": source_type, **overrides})
        node = graph.record(
            "Holding",
            source_id=source_id,
            source_type=source_type,
            epistemic_role=overrides.get("epistemic_role", "interpretation"),
            authority=overrides.get("authority", "editorial"),
            review_status=overrides.get("review_status", "proposed"),
            use_constraints=overrides.get("use_constraints", []),
            scope=overrides.get("scope", ""),
            rationale=overrides.get("rationale", ""),
            reviewer=overrides.get("reviewer", ""),
            status=overrides.get("status", "proposed"),
        )
        if link:
            graph.link(source_id, "held_as", node["id"])
        return node

    return record


def _nodes(graph: ResearchGraph, node_type: str) -> list[dict]:
    return [node for node in graph.state()["nodes"] if node["type"] == node_type]


def test_profiles_cover_formats_and_declare_required_fields():
    expected = {
        # Existing general formats.
        "book", "chapter", "note", "website", "article", "dataset", "software",
        "standard", "report", "thesis", "preprint", "interview", "lecture",
        "video", "other",
        # Research artifacts.
        "protocol", "preregistration", "registered_report", "analysis_plan",
        "lab_notebook", "field_notes", "computational_notebook", "workflow",
        "database", "model", "benchmark", "ontology",
        # Normative and institutional sources.
        "regulation", "statute", "court_case", "guideline",
        "consensus_statement", "patent", "ethics_approval", "consent_form",
        "data_use_agreement", "funding", "conflict_of_interest",
        # Meta-science and review artifacts.
        "peer_review", "editorial_decision", "author_response", "correction",
        "retraction_notice", "conference_abstract", "poster", "slides",
        # Human, grey and secondary sources.
        "oral_history", "focus_group", "survey", "questionnaire", "news",
        "blog", "press_release", "wiki", "forum", "social_post", "podcast",
        "webinar", "archival_document", "letter", "reference_work", "textbook",
        "encyclopedia", "fiction", "film", "art",
        # Physical and synthetic primary data.
        "specimen", "sample", "assay", "instrument_output", "image", "spectrum",
        "sequence", "structure", "synthetic_data", "model_output",
    }
    assert set(sf.SOURCE_FORMATS) == expected
    assert len(sf.SOURCE_FORMATS) == 76
    assert set(sf.list_source_formats()) == expected

    common = set(sf.COMMON_OPTIONAL_METADATA)
    assert common >= {
        "version", "retrieved_at", "license", "checksum", "archive_url",
        "jurisdiction", "issuing_body", "scope", "provenance", "independence",
        "conflict_of_interest", "consent", "chain_of_custody", "instrument",
        "calibration", "repository", "commit", "environment", "status",
        "supersedes", "target_id", "target_doi", "transcript", "medium",
        "acquisition", "processing", "identifier", "date", "authors", "year",
        "title", "url", "accessed_at", "published_at", "site", "publisher",
        "edition", "isbn", "doi", "language", "pages", "notebook", "page",
        "confidentiality", "venue", "event", "pmid", "arxiv", "description",
        "notes",
    }
    for profile in sf.SOURCE_FORMATS.values():
        assert common <= set(profile.metadata_fields)
        assert set(profile.required_metadata).isdisjoint(profile.optional_metadata)
        assert profile.required_metadata or profile.required_any
        for group in profile.required_any:
            assert set(group) <= set(profile.metadata_fields)

    book = sf.SOURCE_FORMATS["book"]
    assert book.required_metadata == ("title", "authors")
    assert set(book.optional_metadata) >= {
        "year", "publisher", "edition", "isbn", "doi", "language", "pages",
    }
    assert book.reference_type == "book"
    assert book.bibliographic is True
    assert book["required_metadata"] == ("title", "authors")

    note = sf.SOURCE_FORMATS["note"]
    assert note.bibliographic is False
    assert note.reference_type is None
    assert note.required_any == (("title", "context"),)

    website = sf.SOURCE_FORMATS["website"]
    assert website.required_metadata == ("url", "accessed_at")
    assert website.reference_type == "web"
    assert website.bibliographic is True

    protocol = sf.SOURCE_FORMATS["protocol"]
    assert protocol.required_metadata == ()
    assert protocol.required_any == (("title", "context"),)
    assert {"version", "date", "registry_id"} <= set(protocol.optional_metadata)

    database = sf.SOURCE_FORMATS["database"]
    assert database.required_metadata == ("title",)
    assert {"version", "license", "checksum", "repository"} <= set(
        database.optional_metadata
    )

    regulation = sf.SOURCE_FORMATS["regulation"]
    assert regulation.required_metadata == ("title",)
    assert regulation.required_any == (("issuing_body", "jurisdiction"),)

    guideline = sf.SOURCE_FORMATS["guideline"]
    assert guideline.required_metadata == ("title", "issuing_body")

    patent = sf.SOURCE_FORMATS["patent"]
    assert patent.required_metadata == ("title",)
    assert patent.required_any == (("identifier", "issuing_body"),)

    peer_review = sf.SOURCE_FORMATS["peer_review"]
    assert peer_review.required_metadata == ("title",)
    assert peer_review.required_any == (("target_id", "target_doi"),)

    news = sf.SOURCE_FORMATS["news"]
    assert news.required_metadata == ("url", "accessed_at")
    assert "title" in news.optional_metadata

    specimen = sf.SOURCE_FORMATS["specimen"]
    assert specimen.required_metadata == ()
    assert specimen.required_any == (("title", "identifier"),)
    assert specimen.bibliographic is False

    synthetic_data = sf.SOURCE_FORMATS["synthetic_data"]
    assert synthetic_data.required_metadata == ("title",)
    assert {"model", "version", "prompt_hash", "license"} <= set(
        synthetic_data.optional_metadata
    )


def test_normalise_book_metadata_identifiers_dates_and_lists():
    metadata = sf.normalise_source_metadata("book", {
        "title": "  A\nBook ",
        "authors": "Smith, Jane and Doe, John",
        "year": "copyright 2024",
        "publisher": "",
        "edition": 2,
        "isbn": "978-0-306-40615-7",
        "doi": "https://doi.org/10.1234/ABC.1",
        "pages": "12\u201334",
        "language": "en",
    })
    assert metadata == {
        "title": "A Book",
        "authors": ["Smith, Jane", "Doe, John"],
        "year": "2024",
        "edition": "2",
        "isbn": "9780306406157",
        "doi": "10.1234/abc.1",
        "pages": "12-34",
        "language": "en",
    }
    assert "publisher" not in metadata


def test_normalise_website_url_and_access_datetime():
    website = sf.normalise_source_metadata("website", {
        "url": "www.Example.com:443/Path?q=1#Part",
        "accessed_at": "2026-11-16T10:30:00Z",
        "published_at": "March 2025",
    })
    assert website["url"] == "https://www.example.com/Path?q=1#Part"
    assert website["accessed_at"] == "2026-11-16T10:30:00Z"
    assert website["published_at"] == "2025-03"


def test_citation_keys_are_accepted_for_bibliographic_formats():
    metadata = sf.normalise_source_metadata("book", {
        "title": "Citable Book",
        "authors": ["A"],
        "year": 2024,
        "citation_key": "author2024",
        "citation_keys": "author2024; author-2024",
    })
    assert metadata["citation_key"] == "author2024"
    assert metadata["citation_keys"] == ["author2024", "author-2024"]
    assert "citation_key" not in sf.SOURCE_FORMATS["book"].optional_metadata
    with pytest.raises(sf.SourceFormatError, match="unknown metadata field"):
        sf.normalise_source_metadata("note", {
            "title": "Note",
            "citation_key": "not-a-reference",
        })


def test_other_formats_accept_creator_and_speaker_metadata():
    dataset = sf.normalise_source_metadata("dataset", {
        "title": "Dataset",
        "year": 2024,
        "creator": "Team",
        "version": "1.2",
    })
    assert dataset["creator"] == "Team"
    assert dataset["version"] == "1.2"
    lecture = sf.normalise_source_metadata("lecture", {
        "title": "A Talk",
        "lecturer": "A Speaker",
        "date": "2026-11-16",
    })
    assert lecture["speaker"] == "A Speaker"


def test_book_registers_reference_source_and_default_holding(tmp_path):
    graph = _graph(tmp_path)
    calls: list[dict] = []
    result = sf.register_source(
        graph,
        source_type="book",
        metadata={
            "title": "A Book",
            "authors": ["Jane Smith"],
            "year": 2024,
            "publisher": "Test Press",
            "edition": 2,
            "isbn": "978-0-306-40615-7",
            "doi": "https://doi.org/10.1234/Book.1",
            "language": "en",
            "pages": "10\u201320",
        },
        root_id="library",
        path="books/a-book.epub",
        reviewer="matt",
        holding_recorder=_recorder(calls),
    )

    reference = result["reference"]
    assert reference is not None
    assert reference["reference_type"] == "book"
    assert reference["title"] == "A Book"
    assert reference["authors"] == ["Jane Smith"]
    assert reference["year"] == "2024"
    assert reference["venue"] == "Test Press"
    assert reference["identifiers"] == {
        "doi": "10.1234/book.1",
        "isbn": "9780306406157",
    }
    assert reference["metadata_status"] == "source_metadata"

    source = result["source"]
    assert source["name"] == "A Book"
    assert source["title"] == "A Book"
    assert source["source_type"] == "book"
    assert source["paper_import"] is False
    assert source["truth_assessed"] is False
    assert source["locator"]["kind"] == "source"
    assert source["locator"]["root_id"] == "library"
    assert source["locator"]["path"] == "books/a-book.epub"
    assert source["locator"]["metadata"]["publisher"] == "Test Press"
    assert source["metadata"]["isbn"] == "9780306406157"
    assert source["metadata"]["edition"] == "2"
    assert source["metadata"]["pages"] == "10-20"

    assert calls == [{
        "source_id": source["id"],
        "source_type": "book",
        "reviewer": "matt",
    }]
    assert result["holding"]["type"] == "Holding"
    assert result["holding"]["source_id"] == source["id"]
    assert result["truth_assessed"] is False
    assert result["warnings"] == []

    state = graph.state()
    assert len(_nodes(graph, "Source")) == 1
    assert len(_nodes(graph, "Reference")) == 1
    assert len(_nodes(graph, "Holding")) == 1
    assert {
        (edge["src"], edge["type"], edge["tgt"]) for edge in state["edges"]
    } >= {
        (source["id"], "manifestation_of", reference["id"]),
        (source["id"], "held_as", result["holding"]["id"]),
    }


def test_note_without_bibliographic_reference(tmp_path):
    graph = _graph(tmp_path)
    calls: list[dict] = []
    result = sf.register_source(
        graph,
        source_type="note",
        metadata={
            "context": "Discussed the book's argument over lunch",
            "author": "Matt",
            "date": "16 November 2026",
            "notebook": "N1",
            "page": "12",
            "confidentiality": "personal",
        },
        holding_recorder=_recorder(calls),
    )
    assert result["reference"] is None
    assert _nodes(graph, "Reference") == []
    assert result["source"]["source_type"] == "note"
    assert result["source"]["title"] == "Discussed the book's argument over lunch"
    assert result["metadata"]["date"] == "2026-11-16"
    assert result["warnings"] == []
    assert result["holding"]["source_type"] == "note"


def test_website_uses_url_locator_and_access_date(tmp_path):
    graph = _graph(tmp_path)
    calls: list[dict] = []
    result = sf.register_source(
        graph,
        source_type="website",
        metadata={
            "title": "Example page",
            "site": "Example",
            "accessed_at": "2026-11-16",
            "published_at": "March 2025",
        },
        url="https://Example.COM/Path?q=1#Part",
        holding_recorder=_recorder(calls),
    )
    assert result["metadata"]["url"] == "https://example.com/Path?q=1#Part"
    assert result["source"]["locator"]["url"] == "https://example.com/Path?q=1#Part"
    assert result["metadata"]["accessed_at"] == "2026-11-16"
    assert result["reference"]["reference_type"] == "web"
    assert result["reference"]["url"] == "https://example.com/Path?q=1#Part"
    assert result["reference"]["venue"] == "Example"
    assert result["reference"]["year"] == "2025"
    assert result["warnings"] == []


@pytest.mark.parametrize(
    ("source_type", "metadata", "message"),
    [
        ("website", {"url": "https://example.com"}, "requires metadata: accessed_at"),
        ("note", {"date": "2026-11-16"}, "requires title or context"),
        (
            "book",
            {"title": "T", "authors": ["A"], "year": 2024, "bogus": 1},
            "unknown metadata field",
        ),
        ("mystery", {"title": "T"}, "unknown source_type"),
    ],
)
def test_required_and_unknown_field_errors(source_type, metadata, message):
    with pytest.raises(sf.SourceFormatError, match=message):
        sf.normalise_source_metadata(source_type, metadata)


@pytest.mark.parametrize(
    ("source_type", "metadata"),
    [
        ("book", {"title": "T", "authors": ["A"], "year": "n.d."}),
        ("book", {"title": "T", "authors": ["A"], "year": 2024, "isbn": "123"}),
        ("book", {"title": "T", "authors": ["A"], "year": 2024, "doi": "not-a-doi"}),
        ("website", {"url": "mailto:someone@example.com", "accessed_at": "2026-11-16"}),
        ("website", {
            "url": "https://example.com",
            "accessed_at": "2026-13-40",
        }),
    ],
)
def test_invalid_metadata_values_raise(source_type, metadata):
    with pytest.raises(sf.SourceFormatError):
        sf.normalise_source_metadata(source_type, metadata)


def test_duplicate_path_source_is_reused_and_enriched(tmp_path):
    graph = _graph(tmp_path)
    calls: list[dict] = []
    first = sf.register_source(
        graph,
        source_type="book",
        metadata={"title": "Same Book", "authors": ["A"], "year": 2020},
        root_id="library",
        path="books/same.epub",
        holding_recorder=_recorder(calls),
    )
    second = sf.register_source(
        graph,
        source_type="book",
        metadata={
            "title": "Same Book",
            "authors": ["A"],
            "year": "2020",
            "publisher": "Added Later",
        },
        root_id="library",
        path="books/same.epub",
        holding_recorder=_recorder(calls),
    )
    assert second["source"]["id"] == first["source"]["id"]
    assert second["reference"]["id"] == first["reference"]["id"]
    assert second["source"]["metadata"]["publisher"] == "Added Later"
    assert second["metadata"]["publisher"] == "Added Later"
    assert second["reference"]["venue"] == "Added Later"
    assert len(_nodes(graph, "Source")) == 1
    assert len(_nodes(graph, "Reference")) == 1


def test_duplicate_url_source_is_reused(tmp_path):
    graph = _graph(tmp_path)
    calls: list[dict] = []
    first = sf.register_source(
        graph,
        source_type="website",
        metadata={
            "url": "https://example.com/page",
            "accessed_at": "2025-01-01",
            "title": "Page",
        },
        holding_recorder=_recorder(calls),
    )
    second = sf.register_source(
        graph,
        source_type="website",
        metadata={
            "url": "https://example.com/page",
            "accessed_at": "2026-11-16",
            "title": "Page",
        },
        holding_recorder=_recorder(calls),
    )
    assert second["source"]["id"] == first["source"]["id"]
    assert second["source"]["metadata"]["accessed_at"] == "2026-11-16"
    assert any(
        warning["code"] == "source_metadata_updated"
        for warning in second["warnings"]
    )
    assert len(_nodes(graph, "Source")) == 1
    assert len(_nodes(graph, "Reference")) == 1


def test_injected_holding_recorder_receives_overrides(tmp_path):
    graph = _graph(tmp_path)
    seen: dict = {}

    def recorder(graph, *, source_id, source_type, **overrides):
        seen.update({"source_id": source_id, "source_type": source_type, **overrides})
        return {
            "id": "holding-mock",
            "type": "Holding",
            "source_id": source_id,
            "source_type": source_type,
            **overrides,
        }

    result = sf.register_source(
        graph,
        source_type="article",
        metadata={
            "title": "An Article",
            "authors": ["A. Author"],
            "year": 2024,
            "doi": "10.1234/article.1",
        },
        reviewer="matt",
        holding={
            "epistemic_role": "reported_evidence",
            "authority": "peer_reviewed",
            "review_status": "reviewed",
            "use_constraints": ["treat_as_reported_not_confirmed"],
            "scope": "results section",
            "rationale": "Method and data are reported.",
            "status": "accepted",
        },
        holding_recorder=recorder,
    )
    assert seen["source_id"] == result["source"]["id"]
    assert seen["source_type"] == "article"
    assert seen["reviewer"] == "matt"
    assert seen["epistemic_role"] == "reported_evidence"
    assert seen["use_constraints"] == ["treat_as_reported_not_confirmed"]
    assert seen["scope"] == "results section"
    assert result["holding"]["epistemic_role"] == "reported_evidence"
    assert result["holding"]["status"] == "accepted"


def test_default_holding_is_recorded_by_holdings_module(tmp_path):
    pytest.importorskip("ozagia.research.holdings")
    graph = _graph(tmp_path)
    result = sf.register_source(
        graph,
        source_type="book",
        metadata={"title": "Default Holding", "authors": ["A"], "year": 2024},
        path="books/default.epub",
    )
    assert result["holding"] is not None
    assert result["holding"]["type"] == "Holding"
    assert result["holding"]["source_type"] == "book"
    assert result["holding"]["epistemic_role"] == "interpretation"
    assert result["holding"]["truth_assessed"] is False
    assert result["warnings"] == []
    state = graph.state()
    assert any(
        edge["src"] == result["source"]["id"]
        and edge["type"] == "held_as"
        and edge["tgt"] == result["holding"]["id"]
        for edge in state["edges"]
    )


def test_unknown_holding_override_is_rejected(tmp_path):
    graph = _graph(tmp_path)
    with pytest.raises(sf.SourceFormatError, match="unknown holding field"):
        sf.register_source(
            graph,
            source_type="note",
            metadata={"title": "Note"},
            holding={"truth_assessed": True},
            holding_recorder=_recorder([]),
        )


def test_holding_fallback_warns_when_module_is_unavailable(tmp_path, monkeypatch):
    graph = _graph(tmp_path)
    monkeypatch.setitem(sys.modules, "ozagia.research.holdings", None)
    result = sf.register_source(
        graph,
        source_type="note",
        metadata={"title": "A note"},
    )
    assert result["holding"] is None
    assert any(
        warning["code"] == "holding_unavailable"
        for warning in result["warnings"]
    )


def test_registration_reads_no_files_and_uses_no_network(tmp_path, monkeypatch):
    graph = _graph(tmp_path)

    def forbidden(*args, **kwargs):
        raise AssertionError("source registration must not read files or use the network")

    import socket

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(Path, "read_bytes", forbidden)
    monkeypatch.setattr(Path, "read_text", forbidden)

    result = sf.register_source(
        graph,
        source_type="book",
        metadata={
            "title": "Offline Book",
            "authors": ["A"],
            "year": 2024,
            "doi": "10.1234/offline.1",
        },
        root_id="library",
        path="books/does-not-need-to-exist.epub",
        sha256="ABC123",
        holding_recorder=_recorder([]),
    )
    assert result["source"]["locator"]["sha256"] == "abc123"
    assert result["source"]["name"] == "Offline Book"


def test_book_without_year_is_accepted():
    metadata = sf.normalise_source_metadata("book", {
        "title": "Undated Book",
        "authors": ["A"],
    })
    assert metadata == {"title": "Undated Book", "authors": ["A"]}
    assert "year" not in metadata


def test_book_without_year_registers_reference(tmp_path):
    graph = _graph(tmp_path)
    result = sf.register_source(
        graph,
        source_type="book",
        metadata={"title": "Undated Book", "authors": ["A"]},
        path="books/undated.epub",
        holding_recorder=_recorder([]),
    )
    assert result["reference"] is not None
    assert result["reference"]["title"] == "Undated Book"
    assert result["reference"]["year"] == ""
    assert result["warnings"] == []


def test_common_metadata_fields_are_accepted_and_normalised():
    metadata = sf.normalise_source_metadata("other", {
        "version": "2.1",
        "retrieved_at": "2026-11-16T10:30:00Z",
        "license": "CC-BY-4.0",
        "checksum": "sha256:abc123",
        "archive_url": "archive.example/item",
        "jurisdiction": "UK",
        "issuing_body": "Example Board",
        "scope": "adults",
        "provenance": "collected in lab",
        "independence": "independent",
        "conflict_of_interest": "none declared",
        "consent": "written",
        "chain_of_custody": "sealed",
        "instrument": "spectrometer",
        "calibration": "2026-10-01",
        "repository": "example/repo",
        "commit": "abc123",
        "environment": "python 3.13",
        "status": "final",
        "supersedes": "source-0001",
        "target_id": "source-0001",
        "target_doi": "https://doi.org/10.1234/ABC.1",
        "transcript": "transcript text",
        "medium": "digital",
        "acquisition": "captured",
        "processing": "normalised",
        "identifier": "ID-1",
        "date": "16 November 2026",
        "authors": "Smith, Jane and Doe, John",
        "year": "copyright 2024",
        "title": "  Complete\nSource ",
        "url": "www.Example.com/item",
        "accessed_at": "2026-11-16",
        "published_at": "March 2025",
        "site": "Example",
        "publisher": "Example Press",
        "edition": 2,
        "isbn": "978-0-306-40615-7",
        "doi": "doi:10.1234/OTHER.2",
        "language": "en",
        "pages": "12\u201334",
        "notebook": "N1",
        "page": "12",
        "confidentiality": "public",
        "venue": "Venue",
        "event": "Event",
        "pmid": "PMID: 123456",
        "arxiv": "arXiv:2401.00001",
        "description": "Description",
        "notes": "Notes",
    })
    assert metadata["title"] == "Complete Source"
    assert metadata["authors"] == ["Smith, Jane", "Doe, John"]
    assert metadata["year"] == "2024"
    assert metadata["date"] == "2026-11-16"
    assert metadata["published_at"] == "2025-03"
    assert metadata["retrieved_at"] == "2026-11-16T10:30:00Z"
    assert metadata["accessed_at"] == "2026-11-16"
    assert metadata["url"] == "https://www.example.com/item"
    assert metadata["archive_url"] == "https://archive.example/item"
    assert metadata["edition"] == "2"
    assert metadata["isbn"] == "9780306406157"
    assert metadata["doi"] == "10.1234/other.2"
    assert metadata["target_doi"] == "10.1234/abc.1"
    assert metadata["pmid"] == "123456"
    assert metadata["arxiv"] == "2401.00001"
    assert metadata["pages"] == "12-34"
    assert metadata["checksum"] == "sha256:abc123"
    assert metadata["repository"] == "example/repo"
    assert metadata["supersedes"] == "source-0001"


@pytest.mark.parametrize(
    ("source_type", "metadata"),
    [
        ("protocol", {"title": "Protocol"}),
        ("preregistration", {"context": "Preregistration"}),
        (
            "registered_report",
            {"title": "Registered report", "version": "1", "date": "2026-01-02"},
        ),
        ("analysis_plan", {"title": "Analysis plan", "registry_id": "RR-1"}),
        (
            "lab_notebook",
            {"context": "Entry 1", "author": "Matt", "date": "2026-11-16", "page": "4"},
        ),
        ("field_notes", {"context": "Field site A"}),
        (
            "computational_notebook",
            {"title": "Notebook", "repository": "repo", "version": "2", "environment": "python"},
        ),
        ("workflow", {"title": "Workflow"}),
        (
            "database",
            {
                "title": "Database", "version": "3", "license": "CC0",
                "checksum": "sha256:abc", "repository": "repo",
            },
        ),
        ("model", {"title": "Model", "version": "1"}),
        ("benchmark", {"title": "Benchmark", "checksum": "abc"}),
        ("ontology", {"title": "Ontology", "version": "2026"}),
        ("regulation", {"title": "Regulation", "jurisdiction": "UK"}),
        ("statute", {"title": "Statute", "issuing_body": "Parliament"}),
        ("court_case", {"title": "Case", "jurisdiction": "UK"}),
        ("guideline", {"title": "Guideline", "issuing_body": "WHO"}),
        ("consensus_statement", {"title": "Consensus", "issuing_body": "Panel"}),
        ("patent", {"title": "Patent", "identifier": "US-1"}),
        (
            "ethics_approval",
            {
                "title": "Approval", "institution": "University",
                "identifier": "IRB-1", "date": "2026-01-01",
            },
        ),
        ("consent_form", {"title": "Consent", "institution": "University"}),
        ("data_use_agreement", {"title": "DUA", "license": "CC0"}),
        ("funding", {"title": "Grant", "funder": "Fund"}),
        ("conflict_of_interest", {"title": "COI", "independence": "independent"}),
        ("peer_review", {"title": "Review", "target_doi": "10.1234/x"}),
        ("editorial_decision", {"title": "Decision", "target_id": "reference-0001"}),
        ("author_response", {"title": "Response", "target_id": "reference-0001"}),
        ("correction", {"title": "Correction", "target_doi": "10.1234/x"}),
        ("retraction_notice", {"title": "Retraction", "target_id": "reference-0001"}),
        (
            "conference_abstract",
            {"title": "Abstract", "authors": ["A"], "year": 2026},
        ),
        ("poster", {"title": "Poster", "event": "Conference"}),
        ("slides", {"title": "Slides", "venue": "Conference"}),
        (
            "oral_history",
            {
                "title": "Oral history", "participants": ["A", "B"],
                "consent": "written", "date": "2026-01-01",
            },
        ),
        (
            "focus_group",
            {"title": "Focus group", "participants": ["A"], "consent": "written"},
        ),
        (
            "survey",
            {
                "title": "Survey", "instrument": "Likert",
                "participants": ["A"], "consent": "informed",
            },
        ),
        ("questionnaire", {"title": "Questionnaire", "version": "2"}),
        ("news", {"url": "example.com/news", "accessed_at": "2026-11-16"}),
        ("blog", {"url": "example.com/blog", "accessed_at": "2026-11-16"}),
        ("press_release", {"url": "example.com/pr", "accessed_at": "2026-11-16"}),
        (
            "wiki",
            {"url": "example.com/wiki", "accessed_at": "2026-11-16", "revision": "42"},
        ),
        (
            "forum",
            {"url": "example.com/forum", "accessed_at": "2026-11-16", "thread": "t"},
        ),
        (
            "social_post",
            {
                "url": "example.com/post", "accessed_at": "2026-11-16",
                "platform": "mastodon",
            },
        ),
        ("podcast", {"title": "Episode", "url": "example.com/ep"}),
        ("webinar", {"title": "Webinar", "date": "2026-11-16"}),
        (
            "archival_document",
            {"title": "Document", "archive": "Archive", "collection": "C", "date": "1950"},
        ),
        ("letter", {"title": "Letter", "archive": "Archive", "date": "1950"}),
        ("reference_work", {"title": "Reference", "authors": ["A"]}),
        ("textbook", {"title": "Textbook", "authors": ["A"]}),
        ("encyclopedia", {"title": "Encyclopedia", "authors": ["A"]}),
        ("fiction", {"title": "Novel", "creator": "Author"}),
        ("film", {"title": "Film", "medium": "digital"}),
        ("art", {"title": "Art", "medium": "oil"}),
        (
            "specimen",
            {
                "identifier": "SP-1", "instrument": "scope",
                "calibration": "2026-01-01", "chain_of_custody": "sealed",
            },
        ),
        ("sample", {"title": "Sample"}),
        ("assay", {"identifier": "AS-1"}),
        ("instrument_output", {"identifier": "IO-1"}),
        ("image", {"title": "Image"}),
        ("spectrum", {"identifier": "SP-2"}),
        ("sequence", {"identifier": "SEQ-1"}),
        ("structure", {"identifier": "STR-1"}),
        (
            "synthetic_data",
            {
                "title": "Synthetic", "model": "gpt", "version": "1",
                "prompt_hash": "abc", "license": "MIT",
            },
        ),
        ("model_output", {"title": "Output", "model": "gpt"}),
    ],
)
def test_new_profiles_accept_minimal_metadata(source_type, metadata):
    result = sf.normalise_source_metadata(source_type, metadata)
    assert result


@pytest.mark.parametrize(
    ("source_type", "metadata", "message"),
    [
        ("protocol", {}, "requires title or context"),
        ("lab_notebook", {"page": "4"}, "requires title or context"),
        ("computational_notebook", {}, "requires metadata: title"),
        ("database", {}, "requires metadata: title"),
        ("regulation", {"title": "R"}, "requires issuing_body or jurisdiction"),
        ("guideline", {"title": "G"}, "requires metadata: issuing_body"),
        ("patent", {"title": "P"}, "requires identifier or issuing_body"),
        ("peer_review", {"title": "PR"}, "requires target_id or target_doi"),
        ("correction", {"title": "C"}, "requires target_id or target_doi"),
        (
            "conference_abstract",
            {"title": "A"},
            "requires authors or event or venue",
        ),
        ("news", {"url": "https://example.com"}, "requires metadata: accessed_at"),
        ("podcast", {"title": "Episode"}, "requires url or date"),
        ("fiction", {"title": "Novel"}, "requires creator or medium"),
        ("specimen", {}, "requires title or identifier"),
        ("synthetic_data", {}, "requires metadata: title"),
        (
            "regulation",
            {"title": "R", "jurisdiction": "UK", "bogus": 1},
            "unknown metadata field",
        ),
        (
            "peer_review",
            {"title": "Review", "target_doi": "not-a-doi"},
            "not a valid DOI",
        ),
    ],
)
def test_new_profiles_reject_missing_or_invalid_metadata(
    source_type, metadata, message
):
    with pytest.raises(sf.SourceFormatError, match=message):
        sf.normalise_source_metadata(source_type, metadata)


def test_register_source_mirrors_common_fields_on_source(tmp_path):
    graph = _graph(tmp_path)
    first = sf.register_source(
        graph,
        source_type="database",
        metadata={
            "title": "Genome DB",
            "version": "4.1",
            "retrieved_at": "2026-11-16",
            "license": "CC-BY-4.0",
            "checksum": "sha256:abc",
            "status": "final",
            "repository": "example.org/db",
            "url": "https://db.example.org",
        },
        root_id="data",
        path="databases/genomes",
        holding_recorder=_recorder([]),
    )
    source = first["source"]
    assert source["version"] == "4.1"
    assert source["retrieved_at"] == "2026-11-16"
    assert source["license"] == "CC-BY-4.0"
    assert source["checksum"] == "sha256:abc"
    assert source["status"] == "final"
    assert source["metadata"]["version"] == "4.1"
    assert source["metadata"]["repository"] == "example.org/db"
    assert source["locator"]["metadata"]["retrieved_at"] == "2026-11-16"

    second = sf.register_source(
        graph,
        source_type="database",
        metadata={
            "title": "Genome DB",
            "version": "4.2",
            "status": "updated",
        },
        root_id="data",
        path="databases/genomes",
        holding_recorder=_recorder([]),
    )
    assert second["source"]["id"] == first["source"]["id"]
    assert second["source"]["version"] == "4.2"
    assert second["source"]["status"] == "updated"
    assert second["source"]["retrieved_at"] == "2026-11-16"
    assert second["source"]["license"] == "CC-BY-4.0"
    assert second["source"]["checksum"] == "sha256:abc"
    assert len(_nodes(graph, "Source")) == 1


def test_register_source_links_supersedes_metadata(tmp_path):
    graph = _graph(tmp_path)
    first = sf.register_source(
        graph,
        source_type="database",
        metadata={"title": "Genome DB", "version": "1"},
        path="databases/v1",
        holding_recorder=_recorder([]),
    )
    second = sf.register_source(
        graph,
        source_type="database",
        metadata={
            "title": "Genome DB",
            "version": "2",
            "supersedes": first["source"]["id"],
        },
        path="databases/v2",
        holding_recorder=_recorder([]),
    )
    assert second["source"]["metadata"]["supersedes"] == first["source"]["id"]
    edges = graph.state()["edges"]
    assert (
        second["source"]["id"], "supersedes", first["source"]["id"]
    ) in {(edge["src"], edge["type"], edge["tgt"]) for edge in edges}

    # Re-registering the same Source does not duplicate the supersedes edge.
    sf.register_source(
        graph,
        source_type="database",
        metadata={
            "title": "Genome DB",
            "version": "2",
            "supersedes": first["source"]["id"],
        },
        path="databases/v2",
        holding_recorder=_recorder([]),
    )
    assert sum(
        edge["type"] == "supersedes"
        and edge["src"] == second["source"]["id"]
        and edge["tgt"] == first["source"]["id"]
        for edge in graph.state()["edges"]
    ) == 1


def test_register_source_rejects_unknown_supersedes_target(tmp_path):
    graph = _graph(tmp_path)
    with pytest.raises(sf.SourceFormatError, match="supersedes target does not exist"):
        sf.register_source(
            graph,
            source_type="database",
            metadata={"title": "Genome DB", "supersedes": "source-9999"},
            holding_recorder=_recorder([]),
        )
    assert _nodes(graph, "Source") == []
    assert _nodes(graph, "Reference") == []


@pytest.mark.parametrize(
    ("source_type", "metadata", "has_reference"),
    [
        ("protocol", {"title": "Protocol", "version": "1"}, True),
        ("lab_notebook", {"context": "Notebook entry"}, False),
        ("regulation", {"title": "Regulation", "jurisdiction": "UK"}, True),
        ("peer_review", {"title": "Review", "target_doi": "10.1234/x"}, True),
        ("oral_history", {"title": "Oral history", "consent": "written"}, True),
        (
            "news",
            {"url": "https://example.com/news", "accessed_at": "2026-11-16"},
            True,
        ),
        ("specimen", {"identifier": "SP-1"}, False),
        ("synthetic_data", {"title": "Synthetic"}, True),
    ],
)
def test_new_source_types_register_source_reference_and_holding(
    tmp_path, source_type, metadata, has_reference
):
    graph = _graph(tmp_path)
    calls: list[dict] = []
    result = sf.register_source(
        graph,
        source_type=source_type,
        metadata=metadata,
        root_id="corpus",
        path=f"{source_type}/{source_type}.dat",
        reviewer="matt",
        holding_recorder=_recorder(calls),
    )
    assert result["source"]["source_type"] == source_type
    assert result["holding"]["source_type"] == source_type
    assert calls == [{
        "source_id": result["source"]["id"],
        "source_type": source_type,
        "reviewer": "matt",
    }]
    assert (result["reference"] is not None) is has_reference
    if not has_reference:
        assert _nodes(graph, "Reference") == []
    if source_type == "specimen":
        assert result["source"]["name"] == "SP-1"
