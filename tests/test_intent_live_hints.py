import json

from ozagia.intent.live_hints import aws_cli_hint, hints, job_hint


def test_ssm_start_hint_contains_ids_only():
    output = json.dumps({"Command": {"CommandId": "cmd-123", "InstanceIds": ["i-abc"]}})
    hint = job_hint(
        "aws ssm send-command --instance-ids i-abc --region us-east-1 "
        "--document-name AWS-RunShellScript --parameters commands='hostname'",
        output,
    )
    assert hint is not None
    assert "cmd-123" in hint and "i-abc" in hint
    assert "hostname" not in hint
    assert "--region us-east-1" in hint


def test_codebuild_and_codepipeline_start_hints():
    build = job_hint(
        "aws codebuild start-build --project-name app --region us-east-1",
        json.dumps({"build": {"id": "app:1234"}}),
    )
    assert build and "app:1234" in build and "--provider codebuild" in build
    pipeline = job_hint(
        "aws codepipeline start-pipeline-execution --pipeline-name deploy "
        "--region us-east-1",
        json.dumps({"pipelineExecutionId": "exec-123"}),
    )
    assert pipeline and "deploy" in pipeline and "exec-123" in pipeline


def test_job_hint_rejects_malformed_or_unsafe_values_and_output():
    assert job_hint(
        "aws ssm send-command --region us-east-1",
        json.dumps({"Command": {"CommandId": "bad id", "InstanceIds": ["i-1"]}}),
    ) is None
    assert job_hint("aws ssm send-command", "not json") is None
    assert job_hint("kubectl rollout status deployment/x", "{}") is None
    assert job_hint("echo hi", "{}") is None


def test_aws_cli_hint_does_not_echo_arguments(monkeypatch):
    import ozagia.aws_cli as module

    monkeypatch.setattr(
        module, "validate_aws_command",
        lambda argv: module.AwsValidationResult(
            True, False, "unknown_operation", service="s3"))
    hint = aws_cli_hint("aws s3 definitely-not-an-operation --secret canary")
    assert hint is not None
    assert "unknown operation" in hint
    assert "canary" not in hint
    assert "definitely-not-an-operation" not in hint


def test_hints_are_deduplicated_and_safe_for_unrelated_commands():
    assert hints("pytest -q", "1 passed") == []
    assert hints("aws s3 ls", "{}") == []


def test_intent_after_appends_job_and_aws_hints(monkeypatch):
    from ozagia import intent
    import ozagia.aws_cli as aws_module

    output = json.dumps({"Command": {"CommandId": "cmd-1",
                                     "InstanceIds": ["i-1"]}})
    result = intent.after(
        None, "shell",
        {"command": "aws ssm send-command --instance-ids i-1 --region us-east-1"},
        output)
    assert "job started" in result["append"]
    assert "cmd-1" in result["append"]
    assert result["meta"]["live_hints"] == 1

    monkeypatch.setattr(
        aws_module, "validate_aws_command",
        lambda argv: aws_module.AwsValidationResult(
            True, False, "unknown_operation", service="s3"))
    result = intent.after(
        None, "shell", {"command": "aws s3 not-real --secret canary"}, "{}")
    assert "aws cli check" in result["append"]
    assert "canary" not in result["append"]
