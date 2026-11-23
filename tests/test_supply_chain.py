"""Offline supply-chain attack detection: rules, policy, CLI and integration."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ozagia.core import supply_chain as SC
from ozagia.core.deep_review.analyzers import dependencies
from ozagia.supply_chain_cli import main as supply_main


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _types(result: dict) -> set[str]:
    return {item["type"] for item in result["findings"]}


def _serialized(result: dict) -> str:
    return json.dumps(result, sort_keys=True)


def test_policy_defaults_validation_and_hash() -> None:
    defaults = SC.load_policy()
    assert defaults["registries"]
    assert defaults["require_integrity"] is False
    assert SC.policy_hash(defaults) == SC.policy_hash(dict(defaults))
    with pytest.raises(ValueError):
        SC.load_policy({"unknown": True})
    with pytest.raises(ValueError):
        SC.load_policy({"severity_overrides": {"missing_integrity": "urgent"}})
    with pytest.raises(ValueError):
        SC.load_policy({"registries": ["ftp://example.com"]})


def test_registry_config_and_credentials_do_not_leak_hosts(
        tmp_path: Path) -> None:
    _write(tmp_path / ".npmrc",
           "registry=https://private.example/simple/\n"
           "//private.example/:_authToken=secret-token\n"
           "strict-ssl=false\n")
    _write(tmp_path / "requirements.txt",
           "--extra-index-url http://plain.example/simple\nrequests==2.32.0\n")

    result = SC.check(tmp_path, budget_s=10)

    types = _types(result)
    assert {"untrusted_registry", "registry_credentials", "unsafe_tls"} <= types
    serialized = _serialized(result)
    assert "private.example" not in serialized
    assert "plain.example" not in serialized
    assert "secret-token" not in serialized
    assert result["summary"]["truth_assessed"] is False


def test_missing_integrity_is_a_mixed_lock_signal(tmp_path: Path) -> None:
    mixed = tmp_path / "mixed"
    _write(mixed / "package-lock.json", json.dumps({
        "lockfileVersion": 3,
        "packages": {
            "": {"name": "demo"},
            "node_modules/one": {"version": "1.0.0",
                                 "resolved": "https://registry.npmjs.org/one.tgz",
                                 "integrity": "sha512-one"},
            "node_modules/two": {"version": "1.0.0",
                                 "resolved": "https://registry.npmjs.org/two.tgz"},
        },
    }))
    mixed_result = SC.check(mixed, budget_s=10)
    assert "missing_integrity" in _types(mixed_result)
    mixed_summary = mixed_result["summary"]
    assert mixed_summary["packages_with_integrity"] == 1
    assert mixed_summary["packages_without_integrity"] == 1

    all_missing = tmp_path / "all_missing"
    _write(all_missing / "package-lock.json", json.dumps({
        "lockfileVersion": 3,
        "packages": {"node_modules/one": {
            "version": "1.0.0",
            "resolved": "https://registry.npmjs.org/one.tgz"}},
    }))
    all_result = SC.check(all_missing, budget_s=10)
    assert "missing_integrity" not in _types(all_result)
    assert all_result["summary"]["packages_without_integrity"] == 1


def test_mutable_sources_direct_sources_and_wildcards(tmp_path: Path) -> None:
    _write(tmp_path / "Cargo.toml",
           '[package]\nname = "demo"\nversion = "0.1.0"\n'
           '[dependencies]\nserde = { git = "https://github.com/serde-rs/serde", '
           'branch = "main" }\n'
           'local = { path = "../local" }\n')
    _write(tmp_path / "package.json", json.dumps({
        "dependencies": {"left-pad": "*", "file-dep": "file:../file-dep"},
    }))

    result = SC.check(tmp_path, budget_s=10)
    types = _types(result)
    assert "mutable_git_dependency" in types
    assert "direct_source_dependency" in types
    assert "wildcard_dependency" in types


def test_install_scripts_and_unknown_build_backend(tmp_path: Path) -> None:
    _write(tmp_path / "pyproject.toml",
           '[build-system]\nrequires = []\nbuild-backend = "custom-backend"\n')
    _write(tmp_path / "setup.py",
           "from setuptools import setup\nsetup(cmdclass={})\n")
    _write(tmp_path / "build.rs",
           'fn main() { std::process::Command::new("curl"); }\n')
    _write(tmp_path / "node_modules" / "evil" / "package.json", json.dumps({
        "name": "evil", "version": "1.0.0",
        "scripts": {"preinstall": "node install.js"},
    }))
    _write(tmp_path / "composer.json", json.dumps({
        "scripts": {"post-install-cmd": "php evil.php"},
        "config": {"allow-plugins": True},
    }))

    result = SC.check(tmp_path, budget_s=10)
    summaries = [item["summary"] for item in result["findings"]
                 if item["type"] == "install_script"]
    text = "\n".join(summaries)
    assert "custom-backend" in text
    assert "setup.py" in text
    assert "Cargo build script" in text
    assert "evil" in text
    assert "Composer" in text


def test_typosquat_and_internal_package_confusion(tmp_path: Path) -> None:
    _write(tmp_path / "package.json", json.dumps({
        "name": "demo",
        "dependencies": {"lodahs": "1.0.0", "@acme/internal": "2.0.0"},
    }))
    _write(tmp_path / "package-lock.json", json.dumps({
        "lockfileVersion": 3,
        "packages": {
            "node_modules/lodahs": {
                "version": "1.0.0",
                "resolved": "https://registry.npmjs.org/lodahs/-/lodahs-1.0.0.tgz",
                "integrity": "sha512-lodahs"},
            "node_modules/@acme/internal": {
                "version": "2.0.0",
                "resolved": "https://registry.npmjs.org/@acme/internal/-/internal-2.0.0.tgz",
                "integrity": "sha512-internal"},
        },
    }))

    result = SC.check(
        tmp_path, policy={"internal_packages": ["@acme/*"]}, budget_s=10)

    types = _types(result)
    assert "typosquat_candidate" in types
    assert "dependency_confusion_candidate" in types
    confusion = [item for item in result["findings"]
                 if item["type"] == "dependency_confusion_candidate"]
    assert "public registry" in confusion[0]["summary"]


def test_policy_allowlists_suppress_registry_and_script_findings(
        tmp_path: Path) -> None:
    _write(tmp_path / ".npmrc",
           "registry=https://registry.internal.example/simple/\n")
    _write(tmp_path / "node_modules" / "esbuild" / "package.json", json.dumps({
        "name": "esbuild", "version": "1.0.0",
        "scripts": {"postinstall": "node install.js"},
    }))
    policy = {
        "registries": ["https://registry.internal.example/simple/",
                       "https://registry.npmjs.org/"],
        "allow_install_scripts": ["esbuild"],
    }

    result = SC.check(tmp_path, policy=policy, budget_s=10)

    types = _types(result)
    assert "untrusted_registry" not in types
    assert "install_script" not in types


def test_cli_json_fail_on_and_rules(tmp_path: Path, capsys) -> None:
    _write(tmp_path / ".npmrc", "strict-ssl=false\n")
    assert supply_main(
        ["check", "--root", str(tmp_path), "--json", "--fail-on", "high"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["summary"]["state"] == "checked"
    assert payload["findings"]
    assert payload["truth_assessed"] is False

    assert supply_main(
        ["check", "--root", str(tmp_path), "--json", "--fail-on", "none"]) == 0
    capsys.readouterr()
    assert supply_main(["rules", "--json"]) == 0
    rules = json.loads(capsys.readouterr().out)
    assert {rule["id"] for rule in rules} >= {
        "untrusted_registry", "install_script", "missing_integrity"}


def test_mcp_tool_is_bounded_and_read_only(tmp_path: Path) -> None:
    from ozagia.integrations.mcp.server import DevMemoryServer

    _write(tmp_path / ".npmrc", "strict-ssl=false\nregistry=http://plain.example/\n")
    server = DevMemoryServer(None, workspace=tmp_path)
    response = server.handle({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "dev_supply_chain",
                   "arguments": {"action": "check", "max_findings": 5}},
    })
    payload = json.loads(response["result"]["content"][0]["text"])
    assert payload["ok"] is True
    assert payload["truth_assessed"] is False
    assert len(payload["findings"]) <= 5
    assert "plain.example" not in json.dumps(payload)


def test_dependency_inventory_integrates_supply_chain(tmp_path: Path) -> None:
    _write(tmp_path / "package.json", json.dumps({
        "dependencies": {"left-pad": "*"}}))
    _write(tmp_path / "package-lock.json", json.dumps({
        "lockfileVersion": 3,
        "packages": {"node_modules/left-pad": {
            "version": "1.0.0",
            "resolved": "http://plain.example/left-pad.tgz"}},
    }))

    with_chain = dependencies.analyze(tmp_path)
    assert "supply_chain" in with_chain["inventory"]
    assert with_chain["inventory"]["supply_chain"]["truth_assessed"] is False
    assert any(item["type"] in SC.RULES for item in with_chain["findings"])

    without = dependencies.analyze(tmp_path, supply_chain=False)
    assert "supply_chain" not in without["inventory"]
    assert not any(item["type"] in SC.RULES for item in without["findings"])
