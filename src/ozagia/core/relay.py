"""Policy-gated command relays and loopback TCP forwarding.

The core has no provider dependencies. Remote SSH/SSM backends in this module
are planners only: they expose argv plans, but never start remote processes.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import re
import select
import socket
import subprocess
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

_MAX_ARGV_COUNT = 128
_MAX_ARG_LENGTH = 4_096
_MAX_ARGV_BYTES = 32 * 1_024
_MAX_TIMEOUT_S = 86_400.0
_MAX_OUTPUT_BYTES = 64 * 1_024
_MAX_TUNNEL_CONNECTIONS = 16
_KIND = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_HOST = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,252}$")
_REGION = re.compile(r"^[a-z]{2}(?:-[a-z]+)+-\d+$")


class RelayError(ValueError):
    """Base class for safe, non-sensitive relay errors."""


class RelayPolicyError(RelayError):
    """The explicit relay policy does not allow an operation."""


class RelayUnsupported(RelayError):
    """A backend does not support an operation."""


class RelaySpecError(RelayError):
    """A relay specification or argv is malformed."""


@dataclass(frozen=True)
class CommandResult:
    """Bounded command result; repr intentionally omits all command output."""

    returncode: int
    stdout: str = field(default="", repr=False)
    stderr: str = field(default="", repr=False)
    elapsed_s: float = 0.0
    dry_run: bool = False
    backend: str = "unknown"
    argv_evidence: str = ""
    planned_argv: tuple[str, ...] = field(default=(), repr=False)


@dataclass(frozen=True)
class TunnelTarget:
    """A TCP target. Live local forwarding is restricted to loopback targets."""

    host: str
    port: int


@dataclass
class TunnelLease:
    """An idempotently closable tunnel (or a dry-run tunnel plan)."""

    local_host: str
    local_port: int
    target_host: str
    target_port: int
    scope: str
    backend: str
    close_callable: Callable[[], None] = field(repr=False, compare=False)
    dry_run: bool = False
    planned_argv: tuple[str, ...] = field(default=(), repr=False)
    _closed: bool = field(default=False, init=False, repr=False, compare=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False, compare=False)

    def close(self) -> None:
        """Close the lease once; never expose backend exception details."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
        try:
            self.close_callable()
        except Exception:
            raise RelayError("failed to close relay tunnel") from None

    def __enter__(self) -> TunnelLease:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


@dataclass(frozen=True)
class RelayPolicy:
    """An explicit allowlist; the empty/default policy denies every operation.

    `command_allowlist` entries are argv prefixes. If omitted, command shape is
    unrestricted only after the kind and live-execution gates are explicitly
    enabled. Policies should use prefixes for every live command.
    """

    allowed_kinds: frozenset[str] = frozenset()
    allow_live: bool = False
    allow_network: bool = False
    max_timeout_s: float = 0.0
    command_allowlist: tuple[tuple[str, ...], ...] | None = field(default=None, repr=False)
    max_output_bytes: int = 4_096

    def __post_init__(self) -> None:
        if not isinstance(self.allowed_kinds, (set, frozenset, tuple, list)):
            raise RelaySpecError("allowed_kinds must be a collection")
        object.__setattr__(self, "allowed_kinds", frozenset(self.allowed_kinds))
        if any(not isinstance(kind, str) or not _KIND.fullmatch(kind) for kind in self.allowed_kinds):
            raise RelaySpecError("invalid relay policy kind")
        if (
            isinstance(self.max_timeout_s, bool)
            or not isinstance(self.max_timeout_s, (int, float))
            or not math.isfinite(self.max_timeout_s)
            or self.max_timeout_s < 0
            or self.max_timeout_s > _MAX_TIMEOUT_S
        ):
            raise RelaySpecError("max_timeout_s must be between 0 and 86400")
        if (
            isinstance(self.max_output_bytes, bool)
            or not isinstance(self.max_output_bytes, int)
            or not 0 <= self.max_output_bytes <= _MAX_OUTPUT_BYTES
        ):
            raise RelaySpecError("max_output_bytes is outside the supported range")
        if self.command_allowlist is not None:
            if not isinstance(self.command_allowlist, tuple) or not self.command_allowlist:
                raise RelaySpecError("command_allowlist must contain argv prefixes")
            for prefix in self.command_allowlist:
                _validate_argv(prefix)

    def check_kind(self, kind: str) -> None:
        if kind not in self.allowed_kinds:
            raise RelayPolicyError("relay kind is not allowed")

    def check_command(self, kind: str, argv: Sequence[str], timeout_s: float, *, live: bool) -> None:
        self.check_command_gate(kind, timeout_s, live=live)
        _validate_argv(argv)
        if self.command_allowlist is not None and not any(
            tuple(argv[: len(prefix)]) == prefix for prefix in self.command_allowlist
        ):
            raise RelayPolicyError("command is not allowlisted")

    def check_command_gate(self, kind: str, timeout_s: float, *, live: bool) -> None:
        self.check_kind(kind)
        _validate_timeout(timeout_s)
        if self.max_timeout_s <= 0 or timeout_s > self.max_timeout_s:
            raise RelayPolicyError("relay timeout exceeds policy")
        if live and not self.allow_live:
            raise RelayPolicyError("live relay execution is disabled")

    def check_tunnel(self, kind: str, timeout_s: float, *, live: bool) -> None:
        self.check_kind(kind)
        _validate_timeout(timeout_s)
        if self.max_timeout_s <= 0 or timeout_s > self.max_timeout_s:
            raise RelayPolicyError("relay timeout exceeds policy")
        if live and not self.allow_live:
            raise RelayPolicyError("live relay execution is disabled")
        if live and not self.allow_network:
            raise RelayPolicyError("network relays are disabled")


@runtime_checkable
class RelayBackend(Protocol):
    """Backend contract. Implementations should not include raw inputs in errors."""

    def run(
        self,
        argv: Sequence[str],
        *,
        timeout_s: float,
        stdin: bytes | None = None,
    ) -> CommandResult: ...

    def open_tunnel(
        self,
        target: TunnelTarget,
        *,
        local_port: int = 0,
        timeout_s: float = 30.0,
    ) -> TunnelLease: ...

    def close(self) -> None: ...


CommandRunner = Callable[..., CommandResult | tuple[int, str, str]]
RelayFactory = Callable[..., RelayBackend]


def _validate_timeout(timeout_s: float) -> None:
    if (
        isinstance(timeout_s, bool)
        or not isinstance(timeout_s, (int, float))
        or not math.isfinite(timeout_s)
        or timeout_s <= 0
    ):
        raise RelaySpecError("timeout_s must be a finite positive number")


def _validate_argv(argv: Sequence[str]) -> tuple[str, ...]:
    if isinstance(argv, (str, bytes)) or not isinstance(argv, Sequence):
        raise RelaySpecError("argv must be a sequence of strings")
    if not 1 <= len(argv) <= _MAX_ARGV_COUNT:
        raise RelaySpecError("argv has an invalid number of arguments")
    result: list[str] = []
    total = 0
    for arg in argv:
        if (
            not isinstance(arg, str)
            or not arg
            or len(arg) > _MAX_ARG_LENGTH
            or "\x00" in arg
            or any(ord(char) < 32 and char not in "\t" for char in arg)
        ):
            raise RelaySpecError("argv contains an invalid argument")
        total += len(arg.encode("utf-8"))
        if total > _MAX_ARGV_BYTES:
            raise RelaySpecError("argv exceeds the supported size")
        result.append(arg)
    return tuple(result)


def _validate_port(port: int, *, allow_zero: bool = False) -> None:
    low = 0 if allow_zero else 1
    if isinstance(port, bool) or not isinstance(port, int) or not low <= port <= 65_535:
        raise RelaySpecError("invalid TCP port")


def _validate_target(target: TunnelTarget, *, loopback_only: bool) -> None:
    if not isinstance(target, TunnelTarget):
        raise RelaySpecError("target must be a TunnelTarget")
    if not isinstance(target.host, str) or not target.host or len(target.host) > 253:
        raise RelaySpecError("invalid tunnel target")
    _validate_port(target.port)
    if loopback_only:
        try:
            loopback = ipaddress.ip_address(target.host).is_loopback
        except ValueError:
            loopback = target.host.lower() == "localhost"
        if not loopback:
            raise RelayPolicyError("local tunnels may target loopback addresses only")


def _evidence(argv: Sequence[str]) -> str:
    encoded = b"\0".join(item.encode("utf-8") for item in argv)
    return f"sha256:{hashlib.sha256(encoded).hexdigest()[:16]};argc:{len(argv)}"


def _bounded_text(value: str, limit: int) -> str:
    encoded = value.encode("utf-8", errors="replace")[:limit]
    return encoded.decode("utf-8", errors="ignore")


def _subprocess_runner(
    argv: Sequence[str], *, timeout_s: float, stdin: bytes | None = None
) -> tuple[int, str, str]:
    captured = [bytearray(), bytearray()]

    def drain(stream: Any, output: bytearray) -> None:
        try:
            while True:
                chunk = stream.read(8 * 1_024)
                if not chunk:
                    return
                remaining = _MAX_OUTPUT_BYTES - len(output)
                if remaining > 0:
                    output.extend(chunk[:remaining])
        except OSError:
            return

    try:
        process = subprocess.Popen(
            list(argv),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
        )
        assert process.stdout is not None and process.stderr is not None
        readers = [
            threading.Thread(target=drain, args=(process.stdout, captured[0]), daemon=True),
            threading.Thread(target=drain, args=(process.stderr, captured[1]), daemon=True),
        ]
        for reader in readers:
            reader.start()
        if process.stdin is not None:
            if stdin is None:
                process.stdin.close()
            else:
                def write_input() -> None:
                    try:
                        process.stdin.write(stdin)
                    except OSError:
                        pass
                    finally:
                        try:
                            process.stdin.close()
                        except OSError:
                            pass

                threading.Thread(target=write_input, daemon=True).start()
        try:
            returncode = process.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            raise RelayError("local command timed out") from None
        for reader in readers:
            reader.join(timeout=1.0)
    except RelayError:
        raise
    except (OSError, subprocess.SubprocessError):
        raise RelayError("local command failed") from None
    return (
        returncode,
        captured[0].decode("utf-8", errors="replace"),
        captured[1].decode("utf-8", errors="replace"),
    )


class LocalTcpTunnel:
    """Bounded TCP forwarder which binds only to IPv4 loopback."""

    def __init__(
        self,
        target: TunnelTarget,
        *,
        local_port: int = 0,
        max_connections: int = _MAX_TUNNEL_CONNECTIONS,
        policy: RelayPolicy | None = None,
        timeout_s: float = 30.0,
    ) -> None:
        if policy is None:
            raise RelayPolicyError("an explicit relay policy is required")
        policy.check_tunnel("local", timeout_s, live=True)
        _validate_target(target, loopback_only=True)
        _validate_port(local_port, allow_zero=True)
        self.target = target
        self._stopped = threading.Event()
        self._lock = threading.Lock()
        self._active: set[socket.socket] = set()
        self._slots = threading.BoundedSemaphore(max_connections)
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", local_port))
            listener.listen(max_connections)
            listener.settimeout(0.2)
        except Exception:
            listener.close()
            raise RelayError("could not bind loopback tunnel") from None
        self._listener = listener
        self.local_host = "127.0.0.1"
        self.local_port = listener.getsockname()[1]
        self._thread = threading.Thread(target=self._accept, name="ozagia-relay-accept", daemon=True)
        self._thread.start()

    def _accept(self) -> None:
        while not self._stopped.is_set():
            try:
                client, _address = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            if not self._slots.acquire(blocking=False):
                client.close()
                continue
            worker = threading.Thread(target=self._forward, args=(client,), daemon=True)
            worker.start()

    def _forward(self, client: socket.socket) -> None:
        upstream: socket.socket | None = None
        with self._lock:
            self._active.add(client)
        try:
            client.settimeout(2.0)
            upstream = socket.create_connection((self.target.host, self.target.port), timeout=2.0)
            upstream.settimeout(None)
            with self._lock:
                self._active.add(upstream)
            sockets = [client, upstream]
            while not self._stopped.is_set():
                readable, _, _ = select.select(sockets, [], [], 0.2)
                for source in readable:
                    destination = upstream if source is client else client
                    data = source.recv(16 * 1_024)
                    if not data:
                        try:
                            destination.shutdown(socket.SHUT_WR)
                        except OSError:
                            pass
                        sockets.remove(source)
                        if not sockets:
                            return
                        continue
                    destination.sendall(data)
        except (OSError, ValueError):
            pass
        finally:
            for connection in (client, upstream):
                if connection is not None:
                    with self._lock:
                        self._active.discard(connection)
                    try:
                        connection.close()
                    except OSError:
                        pass
            self._slots.release()

    def close(self) -> None:
        if self._stopped.is_set():
            return
        self._stopped.set()
        try:
            self._listener.close()
        except OSError:
            pass
        with self._lock:
            active = tuple(self._active)
        for connection in active:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                connection.close()
            except OSError:
                pass
        self._thread.join(timeout=1.0)


class LocalBackend:
    """Local argv runner and loopback-only TCP tunnel backend."""

    kind = "local"

    def __init__(
        self,
        *,
        runner: CommandRunner | None = None,
        policy: RelayPolicy | None = None,
    ) -> None:
        self._runner = runner or _subprocess_runner
        self.policy = policy if policy is not None else RelayPolicy()
        self._closed = False

    def run(
        self,
        argv: Sequence[str],
        *,
        timeout_s: float,
        stdin: bytes | None = None,
    ) -> CommandResult:
        if self._closed:
            raise RelayError("relay backend is closed")
        checked_argv = _validate_argv(argv)
        if stdin is not None and not isinstance(stdin, bytes):
            raise RelaySpecError("stdin must be bytes")
        self.policy.check_command(self.kind, checked_argv, timeout_s, live=True)
        started = time.monotonic()
        try:
            raw = self._runner(checked_argv, timeout_s=timeout_s, stdin=stdin)
        except Exception:
            raise RelayError("local command failed") from None
        elapsed = max(0.0, time.monotonic() - started)
        if isinstance(raw, CommandResult):
            result = raw
        elif (
            isinstance(raw, tuple)
            and len(raw) == 3
            and isinstance(raw[0], int)
            and not isinstance(raw[0], bool)
            and isinstance(raw[1], str)
            and isinstance(raw[2], str)
        ):
            result = CommandResult(raw[0], raw[1], raw[2], elapsed, backend=self.kind)
        else:
            raise RelayError("local command runner returned an invalid result")
        return CommandResult(
            result.returncode,
            _bounded_text(result.stdout, _MAX_OUTPUT_BYTES),
            _bounded_text(result.stderr, _MAX_OUTPUT_BYTES),
            max(0.0, result.elapsed_s or elapsed),
            result.dry_run,
            self.kind,
            result.argv_evidence,
            result.planned_argv,
        )

    def plan_run(self, argv: Sequence[str]) -> tuple[str, ...]:
        return _validate_argv(argv)

    def open_tunnel(
        self,
        target: TunnelTarget,
        *,
        local_port: int = 0,
        timeout_s: float = 30.0,
    ) -> TunnelLease:
        if self._closed:
            raise RelayError("relay backend is closed")
        self.policy.check_tunnel(self.kind, timeout_s, live=True)
        tunnel = LocalTcpTunnel(
            target,
            local_port=local_port,
            policy=self.policy,
            timeout_s=timeout_s,
        )
        return TunnelLease(
            tunnel.local_host,
            tunnel.local_port,
            target.host,
            target.port,
            "local loopback",
            self.kind,
            tunnel.close,
        )

    def plan_tunnel(
        self, target: TunnelTarget, *, local_port: int = 0
    ) -> TunnelLease:
        _validate_target(target, loopback_only=True)
        _validate_port(local_port, allow_zero=True)
        return TunnelLease(
            "127.0.0.1",
            local_port,
            target.host,
            target.port,
            "local loopback (dry-run)",
            self.kind,
            lambda: None,
            dry_run=True,
        )

    def close(self) -> None:
        self._closed = True


class CommandBackend:
    """Safe argv planner for wrapper commands; it never starts a process.

    `command_prefix` is prepended as argv elements. The `parent` is retained
    for compositional planning only; live wrapping is deliberately unsupported.
    """

    kind = "command"

    def __init__(
        self,
        *,
        command_prefix: Sequence[str],
        parent: Relay | None = None,
        tunnel_command: Sequence[str] | None = None,
        policy: RelayPolicy | None = None,
    ) -> None:
        del policy
        self.command_prefix = _validate_argv(command_prefix)
        self.tunnel_command = (
            _validate_argv(tunnel_command) if tunnel_command is not None else None
        )
        self.parent = parent

    def plan_argv(self, argv: Sequence[str]) -> tuple[str, ...]:
        combined = self.command_prefix + _validate_argv(argv)
        if self.parent is not None:
            return self.parent.plan_argv(combined)
        return _validate_argv(combined)

    def plan_tunnel_argv(
        self,
        target: TunnelTarget,
        *,
        local_port: int,
    ) -> tuple[str, ...]:
        if self.tunnel_command is None:
            raise RelayUnsupported("backend does not support tunnel plans")
        values = {
            "local_host": "127.0.0.1",
            "local_port": str(local_port),
            "target_host": target.host,
            "target_port": str(target.port),
        }
        try:
            expanded = tuple(part.format_map(values) for part in self.tunnel_command)
        except (KeyError, ValueError):
            raise RelaySpecError("invalid tunnel argv template") from None
        if self.parent is not None:
            return self.parent.plan_argv(expanded)
        return _validate_argv(expanded)

    def run(
        self,
        argv: Sequence[str],
        *,
        timeout_s: float,
        stdin: bytes | None = None,
    ) -> CommandResult:
        del argv, timeout_s, stdin
        raise RelayUnsupported("command wrappers are dry-run only")

    def open_tunnel(
        self,
        target: TunnelTarget,
        *,
        local_port: int = 0,
        timeout_s: float = 30.0,
    ) -> TunnelLease:
        del target, local_port, timeout_s
        raise RelayUnsupported("command wrappers do not start tunnels")

    def close(self) -> None:
        return None


class SshBackend(CommandBackend):
    """SSH argv planner requiring an explicit host; it never launches SSH."""

    kind = "ssh"

    def __init__(
        self,
        *,
        host: str,
        parent: Relay | None = None,
        user: str | None = None,
        port: int | None = None,
        identity_file: str | None = None,
        policy: RelayPolicy | None = None,
    ) -> None:
        if not isinstance(host, str) or not _HOST.fullmatch(host) or host.startswith("-"):
            raise RelaySpecError("SSH requires a valid explicit host")
        destination = host
        if user is not None:
            if not isinstance(user, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", user):
                raise RelaySpecError("invalid SSH user")
            destination = f"{user}@{host}"
        prefix = ["ssh", "-o", "ExitOnForwardFailure=yes"]
        if port is not None:
            _validate_port(port)
            prefix.extend(["-p", str(port)])
        if identity_file is not None:
            if (
                not isinstance(identity_file, str)
                or not identity_file
                or len(identity_file) > _MAX_ARG_LENGTH
                or "\x00" in identity_file
                or any(ord(char) < 32 for char in identity_file)
            ):
                raise RelaySpecError("invalid SSH identity file")
            prefix.extend(["-i", identity_file])
        prefix.extend(["--", destination])
        tunnel = [
            "ssh", "-N", "-o", "ExitOnForwardFailure=yes",
            "-L", "{local_host}:{local_port}:{target_host}:{target_port}",
        ]
        if port is not None:
            tunnel.extend(["-p", str(port)])
        if identity_file is not None:
            tunnel.extend(["-i", identity_file])
        tunnel.extend(["--", destination])
        super().__init__(
            command_prefix=prefix,
            parent=parent,
            tunnel_command=tunnel,
            policy=policy,
        )

    def plan_run(self, argv: Sequence[str]) -> tuple[str, ...]:
        return self.plan_argv(argv)


class SsmBackend:
    """Dry-run-only AWS SSM command/tunnel shape planner.

    Plans require an explicit instance ID and region. This implementation does
    not invoke AWS CLI or start a Session Manager plugin.
    """

    kind = "ssm"

    def __init__(
        self,
        *,
        instance_id: str,
        region: str,
        parent: Relay | None = None,
        command_id: str | None = None,
        policy: RelayPolicy | None = None,
    ) -> None:
        del policy
        if not isinstance(instance_id, str) or not re.fullmatch(
            r"i-[A-Za-z0-9]{8,32}", instance_id
        ):
            raise RelaySpecError("SSM requires a valid explicit instance ID")
        if not isinstance(region, str) or not _REGION.fullmatch(region):
            raise RelaySpecError("SSM requires a valid explicit region")
        if command_id is not None and (
            not isinstance(command_id, str)
            or not re.fullmatch(r"[A-Za-z0-9-]{1,128}", command_id)
        ):
            raise RelaySpecError("invalid SSM command ID")
        self.instance_id = instance_id
        self.region = region
        self.command_id = command_id
        self.parent = parent

    def plan_status(self) -> tuple[str, ...]:
        if self.command_id is None:
            raise RelaySpecError("read-only SSM status plan requires a command ID")
        argv = (
            "aws", "ssm", "get-command-invocation",
            "--command-id", self.command_id,
            "--instance-id", self.instance_id,
            "--region", self.region,
            "--output", "json",
            "--no-cli-pager",
        )
        return self.parent.plan_argv(argv) if self.parent is not None else argv

    def plan_tunnel_argv(
        self, target: TunnelTarget, *, local_port: int
    ) -> tuple[str, ...]:
        _validate_port(local_port, allow_zero=True)
        argv = (
            "aws", "ssm", "start-session",
            "--target", self.instance_id,
            "--document-name", "AWS-StartPortForwardingSession",
            "--parameters",
            json.dumps(
                {
                    "portNumber": [str(target.port)],
                    "localPortNumber": [str(local_port)],
                },
                separators=(",", ":"),
            ),
            "--region", self.region,
            "--no-cli-pager",
        )
        return self.parent.plan_argv(argv) if self.parent is not None else argv

    def plan_run(self, argv: Sequence[str]) -> tuple[str, ...]:
        if tuple(argv) != ("status",):
            raise RelayUnsupported(
                "SSM command execution is disabled; use the read-only status plan"
            )
        return self.plan_status()

    def run(
        self,
        argv: Sequence[str],
        *,
        timeout_s: float,
        stdin: bytes | None = None,
    ) -> CommandResult:
        del argv, timeout_s, stdin
        raise RelayUnsupported("SSM is a dry-run-only backend")

    def open_tunnel(
        self,
        target: TunnelTarget,
        *,
        local_port: int = 0,
        timeout_s: float = 30.0,
    ) -> TunnelLease:
        del target, local_port, timeout_s
        raise RelayUnsupported("SSM tunnels are dry-run only")

    def close(self) -> None:
        return None


class RelayRegistry:
    """Mapping of backend kind to a factory accepting options/parent/policy."""

    def __init__(self) -> None:
        self._factories: dict[str, RelayFactory] = {}

    def register(self, kind: str, factory: RelayFactory) -> None:
        if not isinstance(kind, str) or not _KIND.fullmatch(kind):
            raise RelaySpecError("invalid relay kind")
        if not callable(factory):
            raise RelaySpecError("relay factory must be callable")
        self._factories[kind] = factory

    def factory(self, kind: str) -> RelayFactory:
        try:
            return self._factories[kind]
        except KeyError:
            raise RelaySpecError("unknown relay kind") from None


DEFAULT_RELAY_REGISTRY = RelayRegistry()


def register_relay(kind: str, factory: RelayFactory) -> None:
    """Register a backend factory in the process-wide default registry."""
    DEFAULT_RELAY_REGISTRY.register(kind, factory)


@dataclass(frozen=True)
class RelaySpec:
    kind: str
    options: Mapping[str, Any] = field(default_factory=dict, repr=False)
    name: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str) or not _KIND.fullmatch(self.kind):
            raise RelaySpecError("invalid relay kind")
        if not isinstance(self.options, Mapping):
            raise RelaySpecError("relay options must be a mapping")
        if self.name is not None and (
            not isinstance(self.name, str) or len(self.name) > 128
        ):
            raise RelaySpecError("invalid relay name")


class Relay:
    """A policy-gated backend node with an optional route parent.

    A chain spec is ordered from inside to outside: `[local, ssm, ssh]` means
    local transport, then SSM, then SSH. Each node owns only its own backend;
    close the outer node first and then its parent(s) if explicit cleanup of a
    complete chain is desired.
    """

    def __init__(
        self,
        backend: RelayBackend | None = None,
        *,
        parent: Relay | None = None,
        policy: RelayPolicy | None = None,
        kind: str | None = None,
        factory: RelayFactory | None = None,
        options: Mapping[str, Any] | None = None,
        ephemeral: bool = False,
    ) -> None:
        if backend is None and factory is None:
            raise RelaySpecError("relay requires a backend or factory")
        self.parent = parent
        self.policy = policy if policy is not None else RelayPolicy()
        self.kind = kind or getattr(backend, "kind", "custom")
        if not isinstance(self.kind, str) or not _KIND.fullmatch(self.kind):
            raise RelaySpecError("invalid relay kind")
        self._factory = factory
        self._options = dict(options or {})
        self._backend = backend
        if backend is not None and hasattr(backend, "policy"):
            try:
                backend.policy = self.policy
            except Exception:
                pass
        self._ephemeral = ephemeral
        self._closed = False
        self._lock = threading.RLock()
        self._leases: set[int] = set()
        self._lease_objects: dict[int, TunnelLease] = {}

    @classmethod
    def chain(
        cls,
        specs: Iterable[RelaySpec],
        *,
        registry: RelayRegistry | None = None,
        policy: RelayPolicy | None = None,
        ephemeral: bool = False,
    ) -> Relay:
        """Build a chain from inner/base to outermost relay."""
        return cls.from_specs(specs, registry=registry, policy=policy, ephemeral=ephemeral)

    @classmethod
    def from_specs(
        cls,
        specs: Iterable[RelaySpec],
        *,
        registry: RelayRegistry | None = None,
        policy: RelayPolicy | None = None,
        ephemeral: bool = False,
    ) -> Relay:
        entries = tuple(specs)
        if not entries or any(not isinstance(spec, RelaySpec) for spec in entries):
            raise RelaySpecError("specs must contain at least one RelaySpec")
        selected_registry = registry or DEFAULT_RELAY_REGISTRY
        selected_policy = policy if policy is not None else RelayPolicy()
        parent: Relay | None = None
        for spec in entries:
            factory = selected_registry.factory(spec.kind)
            parent = cls(
                parent=parent,
                policy=selected_policy,
                kind=spec.kind,
                factory=factory,
                options=spec.options,
                ephemeral=ephemeral,
            )
        assert parent is not None
        return parent

    def _ensure_open(self) -> None:
        if self._closed:
            raise RelayError("relay is closed")

    def _make_backend(self) -> RelayBackend:
        if self._factory is None:
            assert self._backend is not None
            return self._backend
        try:
            backend = self._factory(
                options=self._options,
                parent=self.parent,
                policy=self.policy,
            )
        except Exception:
            raise RelayError("relay backend factory failed") from None
        if not isinstance(backend, RelayBackend):
            raise RelayError("relay factory returned an invalid backend")
        return backend

    def _persistent_backend(self) -> RelayBackend:
        with self._lock:
            self._ensure_open()
            if self._backend is None:
                self._backend = self._make_backend()
            return self._backend

    def plan_argv(self, argv: Sequence[str]) -> tuple[str, ...]:
        self.policy.check_kind(self.kind)
        self._ensure_open()
        checked = _validate_argv(argv)
        try:
            backend = self._persistent_backend()
            planner = getattr(backend, "plan_argv", None)
            if callable(planner):
                return _validate_argv(planner(checked))
            planner = getattr(backend, "plan_run", None)
            if callable(planner):
                return _validate_argv(planner(checked))
            return checked
        except RelayUnsupported:
            raise RelayUnsupported("relay plan is unsupported") from None
        except Exception:
            raise RelayError("relay planning failed") from None

    def _planned_argv(self, backend: RelayBackend, argv: Sequence[str]) -> tuple[str, ...]:
        planner = getattr(backend, "plan_argv", None)
        if callable(planner):
            return _validate_argv(planner(argv))
        planner = getattr(backend, "plan_run", None)
        if callable(planner):
            return _validate_argv(planner(argv))
        return _validate_argv(argv)

    def run(
        self,
        argv: Sequence[str],
        *,
        timeout_s: float = 30.0,
        stdin: bytes | None = None,
        dry_run: bool = False,
    ) -> CommandResult:
        checked = _validate_argv(argv)
        if stdin is not None and not isinstance(stdin, bytes):
            raise RelaySpecError("stdin must be bytes")
        self.policy.check_command_gate(self.kind, timeout_s, live=not dry_run)
        self._ensure_open()
        started = time.monotonic()
        ephemeral_backend: RelayBackend | None = None
        try:
            backend = self._make_backend() if self._ephemeral else self._persistent_backend()
            if self._ephemeral:
                ephemeral_backend = backend
            if dry_run:
                planned = self._planned_argv(backend, checked)
                self.policy.check_command(self.kind, planned, timeout_s, live=False)
                return CommandResult(
                    0,
                    elapsed_s=max(0.0, time.monotonic() - started),
                    dry_run=True,
                    backend=self.kind,
                    argv_evidence=_evidence(planned),
                    planned_argv=planned,
                )
            planned = self._planned_argv(backend, checked)
            self.policy.check_command(self.kind, planned, timeout_s, live=True)
            raw = backend.run(checked, timeout_s=float(timeout_s), stdin=stdin)
            if not isinstance(raw, CommandResult):
                raise RelayError("relay backend returned an invalid result")
            if (
                isinstance(raw.returncode, bool)
                or not isinstance(raw.returncode, int)
                or isinstance(raw.elapsed_s, bool)
                or not isinstance(raw.elapsed_s, (int, float))
                or not math.isfinite(raw.elapsed_s)
                or raw.elapsed_s < 0
                or not isinstance(raw.stdout, str)
                or not isinstance(raw.stderr, str)
            ):
                raise RelayError("relay backend returned an invalid result")
            return CommandResult(
                raw.returncode,
                _bounded_text(raw.stdout, self.policy.max_output_bytes),
                _bounded_text(raw.stderr, self.policy.max_output_bytes),
                max(0.0, raw.elapsed_s or time.monotonic() - started),
                False,
                self.kind,
                _evidence(planned),
                (),
            )
        except RelayUnsupported:
            raise RelayUnsupported("relay operation is unsupported") from None
        except RelayPolicyError:
            raise RelayPolicyError("relay operation is not allowed") from None
        except RelayError:
            raise RelayError("relay operation failed") from None
        except Exception:
            raise RelayError("relay command failed") from None
        finally:
            if ephemeral_backend is not None:
                try:
                    ephemeral_backend.close()
                except Exception:
                    pass

    def open_tunnel(
        self,
        target: TunnelTarget,
        *,
        local_port: int = 0,
        timeout_s: float = 30.0,
        dry_run: bool = False,
    ) -> TunnelLease:
        _validate_target(target, loopback_only=False)
        _validate_port(local_port, allow_zero=True)
        self.policy.check_tunnel(self.kind, timeout_s, live=not dry_run)
        self._ensure_open()
        backend = self._make_backend() if self._ephemeral else self._persistent_backend()
        planner = getattr(backend, "plan_tunnel_argv", None)
        planned: tuple[str, ...] = ()
        try:
            if callable(planner):
                planned = _validate_argv(planner(target, local_port=local_port))
                if self.policy.command_allowlist is not None:
                    self.policy.check_command(
                        self.kind, planned, timeout_s, live=not dry_run
                    )
        except RelayUnsupported:
            if self._ephemeral:
                try:
                    backend.close()
                except Exception:
                    pass
            raise RelayUnsupported("relay tunnel plan is unsupported") from None
        except RelayPolicyError:
            if self._ephemeral:
                try:
                    backend.close()
                except Exception:
                    pass
            raise
        except Exception:
            if self._ephemeral:
                try:
                    backend.close()
                except Exception:
                    pass
            raise RelayError("relay tunnel planning failed") from None
        if dry_run:
            try:
                plan_tunnel = getattr(backend, "plan_tunnel", None)
                if callable(plan_tunnel):
                    planned_lease = plan_tunnel(target, local_port=local_port)
                    planned = planned or planned_lease.planned_argv
                    planned_lease.close()
                return TunnelLease(
                    "127.0.0.1",
                    local_port,
                    target.host,
                    target.port,
                    "dry-run",
                    self.kind,
                    lambda: None,
                    dry_run=True,
                    planned_argv=planned,
                )
            finally:
                if self._ephemeral:
                    try:
                        backend.close()
                    except Exception:
                        pass
        lease: TunnelLease | None = None
        try:
            lease = backend.open_tunnel(
                target,
                local_port=local_port,
                timeout_s=float(timeout_s),
            )
            if not isinstance(lease, TunnelLease):
                raise RelayError("relay backend returned an invalid tunnel")
            if self._ephemeral:
                original_close = lease.close_callable
                closed = False
                close_lock = threading.Lock()

                def close_ephemeral() -> None:
                    nonlocal closed
                    with close_lock:
                        if closed:
                            return
                        closed = True
                    try:
                        original_close()
                    finally:
                        backend.close()

                lease.close_callable = close_ephemeral
            with self._lock:
                self._lease_objects[id(lease)] = lease
            return lease
        except RelayUnsupported:
            if self._ephemeral:
                try:
                    backend.close()
                except Exception:
                    pass
            raise RelayUnsupported("relay tunnel is unsupported") from None
        except RelayPolicyError:
            if self._ephemeral:
                try:
                    backend.close()
                except Exception:
                    pass
            raise RelayPolicyError("relay tunnel is not allowed") from None
        except RelayError:
            if self._ephemeral:
                try:
                    backend.close()
                except Exception:
                    pass
            raise RelayError("could not open relay tunnel") from None
        except Exception:
            if lease is not None:
                try:
                    lease.close()
                except Exception:
                    pass
            if self._ephemeral:
                try:
                    backend.close()
                except Exception:
                    pass
            raise RelayError("could not open relay tunnel") from None

    def close(self) -> None:
        """Close this relay's leases/backend, but never close its parent."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            leases = tuple(self._lease_objects.values())
            self._lease_objects.clear()
            backend = self._backend
        for lease in leases:
            try:
                lease.close()
            except RelayError:
                pass
        if backend is not None:
            try:
                backend.close()
            except Exception:
                raise RelayError("failed to close relay backend") from None

    def __enter__(self) -> Relay:
        self._ensure_open()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def _local_factory(*, options: Mapping[str, Any], parent: Relay | None, policy: RelayPolicy) -> RelayBackend:
    del parent
    if set(options) - {"runner"}:
        raise RelaySpecError("unknown local backend option")
    return LocalBackend(runner=options.get("runner"), policy=policy)


def _command_factory(*, options: Mapping[str, Any], parent: Relay | None, policy: RelayPolicy) -> RelayBackend:
    if set(options) - {"command_prefix", "tunnel_command"}:
        raise RelaySpecError("unknown command backend option")
    return CommandBackend(
        command_prefix=options.get("command_prefix", ()),
        tunnel_command=options.get("tunnel_command"),
        parent=parent,
        policy=policy,
    )


def _ssh_factory(*, options: Mapping[str, Any], parent: Relay | None, policy: RelayPolicy) -> RelayBackend:
    if set(options) - {"host", "user", "port", "identity_file"}:
        raise RelaySpecError("unknown SSH backend option")
    return SshBackend(parent=parent, policy=policy, **options)


def _ssm_factory(*, options: Mapping[str, Any], parent: Relay | None, policy: RelayPolicy) -> RelayBackend:
    if set(options) - {"instance_id", "region", "command_id"}:
        raise RelaySpecError("unknown SSM backend option")
    return SsmBackend(parent=parent, policy=policy, **options)


register_relay("local", _local_factory)
register_relay("command", _command_factory)
register_relay("ssh", _ssh_factory)
register_relay("ssm", _ssm_factory)
