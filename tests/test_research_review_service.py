from __future__ import annotations

from ozagia.research.service import ResearchService


def test_review_plan_runs_agreement_reconciliation_and_argument(tmp_path):
    svc = ResearchService(tmp_path)
    sid = svc.call("init", {"question": "Structured review service"})["investigation_id"]
    claim = svc.call("record", {
        "session_id": sid, "kind": "Claim",
        "fields": {"wording": "The method is robust.",
                   "claim_type": "hypothesis", "status": "open"},
    })
    evidence = svc.call("record", {
        "session_id": sid, "kind": "Evidence",
        "fields": {"wording": "A controlled comparison supports robustness.",
                   "source_locator": {"path": "paper.tex", "start_line": 4}},
    })
    svc.call("link", {
        "session_id": sid, "src": evidence["id"], "relation": "supports",
        "tgt": claim["id"],
    })

    plan = svc.call("review_plan_create", {
        "session_id": sid,
        "question": "Is the method robust?",
        "kind": "peer_review",
        "roles": ["methods", "statistics"],
        "models": ["model-a", "model-b"],
        "min_reviewers": 2,
        "target_id": claim["id"],
    })["plan"]
    packet = svc.call("review_packet", {
        "session_id": sid, "plan_id": plan["id"]})
    assert packet["packet_sha256"]
    assert packet["targets"][0]["id"] == claim["id"]

    output = {
        "summary": "One major methods concern.",
        "findings": [{
            "wording": "The comparison lacks a baseline.",
            "severity": "major",
            "issue_type": "methods",
            "claim_id": claim["id"],
            "confidence": 0.8,
        }],
        "recommendation": "major_revision",
        "confidence": 0.7,
    }
    validated = svc.call("review_validate", {
        "session_id": sid, "output": output, "packet": packet})
    assert validated["valid"] is True

    for model in ("model-a", "model-b"):
        run = svc.call("review_run_record", {
            "session_id": sid,
            "plan_id": plan["id"],
            "reviewer_id": f"methods:{model}",
            "role": "methods",
            "model": model,
            "packet_sha256": packet["packet_sha256"],
            "output": output,
        })
        assert run["run"]["finding_count"] == 1
        assert run["findings"][0]["type"] == "ReviewFinding"

    agreement = svc.call("review_agreement", {
        "session_id": sid, "plan_id": plan["id"]})
    assert agreement["counts"]["runs"] == 2
    assert agreement["counts"]["consensus"] == 1
    assert agreement["counts"]["dissent"] == 0
    assert agreement["fleiss_kappa"] == 1.0

    third = svc.call("review_run_record", {
        "session_id": sid,
        "plan_id": plan["id"],
        "reviewer_id": "statistics:model-b",
        "role": "statistics",
        "model": "model-b",
        "packet_sha256": packet["packet_sha256"],
        "output": {
            "summary": "One statistics concern.",
            "findings": [{
                "wording": "The confidence interval is not reported.",
                "severity": "minor",
                "issue_type": "statistics",
                "claim_id": claim["id"],
                "confidence": 0.6,
            }],
        },
    })
    assert third["run"]["role"] == "statistics"

    reconciled = svc.call("review_reconcile", {
        "session_id": sid, "plan_id": plan["id"], "method": "consensus"})
    assert reconciled["consensus"]
    assert reconciled["dissent"]
    assert reconciled["reconciliation"]["truth_assessed"] is False

    report = svc.call("review_report", {
        "session_id": sid, "plan_id": plan["id"]})
    assert report["counts"]["runs"] == 3
    assert report["reconciliation"]["id"] == reconciled["reconciliation"]["id"]

    brief = svc.call("argument_brief", {
        "session_id": sid, "claim_id": claim["id"], "mode": "steelman"})
    assert brief["supports"]
    argument = svc.call("argument_record", {
        "session_id": sid,
        "claim_id": claim["id"],
        "mode": "steelman",
        "thesis": "The method is robust under the tested conditions.",
        "premises": [{
            "wording": "A controlled comparison supports robustness.",
            "inference": "inductive",
            "evidence_ids": [evidence["id"]],
            "strength": 0.8,
        }],
        "assumptions": ["The tested conditions are representative."],
        "objections": [{
            "wording": "An unmeasured confound could explain the result.",
            "status": "unanswered",
        }],
        "falsifiers": ["A replication failure under the same conditions."],
        "confidence": 0.6,
        "reviewer": "reader",
    })
    assert argument["argument"]["type"] == "Argument"
    checked = svc.call("argument_check", {
        "session_id": sid, "argument_id": argument["argument"]["id"]})
    assert checked["ok"] is True
    assert checked["counts"]["premises"] == 1
