# Relay chains

See [the relay library evaluation](research/relay_libraries.md) for the
build-versus-integrate decision: keep this orchestration layer, but delegate
live SSH to OpenSSH, SSM to the AWS CLI/Session Manager plugin, and Kubernetes
to `kubectl`. AsyncSSH/sshtunnel/pyssm-client/gost are optional backends, not
replacements for the policy layer.

`ozagia.core.relay` defines policy-gated command and TCP relay interfaces. It
uses only the standard library and makes no provider calls when a relay is
constructed. Specs are ordered from the inside out:

```python
Relay.from_specs(
    [
        RelaySpec("local"),
        RelaySpec("ssm", {"instance_id": "i-0123456789abcdef0",
                          "region": "us-east-1"}),
        RelaySpec("ssh", {"host": "bastion.example"}),
    ],
    policy=policy,
)
```

This constructs `local -> SSM -> SSH`; the returned relay is the outermost
node. Each factory receives its options, parent relay, and shared policy.
Actual operations require every backend in a route to support that operation;
the built-in SSM planner does not execute arbitrary wrapped commands. A custom
registry can add backends with `RelayRegistry.register` or `register_relay`.
Backend wrappers should compose argv arrays and should not put input values in
exceptions or logs.

## Policy and lifecycle

The default `RelayPolicy` denies every backend, command, and tunnel. A live
command requires an explicitly allowed kind, `allow_live=True`, a positive
`max_timeout_s`, and (when configured) a matching argv-prefix allowlist. A live
tunnel additionally requires `allow_network=True`. Checks happen before a
backend operation; argv is bounded by count and length, and result output is
bounded by policy. `CommandResult` hides output and argv from its repr and
includes only a hash/count as argv evidence.

Use `dry_run=True` to get a bounded argv plan without starting a process or
binding a socket. Dry-run still requires the kind and timeout to be allowed.
For live commands and network access, require a separate approval step and
review narrow command prefixes. Do not place credentials or tokens in argv.

Persistent chains lazily reuse each backend. With `ephemeral=True`, a new
backend factory instance is used per operation and closed afterward; tunnel
instances are closed with their lease. `Relay.close()` is idempotent and
closes only that relay's resources, never its parent. Close a composed chain
outer-to-inner when explicitly cleaning up every node. `TunnelLease` is also
an idempotent context manager.

## Local TCP tunnels

`LocalBackend` can run an injected argv runner; its default uses
`subprocess` only after the relay policy allows the operation. The local TCP
forwarder (`LocalTcpTunnel`) binds only to `127.0.0.1`, limits active
connections, and only forwards to a loopback target. Closing its lease stops
the listener and active connections. This is intended for local services and
offline tests, not as a general network pivot.

## OpenSSH and SSM live backends

`SshBackend` requires an explicit host and can include an explicit user, port,
and identity file. It invokes the system `ssh` executable with argv arrays,
preserves normal SSH config/agent/known-hosts behavior, and never disables
host-key checking. An explicitly configured, validated `proxy_jump` is passed
as `-J`; `proxy_command` is never synthesized and is used only when explicitly
configured. Command execution goes through the parent relay. Local SSH
forwarding requires a local parent, starts `ssh -N -L` and waits for its
loopback listener; forwarding through a remote parent is unsupported because
that endpoint would not be local to this process.

`SsmBackend` requires an explicit instance ID and region. Commands use the
reviewed `AWS-RunShellScript` document via `aws ssm send-command`, then poll
`get-command-invocation` through the parent relay. An explicit command-prefix
allowlist and `allow_live=True` are mandatory for this remote execution. Its
read-only status plan remains available when a command ID is configured.
Tunnels use `AWS-StartPortForwardingSession`, or
`AWS-StartPortForwardingSessionToRemoteHost` when `remote_host` is explicitly
configured. They require the AWS CLI, the official Session Manager plugin, and
a local parent relay. The plugin is checked before starting a session.

Live commands and tunnels remain default-deny: enable the relevant relay kind,
set a bounded positive timeout and explicitly approve `allow_live`; tunnels
also require `allow_network`. Review command prefixes against the final argv,
including the AWS CLI invocation for SSM. Dry-run plans perform no process
launch, port reservation or provider call. AWS credentials/region are not
discovered by ozagia; provide the region explicitly and configure credentials
through the user's ordinary AWS CLI environment. OpenSSH's normal host-key
validation remains enabled.

The core process boundary is argv-only, starts a process group with stdin
disconnected, bounds captured output, and closes tunnel processes idempotently.
Tests inject fake runners/process handles and use local loopback listeners only;
they never contact AWS, SSH hosts, Kubernetes, or another remote system.
`CommandBackend` remains plan-only.

AsyncSSH, sshtunnel, pyssm-client and gost remain optional/future alternatives,
not dependencies or default backends; see the
[library evaluation](research/relay_libraries.md).
