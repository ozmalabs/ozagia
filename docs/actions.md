# Process and connection actions

`ozagia.core.actions` is a bounded, preview-first command layer over the
existing runtime managers. It does not add another process or tunnel model:

- services and processes call `ServiceManager` / `ProcessManager`;
- tunnels call `TunnelManager` and its relay policy;
- the daemon calls `DaemonClient` and `ozagia.daemon.autostart.ensure`;
- integration status reads the existing OpenCode, Claude Code, Codex and Kiro
  path helpers.

Every mutating action is described first and performed only with a one-time
confirmation token. A passive status refresh never starts, stops or signals
anything.

## Actions

| Action | Kind | Effect |
| --- | --- | --- |
| `daemon.status` | read-only | Ping the graph daemon; a missing daemon is `absent`. |
| `daemon.ensure` | mutating | Start a detached daemon only when none answers. |
| `daemon.stop` | mutating, destructive | Send `shutdown` to a running daemon. |
| `daemon.restart` | mutating, destructive | Shut down a running daemon, then ensure one answers; an absent daemon is started. |
| `service.list` | read-only | List configured services and observed state. |
| `service.status` | read-only | Show one service and verify its PID/start token/group. |
| `service.start` | mutating | Start a background service through `ServiceManager.start`. |
| `service.run` | mutating | Run a foreground service and return bounded output. |
| `service.stop` | mutating, destructive | Stop a verified process group through `ServiceManager.stop`. |
| `service.restart` | mutating, destructive | Stop if running, then start through `ServiceManager.restart`. |
| `service.logs` | read-only | Read a bounded local runtime log. |
| `tunnel.list` | read-only | List configured tunnels; a configured tunnel is not connected. |
| `tunnel.status` | read-only | Show one tunnel; an unheld persisted lease is stale. |
| `tunnel.start` | mutating, live-gated | Preview the exact relay command; apply opens the live tunnel. |
| `integration.status` | read-only | Show install paths/state for OpenCode, Claude, Codex and Kiro. |

Integration install and uninstall are deliberately not actions in this slice.
They remain the existing installer CLIs and must gain their own preview/apply
flow before a UI exposes them.

## Preview and apply

Preview is the default:

```sh
python -m ozagia.actions_cli preview service.start --name api
python -m ozagia.actions_cli preview tunnel.start --name database
python -m ozagia.actions_cli preview daemon.ensure
python -m ozagia.actions_cli preview integration.status --name codex
```

A preview returns:

- the exact operation and expected effect;
- the manager call and, where available, the exact argv;
- preconditions and risks;
- the current observed state;
- a one-time confirmation token.

Preview reads runtime state without reconciling it and never constructs a
service manager that could mutate state. For `tunnel.start`, the command plan
uses the relay's dry-run path; it does not bind a socket or open a network
connection.

Apply uses the token:

```sh
python -m ozagia.actions_cli apply service.start --name api --confirm TOKEN
python -m ozagia.actions_cli apply tunnel.start --name database \
  --confirm TOKEN --allow-live --allow-network
```

`apply` verifies that the token matches the action, arguments and repository,
checks that it has not expired or been used, re-checks the action's
preconditions, consumes the token, then calls the existing manager. A consumed
token cannot be replayed. A changed argument set needs a new preview.

Preconditions and refusals:

- a running or starting service is a no-op for `start`/`run`;
- a stopped service is a no-op for `stop`;
- `stale` or `unknown` process identities are refused, never signalled;
- a missing daemon or service is reported as `absent`, not as a crash;
- an unknown daemon state is not treated as absent for a mutating action;
- `tunnel.start` requires `--allow-live` and `--allow-network` in addition to
  the confirmation token;
- an unheld tunnel lease is stale and cannot be claimed or stopped by a new
  manager.

## Live gates

The relay policy remains the enforcement point. `tunnel.start` passes
`allow_live=True` and `allow_network=True` only after both CLI flags are
present. The configured relay's allowed kinds, timeout, command allowlist and
output limit still apply. The preview shows the planned argv produced by the
relay backend; if a backend cannot plan a command, the preview says so instead
of inventing one.

No preview or test starts a real service, tunnel or daemon. Tests use fake
managers, fake daemon clients and fake relays.

## Audit

Every successful token consumption appends one compact record to
`<state dir>/actions/audit.jsonl` (`0600`). The file is bounded to the most
recent 200 records and 256 KiB.

A record contains:

- timestamp, action, outcome state and success flag;
- the target label (service, tunnel or integration name);
- whether live/network gates were requested;
- a short hash of the consumed token;
- a bounded, redacted error message for a refusal or failure.

It never contains argv, environment values, credentials, process output or log
contents. Pending tokens live under `<state dir>/actions/pending/` (`0600`) and
are pruned after ten minutes or when the pending set grows too large. Neither
path is written outside the configured state directory.

## CLI exit codes

| Code | Meaning |
| --- | --- |
| `0` | Preview succeeded, or apply succeeded (including a documented no-op). |
| `2` | Validation or I/O error: unsupported action/argument, bad root, unreadable config/state. |
| `1` | The action was refused or failed: bad/expired/used token, failed precondition, manager error. |

`list` prints the supported actions and marks each as mutating, destructive and
live-gated. `--json` prints the same structured result for a UI or test.

## What remains

- UI buttons and dialogs that call preview, show the risks/effect, and pass the
  token to apply.
- A read-only MCP `dev_action_preview` (optional; not added in this slice so no
  MCP client receives a token or mutation surface yet).
- Mutating MCP actions are intentionally absent: MCP remains observation and
  review-first until the host can show a confirmation UI.
- Integration install/uninstall previews and trust steps.
- Provider/MCP enable/disable, resource-limit changes, file cleanup and index
  rebuild actions from the wider plan.
- Daemon idle-timeout adjustment as a separate action.
