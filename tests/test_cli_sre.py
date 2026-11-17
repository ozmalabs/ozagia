import json
import os
import sys

from ozagia import cli


def _snapshot(ready=3):
    return json.dumps({
        "schema_version": 1,
        "observed_at": "2026-11-16T10:00:00Z",
        "environment": "staging",
        "service": "api",
        "region": "us-east-1",
        "deployed_images": [{"name": "example/api", "tag": "v2"}],
        "desired_replicas": 3,
        "ready_replicas": ready,
        "target_health": {"status": "healthy"},
    })


def test_cli_iac_check_and_alias(tmp_path, capsys):
    (tmp_path / "main.tf").write_text('resource "aws_vpc" "main" {}\n')
    assert cli.main(["iac", "check", "--root", str(tmp_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["files_checked"] == 1
    assert cli.main(["check", "iac", "--root", str(tmp_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["files_checked"] == 1


def test_cli_ops_record_and_resume(tmp_path, capsys):
    record = {
        "harness": "opencode", "session": "ses_cli", "environment": "staging",
        "kind": "observation", "summary": "ready replicas are healthy",
    }
    assert cli.main(["ops", "record", "--root", str(tmp_path), "--json",
                     "--record", json.dumps(record)]) == 0
    saved = json.loads(capsys.readouterr().out)
    assert saved["session"] == "ses_cli"
    assert cli.main(["ops", "resume", "--root", str(tmp_path),
                     "--environment", "staging", "--json"]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["latest_state"]["summary"] == "ready replicas are healthy"


def test_cli_live_import_diff_and_save(tmp_path, capsys):
    before = tmp_path / "before.json"
    after = tmp_path / "after.json"
    before.write_text(_snapshot(), encoding="utf-8")
    after.write_text(_snapshot(ready=2), encoding="utf-8")
    assert cli.main(["live", "import", "--before-file", str(before),
                     "--provenance", "ci", "--json"]) == 0
    imported = json.loads(capsys.readouterr().out)
    assert imported["snapshot"]["service"] == "api"
    assert cli.main(["live", "diff", "--before-file", str(before),
                     "--after-file", str(after), "--json"]) == 0
    diff = json.loads(capsys.readouterr().out)
    assert diff["differences"][0]["change"] == "changed"
    assert cli.main(["live", "save", "--root", str(tmp_path),
                     "--before-file", str(before), "--provenance", "ci",
                     "--json"]) == 0
    saved = json.loads(capsys.readouterr().out)
    assert (tmp_path / ".ozagia" / "live").is_dir()
    assert saved["path"]


def test_cli_environment_profiles_are_declarative(tmp_path, capsys):
    (tmp_path / ".ozagia.toml").write_text(
        '[environments.staging]\npaths = ["envs/staging"]\nrisk = "low"\n',
        encoding="utf-8")
    assert cli.main(["env", "list", "--root", str(tmp_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["declared"] is True and payload["live"] is False
    assert payload["environments"][0]["name"] == "staging"
    assert cli.main(["env", "show", "staging", "--root", str(tmp_path),
                     "--json"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["environments"][0]["risk"] == "low"


def test_cli_runtime_status_and_foreground_service(tmp_path, capsys):
    # The command names an interpreter under tmp_path, so the checkout's
    # path (its directory names) cannot affect the argv checks.
    python = tmp_path / "bin" / "python"
    python.parent.mkdir()
    python.symlink_to(os.path.realpath(sys.executable))
    (tmp_path / ".ozagia.toml").write_text(
        f"""
[relays.local]
specs = [{{ kind = "local" }}]
allowed_kinds = ["local"]
max_timeout_s = 20

[runtime.services.echo]
command = [{json.dumps(str(python))}, "-c", "print('cli-ok')"]
mode = "foreground"
description = "Echo service"

[runtime.tunnels.database]
relay = "local"
target = "127.0.0.1:5432"
description = "Database tunnel"
""", encoding="utf-8")
    assert cli.main(["runtime", "status", "--root", str(tmp_path),
                     "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["configured"] is True and payload["live"] is False
    assert payload["services"][0]["state"] == "configured"
    assert payload["tunnels"][0]["state"] == "configured"
    assert payload["tunnels"][0]["running"] is False
    assert not (tmp_path / ".ozagia" / "runtime").exists()

    assert cli.main(["service", "run", "echo", "--root", str(tmp_path),
                     "--json"]) == 0
    ran = json.loads(capsys.readouterr().out)
    assert ran["state"] == "exited" and ran["exit_code"] == 0
    assert ran["output"] == "cli-ok\n"
    assert "cli-ok" not in (tmp_path / ".ozagia" / "runtime" /
                            "state.json").read_text()

    assert cli.main(["runtime", "status", "echo", "--root", str(tmp_path),
                     "--json"]) == 0
    updated = json.loads(capsys.readouterr().out)
    assert updated["services"][0]["state"] == "exited"

    assert cli.main(["tunnel", "start", "database", "--root", str(tmp_path),
                     "--json"]) == 2
    assert "--allow-live" in capsys.readouterr().err


def test_cli_job_wait_is_disabled_without_allow_live(tmp_path, capsys):
    assert cli.main([
        "job", "wait", "--provider", "codebuild", "--job-id", "app:123",
        "--region", "us-east-1", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "disabled"
    assert payload["live"] is False


def test_cli_job_wait_uses_injected_live_runner(tmp_path, capsys):
    executable = tmp_path / "aws"
    executable.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' '{\"builds\":[{\"buildId\":\"app:123\","
        "\"buildStatus\":\"SUCCEEDED\"}]}'\n", encoding="utf-8")
    executable.chmod(0o755)
    assert cli.main([
        "job", "wait", "--provider", "codebuild", "--job-id", "app:123",
        "--region", "us-east-1", "--allow-live",
        "--executable", str(executable), "--timeout-s", "5",
        "--poll-s", "0.1", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "succeeded"
    assert payload["live"] is True
    assert payload["last_log_lines"] == ["status=succeeded"]


def _relay_spec(tmp_path, **policy):
    path = tmp_path / "relay.json"
    path.write_text(json.dumps({
        "specs": [{"kind": "local"}],
        "policy": {"allowed_kinds": ["local"], "max_timeout_s": 30, **policy},
    }), encoding="utf-8")
    return path


def test_cli_relay_plan_and_live_local_run(tmp_path, capsys):
    spec = _relay_spec(tmp_path, command_allowlist=[["printf"]])
    assert cli.main(["relay", "plan", "--spec", str(spec), "--", "printf", "hi"]) == 0
    assert "printf hi" in capsys.readouterr().out
    assert cli.main(["relay", "run", "--spec", str(spec), "--", "printf", "hi"]) == 2
    assert "requires --allow-live" in capsys.readouterr().err
    assert cli.main([
        "relay", "run", "--spec", str(spec), "--allow-live", "--timeout-s", "10",
        "--", "printf", "relay-ok"]) == 0
    assert "relay-ok" in capsys.readouterr().out


def test_cli_relay_tunnel_dry_run(tmp_path, capsys):
    spec = _relay_spec(tmp_path)
    assert cli.main([
        "relay", "tunnel", "--spec", str(spec), "--target",
        "127.0.0.1:9000", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["dry_run"] is True
    assert payload["target_port"] == 9000


def test_cli_preflight_plan_is_dry_run(tmp_path, capsys):
    root = tmp_path / "root"
    root.mkdir()
    assert cli.main([
        "preflight", "plan", "--root", str(root),
        "--emulator-bin", "/opt/ozagia/emulator-1.0.0", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["execute"] is False
    assert payload["roots"] == [str(root.resolve())]
    assert payload["environment"]["AWS_EC2_METADATA_DISABLED"] == "true"
