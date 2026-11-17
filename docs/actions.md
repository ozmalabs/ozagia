# Process and connection actions

`ozagia.core.actions` is a bounded, preview-first command layer over the
existing runtime managers. It does not add another process or tunnel model:

- services and processes call `ServiceManager` / `ProcessManager`;
- tunnels call `TunnelManager` and its relay policy;
- the daemon calls `DaemonClient` and `ozagia.daemon.autostart.ensure`;
- integration status reads the existing OpenCode, Claude Code, Codex and Kiro
  path helpers;
- integration install, uninstall and verify call those installers' own
  `__main__.main` (or OpenCode's `check_loads`) in-process.

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
| `integration.install` | mutating | Preview the exact installer call and paths; apply installs through the existing installer. |
| `integration.uninstall` | mutating, destructive | Remove only ozagia's own entries through the existing installer. |
| `integration.verify` | read-only | Run the installer's local check when one exists; otherwise report `unavailable`. |

Integration actions use the landed installers, not a second installation
model. They never subprocess an installer CLI, never auto-install a missing
tool, never install a git hook and never start a service. The only hooks
written are the selected tool integration's own hook files, through its
installer.

## Preview and apply

Preview is the default:

```sh
python -m ozagia.actions_cli preview service.start --name api
python -m ozagia.actions_cli preview tunnel.start --name database
python -m ozagia.actions_cli preview daemon.ensure
python -m ozagia.actions_cli preview integration.status --name codex
python -m ozagia.actions_cli preview integration.install --name codex \
  --scope project --python /path/to/python
```

A preview returns:

- the exact operation and expected effect;
- the manager call and, where available, the exact argv;
- the scope and target paths for integration actions;
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
python -m ozagia.actions_cli apply integration.install --name codex \
  --scope project --python /path/to/python --confirm TOKEN
python -m ozagia.actions_cli apply integration.uninstall --name kiro \
  --scope user --confirm TOKEN
python -m ozagia.actions_cli apply integration.verify --name opencode \
  --confirm TOKEN
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
  manager;
- an installed integration is re-installed idempotently by
  `integration.install`; an absent integration is a no-op for
  `integration.uninstall`;
- an unsupported scope is refused before the installer is called.

## Integration install, uninstall and verify

The integration actions reuse each tool's installer. The preview names the
installer module, the exact in-process arguments and the equivalent CLI argv.

| Action name | Installer module | Scopes | Install targets (project scope) |
| --- | --- | --- | --- |
| `opencode` | `ozagia.integrations.opencode` | project | `.opencode/plugins/ozagia.js`, `.opencode/plugins/ozagia/tui.js` |
| `claude` | `ozagia.integrations.claude_code` | project | `.claude/settings.local.json` |
| `codex` | `ozagia.integrations.codex` | project, user | `.codex/hooks.json`, `.codex/config.toml`, `AGENTS.md` |
| `kiro` | `ozagia.integrations.kiro` | project, user | `.kiro/hooks/ozagia.json`, `.kiro/agents/ozagia.json`, `.kiro/settings/mcp.json` |

User scope uses the tool's own home: `$CODEX_HOME` (default `~/.codex`) for
Codex and `~/.kiro` for Kiro. OpenCode and Claude have no user-scope path, so
`--scope user` is refused for them. Uninstall targets the same set, except
Claude, whose uninstall cleans both `settings.local.json` and `settings.json`.

`--python` sets the interpreter written into Claude, Codex and Kiro hook/MCP
commands. It defaults to the interpreter running ozagia. Use an absolute path
or a name on `PATH`; it is written into the tool's configuration. The OpenCode
installer does not use a Python path and rejects the option.

### Install

```sh
python -m ozagia.actions_cli preview integration.install --name codex \
  --scope project --python /path/to/python
```

The preview shows:

- the installer module and call, e.g.
  `ozagia.integrations.codex.__main__.main([...])`;
- the equivalent argv, e.g.
  `python -m ozagia.integrations.codex install --workdir REPO --scope project --python PYTHON`;
- the scope and exact target files;
- the current integration state (`absent`, `partial` or `installed`);
- the trust step and the risks (user-scope writes outside the repository,
  hooks taking effect only in the next session).

Preview writes nothing to the target files. The only write is the pending
confirmation token under the state directory.

Apply re-checks the scope, installer availability, configured Python and the
current state, consumes the token, then calls the installer's `main(argv)` in
process. Success output is discarded; a failure message is bounded and redacted
before it appears in the result or audit. The installer's merge rules preserve
unrelated settings; a re-install replaces only ozagia's own entries, so
install is idempotent and also completes a partial install. No hook is run and
no service is started.

### Uninstall

```sh
python -m ozagia.actions_cli preview integration.uninstall --name kiro \
  --scope user
```

Uninstall removes only ozagia's hooks, MCP entry and generated section/profile.
Other keys, hooks, servers and file content are left alone. Kiro uninstall
targets the `ozagia` agent profile that install writes, not unrelated profiles.
A file that holds nothing else afterwards can be deleted by the installer. An
absent integration is a no-op. This action is marked destructive because it
edits the tool's live configuration, but it never removes a user's unrelated
settings.

### Verify

```sh
python -m ozagia.actions_cli preview integration.verify --name opencode
```

Verify is read-only and always reports its limit:

- OpenCode: runs `ozagia.integrations.opencode.install.check_loads` in process.
  It may run the local `opencode plugin list` and a Node self-test; it makes no
  model call and installs nothing. The result is `verified` when the check
  passes, `failed` with a bounded error when it does not.
- Claude, Codex and Kiro: their installers have no status/check command, so
  verify reports `unavailable` with the reason. Use `integration.status` for
  read-only path and marker inspection; it is not a load test.

A missing tool is reported by the check, never installed. `unavailable` is not
a failure of the action (exit status `0`); a failed check is (exit status `1`).

### Limits

- One action handles one integration. There is no cross-tool transaction or
  rollback beyond each installer's own merge behavior.
- Install writes the current package's files. There is no version pin,
  download, upgrade check or cache lookup.
- The token binds the integration name, scope and `--python`, so changing any
  of them needs a new preview.
- Target paths are what the installer reports now. Apply re-checks scope and
  state, while the installer performs the actual safe merge against the files
  at call time.
- Only OpenCode exposes a load check. Claude, Codex and Kiro report
  `unavailable`; `integration.status` is path/marker inspection, not a load
  test.
- The OpenCode check depends on the local `opencode` and Node installations
  and can take up to 60 seconds. A missing tool is reported, not installed.
- User scope writes outside the repository under the tool's home directory;
  there is no automatic backup of the tool's configuration outside the
  installer's behavior.

## Action readiness

`ozagia.core.action_readiness.readiness(root, selections=...)` describes the
declared `service.*` and `tunnel.*` operations from `ACTION_SPECS` and joins
read-only observations. It is not a second action layer and it does not replace
preview/apply:

- preview remains the only source of a confirmation token;
- apply remains the only path that performs an operation and it re-checks the
  preconditions;
- readiness never previews, applies, resolves a secret, constructs a mutating
  manager, writes runtime state, appends an audit record or creates a token.

A selection is an explicit list of bounded mappings:

```python
from ozagia.core.action_readiness import readiness

report = readiness(".", selections=[
    {"action": "service.start", "name": "api"},
    {"action": "tunnel.start", "name": "database"},
])
```

`selections=None` reports every operation for every configured service and
tunnel; this is a conservative review view, not a claim that every verb is
applicable. An explicit empty list reports `unknown` with no operations.

### States

| State | Meaning |
| --- | --- |
| `available` | Every observed prerequisite for that operation is satisfied. This is still not permission: preview and confirmation are required. |
| `blocked` | A definite configuration/runtime precondition prevents the operation: foreground/background mismatch, relay present, live gate not granted, stale/unverified process or lease, or an already-active/no-op state. |
| `unknown` | The observation is insufficient: unresolved secret reference, unreadable configuration/runtime state, or the target was not observed. |
| `absent` | The named service or tunnel is not configured. |

A target summary is derived from its lifecycle operations. A tunnel target is
`blocked` whenever `tunnel.start` is blocked, even if `tunnel.status` is
readable. A service target can be `available` when at least one lifecycle verb
fits its mode (for example `service.run` for a foreground service) while the
other verbs remain explicitly `blocked`.

`tunnel.start` is always `blocked` in readiness because live and network gates
are operations, not configuration:

- `--allow-live` and `--allow-network` must be passed explicitly at apply;
- the one-time confirmation token must come from a preview;
- the relay policy, lease state, target reachability and remote authorization
  are not tested by readiness.

### What is joined

For each service:

- configured/absent, mode (`background`/`foreground`) and relay name;
- the existing runtime process state (configured, running, starting, exited,
  failed, stopped, stale, unknown), PID and whether the existing observation
  verified it;
- environment key names, `secret://...` reference strings, plain key names and
  counts. Values are never read or resolved;
- local log presence, path and size from the runtime store's metadata. Log
  content is not read.

For each tunnel:

- configured/absent, relay name and the relay's configured/allowed kinds;
- the existing runtime lease state. A persisted lease is never treated as a
  held connection; `stale`/unknown leases block a new start.

The join uses `ozagia.config.load_project_config`,
`ozagia.core.runtime.runtime_status` and `RuntimeStore(create=False)`. It never
calls `federation`, `relay.open_tunnel`, `ServiceManager` methods or
`TunnelManager` methods.

### Secret boundary

Readiness reports only references: for example
`{"key": "API_TOKEN", "reference": "secret://env/API_TOKEN"}`. It never calls
`reveal()`, `SecretBroker.resolve*` or provider checks. A service whose
environment contains an unresolved reference cannot be `available` for a
mutating operation; it is `unknown` (or `blocked` for a
`secret://session/...` reference, which a runtime service cannot resolve at
spawn). A missing provider, denied reference or invalid credential is therefore
reported as not assessed, never as ready.

`readiness` writes nothing, including no token, audit or runtime record. The
report's `provenance` records `secrets_resolved: false`, `previewed: false`,
`applied: false` and `wrote: false`.

### Task preflight

`ozagia.core.task_preflight.build_preflight(..., action_readiness=[...])` adds
an optional `actions` dimension and top-level `actions` section. It is absent
(and the six-dimension report is unchanged) when `action_readiness` is omitted.
When selected, an `available` operation is the only state that can make the
dimension `true`; `blocked`/`unknown` map to `unknown`, and `absent` maps to
`false`, so none can make the assessment green. See
[task_preflight.md](task_preflight.md#action-readiness-join).

```python
from ozagia.core.task_preflight import build_preflight

report = build_preflight(
    ".", "g1",
    action_readiness=[{"action": "service.start", "name": "api"}],
)
```

The standalone CLI and MCP surfaces do not expose action readiness yet.

## Live gates

The relay policy remains the enforcement point. `tunnel.start` passes
`allow_live=True` and `allow_network=True` only after both CLI flags are
present. The configured relay's allowed kinds, timeout, command allowlist and
output limit still apply. The preview shows the planned argv produced by the
relay backend; if a backend cannot plan a command, the preview says so instead
of inventing one.

No preview or test starts a real service, tunnel or daemon, and no test runs a
real integration install. Tests use fake managers, fake daemon clients, fake
relays and fake installer/check runners.

## Audit

Every successful token consumption appends one compact record to
`<state dir>/actions/audit.jsonl` (`0600`). The file is bounded to the most
recent 200 records and 256 KiB.

A record contains:

- timestamp, action, outcome state and success flag;
- the target label (service, tunnel or integration name);
- the integration scope (`project` or `user`) when one applies;
- whether live/network gates were requested;
- a short hash of the consumed token;
- a bounded, redacted error message for a refusal or failure.

It never contains argv, the configured Python path, installer output, target
file content, environment values, credentials or log contents. A bounded
credential-pattern redactor is applied to error text, including prefixed
credential names such as `API_TOKEN=...`; a manager/installer error must still
not carry a resolved value that has no recognizable credential key. Pending
tokens live under `<state dir>/actions/pending/` (`0600`) and
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
`--scope project|user` and `--python PYTHON` are passed through to the
integration actions; a changed value needs a new preview and token.

## What remains

- UI buttons and dialogs that call preview, show the risks/effect, and pass the
  token to apply.
- A read-only MCP `dev_action_preview` (optional; not added in this slice so no
  MCP client receives a token or mutation surface yet).
- Mutating MCP actions are intentionally absent: MCP remains observation and
  review-first until the host can show a confirmation UI.
- Integrated trust-step dialogs and version/upgrade checks; this slice shows
  the trust text and installs only the current package.
- Provider/MCP enable/disable, resource-limit changes, file cleanup and index
  rebuild actions from the wider plan.
- Daemon idle-timeout adjustment as a separate action.
