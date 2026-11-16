from __future__ import annotations

import json

import pytest

from ozagia.research import holdings as ho
from ozagia.research.graph import ResearchError, ResearchGraph


EXPECTED_TYPES = {
    # Existing registry
    "book",
    "note",
    "website",
    "article",
    "dataset",
    "software",
    "standard",
    "report",
    "thesis",
    "preprint",
    "interview",
    "lecture",
    "video",
    "chapter",
    "other",
    # A user's stated position in an agent conversation
    "conversation",
    # Research artifacts
    "protocol",
    "preregistration",
    "registered_report",
    "analysis_plan",
    "lab_notebook",
    "field_notes",
    "computational_notebook",
    "workflow",
    "database",
    "model",
    "benchmark",
    "ontology",
    # Normative and institutional
    "regulation",
    "statute",
    "court_case",
    "guideline",
    "consensus_statement",
    "patent",
    "ethics_approval",
    "consent_form",
    "data_use_agreement",
    "funding",
    "conflict_of_interest",
    # Meta-science
    "peer_review",
    "editorial_decision",
    "author_response",
    "correction",
    "retraction_notice",
    "conference_abstract",
    "poster",
    "slides",
    # Human, grey and humanities
    "oral_history",
    "focus_group",
    "survey",
    "questionnaire",
    "news",
    "blog",
    "press_release",
    "wiki",
    "forum",
    "social_post",
    "podcast",
    "webinar",
    "archival_document",
    "letter",
    "reference_work",
    "textbook",
    "encyclopedia",
    "fiction",
    "film",
    "art",
    # Physical and synthetic
    "specimen",
    "sample",
    "assay",
    "instrument_output",
    "image",
    "spectrum",
    "sequence",
    "structure",
    "synthetic_data",
    "model_output",
}

NEW_ROLES = (
    "plan",
    "normative",
    "legal",
    "governance",
    "review_artifact",
    "integrity_event",
    "primary_data",
    "synthetic",
    "candidate",
    "technical_claim",
    "context",
    "evaluation",
)

NEW_AUTHORITIES = ("regulatory", "legal", "standard_body", "repository")


def _graph(tmp_path, investigation_id: str = "holdings-tests") -> ResearchGraph:
    return ResearchGraph.create(
        "Epistemic holdings tests",
        root=tmp_path,
        investigation_id=investigation_id,
    )


def _source(graph, *, path: str = "source.md", **fields):
    fields.setdefault("locator", {"path": path})
    return graph.record("Source", **fields)


def _claim(graph, **fields):
    fields.setdefault("wording", "The claim under test.")
    fields.setdefault("claim_type", "hypothesis")
    fields.setdefault("status", "unresolved")
    fields.setdefault("author", "reader")
    fields.setdefault("scope", "the recorded corpus")
    return graph.record("Claim", **fields)


def _codes(items):
    return [item["code"] for item in items]


def _complete_book_metadata() -> dict[str, str]:
    return {"publisher": "Example Press", "year": "2020"}


# --------------------------------------------------------------------------- #
# format defaults
# --------------------------------------------------------------------------- #
def test_list_source_types_is_complete_sorted_and_deterministic():
    specs = ho.list_source_types()

    assert [spec["source_type"] for spec in specs] == sorted(EXPECTED_TYPES)
    assert {spec["source_type"] for spec in specs} == EXPECTED_TYPES
    assert specs == ho.list_source_types()
    assert len({spec["label"] for spec in specs}) == len(EXPECTED_TYPES)
    for spec in specs:
        assert set(spec) == {
            "source_type",
            "label",
            "epistemic_role",
            "authority",
            "review_status",
            "use_constraints",
            "required_metadata",
            "notes",
        }
        assert spec["epistemic_role"] in ho.EPISTEMIC_ROLES
        assert spec["authority"] in ho.AUTHORITY_LEVELS
        assert spec["review_status"] in ho.REVIEW_STATUSES
        assert spec["use_constraints"]
        assert spec["required_metadata"]
        assert spec["notes"].strip()


def test_book_note_website_and_article_defaults_are_conservative():
    specs = {spec["source_type"]: spec for spec in ho.list_source_types()}

    book = specs["book"]
    assert book["epistemic_role"] == "interpretation"
    assert book["authority"] == "editorial"
    assert book["review_status"] == "editorial"
    assert "treat_as_author_argument" in book["use_constraints"]
    assert "not_as_behavioural_evidence_without_extraction" in book["use_constraints"]

    note = specs["note"]
    assert note["epistemic_role"] == "personal_note"
    assert note["authority"] == "personal"
    assert note["review_status"] == "none"
    assert note["use_constraints"] == ["not_external_evidence"]

    website = specs["website"]
    assert website["epistemic_role"] == "background"
    assert website["authority"] == "unknown"
    assert website["review_status"] == "unknown"
    assert "check_accessed_date" in website["use_constraints"]
    assert "archive_snapshot" in website["use_constraints"]

    article = specs["article"]
    assert article["epistemic_role"] == "reported_evidence"
    assert article["review_status"] == "unknown"

    # Returned specs are copies: callers cannot mutate the shared mapping.
    specs["book"]["use_constraints"].append("mutated")
    specs["book"]["required_metadata"].append("mutated")
    assert "mutated" not in ho.SOURCE_TYPES["book"]["use_constraints"]
    assert "mutated" not in ho.SOURCE_TYPES["book"]["required_metadata"]


@pytest.mark.parametrize(
    ("source_type", "role", "authority"),
    [
        # Research artifacts
        ("protocol", "plan", "institutional"),
        ("preregistration", "plan", "institutional"),
        ("registered_report", "plan", "institutional"),
        ("analysis_plan", "plan", "unknown"),
        ("lab_notebook", "primary_source", "personal"),
        ("field_notes", "primary_source", "personal"),
        ("computational_notebook", "method", "repository"),
        ("workflow", "method", "repository"),
        ("database", "primary_data", "repository"),
        ("model", "method", "repository"),
        ("benchmark", "evaluation", "repository"),
        ("ontology", "method", "repository"),
        # Normative and institutional
        ("regulation", "normative", "regulatory"),
        ("statute", "legal", "legal"),
        ("court_case", "legal", "legal"),
        ("guideline", "normative", "standard_body"),
        ("consensus_statement", "interpretation", "standard_body"),
        ("patent", "technical_claim", "legal"),
        ("ethics_approval", "governance", "institutional"),
        ("consent_form", "governance", "institutional"),
        ("data_use_agreement", "governance", "legal"),
        ("funding", "context", "institutional"),
        ("conflict_of_interest", "context", "unknown"),
        # Meta-science
        ("peer_review", "review_artifact", "unknown"),
        ("editorial_decision", "review_artifact", "editorial"),
        ("author_response", "review_artifact", "unknown"),
        ("correction", "integrity_event", "editorial"),
        ("retraction_notice", "integrity_event", "editorial"),
        ("conference_abstract", "reported_evidence", "unknown"),
        ("poster", "reported_evidence", "unknown"),
        ("slides", "reported_evidence", "unknown"),
        # Human, grey and humanities
        ("oral_history", "primary_source", "personal"),
        ("focus_group", "primary_source", "personal"),
        ("survey", "primary_data", "unknown"),
        ("questionnaire", "method", "unknown"),
        ("news", "background", "editorial"),
        ("blog", "background", "self_published"),
        ("press_release", "background", "institutional"),
        ("wiki", "background", "unknown"),
        ("forum", "background", "unknown"),
        ("social_post", "background", "self_published"),
        ("podcast", "reported_evidence", "unknown"),
        ("webinar", "background", "institutional"),
        ("archival_document", "primary_source", "institutional"),
        ("letter", "primary_source", "personal"),
        ("reference_work", "secondary_source", "editorial"),
        ("textbook", "secondary_source", "editorial"),
        ("encyclopedia", "secondary_source", "editorial"),
        ("fiction", "primary_source", "editorial"),
        ("film", "primary_source", "editorial"),
        ("art", "primary_source", "unknown"),
        # Physical and synthetic
        ("specimen", "primary_data", "unknown"),
        ("sample", "primary_data", "unknown"),
        ("assay", "primary_data", "unknown"),
        ("instrument_output", "primary_data", "unknown"),
        ("image", "primary_data", "unknown"),
        ("spectrum", "primary_data", "unknown"),
        ("sequence", "primary_data", "unknown"),
        ("structure", "primary_data", "unknown"),
        ("synthetic_data", "synthetic", "unknown"),
        ("model_output", "synthetic", "unknown"),
    ],
)
def test_new_source_type_defaults_are_conservative(source_type, role, authority):
    spec = ho.SOURCE_TYPES[source_type]

    assert spec["epistemic_role"] == role
    assert spec["authority"] == authority
    assert spec["review_status"] in ho.REVIEW_STATUSES
    assert spec["use_constraints"]
    assert spec["required_metadata"]
    assert spec["notes"].strip()


def test_plan_and_synthetic_defaults_forbid_result_evidence():
    for source_type in ("protocol", "preregistration", "registered_report", "analysis_plan"):
        assert "not_result_evidence" in ho.SOURCE_TYPES[source_type]["use_constraints"]
        assert ho.SOURCE_TYPES[source_type]["epistemic_role"] == "plan"

    for source_type in ("synthetic_data", "model_output"):
        assert "not_evidence_by_default" in ho.SOURCE_TYPES[source_type]["use_constraints"]
        assert ho.SOURCE_TYPES[source_type]["epistemic_role"] == "synthetic"

    for source_type in ("conference_abstract", "poster", "slides"):
        spec = ho.SOURCE_TYPES[source_type]
        assert spec["epistemic_role"] == "reported_evidence"
        assert spec["review_status"] == "none"


def test_new_roles_and_authorities_are_valid_and_recordable(tmp_path):
    assert set(NEW_ROLES) <= set(ho.EPISTEMIC_ROLES)
    assert set(NEW_AUTHORITIES) <= set(ho.AUTHORITY_LEVELS)

    graph = _graph(tmp_path)
    source = _source(graph)
    for role in NEW_ROLES:
        holding = ho.record_holding(
            graph,
            source_id=source["id"],
            source_type="book",
            epistemic_role=role,
        )["holding"]
        assert holding["epistemic_role"] == role
    for authority in NEW_AUTHORITIES:
        holding = ho.record_holding(
            graph,
            source_id=source["id"],
            source_type="book",
            authority=authority,
        )["holding"]
        assert holding["authority"] == authority


# --------------------------------------------------------------------------- #
# recording
# --------------------------------------------------------------------------- #
def test_record_book_holding_applies_defaults_and_links_source(tmp_path):
    graph = _graph(tmp_path)
    source = _source(graph, path="books/argument.md", metadata=_complete_book_metadata())

    result = ho.record_holding(
        graph,
        source_id=source["id"],
        source_type="book",
    )

    holding = result["holding"]
    assert holding["type"] == "Holding"
    assert holding["id"] == "holding-0001"
    assert holding["source_id"] == source["id"]
    assert holding["source_type"] == "book"
    assert holding["epistemic_role"] == "interpretation"
    assert holding["authority"] == "editorial"
    assert holding["review_status"] == "editorial"
    assert holding["use_constraints"] == ho.SOURCE_TYPES["book"]["use_constraints"]
    assert holding["status"] == "proposed"
    assert holding["truth_assessed"] is False
    assert result["created"] is True
    assert result["truth_assessed"] is False
    assert result["edge"]["type"] == "held_as"
    assert result["edge"]["src"] == source["id"]
    assert result["edge"]["tgt"] == holding["id"]
    assert result["edge"]["provenance"]["kind"] == "epistemic_holding"

    check = ho.check_holding(graph.state(), holding["id"])
    assert check["ok"] is True
    assert check["issues"] == []
    assert check["warnings"] == []
    assert check["counts"]["held_as_edges"] == 1
    assert check["truth_assessed"] is False


def test_multiple_holdings_for_one_source_are_allowed(tmp_path):
    graph = _graph(tmp_path)
    source = _source(graph, metadata=_complete_book_metadata())

    first = ho.record_holding(graph, source_id=source["id"], source_type="book")
    second = ho.record_holding(
        graph,
        source_id=source["id"],
        source_type="book",
        epistemic_role="evidence",
        authority="peer_reviewed",
        review_status="peer_reviewed",
        use_constraints=["extracted_evidence_only"],
        scope="chapter 2, experiment 1",
        rationale="The chapter reports a controlled experiment with extracted data.",
        reviewer="reviewer-a",
        status="accepted",
        confidence=0.7,
    )

    assert first["holding"]["id"] != second["holding"]["id"]
    assert second["holding"]["epistemic_role"] == "evidence"
    assert second["holding"]["status"] == "accepted"
    assert second["holding"]["reviewer"] == "reviewer-a"
    assert second["holding"]["confidence"] == 0.7
    assert second["holding"]["scope"] == "chapter 2, experiment 1"

    state = graph.state()
    assert len([node for node in state["nodes"] if node["type"] == "Holding"]) == 2
    edges = [edge for edge in state["edges"] if edge["type"] == "held_as"]
    assert len(edges) == 2
    assert {edge["src"] for edge in edges} == {source["id"]}
    assert {edge["tgt"] for edge in edges} == {
        first["holding"]["id"],
        second["holding"]["id"],
    }

    report = ho.holding_report(graph, source_id=source["id"])
    assert len(report["holdings"]) == 2
    assert report["defaults"]["book"]["epistemic_role"] == "interpretation"

    check = ho.check_holding(graph.state(), second["holding"]["id"])
    assert check["ok"] is True
    assert check["warnings"] == []


def test_article_default_does_not_assume_peer_review(tmp_path):
    graph = _graph(tmp_path)
    source = _source(graph, path="articles/report.md")

    result = ho.record_holding(
        graph, source_id=source["id"], source_type="article"
    )

    assert result["holding"]["epistemic_role"] == "reported_evidence"
    assert result["holding"]["authority"] == "unknown"
    assert result["holding"]["review_status"] == "unknown"
    assert "check_peer_review_status" in result["holding"]["use_constraints"]


def test_explicit_empty_use_constraints_overrides_defaults(tmp_path):
    graph = _graph(tmp_path)
    source = _source(graph, path="notes/context.md")

    result = ho.record_holding(
        graph,
        source_id=source["id"],
        source_type="note",
        use_constraints=[],
    )

    assert result["holding"]["use_constraints"] == []
    assert result["holding"]["epistemic_role"] == "personal_note"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"source_id": "source-9999", "source_type": "book"},
        {"source_id": None, "source_type": "book"},
        {"source_type": "webpage"},
        {"source_type": ""},
        {"source_type": "book", "epistemic_role": "fact"},
        {"source_type": "book", "authority": "famous"},
        {"source_type": "book", "review_status": "assumed"},
        {"source_type": "book", "status": "draft"},
        {"source_type": "book", "use_constraints": "treat_as_author_argument"},
        {"source_type": "book", "use_constraints": ["treat_as_author_argument", "  "]},
        {"source_type": "book", "status": "accepted"},
        {"source_type": "book", "scope": ""},
        {"source_type": "book", "rationale": " "},
        {"source_type": "book", "reviewer": ""},
        {"source_type": "book", "confidence": -0.1},
        {"source_type": "book", "confidence": 1.1},
        {"source_type": "book", "confidence": True},
        {"source_type": "book", "confidence": "0.5"},
    ],
)
def test_record_holding_rejects_invalid_input(tmp_path, kwargs):
    graph = _graph(tmp_path)
    source = _source(graph)
    payload = {"source_id": source["id"], **kwargs}

    with pytest.raises(ho.HoldingError):
        ho.record_holding(graph, **payload)

    assert not [node for node in graph.state()["nodes"] if node["type"] == "Holding"]


def test_record_holding_rejects_a_source_already_labelled_with_another_format(tmp_path):
    graph = _graph(tmp_path)
    source = _source(graph, source_type="book")

    with pytest.raises(ho.HoldingError, match="recorded as 'book'"):
        ho.record_holding(graph, source_id=source["id"], source_type="note")


def test_holding_error_is_a_research_error():
    assert issubclass(ho.HoldingError, ResearchError)


# --------------------------------------------------------------------------- #
# format-specific checks
# --------------------------------------------------------------------------- #
def test_note_held_as_evidence_is_an_issue(tmp_path):
    graph = _graph(tmp_path)
    source = _source(graph, path="notes/reading.md")

    result = ho.record_holding(
        graph,
        source_id=source["id"],
        source_type="note",
        epistemic_role="evidence",
        scope="the quoted passage",
        rationale="It is the only record of the observation.",
    )
    check = ho.check_holding(graph, result["holding"]["id"])

    assert check["ok"] is False
    assert "note_held_as_evidence" in _codes(check["issues"])
    assert check["truth_assessed"] is False


def test_website_missing_access_and_archive_warns_then_passes(tmp_path):
    graph = _graph(tmp_path)
    source = _source(graph, path="sites/example.html")
    holding = ho.record_holding(
        graph, source_id=source["id"], source_type="website"
    )["holding"]

    check = ho.check_holding(graph.state(), holding["id"])
    assert check["ok"] is True
    assert "website_missing_archive_metadata" in _codes(check["warnings"])
    warning = next(
        item for item in check["warnings"]
        if item["code"] == "website_missing_archive_metadata"
    )
    assert warning["missing"] == ["accessed_at", "archive_url"]

    graph.amend(
        source["id"],
        metadata={
            "accessed_at": "2026-11-16",
            "archive_url": "https://archive.example/sites/example.html",
        },
    )
    check = ho.check_holding(graph.state(), holding["id"])
    assert check["ok"] is True
    assert check["warnings"] == []


@pytest.mark.parametrize(
    "source_type",
    ["protocol", "preregistration", "registered_report", "analysis_plan"],
)
def test_plan_types_held_as_result_evidence_are_issues(tmp_path, source_type):
    graph = _graph(tmp_path)
    source = _source(graph, path=f"plans/{source_type}.md")
    holding = ho.record_holding(
        graph,
        source_id=source["id"],
        source_type=source_type,
        epistemic_role="evidence",
    )["holding"]

    check = ho.check_holding(graph.state(), holding["id"])
    assert check["ok"] is False
    assert "plan_held_as_result_evidence" in _codes(check["issues"])
    assert check["truth_assessed"] is False


@pytest.mark.parametrize(
    "source_type",
    [
        "standard",
        "regulation",
        "statute",
        "court_case",
        "guideline",
        "consensus_statement",
        "patent",
    ],
)
def test_normative_types_held_as_empirical_evidence_are_issues(tmp_path, source_type):
    graph = _graph(tmp_path)
    source = _source(graph, path=f"normative/{source_type}.md")
    holding = ho.record_holding(
        graph,
        source_id=source["id"],
        source_type=source_type,
        epistemic_role="evidence",
    )["holding"]

    check = ho.check_holding(graph.state(), holding["id"])
    assert check["ok"] is False
    assert "normative_held_as_empirical_evidence" in _codes(check["issues"])


@pytest.mark.parametrize("source_type", ["synthetic_data", "model_output"])
def test_synthetic_types_held_as_evidence_are_issues(tmp_path, source_type):
    graph = _graph(tmp_path)
    source = _source(graph, path=f"synthetic/{source_type}.json")
    holding = ho.record_holding(
        graph,
        source_id=source["id"],
        source_type=source_type,
        epistemic_role="evidence",
    )["holding"]

    check = ho.check_holding(graph.state(), holding["id"])
    assert check["ok"] is False
    assert "synthetic_held_as_evidence" in _codes(check["issues"])


@pytest.mark.parametrize(
    "source_type",
    [
        "dataset",
        "database",
        "software",
        "model",
        "benchmark",
        "workflow",
        "computational_notebook",
    ],
)
def test_technical_artifact_missing_version_license_and_checksum_warns(
    tmp_path, source_type
):
    graph = _graph(tmp_path)
    source = _source(graph, path=f"artifacts/{source_type}.bin")
    holding = ho.record_holding(
        graph, source_id=source["id"], source_type=source_type
    )["holding"]

    check = ho.check_holding(graph.state(), holding["id"])
    assert check["ok"] is True
    warning = next(
        item for item in check["warnings"]
        if item["code"] == "artifact_metadata_incomplete"
    )
    assert warning["missing"] == ["version", "license", "checksum"]

    graph.amend(
        source["id"],
        metadata={
            "version": "1.0",
            "license": "CC-BY-4.0",
            "checksum": "sha256:0000000000000000",
        },
    )
    check = ho.check_holding(graph.state(), holding["id"])
    assert check["warnings"] == []
    assert check["ok"] is True


@pytest.mark.parametrize(
    ("source_type", "missing"),
    [
        ("standard", ["issuing_body"]),
        ("regulation", ["jurisdiction", "issuing_body"]),
        ("statute", ["jurisdiction", "issuing_body"]),
        ("court_case", ["jurisdiction"]),
        ("guideline", ["issuing_body"]),
        ("consensus_statement", ["issuing_body"]),
        ("patent", ["jurisdiction"]),
    ],
)
def test_normative_source_missing_jurisdiction_or_issuing_body_warns(
    tmp_path, source_type, missing
):
    graph = _graph(tmp_path)
    source = _source(graph, path=f"normative/{source_type}.md")
    holding = ho.record_holding(
        graph, source_id=source["id"], source_type=source_type
    )["holding"]

    check = ho.check_holding(graph.state(), holding["id"])
    assert check["ok"] is True
    warning = next(
        item for item in check["warnings"]
        if item["code"] == "normative_metadata_incomplete"
    )
    assert warning["missing"] == missing

    graph.amend(
        source["id"],
        metadata={"jurisdiction": "EU", "issuing_body": "Example Body"},
    )
    check = ho.check_holding(graph.state(), holding["id"])
    assert check["warnings"] == []
    assert check["ok"] is True


@pytest.mark.parametrize(
    ("source_type", "context"),
    [
        ("interview", {"interviewer": "R. Reader"}),
        ("oral_history", {"interviewer": "R. Reader"}),
        ("focus_group", {"context": "moderated session"}),
        ("survey", {"sampling": "stratified sample"}),
        ("questionnaire", {"sampling": "convenience sample"}),
    ],
)
def test_human_source_missing_consent_or_context_warns(tmp_path, source_type, context):
    graph = _graph(tmp_path)
    source = _source(graph, path=f"human/{source_type}.md")
    holding = ho.record_holding(
        graph, source_id=source["id"], source_type=source_type
    )["holding"]

    check = ho.check_holding(graph.state(), holding["id"])
    assert check["ok"] is True
    warning = next(
        item for item in check["warnings"]
        if item["code"] == "human_source_missing_consent_or_context"
    )
    assert warning["missing"] == ["consent", "context"]

    graph.amend(source["id"], metadata={"consent": "written", **context})
    check = ho.check_holding(graph.state(), holding["id"])
    assert check["warnings"] == []
    assert check["ok"] is True


@pytest.mark.parametrize(
    "source_type",
    ["news", "blog", "press_release", "wiki", "forum", "social_post"],
)
def test_mutable_web_source_missing_access_and_archive_warns(tmp_path, source_type):
    graph = _graph(tmp_path)
    source = _source(graph, path=f"web/{source_type}.html")
    holding = ho.record_holding(
        graph, source_id=source["id"], source_type=source_type
    )["holding"]

    check = ho.check_holding(graph.state(), holding["id"])
    assert check["ok"] is True
    warning = next(
        item for item in check["warnings"]
        if item["code"] == "mutable_source_missing_access_metadata"
    )
    assert warning["missing"] == ["accessed_at", "archive_url"]

    graph.amend(
        source["id"],
        metadata={
            "accessed_at": "2026-11-16",
            "archive_url": "https://archive.example/web/page.html",
        },
    )
    check = ho.check_holding(graph.state(), holding["id"])
    assert check["warnings"] == []
    assert check["ok"] is True


@pytest.mark.parametrize(
    "source_type",
    [
        "peer_review",
        "editorial_decision",
        "author_response",
        "correction",
        "retraction_notice",
    ],
)
def test_review_artifact_missing_target_warns(tmp_path, source_type):
    graph = _graph(tmp_path)
    source = _source(graph, path=f"meta/{source_type}.md")
    holding = ho.record_holding(
        graph, source_id=source["id"], source_type=source_type
    )["holding"]

    check = ho.check_holding(graph.state(), holding["id"])
    assert check["ok"] is True
    warning = next(
        item for item in check["warnings"]
        if item["code"] == "review_artifact_missing_target"
    )
    assert warning["missing"] == ["target"]

    graph.amend(source["id"], metadata={"target_doi": "10.1000/example"})
    check = ho.check_holding(graph.state(), holding["id"])
    assert check["warnings"] == []
    assert check["ok"] is True


@pytest.mark.parametrize(
    ("source_type", "missing", "metadata"),
    [
        ("specimen", ["chain_of_custody"], {"chain_of_custody": "lab A to lab B"}),
        ("sample", ["chain_of_custody"], {"chain_of_custody": "sealed transfer"}),
        ("assay", ["calibration", "instrument"], {
            "calibration": "2026-10-01",
            "instrument": "plate reader",
        }),
        ("instrument_output", ["instrument", "calibration"], {
            "instrument": "spectrometer",
            "calibration": "daily",
        }),
    ],
)
def test_physical_source_missing_custody_calibration_or_instrument_warns(
    tmp_path, source_type, missing, metadata
):
    graph = _graph(tmp_path)
    source = _source(graph, path=f"physical/{source_type}.dat")
    holding = ho.record_holding(
        graph, source_id=source["id"], source_type=source_type
    )["holding"]

    check = ho.check_holding(graph.state(), holding["id"])
    assert check["ok"] is True
    warning = next(
        item for item in check["warnings"]
        if item["code"] == "physical_source_metadata_incomplete"
    )
    assert warning["missing"] == missing

    graph.amend(source["id"], metadata=metadata)
    check = ho.check_holding(graph.state(), holding["id"])
    assert check["warnings"] == []
    assert check["ok"] is True


def test_checks_read_metadata_stored_in_the_source_locator(tmp_path):
    graph = _graph(tmp_path)
    source = graph.record(
        "Source",
        source_type="dataset",
        locator={
            "path": "data/table.csv",
            "metadata": {
                "version": "1.0",
                "license": "CC-BY-4.0",
                "checksum": "sha256:1111111111111111",
            },
        },
    )
    holding = ho.record_holding(
        graph, source_id=source["id"], source_type="dataset"
    )["holding"]

    check = ho.check_holding(graph.state(), holding["id"])
    assert check["ok"] is True
    assert check["warnings"] == []


def test_new_formats_are_usable_in_holding_report_and_claim_context(tmp_path):
    graph = _graph(tmp_path)
    source = _source(graph, path="plans/protocol.md", source_type="protocol")
    holding = ho.record_holding(
        graph, source_id=source["id"], source_type="protocol"
    )["holding"]

    report = ho.holding_report(graph, source_id=source["id"])
    assert report["defaults"]["protocol"]["epistemic_role"] == "plan"
    assert report["holdings"][0]["id"] == holding["id"]

    unheld = _source(graph, path="plans/other.md", source_type="preregistration")
    claim = _claim(graph, source_locator={"path": "plans/other.md"})
    result = ho.claim_holding(graph, claim["id"])

    assert [item["source_id"] for item in result["unheld_sources"]] == [unheld["id"]]
    assert result["default"]["source_type"] == "preregistration"
    assert result["default"]["epistemic_role"] == "plan"
    assert "not_result_evidence" in result["default"]["use_constraints"]
    assert result["truth_assessed"] is False


def test_book_as_evidence_warns_without_rationale_and_scope(tmp_path):
    graph = _graph(tmp_path)
    source = _source(graph, path="books/argument.md")
    holding = ho.record_holding(
        graph,
        source_id=source["id"],
        source_type="book",
        epistemic_role="evidence",
    )["holding"]

    check = ho.check_holding(graph.state(), holding["id"])
    assert check["ok"] is True
    codes = _codes(check["warnings"])
    assert "book_held_as_evidence_without_rationale_or_scope" in codes
    assert "book_metadata_incomplete" in codes

    graph.amend(
        holding["id"],
        scope="chapter 2, experiment 1",
        rationale="The experiment reports extracted measurements.",
    )
    graph.amend(source["id"], metadata=_complete_book_metadata())
    check = ho.check_holding(graph.state(), holding["id"])
    assert check["warnings"] == []
    assert check["ok"] is True


def test_book_metadata_can_come_from_a_manifesting_reference(tmp_path):
    graph = _graph(tmp_path)
    reference = graph.record(
        "Reference",
        title="A Book",
        authors=["Author, A."],
        year="2020",
        venue="Example Press",
        reference_type="book",
        citation_key="author2020",
    )
    source = _source(graph, path="books/argument.md", reference_id=reference["id"])
    graph.link(source["id"], "manifestation_of", reference["id"])
    holding = ho.record_holding(
        graph, source_id=source["id"], source_type="book"
    )["holding"]

    check = ho.check_holding(graph.state(), holding["id"])
    assert check["warnings"] == []


def test_manual_accepted_and_rejected_holdings_are_rechecked(tmp_path):
    graph = _graph(tmp_path)
    source = _source(graph, path="notes/context.md")

    accepted = graph.record(
        "Holding",
        source_id=source["id"],
        source_type="note",
        epistemic_role="personal_note",
        authority="personal",
        review_status="none",
        status="accepted",
        truth_assessed=False,
    )
    graph.link(source["id"], "held_as", accepted["id"])
    check = ho.check_holding(graph.state(), accepted["id"])
    assert check["ok"] is False
    assert "accepted_without_reviewer" in _codes(check["issues"])

    rejected = graph.record(
        "Holding",
        source_id=source["id"],
        source_type="note",
        epistemic_role="personal_note",
        authority="personal",
        review_status="none",
        status="rejected",
        reviewer="reviewer-a",
        truth_assessed=False,
    )
    graph.link(source["id"], "held_as", rejected["id"])
    check = ho.check_holding(graph.state(), rejected["id"])
    assert "rejected_without_rationale" in _codes(check["warnings"])


def test_check_holding_reports_missing_source_and_link(tmp_path):
    graph = _graph(tmp_path)
    orphan = graph.record(
        "Holding",
        source_id="source-9999",
        source_type="book",
        epistemic_role="interpretation",
        authority="editorial",
        review_status="editorial",
        status="proposed",
        truth_assessed=False,
    )

    check = ho.check_holding(graph.state(), orphan["id"])
    assert check["ok"] is False
    codes = _codes(check["issues"])
    assert "holding_source_missing" in codes
    assert "holding_link_missing" in codes
    assert check["counts"]["held_as_edges"] == 0

    with pytest.raises(ho.HoldingError, match="unknown holding id"):
        ho.check_holding(graph.state(), "holding-9999")
    source = _source(graph)
    with pytest.raises(ho.HoldingError, match="not a Holding"):
        ho.check_holding(graph.state(), source["id"])


# --------------------------------------------------------------------------- #
# report
# --------------------------------------------------------------------------- #
def test_holding_report_counts_defaults_and_filters_deterministically(tmp_path):
    graph = _graph(tmp_path)
    book_source = _source(
        graph, path="books/argument.md", metadata=_complete_book_metadata()
    )
    note_source = _source(graph, path="notes/context.md")

    ho.record_holding(graph, source_id=book_source["id"], source_type="book")
    ho.record_holding(
        graph,
        source_id=book_source["id"],
        source_type="book",
        epistemic_role="evidence",
        authority="peer_reviewed",
        review_status="peer_reviewed",
        scope="chapter 2",
        rationale="Extracted experiment.",
        reviewer="reviewer-a",
        status="accepted",
    )
    ho.record_holding(graph, source_id=note_source["id"], source_type="note")

    report = ho.holding_report(graph.state())
    assert len(report["holdings"]) == 3
    assert set(report["defaults"]) == {"book", "note"}
    assert report["defaults"]["book"]["epistemic_role"] == "interpretation"
    assert report["defaults"]["note"]["epistemic_role"] == "personal_note"
    assert report["counts"]["holdings"] == 3
    assert report["counts"]["source_type"] == {"book": 2, "note": 1}
    assert report["counts"]["epistemic_role"] == {
        "evidence": 1,
        "interpretation": 1,
        "personal_note": 1,
    }
    assert report["counts"]["authority"] == {
        "editorial": 1,
        "peer_reviewed": 1,
        "personal": 1,
    }
    assert report["counts"]["status"] == {"accepted": 1, "proposed": 2}
    assert report["truth_assessed"] is False
    assert report == ho.holding_report(graph.state())
    assert json.dumps(report, sort_keys=True) == json.dumps(
        ho.holding_report(graph.state()), sort_keys=True
    )

    filtered = ho.holding_report(graph, source_id=book_source["id"])
    assert filtered["source_id"] == book_source["id"]
    assert filtered["source"]["id"] == book_source["id"]
    assert len(filtered["holdings"]) == 2
    assert set(filtered["defaults"]) == {"book"}
    assert filtered["counts"]["status"] == {"accepted": 1, "proposed": 1}
    assert filtered["truth_assessed"] is False

    with pytest.raises(ho.HoldingError, match="unknown source id"):
        ho.holding_report(graph.state(), source_id="source-9999")


# --------------------------------------------------------------------------- #
# claim context
# --------------------------------------------------------------------------- #
def test_claim_holding_finds_a_cited_source_and_its_holding(tmp_path):
    graph = _graph(tmp_path)
    source = _source(graph, path="books/argument.md", metadata=_complete_book_metadata())
    holding = ho.record_holding(
        graph, source_id=source["id"], source_type="book"
    )["holding"]
    claim = _claim(graph)
    graph.link(claim["id"], "cites", source["id"])

    result = ho.claim_holding(graph, claim["id"])

    assert result["claim"]["id"] == claim["id"]
    assert result["default"] is None
    assert len(result["contexts"]) == 1
    context = result["contexts"][0]
    assert context["source"]["id"] == source["id"]
    assert context["holding"]["id"] == holding["id"]
    assert context["held_as"]["type"] == "held_as"
    assert context["defaults"]["source_type"] == "book"
    assert context["checks"]["ok"] is True
    assert result["counts"] == {
        "sources": 1,
        "holdings": 1,
        "contexts": 1,
        "unheld_sources": 0,
        "warnings": 0,
        "issues": 0,
    }
    assert result["truth_assessed"] is False
    assert result == ho.claim_holding(graph.state(), claim["id"])


def test_claim_holding_finds_a_source_from_the_claim_locator(tmp_path):
    graph = _graph(tmp_path)
    source = _source(graph, path="notes/reading.md")
    holding = ho.record_holding(
        graph,
        source_id=source["id"],
        source_type="note",
        epistemic_role="evidence",
        scope="the quoted passage",
        rationale="The note records the only observation.",
    )["holding"]
    claim = _claim(graph, source_locator={"path": "notes/reading.md"})

    result = ho.claim_holding(graph.state(), claim["id"])

    assert [context["holding"]["id"] for context in result["contexts"]] == [
        holding["id"]
    ]
    assert "note_held_as_evidence" in _codes(result["warnings"])
    assert "note_held_as_evidence" in _codes(result["issues"])
    assert result["contexts"][0]["checks"]["ok"] is False
    assert result["default"] is None
    assert result["truth_assessed"] is False


def test_claim_holding_returns_a_default_when_a_cited_source_has_no_holding(tmp_path):
    graph = _graph(tmp_path)
    source = _source(graph, path="notes/context.md", source_type="note")
    claim = _claim(graph, source_locator={"path": "notes/context.md"})

    result = ho.claim_holding(graph, claim["id"])

    assert result["contexts"] == []
    assert result["default"] is not None
    assert result["default"]["source_id"] == source["id"]
    assert result["default"]["source_type"] == "note"
    assert result["default"]["epistemic_role"] == "personal_note"
    assert result["default"]["authority"] == "personal"
    assert result["default"]["use_constraints"] == ["not_external_evidence"]
    assert "claim_source_without_holding" in _codes(result["warnings"])
    assert len(result["unheld_sources"]) == 1
    assert result["counts"]["unheld_sources"] == 1
    assert result["truth_assessed"] is False


def test_claim_holding_returns_a_generic_default_when_no_source_is_recorded(tmp_path):
    graph = _graph(tmp_path)
    claim = _claim(graph)

    result = ho.claim_holding(graph.state(), claim["id"])

    assert result["contexts"] == []
    assert result["sources"] == []
    assert result["default"]["source_type"] is None
    assert result["default"]["epistemic_role"] == "background"
    assert result["default"]["authority"] == "unknown"
    assert "record_holding_before_use" in result["default"]["use_constraints"]
    assert result["warnings"] == []
    assert result["truth_assessed"] is False


def test_claim_holding_follows_a_cited_reference_to_its_source(tmp_path):
    graph = _graph(tmp_path)
    reference = graph.record(
        "Reference",
        title="A Book",
        year="2020",
        venue="Example Press",
        reference_type="book",
        citation_key="author2020",
    )
    source = _source(
        graph,
        path="books/argument.md",
        reference_id=reference["id"],
        metadata=_complete_book_metadata(),
    )
    graph.link(source["id"], "manifestation_of", reference["id"])
    holding = ho.record_holding(
        graph, source_id=source["id"], source_type="book"
    )["holding"]
    claim = _claim(graph)
    graph.link(claim["id"], "cites", reference["id"])

    result = ho.claim_holding(graph, claim["id"])

    assert result["reference_ids"] == [reference["id"]]
    assert [context["holding"]["id"] for context in result["contexts"]] == [
        holding["id"]
    ]
    assert result["truth_assessed"] is False

    with pytest.raises(ho.HoldingError, match="not a Claim"):
        ho.claim_holding(graph.state(), reference["id"])
