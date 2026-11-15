"""The multi-model review driver (ozagia.harness.review).

No model or network endpoint is called. A fake runner, validator and recorder
cover the matrix; a fake ``http_json`` covers the OpenAI-compatible runner.
"""
from __future__ import annotations

import json
import re
import threading
import time
from typing import Any

import pytest

from ozagia.harness import review

PACKET: dict[str, Any] = {
    "plan_id": "reviewplan-0001",
    "question": "Does X cause Y?",
    "claims": [{"id": "claim-0001", "wording": "X causes Y."}],
    "sha256": "a" * 64,
}
PLAN: dict[str, Any] = {
    "id": "reviewplan-0001",
    "roles": ["methods", "statistics"],
    "models": ["model-a", "model-b"],
    "params": {"temperature": 0.0},
}
EXPECTED_ORDER = [
    ("methods", "model-a"),
    ("methods", "model-b"),
    ("statistics", "model-a"),
    ("statistics", "model-b"),
]


def _role(messages: list[dict[str, str]]) -> str:
    match = re.search(r"^Review role: (.+)$", messages[1]["content"],
                      re.MULTILINE)
    assert match is not None
    return match.group(1).strip()


def _finding() -> dict[str, Any]:
    return {
        "wording": "The claim states a causal direction without a scope.",
        "severity": "major",
        "issue_type": "scope",
        "claim_id": "claim-0001",
        "quote": "X causes Y.",
        "recommendation": "Narrow the claim.",
        "confidence": 0.6,
    }


class FakeRunner:
    """A model_runner that answers from the role in the prompt.

    The first role/model combination is deliberately slow, so completion order
    differs from the role/model order the matrix must return.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.lock = threading.Lock()

    def __call__(self, model: str, messages: list[dict[str, str]],
                *, response_format: Any = None) -> dict[str, Any]:
        role = _role(messages)
        with self.lock:
            self.calls.append({"role": role, "model": model,
                               "messages": messages,
                               "response_format": response_format})
        if (role, model) == ("methods", "model-a"):
            time.sleep(0.05)
        output = {"summary": f"{role}/{model}",
                  "findings": [_finding()]}
        if model == "model-a":
            return {
                "output": output,
                "usage": {"prompt_tokens": 100, "completion_tokens": 20,
                          "total_tokens": 120, "cost": 0.002},
                "provider": "fake-provider",
                "prompt_sha256": "p" * 64,
                "context_sha256": "c" * 64,
                "params": {"temperature": 0.2},
            }
        return output


def _valid(output: Any, *, state: Any = None, packet: Any = None) -> dict[str, Any]:
    return {"valid": True, "errors": []}


class _Graph:
    def __init__(self) -> None:
        self.nodes: list[dict[str, Any]] = []

    def state(self) -> dict[str, Any]:
        return {"nodes": self.nodes, "edges": []}


# --------------------------------------------------------------------------- #
# matrix: ordering, independence, normalization
# --------------------------------------------------------------------------- #
def test_two_roles_two_models_are_ordered_and_independent():
    runner = FakeRunner()
    result = review.run_review_matrix(
        PLAN, PACKET, model_runner=runner, max_workers=2, validator=_valid)

    assert result["count"] == 4 and result["failed"] == 0
    assert result["errors"] == []
    assert result["round"] == "independent"
    assert result["truth_assessed"] is False
    assert [(run["role"], run["model"]) for run in result["runs"]] == EXPECTED_ORDER
    assert [run["output"]["summary"] for run in result["runs"]] == [
        f"{role}/{model}" for role, model in EXPECTED_ORDER]
    assert all(run["validation"] == {"valid": True, "errors": []}
               for run in result["runs"])
    assert all(run["packet_sha256"] == PACKET["sha256"]
               for run in result["runs"])
    assert all("recorded" not in run for run in result["runs"])

    calls = {(call["role"], call["model"]): call for call in runner.calls}
    assert set(calls) == set(EXPECTED_ORDER)
    for role, model in EXPECTED_ORDER:
        messages = calls[(role, model)]["messages"]
        assert [message["role"] for message in messages] == ["system", "user"]
        assert calls[(role, model)]["response_format"] == {"type": "json_object"}
        assert f"Review role: {role}" in messages[1]["content"]
        assert "Round: independent" in messages[1]["content"]
        assert json.dumps(PACKET, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":")) in messages[1]["content"]
        assert "strict schema" in messages[1]["content"]
        # Independent: no role/model output (own or another reviewer's) is
        # echoed into a later prompt.
        for marker in (f"{other_role}/{other_model}"
                       for other_role, other_model in EXPECTED_ORDER):
            assert marker not in messages[1]["content"]

    methods_system = calls[("methods", "model-a")]["messages"][0]["content"]
    stats_system = calls[("statistics", "model-a")]["messages"][0]["content"]
    assert "methods reviewer" in methods_system
    assert "statistics reviewer" in stats_system
    assert "'methods'" in methods_system and "'independent'" in methods_system

    # A wrapper return keeps provider/usage/hashes and its own params.
    wrapper = result["runs"][0]
    assert wrapper["provider"] == "fake-provider"
    assert wrapper["usage"] == {"prompt_tokens": 100, "completion_tokens": 20,
                                "total_tokens": 120, "cost": 0.002}
    assert wrapper["prompt_sha256"] == "p" * 64
    assert wrapper["context_sha256"] == "c" * 64
    assert wrapper["params"] == {"temperature": 0.2}

    # A bare output return is normalized and hashes are computed from the
    # exact messages that were sent.
    bare = result["runs"][1]
    assert bare["output"] == {"summary": "methods/model-b",
                              "findings": [_finding()]}
    assert bare["usage"] == {} and bare["provider"] is None
    assert bare["prompt_sha256"] == review.messages_sha256(
        calls[("methods", "model-b")]["messages"])
    assert bare["context_sha256"] == review.context_sha256(
        calls[("methods", "model-b")]["messages"])
    assert len(bare["prompt_sha256"]) == 64
    assert len(bare["context_sha256"]) == 64
    assert bare["params"] == {"temperature": 0.0}


def test_unknown_role_and_csv_arguments_get_prompts():
    runner = FakeRunner()
    result = review.run_review_matrix(
        PLAN, PACKET, model_runner=runner, roles="critic",
        models="model-a, model-a", validator=_valid)
    assert result["count"] == 1
    call = runner.calls[0]
    system = call["messages"][0]["content"]
    user = call["messages"][1]["content"]
    assert "critic (red-team) reviewer" in system
    assert "Review role: critic" in user

    runner = FakeRunner()
    result = review.run_review_matrix(
        PLAN, PACKET, model_runner=runner, roles=["mystery"],
        models=["model-a"], validator=_valid)
    assert result["count"] == 1
    system = runner.calls[0]["messages"][0]["content"]
    assert review.GENERIC_REVIEWER_PROMPT in system
    assert "'mystery'" in system


def test_packet_hash_field_wins_and_is_stable_when_absent():
    runner = FakeRunner()
    result = review.run_review_matrix(
        PLAN, PACKET, model_runner=runner, roles=["methods"],
        models=["model-a"], validator=_valid)
    assert result["runs"][0]["packet_sha256"] == "a" * 64

    packet = {key: value for key, value in PACKET.items() if key != "sha256"}
    first = review.run_review_matrix(
        PLAN, packet, model_runner=runner, roles=["methods"],
        models=["model-a"], validator=_valid)
    second = review.run_review_matrix(
        PLAN, packet, model_runner=runner, roles=["methods"],
        models=["model-a"], validator=_valid)
    assert first["runs"][0]["packet_sha256"] == second["runs"][0]["packet_sha256"]
    assert len(first["runs"][0]["packet_sha256"]) == 64


# --------------------------------------------------------------------------- #
# matrix: validation, recording, errors
# --------------------------------------------------------------------------- #
def test_invalid_output_is_an_error_and_is_not_recorded():
    seen: list[tuple[Any, Any]] = []

    def validator(output: dict[str, Any], *, state: Any = None,
                  packet: Any = None) -> dict[str, Any]:
        seen.append((state, packet))
        good = output["summary"] != "statistics/model-b"
        return {"valid": good, "errors": [] if good else ["not credible"]}

    records: list[tuple[Any, dict[str, Any]]] = []

    def recorder(graph: Any, **kwargs: Any) -> dict[str, Any]:
        records.append((graph, kwargs))
        index = len(records)
        return {"run": {"id": f"reviewrun-{index:04d}", "type": "ReviewRun"},
                "findings": [{"id": f"reviewfinding-{index:04d}",
                              "type": "ReviewFinding"}]}

    graph = _Graph()
    result = review.run_review_matrix(
        PLAN, PACKET, model_runner=FakeRunner(), validator=validator,
        graph=graph, record=True, recorder=recorder)

    assert result["count"] == 3 and result["failed"] == 1
    assert len(records) == 3
    assert [kwargs["reviewer_id"] for _, kwargs in records] == [
        "methods:model-a", "methods:model-b", "statistics:model-a"]
    assert all(record_graph is graph for record_graph, _ in records)
    error = result["errors"][0]
    assert (error["role"], error["model"]) == ("statistics", "model-b")
    assert error["stage"] == "validation"
    assert error["validation"] == {"valid": False, "errors": ["not credible"]}
    assert error["output"]["summary"] == "statistics/model-b"
    assert seen and all(state == {"nodes": [], "edges": []} for state, _ in seen)
    assert all(packet is PACKET for _, packet in seen)

    wrapper = records[0][1]
    assert wrapper["plan_id"] == "reviewplan-0001"
    assert wrapper["role"] == "methods" and wrapper["model"] == "model-a"
    assert wrapper["round"] == "independent"
    assert wrapper["packet_sha256"] == PACKET["sha256"]
    assert wrapper["output"]["summary"] == "methods/model-a"
    assert wrapper["provider"] == "fake-provider"
    assert wrapper["prompt_sha256"] == "p" * 64
    assert wrapper["context_sha256"] == "c" * 64
    assert wrapper["usage"]["prompt_tokens"] == 100
    assert wrapper["tokens"] == {"prompt_tokens": 100,
                                 "completion_tokens": 20,
                                 "total_tokens": 120}
    assert wrapper["cost"] == pytest.approx(0.002)
    assert wrapper["params"] == {"temperature": 0.2}

    bare = records[1][1]
    assert bare["usage"] == {} and bare["tokens"] == {}
    assert bare["cost"] is None
    assert bare["params"] == {"temperature": 0.0}
    assert len(bare["prompt_sha256"]) == 64

    for run in result["runs"]:
        assert run["recorded"]["run"]["type"] == "ReviewRun"
        assert run["recorded"]["findings"][0]["type"] == "ReviewFinding"


def test_recorder_with_an_explicit_signature_gets_alias_fields():
    received: list[dict[str, Any]] = []

    def recorder(graph: Any, *, plan_id: str, reviewer_id: str, role: str,
                 model: str, packet_sha256: str, output: dict[str, Any],
                 provider: str | None = None, prompt_sha256: str | None = None,
                 context_sha256: str | None = None,
                 usage: dict[str, Any] | None = None,
                 tokens: dict[str, Any] | None = None, cost: float | None = None,
                 params: dict[str, Any] | None = None,
                 round: str | None = None) -> dict[str, Any]:
        received.append(dict(
            plan_id=plan_id, reviewer_id=reviewer_id, role=role, model=model,
            packet_sha256=packet_sha256, output=output, provider=provider,
            prompt_sha256=prompt_sha256, context_sha256=context_sha256,
            usage=usage, tokens=tokens, cost=cost, params=params, round=round))
        return {"run": {"id": "reviewrun-0001"}, "findings": []}

    result = review.run_review_matrix(
        PLAN, PACKET, model_runner=FakeRunner(), roles=["methods"],
        models=["model-a"], validator=_valid, graph=_Graph(), record=True,
        recorder=recorder)
    assert result["count"] == 1
    record = received[0]
    assert record["reviewer_id"] == "methods:model-a"
    assert record["cost"] == pytest.approx(0.002)
    assert record["tokens"]["prompt_tokens"] == 100
    assert record["usage"]["completion_tokens"] == 20
    assert record["params"] == {"temperature": 0.2}
    assert result["runs"][0]["recorded"] == {
        "run": {"id": "reviewrun-0001"}, "findings": []}


def test_default_validator_accepts_the_advertised_output_schema():
    pytest.importorskip("ozagia.research.structured_review")
    state = {
        "nodes": [
            {"id": "claim-0001", "type": "Claim", "wording": "X causes Y."},
            {"id": "reference-0001", "type": "Reference",
             "citation_key": "doe2024"},
        ],
        "edges": [],
    }
    packet = {
        "plan": {"id": "reviewplan-0001", "question": "Does X cause Y?"},
        "target": {"id": "claim-0001", "type": "Claim"},
        "gathered_text": "X causes Y.",
        "truth_assessed": False,
    }

    def runner(model: str, messages: list[dict[str, str]],
               *, response_format: Any = None) -> dict[str, Any]:
        return {"summary": "One issue.", "findings": [_finding()]}

    result = review.run_review_matrix(
        {"roles": ["methods"], "models": ["model-a"]}, packet,
        model_runner=runner, graph=state)
    assert result["count"] == 1 and result["failed"] == 0
    validation = result["runs"][0]["validation"]
    assert validation["valid"] is True
    assert validation["findings"][0]["severity"] == "major"
    assert validation["findings"][0]["claim_id"] == "claim-0001"


class _MemoryGraph:
    """The small graph surface structured_review.record_review_run uses."""

    def __init__(self, nodes: list[dict[str, Any]]) -> None:
        self.nodes = [dict(node) for node in nodes]
        self.edges: list[dict[str, Any]] = []

    def state(self) -> dict[str, Any]:
        return {"nodes": [dict(node) for node in self.nodes],
                "edges": [dict(edge) for edge in self.edges]}

    def record(self, node_type: str, **fields: Any) -> dict[str, Any]:
        ordinal = 1 + sum(node["type"] == node_type for node in self.nodes)
        node = {"id": f"{node_type.lower()}-{ordinal:04d}",
                "type": node_type, "created_at": "2026-01-01T00:00:00Z",
                **fields}
        self.nodes.append(node)
        return dict(node)

    def link(self, src: str, relation: str, tgt: str,
             **fields: Any) -> dict[str, Any]:
        edge = {"id": f"edge-{len(self.edges) + 1:06d}",
                "type": relation, "src": src, "tgt": tgt, **fields}
        self.edges.append(edge)
        return dict(edge)


def test_real_validator_and_recorder_round_trip():
    pytest.importorskip("ozagia.research.structured_review")
    plan = {
        "id": "reviewplan-0001", "type": "ReviewPlan",
        "question": "Does X cause Y?", "roles": ["methods"],
        "models": ["model-a"], "target_id": "claim-0001", "min_reviewers": 1,
    }
    claim = {"id": "claim-0001", "type": "Claim", "wording": "X causes Y.",
             "scope": "all", "claim_type": "hypothesis", "author": "tester",
             "status": "open"}
    packet = {
        "schema": "ozagia.review_packet.v1", "plan": plan, "target": claim,
        "target_id": "claim-0001", "target_type": "Claim",
        "gathered_text": "Does X cause Y?\nX causes Y.",
        "truth_assessed": False,
    }
    graph = _MemoryGraph([plan, claim])

    def runner(model: str, messages: list[dict[str, str]],
               *, response_format: Any = None) -> dict[str, Any]:
        return {
            "output": {"summary": "One scope issue.", "findings": [_finding()]},
            "usage": {"prompt_tokens": 10, "completion_tokens": 5,
                      "cost": 0.001},
            "provider": "fake-provider",
        }

    result = review.run_review_matrix(
        plan, packet, model_runner=runner, graph=graph, record=True)
    assert result["count"] == 1 and result["failed"] == 0
    recorded = result["runs"][0]["recorded"]
    run = recorded["run"]
    assert run["type"] == "ReviewRun"
    assert run["tokens"] == {"prompt_tokens": 10, "completion_tokens": 5,
                             "total_tokens": 15}
    assert run["cost"] == pytest.approx(0.001)
    assert run["provider"] == "fake-provider"
    assert recorded["findings"][0]["type"] == "ReviewFinding"
    assert any(edge["type"] == "implements" and edge["src"] == run["id"]
               for edge in graph.edges)
    assert any(edge["type"] == "finds" and edge["src"] == run["id"]
               for edge in graph.edges)


def test_record_false_never_calls_the_recorder():
    def recorder(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("recorder must not be called")

    result = review.run_review_matrix(
        PLAN, PACKET, model_runner=FakeRunner(), validator=_valid,
        graph=_Graph(), record=False, recorder=recorder)
    assert result["count"] == 4
    assert all("recorded" not in run for run in result["runs"])


def test_one_argument_validator_and_model_errors_are_reported():
    def validator(output: dict[str, Any]) -> bool:
        return True

    class BrokenRunner:
        def __call__(self, model: str, messages: list[dict[str, str]],
                    *, response_format: Any = None) -> dict[str, Any]:
            if model == "model-b":
                raise RuntimeError("endpoint exploded")
            return {"summary": "ok", "findings": []}

    result = review.run_review_matrix(
        PLAN, PACKET, model_runner=BrokenRunner(), validator=validator)
    assert result["count"] == 2 and result["failed"] == 2
    assert {error["stage"] for error in result["errors"]} == {"model"}
    assert {error["model"] for error in result["errors"]} == {"model-b"}
    assert "endpoint exploded" in result["errors"][0]["error"]

    def broken_validator(output: dict[str, Any]) -> dict[str, Any]:
        raise ValueError("validator exploded")

    result = review.run_review_matrix(
        PLAN, PACKET, model_runner=FakeRunner(), roles=["methods"],
        models=["model-a"], validator=broken_validator)
    assert result["count"] == 0 and result["failed"] == 1
    assert result["errors"][0]["stage"] == "validation"
    assert "validator failed" in result["errors"][0]["validation"]["errors"][0]


# --------------------------------------------------------------------------- #
# openai_runner: fake HTTP only
# --------------------------------------------------------------------------- #
def _completion(content: str, usage: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "id": "cmpl-review",
        "object": "chat.completion",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": content},
            "finish_reason": "stop",
        }],
        "usage": usage or {"prompt_tokens": 11, "completion_tokens": 4,
                           "total_tokens": 15},
    }


def test_openai_runner_parses_output_and_never_returns_the_key(
        monkeypatch, caplog):
    secret = "sk-review-SECRET-1234567890"
    monkeypatch.setenv("OZAGIA_REVIEW_TEST_KEY", secret)
    requests: list[dict[str, Any]] = []

    def fake_http(url: str, body: dict[str, Any],
                  headers: dict[str, str], timeout: float) -> dict[str, Any]:
        requests.append({"url": url, "body": body, "headers": headers,
                         "timeout": timeout})
        return _completion(json.dumps({"summary": "looks sound",
                                       "findings": []}))

    runner = review.openai_runner(
        base_url="https://review.invalid/v1", model="model-a",
        api_key_env="OZAGIA_REVIEW_TEST_KEY", http_json=fake_http,
        timeout=12.5, temperature=0.0)
    messages = [{"role": "system", "content": "system prompt"},
                {"role": "user", "content": "user prompt"}]
    result = runner("model-b", messages)

    assert set(result) == {"output", "usage", "provider", "prompt_sha256",
                           "context_sha256"}
    assert result["output"] == {"summary": "looks sound", "findings": []}
    assert result["usage"] == {"prompt_tokens": 11, "completion_tokens": 4,
                               "total_tokens": 15}
    assert result["provider"] == "openai-compatible"
    assert result["prompt_sha256"] == review.messages_sha256(messages)
    assert result["context_sha256"] == review.context_sha256(messages)
    assert len(result["prompt_sha256"]) == 64

    assert requests[0]["url"] == "https://review.invalid/v1/chat/completions"
    assert requests[0]["timeout"] == 12.5
    assert requests[0]["body"]["model"] == "model-b"
    assert requests[0]["body"]["messages"] == messages
    assert requests[0]["body"]["temperature"] == 0.0
    assert requests[0]["body"]["response_format"] == {"type": "json_object"}
    assert requests[0]["headers"]["Authorization"] == f"Bearer {secret}"
    assert secret not in json.dumps(requests[0]["body"])
    assert secret not in json.dumps(result)
    assert secret not in caplog.text
    assert all(secret not in json.dumps(message) for message in messages)


def test_openai_runner_reads_key_file_and_accepts_fenced_json(tmp_path):
    secret = "sk-file-SECRET-abcdef"
    key_file = tmp_path / "key.txt"
    key_file.write_text(secret + "\n", encoding="utf-8")
    requests: list[dict[str, Any]] = []

    def fake_http(url: str, body: dict[str, Any],
                  headers: dict[str, str], timeout: float) -> dict[str, Any]:
        requests.append({"url": url, "headers": headers})
        return _completion("```json\n{\"summary\": \"fenced\", "
                           "\"findings\": []}\n```")

    runner = review.openai_runner(
        base_url="https://review.invalid/v1/chat/completions",
        model="model-a", api_key_env=None, api_key_file=key_file,
        http_json=fake_http)
    result = runner("model-a", [{"role": "user", "content": "review"}])
    assert result["output"] == {"summary": "fenced", "findings": []}
    assert requests[0]["url"] == "https://review.invalid/v1/chat/completions"
    assert requests[0]["headers"]["Authorization"] == f"Bearer {secret}"


def test_openai_runner_retries_429_and_5xx_but_not_400(monkeypatch):
    calls: list[int] = []
    sleeps: list[float] = []

    def flaky(url: str, body: dict[str, Any], headers: dict[str, str],
              timeout: float) -> dict[str, Any]:
        calls.append(1)
        if len(calls) < 3:
            raise review.ReviewDriverError("HTTP 429: busy", status=429)
        return _completion(json.dumps({"summary": "ok", "findings": []}))

    monkeypatch.setattr(review, "RETRY_BACKOFF_S", 0.0)
    monkeypatch.setattr(review.time, "sleep", sleeps.append)
    runner = review.openai_runner(
        base_url="https://review.invalid/v1", model="model-a",
        api_key_env=None, http_json=flaky)
    assert runner("model-a", [{"role": "user", "content": "x"}])["output"][
        "summary"] == "ok"
    assert len(calls) == 3
    assert sleeps == [0.0, 0.0]

    calls.clear()

    def bad_request(url: str, body: dict[str, Any], headers: dict[str, str],
                    timeout: float) -> dict[str, Any]:
        calls.append(1)
        raise review.ReviewDriverError("HTTP 400: bad request", status=400)

    runner = review.openai_runner(
        base_url="https://review.invalid/v1", model="model-a",
        api_key_env=None, http_json=bad_request)
    with pytest.raises(review.ReviewDriverError, match="HTTP 400"):
        runner("model-a", [{"role": "user", "content": "x"}])
    assert len(calls) == 1


def test_openai_runner_drops_response_format_if_the_endpoint_rejects_it():
    bodies: list[dict[str, Any]] = []

    def fake_http(url: str, body: dict[str, Any], headers: dict[str, str],
                  timeout: float) -> dict[str, Any]:
        bodies.append(dict(body))
        if "response_format" in body:
            raise review.ReviewDriverError(
                "HTTP 400: response_format is unsupported", status=400)
        return _completion(json.dumps({"summary": "ok", "findings": []}))

    runner = review.openai_runner(
        base_url="https://review.invalid/v1", model="model-a",
        api_key_env=None, http_json=fake_http)
    result = runner("model-a", [{"role": "user", "content": "x"}])
    assert result["output"]["summary"] == "ok"
    assert "response_format" in bodies[0]
    assert "response_format" not in bodies[1]
    assert len(bodies) == 2


def test_cli_requires_live_confirmation(tmp_path, monkeypatch, capsys):
    plan = tmp_path / "plan.json"
    packet = tmp_path / "packet.json"
    plan.write_text("{}", encoding="utf-8")
    packet.write_text("{}", encoding="utf-8")
    monkeypatch.delenv("OZAGIA_EVAL_LIVE", raising=False)
    code = review.main([
        "--packet", str(packet), "--plan", str(plan),
        "--base-url", "https://review.invalid/v1",
    ])
    assert code == 2
    assert "OZAGIA_EVAL_LIVE" in capsys.readouterr().err


def test_openai_runner_rejects_non_json_content_without_echoing_it():
    def fake_http(url: str, body: dict[str, Any], headers: dict[str, str],
                  timeout: float) -> dict[str, Any]:
        return _completion("I cannot answer that.")

    runner = review.openai_runner(
        base_url="https://review.invalid/v1", model="model-a",
        api_key_env=None, http_json=fake_http)
    with pytest.raises(review.ReviewDriverError,
                       match="not a JSON object"):
        runner("model-a", [{"role": "user", "content": "x"}])
