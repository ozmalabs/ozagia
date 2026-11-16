"""Offline validation of AWS CLI service and operation names.

Only a bounded subset of scalar enum options is checked. This module never
creates a botocore session, loads credentials, or contacts an endpoint.
"""
from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

_MAX_ARG_COUNT = 128
_MAX_ARG_LENGTH = 4_096
_MAX_TOTAL_LENGTH = 16_384
_SERVICE_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_OPTION_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9-]*$")


@dataclass(frozen=True)
class AwsValidationResult:
    """A compact validation hint; command arguments and values are not retained."""

    available: bool
    valid: bool | None
    reason: str
    service: str | None = None
    operation: str | None = None
    enum_checks: int = 0


class _UnavailableModel(Exception):
    pass


def _botocore_catalog() -> Any:
    """Load local service definitions without creating a credential session."""
    try:
        from botocore.loaders import Loader
    except ImportError as exc:
        raise _UnavailableModel from exc
    return Loader()


def _operation_name(cli_name: str) -> str:
    return "".join(part[:1].upper() + part[1:] for part in cli_name.split("-"))


def _cli_name(operation_name: str) -> str:
    # Preserve acronym runs (`SSECustomerAlgorithm` -> `sse-customer-algorithm`)
    # while separating ordinary word boundaries.
    separated = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1-\2", operation_name)
    separated = re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", separated)
    return separated.lower()


def _operations(service: Any) -> Mapping[str, Any] | None:
    if not isinstance(service, Mapping):
        return None
    operations = service.get("operations")
    return operations if isinstance(operations, Mapping) else None


def _enum_parameters(service: Mapping[str, Any], operation: Any) -> dict[str, set[str]]:
    """Return simple input member enums, keyed by AWS CLI kebab option."""
    if not isinstance(operation, Mapping):
        return {}
    shapes = service.get("shapes")
    if not isinstance(shapes, Mapping):
        shapes = {}
    input_shape = operation.get("input")
    if not isinstance(input_shape, Mapping):
        return {}
    shape_name = input_shape.get("shape")
    shape = shapes.get(shape_name) if isinstance(shape_name, str) else None
    members = shape.get("members") if isinstance(shape, Mapping) else None
    if not isinstance(members, Mapping):
        # Also support a small direct fake-model form:
        # input: {members: {Mode: {enum: ["A", "B"]}}}
        members = input_shape.get("members")
    if not isinstance(members, Mapping):
        return {}

    enums: dict[str, set[str]] = {}
    for member_name, member in members.items():
        if not isinstance(member_name, str) or not isinstance(member, Mapping):
            continue
        member_shape_name = member.get("shape")
        member_shape = shapes.get(member_shape_name) if isinstance(member_shape_name, str) else None
        values = member.get("enum")
        if values is None and isinstance(member_shape, Mapping):
            values = member_shape.get("enum")
        if (
            isinstance(values, list)
            and values
            and len(values) <= 256
            and all(isinstance(value, str) for value in values)
        ):
            enums[_cli_name(member_name)] = set(values)
    return enums


def _parse_argv(argv: Sequence[str]) -> tuple[str, str, list[str]] | None:
    if (
        not isinstance(argv, Sequence)
        or isinstance(argv, (str, bytes))
        or len(argv) < 3
        or len(argv) > _MAX_ARG_COUNT
        or any(not isinstance(arg, str) or len(arg) > _MAX_ARG_LENGTH for arg in argv)
        or sum(len(arg) for arg in argv) > _MAX_TOTAL_LENGTH
    ):
        return None
    executable = os.path.basename(argv[0])
    if executable != "aws":
        return None
    service, operation = argv[1], argv[2]
    if not _SERVICE_NAME.fullmatch(service) or not _SERVICE_NAME.fullmatch(operation):
        return None
    return service, operation, list(argv[3:])


def _enum_failure(arguments: list[str], enums: Mapping[str, set[str]]) -> bool:
    """Check only known scalar enum options; all other flags are left alone."""
    index = 0
    while index < len(arguments):
        token = arguments[index]
        if token == "--":
            break
        if not token.startswith("--"):
            index += 1
            continue
        option, separator, value = token[2:].partition("=")
        if not _OPTION_NAME.fullmatch(option):
            index += 1
            continue
        allowed = enums.get(option)
        if allowed is None:
            index += 1
            continue
        if separator:
            candidate = value
        elif index + 1 < len(arguments) and not arguments[index + 1].startswith("--"):
            index += 1
            candidate = arguments[index]
        else:
            # Required/value syntax is outside this validator's bounded scope.
            index += 1
            continue
        if candidate not in allowed:
            return True
        index += 1
    return False


def validate_aws_command(
    argv: Sequence[str],
    *,
    model: Mapping[str, Any] | Any | None = None,
) -> AwsValidationResult:
    """Check `aws <service> <operation>` against local service metadata.

    `argv` must be an argument array, not a shell command string. An injected
    fake `model` may be a botocore-style mapping of service names to
    `service-2` model dictionaries, or an object implementing
    `list_available_services("service-2")` and
    `load_service_model(service, "service-2")`.

    Only options with simple local string enums are checked. Other flags,
    parameter types, required fields, resource identifiers and JSON/YAML
    document contents are intentionally not validated.
    """
    parsed = _parse_argv(argv)
    if parsed is None:
        return AwsValidationResult(False, None, "unsupported_command")
    service_name, operation_cli, arguments = parsed

    if model is None:
        try:
            catalog: Any = _botocore_catalog()
        except _UnavailableModel:
            return AwsValidationResult(False, None, "botocore_unavailable")
    else:
        catalog = model

    if isinstance(catalog, Mapping):
        service_names = catalog.keys()
        service = catalog.get(service_name)
    else:
        try:
            service_names = catalog.list_available_services("service-2")
            service = (
                catalog.load_service_model(service_name, "service-2")
                if service_name in service_names else None
            )
        except (AttributeError, OSError, ValueError, KeyError):
            return AwsValidationResult(False, None, "model_unavailable")
    if service_name not in service_names:
        return AwsValidationResult(True, False, "unknown_service")

    operations = _operations(service)
    if operations is None:
        return AwsValidationResult(False, None, "model_unavailable", service=service_name)
    operation_name = _operation_name(operation_cli)
    operation = operations.get(operation_name)
    if operation is None:
        # Some models may use an atypical acronym boundary; compare their
        # canonical CLI spellings without exposing the supplied unknown token.
        matched_name = next(
            (name for name in operations if _cli_name(name) == operation_cli),
            None,
        )
        if matched_name is None:
            return AwsValidationResult(True, False, "unknown_operation", service=service_name)
        operation = operations[matched_name]
        operation_name = matched_name

    enum_parameters = _enum_parameters(service, operation)
    if _enum_failure(arguments, enum_parameters):
        return AwsValidationResult(
            True, False, "invalid_enum_choice", service=service_name,
            operation=_cli_name(operation_name), enum_checks=len(enum_parameters),
        )
    return AwsValidationResult(
        True, True, "valid", service=service_name,
        operation=_cli_name(operation_name), enum_checks=len(enum_parameters),
    )
