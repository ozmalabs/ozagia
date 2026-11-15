from __future__ import annotations

from ozagia.research.service import ResearchService


def test_extraction_appraisal_contradiction_and_alerts(tmp_path):
    svc = ResearchService(tmp_path)
    sid = svc.call("init", {"question": "Synthesis service"})["investigation_id"]
    reference = svc.call("ref_record", {
        "session_id": sid,
        "reference": {"title": "A Trial", "citation_key": "trial2024",
                      "identifiers": {"doi": "10.1234/trial.1"}},
    })["reference"]
    claim_a = svc.call("record", {
        "session_id": sid, "kind": "Claim",
        "fields": {"wording": "The treatment helps.", "claim_type": "hypothesis",
                   "status": "supported_within_scope", "scope": "adults",
                   "citations": ["trial2024"]},
    })
    claim_b = svc.call("record", {
        "session_id": sid, "kind": "Claim",
        "fields": {"wording": "The treatment does not help.", "claim_type": "hypothesis",
                   "status": "contradicted", "scope": "adults"},
    })
    svc.call("link", {
        "session_id": sid, "src": claim_a["id"], "relation": "opposes",
        "tgt": claim_b["id"],
    })

    extraction = svc.call("extraction_record", {
        "session_id": sid,
        "reference_id": reference["id"],
        "extractor": "reader",
        "source_locator": {"path": "trial.tex", "start_line": 10},
        "fields": {
            "study_design": "randomised trial",
            "population": "adults",
            "intervention": "treatment",
            "comparator": "placebo",
            "outcomes": ["response"],
            "effect_measure": "risk ratio",
            "effect_size": 1.4,
            "ci_low": 1.1,
            "ci_high": 1.8,
            "p_value": 0.01,
            "sample_size": 120,
            "unit": "ratio",
            "direction": "increase",
        },
    })
    checked = svc.call("extraction_check", {
        "session_id": sid, "extraction_id": extraction["extraction"]["id"]})
    assert checked["ok"] is True
    table = svc.call("extraction_table", {"session_id": sid, "format": "csv"})
    assert "risk ratio" in table["content"]

    risk = svc.call("risk_of_bias_record", {
        "session_id": sid,
        "target_id": extraction["extraction"]["id"],
        "tool": "RoB 2",
        "domains": [
            {"name": "randomisation", "judgement": "low",
             "support": "Random sequence reported."},
            {"name": "missing data", "judgement": "some_concerns",
             "support": "Attrition is not described."},
        ],
        "overall": "some_concerns",
        "reviewer": "reader",
    })
    assert risk["assessment"]["type"] == "RiskOfBias"
    assert svc.call("risk_of_bias_check", {
        "session_id": sid, "assessment_id": risk["assessment"]["id"]})["ok"] is True

    certainty = svc.call("certainty_record", {
        "session_id": sid,
        "target_id": claim_a["id"],
        "rating": "moderate",
        "reasons": ["One trial with some risk of bias."],
        "reviewer": "reader",
        "downgrades": [{"domain": "risk of bias", "level": 1,
                        "reason": "Some concerns in missing data."}],
    })
    assert certainty["certainty"]["rating"] == "moderate"
    report = svc.call("certainty_report", {
        "session_id": sid, "target_id": claim_a["id"]})
    assert report["counts"]["certainty"] == 1
    appraisal = svc.call("appraisal_table", {"session_id": sid, "format": "markdown"})
    assert "RoB 2" in appraisal["content"]

    candidates = svc.call("contradiction_scan", {"session_id": sid})
    assert candidates["count"] >= 1
    contradiction = svc.call("contradiction_record", {
        "session_id": sid,
        "claim_a_id": claim_a["id"],
        "claim_b_id": claim_b["id"],
        "kind": "explicit_opposition",
        "rationale": "The claims assert opposite effects in the same scope.",
        "reviewer": "reader",
    })
    assert contradiction["contradiction"]["type"] == "Contradiction"
    assert svc.call("contradiction_check", {
        "session_id": sid,
        "contradiction_id": contradiction["contradiction"]["id"]})["ok"] is True
    assert svc.call("contradiction_report", {"session_id": sid})["counts"]["records"] >= 1

    retracted = svc.call("ref_record", {
        "session_id": sid,
        "reference": {"title": "Retracted Work", "citation_key": "bad2020",
                      "identifiers": {"doi": "10.9999/bad"},
                      "retraction": {"status": "retracted", "provider": "openalex"}},
    })["reference"]
    svc.call("record", {
        "session_id": sid, "kind": "Claim",
        "fields": {"wording": "A claim relying on a retracted work.",
                   "claim_type": "inference", "status": "open",
                   "citations": ["bad2020"]},
    })
    alerts = svc.call("alert_scan", {"session_id": sid})
    kinds = {alert["kind"] for alert in alerts["alerts"]}
    assert "retracted_reference" in kinds
    assert "claim_cites_retracted" in kinds
    first = alerts["alerts"][0]
    recorded = svc.call("alert_record", {
        "session_id": sid, "kind": first["kind"], "target_id": first["target_id"],
        "payload": first.get("payload"), "reviewer": "reader",
    })
    ack = svc.call("alert_ack", {
        "session_id": sid, "alert_id": recorded["alert"]["id"],
        "reviewer": "reader", "note": "Reviewed."})
    assert ack["alert"]["status"] == "acknowledged"
    alert_report = svc.call("alert_report", {"session_id": sid})
    assert alert_report["counts"]["acknowledged"] >= 1
    assert retracted["id"]
