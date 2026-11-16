"""Small, bounded hints after shell commands: AWS CLI shape and job IDs.

Hints are advisory text appended after the built-in already ran. This module
never executes a command, reads credentials, or copies arbitrary command output.
"""
from __future__ import annotations

import json
import os
import re
import shlex
from typing import Any

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+=,-]{0,255}$")
_MAX_COMMAND = 8_192
_MAX_OUTPUT = 256 * 1024


def _argv(command: str) -> list[str] | None:
    if not isinstance(command, str) or not command.strip() or len(command) > _MAX_COMMAND:
        return None
    try:
        argv = shlex.split(command)
    except ValueError:
        return None
    if not argv or os.path.basename(argv[0]) != "aws":
        return None
    return argv


def aws_cli_hint(command: str) -> str | None:
    """A one-line warning for an invalid AWS CLI service/operation/enum."""
    argv = _argv(command)
    if argv is None or len(argv) < 3:
        return None
    from ozagia.aws_cli import validate_aws_command
    result = validate_aws_command(argv)
    if not result.available or result.valid is not False:
        return None
    if result.reason == "unknown_service":
        detail = "unknown service"
    elif result.reason == "unknown_operation":
        detail = f"unknown operation for {result.service}"
    elif result.reason == "invalid_enum_choice":
        detail = "invalid enumerated option value"
    else:
        return None
    return f"[ozagia] aws cli check: {detail}; the command was left unchanged"


def _json_output(output: Any) -> dict[str, Any] | None:
    if not isinstance(output, str) or len(output) > _MAX_OUTPUT:
        return None
    text = output.strip()
    if not text.startswith("{"):
        return None
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _option(argv: list[str], name: str) -> str | None:
    for index, token in enumerate(argv):
        if token == name and index + 1 < len(argv):
            return argv[index + 1]
        prefix = name + "="
        if token.startswith(prefix) and len(token) > len(prefix):
            return token[len(prefix):]
    return None


def _safe(value: Any) -> str | None:
    return value if isinstance(value, str) and _ID.fullmatch(value) else None


def job_hint(command: str, output: Any) -> str | None:
    """Return a wait hint for a just-started SSM, CodeBuild or CodePipeline job."""
    argv = _argv(command)
    if argv is None or len(argv) < 3:
        return None
    service, operation = argv[1], argv[2]
    value = _json_output(output)
    if value is None:
        return None
    region = _option(argv, "--region")
    suffix = f" --region {region}" if _safe(region) else ""
    if service == "ssm" and operation == "send-command":
        command_block = value.get("Command")
        if not isinstance(command_block, dict):
            return None
        command_id = _safe(command_block.get("CommandId"))
        instances = command_block.get("InstanceIds")
        instance_id = (_safe(instances[0])
                       if isinstance(instances, list) and instances else None)
        if not command_id or not instance_id:
            return None
        return (f"[ozagia] job started: ssm {command_id} on {instance_id}; "
                f"wait with `ozagia job wait --provider ssm --job-id {command_id} "
                f"--instance-id {instance_id}{suffix} --allow-live`")
    if service == "codebuild" and operation == "start-build":
        build = value.get("build")
        build_id = _safe(build.get("id")) if isinstance(build, dict) else None
        if not build_id:
            return None
        return (f"[ozagia] job started: codebuild {build_id}; wait with "
                f"`ozagia job wait --provider codebuild --job-id {build_id}"
                f"{suffix} --allow-live`")
    if service == "codepipeline" and operation == "start-pipeline-execution":
        execution_id = _safe(value.get("pipelineExecutionId"))
        pipeline = _safe(_option(argv, "--pipeline-name"))
        if not execution_id or not pipeline:
            return None
        return (f"[ozagia] job started: codepipeline {pipeline}; wait with "
                f"`ozagia job wait --provider codepipeline --resource {pipeline} "
                f"--job-id {execution_id}{suffix} --allow-live`")
    return None


def hints(command: str, output: Any) -> list[str]:
    found: list[str] = []
    for hint in (job_hint(command, output), aws_cli_hint(command)):
        if hint and hint not in found:
            found.append(hint)
    return found
