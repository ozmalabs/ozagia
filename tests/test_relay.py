from __future__ import annotations

import json
import socket
import subprocess
import threading
from collections.abc import Sequence

import pytest

from ozagia.core.relay import (
    CommandBackend,
    CommandResult,
    LocalBackend,
    ProcessLease,
    Relay,
    RelayError,
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


class FakeProcess:
    def __init__(self, listener: socket.socket | None = None) -> None:
        self.listener = listener
        self.returncode: int | None = None
        self.terminated = 0

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def terminate(self) -> None:
        self.terminated += 1
        self.returncode = -15
        if self.listener is not None:
            self.listener.close()

    def kill(self) -> None:
        self.returncode = -9
        if self.listener is not None:
            self.listener.close()


def _tunnel_port(argv: Sequence[str]) -> int:
    if argv[0] == "ssh":
        forward = argv[argv.index("-L") + 1]
        return int(forward.split(":", 2)[1])
    parameters = json.loads(argv[argv.index("--parameters") + 1])
    return int(parameters["localPortNumber"][0])


def _listener_process_factory(
    calls: list[tuple[tuple[str, ...], dict[str, object]]],
):
    def make(argv: Sequence[str], **kwargs: object) -> FakeProcess:
        checked = tuple(argv)
        calls.append((checked, kwargs))
        port = _tunnel_port(checked)
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", port))
        listener.listen(1)
        return FakeProcess(listener)

    return make


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


def test_process_lease_is_argv_only_bounded_and_idempotently_closed() -> None:
    seen: list[tuple[tuple[str, ...], dict[str, object]]] = []
    process = FakeProcess()

    def factory(argv: Sequence[str], **kwargs: object) -> FakeProcess:
        seen.append((tuple(argv), kwargs))
        return process

    lease = ProcessLease(
        ["local-tool", "CANARY"],
        process_factory=factory,
    )
    lease.start()
    assert lease.wait(timeout_s=1) == 0
    lease.close()
    lease.close()
    assert seen[0][0] == ("local-tool", "CANARY")
    assert seen[0][1]["shell"] is False
    assert seen[0][1]["stdin"] == subprocess.DEVNULL
    assert seen[0][1]["start_new_session"] is True
    assert seen[0][1]["close_fds"] is True
    assert process.terminated == 0
    assert "CANARY" not in repr(lease)

    stopping_process = FakeProcess()
    stopping = ProcessLease(
        ["long-lived"],
        process_factory=lambda argv, **kwargs: stopping_process,
    )
    stopping.start()
    stopping.close()
    stopping.close()
    assert stopping_process.terminated == 1


def test_ssh_runs_exact_argv_through_parent_with_explicit_proxy_options() -> None:
    calls: list[tuple[str, ...]] = []
    explicit = policy(
        "local",
        "ssh",
        allowlist=(("ssh",),),
    )
    parent = Relay(
        LocalBackend(
            runner=lambda argv, **kwargs: (
                calls.append(tuple(argv)) or (0, "remote output", "")
            )
        ),
        policy=explicit,
    )
    backend = SshBackend(
        host="bastion.example",
        user="operator",
        port=2222,
        identity_file="/keys/relay",
        proxy_jump="jump.example:2200",
        proxy_command="nc -x proxy.example:1080 %h %p",
        parent=parent,
        policy=explicit,
    )
    relay = Relay(backend, parent=parent, policy=explicit, kind="ssh")

    result = relay.run(["uname", "-a"], timeout_s=5)

    assert calls == [
        (
            "ssh",
            "-o",
            "ExitOnForwardFailure=yes",
            "-p",
            "2222",
            "-i",
            "/keys/relay",
            "-J",
            "jump.example:2200",
            "-o",
            "ProxyCommand=nc -x proxy.example:1080 %h %p",
            "--",
            "operator@bastion.example",
            "uname",
            "-a",
        )
    ]
    assert result.stdout == "remote output"
    assert all("StrictHostKeyChecking=no" not in arg for arg in calls[0])


def test_ssh_dry_run_and_policy_denial_never_start_a_process() -> None:
    process_calls: list[tuple[tuple[str, ...], dict[str, object]]] = []
    explicit = policy("local", "ssh", network=True, allowlist=(("ssh",),))
    parent = Relay(
        LocalBackend(process_factory=_listener_process_factory(process_calls)),
        policy=explicit,
    )
    ssh = Relay(
        SshBackend(host="bastion.example", parent=parent, policy=explicit),
        parent=parent,
        policy=explicit,
        kind="ssh",
    )
    dry = ssh.open_tunnel(
        TunnelTarget("db.internal", 5432),
        timeout_s=5,
        dry_run=True,
    )
    assert dry.dry_run
    assert dry.planned_argv[:2] == ("ssh", "-N")
    assert process_calls == []

    denied = Relay(
        SshBackend(host="bastion.example", parent=parent),
        parent=parent,
        policy=RelayPolicy(),
        kind="ssh",
    )
    with pytest.raises(RelayPolicyError):
        denied.open_tunnel(TunnelTarget("db.internal", 5432), timeout_s=5)
    assert process_calls == []


def test_ssh_tunnel_waits_for_loopback_listener_and_closes_process() -> None:
    process_calls: list[tuple[tuple[str, ...], dict[str, object]]] = []
    explicit = policy(
        "local",
        "ssh",
        network=True,
        allowlist=(("ssh",),),
    )
    parent = Relay(
        LocalBackend(process_factory=_listener_process_factory(process_calls)),
        policy=explicit,
    )
    relay = Relay(
        SshBackend(
            host="bastion.example",
            port=2222,
            parent=parent,
            policy=explicit,
        ),
        parent=parent,
        policy=explicit,
        kind="ssh",
    )
    lease = relay.open_tunnel(TunnelTarget("db.internal", 5432), timeout_s=2)
    assert process_calls[0][0] == (
        "ssh",
        "-N",
        "-o",
        "ExitOnForwardFailure=yes",
        "-L",
        f"127.0.0.1:{lease.local_port}:db.internal:5432",
        "-p",
        "2222",
        "--",
        "bastion.example",
    )
    assert process_calls[0][1]["stdin"] == subprocess.DEVNULL
    probe = socket.create_connection((lease.local_host, lease.local_port), timeout=1)
    probe.close()
    lease.close()
    lease.close()
    with pytest.raises(OSError):
        socket.create_connection((lease.local_host, lease.local_port), timeout=0.1)


def test_tunnel_readiness_timeout_closes_the_started_process() -> None:
    process_calls: list[tuple[tuple[str, ...], dict[str, object]]] = []
    process_handles: list[FakeProcess] = []

    def no_listener(argv: Sequence[str], **kwargs: object) -> FakeProcess:
        process_calls.append((tuple(argv), kwargs))
        handle = FakeProcess()
        process_handles.append(handle)
        return handle

    explicit = policy(
        "local",
        "ssh",
        network=True,
        allowlist=(("ssh",),),
    )
    parent = Relay(
        LocalBackend(process_factory=no_listener),
        policy=explicit,
    )
    relay = Relay(
        SshBackend(host="bastion.example", parent=parent, policy=explicit),
        parent=parent,
        policy=explicit,
        kind="ssh",
    )
    with pytest.raises(RelayError, match="could not open relay tunnel"):
        relay.open_tunnel(TunnelTarget("db.internal", 5432), timeout_s=0.05)
    assert len(process_calls) == 1
    assert process_handles[0].terminated == 1


def test_ssm_plans_exact_send_and_both_tunnel_documents() -> None:
    ssm = SsmBackend(
        instance_id="i-0123456789abcdef0",
        region="us-east-1",
        command_id="00000000-0000-0000-0000-000000000001",
        policy=policy("ssm", allowlist=(("aws", "ssm"),)),
    )
    send = ssm.plan_run(["printf", "hello world"])
    assert send == (
        "aws",
        "ssm",
        "send-command",
        "--document-name",
        "AWS-RunShellScript",
        "--instance-ids",
        "i-0123456789abcdef0",
        "--parameters",
        '{"commands":["printf \'hello world\'"]}',
        "--region",
        "us-east-1",
        "--output",
        "json",
        "--no-cli-pager",
    )
    assert json.loads(send[send.index("--parameters") + 1]) == {
        "commands": ["printf 'hello world'"]
    }
    assert send[send.index("--region") + 1] == "us-east-1"

    instance_forward = ssm.plan_tunnel_argv(
        TunnelTarget("localhost", 5432),
        local_port=15432,
    )
    assert instance_forward == (
        "aws",
        "ssm",
        "start-session",
        "--target",
        "i-0123456789abcdef0",
        "--document-name",
        "AWS-StartPortForwardingSession",
        "--parameters",
        '{"portNumber":["5432"],"localPortNumber":["15432"]}',
        "--region",
        "us-east-1",
        "--no-cli-pager",
    )
    assert json.loads(instance_forward[instance_forward.index("--parameters") + 1]) == {
        "portNumber": ["5432"],
        "localPortNumber": ["15432"],
    }

    remote_forward = SsmBackend(
        instance_id="i-0123456789abcdef0",
        region="us-east-1",
        remote_host="db.internal",
    ).plan_tunnel_argv(TunnelTarget("unused", 5432), local_port=15432)
    assert remote_forward == (
        "aws",
        "ssm",
        "start-session",
        "--target",
        "i-0123456789abcdef0",
        "--document-name",
        "AWS-StartPortForwardingSessionToRemoteHost",
        "--parameters",
        '{"host":["db.internal"],"portNumber":["5432"],"localPortNumber":["15432"]}',
        "--region",
        "us-east-1",
        "--no-cli-pager",
    )
    assert json.loads(remote_forward[remote_forward.index("--parameters") + 1]) == {
        "host": ["db.internal"],
        "portNumber": ["5432"],
        "localPortNumber": ["15432"],
    }


def test_ssm_live_run_sends_then_polls_with_bounded_output() -> None:
    calls: list[tuple[str, ...]] = []

    def runner(argv: Sequence[str], **kwargs: object):
        del kwargs
        checked = tuple(argv)
        calls.append(checked)
        if checked[2] == "send-command":
            return 0, json.dumps({"Command": {"CommandId": "command-123"}}), ""
        return (
            0,
            json.dumps(
                {
                    "Status": "Success",
                    "ResponseCode": 0,
                    "StandardOutputContent": "output-too-long",
                    "StandardErrorContent": "",
                }
            ),
            "",
        )

    explicit = policy(
        "local",
        "ssm",
        allowlist=(("aws", "ssm"),),
        output=6,
    )
    parent = Relay(LocalBackend(runner=runner), policy=explicit)
    relay = Relay(
        SsmBackend(
            instance_id="i-0123456789abcdef0",
            region="us-east-1",
            parent=parent,
            policy=explicit,
        ),
        parent=parent,
        policy=explicit,
        kind="ssm",
    )
    result = relay.run(["printf", "hello world"], timeout_s=5)
    assert result.returncode == 0
    assert result.stdout == "output"
    assert [call[2] for call in calls] == [
        "send-command",
        "get-command-invocation",
    ]
    assert calls[0][calls[0].index("--document-name") + 1] == "AWS-RunShellScript"
    assert calls[0][calls[0].index("--region") + 1] == "us-east-1"
    assert calls[1][calls[1].index("--command-id") + 1] == "command-123"


@pytest.mark.parametrize(
    ("status", "response_code", "expected"),
    [
        ("Failed", 23, 23),
        ("TimedOut", -1, 124),
        ("Cancelled", -1, 130),
    ],
)
def test_ssm_terminal_failure_statuses_map_to_exit_codes(
    status: str, response_code: int, expected: int
) -> None:
    def runner(argv: Sequence[str], **kwargs: object):
        del kwargs
        if argv[2] == "send-command":
            return 0, '{"Command":{"CommandId":"command-123"}}', ""
        return (
            0,
            json.dumps(
                {
                    "Status": status,
                    "ResponseCode": response_code,
                    "StandardOutputContent": "",
                    "StandardErrorContent": "remote failure",
                }
            ),
            "",
        )

    explicit = policy("local", "ssm", allowlist=(("aws", "ssm"),))
    parent = Relay(LocalBackend(runner=runner), policy=explicit)
    relay = Relay(
        SsmBackend(
            instance_id="i-0123456789abcdef0",
            region="us-east-1",
            parent=parent,
            policy=explicit,
        ),
        parent=parent,
        policy=explicit,
        kind="ssm",
    )
    assert relay.run(["true"], timeout_s=2).returncode == expected


def test_ssm_timeout_malformed_json_and_canary_redaction() -> None:
    def pending_runner(argv: Sequence[str], **kwargs: object):
        del kwargs
        if argv[2] == "send-command":
            return 0, '{"Command":{"CommandId":"command-123"}}', ""
        return 0, '{"Status":"InProgress"}', ""

    explicit = policy("local", "ssm", allowlist=(("aws", "ssm"),))
    parent = Relay(LocalBackend(runner=pending_runner), policy=explicit)
    relay = Relay(
        SsmBackend(
            instance_id="i-0123456789abcdef0",
            region="us-east-1",
            parent=parent,
            policy=explicit,
        ),
        parent=parent,
        policy=explicit,
        kind="ssm",
    )
    assert relay.run(["sleep", "1"], timeout_s=0.03).returncode == 124

    canary = "SSM-OUTPUT-CANARY"

    def malformed_runner(argv: Sequence[str], **kwargs: object):
        del argv, kwargs
        return 0, f"not-json-{canary}", ""

    malformed_parent = Relay(
        LocalBackend(runner=malformed_runner),
        policy=explicit,
    )
    malformed = Relay(
        SsmBackend(
            instance_id="i-0123456789abcdef0",
            region="us-east-1",
            parent=malformed_parent,
            policy=explicit,
        ),
        parent=malformed_parent,
        policy=explicit,
        kind="ssm",
    )
    with pytest.raises(RelayError) as caught:
        malformed.run(["true"], timeout_s=2)
    assert canary not in str(caught.value)
    assert canary not in repr(malformed._backend)


def test_ssm_requires_allowlist_and_session_manager_plugin() -> None:
    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []
    explicit = policy("local", "ssm", network=True)
    parent = Relay(
        LocalBackend(process_factory=_listener_process_factory(calls)),
        policy=explicit,
    )
    backend = SsmBackend(
        instance_id="i-0123456789abcdef0",
        region="us-east-1",
        parent=parent,
        executable_lookup=lambda name: None,
        policy=explicit,
    )
    with pytest.raises(RelayUnsupported, match="plugin"):
        backend.open_tunnel(TunnelTarget("localhost", 5432), timeout_s=2)
    assert calls == []

    no_allowlist = Relay(
        SsmBackend(
            instance_id="i-0123456789abcdef0",
            region="us-east-1",
            parent=parent,
            policy=explicit,
        ),
        parent=parent,
        policy=explicit,
        kind="ssm",
    )
    with pytest.raises(RelayPolicyError):
        no_allowlist.run(["id"], timeout_s=2)
    assert calls == []

    dry_calls: list[tuple[str, ...]] = []
    dry_policy = policy(
        "local",
        "ssm",
        live=False,
        allowlist=(("aws", "ssm"),),
    )
    dry_parent = Relay(
        LocalBackend(
            runner=lambda argv, **kwargs: (
                dry_calls.append(tuple(argv)) or (0, "", "")
            )
        ),
        policy=dry_policy,
    )
    dry_relay = Relay(
        SsmBackend(
            instance_id="i-0123456789abcdef0",
            region="us-east-1",
            parent=dry_parent,
            policy=dry_policy,
        ),
        parent=dry_parent,
        policy=dry_policy,
        kind="ssm",
    )
    planned = dry_relay.run(["id"], timeout_s=2, dry_run=True)
    assert planned.dry_run
    assert planned.planned_argv[2] == "send-command"
    assert dry_calls == []


def test_ssm_poll_allowlist_is_checked_before_send_command() -> None:
    calls: list[tuple[str, ...]] = []
    explicit = policy(
        "local",
        "ssm",
        allowlist=(("aws", "ssm", "send-command"),),
    )
    parent = Relay(
        LocalBackend(
            runner=lambda argv, **kwargs: (
                calls.append(tuple(argv)) or (0, "", "")
            )
        ),
        policy=explicit,
    )
    relay = Relay(
        SsmBackend(
            instance_id="i-0123456789abcdef0",
            region="us-east-1",
            parent=parent,
            policy=explicit,
        ),
        parent=parent,
        policy=explicit,
        kind="ssm",
    )
    with pytest.raises(RelayPolicyError):
        relay.run(["id"], timeout_s=2)
    assert calls == []


def test_ssm_remote_host_tunnel_waits_and_closes_on_loopback() -> None:
    process_calls: list[tuple[tuple[str, ...], dict[str, object]]] = []
    explicit = policy(
        "local",
        "ssm",
        network=True,
        allowlist=(("aws", "ssm", "start-session"),),
    )
    parent = Relay(
        LocalBackend(process_factory=_listener_process_factory(process_calls)),
        policy=explicit,
    )
    relay = Relay(
        SsmBackend(
            instance_id="i-0123456789abcdef0",
            region="us-east-1",
            remote_host="db.internal",
            parent=parent,
            executable_lookup=lambda name: "/fake/session-manager-plugin",
            policy=explicit,
        ),
        parent=parent,
        policy=explicit,
        kind="ssm",
    )
    lease = relay.open_tunnel(TunnelTarget("unused", 5432), timeout_s=2)
    argv = process_calls[0][0]
    assert argv[argv.index("--document-name") + 1] == (
        "AWS-StartPortForwardingSessionToRemoteHost"
    )
    assert process_calls[0][1]["stdin"] == subprocess.DEVNULL
    lease.close()
    with pytest.raises(OSError):
        socket.create_connection((lease.local_host, lease.local_port), timeout=0.1)


def test_nested_ssh_over_ssm_uses_fake_local_parent_only() -> None:
    calls: list[tuple[str, ...]] = []

    def runner(argv: Sequence[str], **kwargs: object):
        del kwargs
        checked = tuple(argv)
        calls.append(checked)
        if checked[2] == "send-command":
            return 0, '{"Command":{"CommandId":"nested-1"}}', ""
        return (
            0,
            '{"Status":"Success","ResponseCode":0,'
            '"StandardOutputContent":"nested ok","StandardErrorContent":""}',
            "",
        )

    explicit = policy(
        "local",
        "ssm",
        "ssh",
        allowlist=(("aws", "ssm"),),
    )
    local = Relay(LocalBackend(runner=runner), policy=explicit)
    ssm = Relay(
        SsmBackend(
            instance_id="i-0123456789abcdef0",
            region="us-east-1",
            parent=local,
            policy=explicit,
        ),
        parent=local,
        policy=explicit,
        kind="ssm",
    )
    ssh = Relay(
        SshBackend(
            host="nested.example",
            parent=ssm,
            policy=explicit,
        ),
        parent=ssm,
        policy=explicit,
        kind="ssh",
    )

    result = ssh.run(["uname", "-a"], timeout_s=5)

    assert result.stdout == "nested ok"
    assert [call[2] for call in calls] == [
        "send-command",
        "get-command-invocation",
    ]
    remote_script = json.loads(
        calls[0][calls[0].index("--parameters") + 1]
    )["commands"][0]
    assert remote_script == (
        "ssh -o ExitOnForwardFailure=yes -- nested.example uname -a"
    )
