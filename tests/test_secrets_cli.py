"""CLI tests for secret inspection commands (never a resolved value)."""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

from ozagia import secrets_cli as CLI
from ozagia.core import secrets as S
from ozagia.daemon.jobcli import JobUnavailable


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


def test_cli_has_capture_commands_but_no_resolve_or_show_command() -> None:
    parser = CLI.build_parser()
    assert parser.parse_args(["capture-text"]).command == "capture-text"
    assert parser.parse_args(["capture"]).command == "capture"
    with pytest.raises(SystemExit):
        parser.parse_args(["capture", "a-value-in-argv"])
    with pytest.raises(SystemExit):
        parser.parse_args(["resolve", "secret://env/TOKEN"])
    with pytest.raises(SystemExit):
        parser.parse_args(["show", "secret://env/TOKEN"])
    assert S.REDACTED == "[REDACTED]"


def _fake_request(calls: list[dict], reply: dict):
    def request(root, op, fields, **kwargs):
        calls.append({"root": Path(root), "op": op, "fields": dict(fields)})
        return dict(reply)
    return request


def test_cli_capture_text_uses_stdin_and_daemon(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    secret = "sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789"
    text = f'api_key = "{secret}"\n'
    calls: list[dict] = []
    reply = {"ok": True, "text": 'api_key = "secret://session/token"\n',
             "secrets": [{"ref": "secret://session/token",
                          "fingerprint": "sha256:abc", "label": "api_key",
                          "start": 0, "end": 0, "kind": "anthropic_key",
                          "captured": True}],
             "detected": 1, "captured": 1, "withheld": 0,
             "truncated": False, "not_assessed": []}
    monkeypatch.setattr("ozagia.daemon.jobcli.request",
                        _fake_request(calls, reply))
    monkeypatch.setattr(sys, "stdin", io.StringIO(text))
    assert CLI.main(["capture-text", "--root", str(tmp_path),
                     "--session", "s1", "--harness", "opencode"]) == 0
    out, err = capsys.readouterr()
    assert secret not in out and secret not in err
    assert "secret://session/token" in out
    assert calls[0]["op"] == "secrets"
    assert calls[0]["fields"]["action"] == "capture_text"
    assert calls[0]["fields"]["text"] == text
    assert calls[0]["fields"]["session"] == "s1"

    monkeypatch.setattr(sys, "stdin", io.StringIO(text))
    assert CLI.main(["capture-text", "--root", str(tmp_path),
                     "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["captured"] == 1
    assert secret not in json.dumps(payload)


def test_cli_capture_text_fails_closed_without_a_daemon(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    secret = "sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789"
    text = f'api_key = "{secret}"\n'

    def unavailable(*args, **kwargs):
        raise JobUnavailable("no daemon answers")

    monkeypatch.setattr("ozagia.daemon.jobcli.request", unavailable)
    monkeypatch.setattr(sys, "stdin", io.StringIO(text))
    assert CLI.main(["capture-text", "--root", str(tmp_path)]) == 2
    out, err = capsys.readouterr()
    assert secret not in out and secret not in err
    assert "[REDACTED]" in out
    assert "redacted" in err
    assert "api_key" in out


def test_cli_capture_reads_one_value_from_stdin_only(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    value = "captured-value-1234"
    calls: list[dict] = []
    reply = {"ok": True, "ref": "secret://session/token",
             "fingerprint": "sha256:abc", "label": None,
             "created": 1.0, "expires": 2.0}
    monkeypatch.setattr("ozagia.daemon.jobcli.request",
                        _fake_request(calls, reply))
    monkeypatch.setattr(sys, "stdin", io.StringIO(value + "\n"))
    assert CLI.main(["capture", "--root", str(tmp_path),
                     "--session", "s1", "--label", "cli token",
                     "--ttl", "60"]) == 0
    out, err = capsys.readouterr()
    assert value not in out and value not in err
    assert "secret://session/token" in out
    assert "sha256:abc" in out
    fields = calls[0]["fields"]
    assert fields["action"] == "capture"
    assert fields["value"] == value
    assert fields["label"] == "cli token" and fields["ttl_s"] == 60.0

    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    assert CLI.main(["capture", "--root", str(tmp_path)]) == 2
    assert "non-empty" in capsys.readouterr().err


def test_cli_capture_and_list_are_status_only_without_a_daemon(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    def unavailable(*args, **kwargs):
        raise JobUnavailable("no daemon answers")

    monkeypatch.setattr("ozagia.daemon.jobcli.request", unavailable)
    monkeypatch.setattr(sys, "stdin", io.StringIO("value-1234\n"))
    assert CLI.main(["capture", "--root", str(tmp_path)]) == 2
    out, err = capsys.readouterr()
    assert "value-1234" not in out and "value-1234" not in err
    assert "no daemon" in err
    assert CLI.main(["list", "--root", str(tmp_path),
                     "--session", "s1"]) == 2
    out, err = capsys.readouterr()
    assert out == "" and "no daemon" in err


def test_cli_list_forget_and_check_session_use_the_daemon(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    calls: list[dict] = []
    listed = {"ok": True, "count": 1, "storage": "memory-only", "secrets": [{
        "ref": "secret://session/token", "fingerprint": "sha256:abc",
        "label": "cli", "created": 1.0, "expires": 2.0}]}
    monkeypatch.setattr("ozagia.daemon.jobcli.request",
                        _fake_request(calls, listed))
    assert CLI.main(["list", "--root", str(tmp_path),
                     "--session", "s1"]) == 0
    out = capsys.readouterr().out
    assert "secret://session/token" in out and "sha256:abc" in out
    assert calls[0]["fields"]["action"] == "list"

    forgotten = {"ok": True, "forgotten": True,
                 "ref": "secret://session/token"}
    monkeypatch.setattr("ozagia.daemon.jobcli.request",
                        _fake_request(calls, forgotten))
    assert CLI.main(["forget", "secret://session/token", "--root",
                     str(tmp_path), "--session", "s1"]) == 0
    assert "forgotten" in capsys.readouterr().out
    assert calls[-1]["fields"]["action"] == "forget"

    checks = {"ok": True, "checks": [{
        "reference": "secret://session/token", "status": "available",
        "provider": "session", "code": None, "detail": "session secret"}]}
    monkeypatch.setattr("ozagia.daemon.jobcli.request",
                        _fake_request(calls, checks))
    assert CLI.main(["check", "secret://session/token", "--root",
                     str(tmp_path), "--session", "s1"]) == 0
    out = capsys.readouterr().out
    assert "available" in out and "secret://session/token" in out
    assert calls[-1]["fields"]["action"] == "check"
