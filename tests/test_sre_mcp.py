import json
from pathlib import Path

from ozagia.integrations.mcp.server import DevMemoryServer
from ozagia.integrations.mcp.sre import sre_tools


class Memory:
    def __init__(self, root):
        self.raw_repo_root = str(root)
        self.nodes = {}


def tools(root):
    return {spec["name"]: handler for spec, handler in sre_tools(Memory(root))}


def snapshot(service="api", ready=3):
    return json.dumps({
        "schema_version": 1,
        "observed_at": "2026-11-16T10:00:00Z",
        "environment": "staging",
        "service": service,
        "region": "us-east-1",
        "deployed_images": [{"name": "example/api", "tag": "v2"}],
        "desired_replicas": 3,
        "ready_replicas": ready,
        "target_health": {"status": "healthy"},
    })


def test_offline_sre_tools_import_ops_and_live_artifacts(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    handlers = tools(root)

    ops = handlers["dev_ops_record"]({
        "record": {
            "harness": "opencode", "session": "ses_test", "environment": "staging",
            "kind": "observation", "summary": "ready replicas are healthy",
            "findings": ["three ready"],
        }})
    assert ops["ok"] is True and ops["record"]["schema_version"] == 1
    resume = handlers["dev_ops_resume"]({"environment": "staging"})
    assert resume["ok"] is True
    assert resume["latest_state"]["summary"] == "ready replicas are healthy"

    imported = handlers["dev_live_import"]({
        "snapshot": snapshot(), "provenance": "ci-artifact"})
    assert imported["ok"] is True
    diff = handlers["dev_live_diff"]({
        "before": snapshot(), "after": snapshot(ready=2),
        "provenance": "artifact"})
    assert diff["ok"] is True
    assert tuple(diff["differences"][0]["changed_fields"]) == ("ready_replicas",)
    saved = handlers["dev_live_save"]({
        "snapshot": snapshot(), "provenance": "ci-artifact"})
    assert saved["ok"] is True
    assert Path(saved["path"]).is_file()
    assert "example/api" not in repr(diff)


def test_environment_profiles_are_declarative_only(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".ozagia.toml").write_text(
        '[environments.staging]\npaths = ["envs/staging"]\n'
        'namespaces = ["staging"]\nrisk = "low"\n', encoding="utf-8")
    result = tools(root)["dev_environments"]({})
    assert result["ok"] is True and result["live"] is False
    assert result["environments"][0]["name"] == "staging"
    assert result["environments"][0]["risk"] == "low"


def test_aws_cli_check_uses_injected_model_result(monkeypatch, tmp_path):
    import ozagia.aws_cli as aws_module

    monkeypatch.setattr(
        aws_module, "validate_aws_command",
        lambda argv: aws_module.AwsValidationResult(
            True, False, "unknown_operation", service="s3"))
    handlers = tools(tmp_path)
    result = handlers["dev_aws_cli_check"]({"argv": ["aws", "s3", "nope"]})
    assert result["ok"] is True
    assert result["valid"] is False
    assert result["service"] == "s3"


def test_iac_check_and_preflight_are_supported_without_live_calls(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "main.tf").write_text('resource "aws_vpc" "main" {}\n')
    handlers = tools(root)
    checked = handlers["dev_check_iac"]({})
    assert checked["ok"] is True
    assert checked["live"] is False
    assert checked["status"] in {"clean", "findings"}

    emulator = "/opt/ozagia/emulator-1.0.0"
    plan = handlers["dev_preflight_plan"]({
        "roots": [str(root)], "emulator_bin": emulator})
    assert plan["ok"] is True
    assert plan["execute"] is False
    assert plan["roots"] == [str(root.resolve())]
    assert any("AWS_EC2_METADATA_DISABLED" == key
               for key in plan["environment"])

    escaped = handlers["dev_preflight_plan"]({
        "roots": ["../outside"], "emulator_bin": emulator})
    assert escaped["ok"] is False


def test_mcp_server_registers_offline_sre_tools(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    server = DevMemoryServer(Memory(root))
    assert {"dev_environments", "dev_check_iac", "dev_ops_record", "dev_ops_resume",
            "dev_live_import", "dev_live_diff", "dev_live_save",
            "dev_aws_cli_check", "dev_preflight_plan"} <= set(server._handlers)
    assert server._schemas["dev_ops_record"]["additionalProperties"] is False
