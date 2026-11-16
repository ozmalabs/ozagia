from __future__ import annotations

import sys

from ozagia import aws_cli
from ozagia.aws_cli import validate_aws_command


_MODEL = {
    "s3": {
        "operations": {
            "ListBuckets": {},
            "PutObject": {
                "input": {"shape": "PutObjectInput"},
            },
        },
        "shapes": {
            "PutObjectInput": {
                "type": "structure",
                "members": {"StorageClass": {"shape": "StorageClass"}},
            },
            "StorageClass": {
                "type": "string",
                "enum": ["STANDARD", "STANDARD_IA"],
            },
        },
    },
}


def test_valid_service_and_operation() -> None:
    result = validate_aws_command(["aws", "s3", "list-buckets"], model=_MODEL)
    assert result.available
    assert result.valid is True
    assert result.service == "s3"
    assert result.operation == "list-buckets"


def test_invalid_operation_and_service() -> None:
    unknown_operation = validate_aws_command(
        ["aws", "s3", "launch-the-moon"], model=_MODEL,
    )
    unknown_service = validate_aws_command(
        ["aws", "not-a-service", "list"], model=_MODEL,
    )
    assert unknown_operation.available and not unknown_operation.valid
    assert unknown_operation.reason == "unknown_operation"
    assert unknown_service.available and not unknown_service.valid
    assert unknown_service.reason == "unknown_service"


def test_unavailable_botocore_is_explicit(monkeypatch) -> None:
    def unavailable():
        raise aws_cli._UnavailableModel

    monkeypatch.setattr(aws_cli, "_botocore_catalog", unavailable)
    result = validate_aws_command(["aws", "s3", "list-buckets"])
    assert result.available is False
    assert result.valid is None
    assert result.reason == "botocore_unavailable"


def test_simple_enum_choice_is_checked_without_echoing_value() -> None:
    good = validate_aws_command(
        ["aws", "s3", "put-object", "--storage-class", "STANDARD"],
        model=_MODEL,
    )
    canary = "credential-canary-value"
    bad = validate_aws_command(
        ["aws", "s3", "put-object", "--storage-class", canary],
        model=_MODEL,
    )
    assert good.valid is True
    assert bad.valid is False
    assert bad.reason == "invalid_enum_choice"
    assert canary not in repr(bad)


def test_extra_flags_are_not_deeply_validated_or_retained() -> None:
    canary = "aws_secret_access_key_canary"
    result = validate_aws_command(
        ["aws", "s3", "list-buckets", "--unknown-option", canary],
        model=_MODEL,
    )
    assert result.valid is True
    assert canary not in repr(result)


def test_command_input_must_be_an_argv_array() -> None:
    result = validate_aws_command("aws s3 list-buckets", model=_MODEL)
    assert result.available is False
    assert result.reason == "unsupported_command"


def test_botocore_catalog_uses_only_loader_and_local_models(monkeypatch) -> None:
    calls = []

    class FakeLoader:
        def list_available_services(self, model_type):
            calls.append(("list", model_type))
            return ["s3"]

        def load_service_model(self, service, model_type):
            calls.append(("load", service, model_type))
            return _MODEL[service]

    fake_botocore = type(sys)("botocore")
    fake_loaders = type(sys)("botocore.loaders")
    fake_loaders.Loader = FakeLoader
    fake_botocore.loaders = fake_loaders
    monkeypatch.setitem(sys.modules, "botocore", fake_botocore)
    monkeypatch.setitem(sys.modules, "botocore.loaders", fake_loaders)

    result = validate_aws_command(["aws", "s3", "list-buckets"])
    assert result.valid is True
    assert calls == [("list", "service-2"), ("load", "s3", "service-2")]


def test_invalid_enum_value_and_unknown_option_never_leak() -> None:
    canary = "AKIAIOSFODNN7EXAMPLE"
    result = validate_aws_command(
        ["aws", "s3", "put-object", "--storage-class", canary, "--secret", canary],
        model=_MODEL,
    )
    assert result.reason == "invalid_enum_choice"
    assert canary not in repr(result)
