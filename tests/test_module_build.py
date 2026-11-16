"""Tests for preview-first module scaffolding from a repo-scan report."""
from __future__ import annotations

import importlib
import json
import os
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from ozagia import module_build_cli as CLI
from ozagia.core import module_build as MB

OPPORTUNITY_ID = "missing-tests:src/app.py"


def _opportunity(**overrides: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": OPPORTUNITY_ID,
        "category": "missing_tests",
        "title": "No linked test for src/app.py",
        "expected_effect": (
            "a regression in src/app.py would fail a targeted test"),
        "effort": "S",
        "risk": "low",
        "confidence": "medium",
        "proposed_module": "tests/test_app.py",
        "action": "Add focused tests for the public symbols.",
        "evidence": [
            {"kind": "file", "path": "src/app.py"},
            {"kind": "graph_key", "key": "file:src/app.py"},
            {"kind": "metric", "name": "symbols", "value": 2},
        ],
        "not_assessed": ["coverage quality"],
        "rank": 1,
        "score": 82,
    }
    item.update(overrides)
    return item


def _report(*items: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": "ozagia.repo_scan.report.v1",
        "schema_version": 1,
        "advisory": True,
        "truth_assessed": False,
        "opportunities": list(items),
    }


def _tree(root: Path) -> set[str]:
    return {path.relative_to(root).as_posix() for path in root.rglob("*")}


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch
         ) -> tuple[Path, Path]:
    monkeypatch.delenv("OZAGIA_STATE_DIR", raising=False)
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text(
        "def app():\n    return 1\n", encoding="utf-8")
    report = tmp_path / "scan.json"
    report.write_text(json.dumps(_report(_opportunity())), encoding="utf-8")
    return root, report


def _plan(root: Path, report: Path, **kwargs: Any) -> dict[str, Any]:
    return MB.build_plan(
        root, report_path=report, opportunity_id=OPPORTUNITY_ID, **kwargs)


# --------------------------------------------------------------------------- #
# preview and determinism
# --------------------------------------------------------------------------- #
def test_preview_writes_nothing_and_copies_the_opportunity(
        repo: tuple[Path, Path]) -> None:
    root, report = repo
    before = _tree(root)
    plan = _plan(root, report)
    assert _tree(root) == before
    assert not (root / ".ozagia").exists()
    assert plan["schema"] == MB.SCHEMA
    assert plan["advisory"] is True
    assert plan["truth_assessed"] is False
    assert "not a merge" in plan["not_merge"]
    assert plan["module"]["id"] == "app"
    assert plan["module"]["destination"] == "modules/app"
    assert plan["module"]["package"] == "modules.app"
    assert plan["module"]["test"] == "modules/app/tests/test_app.py"
    assert [item["path"] for item in plan["files"]] == [
        "modules/app/README.md",
        "modules/app/__init__.py",
        "modules/app/module.py",
        "modules/app/tests/test_app.py",
    ]
    assert plan["dependencies"] == []
    assert plan["write_scope"] == ["modules/app"]
    assert plan["effort"] == "S"
    assert plan["risk"] == "low"
    assert plan["confidence"] == "medium"
    assert plan["expected_effect"].startswith("a regression")
    assert plan["evidence"][0] == {"kind": "file", "path": "src/app.py"}
    assert plan["opportunity"]["action"].startswith("Add focused tests")
    assert plan["generated_tests"][0]["path"] == (
        "modules/app/tests/test_app.py")
    assert {gate["id"] for gate in plan["review_gates"]} >= {
        "generated_tests", "lint", "importable", "human_review"}
    assert any("coverage quality" in item for item in plan["not_assessed"])
    assert plan["confirmation"]["required"] is True
    assert plan["confirmation"]["one_time"] is True
    assert plan["confirmation"]["token"].startswith("mb1_")


def test_plan_is_deterministic(repo: tuple[Path, Path]) -> None:
    root, report = repo
    first = _plan(root, report)
    second = _plan(root, report)
    assert MB.plan_json(first) == MB.plan_json(second)
    assert first["plan_digest"] == second["plan_digest"]
    assert first["confirmation"]["token"] == second["confirmation"]["token"]
    assert "generated_at" not in first
    assert "at" not in first


def test_multi_opportunity_report_requires_an_id(
        repo: tuple[Path, Path]) -> None:
    root, report = repo
    other = _opportunity(id="hotspot:src/app.py", category="hotspots",
                         title="Hotspot: src/app.py",
                         proposed_module="src/app.py")
    report.write_text(json.dumps(_report(_opportunity(), other)),
                      encoding="utf-8")
    with pytest.raises(MB.ModuleBuildError, match="choose one"):
        MB.build_plan(root, report_path=report)
    plan = MB.build_plan(root, report_path=report,
                         opportunity_id="hotspot:src/app.py")
    assert plan["module"]["id"] == "app"
    assert plan["opportunity"]["category"] == "hotspots"

    single = root / "single.json"
    single.write_text(json.dumps(_opportunity()), encoding="utf-8")
    explicit = MB.build_plan(root, report_path=single)
    assert explicit["opportunity"]["id"] == OPPORTUNITY_ID


def test_default_report_path_is_read_only(repo: tuple[Path, Path]) -> None:
    root, report = repo
    state = root / ".ozagia"
    state.mkdir()
    (state / "repo_scan.json").write_text(report.read_text(encoding="utf-8"),
                                          encoding="utf-8")
    before = _tree(root)
    plan = MB.build_plan(root, opportunity_id=OPPORTUNITY_ID)
    assert plan["module"]["id"] == "app"
    assert _tree(root) == before


# --------------------------------------------------------------------------- #
# token and path checks
# --------------------------------------------------------------------------- #
def test_token_is_required_and_one_time(repo: tuple[Path, Path]) -> None:
    root, report = repo
    plan = _plan(root, report)
    token = plan["confirmation"]["token"]
    with pytest.raises(MB.ModuleBuildRefused, match="required"):
        MB.apply(root, plan, None)
    with pytest.raises(MB.ModuleBuildRefused, match="does not match"):
        MB.apply(root, plan, "mb1_" + "0" * 40)
    with pytest.raises(MB.ModuleBuildRefused, match="malformed"):
        MB.apply(root, plan, "not-a-token")
    assert not (root / "modules" / "app").exists()

    result = MB.apply(root, plan, token)
    assert result["ok"] is True
    assert result["module_id"] == "app"
    with pytest.raises(MB.ModuleBuildRefused, match="already used"):
        MB.apply(root, plan, token)
    with pytest.raises(MB.ModuleBuildRefused, match="already used"):
        MB.apply(root, plan, token, force=True)


def test_token_is_bound_to_the_root(repo: tuple[Path, Path],
                                    tmp_path: Path) -> None:
    root, report = repo
    plan = _plan(root, report)
    other = tmp_path / "other"
    other.mkdir()
    with pytest.raises(MB.ModuleBuildRefused):
        MB.apply(other, plan, plan["confirmation"]["token"])
    assert list(other.iterdir()) == []


@pytest.mark.parametrize("dest", [
    "../escape",
    "/tmp/escape",
    "modules/../escape",
    ".git/hooks",
    ".ozagia/modules/app",
    "modules/.git/app",
    "modules/.ozagia/app",
    "modules/bad-name",
    ".",
])
def test_unsafe_destination_is_refused(repo: tuple[Path, Path],
                                       dest: str) -> None:
    root, report = repo
    with pytest.raises(MB.ModuleBuildRefused):
        _plan(root, report, dest=dest)
    assert not (root / "modules").exists()


def test_symlinked_destination_is_refused(repo: tuple[Path, Path],
                                          tmp_path: Path) -> None:
    root, report = repo
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "modules").symlink_to(outside, target_is_directory=True)
    with pytest.raises(MB.ModuleBuildRefused, match="symlink"):
        _plan(root, report)
    assert list(outside.iterdir()) == []


def test_existing_target_needs_force(repo: tuple[Path, Path]) -> None:
    root, report = repo
    destination = root / "modules" / "app"
    destination.mkdir(parents=True)
    readme = destination / "README.md"
    readme.write_text("keep me\n", encoding="utf-8")
    plan = _plan(root, report)
    assert plan["applicable"] is False
    assert plan["collisions"] == [
        {"path": "modules/app/README.md", "kind": "existing_file"}]
    token = plan["confirmation"]["token"]
    with pytest.raises(MB.ModuleBuildRefused, match="already exist"):
        MB.apply(root, plan, token)
    assert readme.read_text(encoding="utf-8") == "keep me\n"

    result = MB.apply(root, plan, token, force=True)
    replaced = [item for item in result["files"] if item["replaced"]]
    assert [item["path"] for item in replaced] == ["modules/app/README.md"]
    assert readme.read_text(encoding="utf-8") != "keep me\n"


def test_destination_that_is_a_file_is_refused(repo: tuple[Path, Path]) -> None:
    root, report = repo
    (root / "modules").mkdir()
    (root / "modules" / "app").write_text("not a directory\n", encoding="utf-8")
    plan = _plan(root, report)
    assert any(item["kind"] == "not_a_directory"
               for item in plan["collisions"])
    with pytest.raises(MB.ModuleBuildRefused, match="not a directory"):
        MB.apply(root, plan, plan["confirmation"]["token"])


def test_tampered_plan_is_refused(repo: tuple[Path, Path]) -> None:
    root, report = repo
    plan = _plan(root, report)
    tampered = json.loads(json.dumps(plan))
    tampered["files"][0]["content"] += "changed"
    with pytest.raises(MB.ModuleBuildRefused, match="changed after preview"):
        MB.apply(root, plan=tampered,
                 confirmation=tampered["confirmation"]["token"])

    retargeted = json.loads(json.dumps(plan))
    retargeted["files"][0]["path"] = "modules/app/../escape.md"
    with pytest.raises(MB.ModuleBuildRefused):
        MB.apply(root, retargeted,
                 confirmation=retargeted["confirmation"]["token"])
    assert not (root / "modules").exists()


# --------------------------------------------------------------------------- #
# apply output and generated code
# --------------------------------------------------------------------------- #
def _drop_generated_modules() -> None:
    for name in list(sys.modules):
        if name == "modules" or name.startswith("modules."):
            del sys.modules[name]


def test_apply_writes_only_the_destination_and_audits(
        repo: tuple[Path, Path]) -> None:
    root, report = repo
    plan = _plan(root, report)
    before = _tree(root)
    source_before = (root / "src" / "app.py").read_text(encoding="utf-8")
    result = MB.apply(root, plan, plan["confirmation"]["token"])
    after = _tree(root)
    new = after - before
    assert new
    assert all(
        rel in {"modules", "modules/app", ".ozagia", ".ozagia/modules"}
        or rel.startswith("modules/app/")
        or rel.startswith(".ozagia/")
        for rel in new
    )
    assert (root / "src" / "app.py").read_text(encoding="utf-8") == source_before
    assert result["applied"] is True
    assert result["audit"] == ".ozagia/modules/audit.jsonl"
    audit_text = (root / result["audit"]).read_text(encoding="utf-8")
    assert plan["confirmation"]["token"] not in audit_text
    assert plan["plan_digest"] in audit_text
    assert (root / result["audit"]).is_file()
    assert len(result["files"]) == 4
    assert {item["path"] for item in result["files"]} == {
        "modules/app/README.md",
        "modules/app/__init__.py",
        "modules/app/module.py",
        "modules/app/tests/test_app.py",
    }
    records = MB.list_applied(root)
    assert len(records) == 1
    assert records[0]["module_id"] == "app"
    assert records[0]["destination"] == "modules/app"
    assert records[0]["merged"] is False


def test_generated_module_is_importable_and_its_test_runs(
        repo: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    root, report = repo
    plan = _plan(root, report)
    MB.apply(root, plan, plan["confirmation"]["token"])

    monkeypatch.syspath_prepend(str(root))
    importlib.invalidate_caches()
    try:
        scaffold = importlib.import_module("modules.app.module")
        summary = scaffold.build_summary()
        assert summary["module"] == "app"
        assert summary["source_opportunity"] == OPPORTUNITY_ID
        assert summary["status"] == "scaffold"
        assert summary["schema"] == scaffold.SCHEMA
        assert scaffold.main() == 0
    finally:
        _drop_generated_modules()

    test_path = root / plan["module"]["test"]
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         str(test_path)],
        cwd=root, capture_output=True, text=True, env=env, check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_custom_destination_is_importable(repo: tuple[Path, Path],
                                          monkeypatch: pytest.MonkeyPatch
                                          ) -> None:
    root, report = repo
    plan = _plan(root, report, dest="lib/custom_app")
    assert plan["module"]["destination"] == "lib/custom_app"
    assert plan["module"]["test"] == "lib/custom_app/tests/test_app.py"
    assert plan["write_scope"] == ["lib/custom_app"]
    assert plan["module_id"] == "app"
    assert plan["file_paths"] == [item["path"] for item in plan["files"]]
    MB.apply(root, plan, plan["confirmation"]["token"])
    assert (root / "lib" / "custom_app" / "module.py").is_file()

    monkeypatch.syspath_prepend(str(root))
    importlib.invalidate_caches()
    try:
        scaffold = importlib.import_module("lib.custom_app.module")
        assert scaffold.build_summary()["module"] == "app"
    finally:
        for name in list(sys.modules):
            if name == "lib" or name.startswith("lib."):
                del sys.modules[name]


def test_absolute_destination_inside_root_and_report_path_alias(
        repo: tuple[Path, Path]) -> None:
    root, report = repo
    plan = MB.build_plan(
        root, report=report, opportunity_id=OPPORTUNITY_ID,
        dest=root / "custom")
    assert plan["destination"] == "custom"
    assert plan["module"]["destination"] == "custom"
    assert plan["module"]["test"] == "custom/tests/test_app.py"
    assert plan["confirmation_token"] == plan["confirmation"]["token"]
    with pytest.raises(MB.ModuleBuildRefused):
        MB.build_plan(root, report_path=report, opportunity_id=OPPORTUNITY_ID,
                      dest=repo[0].parent / "outside")


def test_quoted_title_does_not_break_generated_code(
        repo: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    root, _report_path = repo
    odd = _opportunity(title='Add a parser """ with quotes', id="odd:1",
                       proposed_module="src/parser.py")
    plan = MB.build_plan(root, opportunity=odd)
    MB.apply(root, plan, plan["confirmation"]["token"])
    monkeypatch.syspath_prepend(str(root))
    importlib.invalidate_caches()
    try:
        scaffold = importlib.import_module("modules.parser.module")
        assert scaffold.build_summary()["title"].startswith("Add a parser")
    finally:
        _drop_generated_modules()


def test_no_network_is_used(repo: tuple[Path, Path],
                            monkeypatch: pytest.MonkeyPatch) -> None:
    root, report = repo

    def boom(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("network access is not allowed")

    monkeypatch.setattr(socket, "socket", boom)
    monkeypatch.setattr(socket, "create_connection", boom)
    plan = _plan(root, report)
    MB.apply(root, plan, plan["confirmation"]["token"])
    assert (root / "modules" / "app" / "module.py").is_file()


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def test_cli_preview_apply_and_list(repo: tuple[Path, Path],
                                    capsys: pytest.CaptureFixture[str]) -> None:
    root, report = repo
    preview = CLI.main([
        "preview", "--root", str(root), "--report", str(report),
        "--opportunity", OPPORTUNITY_ID, "--json",
    ])
    assert preview == 0
    plan = json.loads(capsys.readouterr().out)
    token = plan["confirmation"]["token"]
    assert plan["module"]["id"] == "app"
    assert not (root / "modules").exists()

    refused = CLI.main([
        "apply", "--root", str(root), "--report", str(report),
        "--opportunity", OPPORTUNITY_ID, "--confirm", "mb1_" + "0" * 40,
    ])
    assert refused == 1
    assert "does not match" in capsys.readouterr().err

    applied = CLI.main([
        "apply", "--root", str(root), "--report", str(report),
        "--opportunity", OPPORTUNITY_ID, "--confirm", token,
    ])
    assert applied == 0
    assert "applied scaffold: app" in capsys.readouterr().out
    assert (root / "modules" / "app" / "module.py").is_file()

    listed = CLI.main(["list", "--root", str(root), "--json"])
    assert listed == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == MB.AUDIT_SCHEMA
    assert payload["records"][0]["module_id"] == "app"


def test_cli_usage_errors_return_two(repo: tuple[Path, Path],
                                     capsys: pytest.CaptureFixture[str]) -> None:
    root, report = repo
    missing = CLI.main(["preview", "--root", str(root)])
    assert missing == 2
    assert "no conventional report" in capsys.readouterr().err

    unknown = CLI.main([
        "preview", "--root", str(root), "--report", str(report),
        "--opportunity", "does-not-exist",
    ])
    assert unknown == 2
    assert "is not in the report" in capsys.readouterr().err

    traversal = CLI.main([
        "preview", "--root", str(root), "--report", str(report),
        "--opportunity", OPPORTUNITY_ID, "--dest", "../escape",
    ])
    assert traversal == 1
    assert "refused" in capsys.readouterr().err
