# ozagia OpenCode add-in

Read-only graph, status and test-planning panels for OpenCode Desktop. This is
the first interactive UI host for ozagia (plan item 8): the add-in talks to the
local `ozagia` CLIs over a typed RPC and never imports Python or edits the
repository.

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

One side panel, `ozagia`, with up to three tabs:

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

Commands in the palette:

- `ozagia: Open graph panel`
- `ozagia: Open status panel`
- `ozagia: Open testing panel`
- `ozagia: Check worktree` (runs the read-only check and opens status)
- `ozagia: Plan tests` (plans HEAD against the working tree and opens testing)

The status and testing tabs are opened by their commands; the graph tab is
pinned so the panel is discoverable. All actions are read-only.

## Read-only scope

This slice has no commits, edits, service starts/stops, downloads, index
builds, test runs or model calls. It only invokes:

```text
python -m ozagia.status_cli
python -m ozagia.view_cli
python -m ozagia.worktree_cli
python -m ozagia.resources_cli
python -m ozagia.testing_cli
python -m ozagia.research_ui_cli
```

The subprocesses use `spawn` with argument arrays, no shell, closed stdin,
a bounded timeout and a bounded output buffer. Stdout is parsed as JSON; stderr
is capped, redacted and returned as a short tail. A CLI exit status of 1 is
treated as a successful report because these CLIs use it for findings, policy
failures and admission refusals while still emitting JSON.

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
bounds, redaction and the subprocess wrapper. They are offline and do not load
`@opencode/plugin`; a small helper transpiles `src/runner.ts` with the installed
TypeScript package.

## Package layout

```text
package.json          exports "." "./rpc" "./gui-experimental"
vite.config.ts        gui() preset
tsconfig.json
src/rpc.ts            Rpc.define contract (ozagia)
src/index.ts          server part: Plugin.define + ctx.rpc.register
src/runner.ts         argument builders + bounded subprocess runner
src/gui.tsx           GUI Plugin.define, Panel/Command/Style registrations
src/gui/              Solid components (graph, status, testing)
src/types.ts          envelope and read-only data shapes
tests/                node:test for runner/argument builders
dist/gui-experimental.cjs
README.md
```

See [docs/opencode_addin.md](../../docs/opencode_addin.md) for the design,
method mapping, safety rules and known API gaps.
