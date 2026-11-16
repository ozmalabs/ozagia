# Runtime services, processes and tunnel configuration plan

Status: proposal. Extends the relay/tunnel foundation without treating a
configured tunnel as a live connection.

## Outcome

Give an agent a small, inspectable runtime layer for:

- explicitly configured services and arbitrary foreground/background processes;
- starting, stopping, restarting, listing, tailing and checking them;
- named tunnel configurations that may be available but not connected;
- context hints that distinguish **configured**, **starting**, **running**,
  **exited**, **stale** and **failed** states.

Service and process definitions are configuration. Runtime state is separate,
timestamped and reconciles with the actual OS/relay lease. A tunnel
configuration is not evidence that the tunnel is connected.

## Current baseline

- `core/relay.py` has policy-gated local process execution, local loopback TCP
  tunnels, OpenSSH/SSM live backends, process/port leases and dry-run plans.
- `core/services`/`[services]` in config are IaC source mappings, not runtime
  service processes.
- `jobs.py` waits for named external jobs; it does not own a process.
- `.ozagia/` already holds snapshots, journals, sessions, edits and live
  snapshots.
- The proactive context budget has code and infrastructure facts; it has no
  runtime/service/tunnel facet.

## Configuration

Add `[relays.<name>]`, `[runtime.services.<name>]` and
`[runtime.tunnels.<name>]` tables. All are optional and declarative. No
credentials, environment values or raw files belong in them.

```toml
[relays.prod]
specs = [
  { kind = "local" },
  { kind = "ssm", options = { instance_id = "i-...", region = "us-east-1" } },
  { kind = "ssh", options = { host = "internal.example" } },
]
allowed_kinds = ["local", "ssm", "ssh"]
max_timeout_s = 600
command_allowlist = [["uptime"], ["kubectl", "get"]]

[runtime.services.api]
command = ["python", "-m", "app"]
workdir = "services/api"
relay = "prod"                 # omit for local execution
mode = "background"            # or "foreground"
description = "API service"

[runtime.services.migrate]
command = ["python", "manage.py", "migrate"]
workdir = "services/api"
mode = "foreground"

[runtime.tunnels.payments-db]
relay = "prod"
target = "db.internal.example:5432"
local_port = 0                 # 0 means allocate a loopback port
description = "Payments database through the production relay"
```

Validation: paths remain inside the repository; argv is non-empty, bounded and
shell-free; relay names must exist; `target` is a host:port; local ports are
bounded; tunnel definitions are unique by name. `mode` is foreground or
background. Unknown keys and values are errors. The policy remains separate
from the service/tunnel definition and cannot be weakened by context.

## Runtime model

Create `core/runtime.py` (name can change) with two managers sharing a small
state store:

- `RuntimeStore`: `.ozagia/runtime/state.json` (atomic, versioned, bounded) and
  `.ozagia/runtime/logs/<id>.log` (0600, bounded ring). No raw environment or
  prompt text. State file contains names, modes, configured commands? Prefer
  only a digest/name, never full argv; reconstruct from config on read.
- `ServiceManager`/`ProcessManager`:
  - `start(name)` for a configured background service;
  - `run(name)` for a configured foreground process and wait;
  - `start_raw(argv, ...)` only when a caller explicitly supplies a reviewed
    spec; default no implicit arbitrary command;
  - `stop/restart/status/list/logs` (bounded tail);
  - process states; exit code/signal; started/ended timestamps; PID and start
    token for safe PID-reuse checks; restart count if implemented.
  - Background processes run in their own session/process group; output is
    captured into a bounded log. Foreground runs return output and state.
  - If the manager process exits, a later invocation reconciles by checking the
    recorded PID/start token. Never signal an unverified or reused PID; mark
    `stale`/`unknown` and require explicit cleanup instead.
  - Remote relay background services are not claimed: local background
    processes are supported first. Foreground commands may use a relay.
  - Stop is graceful with a short deadline, then group termination; never
    target a PID outside the recorded process group.
- `TunnelManager`:
  - `start(name)`: build the configured relay chain, open the tunnel through
    the existing `Relay`, persist a runtime lease/state and keep it alive.
  - `stop/status/list/logs?`: stop the lease/process and reconcile state.
  - States: `configured`, `starting`, `running`, `stopped`, `failed`,
    `stale`, `unknown`. `configured` is derived only from config.
  - The configuration is available to context even when the runtime state is
    absent. Never render "connected" from config alone.
  - Tunnel state records route shape, target, local endpoint, mode, start time
    and a non-secret process/session token; no credentials or full argv.
- A combined `runtime_status()` returns configured and active items without
  claiming that an unavailable or stale tunnel is connected.

## Context

Add a bounded runtime-context provider, used by `build_context` after freshness
checks:

- Match service/tunnel names, descriptions, workdirs and environment names from
  the current request/history. A generic word must not cause every config item
  to be injected.
- Render compact lines with explicit state, e.g.:

  ```text
  [ozagia] tunnel payments-db: configured, not connected (target db.internal.example:5432)
  [ozagia] service api: running (pid 1234, started 12:04Z, background)
  [ozagia] service migrate: exited 0 (12:01Z)
  ```

- Never use "connected" unless a live lease is verified. Include a reason when
  a configured tunnel is unavailable or stale.
- Budget: a small reserve within the existing code/infra context budget; do not
  read logs or commands into the hint. Do not include process environment,
  full argv or credentials.
- Cache the config/state view by config mtime and runtime state version.

## CLI and MCP

CLI:

```text
ozagia runtime status|list
ozagia service start|run|stop|restart|status|logs NAME
ozagia process start|run|stop|restart|status|logs NAME
ozagia tunnel start|stop|status|list [NAME]
```

- `start` defaults to background; `run` is foreground and waits.
- `logs` is bounded, local, and never included in automatic context.
- Start/stop require explicit confirmation for live remote relays? Local
  service management is user-invoked CLI, but the relay policy and
  `--allow-live`/`--allow-network` remain mandatory for remote/network.
- `tunnel status` must distinguish configured from running/stale.

MCP: expose read-only `dev_runtime_status`, `dev_runtime_list`,
`dev_runtime_logs` (bounded, caller-requested) and `dev_tunnel_config`
(config + runtime state). Do not expose start/stop/restart over MCP in this
phase; no implicit process or tunnel control.

## Safety

- Default deny: no service runs merely because it is configured.
- No shell strings; argv only. No credential/environment discovery. Do not
  show or persist environment values.
- Runtime state/logs are local state under `.ozagia/`; logs are bounded and
  permission-restricted, never injected into automatic context.
- PID reuse verification and process-group ownership are mandatory before stop.
- A tunnel configuration does not imply connectivity. A runtime record does not
  imply a live process after a manager restart.
- Tests use fake process/relay/lease backends and local loopback sockets only;
  no live SSH/SSM/cloud process or tunnel is started.
- Live remote service management remains separate: foreground SSH/SSM commands
  may use the relay; background remote ownership needs a later reviewed
  mechanism (tmux/systemd/remote supervisor) and is not claimed here.

## Work packages

1. Config parsing and validation for relays/runtime services/tunnels.
2. Runtime store, process manager, reconciliation and tests.
3. Tunnel manager and configured-vs-running state tests.
4. Runtime context provider and proactive-context integration.
5. CLI read/write surface and bounded logs.
6. Read-only MCP tools and docs.
7. Full-suite, ruff, context-budget and canary tests.

## Acceptance criteria

- A configured service/tunnel appears in context as configured without implying
  it is running.
- Background start/status/logs/stop/restart work in tests with fake processes;
  foreground run returns bounded output and terminal state.
- A manager restart marks old runtime records stale/unknown and never kills an
  unverified PID.
- A tunnel config can be queried while disconnected; a running lease reports
  running; a stale record reports stale and never connected.
- No test starts a real SSH, SSM, AWS, Kubernetes, service or remote process.
- Full suite and ruff pass; docs explain the config/state distinction and live
  gates.
