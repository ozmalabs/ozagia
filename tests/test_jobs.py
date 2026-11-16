from __future__ import annotations

import json

import pytest

from ozagia.jobs import (
    JobSpec,
    JobSpecError,
    job_policy_from_config,
    wait_for_job,
)


def _policy(provider: str = "ssm"):
    executable = "kubectl" if provider == "kubectl" else "aws"
    return job_policy_from_config({
        "allowed_providers": [provider],
        "executables": {provider: executable},
        "max_timeout_s": 30,
    })


def _ssm_spec() -> JobSpec:
    return JobSpec(
        provider="ssm",
        kind="command_invocation",
        job_id="cmd-123",
        instance_id="i-123",
        region="us-east-1",
    )


def test_success_and_only_normalized_status_is_returned() -> None:
    calls = []

    def runner(argv, timeout_s):
        calls.append((argv, timeout_s))
        return 0, json.dumps({"Status": "Success", "StandardOutputContent": "private"}), ""

    result = wait_for_job(_ssm_spec(), runner=runner, policy=_policy())

    assert result.status == "succeeded"
    assert result.terminal_phase == "succeeded"
    assert result.attempts == 1
    assert result.last_log_lines == ("status=succeeded",)
    assert result.source == "read-only polling command"
    assert calls[0][0][:3] == ["aws", "ssm", "get-command-invocation"]
    assert "--region" in calls[0][0]


def test_terminal_failure() -> None:
    result = wait_for_job(
        _ssm_spec(),
        runner=lambda _argv, _timeout: (0, '{"Status":"Failed"}', ""),
        policy=_policy(),
    )
    assert result.status == "failed"
    assert result.terminal_phase == "failed"


def test_timeout_returns_non_terminal_result_with_exponential_polling() -> None:
    now = [0.0]
    sleeps = []
    statuses = iter([
        '{"Status":"InProgress"}',
        '{"Status":"InProgress"}',
        '{"Status":"InProgress"}',
    ])

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    result = wait_for_job(
        _ssm_spec(),
        runner=lambda _argv, _timeout: (0, next(statuses), ""),
        policy=_policy(),
        timeout_s=7,
        poll_interval_s=1,
        clock=lambda: now[0],
        sleep=sleep,
    )

    assert result.status == "timed_out"
    assert result.terminal_phase is None
    assert result.attempts == 3
    assert sleeps == [1, 2, 4]


def test_malformed_output_is_not_echoed() -> None:
    result = wait_for_job(
        _ssm_spec(),
        runner=lambda _argv, _timeout: (0, "credential-canary malformed", ""),
        policy=_policy(),
    )
    assert result.status == "malformed"
    assert "credential-canary" not in repr(result)


def test_policy_defaults_disabled_and_disallowed_provider_never_runs() -> None:
    calls = []
    disabled = wait_for_job(
        _ssm_spec(),
        runner=lambda *args: calls.append(args),
    )
    disallowed = wait_for_job(
        _ssm_spec(),
        runner=lambda *args: calls.append(args),
        policy=_policy("codebuild"),
    )
    assert disabled.status == "disabled"
    assert disallowed.status == "disabled"
    assert not calls


def test_extra_args_are_rejected() -> None:
    spec = JobSpec(
        provider="ssm",
        kind="command_invocation",
        job_id="cmd-123",
        instance_id="i-123",
        region="us-east-1",
        args=("--query", "secret"),
    )
    with pytest.raises(JobSpecError, match="extra arguments"):
        wait_for_job(spec, runner=lambda *_: (0, "{}", ""), policy=_policy())


def test_cancellation_before_first_attempt() -> None:
    calls = []
    result = wait_for_job(
        _ssm_spec(),
        runner=lambda *args: calls.append(args),
        policy=_policy(),
        cancel=lambda: True,
    )
    assert result.status == "cancelled"
    assert result.attempts == 0
    assert not calls


def test_credential_canary_is_not_in_any_result_field() -> None:
    canary = "AKIAIOSFODNN7EXAMPLE"
    now = [0.0]

    def sleep(seconds):
        now[0] += seconds

    result = wait_for_job(
        _ssm_spec(),
        runner=lambda _argv, _timeout: (
            0,
            json.dumps({"Status": "InProgress", "StandardOutputContent": canary}),
            canary,
        ),
        policy=_policy(),
        timeout_s=1,
        poll_interval_s=2,
        clock=lambda: now[0],
        sleep=sleep,
    )
    assert result.status == "timed_out"
    assert canary not in repr(result)


def test_codebuild_and_pipeline_use_reviewed_shapes() -> None:
    build = JobSpec(
        provider="codebuild",
        kind="build",
        job_id="project:build-1",
        region="us-west-2",
    )
    build_result = wait_for_job(
        build,
        runner=lambda argv, _timeout: (
            0,
            json.dumps({"builds": [{"buildId": "project:build-1", "buildStatus": "SUCCEEDED"}]}),
            "",
        ),
        policy=_policy("codebuild"),
    )
    assert build_result.status == "succeeded"

    pipeline = JobSpec(
        provider="codepipeline",
        kind="execution",
        job_id="execution-1",
        resource="pipeline-1",
        region="us-west-2",
    )
    now = [0.0]
    pipeline_result = wait_for_job(
        pipeline,
        runner=lambda argv, _timeout: (
            0, '{"pipelineExecution":{"status":"InProgress"}}', "",
        ),
        policy=_policy("codepipeline"),
        timeout_s=1,
        poll_interval_s=2,
        clock=lambda: now[0],
        sleep=lambda seconds: now.__setitem__(0, now[0] + seconds),
    )
    assert pipeline_result.status == "timed_out"


def test_kubectl_rollout_success_is_read_only_and_bounded() -> None:
    seen = []

    def runner(argv, _timeout):
        seen.extend(argv)
        return 0, 'deployment "api" successfully rolled out', ""

    result = wait_for_job(
        JobSpec(
            provider="kubectl",
            kind="rollout_status",
            job_id="api",
            namespace="staging",
        ),
        runner=runner,
        policy=_policy("kubectl"),
    )
    assert result.status == "succeeded"
    assert seen[:3] == ["kubectl", "rollout", "status"]
    assert "--watch=false" in seen
    assert not any(arg in {"apply", "delete", "patch"} for arg in seen)


def test_output_line_limit_is_enforced() -> None:
    now = [0.0]
    count = [0]

    def runner(_argv, _timeout):
        count[0] += 1
        return 0, '{"Status":"InProgress"}', ""

    def sleep(seconds):
        now[0] += seconds

    result = wait_for_job(
        _ssm_spec(),
        runner=runner,
        policy=_policy(),
        timeout_s=10,
        poll_interval_s=1,
        max_output_lines=2,
        clock=lambda: now[0],
        sleep=sleep,
    )
    assert result.attempts > 2
    assert result.last_log_lines == ("status=running", "status=running")


def test_oversized_provider_output_is_rejected_without_retention() -> None:
    canary = "credential-canary"
    result = wait_for_job(
        _ssm_spec(),
        runner=lambda _argv, _timeout: (0, '{"Status":"InProgress"}' + canary * 5_000, ""),
        policy=_policy(),
    )
    assert result.status == "malformed"
    assert canary not in repr(result)


def test_policy_config_rejects_unknown_provider_and_exec_shape() -> None:
    with pytest.raises(ValueError, match="unknown job provider"):
        job_policy_from_config({
            "allowed_providers": ["shell"],
            "executables": {"shell": "sh"},
            "max_timeout_s": 10,
        })
    with pytest.raises(ValueError, match="reviewed provider"):
        job_policy_from_config({
            "allowed_providers": ["ssm"],
            "executables": {"ssm": "aws --profile production"},
            "max_timeout_s": 10,
        })
