# Codex hooks and MCP server

`ozagia.integrations.codex` connects Codex to ozagia in three ways:

- **Command hooks** (`.codex/hooks.json`): a check of every `apply_patch`
  before it runs (a patch that would fail is refused with the current text
  of the regions it missed), graph context after shell commands and
  patches, refusal of ref-moving git commands, and a short brief at session
  start.
- **The ozagia MCP server** (`[mcp_servers.ozagia]` in `.codex/config.toml`):
  the graph tools (`dev_symbol`, `dev_node`, `dev_outline`, `dev_impact`,
  `dev_context`, ...) next to Codex's own.
- **An `AGENTS.md` section** telling the agent to prefer those tools for
  reading by key, outlines and impact, and, when the project enables
  background jobs (`--jobs`), to run long commands as jobs (`job_start`,
  `job_wait`) instead of polling them.

Codex keeps running its own tools. The hooks add context to their results;
they never replace a result. They refuse two kinds of call: ref-moving git
commands, and a patch whose hunks would not apply.

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
    "PreToolUse": [{"matcher": "^(Bash|apply_patch)$", "hooks": [{"type": "command", "timeout": 20,
      "command": "/path/to/python -m ozagia.integrations.codex hook"}]}],
    "PostToolUse": [{"matcher": "^(Bash|apply_patch)$", "hooks": [{"type": "command",
      "timeout": 20, "command": "/path/to/python -m ozagia.integrations.codex hook"}]}],
    "PreCompact": [{"hooks": [{"type": "command", "timeout": 20,
      "command": "/path/to/python -m ozagia.integrations.codex hook"}]}],
    "SessionEnd": [{"hooks": [{"type": "command", "timeout": 20,
      "command": "/path/to/python -m ozagia.integrations.codex hook"}]}]
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
there from the shell: `ozagia bg run|wait|list|log|stop`.

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
| `SessionStart` (`startup`, `resume`, `clear`) | brief | In a git work tree only: starts the daemon if needed (waits at most 0.5 s, not for the snapshot load), and gives the brief "since the last session": the dirty files (`git status --porcelain`, at most 15), the last 3 commits, the tests still failing in the root's test record (each with the time of the first run that failed it) and their rerun command, running background jobs, and the ozagia MCP tools to prefer. At most 1,500 characters. A running daemon builds it (1 s); otherwise the hook builds it from git and `.ozagia/working_set/tests.json`, without jobs. Also sends `session_event` `start` (with `compaction_hook: true`), which clears the session's read ledger; a failed one leaves a marker, and the next ledger request carries `reset_pending`. | `additionalContext` |
| `SessionStart` `compact` | working set | The session's working set from a running daemon (`working_set` op, 3 s, keyed by `session_id` and `transcript_path`): files and node keys it edited, tests failing in its last pytest runs with the command that reruns them, running jobs, the newest open DevSession's step, and the files and line ranges it read last (see "Working set" below). At most 7,000 characters (about 2,000 tokens), under Codex's 2,500-token cap on hook output, so `additionalContextLimit` is not set. When the daemon has nothing, the brief instead. Then `session_event` `compact`. | `additionalContext` |
| `PreCompact`, `SessionEnd` | ledger | `session_event` `compact` / `end`: the session's read ledger is cleared / dropped. | nothing |
| `PreToolUse` `Bash` with a shell read (`sed -n`, `cat`, `head`, `tail`, `nl`, `nl -ba F \| sed -n 'a,bp'`) | repeat check | Asks the daemon's `read_check`. Under the `stub` policy, a repeat of an unchanged range this session already read is refused with the one-line stub. The default policy is `note` (see below), so by default this never refuses. A command with a `cd` is not tracked. | `permissionDecision: "deny"` with the stub as the reason, or nothing |
| `PreToolUse` `Bash` with a ref-moving git command | refusal | Refuses it locally, without the daemon, with the intent layer's check (`ozagia.intent.shell.classify` is `git_refs`; see docs/claude_code.md for the list and the parsing). Listing forms (`git branch`, `git tag -l`, `git stash list`) are allowed. | `permissionDecision: "deny"` with a reason |
| `PreToolUse` `apply_patch` | patch check | Checks the patch against the current files locally, as Codex's apply_patch matches hunks (see "Patch check" below). If every hunk matches, prints nothing. If one would fail, refuses the call with the nearest region of each failing hunk; only then, if a daemon is running, it is asked too (at most 1.5 s, never started for this), for the graph's keys and to count the miss. A matching patch never waits for the daemon. | nothing, or `permissionDecision: "deny"` with the regions as the reason |
| `PreToolUse` `Bash` with `pgrep`/`ps -p` about a job | job table | Asks a running daemon (3 s, no autostart) with `job_ps`; a null answer (not about a job) lets the command run. | `permissionDecision: "deny"` with the answer |
| `PostToolUse` `Bash` | `after` as `shell` | Sends `{command, workdir: cwd}` and the command's output. The daemon maps pytest runs to test keys, re-ingests files the command changed, and maps `git diff`/`git show` hunks to node keys. | `additionalContext` with the daemon's `append`, when not empty |
| `PostToolUse` `apply_patch` | `after` as `patch` | Sends `{patch: <patch text>}`; the daemon re-ingests the changed files. | the graph delta (`[ozagia] delta: ...`) as `additionalContext` |

The hook sends `harness: "codex"` and `session` = `session_id`; the daemon
counts requests by harness (`stats.by_harness`). Ledger requests also send
`sub` = `transcript_path` (and `agent_id` when the payload has one).

### Read ledger and subagents

The daemon records each shell read (file sha256 and line range) per
session; `PostToolUse` adds `[ozagia] unchanged since read N (lines a-b,
sha ...)` to a read that repeats an unchanged range (`docs/intent.md`,
read). The default repeat policy is `note` for every harness, and Codex
should stay there: the Codex hooks reference says subagent hooks use the
parent's session id, and documents no subagent field (`agent_id`) for
PreToolUse or PostToolUse. A subagent does not have its parent's context,
so a stub keyed on the shared session id could hide text it never saw.
`transcript_path` (and `agent_id`, if Codex sends it) separates ledgers
where the payload differs, but that is not documented to tell subagents
apart. Codex also truncates exec output; the ledger records only the lines
the output shows, so a truncated `cat` counts as its head only. A
configured `stub` applies only after the session's SessionStart
`session_event` (which carries `compaction_hook: true`) reached the daemon.

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

## Working set

Codex compacts often (1,571 compactions in the usage logs, 1,485 of them
Codex, one session with 476) and the model then reads the same files
again (13k re-reads right after a compaction). The daemon keeps, per
session, what the session was working on, and `SessionStart` with source
`compact` hands it back:

```
[ozagia] working set (codex session 019a...): what this session edited, ran and read before the context was compacted (1 compaction(s) so far). Re-read only what the next step needs.
How to read: read a key with the ozagia MCP tool dev_node (key=...), a range with sed -n 'a,bp' FILE.
failing tests (last pytest run 2025-06-02 12:03 UTC: `.venv/bin/python -m pytest -q tests/test_a.py`):
  failed test:tests/test_a.py::test_one (failing since 2025-06-02 11:40 UTC): assert 2 == 1
  rerun (from the repo root): .venv/bin/python -m pytest -q tests/test_a.py::test_one
running jobs:
  j3 `npm run dev` running, 120 s: job_wait job=j3 (or job_log)
edited (most recent first; read a key instead of the whole file):
  src/a.py: sym:src/a.py::f, sym:src/a.py::g
recently read (re-read a range only if the summary lacks it):
  src/b.py lines 1-120, 300-340
```

What goes in (`ozagia.core.working_set`):

- edited files and keys: `after` for `apply_patch` (the files the patch
  names that changed) and for shell commands that changed files (changed
  files written in the last minute, and the delta's keys in them); never
  every pending change in the root, which may come from other sessions or
  the user's editor; at most 20 files and 30 keys;
- failing tests: pytest runs seen by `after` (pytest's `FAILED`/`ERROR`
  summary lines); a later run that selects a test and does not fail it
  clears it, but only when nothing is in doubt: it printed a summary (a
  captured run: status `passed` or `failed`, no collection errors, no
  `[ozagia] error:`), the summary counts no more failures and errors than
  the output names, it was not filtered (`-k`, `-m`, also `-kEXPR`,
  `--deselect`, `--lf`) and did not stop early with failures (`-x`,
  `--maxfail`). A run without paths selects its workdir (`cd pkg &&
  pytest` selects `pkg/`). At most 10; each shows the time of the first
  run that failed it;
- the last 12 files read with up to 4 ranges each (a copy of the read
  ledger's records, which the compaction clears);
- running jobs (this session's first; jobs started through MCP carry the
  MCP server's id, so they are listed as "another session");
- the current step of the newest open DevSession in the root (best effort:
  nothing links a Codex session to a DevSession).

It lives in the daemon's memory, keyed like the read ledger (session id,
`transcript_path`), survives `PreCompact` and `SessionStart:compact`, and
goes with `SessionEnd` or a daemon restart. The root's failing tests are
also kept in `.ozagia/working_set/tests.json` for the next session's brief:
test ids, outcomes, times and the rerun prefix (interpreter and `-m pytest`),
and the last 50 runs (time, selectors as paths or node ids, failed test
ids, completeness; for goal criteria, docs/goals.md),
never failure messages or command lines.
The MCP tool `dev_working_set` returns the same text; an MCP server does
not know Codex's session id, so without `session` it answers for the
session whose working set changed last in the repository, of any harness
unless `harness` is given (the header names it). Counters:
`stats.working_sets_served`, `working_set_chars`, `session_briefs_served`.

For dirty files whose extensions name an external-tool capability (PDF,
images, office documents, media, ...), the session brief also adds a bounded
`[ozagia] tools:` block: what is installed, what is missing and the install
preview. It runs no version or sudo probe and installs nothing; the setting
`[tools] brief = false` turns it off (docs/toolchain.md, "Session brief").
The MCP tool `dev_tools` returns the same plan for capabilities or paths.

## Patch check

`ozagia.intent.patch` ports Codex's apply_patch parser and matcher
(`codex-rs/apply-patch` in github.com/openai/codex; the module docstring
lists the files and the rules). In short: hunks are matched in order, each
`@@ <line>` anchor is searched forward from the previous hunk, and the
expected lines (context and `-` lines) are matched exactly, then with
trailing whitespace ignored, then with leading and trailing whitespace
ignored, then with Unicode dashes, quotes and spaces read as ASCII. With
`*** End of File` the lines must end the file.

For each failing hunk (at most 4), the reason gives:

```
*** src/a.py  sha256 <64 hex>  (412 lines)
hunk 2 of 3 (@@ def foo): the expected lines are not in the file after line 40
  nearest region: L120-L131 sym:src/a.py::Foo.foo  similarity 0.86
  node L118-L140 node sha256 <64 hex>; the region is part of it, a key edit replaces all of it
  differs (-patch expects, +file has):
  -    return x+1
  +    return x + 2
  current L120-L131:
120:     def foo(self):
...
```

The nearest region is the window of the file most like the hunk's expected
lines (whitespace-normalised difflib ratios; windows after the anchor are
preferred). Its key is the enclosing symbol (`sym:`/`test:`), Markdown or TeX
section (`doc:<file>#<slug>`, the key reads and outlines show, see
`ozagia.core.sections`), JSON value (`json:<file>#<pointer>`) or text
chunk (`text:<file>#L<a>-L<b>`, for other unparsed files). The
`node` line gives that node's full line range and the sha256 of its current
text: the `base_sha256` a key edit (`dev_key_edit`) of it takes. A key edit
of part of a file does not take the file's sha256, since the region may show
only part of the node it would replace. Lines are counted at CRLF, lone CR
and LF, the same in the reply and in key edits (a section key's node range
counts at LF, as reads do; the two differ only for lone-CR files). A
region shows at most 60 lines, a line at most 1,500 characters, and the
whole reason stays under 7,000 characters (about 2,000 tokens, under
Codex's limit of about 2,500 tokens of hook output). When the expected lines
exist but before the anchor or the previous hunk, the reason says so.

The patch is allowed (the hook prints nothing) when it does not parse,
when the checker fails, and for files the check cannot judge: paths outside
the repository or in `.git`, files that do not exist, binary or non-UTF-8
files, and files over 4 MB. Codex reports those itself. A patch with an
`*** Environment ID:` line is not checked at all: Codex applies it in that
environment, whose files the hook may not see. Paths are taken
relative to the event's `cwd`.

The daemon counts checks in `stats`: `patch_checks`, `patch_misses_caught`
and `region_replies`. The Codex hook asks the daemon only after a local
miss, so from Codex `patch_checks` counts the patches that missed; checks
made while no daemon runs are not counted.

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
- The patch check reads the files when the hook runs. A file changed
  between the check and Codex's own apply is not seen.
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
