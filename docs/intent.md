# Intent layer and graph daemon

A harness plugin (W2-B) captures the agent's built-in tool calls and sends
them to the graph daemon. The daemon carries out each call on the repository
graph and answers in the built-in tool's own output format, with graph
structure added on labelled lines. The protocol is in
`src/ozagia/daemon/protocol.py`; the mapping is `src/ozagia/intent/`.

```
python -m ozagia.daemon serve --root <repo> [--dump <graph>] [--journal <jsonl>] [--sock <path>] [--no-build]
python -m ozagia.daemon ping|stats|shutdown [--root <repo> | --sock <path>]
```

## Rules

- **Verbatim source.** A source line is `<n>: ` followed by the file's exact
  text (line ending and a UTF-8 BOM dropped). Source lines are never dropped
  or reordered.
- **Labelled additions.** Every graph addition is its own line starting with
  `[ozagia] `, or (directory and glob entries) a suffix `  [ozagia] ...`.
- **Paths** may be absolute or relative to the root. A path outside the root
  (after resolving symlinks) is refused as a tool error. Writes into the
  state directory are refused.
- **Fallback.** A binary, non-UTF-8 or larger than 2 MiB file, a missing
  file, `patch`, an unmapped shell command, or any exception inside a handler
  returns `handled: false`, and the plugin runs the built-in.

Every response has `meta = {intent, keys, ms}`.

## read

```
Read file /repo/pkg/mod.py, lines 1-20
[ozagia] file:pkg/mod.py  imports: 0  imported by: 2  tests: 2
1: """Module."""
2: import os
3:
4:
[ozagia] sym:pkg/mod.py::helper  tests: 2 (test:tests/test_mod.py::test_helper, test:tests/test_mod.py::test_broken)
5: def helper(a, b=1):
...
```

- Default range: offset 1, limit 2000 lines. ` of <total>` follows when the
  range does not cover the file. An offset past the end is a tool error.
- The file header is shown for a file in the graph: `imports` and
  `imported by` count `imports` edges, `tests` counts tests that import or
  mention the file plus tests it holds.
- A symbol header precedes the first line (decorators included) of each
  module-level def/class of an indexed Python file. Its tests are the
  file's covering tests whose source spells the symbol's name, else all of
  them (the rule of `DevMemory.symbol`); up to 3 keys are listed. A test
  function shows only its `test:` key. When the range starts inside a
  symbol, its header comes first with `  (from line <start>)`. Callers are
  not shown (no cheap caller edges in the graph).
- Markdown in the graph: `[ozagia] doc:<rel>` after the file header.
  Unparsed text files (YAML, TOML, shell, ...): `[ozagia] text:<rel>#L<a>-L<b>`
  before each chunk (`ozagia.index.text_nodes`).
- A node key as `path` (`sym:`, `test:`, `text:`, `doc:`, `file:`) reads that
  node's line range with its header. An unknown key is a tool error that
  lists the nearest keys of the file.

Directory:

```
Read directory /repo/pkg, entries 1-3
__init__.py  [ozagia] 0 symbols, 0 tests
mod.py  [ozagia] 5 symbols, 2 tests
sub/
```

## grep

```
[ozagia] defined: sym:pkg/mod.py::helper
Found 7 matches
/repo/pkg/use.py:
[ozagia] file:pkg/use.py
  Line 2: from pkg.mod import helper
[ozagia] sym:pkg/use.py::run
  Line 6:     return helper(1, 2)

/repo/tests/test_mod.py:
...
```

- ripgrep with `--hidden`, `.gitignore` respected, `.git` and the state dir
  excluded, files over 2 MiB skipped; `literal` is `-F`, `include` is
  `--glob`, `path` limits the search. A Python `re` walk is used when `rg`
  is missing.
- Files newest first, matches in line order, each run of matches under the
  key that encloses it (innermost def/class, `doc:`, text chunk, else
  `file:`).
- `defined:` comes first when the pattern is an identifier that names graph
  Symbol or Test nodes (up to 10 keys).
- At most 100 matches are shown: the first line says
  `Found <N> matches (showing first 100)` and the last
  `(Results are truncated: showing 100 of <N> matches. ...)`.
  No match: `No files found`. A bad regex is a tool error.

## glob

Absolute paths, newest first, at most 100; graph files get
`  [ozagia] <n> symbols, <m> tests`. No match: `No files found`.

## edit and write

```
Edited /repo/pkg/mod.py (1 replacement)
[ozagia] in: sym:pkg/mod.py::helper
[ozagia] delta: 0 added, 0 removed, 2 changed, edges +0 -0: ~file:pkg/mod.py, ~sym:pkg/mod.py::helper
[ozagia] broken: arity sym:pkg/use.py::run pkg/use.py:6 helper — 2 positional given, at most 1; signature helper(a)
```

`DevMemory.span_edit(..., apply=True)` does the work (exact match, parse
guard, transaction, reingest, broken references). An `oldString` with no
exact match is `handled: false`: OpenCode's built-in edit retries with
whitespace- and indentation-tolerant matching, and `after` re-ingests what it
wrote. Tool errors:

```
Found multiple matches for oldString. Provide more surrounding lines in oldString to identify the correct match.
[ozagia] match: L7 sym:pkg/mod.py::helper: return a + b

Edit refused: the result does not parse (invalid syntax at line 19)
[ozagia] in: sym:pkg/mod.py::Box.m
```

An empty `oldString` writes `newString` as the file. `write` goes through
`ozagia.edit.txn` (backup of an existing file; atomic create of a new one,
parent directories made), then `reingest`:

```
Wrote file successfully.
[ozagia] replaced file:pkg/use.py  backup: /repo/.ozagia/edits/backups/...
[ozagia] delta: 1 added, 1 removed, 1 changed, edges +2 -3: +sym:pkg/use.py::other, -sym:pkg/use.py::run, ~file:pkg/use.py
```

## shell

`ozagia.intent.shell.classify(command)` (pure, `shlex`) maps only simple
commands: no operators, redirects, substitutions, variable or tilde
expansion, unquoted globs (except in `--include=`/`--glob=`), newlines or
environment prefixes.

| class | commands | what happens |
|---|---|---|
| pytest | `pytest`, `py.test`, `python[3] -m pytest`, `<path>/python -m pytest` | `ozagia.check.run_tests` with the named interpreter (a pytest script's `#!` line), the selectors made root-relative, the other flags, `timeout`/1000 s (default 300). Refused flags (cache, `--pdb`, `--junitxml`, ...) and ambiguous options fall back. |
| read | `cat F`, `head [-n N] F`, `tail [-n N\|+N] F`, `sed -n 'A[,B]p' F` | read of that range (no 2000-line cap) |
| grep | `grep [-rRnHiFE] [--include G] PAT [PATH]`, `rg [-nHiF] [-g G] PAT [PATH]`, `git grep [-niFE] PAT [PATH]` | grep; `-i` is case-insensitive. A basic regex with `\ + ? \| { } ( )`, or GNU-only escapes, falls back. |
| git_refs | `git commit\|push\|stash\|branch\|checkout\|switch\|reset\|rebase\|merge\|tag\|worktree\|cherry-pick\|revert\|am\|pull\|fetch\|bisect\|...` (`intent.shell.GIT_REF_MOVING`, shared with the Claude Code hook and `eval.agent.OPENCODE_GIT_DENY`), also inside a compound or multi-line command, after `VAR=value` prefixes, as `/path/to/git`, through `git -c alias.<x>=<ref-moving>`, inside a backtick or `$(...)` substitution, and inside `sh\|bash\|zsh -c '<script>'` (classified once more; a script nested deeper is refused when it names `git` and then a ref-moving subcommand) | tool error `Refused by ozagia: <command> changes git refs`. Listing forms (`git branch`, `git tag -l`, `git stash list`, `git worktree list`, `git bisect log`) are read-only. |
| git_read, other, complex | everything else | `handled: false` |

pytest output is pytest's own output tail, then:

```
[ozagia] pytest failed: exit 1, 0.4 s, 1 passed, 1 failed, 0 error, 0 skipped
[ozagia] failed test:tests/test_mod.py::test_broken: assert 3 == 5
[ozagia]   frame test:tests/test_mod.py::test_broken tests/test_mod.py:9
```

A captured pytest runs with the timeout `input.timeout` (ms), else 300 s.
The native loop sets the reserved fields `_deadline_s_remaining` (the
timeout is clamped to it, at least 0.1 s) and `_test_env` (the environment
replaces the inherited one); it also passes the plain shell's default
timeout, `harness.tools.DEFAULT_SHELL_TIMEOUT_MS`, when the model gave none.
The daemon sets `_test_env` to its own environment without `OZAGIA_*`,
`PYTHONPATH` and `COLUMNS`, and drops any `_` field a client sends. Reserved
fields never appear in an output.

Edits and writes refuse paths inside ozagia's state directory and paths
with a `.git` component (at any depth, also through a symlink). Once the
file is written, a failure in the journal or the reingest is reported as an
`[ozagia] warning: <error>` (or `[ozagia] graph update failed: ...`) line in
a handled result; it never turns into `handled: false`, which would make
the plugin apply the change a second time.

## after

`after(tool, input, output)` runs after a built-in call. For edit, write,
patch and shell commands that are not read-only (read, grep, git_read and
refused classes are), it reingests `memory.pending_changes()` and appends
the delta line; more than `MAX_REFRESH_FILES` (200) changed files appends
`[ozagia] many files changed; graph refresh deferred` instead. For
`git diff` / `git show` it appends `[ozagia] touched: <keys>`: the key
enclosing each `+` line, and the line before each deletion point, in the
current file.

## Daemon

One process per repository. `socketserver.ThreadingUnixStreamServer`, a
thread per connection, one lock around `tool` and `after` (`DevMemory` is
not thread-safe), except a captured pytest: it reads no graph state and
runs without the lock, so edits and reads sent while tests run are answered
at once. Requests are JSON lines, answered in order; a line over
`MAX_MESSAGE_BYTES` gets an error and the connection is closed. A request
with a `deadline_ms` that has passed when its turn comes is answered
`expired` and not executed (`ozagia.daemon.protocol`). The socket
is mode 0600; a stale socket file (no answer to `ping`) is replaced, a live
daemon is an error (exit 2). `shutdown`, SIGTERM and SIGINT stop it and
remove the socket; with `--parent-pid` it also exits within
`PARENT_POLL_S` (2 s) of that process going away (the harnesses pass their
own pid). The journal (`<state dir>/daemon_journal.jsonl`) has one
record per request: `ts, op, tool, intent, handled, is_error, expired, ms,
keys (≤20), reason, fallback, session, harness`. `stats` returns counts by
op, tool, intent and handled; `passthrough` (not handled by design),
`expired`, `after_expired`, `late` (handled after the deadline),
`plugin_fallbacks` (`after` requests with `meta.fallback`),
`unknown_outcomes`, `fallbacks` (= `plugin_fallbacks`: one per call the
plugin could not get answered; `expired` and failed `tool` requests are the
daemon's view of some of the same calls), errors, hellos and their sessions, mean and max ms and
uptime.

`ozagia.core.memory` takes its layout from `$OZAGIA_ROOT` when imported, so
`serve` sets it to `--root` first. `DevMemory`'s own journal goes next to a
`--dump`, else to the state dir.

## Known limits

- Handled calls bypass OpenCode's own permission prompts. The plugin
  replaces the tools' `execute`, so an edit, write or captured pytest the
  daemon handles never reaches OpenCode's permission check (`ask` rules).
  `ozagia agent` and the eval run with the eval's policy (`bash` allow-all
  except ref-moving git), so nothing is lost there; an interactive session
  with `ask` rules does not get its prompts for handled calls.
