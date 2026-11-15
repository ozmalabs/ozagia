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
guard, transaction, reingest, broken references). Tool errors:

```
oldString not found in content
[ozagia] nearest: L5 sym:pkg/mod.py::helper: def helper(a, b=1):

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
| git_refs | `git commit\|push\|stash\|branch\|checkout\|switch\|reset\|rebase\|merge\|tag\|worktree\|cherry-pick\|revert\|am\|pull\|fetch\|...`, also inside a compound command | tool error `Refused by ozagia: <command> changes git refs`. Listing forms (`git branch`, `git tag -l`, `git stash list`, `git worktree list`) are read-only. |
| git_read, other, complex | everything else | `handled: false` |

pytest output is pytest's own output tail, then:

```
[ozagia] pytest failed: exit 1, 0.4 s, 1 passed, 1 failed, 0 error, 0 skipped
[ozagia] failed test:tests/test_mod.py::test_broken: assert 3 == 5
[ozagia]   frame test:tests/test_mod.py::test_broken tests/test_mod.py:9
```

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
not thread-safe). Requests are JSON lines, answered in order; a line over
`MAX_MESSAGE_BYTES` gets an error and the connection is closed. The socket
is mode 0600; a stale socket file (no answer to `ping`) is replaced, a live
daemon is an error (exit 2). `shutdown`, SIGTERM and SIGINT stop it and
remove the socket. The journal (`<state dir>/daemon_journal.jsonl`) has one
record per request: `ts, op, tool, intent, handled, is_error, ms, keys (≤20),
reason, session, harness`. `stats` returns counts by op, tool, intent and
handled, fallbacks, hellos and their sessions, mean and max ms and uptime.

`ozagia.core.memory` takes its layout from `$OZAGIA_ROOT` when imported, so
`serve` sets it to `--root` first. `DevMemory`'s own journal goes next to a
`--dump`, else to the state dir.
