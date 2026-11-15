from __future__ import annotations

import csv
import io

import pytest

from ozagia.research import extraction as ext
from ozagia.research import references as refs
from ozagia.research.graph import ResearchGraph


def _graph(tmp_path) -> ResearchGraph:
    return ResearchGraph.create(
        "Structured extraction tests",
        root=tmp_path,
        investigation_id="extraction-test",
    )


def _reference(graph: ResearchGraph, key: str = "smith2024") -> dict:
    return refs.record_reference(graph, {
        "title": "A Trial of Something",
        "citation_key": key,
        "identifiers": {"doi": f"10.1234/{key}"},
    })["reference"]


def _claim(graph: ResearchGraph) -> dict:
    return graph.record(
        "Claim",
        wording="The intervention reduces the outcome.",
        scope="Adults in the trial.",
        claim_type="hypothesis",
        author="reader",
        status="unresolved",
    )


def _fields(**overrides) -> dict:
    fields = {
        "study_design": "randomized_controlled_trial",
        "population": "adults",
        "intervention": "intervention",
        "comparator": "placebo",
        "outcomes": ["mortality"],
        "effect_measure": "risk_ratio",
        "effect_size": 0.75,
        "ci_low": 0.6,
        "ci_high": 0.9,
        "p_value": 0.01,
        "sample_size": 120,
        "unit": "ratio",
        "direction": "decrease",
        "notes": "Reported in Table 2.",
    }
    fields.update(overrides)
    return fields


def _record(graph: ResearchGraph, reference: dict | None = None, **overrides) -> dict:
    reference = reference or _reference(graph)
    options = {
        "reference_id": reference["id"],
        "fields": _fields(),
        "source_locator": {"path": "paper.pdf", "start_line": 4, "end_line": 6},
        "extractor": "reader",
        "reviewer": "checker",
    }
    options.update(overrides)
    return ext.record_extraction(graph, **options)


def test_record_extraction_records_fields_and_extracts_link(tmp_path):
    graph = _graph(tmp_path)
    reference = _reference(graph)
    result = _record(graph, reference)
    node = result["extraction"]

    assert node["type"] == "Extraction"
    assert node["target_id"] == reference["id"]
    assert node["reference_id"] == reference["id"]
    assert node["claim_id"] is None
    assert node["status"] == "candidate"
    assert node["extractor"] == "reader"
    assert node["reviewer"] == "checker"
    assert node["truth_assessed"] is False
    assert list(node["fields"]) == list(ext.EXTRACTION_FIELDS)
    assert all(node[field] == node["fields"][field] for field in ext.EXTRACTION_FIELDS)
    assert node["fields"]["effect_size"] == 0.75
    assert node["fields"]["sample_size"] == 120
    assert node["source_locator"]["path"] == "paper.pdf"
    assert result["edge"]["type"] == "extracts"
    assert result["edge"]["src"] == node["id"]
    assert result["edge"]["tgt"] == reference["id"]

    state = graph.state()
    assert any(
        edge["type"] == "extracts"
        and edge["src"] == node["id"]
        and edge["tgt"] == reference["id"]
        for edge in state["edges"]
    )
    check = ext.check_extraction(state, node["id"])
    assert check["ok"] is True
    assert check["issues"] == []
    assert check["warnings"] == []
    assert check["counts"]["fields_present"] == len(ext.EXTRACTION_FIELDS)
    assert check["truth_assessed"] is False

    report = ext.extraction_report(state, node["id"])
    assert report["node"]["id"] == node["id"]
    assert report["target"]["id"] == reference["id"]
    assert report["links"][0]["tgt"] == reference["id"]
    assert report["checks"]["ok"] is True
    assert report["truth_assessed"] is False


def test_claim_target_records_claim_id(tmp_path):
    graph = _graph(tmp_path)
    claim = _claim(graph)
    result = ext.record_extraction(
        graph,
        claim_id=claim["id"],
        fields=_fields(),
        source_locator={"page": 7},
        extractor="reader",
    )
    node = result["extraction"]
    assert node["target_id"] == claim["id"]
    assert node["claim_id"] == claim["id"]
    assert node["reference_id"] is None
    assert result["edge"]["tgt"] == claim["id"]
    assert ext.check_extraction(graph.state(), node["id"])["ok"] is True


def test_record_extraction_rejects_unknown_or_ambiguous_targets(tmp_path):
    graph = _graph(tmp_path)
    reference = _reference(graph)
    claim = _claim(graph)
    common = {
        "fields": _fields(),
        "source_locator": {"path": "paper.pdf"},
        "extractor": "reader",
    }

    with pytest.raises(ext.ExtractionError, match="unknown Reference"):
        ext.record_extraction(
            graph, reference_id="reference-9999", **common)
    with pytest.raises(ext.ExtractionError, match="not a Reference"):
        ext.record_extraction(
            graph, reference_id=claim["id"], **common)
    with pytest.raises(ext.ExtractionError, match="exactly one"):
        ext.record_extraction(graph, **common)
    with pytest.raises(ext.ExtractionError, match="exactly one"):
        ext.record_extraction(
            graph,
            reference_id=reference["id"],
            claim_id=claim["id"],
            **common,
        )
    assert not any(
        node["type"] == "Extraction" for node in graph.state()["nodes"]
    )


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"sample_size": -1}, "sample_size must be a positive integer"),
        ({"sample_size": 0}, "sample_size must be a positive integer"),
        ({"sample_size": 1.5}, "sample_size must be a positive integer"),
        ({"ci_low": 0.9, "ci_high": 0.1}, "ci_low must not exceed ci_high"),
        ({"p_value": -0.01}, "p_value must be between 0 and 1"),
        ({"p_value": 1.01}, "p_value must be between 0 and 1"),
        ({"effect_size": float("inf")}, "effect_size must be a finite number"),
        ({"effect_size": float("nan")}, "effect_size must be a finite number"),
        ({"unexpected": "value"}, "unknown extraction field"),
        ({"outcomes": "mortality"}, "outcomes must be a non-empty list"),
        ({"outcomes": ["mortality", ""]}, "outcomes must contain non-empty strings"),
        ({"effect_measure": 7}, "effect_measure must be a non-empty string"),
    ],
)
def test_record_extraction_rejects_invalid_fields(tmp_path, overrides, message):
    graph = _graph(tmp_path)
    reference = _reference(graph)
    with pytest.raises(ext.ExtractionError, match=message):
        _record(graph, reference, fields=_fields(**overrides))
    assert not any(
        node["type"] == "Extraction" for node in graph.state()["nodes"]
    )


def test_absent_optional_fields_are_preserved_as_none(tmp_path):
    graph = _graph(tmp_path)
    reference = _reference(graph)
    result = _record(graph, reference, fields={})
    fields = result["extraction"]["fields"]
    assert list(fields) == list(ext.EXTRACTION_FIELDS)
    assert all(value is None for value in fields.values())
    check = ext.check_extraction(graph.state(), result["extraction"]["id"])
    codes = {issue["code"] for issue in check["issues"]}
    assert "required_field_missing" in codes
    assert "effect_measure_or_size_missing" in codes
    assert check["counts"]["fields_present"] == 0


def test_check_reports_missing_source_locator(tmp_path):
    graph = _graph(tmp_path)
    reference = _reference(graph)
    result = _record(graph, reference, source_locator=None)
    node = result["extraction"]
    check = ext.check_extraction(graph.state(), node["id"])
    assert check["ok"] is False
    assert any(
        issue["code"] == "source_locator_missing" for issue in check["issues"]
    )
    assert check["counts"]["issues"] == len(check["issues"])
    assert check["truth_assessed"] is False


def test_check_reports_numeric_inconsistency_in_legacy_node(tmp_path):
    graph = _graph(tmp_path)
    reference = _reference(graph)
    node = graph.record(
        "Extraction",
        target_id=reference["id"],
        reference_id=reference["id"],
        claim_id=None,
        fields={
            "study_design": "cohort",
            "outcomes": ["response"],
            "effect_measure": "risk_ratio",
            "effect_size": 1.5,
            "ci_low": 2.0,
            "ci_high": 1.0,
            "p_value": 1.2,
            "sample_size": 0,
        },
        source_locator={"path": "legacy.pdf", "page": 3},
        extractor="reader",
        reviewer=None,
        status="candidate",
        truth_assessed=False,
    )
    graph.link(node["id"], "extracts", reference["id"])
    check = ext.check_extraction(graph.state(), node["id"])
    codes = {issue["code"] for issue in check["issues"]}
    assert check["ok"] is False
    assert {"ci_order_invalid", "p_value_out_of_range", "sample_size_invalid"} <= codes
    assert "required_field_missing" not in codes
    assert any(
        warning["code"] == "reviewer_missing" for warning in check["warnings"]
    )


def test_check_and_report_require_an_existing_extraction(tmp_path):
    graph = _graph(tmp_path)
    with pytest.raises(ext.ExtractionError, match="unknown extraction id"):
        ext.check_extraction(graph.state(), "extraction-9999")
    with pytest.raises(ext.ExtractionError, match="unknown extraction id"):
        ext.extraction_report(graph.state(), "extraction-9999")


def test_extraction_table_is_deterministic_in_markdown_and_csv(tmp_path):
    graph = _graph(tmp_path)
    reference = _reference(graph)
    first = _record(
        graph,
        reference,
        fields=_fields(population="adults | older\ncohort"),
    )["extraction"]
    second = _record(
        graph,
        reference,
        fields=_fields(
            study_design="cohort",
            outcomes=["response", "adherence"],
            effect_measure="mean_difference",
            effect_size=1.2,
            ci_low=0.3,
            ci_high=2.1,
            p_value=0.04,
            sample_size=42,
            unit="points",
            direction="increase",
            notes=None,
        ),
        source_locator={"page": 7},
    )["extraction"]
    state = graph.state()

    markdown = ext.extraction_table(state, format="markdown")
    assert markdown["content"] == ext.extraction_table(state, format="markdown")["content"]
    assert markdown["rows"] == ext.extraction_table(
        {**state, "nodes": list(reversed(state["nodes"]))}
    )["rows"]
    assert markdown["count"] == 2
    assert markdown["rows"][0]["extraction_id"] == first["id"]
    assert markdown["rows"][1]["extraction_id"] == second["id"]
    assert list(markdown["rows"][0]) == list(ext.EXTRACTION_TABLE_COLUMNS)
    assert markdown["content"].startswith(
        "| extraction_id | target | design | population | intervention | comparator |"
    )
    assert "adults \\| older cohort" in markdown["content"]
    assert "paper.pdf:4-6" in markdown["content"]
    assert "0.6 to 0.9" in markdown["content"]
    assert "p. 7" in markdown["content"]

    csv_table = ext.extraction_table(state, format="csv")
    assert csv_table["content"] == ext.extraction_table(state, format="csv")["content"]
    assert csv_table["content"].startswith(
        "extraction_id,target,design,population,intervention,comparator,"
    )
    rows = list(csv.DictReader(io.StringIO(csv_table["content"])))
    assert [row["extraction_id"] for row in rows] == [first["id"], second["id"]]
    assert rows[0]["population"] == "adults | older\ncohort"
    assert rows[0]["CI"] == "0.6 to 0.9"
    assert rows[1]["outcomes"] == "response; adherence"
    assert rows[1]["status"] == "candidate"
    assert csv_table["truth_assessed"] is False

    with pytest.raises(ext.ExtractionError, match="markdown or csv"):
        ext.extraction_table(state, format="html")
