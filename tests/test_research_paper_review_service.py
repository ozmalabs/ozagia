from __future__ import annotations

from pathlib import Path

from ozagia.research.service import ResearchService


def test_paper_review_start_runs_arguments_report_and_bundle(tmp_path):
    source_root = tmp_path / "sources"
    source_root.mkdir()
    (source_root / "paper.tex").write_text(
        r"\begin{theorem}\label{thm:main} The treatment helps adults \cite{trial2024}.\end{theorem}"
        "\n"
        r"\begin{thebibliography}{9}"
        "\n"
        r"\bibitem{trial2024} A. Author. \emph{A Trial}. 2024. doi:10.1234/trial.1"
        "\n"
        r"\end{thebibliography}"
        "\n",
        encoding="utf-8",
    )
    svc = ResearchService(tmp_path)
    sid = svc.call("init", {
        "question": "Review a paper",
        "roots": [{
            "root_id": "papers", "path": str(source_root),
            "files": ["paper.tex"],
        }],
    })["investigation_id"]

    started = svc.call("paper_review_start", {
        "session_id": sid,
        "root_id": "papers",
        "path": "paper.tex",
        "question": "Is the treatment effective?",
        "roles": ["methods", "statistics"],
        "models": ["model-a", "model-b"],
        "min_reviewers": 2,
        "reviewer": "reader",
    })
    packet = started["packet"]
    plan = started["plan"]
    assert packet["claim_ids"]
    claim_id = packet["claim_ids"][0]

    output = {
        "summary": "One methods concern.",
        "findings": [{
            "wording": "The comparison lacks a baseline.",
            "severity": "major",
            "issue_type": "methods",
            "claim_id": claim_id,
            "confidence": 0.8,
        }],
        "recommendation": "major_revision",
        "confidence": 0.7,
    }
    for model in ("model-a", "model-b"):
        svc.call("review_run_record", {
            "session_id": sid,
            "plan_id": plan["id"],
            "reviewer_id": f"methods:{model}",
            "role": "methods",
            "model": model,
            "packet_sha256": packet["packet_sha256"],
            "output": output,
        })

    argument = svc.call("argument_record", {
        "session_id": sid,
        "claim_id": claim_id,
        "mode": "steelman",
        "thesis": "The treatment helps adults under the tested conditions.",
        "premises": [{
            "wording": "The trial reports a benefit.",
            "inference": "inductive",
            "claim_ids": [claim_id],
            "strength": 0.7,
        }],
        "assumptions": ["The trial population is representative."],
        "objections": [{
            "wording": "An unmeasured confound could explain the result.",
            "status": "unanswered",
        }],
        "falsifiers": ["A larger trial shows no benefit."],
        "confidence": 0.6,
        "reviewer": "advocate:model-a",
    })["argument"]
    assert svc.call("argument_check", {
        "session_id": sid, "argument_id": argument["id"]})["ok"] is True

    reconciled = svc.call("review_reconcile", {
        "session_id": sid, "plan_id": plan["id"], "method": "consensus"})
    assert reconciled["consensus"]

    report = svc.call("paper_review_report", {
        "session_id": sid, "plan_id": plan["id"]})
    assert report["plan"]["id"] == plan["id"]
    assert report["claims"]
    assert report["arguments"]
    assert "strengths" in report["sections"]
    assert report["truth_assessed"] is False

    bundle = svc.call("paper_review_bundle", {
        "session_id": sid, "plan_id": plan["id"],
        "output_dir": str(tmp_path / "review-bundle"),
    })
    for name in ("review.md", "review.json", "claims.csv", "evidence.csv",
                 "references.bib", "arguments.json", "checks.json"):
        assert Path(bundle["paths"][name]).is_file()
    assert (tmp_path / "review-bundle" / "review.md").read_text(
        encoding="utf-8").startswith("#")
