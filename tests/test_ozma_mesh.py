"""Read-only Ozma bridge configuration, identity mapping and readiness."""
from __future__ import annotations

import hashlib
import json
import socket
import stat
from pathlib import Path

import pytest

from ozagia import cli
from ozagia.core import mesh as M
from ozagia.core import ozma_mesh as O


@pytest.fixture()
def env(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    state = tmp_path / "state"
    monkeypatch.setenv("OZAGIA_ROOT", str(repo))
    monkeypatch.setenv("OZAGIA_STATE_DIR", str(state))
    monkeypatch.delenv("OZAGIA_MESH_ID", raising=False)
    monkeypatch.delenv("OZAGIA_MESH_HARNESS", raising=False)
    monkeypatch.delenv("OZAGIA_MESH_SESSION", raising=False)
    return repo, state


def _write(repo: Path, body: str) -> None:
    (repo / ".ozagia.toml").write_text(body, encoding="utf-8")


def _card(repo: Path, body: bytes = b'{"agent":"agent:reviewer"}\n') -> str:
    path = repo / "card.json"
    path.write_bytes(body)
    return "sha256:" + hashlib.sha256(body).hexdigest()


def _configured(repo: Path, *, mode: str = "status", transport: str = "native",
                verified_by: str = "host", card: bool = True,
                fingerprint: str | None = None, agent: str = "agent:reviewer:wu1",
                extra: str = "") -> str:
    created = _card(repo) if card else ""
    digest = fingerprint if fingerprint is not None else created
    lines = [
        "[ozma_mesh]",
        "enabled = true",
        f'mode = "{mode}"',
        f'transport = "{transport}"',
        f'agent_id = "{agent}"',
        f'verified_by = "{verified_by}"',
    ]
    if card:
        lines.append('card_path = "card.json"')
    if digest:
        lines.append(f'card_fingerprint = "{digest}"')
    _write(repo, "\n".join(lines) + "\n" + extra)
    return digest


def _json(capsys) -> dict:
    return json.loads(capsys.readouterr().out)


def _snapshot(state: Path) -> dict[str, tuple[int, bytes]]:
    if not state.exists():
        return {}
    return {
        str(path.relative_to(state)): (
            stat.S_IMODE(path.stat().st_mode), path.read_bytes())
        for path in sorted(state.rglob("*")) if path.is_file()
    }


def test_config_defaults_off_and_status_is_read_only(env) -> None:
    repo, state = env
    config = O.load_config(repo)
    assert config.configured is False
    assert config.enabled is False
    assert config.mode == "off"
    assert config.agent_id == ""
    assert config.card_path == ""
    payload = O.status(repo)
    assert payload["state"] == "off"
    assert payload["network"] is False
    assert payload["readiness"]["ready"] is True
    assert payload["agent"]["id"] == ""
    assert _snapshot(state) == {}


def test_valid_closed_config_loads_every_field(env) -> None:
    repo, _ = env
    digest = _card(repo)
    _write(repo, f"""
[ozma_mesh]
enabled = true
mode = "status"
transport = "native"
shim_url = "http://127.0.0.1:8787"
allow_remote = false
auth_ref = "secret://ozma/token"
flight_id = "flight-a"
agent_id = "agent:reviewer:wu1"
agent_scope = "user"
card_path = "card.json"
card_fingerprint = "{digest}"
verified_by = "host"
wake_modes = ["passive", "scheduled"]
channels = ["#flight"]
subscriptions = ["#flight"]
priority_floor = 0.45
queue_max_messages = 17
queue_max_bytes = 4096
outbox_ttl_s = 120
ack_timeout_s = 5
dedupe_window_s = 60
message_rate = "10/1min"
""")
    config = O.load_config(repo)
    assert config.configured is True
    assert config.flight_id == "flight-a"
    assert config.agent_scope == "user"
    assert config.auth_ref == "secret://ozma/token"
    assert config.wake_modes == ("passive", "scheduled")
    assert config.channels == ("#flight",)
    assert config.priority_floor == 0.45
    assert config.queue_max_messages == 17
    assert config.message_rate == "10/1min"
    payload = O.status(repo)
    assert payload["state"] == "host_declared"
    assert payload["wake"]["scheduled"]["approved"] is True
    assert payload["wake"]["scheduled"]["spawnable"] is False
    assert payload["auth_ref_set"] is True
    text = json.dumps(payload)
    assert "secret://ozma/token" not in text
    assert "secret://" not in text


def test_unknown_keys_and_invalid_values_are_rejected(env) -> None:
    repo, _ = env
    _write(repo, '[ozma_mesh]\nnot_a_key = 1\n')
    with pytest.raises(O.OzmaMeshConfigError):
        O.load_config(repo)

    cases = [
        'mode = "bridge"',
        'transport = "carrier"',
        'agent_scope = "team"',
        'verified_by = "self"',
        'wake_modes = ["loud"]',
        'card_fingerprint = "sha256:xyz"',
        'agent_id = "reviewer"',
        'flight_id = "bad flight"',
        'shim_url = "http://example.com:8787"',
        'shim_url = "http://user:pass@localhost:8787"',
        'allow_remote = "yes"',
        'queue_max_messages = 0',
        'queue_max_messages = 10001',
        'queue_max_bytes = 1',
        'priority_floor = 1.1',
        'outbox_ttl_s = 0',
        'ack_timeout_s = 0',
        'dedupe_window_s = -1',
        'message_rate = "often"',
    ]
    for line in cases:
        _write(repo, "[ozma_mesh]\n" + line + "\n")
        with pytest.raises(O.OzmaMeshConfigError):
            O.load_config(repo)


def test_remote_shim_needs_explicit_allow_remote(env) -> None:
    repo, _ = env
    _write(repo, '[ozma_mesh]\nshim_url = "http://example.com:8787"\n'
                'allow_remote = true\n')
    assert O.load_config(repo).shim_url == "http://example.com:8787"
    payload = O.status(repo)
    assert payload["shim"]["host"] == "remote"
    assert payload["network"] is False


def test_inline_auth_ref_is_rejected_without_echoing_it(env, capsys) -> None:
    repo, _ = env
    canary = "canary-inline-credential"
    _write(repo, f'[ozma_mesh]\nenabled = true\nmode = "status"\n'
                f'auth_ref = "{canary}"\n')
    with pytest.raises(O.OzmaMeshConfigError) as excinfo:
        O.load_config(repo)
    assert canary not in str(excinfo.value)
    assert cli.main(["mesh", "ozma", "status", "--json"]) == 2
    output = capsys.readouterr().out
    assert canary not in output
    assert json.loads(output)["code"] == "ozma_mesh_config"


def test_mode_cannot_exceed_status_without_host_identity(env) -> None:
    repo, _ = env
    _write(repo, '[ozma_mesh]\nenabled = true\nmode = "messages"\n')
    with pytest.raises(O.OzmaMeshConfigError):
        O.load_config(repo)
    _configured(repo, mode="messages", card=False, agent="")
    with pytest.raises(O.OzmaMeshConfigError):
        O.load_config(repo)


def test_missing_identity_stays_missing_and_never_generates_one(env) -> None:
    repo, state = env
    _write(repo, '[ozma_mesh]\nenabled = true\nmode = "status"\n')
    before = _snapshot(state)
    assert cli.main(["mesh", "ozma", "status", "--json"]) == 0
    # The CLI surface is checked in test_mesh_cli; the module report is direct.
    payload = O.status(repo)
    assert payload["state"] == "missing"
    assert payload["agent"]["id"] == ""
    assert payload["agent"]["present"] is False
    assert payload["card"]["fingerprint"] == "missing"
    check = O.check(repo)
    assert check["ready"] is True
    assert check["ready_for"] == ["status"]
    assert _snapshot(state) == before


def test_fingerprint_match_host_declared_and_none_unverified(env) -> None:
    repo, _ = env
    digest = _configured(repo, verified_by="host")
    payload = O.status(repo)
    assert payload["card"]["fingerprint"] == "match"
    assert payload["verification"]["status"] == "host_declared"
    assert payload["verification"]["cryptographic"] is False
    assert payload["state"] == "host_declared"
    assert O.check(repo)["ready"] is True

    _configured(repo, verified_by="none", fingerprint=digest)
    payload = O.status(repo)
    assert payload["card"]["fingerprint"] == "match"
    assert payload["verification"]["status"] == "fingerprint_matched"
    assert payload["state"] == "unverified"
    assert O.check(repo)["ready"] is True


def test_fingerprint_mismatch_is_never_ready(env) -> None:
    repo, _ = env
    _configured(repo, fingerprint="sha256:" + "0" * 64)
    payload = O.status(repo)
    assert payload["card"]["fingerprint"] == "mismatch"
    assert payload["verification"]["status"] == "fingerprint_mismatch"
    assert payload["state"] == "mismatch"
    check = O.check(repo)
    assert check["ready"] is False
    assert check["ready_for"] == []


def test_missing_card_and_missing_fingerprint(env) -> None:
    repo, _ = env
    _configured(repo, card=False)
    payload = O.status(repo)
    assert payload["state"] == "missing"
    assert payload["card"]["fingerprint"] == "missing"

    _card(repo)
    _write(repo, '[ozma_mesh]\nenabled = true\nmode = "status"\n'
                'agent_id = "agent:reviewer"\ncard_path = "card.json"\n')
    payload = O.status(repo)
    assert payload["card"]["present"] is True
    assert payload["card"]["fingerprint"] == "missing"
    assert payload["verification"]["status"] == "unverified"
    assert payload["state"] == "unverified"


def test_card_symlink_is_unknown(env) -> None:
    repo, _ = env
    target = repo / "real-card.json"
    target.write_text('{"agent":"agent:reviewer"}\n', encoding="utf-8")
    digest = "sha256:" + hashlib.sha256(target.read_bytes()).hexdigest()
    (repo / "card.json").symlink_to(target)
    _write(repo, f'[ozma_mesh]\nenabled = true\nmode = "status"\n'
                f'agent_id = "agent:reviewer"\ncard_path = "card.json"\n'
                f'card_fingerprint = "{digest}"\n')
    payload = O.status(repo)
    assert payload["card"]["fingerprint"] == "unknown"
    assert payload["state"] == "unknown"
    assert O.check(repo)["ready"] is False


def test_flight_defaults_to_mesh_id_and_config_overrides(env, monkeypatch) -> None:
    repo, _ = env
    _write(repo, '[ozma_mesh]\nenabled = true\nmode = "status"\n')
    assert O.status(repo)["flight"] == {
        "id": M.mesh_id(repo), "source": "local_mesh"}
    monkeypatch.setenv("OZAGIA_MESH_ID", "flight-env")
    assert O.status(repo)["flight"] == {
        "id": "flight-env", "source": "local_mesh"}
    _write(repo, '[ozma_mesh]\nenabled = true\nmode = "status"\n'
                'flight_id = "flight-config"\n')
    assert O.status(repo)["flight"] == {
        "id": "flight-config", "source": "config"}


def test_map_save_writes_private_record_and_is_flight_scoped(env) -> None:
    repo, state = env
    _configured(repo, extra='flight_id = "flight-a"\n')
    first = O.map_report(repo, harness="opencode", session="a", save=True)
    record_path = Path(first["record"]["path"])
    assert first["record"]["saved"] is True
    assert record_path.is_file()
    assert stat.S_IMODE(record_path.stat().st_mode) == 0o600
    identities = state / "mesh" / "ozma" / "identities"
    assert stat.S_IMODE(identities.stat().st_mode) == 0o700
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert record["flight_id"] == "flight-a"
    assert record["ozagia"]["harness"] == "opencode"
    assert record["ozma"]["agent_id"] == "agent:reviewer:wu1"
    assert record["verification"]["cryptographic"] is False
    assert "auth_ref" not in json.dumps(record)

    _configured(repo, extra='flight_id = "flight-b"\n')
    second = O.map_report(repo, harness="opencode", session="a")
    assert second["record"]["exists"] is False
    assert Path(second["record"]["path"]) != record_path
    assert O.status(repo, harness="opencode",
                    session="a")["mapping"]["exists"] is False

    _configured(repo, extra='flight_id = "flight-a"\n')
    again = O.map_report(repo, harness="opencode", session="a")
    assert again["record"]["exists"] is True
    assert Path(again["record"]["path"]) == record_path


def test_map_without_save_and_status_check_do_not_write(env) -> None:
    repo, state = env
    _configured(repo)
    before = _snapshot(state)
    assert cli.main(["mesh", "ozma", "status", "--json"]) == 0
    assert cli.main(["mesh", "ozma", "check", "--json"]) == 0
    assert cli.main(["mesh", "ozma", "map", "--json"]) == 0
    assert _snapshot(state) == before


def test_a2a_transport_is_lossy_with_named_fields(env) -> None:
    repo, _ = env
    _configured(repo, transport="a2a")
    payload = O.status(repo)
    assert payload["lossy"] is True
    assert set(payload["loss_fields"]) == set(O.A2A_LOSS_FIELDS)
    assert payload["network"] is False
    mapped = O.map_report(repo, harness="opencode", session="a")
    assert {row["field"] for row in mapped["loss_table"]} == set(
        O.A2A_LOSS_FIELDS)
    assert {row["a2a"] for row in mapped["loss_table"]} == {"lost"}
    checked = O.check(repo)
    assert checked["lossy"] is True
    assert set(checked["loss_fields"]) == set(O.A2A_LOSS_FIELDS)

    _configured(repo, transport="native")
    native = O.status(repo)
    assert native["lossy"] is False
    assert native["loss_fields"] == []


def test_live_check_is_unavailable_without_calling_out(env, monkeypatch) -> None:
    repo, _ = env
    _configured(repo)

    def forbidden(*args, **kwargs):
        raise AssertionError("the read-only bridge must not open a socket")

    monkeypatch.setattr(socket, "socket", forbidden)
    assert O.status(repo)["network"] is False
    checked = O.check(repo, live=True)
    assert checked["live"]["state"] == "unavailable"
    assert checked["live"]["code"] == "live_not_configured"
    assert checked["live"]["network"] is False
    assert checked["network"] is False
    assert cli.main(["mesh", "ozma", "check", "--live", "--json"]) == 0


def test_messages_mode_is_not_ready_even_when_host_declared(env, capsys) -> None:
    repo, _ = env
    _configured(repo, mode="messages")
    checked = O.check(repo)
    assert checked["state"] == "host_declared"
    assert checked["ready"] is False
    assert checked["ready_for"] == ["status"]
    assert cli.main(["mesh", "ozma", "check", "--require-ready", "--json"]) == 1
    payload = _json(capsys)
    assert payload["ready"] is False
    assert any("host_declared" in reason for reason in payload["reasons"])


def test_status_and_check_do_not_network_or_write_with_identity(env,
                                                               monkeypatch) -> None:
    repo, state = env
    _configured(repo)

    def forbidden(*args, **kwargs):
        raise AssertionError("no network in read-only status")

    monkeypatch.setattr(socket, "socket", forbidden)
    before = _snapshot(state)
    payload = O.status(repo, harness="opencode", session="a")
    assert payload["identity"]["harness"] == "opencode"
    assert payload["network"] is False
    assert O.check(repo, harness="opencode", session="a")["ready"] is True
    assert _snapshot(state) == before
