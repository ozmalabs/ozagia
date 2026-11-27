"""Tests for the offline Ocelot test-planner evaluation harness."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from ozagia.core.memory import DevMemory


def _load_fixture_builder() -> Any:
    path = Path(__file__).resolve().parent / "fixtures" / "ocelot_workspace_repo.py"
    spec = importlib.util.spec_from_file_location(
        "_ocelot_workspace_eval_fixture", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


FIXTURE = _load_fixture_builder()

LOCAL_CASE = {
    "name": "local-seed",
    "changed": ["seeds/local.json"],
    "expected_tests": ["tests/test_local_migrated.py"],
    "expected_mode": "affected",
}
ENGINE_CASE = {
    "name": "engine-full",
    "changed": ["runners/dsl/Cargo.toml"],
    "expected_tests": ["tests/test_engine_provenance_migrated.py"],
    "expected_mode": "full",
}
DOCS_CASE = {
    "name": "docs-only",
    "changed": ["docs/seed_notes.md"],
    "expected_tests": [],
    "expected_mode": "none",
}
WRONG_CASE = {
    "name": "wrong-seed-test",
    "changed": ["seeds/local.json"],
    "expected_tests": ["tests/test_shared_migrated.py"],
    "expected_mode": "affected",
}
SHARED_CASE = {
    "name": "shared-seed",
    "changed": ["seeds/shared.json"],
    "expected_tests": ["tests/test_shared_migrated.py"],
    "expected_mode": "affected",
}


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, DevMemory]:
    monkeypatch.delenv("OZAGIA_CONFIG", raising=False)
    monkeypatch.delenv("OZAGIA_ADAPTERS", raising=False)
    return FIXTURE.build_workspace(tmp_path / "ocelot-eval")


@pytest.fixture
def eval_api() -> Any:
    return pytest.importorskip(
        "ozagia.integrations.ocelot.workspace_eval",
        reason="the Ocelot planner evaluation has not been written yet")


def test_evaluate_three_cases_metrics_and_no_test_execution(
        workspace: tuple[Path, DevMemory], eval_api: Any,
        monkeypatch: pytest.MonkeyPatch) -> None:
    root, mem = workspace

    def refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("evaluation must never execute a process")

    monkeypatch.setattr(subprocess, "run", refuse)
    result = eval_api.evaluate(
        root, [LOCAL_CASE, ENGINE_CASE, DOCS_CASE], memory=mem)

    assert result["cases"] == 3
    assert result["affected_cases"] == 1
    assert result["full_cases"] == 1
    assert result["none_cases"] == 1
    assert result["expected_miss"] == 0
    assert result["missed_tests"] == []
    assert result["recall"] == pytest.approx(1.0)
    assert result["ok"] is True
    assert result["engine_rebuild"] == 1
    assert result["engine_rebuild_cases"] == ["engine-full"]
    assert result["full_gate_required_but_expected_affected"] == 0

    details = {item["name"]: item for item in result["details"]}
    assert details["local-seed"]["mode"] == "affected"
    assert details["local-seed"]["selected_count"] >= 1
    assert details["local-seed"]["recall"] == pytest.approx(1.0)
    assert details["engine-full"]["mode"] == "full"
    assert details["engine-full"]["full_gate"] is True
    assert details["engine-full"]["engine_rebuild"] is True
    assert details["engine-full"]["covered"] is True
    assert details["engine-full"]["recall"] is None
    assert details["docs-only"]["mode"] == "none"
    assert details["docs-only"]["expected_count"] == 0
    assert details["docs-only"]["covered"] is True


def test_evaluate_detects_a_seeded_wrong_expectation(
        workspace: tuple[Path, DevMemory], eval_api: Any) -> None:
    root, mem = workspace
    result = eval_api.evaluate(root, [LOCAL_CASE, WRONG_CASE], memory=mem)

    assert result["expected_miss"] == 1
    assert result["missed_tests"] == ["tests/test_shared_migrated.py"]
    assert result["missed_cases"] == ["wrong-seed-test"]
    assert result["recall"] == pytest.approx(0.5)
    assert result["ok"] is False

    details = {item["name"]: item for item in result["details"]}
    assert details["wrong-seed-test"]["missed"] == [
        "tests/test_shared_migrated.py"]
    assert details["wrong-seed-test"]["covered"] is False
    assert any("missed expected test" in warning
               for warning in result["warnings"])


def test_full_gate_for_an_expected_affected_case_is_flagged_not_recall(
        workspace: tuple[Path, DevMemory], eval_api: Any) -> None:
    root, mem = workspace
    result = eval_api.evaluate(root, [SHARED_CASE], memory=mem)

    assert result["full_cases"] == 0
    assert result["affected_cases"] == 1
    assert result["full_gate_required_but_expected_affected"] == 1
    assert result["full_gate_required_but_expected_affected_cases"] == [
        "shared-seed"]
    assert result["expected_miss"] == 0
    assert result["recall"] is None
    assert result["ok"] is False
    detail = result["details"][0]
    assert detail["mode"] == "full"
    assert detail["expected_mode"] == "affected"
    assert detail["covered"] is True
    assert detail["recall"] is None
    assert any("required the full gate" in warning
               for warning in result["warnings"])


def test_load_cases_write_json_and_markdown_reports(
        tmp_path: Path, workspace: tuple[Path, DevMemory],
        eval_api: Any) -> None:
    root, mem = workspace
    cases_path = tmp_path / "cases.json"
    cases_path.write_text(
        json.dumps({"cases": [LOCAL_CASE, ENGINE_CASE, DOCS_CASE]}),
        encoding="utf-8")
    cases = eval_api.load_cases(cases_path)
    assert [case["name"] for case in cases] == [
        "local-seed", "engine-full", "docs-only"]

    result = eval_api.evaluate(root, cases, budget_tests=5, memory=mem)
    json_path = tmp_path / "reports" / "result.json"
    written = eval_api.write_report(result, json_path)
    assert written["format"] == "json"
    assert json_path.is_file()
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["cases"] == 3
    assert payload["budget_tests"] == 5

    markdown_path = tmp_path / "reports" / "result.md"
    written = eval_api.write_report(result, markdown_path)
    assert written["format"] == "markdown"
    text = markdown_path.read_text(encoding="utf-8")
    assert text.startswith("# Ocelot test planner evaluation")
    assert "local-seed" in text
    assert "recall (affected)" in text
    assert "No tests were executed" in text

    with pytest.raises(eval_api.WorkspaceEvalError):
        eval_api.write_report(result, tmp_path / "report.txt")


def test_evaluate_loads_the_existing_snapshot(
        workspace: tuple[Path, DevMemory], eval_api: Any) -> None:
    root, _mem = workspace
    result = eval_api.evaluate(root, [LOCAL_CASE])
    assert result["cases"] == 1
    assert result["recall"] == pytest.approx(1.0)


def test_main_prints_json_and_writes_markdown(
        tmp_path: Path, workspace: tuple[Path, DevMemory], eval_api: Any,
        capsys: pytest.CaptureFixture[str]) -> None:
    root, _mem = workspace
    cases_path = tmp_path / "cases.json"
    cases_path.write_text(json.dumps([LOCAL_CASE, ENGINE_CASE, DOCS_CASE]),
                          encoding="utf-8")

    code = eval_api.main([
        "--root", str(root), "--cases", str(cases_path), "--json",
        "--budget-tests", "10",
    ])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["cases"] == 3
    assert payload["budget_tests"] == 10
    assert payload["ok"] is True

    out = tmp_path / "report.md"
    code = eval_api.main([
        "--root", str(root), "--cases", str(cases_path),
        "--out", str(out),
    ])
    assert code == 0
    assert out.is_file()
    assert "wrote" in capsys.readouterr().out
    assert "# Ocelot test planner evaluation" in out.read_text(encoding="utf-8")


def test_main_returns_1_when_evaluation_fails(
        tmp_path: Path, workspace: tuple[Path, DevMemory], eval_api: Any,
        capsys: pytest.CaptureFixture[str]) -> None:
    root, _mem = workspace
    cases_path = tmp_path / "failing-cases.json"
    cases_path.write_text(json.dumps([WRONG_CASE]), encoding="utf-8")

    code = eval_api.main([
        "--root", str(root), "--cases", str(cases_path), "--json",
    ])
    captured = capsys.readouterr()
    assert code == 1
    payload = json.loads(captured.out)
    assert payload["ok"] is False
    assert payload["missed_cases"] == ["wrong-seed-test"]
    assert payload["missed_tests"] == ["tests/test_shared_migrated.py"]


def test_module_cli_from_clean_cwd_with_explicit_paths(
        tmp_path: Path, workspace: tuple[Path, DevMemory]) -> None:
    root, _mem = workspace
    cases_path = tmp_path / "cases.json"
    cases_path.write_text(
        json.dumps([LOCAL_CASE, ENGINE_CASE, DOCS_CASE]),
        encoding="utf-8")
    clean_cwd = tmp_path / "clean-cwd"
    clean_cwd.mkdir()

    completed = subprocess.run(
        [
            sys.executable, "-m",
            "ozagia.integrations.ocelot.workspace_eval",
            "--root", str(root.resolve()),
            "--cases", str(cases_path.resolve()),
            "--json",
        ],
        cwd=clean_cwd,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["cases"] == 3
    assert payload["ok"] is True
    assert payload["root"] == str(root.resolve())
