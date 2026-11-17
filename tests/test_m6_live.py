"""Offline tests for the M6 local-Qwen map-arm study.

No test starts a model, network request, MCP server, daemon or checkout.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
M6 = REPO / "evaluation" / "m6_live"
sys.path.insert(0, str(M6))

import m6_live_lib as L  # noqa: E402
import run_m6  # noqa: E402


def test_corpus_shape_and_categories() -> None:
    tasks, assertions = L.load_corpus()
    assert len(tasks["tasks"]) == 20
    assert len({row["id"] for row in tasks["tasks"]}) == 20
    assert set(assertions["tasks"]) == {row["id"] for row in tasks["tasks"]}
    special = {"self_graph", "model_api", "evidence", "preflight", "actions",
               "prefetch", "ocelot"}
    assert sum(1 for row in tasks["tasks"] if row["category"] in special) == 10
    assert sum(1 for row in tasks["tasks"] if row["category"] not in special) == 10
    assert all("format" in row["answer_contract"] for row in tasks["tasks"])


def test_prompts_do_not_name_files_or_answers() -> None:
    tasks, _assertions = L.load_corpus()
    banned = ("docs/", "src/", "tests/", ".py", ".md", ".json", "eval/")
    for row in tasks["tasks"]:
        prompt = row["prompt"].casefold()
        for token in banned:
            assert token not in prompt, (row["id"], token)


def test_frozen_hashes_and_schedule() -> None:
    L.verify_frozen_corpus()
    tasks, _assertions = L.load_corpus()
    assert L.file_sha256(L.TASKS_PATH) == L.TASKS_SHA256
    assert L.file_sha256(L.ASSERTIONS_PATH) == L.ASSERTIONS_SHA256
    assert L.schedule_hash(tasks) == L.SCHEDULE_SHA256
    assert L.SCHEDULE_PATH.read_bytes() == L.schedule_bytes(tasks)
    first = L.schedule_document(tasks)
    second = L.schedule_document(tasks)
    assert first == second
    assert len(first["schedule"]) == 40
    assert L.validate_schedule_shape(first["schedule"]) == []
    assert {row["arm"] for row in first["schedule"]} == set(L.ARMS)
    assert len({row["pair_id"] for row in first["schedule"]}) == 20
    for pair in {row["pair_id"] for row in first["schedule"]}:
        rows = [row for row in first["schedule"] if row["pair_id"] == pair]
        assert sorted(row["arm"] for row in rows) == sorted(L.ARMS)


def test_hash_validation_detects_a_changed_corpus(tmp_path: Path) -> None:
    for name in ("tasks.json", "assertions.json", "schedule.json"):
        (tmp_path / name).write_bytes((M6 / name).read_bytes())
    L.verify_frozen_corpus(tmp_path)
    tasks = json.loads((tmp_path / "tasks.json").read_text(encoding="utf-8"))
    tasks["tasks"][0]["prompt"] += " changed"
    (tmp_path / "tasks.json").write_text(json.dumps(tasks), encoding="utf-8")
    with pytest.raises(L.CorpusError):
        L.verify_frozen_corpus(tmp_path)


def test_validate_facts_passes() -> None:
    report = L.validate_facts()
    assert report["ok"] is True, report["errors"][:3]
    assert report["tasks"] == 20
    assert report["required_groups"] == 144
    assert report["forbidden_phrases"] == 59


def test_scorer_required_alternatives_and_forbidden() -> None:
    _tasks, assertions = L.load_corpus()
    spec = assertions["tasks"]["core-boundaries"]
    good = (
        "STANDARD LIBRARY ONLY. Ozagia never imports a target project. "
        "Writes stay in the state dir except the edit the caller asked for."
    )
    result = L.score_answer(good, spec)
    assert result["success"] is True, result
    assert result["required_missing"] == []
    bad = good + " Ozagia imports the target project."
    result = L.score_answer(bad, spec)
    assert result["success"] is False
    assert "ozagia imports the target project" in result["forbidden_hits"]
    missing = good.replace("state dir", "somewhere")
    result = L.score_answer(missing, spec)
    assert result["success"] is False
    assert "state_dir_boundary" in result["required_missing"]


def test_scorer_extracts_json_from_fence() -> None:
    _tasks, assertions = L.load_corpus()
    spec = assertions["tasks"]["maps-types-and-budgets"]
    answer = """```json
{
  "types": ["graph", "outline", "capabilities", "work"],
  "defaults": {"bytes": "64 KiB", "items": 25, "depth": 2},
  "hard_caps": {"bytes": "256 KiB", "items": 200, "depth": 4}
}
```"""
    result = L.score_answer(answer, spec)
    assert result["success"] is True, result
    result = L.score_answer(answer + "\nMaps are a new graph store.", spec)
    assert result["success"] is False
    assert "maps are a new graph store" in result["forbidden_hits"]


def test_arm_configs_are_isolated_and_redacted() -> None:
    secret = "sk-local-test-only"
    baseline = L.build_arm_config(
        "baseline", api_key=secret, workdir="/tmp/work",
        tool_python="/usr/bin/python3", source_src="/tmp/src")
    maps = L.build_arm_config(
        "maps", api_key=secret, workdir="/tmp/work",
        tool_python="/usr/bin/python3", source_src="/tmp/src")
    L.validate_arm_config(baseline, "baseline")
    L.validate_arm_config(maps, "maps")
    assert baseline["plugins"] == [] and baseline["mcp"] == {}
    assert maps["plugins"] == [] and set(maps["mcp"]["servers"]) == {L.MCP_SERVER_NAME}
    assert maps["mcp"]["servers"][L.MCP_SERVER_NAME]["command"][1:4] == [
        "-m", L.MCP_MODULE, "--root"]
    assert maps["mcp"]["servers"][L.MCP_SERVER_NAME]["command"][5:] == [
        "--mode", "coding", "--surface", "full"]
    assert L.permission_effect(maps, "ozagia_dev_maps") == "allow"
    assert L.permission_effect(maps, "ozagia_dev_capabilities") == "allow"
    assert L.permission_effect(maps, "ozagia_dev_edit") == "deny"
    assert L.permission_effect(maps, "ozagia_dev_refresh") == "deny"
    assert L.permission_effect(maps, "execute") == "deny"
    assert L.permission_effect(baseline, "execute") == "deny"
    assert maps["mcp"]["servers"][L.MCP_SERVER_NAME]["codemode"] is False
    assert not any(rule["action"].startswith("ozagia_")
                   for rule in baseline["permissions"])
    assert baseline["permissions"][0] == {
        "action": "*", "resource": "*", "effect": "deny"}
    for action in ("edit", "write", "patch", "shell", "bash", "webfetch",
                   "websearch", "external_directory"):
        assert L.permission_effect(maps, action) == "deny", action
    assert "permission" not in maps and isinstance(maps["permissions"], list)
    assert "enabled" not in maps["mcp"]["servers"][L.MCP_SERVER_NAME]
    assert maps["mcp"]["servers"][L.MCP_SERVER_NAME].get("disabled") is None
    assert secret not in json.dumps(maps)
    assert secret not in json.dumps(L.redacted_config(maps))
    assert secret not in L.config_digest(maps)
    assert maps["providers"]["vllm"]["settings"]["apiKey"] == "{env:M6_API_KEY}"
    assert L.arm_shape_digest("baseline") == L.arm_shape_digest("baseline")
    assert L.arm_shape_digest("maps") == L.arm_shape_digest("maps")
    with pytest.raises(ValueError):
        L.build_arm_config("baseline", api_key="", workdir="/tmp/work",
                           tool_python="/usr/bin/python3", source_src="/tmp/src")


def test_mcp_tool_permissions_match_server_table() -> None:
    from ozagia.integrations.mcp.server import _TOOLS
    names = {str(tool["name"]) for tool in _TOOLS}
    assert "dev_maps" in names and "dev_capabilities" in names
    assert set(L.MCP_READ_TOOLS) <= names
    assert set(L.MCP_DENIED_TOOLS) <= names
    assert not (set(L.MCP_READ_TOOLS) & set(L.MCP_DENIED_TOOLS))
    for tool in (*L.MCP_READ_TOOLS, *L.MCP_DENIED_TOOLS):
        assert L.mcp_permission_action(tool) == f"ozagia_{tool}"


def test_parse_opencode_events_keeps_final_message_and_totals() -> None:
    stdout = "\n".join([
        json.dumps({"type": "step_finish", "part": {
            "messageID": "msg1", "tokens": {
                "input": 10, "output": 2, "cache": {"read": 3, "write": 4}},
            "cost": 0.1}}),
        json.dumps({"type": "text", "part": {"messageID": "msg1", "text": "interim"}}),
        json.dumps({"type": "tool_use", "part": {
            "tool": "read", "state": {"status": "completed"}}}),
        json.dumps({"type": "step_finish", "part": {
            "messageID": "msg2", "tokens": {"input": 20, "output": 5}, "cost": 0.2}}),
        json.dumps({"type": "tool_use", "part": {
            "tool": "execute", "state": {"input": {
                "code": "tools.ozagia.dev_maps({})"}}}}),
        json.dumps({"type": "text", "part": {"messageID": "msg2", "text": "final"}}),
        json.dumps({"type": "tool_use", "part": {
            "tool": "ozagia_dev_capabilities", "state": {"status": "completed"}}}),
        json.dumps({"type": "tool_use", "part": {
            "tool": "edit", "state": {
                "status": "error", "error": "permission denied"}}}),
    ])
    parsed = L.parse_opencode_events(stdout)
    assert parsed["answer"] == "final"
    assert parsed["steps"] == 2
    assert parsed["tokens"] == {"input": 30, "output": 7, "cache_read": 3,
                                "cache_write": 4, "cost_usd": 0.3}
    assert parsed["tool_calls"] == 4
    assert parsed["mcp_calls"] == 2
    assert parsed["maps_calls"] == 1
    assert parsed["capabilities_calls"] == 1
    assert parsed["permission_denials"] == [
        {"tool": "edit", "reason": "permission denied"}]
    assert parsed["driver_error"] is None


def test_parse_opencode_events_reports_no_step_and_scrubs_secret() -> None:
    secret = "sk-very-secret-value"
    parsed = L.parse_opencode_events(
        json.dumps({"type": "text", "part": {"text": f"oops {secret}"}}),
        secret=secret)
    assert secret not in parsed["answer"]
    assert "<redacted>" in parsed["answer"]
    assert parsed["driver_error"] == "opencode produced no finished step"
    assert parsed["tokens"]["input"] is None


def test_dry_run_makes_no_process_or_model_call(tmp_path: Path, monkeypatch) -> None:
    def fail(*_args, **_kwargs):
        raise AssertionError("a process or network call was attempted in dry-run")

    monkeypatch.setattr(run_m6.subprocess, "run", fail)
    monkeypatch.setattr(run_m6.subprocess, "Popen", fail)
    monkeypatch.setattr(run_m6.urllib.request, "urlopen", fail)
    out = tmp_path / "dry"
    rc = run_m6.main(["--dry-run", "--out", str(out)])
    assert rc == 0
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["mode"] == "dry-run"
    assert manifest["process_calls"] == 0
    assert manifest["model_calls"] == 0
    assert manifest["network_calls"] == 0
    assert manifest["schedule"]["sha256"] == L.SCHEDULE_SHA256
    assert json.loads((out / "schedule.json").read_text(encoding="utf-8"))[
        "sessions"] == 40


def test_live_gate_refuses_without_env(tmp_path: Path, monkeypatch) -> None:
    def fail(*_args, **_kwargs):
        raise AssertionError("a process call was attempted before the live gate")

    monkeypatch.delenv(L.LIVE_ENV, raising=False)
    monkeypatch.setattr(run_m6.subprocess, "run", fail)
    monkeypatch.setattr(run_m6.subprocess, "Popen", fail)
    rc = run_m6.main(["--live", "--out", str(tmp_path / "live")])
    assert rc == 2


def test_summary_pairs_only_both_verified_and_reports_failures() -> None:
    def run(arm: str, task: str, ms: int | None, verified: bool = True) -> dict:
        return {
            "arm": arm, "task_id": task, "status": "completed",
            "valid": verified, "verified": verified,
            "setup_inclusive_ms": ms,
            "timed_out": False, "score": {"success": verified},
            "tokens": {"input": 10, "output": 2, "cache_read": 1,
                       "cache_write": 0},
            "tool_calls": 2, "mcp_calls": 1, "maps_calls": 1,
            "permission_denials": [],
        }

    runs = [
        run("baseline", "t1", 1000), run("maps", "t1", 800),
        run("baseline", "t2", 2000), run("maps", "t2", 1900),
        run("baseline", "t3", 1000), run("maps", "t3", None, verified=False),
    ]
    summary = L.summarise_runs(runs)
    assert summary["pairs_both_verified"] == 2
    assert summary["secondary"]["pairs_used"] == 2
    assert summary["secondary"]["baseline_median_ms"] == 1500
    assert summary["secondary"]["maps_median_ms"] == 1350
    assert summary["secondary"]["improvement_per_mille"] == 100
    assert summary["secondary"]["insufficient_pairs"] is True
    assert len(summary["pairs_not_both_verified"]) == 1
    assert summary["pairs_not_both_verified"][0]["task_id"] == "t3"


def test_arm_shape_redaction_is_stable() -> None:
    first = L.arm_shape_config("maps")
    second = L.arm_shape_config("maps")
    assert first == second
    assert first["providers"]["vllm"]["settings"]["apiKey"] == "{env:M6_API_KEY}"
    assert "<WORKDIR>" in json.dumps(first)
    assert "<TOOL_PYTHON>" in json.dumps(first)
