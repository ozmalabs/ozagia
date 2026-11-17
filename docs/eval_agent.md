# Agent end-to-end evaluation (W3-C)

Status: harness shipped. The `opencode` driver has run live (the 14-session pilot in
`evaluation/agent_e2e/pilot_opencode.md`). The `claude` driver is untested live.

Code: `src/ozagia/eval/agent.py`. Tasks: `evaluation/agent_e2e/tasks.json`.
Tests: `tests/test_agent_eval_migrated.py`. Plan: `docs/features/DEV_TOOLING_V2_PLAN.md`, item V6.

## Why

Retrieval metrics (recall at a token budget) measure whether the right files
appear in a prompt. They do not measure whether the agent fixed the problem.
The studies cited in the plan (arXiv 2611.07286, 2610.33958) report that better
retrieval often leaves task success unchanged. `ocelot_repo_map_eval` covers the
retrieval half. This harness covers the other half: given a symptom, does the
agent make a failing test pass again?

## Task format

`evaluation/agent_e2e/tasks.json`:

```json
{
  "version": 1,
  "base": "<commit sha>",
  "tasks": [
    {
      "id": "kebab-case-id",
      "prompt": "Symptom-style description the agent receives.",
      "area": "scripts",
      "break_patch": "patches/kebab-case-id.patch",
      "check": {"argv": ["-m", "pytest", "-q", "-p", "no:cacheprovider",
                         "--confcutdir={workdir}", "tests/x_migrated.py::test_y"],
                "timeout_s": 60},
      "expected_files": ["scripts/x.py"],
      "notes": "What the break does (for reviewers, not the agent)."
    }
  ]
}
```

- `break_patch` is a unified diff relative to `base`. It must apply with `git apply`.
- `check` is a pytest argv without the interpreter. The harness runs it with the
  chosen Python in the workdir. `{workdir}` is replaced by the workdir path.
- The check must **pass on base and fail after the break**. `validate` enforces this.
  A break the check does not catch is rejected.
- `prompt` describes the missing behaviour the way a user would. It must not name the
  file to edit. It may name the failing test.
- `expected_files` are the files a correct fix touches. They are used only for the
  localization metric.

### Adding a task

1. Pick a small behaviour with a fast existing test (under 60 s). Do not add new tests
   to the base.
2. Write the break as a patch, from a file at `base`. Keep it to one function body or a
   few lines. Generate the patch with `diff -u` (or `git diff`) against a clean copy,
   and check that the header paths are `a/...` and `b/...`.
3. Add the entry to `tasks.json`, then run
   `python -m ozagia.eval.agent validate --task-ids <id>`.
4. Run the fake driver on that task to confirm the pipeline end to end.

## Harness flow

For each task and arm, `run_eval` does the following:

1. Creates a workdir at `base` under `<run_root>/work/<task>-<arm>-r<n>`: a detached
   linked worktree for the local drivers, an isolated clone for live drivers (see
   Isolation).
2. Applies the break patch.
3. Writes the arm's MCP config into the workdir, if the arm needs one.
4. For dev_memory, runs prep (see Arms). Prep time is recorded as `prep_s`.
5. Snapshots the content hash of every modified, deleted or untracked file.
6. Runs the driver with the prompt. The driver's `run` returns exit code, wall time,
   tokens, cost, transcript and tool calls. Unknown values are `None`.
7. Snapshots again. `files_changed` lists paths whose content hash changed in step 6.
   The break itself does not count as an agent edit.
8. Runs the check. `success` means the check passed.
9. `localized` is true if any `expected_files` entry is in `files_changed`.
10. Removes the workdir, unless `--keep`. Cleanup problems are listed in
    `cleanup_warnings` on the run record.
11. Computes the run's `timing` from the record and its logs (see Time per
    component).

Logs (driver stdout and stderr, check output, transcripts, the dev-memory snapshot and
journal) stay in `<run_root>/logs/`. Only workdirs are removed.

`validate` runs check, then break, then check on a worktree at `base`. It reports
`ok` only when the check passes on base, the patch applies, and the check fails
after the break.

## Drivers

| name | what it does | live |
|---|---|---|
| `noop` | Does nothing. Negative control. Expected success 0%. | no |
| `fake` | Reverts the break with `git apply -R`. Expected success 100%. Checks the harness only. | no |
| `claude` | `claude -p <prompt> --output-format json --permission-mode acceptEdits --no-session-persistence --allowedTools Read,Edit,Write,Glob,Grep,Bash --disallowedTools "Bash(git commit:*),Bash(git push:*),Bash(git stash:*),Bash(git branch:*),Bash(git checkout:*),Bash(git switch:*),Bash(git reset:*),Bash(git rebase:*),Bash(git merge:*),Bash(git tag:*),Bash(git worktree:*)"`, run in the workdir. | **untested live** |
| `opencode` | `opencode run --standalone --format json --auto --archived [-m <model>] <prompt>`, run in the workdir with a generated `OPENCODE_CONFIG` (see OpenCode). | yes |
| `native` | ozagia's own agent loop, `python -m ozagia.harness.native run`, against an OpenAI-compatible endpoint (see Native, and `docs/native_loop.md`). Arms `baseline` and `graph_intent` only. | yes (tested against a local stub server only) |

Token and cost fields for `claude` come from its JSON output (`usage.input_tokens`,
`usage.output_tokens`, `total_cost_usd`, `result`). Those names were confirmed in the
installed binary (claude 2.1.322), not in a live run. `usage.input_tokens` may
exclude cache-read tokens. For `opencode` they are summed over the `step_finish`
events of the JSON event stream (`cost`, `tokens.input`, `tokens.output`), and
`tool_calls` counts `tool_use` events. An `error` event, or a stream with no finished
step, is recorded as `driver_error`. Confirmed against opencode 2.3.0.

`tool_calls` and `tools_by_name` count OpenCode's tool events. `mcp_calls` counts calls
to the Ocelot server. OpenCode 2.3 runs MCP tools through Code Mode: the agent calls a
single `execute` tool whose code calls `tools["ozagia"].<tool>(...)`, so
`mcp_calls` counts those calls inside the code (and any tool named
`ocelot-dev-memory*`). The report gives mean tool calls and mean MCP calls per arm.
A dev_memory session that makes no MCP call is a session where the server was
available and unused.

### Selecting the backend

`--driver` picks the backend. Without it, the harness reads
`OZAGIA_EVAL_DRIVER`, so `OZAGIA_EVAL_DRIVER=opencode` switches every run
in a shell. With neither, the CLI refuses. `--model` passes a model through
(`-m provider/model` for opencode, `--model` for claude). Without `--model`, each CLI
uses its own default. For opencode that is the `model` key of
`~/.config/opencode/opencode.json`, which the report records. The report and the
Markdown summary carry a `Backend` line (driver and model) and the run's total cost.

Before the first workdir is made, the driver runs a preflight that makes no model
call. `claude` checks `claude auth status` (or an API key in the environment) and
refuses when logged out. `opencode` runs `opencode models` and, with `--model`,
refuses a model it does not list. During a run, an authentication error from the
driver stops the run.

### OpenCode

- `--standalone` starts a private server for each session, so a background OpenCode
  service and its cached project state are not used.
- `OPENCODE_CONFIG` points at `<log_dir>/opencode_config.json`, written per session.
  Both arms get `permission.bash` with `"*": "allow"` and `deny` for the ref-moving
  git commands (`git commit*`, `push*`, `stash*`, `branch*`, `checkout*`, `switch*`,
  `reset*`, `rebase*`, `merge*`, `tag*`, `worktree*`). The dev_memory arm also gets
  the Ocelot MCP server, copied from the workdir's `.ozagia_eval_opencode.json`.
  The arms differ only in that server.
- The user's global OpenCode config still loads (providers, the default model, any
  global MCP servers). The baseline is "the operator's OpenCode without Ocelot", not a
  bare agent.

### Native

The `native` driver runs ozagia's agent loop (`src/ozagia/harness/native.py`)
as a subprocess, through `spawn_group`, with the run's `python` shim first on
`PATH`. It is configured from the environment:

| variable | meaning |
|---|---|
| `OZAGIA_NATIVE_BASE_URL` | API root, e.g. `http://127.0.0.1:18020/v1` (required) |
| `OZAGIA_NATIVE_MODEL` | model id; `--model` overrides it (one of the two is required) |
| `OZAGIA_NATIVE_API_KEY_FILE` | file holding the API key, passed as `--api-key-file` |
| `OZAGIA_NATIVE_API_KEY_ENV` | name of the variable holding the key, passed as `--api-key-env` |

The key is never on the command line: the loop reads it from the file or the
named variable. Arms map to the loop's modes: `baseline` is `--mode plain`
(built-in tools only) and `graph_intent` is `--mode graph --dump
<log_dir>/dev_graph.json` (the snapshot prep built). The loop calls the intent
layer in its own process, so for this driver `run_one` starts no daemon and
installs no plugin (`NativeDriver.uses_daemon = False`); it passes
`OZAGIA_CONFIG` so the loop's graph uses the layout prep used. `dev_memory` is
refused before any workdir is made: the loop has no MCP client.

The loop gets `--timeout <timeout-s>` and stops itself; its process group is
killed `NATIVE_GRACE_S` (30 s) later if it is still running. The driver reads the
loop's `summary.json` (stdout, else `<log_dir>/native/summary.json`): `tokens_in`,
`tokens_out`, `cost_usd` (only when the server reports a cost), `tool_calls`,
`tools_by_name`, and for graph_intent `captured_calls` (calls the intent layer
handled), `passthrough_calls` (calls it left to the built-in by design) and
`fallbacks` (calls where the intent layer raised, so the built-in ran). The two
modes apply the same ref-moving git refusal; a captured pytest in graph mode gets
the plain shell's default timeout (120 s), is clamped to the run deadline, and
runs with the agent shell's environment (no `PYTHONPATH`, no API key, no
`OZAGIA_NATIVE_*`). Graph mode writes the snapshot once, at the end of the loop.
The run record also gets
`stopped` (`done`, `max_steps`, `timeout`, `error`) and `steps` (model turns). A
loop that stops with `error`, or writes no summary, sets `driver_error`. The
loop's `transcript.jsonl` and `summary.json` stay in `<log_dir>/native/`.

Preflight: `GET {base_url}/models` must list the model. It makes no completion
request.

### Isolation

Live drivers run in a clone made with `git clone --shared --no-checkout`, then
`git checkout --detach <base>` and `git remote remove origin`. On this repository a
clone takes about 11 s and 3.4 GB of working tree. A clone has its own refs and no
remote, so a ref the agent moves is not a ref of the operator's repository, and
OpenCode's worktree tools see no sibling worktrees. A shared clone still borrows
objects from the source repository through `.git/objects/info/alternates`; the agent
can read that history but cannot change it from the clone.

The first two OpenCode smoke runs worked in the operator's checkout instead of the
workdir and found the task's tests passing there. The cause was `$PWD`: OpenCode
takes its project directory from `$PWD`, not from the process's working directory,
and the harness passed the operator's `PWD` through. `spawn_group` now sets `PWD` to
the child's cwd for every driver and check. The same runs found no `python` on
`PATH`, so live drivers get `<log_dir>/bin` first on `PATH`, holding `python` and
`python3` links to the run's `--python`.

The `fake` and `noop` drivers ignore the arm. They do not start an MCP server.

### Live gate

`claude`, `opencode` and `native` refuse to run unless **both** of these hold:

- the command includes `--live`, and
- the environment has `OZAGIA_EVAL_LIVE=1`.

The refusal happens before any worktree is created. The CLI exits with status 2. The
driver also checks the environment variable again when it runs.

### Process groups and timeouts

Every driver and check runs through `spawn_group`. The child starts in its own session
(`start_new_session=True`), so its process group holds everything it starts: MCP
servers, language servers, shell children. On timeout the harness sends SIGTERM to the
group, waits up to 5 s for it to exit, and sends SIGKILL to the group. The run is
recorded with `timed_out: true` and scores as a failure. Before this change only the
direct child was killed, and an MCP server or sleeping child could keep running against
a worktree that was about to be deleted.

### Cleanup

Worktrees are removed by `git worktree remove --force <path>`, and only for paths under
`<run_root>/work`. The harness no longer runs a repo-wide `git worktree prune`. That
prune also dropped the admin entry of any unrelated worktree whose directory was
missing, for example one on an unmounted volume.

If `git worktree remove --force` fails (for example, the worktree is locked), the
harness deletes the directory. It then reads `<path>/.git` before deletion. If the
admin directory it points to is under `<common git dir>/worktrees/`, the harness
deletes only that directory. Each partial step is recorded in `cleanup_warnings`.

### The git restriction

Live drivers work in clones (see Isolation), so moving a ref no longer reaches the
operator's repository. The ref-moving git commands are still denied, so both
backends work under the same rules and an agent cannot hide its fix in a commit or a
stash. The `claude` driver passes `--disallowedTools` for these Bash patterns:
`git commit`, `push`, `stash`, `branch`, `checkout`, `switch`, `reset`, `rebase`,
`merge`, `tag` and `worktree`. The `opencode` driver denies the same commands through
its permission config. Other shell commands stay allowed.

Residual risk for `claude`: the patterns match the start of a Bash command. A command
such as `git -C <dir> commit`, `git --git-dir=... push`, `sh -c 'git commit'`, a full
path to git, or a script that runs git can get past them. OpenCode's patterns are
also prefix globs and have the same gaps. The restriction limits accidents. It is not
a sandbox: the agent runs as the operator and can reach any path by absolute name.

## Arms

| arm | configuration |
|---|---|
| `baseline` | No Ocelot MCP server. Claude Code runs with `--strict-mcp-config`, which ignores the user's MCP servers. OpenCode gets the generated config without the Ocelot server; a user's global MCP servers can still load in both arms. |
| `dev_memory` | The workdir gets `.ozagia_eval_mcp.json` (Claude Code, passed with `--mcp-config`) and `.ozagia_eval_opencode.json` (OpenCode, whose `mcp` block is copied into the generated `OPENCODE_CONFIG`). Both start `<python> -m ozagia.integrations.mcp.server` with `PYTHONPATH` and `OZAGIA_ROOT` set to the workdir. The snapshot and journal go under the run's log directory. `OZAGIA_AUTOBUILD=0`, because the harness builds the snapshot before the driver starts (see Prep). |
| `graph_intent` | No MCP server. dev_memory's prep, then the graph daemon for the session and the ozagia OpenCode plugin in `<workdir>/.opencode/plugins/`, which hands the built-in file and shell tools to the daemon. `opencode`, `fake` and `noop` drivers. The `native` driver runs this arm as its loop's graph mode, with prep but without the daemon and plugin. See below. |

The two config files are untracked and are written before the snapshot. They are
not counted as agent edits. The agent can still see them with `git status`.

### Prep (dev_memory and graph_intent, outside the timer)

Before the driver starts, the dev_memory arm builds the dev-memory graph snapshot
(`build_index`, written to the run's log directory) and warms the tag cache
(`update_tag_cache`, written to `<workdir>/.ozagia/tags.json`). Both run in a
subprocess with the run's Python and `PYTHONPATH=<workdir>`, so they describe the
broken worktree. `var/` is gitignored, so the cache does not count as an agent edit.
The change snapshot is taken after prep.

Prep time is recorded as `prep_s` on each run and reported as "mean prep s" per arm.
The driver's `wall_s` does not include it. The baseline arm has `prep_s = 0`.
In the per-component report prep is the `index` component, cold once per
revision, with the repeat cost of a later session measured apart (see Time per
component).
Before this change the snapshot build (about 100 s on this tree) ran inside the
session's wall time, so dev_memory sessions were timed unfairly.

Prep is not a gate. If it fails, the run records `prep_ok: false` and `prep_error`,
the driver still runs, and the check still decides. The report counts "prep failures"
per arm, so a broken index shows up as a result and is not hidden.

### graph_intent (graph daemon and the OpenCode plugin)

The third arm keeps the agent's built-in tools and changes what they do. The
OpenCode plugin (`src/ozagia/integrations/opencode/plugin.js`) replaces `read`,
`grep`, `glob`, `edit`, `write` and `shell` and sends each call to the graph
daemon (`python -m ozagia.daemon serve`, protocol in
`src/ozagia/daemon/protocol.py`). The agent's tool list is the baseline's: no MCP
server is registered, and the generated `OPENCODE_CONFIG` is the baseline's.

Drivers: `opencode`, and `fake` and `noop` for testing the pipeline without a
model. `claude` with this arm is refused before any workdir is made. Without
`--arms`, the CLI runs every arm the driver supports.

Per run, in order:

1. The workdir and prep are the dev_memory arm's (`prepare_dev_memory`).
2. The daemon starts in its own process group: `<tool python> -m ozagia.daemon
   serve --root <workdir> --dump <log_dir>/dev_graph.json --journal
   <log_dir>/daemon_journal.jsonl --sock <tmp>/ozagia-eval-<random>.sock`, with
   `OZAGIA_ROOT` and `OZAGIA_CONFIG` as for prep. The harness waits up to
   `DAEMON_READY_TIMEOUT_S` (120 s) for its `ready <socket>` line. Its stdout and
   stderr go to `daemon.stdout.txt` and `daemon.stderr.txt` in the log dir. A daemon
   that exits or is not ready in time is killed and recorded (`daemon_ok: false`,
   `daemon_error`); the run goes on, and the plugin then falls back to the built-ins
   on every call.
3. The plugin is written to `<workdir>/.opencode/plugins/ozagia.js`, before the
   "before" snapshot. `_snapshot` never lists that file (nor the state dir), so it
   is not an agent edit even in a repository that does not ignore `.opencode/`.
4. The driver runs with `OZAGIA_DAEMON_SOCK=<socket>`. The plugin does nothing
   without that variable.
5. After the "after" snapshot the harness sends `stats` and `shutdown`, kills the
   daemon's process group if anything in it is still alive, removes the socket,
   and removes the plugin file. The check then runs as for the other arms.

Run record fields for this arm:

| field | meaning |
|---|---|
| `daemon_ok`, `daemon_error`, `daemon_ready_s` | daemon start, and seconds to its `ready` line |
| `daemon_stats` | the daemon's `stats` response as is (counts by tool, intent and handled, passthrough, expired, plugin fallbacks, fallbacks, hellos, latency) |
| `daemon_stop_error` | `stats` could not be read at the end |
| `plugin_loaded` | `stats.hellos > 0`: the plugin connected at least once. `None` without stats |
| `captured_calls` | tool calls the daemon handled (`stats.handled.true`; a request that expired before it ran is never handled) |
| `passthrough_calls` | `stats.passthrough`: tool calls the daemon left to the built-in by design (not mapped), not counting expired ones |
| `preflights` | `stats.preflights`: edit, write and shell calls the daemon would carry out and for which the plugin asked OpenCode's permission first (`docs/intent.md`, Permissions) |
| `permission_assert` | whether the plugin found `ctx.permission.assert` (from its `hello`) |
| `codemode_tools` | replaced tools whose `options.codemode` is not `false`, so Code Mode's `execute` could call their built-in (from the plugin's `hello`; empty on 2.3.0, where every built-in is `codemode: false`). The report warns when any run has one. |
| `structured_queries`, `structured_outlines`, `jq_captures` | `stats.structured_queries` (`json:`/`yaml:`/`toml:` reads), `stats.structured_outlines` (outlines served for large structured files) and `stats.jq_captures` (captured `jq` commands); the summary adds `total_structured_queries`, `total_structured_outlines` and `total_jq_captures` per arm (`docs/intent.md`, read and shell) |
| `repeats_seen`, `stubs_served`, `notes_served`, `outlines_served`, `bytes_saved` | the daemon's read-ledger counters (`stats`, `ozagia.intent.ledger`): repeats of an unchanged range, how many got a stub or a note, outline-first reads served, and the estimated bytes kept out of context; the summary adds `total_<field>` per arm (`docs/intent.md`, read) |
| `jobs_started`, `job_waits`, `job_wait_seconds`, `polls_avoided` | `stats.jobs_started`, `stats.waits`, `stats.wait_seconds` and `stats.polls_avoided` (background jobs, `ozagia.daemon.jobs`); the summary adds `total_jobs_started`, `total_job_waits`, `total_job_wait_seconds` and `total_polls_avoided` per arm |
| `fallbacks` | `stats.fallbacks` = `plugin_fallbacks`: the plugin's `after` reports with `meta.fallback` (daemon error, timeout, `ok: false`, or an edit/write/shell whose outcome is unknown), one per call. `daemon_stats` also has `expired` (requests dropped by their `deadline_ms`) and failed `tool` requests, the daemon's view of some of the same calls. |

The per-arm summary adds `mean_captured_calls`, `mean_passthrough_calls`, `total_fallbacks`,
`plugin_load_failures` (runs that started a daemon and have `plugin_loaded` not
true, so `fake` and `noop` runs count, as they load no plugin; `native` runs start
no daemon and do not count) and `daemon_failures`. The Markdown report has
a "Graph daemon and plugin" table for arms with these fields.

Plugin behaviour, per replaced tool: the original `execute` is kept, the output
schema becomes the edit tool's string schema (each built-in validates its result
against its own schema), and the description gains one sentence (`read`: paths may
be node keys such as `sym:<file>::<name>`; all: lines starting with `[ozagia]` are
graph context). A call sends `tool`; a handled answer is returned as the tool's
output, or thrown as the tool's error when `is_error` is set. An unhandled call
runs the original, converts its result to text, sends `after` with that text, and
appends the daemon's `append`. Every request carries `deadline_ms` (now plus its
timeout: 30 s; a shell call gets its own `timeout` plus 30 s, 330 s by default);
the daemon drops a request whose deadline has passed without running it. When the
daemon cannot be reached, answers an error, or a read, grep or glob times out, the
original's output is returned unchanged and a best-effort `after` with
`meta.fallback` records it. When an `edit`, `write` or `shell` request was sent
and then failed or timed out, the original does not run (the daemon might still
run the request): the call fails with an "outcome unknown" error that tells the
model to re-read the file or check the command's effects, and `after` records
`meta.fallback = "unknown_outcome"`. Each request uses its own connection, so a
long captured pytest never holds up other calls; `hello` is sent once per process
on its own connection before the first request, so OpenCode never waits for the
daemon at startup.

Install and check by hand:

```
python -m ozagia.integrations.opencode install --workdir W
python -m ozagia.integrations.opencode check --workdir W
python -m ozagia.integrations.opencode uninstall --workdir W
```

`check` makes no model call. It runs `opencode plugin list` in W, which asks
OpenCode's background service. The service imports a directory's plugins
asynchronously after the first request for it: the first calls print "No plugins
found", later ones a table `ID VERSION SOURCE` with one row per plugin file, whose
ID is the module's `id` (`-` when the file did not import or has no plugin shape).
`opencode plugin list --standalone` printed "No plugins found" every time on 2.3.0,
so `check` polls the background service until the plugin's row appears or the
timeout passes. (A private server does load the plugin: `run --standalone` and the
TUI with `--standalone` both send its setup `hello`.) A row with
ID `ozagia` proves the file imported with the right shape; it does not run `setup`.
`check` also imports the plugin in Node and runs `setup` against a stub registry,
checking that the six tools were replaced and took the string schema. OpenCode
runs on Bun, so the Node test is a proxy.

## Running

Validate the task set (no models, creates worktrees):

```
PY=/path/to/ocelot/.venv/bin/python
$PY -m ozagia.eval.agent validate --python $PY \
    --out-json evaluation/agent_e2e/validation.json
```

Local dry runs (no models):

```
$PY -m ozagia.eval.agent run --driver fake --arms baseline,dev_memory \
    --python $PY --out-json evaluation/agent_e2e/dry_run_fake.json \
    --out-md evaluation/agent_e2e/dry_run_fake.md
$PY -m ozagia.eval.agent run --driver noop --arms baseline \
    --python $PY --out-json evaluation/agent_e2e/dry_run_noop.json \
    --out-md evaluation/agent_e2e/dry_run_noop.md
```

## Time per component

Total wall time is the wrong measure for comparing arms. An arm that saves 30 s
of agent time per task and spends 35 s once on an index is faster from the
second task on the same revision. Each run therefore gets a `timing` block
(`src/ozagia/eval/timing.py`), and the report shows the components per arm, a
per-task time without setup and index, and an amortized time.

| component | what it is | source | kind |
|---|---|---|---|
| setup | workdir creation, break patch and arm config (`setup_workdir_s`); plugin install (`plugin_install_s`); daemon start to `ready`, including the snapshot load (`daemon_ready_s`) | harness timers | per session |
| index | prep (`build_index` and `update_tag_cache`), cold (`prep_s`); `repeat_s`: what a later session on the same revision pays instead (`prep_repeat_s`, below) | harness timers | cold once per revision, repeat per later session |
| model | per step, the wait from the previous step's end (its `step_finish` or last tool end) to the step's first tool start, or to its `step_finish` when it calls no tool, plus the step's last tool end to its `step_finish`; prefill and generation together | driver events | per task |
| tools | the union of the tool calls' `state.time` intervals; per tool name the split below | driver events, daemon journal | per task |
| other | driver wall minus model, tools and timeout tail, never below 0 | difference | per task |
| timeout tail | for a run killed at its timeout, the time after its last event (`timeout_tail_s`) | difference | per task |
| check | the task check (`check_s`) | harness timer | not agent time |

`per_task_s` = model + tools + other + timeout tail, which is the driver wall
unless the driver's clock runs past it. Every eval task is its own OpenCode
session, with its own workdir, plugin install and daemon start, so setup is a
per-session cost and is not amortized. The amortized time per task for N tasks
(sessions) on one revision is

    (index cold + (N - 1) × index repeat) / N + setup + per task

with N from `--amortize N` (default: the number of tasks run). `pay_once_s` =
setup + index cold is what a first task pays on top of its per-task time. When a
run root has no repeat measurement (older runs, `--no-repeat-index`), the
repeat cost counts as 0 and the headline says the amortized time is a lower
bound. When no setup part is recorded (older runs), setup, pay-once and
amortized are blank (unknown), not 0. When only some parts are recorded, the
sum is shown with `*` and a footnote names the missing parts (a lower bound).
The report's headline (in the header list, once) compares per-task, model and
amortized time between arms, never the sum of everything.

The repeat index cost is measured once per run root, in the first run that
preps, right after prep and outside every timer (`measure_repeat_index` in
`agent.py`). It does what a later session on an indexed checkout does instead
of a build, as the daemon's `after_refresh` does: load the snapshot
(`DevMemory`), reingest the files changed since it (`pending_changes` +
`reingest`, none here since the tree is the one just indexed) and bring the tag
cache up to date (`update_tag_cache`). It runs on a copy of the run's snapshot,
which is removed afterwards, so the daemon's snapshot is not touched. Each step
is timed inside the subprocess (`prep_repeat_parts`: `load_s`, `refresh_s`,
`tags_s`, `pending`), and `prep_repeat_s` is their sum; output in
`prep_repeat.log`. On the Ocelot eval base (54e5eb5) it takes about 6.3 s
(load 2.1 s, refresh 1.6 s, tag cache 2.6 s) against 52.3 s for the cold prep.
A changed revision costs more than the unchanged-tree figure (reingest and tag
cache of the changed files: about 9 s for 3 changed domain files). The snapshot
load is also inside `daemon_ready_s`, so for graph_intent it is counted in both
setup and repeat. `--no-repeat-index` skips the measurement.

### Model time and its limits

OpenCode (`--format json`) prints `step_start` after it has the response
stream and has taken its per-step snapshot. In the live Ocelot runs the median
time from `step_start` to the first tool start was 3–4 ms in two log sets and
236 ms in a third, while the gap since the previous `step_finish` was seconds.
So the model time of a step starts at the previous step's end: `pre_step_s` is
the gap before `step_start`, `in_step_s` the rest up to the first tool. The
time from a step's last tool end to its `step_finish` (4–15 s per log set) is
also model time (`post_tool_s`): the turn closing after the tool results are
in. The three include OpenCode's per-step bookkeeping (its snapshot), which
cannot be separated. The first step has no observable start, so its wait is in
other. OpenCode reports no per-token timing, so prefill and generation are one
number (`split: "combined"`). Tokens per step: `input_uncached` is OpenCode's
`tokens.input` (which excludes cache reads), `input_cached` is
`tokens.cache.read`.

For graph_intent, model time also holds the plugin's wait for the daemon's
task hint before each model request (at most 6 s per hint). `model.hint_waited_s`
is that wait for the hints that started after the driver's first event (the
first hint, before the first request, is in other); `model_excl_hint_s` is model
time without it. The components table shows both, and the headline's model line
uses model without hint for arms with hints and names the hint wait.

The native loop's transcript has per-step request `ms` (non-streaming, so
also combined) and per-tool `ms` with `handled`; its tool time in built-in
includes ozagia's after-processing, which shares the timer. For `claude`,
model time is `duration_api_ms` of the JSON result, tools are unknown and
other includes them (untested live). `fake` and `noop` have no events: model,
tools and other are null and per task is the driver wall.

### Tool time split

The plugin writes up to three journal entries per call: a `*_preflight` entry
(permission request), the deciding `tool` entry (`handled` true or false) and,
for a call left to the built-in, an `after` entry (awaited inside the call).
`journal_calls` groups them into calls: a decision takes the oldest waiting
preflight of its tool, and an `after` entry joins the latest decided call. A
preflight that never gets a decision is a call refused at the permission step
(OpenCode's "Permission denied") and becomes a call of its own: one still
waiting when another preflight of the tool arrives more than 1 s later
(preflights within 1 s are parallel calls), or at the end of the journal.

Per tool name, the driver's calls are paired in start order with these journal
calls:

- in ozagia: preflight + decision `ms` of handled calls. This includes
  commands the daemon runs itself, such as a captured `pytest`.
- in built-in: a passed-through or refused call's duration minus its daemon
  `ms`.
- daemon overhead: preflight + decision + after `ms` of passed-through and
  refused calls (and after `ms` of handled ones). For passthrough shells this
  is mostly `after_refresh` (about 0.43 s per call on Ocelot), real time the
  baseline does not pay.
- plugin and IPC: a handled call's duration minus its daemon `ms` (socket,
  JSON, the permission check in OpenCode).

The pairing is trusted (`matched: true`) only when the counts agree and every
pair is plausible: the daemon's `ms` fit in the call's duration + 0.25 s, and
every journal `ts` of the call (whole seconds) lies within the call's seconds
± 1 s. A call still running at the timeout leaves a decision with no driver
event, and a lost preflight shifts every later pair; either fails one of these
checks. Then the row has `matched: false` and `mismatch` gives the reason
(`timing.tools.mismatches` per run): ozagia and overhead come from the journal
totals, built-in is the rest, plugin and IPC is unknown. The components table
lists the runs with an unpaired tool. Journal `ts` are the daemon host's local
time; a report made in another time zone shows up as a whole-quarter-hour
offset, which is removed (`tools.journal_offset_s`). The per-name values are
sums over calls; with parallel calls they can exceed the tools union. Without
a daemon (baseline, dev_memory) every tool is built-in time, except MCP tools
named `ozagia...`, which are ozagia time.

Other daemon ops are totalled in `timing.daemon_ops`. `other.hint_waited_s`
sums `min(ms, 6 s)` over all hints, and `hint_daemon_s` the daemon's full time.
A hint that runs past 6 s keeps the daemon busy and shows up as overhead on the
next calls.

### Clocks

Three clocks are involved: the harness's monotonic timer (driver wall, setup,
prep, check), OpenCode's `Date.now()` (event and tool times) and the daemon's
own timer (journal `ms`). Differences between them are clipped at 0 per call
and in other; when model + tools exceed the harness wall, other is 0 and
`other.clock_note` gives the excess. `other.startup_shutdown_s` is the wall
minus the event span. New runs record `driver_started_at` (system wall clock,
the same source as OpenCode's timestamps), which splits that into `head_s`
(before the first event: start-up, plugin load, the first hint and the first
request) and `tail_s` (after the last event: exit).

### Timed-out runs

A run killed at its timeout has `timed_out: true`, and the table counts them
per arm ("timed out"). The time after its last event is an unanswered request
or a tool still running (the journal of one such run shows a shell decided
after the driver's last event), so it goes to `timeout_tail_s` instead of
other. Its per-task time is the timeout. For runs without `driver_started_at`
(older runs) the head cannot be told apart, so the tail is the wall minus the
event span, start-up included (`other.timeout_tail_includes_head`, and the
headline says how many runs this applies to).

### Missing logs

Every component that cannot be computed is null, with the reason in
`timing.reasons`: no log dir, no `driver.stdout.txt`, no step events, logs that
are not this run's (below), no `daemon_journal.jsonl` for a daemon run (tools
then have durations but no split), or setup parts an older harness did not
record. Timing never fails a run.

### Re-reporting earlier runs

```
$PY -m ozagia.eval.agent report --in <run>.json [--logs-root <run root or logs dir>] \
    [--amortize N] --out-md <out>.md [--out-json <out>.json]
```

`report` recomputes every run's `timing` from the JSON and the logs and
rebuilds the summary and the markdown; the input file is not changed.
`--logs-root` points at where the logs are now, when the run root was moved
or reused: runs are found by name under `<logs-root>/logs/` or `<logs-root>/`.
Without it each run's recorded `log_dir` is read, which a later run may have
reused. So for OpenCode runs the logs must match the record: the number of
`tool_use` events against `tool_calls`, the uncached input tokens against
`tokens_in`, and, where recorded, the first event within the run's wall of
`driver_started_at`. A run whose logs do not match gets no model, tools or
other (`timing.log_mismatch`, `log_mismatches` in the JSON); `report` prints a
warning listing these runs and the markdown header names them. The repeat
index cost cannot be recomputed from logs; it is used only where a run
recorded `prep_repeat_s`.

## How a live run is approved and launched

A live run spends hosted-model tokens. The supervisor requests approval from the user
and states the exact command, the number of sessions and the cost ceiling. The
command only runs after approval:

```
OZAGIA_EVAL_DRIVER=opencode OZAGIA_EVAL_LIVE=1 \
  $PY -m ozagia.eval.agent run --live --arms baseline,dev_memory \
    --repeats 1 --max-total-usd <USD ceiling> --timeout-s 900 --python $PY \
    [--model provider/model] --out-json <out>.json --out-md <out>.md
```

For Claude Code, use `--driver claude` (or `OZAGIA_EVAL_DRIVER=claude`) and add
`--max-budget-usd <USD per session>`.

Size: sessions = tasks × arms × repeats. The shipped set has 7 tasks, so a pilot with
1 repeat and both arms is 14 sessions.

## Cost controls

- `--repeats`: keep at 1 for a pilot. Repeats are the only way to see variance.
- `--task-ids a,b`: run a subset.
- `--arms`: run one arm at a time.
- `--timeout-s`: wall-clock limit per session. A timed-out session scores as a
  failure, with `timed_out` recorded.
- `--max-budget-usd`: passed to `claude --max-budget-usd`, a per-session cap. Claude
  only.
- `--max-total-usd`: a run-wide cap for any backend. After each session the harness
  adds up the reported `cost_usd`; once the total exceeds the cap, no further session
  starts, and the report records `stopped`. It is a soft cap: the session that crosses
  it finishes. A backend that reports no cost counts as $0.

## Limits

- The break patches are authored by the task author. A break can be easier or harder
  than a real bug. The task set is small, so it does not measure general task success.
- Drivers' network access and tool permissions are not restricted by the harness. The
  agent runs as the operator with the workdir as its working directory.
- The checks use the Python passed in with `--python`, and the agent can run any shell
  command through `Bash`. A `Bash` call can change files outside the workdir.
- `PYTHONDONTWRITEBYTECODE=1` is set for checks and drivers. Without it, a break that
  keeps the byte length of a file, such as `a + b` to `a - b`, can run stale bytecode
  from the base check when the edit lands within the same mtime second.
- Results from `fake` and `noop` validate the harness. They say nothing about any agent.
