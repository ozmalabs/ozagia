# Claude Code hooks (partial capture)

`ozagia.integrations.claude_code` connects Claude Code to the ozagia graph
daemon through Claude Code's command hooks. Claude Code keeps running its own
built-in tools. The hooks add graph context to their results and refuse a few
calls before they run. The OpenCode plugin replaces the built-ins; this
integration does not (see "What does not work").

**Status: untested live.** Claude Code is logged out on the development
machine, so the hooks have only been run as subprocesses against a stub
daemon, with payloads shaped like the examples in the hooks reference
(`tests/test_claude_hooks.py`).

## Reference implemented against

- Hooks reference: <https://code.claude.com/docs/en/hooks> (markdown at
  `.../hooks.md`), page `last-modified: Sat, 14 Nov 2026 19:15:52 GMT`,
  fetched 2026-11-15. The newest version the page mentions is v2.1.319.
- Tools reference: <https://code.claude.com/docs/en/tools-reference>, fetched
  the same day.
- Latest release in the Claude Code changelog on that day: 2.1.325.

## What works

| Claude Code call | hook | what ozagia does | what Claude gets |
|---|---|---|---|
| `Read`, `Grep`, `Glob` | PostToolUse | Sends `after` with the mapped input and the built-in's result. | The built-in's result, plus the daemon's `append` (node keys, callers, tests for the read range; grep matches grouped by node) as `additionalContext`. |
| `Edit`, `MultiEdit`, `Write` | PostToolUse | Sends `after`; the daemon re-ingests the file. | The built-in's result, plus the graph delta and broken references. |
| `Bash` | PostToolUse | Sends `after` as `shell`. The daemon maps `pytest` runs to per-test results and keys, and re-ingests files the command changed. | Raw output, plus the daemon's context. |
| `Bash` running a ref-moving git command | PreToolUse | Refuses it locally, without the daemon, with the intent layer's check (`ozagia.intent.shell.classify` is `git_refs`; the list is `GIT_REF_MOVING`: `commit`, `push`, `stash`, `branch`, `checkout`, `switch`, `reset`, `rebase`, `merge`, `tag`, `worktree`, `cherry-pick`, `revert`, `am`, `pull`, `fetch`, `update-ref`, `symbolic-ref`, `filter-branch`, `filter-repo`, `replace`, `notes`, `bisect`). It looks in every segment (`&&`, `;`, `\|`, newlines, subshells), past `VAR=value` prefixes, shell keywords, wrapper commands (`env`, `xargs`, `timeout`, `nice`, `sudo`, `command`, `exec`, `eval`, `find -exec`, ...), git's global options and `/path/to/git`, into substitutions, `bash -c` scripts and `git -c alias.*`. Listing forms (`git branch`, `git tag -l`, `git stash list`, `git worktree list`) are allowed. | `permissionDecision: "deny"` with a reason. |
| `Read` of a node key (**experimental**) | PreToolUse | If `file_path` is a key (`sym:`, `test:`, `doc:`, `text:`), asks the daemon with the `tool` op (`read`, `path` = the key). | When the daemon handles it, the built-in Read is denied and the daemon's output is the `permissionDecisionReason`. |
| `Read` of a path, `Bash` with a shell read | PreToolUse | Asks the daemon's `read_check` whether the read repeats an unchanged range this session already read (`docs/intent.md`, read ledger). Claude Code's default repeat policy is `stub`: subagent calls carry `agent_id`, sent as the ledger's `sub`, so a subagent never gets a stub for its parent's reads. | A repeat is denied with the one-line stub as the `permissionDecisionReason`; otherwise nothing, and PostToolUse records the read. |
| `SessionStart` (`startup`, `resume`, `clear`, `compact`), `SessionEnd` | SessionStart / SessionEnd | `session_event`: `compact` for source `compact`, else `start`; `end` on SessionEnd. Clears (drops) the session's read ledger. | nothing |

The node-key Read is a trick: the reference says the reason of a `"deny"` is
shown to Claude, so the node's text reaches the model as the denial message.
Claude may treat it as an error rather than a file read. Claude Code makes
`file_path` absolute before hooks run, so a key may arrive as
`<cwd>/sym:src/a.py::f`; the hook takes the key from the first path segment
that starts with a key prefix, and only when no file exists at that path.
When the daemon does not handle the key, the hook prints nothing and the
built-in Read runs (and fails as a missing file).

Failures never block the agent. If the daemon is down, slow or returns an
error, or the hook input is malformed, the hook prints nothing and exits 0.
Each failure is appended as one JSON line (`ts`, `event`, `tool`, `session`,
`error`) to `<state dir>/claude_hook.log` (`.ozagia/claude_hook.log` by
default). A daemon request has 15 s (`DAEMON_TIMEOUT_S`) in total; the hook
stops itself at 20 s (`HOOK_TIMEOUT_S`, also its `timeout` in the settings).
The hook never exits 2.

## What does not work

- **The built-in result is never replaced.** Claude sees the raw output and the
  context next to it, so a read or grep costs the built-in's tokens plus
  ozagia's. The current reference does document a PostToolUse
  `updatedToolOutput` that replaces a built-in's result if the value matches the
  tool's output schema (for `Bash`: `{stdout, stderr, interrupted, isImage}`).
  This integration does not use it yet.
- Edits are not applied as node edits. The built-in `Edit` writes the file; the
  delta comes afterwards. PreToolUse could rewrite an `Edit` through
  `updatedInput`, but nothing here does.
- `additionalContext` is capped at 10,000 characters by Claude Code. Longer
  context is saved to a file and Claude sees the path plus a 2,000-character
  preview, which it is not asked to read.
- On macOS, Linux and WSL, Claude Code leaves `Grep` and `Glob` out of the
  default tool set. Claude searches with `grep` and `find` through `Bash`, so
  those searches reach ozagia as `shell` calls, and the daemon's shell
  classifier decides what to do with them.
- `MultiEdit` is not in the current tools reference. The matcher and mapping
  stay for older versions.
- The `PowerShell` and `Monitor` tools are not mapped. A `PreToolUse` hook with
  a `Bash` matcher also runs for `Monitor` commands, so the git refusal covers
  them.
- The git refusal is best-effort parsing. A command that hides git behind
  `eval`, `sh -c`, a script or `$(...)` gets through. Use Claude Code's
  permission rules for a hard rule.

## Setup

1. Install ozagia into a Python environment (`uv pip install -e .` from this
   repository).
2. Start the daemon for the project:
   `python -m ozagia.daemon serve --root <project>`. The hook finds it at
   `$OZAGIA_DAEMON_SOCK`, else at `protocol.socket_path(<root>)`, where
   `<root>` is the Git top level of the hook's `cwd` (or `$OZAGIA_ROOT`).
3. Install the hooks:

   ```
   python -m ozagia.integrations.claude_code install --workdir <project>
   ```

   This writes `<project>/.claude/settings.local.json` (personal, not shared).
   `--settings project` writes `.claude/settings.json` instead, which can be
   committed. `--python` chooses the interpreter the hook runs with (default:
   the one running the command). Existing settings and hooks are kept, and
   running it again does not add duplicates. Claude Code only gitignores
   `settings.local.json` when it writes the file itself, so check your
   `.gitignore`.
4. Remove them with
   `python -m ozagia.integrations.claude_code uninstall --workdir <project>`
   (both files unless `--settings` is given). Only ozagia's handlers are
   removed.

The entries look like this:

```json
{
  "hooks": {
    "PostToolUse": [{"matcher": "Read|Grep|Glob|Edit|MultiEdit|Write|Bash",
                     "hooks": [{"type": "command", "timeout": 20,
                                "command": "/path/to/python -m ozagia.integrations.claude_code hook"}]}],
    "PreToolUse": [{"matcher": "Bash|Read",
                    "hooks": [{"type": "command", "timeout": 20,
                               "command": "/path/to/python -m ozagia.integrations.claude_code hook"}]}],
    "SessionStart": [{"matcher": "startup|resume|clear|compact",
                      "hooks": [{"type": "command", "timeout": 20,
                                 "command": "/path/to/python -m ozagia.integrations.claude_code hook"}]}],
    "SessionEnd": [{"matcher": "",
                    "hooks": [{"type": "command", "timeout": 20,
                               "command": "/path/to/python -m ozagia.integrations.claude_code hook"}]}]
  }
}
```

ozagia's handlers are the command handlers whose command ends in
`-m ozagia.integrations.claude_code hook`.

## Input mapping

Claude Code passes the tool input with absolute file paths. The hook converts
it to the protocol's OpenCode-style fields (`ozagia.daemon.protocol`), with
`harness: "claude"` and `session` = `session_id`:

| Claude Code | protocol tool | protocol input |
|---|---|---|
| `Read {file_path, offset, limit}` | `read` | `{path, offset, limit}` |
| `Grep {pattern, path, glob, ...}` | `grep` | `{pattern, path, include: glob, literal: false}`. Claude's Grep is always a ripgrep regex. `-i`, `type`, `output_mode` and `multiline` are not passed. |
| `Glob {pattern, path}` | `glob` | `{pattern, path}` |
| `Edit {file_path, old_string, new_string, replace_all}` | `edit` | `{path, oldString, newString, replaceAll}` |
| `MultiEdit {file_path, edits[]}` | `edit` (one `after`) | `{path, edits: [{oldString, newString, replaceAll}]}` |
| `Write {file_path, content}` | `write` | `{path, content}` |
| `Bash {command, timeout, description}` | `shell` | `{command, workdir: cwd, timeout, description}`; `timeout` stays in milliseconds. |

`output` is the built-in's result as text. PostToolUse passes the tool's
structured output object, so the hook takes `stdout` and `stderr` for Bash,
`file.content` for Read, `content` or `filenames` for Grep and Glob, and
otherwise the JSON of the object without `originalFile` (Edit echoes the whole
file).
