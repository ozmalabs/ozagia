import json
import sys

import pytest

from ozagia.core.relay import RelayPolicyError
from ozagia.relay_cli import (
    RelaySpecFileError,
    build_relay,
    load_document,
    parse_target,
)


def _document(commands=None, **policy):
    return load_document(json.dumps({
        "specs": [{"kind": "local"}],
        "policy": {
            "allowed_kinds": ["local"],
            "max_timeout_s": 30,
            **(commands and {"command_allowlist": commands} or {}),
            **policy,
        },
    }))


def test_load_and_build_local_plan_without_execution(tmp_path):
    document = _document()
    relay = build_relay(document)
    planned = relay.plan_argv(["printf", "ok"])
    assert planned == ("printf", "ok")
    assert relay.run(["printf", "never"], dry_run=True).dry_run is True


def test_live_local_run_requires_policy_and_runs_argv_only(tmp_path):
    document = _document(commands=[[sys.executable, "-c"]])
    with pytest.raises(RelayPolicyError):
        build_relay(document).run(
            [sys.executable, "-c", "print('denied')"], timeout_s=5)
    relay = build_relay(document, allow_live=True)
    result = relay.run(
        [sys.executable, "-c", "print('relay-ok')"], timeout_s=5)
    assert result.returncode == 0
    assert "relay-ok" in result.stdout
    assert "python" not in result.argv_evidence
    assert "relay-ok" not in repr(result)


def test_spec_rejects_unknown_fields_and_unsafe_targets(tmp_path):
    with pytest.raises(RelaySpecFileError, match="unknown"):
        load_document('{"specs":[{"kind":"local"}],"extra":1}')
    with pytest.raises(RelaySpecFileError, match="command_allowlist"):
        build_relay(_document(commands=[[]]))
    with pytest.raises(RelaySpecFileError, match="HOST:PORT"):
        parse_target("localhost")
    assert parse_target("127.0.0.1:8080").port == 8080


def test_ssh_plan_composes_with_parent():
    document = load_document(json.dumps({
        "specs": [
            {"kind": "local"},
            {"kind": "ssh", "options": {"host": "bastion.example"}},
        ],
        "policy": {"allowed_kinds": ["local", "ssh"], "max_timeout_s": 30},
    }))
    relay = build_relay(document)
    planned = relay.plan_argv(["uptime"])
    assert planned[0] == "ssh"
    assert planned[-1] == "uptime"
    assert "bastion.example" in planned
