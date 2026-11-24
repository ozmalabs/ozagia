# Kiro CLI hooks and MCP server

`ozagia.integrations.kiro` connects Kiro CLI to ozagia in two ways:

- **Command hooks**: refusal of ref-moving git commands, reads of node keys
  (`sym:...`, `json:...#/a`) answered from the graph, a graph refresh after
  edits and shell commands, and a short brief at session start.
- **The ozagia MCP server** (`mcpServers.ozagia` in
  `.kiro/settings/mcp.json`): the graph tools (`dev_symbol`, `dev_node`,
  `dev_outline`, `dev_impact`, `dev_context`, ...) next to Kiro's own.

**Status: untested live.** The hooks have run only as subprocesses against
a stub daemon, with payloads shaped as described below
(`tests/test_kiro_hooks.py`). The agent profiles install writes pass
`kiro-cli agent validate`, `kiro-cli agent list` finds them, and
`kiro-cli agent upgrade --dry-run` leaves them alone. No Kiro chat session
has run them. See "Open questions" and the probe hook.

## Reference implemented against

- kiro-cli 2.37.0 (Linux), with its bundled V3 engine
  (`~/.local/share/kiro-cli/kas/2.37.0-*/`).
- Docs fetched 2026-11-16: <https://kiro.dev/docs/hooks/>,
  <https://kiro.dev/docs/hooks/types/>,
  <https://kiro.dev/docs/cli/v3/hooks-migration/>,
  <https://kiro.dev/docs/custom-agents/configuration-reference/>.

## Which hooks kiro-cli 2.37 loads

kiro-cli 2.37 ships three agent engines: V1, V2 (`kiro-cli --v2`, "the
V2 agent engine, the pre-3.0 default"; both Rust) and V3 (`kiro-cli --v3`,
the KAS engine, on Node; `kiro-cli acp --agent-engine v1|v2|v3` picks any
of them). `kiro-cli chat` uses the `chat.agentEngine` setting (on the
machine this was written on it is `"v3"`); `kiro-cli acp` uses
`acp.agentEngine`, default `v2`. They load hooks from different places:

| engine | `.kiro/hooks/*.json` and `~/.kiro/hooks/*.json` | hooks in an agent profile |
|---|---|---|
| V3 | yes, in every session of a trusted workspace | yes, when the agent is used; object form and array form |
| V1, V2 (Rust) | no | yes, when the agent is used; object form and array form |

How this was established, offline only:

- The V3 engine bundle (`acp-server.js`) has a standalone loader that reads
  `<workspace>/.kiro/hooks/*.json` and `~/.kiro/hooks/` (`globalHookDirs`),
  validates `{"version": "v1", "hooks": [...]}` with fields `name`,
  `description`, `trigger`, `matcher`, `action`, `timeout` (seconds,
  default 60), `enabled`, `confirm`, and skips execution in an untrusted
  workspace (`hooks.v2.executionDisabledUntrustedWorkspace`). It also loads
  profile hooks, converting the object form and mapping the CLI 2.x names
  (`agentSpawn` → `SessionStart`, `preToolUse` → `PreToolUse`, ...).
  Its trigger list has no `PreCompact`/`PostCompact` (the docs say they
  need CLI 3.1.0). A hook document with a trigger it does not know is
  skipped with a warning ("Hook trigger is not a recognized name") and the
  rest of the file loads (the schema takes any non-empty `trigger`), so
  install puts `PostCompact` in the hook file: inert on 2.37, live on
  3.1.0+. The CLI 2.x agent validator rejects unknown object-form
  triggers, so profiles do not get it.
- The Rust binary (`kiro-cli-chat`) has no `.kiro/hooks` path; its agent
  schema knows `agentSpawn`, `userPromptSubmit`, `preToolUse`,
  `postToolUse`, `stop`, and reads both `hooks` shapes ("Schema-only
  representation documenting both accepted `hooks` shapes"). `/context
  hooks` says to configure hooks in the agent.
- `kiro-cli agent upgrade --help` says it rewrites profiles "to the
  universal format both engines load". On a temp copy it turned object-form
  hooks into an array of hook documents with the CLI 2.x trigger names and
  `timeout` in seconds; a profile already in that form is reported as
  "already universal" and left alone.
- `kiro-cli agent validate --path` accepts the profiles below and rejects an
  unknown object-form trigger. (It exits 0 either way and prints `Error:` on
  stderr.)

So the default, `--format v3`, writes the hook file: it is what
`kiro-cli chat` runs with `chat.agentEngine = "v3"`, and it applies to
every session without choosing an agent. `--format agent` is for the V1/V2
engines (and `kiro-cli acp` with its default engine).

## Install

```
ozagia install kiro --workdir <project>                    # hook file (V3)
ozagia install kiro --workdir <project> --format agent     # agent profile
ozagia install kiro --workdir <project> --format both
ozagia install kiro --workdir <project> --scope user       # under ~/.kiro
```

(or `python -m ozagia.integrations.kiro install ...`). Options:

- `--format v3` (default) writes `.kiro/hooks/ozagia.json`.
- `--format agent` writes the hooks into `.kiro/agents/<agent>.json`
  (`--agent NAME`, default `ozagia`). A new profile is
  `{"name": "ozagia", "description": ..., "tools": ["*"],
  "includeMcpJson": true, "hooks": [...]}`: Kiro's tools and the MCP
  servers from `mcp.json`. Start it with `kiro-cli chat --agent ozagia`.
  An existing profile (`--agent dev`) keeps every field and gets only
  ozagia's hooks, in the form it already uses (array, or the CLI 2.x
  object form with `timeout_ms`).
- `--format both` writes both. In a V3 session that uses the ozagia agent
  the hooks then run twice: harmless, but double the work.
- `--scope user` writes `~/.kiro/hooks/ozagia.json`,
  `~/.kiro/agents/<agent>.json` and `~/.kiro/settings/mcp.json`. The MCP
  server has no `--root`, so it serves the Git repository of its working
  directory.
- `--python P` is the interpreter that runs the hook and the MCP server
  (default: the one running the command).

Every file is JSON. Install reads and checks all of them before it writes
any, removes ozagia's earlier entries (hooks whose command ends in
`-m ozagia.integrations.kiro hook`, the `ozagia` MCP server), adds the
current ones, keeps everything else (other hooks, servers and keys, in their
order), and writes each changed file atomically with 2-space indentation.
A file that is not valid JSON or has the wrong shape (`hooks` not a list in
the hook file, a `version` other than `"v1"`, `mcpServers` not an object,
profile `hooks` neither a list nor an object of lists) is refused with an
error and nothing is written. If a write fails partway (a permission
error, a full disk), the files already written get their old content back
and the error is reported. A file that is a symlink (for example a
dotfiles-managed `~/.kiro/settings/mcp.json`) is written through the link,
so its target changes and the link stays. Running install again changes
nothing, or only what differs (a new `--python`); keys you added to the
`ozagia` MCP entry (`autoApprove`, `disabled`, `timeout`) are kept. Install
touches only the formats you ask for; uninstall removes all of them.

The hook file:

```json
{
  "version": "v1",
  "hooks": [
    {"name": "ozagia-session-brief", "description": "Added by `ozagia install kiro`; ...",
     "trigger": "SessionStart",
     "action": {"type": "command", "command": "/path/to/python -m ozagia.integrations.kiro hook"},
     "timeout": 20},
    {"name": "ozagia-pre-shell",  "trigger": "PreToolUse",  "matcher": "shell", ...},
    {"name": "ozagia-pre-read",   "trigger": "PreToolUse",  "matcher": "read",  ...},
    {"name": "ozagia-pre-write",  "trigger": "PreToolUse",  "matcher": "write", ...},
    {"name": "ozagia-post-read",  "trigger": "PostToolUse", "matcher": "read",  ...},
    {"name": "ozagia-post-write", "trigger": "PostToolUse", "matcher": "write", ...},
    {"name": "ozagia-post-shell", "trigger": "PostToolUse", "matcher": "shell", ...},
    {"name": "ozagia-post-web-fetch", "trigger": "PostToolUse", "matcher": "web_fetch", ...},
    {"name": "ozagia-post-compact", "trigger": "PostCompact", ...}
  ]
}
```

`ozagia-post-compact` is skipped by the 2.37 V3 loader (it does not know
the trigger) and runs on CLI 3.1.0 and later. The profile has the first seven
hooks with the triggers `agentSpawn`,
`preToolUse` and `postToolUse`. The matchers are Kiro's tool categories:
in V3 they match a tool's tags (`read`: file reads, grep, file search,
directory listing; `write`: fs_write, fs_append, str_replace; `shell`:
execute_bash), and the V2 engine has the same groups. A plain word is also
tried as a regex against the tool ID, so an MCP tool whose name contains
`read` fires the hook too; the hook ignores tools it does not know.

The MCP entry:

```json
{"mcpServers": {"ozagia": {
  "command": "/path/to/python",
  "args": ["-m", "ozagia.integrations.mcp.server", "--root", "/path/to/project", "--mode", "coding"],
  "env": {"OZAGIA_ROOT": "/path/to/project"}}}}
```

(`env` also has `PYTHONPATH` when `--python` is the interpreter running the
installer.) Kiro shows MCP tools to hooks as `mcp_ozagia_<tool>`.

### Trust

The V3 engine runs workspace hooks, agents and MCP servers only in a
trusted workspace. The first `kiro-cli chat` in the project asks "Trust
this workspace?"; `--trust-workspace` trusts it for one launch without
saving.

### The daemon

As for Codex (docs/codex.md, "The daemon"): a hook starts the graph daemon
(`ozagia daemon ensure`) when none answers, waits at most 3 s, and only for
a git work tree, `$OZAGIA_ROOT` or a root with a graph snapshot. The daemon
stops after 30 minutes without requests (`OZAGIA_DAEMON_IDLE_TIMEOUT`).
`SessionStart` starts it so the snapshot is loaded by the first tool call.
`OZAGIA_DAEMON_AUTOSTART=0` turns autostart off.

## What each hook does

| Kiro event | tools | what ozagia does | what Kiro gets |
|---|---|---|---|
| `SessionStart` (V3), `agentSpawn` (V1/V2) | | In a git work tree only: starts the daemon (0.5 s budget) and prints the brief "since the last session": dirty files (at most 15), the last 3 commits, the tests still failing in the root's test record, running jobs (from a running daemon), the MCP tools to prefer; at most 1,500 characters. | stdout, exit 0: added to the agent's context |
| `PreToolUse` | shell: `execute_bash`, `execute_pwsh`, `execute_cmd`, `shell` | A ref-moving git command (`ozagia.intent.shell.classify` is `git_refs`) is refused, locally. | exit 2, the reason on stderr: the tool is blocked and the model gets the reason |
| `PreToolUse` | read: `read_file`, `readFile`, `read_files`, `fs_read`, `read` | When every requested path is a node key (`sym:`, `test:`, `doc:`, `text:`, `json:`, `yaml:`, `toml:`; also after `<cwd>/`), the daemon answers each (`tool` op, `read`, at most 8). If it handled any, the built-in is blocked with the answers. Real paths, mixed batches and image reads run as usual. | exit 2, the node text on stderr |
| `PostToolUse` | see the mapping below | `after` with the neutral tool and input and the tool's result; the daemon re-ingests changed files and counts the call. | stdout, when the `append` is not empty (but see "Limits") |
| `PostCompact` (CLI 3.1.0+) | | `hook.compaction_context`: asks a running daemon (3 s, no autostart) for the session's working set (`working_set` op: edited files and keys, failing tests and their rerun command, running jobs, the DevSession step, recent reads; see docs/codex.md, "Working set"), then sends `session_event` `compact`. Install registers it in the V3 hook file only (see below). | stdout, at most 3,000 characters (Kiro's cap), added after the summary |
| `SessionEnd` (V3) | | Sends `session_event` `end`, which clears the session's read ledger. The CLI 2.x engines have no session-end trigger, so profiles do not get it. | nothing |
| `PreCompact` | | Nothing, and not registered: its stdout would be summarised away, and a non-zero exit would cancel the compaction. | nothing |

Every request has `harness: "kiro"` and `session` = `session_id`; the
daemon counts requests by harness (`stats.by_harness`). Each mapped event also
sends one metadata-only `profile_event` (session start/end/compact, tool
started/terminal counts and any host-supplied model, duration or numeric token
counts); prompts, paths, command text and tool bodies are never stored.

### Tool mapping

Tool names and input fields come from Kiro's tool definitions and from
counting tool names and argument names (no content) in
`~/.kiro/sessions/*/*/messages.jsonl` on one machine.

| Kiro tool (input) | neutral tool (input) |
|---|---|
| `execute_bash` / `execute_pwsh` / `execute_cmd` / `shell` (`command`, `cwd?`, `timeout?`, `description?`) | `shell` (`command`, `workdir` = `cwd` resolved against the event's `cwd`, else the event's `cwd`, `timeout`, `description`) |
| `read_file` / `readFile` (`path`, `offset?`, `limit?`) | `read` (`path`, `offset + 1`, `limit`): Kiro's offset counts from 0, the protocol's from 1 |
| `read_files` (`paths` or `files` of one path) | `read` (a batch of several files is not sent) |
| `fs_read` / `read` (`operations: [{mode: Line|Directory, path, offset?, limit?}]`) | `read` (one operation; `Image` mode is not sent) |
| `list_directory` (`path`, `depth?`) | `read` (`path`) |
| `str_replace` (`path`, `oldStr`, `newStr`, `replace_all`) | `edit` (`path`, `oldString`, `newString`, `replaceAll`) |
| `fs_write` (`path`, `text`), or `fs_write` / `write` with `command: create` (`content` or `file_text`) | `write` (`path`, `content`) |
| `fs_write` / `write` with `command: strReplace` or `str_replace` (`oldStr`/`old_str`, `newStr`/`new_str`) | `edit` |
| `fs_append` (`path`, `text`); `fs_write` with `command: insert` or `append` | `write` (`path` only; the daemon re-reads the file) |
| `grep_search` (`query`, `includePattern?`, ...) / `grep` (`pattern`, `path?`, `include?`) | `grep` (`pattern`, `path`, `include`, `literal: false`) |
| `glob` (`pattern`, `path?`) | `glob` |
| `file_search` (`query`, a path substring) | `glob` (`**/*<query>*`, an approximation) |
| `web_fetch` (`url`, `mode?`, `searchPhrase?`) | `webfetch` (`url`): `after` only; a failed fetch of a paper gets a `research_fetch` hint (docs/research.md, "Fetch fallback"). Hook `ozagia-post-web-fetch`, matcher `web_fetch`. |
| `delete_file`, MCP tools, anything else | not sent |

`tool_response` is a string in V3 (the tool's result message); an object
with a `result` list or string is flattened, and other shapes as for
Claude Code (`hookcore.response_text`).

### Exit codes and output

The hook answers in plain text, not JSON. Exit 0 with stdout on
`SessionStart` and `PostToolUse`; exit 2 with stderr only on `PreToolUse`
to block a tool. It never prints `hookSpecificOutput.permissionDecision`.

Failures never block the agent. If the daemon is down, slow or returns an
error, or the hook input is malformed, the hook prints nothing and exits 0
(the tool runs), and appends one JSON line (`ts`, `event`, `tool`,
`session`, `error`) to `<state dir>/kiro_hook.log`. The handler timeout
install writes is 20 s (`HOOK_TIMEOUT_S`; `timeout_ms: 20000` in an
object-form profile, which V3 ignores and replaces with its default of
60 s); the hook stops itself at 15 s (`HOOK_ALARM_S`). Autostart waits at
most 3 s and a daemon request has 10 s; if the alarm fires while
`SessionStart` starts the daemon, the brief still gets 3 s.

## Uninstall

```
python -m ozagia.integrations.kiro uninstall --workdir <project> [--scope user] [--agent NAME]
```

This removes ozagia's hooks from the hook file and the agent profiles
(whichever exist) and the `ozagia` server from `mcp.json`. Without
`--agent` it looks at every `.kiro/agents/*.json` (so a profile from
`install --agent NAME` is cleaned too; a profile it cannot parse is
skipped); with `--agent NAME` only at that one. Everything else stays. A hook file left with no hooks, a profile that is then exactly what
install created, and an `mcp.json` with no other server or key are deleted.
(If Kiro changed the profile, for example `/upgrade-agent` adding
`kiroUpgrade`, the profile is kept without ozagia's hooks.) Directories
are left in place. A symlinked file is never deleted; it keeps what is
left after ozagia's entries are removed.

## Limits

- **PostToolUse output does not reach the model in 2.37.** In the V3
  engine bundled with 2.37, a hook's stdout is passed on only for
  `SessionStart`, `UserPromptSubmit` and `PreTaskExec` with exit 0, and
  stderr only for exit 2 on `UserPromptSubmit`, `PreToolUse` and
  `PreTaskExec`; PostToolUse output is dropped whatever the exit code. The
  Kiro docs for CLI 2.x say the same for `postToolUse` (exit 0 is success;
  other codes show stderr to the user). The PostToolUse hooks still keep
  the graph current after edits and shell commands and feed the daemon's
  counters; the context they print is there for a version that passes it
  on.
- The V1 engine treats `read`, `write` and `shell` as aliases for one
  tool each (`fs_read`, `fs_write`, `execute_bash`), so its grep and glob
  calls do not run the hooks.
- Node-key reads work by blocking the built-in. Kiro wraps the stderr in a
  `<HOOK_INSTRUCTION>` block that says the tool was not executed; the
  message tells the model it is the result.
- A `read_files` batch is not sent to `after` (the protocol reads one
  path), so batched reads are not counted.
- The git refusal is best-effort parsing; use Kiro's permissions
  (`permissions.rules` with `capability: shell`, `effect: deny`) for a
  hard rule.

## Open questions (need a live Kiro session)

- The exact stdin per engine: field names (`tool_response` in V1/V2, the
  `tool_tags` the V2 harness adds), event names (V1/V2 should send
  `agentSpawn`, `preToolUse`, `postToolUse`), and whether V1/V2 tool names
  are `fs_read`/`fs_write`/`execute_bash` or the `read`/`write`/`shell`
  seen in session records.
- Whether the V2 `fs_read` `offset` counts from 0 like V3's `read_file`.
- Whether any engine passes PostToolUse stdout to the model.
- Whether an exit-2 `PreToolUse` block on V1/V2 gives the model the full
  stderr (and how much of it).
- Whether `kiro-cli acp` (the IDE path) runs workspace hook files.

### The probe hook

`scripts/kiro_probe_hook.py` answers these without ozagia. It records each
event's stdin, argv, working directory and the names (not values) of the
environment variables as one JSON file per event, prints nothing and exits
0. Run it in a scratch workspace, never a real project:

```
python3 scripts/kiro_probe_hook.py install --workdir /tmp/kiro-probe [--echo]
cd /tmp/kiro-probe && git init -q && echo 'x = 1' > a.py
kiro-cli chat --v3                        # trust the workspace; ask it to read a.py,
                                          # run `ls`, edit a.py, then quit
kiro-cli chat --v2 --agent ozagia-probe   # the same with the V2 engine
python3 scripts/kiro_probe_hook.py show --workdir /tmp/kiro-probe
python3 scripts/kiro_probe_hook.py uninstall --workdir /tmp/kiro-probe
```

`install` copies the script to `/tmp/kiro-probe/.ozagia/kiro_probe_hook.py`
and every hook runs that copy with `|| true` appended, so a probe hook
always exits 0 even if this checkout goes away (Python exits 2 when its
script is missing, and exit 2 on `PreToolUse` would block every tool). It
writes `.kiro/hooks/ozagia-probe.json` (V3; `SessionStart`,
`UserPromptSubmit`, `PreToolUse` and `PostToolUse` on `*`, `Stop`, and
`PreCompact`/`PostCompact`, which 2.37 skips) and
`.kiro/agents/ozagia-probe.json` (the CLI 2.x triggers). The records go to
`/tmp/kiro-probe/.ozagia/kiro_probe/` (or `$OZAGIA_KIRO_PROBE_DIR`); `show`
prints each event's keys, tool name, input keys and response type, not
their content. With `--echo` every hook prints `ozagia-probe-marker
<event> <tool>` on stdout: ask the agent at the end whether it saw such a
line after a tool call to learn whether PostToolUse stdout reaches the
model. These sessions spend model tokens, so a person runs them.
