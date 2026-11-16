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
  reading by key, outlines and impact.

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

## What each hook does

| Codex event | hook | what ozagia does | what Codex gets |
|---|---|---|---|
| `SessionStart` (every `source`) | brief | In a git work tree only: starts the daemon if needed (waits at most 0.5 s, not for the snapshot load), and lists the dirty files (`git status --porcelain`, at most 15), the last 3 commits, and the ozagia MCP tools to prefer. At most 1,500 characters. | `additionalContext` |
| `PreToolUse` `Bash` with a ref-moving git command | refusal | Refuses it locally, without the daemon, with the intent layer's check (`ozagia.intent.shell.classify` is `git_refs`; see docs/claude_code.md for the list and the parsing). Listing forms (`git branch`, `git tag -l`, `git stash list`) are allowed. | `permissionDecision: "deny"` with a reason |
| `PreToolUse` `apply_patch` | patch check | Checks the patch against the current files locally, as Codex's apply_patch matches hunks (see "Patch check" below). If every hunk matches, prints nothing. If one would fail, refuses the call with the nearest region of each failing hunk; only then, if a daemon is running, it is asked too (at most 1.5 s, never started for this), for the graph's keys and to count the miss. A matching patch never waits for the daemon. | nothing, or `permissionDecision: "deny"` with the regions as the reason |
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
preferred). Its key is the enclosing symbol (`sym:`/`test:`), Markdown
section (`doc:<file>#<slug>`), JSON value (`json:<file>#<pointer>`) or text
chunk (`text:<file>#L<a>-L<b>`, for `.tex` and other unparsed files). The
`node` line gives that node's full line range and the sha256 of its current
text: the `base_sha256` a key edit (`dev_key_edit`) of it takes. A key edit
of part of a file does not take the file's sha256, since the region may show
only part of the node it would replace. Lines are counted at CRLF, lone CR
and LF, the same in the reply and in key edits. A
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
the marked section from `AGENTS.md`. Everything else stays. A file left with
nothing in it is deleted. The daemon stops on its own after the idle
timeout, or with `ozagia daemon shutdown --root <project>`.

## Limits

- `write_stdin` polls (input to a running interactive command) do not run
  hooks, so output read that way gets no context.
- The built-in result is never replaced. ozagia does not use PostToolUse
  `decision: "block"` or PreToolUse `updatedInput`.
- The patch check reads the files when the hook runs. A file changed
  between the check and Codex's own apply is not seen.
- Codex spills `additionalContext` over about 2,500 tokens to a file and
  gives the model a preview. The handlers do not set
  `additionalContextLimit`; the brief is far below the limit, and the
  daemon's `append` normally is.
- The git refusal is best-effort parsing; use Codex's sandbox and approval
  settings for a hard rule.
- The user-scope MCP server serves the repository of its working directory.
  Whether Codex starts MCP servers in the session's directory has not been
  checked live.
