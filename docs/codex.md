# Codex hooks and MCP server

`ozagia.integrations.codex` connects Codex to ozagia in three ways:

- **Command hooks** (`.codex/hooks.json`): graph context after shell
  commands and patches, refusal of ref-moving git commands, and a short brief
  at session start.
- **The ozagia MCP server** (`[mcp_servers.ozagia]` in `.codex/config.toml`):
  the graph tools (`dev_symbol`, `dev_node`, `dev_outline`, `dev_impact`,
  `dev_context`, ...) next to Codex's own.
- **An `AGENTS.md` section** telling the agent to prefer those tools for
  reading by key, outlines and impact, and, when the project enables
  background jobs (`--jobs`), to run long commands as jobs (`job_start`,
  `job_wait`) instead of polling them.

Codex keeps running its own tools. The hooks add context to their results;
they never replace a result.

**Status: untested live.** The hooks have been run only as subprocesses
against a stub daemon and a real daemon on a temp repository, with payloads
shaped like the reference (`tests/test_codex_hooks.py`). The MCP table was
checked with `codex mcp get ozagia --json` on Codex 0.169.0, which reads
`command`, `args` and `env` as written.

## Reference implemented against

- Codex CLI 0.169.0.
- Hooks reference: <https://learn.chatgpt.com/docs/hooks>, fetched
  2026-11-16.

## Install

```
ozagia install codex --workdir <project>            # project scope (default)
ozagia install codex --workdir <project> --jobs     # and enable background jobs
ozagia install codex --workdir <project> --scope user
```

(or `python -m ozagia.integrations.codex install ...`). Options:

- `--scope project` writes `<project>/.codex/hooks.json`,
  `<project>/.codex/config.toml` and `<project>/AGENTS.md`. The MCP server
  gets `--root <project>`.
- `--scope user` writes `hooks.json`, `config.toml` and `AGENTS.md` under
  `$CODEX_HOME` (default `~/.codex`). The MCP server has no `--root`, so it
  serves the Git repository of its working directory.
- `--python P` is the interpreter that runs the hook and the MCP server
  (default: the one running the command).
- `--jobs` (project scope only) enables background jobs for the project:
  it appends `[jobs]` / `enabled = true` (with a comment saying who added
  it) to `<project>/.ozagia.toml`, and the AGENTS.md section gets the jobs
  paragraph. A `[jobs]` table already there is left as it is if it enables
  jobs, and refused if it does not. Jobs run outside the Codex sandbox and
  approvals (see [Background jobs](#background-jobs)); do not use `--jobs`
  where that matters. The AGENTS.md section and the session brief mention
  jobs only while the project enables them, so a reinstall without `--jobs`
  keeps the paragraph as long as `.ozagia.toml` enables jobs.

What install does to each file:

- `hooks.json`: removes ozagia's handlers (command handlers whose command
  ends in `-m ozagia.integrations.codex hook`) and adds them again. Other
  keys and handlers stay. The file is written atomically. A file that is not
  JSON, not an object, or whose `hooks` is not an object of lists is refused
  with an error, and nothing is written.
- `config.toml`: adds or replaces only the `[mcp_servers.ozagia]` table (and
  any `[mcp_servers.ozagia.*]` subtables). The table is appended at the end,
  after one blank line, or written where the old one was. Every other byte
  stays the same (CRLF line ends too), with one exception: if the file's
  last line has no newline, install adds one before the table and says so in
  a comment inside the table; uninstall takes that newline away again. The
  file is parsed with `tomllib` before and after, and the edit is refused if
  anything outside the table would parse differently (an empty
  `[mcp_servers]` header counts as no `mcp_servers`), or if
  `mcp_servers.ozagia` is written as dotted keys or an inline table. When
  the line scanner cannot tell where ozagia's table ends (a header it cannot
  read, such as `[mcp_servers."a/b"]`, after it, or a header-like line inside
  a multi-line string), the error names the line.
- `AGENTS.md`: adds or replaces the text between `<!-- ozagia:begin -->` and
  `<!-- ozagia:end -->`, after one blank line. A missing final newline is
  handled as in `config.toml`, with an HTML comment as the marker.

So install followed by uninstall gives back the original bytes of
`config.toml` and `AGENTS.md`.

All three are checked before any is written. Running install again changes
nothing, or only what differs (for example a new `--python`).

The hook entries:

```json
{
  "hooks": {
    "SessionStart": [{"hooks": [{"type": "command", "timeout": 20,
      "command": "/path/to/python -m ozagia.integrations.codex hook"}]}],
    "PreToolUse": [{"matcher": "^Bash$", "hooks": [{"type": "command", "timeout": 20,
      "command": "/path/to/python -m ozagia.integrations.codex hook"}]}],
    "PostToolUse": [{"matcher": "^(Bash|apply_patch)$", "hooks": [{"type": "command",
      "timeout": 20, "command": "/path/to/python -m ozagia.integrations.codex hook"}]}]
  }
}
```

The MCP table:

```toml
[mcp_servers.ozagia]
# Added by `ozagia install codex`; removed by
# `python -m ozagia.integrations.codex uninstall`.
command = "/path/to/python"
args = ["-m", "ozagia.integrations.mcp.server", "--root", "/path/to/project", "--mode", "coding"]
env = { OZAGIA_ROOT = "/path/to/project" }
```

(`env` also has `PYTHONPATH` when `--python` is the interpreter running the
installer, as in `ozagia agent --mcp`.)

### Trust review

Codex runs a hook that is not managed by policy only after you review and
trust it. Codex records trust against the hook's hash, so a new or changed
hook (also after a reinstall with a different `--python`) is skipped until
you trust it again. Project hooks (`<project>/.codex/hooks.json`) load only
when the project itself is trusted.

After installing, start Codex in the project, open `/hooks`, review the
three ozagia hooks and trust them. If hooks need review at startup, Codex
prints a warning that points to `/hooks`.

### The daemon

The hooks talk to the graph daemon over its Unix socket. You do not have to
start it: when nothing answers, a hook runs `ozagia daemon ensure`, which
starts `ozagia daemon serve` detached, with its output in
`<state dir>/daemon.log` and an idle timeout of 30 minutes (set
`OZAGIA_DAEMON_IDLE_TIMEOUT` in Codex's environment to change it; 0 keeps
it running). `SessionStart` starts it too, so it is usually loaded by the
first tool call. A hook waits at most 3 seconds for a daemon it started. On
a large repository the first snapshot build takes longer; the calls made
meanwhile come back without context. Start it ahead with
`ozagia daemon ensure --root <project>`. Set `OZAGIA_DAEMON_AUTOSTART=0` to
turn autostart off.

A hook starts a daemon only when the session's root is a git work tree,
`OZAGIA_ROOT` is set, or the root already has a graph snapshot
(`<state dir>/graph.json`). In any other directory (`$HOME`, `/tmp`, a
download folder; this matters for a user-scope install) the hooks start
nothing, write nothing, and `SessionStart` prints no brief.

Two hooks that start at the same moment start one daemon: the starter holds
a lock on `<state dir>/daemon.lock`, and the daemon keeps it for its life.
`ozagia daemon serve` takes the same lock itself before it loads the
snapshot, so a daemon started by hand or by the OpenCode plugin holds it
too, and a second `serve` for the same socket exits at once with an error
instead of loading the graph again. (A daemon on a socket other than the
root's default locks `<socket>.lock` instead.)

## Background jobs

In Codex history, 44,938 of 51,005 `write_stdin` calls were empty polls of
a running command, next to 5,914 `wait`, 1,020 `sleep` and 6,099 `ps`/`pgrep`
calls. Each poll is a model call. The MCP tools `job_start`, `job_wait`,
`job_list`, `job_log` and `job_stop` replace them (see
`ozagia.daemon.jobs`).

**Jobs are not sandboxed, and they are off by default.** A job runs in the
ozagia daemon with the daemon's privileges and environment, outside the
Codex sandbox (including its read-only `.git`), its approval prompts and
its `shell_environment_policy`. The MCP tool descriptions, the AGENTS.md
paragraph and the session brief say so. `job_start` is refused, with a
message that says this and how to enable jobs, unless the project has
`[jobs] enabled = true` in `.ozagia.toml` (`--jobs` above) or the daemon
was started with `OZAGIA_JOBS=1` in its environment (`OZAGIA_JOBS=0` turns
jobs off whatever the file says). `job_list`, `job_wait`, `job_log` and
`job_stop` work either way. The checks on what a job may run are
best-effort parsing, not a boundary: ref-moving git commands are refused as
in the shell; so is a command the shell check cannot follow (pipes,
substitutions, loops) when it mentions git anywhere or feeds text to a
shell (`| bash`, `sh <<< ...`), and an `env` with any `GIT_*` key
(`GIT_CONFIG_*` can alias a commit, `GIT_DIR` can point anywhere). The
`cwd` check limits where a job starts, not what it touches.


- `job_start {"command": "npm run dev"}` (or `argv`, `cwd` inside the
  project, `env`, `detach`) runs the command in the daemon, in its own
  process group, with stdin from /dev/null and stdout and stderr in
  `<state dir>/jobs/<id>.log`. It returns the job id.
- `job_wait {"job": "j1", "until": "log:Listening on"}` blocks in the daemon
  until the job exits (`until: "exit"`, the default), a file exists
  (`file:<path>`) or a new log line matches, or until `deadline_s`
  (default 45 s, at most 50 s, below Codex's MCP tool timeout,
  `tool_timeout_sec`, 60 s by default). It returns the status, the exit code,
  the elapsed time, and only the log lines this MCP server has not read yet
  (the newest 200 lines / 16 kB of them), with a `cursor`. If the job is
  still running the reply says so; the agent calls `job_wait` again and
  gets the next lines. `job: "any"` waits for the first running job to end;
  with `until: "file:<path>"` and no `job`, the wait is for the file alone.
- `job_log` reads new lines without waiting (`cursor: 0`: the end of the
  whole log); `job_list` lists the last 50 jobs; `job_stop` signals the
  job's process group (SIGTERM, then SIGKILL after `grace_s`, at most 10 s
  over MCP).

A job is running until its whole process group is gone: when the command
(the group leader) exits but something it started in the background keeps
running (`./start.sh` that runs the server with `&`), the job stays
`running` with `leader_exited` and `group_alive` true, and `job_wait`
waits for the rest. Signals go to the group only while it is still the
job's (the daemon reaps the leader only once the group is empty, so the
group id cannot be reused before that), and never after the group was
found empty.

The MCP server runs job tool calls on worker threads (4), so a `job_wait`
does not hold up other tool calls; their replies may arrive out of order.

The MCP server is a separate process: it reaches the daemon over the socket
and starts it when nothing answers (as the hooks do, waiting up to 10 s).
When no daemon can be reached, the tool returns an error that says so and
how to start one (`ozagia daemon ensure --root <project>`). The same jobs are
there from the shell: `ozagia job run|wait|list|log|stop`.

The daemon does not stop on its idle timeout while a job runs. When it
stops, it stops its jobs (SIGTERM to each process group, SIGKILL after 2 s),
except jobs started with `detach: true`, which it leaves running and names
in `<state dir>/daemon.log`; the next daemon lists them as `detached` and
can stop them. `detach` is the explicit opt-out from "jobs end with the
daemon". A daemon killed without its shutdown (SIGKILL, OOM) cannot stop
its jobs; the next daemon stops the ones not started with `detach` when it
starts (`gone`, `stopped_on_load`), checking each process's start time so a
reused pid is left alone.

The hooks help too:

- `PreToolUse` `Bash` with `pgrep [-f] [-a|-l] PAT` or `ps -p PID [-o
  FIELD=]` about a job's process: if a daemon is running, the hook denies
  the command with its answer as the reason (the processes as pgrep or ps
  would list them, from /proc, and the job's status and the `job_wait`
  call to make). Other `ps`/`pgrep` commands run as usual.
- `PostToolUse` `Bash`: a `sleep N` of 20 s or more while a job runs gets a
  `[ozagia]` line pointing to `job_wait`; a command that starts something in
  the background (`... &`, `nohup`, `setsid`) gets one pointing to
  `job_start` (only when jobs are enabled). Each hint is shown at most 3
  times per session.

## What each hook does

| Codex event | hook | what ozagia does | what Codex gets |
|---|---|---|---|
| `SessionStart` (every `source`) | brief | In a git work tree only: starts the daemon if needed (waits at most 0.5 s, not for the snapshot load), and lists the dirty files (`git status --porcelain`, at most 15), the last 3 commits, and the ozagia MCP tools to prefer. At most 1,500 characters. | `additionalContext` |
| `PreToolUse` `Bash` with a ref-moving git command | refusal | Refuses it locally, without the daemon, with the intent layer's check (`ozagia.intent.shell.classify` is `git_refs`; see docs/claude_code.md for the list and the parsing). Listing forms (`git branch`, `git tag -l`, `git stash list`) are allowed. | `permissionDecision: "deny"` with a reason |
| `PreToolUse` `Bash` with `pgrep`/`ps -p` about a job | job table | Asks a running daemon (3 s, no autostart) with `job_ps`; a null answer (not about a job) lets the command run. | `permissionDecision: "deny"` with the answer |
| `PostToolUse` `Bash` | `after` as `shell` | Sends `{command, workdir: cwd}` and the command's output. The daemon maps pytest runs to test keys, re-ingests files the command changed, and maps `git diff`/`git show` hunks to node keys. | `additionalContext` with the daemon's `append`, when not empty |
| `PostToolUse` `apply_patch` | `after` as `patch` | Sends `{patch: <patch text>}`; the daemon re-ingests the changed files. | the graph delta (`[ozagia] delta: ...`) as `additionalContext` |

The hook sends `harness: "codex"` and `session` = `session_id`; the daemon
counts requests by harness (`stats.by_harness`).

The hook prints only fields Codex supports for each event: on PreToolUse
`hookSpecificOutput` with `permissionDecision: "deny"` (never `ask`,
`continue`, `stopReason` or `suppressOutput`, which Codex treats as a failed
hook); on PostToolUse and SessionStart `hookSpecificOutput.additionalContext`.

Failures never block the agent. If the daemon is down, slow or returns an
error, or the hook input is malformed, the hook prints nothing and exits 0,
and appends one JSON line (`ts`, `event`, `tool`, `session`, `error`) to
`<state dir>/codex_hook.log`. Codex's handler `timeout` in `hooks.json` is
20 s (`HOOK_TIMEOUT_S`); the hook stops itself earlier, at 15 s
(`HOOK_ALARM_S`), so Codex never has to kill it. Autostart waits at most
3 s and a daemon request has 10 s. If the alarm fires while `SessionStart`
starts the daemon, the brief still gets 3 s (`BRIEF_ALARM_S`) before the
hook gives up. The hook never exits 2.

## Uninstall

```
python -m ozagia.integrations.codex uninstall --workdir <project> [--scope user]
```

This removes ozagia's handlers from `hooks.json`, the `[mcp_servers.ozagia]`
table (with the blank line install put before it) from `config.toml`, and
the marked section from `AGENTS.md`, and the `[jobs]` block `--jobs` added
to `.ozagia.toml` if it is there as install wrote it. Everything else stays. A file left with
nothing in it is deleted. The daemon stops on its own after the idle
timeout, or with `ozagia daemon shutdown --root <project>`.

## Limits

- `write_stdin` polls (input to a running interactive command) do not run
  hooks, so output read that way gets no context, and a poll cannot be
  turned into a `job_wait`; the AGENTS.md section asks for jobs instead.
- The built-in result is never replaced. ozagia does not use PostToolUse
  `decision: "block"` or PreToolUse `updatedInput`.
- Patch misses get no special handling: when `apply_patch` fails, the
  `after` finds no changed files and adds nothing.
- Codex spills `additionalContext` over about 2,500 tokens to a file and
  gives the model a preview. The handlers do not set
  `additionalContextLimit`; the brief is far below the limit, and the
  daemon's `append` normally is.
- The git refusal is best-effort parsing; use Codex's sandbox and approval
  settings for a hard rule. Those settings do not apply to background jobs,
  which is why jobs are opt-in.
- The user-scope MCP server serves the repository of its working directory.
  Whether Codex starts MCP servers in the session's directory has not been
  checked live.
