"""Read-only resource grants: schema, loading, readiness and CLI."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ozagia import resources_cli
from ozagia.core import resource_grants as RG
from ozagia.core import resources as R


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
