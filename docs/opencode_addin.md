# OpenCode add-in

**Status:** preview-first add-in (plan item 8 follow-on; task-class catalogue
for plan item 11; explicit apply/run controls from the "Not yet" list;
read-only external-tool readiness and install previews). The package lives in
[`addins/opencode/`](../addins/opencode/); this page records its design, the CLI
mapping, the GUI surface and the API gaps found while building it.

The plan item is [item 8, OpenCode add-in MVP](plans/2026-11-16_ui_and_agent_features.md),
with the task-class catalogue from item 11, explicit action apply from item 14
and an executable task launcher for items 10–11. It reuses the presentation
layer from items 1 and 6: the same apertures, view model and status snapshot
that the CLI and static HTML export use. The add-in does not import Python and
does not duplicate graph queries in TypeScript.

The add-in is no longer strictly read-only. It has exactly two mutating
surfaces, both preview-first and explicitly confirmed:

- `actionApply` consumes the one-time token from an action preview and runs
  `actions_cli apply`;
- `taskRun` resolves a library task by id and runs it through
  `tasks_cli run --executor local --allow-exec`.

Everything else remains read-only: no setting write, service start/stop
without a previewed action, graph rebuild, index build, edit/write/patch tool,
model call, provider call, network fetch or arbitrary shell. The Tools tab only
reads the static registry, host facts and a bounded plan; install commands are
display text with `would_run: false`.

```text
ozagia CLI JSON  ->  server part (RPC)  ->  GUI Solid panel / settings page
python -m ...        spawn, validate        graph / status / testing /
                                             research / actions / tasks /
                                             tools / settings
```

## Package and loading

One npm package with three exports:

| Export | File | Loaded by |
| --- | --- | --- |
| `.` | `src/index.ts` | OpenCode server: `Plugin.define` + `ctx.rpc.register` |
| `./tui` | `src/tui.tsx` | OpenCode TUI: `sidebar.content` slot |
| `./rpc` | `src/rpc.ts` | server, GUI and external clients: `Rpc.define` contract |
| `./gui-experimental` | `dist/gui-experimental.cjs` | OpenCode Desktop windows and main process |

The server part declares `id: "ozagia-addin"`. The GUI part declares no id and
inherits that one, as the GUI API requires. OpenCode Desktop loads GUI parts from
the plugins in the **global** `opencode.json(c)`; this is separate from the TUI
plugin list in `cli.json`. A plugin whose server part fails to load has no GUI.

Local install (absolute path, object form for options):

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "plugins": [
    {
      "package": "/home/matt/ozmadev/ozagia/addins/opencode",
      "options": {
        "python": "/home/matt/ozmadev/ozagia/.venv/bin/python",
        "root": "/home/matt/ozmadev/ozagia",
        "timeoutMs": 20000,
        "taskTimeoutMs": 900000
      }
    }
  ]
}
```

The object form uses the same local-path resolution as the string entry.

`python` defaults to `python3`, `root` to the plugin location directory
(`ctx.location.directory`), `timeoutMs` to 20000 ms with a 1000–120000 ms
clamp, and `taskTimeoutMs` to 900000 ms with a 1000–900000 ms clamp. A relative
`root` is resolved against the location directory. The interpreter must be able
to import `ozagia`; a bare checkout is not on `sys.path`, so use an installed
environment or set `PYTHONPATH` for the OpenCode process.

The GUI is experimental. It is built and typed against `@opencode/plugin`
2.3.0, the version whose `gui()` Vite preset, `Panel`, `Command`, `Style`,
`Plugin.define`, `Rpc.define` and `ctx.rpc.register` signatures are documented
in the official pages linked below. Desktop is the supported host in this
slice; browser-tab support depends on the host version.

## Server part

`src/index.ts` is deliberately small:

```ts
export default Plugin.define({
  id: "ozagia-addin",
  async setup(ctx) {
    const config = runnerConfig(ctx.options, ctx.location.directory)
    const registration = await ctx.rpc.register(Ozagia, { ...handlers })
    return async () => registration.dispose()
  },
})
```

Every handler builds a fixed argument array and calls `runOzagia` in
`src/runner.ts`. The runner uses `node:child_process.spawn` with:

- no shell;
- `stdio: ["ignore", "pipe", "pipe"]` (closed stdin);
- `cwd = root`;
- a wall-clock timeout that sends `SIGKILL` (20 s for read-only CLIs;
  `taskTimeoutMs`, at most 15 minutes, for a task run);
- an 8 MiB stdout cap and a 64 KiB stderr tail;
- exact-value scrubbing for confirmation tokens and task input values of eight
  or more characters before any command display or error tail is returned;
- JSON parsing, with a structured error envelope for every failure.

Exit status 0 and 1 are accepted. The read-only CLIs use 1 for a report that
contains error-level findings, a refused policy or an admission refusal, and
still print valid JSON. `ok` in the envelope means "the CLI produced a report",
not "the check passed"; the report's own `ok`, `findings`, `refused` and counts
carry the result. A non-zero status other than 1, a missing interpreter, a
timeout, oversized output, invalid JSON or an unexpected spawn error yields
`{ ok: false, command, data: null, error: { code, message, detail? } }`.

Error text is collapsed, capped at 500 characters and redacted for common
credential shapes (GitHub, OpenAI-style `sk-`, Slack, AWS access keys, bearer
tokens, `token=`/`secret=` assignments and URL credentials). Full stderr is
never returned.

### RPC contract

`src/rpc.ts` defines one RPC, `ozagia`, with a permissive JSON-object output
schema and these methods:

| Method | Input | CLI | Notes |
| --- | --- | --- | --- |
| `status()` | `{}` | `python -m ozagia.status_cli --root ROOT --json` | Composed status snapshot. |
| `apertures()` | `{}` | `python -m ozagia.view_cli --list-apertures --format json --root ROOT` | Built-ins plus repo overrides. |
| `view()` | `{aperture, depth?, node?, graph?}` | `python -m ozagia.view_cli --aperture ... --format json --root ROOT` | Adds `--depth`, `--node`, `--graph` when supplied. |
| `worktreeCheck()` | `{}` | `python -m ozagia.worktree_cli check --root ROOT --json` | Worktrees, expected branches, drift, findings. |
| `policyCheck()` | `{kind, message?, target?, force?, tags?}` | `python -m ozagia.worktree_cli policy --for KIND --root ROOT --json` | `kind` is `commit`, `merge` or `push`. |
| `resourcesCheck()` | `{kind?, memoryMb?, diskMb?, path?}` | `python -m ozagia.resources_cli check --for KIND --root ROOT --json` | `kind` defaults to `job`; admission only. |
| `toolchainEnv()` | `{probeSudo?}` | `python -m ozagia.toolchain_cli env --root ROOT --json [--no-sudo]` | Host, managers, Python, sudo and `[tools]` approval facts. |
| `toolchainList()` | `{}` | `python -m ozagia.toolchain_cli list --json` | Static registry; the CLI has no `--root` flag for `list`. |
| `toolchainPlan()` | `{capabilities?, paths?, probeVersions?, probeSudo?}` | `python -m ozagia.toolchain_cli plan --root ROOT [--capability ID ...] [--path P ...] --json [--no-versions] [--no-sudo]` | Read-only readiness and install previews; every option has `would_run: false`. |
| `testingPlan()` | `{base?, head?, task?, changed?}` | `python -m ozagia.testing_cli plan --root ROOT --json` | `changed` is a string or string array; repeats `--changed`. |
| `researchView()` | `{investigation, view}` | `python -m ozagia.research_ui_cli --root ROOT --investigation ID --view VIEW --format json` | Read-only research projection; backs the Research tab. |
| `settingsSchema()` | `{}` | `python -m ozagia.settings_cli schema --json` | Registry entries with type, default, scope, validation and env names. |
| `settingsValues()` | `{}` | `python -m ozagia.settings_cli values --root ROOT --json` | Effective values, sources and file locations. |
| `tasksList()` | `{}` | `python -m ozagia.task_library_cli list --root ROOT --json` | Task-class catalogue entries; the GUI filters by domain locally. |
| `tasksShow()` | `{id}` | `python -m ozagia.task_library_cli show ID --root ROOT --json` | One task class: library entry plus the normalised specification. |
| `tasksValidate()` | `{}` | `python -m ozagia.task_library_cli validate --root ROOT --json` | Library validation: ok, counts, errors, warnings, issues and loaded entries. |
| `tasksCatalog()` | `{}` | `python -m ozagia.task_library_cli catalog --root ROOT --json` | Domain counts and the same catalogue metadata, for clients that prefer one object. |
| `actionsList()` | `{}` | `python -m ozagia.actions_cli list --json` | Action catalogue with mutating/destructive/live-gated flags. |
| `actionPreview()` | `{action, name?, live?, network?, maxBytes?, scope?, wait?, idleTimeout?}` | `python -m ozagia.actions_cli preview ACTION --root ROOT ... --json` | Preview only; returns the operation, argv, risks and a one-time token. |
| `actionApply()` | `{action, confirm, ...the same bounded arguments}` | `python -m ozagia.actions_cli apply ACTION --confirm TOKEN --root ROOT ... --json` | Explicit apply; the token is argv-only and displayed as `<redacted>`. |
| `taskRun()` | `{id, inputs?}` | `task_library_cli show ID --root ROOT --json` then `tasks_cli run PATH --executor local --allow-exec --root ROOT --input NAME=VALUE ... --json` | Resolves the path from the library; no approve flag. |

Input rules:

- unknown arguments are rejected (`additionalProperties: false`);
- aperture and investigation ids are pattern-checked; views and policy/resource
  kinds are enums; a task id is bounded to 64 characters and must match
  `^[A-Za-z][A-Za-z0-9_.-]{0,63}$` before it reaches argv;
- every string has a maximum length; changed-file lists are capped at 500;
  depth is an integer 0–32; memory/disk values are finite and non-negative;
- `actionPreview` validates the action against the fifteen published names
  (`integration.install`, `integration.uninstall` and `integration.verify`
  remain CLI-only) and rejects an argument the chosen action does not accept
  (for example `live` on `daemon.status`, or `scope` on `service.start`).
  `name` is required for service and tunnel actions and is bounded to 128
  characters with no control characters; `maxBytes` is an integer 0–65536;
  `wait` is 0–600 s and `idleTimeout` is 0–86400 s;
- `actionApply` repeats those action/argument checks and requires `confirm` to
  match `core.actions._TOKEN`: `^[A-Za-z0-9_-]{20,128}$`, which also rejects
  control characters and spaces. The token is not trimmed or echoed;
- `taskRun` validates `id` with the library regex, takes at most 16 inputs,
  requires names to match `^[A-Za-z][A-Za-z0-9_.-]{0,63}$`, and requires string
  values of at most 4096 characters with no NUL or control characters. The
  whole argument vector is capped at 64 KiB. The handler rejects an invalid
  request before spawning anything;
- the task spec path is never accepted from the caller. It comes from
  `task_library_cli show`, must be absolute and named `<id>.toml`, and must sit
  under `<root>/ozagia/tasks` for a repo override or the package `task_specs`
  tree for a built-in. A missing, mismatched or out-of-library path returns a
  structured `task_path_invalid` envelope with no run;
- `toolchainPlan` accepts at most 16 capability/group ids and 32 paths.
  Capability ids must match `^[a-z][a-z0-9_-]{0,31}$`, so an id can never look
  like an option or an arbitrary argument. Paths are at most 2000 characters
  with no control characters; they are used only for extension inference and
  are never opened. Duplicate ids and paths are removed. `probeVersions: false`
  adds `--no-versions` and `probeSudo: false` adds `--no-sudo`; nothing else is
  accepted. The CLI has no `--model-family` flag, so the model brief in the GUI
  is rendered locally from `native_for`/`preferred_by` metadata;
- the handler repeats the validation in TypeScript, so a caller that bypasses
  the JSON-schema validator still gets `{ ok: false, error: { code:
  "invalid_input" } }` rather than an unbounded argv.

`command` in the envelope is a short display string such as
`python3 -m ozagia.view_cli --aperture architecture --format json`. Free-form
values (paths, commit messages, changed files, action target names) are not
echoed there. The apply display always says `--confirm <redacted>`, and a task
run display names input keys with `<redacted>` values. The runner also scrubs
the exact token (and input values of eight or more characters) from any error
tail, in addition to the credential-shape redaction.

### Root selection

The root is the plugin option, defaulting to `ctx.location.directory` (the
OpenCode server location for the plugin). That is the project/session directory
the server instance was loaded for. A single plugin instance does not switch
roots per routed session; point a per-worktree OpenCode server at its own root,
or set `options.root`, when working across worktrees. This is the main
per-session gap in the first slice.

## GUI part

### Session profile in OpenCode

The normal `ozagia integrations opencode install --workdir <repo>` command
installs a separate `.opencode/plugins/ozagia-profile/tui.js` companion. It
uses OpenCode's supported `sidebar.content` slot and leaves the existing
`.opencode/plugins/ozagia/tui.js` brand plugin alone. On OpenCode 2.3, the
server-side Ozagia plugin subscribes to `session.step.*` and
`session.message.content.updated` events, so host profiling does not depend on
opening the sidebar. It records only model, token, timing, and terminal tool
metadata; message text and tool input/output are not copied. The TUI companion
renders the report and status only. Neither plugin requires a change to the
global `cli.json`. The optional Desktop GUI add-in offers the same report in
its Profile tab through the add-in's `sessionProfile` RPC.

While an `ozagia agent` launch is preparing the graph, the sidebar shows the
launch phase instead of the status metrics:

```
Ozagia
Status: starting
Graph: building · 4s
Daemon: waiting
```

It reads the file named by `OZAGIA_LAUNCH_STATUS` (the session sets it in the
TUI's environment) every 500 ms and stops once the phase is `ready` or
`failed`, so the sidebar does not poll the launch file for the rest of the
session. Without that variable (a standalone install, the Desktop add-in) the
block is the live status only. Once ready it shows:

```
Ozagia
Status: online
Graph: stale · working tree changed
Model: vllm/qwen3.8-27b
Session: 366 tools · 268 model calls
```

`Status` is `online` when the graph daemon answers, `offline` when no daemon is
configured, and `unavailable`/`unknown` when the probe cannot establish it.
`Graph` reports the snapshot state and, when stale, the reason recorded by the
doctor (`working tree changed`, `HEAD moved` or `different root`). The session
line reports the observed tool and model-call counts for this session and adds
an error count only when one was recorded. The previous `H/P/F/U/E` counters
are gone; the detailed outcome counts remain available through the session
profile report and CLI.

The report counts daemon-observed tool/after operations per harness and
session, handled tools, passthrough, expiry, errors, plugin fallbacks, and the
durations the daemon actually recorded. It asks the live daemon's `stats`
operation for the active journal path. If the daemon is unavailable, a default
journal can be shown only as `fallback_unverified`; counts then are not evidence
of complete session activity. The journal read is capped at 2 MiB and
200 sessions are returned. Sanitized daemon outcome counters are also persisted
as operations occur under `.ozagia/session_profiles/`, allowing retrospective
reports after the daemon or its temporary journal disappears. Handled,
successful passthrough, expired, failed (`ok=false`), and unknown tool outcomes
are reported separately; errors include daemon failures. Legacy rows without
`ok` retain the old explicit `handled=false` passthrough interpretation. Writers
use a cross-process file lock with a 50 ms wait bound and atomic replacement; the
daemon performs this write after releasing its stats lock. A lock timeout drops
that profile event and is returned by the RPC or logged by the daemon. Persisted
counters take precedence over a truncated journal window; reports mark bounded
journal truncation. Writes close before returning but skip per-tool
`fsync` to avoid storage-device sync on the tool response path, matching the
journal's process-crash durability level. A local 80-write microbenchmark on the
worktree filesystem measured 0.229 ms median and 0.258 ms p95; this is an
observed measurement, not a platform guarantee. The report does not expose
journal keys, paths, prompts, source, or tool contents. Model and
token usage come from OpenCode message events when supplied; missing fields are
reported as unknown. The session profile state is bounded to 200 sessions and
32 KiB per session. No message body or tool input/output is stored. SDK-shaped
event extraction is tested, and a temporary PTY smoke verifies the installed
sidebar, but live host event delivery has not been end-to-end injected because
the available host API for creating assistant messages invokes a model.
Operation durations are not a task critical path: no
dependency graph or model-think times are available here. Missing journals,
unknown fields, and daemon/RPC failures remain explicit.

Use `python -m ozagia.session_profile_cli --root <repo> --session <id> --json`
to inspect one session without the add-in. The package also exposes
`ozagia-session-profile` on a fresh installation.

`src/gui.tsx` registers:

- `Style` with a small prefixed stylesheet (`ozagia-*`), so the panel does not
  depend on host internals;
- one `Panel` with id `ozagia`, region `side`, width 430, a mobile menu view and
  tabs `graph`, `status`, `testing`, `research`, `actions`, `tasks`, `tools`;
- one `SettingsPage` with id `ozagia-settings` and the host's
  `SettingsLayout`, `SettingsSection` and `SettingsRow` components;
- ten `Command`s: open graph, status, testing, research, actions, tasks, tools
  and settings, check worktree and plan tests.

The graph tab is pinned and is the panel's fallback. Status, testing, research,
actions, tasks and tools are listed when their stored tab ids are open; their
commands call `ctx.layout.open(`${ctx.id}:${tab}`, session, { background:
true })`. The settings command calls `ctx.layout.settings("ozagia-settings")`;
it does not need a routed session. Panel commands are disabled when no session
is routed. The commands themselves only open panels or run read-only checks;
mutation stays inside the Actions and Tasks tabs and their explicit
confirmations.

Labels are rendered as Solid text/attributes. There is no `innerHTML`; recorded
node/edge labels, findings, file paths and error text are escaped by the
framework. The SVG graph is built from Solid-owned `<line>`, `<circle>` and
`<title>` elements, and the node table uses ordinary table cells. The node
inspector renders recorded `attrs` with `JSON.stringify` inside `<pre>`, and SVG
edges use `<title>` for their accessible tooltip.

### Graph

- Loads the aperture list once and refreshes the view when the picker or Refresh
  changes. The default is `architecture`; if the list is available and the
  current id is not, the first aperture is selected.
- Shows aperture, layout kind, node/edge/group counts and omitted counts.
- Deterministic layout: nodes are sorted by id and placed on a circle with the
  aperture's fixed seed as the angle offset. There is no random force
  simulation, so the same model and seed produce the same SVG. Edges are drawn
  between known endpoints; the graph model already counts dangling edges.
- Nodes are keyboard-focusable (`Enter`/`Space` selects). The selected node
  shows id, label, type/raw type, group, key, weight and all recorded
  attributes.
- The node table is capped at 120 rows for DOM size, with the full count
  reported; the SVG still draws every node.
- Snapshot root/head/freshness, graph kind/schema, sources, omissions and the
  breakdown are shown. A missing freshness is shown as "not recorded" rather
  than "fresh".
- A missing graph or invalid aperture shows the CLI's message plus a hint to
  build a snapshot; it does not render an empty successful view.

### Status

Sections mirror `ozagia status --json`:

- snapshot header: hard errors and degraded components, root, schema, checked
  time;
- workspace: worktree path, branch, expected-branch mismatch, ahead/behind,
  dirty count, free disk and tmpfs; workspace and per-worktree findings;
- resources: memory used/available, load, CPU count, memory meter, primary
  disk/mount/fstype/free/inodes/tmpfs, admission policy and top process groups;
- files: ledger records, orphans, new count, presence and path;
- daemon: state, ping and stats availability, socket, timeout, stats JSON in a
  details block;
- graph snapshot: doctor state/path and snapshot degraded messages;
- runtime and configuration summaries.

Every section shows the status envelope's `state` and `error`, and the panel
distinguishes `absent` from `unknown` as the status snapshot does. Refresh
re-runs the whole snapshot once; the panel does not shell out per tile.

### Testing

Inputs are base (default `HEAD`), head (empty means the working tree), task
class and changed files. The Plan button calls `testingPlan` once. The result
shows:

- effective mode and requested mode, changed/selected/omitted counts and cap;
- graph freshness, whether the graph was usable and why;
- selected tests with source labels and graph node counts;
- omitted tests with reasons, including a truncated-list note;
- fallback use and reason, refusal state, warnings and the explicit
  `not_assessed` list.

The panel states plainly that planning executes nothing and assesses no truth.
A plan is evidence, not a test result.

### Research

Inputs are an investigation id and one of the six recorded views (`library`,
`fetch`, `screening`, `evidence`, `consensus`, `citation`). Load view calls
`researchView` once and renders:

- the view title, investigation id and truth-assessed flag;
- summary counts and names, including `row_count`;
- the primary row table with the view's ordered columns;
- provenance: generator, source graph path, root, graph/view schema, derived
  flag and record counts by node and edge type;
- omitted rows and the row-cap limit, plus the graph projection's own omissions
  when present;
- the explicit `not_assessed` list and the view's read-only note.

A missing investigation, missing graph or malformed state is shown as the CLI's
error plus a "check the id or state directory" message. The tab never creates an
investigation, builds a graph or renders a missing record as a successful empty
view. The table shows the first 100 returned rows for DOM size; the returned-row
count and omitted-row count are reported separately.

### Settings

The contributed **ozagia settings** page calls both `settingsSchema` and
`settingsValues` once. It merges them by key and renders the host's settings
components:

- a read-only status row and the effective root/repo-config path;
- one section per registry group (`index`, `resources`, `worktrees`, `markdown`,
  `files`, `models`, `tasks`, `review`, `testing`, `ui`), with open-ended
  `testing.tasks.*` and `testing.paths.*` entries grouped under `testing`;
- one row per key with the current value, its source badge (`default`, `user`,
  `repo`, `env` or `override`), type, scope, default, description and restart
  flag.

There is no editable control and no `settings_cli set` call. A failed read shows
the structured error and a retry; a successful empty read shows an explicit
empty state. Write previews and applies remain CLI-only.

### Actions

The Actions tab reads `actionsList` and shows every action this add-in
supports, with `read-only`, `mutating`, `destructive` and `live-gated` badges.
Selecting an action shows only its documented arguments: target name,
integration name/scope, service-log byte cap, daemon wait/idle timeout, and the
tunnel live/network gates. Preview calls `actionPreview` and renders:

- operation, expected effect, current state and manager call;
- the exact argv as a JSON array (no shell);
- preconditions, risks and notes;
- the one-time confirmation token, clearly labelled.

After a preview with a token, the **Apply action** card repeats the action,
kind and risks, requires an explicit checkbox confirmation, and calls
`actionApply` with the same bounded arguments plus the token. Preview never
applies by itself, and editing the form does not silently retarget the token:
the apply call reuses the exact arguments that produced the preview. The result
shows state, `ok`, mode, an error string, the structured result, the audit note
and any audit warning. A refused or failed apply is rendered as a structured
error, not hidden.

The token is argv-only. The GUI command shape is
`python -m ozagia.actions_cli apply ACTION --confirm <redacted> --json`; the
actual RPC carries the token, and the runner scrubs it from the display command
and error tails. The token remains visible in the preview card because a person
needs to see what was issued; it is never copied into command text. A preview
writes one pending token under `.ozagia/actions/pending` (mode `0600`,
ten-minute TTL). Apply re-checks the preconditions, consumes the token and
performs the action through the landed managers.

### Tasks

The Tasks tab is the catalogue from [item 11](plans/2026-11-16_ui_and_agent_features.md)
over `core.task_library`. It loads `tasksList` and `tasksValidate` together,
then the selected class through `tasksShow`:

- validation card: library valid/invalid, total/built-in/repo counts, every
  error and warning, and a capped issues table with severity, code, task id and
  message. An invalid override is shown, not hidden;
- domain filter: `all`, `research`, `coding` and `ops`, with counts; filtering is
  local because `tasksList` already returns the whole catalogue;
- catalogue table: id, domain, version, source badge (`builtin` or `repo`),
  override version, title and warning count; rows are keyboard-selectable;
- detail: title, objective/description, id, version, source, path, digest,
  built-in version, inputs, tool allowlist, write scope, budget, resources,
  worktree expectations, before/after gates, stop conditions, approval policy,
  `requires_graph`, optional test policy and every step with tool, goal, paths
  and write paths;
- copy command: a read-only **Copy command** button copies a display-only
  `python -m ozagia.tasks_cli run PATH --dry-run --root ROOT ... --json`
  command. Each declared, non-automatic input becomes an editable
  `--input NAME=<value>` placeholder. It never contains `--allow-exec` or
  `--approve`, and the add-in never executes the copied text;
- run: a **Run task (local executor)** form fills the declared inputs; inputs
  the runner supplies (`repo_root`, `run_id`, `state_dir`) are listed rather
  than asked for. The first click opens a confirmation showing the task id and
  the exact redacted command; only the second click calls `taskRun`. Nothing
  runs on selection, input edits or refresh.

The run RPC resolves the task with `task_library_cli show` and takes
`entry.path` from that JSON. It never accepts a caller path and has no fallback
for a missing or invalid one. The path must be absolute, named `<id>.toml`, and
inside `<root>/ozagia/tasks` (repo override) or the package `task_specs` tree
(built-in). Inputs are bounded to 16 names, the library alphabet, 4096-character
strings without control characters, and 64 KiB of total argv. The task call uses
the `taskTimeoutMs` budget (default and maximum 15 minutes), while
`LocalExecutor`'s pytest runner is itself capped at 900 s. The call never passes
`--approve`, so mutating, model and unimplemented tools remain blocked.

The result shows run id, status, stop reason, exit code, executed flag, step
count, a findings count when the output exposes one, the explicit
`what_was_not_assessed` list and a bounded output tail (4000 characters).

`list` refuses an invalid library without printing JSON. The tab therefore
falls back to the entries that `validate` still reports, and labels the state
"catalogue read from validate"; the validation card carries the actual errors.
`show` also refuses an invalid library, so the detail, copy and run cards stay
hidden until validation passes, and the tab says why. A missing root, a broken
interpreter or a failed `show` is shown as a structured error with retry, not as
an empty successful catalogue.

The tab does not approve a mutating step, copy a class into `ozagia/tasks/` or
call `task_library_cli copy`. Run is the only execution surface.

### Tools

The Tools tab is the read-only surface for external-tool availability over the
landed `core.toolchain` registry and CLIs from [toolchain](toolchain.md). It
loads `toolchainList` and `toolchainEnv` together, then `toolchainPlan` on an
explicit **Plan tools** action:

- environment card: OS/architecture and distribution, WSL/container hints,
  managers with paths, Python version/executable, venv and uv state, sudo
  availability and whether non-interactive sudo was probed, the three `[tools]`
  approval gates with their sources, and configuration errors (an unknown or
  invalid `[tools]` key is shown, not hidden);
- registry card: registry version, capability/tool/group counts and the group
  list from the static `list --json` document; this document is not probed;
- inputs: comma/space separated capability or group ids and newline/comma
  separated paths for extension inference. At most 16 capabilities and 32 paths
  are accepted locally and remotely; capability ids are lower-case registry ids
  (`^[a-z][a-z0-9_-]{0,31}$`), paths are at most 2000 characters and may not
  contain control characters. Two checkboxes control the CLI's only probe
  switches: bounded `--version` probes (`--no-versions`) and the
  `sudo -n true` usability check (`--no-sudo`). Both are off by default in the
  GUI so opening the tab does not start probes; the environment card then says
  what was not checked;
- capability cards: `ready`, `missing`, `blocked`, `remote` and `unprobed`
  counts; available tools with kind, version, path/module and `provides`;
  missing tools; remote/MCP providers named as requiring network (and
  credentials when relevant) but never connected or authenticated; local Node
  libraries marked "presence not checked" rather than guessed; and the
  recommended path/tool when one exists;
- install previews: at most eight per capability with the exact command, the
  manager and install kind, `requires_sudo`/`requires_network`, `would_run:
  false`, the `blocked_by` list (`install`, `network`, `sudo`, `manager`) and
  the human-readable blockers. The tab has no install or apply control; a
  preview with no blocker is still text and says so;
- model brief: a compact multi-line brief rendered locally from the plan
  (environment, per-capability readiness, useful providers and `provides`, and
  the exact recommended command only when installation is approved). A
  model-family hint (`claude`, `codex`, `gemini`, `cursor`, `aider`,
  `openhands`, `local`) uses the registry's `native_for`/`preferred_by`
  metadata. There is no model call and no CLI flag; the CLI has no
  `--model-family` option. **Copy brief** only writes the text to the
  clipboard.

The plan's own `not_assessed` list is shown verbatim: versions are not checked
against a range, repository availability and network reachability are not
checked, remote credentials are not checked, and nothing is installed. A
missing tool is a report, not an error. The tab does not call
`toolchain_cli explain`, install a package, start a package manager, set a gate
or write `[tools]` configuration.

## Mutation surface and guarantees

The add-in is narrower than the full plan item. Apart from `actionApply` and
`taskRun`, it does **not**:

- commit, merge, push, stage, edit, create or delete repository files;
- start, stop, signal or autostart a daemon, service, tunnel or job without a
  previewed action and its one-time token;
- write a setting, or call `settings_cli set` (the Settings page only reads);
- approve a mutating task step, copy a task class into `ozagia/tasks/` or call
  `task_library_cli copy`;
- download artifacts, install Ocelot or an external tool, fetch references or
  call a model;
- build or refresh the graph snapshot or a research investigation;
- run tests or apply a test plan outside a task's `test` step;
- expose an arbitrary shell, direct edit/write/patch tool or caller-supplied
  spec path.

The state-dir writes are the ones the landed CLIs already own: a preview's
pending token (bounded, `0600`, ten-minute TTL), an apply's audit record and the
task run's `manifest.json`, `events.jsonl`, `artifacts.json` and optional
`output` under `.ozagia/tasks/`. No other repository file is written.

The graph, status, testing, research, settings and tools panels read existing
JSON. The worktree/policy/resource commands on the RPC are checks, not actions.
The action preview is the landed `core.actions` preview: it reads runtime state
and returns a token, but never constructs a manager that could mutate state.
Apply is the landed `core.actions.apply`: it re-checks the token, preconditions
and arguments, consumes the token and dispatches through the verified managers.
The task catalogue only reads the library; its copied command is text. A task
run is the landed `tasks_cli run --executor local --allow-exec`: it resolves the
spec path from the library, runs only the declared read-only tools and never
approves a mutation. The CLI paths themselves are documented in
[view](view.md), [status](status.md), [worktree](worktree.md),
[resources](resources.md), [testing](testing.md), [settings](settings.md),
[actions](actions.md), [task library](task_library.md),
[research UI](research_ui.md) and [toolchain](toolchain.md).

## Build and tests

```bash
cd addins/opencode
npm install --no-audit --no-fund
npm run typecheck
npm test
npm run build
```

- `npm run typecheck` runs `tsc --noEmit` over `src` and `vite.config.ts`.
- `npm test` runs `node --test tests/*.test.cjs`. The suite covers every
  argument builder, optional flags, bounds and enum rejection, task-id
  validation, the copy-command and redacted task-run builders, action/argument
  matching, apply token validation and command/error redaction, task path
  resolution and no-fallback behaviour, toolchain capability/path bounds and
  repeated flags, the model brief, RPC schema shape, confirmation gating,
  `changed` repetition, credential redaction/tail capping, and the subprocess
  wrapper (JSON parse, exit 1, redacted exit errors, timeout, cancellation and
  stdout cap). It is offline and does not load `@opencode/plugin`;
  `tests/load-ts.cjs` transpiles `src/runner.ts` and the pure task-command,
  toolchain-brief, mutation and schema modules with the installed TypeScript
  package.
- `npm run build` uses `gui()` from `@opencode/plugin/gui/vite` and emits one
  CommonJS file at `dist/gui-experimental.cjs`. The bundle keeps `solid-js`,
  `solid-js/web`, `@opencode/plugin/gui` and `@opencode/plugin/rpc` external and
  bundles no Python code.

`dist/gui-experimental.cjs` and `package-lock.json` are committed so a local
directory plugin loads without a build. `node_modules/` is ignored.

Local verification on 2026-11-17 (Node 20.5.1, npm 9.8.0, Vite 7.3.8):

- `npm run typecheck`: passed.
- `node --test tests/*.test.cjs`: 49 tests passed, 0 failed, offline.
- `npm run build`: one `dist/gui-experimental.cjs` (143.14 kB, 32.96 kB gzip).
- The Node suite exercises apply argv/display separation and token scrubbing,
  task id resolution, library path containment and no-fallback behaviour,
  input/argv bounds, task timeout selection, toolchain argv/display separation,
  capability/path bounds, the model brief, no-shell execution, the RPC schemas
  and the GUI confirmation predicates with a fake interpreter. It does not run
  `actions_cli apply` or a real task against this checkout.
- Earlier slices' runner smoke tests covered `status`, `apertures`,
  `worktreeCheck`, `policyCheck`, `resourcesCheck`, `testingPlan`,
  `settingsSchema`, `settingsValues`, `actionsList`, `actionPreview` and
  `researchView`; the task library commands were read directly and through the
  builder (`list`, `validate`, `show`). The toolchain `list`, `env --no-sudo`
  and `plan --capability documents --no-sudo --no-versions --json` were read
  directly while building. The new handlers use the same wrapper and the pure
  builders tested by the Node suite.
- Vite printed its engine warning because this host has Node 20.5.1 (Vite 7
  asks for 20.19+ or 22.12+); the build still completed.

## API-version notes and gaps

Built and typechecked against `@opencode/plugin` 2.3.0 (the current latest at the
time of writing). The GUI SDK is explicitly experimental. The following are
known gaps or host dependencies, not hidden behaviour:

1. **No live host test in this worktree.** The build, typecheck and runner smoke
   tests were exercised locally; the GUI cannot be mounted without OpenCode
   Desktop. Verify `@opencode/plugin` compatibility with the installed Desktop
   version before relying on the panel.
2. **Browser tabs.** The GUI docs describe windows and the desktop main process;
   GitHub-style browser-tab support depends on the host version. This package
   uses only window APIs (`Panel`, `SettingsPage`, `Command`, `Style`,
   `ctx.layout`, `ctx.toast`, `ctx.client`, `ctx.ui`), so it should work wherever
   those are provided.
3. **Panel layout breadth.** The panel is a side panel with a pinned graph tab
   and command-opened status/testing/research/actions/tasks/tools tabs. The
   settings page is a separate Settings contribution. There is no slot-based
   embedded view.
4. **Per-session root.** As above, the root is per plugin/server instance, not
   per routed session. `options.root` is the override.
5. **Exactly two mutation surfaces.** `actionApply` and `taskRun` are the only
   methods that can change local state; there is still no setting write, direct
   service/tunnel management outside a previewed action, review run, task
   approve, copy-into-repository or reindex RPC. The Actions tab requires a
   preview token plus a second checkbox confirmation; the Tasks tab requires a
   second Run confirmation and never auto-runs. Action apply and task run are
   asynchronous CLI calls, so a long task can be cancelled only by aborting the
   host request, not by a cancel button. The local executor remains read-only:
   mutating, model and unimplemented tools are blocked and there is no
   `--approve` path in the add-in. The Tools tab adds no install or apply
   method; install commands are preview text only.
6. **Research writes.** The Research tab is a projection only. Reference merge,
   screening revision, adjudication, fetch apply and manuscript controls are not
   exposed.
7. **Settings search.** The page uses the host settings components and renders
   one section per registry group, but its per-key rows are not added to the
   settings search index; the page-level entry is.
8. **Node version.** Vite 7 warns on Node 20.5.1 and recommends 20.19+/22.12+.
   The build succeeds; use a supported Node when possible.
9. **Output schema.** The envelope uses a permissive JSON-object schema by
   design, so the host validates transport shape but not every CLI payload
   field. The GUI narrows defensively and renders section errors instead of
   assuming a report.
10. **Toolchain scope.** The Tools tab reports what `core.toolchain` knows. Remote
    MCP/API providers are named but not connected or authenticated; credentials,
    quotas, network reachability and permissions are not checked. Version output
    is not compared with a minimum. The registry covers the documented
    capabilities and may not name a repository's local alternative. Probe
    versions and the `sudo -n true` check default to off in the GUI to avoid
    starting probes on tab open; enable them for a fuller plan. The tab has no
    install/apply control and does not run `toolchain_cli explain`. A default
    plan from the current working tree is not supplied automatically because
    this slice has no host path input; enter paths in the tab.

## Related documentation

- Official API: [plugins overview](https://opencode.ai/docs/build/plugins/),
  [GUI](https://opencode.ai/docs/build/plugins/gui/),
  [RPC](https://opencode.ai/docs/build/plugins/rpc/),
  [server](https://opencode.ai/docs/build/plugins/server/).
- ozagia: [status](status.md), [view](view.md), [worktree](worktree.md),
  [resources](resources.md), [testing](testing.md), [settings](settings.md),
  [actions](actions.md), [task library](task_library.md),
  [research UI](research_ui.md), [toolchain](toolchain.md).
