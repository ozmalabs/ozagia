"""Offline fixture evaluation for source-only Terraform/Kubernetes retrieval."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from ozagia.core.infra_query import context
from ozagia.core.memory import DevMemory, build_index
from ozagia.eval.iac import score_question


FIXTURE = Path(__file__).parent / "fixtures" / "iac_repo"
QUESTIONS = Path(__file__).parent / "fixtures" / "iac_eval" / "retrieval_questions.json"
SECRET_CANARY = "BASE64_SECRET_CANARY_63819"


def _tracked_fixture(root: Path) -> None:
    shutil.copytree(FIXTURE, root, dirs_exist_ok=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run([
        "git", "-C", str(root), "-c", "user.name=Fixture",
        "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fixture",
    ], check=True)


def test_iac_fixture_source_and_edge_recall_budget_and_secret_safety(tmp_path, monkeypatch):
    root = tmp_path / "iac-repo"
    _tracked_fixture(root)
    monkeypatch.delenv("OZAGIA_CONFIG", raising=False)
    dump = tmp_path / "graph.json"
    build_index(root, dump, include_docs="none", include_git=False)
    memory = DevMemory(dump, tmp_path / "journal.jsonl", root=root)

    # Expected fixture questions and answer locations. No model or remote tool
    # participates; the source and edge sets are the frozen expected answers.
    terraform = memory.adapter_query("infra.locate", query="aws_subnet.private")
    subnet = next(row for row in terraform["results"] if row.get("name") == "aws_subnet.private")
    dependencies = memory.adapter_query("infra.dependencies", key=subnet["key"], depth=1)
    terraform_sources = {subnet["path"]} | {row["path"] for row in dependencies["nodes"]}
    expected_tf_sources = {"infra/subnet.tf", "infra/network.tf"}
    tf_source_recall = len(terraform_sources & expected_tf_sources) / len(expected_tf_sources)
    assert tf_source_recall == 1.0
    assert any(edge["relation"] == "infra_references" for edge in dependencies["edges"])
    assert any(edge.get("evidence") == "aws_vpc.payments.id"
               for edge in dependencies["edges"])

    kube = memory.adapter_query("infra.locate", query="payments-api")
    workload = next(row for row in kube["results"] if row.get("kind") == "Deployment")
    kube_edges = memory.adapter_query("infra.dependencies", key=workload["key"], depth=1)
    kube_sources = {workload["path"]} | {row["path"] for row in kube_edges["nodes"]}
    expected_kube_sources = {
        "k8s/deployment.yaml", "k8s/service.yaml", "k8s/service-account.yaml",
    }
    kube_source_recall = len(kube_sources & expected_kube_sources) / len(expected_kube_sources)
    relations = {edge["relation"] for edge in kube_edges["edges"]}
    expected_relations = {"infra_workload_service_selector", "infra_workload_service_account"}
    edge_recall = len(relations & expected_relations) / len(expected_relations)
    assert kube_source_recall == 1.0
    assert edge_recall == 1.0

    context = memory.adapter_query(
        "infra.context", query="payments-api service account selector", max_chars=240,
    )
    assert len(context["context"]) <= 240
    assert "deployment.yaml" in context["context"]
    assert SECRET_CANARY not in dump.read_text(encoding="utf-8")
    assert SECRET_CANARY not in context["context"]

    print({
        "terraform_source_recall": tf_source_recall,
        "kubernetes_source_recall": kube_source_recall,
        "kubernetes_edge_recall": edge_recall,
        "context_chars": len(context["context"]),
        "context_budget_chars": 240,
        "secret_canary_leaked": False,
    })


def _locations(rows):
    return [{"path": row.get("path", ""), "line": row.get("line"),
             "pointer": row.get("pointer")} for row in rows]


def test_retrieval_questions_score_actual_context_at_two_budgets(tmp_path, monkeypatch):
    root = tmp_path / "iac-repo"
    _tracked_fixture(root)
    monkeypatch.delenv("OZAGIA_CONFIG", raising=False)
    dump = tmp_path / "graph.json"
    build_index(root, dump, include_docs="none", include_git=False)
    memory = DevMemory(dump, tmp_path / "journal.jsonl", root=root)
    questions = json.loads(QUESTIONS.read_text(encoding="utf-8"))["questions"]
    report = []
    for budget in (360, 600):
        for question in questions:
            answer = context(memory, question["question"],
                             history=question.get("history", ()),
                             max_chars=budget)
            edges = [{
                "source": edge["source_label"],
                "target": edge["target_label"],
                "kind": edge["relation"],
                "evidence": edge["evidence"],
            } for edge in answer["edges"]]
            scored = score_question(question, {
                "sources": _locations(answer["results"] + answer["related"]),
                "edges": edges,
                "text": answer["context"],
            })
            assert len(answer["context"]) <= budget
            assert scored["context_chars"] <= budget
            assert answer["estimated_tokens"] <= budget // 4 + 1
            assert SECRET_CANARY not in answer["context"]
            assert SECRET_CANARY not in json.dumps(answer)
            report.append({"id": question["id"], "budget": budget,
                           "sources": scored["sources"],
                           "edges": scored["edges"],
                           "chars": scored["context_chars"]})
    for row in report:
        assert row["sources"]["recall"] == 1.0, row
        assert row["sources"]["precision"] == 1.0, row
        if row["id"] != "history-follow-up":
            assert row["edges"]["recall"] == 1.0, row
    unrelated = context(memory, "unrelated invoice worker", max_chars=600)
    assert unrelated["context"] == ""
    assert unrelated["reason"] == "no matching infrastructure declarations"
    print(report)
