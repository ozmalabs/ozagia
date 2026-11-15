from __future__ import annotations

from ozagia.research.service import ResearchService


def test_theory_ledger_shared_evidence_compatibility_and_discernment(tmp_path):
    svc = ResearchService(tmp_path)
    sid = svc.call("init", {"question": "Theory competition"})["investigation_id"]

    shared = svc.call("record", {
        "session_id": sid, "kind": "Evidence",
        "fields": {"wording": "Both theories predict this observation.",
                   "source_locator": {"path": "study.tex", "start_line": 3}},
    })
    unique_a = svc.call("record", {
        "session_id": sid, "kind": "Evidence",
        "fields": {"wording": "Only theory A predicts this.",
                   "source_locator": {"path": "study.tex", "start_line": 8}},
    })
    unique_b = svc.call("record", {
        "session_id": sid, "kind": "Evidence",
        "fields": {"wording": "Only theory B predicts this.",
                   "source_locator": {"path": "study.tex", "start_line": 12}},
    })
    discriminating = svc.call("record", {
        "session_id": sid, "kind": "Evidence",
        "fields": {"wording": "A test that separates the theories.",
                   "source_locator": {"path": "study.tex", "start_line": 20}},
    })

    theory_a = svc.call("theory_record", {
        "session_id": sid,
        "name": "Theory A",
        "statement": "Mechanism A produces the effect.",
        "scope": "adults",
        "mechanism": "Pathway A",
        "assumptions": ["The assay is specific."],
        "predictions": ["A increases under condition X."],
        "reviewer": "reader",
    })["theory"]
    theory_b = svc.call("theory_record", {
        "session_id": sid,
        "name": "Theory B",
        "statement": "Mechanism B produces the effect.",
        "scope": "adults",
        "mechanism": "Pathway B",
        "predictions": ["B increases under condition Y."],
        "reviewer": "reader",
    })["theory"]

    for theory, evidence in (
        (theory_a, shared), (theory_b, shared),
        (theory_a, unique_a), (theory_b, unique_b),
    ):
        svc.call("theory_link_evidence", {
            "session_id": sid, "theory_id": theory["id"],
            "target_id": evidence["id"], "relation": "supported_by",
        })
    svc.call("theory_link_evidence", {
        "session_id": sid, "theory_id": theory_a["id"],
        "target_id": discriminating["id"], "relation": "discriminated_by",
    })

    svc.call("theory_rivalry", {
        "session_id": sid, "theory_a": theory_a["id"], "theory_b": theory_b["id"],
        "scope": "adults", "rationale": "They posit different mechanisms.",
        "reviewer": "reader",
    })
    compatibility = svc.call("theory_compatibility", {
        "session_id": sid, "theory_a": theory_a["id"], "theory_b": theory_b["id"],
        "degree": "partially_compatible", "scope": "adults",
        "rationale": "They share the same outcome but differ in mechanism.",
        "reviewer": "reader",
    })
    assert compatibility["edge"]["provenance"]["degree"] == "partially_compatible"

    comparison = svc.call("theory_compare", {
        "session_id": sid, "theory_a": theory_a["id"], "theory_b": theory_b["id"]})
    assert comparison["shared_evidence"] == [shared["id"]]
    assert comparison["unique_a"] == [unique_a["id"]]
    assert comparison["unique_b"] == [unique_b["id"]]
    assert comparison["compatibility"][0]["degree"] == "partially_compatible"
    assert comparison["discernment"]["status"] == "discriminating_evidence_present"
    assert comparison["truth_assessed"] is False

    scan = svc.call("theory_scan", {"session_id": sid})
    assert scan["counts"]["theories"] == 2
    assert scan["truth_assessed"] is False

    report = svc.call("theory_report", {
        "session_id": sid, "theory_id": theory_a["id"]})
    assert report["theory"]["id"] == theory_a["id"]
    matrix = svc.call("theory_compatibility_matrix", {"session_id": sid})
    assert matrix["pairs"]
    assert matrix["truth_assessed"] is False
