"""The paper review driver (ozagia.harness.paper_review).

No live model or optional Stage A report module is used. A fake runner answers
review-shaped and argument-shaped prompts, and a temporary ResearchGraph holds
the recorded runs, arguments and reconciliation.
"""
from __future__ import annotations

import re
import threading
from typing import Any

import pytest

from ozagia.harness.paper_review import (
    DEFAULT_ARGUMENT_ROLES,
    PaperReviewDriverError,
    run_paper_review,
)
from ozagia.research import structured_review as sr
from ozagia.research.graph import ResearchGraph

REVIEW_ROLE_RE = re.compile(r"^Review role: (.+)$", re.MULTILINE)
ARGUMENT_ROLE_RE = re.compile(r"^Argument role: (.+)$", re.MULTILINE)
MODE_RE = re.compile(r"^Mode: (.+)$", re.MULTILINE)
CLAIM_RE = re.compile(r"^Claim id: (.+)$", re.MULTILINE)


def _claim(graph: ResearchGraph, wording: str, **fields: Any) -> dict[str, Any]:
    values = {
        "claim_type": "hypothesis",
        "status": "unresolved",
        "scope": "the tested corpus",
        "author": "reader",
    }
    values.update(fields)
    return graph.record("Claim", wording=wording, **values)


def _setup(tmp_path: Any, *, investigation_id: str = "paper-review"):
    graph = ResearchGraph.create(
        "Paper review driver",
        root=tmp_path,
        investigation_id=investigation_id,
    )
    source = graph.record(
        "Source",
        source_type="paper",
        title="A test paper",
        locator={"path": "paper.tex", "start_line": 1},
    )
    claim = _claim(
        graph,
        "The method improves review coverage.",
        source_locator={"path": "paper.tex", "start_line": 10},
    )
    graph.link(claim["id"], "derived_from", source["id"])
    plan = sr.create_review_plan(
        graph,
        question="Does the method improve review coverage?",
        kind="peer_review",
        roles=["methods", "statistics"],
        models=["model-a"],
        target_id=claim["id"],
        min_reviewers=1,
    )["plan"]
    packet = {
        "plan_id": plan["id"],
        "question": plan["question"],
        "claims": [{"id": claim["id"], "wording": claim["wording"]}],
        "packet_sha256": "a" * 64,
    }
    return graph, plan, claim, packet


class FakeRunner:
    """Answer review prompts and argument prompts from their own schemas."""

    def __init__(self, claim_id: str) -> None:
        self.claim_id = claim_id
        self.calls: list[dict[str, Any]] = []
        self.lock = threading.Lock()

    def __call__(
        self,
        model: str,
        messages: list[dict[str, str]],
        *,
        response_format: Any = None,
    ) -> dict[str, Any]:
        system = messages[0]["content"]
        user = messages[1]["content"]
        argument = ARGUMENT_ROLE_RE.search(user)
        if argument is None:
            role = REVIEW_ROLE_RE.search(user)
            assert role is not None
            role_name = role.group(1).strip()
            with self.lock:
                self.calls.append({
                    "kind": "review",
                    "role": role_name,
                    "model": model,
                    "messages": messages,
                })
            output: dict[str, Any] = {
                "summary": f"review summary {role_name}/{model}",
                "findings": [{
                    "wording": f"{role_name} review finding.",
                    "severity": "minor",
                    "issue_type": "scope",
                    "claim_id": self.claim_id,
                }],
            }
        else:
            role_name = argument.group(1).strip()
            mode = MODE_RE.search(user)
            assert mode is not None
            claim = CLAIM_RE.search(user)
            assert claim is not None
            claim_id = claim.group(1).strip()
            assert "You are working independently" in system
            with self.lock:
                self.calls.append({
                    "kind": "argument",
                    "role": role_name,
                    "mode": mode.group(1).strip(),
                    "claim_id": claim_id,
                    "model": model,
                    "messages": messages,
                })
            output = {
                "thesis": (
                    f"{role_name}/{model}/{claim_id} thesis"
                ),
                "premises": [{
                    "wording": "The trial improved coverage.",
                    "inference": "inductive",
                    "claim_ids": [claim_id],
                }],
                "assumptions": ["The corpus is representative."],
                "objections": [{
                    "wording": "A replication could fail.",
                    "status": "open",
                }],
                "falsifiers": ["A replication finds no improvement."],
                "confidence": 0.6,
                "notes": "candidate only",
            }
        return {
            "output": output,
            "usage": {"prompt_tokens": 10, "completion_tokens": 5,
                      "total_tokens": 15},
            "provider": "fake-provider",
        }


def _argument_calls(runner: FakeRunner) -> list[dict[str, Any]]:
    return [call for call in runner.calls if call["kind"] == "argument"]


def test_review_and_argument_matrices_are_independent_and_recorded(tmp_path):
    graph, plan, claim, packet = _setup(tmp_path)
    runner = FakeRunner(claim["id"])

    result = run_paper_review(
        plan=plan,
        packet=packet,
        model_runner=runner,
        graph=graph,
        record=True,
        max_workers=2,
    )

    assert result["errors"] == []
    assert result["truth_assessed"] is False
    assert result["review"]["count"] == 2
    assert [(run["role"], run["model"]) for run in result["review"]["runs"]] == [
        ("methods", "model-a"),
        ("statistics", "model-a"),
    ]
    assert len(result["arguments"]) == len(DEFAULT_ARGUMENT_ROLES)
    assert [entry["role"] for entry in result["arguments"]] == (
        DEFAULT_ARGUMENT_ROLES)
    assert [entry["mode"] for entry in result["arguments"]] == [
        "steelman", "red_team", "synthesis"]
    assert result["count"] == 5
    assert result["report"] is None and result["bundle"] is None

    argument_calls = _argument_calls(runner)
    assert {(call["role"], call["mode"]) for call in argument_calls} == {
        ("advocate", "steelman"),
        ("critic", "red_team"),
        ("synthesizer", "synthesis"),
    }
    review_calls = [call for call in runner.calls if call["kind"] == "review"]
    review_markers = {
        call["messages"][1]["content"].split("Review packet", 1)[0]
        for call in review_calls
    }
    # Every argument run gets its own complete prompt; no review output or
    # another argument's thesis is echoed into it.
    for call in argument_calls:
        prompt = "\n".join(message["content"] for message in call["messages"])
        assert f"Claim id: {claim['id']}" in prompt
        assert "The method improves review coverage." in prompt
        for marker in review_markers:
            assert marker not in prompt
        for other in argument_calls:
            if other is call:
                continue
            other_thesis = next(
                entry["output"]["thesis"] for entry in result["arguments"]
                if entry["role"] == other["role"]
            )
            assert other_thesis not in prompt
    for call in review_calls:
        prompt = "\n".join(message["content"] for message in call["messages"])
        for entry in result["arguments"]:
            assert entry["output"]["thesis"] not in prompt

    state = graph.state()
    nodes = state["nodes"]
    runs = [node for node in nodes if node["type"] == "ReviewRun"]
    arguments = [node for node in nodes if node["type"] == "Argument"]
    reconciliations = [
        node for node in nodes if node["type"] == "Reconciliation"]
    assert len(runs) == 2
    assert len(arguments) == 3
    assert len(reconciliations) == 1
    assert result["reconciliation"]["reconciliation"]["id"] == (
        reconciliations[0]["id"])
    assert result["reconciliation"]["reconciliation"]["method"] == "consensus"
    assert result["reconciliation"]["truth_assessed"] is False

    edges = state["edges"]
    for entry in result["arguments"]:
        recorded = entry["recorded"]
        assert recorded is not None
        node = recorded["argument"]
        assert node["reviewer"] == f"{entry['role']}:{entry['model']}"
        assert node["mode"] == entry["mode"]
        assert node["truth_assessed"] is False
        assert node["model_runs"] == [{
            "model": entry["model"],
            "provider": "fake-provider",
            "prompt_sha256": entry["prompt_sha256"],
            "context_sha256": entry["context_sha256"],
            "output_sha256": entry["output_sha256"],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5,
                      "total_tokens": 15},
        }]
        assert any(
            edge["type"] == "argues_for"
            and edge["src"] == node["id"]
            and edge["tgt"] == claim["id"]
            for edge in edges
        )


def test_invalid_argument_output_is_reported_and_not_recorded(tmp_path):
    graph, plan, claim, packet = _setup(tmp_path, investigation_id="invalid")

    class InvalidRunner(FakeRunner):
        def __call__(self, model, messages, *, response_format=None):
            user = messages[1]["content"]
            if ARGUMENT_ROLE_RE.search(user):
                with self.lock:
                    self.calls.append({
                        "kind": "argument",
                        "role": "advocate",
                        "model": model,
                        "messages": messages,
                    })
                return {
                    "output": {
                        "thesis": "A thesis with no premises.",
                        "premises": [],
                        "assumptions": [],
                        "objections": [],
                        "falsifiers": [],
                        "confidence": 0.5,
                    },
                    "provider": "fake-provider",
                }
            return super().__call__(
                model, messages, response_format=response_format)

    result = run_paper_review(
        plan=plan,
        packet=packet,
        model_runner=InvalidRunner(claim["id"]),
        review_roles=["methods"],
        argument_roles=["advocate"],
        graph=graph,
        record=True,
    )

    assert result["arguments"] == []
    assert result["count"] == 1
    validation_errors = [
        error for error in result["errors"]
        if error["stage"] == "argument_validation"]
    assert len(validation_errors) == 1
    error = validation_errors[0]
    assert error["claim_id"] == claim["id"]
    assert error["role"] == "advocate"
    assert error["output"]["premises"] == []
    assert "premises" in error["error"]
    assert result["reconciliation"] is not None

    state = graph.state()
    assert [node for node in state["nodes"] if node["type"] == "Argument"] == []
    assert len([node for node in state["nodes"]
                if node["type"] == "ReviewRun"]) == 1


def test_record_false_returns_raw_argument_candidates_without_writing(tmp_path):
    graph, plan, claim, packet = _setup(tmp_path, investigation_id="no-record")
    # Claims may be supplied without an explicit claim_ids list.
    packet.pop("claim_ids", None)
    runner = FakeRunner(claim["id"])
    before = graph.state()

    result = run_paper_review(
        plan=plan,
        packet=packet,
        model_runner=runner,
        review_roles=["methods"],
        argument_roles=["critic"],
        graph=graph,
        record=False,
    )

    assert result["errors"] == []
    assert result["count"] == 2
    assert len(result["arguments"]) == 1
    entry = result["arguments"][0]
    assert entry["role"] == "critic"
    assert entry["mode"] == "red_team"
    assert entry["claim_id"] == claim["id"]
    assert entry["recorded"] is None
    assert entry["output"]["thesis"] == (
        f"critic/model-a/{claim['id']} thesis")
    assert len(entry["output_sha256"]) == 64
    assert result["reconciliation"] is None
    assert [run["role"] for run in result["review"]["runs"]] == ["methods"]

    after = graph.state()
    assert len(after["nodes"]) == len(before["nodes"])
    assert [node["id"] for node in after["nodes"]] == [
        node["id"] for node in before["nodes"]]
    assert len(after["edges"]) == len(before["edges"])
    assert not any(
        node["type"] in {"ReviewRun", "Argument", "Reconciliation"}
        for node in after["nodes"]
    )


def test_bundle_dir_calls_injected_report_and_bundle_builders(tmp_path):
    graph, plan, claim, packet = _setup(tmp_path, investigation_id="bundle")
    runner = FakeRunner(claim["id"])
    bundle_dir = tmp_path / "bundle"
    seen: dict[str, Any] = {}

    def report_builder(plan: dict[str, Any], *, plan_id: str | None = None,
                       state: Any = None) -> dict[str, Any]:
        seen["report"] = {"plan": plan, "plan_id": plan_id, "state": state}
        return {"markdown": "# Paper review", "plan_id": plan_id}

    def bundle_writer(graph: Any, *, plan: dict[str, Any],
                      plan_id: str | None = None,
                      output_dir: Any = None) -> dict[str, Any]:
        seen["bundle"] = {
            "graph": graph,
            "plan": plan,
            "plan_id": plan_id,
            "output_dir": output_dir,
        }
        return {"files": ["review.md"], "output_dir": output_dir}

    result = run_paper_review(
        plan=plan,
        packet=packet,
        model_runner=runner,
        review_roles=["methods"],
        argument_roles=[],
        graph=graph,
        record=True,
        bundle_dir=bundle_dir,
        report_builder=report_builder,
        bundle_writer=bundle_writer,
    )

    assert result["errors"] == []
    assert result["report"] == {"markdown": "# Paper review",
                                "plan_id": plan["id"]}
    assert result["bundle"]["files"] == ["review.md"]
    assert result["bundle"]["output_dir"] == bundle_dir
    assert seen["report"]["plan"] is plan
    assert seen["report"]["plan_id"] == plan["id"]
    assert seen["report"]["state"] == graph.state()
    assert seen["bundle"]["graph"] is graph
    assert seen["bundle"]["plan"] is plan
    assert seen["bundle"]["plan_id"] == plan["id"]
    assert seen["bundle"]["output_dir"] == bundle_dir
    assert result["arguments"] == []


def test_claim_ids_are_capped_at_twenty(tmp_path):
    graph = ResearchGraph.create(
        "Paper review cap",
        root=tmp_path,
        investigation_id="claim-cap",
    )
    claims = [
        _claim(graph, f"Claim number {index}.")
        for index in range(25)
    ]
    plan = sr.create_review_plan(
        graph,
        question="How many claims are reviewed?",
        kind="peer_review",
        roles=["methods"],
        models=["model-a"],
        target_id=claims[0]["id"],
        min_reviewers=1,
    )["plan"]
    packet = {
        "plan_id": plan["id"],
        "claim_ids": [claim["id"] for claim in claims],
        "packet_sha256": "b" * 64,
    }
    runner = FakeRunner(claims[0]["id"])

    result = run_paper_review(
        plan=plan,
        packet=packet,
        model_runner=runner,
        review_roles=["methods"],
        argument_roles=["advocate"],
        models=["model-a"],
        graph=graph,
        record=True,
    )

    assert result["errors"] == []
    assert len(result["arguments"]) == 20
    assert [entry["claim_id"] for entry in result["arguments"]] == [
        claim["id"] for claim in claims[:20]]
    assert result["count"] == 21
    assert len([node for node in graph.state()["nodes"]
                if node["type"] == "Argument"]) == 20


def test_invalid_driver_configuration_raises_driver_error(tmp_path):
    _graph, plan, _claim, packet = _setup(tmp_path, investigation_id="config")

    with pytest.raises(PaperReviewDriverError, match="model_runner"):
        run_paper_review(
            plan=plan, packet=packet, model_runner=None)  # type: ignore[arg-type]
    with pytest.raises(PaperReviewDriverError, match="max_workers"):
        run_paper_review(
            plan=plan, packet=packet, model_runner=FakeRunner("claim-0001"),
            max_workers=0)
    with pytest.raises(PaperReviewDriverError, match="argument role"):
        run_paper_review(
            plan=plan, packet=packet, model_runner=FakeRunner("claim-0001"),
            argument_roles=["mystery"])
    with pytest.raises(PaperReviewDriverError, match="plan id"):
        run_paper_review(
            plan={"roles": ["methods"], "models": ["model-a"]},
            packet=packet,
            model_runner=FakeRunner("claim-0001"),
            graph=object(),
            record=True,
        )
