# The `ozagia` command and `ozagia agent`

`ozagia` is one command for the graph, the daemon, the integrations and an
OpenCode session with the graph in its path. Code: `src/ozagia/cli.py`,
`src/ozagia/harness/session.py`, `src/ozagia/harness/opencode.py`.

## Commands

| command | what it does |
|---|---|
| `ozagia index [--root R] [--rebuild] [--dump F] [--json]` | Builds the snapshot (`<state dir>/graph.json`) if missing; if present and stale, refreshes it. Prints the action (`built`, `refreshed`, `fresh`), node and edge counts and seconds. `--rebuild` builds from scratch. |
| `ozagia agent [options] [-- <opencode args>]` | An OpenCode session on the repository with the graph daemon and the plugin. See below. |
| `ozagia daemon serve\|ping\|stats\|shutdown ...` | `python -m ozagia.daemon`, arguments unchanged. |
| `ozagia bg run\|wait\|list\|log\|stop ...` | Background jobs in the daemon (`ozagia.daemon.jobcli`; see `ozagia.daemon.jobs`). Starts the daemon if none answers. |
| `ozagia mcp ...` | The MCP server (`ozagia.integrations.mcp.server`). |
| `ozagia eval ...` | The break/fix agent eval (`python -m ozagia.eval.agent`). |
| `ozagia check ...` | Run tests and report graph keys (`python -m ozagia.check`). |
| `ozagia install opencode\|claude [--workdir W] [...]` | Installs the OpenCode plugin or the Claude Code hooks. Other options (`--settings`, `--python`) go to the installer. |
| `ozagia --version` | The installed version. |

`--root` defaults to `$OZAGIA_ROOT`, else the git top level of the current
directory.

## `ozagia agent`

```bash
ozagia agent --prompt "make test_retry pass" -m vllm/qwen3.8-27b   # opencode run
ozagia agent --clone --base main~3 --prompt-file task.txt         # in a throwaway clone
ozagia agent                                                       # the TUI
```

| option | meaning |
|---|---|
| `--workdir W` / `--root W` | the repository (default as above) |
| `--prompt TEXT`, `--prompt-file F` | run non-interactively; the prompt is written to OpenCode's stdin, never argv; without either, the TUI |
| `--model M`, `-m M` | `provider/model`. `opencode run` gets `-m`; the TUI has no model flag, so it goes into the generated config as `model` |
| `--opencode-config PATH` | layer provider settings from an OpenCode JSON config into the generated harness config. Allowlist: `provider`, `providers`, `model`, `small_model` (`$schema` is allowed and ignored). `--model` wins over a config `model`. The file's plugins, MCP servers, permissions, agents and tools are never copied; any other key, invalid JSON or unreadable file is an error before OpenCode starts. See [Local providers](#local-providers-vllm). |
| `--clone [--base REV]` | run in an isolated clone at REV (default HEAD), kept afterwards |
| `--keep-plugin` | leave `.opencode/plugins/ozagia.js` in the workdir |
| `--timeout S` | kill the session after S seconds (exit 124) |
| `--python P` | `python` in the agent's shell commands (and the daemon's) is P |
| `--opencode EXE` | the opencode executable (default `opencode` on PATH) |
| `--runs-dir D` | where run roots go (default `<tmp>/ozagia_agent`) |
| `--repo-map` | send a bounded, task-ranked repository map in the first model request; off by default |
| `--repo-map-tokens N` | repo-map budget for `--repo-map`, 200..8000 (default 1500); needs `--repo-map` |
| `-- ARGS` | passed to opencode before the prompt |

Exit status: opencode's exit code; 124 after a timeout; 130 after Ctrl-C; 2
when the session cannot start (no opencode, bad arguments, snapshot or
daemon failure).

### What a session does

1. A run root `<runs dir>/<stamp>-<pid>-<hex>/` holds the logs (`logs/`) and,
   with `--clone`, the clone (`work/clone`).
2. `--clone`: `git clone --shared --no-checkout`, checkout of REV detached,
   `origin` removed (as the eval's live drivers). Uncommitted changes in the
   source are not in the clone. The summary prints `rm -rf <run root>` to
   remove it.
3. `/.opencode/plugins/ozagia.js` and `/.ozagia/` are added to the
   repository's `info/exclude` when missing (anchored at the workdir), so
   `git status` stays clean and no tracked file changes. At teardown the
   lines this run added are removed when their path is gone (the plugin's);
   the state dir's line stays while the state dir (with the snapshot) does.
4. `GraphSession` makes the snapshot current and starts the daemon (below).
   The interactive TUI does not wait for this: the snapshot build/refresh and
   the daemon start run in the background while OpenCode runs, and the phases
   go to `logs/launch_status.json` for the TUI sidebar. A non-interactive run
   waits for both before starting `opencode run`.
5. The plugin is written to `.opencode/plugins/ozagia.js`. A plugin file that
   was there before is overwritten unless git tracks it, and is not removed,
   except one whose content is exactly the packaged plugin: a run killed
   before its teardown left it, so it is removed like a new one.
6. `logs/opencode_config.json` becomes `OPENCODE_CONFIG`. The harness does not
   set a shell permission policy, so OpenCode applies the user's own approval
   rules. Ref-moving git commands are delegated to OpenCode's built-in shell;
   eval sessions keep their separate deny policy and the daemon's refusal.
   `--opencode-config PATH` adds only the allowlisted provider keys from the
   caller's file to this document (see below); the host/global config is not
   read implicitly.
7. OpenCode runs in the workdir with `OZAGIA_DAEMON_SOCK`, `PWD` (OpenCode
   takes its project directory from it) and `OPENCODE_CONFIG` added to this
   process's environment, and with `--python`, the python shim first on PATH.
   `OZAGIA_GIT_REFS_PASSTHROUGH=1` tells the plugin to let ref-moving git
   commands reach that built-in shell, where OpenCode's permission service
   remains authoritative. With `--repo-map`, `OZAGIA_REPO_MAP_TOKENS` carries
   the budget; without it the variable is absent from the OpenCode process's
   environment even when the caller's environment sets it.
   The TUI also gets `OZAGIA_LAUNCH_STATUS`, the status file its sidebar
   polls.
   - With a prompt: `opencode run --standalone --format json --auto --archived
     [-m M] [ARGS]` in its own process group, the prompt on stdin, stdout to
     `logs/opencode.stdout.jsonl`, stderr to `logs/opencode.stderr.txt`, text
     parts to `logs/transcript.txt`.
   - Without: `opencode --standalone [ARGS] <workdir>` on the terminal.
8. In `finally` (also on Ctrl-C and SIGTERM): the plugin and its exclude
   line are removed unless `--keep-plugin`, then the daemon is stopped.
   SIGINT is held off during teardown. After a hard kill (SIGKILL) no
   teardown runs: the daemon, started with `--parent-pid`, exits by itself
   within 2 s, and the next run removes the leftover plugin. A background
   preparation that has not reached the daemon is cancelled (its prep process
   is killed); the summary then says `graph: not ready (the session ended
   first)`.

### Prompt transport and argv logging

A non-interactive prompt is never an argument. `ozagia agent` writes it to
`opencode run`'s stdin through an anonymous, mode-0600 temporary file (no
pathname; it is closed after the session). `--prompt-file F` is read in the
ozagia process, and only its contents go to stdin: the path is not passed to
OpenCode either. This is deliberate. OpenCode writes its full argv to
`~/.local/share/opencode/log/opencode.log` once, before its plugin hook runs,
so a prompt or prompt-file path in argv leaks there no matter what the plugin
does later (observed in the live `--opencode-config` canary). For a
non-interactive run, the result's `argv` and the harness's own config and log
files never contain the prompt.

The TUI path is unchanged: a library caller that passes both `prompt=` and
`interactive=True` still gets `opencode --standalone [--prompt P] <workdir>`.
The `ozagia agent` CLI never takes that path (with a prompt it runs
`opencode run`; without one it opens the TUI with no initial prompt). The
TUI's `--prompt` handling and OpenCode Desktop prompt paths remain untested;
`ozagia agent` uses neither.

Interactive launch: the TUI starts at once and the sidebar (the
`ozagia-profile` TUI plugin) shows the phase until the snapshot is current and
the daemon answers:

```
Ozagia
Status: starting
Graph: checking · 0s
Daemon: waiting

Ozagia
Status: starting
Graph: building · 4s
Daemon: waiting

Ozagia
Status: online
Graph: fresh
Model: vllm/qwen3.8-27b
Session: 366 tools · 268 model calls
```

`Graph: building` (or `refreshing`) comes from the prep process, which says why
in the status document (`detail`); `failed` stays in the sidebar. While the
daemon is not up, a tool call falls back to OpenCode's built-in tool (the
plugin's normal degraded path), so send the first prompt after the line reads
`ready`. A fresh snapshot reaches `ready` in well under a second; a first
launch, or one after a change the refresh cannot reingest (a tracked file the
build does not index, such as a plugin's `.js`), rebuilds first.

A non-interactive run reports the synchronous phases on stderr, with a
ticking frame while a phase runs (plain lines when stderr is not a terminal):

```
graph: making the snapshot current for /path/repo (build=auto) -
graph: built (13746 nodes) in 7.2 s: stale after refresh: 1 changed file(s) refresh cannot take in
graph daemon: starting |
graph daemon: ready in 0.3 s
opencode: starting
```

`run_agent(..., progress=...)` takes any `ozagia.harness.progress.Progress`;
the library default is silent, so the eval drivers and tests are unaffected.

The summary:

```
ozagia agent: exit 0 after 41.2 s in /path/repo
  graph: refreshed (5321 nodes) in 0.8 s; daemon ready in 2.1 s
  plugin loaded: yes; captured calls: 37; passthrough: 9; fallbacks: 0
  transcript: /tmp/ozagia_agent/.../logs/transcript.txt
  logs: /tmp/ozagia_agent/.../logs
```

`plugin loaded` is the daemon's `hellos > 0`, `captured calls` its
`handled.true`, `passthrough` the calls it left to the built-in by design,
`fallbacks` its `stats.fallbacks` (the plugin's fallback reports, expired
requests and failed requests; the `eval.agent.graph_stats_fields` fields). `run_agent` returns these and the
paths as a dict.

### Repo-map delivery (opt-in)

```bash
ozagia agent --repo-map --repo-map-tokens 1200 --prompt "fix the retry loop"
```

`--repo-map` is off by default. When on, the runner validates the budget
(200..8000; default 1500), sets `OZAGIA_REPO_MAP_TOKENS` for the OpenCode
process only, and puts `repo_map_tokens` in the result. The plugin reads the
variable once and sends that value on its existing `hint` request. The daemon:

1. calls `memory.staleness(force=True)` before doing any map work;
2. serves `memory.repo_map(prompt, budget_tokens=N)` only when the result says
   `freshness == "fresh"` and the snapshot is not stale;
3. wraps the markdown map in a short header naming the ranked map, the
   snapshot's freshness and the budget, followed by a one-line instruction to
   verify paths and symbols with `read` or `ozagia_structure`;
4. answers an empty context for a stale, unknown, empty or failed map, with a
   bounded reason in `meta.repo_map` and no stale text.

A delivered answer carries `meta.repo_map = {delivered: true, tokens: N,
estimated_tokens, files: [top paths], freshness, graph_hash, cache, truncated:
false}`. A skip carries `{delivered: false, reason, freshness}`. The daemon
counts `repo_map_delivered`, `repo_map_skipped`, `repo_map_tokens` (estimated
tokens delivered) and `repo_map_files` (top paths delivered) in `stats`.

In repo-map mode the plugin asks on model requests instead of caching the
first answer for the whole prompt, so a new task gets a new map. A skipped or
empty map adds nothing to `[ozagia task context]`; it never injects the
ordinary "no task-specific context" line or a stale map. Without
`--repo-map`, the ordinary hint and its once-per-prompt cache are unchanged.
The map adds no tool and changes no file; ordinary search and reads remain
available. See [repo_map.md](repo_map.md).

The summary adds one line when the mode is on:

```
  repo map: 1200 tokens; delivered 4, skipped 0
```

### Local providers (vllm)

`ozagia agent` always sets `OPENCODE_CONFIG` to the generated harness config.
That is deliberate: a global config may carry plugins, MCP servers or
permissions the harness must not adopt. It also means a local
OpenAI-compatible provider configured only in the host's
`~/.config/opencode/opencode.json` is not visible to the session. This was
found in the live prompt-secret test, which had to fall back to
`opencode run --standalone` for `vllm/qwen3.8-27b`.

`--opencode-config PATH` layers a caller-supplied JSON config into the harness
config:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "vllm": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "vLLM",
      "options": {
        "baseURL": "http://127.0.0.1:8000/v1",
        "apiKey": "{env:VLLM_API_KEY}"
      },
      "models": {"qwen3.8-27b": {"name": "Qwen 3.8 27B"}}
    }
  }
}
```

```bash
ozagia agent --opencode-config ~/.config/opencode/vllm.json \
  -m vllm/qwen3.8-27b --prompt "make test_retry pass"
```

The exact allowlist is `provider`, `providers`, `model` and `small_model`;
`$schema` is allowed and ignored. The harness sets its own `model` from
`--model`, so a config `model` is used only when `--model` is absent. With
`--model`, the TUI config gets the CLI model, while a non-interactive run
passes `-m` and drops the file's `model` from the config. `mcp.ozagia` is still
added by `--mcp`/`--research`, independent of the supplied file. Values such
as `{env:VLLM_API_KEY}` and `{file:...}` are copied verbatim and are never
resolved, printed or written to logs.

The trust boundary is the allowlist. `plugin`, `plugins`, `mcp`, `permission`,
`agent`, `agents`, `tools` and every other key are refused, not silently
dropped, so this option cannot import a plugin, MCP server or permission
override. Invalid JSON, a non-object document, a missing/unreadable file, an
unsupported key or a wrong value type raises `AgentError` before OpenCode
starts (CLI exit 2). The host/global config is never used implicitly; point
`--opencode-config` at the file you mean.

### Interactive mode and the plugin's environment

The plugin acts only when `OZAGIA_DAEMON_SOCK` is in the environment of the
process that loads plugins. By default the TUI attaches to OpenCode's
background service. That service was started earlier with its own
environment, so a plugin it loads never sees the socket. `opencode --help`
(2.3.0) lists `--standalone` for the TUI as well as for `run`: "Run with a
private server instead of the background service". The TUI starts that
server itself, so the server inherits the environment the harness sets. The
harness always passes `--standalone`.

Verified: `opencode run --standalone` loads project plugins (the v3 W0
probes, `docs/plans/2026-11-15_v3_graph_intent.md`), and so does the TUI's
private server. Checked on 2.3.0 without a model call: `opencode
--standalone <workdir>` under a pty sent the plugin's setup `hello` (phase
`setup`, `permission_assert: true`) to the daemon socket 1.5 s after
starting. The plugin says hello from `setup`, so the summary after the TUI
exits shows whether it loaded even when the session made no tool call.

While the TUI runs, the harness ignores SIGINT itself, so Ctrl-C reaches the
TUI. Teardown runs when the TUI exits.

## `GraphSession`

```python
from ozagia.harness.session import GraphSession

with GraphSession("/path/repo", run_python="/path/venv/bin/python") as s:
    s.sock, s.dump, s.prep, s.build_s, s.ready_s
    s.stats()
s.final_stats
```

Arguments: `root`, `dump` (default `<state dir>/graph.json`), `state_dir`
(sets `OZAGIA_STATE_DIR` for the subprocesses), `tool_python` (the
interpreter ozagia runs in, default this one), `run_python`, `build`
(`auto`, `always`, `never`), `log_dir` (default a new temp dir), `config`
(becomes `OZAGIA_CONFIG`), `progress` (a `harness.progress.Progress` for the
launch lines; default silent), `background` (False: prepare the snapshot and
start the daemon before returning; True: in a thread, with `sock` naming the
future socket), `status_path` (a `harness.launch_status` document reporting
the phases).

Enter:

1. The snapshot, in a subprocess with `OZAGIA_ROOT=root`
   (`ozagia.core.memory` reads its layout at import). `auto`: build when
   missing. Otherwise `staleness()`; if stale, `refresh()`. A version-1
   snapshot or one built for another root is rebuilt, and so is one still
   stale after a refresh that reingested nothing or left
   `DevMemory.unhandled_changes()`. `refresh` does not recompute git
   history, so after a HEAD change with files to reingest the report has
   `still_stale: true` and the summary suggests `ozagia index --rebuild`.
   `always` rebuilds; `never` fails without a snapshot and never refreshes.
2. The daemon: `python -m ozagia.daemon serve --root R --dump D --journal
   <log_dir>/daemon_journal.jsonl --sock S --no-build --parent-pid <pid>`
   in its own process group, with `S` a short path in the temp dir; it exits
   by itself if this process dies. No `ready` line within
   `eval.agent.DAEMON_READY_TIMEOUT_S` (120 s), or an early exit: the daemon
   is killed and `SessionError` names the reason and the log dir.

With `background=True`, enter returns before both steps: `sock` is reserved
first (so OpenCode can be told where the daemon will be), the two steps run in
a thread, and `status_path` receives `preparing`, the prep process's
`building`/`refreshing`, then `daemon`/`ready`/`failed`. `prep`, `build_s`,
`ready_s` and `daemon` fill in later; `error` names a failure. `close`
cancels a preparation that has not started the daemon (killing its prep
process) and still stops a daemon that did start.

Exit: `stats` goes to `final_stats`, `shutdown` makes the daemon write the
snapshot, a surviving process group gets SIGTERM and then SIGKILL, and the
socket is removed.

Subprocess environment: the inherited `PYTHONPATH` is dropped (the target's
path must not break the tool). When `tool_python` is the running
interpreter, `PYTHONPATH` is set to the directory this `ozagia` package was
imported from, so the subprocesses run the same ozagia code as the caller.

`eval.agent.start_daemon` is not reused: it serves the snapshot at
`<log_dir>/dev_graph.json`, and the session serves the repository's own
snapshot. The session reuses `GraphDaemon.stop`, the socket path, the python
shim and the process-group kill from `eval.agent`.

## Known limits

- A changed tracked file that is neither indexed, adapter-owned nor matched
  by an indexer's `watch` glob keeps the snapshot stale
  (`DevMemory.unhandled_changes()`), and `GraphSession` rebuilds it.
- An adapter indexer that leaves `texts` at its default (None) runs again
  on every reingest of an indexed file; declare `watch` and `texts` to
  avoid that (see `ozagia.adapters`).
- Background job announcements: while a daemon answer says jobs run
  (`meta.jobs_running`), the plugin waits for job ends (`job_events`) and
  queues one message per finished job with `ctx.session.synthetic`
  (`delivery: "queue"`), in the job's OpenCode session or the last session
  that used a tool. `session.synthetic` is in the plugin session API of
  `@opencode/plugin` 2.1.2 (`SessionDomain`); the announcement has been
  tested only against a stub `ctx`, not in a live OpenCode 2.3 session.
  Without `session.synthetic` the plugin announces nothing.
