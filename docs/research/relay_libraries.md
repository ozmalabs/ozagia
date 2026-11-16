# Relay and tunnel library evaluation

Date: 2026-11-16

Question: should ozagia keep its own relay/tunnel abstraction, or integrate an
existing open-source library for command relays and network tunnels through
nested SSH/SSM/bastion hops?

Short answer: **keep the small `Relay` policy/registry layer, but do not
implement SSH or the SSM data channel ourselves.** Use OpenSSH and the AWS
Session Manager plugin (or a reviewed library alternative) as backends. No
single library covers command execution, network tunnels, SSM, Kubernetes,
approval policy and arbitrary nested backends together.

## What was evaluated

Repository metadata was read on 2026-11-16. Stars and last-push dates are
context, not quality claims.

| Project | License | Activity | What it provides | Fit |
| --- | --- | --- | --- | --- |
| [OpenSSH](https://www.openssh.com/) | BSD-style | system package | SSH commands, `-L`/`-R` forwarding, `ProxyJump`, `ProxyCommand`, agent/known-hosts/config | Strong default for SSH commands and tunnels; no Python dependency |
| [AsyncSSH](https://github.com/ronf/asyncssh) | EPL-2.0 | 1.7k stars, active Nov 2026 | Async SSH client/server, local/remote forwarding, TUN/TAP, multiple ProxyJump hosts (since 2.15) | Best in-process Python SSH option, but EPL-2.0 and async; optional backend only |
| [sshtunnel](https://github.com/pahaz/sshtunnel) | MIT | 1.3k stars, active Nov 2026 | Paramiko-based local/remote tunnels, SSH config/proxy, nested-forward examples | Good small tunnel library; no command abstraction or SSM; optional pure-Python fallback |
| [Paramiko](https://github.com/paramiko/paramiko) | LGPL-2.1 | 9.9k stars, active | SSHv2 transport, channels, SFTP, forwarding primitives | Lower-level; sshtunnel/AsyncSSH are better fits for this feature |
| [pyssm-client](https://github.com/dacort/pyssm-client) | Apache-2.0 | 8 stars, v0.2.0 beta, last push Mar 2026 | Python SSM Session Manager client: exec, port-forward, copy, protocol data channel | Relevant and permissively licensed, but young/low adoption; KMS handshake and other limitations. Experimental only |
| [aws-gate](https://github.com/xen0l/aws-gate) | BSD | 518 stars, active Apr 2026 | SSM session/SSH CLI, ProxyCommand generation, local port forwarding | Useful optional SSH-over-SSM CLI or reference; not a library and duplicates our command policy |
| [AWS session-manager-plugin](https://github.com/aws/session-manager-plugin) | Apache-2.0 | official AWS, 327 stars, active Oct 2026 | Canonical SSM data-channel/port-forward plugin used by AWS CLI | Use via `aws ssm start-session`; do not reimplement the data channel |
| [go-gost](https://github.com/go-gost/gost) | MIT | 7.9k stars, active | Multi-level forwarding chains, many transports/listeners/connectors | Strong generic network tunnel engine, but Go and no command/SSM approval semantics; optional sidecar only |
| [chisel](https://github.com/jpillora/chisel) | MIT | 16.8k stars, active | TCP/UDP tunnel over HTTP, client/server | Not a fit: requires its own server, no command execution or SSM |
| [pyinfra](https://github.com/pyinfra-dev/pyinfra) | MIT | 6.1k stars, active | Connector abstraction for command execution over SSH/local/Docker | Command-only; no network tunnel or SSM; not worth the dependency |
| [sshuttle](https://github.com/sshuttle/sshuttle) | LGPL-2.1 | 13.7k stars, active | Transparent VPN-like proxy over SSH | Requires routing/TUN; not a nested command/tunnel abstraction |
| [Teleport](https://github.com/gravitational/teleport) | AGPL-3.0 | 21.1k stars, active | Full access platform (SSH/Kubernetes/db/RDP, audit) | Too large and AGPL; not embeddable for this feature |
| [HashiCorp Boundary](https://github.com/hashicorp/boundary) | other/archived | 4k stars | Session brokering platform | Archived; not a library |

## Recommended composition

1. **Keep `ozagia.core.relay` as the policy/registry/orchestration layer.**
   It supplies the pieces no library provides together: explicit default-deny
   policy, backend allowlists, nested chain construction, ephemeral backend
   instances, bounded results, approval and audit integration.
2. **Use OpenSSH for SSH commands and tunnels.** A thin backend should invoke
   `ssh` with argv arrays, preserving the user's config, agent, known_hosts,
   `ProxyJump` and `ProxyCommand`. It can run locally or through a parent relay
   (for example a command on an SSM bastion). This avoids a Python SSH
   implementation and is the best default for both command and `-L` forwarding.
3. **Use the AWS CLI plus the official Session Manager plugin for SSM.** For
   commands, use reviewed `send-command`/status shapes (the existing job
   module already polls invocations); for network access, use
   `aws ssm start-session` with `AWS-StartPortForwardingSession(ToRemoteHost)`.
   The plugin is the AWS-maintained data channel; reimplementing it is not
   justified.
4. **Use `kubectl` for Kubernetes exec/port-forward.** The CLI is the supported
   surface and is already the ecosystem's canonical implementation; a
   client-go sidecar is not needed for this feature.
5. **Offer AsyncSSH as an optional SSH backend, not a requirement.** It is the
   best pure-Python option if ozagia must embed SSH in a long-lived async
   process or avoid spawning `ssh`. Its EPL-2.0 license and async event-loop
   integration need an explicit project decision.
6. **Treat `sshtunnel` as an optional simple fallback.** It is MIT and active,
   and its nested-forward pattern is close to the relay chain model. It still
   needs Paramiko and does not help with SSM/commands, so it is not a replacement
   for the relay layer.
7. **Do not make `pyssm-client` the default yet.** It is the most interesting
   pure-Python SSM option and supports port-forward/exec, but it is a young
   beta with a small user base and documented KMS limitations. Evaluate it
   behind an `ssm-python` extra with a separate security review.
8. **Use `go-gost` only if broad network protocols are a product requirement.**
   It is a mature chain engine, but adding a Go binary for the current
   SSH/SSM/bastion use cases would be heavier than integrating OpenSSH and the
   AWS plugin. Chisel, frp and similar tools are not substitutes.

## Consequences for the current relay implementation

- The existing abstraction, policy, registry, ephemeral lifecycle, local
  loopback tunnel and dry-run planners are useful and tested; keep them.
- The current `SshBackend`/`SsmBackend` are planners only. They should be
  replaced by thin live backends that delegate to OpenSSH/AWS CLI, rather than
  growing an SSH or SSM protocol implementation in Python.
- `LocalTcpTunnel` is a small, auditable loopback forwarder; keep it for local
  services and tests. Do not generalize it into a public proxy or VPN.
- The next live backend should have: argv-only construction, explicit
  executable allowlist, host-key/known-hosts policy, bounded timeouts/output,
  named environment/region/namespace, a dry-run mode, and an audit record.
- No live SSH/SSM/network trial should run from this evaluation. Live tests
  need the separate approval gates in `AGENTS.md` and the project plan.

## Dependencies worth adding, in order

1. None for OpenSSH/AWS CLI/kubectl: they are optional external executables,
   already represented as command backends. Detect their absence and report it.
2. `asyncssh` (optional extra) only if the daemon needs in-process SSH sessions
   and EPL-2.0 is acceptable.
3. `sshtunnel` (optional extra) only if a simpler Paramiko tunnel backend is
   needed with no command support.
4. `pyssm-client` (experimental extra) only after an independent review of its
   protocol implementation and KMS limitation.

The relay plan should therefore be: keep the orchestration abstraction, delegate
SSH to OpenSSH, SSM to the AWS CLI/plugin, Kubernetes to kubectl, and reserve
AsyncSSH/sshtunnel/pyssm-client/gost for explicitly selected optional backends.
