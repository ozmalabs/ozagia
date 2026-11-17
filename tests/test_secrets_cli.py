"""CLI tests for secret inspection commands (never a resolved value)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ozagia import secrets_cli as CLI
from ozagia.core import secrets as S


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "OZAGIA_CONFIG", "OZAGIA_ROOT", "OZAGIA_STATE_DIR",
        "OZAGIA_SECRETS_DIR", "OZAGIA_SECRET_PROVIDERS",
        "OZAGIA_ALLOW_SECRET_NETWORK", "OZAGIA_CLI_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("ozagia.core.secrets.shutil.which", lambda name: None)


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_cli_providers_has_no_values(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert CLI.main(["providers", "--root", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "env" in out and "file" in out and "session" in out
    assert "requires:" in out
    assert "OZAGIA_CLI_TOKEN" not in out

    assert CLI.main(["providers", "--root", str(tmp_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert {item["name"] for item in payload["providers"]} >= {
        "env", "file", "session"}
    assert "OZAGIA_CLI_TOKEN" not in json.dumps(payload)


def test_cli_check_statuses_and_exit_codes(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("OZAGIA_CLI_TOKEN", "hunter2hunter2")
    assert CLI.main([
        "check", "secret://env/OZAGIA_CLI_TOKEN", "--root", str(tmp_path),
    ]) == 0
    out = capsys.readouterr().out
    assert "available" in out
    assert "hunter2hunter2" not in out

    assert CLI.main([
        "check", "secret://env/MISSING_TOKEN", "--root", str(tmp_path),
    ]) == 1
    assert "missing" in capsys.readouterr().out

    assert CLI.main([
        "check", "secret://env/MISSING_TOKEN", "not-a-ref",
        "--root", str(tmp_path),
    ]) == 2
    out = capsys.readouterr().out
    assert "invalid" in out
    assert "not-a-ref" not in out


def test_cli_check_json_has_no_value(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("OZAGIA_CLI_TOKEN", "hunter2hunter2")
    assert CLI.main([
        "check", "secret://env/OZAGIA_CLI_TOKEN", "secret://env/MISSING",
        "--root", str(tmp_path), "--json",
    ]) == 1
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert [item["status"] for item in payload["checks"]] == [
        "available", "missing"]
    assert "hunter2hunter2" not in out


def test_cli_list_reads_both_config_files_without_values(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("OZAGIA_CLI_TOKEN", "hunter2hunter2")
    _write(tmp_path / ".ozagia.toml", """
[custom]
token = "secret://env/OZAGIA_CLI_TOKEN"
""")
    _write(tmp_path / "ozagia" / "config.toml", """
[custom]
file = "secret://file/token"
""")
    assert CLI.main(["list", "--root", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "secret://env/OZAGIA_CLI_TOKEN" in out
    assert "secret://file/token" in out
    assert "hunter2hunter2" not in out

    assert CLI.main(["list", "--root", str(tmp_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    refs = {row["ref"] for row in payload["references"]}
    assert refs == {"secret://env/OZAGIA_CLI_TOKEN",
                    "secret://file/token"}
    assert "hunter2hunter2" not in json.dumps(payload)


def test_cli_doctor_reports_config_without_values(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write(tmp_path / "ozagia" / "config.toml", """
[secrets]
unknown_setting = "hunter2hunter2"
""")
    assert CLI.main(["doctor", "--root", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "secrets:" in out
    assert "unknown_setting" in out
    assert "hunter2hunter2" not in out

    assert CLI.main(["doctor", "--root", str(tmp_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["sessions"]["storage"] == "memory-only"
    assert "hunter2hunter2" not in json.dumps(payload)


def test_cli_pass_check_never_runs_the_executable(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # `which` is patched to None in the fixture; no pass process is started.
    _write(tmp_path / ".ozagia.toml", """
[secrets]
providers = ["pass"]
allow_cli = ["pass"]
""")
    assert CLI.main([
        "check", "secret://pass/token", "--root", str(tmp_path),
    ]) == 1
    out = capsys.readouterr().out
    assert "provider_unavailable" in out
    assert "token" in out


def test_cli_has_no_capture_or_resolve_command() -> None:
    parser = CLI.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["capture"])
    with pytest.raises(SystemExit):
        parser.parse_args(["resolve", "secret://env/TOKEN"])
    assert S.REDACTED == "[REDACTED]"
