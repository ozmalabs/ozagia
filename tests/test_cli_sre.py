import json

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
