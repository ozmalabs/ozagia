# OpenCode add-in

**Status:** read-only add-in extension (plan item 8 follow-on; task-class
catalogue for plan item 11). The package lives in
[`addins/opencode/`](../addins/opencode/); this page records its design, the
CLI mapping, the GUI surface and the API gaps found while building it.

The plan item is [item 8, OpenCode add-in MVP](plans/2026-11-16_ui_and_agent_features.md),
with the task-class catalogue from item 11. It reuses the presentation layer
from items 1 and 6: the same apertures, view model and status snapshot that the
CLI and static HTML export use. The add-in does not import Python and does not
duplicate graph queries in TypeScript.

```text
ozagia CLI JSON  ->  server part (RPC)  ->  GUI Solid panel / settings page
python -m ...        spawn, validate        graph / status / testing /
                                             research / actions / tasks /
                                             settings
```

## Package and loading

One npm package with three exports:

| Export | File | Loaded by |
| --- | --- | --- |
| `.` | `src/index.ts` | OpenCode server: `Plugin.define` + `ctx.rpc.register` |
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
        "timeoutMs": 20000
      }
    }
  ]
}
```

The object form uses the same local-path resolution as the string entry.

`python` defaults to `python3`, `root` to the plugin location directory
(`ctx.location.directory`), and `timeoutMs` to 20000 ms with a 1000–120000 ms
clamp. A relative `root` is resolved against the location directory. The
interpreter must be able to import `ozagia`; a bare checkout is not on
`sys.path`, so use an installed environment or set `PYTHONPATH` for the OpenCode
process.

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
- a wall-clock timeout that sends `SIGKILL`;
- an 8 MiB stdout cap and a 64 KiB stderr tail;
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
| `testingPlan()` | `{base?, head?, task?, changed?}` | `python -m ozagia.testing_cli plan --root ROOT --json` | `changed` is a string or string array; repeats `--changed`. |
| `researchView()` | `{investigation, view}` | `python -m ozagia.research_ui_cli --root ROOT --investigation ID --view VIEW --format json` | Read-only research projection; backs the Research tab. |
| `settingsSchema()` | `{}` | `python -m ozagia.settings_cli schema --json` | Registry entries with type, default, scope, validation and env names. |
| `settingsValues()` | `{}` | `python -m ozagia.settings_cli values --root ROOT --json` | Effective values, sources and file locations. |
| `tasksList()` | `{}` | `python -m ozagia.task_library_cli list --root ROOT --json` | Task-class catalogue entries; the GUI filters by domain locally. |
| `tasksShow()` | `{id}` | `python -m ozagia.task_library_cli show ID --root ROOT --json` | One task class: library entry plus the normalised specification. |
| `tasksValidate()` | `{}` | `python -m ozagia.task_library_cli validate --root ROOT --json` | Library validation: ok, counts, errors, warnings, issues and loaded entries. |
| `tasksCatalog()` | `{}` | `python -m ozagia.task_library_cli catalog --root ROOT --json` | Domain counts and the same catalogue metadata, for clients that prefer one object. |
| `actionsList()` | `{}` | `python -m ozagia.actions_cli list --json` | Action catalogue with mutating/destructive/live-gated flags. |
| `actionPreview()` | `{action, name?, live?, network?, maxBytes?, scope?, wait?, idleTimeout?}` | `python -m ozagia.actions_cli preview ACTION --root ROOT ... --json` | Preview only; returns the operation, argv, risks and token. No `actionApply` exists. |

Input rules:

- unknown arguments are rejected (`additionalProperties: false`);
- aperture and investigation ids are pattern-checked; views and policy/resource
  kinds are enums; a task id is bounded to 64 characters and must match
  `^[A-Za-z][A-Za-z0-9_.-]{0,63}$` before it reaches argv;
- every string has a maximum length; changed-file lists are capped at 500;
  depth is an integer 0–32; memory/disk values are finite and non-negative;
- `actionPreview` validates the action against the fifteen published names and
  rejects an argument the chosen action does not accept (for example `live` on
  `daemon.status`, or `scope` on `service.start`). `name` is required for
  service and tunnel actions and is bounded to 128 characters with no control
  characters; `maxBytes` is an integer 0–65536; `wait` is 0–600 s and
  `idleTimeout` is 0–86400 s;
- the handler repeats the validation in TypeScript, so a caller that bypasses
  the JSON-schema validator still gets `{ ok: false, error: { code:
  "invalid_input" } }` rather than an unbounded argv.

`command` in the envelope is a short display string such as
`python3 -m ozagia.view_cli --aperture architecture --format json`. Free-form
values (paths, commit messages, changed files, action target names) are not
echoed there.

### Root selection

The root is the plugin option, defaulting to `ctx.location.directory` (the
OpenCode server location for the plugin). That is the project/session directory
the server instance was loaded for. A single plugin instance does not switch
roots per routed session; point a per-worktree OpenCode server at its own root,
or set `options.root`, when working across worktrees. This is the main
per-session gap in the first slice.

## GUI part

`src/gui.tsx` registers:

- `Style` with a small prefixed stylesheet (`ozagia-*`), so the panel does not
  depend on host internals;
- one `Panel` with id `ozagia`, region `side`, width 430, a mobile menu view and
  tabs `graph`, `status`, `testing`, `research`, `actions`, `tasks`;
- one `SettingsPage` with id `ozagia-settings` and the host's
  `SettingsLayout`, `SettingsSection` and `SettingsRow` components;
- nine `Command`s: open graph, status, testing, research, actions, tasks and
  settings, check worktree and plan tests.

The graph tab is pinned and is the panel's fallback. Status, testing, research,
actions and tasks are listed when their stored tab ids are open; their commands
call `ctx.layout.open(`${ctx.id}:${tab}`, session, { background: true })`. The
settings command calls `ctx.layout.settings("ozagia-settings")`; it does not
need a routed session. Panel commands are disabled when no session is routed.
No command mutates anything.

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

The Actions tab reads `actionsList` and shows every action with `read-only`,
`mutating`, `destructive` and `live-gated` badges. Selecting an action shows only
its documented arguments: target name, integration name/scope, service-log byte
cap, daemon wait/idle timeout, and the tunnel live/network gates. Preview calls
`actionPreview` and renders:

- operation, expected effect, current state and manager call;
- the exact argv as a JSON array (no shell);
- preconditions, risks and notes;
- the one-time confirmation token, clearly labelled display-only.

The tab has no Apply button and the RPC has no `actionApply` method. Applying
requires the separate `actions_cli apply` command after a person reviews the
operation, argv and risks. A preview writes one pending token under
`.ozagia/actions/pending` (mode `0600`, ten-minute TTL); it starts, stops,
signals and connects nothing.

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
  `--approve`, and the add-in never executes it.

`list` refuses an invalid library without printing JSON. The tab therefore
falls back to the entries that `validate` still reports, and labels the state
"catalogue read from validate"; the validation card carries the actual errors.
`show` also refuses an invalid library, so the detail and copy cards stay
hidden until validation passes, and the tab says why. A missing root, a broken
interpreter or a failed `show` is shown as a structured error with retry, not as
an empty successful catalogue.

The tab does not run a task, approve a mutation, copy a class into
`ozagia/tasks/` or write any file. Browsing, copying the display command and
editing the repository configuration remain separate, explicit actions.

## Read-only guarantees

This slice is intentionally narrower than the full plan item. It does **not**:

- commit, merge, push, stage, edit, create or delete files;
- apply a process/connection action, or start, stop, signal or autostart a
  daemon, service, tunnel or job (the Actions tab only previews);
- write a setting, or call `settings_cli set` (the Settings page only reads);
- run a bounded task, call `tasks_cli run`, approve a mutating step, copy a
  task class into `ozagia/tasks/` or call `task_library_cli copy`;
- download artifacts, install Ocelot, fetch references or call a model;
- build or refresh the graph snapshot or a research investigation;
- run tests or apply a test plan;
- write repository files. A preview's pending token is the only state-dir write,
  and it is bounded, `0600` and expires.

The graph, status, testing, research and tasks panels read existing JSON. The
worktree/policy/resource commands on the RPC are checks, not actions. The action
preview is the landed `core.actions` preview: it reads runtime state and returns
a token, but never constructs a manager that could mutate state. The task
catalogue only reads the library; its copied command is text. The CLI paths
themselves are read-only by design (see [view](view.md), [status](status.md),
[worktree](worktree.md), [resources](resources.md), [testing](testing.md),
[settings](settings.md), [actions](actions.md), [task library](task_library.md)
and [research UI](research_ui.md)).

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
  validation, the copy-command builder and its shell quoting, action/argument
  matching, `changed` repetition, credential redaction/tail capping, and the
  subprocess wrapper (JSON parse, exit 1, redacted exit errors, timeout,
  cancellation and stdout cap). It is offline and does not load
  `@opencode/plugin`; `tests/load-ts.cjs` transpiles `src/runner.ts` and the pure
  task-command module with the installed TypeScript package.
- `npm run build` uses `gui()` from `@opencode/plugin/gui/vite` and emits one
  CommonJS file at `dist/gui-experimental.cjs`. The bundle keeps `solid-js`,
  `solid-js/web`, `@opencode/plugin/gui` and `@opencode/plugin/rpc` external and
  bundles no Python code.

`dist/gui-experimental.cjs` and `package-lock.json` are committed so a local
directory plugin loads without a build. `node_modules/` is ignored.

Local verification on 2026-11-16 (Node 20.5.1, npm 9.8.0, Vite 7.3.8):

- `npm run typecheck`: passed.
- `node --test tests/*.test.cjs`: 20 tests passed, 0 failed, offline.
- `npm run build`: one `dist/gui-experimental.cjs` (100.26 kB, 22.93 kB gzip).
- A direct runner smoke test against this worktree returned valid JSON for
  `settingsSchema`, `settingsValues`, `actionsList` and `actionPreview`;
  `researchView` correctly returned a structured missing-investigation error.
  Earlier checks covered `status`, `apertures`, `worktreeCheck`, `policyCheck`,
  `resourcesCheck` and `testingPlan`. The task library commands were read
  directly and through the builder: `list` returned 13 entries, `validate`
  returned `ok: true` with one expected `evidence_table` warning, and `show`
  returned the `diff_review` entry and spec.
- The server registration was exercised with a mock `ctx.rpc.register` in the
  earlier slice; the new handlers use the same wrapper and the pure builders
  tested by the Node suite.
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
   and command-opened status/testing/research/actions/tasks tabs. The settings
   page is a separate Settings contribution. There is no slot-based embedded
   view.
4. **Per-session root.** As above, the root is per plugin/server instance, not
   per routed session. `options.root` is the override.
5. **No mutation surface.** There is no setting write, action apply, service
   start/stop, tunnel start, review run, bounded-task run or reindex RPC. The
   plan item lists those commands, but they remain behind their existing CLI
   layers and confirmation flows. In particular, `actionPreview` returns a token
   and the add-in deliberately has no `actionApply`, and the Tasks tab browses
   the task library but has no run, approve or copy-into-repository method.
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

## Related documentation

- Official API: [plugins overview](https://opencode.ai/docs/build/plugins/),
  [GUI](https://opencode.ai/docs/build/plugins/gui/),
  [RPC](https://opencode.ai/docs/build/plugins/rpc/),
  [server](https://opencode.ai/docs/build/plugins/server/).
- ozagia: [status](status.md), [view](view.md), [worktree](worktree.md),
  [resources](resources.md), [testing](testing.md), [settings](settings.md),
  [actions](actions.md), [task library](task_library.md),
  [research UI](research_ui.md).
