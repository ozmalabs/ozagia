"""The ``ozagia-toolchain`` command: env, list, plan and explain."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ozagia import toolchain_cli as C
from ozagia.core import toolchain as T


def _which(monkeypatch: pytest.MonkeyPatch,
           mapping: dict[str, str]) -> None:
    monkeypatch.setattr(
        T.shutil, "which", lambda name: mapping.get(name))


def _no_process(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*_args, **_kwargs):
        raise AssertionError("the CLI must not run a process here")

    monkeypatch.setattr(T.subprocess, "run", fail)


def test_cli_env_json_and_text(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                               capsys: pytest.CaptureFixture[str]) -> None:
    _which(monkeypatch, {
        "apt-get": "/usr/bin/apt-get", "pip3": "/usr/bin/pip3",
        "sudo": "/usr/bin/sudo"})
    _no_process(monkeypatch)
    assert C.main(["env", "--root", str(tmp_path), "--no-sudo", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["schema"] == T.SCHEMA
    assert "apt-get" in report["managers"]
    assert report["sudo"]["probed"] is False

    assert C.main(["env", "--root", str(tmp_path), "--no-sudo"]) == 0
    text = capsys.readouterr().out
    assert "platform:" in text
    assert "managers:" in text
    assert "approval:" in text


def test_cli_list_json_and_text(tmp_path: Path,
                                capsys: pytest.CaptureFixture[str]) -> None:
    assert C.main(["list", "--json"]) == 0
    registry = json.loads(capsys.readouterr().out)
    assert registry["registry_version"] == T.REGISTRY_VERSION
    assert "pdf" in registry["capabilities"]
    assert "documents" in registry["groups"]
    assert "exa" in registry["tools"]

    assert C.main(["list"]) == 0
    text = capsys.readouterr().out
    assert "toolchain registry" in text
    assert "group documents" in text
    assert "pdf:" in text
    assert "qpdf" in text


def test_cli_plan_expands_a_group(tmp_path: Path,
                                  monkeypatch: pytest.MonkeyPatch,
                                  capsys: pytest.CaptureFixture[str]) -> None:
    _which(monkeypatch, {})
    _no_process(monkeypatch)
    assert C.main([
        "plan", "--root", str(tmp_path), "--capability", "documents",
        "--json", "--no-versions", "--no-sudo"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["request"]["groups"] == ["documents"]
    assert plan["request"]["capabilities"] == ["ocr", "office", "pdf"]
    assert {item["id"] for item in plan["capabilities"]} == {
        "ocr", "office", "pdf"}


def test_cli_plan_text_is_the_default(tmp_path: Path,
                                      monkeypatch: pytest.MonkeyPatch,
                                      capsys: pytest.CaptureFixture[str]) -> None:
    _which(monkeypatch, {})
    _no_process(monkeypatch)
    assert C.main([
        "plan", "--root", str(tmp_path), "--path", "report.pdf",
        "--no-versions", "--no-sudo"]) == 0
    text = capsys.readouterr().out
    assert "[ozagia] toolchain plan" in text
    assert "request: pdf" in text
    assert "pdf: missing" in text
    assert "installation is not approved" in text


def test_cli_plan_json_and_explicit_capabilities(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    _which(monkeypatch, {"pypdf": "/usr/bin/pypdf"})
    _no_process(monkeypatch)
    assert C.main([
        "plan", "--root", str(tmp_path), "--capability", "pdf",
        "--capability", "ocr", "--json", "--no-versions", "--no-sudo"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["request"]["explicit"] == ["ocr", "pdf"]
    assert plan["request"]["capabilities"] == ["ocr", "pdf"]
    assert "environment" in plan
    assert plan["summary"]["capabilities"] == 2


def test_cli_require_ready_exit_code(tmp_path: Path,
                                     monkeypatch: pytest.MonkeyPatch,
                                     capsys: pytest.CaptureFixture[str]) -> None:
    _which(monkeypatch, {})
    _no_process(monkeypatch)
    assert C.main([
        "plan", "--root", str(tmp_path), "--capability", "pdf",
        "--require-ready", "--no-versions", "--no-sudo"]) == 1
    output = capsys.readouterr()
    assert "no available tool for: pdf" in output.err
    assert "pdf: missing" in output.out

    _which(monkeypatch, {"qpdf": "/usr/bin/qpdf"})
    assert C.main([
        "plan", "--root", str(tmp_path), "--capability", "pdf",
        "--require-ready", "--no-versions", "--no-sudo"]) == 0
    capsys.readouterr()


def test_cli_unknown_capability_is_an_error(tmp_path: Path,
                                           capsys: pytest.CaptureFixture[str]) -> None:
    assert C.main([
        "plan", "--root", str(tmp_path), "--capability", "nope",
        "--no-versions", "--no-sudo"]) == 2
    assert "unknown capability" in capsys.readouterr().err


def test_cli_bounded_inputs(tmp_path: Path,
                            capsys: pytest.CaptureFixture[str]) -> None:
    argv = ["plan", "--root", str(tmp_path), "--no-versions", "--no-sudo"]
    for _ in range(33):
        argv.extend(["--capability", "pdf"])
    assert C.main(argv) == 2
    assert "at most" in capsys.readouterr().err


def test_cli_explain_json_and_unknown_tool(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    _which(monkeypatch, {"qpdf": "/usr/bin/qpdf"})
    _no_process(monkeypatch)
    assert C.main([
        "explain", "qpdf", "--root", str(tmp_path), "--json",
        "--no-versions", "--no-sudo"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["tool"]["id"] == "qpdf"
    assert data["status"]["available"] is True
    assert any(item["manager"] == "apt" for item in data["tool"]["installs"])

    assert C.main([
        "explain", "not-a-tool", "--root", str(tmp_path),
        "--no-versions", "--no-sudo"]) == 2
    assert "unknown tool" in capsys.readouterr().err


def test_explain_accepts_a_probe_alias(tmp_path: Path,
                                       monkeypatch: pytest.MonkeyPatch,
                                       capsys: pytest.CaptureFixture[str]) -> None:
    _which(monkeypatch, {})
    _no_process(monkeypatch)
    assert C.main([
        "explain", "gs", "--root", str(tmp_path), "--json",
        "--no-versions", "--no-sudo"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["tool"]["id"] == "ghostscript"


def test_cli_explain_remote_provider(tmp_path: Path,
                                     monkeypatch: pytest.MonkeyPatch,
                                     capsys: pytest.CaptureFixture[str]) -> None:
    _which(monkeypatch, {})
    _no_process(monkeypatch)
    assert C.main([
        "explain", "exa", "--root", str(tmp_path),
        "--no-versions", "--no-sudo"]) == 0
    text = capsys.readouterr().out
    assert "status: remote provider" in text
    assert "requires network and credentials" in text
    assert "install recipes: none" in text


def test_cli_explain_text_lists_install_recipes(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    _which(monkeypatch, {})
    _no_process(monkeypatch)
    assert C.main([
        "explain", "qpdf", "--root", str(tmp_path),
        "--no-versions", "--no-sudo"]) == 0
    text = capsys.readouterr().out
    assert "qpdf [binary]" in text
    assert "status: missing" in text
    assert "sudo apt-get install -y qpdf" in text


def test_cli_plan_with_approved_config_still_only_previews(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    config = tmp_path / "ozagia" / "config.toml"
    config.parent.mkdir()
    config.write_text(
        "[tools]\nallow_install = true\nallow_network = true\n"
        "allow_sudo = true\n", encoding="utf-8")
    _which(monkeypatch, {
        "apt-get": "/usr/bin/apt-get", "sudo": "/usr/bin/sudo"})
    _no_process(monkeypatch)
    assert C.main([
        "plan", "--root", str(tmp_path), "--capability", "ocr", "--json",
        "--no-versions", "--no-sudo"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert all(option["would_run"] is False
               for capability in plan["capabilities"]
               for option in capability["install"])
    assert plan["environment"]["approval"]["allow_install"] is True
