"""Bounded, allowlisted polling for explicitly named infrastructure jobs.

This module does not launch commands itself. Callers inject a runner that
accepts an argv list and a timeout; default policy is disabled.
"""
from __future__ import annotations

import json
import math
import os
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

ProviderRunner = Callable[[list[str], float], tuple[int, str, str]]
Clock = Callable[[], float]
Sleep = Callable[[float], None]

_PROVIDER_KIND = {
    "ssm": "command_invocation",
    "codebuild": "build",
    "codepipeline": "execution",
    "kubectl": "rollout_status",
}
_EXPECTED_EXECUTABLES = {
    "ssm": "aws",
    "codebuild": "aws",
    "codepipeline": "aws",
    "kubectl": "kubectl",
}
_MAX_TIMEOUT_S = 86_400.0
_MAX_POLL_INTERVAL_S = 30.0
_MAX_OUTPUT_LINES = 50
_MAX_OUTPUT_BYTES = 2_048
_MAX_PROVIDER_OUTPUT_BYTES = 64 * 1_024
_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+=,-]{0,255}$")
_KUBE_NAME = re.compile(r"^[a-z0-9](?:[-a-z0-9.]{0,251}[a-z0-9])?$")
_REGION = re.compile(r"^[a-z]{2}(?:-[a-z]+)+-\d+$")


class JobSpecError(ValueError):
    """A job specification does not match a reviewed read-only command shape."""


class JobConfigError(ValueError):
    """The job allowlist configuration is malformed."""


@dataclass(frozen=True)
class JobSpec:
    """One job identifier and the reviewed fields needed to poll it.

    `args` exists to make attempts to add arbitrary arguments explicit; it
    must remain empty. `job_id` is the command ID, build ID, execution ID, or
    Kubernetes deployment name, depending on provider and kind.
    """

    provider: str
    kind: str
    job_id: str
    resource: str | None = None
    instance_id: str | None = None
    region: str | None = None
    namespace: str | None = None
    args: tuple[str, ...] = ()


@dataclass(frozen=True)
class JobResult:
    """Small, safe summary of a wait; raw provider output is never included."""

    status: str
    terminal_phase: str | None
    elapsed_s: float
    attempts: int
    last_log_lines: tuple[str, ...]
    source: str
    provider: str
    evidence: str


@dataclass(frozen=True)
class JobPolicy:
    """Explicit provider/executable allowlist; an empty policy is disabled."""

    allowed_providers: frozenset[str] = frozenset()
    executables: Mapping[str, str] = field(default_factory=dict)
    max_timeout_s: float = 0.0

    def allows(self, provider: str) -> bool:
        """Whether this policy enables the reviewed executable for a provider."""
        executable = self.executables.get(provider)
        return (
            provider in self.allowed_providers
            and isinstance(executable, str)
            and os.path.basename(executable) == _EXPECTED_EXECUTABLES.get(provider)
            and self.max_timeout_s > 0
        )


def job_policy_from_config(config: Mapping[str, Any] | None = None) -> JobPolicy:
    """Parse the strict job allowlist shape.

    Expected keys are `allowed_providers` (a list), `executables` (provider to
    executable string), and `max_timeout_s`. Missing values are empty/disabled.
    No provider is enabled by default.
    """
    if config is None:
        config = {}
    if not isinstance(config, Mapping):
        raise JobConfigError("jobs config must be a mapping")

    unknown = set(config) - {"allowed_providers", "executables", "max_timeout_s"}
    if unknown:
        raise JobConfigError("unknown jobs config key")

    providers_value = config.get("allowed_providers", ())
    executables_value = config.get("executables", {})
    max_timeout_value = config.get("max_timeout_s", 0)

    if (
        isinstance(providers_value, (str, bytes))
        or not isinstance(providers_value, (list, tuple, set, frozenset))
        or any(not isinstance(item, str) for item in providers_value)
    ):
        raise JobConfigError("allowed_providers must be a list of provider names")
    providers = frozenset(providers_value)
    if providers - _PROVIDER_KIND.keys():
        raise JobConfigError("unknown job provider")

    if not isinstance(executables_value, Mapping):
        raise JobConfigError("executables must be a provider-to-executable mapping")
    executables: dict[str, str] = {}
    for provider, executable in executables_value.items():
        if provider not in _PROVIDER_KIND:
            raise JobConfigError("unknown executable provider")
        if (
            not isinstance(executable, str)
            or not executable
            or any(char.isspace() or ord(char) < 32 for char in executable)
            or os.path.basename(executable) != _EXPECTED_EXECUTABLES[provider]
        ):
            raise JobConfigError("executable does not match the reviewed provider")
        executables[provider] = executable

    if set(executables) - providers or providers - set(executables):
        raise JobConfigError("each allowed provider must have exactly one executable")
    if (
        isinstance(max_timeout_value, bool)
        or not isinstance(max_timeout_value, (int, float))
        or not math.isfinite(max_timeout_value)
        or max_timeout_value < 0
        or max_timeout_value > _MAX_TIMEOUT_S
    ):
        raise JobConfigError("max_timeout_s must be between 0 and 86400")
    if providers and max_timeout_value <= 0:
        raise JobConfigError("an enabled provider requires a positive max_timeout_s")

    return JobPolicy(
        allowed_providers=providers,
        executables=executables,
        max_timeout_s=float(max_timeout_value),
    )


def _validate_spec(spec: JobSpec) -> None:
    if not isinstance(spec, JobSpec):
        raise JobSpecError("spec must be a JobSpec")
    expected_kind = _PROVIDER_KIND.get(spec.provider)
    if expected_kind is None:
        raise JobSpecError("unknown provider")
    if spec.kind != expected_kind:
        raise JobSpecError("unknown kind for provider")
    if spec.args:
        raise JobSpecError("extra arguments are not supported")
    if not isinstance(spec.job_id, str) or not _TOKEN.fullmatch(spec.job_id):
        raise JobSpecError("job_id must be a bounded identifier")

    if spec.provider in {"ssm", "codebuild", "codepipeline"}:
        if not isinstance(spec.region, str) or not _REGION.fullmatch(spec.region):
            raise JobSpecError("AWS jobs require an explicit region")
        if spec.namespace is not None:
            raise JobSpecError("namespace is only valid for kubectl")
    elif spec.region is not None:
        raise JobSpecError("region is only valid for AWS jobs")

    if spec.provider == "ssm":
        if not isinstance(spec.instance_id, str) or not _TOKEN.fullmatch(spec.instance_id):
            raise JobSpecError("SSM invocation requires an instance_id")
        if spec.resource is not None or spec.namespace is not None:
            raise JobSpecError("unexpected SSM job field")
    elif spec.provider == "codebuild":
        if spec.resource is not None or spec.instance_id is not None:
            raise JobSpecError("unexpected CodeBuild job field")
    elif spec.provider == "codepipeline":
        if not isinstance(spec.resource, str) or not _TOKEN.fullmatch(spec.resource):
            raise JobSpecError("CodePipeline execution requires a pipeline name")
        if spec.instance_id is not None:
            raise JobSpecError("unexpected CodePipeline job field")
    elif spec.provider == "kubectl":
        if (
            not isinstance(spec.namespace, str)
            or not _KUBE_NAME.fullmatch(spec.namespace)
            or not _KUBE_NAME.fullmatch(spec.job_id)
        ):
            raise JobSpecError("kubectl requires a deployment name and namespace")
        if spec.resource is not None or spec.instance_id is not None:
            raise JobSpecError("unexpected kubectl job field")


def _command(spec: JobSpec, executable: str) -> list[str]:
    if spec.provider == "ssm":
        argv = [
            executable, "ssm", "get-command-invocation",
            "--command-id", spec.job_id,
            "--instance-id", spec.instance_id or "",
            "--output", "json",
            "--no-cli-pager",
        ]
    elif spec.provider == "codebuild":
        argv = [
            executable, "codebuild", "batch-get-builds",
            "--ids", spec.job_id,
            "--output", "json",
            "--no-cli-pager",
        ]
    elif spec.provider == "codepipeline":
        argv = [
            executable, "codepipeline", "get-pipeline-execution",
            "--pipeline-name", spec.resource or "",
            "--pipeline-execution-id", spec.job_id,
            "--output", "json",
            "--no-cli-pager",
        ]
    else:
        # A one-shot, non-following status check. `runner` enforces the
        # remaining deadline as its process timeout as well.
        argv = [
            executable, "rollout", "status", f"deployment/{spec.job_id}",
            "--namespace", spec.namespace or "",
            "--watch=false",
            "--timeout=1s",
        ]
    if spec.region is not None:
        argv.extend(["--region", spec.region])
    return argv


def _json_status(spec: JobSpec, stdout: str) -> tuple[str, str | None] | None:
    try:
        payload = json.loads(stdout)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None

    state: Any
    if spec.provider == "ssm":
        state = payload.get("Status")
        mapping = {
            "Pending": ("running", None),
            "InProgress": ("running", None),
            "Delayed": ("running", None),
            "Cancelling": ("running", None),
            "Success": ("succeeded", "succeeded"),
            "Failed": ("failed", "failed"),
            "TimedOut": ("failed", "timed_out"),
            "Cancelled": ("cancelled", "cancelled"),
        }
    elif spec.provider == "codebuild":
        builds = payload.get("builds")
        if not isinstance(builds, list) or len(builds) != 1 or not isinstance(builds[0], dict):
            return None
        if builds[0].get("buildId") != spec.job_id:
            return None
        state = builds[0].get("buildStatus")
        mapping = {
            "IN_PROGRESS": ("running", None),
            "SUCCEEDED": ("succeeded", "succeeded"),
            "FAILED": ("failed", "failed"),
            "FAULT": ("failed", "fault"),
            "STOPPED": ("cancelled", "cancelled"),
        }
    elif spec.provider == "codepipeline":
        execution = payload.get("pipelineExecution")
        if not isinstance(execution, dict):
            return None
        state = execution.get("status")
        mapping = {
            "InProgress": ("running", None),
            "Stopping": ("running", None),
            "Succeeded": ("succeeded", "succeeded"),
            "Failed": ("failed", "failed"),
            "Superseded": ("failed", "superseded"),
            "Cancelled": ("cancelled", "cancelled"),
            "Stopped": ("cancelled", "cancelled"),
        }
    else:
        return None

    if not isinstance(state, str):
        return None
    return mapping.get(state)


def _kubectl_status(returncode: int, stdout: str, stderr: str) -> tuple[str, str | None] | None:
    # Only inspect output to classify known status phrases. Never copy it into
    # the result, because kubectl diagnostics can contain arbitrary data.
    text = f"{stdout}\n{stderr}".lower()
    if "successfully rolled out" in text and returncode == 0:
        return "succeeded", "succeeded"
    if any(phrase in text for phrase in (
        "waiting for deployment",
        "waiting for rollout",
        "timed out waiting for the condition",
        "timed out waiting for",
    )):
        return "running", None
    if any(phrase in text for phrase in (
        "exceeded its progress deadline",
        "not found",
        "forbidden",
        "unauthorized",
        "no matching resources found",
    )):
        return "failed", "failed"
    return None


def _result(
    status: str,
    phase: str | None,
    started: float,
    clock: Clock,
    attempts: int,
    observations: list[str],
    max_output_lines: int,
    provider: str,
) -> JobResult:
    lines = observations[-max_output_lines:] if max_output_lines else []
    # A second cap keeps aggregate output bounded even if the limit changes.
    while lines and sum(len(line.encode("utf-8")) for line in lines) > _MAX_OUTPUT_BYTES:
        lines.pop(0)
    safe_elapsed = max(0.0, clock() - started)
    return JobResult(
        status=status,
        terminal_phase=phase,
        elapsed_s=round(safe_elapsed, 3),
        attempts=attempts,
        last_log_lines=tuple(lines),
        source="read-only polling command",
        provider=provider,
        evidence=f"{provider} status response",
    )


def wait_for_job(
    spec: JobSpec,
    *,
    runner: ProviderRunner,
    timeout_s: float = 300.0,
    poll_interval_s: float = 2.0,
    max_output_lines: int = 10,
    clock: Clock = time.monotonic,
    sleep: Sleep = time.sleep,
    cancel: Callable[[], bool] | None = None,
    policy: JobPolicy | Mapping[str, Any] | None = None,
) -> JobResult:
    """Poll one reviewed job using an injected argv runner and bounded waits.

    `policy` must explicitly allow the provider. `runner` must honor its
    per-call timeout; this function also checks a monotonic hard deadline
    before every call and after it returns. Output observations contain only
    normalized statuses, never provider stdout/stderr.
    """
    _validate_spec(spec)
    if isinstance(policy, Mapping) or policy is None:
        policy = job_policy_from_config(policy)
    if not isinstance(policy, JobPolicy):
        raise TypeError("policy must be a JobPolicy or jobs configuration mapping")
    if not callable(runner):
        raise TypeError("runner must be callable")
    if (
        isinstance(timeout_s, bool)
        or not isinstance(timeout_s, (int, float))
        or not math.isfinite(timeout_s)
        or timeout_s <= 0
    ):
        raise ValueError("timeout_s must be a finite positive number")
    if (
        isinstance(poll_interval_s, bool)
        or not isinstance(poll_interval_s, (int, float))
        or not math.isfinite(poll_interval_s)
        or poll_interval_s <= 0
    ):
        raise ValueError("poll_interval_s must be a finite positive number")
    if (
        isinstance(max_output_lines, bool)
        or not isinstance(max_output_lines, int)
        or not 0 <= max_output_lines <= _MAX_OUTPUT_LINES
    ):
        raise ValueError("max_output_lines must be between 0 and 50")

    started = clock()
    observations: list[str] = []

    def done(status: str, phase: str | None, attempts: int) -> JobResult:
        return _result(
            status, phase, started, clock, attempts, observations,
            max_output_lines, spec.provider,
        )

    if not policy.allows(spec.provider):
        return done("disabled", None, 0)
    if timeout_s > policy.max_timeout_s:
        timeout_s = policy.max_timeout_s

    deadline = started + float(timeout_s)
    executable = policy.executables[spec.provider]
    backoff = min(float(poll_interval_s), _MAX_POLL_INTERVAL_S)
    attempts = 0

    while True:
        if cancel is not None and cancel():
            return done("cancelled", None, attempts)
        remaining = deadline - clock()
        if remaining <= 0:
            return done("timed_out", None, attempts)

        argv = _command(spec, executable)
        try:
            returncode, stdout, stderr = runner(argv, remaining)
        except Exception:
            return done("provider_error", None, attempts + 1)
        attempts += 1
        if (
            isinstance(returncode, bool)
            or not isinstance(returncode, int)
            or not isinstance(stdout, str)
            or not isinstance(stderr, str)
            or len(stdout) > _MAX_PROVIDER_OUTPUT_BYTES
            or len(stderr) > _MAX_PROVIDER_OUTPUT_BYTES
        ):
            return done("malformed", None, attempts)
        if clock() >= deadline:
            return done("timed_out", None, attempts)

        if spec.provider == "kubectl":
            observed = _kubectl_status(returncode, stdout, stderr)
        elif returncode == 0:
            observed = _json_status(spec, stdout)
        else:
            observed = None
        if observed is None:
            return done("malformed", None, attempts)
        status, phase = observed
        observations.append(f"status={phase or status}")

        if status in {"succeeded", "failed", "cancelled"}:
            return done(status, phase, attempts)
        if clock() >= deadline:
            return done("timed_out", None, attempts)
        if cancel is not None and cancel():
            return done("cancelled", None, attempts)

        remaining = deadline - clock()
        if remaining <= 0:
            return done("timed_out", None, attempts)
        sleep(min(backoff, remaining))
        backoff = min(backoff * 2, _MAX_POLL_INTERVAL_S)
