from __future__ import annotations

import socket
import threading
from collections.abc import Sequence

import pytest

from ozagia.core.relay import (
    CommandBackend,
    CommandResult,
    LocalBackend,
    Relay,
    RelayPolicy,
    RelayPolicyError,
    RelayRegistry,
    RelaySpec,
    RelaySpecError,
    RelayUnsupported,
    SshBackend,
    SsmBackend,
    TunnelLease,
    TunnelTarget,
)


def policy(
    *kinds: str,
    live: bool = True,
    network: bool = False,
    timeout: float = 10,
    allowlist: tuple[tuple[str, ...], ...] | None = None,
    output: int = 4_096,
) -> RelayPolicy:
    return RelayPolicy(
        allowed_kinds=frozenset(kinds),
        allow_live=live,
        allow_network=network,
        max_timeout_s=timeout,
        command_allowlist=allowlist,
        max_output_bytes=output,
    )


class RecordingBackend:
    def __init__(self, *, kind: str, parent: Relay | None, events: list[str], prefix: str):
        self.kind = kind
        self.parent = parent
        self.events = events
        self.prefix = prefix
        self.closed = 0

    def plan_argv(self, argv: Sequence[str]) -> tuple[str, ...]:
        result = (self.prefix, *argv)
        return self.parent.plan_argv(result) if self.parent is not None else result

    def run(self, argv, *, timeout_s, stdin=None):
        del timeout_s, stdin
        self.events.append(self.kind)
        if self.parent is not None:
            return self.parent.run((self.prefix, *argv), timeout_s=5)
        return CommandResult(0)

    def open_tunnel(self, target, *, local_port=0, timeout_s=30):
        del target, local_port, timeout_s
        raise RelayUnsupported("unsupported in test backend")

    def close(self):
        self.closed += 1
        self.events.append(f"close:{self.kind}")


def test_chain_order_and_nested_plan_are_inner_to_outer() -> None:
    events: list[str] = []
    registry = RelayRegistry()

    def factory(kind: str, prefix: str):
        return lambda *, options, parent, policy: RecordingBackend(
            kind=kind, parent=parent, events=events, prefix=prefix
        )

    registry.register("base", factory("base", "base"))
    registry.register("jump", factory("jump", "jump"))
    relay = Relay.chain(
        [RelaySpec("base"), RelaySpec("jump")],
        registry=registry,
        policy=policy("base", "jump", live=True),
    )

    result = relay.run(["status"], timeout_s=5, dry_run=True)
    assert result.planned_argv == ("base", "jump", "status")
    assert events == []

    # The chain creates the inner backend before the outer one; each route hop
    # is entered from outermost to innermost during command execution.
    result = relay.run(["status"], timeout_s=5)
    assert result.returncode == 0
    assert events == ["jump", "base"]

    # close() owns only this node. Closing the chain explicitly is outer first.
    relay.close()
    assert events[-1] == "close:jump"
    relay.parent.close()
    assert events[-1] == "close:base"
    relay.close()
    relay.parent.close()
    assert events.count("close:jump") == 1
    assert events.count("close:base") == 1


def test_ephemeral_factories_are_not_shared_and_persistent_are_reused() -> None:
    created: list[RecordingBackend] = []
    events: list[str] = []
    registry = RelayRegistry()

    def make(*, options, parent, policy):
        del options, parent, policy
        instance = RecordingBackend(
            kind="fake", parent=None, events=events, prefix="unused"
        )
        created.append(instance)
        return instance

    registry.register("fake", make)
    explicit = policy("fake")
    persistent = Relay.from_specs(
        [RelaySpec("fake")], registry=registry, policy=explicit
    )
    persistent.run(["noop"], timeout_s=5)
    persistent.run(["noop"], timeout_s=5)
    assert len(created) == 1

    ephemeral = Relay.from_specs(
        [RelaySpec("fake")], registry=registry, policy=explicit, ephemeral=True
    )
    ephemeral.run(["noop"], timeout_s=5)
    ephemeral.run(["noop"], timeout_s=5)
    assert len(created) == 3
    assert created[1] is not created[2]
    assert created[1].closed == created[2].closed == 1


def test_ephemeral_tunnel_backend_is_closed_with_its_lease() -> None:
    created: list[RecordingBackend] = []
    closed_leases: list[bool] = []
    registry = RelayRegistry()

    class TunnelBackend(RecordingBackend):
        def open_tunnel(self, target, *, local_port=0, timeout_s=30):
            del timeout_s
            return TunnelLease(
                "127.0.0.1",
                local_port,
                target.host,
                target.port,
                "fake loopback",
                self.kind,
                lambda: closed_leases.append(True),
            )

    def make(*, options, parent, policy):
        del options, parent, policy
        backend = TunnelBackend(
            kind="fake", parent=None, events=[], prefix="unused"
        )
        created.append(backend)
        return backend

    registry.register("fake", make)
    relay = Relay.from_specs(
        [RelaySpec("fake")],
        registry=registry,
        policy=policy("fake", network=True),
        ephemeral=True,
    )
    lease = relay.open_tunnel(TunnelTarget("127.0.0.1", 9), timeout_s=5)
    assert len(created) == 1
    assert created[0].closed == 0
    lease.close()
    assert created[0].closed == 1
    assert closed_leases == [True]


def test_default_policy_denies_before_runner_or_network_side_effect() -> None:
    calls: list[tuple[str, ...]] = []
    backend = LocalBackend(runner=lambda argv, **kwargs: calls.append(tuple(argv)))
    relay = Relay(backend)
    with pytest.raises(RelayPolicyError):
        relay.run(["echo", "never"], timeout_s=1)
    with pytest.raises(RelayPolicyError):
        relay.open_tunnel(TunnelTarget("127.0.0.1", 9), timeout_s=1)
    assert calls == []


def test_policy_allowlist_and_dry_run_have_no_side_effects() -> None:
    calls: list[tuple[str, ...]] = []
    relay = Relay(
        LocalBackend(runner=lambda argv, **kwargs: calls.append(tuple(argv))),
        policy=policy("local", live=False, allowlist=(("safe",),)),
    )
    result = relay.run(["safe", "arg"], timeout_s=5, dry_run=True)
    assert result.dry_run
    assert result.planned_argv == ("safe", "arg")
    assert calls == []
    with pytest.raises(RelayPolicyError):
        relay.run(["other"], timeout_s=5, dry_run=True)
    with pytest.raises(RelayPolicyError):
        relay.run(["safe"], timeout_s=5)
    assert calls == []

    # A dry-run tunnel reports intent but does not bind the requested port.
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    tunnel_plan = relay.open_tunnel(
        TunnelTarget("127.0.0.1", 9),
        local_port=port,
        timeout_s=5,
        dry_run=True,
    )
    assert tunnel_plan.dry_run
    check = socket.socket()
    check.bind(("127.0.0.1", port))
    check.close()


def test_command_output_is_bounded_and_hidden_from_repr() -> None:
    secret = "token-super-secret"
    relay = Relay(
        LocalBackend(
            runner=lambda argv, **kwargs: (0, secret * 100, secret * 100)
        ),
        policy=policy("local", output=13),
    )
    result = relay.run(["safe"], timeout_s=5)
    assert len(result.stdout.encode()) <= 13
    assert len(result.stderr.encode()) <= 13
    assert secret not in repr(result)


def test_malformed_argv_and_secret_values_are_not_echoed() -> None:
    secret = "credentials-do-not-print"
    relay = Relay(
        LocalBackend(runner=lambda argv, **kwargs: (0, "", "")),
        policy=policy("local"),
    )
    with pytest.raises(RelaySpecError) as caught:
        relay.run(["ok", f"bad\x00{secret}"], timeout_s=5)
    assert secret not in str(caught.value)

    spec = RelaySpec("ssh", {"host": "host.example", "token": secret})
    assert secret not in repr(spec)
    with pytest.raises(RelaySpecError) as caught:
        SshBackend(host=f"-oProxyCommand={secret}")
    assert secret not in str(caught.value)


def test_local_tcp_tunnel_forwards_loopback_echo_and_closes() -> None:
    echo = socket.socket()
    echo.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    echo.bind(("127.0.0.1", 0))
    echo.listen(1)
    echo.settimeout(0.2)
    stop = threading.Event()

    def serve() -> None:
        while not stop.is_set():
            try:
                client, _ = echo.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with client:
                try:
                    data = client.recv(4_096)
                    if data:
                        client.sendall(data)
                except OSError:
                    pass

    server = threading.Thread(target=serve, daemon=True)
    server.start()
    relay = Relay(
        LocalBackend(runner=lambda argv, **kwargs: (0, "", "")),
        policy=policy("local", network=True),
    )
    lease = relay.open_tunnel(
        TunnelTarget("127.0.0.1", echo.getsockname()[1]),
        timeout_s=5,
    )
    try:
        client = socket.create_connection((lease.local_host, lease.local_port), timeout=2)
        with client:
            client.sendall(b"loopback")
            assert client.recv(8) == b"loopback"
    finally:
        lease.close()
        lease.close()
        relay.close()
        stop.set()
        echo.close()
        server.join(timeout=1)
    with pytest.raises(OSError):
        socket.create_connection((lease.local_host, lease.local_port), timeout=0.2)


def test_local_tunnel_rejects_non_loopback_before_binding() -> None:
    relay = Relay(
        LocalBackend(runner=lambda argv, **kwargs: (0, "", "")),
        policy=policy("local", network=True),
    )
    with pytest.raises(RelayPolicyError):
        relay.open_tunnel(TunnelTarget("example.com", 443), timeout_s=5)


def test_unsupported_tunnel_is_clear_and_does_not_start_process() -> None:
    parent = Relay(
        LocalBackend(runner=lambda argv, **kwargs: (0, "", "")),
        policy=policy("local", "ssh", network=True),
    )
    ssh = Relay(
        SshBackend(host="bastion.example", parent=parent),
        parent=parent,
        policy=parent.policy,
        kind="ssh",
    )
    with pytest.raises(RelayUnsupported):
        ssh.open_tunnel(TunnelTarget("127.0.0.1", 8000), timeout_s=5)


def test_ssh_and_ssm_are_argv_only_planners() -> None:
    ssh = SshBackend(
        host="bastion.example",
        user="operator",
        port=2222,
        identity_file="/keys/relay",
    )
    command = ssh.plan_run(["uname", "-a"])
    assert command[:3] == ("ssh", "-o", "ExitOnForwardFailure=yes")
    assert command[-4:] == ("--", "operator@bastion.example", "uname", "-a")
    tunnel = ssh.plan_tunnel_argv(
        TunnelTarget("127.0.0.1", 9000), local_port=5000
    )
    assert "-N" in tunnel
    assert "127.0.0.1:5000:127.0.0.1:9000" in tunnel
    with pytest.raises(RelayUnsupported):
        ssh.run(["uname"], timeout_s=5)

    ssm = SsmBackend(
        instance_id="i-0123456789abcdef0",
        region="us-east-1",
        command_id="00000000-0000-0000-0000-000000000001",
    )
    status = ssm.plan_status()
    assert status[:4] == ("aws", "ssm", "get-command-invocation", "--command-id")
    ssm_tunnel = ssm.plan_tunnel_argv(
        TunnelTarget("localhost", 5432), local_port=15432
    )
    assert "AWS-StartPortForwardingSession" in ssm_tunnel
    assert "--region" in ssm_tunnel
    with pytest.raises(RelayUnsupported):
        ssm.run(["status"], timeout_s=5)
    with pytest.raises(RelaySpecError):
        SsmBackend(instance_id="i-1", region="us-east-1")


def test_generic_command_backend_is_plan_only_and_requires_argv() -> None:
    backend = CommandBackend(command_prefix=["wrapper", "--hop"])
    relay = Relay(
        backend,
        policy=policy("command", live=True, allowlist=(("wrapper",),)),
    )
    result = relay.run(["echo", "hello"], timeout_s=5, dry_run=True)
    assert result.planned_argv == ("wrapper", "--hop", "echo", "hello")
    with pytest.raises(RelayUnsupported):
        relay.run(["echo"], timeout_s=5, dry_run=False)
    with pytest.raises(RelaySpecError):
        CommandBackend(command_prefix=["bad\x00arg"])
