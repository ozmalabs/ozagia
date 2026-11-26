"""Stage G Ozma bridge: envelopes, outbox/inbox, claims, channels and CLI."""
from __future__ import annotations

import hashlib
import json
import socket
import stat
from pathlib import Path

import pytest

from ozagia import cli
from ozagia.core import mesh as M
from ozagia.core import ozma_bridge as B
from ozagia.core import ozma_mesh as O


class FakeShim:
    """In-process shim used only by tests; it never opens a socket."""

    def __init__(self, inbound=(), *, accept=True) -> None:
        self.inbound = list(inbound)
        self.accept = accept
        self.delivered: list[dict] = []
        self.polled = 0

    def deliver(self, envelope):
        self.delivered.append(dict(envelope))
        if callable(self.accept):
            return self.accept(envelope)
        return {"accepted": self.accept, "message_id": "remote-" + str(envelope["id"])}

    def poll(self, after, limit):
        self.polled += 1
        return self.inbound[:limit]


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


def _card(repo: Path) -> str:
    path = repo / "card.json"
    path.write_text('{"agent":"agent:reviewer"}\n', encoding="utf-8")
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _configured(repo: Path, *, mode: str = "messages", transport: str = "native",
                channels=("#flight",), subscriptions=("#flight",),
                priority_floor: float = 0.3, rate: str = "60/5min",
                max_messages: int = 200, max_bytes: int = 262_144,
                ttl: int = 86_400, dedupe: int = 86_400,
                extra: str = "") -> O.OzmaMeshConfig:
    digest = _card(repo)
    lines = [
        "[ozma_mesh]",
        "enabled = true",
        f'mode = "{mode}"',
        f'transport = "{transport}"',
        'agent_id = "agent:reviewer:wu1"',
        'card_path = "card.json"',
        f'card_fingerprint = "{digest}"',
        'verified_by = "host"',
        "channels = [" + ", ".join(f'"{item}"' for item in channels) + "]",
        "subscriptions = ["
        + ", ".join(f'"{item}"' for item in subscriptions) + "]",
        f"priority_floor = {priority_floor}",
        f'message_rate = "{rate}"',
        f"queue_max_messages = {max_messages}",
        f"queue_max_bytes = {max_bytes}",
        f"outbox_ttl_s = {ttl}",
        f"dedupe_window_s = {dedupe}",
    ]
    (repo / ".ozagia.toml").write_text(
        "\n".join(lines) + "\n" + extra, encoding="utf-8")
    return O.load_config(repo)


def _envelope(repo: Path, config: O.OzmaMeshConfig, message_id: str = "ozm_1",
              ozma_type: str = "heads-up", body=None, *,
              direction: str = "inbound", **extra):
    return B.build_envelope(
        flight_id=B.flight_id(repo, config), message_id=message_id,
        ozma_type=ozma_type, body=body or {"text": "hello"},
        direction=direction, channel=extra.pop("channel", "#flight"),
        from_agent=extra.pop("from_agent", "agent:peer:wu2"),
        **extra)


def _snapshot(state: Path) -> dict[str, tuple[int, bytes]]:
    if not state.exists():
        return {}
    return {
        str(path.relative_to(state)): (
            stat.S_IMODE(path.stat().st_mode), path.read_bytes())
        for path in sorted(state.rglob("*")) if path.is_file()
    }


def _save_peer_mapping(repo: Path, key: str, *,
                       agent_id: str = "agent:peer:wu2",
                       session: str = "a") -> None:
    cfg = O.load_config(repo)
    O.save_mapping(repo, {
        "flight": {"id": B.flight_id(repo, cfg)},
        "ozagia": {"harness": "opencode", "session": session, "key": key},
        "ozma": {
            "agent_id": agent_id,
            "scope": "workspace",
            "card_path": "card.json",
            "card_fingerprint": cfg.card_fingerprint,
            "transport": "native",
        },
        "verification": {"status": "host_declared", "source": "host"},
        "transport": "native",
        "state": "host_declared",
    })


def _local_message(repo: Path, monkeypatch) -> tuple[dict, str, str]:
    M.register(repo, "opencode", "a", exposure="coordinate")
    M.register(repo, "opencode", "b", exposure="coordinate")
    M.send(repo, "opencode", "b", kind="request", subject="review",
           body="please look", to="opencode:a")
    inbox = M.inbox(repo, "opencode", "a")
    message_id = inbox["messages"][0]["msg_id"]
    message = M.read(repo, "opencode", "a", message_id, peek=True)
    _save_peer_mapping(repo, str((message.get("to") or {}).get("key") or ""))
    return message, "opencode", "a"


# --------------------------------------------------------------------------- #
# declared mapping
# --------------------------------------------------------------------------- #
def test_mapping_table_and_both_directions(env) -> None:
    repo, _ = env
    config = _configured(repo)
    table = {row["local"]: row["ozma"] for row in B.mapping_table()}
    assert table["request"] == "request"
    assert table["accept"] == "ack"
    assert table["reject"] == "ack"
    assert table["offer"] == "response|handoff"
    assert table["status"] == "status"
    assert table["release"] == "release"
    assert table["note"] == "heads-up"
    assert table["block"] == "block"
    assert table["unblock"] == "unblock"
    assert table["handoff"] == "handoff"
    assert table["hello"] is None
    assert B.LOCAL_TO_OZMA["note"] == "heads-up"

    assert B.translate_local("request", subject="q")["type"] == "request"
    assert B.translate_local("accept", subject="ok")["body"] == {
        "accepted": True, "text": "ok"}
    reject = B.translate_local("reject", body="no thanks")
    assert reject["body"] == {"accepted": False, "reason": "no thanks"}
    assert B.translate_local("offer", body="maybe",
                             offer_as="response")["choice"] == "response"
    assert B.translate_local("offer", body="maybe",
                             offer_as="handoff")["type"] == "handoff"
    assert B.translate_local("status", body="working")["type"] == "status"
    assert B.translate_local("release")["type"] == "release"
    release = B.translate_local("release", claim_id="clm_123")
    assert release["in_reply_to"] == "clm_123"
    assert release["body"] == {"claim_id": "clm_123"}
    assert B.translate_local("note", body="fyi")["type"] == "heads-up"
    assert B.translate_local("block", reason="wait")["type"] == "block"
    assert B.translate_local("unblock", reason="go")["type"] == "unblock"
    assert B.translate_local("handoff", body="take it")["type"] == "handoff"

    assert B.translate_ozma("request", {"purpose": "review"})["kind"] == "request"
    with pytest.raises(B.OzmaBridgeRefusal):
        B.translate_local("request", purpose="bogus")
    assert B.translate_ozma("ack", {"accepted": True})["kind"] == "accept"
    assert B.translate_ozma("ack", {"accepted": False})["kind"] == "reject"
    assert B.translate_ozma("response", {})["kind"] == "offer"
    assert B.translate_ozma("heads-up", {"text": "x"})["kind"] == "note"
    assert B.translate_ozma("claim", {"scope": "file", "resource": "a"})[
        "local"] is False
    assert config.transport == "native"


def test_unknown_kinds_fields_and_hello_are_rejected(env) -> None:
    repo, _ = env
    config = _configured(repo)
    for kind in ("hello", "claim", "shout", ""):
        with pytest.raises(B.OzmaBridgeRefusal) as excinfo:
            B.translate_local(kind, body="x")
        assert excinfo.value.code in {"identity_metadata", "unsupported_kind"}

    with pytest.raises(B.OzmaBridgeRefusal) as excinfo:
        B.translate_local("offer", body="x")
    assert excinfo.value.code == "offer_choice"

    envelope = _envelope(repo, config, ozma_type="request",
                         body={"purpose": "general"})
    envelope["surprise"] = "kept?"
    with pytest.raises(B.OzmaBridgeRefusal) as excinfo:
        B.validate_envelope(envelope, direction="inbound")
    assert excinfo.value.code == "unsupported_field"

    body = _envelope(repo, config, ozma_type="request",
                     body={"purpose": "general"})
    body["body"]["unknown"] = "x"
    with pytest.raises(B.OzmaBridgeRefusal) as excinfo:
        B.validate_envelope(body, direction="inbound")
    assert excinfo.value.code == "unsupported_field"

    hello = _envelope(repo, config, ozma_type="request", body={"purpose": "general"})
    hello["type"] = "hello"
    with pytest.raises(B.OzmaBridgeRefusal) as excinfo:
        B.validate_envelope(hello, direction="inbound")
    assert excinfo.value.code == "identity_metadata"


def test_typed_fields_preserved_and_free_text_marked_untrusted(env) -> None:
    repo, _ = env
    config = _configured(repo)
    envelope = _envelope(
        repo, config, ozma_type="request", body={"purpose": "review"},
        in_reply_to="ozm_0", thread="thread-1", refs=["msg_a", "msg_b"],
        priority=0.7, deliver_at="now", requires_ack=True,
        compliance=["soc2"], workunit="wu1")
    assert envelope["in_reply_to"] == "ozm_0"
    assert envelope["thread"] == "thread-1"
    assert envelope["refs"] == ["msg_a", "msg_b"]
    assert envelope["priority"] == 0.7
    assert envelope["deliver_at"] == "now"
    assert envelope["requires_ack"] is True
    assert envelope["compliance"] == ["soc2"]
    assert envelope["workunit"] == "wu1"
    assert envelope["digest"].startswith("sha256:")

    note = B.translate_local("note", body="free text")
    assert note["type"] == "heads-up"
    assert note["body_trust"] == "untrusted"
    inbound = _envelope(repo, config, body={"text": "free text"},
                        body_trust="typed")
    assert inbound["body_trust"] == "untrusted"

    with pytest.raises(B.OzmaBridgeRefusal):
        _envelope(repo, config, ozma_type="request", body={"purpose": "bogus"})
    with pytest.raises(B.OzmaBridgeRefusal):
        _envelope(repo, config, priority=2.0)
    with pytest.raises(B.OzmaBridgeRefusal):
        _envelope(repo, config, channel="#bad channel")


# --------------------------------------------------------------------------- #
# outbox
# --------------------------------------------------------------------------- #
def test_outbox_enqueue_is_durable_bounded_and_hashed(env, monkeypatch) -> None:
    repo, state = env
    config = _configured(repo)
    message, harness, session = _local_message(repo, monkeypatch)
    queued = B.enqueue_local_message(repo, message, config=config)
    assert queued["queued"] is True
    path = B.record_path(repo, B.OUTBOX_DIR, "default", message["msg_id"],
                         "outbound")
    assert path.is_file()
    assert message["msg_id"] not in path.name
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700

    again = B.enqueue_local_message(repo, message, config=config)
    assert again["duplicate"] is True
    assert len(list(path.parent.glob("*.json"))) == 1

    # Same id, different content is an explicit conflict, not a silent replace.
    conflict = B.build_envelope(
        flight_id=B.flight_id(repo, config), message_id=message["msg_id"],
        ozma_type="heads-up", body={"text": "different"},
        body_trust="untrusted")
    with pytest.raises(B.OzmaBridgeRefusal) as excinfo:
        B.enqueue(repo, conflict, config=config)
    assert excinfo.value.code == "id_conflict"

    listed = B.outbox(repo, config=config)
    assert listed["counts"]["pending"] == 1
    assert "body" not in json.dumps(listed)
    assert harness and session


def test_direct_message_needs_a_saved_peer_ozma_identity(env, monkeypatch) -> None:
    repo, _ = env
    config = _configured(repo)
    M.register(repo, "opencode", "a", exposure="coordinate")
    M.register(repo, "opencode", "b", exposure="coordinate")
    M.send(repo, "opencode", "b", kind="note", subject="s", body="b",
           to="opencode:a")
    message_id = M.inbox(repo, "opencode", "a")["messages"][0]["msg_id"]
    message = M.read(repo, "opencode", "a", message_id, peek=True)

    # A local session key is never used as an Ozma id.
    with pytest.raises(B.OzmaBridgeRefusal) as excinfo:
        B.enqueue_local_message(repo, message, config=config)
    assert excinfo.value.code == "unknown_peer_mapping"

    _save_peer_mapping(repo, str(message["to"]["key"]))
    queued = B.enqueue_local_message(repo, message, config=config)
    assert queued["queued"] is True
    view = B.outbox(repo, config=config, include_all=True)["messages"][0]
    assert view["to"] == "agent:peer:wu2"
    assert message["to"]["key"] not in view["to"]

    # The envelope grammar itself only accepts host-provided agent ids.
    with pytest.raises(B.OzmaBridgeRefusal) as excinfo:
        B.build_envelope(
            flight_id=B.flight_id(repo, config), message_id="ozm_bad_from",
            ozma_type="heads-up", body={"text": "x"}, direction="outbound",
            from_agent=str(message["to"]["key"]))
    assert excinfo.value.code == "invalid_value"


def test_outbox_retry_ttl_rate_and_caps(env, monkeypatch) -> None:
    repo, _ = env
    config = _configured(repo, rate="2/1min", ttl=100)
    message, _, _ = _local_message(repo, monkeypatch)
    queued = B.enqueue_local_message(repo, message, config=config, now=1000)
    assert queued["queued"] is True

    calls = {"n": 0}

    def flaky(envelope):
        calls["n"] += 1
        return {"accepted": calls["n"] > 1}

    shim = FakeShim(accept=flaky)
    first = B.deliver_pending(repo, shim, config=config, now=1000)
    assert first["failed"] == 1
    second = B.deliver_pending(repo, shim, config=config, now=1001)
    assert second["retried"] == 1 and second["delivered"] == 1
    assert [item["id"] for item in shim.delivered] == [
        message["msg_id"], message["msg_id"]]
    view = B.outbox(repo, config=config, include_all=True,
                    now=1001)["messages"][0]
    assert view["state"] == "sent" and view["attempts"] == 2

    # TTL is reported, not deleted; a sent message stays sent.
    second_message = dict(message, msg_id="msg_second00000000")
    B.enqueue_local_message(repo, second_message, config=config, now=1000)
    expired = B.outbox(repo, config=config, include_all=True, now=1200)
    states = {item["id"]: item["state"] for item in expired["messages"]}
    assert states[message["msg_id"]] == "sent"
    assert states[second_message["msg_id"]] == "expired"

    # The rate limit still covers the trailing window.
    with pytest.raises(B.OzmaBridgeRefusal) as excinfo:
        B.enqueue_local_message(
            repo, dict(message, msg_id="msg_third0000000000"),
            config=config, now=1000)
    assert excinfo.value.code == "rate_limited"

    tiny = _configured(repo, max_messages=1)
    fourth = B.enqueue_local_message(
        repo, dict(message, msg_id="msg_fourth0000000"), config=tiny)
    assert fourth["queued"] is True and fourth["evicted"] >= 1
    assert B.outbox(repo, config=tiny, include_all=True)["queue"][
        "messages"] <= 1


def test_terminal_history_yields_but_live_work_never_does(env,
                                                           monkeypatch) -> None:
    repo, _ = env
    config = _configured(repo, rate="600/5min", max_messages=1)
    message, _, _ = _local_message(repo, monkeypatch)
    first = B.enqueue_local_message(repo, message, config=config, now=1000)
    assert first["queued"] is True

    # A pending outbound row is live work: the cap refuses rather than evicting.
    with pytest.raises(B.OzmaBridgeRefusal) as excinfo:
        B.enqueue_local_message(
            repo, dict(message, msg_id="msg_live0000000001"),
            config=config, now=1001)
    assert excinfo.value.code == "queue_full"

    # Delivered history is evictable, and the eviction is reported.
    B.deliver_pending(repo, FakeShim(), config=config, now=1001)
    second = B.enqueue_local_message(
        repo, dict(message, msg_id="msg_live0000000002"),
        config=config, now=1002)
    assert second["queued"] is True and second["evicted"] == 1

    # An unread inbound message is never evicted.
    inbound = _envelope(repo, config, message_id="ozm_in_1")
    assert B.ingest(repo, inbound, config=config, now=2000)["accepted"] is True
    with pytest.raises(B.OzmaBridgeRefusal) as excinfo:
        B.ingest(repo, _envelope(repo, config, message_id="ozm_in_2"),
                 config=config, now=2001)
    assert excinfo.value.code == "queue_full"

    # Acked mail yields, and the eviction is reported.
    B.ack_message(repo, "ozm_in_1", config=config)
    accepted = B.ingest(repo, _envelope(repo, config, message_id="ozm_in_2"),
                        config=config, now=2002)
    assert accepted["accepted"] is True and accepted["evicted"] == 1

    # Read mail that still needs an ack stays until acked.
    B.ack_message(repo, "ozm_in_2", config=config)
    B.ingest(repo, B.build_envelope(
        flight_id=B.flight_id(repo, config), message_id="ozm_in_3",
        ozma_type="request", body={"purpose": "general", "text": "review"},
        direction="inbound", channel="#flight",
        from_agent="agent:peer:wu2", requires_ack=True),
        config=config, now=2003)
    read = B.read_message(repo, "ozm_in_3", config=config)
    assert read["id"] == "ozm_in_3" and read["acked"] is False
    with pytest.raises(B.OzmaBridgeRefusal) as excinfo:
        B.ingest(repo, _envelope(repo, config, message_id="ozm_in_4"),
                 config=config, now=2004)
    assert excinfo.value.code == "queue_full"
    B.ack_message(repo, "ozm_in_3", config=config)
    final = B.ingest(repo, _envelope(repo, config, message_id="ozm_in_4"),
                     config=config, now=2005)
    assert final["accepted"] is True and final["evicted"] == 1


def test_reads_do_not_create_state(env) -> None:
    repo, state = env
    config = _configured(repo)
    before = _snapshot(state)
    assert B.outbox(repo, config=config)["messages"] == []
    assert B.inbox(repo, config=config)["messages"] == []
    assert B.claims(repo, config=config)["claims"] == []
    assert B.read_status(repo, config)["store_present"] is False
    assert _snapshot(state) == before


# --------------------------------------------------------------------------- #
# inbox, dedupe and redaction
# --------------------------------------------------------------------------- #
def test_inbox_ingest_list_read_ack_and_dedupe(env) -> None:
    repo, _ = env
    config = _configured(repo)
    envelope = _envelope(repo, config, body={"text": "remote body"})
    first = B.ingest(repo, envelope, config=config)
    assert first["accepted"] is True and first["duplicate"] is False
    replay = B.ingest(repo, envelope, config=config)
    assert replay["accepted"] is True and replay["duplicate"] is True

    listed = B.inbox(repo, config=config)
    assert listed["counts"]["unread"] == 1
    metadata = listed["messages"][0]
    assert metadata["id"] == "ozm_1"
    assert metadata["from"] == "agent:peer:wu2"
    assert "body" not in metadata
    assert "remote body" not in json.dumps(listed)

    body = B.read_message(repo, "ozm_1", config=config)
    assert body["body"] == {"text": "remote body"}
    assert body["untrusted"] is True
    assert B.inbox(repo, config=config)["counts"]["read"] == 1
    acked = B.ack_message(repo, "ozm_1", config=config)
    assert acked["state"] == "acked"
    assert B.inbox(repo, config=config)["counts"]["acked"] == 1

    conflict = _envelope(repo, config, body={"text": "different"})
    assert B.ingest(repo, conflict, config=config)["code"] == "id_conflict"
    status = B.read_status(repo, config)
    assert status["dedupe"]["conflicts"] == 1


def test_inbox_redacts_and_marks_remote_text_untrusted(env) -> None:
    repo, _ = env
    config = _configured(repo)
    canary = "canary-secret-value-1234567890"
    envelope = _envelope(repo, config,
                         body={"text": f"api_key = '{canary}'"})
    assert B.ingest(repo, envelope, config=config)["accepted"] is True
    listed = B.inbox(repo, config=config)
    assert canary not in json.dumps(listed)
    read = B.read_message(repo, "ozm_1", config=config)
    assert canary not in json.dumps(read)
    assert read["untrusted"] is True
    assert read["body_trust"] == "untrusted"
    assert read["redactions"]


def test_channels_subscriptions_and_priority_floor(env) -> None:
    repo, _ = env
    config = _configured(repo, channels=("#flight",),
                         subscriptions=("type:heads-up",), priority_floor=0.5)
    off_scope = _envelope(repo, config, message_id="ozm_off",
                          channel="#other")
    assert B.ingest(repo, off_scope, config=config)["code"] == (
        "channel_not_allowed")
    low = _envelope(repo, config, message_id="ozm_low", priority=0.1)
    assert B.ingest(repo, low, config=config)["code"] == "below_priority_floor"
    wrong_type = _envelope(repo, config, message_id="ozm_req",
                           ozma_type="request", body={"purpose": "general"})
    assert B.ingest(repo, wrong_type, config=config)["code"] == "not_subscribed"
    good = _envelope(repo, config, message_id="ozm_ok", priority=0.9)
    assert B.ingest(repo, good, config=config)["accepted"] is True
    assert B.inbox(repo, config=config)["counts"]["unread"] == 1


def test_inbox_bounds_and_egress_redaction(env) -> None:
    repo, _ = env
    config = _configured(repo, max_messages=1)
    assert B.ingest(repo, _envelope(repo, config, message_id="ozm_one"),
                    config=config)["accepted"] is True
    with pytest.raises(B.OzmaBridgeRefusal) as excinfo:
        B.ingest(repo, _envelope(repo, config, message_id="ozm_two"),
                 config=config)
    assert excinfo.value.code == "queue_full"

    outbound = _configured(repo)
    canary = "canary-secret-value-1234567890"
    envelope = B.build_envelope(
        flight_id=B.flight_id(repo, outbound), message_id="msg_out",
        ozma_type="heads-up", body={"text": f"api_key = '{canary}'"},
        body_trust="untrusted")
    B.enqueue(repo, envelope, config=outbound)
    files = list((state := B.store_dir(repo, B.OUTBOX_DIR, "default")).glob(
        "*.json"))
    assert len(files) == 1
    assert canary not in files[0].read_text(encoding="utf-8")
    assert canary not in json.dumps(B.outbox(repo, config=outbound))
    assert state and env


# --------------------------------------------------------------------------- #
# claims and sync
# --------------------------------------------------------------------------- #
def test_fake_shim_sync_both_directions_and_retry_same_id(env) -> None:
    repo, _ = env
    config = _configured(repo)
    message, _, _ = _local_message(repo, monkeypatch=None)
    B.enqueue_local_message(repo, message, config=config)
    inbound = _envelope(repo, config, message_id="ozm_in",
                        body={"text": "inbound"})
    shim = FakeShim([inbound])
    report = B.sync(repo, shim, config=config)
    assert report["delivered"] == 1
    assert report["delivery"]["delivered"] == 1
    assert report["accepted"] == 1
    assert shim.delivered[0]["id"] == message["msg_id"]
    assert B.inbox(repo, config=config)["counts"]["unread"] == 1

    replay = B.sync(repo, FakeShim([inbound]), config=config)
    assert replay["replay"] == 1
    assert B.inbox(repo, config=config)["counts"]["unread"] == 1


def test_remote_claim_conflict_is_advisory_and_merge_is_never_authority(env) -> None:
    repo, _ = env
    config = _configured(repo)
    M.register(repo, "opencode", "a", exposure="coordinate")
    lease = M.lease_acquire(repo, "opencode", "a", kind="path",
                            resource_id="src/foo.py", mode="write")["lease"]
    outbound = B.enqueue_claim(repo, lease, config=config)
    assert outbound["scope"] == "file" and outbound["advisory"] is True

    remote = _envelope(
        repo, config, message_id="clm_remote", ozma_type="claim",
        body={"scope": "file", "resource": "src/foo.py", "mode": "write",
              "expires_at": "2030-01-01T00:00:00Z"})
    assert B.ingest(repo, remote, config=config)["accepted"] is True
    report = B.claims(repo, config=config)
    assert report["conflicts"]
    assert report["conflicts"][0]["advisory"] is True
    assert report["conflicts"][0]["blocking"] is False
    # The local lease remains usable and remote claims are not local leases.
    assert len(M.lease_list(repo)["leases"]) == 1
    assert M.lease_status(repo, lease["lease_id"])["lease"][
        "effective_state"] == "active"
    assert B.read_status(repo, config)["conflicts"] == 1

    merge = M.lease_acquire(repo, "opencode", "a", kind="merge",
                            resource_id="main", mode="merge")["lease"]
    merge_claim = B.enqueue_claim(repo, merge, config=config)
    assert merge_claim["merge"] is True and merge_claim["advisory"] is True
    # A merge claim is visible but never authorizes anything.
    assert M.merge_request is not None


def test_remote_claim_expiry_and_release_are_reported(env) -> None:
    repo, _ = env
    config = _configured(repo)
    expired = _envelope(
        repo, config, message_id="clm_old", ozma_type="claim",
        body={"scope": "file", "resource": "src/old.py",
              "expires_at": "2020-01-01T00:00:00Z"})
    assert B.ingest(repo, expired, config=config)["accepted"] is True
    assert B.claims(repo, config=config)["claims"][0]["state"] == "expired"
    # Expiry is retained, not deleted or overwritten.
    assert B.claims(repo, config=config)["claims"][0]["claim_id"] == "clm_old"

    active = _envelope(
        repo, config, message_id="clm_live", ozma_type="claim",
        body={"scope": "branch", "resource": "main"})
    assert B.ingest(repo, active, config=config)["accepted"] is True
    wrong = _envelope(
        repo, config, message_id="ozm_release_wrong", ozma_type="release",
        body={"claim_id": "clm_live"}, in_reply_to="clm_live",
        from_agent="agent:impostor:wu9")
    assert B.ingest(repo, wrong, config=config)["accepted"] is True
    states = {item["claim_id"]: item["state"]
              for item in B.claims(repo, config=config)["claims"]}
    assert states["clm_live"] == "active"
    release = _envelope(
        repo, config, message_id="ozm_release", ozma_type="release",
        body={"claim_id": "clm_live"}, in_reply_to="clm_live")
    assert B.ingest(repo, release, config=config)["accepted"] is True
    states = {item["claim_id"]: item["state"]
              for item in B.claims(repo, config=config)["claims"]}
    assert states["clm_live"] == "released"


def test_grant_has_no_claim_mapping(env) -> None:
    repo, _ = env
    config = _configured(repo)
    M.register(repo, "opencode", "a", exposure="coordinate")
    lease = M.lease_acquire(repo, "opencode", "a", kind="grant",
                            resource_id="cap", mode="read")["lease"]
    with pytest.raises(B.OzmaBridgeRefusal) as excinfo:
        B.enqueue_claim(repo, lease, config=config)
    assert excinfo.value.code == "unsupported_mapping"


# --------------------------------------------------------------------------- #
# status, check and no-network boundary
# --------------------------------------------------------------------------- #
def test_status_and_check_report_counts_without_creating_state(env,
                                                               monkeypatch) -> None:
    repo, state = env
    config = _configured(repo)
    before = _snapshot(state)

    def forbidden(*args, **kwargs):
        raise AssertionError("the bridge must not open a socket")

    monkeypatch.setattr(socket, "socket", forbidden)
    status = O.status(repo)
    assert status["bridge"]["store_present"] is False
    assert status["bridge"]["outbox"]["pending"] == 0
    assert status["network"] is False
    checked = O.check(repo, live=True)
    assert checked["live"]["state"] == "unavailable"
    assert checked["bridge"]["conformance"] == "declared"
    assert checked["conformance"] == "declared"
    assert _snapshot(state) == before
    assert config.mode == "messages"


def test_shim_unavailable_and_sync_never_network(env, monkeypatch) -> None:
    repo, _ = env
    _configured(repo)
    # Warm the lazy CLI imports before replacing socket.socket.
    assert cli.main(["mesh", "ozma", "status", "--json"]) == 0

    def forbidden(*args, **kwargs):
        raise AssertionError("no socket in the unavailable shim")

    monkeypatch.setattr(socket, "socket", forbidden)
    with pytest.raises(B.OzmaBridgeRefusal) as excinfo:
        B.UnavailableShim().deliver({"id": "x"})
    assert excinfo.value.code == "shim_unavailable"
    with pytest.raises(B.OzmaBridgeRefusal):
        B.sync(repo, B.UnavailableShim())
    assert cli.main(["mesh", "ozma", "sync", "--json"]) == 1


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def test_cli_outbox_inbox_read_ack_and_enqueue(env, capsys) -> None:
    repo, state = env
    config = _configured(repo)
    message, harness, session = _local_message(repo, monkeypatch=None)
    B.enqueue_local_message(repo, message, config=config)
    inbound = _envelope(repo, config, message_id="ozm_cli",
                        body={"text": "cli body"})
    B.ingest(repo, inbound, config=config)

    assert cli.main(["mesh", "ozma", "outbox", "--json"]) == 0
    outbox = json.loads(capsys.readouterr().out)
    assert outbox["counts"]["pending"] == 1
    assert "cli body" not in json.dumps(outbox)

    assert cli.main(["mesh", "ozma", "inbox", "--json"]) == 0
    inbox = json.loads(capsys.readouterr().out)
    assert any(item["id"] == "ozm_cli" for item in inbox["messages"])
    assert "cli body" not in json.dumps(inbox)

    assert cli.main(["mesh", "ozma", "read", "ozm_cli", "--json"]) == 0
    read = json.loads(capsys.readouterr().out)
    assert read["body"] == {"text": "cli body"}
    assert cli.main(["mesh", "ozma", "ack", "ozm_cli", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "acked"

    assert cli.main(["mesh", "ozma", "inbox", "--unread", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["messages"] == []

    # Enqueue an existing local mailbox message through the CLI.
    assert cli.main(["mesh", "ozma", "enqueue", "--harness", harness,
                     "--session", session, "--message", message["msg_id"],
                     "--json"]) == 0
    queued = json.loads(capsys.readouterr().out)
    assert queued["duplicate"] is True

    assert cli.main(["mesh", "ozma", "claims", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["claims"] == []
    assert state.exists()


def test_cli_exit_codes_errors_and_canaries(env, capsys) -> None:
    repo, _ = env
    config = _configured(repo, mode="status")
    # A read view needs no identity and creates nothing.
    assert cli.main(["mesh", "ozma", "outbox", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["messages"] == []

    # Remote text is redacted on ingest, before any read view or error.
    canary = "canary-secret-value-1234567890"
    outcome = B.ingest(
        repo, _envelope(repo, config, message_id="ozm_bad",
                        body={"text": f"api_key = '{canary}'"}),
        config=config)
    assert outcome["accepted"] is True
    assert canary not in json.dumps(B.inbox(repo, config=config))

    message, harness, session = _local_message(repo, monkeypatch=None)
    # mode=status refuses an attributable enqueue with exit 1.
    assert cli.main(["mesh", "ozma", "enqueue", "--harness", "opencode",
                     "--session", "a", "--message", message["msg_id"],
                     "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["code"] == "bridge_not_ready"
    assert canary not in json.dumps(payload)

    assert cli.main(["mesh", "ozma", "read", "ozm_missing", "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["code"] == "unknown_message"

    # A missing identity is a usage/config failure, not a bridge write.
    assert cli.main(["mesh", "ozma", "enqueue", "--message",
                     message["msg_id"], "--json"]) == 2
    capsys.readouterr()
    assert cli.main(["mesh", "ozma", "check", "--live", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["live"]["state"] == "unavailable"
    assert harness and session
