# OpenCode add-in

**Status:** wave 4, first interactive UI host. Read-only. The package lives in
[`addins/opencode/`](../addins/opencode/); this page records its design, the CLI
mapping, the GUI surface and the API gaps found while building it.

The plan item is [item 8, OpenCode add-in MVP](plans/2026-11-16_ui_and_agent_features.md).
It reuses the presentation layer from items 1 and 6: the same apertures, view
model and status snapshot that the CLI and static HTML export use. The add-in
does not import Python and does not duplicate graph queries in TypeScript.

```text
ozagia CLI JSON  ->  server part (RPC)  ->  GUI Solid panel
python -m ...        spawn, validate        graph / status / testing
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
      "package": "/home/matt/ozmadev/ozagia-wt/opencode-addin/addins/opencode",
      "options": {
        "python": "/home/matt/ozmadev/ozagia/.venv/bin/python",
        "root": "/home/matt/ozmadev/ozagia-wt/opencode-addin",
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
| `researchView()` | `{investigation, view}` | `python -m ozagia.research_ui_cli --root ROOT --investigation ID --view VIEW --format json` | Read-only research projection; no UI tab yet. |

Input rules:

- unknown arguments are rejected (`additionalProperties: false`);
- aperture and investigation ids are pattern-checked; views and policy/resource
  kinds are enums;
- every string has a maximum length; changed-file lists are capped at 500;
  depth is an integer 0–32; memory/disk values are finite and non-negative;
- the handler repeats the validation in TypeScript, so a caller that bypasses
  the JSON-schema validator still gets `{ ok: false, error: { code:
  "invalid_input" } }` rather than an unbounded argv.

`command` in the envelope is a short display string such as
`python3 -m ozagia.view_cli --aperture architecture --format json`. Free-form
values (paths, commit messages, changed files) are not echoed there.

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
  tabs `graph`, `status`, `testing`;
- five `Command`s: open graph, open status, open testing, check worktree, plan
  tests.

The graph tab is pinned and is the panel's fallback. Status and testing are
listed when their stored tab ids are open; their commands call
`ctx.layout.open(`${ctx.id}:${tab}`, session, { background: true })`. Commands
are disabled when no session is routed. No command mutates anything.

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

## Read-only guarantees

This slice is intentionally narrower than the plan item. It does **not**:

- commit, merge, push, stage, edit, create or delete files;
- start, stop, signal or autostart a daemon, service, tunnel or job;
- download artifacts, install Ocelot, fetch references or call a model;
- build or refresh the graph snapshot;
- run tests or apply a test plan;
- write into the repository or its state directory.

The graph and status panels read existing JSON. The worktree/policy/resource
commands on the RPC are checks, not actions. The CLI paths themselves are
read-only by design (see [view](view.md), [status](status.md),
[worktree](worktree.md), [resources](resources.md) and [testing](testing.md)).

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
  argument builder, optional flags, bounds and enum rejection, `changed`
  repetition, credential redaction/tail capping, and the subprocess wrapper
  (JSON parse, exit 1, redacted exit errors, timeout, cancellation and stdout
  cap). It is offline and does not load `@opencode/plugin`; `tests/load-ts.cjs`
  transpiles `src/runner.ts` with the installed TypeScript package.
- `npm run build` uses `gui()` from `@opencode/plugin/gui/vite` and emits one
  CommonJS file at `dist/gui-experimental.cjs`. The bundle keeps `solid-js`,
  `solid-js/web`, `@opencode/plugin/gui` and `@opencode/plugin/rpc` external and
  bundles no Python code.

`dist/gui-experimental.cjs` and `package-lock.json` are committed so a local
directory plugin loads without a build. `node_modules/` is ignored.

Local verification on 2026-11-16 (Node 20.5.1, npm 9.8.0, Vite 7.3.8):

- `npm run typecheck`: passed.
- `npm test`: 14 tests passed, 0 failed, offline.
- `npm run build`: one `dist/gui-experimental.cjs` (about 49 kB, 12.1 kB gzip).
- A direct runner smoke test against this worktree returned valid JSON for
  `status`, `apertures`, `worktreeCheck`, `policyCheck`, `resourcesCheck` and
  `testingPlan`; `view` and `researchView` correctly returned structured errors
  for a missing graph and a missing investigation.
- The server registration was exercised with a mock `ctx.rpc.register`; all
  eight handlers were present and an invalid aperture returned
  `invalid_input`.
- Vite printed its engine warning because this host has Node 20.5.1 (Vite 7
  asks for 20.19+ or 22.12+); the build still completed.

## API-version notes and gaps

Built and typechecked against `@opencode/plugin` 2.3.0 (the current latest at the
time of writing). The GUI SDK is explicitly experimental. The following are
known gaps or host dependencies, not hidden behaviour:

1. **No live host test in this worktree.** The build, typecheck and server
   registration were exercised locally; the GUI cannot be mounted without
   OpenCode Desktop. Verify `@opencode/plugin` compatibility with the installed
   Desktop version before relying on the panel.
2. **Browser tabs.** The GUI docs describe windows and the desktop main process;
   GitHub-style browser-tab support depends on the host version. This package
   uses only window APIs (`Panel`, `Command`, `Style`, `ctx.layout`, `ctx.toast`,
   `ctx.client`), so it should work wherever those are provided.
3. **Panel layout breadth.** The panel is a side panel with a pinned graph tab
   and command-opened status/testing tabs. It is not yet a settings page or a
   slot-based embedded view.
4. **Per-session root.** As above, the root is per plugin/server instance, not
   per routed session. `options.root` is the override.
5. **Settings page and mutations.** The plan item lists a settings page and
   more commands ("Check policy", "Explain resources", "Review this diff",
   "Show impact", "Open evidence", "Start bounded task", "Reindex"). This slice
   implements graph/status/testing and the worktree/test-plan commands only;
   actions follow the reviewed action layer.
6. **Research UI.** The `researchView` RPC exists and is read-only, but no GUI
   tab renders it yet. It is available for a later view.
7. **Node version.** Vite 7 warns on Node 20.5.1 and recommends 20.19+/22.12+.
   The build succeeds; use a supported Node when possible.
8. **Output schema.** The envelope uses a permissive JSON-object schema by
   design, so the host validates transport shape but not every CLI payload
   field. The GUI narrows defensively and renders section errors instead of
   assuming a report.

## Related documentation

- Official API: [plugins overview](https://opencode.ai/docs/build/plugins/),
  [GUI](https://opencode.ai/docs/build/plugins/gui/),
  [RPC](https://opencode.ai/docs/build/plugins/rpc/),
  [server](https://opencode.ai/docs/build/plugins/server/).
- ozagia: [status](status.md), [view](view.md), [worktree](worktree.md),
  [resources](resources.md), [testing](testing.md), [research UI](research_ui.md).
