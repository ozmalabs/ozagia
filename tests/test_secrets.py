"""Tests for secret references, providers, redaction and session capture."""
from __future__ import annotations

import base64
import json
import os
import shlex
import subprocess
from pathlib import Path

import pytest

from ozagia.config import clear_config_cache, load_project_config
from ozagia.core import secrets as S


@pytest.fixture(autouse=True)
def _clean_secret_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "OZAGIA_CONFIG", "OZAGIA_ROOT", "OZAGIA_STATE_DIR",
        "OZAGIA_SECRETS_DIR", "OZAGIA_SECRET_PROVIDERS",
        "OZAGIA_ALLOW_SECRET_NETWORK",
    ):
        monkeypatch.delenv(name, raising=False)


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _legacy(root: Path, text: str) -> Path:
    return _write(root / ".ozagia.toml", text)


def _repo(root: Path, text: str) -> Path:
    return _write(root / "ozagia" / "config.toml", text)


def _broker(root: Path, **kwargs) -> S.SecretBroker:
    kwargs.setdefault("environ", {})
    return S.SecretBroker(root, **kwargs)


# --------------------------------------------------------------------------- #
# references
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("reference,provider,segments", [
    ("secret://env/TOKEN", "env", ("TOKEN",)),
    ("secret://file/api-token", "file", ("api-token",)),
    ("secret://keyring/ozagia/api", "keyring", ("ozagia", "api")),
    ("secret://pass/services/api/token", "pass",
     ("services", "api", "token")),
    ("secret://op/vault/item/field", "op", ("vault", "item", "field")),
    ("secret://session/" + "a" * 32, "session", ("a" * 32,)),
])
def test_parse_ref_valid(reference: str, provider: str,
                         segments: tuple[str, ...]) -> None:
    ref = S.parse_ref(reference)
    assert ref.provider == provider
    assert ref.segments == segments
    assert ref.original == reference
    assert str(ref) == reference
    assert reference in repr(ref)
    assert ref.canonical.startswith("secret://" + provider + "/")


def test_parse_ref_percent_encoding_is_decoded() -> None:
    ref = S.parse_ref("secret://env/TO%4bEN")
    assert ref.segments == ("TOKEN",)
    ref = S.parse_ref("secret://pass/serv%69ces/api")
    assert ref.segments == ("services", "api")


@pytest.mark.parametrize("reference,code", [
    ("", "invalid_ref"),
    ("TOKEN", "invalid_scheme"),
    ("literal://TOKEN", "literal_scheme"),
    ("secret://", "invalid_ref"),
    ("secret://env", "invalid_ref"),
    ("secret://env/", "invalid_ref"),
    ("secret://nope/TOKEN", "unknown_provider"),
    ("secret://env/A/B", "invalid_segment_count"),
    ("secret://env/9TOKEN", "invalid_segment"),
    ("secret://env/A B", "invalid_segment"),
    ("secret://env/A%00B", "invalid_control"),
    ("secret://env/%2E%2E", "invalid_segment"),
    ("secret://file/..", "invalid_traversal"),
    ("secret://file/%2E%2E/token", "invalid_traversal"),
    ("secret://file/a/b", "invalid_segment_count"),
    ("secret://file//etc/passwd", "absolute_path"),
    ("secret://file/%2Fetc%2Fpasswd", "absolute_path"),
    ("secret://keyring/only-one", "invalid_segment_count"),
    ("secret://op/vault/item", "invalid_segment_count"),
    ("secret://pass/a/../b", "invalid_segment"),
    ("secret://session/short", "invalid_segment"),
    ("secret://env/%ZZ", "invalid_encoding"),
])
def test_parse_ref_rejects_invalid(reference: str, code: str) -> None:
    with pytest.raises(S.SecretRefError) as excinfo:
        S.parse_ref(reference)
    assert excinfo.value.code == code


def test_parse_ref_bounded_length() -> None:
    with pytest.raises(S.SecretRefError) as excinfo:
        S.parse_ref("secret://env/" + "A" * S.MAX_REF_LENGTH)
    assert excinfo.value.code == "invalid_length"


def test_absolute_file_reference_needs_explicit_opt_in(tmp_path: Path) -> None:
    target = tmp_path / "token"
    _write(target, "hunter2hunter2\n")
    reference = "secret://file/" + str(target)
    with pytest.raises(S.SecretRefError) as excinfo:
        S.parse_ref(reference)
    assert excinfo.value.code == "absolute_path"
    ref = S.parse_ref(reference, allow_absolute=True)
    assert ref.segments == (str(target),)


# --------------------------------------------------------------------------- #
# masked values and redaction
# --------------------------------------------------------------------------- #
def test_secret_value_masks_str_repr_and_format() -> None:
    value = "hunter2hunter2"
    secret = S.SecretValue("secret://env/TOKEN", value)
    assert value not in str(secret)
    assert value not in repr(secret)
    assert value not in f"{secret}"
    assert value not in f"{secret:>40}"
    assert str(secret) == "SecretValue(secret://env/TOKEN)"
    assert secret.reveal() == value
    assert secret.reference == "secret://env/TOKEN"


def test_secret_value_equality_and_hash_ignore_the_value() -> None:
    one = S.SecretValue("secret://env/TOKEN", "first-secret")
    two = S.SecretValue("secret://env/TOKEN", "second-secret")
    other = S.SecretValue("secret://env/OTHER", "first-secret")
    assert one == two
    assert hash(one) == hash(two)
    assert one != other


def test_redactor_exact_json_url_shell_and_base64() -> None:
    value = 'p"a\\ss word123'
    redactor = S.Redactor()
    redactor.add(value)
    forms = {
        value,
        json.dumps(value)[1:-1],
        S.quote(value, safe=""),
        S.quote(value, safe="/"),
        shlex.quote(value),
        base64.b64encode(value.encode()).decode(),
        base64.urlsafe_b64encode(value.encode()).decode(),
    }
    for form in forms:
        if form and form != S.REDACTED:
            assert value not in redactor.redact("before " + form + " after")
            assert form not in redactor.redact("before " + form + " after")
    assert redactor.contains(value)


def test_redactor_nested_structures_and_keys() -> None:
    value = "hunter2hunter2"
    redactor = S.Redactor()
    redactor.add(value)
    payload = {
        value: [value, {"nested": value, "number": 3}],
        "tuple": (value, "plain"),
    }
    result = redactor.redact_structure(payload)
    assert value not in json.dumps(result)
    assert result["tuple"][1] == "plain"
    assert result[S.REDACTED][1]["number"] == 3


def test_redactor_longest_first_and_minimum_length() -> None:
    redactor = S.Redactor()
    redactor.add("abcdefgh")
    redactor.add("abcdefghij")
    assert redactor.redact("abcdefghij") == S.REDACTED
    short = S.Redactor()
    short.add("short")
    short.add("")
    assert short.redact("short") == "short"
    assert len(short) == 0


def test_redactor_skips_trivial_values_and_empty_replacement() -> None:
    redactor = S.Redactor()
    for value in ("", "       ", "aaaaaaaa", "true", "changeme", "test"):
        redactor.add(value)
    assert len(redactor) == 0
    assert redactor.redact("") == ""
    assert redactor.redact("plain text") == "plain text"
    assert "values=" in repr(redactor)
    assert "changeme" not in repr(redactor)


def test_redactor_bounded_registry() -> None:
    redactor = S.Redactor(max_values=2)
    redactor.add("firstsecret1")
    redactor.add("secondsecret2")
    redactor.add("thirdsecret3")
    assert len(redactor) == 2
    assert not redactor.contains("firstsecret1")
    assert redactor.contains("secondsecret2")


# --------------------------------------------------------------------------- #
# configuration
# --------------------------------------------------------------------------- #
def test_secret_config_defaults(tmp_path: Path) -> None:
    config = S.SecretConfig.load(tmp_path, environ={})
    assert config.providers == ("env", "file")
    assert config.allow_cli == ()
    assert config.allow_network is False
    assert config.allow_absolute_files is False
    assert config.max_bytes == 4096
    assert config.redact_output is True
    assert config.errors == ()
    assert config.unknown == ()
    assert set(config.sources.values()) == {"default"}


def test_secret_config_reads_repository_and_legacy_files(tmp_path: Path) -> None:
    _repo(tmp_path, """
[secrets]
providers = ["env", "file", "keyring"]
allow_cli = ["pass"]
allow_network = true
allow_absolute_files = true
max_bytes = 8192
redact_output = false
""")
    config = S.SecretConfig.load(tmp_path, environ={})
    assert config.providers == ("env", "file", "keyring")
    assert config.allow_cli == ("pass",)
    assert config.allow_network is True
    assert config.allow_absolute_files is True
    assert config.max_bytes == 8192
    assert config.redact_output is False
    assert config.sources["providers"] == "repo"
    assert config.errors == ()

    _legacy(tmp_path, "[secrets]\nproviders = [\"file\"]\n")
    config = S.SecretConfig.load(tmp_path, environ={})
    assert config.providers == ("file",)
    assert config.sources["providers"] == "legacy"
    # Keys the legacy file omits still come from the repository file.
    assert config.allow_cli == ("pass",)


def test_secret_config_env_overrides(tmp_path: Path) -> None:
    _repo(tmp_path, """
[secrets]
providers = ["file"]
allow_network = false
""")
    config = S.SecretConfig.load(tmp_path, environ={
        "OZAGIA_SECRET_PROVIDERS": "env",
        "OZAGIA_ALLOW_SECRET_NETWORK": "yes",
    })
    assert config.providers == ("env",)
    assert config.allow_network is True
    assert config.sources["providers"] == "env"
    assert config.sources["allow_network"] == "env"


def test_secret_config_reports_unknown_and_invalid_values(
        tmp_path: Path) -> None:
    path = _repo(tmp_path, """
[secrets]
providers = ["env", "bogus"]
max_bytes = -1
unknown_setting = true
""")
    config = S.SecretConfig.load(tmp_path, environ={})
    assert config.providers == ("env", "file")
    assert config.max_bytes == 4096
    assert "unknown_setting" in config.unknown
    assert any(str(path) in message for message in config.errors)
    assert any("providers" in message for message in config.errors)
    assert any("max_bytes" in message for message in config.errors)


def test_secret_config_malformed_legacy_is_forgiving_but_reported(
        tmp_path: Path) -> None:
    _repo(tmp_path, "[secrets]\nproviders = [\"file\"]\n")
    _write(tmp_path / ".ozagia.toml", "[secrets\n")
    config = S.SecretConfig.load(tmp_path, environ={})
    assert config.providers == ("file",)
    assert config.errors


def test_secret_config_caps_max_bytes(tmp_path: Path) -> None:
    _repo(tmp_path, "[secrets]\nmax_bytes = 2000000\n")
    config = S.SecretConfig.load(tmp_path, environ={})
    assert config.max_bytes == S.MAX_CONFIG_BYTES


def test_secrets_is_an_owned_config_section(tmp_path: Path) -> None:
    _legacy(tmp_path, """
[index]
source_dirs = ["src"]

[secrets]
providers = ["env"]
""")
    clear_config_cache()
    cfg = load_project_config(tmp_path)
    assert cfg.source_dirs == ("src",)
    assert ("secrets", "read by ozagia.core.secrets") in cfg.unmapped


def test_secrets_dir_override_is_relative_to_root(tmp_path: Path) -> None:
    base = S.secrets_dir(tmp_path, environ={"OZAGIA_SECRETS_DIR": "vault"})
    assert base == (tmp_path / "vault").resolve()
    base = S.secrets_dir(tmp_path, environ={"OZAGIA_SECRETS_DIR": str(tmp_path)})
    assert base == tmp_path.resolve()


# --------------------------------------------------------------------------- #
# env and file providers
# --------------------------------------------------------------------------- #
def test_env_provider_resolve_check_and_cache(tmp_path: Path) -> None:
    calls: list[str] = []

    def fake_env(ref: S.SecretRef) -> str:
        calls.append(ref.name)
        return "hunter2hunter2"

    broker = _broker(tmp_path, provider_overrides={"env": fake_env})
    first = broker.resolve("secret://env/TOKEN")
    second = broker.resolve("secret://env/TOKEN")
    assert first.reveal() == "hunter2hunter2"
    assert first is second
    assert calls == ["TOKEN"]
    assert broker.redactor.contains("hunter2hunter2")
    broker.clear_cache()
    broker.resolve("secret://env/TOKEN")
    assert calls == ["TOKEN", "TOKEN"]


def test_env_provider_missing_and_empty(tmp_path: Path) -> None:
    broker = _broker(tmp_path, environ={"PRESENT": "hunter2hunter2",
                                        "EMPTY": ""})
    assert broker.resolve("secret://env/PRESENT").reveal() == "hunter2hunter2"
    assert broker.check("secret://env/MISSING") == S.STATUS_MISSING
    assert broker.check("secret://env/EMPTY").code == "empty"
    with pytest.raises(S.SecretNotFound):
        broker.resolve("secret://env/MISSING")


def _file_broker(tmp_path: Path, *, config: S.SecretConfig | None = None,
                 base: Path | None = None) -> tuple[S.SecretBroker, Path]:
    base = base or (tmp_path / "secrets")
    base.mkdir(parents=True, exist_ok=True)
    broker = S.SecretBroker(
        tmp_path, config=config,
        environ={"OZAGIA_SECRETS_DIR": str(base)})
    return broker, base


def test_file_provider_newline_trim_permissions_and_size(tmp_path: Path) -> None:
    broker, base = _file_broker(tmp_path)
    path = base / "token"
    path.write_text("hunter2hunter2\n", encoding="utf-8")
    os.chmod(path, 0o644)
    secret = broker.resolve("secret://file/token")
    assert secret.reveal() == "hunter2hunter2"
    assert secret.warnings and "group/world" in secret.warnings[0]
    assert "mode" in broker.check("secret://file/token").detail

    config = S.SecretConfig(providers=("env", "file"), max_bytes=4)
    broker = S.SecretBroker(
        tmp_path, config=config,
        environ={"OZAGIA_SECRETS_DIR": str(base)})
    assert broker.check("secret://file/token") == S.STATUS_INVALID
    with pytest.raises(S.SecretProviderError):
        broker.resolve("secret://file/token")


def test_file_provider_missing_empty_and_directory(tmp_path: Path) -> None:
    broker, base = _file_broker(tmp_path)
    assert broker.check("secret://file/missing") == S.STATUS_MISSING
    (base / "empty").write_text("", encoding="utf-8")
    assert broker.check("secret://file/empty") == S.STATUS_MISSING
    (base / "folder").mkdir()
    assert broker.check("secret://file/folder") == S.STATUS_INVALID
    with pytest.raises(S.SecretNotFound):
        broker.resolve("secret://file/missing")


def test_file_provider_rejects_symlink_escape(tmp_path: Path) -> None:
    broker, base = _file_broker(tmp_path)
    outside = tmp_path / "outside"
    outside.write_text("hunter2hunter2", encoding="utf-8")
    os.symlink(outside, base / "link")
    check = broker.check("secret://file/link")
    assert check == S.STATUS_DENIED
    assert check.code == "path_escape"
    with pytest.raises(S.SecretDenied):
        broker.resolve("secret://file/link")


def test_file_provider_relative_symlink_inside_base_is_allowed(
        tmp_path: Path) -> None:
    broker, base = _file_broker(tmp_path)
    target = base / "real"
    target.write_text("hunter2hunter2", encoding="utf-8")
    os.symlink(target, base / "link")
    assert broker.resolve("secret://file/link").reveal() == "hunter2hunter2"


def test_file_provider_absolute_requires_config(tmp_path: Path) -> None:
    target = tmp_path / "outside-token"
    target.write_text("hunter2hunter2", encoding="utf-8")
    reference = "secret://file/" + str(target)
    broker, _base = _file_broker(tmp_path)
    assert broker.check(reference) == S.STATUS_INVALID
    assert broker.check(reference).code == "absolute_path"

    config = S.SecretConfig(providers=("env", "file"),
                            allow_absolute_files=True)
    broker = S.SecretBroker(tmp_path, config=config, environ={})
    assert broker.resolve(reference).reveal() == "hunter2hunter2"


# --------------------------------------------------------------------------- #
# optional providers via injected fakes
# --------------------------------------------------------------------------- #
class _Keyring:
    def __init__(self, values: dict[tuple[str, str], str]) -> None:
        self.values = values

    def get_password(self, service: str, account: str) -> str | None:
        return self.values.get((service, account))


def test_keyring_provider_available_and_missing(tmp_path: Path) -> None:
    fake = _Keyring({("ozagia", "api"): "hunter2hunter2"})
    config = S.SecretConfig(providers=("keyring",))
    broker = S.SecretBroker(
        tmp_path, config=config, environ={},
        module_finder=lambda name: fake)
    assert broker.resolve(
        "secret://keyring/ozagia/api").reveal() == "hunter2hunter2"
    assert broker.check("secret://keyring/ozagia/other") == S.STATUS_MISSING


def test_keyring_provider_unavailable_without_module(tmp_path: Path) -> None:
    config = S.SecretConfig(providers=("keyring",))

    def missing(name: str):
        raise ImportError(name)

    broker = S.SecretBroker(
        tmp_path, config=config, environ={}, module_finder=missing)
    check = broker.check("secret://keyring/ozagia/api")
    assert check == S.STATUS_PROVIDER_UNAVAILABLE
    with pytest.raises(S.SecretProviderUnavailable):
        broker.resolve("secret://keyring/ozagia/api")


def test_optional_providers_are_denied_unless_enabled(tmp_path: Path) -> None:
    broker = _broker(tmp_path)
    assert broker.check("secret://keyring/ozagia/api") == S.STATUS_DENIED
    assert broker.check("secret://pass/token") == S.STATUS_DENIED
    assert broker.check("secret://op/vault/item/field") == S.STATUS_DENIED


class _Proc:
    def __init__(self, returncode: int = 0, stdout: bytes = b"") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = b""


def test_pass_provider_command_and_sanitized_environment(
        tmp_path: Path) -> None:
    calls: list[tuple[list[str], dict]] = []

    def runner(argv, **kwargs):
        calls.append((list(argv), kwargs))
        return _Proc(0, b"hunter2hunter2\n")

    config = S.SecretConfig(providers=("pass",), allow_cli=("pass",))
    broker = S.SecretBroker(
        tmp_path, config=config,
        environ={"PATH": "/usr/bin", "OZAGIA_SECRET_TOKEN": "leak"},
        runner=runner, which=lambda name: "/usr/bin/pass")
    secret = broker.resolve("secret://pass/services/api/token")
    assert secret.reveal() == "hunter2hunter2"
    argv, kwargs = calls[0]
    assert argv == ["/usr/bin/pass", "show", "services/api/token"]
    assert kwargs["shell"] is False
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["timeout"] == 5
    assert "OZAGIA_SECRET_TOKEN" not in kwargs["env"]
    assert "leak" not in json.dumps(kwargs["env"])


def test_pass_provider_cli_gate_and_missing_binary(tmp_path: Path) -> None:
    config = S.SecretConfig(providers=("pass",))
    broker = S.SecretBroker(
        tmp_path, config=config, environ={},
        which=lambda name: "/usr/bin/pass")
    assert broker.check("secret://pass/token") == S.STATUS_DENIED
    assert broker.check("secret://pass/token").code == "cli_not_allowed"

    config = S.SecretConfig(providers=("pass",), allow_cli=("pass",))
    broker = S.SecretBroker(
        tmp_path, config=config, environ={}, which=lambda name: None)
    assert broker.check("secret://pass/token") == S.STATUS_PROVIDER_UNAVAILABLE


def test_pass_provider_failure_never_exposes_output(tmp_path: Path) -> None:
    secret_text = "hunter2hunter2"

    def runner(argv, **kwargs):
        return _Proc(1, secret_text.encode())

    config = S.SecretConfig(providers=("pass",), allow_cli=("pass",))
    broker = S.SecretBroker(
        tmp_path, config=config, environ={}, runner=runner,
        which=lambda name: "/usr/bin/pass")
    with pytest.raises(S.SecretProviderError) as excinfo:
        broker.resolve("secret://pass/token")
    assert excinfo.value.code == "pass_failed"
    assert secret_text not in str(excinfo.value)
    assert secret_text not in repr(excinfo.value)


def test_pass_provider_timeout_is_bounded(tmp_path: Path) -> None:
    def runner(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs.get("timeout", 5))

    config = S.SecretConfig(providers=("pass",), allow_cli=("pass",))
    broker = S.SecretBroker(
        tmp_path, config=config, environ={}, runner=runner,
        which=lambda name: "/usr/bin/pass")
    with pytest.raises(S.SecretProviderError) as excinfo:
        broker.resolve("secret://pass/token")
    assert excinfo.value.code == "timeout"


def test_op_provider_requires_cli_and_network(tmp_path: Path) -> None:
    config = S.SecretConfig(providers=("op",), allow_cli=("op",))
    broker = S.SecretBroker(
        tmp_path, config=config, environ={}, which=lambda name: "/usr/bin/op")
    check = broker.check("secret://op/vault/item/field")
    assert check == S.STATUS_DENIED
    assert check.code == "network_denied"

    calls: list[list[str]] = []

    def runner(argv, **kwargs):
        calls.append(list(argv))
        return _Proc(0, b"hunter2hunter2\n")

    config = S.SecretConfig(providers=("op",), allow_cli=("op",),
                            allow_network=True)
    broker = S.SecretBroker(
        tmp_path, config=config, environ={}, runner=runner,
        which=lambda name: "/usr/bin/op")
    assert broker.resolve(
        "secret://op/vault/item/field").reveal() == "hunter2hunter2"
    assert calls == [
        ["/usr/bin/op", "read", "op://vault/item/field"]]


def test_providers_and_describe_have_no_values(tmp_path: Path) -> None:
    broker = _broker(tmp_path, environ={"TOKEN": "hunter2hunter2"})
    broker.resolve("secret://env/TOKEN")
    providers = broker.providers()
    names = {item["name"] for item in providers}
    assert {"env", "file", "keyring", "pass", "op", "session"} <= names
    payload = json.dumps(broker.describe(), sort_keys=True)
    assert "hunter2hunter2" not in payload
    assert "session" in payload


# --------------------------------------------------------------------------- #
# environment boundary
# --------------------------------------------------------------------------- #
def test_resolve_env_refs_plain_keys_and_redaction(tmp_path: Path) -> None:
    broker = _broker(tmp_path, environ={"TOKEN": "hunter2hunter2"})
    resolved = broker.resolve_env({
        "TOKEN": "secret://env/TOKEN",
        "PLAIN": "not-a-secret",
    })
    assert resolved.values == {"TOKEN": "hunter2hunter2",
                               "PLAIN": "not-a-secret"}
    assert resolved.refs["TOKEN"].provider == "env"
    assert resolved.plain_keys == ("PLAIN",)
    assert "hunter2hunter2" not in repr(resolved)
    assert broker.redactor.redact("x hunter2hunter2") == "x [REDACTED]"


def test_resolve_env_rejects_bad_names_and_values(tmp_path: Path) -> None:
    broker = _broker(tmp_path)
    with pytest.raises(S.SecretError) as excinfo:
        broker.resolve_env({"BAD-NAME": "x"})
    assert excinfo.value.code == "invalid_env_key"
    with pytest.raises(S.SecretError) as excinfo:
        broker.resolve_env({"GOOD": 3})
    assert excinfo.value.code == "invalid_env_value"


# --------------------------------------------------------------------------- #
# statuses, discovery and the owned config path
# --------------------------------------------------------------------------- #
def test_check_statuses_have_no_value(tmp_path: Path) -> None:
    broker = _broker(tmp_path, environ={"TOKEN": "hunter2hunter2"})
    available = broker.check("secret://env/TOKEN")
    assert available == S.STATUS_AVAILABLE
    assert available.status == S.STATUS_AVAILABLE
    assert "hunter2hunter2" not in repr(available)
    assert "hunter2hunter2" not in json.dumps(available.as_dict())
    invalid = broker.check("not-a-ref")
    assert invalid == S.STATUS_INVALID
    assert invalid.code == "invalid_scheme"
    results = broker.check_many(["secret://env/TOKEN",
                                 "secret://env/MISSING"])
    assert [str(item) for item in results] == ["available", "missing"]


def test_discover_refs_from_both_config_paths(tmp_path: Path) -> None:
    legacy = _legacy(tmp_path, """
[custom]
token = "secret://env/LEGACY_TOKEN"
""")
    repo = _repo(tmp_path, """
[custom]
items = ["secret://file/repo-token", "plain"]
nested = { value = "secret://op/vault/item/field" }
bad = "secret://file/../escape"
""")
    broker = _broker(tmp_path)
    hits = broker.discover_refs()
    by_key = {hit.key: hit for hit in hits}
    assert by_key["custom.token"].ref == "secret://env/LEGACY_TOKEN"
    assert by_key["custom.token"].source == legacy.resolve()
    assert by_key["custom.items[0]"].source == repo.resolve()
    assert by_key["custom.nested.value"].ref == "secret://op/vault/item/field"
    assert by_key["custom.bad"].valid is False
    assert by_key["custom.bad"].error == "invalid_traversal"
    assert all("hunter2" not in repr(hit) for hit in hits)


# --------------------------------------------------------------------------- #
# session-scoped capture
# --------------------------------------------------------------------------- #
def test_session_capture_metadata_and_resolution(tmp_path: Path) -> None:
    store = S.SessionSecretStore()
    value = "hunter2hunter2"
    captured = store.capture(value, harness="opencode", session="s1",
                             label="api token")
    assert captured.ref.startswith("secret://session/")
    assert value not in captured.ref
    assert captured.fingerprint.startswith("sha256:")
    assert value not in repr(captured)
    assert value not in json.dumps(captured.as_dict())
    listed = store.list("opencode", "s1")
    assert len(listed) == 1
    assert listed[0].ref == captured.ref
    assert value not in repr(listed)
    secret = store.resolve(captured.ref, harness="opencode", session="s1")
    assert secret.reveal() == value
    assert value not in str(secret)


def test_session_capture_cross_session_and_missing_context() -> None:
    store = S.SessionSecretStore()
    captured = store.capture("hunter2hunter2", harness="opencode", session="s1")
    with pytest.raises(S.SecretDenied) as excinfo:
        store.resolve(captured.ref, harness="opencode", session="s2")
    assert excinfo.value.code == "session_mismatch"
    with pytest.raises(S.SecretDenied) as excinfo:
        store.resolve(captured.ref, harness=None, session=None)
    assert excinfo.value.code == "session_context_required"
    assert store.status(captured.ref, harness="opencode",
                        session="s2")[0] == S.STATUS_DENIED
    assert store.status(captured.ref, harness=None,
                        session=None)[0] == S.STATUS_DENIED


def test_session_capture_expiry_and_prune() -> None:
    now = [1000.0]
    store = S.SessionSecretStore(clock=lambda: now[0])
    captured = store.capture("hunter2hunter2", harness="h", session="s",
                             ttl_s=10)
    assert captured.expires - captured.created == 10
    now[0] = 1011.0
    with pytest.raises(S.SecretNotFound) as excinfo:
        store.resolve(captured.ref, harness="h", session="s")
    assert excinfo.value.code == "expired"
    assert store.list("h", "s") == []
    assert store.prune() == 0


def test_session_capture_ttl_is_capped() -> None:
    store = S.SessionSecretStore(max_ttl_s=10)
    captured = store.capture("hunter2hunter2", harness="h", session="s",
                             ttl_s=100)
    assert captured.expires - captured.created == 10
    with pytest.raises(S.SecretError) as excinfo:
        store.capture("hunter2hunter2", harness="h", session="s", ttl_s=0)
    assert excinfo.value.code == "invalid_ttl"


def test_session_capture_bounds_entries_and_value_size() -> None:
    store = S.SessionSecretStore(max_entries=2, max_value_bytes=16)
    first = store.capture("firstsecret", harness="h", session="s")
    second = store.capture("secondsecret", harness="h", session="s")
    third = store.capture("thirdsecret", harness="h", session="s")
    assert [item.ref for item in store.list("h", "s")] == [
        second.ref, third.ref]
    with pytest.raises(S.SecretNotFound):
        store.resolve(first.ref, harness="h", session="s")
    with pytest.raises(S.SecretError) as excinfo:
        store.capture("12345678901234567", harness="h", session="s")
    assert excinfo.value.code == "too_large"


def test_session_capture_session_bound() -> None:
    store = S.SessionSecretStore(max_sessions=1)
    first = store.capture("hunter2hunter2", harness="h", session="one")
    store.capture("othersecret", harness="h", session="two")
    assert store.list("h", "one") == []
    with pytest.raises(S.SecretNotFound):
        store.resolve(first.ref, harness="h", session="one")


def test_session_capture_forget_and_clear() -> None:
    store = S.SessionSecretStore()
    one = store.capture("hunter2hunter2", harness="h", session="s")
    store.capture("othersecret", harness="h", session="s")
    assert store.forget(one.ref, harness="h", session="s") is True
    assert store.forget(one.ref, harness="h", session="s") is False
    assert store.clear("h", "s") == 1
    assert store.list("h", "s") == []
    assert store.clear("h", "s") == 0


def test_session_store_repr_and_describe_have_no_values() -> None:
    store = S.SessionSecretStore()
    store.capture("hunter2hunter2", harness="h", session="s")
    assert "hunter2hunter2" not in repr(store)
    assert "hunter2hunter2" not in json.dumps(store.describe())


# --------------------------------------------------------------------------- #
# model-initiated requests (metadata only)
# --------------------------------------------------------------------------- #
def test_secret_request_lifecycle_and_fulfillment() -> None:
    now = [1000.0]
    store = S.SessionSecretStore(clock=lambda: now[0])
    value = "request-canary-value-1234"
    request = store.request(
        harness="opencode", session="s1", label="OpenAI API key",
        purpose="The task calls the OpenAI API.", ttl_s=60)
    assert request.id.startswith("req_")
    assert request.state == S.SESSION_REQUEST_PENDING
    assert request.ref is None and request.fingerprint is None
    assert value not in repr(request)
    assert value not in json.dumps(request.as_dict())
    assert request.expires - request.created == 60

    same = store.request(harness="opencode", session="s1",
                         label="OpenAI API key",
                         purpose="The task calls the OpenAI API.")
    assert same.id == request.id
    assert len(store.requests("opencode", "s1")) == 1

    assert store.request_status(request.id, harness="opencode",
                                session="s1").state == (
                                    S.SESSION_REQUEST_PENDING)
    with pytest.raises(S.SecretDenied) as excinfo:
        store.request_status(request.id, harness="opencode", session="s2")
    assert excinfo.value.code == "request_mismatch"
    with pytest.raises(S.SecretDenied) as excinfo:
        store.fulfill_request(request.id, value, harness="opencode",
                              session="s2")
    assert excinfo.value.code == "request_mismatch"
    with pytest.raises(S.SecretNotFound) as excinfo:
        store.request_status("req_does-not-exist", harness="opencode",
                             session="s1")
    assert excinfo.value.code == "request_not_found"

    fulfilled = store.fulfill_request(request.id, value, harness="opencode",
                                      session="s1", ttl_s=120)
    assert fulfilled.state == S.SESSION_REQUEST_FULFILLED
    assert fulfilled.ref.startswith("secret://session/")
    assert fulfilled.fingerprint.startswith("sha256:")
    assert value not in fulfilled.ref
    assert value not in json.dumps(fulfilled.as_dict())
    assert value not in repr(store)
    assert value not in json.dumps(store.describe())
    resolved = store.resolve(fulfilled.ref, harness="opencode", session="s1")
    assert resolved.reveal() == value
    assert len(store) == 1

    # Idempotent: a second value is ignored, not captured.
    again = store.fulfill_request(request.id, "another-value-5678",
                                  harness="opencode", session="s1")
    assert again.ref == fulfilled.ref
    assert len(store) == 1
    with pytest.raises(S.SecretDenied) as excinfo:
        store.resolve(fulfilled.ref, harness="opencode", session="s2")
    assert excinfo.value.code == "session_mismatch"

    listed = store.requests("opencode", "s1")
    assert [item.state for item in listed] == [S.SESSION_REQUEST_FULFILLED]
    assert value not in json.dumps([item.as_dict() for item in listed])


def test_secret_request_decline_is_terminal_and_idempotent() -> None:
    store = S.SessionSecretStore()
    request = store.request(harness="h", session="s", label="api token")
    declined = store.decline_request(request.id, harness="h", session="s")
    assert declined.state == S.SESSION_REQUEST_DECLINED
    assert declined.ref is None and declined.fingerprint is None
    again = store.decline_request(request.id, harness="h", session="s")
    assert again.state == S.SESSION_REQUEST_DECLINED
    with pytest.raises(S.SecretError) as excinfo:
        store.fulfill_request(request.id, "hunter2hunter2",
                              harness="h", session="s")
    assert excinfo.value.code == "request_closed"
    with pytest.raises(S.SecretError) as excinfo:
        store.decline_request("req_unknown-request", harness="h", session="s")
    assert excinfo.value.code == "request_not_found"
    # A declined duplicate may ask again.
    fresh = store.request(harness="h", session="s", label="api token")
    assert fresh.id != request.id
    assert fresh.state == S.SESSION_REQUEST_PENDING


def test_secret_request_expiry_drops_references_and_bounded_pending() -> None:
    now = [2000.0]
    store = S.SessionSecretStore(clock=lambda: now[0],
                                 max_pending_requests=2,
                                 max_requests=3)
    first = store.request(harness="h", session="s", label="one", ttl_s=10)
    store.request(harness="h", session="s", label="two", ttl_s=60)
    with pytest.raises(S.SecretError) as excinfo:
        store.request(harness="h", session="s", label="three")
    assert excinfo.value.code == "request_limit"

    now[0] = 2011.0
    expired = store.request_status(first.id, harness="h", session="s")
    assert expired.state == S.SESSION_REQUEST_EXPIRED
    assert expired.ref is None and expired.fingerprint is None
    with pytest.raises(S.SecretError) as excinfo:
        store.fulfill_request(first.id, "hunter2hunter2",
                              harness="h", session="s")
    assert excinfo.value.code == "request_closed"
    # Expired records are terminal, so the pending slot is free again.
    store.request(harness="h", session="s", label="one")


def test_secret_request_fulfilled_capture_expiry_drops_the_ref() -> None:
    now = [3000.0]
    store = S.SessionSecretStore(clock=lambda: now[0])
    request = store.request(harness="h", session="s", label="token")
    fulfilled = store.fulfill_request(request.id, "hunter2hunter2",
                                      harness="h", session="s", ttl_s=10)
    assert fulfilled.ref
    now[0] = 3011.0
    expired = store.request_status(request.id, harness="h", session="s")
    assert expired.state == S.SESSION_REQUEST_EXPIRED
    assert expired.ref is None
    assert "secret://session/" not in json.dumps(expired.as_dict())
    # A fresh request is allowed now, and a new fulfill captures a new value.
    fresh = store.request(harness="h", session="s", label="token")
    assert fresh.id != request.id


def test_secret_request_bounds_and_session_clear() -> None:
    store = S.SessionSecretStore(max_requests=2)
    first = store.request(harness="h", session="s", label="one")
    store.decline_request(first.id, harness="h", session="s")
    store.request(harness="h", session="s", label="two")
    store.request(harness="h", session="s", label="three")
    listed = store.requests("h", "s")
    assert len(listed) == 2
    assert "one" not in {item.label for item in listed}
    assert store.clear_requests("h", "s") == 2
    assert store.requests("h", "s") == []
    assert store.clear_requests("h", "s") == 0


def test_secret_request_validation_has_no_value() -> None:
    store = S.SessionSecretStore()
    for value in ("", "x" * (S.MAX_REQUEST_PURPOSE + 1), "bad\npurpose"):
        with pytest.raises(S.SecretError):
            store.request(harness="h", session="s", label="label",
                          purpose=value)
    with pytest.raises(S.SecretError) as excinfo:
        store.request(harness="h", session="s", label="label",
                      target="file")
    assert excinfo.value.code == "invalid_target"
    with pytest.raises(S.SecretError) as excinfo:
        store.request(harness="h", session="s")
    assert excinfo.value.code == "invalid_label"
    with pytest.raises(S.SecretError) as excinfo:
        store.request_status("../../etc/passwd", harness="h", session="s")
    assert excinfo.value.code == "invalid_request_id"


# --------------------------------------------------------------------------- #
# broker + session provider
# --------------------------------------------------------------------------- #
def test_broker_resolves_session_refs_with_context(tmp_path: Path) -> None:
    store = S.SessionSecretStore()
    value = "hunter2hunter2"
    captured = store.capture(value, harness="opencode", session="s1")
    broker = _broker(tmp_path, session_store=store)
    secret = broker.resolve(captured.ref, harness="opencode", session="s1")
    assert secret.reveal() == value
    assert broker.redactor.contains(value)
    assert broker.redactor.redact(value) == S.REDACTED
    assert broker.check(captured.ref, harness="opencode",
                        session="s1") == S.STATUS_AVAILABLE
    assert broker.check(captured.ref) == S.STATUS_DENIED
    with pytest.raises(S.SecretDenied):
        broker.resolve(captured.ref, harness="opencode", session="s2")
    assert "session" in json.dumps(broker.describe(), sort_keys=True)
    assert "hunter2hunter2" not in json.dumps(broker.describe(), sort_keys=True)


def test_broker_session_refs_survive_a_disabled_provider_list(
        tmp_path: Path) -> None:
    store = S.SessionSecretStore()
    captured = store.capture("hunter2hunter2", harness="h", session="s")
    config = S.SecretConfig(providers=())
    broker = S.SecretBroker(tmp_path, config=config, environ={},
                            session_store=store)
    assert broker.resolve(captured.ref, harness="h",
                          session="s").reveal() == "hunter2hunter2"


def test_resolve_env_with_session_reference(tmp_path: Path) -> None:
    store = S.SessionSecretStore()
    captured = store.capture("hunter2hunter2", harness="h", session="s")
    broker = _broker(tmp_path, session_store=store)
    resolved = broker.resolve_env({"TOKEN": captured.ref, "PLAIN": "x"},
                                  harness="h", session="s")
    assert resolved.values["TOKEN"] == "hunter2hunter2"
    assert resolved.refs["TOKEN"].provider == "session"
    assert resolved.plain_keys == ("PLAIN",)


# --------------------------------------------------------------------------- #
# conservative text detection
# --------------------------------------------------------------------------- #
def test_detect_text_tokens_assignments_and_refs() -> None:
    store = S.SessionSecretStore()
    text = (
        "token ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789\n"
        "api_key = \"sk-proj-abcdefghijklmnopqrstuvwxyz0123456789\"\n"
        "password: \"hunter2hunter2\"\n"
        "token: unquoted-secret-value-12345\n"
        "existing = secret://env/TOKEN\n"
        "example AKIAIOSFODNN7EXAMPLE\n"
        "short = \"abc\"\n"
    )
    capture = S.detect_text(
        text, ref_factory=lambda value, label: store.capture(
            value, harness="h", session="s", label=label))
    assert capture.detected == 4
    assert capture.captured == 4
    assert capture.withheld == 0
    assert "ghp_ABCDEF" not in capture.text
    assert "sk-proj-" not in capture.text
    assert "hunter2hunter2" not in capture.text
    assert "unquoted-secret-value-12345" not in capture.text
    assert "secret://env/TOKEN" in capture.text
    assert "AKIAIOSFODNN7EXAMPLE" in capture.text
    assert "abc" in capture.text
    assert all(item.ref and item.ref.startswith("secret://session/")
               for item in capture.secrets)
    assert "ghp_ABCDEF" not in repr(capture)
    assert "ghp_ABCDEF" not in json.dumps(capture.as_dict())


def test_detect_text_without_factory_redacts_and_withholds() -> None:
    capture = S.detect_text(
        "token ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789\n")
    assert capture.detected == 1
    assert capture.captured == 0
    assert capture.withheld == 1
    assert "ghp_ABCDEF" not in capture.text
    assert S.REDACTED in capture.text


def test_detect_text_cap_and_multiple_secrets() -> None:
    store = S.SessionSecretStore()
    text = (
        "a ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789\n"
        "b ghp_ZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZ\n"
        "c ghp_YYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYY\n"
    )
    capture = S.detect_text(
        text, max_secrets=1,
        ref_factory=lambda value, label: store.capture(
            value, harness="h", session="s", label=label))
    assert capture.detected == 3
    assert capture.captured == 1
    assert capture.withheld == 2
    assert capture.text.count("secret://session/") == 1
    assert capture.text.count(S.REDACTED) == 2
    assert store.counts()["entries"] == 1


def test_detect_text_placeholder_and_short_values_are_left_alone() -> None:
    text = (
        "password = \"changeme\"\n"
        "api_key = \"abc\"\n"
        "token = \"your-token-here\"\n"
    )
    capture = S.detect_text(text)
    assert capture.detected == 0
    assert capture.text == text


def test_detect_text_factory_failure_is_withheld() -> None:
    def factory(value: str, label: str) -> str:
        raise RuntimeError("store unavailable")

    capture = S.detect_text(
        "token ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789", max_secrets=2,
        ref_factory=factory)
    assert capture.detected == 1
    assert capture.withheld == 1
    assert "ghp_ABCDEF" not in capture.text


def test_detect_text_replacement_that_echoes_the_value_is_withheld() -> None:
    capture = S.detect_text(
        "token ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
        ref_factory=lambda value, label: value)
    assert capture.detected == 1
    assert capture.captured == 0
    assert capture.withheld == 1
    assert "ghp_ABCDEF" not in capture.text


def test_detect_text_accepts_captured_secret_result() -> None:
    store = S.SessionSecretStore()
    capture = S.detect_text(
        "token ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
        ref_factory=lambda value, label: store.capture(
            value, harness="h", session="s", label=label))
    assert capture.captured == 1
    assert capture.secrets[0].ref in capture.text
