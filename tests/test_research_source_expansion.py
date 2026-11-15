from __future__ import annotations

from ozagia.research import holdings, source_formats
from ozagia.research.service import ResearchService


def test_registries_cover_the_same_source_types():
    assert set(holdings.SOURCE_TYPES) == set(source_formats.SOURCE_FORMATS)
    assert len(holdings.SOURCE_TYPES) >= 70


def test_new_source_holdings_and_checks(tmp_path):
    svc = ResearchService(tmp_path)
    sid = svc.call("init", {"question": "Expanded sources"})["investigation_id"]

    protocol = svc.call("source_register", {
        "session_id": sid,
        "source_type": "protocol",
        "metadata": {"title": "Trial protocol", "version": "1.0"},
        "reviewer": "reader",
    })
    assert protocol["holding"]["epistemic_role"] == "plan"
    evidence_hold = svc.call("holding_record", {
        "session_id": sid,
        "source_id": protocol["source"]["id"],
        "source_type": "protocol",
        "epistemic_role": "evidence",
        "reviewer": "reader",
    })["holding"]
    checked = svc.call("holding_check", {
        "session_id": sid, "holding_id": evidence_hold["id"]})
    assert any(issue["code"] == "plan_held_as_result_evidence"
               for issue in checked["issues"])

    dataset = svc.call("source_register", {
        "session_id": sid,
        "source_type": "dataset",
        "metadata": {"title": "Dataset", "year": "2024"},
        "reviewer": "reader",
    })
    assert dataset["holding"]["epistemic_role"] in {"primary_source", "primary_data"}
    dataset_check = svc.call("holding_check", {
        "session_id": sid, "holding_id": dataset["holding"]["id"]})
    assert any(warning["code"] == "artifact_metadata_incomplete"
               for warning in dataset_check["warnings"])

    regulation = svc.call("source_register", {
        "session_id": sid,
        "source_type": "regulation",
        "metadata": {"title": "Regulation", "issuing_body": "Authority"},
        "reviewer": "reader",
    })
    assert regulation["holding"]["epistemic_role"] == "normative"
    assert regulation["holding"]["authority"] == "regulatory"

    review = svc.call("source_register", {
        "session_id": sid,
        "source_type": "peer_review",
        "metadata": {"title": "Review report", "target_doi": "10.1234/a.1"},
        "reviewer": "reader",
    })
    assert review["holding"]["epistemic_role"] == "review_artifact"

    interview = svc.call("source_register", {
        "session_id": sid,
        "source_type": "interview",
        "metadata": {"title": "Interview transcript"},
        "reviewer": "reader",
    })
    assert interview["holding"]["epistemic_role"] == "primary_source"

    synthetic = svc.call("source_register", {
        "session_id": sid,
        "source_type": "synthetic_data",
        "metadata": {"title": "Synthetic sample"},
        "reviewer": "reader",
    })
    assert synthetic["holding"]["epistemic_role"] == "synthetic"
    synthetic_hold = svc.call("holding_record", {
        "session_id": sid,
        "source_id": synthetic["source"]["id"],
        "source_type": "synthetic_data",
        "epistemic_role": "evidence",
        "reviewer": "reader",
    })["holding"]
    synthetic_check = svc.call("holding_check", {
        "session_id": sid, "holding_id": synthetic_hold["id"]})
    assert any(issue["code"] == "synthetic_held_as_evidence"
               for issue in synthetic_check["issues"])

    versioned = svc.call("source_register", {
        "session_id": sid,
        "source_type": "dataset",
        "metadata": {
            "title": "Dataset v2",
            "year": "2025",
            "version": "2.0",
            "license": "CC-BY-4.0",
            "checksum": "sha256:abc",
            "supersedes": dataset["source"]["id"],
        },
        "reviewer": "reader",
    })
    assert versioned["source"]["version"] == "2.0"
    assert versioned["source"]["license"] == "CC-BY-4.0"
    edges = svc.call("inspect", {
        "session_id": sid, "node_id": versioned["source"]["id"]})["edges"]
    assert any(edge["type"] == "supersedes"
               and edge["src"] == versioned["source"]["id"]
               and edge["tgt"] == dataset["source"]["id"] for edge in edges)

    report = svc.call("holding_report", {"session_id": sid})
    assert report["counts"]["holdings"] >= 7
    assert report["truth_assessed"] is False
