from __future__ import annotations

import json
from pathlib import Path

from ozagia.eval.iac import evaluate, score_question


FIXTURE = Path(__file__).parent / "fixtures" / "iac_eval" / "questions.json"


def _questions() -> list[dict]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))["questions"]


def test_source_and_evidence_attributed_edge_precision_recall() -> None:
    question = _questions()[0]
    result = score_question(question, {
        "sources": ["infra/network/main.tf:18",
                    {"path": "infra/network/main.tf:4"},
                    "infra/unrelated.tf:2",
                    {"path": "infra/unsupported.tf:1", "attributed": False}],
        "edges": [
            {"source": "module.app.aws_subnet.private",
             "target": "module.network.aws_vpc.main", "kind": "depends_on",
             "evidence": "infra/network/main.tf:18"},
            {"source": "module.app.aws_subnet.private",
             "target": "module.other.aws_vpc.main", "kind": "depends_on",
             "evidence": "infra/network/main.tf:19"},
            {"source": "module.app.aws_subnet.private",
             "target": "module.network.aws_vpc.main", "kind": "depends_on"},
        ],
        "text": "subnet context",
    })
    assert result["sources"] == {
        "true_positive": 2, "predicted": 3, "expected": 2,
        "precision": 2 / 3, "recall": 1.0}
    assert result["edges"] == {
        "true_positive": 1, "predicted": 2, "expected": 1,
        "precision": 0.5, "recall": 1.0}
    assert result["context_chars"] == len("subnet context")
    assert result["estimated_tokens_4chars"] == 4


def test_empty_context_has_zero_recall_and_deterministic_question_order() -> None:
    questions = _questions()
    result = evaluate(questions, {})
    assert [q["question_id"] for q in result["questions"]] == [
        "terraform-network", "kubernetes-service"]
    assert result["questions"][0]["sources"]["recall"] == 0
    assert result["questions"][1]["edges"]["recall"] == 0
    assert result["summary"]["questions"] == 2
    assert result["summary"]["sources"] == {
        "true_positive": 0, "predicted": 0, "expected": 4,
        "precision": 0.0, "recall": 0.0}
    assert result["summary"]["edges"]["expected"] == 2
    assert result["summary"]["context_chars"]["max"] == 0


def test_summary_micro_averages_attributed_results() -> None:
    questions = _questions()
    first = score_question(questions[0], {
        "sources": ["infra/network/main.tf:18"],
        "edges": [],
        "text": "abc",
    })
    second = score_question(questions[1], {
        "sources": ["deploy/payments/workload.yaml:1"],
        "edges": [{"source": "Deployment/payments/api",
                   "target": "Service/payments/api", "kind": "selector_match",
                   "evidence": "deploy/payments/workload.yaml:1"}],
        "text": "abcdef",
    })
    from ozagia.eval.iac import _summary

    summary = _summary([first, second])
    assert summary["sources"]["true_positive"] == 2
    assert summary["sources"]["predicted"] == 2
    assert summary["sources"]["expected"] == 4
    assert summary["sources"]["precision"] == 1.0
    assert summary["sources"]["recall"] == 0.5
    assert summary["edges"]["true_positive"] == 1
    assert summary["edges"]["recall"] == 0.5
    assert summary["context_chars"] == {
        "total": 9, "min": 3, "max": 6, "mean": 4.5}


def test_path_traversal_locations_are_not_credited() -> None:
    question = {"id": "safe", "expected_sources": ["good/file.tf"]}
    result = score_question(question, {
        "sources": ["../good/file.tf", "/good/file.tf"]})
    assert result["sources"]["predicted"] == 0
