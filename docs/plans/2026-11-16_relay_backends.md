# Relay backend integration plan

Status: implemented for the offline-tested Foundation phase. Live OpenSSH/AWS
calls were not run; `src/ozagia/core/relay.py` now has live code paths behind
default-deny policy and fakes exercise them. CLI, dry-run MCP and `doctor`
wiring landed with the implementation.

## Decision

Keep `ozagia.core.relay` as the policy, registry, chain and lifecycle layer.
Do not implement SSH or the AWS SSM data channel ourselves.

- **SSH:** use OpenSSH (`ssh`) through an argv-only subprocess backend. This
  preserves the user's `ssh_config`, agent, known_hosts, `ProxyJump` and
  `ProxyCommand` behavior.
- **SSM:** use the AWS CLI plus the official Session Manager plugin. Commands
  use reviewed `send-command`/`get-command-invocation` shapes; network access
  uses `AWS-StartPortForwardingSession`/`AWS-StartPortForwardingSessionToRemoteHost`.
- **Kubernetes:** use `kubectl`; do not add a client-go sidecar for this
  feature.
- **Optional in-process SSH:** AsyncSSH behind an optional extra, only if the
  daemon later needs long-lived async SSH sessions. Its EPL-2.0 license needs an
  explicit project decision.
- **Optional pure-Python SSM:** `pyssm-client` remains experimental because it
  is beta, small and has a documented KMS limitation.
- **Generic network chains:** `go-gost` is a possible optional sidecar for
  protocols beyond SSH/SSM/kubectl; it is not part of this implementation.
- **Rejected for now:** chisel/frp (server-oriented), sshuttle (VPN/root),
  pyinfra (command-only), Teleport (AGPL, access platform), Boundary (archived).

## Current foundation

`src/ozagia/core/relay.py` already provides:

- default-deny `RelayPolicy`, `RelaySpec`, arbitrary `RelayRegistry`, nested
  `Relay` chains, ephemeral backend factories and idempotent lifecycle;
- a real loopback TCP forwarder (`LocalTcpTunnel`) with bounded connections;
- dry-run SSH/SSM argv planners;
- command/tunnel result types with bounded output and redacted reprs.

The SSH/SSM planners do not execute anything. This plan makes the two primary
backends live without growing SSH or SSM protocol code.

## Work packages

### 1. Process and tunnel lifecycle primitives

Add a small internal process boundary used by local and remote backends:

- `ProcessLease`/`ProcessHandle`: argv, pid, process group, start/close/wait,
  bounded stop; no shell; no inherited stdin; output discarded or drained.
- `PortLease`/readiness helper: reserve or discover a loopback port, wait for
  a listener with a hard deadline, and close without leaking threads.
- `TunnelLease` remains the public handle. It carries the local endpoint and
  the process/listener close callable.
- Local backend uses the same helper for host commands and loopback tunnels;
  SSH and SSM use it to manage their forwarding process.
- Tests use fake process handles and a local loopback echo server. Never start
  a remote process.

### 2. OpenSSH live backend

Extend `SshBackend` from planner to executor:

- `run`: invoke `ssh` via the parent relay (normally `local`), with argv:
  `ssh [-p PORT] [-i IDENTITY] [host-key/options] -- host argv...`.
  Preserve the caller's SSH config; do not set `StrictHostKeyChecking=no` or
  disable known_hosts. A `proxy_jump`/`proxy_command` option may be passed as an
  explicit reviewed OpenSSH option rather than reimplementing jump logic.
- `open_tunnel`: start `ssh -N -L 127.0.0.1:PORT:target:port ...` through the
  parent; wait for the local listener; return a `TunnelLease` whose close
  terminates the process and listener.
- Only support a local endpoint when the parent is the local backend. A remote
  parent produces a remote endpoint; return `RelayUnsupported` until a
  composing tunnel backend is implemented.
- Add optional `host_key_checking`/`known_hosts` policy fields only if they
  are concrete file paths; never default to disabling validation.
- Tests: fake parent runner/process, argv exactness, local port readiness,
  cleanup, timeout, nonzero exit, no shell interpolation, no secret in repr.

### 3. AWS SSM live backend

Extend `SsmBackend`:

- `run`: build a reviewed `aws ssm send-command` argv with an explicit document
  (`AWS-RunShellScript` only by default), instance ID and region; wait using
  `get-command-invocation`; return bounded stdout/stderr and exit status.
  The command is a remote execution; require an explicit command allowlist,
  `allow_live`, and a positive timeout. `send-command` is a write/execute
  operation even though the subsequent poll is read-only.
- `open_tunnel`: start the official `aws ssm start-session` with
  `AWS-StartPortForwardingSession`/`...ToRemoteHost`, explicit local port and
  target; run via the local parent; wait for the local listener. The Session
  Manager plugin must be detected; absence is an explicit `RelayUnsupported`
  error with an installation hint.
- Output is bounded; `get-command-invocation` output is never logged by
  default. Terminal statuses map to success/failure/cancel/timeout.
- Tests: fake parent runner returning send/get JSON, terminal success/failure,
  timeout, malformed JSON, missing plugin, dry-run plan, canary absence.

### 4. Relay CLI/MCP surface

Add a small, explicit operational surface:

- `ozagia relay plan` — print a dry-run argv plan for a chain spec JSON file.
- `ozagia relay run` — execute one allowlisted command through a chain; default
  dry-run, live only with `--allow-live` and a policy file/config.
- `ozagia relay tunnel` — open a local tunnel; default dry-run, live only with
  `--allow-live --allow-network`.
- A chain spec is a bounded JSON document (kind/options/name) and may be
  supplied by file/stdin. Do not put credentials in specs. The shape is:

  ```json
  {
    "specs": [
      {"kind": "local"},
      {"kind": "ssm", "options": {"instance_id": "i-...", "region": "us-east-1"}},
      {"kind": "ssh", "options": {"host": "internal.example"}}
    ],
    "policy": {
      "allowed_kinds": ["local", "ssm", "ssh"],
      "max_timeout_s": 600,
      "command_allowlist": [["uptime"], ["kubectl", "get"]]
    }
  }
  ```

  The file can describe policy and chain shape, but `--allow-live` and
  `--allow-network` are still required on the command line. Specs are ordered
  from inside to outside (`local -> ssm -> ssh`).
- The full MCP coding surface may expose `dev_relay_plan` only (dry-run). Do
  not expose live remote command/tunnel execution through MCP until there is
  a separate human approval mechanism.
- The daemon/shell capture does not auto-execute relays; it may mention a
  planned relay only if the user explicitly requests it.

### 5. Doctor and documentation

- `doctor` reports availability and versions of `ssh`, `aws`,
  `session-manager-plugin`, and `kubectl`; absence is not a failure.
- Document exact human prerequisites, live approval, host-key policy, supported
  SSM documents, and unsupported combinations.
- Link the library evaluation and state that AsyncSSH/sshtunnel/pyssm/gost are
  optional future backends.

## Safety and gates

- Default policy denies every kind, command, tunnel and network operation.
- Live command execution additionally requires a command prefix allowlist and
  `allow_live`; live network requires `allow_network`.
- No shell strings; argv arrays only. No credential discovery, profile
  guessing, secret logging, or raw output in errors.
- Dry-run must not start a process or bind a socket.
- Live tests are not run by default. A live trial needs explicit human
  approval, a named environment/region/namespace and an audit record.
- Remote commands through SSH/SSM are not presented as source graph facts.

## Implementation order

1. Process/port lifecycle helper and tests.
2. OpenSSH live backend and tests with fakes.
3. SSM live backend and tests with fakes.
4. CLI `relay plan|run|tunnel`, policy/spec parsing and tests.
5. `doctor` availability and docs.
6. Optional AsyncSSH/sshtunnel/pyssm-client evaluation remains a separate
   decision; no new dependency in this work.

## Acceptance criteria

- Existing `tests/test_relay.py` and the full suite pass; new focused tests
  cover argv construction, lifecycle, cleanup, policy denial and dry-run.
- A local loopback tunnel still works and closes cleanly.
- Fake OpenSSH/SSM operations can complete a nested chain without a network
  or cloud call.
- `ozagia relay plan` emits the exact argv arrays that live mode would use.
- No live SSH/SSM/AWS/Kubernetes command is executed during tests or indexing.

## Implementation progress

- `ProcessLease`, loopback port reservation/readiness and tunnel process
  cleanup are implemented and tested with fakes.
- `SshBackend` executes `ssh` through the parent relay, supports explicit
  `proxy_jump`/`proxy_command`, and opens local `-L` tunnels only with a local
  parent. It never disables host-key checking.
- `SsmBackend` sends reviewed `AWS-RunShellScript` commands and polls
  `get-command-invocation`. It opens `AWS-StartPortForwardingSession` tunnels
  when the official plugin is present. It still requires explicit policy and
  a command allowlist.
- `ozagia relay plan|run|tunnel`, JSON chain specs, `dev_relay_plan` (dry-run
  only) and `doctor` binary detection are implemented.
- Focused tests use fake runners/processes and loopback sockets only. A live
  SSH/SSM/session-manager trial remains unrun and needs explicit human
  approval, a named environment and a matched evaluation.
