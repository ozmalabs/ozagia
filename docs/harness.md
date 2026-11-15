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
| `--prompt TEXT`, `--prompt-file F` | run non-interactively; without either, the TUI |
| `--model M`, `-m M` | `provider/model`. `opencode run` gets `-m`; the TUI has no model flag, so it goes into the generated config as `model` |
| `--clone [--base REV]` | run in an isolated clone at REV (default HEAD), kept afterwards |
| `--keep-plugin` | leave `.opencode/plugins/ozagia.js` in the workdir |
| `--timeout S` | kill the session after S seconds (exit 124) |
| `--python P` | `python` in the agent's shell commands (and the daemon's) is P |
| `--opencode EXE` | the opencode executable (default `opencode` on PATH) |
| `--runs-dir D` | where run roots go (default `<tmp>/ozagia_agent`) |
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
5. The plugin is written to `.opencode/plugins/ozagia.js`. A plugin file that
   was there before is overwritten unless git tracks it, and is not removed,
   except one whose content is exactly the packaged plugin: a run killed
   before its teardown left it, so it is removed like a new one.
6. `logs/opencode_config.json` becomes `OPENCODE_CONFIG`: the eval's policy,
   `permission.bash` with `"*": "allow"` and `deny` for `git commit*`,
   `push*`, `stash*`, `branch*`, `checkout*`, `switch*`, `reset*`, `rebase*`,
   `merge*`, `tag*`, `worktree*` and `git <sub>*` for every other
   subcommand in `intent.shell.GIT_REF_MOVING` (`eval.agent.OPENCODE_GIT_DENY`).
7. OpenCode runs in the workdir with `OZAGIA_DAEMON_SOCK`, `PWD` (OpenCode
   takes its project directory from it) and `OPENCODE_CONFIG` added to this
   process's environment, and with `--python`, the python shim first on PATH.
   - With a prompt: `opencode run --standalone --format json --auto --archived
     [-m M] [ARGS] <prompt>` in its own process group, stdin `/dev/null`,
     stdout to `logs/opencode.stdout.jsonl`, stderr to
     `logs/opencode.stderr.txt`, text parts to `logs/transcript.txt`.
   - Without: `opencode --standalone [ARGS] <workdir>` on the terminal.
8. In `finally` (also on Ctrl-C and SIGTERM): the plugin and its exclude
   line are removed unless `--keep-plugin`, then the daemon is stopped.
   SIGINT is held off during teardown. After a hard kill (SIGKILL) no
   teardown runs: the daemon, started with `--parent-pid`, exits by itself
   within 2 s, and the next run removes the leftover plugin.

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
(becomes `OZAGIA_CONFIG`).

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
