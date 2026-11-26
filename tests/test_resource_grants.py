"""Read-only resource grants: schema, loading, readiness and CLI."""
from __future__ import annotations

import json
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from ozagia import resources_cli
from ozagia.core import resource_grants as RG
from ozagia.core import resources as R
from ozagia.core import status as status_module


def _write(root: Path, relative: str, text: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _resources(root: Path, text: str) -> Path:
    return _write(root, "ozagia/resources.toml", text)


def _config(root: Path, text: str) -> Path:
    return _write(root, "ozagia/config.toml", text)


def _vm(text: str = "") -> str:
    grant = ('[resource_grants.local-vm]\n'
             'kind = "vm"\n'
             'provider = "qemu"\n'
             'image = "/tmp/win10.qcow2"\n')
    return grant + text


# --------------------------------------------------------------------------- #
# loading and validation
# --------------------------------------------------------------------------- #
def test_load_grants_from_both_files(tmp_path: Path) -> None:
    _resources(tmp_path, _vm('description = "Local Windows test VM"\n'
                             'capabilities = ["windows", "kvm"]\n'))
    _config(tmp_path, '[resource_grants.aws-capped]\n'
                      'kind = "cloud"\n'
                      'provider = "aws"\n'
                      'permissions = ["admin"]\n'
                      'region = "us-east-1"\n'
                      'profile = "test-admin"\n'
                      'budget = { max_usd = 50.0, max_hours = 2, max_instances = 4 }\n'
                      'cleanup = "destroy"\n'
                      'requires_approval = true\n')
    grants = RG.load_grants(tmp_path)
    assert [grant.id for grant in grants] == ["aws-capped", "local-vm"]
    assert grants[0].source == "ozagia/config.toml"
    assert grants[0].budget is not None
    assert grants[0].budget.max_usd == 50.0
    assert grants[0].requires_approval is True
    assert grants[1].source == "ozagia/resources.toml"
    assert grants[1].capabilities == ("windows", "kvm")

    report = RG.list_grants(tmp_path, kind="vm")
    assert report["schema"] == RG.SCHEMA
    assert report["count"] == 1
    assert report["grants"][0]["id"] == "local-vm"
    assert report["errors"] == []


def test_duplicate_unknown_field_and_invalid_id(tmp_path: Path) -> None:
    _resources(tmp_path, _vm())
    _config(tmp_path, _vm())
    with pytest.raises(RG.GrantError, match="duplicate"):
        RG.load_grants(tmp_path)

    other = tmp_path / "other"
    _resources(other, _vm('token = "canary-value-123"\n'))
    with pytest.raises(RG.GrantError) as caught:
        RG.load_grants(other)
    assert "canary-value-123" not in str(caught.value)
    assert "token" not in str(caught.value)

    bad = tmp_path / "bad"
    _resources(bad, '[resource_grants."1bad"]\n'
                    'kind = "service"\nprovider = "other"\n')
    with pytest.raises(RG.GrantError, match="invalid resource grant id"):
        RG.load_grants(bad)


def test_admin_and_unbounded_force_approval(tmp_path: Path) -> None:
    _resources(tmp_path, _vm('permissions = ["admin"]\n'))
    grant = RG.load_grants(tmp_path)[0]
    assert grant.requires_approval is True

    cloud = tmp_path / "cloud"
    _resources(cloud, '[resource_grants.aws-unbounded]\n'
                      'kind = "cloud"\nprovider = "aws"\n'
                      'permissions = ["admin"]\n'
                      'budget = { unbounded = true }\n'
                      'cleanup = "destroy"\n')
    grant = RG.load_grants(cloud)[0]
    assert grant.requires_approval is True
    assert grant.budget is not None and grant.budget.unbounded is True

    invalid = tmp_path / "invalid"
    _resources(invalid, '[resource_grants.bad-budget]\n'
                        'kind = "cloud"\nprovider = "aws"\n'
                        'budget = { unbounded = true, max_usd = 1 }\n'
                        'cleanup = "destroy"\n')
    with pytest.raises(RG.GrantError, match="cannot be combined"):
        RG.load_grants(invalid)


def test_cloud_keep_requires_approval_and_risk(tmp_path: Path) -> None:
    no_approval = tmp_path / "no-approval"
    _resources(no_approval, '[resource_grants.keep]\n'
                            'kind = "cloud"\nprovider = "aws"\n'
                            'cleanup = "keep"\n')
    with pytest.raises(RG.GrantError, match="keep requires requires_approval"):
        RG.load_grants(no_approval)

    no_risk = tmp_path / "no-risk"
    _resources(no_risk, '[resource_grants.keep]\n'
                        'kind = "cloud"\nprovider = "aws"\n'
                        'cleanup = "keep"\nrequires_approval = true\n')
    with pytest.raises(RG.GrantError, match="keep requires a risk note"):
        RG.load_grants(no_risk)

    approved = tmp_path / "approved"
    _resources(approved, '[resource_grants.keep]\n'
                         'kind = "cloud"\nprovider = "aws"\n'
                         'cleanup = "keep"\nrequires_approval = true\n'
                         'risk = "billing continues until a human tears it down"\n')
    grant = RG.load_grants(approved)[0]
    assert grant.cleanup == "keep"
    assert grant.requires_approval is True

    defaulted = tmp_path / "defaulted"
    _resources(defaulted, '[resource_grants.default-cleanup]\n'
                          'kind = "cloud"\nprovider = "aws"\n')
    assert RG.load_grants(defaulted)[0].cleanup == "destroy"


def test_vm_requires_image_and_host_requires_capability(tmp_path: Path) -> None:
    vm = tmp_path / "vm"
    _resources(vm, '[resource_grants.no-image]\n'
                   'kind = "vm"\nprovider = "qemu"\n')
    with pytest.raises(RG.GrantError, match="image is required"):
        RG.load_grants(vm)

    host = tmp_path / "host"
    _resources(host, '[resource_grants.no-capability]\n'
                     'kind = "host"\nprovider = "qemu"\n')
    with pytest.raises(RG.GrantError, match="capabilities must declare"):
        RG.load_grants(host)

    good = tmp_path / "good"
    _resources(good, '[resource_grants.kvm-host]\n'
                     'kind = "host"\nprovider = "qemu"\n'
                     'capabilities = ["kvm"]\n')
    grant = RG.load_grants(good)[0]
    assert grant.capabilities == ("kvm",)
    assert grant.cleanup == "none"


def test_secret_like_values_are_rejected_and_never_echoed(
        tmp_path: Path) -> None:
    canary = "AKIAIOSFODNN7EXAMPLE"
    secret = tmp_path / "secret"
    _resources(secret, _vm(f'profile = "{canary}"\n'))
    with pytest.raises(RG.GrantError) as caught:
        RG.load_grants(secret)
    assert canary not in str(caught.value)

    inline = tmp_path / "inline"
    _resources(inline, _vm('risk = "AWS_SECRET_ACCESS_KEY=another-canary"\n'))
    with pytest.raises(RG.GrantError) as caught:
        RG.load_grants(inline)
    assert "another-canary" not in str(caught.value)

    referenced = tmp_path / "referenced"
    _resources(referenced, _vm('profile = "secret://vault/aws/test-admin"\n'))
    grant = RG.load_grants(referenced)[0]
    assert grant.profile == "secret://vault/aws/test-admin"

    cloud = tmp_path / "referenced-cloud"
    _resources(cloud, '[resource_grants.aws-secret]\n'
                      'kind = "cloud"\nprovider = "aws"\n'
                      'permissions = ["user"]\n'
                      'profile = "secret://vault/aws/test-admin"\n'
                      'cleanup = "destroy"\n')
    report = RG.check_grant(cloud, "aws-secret",
                            which=lambda name: "/usr/bin/aws",
                            path_exists=lambda path: False)
    assert report["state"] == "unknown"
    assert report["provenance"]["secrets_resolved"] is False
    assert any(check["id"] == "cloud.profile"
               and check["detail"].get("secret_reference")
               for check in report["checks"])


# --------------------------------------------------------------------------- #
# local checks
# --------------------------------------------------------------------------- #
def _fake_vm_grant(root: Path, image: Path) -> None:
    _resources(root, f'[resource_grants.local-vm]\n'
                     f'kind = "vm"\nprovider = "qemu"\n'
                     f'image = "{image}"\n'
                     f'constraints = {{ max_memory_gb = 1, max_disk_gb = 1 }}\n')


def test_check_vm_ready_with_injected_local_probes(tmp_path: Path,
                                                   monkeypatch) -> None:
    image = tmp_path / "win10.qcow2"
    image.write_bytes(b"qcow2" * 1024)
    _fake_vm_grant(tmp_path, image)

    def forbidden(*args, **kwargs):
        raise AssertionError("no command may run by default")

    monkeypatch.setattr(RG.subprocess, "run", forbidden)
    report = RG.check_grant(
        tmp_path, "local-vm",
        which=lambda name: f"/usr/bin/{name}",
        path_exists=lambda path: path in {"/dev/kvm", str(image)},
        access=lambda path, mode: True,
        admission=lambda *args, **kwargs: {"ok": True,
                                           "requested": {"memory_mb": 1024}},
    )
    assert report["schema"] == RG.SCHEMA
    assert report["state"] == "ready"
    assert report["ready"] is True
    assert report["permissions_verified"] is False
    assert report["cleanup"] == "destroy"
    assert report["provenance"] == {
        "network": False, "wrote": False, "secrets_resolved": False}
    assert report["live_checked"] is False
    ids = {check["id"] for check in report["checks"]}
    assert {"kvm.device", "executable.qemu-system-x86_64", "image.path",
            "admission"} <= ids
    assert all({"id", "level", "message", "detail"} <= set(check)
               for check in report["checks"])


def test_check_vm_missing_and_unavailable_without_raising(tmp_path: Path) -> None:
    image = tmp_path / "win10.qcow2"
    image.write_bytes(b"qcow2")
    _fake_vm_grant(tmp_path, image)

    missing = RG.check_grant(
        tmp_path, "local-vm",
        which=lambda name: None,
        path_exists=lambda path: False,
        access=lambda path, mode: False,
        admission=lambda *args, **kwargs: {"ok": True},
    )
    assert missing["state"] == "missing"
    assert missing["ready"] is False

    no_qemu = RG.check_grant(
        tmp_path, "local-vm",
        which=lambda name: None,
        path_exists=lambda path: path in {"/dev/kvm", str(image)},
        access=lambda path, mode: True,
        admission=lambda *args, **kwargs: {"ok": True},
    )
    assert no_qemu["state"] == "missing"
    assert any(check["id"] == "executable.qemu-system-x86_64"
               and check["level"] == "error" for check in no_qemu["checks"])

    no_image = RG.check_grant(
        tmp_path, "local-vm",
        which=lambda name: f"/usr/bin/{name}",
        path_exists=lambda path: path == "/dev/kvm",
        access=lambda path, mode: True,
        admission=lambda *args, **kwargs: {"ok": True},
    )
    assert no_image["state"] == "missing"

    kvm_denied = RG.check_grant(
        tmp_path, "local-vm",
        which=lambda name: f"/usr/bin/{name}",
        path_exists=lambda path: path in {"/dev/kvm", str(image)},
        access=lambda path, mode: path != "/dev/kvm",
        admission=lambda *args, **kwargs: {"ok": True},
    )
    assert kvm_denied["state"] == "unavailable"

    too_big = RG.check_grant(
        tmp_path, "local-vm",
        which=lambda name: f"/usr/bin/{name}",
        path_exists=lambda path: path in {"/dev/kvm", str(image)},
        access=lambda path, mode: True,
        admission=lambda *args, **kwargs: {
            "ok": False, "reason": "memory.request",
            "shortfall": {"memory_mb": 128}},
    )
    assert too_big["state"] == "unavailable"


def test_check_host_reports_capability_without_launching(tmp_path: Path,
                                                         monkeypatch) -> None:
    _resources(tmp_path, '[resource_grants.kvm-host]\n'
                         'kind = "host"\nprovider = "qemu"\n'
                         'capabilities = ["kvm"]\n')

    def forbidden(*args, **kwargs):
        raise AssertionError("a host check must not launch anything")

    monkeypatch.setattr(RG.subprocess, "run", forbidden)
    report = RG.check_grant(
        tmp_path, "kvm-host",
        which=lambda name: f"/usr/bin/{name}",
        path_exists=lambda path: path == "/dev/kvm",
        access=lambda path, mode: True,
    )
    assert report["state"] == "ready"
    ids = {check["id"] for check in report["checks"]}
    assert "capability.declared" in ids
    assert "image.path" not in ids


def test_host_without_a_known_probe_is_unknown(tmp_path: Path) -> None:
    _resources(tmp_path, '[resource_grants.odd-host]\n'
                         'kind = "host"\nprovider = "other"\n'
                         'capabilities = ["windows"]\n')
    report = RG.check_grant(tmp_path, "odd-host",
                            which=lambda name: None,
                            path_exists=lambda path: False,
                            access=lambda path, mode: False)
    assert report["state"] == "unknown"


# --------------------------------------------------------------------------- #
# cloud checks
# --------------------------------------------------------------------------- #
def _aws_home(tmp_path: Path, monkeypatch, *, profile: str = "test-admin") -> Path:
    home = tmp_path / "home"
    (home / ".aws").mkdir(parents=True)
    (home / ".aws/config").write_text(
        f"[profile {profile}]\n"
        "region = us-east-1\n"
        "aws_secret_access_key = CANARY-CONFIG-VALUE\n"
        "[profile unrelated]\n"
        "region = eu-west-1\n",
        encoding="utf-8")
    (home / ".aws/credentials").write_text(
        f"[{profile}]\n"
        "aws_access_key_id = CANARY-ACCESS-ID\n"
        "aws_secret_access_key = CANARY-SECRET-VALUE\n",
        encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("AWS_CONFIG_FILE", raising=False)
    monkeypatch.delenv("AWS_SHARED_CREDENTIALS_FILE", raising=False)
    return home


def _aws_grant(root: Path, *, profile: str = "test-admin",
               permissions: str = '["admin"]') -> None:
    _resources(root, f'[resource_grants.aws-capped]\n'
                     f'kind = "cloud"\nprovider = "aws"\n'
                     f'description = "Admin test account with a cap"\n'
                     f'permissions = {permissions}\n'
                     f'region = "us-east-1"\n'
                     f'profile = "{profile}"\n'
                     f'budget = {{ max_usd = 50.0, max_hours = 2, max_instances = 4 }}\n'
                     f'cleanup = "destroy"\n'
                     f'requires_approval = true\n')


def test_aws_offline_shows_budget_and_never_verifies_admin(
        tmp_path: Path, monkeypatch) -> None:
    _aws_home(tmp_path, monkeypatch)
    _aws_grant(tmp_path)
    report = RG.check_grant(
        tmp_path, "aws-capped", which=lambda name: "/usr/bin/aws",
        approval_granted=True)
    assert report["state"] == "ready"
    assert report["budget"] == {"max_usd": 50.0, "max_hours": 2.0,
                                "max_instances": 4, "unbounded": False}
    assert report["cleanup"] == "destroy"
    assert report["permissions"] == ["admin"]
    assert report["permissions_verified"] is False
    assert report["identity_present"] is None
    assert report["live_checked"] is False
    assert report["provenance"]["network"] is False
    assert "cloud_identity" in report["not_assessed"]
    assert "administrator" not in json.dumps(report).lower()
    text = json.dumps(report)
    assert "CANARY" not in text
    profile = next(check for check in report["checks"]
                   if check["id"] == "cloud.profile")
    assert profile["detail"]["profile"] == "test-admin"
    assert profile["detail"]["found_in"] == ["config", "credentials"]


def test_aws_missing_profile_section_is_unavailable(tmp_path: Path,
                                                    monkeypatch) -> None:
    _aws_home(tmp_path, monkeypatch)
    _aws_grant(tmp_path, profile="not-there", permissions='["user"]')
    # Remove the forced approval so the profile check decides the state.
    text = (tmp_path / "ozagia/resources.toml").read_text(encoding="utf-8")
    (tmp_path / "ozagia/resources.toml").write_text(
        text.replace("requires_approval = true\n", ""), encoding="utf-8")
    report = RG.check_grant(
        tmp_path, "aws-capped", which=lambda name: "/usr/bin/aws")
    assert report["state"] == "unavailable"
    profile = next(check for check in report["checks"]
                   if check["id"] == "cloud.profile")
    assert profile["level"] == "error"


def test_aws_admin_requires_approval(tmp_path: Path, monkeypatch) -> None:
    _aws_home(tmp_path, monkeypatch)
    _aws_grant(tmp_path)
    report = RG.check_grant(
        tmp_path, "aws-capped", which=lambda name: "/usr/bin/aws")
    assert report["state"] == "requires_approval"
    assert report["ready"] is False


def test_live_check_needs_a_runner_and_does_not_echo_accounts(
        tmp_path: Path, monkeypatch) -> None:
    _aws_home(tmp_path, monkeypatch)
    _aws_grant(tmp_path, permissions='["user"]')
    text = (tmp_path / "ozagia/resources.toml").read_text(encoding="utf-8")
    (tmp_path / "ozagia/resources.toml").write_text(
        text.replace("requires_approval = true\n", ""), encoding="utf-8")

    offline = RG.check_grant(
        tmp_path, "aws-capped", which=lambda name: "/usr/bin/aws", live=True)
    assert offline["state"] == "unavailable"
    assert offline["live_checked"] is False
    assert offline["provenance"]["network"] is False
    assert any(check["id"] == "live.runner_required"
               for check in offline["checks"])

    calls: list[str] = []

    def runner(grant: RG.ResourceGrant) -> dict:
        calls.append(grant.id)
        return {
            "identity_present": True,
            "account_id": "123456789012",
            "arn": "arn:aws:iam::123456789012:user/test-admin",
            "permissions_verified": True,
        }

    report = RG.check_grant(
        tmp_path, "aws-capped", which=lambda name: "/usr/bin/aws",
        live=True, live_runner=runner)
    assert calls == ["aws-capped"]
    assert report["state"] == "ready"
    assert report["live"] is True
    assert report["live_checked"] is True
    assert report["identity_present"] is True
    assert report["provenance"]["network"] is True
    assert report["permissions_verified"] is False
    text = json.dumps(report)
    assert "123456789012" not in text
    assert "arn:aws" not in text
    assert "account_id" not in text


# --------------------------------------------------------------------------- #
# read-only boundaries, errors and CLI
# --------------------------------------------------------------------------- #
def test_list_reports_bounded_errors_without_raising(tmp_path: Path) -> None:
    parts = ['[resource_grants.good]\nkind = "service"\nprovider = "other"\n']
    for index in range(25):
        parts.append(f'[resource_grants.bad-{index}]\nkind = "vm"\n'
                     'provider = "qemu"\n')
    _resources(tmp_path, "\n".join(parts))

    with pytest.raises(RG.GrantError):
        RG.load_grants(tmp_path)
    report = RG.list_grants(tmp_path)
    assert report["count"] == 1
    assert len(report["errors"]) == RG.MAX_ERRORS
    assert report["errors"][-1].endswith("more error(s) omitted")


def test_check_and_list_do_not_write_state(tmp_path: Path) -> None:
    image = tmp_path / "win10.qcow2"
    image.write_bytes(b"qcow2")
    _fake_vm_grant(tmp_path, image)
    RG.list_grants(tmp_path)
    RG.show_grant(tmp_path, "local-vm")
    RG.check_grant(
        tmp_path, "local-vm", which=lambda name: f"/usr/bin/{name}",
        path_exists=lambda path: path in {"/dev/kvm", str(image)},
        access=lambda path, mode: True,
        admission=lambda *args, **kwargs: {"ok": True})
    assert not (tmp_path / ".ozagia").exists()


def test_resources_status_does_not_include_grants(tmp_path: Path,
                                                  monkeypatch) -> None:
    _resources(tmp_path, _vm())
    _config(tmp_path, '[resource_grants.aws]\nkind = "cloud"\n'
                      'provider = "aws"\ncleanup = "destroy"\n')
    monkeypatch.setattr(R, "host_snapshot", lambda: {
        "memory": {"total_mb": 1000.0, "available_mb": 900.0},
        "platform": "linux", "cpu_count": 4, "load": {},
        "pressure": {}, "schema": R.SCHEMA,
        "checked_at": "2026-11-26T00:00:00+00:00",
    })
    monkeypatch.setattr(R, "disk_usage", lambda path: {
        "path": str(path), "mount": "/", "fstype": "ext4",
        "total_gb": 10.0, "free_gb": 5.0, "free_pct": 50.0,
    })
    policy = R.load_policy(tmp_path)
    assert "resource_grants" not in json.dumps(policy)
    report = R.status(tmp_path, top=0)
    assert "resource_grants" not in json.dumps(report)


def test_cli_grants_list_show_and_check_exit_codes(tmp_path: Path,
                                                   capsys) -> None:
    _resources(tmp_path, '[resource_grants.worker]\n'
                         'kind = "service"\nprovider = "other"\n'
                         'description = "A placeholder service"\n')
    code = resources_cli.main(
        ["grants", "list", "--root", str(tmp_path), "--json"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["count"] == 1
    assert payload["grants"][0]["id"] == "worker"

    code = resources_cli.main(
        ["grants", "show", "worker", "--root", str(tmp_path), "--json"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["grant"]["id"] == "worker"

    code = resources_cli.main(
        ["grants", "check", "worker", "--root", str(tmp_path), "--json"])
    assert code == 0
    report = json.loads(capsys.readouterr().out)
    assert report["state"] == "unknown"
    assert report["ready"] is False
    assert {"schema", "grant", "state", "ready", "checks", "provenance",
            "not_assessed"} <= set(report)

    code = resources_cli.main(
        ["grants", "check", "worker", "--root", str(tmp_path),
         "--require-ready", "--json"])
    assert code == 1
    capsys.readouterr()

    code = resources_cli.main(
        ["grants", "check", "absent", "--root", str(tmp_path)])
    assert code == 2
    assert "unknown resource grant" in capsys.readouterr().err

    with pytest.raises(SystemExit) as caught:
        resources_cli.main(["grants", "claim", "worker", "--root", str(tmp_path)])
    assert caught.value.code == 2


def test_cli_list_reports_malformed_grants_without_leaking_canary(
        tmp_path: Path, capsys) -> None:
    _resources(tmp_path, '[resource_grants.bad]\n'
                         'kind = "vm"\nprovider = "qemu"\n'
                         'token = "canary-value-456"\n')
    code = resources_cli.main(
        ["grants", "list", "--root", str(tmp_path), "--json"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["count"] == 0
    assert payload["errors"]
    assert "canary-value-456" not in json.dumps(payload)
    assert "token" not in json.dumps(payload)

    code = resources_cli.main(
        ["grants", "show", "bad", "--root", str(tmp_path)])
    assert code == 2
    captured = capsys.readouterr()
    assert "canary-value-456" not in captured.err


# --------------------------------------------------------------------------- #
# claim ledger
# --------------------------------------------------------------------------- #
def _patch_ready_check(monkeypatch) -> None:
    def ready(root, grant_id, **kwargs):
        return {"schema": RG.SCHEMA, "id": grant_id, "state": "ready",
                "ready": True, "checks": [],
                "approval": {"required": False, "granted": False}}

    monkeypatch.setattr(RG, "check_grant", ready)


def _claim_grant(root: Path, text: str) -> None:
    _resources(root, text)


def test_claim_accepts_a_ready_grant_without_running_a_command(
        tmp_path: Path, monkeypatch) -> None:
    _claim_grant(tmp_path, '[resource_grants.kvm-host]\n'
                           'kind = "host"\nprovider = "qemu"\n'
                           'capabilities = ["kvm"]\n'
                           'cleanup = "none"\n')
    monkeypatch.setattr(RG.shutil, "which",
                        lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(RG.os.path, "exists",
                        lambda path: path == "/dev/kvm")
    monkeypatch.setattr(RG.os, "access", lambda path, mode: True)

    def forbidden(*args, **kwargs):
        raise AssertionError("a claim must not run a command")

    monkeypatch.setattr(RG.subprocess, "run", forbidden)
    record = RG.claim(tmp_path, "kvm-host", reason="ready host",
                      now="2026-01-01T00:00:00Z")
    assert record["status"] == "active"
    assert record["grant_kind"] == "host"
    assert record["cleanup"]["policy"] == "none"
    assert len(_records(tmp_path)) == 1


def _ledger(root: Path) -> Path:
    return root / ".ozagia" / "resources" / "grants"


def _records(root: Path) -> list[Path]:
    directory = _ledger(root)
    return sorted(directory.glob("*.json")) if directory.is_dir() else []


def test_claim_refuses_unready_and_unapproved_grants_without_writing(
        tmp_path: Path) -> None:
    _claim_grant(tmp_path, '[resource_grants.missing-vm]\n'
                           'kind = "vm"\nprovider = "qemu"\n'
                           'image = "/tmp/not-there.qcow2"\n'
                           'cleanup = "destroy"\n')
    with pytest.raises(RG.GrantRefused) as caught:
        RG.claim(tmp_path, "missing-vm", reason="test the missing image")
    assert caught.value.code == "missing"
    assert not _ledger(tmp_path).exists()

    _claim_grant(tmp_path, '[resource_grants.aws]\n'
                           'kind = "cloud"\nprovider = "aws"\n'
                           'permissions = ["admin"]\n'
                           'budget = { max_usd = 5.0, max_hours = 1 }\n'
                           'cleanup = "destroy"\n'
                           'requires_approval = true\n')
    with pytest.raises(RG.GrantRefused) as caught:
        RG.claim(tmp_path, "aws", reason="no approval yet")
    assert caught.value.code == "requires_approval"
    assert not _ledger(tmp_path).exists()


def test_admin_and_unbounded_need_both_approval_flags(
        tmp_path: Path, monkeypatch) -> None:
    _patch_ready_check(monkeypatch)
    _claim_grant(tmp_path, '[resource_grants.aws]\n'
                           'kind = "cloud"\nprovider = "aws"\n'
                           'permissions = ["admin"]\n'
                           'budget = { max_usd = 50.0, max_hours = 2, '
                           'max_instances = 4 }\n'
                           'cleanup = "destroy"\n'
                           'requires_approval = true\n')
    with pytest.raises(RG.GrantRefused) as caught:
        RG.claim(tmp_path, "aws", reason="approve only", approve=True)
    assert caught.value.code == "requires_approval"
    assert caught.value.detail["flag"] == "allow_unbounded"

    with pytest.raises(RG.GrantRefused):
        RG.claim(tmp_path, "aws", reason="allow only",
                 allow_unbounded=True)

    record = RG.claim(tmp_path, "aws", reason="both flags", approve=True,
                      allow_unbounded=True, budget={"max_usd": 25.0})
    assert record["status"] == "active"
    assert record["budget"]["max_usd"] == 25.0
    assert record["budget"]["max_minutes"] == 120.0
    assert record["budget"]["max_instances"] == 4
    assert record["approval"]["granted"] is True
    assert record["approval"]["approved_by"] == "host"
    assert record["approval"]["source"] == "cli"
    assert record["approval"]["flags"] == {
        "approve": True, "allow_unbounded": True, "allow_keep": False}

    _claim_grant(tmp_path / "unbounded", '[resource_grants.aws-u]\n'
                                        'kind = "cloud"\nprovider = "aws"\n'
                                        'permissions = ["admin"]\n'
                                        'budget = { unbounded = true }\n'
                                        'cleanup = "destroy"\n'
                                        'requires_approval = true\n')
    with pytest.raises(RG.GrantRefused):
        RG.claim(tmp_path / "unbounded", "aws-u", reason="only approve",
                 approve=True)
    record = RG.claim(tmp_path / "unbounded", "aws-u", reason="both",
                      approve=True, allow_unbounded=True)
    assert record["budget"]["unbounded"] is True
    assert record["budget"]["max_minutes"] is None
    assert record["budget"]["ttl_minutes"] == RG.DEFAULT_CLAIM_TTL_MINUTES


def test_keep_cleanup_needs_allow_keep(tmp_path: Path, monkeypatch) -> None:
    _patch_ready_check(monkeypatch)
    _claim_grant(tmp_path, '[resource_grants.keep]\n'
                           'kind = "cloud"\nprovider = "aws"\n'
                           'cleanup = "keep"\n'
                           'requires_approval = true\n'
                           'risk = "billing continues until a human '
                           'tears it down"\n')
    with pytest.raises(RG.GrantRefused) as caught:
        RG.claim(tmp_path, "keep", reason="keep it", approve=True)
    assert caught.value.code == "requires_approval"
    record = RG.claim(tmp_path, "keep", reason="keep it", approve=True,
                      allow_keep=True)
    assert record["cleanup"]["policy"] == "keep"
    assert record["approval"]["flags"]["allow_keep"] is True


def test_claim_writes_a_bounded_private_ledger_and_refuses_double_claim(
        tmp_path: Path, monkeypatch) -> None:
    _patch_ready_check(monkeypatch)
    _claim_grant(tmp_path, '[resource_grants.svc]\n'
                           'kind = "service"\nprovider = "other"\n'
                           'cleanup = "destroy"\n')
    record = RG.claim(
        tmp_path, "svc", reason="run the integration test", task="t1",
        session="s1", budget={"max_usd": 5.0, "max_minutes": 30.0},
        now="2026-01-01T00:00:00Z")
    assert record["claim_id"].startswith("clm-")
    assert record["grant_id"] == "svc"
    assert record["grant_kind"] == "service"
    assert record["task"] == "t1"
    assert record["session"] == "s1"
    assert record["budget"] == {
        "max_usd": 5.0, "max_minutes": 30.0, "max_instances": None,
        "unbounded": False, "ttl_minutes": 30.0}
    assert record["started"] == "2026-01-01T00:00:00+00:00"
    assert record["expires"] == "2026-01-01T00:30:00+00:00"
    assert record["observed"] == {
        "cost_usd": None, "minutes": None, "provided": False}
    assert record["cleanup"] == {
        "policy": "destroy", "status": "unknown", "verified": False,
        "source": None, "evidence": None}
    assert record["status"] == "active"

    directory = _ledger(tmp_path)
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    files = _records(tmp_path)
    assert len(files) == 1
    assert RG._CLAIM_FILE_RE.fullmatch(files[0].name)
    assert files[0].name == RG._claim_file_name(record["claim_id"])
    assert stat.S_IMODE(files[0].stat().st_mode) == 0o600
    assert "secret" not in files[0].read_text(encoding="utf-8").lower()

    with pytest.raises(RG.GrantRefused) as caught:
        RG.claim(tmp_path, "svc", reason="a second claim",
                 now="2026-01-01T00:01:00Z")
    assert caught.value.code == "grant_in_use"

    RG.release(tmp_path, record["claim_id"], apply=True,
               now="2026-01-01T00:02:00Z")
    again = RG.claim(tmp_path, "svc", reason="after release",
                     now="2026-01-01T00:03:00Z")
    assert again["status"] == "active"
    assert len(_records(tmp_path)) == 2


def test_claims_report_expiry_and_orphans_without_deleting(
        tmp_path: Path, monkeypatch) -> None:
    _patch_ready_check(monkeypatch)
    _claim_grant(tmp_path, '[resource_grants.svc]\n'
                           'kind = "service"\nprovider = "other"\n'
                           'cleanup = "destroy"\n')
    record = RG.claim(tmp_path, "svc", reason="short lease",
                      budget={"max_minutes": 5.0},
                      now="2000-01-01T00:00:00Z")
    report = RG.claims(tmp_path)
    assert report["counts"]["total"] == 1
    assert report["counts"]["active"] == 0
    assert report["counts"]["expired"] == 1
    assert report["counts"]["orphaned"] == 1
    assert report["claims"][0]["status"] == "orphaned"
    assert report["claims"][0]["expired"] is True
    assert report["claims"][0]["orphaned"] is True
    assert len(_records(tmp_path)) == 1

    status = RG.claim_status(tmp_path, record["claim_id"])
    assert status["status"] == "orphaned"
    assert RG.claims(tmp_path, status="orphaned")["count"] == 1
    assert RG.claims(tmp_path, status="expired")["count"] == 1
    assert RG.claims(tmp_path, status="active")["count"] == 0

    released = RG.release(tmp_path, record["claim_id"], apply=True,
                          now="2026-01-01T00:00:00Z")
    assert released["status"] == "released"
    assert released["release"]["previous_status"] == "orphaned"
    assert released["cleanup"]["status"] == "unknown"
    assert len(_records(tmp_path)) == 1


def test_release_preview_then_apply_records_only_caller_evidence(
        tmp_path: Path, monkeypatch) -> None:
    _patch_ready_check(monkeypatch)
    _claim_grant(tmp_path, '[resource_grants.svc]\n'
                           'kind = "service"\nprovider = "other"\n'
                           'cleanup = "destroy"\n')
    record = RG.claim(tmp_path, "svc", reason="cleanup test",
                      now="2026-01-01T00:00:00Z")
    path = _records(tmp_path)[0]
    before = path.read_bytes()

    preview = RG.release(
        tmp_path, record["claim_id"],
        observed={"cost_usd": 1.25, "minutes": 12.0,
                  "cleanup": "failed", "evidence": "operator saw an instance"},
        now="2026-01-01T00:10:00Z")
    assert preview["dry_run"] is True
    assert preview["applied"] is False
    assert preview["would_release"] is True
    assert preview["status"] == "active"
    assert preview["cleanup"]["status"] == "failed"
    assert preview["observed"]["cost_usd"] == 1.25
    assert path.read_bytes() == before

    applied = RG.release(
        tmp_path, record["claim_id"], apply=True,
        observed={"cost_usd": 1.25, "minutes": 12.0,
                  "cleanup": "failed", "evidence": "operator saw an instance"},
        now="2026-01-01T00:10:00Z")
    assert applied["dry_run"] is False
    assert applied["applied"] is True
    assert applied["status"] == "released"
    assert applied["observed"] == {
        "cost_usd": 1.25, "minutes": 12.0, "provided": True}
    assert applied["cleanup"] == {
        "policy": "destroy", "status": "failed", "verified": False,
        "source": "caller", "evidence": "operator saw an instance"}
    assert applied["release"]["by"] == "host"
    assert applied["release"]["source"] == "cli"
    assert applied["release"]["previous_status"] == "active"
    assert len(_records(tmp_path)) == 1

    closed_preview = RG.release(tmp_path, record["claim_id"],
                                now="2026-01-01T00:11:00Z")
    assert closed_preview["status"] == "released"
    assert closed_preview["would_release"] is False
    assert closed_preview["already_released"] is True
    assert closed_preview["cleanup"]["status"] == "failed"

    with pytest.raises(RG.GrantRefused) as caught:
        RG.release(tmp_path, record["claim_id"], apply=True)
    assert caught.value.code == "claim_released"


def test_release_without_observed_evidence_stays_unknown(
        tmp_path: Path, monkeypatch) -> None:
    _patch_ready_check(monkeypatch)
    _claim_grant(tmp_path, '[resource_grants.svc]\n'
                           'kind = "service"\nprovider = "other"\n'
                           'cleanup = "destroy"\n')
    record = RG.claim(tmp_path, "svc", reason="no evidence",
                      now="2026-01-01T00:00:00Z")
    applied = RG.release(tmp_path, record["claim_id"], apply=True,
                         now="2026-01-01T00:10:00Z")
    assert applied["cleanup"]["status"] == "unknown"
    assert applied["cleanup"]["verified"] is False
    assert applied["observed"] == {
        "cost_usd": None, "minutes": None, "provided": False}
    assert applied["release"]["cleanup"]["status"] == "unknown"


def test_claim_and_release_validate_ids_numbers_and_no_canary(
        tmp_path: Path, monkeypatch) -> None:
    _patch_ready_check(monkeypatch)
    _claim_grant(tmp_path, '[resource_grants.svc]\n'
                           'kind = "service"\nprovider = "other"\n'
                           'cleanup = "destroy"\n')
    with pytest.raises(RG.GrantError, match="control"):
        RG.claim(tmp_path, "svc", reason="bad\nreason")
    with pytest.raises(RG.GrantError, match="positive"):
        RG.claim(tmp_path, "svc", reason="negative",
                 budget={"max_usd": -1})
    with pytest.raises(RG.GrantError, match="finite"):
        RG.claim(tmp_path, "svc", reason="nan",
                 budget={"max_usd": float("nan")})
    with pytest.raises(RG.GrantError) as caught:
        RG.claim(tmp_path, "svc",
                 reason="key " + "AKIA" + "B" * 16)
    assert "AKIA" not in str(caught.value)
    assert not _ledger(tmp_path).exists()

    record = RG.claim(tmp_path, "svc", reason="valid",
                      now="2026-01-01T00:00:00Z")
    with pytest.raises(RG.GrantError, match="claim id"):
        RG.release(tmp_path, "../../etc/passwd")
    with pytest.raises(RG.GrantRefused) as caught:
        RG.release(tmp_path, "clm-" + "0" * 32)
    assert caught.value.code == "claim_missing"
    with pytest.raises(RG.GrantError, match="must not be negative"):
        RG.release(tmp_path, record["claim_id"],
                   observed={"cost_usd": -0.01})
    with pytest.raises(RG.GrantError, match="observed.cleanup"):
        RG.release(tmp_path, record["claim_id"],
                   observed={"cleanup": "probably"})


def test_concurrent_claims_have_one_winner(tmp_path: Path,
                                           monkeypatch) -> None:
    _patch_ready_check(monkeypatch)
    _claim_grant(tmp_path, '[resource_grants.svc]\n'
                           'kind = "service"\nprovider = "other"\n'
                           'cleanup = "destroy"\n')

    def attempt(index: int) -> tuple[bool, str | None]:
        try:
            RG.claim(tmp_path, "svc", reason=f"concurrent {index}",
                     now="2026-01-01T00:00:00Z")
            return True, None
        except RG.GrantRefused as exc:
            return False, exc.code

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(attempt, range(4)))
    winners = [item for item in results if item[0]]
    refusals = [item for item in results if not item[0]]
    assert len(winners) == 1
    assert len(refusals) == 3
    assert all(code == "grant_in_use" for _ok, code in refusals)
    assert len(_records(tmp_path)) == 1


def test_status_section_and_claims_views_create_no_state(tmp_path: Path) -> None:
    report = RG.claims(tmp_path)
    assert report["counts"]["total"] == 0
    assert report["read_only"] is True
    with pytest.raises(RG.GrantRefused):
        RG.claim_status(tmp_path, "clm-" + "0" * 32)
    degraded: list[dict[str, str]] = []
    section = status_module._grants_status(tmp_path, degraded)
    assert section["state"] == "absent"
    assert section["report"]["claims"]["counts"]["total"] == 0
    assert section["report"]["readiness"]["checked"] is False
    assert not (tmp_path / ".ozagia").exists()
    assert degraded == []


def test_cli_claim_release_and_status_exit_codes(tmp_path: Path,
                                                 monkeypatch, capsys) -> None:
    _patch_ready_check(monkeypatch)
    _claim_grant(tmp_path, '[resource_grants.svc]\n'
                           'kind = "service"\nprovider = "other"\n'
                           'cleanup = "destroy"\n')

    code = resources_cli.main(
        ["grants", "claim", "svc", "--reason", "cli claim",
         "--max-usd", "2.5", "--max-minutes", "15",
         "--root", str(tmp_path), "--json"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    claim_id = payload["claim"]["claim_id"]
    assert payload["ok"] is True
    assert payload["claim"]["budget"]["max_usd"] == 2.5

    code = resources_cli.main(
        ["grants", "claim", "svc", "--reason", "again",
         "--root", str(tmp_path), "--json"])
    assert code == 1
    assert "already claimed" in capsys.readouterr().err

    code = resources_cli.main(
        ["grants", "status", "--root", str(tmp_path), "--json"])
    assert code == 0
    status = json.loads(capsys.readouterr().out)
    assert status["counts"]["active"] == 1
    assert status["claims"][0]["claim_id"] == claim_id

    code = resources_cli.main(
        ["grants", "release", claim_id, "--observed-usd", "1.0",
         "--cleanup", "done", "--root", str(tmp_path), "--json"])
    assert code == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["dry_run"] is True
    assert preview["applied"] is False

    code = resources_cli.main(
        ["grants", "release", claim_id, "--apply", "--observed-usd", "1.0",
         "--observed-minutes", "5", "--cleanup", "done",
         "--root", str(tmp_path), "--json"])
    assert code == 0
    applied = json.loads(capsys.readouterr().out)
    assert applied["applied"] is True
    assert applied["cleanup"]["status"] == "done"
    assert applied["observed"]["cost_usd"] == 1.0

    code = resources_cli.main(
        ["grants", "release", claim_id, "--apply",
         "--root", str(tmp_path), "--json"])
    assert code == 1
    assert "already released" in capsys.readouterr().err

    code = resources_cli.main(
        ["grants", "claim", "svc", "--reason", "negative",
         "--max-usd", "-1", "--root", str(tmp_path), "--json"])
    assert code == 2

    code = resources_cli.main(
        ["grants", "claim", "absent", "--reason", "no grant",
         "--root", str(tmp_path), "--json"])
    assert code == 2
    assert "unknown resource grant" in capsys.readouterr().err

    with pytest.raises(SystemExit) as caught:
        resources_cli.main(["grants", "claim", "svc", "--root",
                            str(tmp_path)])
    assert caught.value.code == 2


def test_mcp_dev_resource_grants_is_read_only_and_bounded(
        tmp_path: Path, monkeypatch) -> None:
    from ozagia.integrations.mcp import server as mcp_server

    _patch_ready_check(monkeypatch)
    _claim_grant(tmp_path, '[resource_grants.svc]\n'
                           'kind = "service"\nprovider = "other"\n'
                           'cleanup = "destroy"\n')
    record = RG.claim(tmp_path, "svc", reason="mcp canary-free",
                      task="t1")

    server = mcp_server.DevMemoryServer(None, mode="coding",
                                        workspace=str(tmp_path))
    names = {tool["name"] for tool in server._tools}
    assert "dev_resource_grants" in names
    assert not {"dev_resource_claim", "dev_resource_release",
                "dev_resource_approve"} & names
    assert "dev_resource_grants" in server._handlers

    payload = server._handlers["dev_resource_grants"]({"limit": 10})
    assert payload["ok"] is True
    assert payload["read_only"] is True
    assert payload["network"] is False
    assert payload["secrets_resolved"] is False
    assert payload["wrote"] is False
    assert payload["grants"]["count"] == 1
    assert payload["claims"]["counts"]["total"] == 1
    assert payload["claims"]["items"][0]["claim_id"] == record["claim_id"]

    from ozagia.core import capabilities as K
    assert "cap:resources.grants" in K.surface_index()[
        "mcp:tool:dev_resource_grants"]
    section = status_module._grants_status(tmp_path, [])
    assert section["state"] == "ok"
    assert section["report"]["claims"]["counts"]["active"] == 1
    assert section["report"]["readiness"]["checked"] is False

    filtered = server._handlers["dev_resource_grants"](
        {"grant_id": "svc", "claim_status": "active"})
    assert filtered["claims"]["count"] == 1
    filtered = server._handlers["dev_resource_grants"](
        {"claim_status": "orphaned"})
    assert filtered["claims"]["count"] == 0

    canary = "AKIA" + "C" * 16
    _claim_grant(tmp_path / "canary", f'[resource_grants.bad]\n'
                                      f'kind = "service"\nprovider = "other"\n'
                                      f'description = "{canary}"\n')
    canary_server = mcp_server.DevMemoryServer(
        None, mode="coding", workspace=str(tmp_path / "canary"))
    payload = canary_server._handlers["dev_resource_grants"]({})
    assert canary not in json.dumps(payload)
