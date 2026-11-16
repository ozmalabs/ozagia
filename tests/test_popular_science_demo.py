from __future__ import annotations

import json
from pathlib import Path

from ozagia.harness.paper_review import run_paper_review
from ozagia.research.graph import ResearchGraph
from ozagia.research.service import ResearchService

FIXTURE = Path(__file__).parent / "fixtures/research/origin_excerpt.md"


def _prompt(messages) -> str:
    return "\n".join(str(message.get("content") or "") for message in messages)


def _fake_runner(claim_id: str):
    def run(model: str, messages, **_kwargs):
        prompt = _prompt(messages)
        if "argument builder" in prompt:
            output = {
                "thesis": "Natural selection is the main, but not exclusive, means of modification.",
                "premises": [{
                    "wording": "The book argues from distribution, geology and coadaptation.",
                    "inference": "inductive",
                    "claim_ids": [claim_id],
                    "strength": 0.7,
                }],
                "assumptions": ["The excerpt represents the book's central argument."],
                "objections": [{
                    "wording": "The excerpt does not quantify the selective mechanism.",
                    "status": "unanswered",
                }],
                "falsifiers": ["A fossil record incompatible with gradual descent."],
                "confidence": 0.6,
                "notes": f"fake argument from {model}",
            }
        else:
            findings = [{
                "wording": "The excerpt asserts a mechanism but supplies little quantitative evidence.",
                "severity": "major",
                "issue_type": "evidence",
                "claim_id": claim_id,
                "confidence": 0.7,
            }]
            if model.endswith("b"):
                findings.append({
                    "wording": "The argument is explicitly framed as an abstract, not a complete proof.",
                    "severity": "note",
                    "issue_type": "scope",
                    "claim_id": claim_id,
                    "confidence": 0.8,
                })
            output = {
                "summary": "A historically important argument with an explicit evidential limit.",
                "strengths": [
                    "States a testable two-part thesis: common descent plus a selective mechanism.",
                    "Distinguishes the main mechanism from exclusive causation.",
                ],
                "findings": findings,
                "recommendation": "major_revision",
                "confidence": 0.65,
            }
        return {
            "output": output,
            "usage": {"input": 100, "output": 50, "total": 150, "cost": 0},
            "provider": "fake",
            "prompt_sha256": "a" * 64,
            "context_sha256": "b" * 64,
        }
    return run


def test_popular_science_multi_model_demo(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    svc = ResearchService(workspace)
    sid = svc.call("init", {
        "question": "What does the book claim, and what is interpretation?",
        "roots": [{
            "root_id": "sources",
            "path": str(FIXTURE.parent),
            "files": {FIXTURE.name: "selected"},
        }],
    })["investigation_id"]

    ingested = svc.call("document_claim_ingest", {
        "session_id": sid,
        "root_id": "sources",
        "path": FIXTURE.name,
        "source_type": "book",
        "metadata": {
            "title": "On the Origin of Species (excerpt)",
            "authors": ["Charles Darwin"],
            "year": "1859",
            "publisher": "John Murray",
            "license": "Public domain",
        },
        "reviewer": "test",
    })
    assert ingested["holding"]["epistemic_role"] == "interpretation"
    assert ingested["holding"]["authority"] == "editorial"
    assert ingested["claims"]
    claim_id = ingested["claims"][0]["id"]

    plan = svc.call("review_plan_create", {
        "session_id": sid,
        "question": "Is the central claim supported?",
        "kind": "peer_review",
        "roles": ["domain"],
        "models": ["fake-a", "fake-b"],
        "min_reviewers": 2,
        "target_id": ingested["source"]["id"],
    })["plan"]
    packet = svc.call("paper_review_packet", {
        "session_id": sid,
        "source_id": ingested["source"]["id"],
        "plan_id": plan["id"],
    })
    graph = ResearchGraph.load(sid, root=workspace)
    bundle_dir = tmp_path / "bundle"
    result = run_paper_review(
        plan=plan,
        packet=packet,
        model_runner=_fake_runner(claim_id),
        review_roles=["domain"],
        argument_roles=["advocate", "critic"],
        models=["fake-a", "fake-b"],
        claim_ids=[claim_id],
        graph=graph,
        record=True,
        max_workers=2,
        bundle_dir=bundle_dir,
    )
    assert result["review"]["count"] == 2
    assert len(result["arguments"]) == 4
    assert result["reconciliation"]["reconciliation"]["type"] == "Reconciliation"
    assert result["reconciliation"]["consensus"]
    assert result["reconciliation"]["dissent"]
    assert result["errors"] == []
    assert result["truth_assessed"] is False

    for name in ("review.md", "review.json", "claims.csv", "evidence.csv",
                 "references.bib", "arguments.json", "checks.json"):
        assert (bundle_dir / name).is_file()
    report = json.loads((bundle_dir / "review.json").read_text(encoding="utf-8"))
    assert report["claims"]
    assert report["arguments"]
    assert report["strengths"]
    assert report["sections"]["dissent"]
    assert "Steelman" in (bundle_dir / "review.md").read_text(encoding="utf-8") or \
        "steelman" in (bundle_dir / "review.md").read_text(encoding="utf-8")
