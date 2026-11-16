# Runtime services and tunnels

Runtime configuration is optional and declarative. It does not start anything
when ozagia loads a repository. IaC source mappings in `[services]` are separate
from executable processes in `[runtime.services]`.

```toml
[relays.prod]
specs = [
  { kind = "local" },
  { kind = "ssh", options = { host = "internal.example" } },
]
allowed_kinds = ["local", "ssh"]
max_timeout_s = 300
command_allowlist = [["uptime"], ["kubectl", "get"]]
max_output_bytes = 4096

[runtime.services.api]
command = ["python", "-m", "app"]
workdir = "services/api"       # repository-relative; may be omitted
mode = "background"             # background or foreground
description = "API service"

[runtime.services.migrate]
command = ["python", "manage.py", "migrate"]
workdir = "services/api"
mode = "foreground"

[runtime.tunnels.database]
relay = "prod"
target = "db.internal.example:5432"
local_port = 0                  # 0 requests an allocated loopback port
description = "Payments database"
```

Relay specs and commands are argv data, never shell strings. Config accepts
bounded structural values only; unknown keys, secret-like option names,
credential-like command arguments, environment assignments, and repository
escaping workdirs are rejected. Config models contain no environment values or
credentials. Treat the config file itself as reviewed executable policy.

## Configuration and runtime state

Configuration lives in `.ozagia.toml` (or the configured project file).
Observed state is separate, normally under `.ozagia/runtime/`:

- `state.json` is versioned, atomically replaced, bounded, and mode `0600`;
- `logs/<id>.log` is a bounded tail with mode `0600`;
- records contain names, state, timestamps, fingerprints, and process/tunnel
  metadata, not full argv, environment, prompts, or output.

Both the runtime directory and logs are local state. Add `.ozagia/` to the
repository's ignore rules. Logs may still contain sensitive application output:
read them only on explicit request and protect the local state directory.
Automatic runtime context never reads logs or includes commands.

## Services and processes

`ServiceManager.start(name)` starts a configured background service.
`ServiceManager.run(name)` runs a configured service in the foreground and
returns bounded output plus its terminal state. `stop`, `restart`, `status`,
`list`, and `logs` operate on configured names. `ProcessManager` is an alias for
the same manager. `start_raw(argv, ...)` and `run_raw(argv, ...)` are explicit
APIs for caller-reviewed argv; raw commands are not added to configuration or
persisted as argv.

Background processes use `shell=False`, their own session/process group, and a
bounded log. Local execution is supported. Background services with a relay and
foreground relay execution are explicitly unsupported in this phase.

The manager stores a PID, expected process group, and process start token where
the operating system provides one. Before stopping, it verifies that the PID
still has the recorded start token and is still the owned process-group leader.
After a manager restart, an unverifiable process is `stale`/`unknown`; ozagia
will not signal it. It is safer to require manual investigation than to risk
signalling a reused PID. If process identity cannot be established at startup,
the process is not considered safely manageable.

States include `configured`, `starting`, `running`, `exited`, `failed`,
`stopped`, `stale`, and `unknown`. `configured` describes a definition only;
it is not a claim that a process exists.

## Tunnels and live gates

`TunnelManager.status(name)` and `.list()` show configured tunnels even when no
lease exists. `start(name)` requires both `allow_live=True` and
`allow_network=True`, and builds the chain with the configured relay policy.
This explicit API gate is in addition to the relay's allowed-kind,
timeout, command-allowlist, and output-size policy. Do not pass those flags
without a user's live-operation approval. `stop(name)` closes the lease held by
that manager.

An unheld persisted lease becomes `stale` after a manager restart and is never
rendered as a connected tunnel. A `configured` tunnel is rendered as
“configured, not connected”; only a lease held by the current manager is
reported as `running`. An unheld stale lease cannot be safely closed by a new
manager and requires manual investigation.

## Runtime context

`runtime_context(memory, query, history=(), max_chars=...)` returns a bounded
`context` string, matching structured `items`, and a `truncated` flag. Matching
uses distinctive exact terms in configured names, descriptions, and workdirs;
generic words such as “status”, “service”, or “tunnel” do not inject every
configured entry. Context excludes argv, logs, environment, credentials, and
raw output.

`build_context` reserves a small share of its character budget for these lines
and puts them before the code/infrastructure hint. A configured tunnel is
always rendered as “configured, not connected” until the observing process
holds its lease. `runtime_status` is the underlying read-only snapshot used by
CLI and MCP; it never creates the state directory, starts a process, or opens a
tunnel. A persisted tunnel lease is reported as stale because another process
cannot verify that the lease is still held.

## CLI

```
ozagia runtime status|list [NAME] [--json]
ozagia service list|status|logs NAME [--json]
ozagia service start|run|stop|restart NAME [--json]
ozagia process run|start [--workdir DIR] -- ARGV...
ozagia process list|status|stop|logs NAME [--json]
ozagia tunnel status|list [NAME] [--json]
ozagia tunnel start NAME --allow-live --allow-network [--once] [--json]
ozagia tunnel stop NAME [--json]
```

`service start` starts a configured background service and returns its state.
`service run` is foreground and waits, returning bounded output and an exit
status. `process start|run` needs explicit argv after `--`; there is no shell
interpolation and the optional workdir must stay inside the repository. Logs
are bounded and only printed when requested.

`tunnel start` holds the lease in the foreground until Ctrl-C, which stops it.
`--once` opens and closes the tunnel immediately for a reviewed check. Both
live flags are mandatory; without them the command refuses before building a
relay. A tunnel started by one CLI process cannot be stopped by a different
process because no process can prove it still holds that lease. Run the tunnel
in the process that should own it, or investigate a stale record manually.

## MCP

The SRE MCP surface exposes two read-only tools:

- `dev_runtime_status` returns configured services/tunnels, explicit raw
  process records, observed local states, relay names, and compact context
  lines. `name` selects one item.
- `dev_runtime_logs` returns one bounded, caller-requested local log tail.

MCP never starts, stops, or restarts a service or tunnel. Live relay execution
remains CLI-only and doubly gated. Runtime logs are not included in automatic
context or the repo brief.

## Testing and live operations

Runtime tests use fake process and relay implementations. They do not start
SSH, SSM, AWS, Kubernetes, remote services, or real tunnel bridges. Keep
integration testing behind explicit live/network approval and use local loopback
only for any future bridge-specific tests.
