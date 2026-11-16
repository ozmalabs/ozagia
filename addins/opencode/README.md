# ozagia OpenCode add-in

Read-only graph, status, testing, research, settings, action-preview and
task-class catalogue surfaces for OpenCode Desktop. This is the first
interactive UI host for ozagia (plan item 8): the add-in talks to the local
`ozagia` CLIs over a typed RPC and never imports Python, applies an action,
runs a bounded task or edits the repository.

## Requirements

- OpenCode Desktop with the experimental GUI plugin API and the plugin server
  part (`@opencode/plugin` 2.3.x is what this package is built and typed
  against). Browser-tab support depends on the host version; OpenCode Desktop
  loads GUI parts from the plugin in global `opencode.json(c)`.
- Python 3.12+ with `ozagia` importable by the configured interpreter. This
  worktree is not installed on `sys.path` by default, so point `python` at a
  virtual environment that has the package installed, or set `PYTHONPATH`
  before starting OpenCode.
- A graph snapshot (`.ozagia/graph.json`) for the graph panel. The panel does
  not build one; a missing graph is reported as an actionable error.
- A recorded research investigation under `.ozagia/research/` for the Research
  tab. A missing investigation is reported as an error, not an empty view.
- Optional user/repository configuration for the Settings page; with no files it
  shows the built-in defaults and their source. The Actions tab needs no extra
  file unless the selected action targets a configured service or tunnel.
- The Tasks tab reads the task-class library that ships with the installed
  `ozagia` package and any `<root>/ozagia/tasks/` overrides. No extra file is
  needed; an invalid override is reported in the validation card, not hidden.
- Node 20.19+ or 22.12+ to build the GUI bundle with Vite 7. Node 20.5 builds
  successfully but prints Vite's engine warning.

## Install (local absolute path)

Add one plugin entry to your **global** OpenCode config (`opencode.json` or
`opencode.jsonc`). Use the absolute path to this directory:

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

Options:

| Option | Default | Meaning |
| --- | --- | --- |
| `python` | `python3` | Interpreter with `ozagia` importable. Relative paths resolve from `root`. |
| `root` | the plugin location directory | Repository root passed as `--root`. Relative paths resolve against the plugin location. |
| `timeoutMs` | `20000` | Per-call wall-clock limit, clamped to 1000–120000 ms. |

The object form uses the same local-path resolution as the string entry.

The plugin id is `ozagia-addin` from the server part. The GUI declares no id and
takes that one. OpenCode reloads the plugin when its `options` change; a restart
also works. The server entry does not hot-reload on source edits.

If you only want to experiment without options, the equivalent string entry is:

```jsonc
"plugins": ["/home/matt/ozmadev/ozagia/addins/opencode"]
```

## What it shows

One side panel, `ozagia`, with up to six tabs:

- **Graph** (pinned): aperture picker loaded from `view_cli --list-apertures`,
  refresh, node/edge/group counts, deterministic seeded circle layout in SVG,
  node inspector, node table, groups/legend, snapshot metadata, provenance and
  omitted counts. A missing or invalid graph shows an error with the CLI's
  message, not an empty view.
- **Status**: workspace/worktree rows with branch, expected branch, ahead/behind
  and dirty counts; worktree findings; memory, disk, load and admission policy;
  process groups; file-ledger counts; daemon state/stats; graph snapshot state;
  runtime and configuration summaries.
- **Testing**: base, head, task-class and changed-file inputs; a Plan button;
  mode, counts, selected tests, omissions, fallback, graph freshness and the
  plan's explicit "not assessed" list.
- **Research**: investigation id and a six-view picker (`library`, `fetch`,
  `screening`, `evidence`, `consensus`, `citation`); summary counts, the primary
  row table, provenance and record counts, omitted rows/graph material and the
  explicit "not assessed" list. A missing investigation or graph is an
  actionable error, never an empty successful view.
- **Actions**: the action catalogue with `read-only`, `mutating`, `destructive`
  and `live-gated` badges, action-specific arguments, and a Preview button. The
  preview shows the exact operation, manager call, argv, preconditions, risks,
  current state and one-time confirmation token. There is no Apply button.
- **Tasks**: the versioned task-class catalogue with a domain filter, library
  validation status (counts, errors, warnings and issues), and a detail view for
  the selected class. The detail shows title, description, version, source,
  inputs, tools, write scope, budget, resources, worktree expectations, before
  and after gates, stop conditions, approval policy, output and every step. A
  read-only **Copy command** affordance copies a `tasks_cli run --dry-run`
  preview command with input placeholders. The tab never runs a task, applies an
  approval or writes a file.

A `SettingsPage` also contributes an **ozagia settings** page under Settings.
It reads `settings_cli schema` and `settings_cli values` and shows every key's
value, source (`default`, `user`, `repo`, `env` or `override`), scope, type,
default, description and restart flag. It uses the host's
`SettingsLayout`/`SettingsSection`/`SettingsRow` components and has no write
control.

Commands in the palette:

- `ozagia: Open graph panel`
- `ozagia: Open status panel`
- `ozagia: Open testing panel`
- `ozagia: Open research panel`
- `ozagia: Open action previews`
- `ozagia: Open task classes`
- `ozagia: Open settings page`
- `ozagia: Check worktree` (runs the read-only check and opens status)
- `ozagia: Plan tests` (plans HEAD against the working tree and opens testing)

The status, testing, research, actions and tasks tabs are opened by their
commands; the graph tab is pinned so the panel is discoverable. The settings
command opens the contributed settings page directly. All actions are read-only.

## Read-only scope

This slice has no commits, edits, service starts/stops, downloads, index
builds, test runs, task runs, setting writes or model calls. It only invokes:

```text
python -m ozagia.status_cli
python -m ozagia.view_cli
python -m ozagia.worktree_cli
python -m ozagia.resources_cli
python -m ozagia.testing_cli
python -m ozagia.research_ui_cli
python -m ozagia.settings_cli schema|values
python -m ozagia.actions_cli list|preview
python -m ozagia.task_library_cli list|show|validate|catalog
```

The settings page only uses `schema` and `values`; it never calls `settings_cli
set`. The actions tab only uses `list` and `preview`; it never calls
`actions_cli apply` and has no mutating RPC. A preview issues a one-time
confirmation token and records it under `.ozagia/actions/pending` (mode `0600`,
ten-minute TTL); it does not start, stop, signal or connect anything. Applying
an action remains a separate, explicit CLI command after a person reviews the
preview.

The Tasks tab uses `task_library_cli list`, `show` and `validate`; the RPC also
exposes `catalog` for a client that prefers one object. The tab never calls
`task_library_cli copy`, `tasks_cli run` or `tasks_cli run --allow-exec`. The
copied command is display text with `--dry-run` and no `--allow-exec`; this
add-in does not execute it, approve a mutating step or write to
`ozagia/tasks/`.

The subprocesses use `spawn` with argument arrays, no shell, closed stdin,
a bounded timeout and a bounded output buffer. Stdout is parsed as JSON; stderr
is capped, redacted and returned as a short tail. A CLI exit status of 1 is
treated as a successful report because these CLIs use it for findings, policy
failures and admission refusals while still emitting JSON.

The RPC contract exposes `status`, `apertures`, `view`, `worktreeCheck`,
`policyCheck`, `resourcesCheck`, `testingPlan`, `researchView`,
`settingsSchema`, `settingsValues`, `tasksList`, `tasksShow`, `tasksValidate`,
`tasksCatalog`, `actionsList` and `actionPreview`. There is no `actionApply`,
task run, settings write or other mutating method. Action input is limited to
the published action names and each action's documented arguments; names are
bounded, byte caps and timeouts are range-checked, and no value is passed
through a shell. A task id is bounded to 64 characters and must match the
library's `[A-Za-z][A-Za-z0-9_.-]{0,63}` form before it reaches argv.

## Build and test

```bash
cd addins/opencode
npm install --no-audit --no-fund
npm run typecheck
npm test
npm run build
```

`npm run build` uses the documented preset:

```ts
import { defineConfig } from "vite"
import { gui } from "@opencode/plugin/gui/vite"

export default defineConfig({ plugins: [gui()] })
```

and writes one CommonJS file, `dist/gui-experimental.cjs`, which is committed so
a local plugin directory loads without a build step. `node_modules/` is ignored;
`package-lock.json` and `dist/` are committed.

The Node tests (`tests/*.test.cjs`) cover the pure argument builders, input
bounds, task-id validation, the copy-command builder, action argument/action
matching, redaction and the subprocess wrapper. They are offline and do not
load `@opencode/plugin`; a small helper transpiles `src/runner.ts` and the pure
task-command module with the installed TypeScript package.

## Package layout

```text
package.json          exports "." "./rpc" "./gui-experimental"
vite.config.ts        gui() preset
tsconfig.json
src/rpc.ts            Rpc.define contract (ozagia)
src/index.ts          server part: Plugin.define + ctx.rpc.register
src/runner.ts         argument builders + bounded subprocess runner
src/gui.tsx           GUI Plugin.define, Panel/SettingsPage/Command/Style
src/gui/              Solid components (graph, status, testing, research,
                      actions, tasks, settings), task-command.ts
src/types.ts          envelope and read-only data shapes
tests/                node:test for runner/argument builders
dist/gui-experimental.cjs
README.md
```

See [docs/opencode_addin.md](../../docs/opencode_addin.md) for the design,
method mapping, safety rules and known API gaps.
