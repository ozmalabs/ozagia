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

Structured data (`ozagia.core.structured`). A `path` of the form
`json:<rel>#<pointer>`, `yaml:<rel>#<pointer>` or `toml:<rel>#<pointer>`
(an RFC 6901 JSON pointer; `#` alone or nothing is the root; `~1` is `/`
and `~0` is `~` in a key) reads the node at the pointer:

```
Read json /repo/data/run.json#/runs: array, 30 items, line 5
[ozagia] json:data/run.json#/runs
[2] {"id":2,"ok":true}  (line 14)
[3] {"id":3,"ok":false}  (line 18)
[ozagia] items 2-3 of 30 (0-based index); offset 5 shows the next ones
[ozagia] query a member: json:data/run.json#/runs/<index>
```

- An object lists its keys with type, size and a short preview (default 50
  keys), an array its items (default 20), a scalar its value. `offset` and
  `limit` page the keys or items; the read tool's `offset` is 1-based
  (offset 1 is index 0), `dev_query`'s is the 0-based index. Output is
  capped at 8 kB (`OUTPUT_CAP`) with a truncation line.
- JSON numbers print as written (`1.50`). Lines come from the span parser
  of `edit.json_edit` for JSON files up to 1 MB.
- A missing file, a bad pointer (with the nearest keys, `difflib`) and a
  parse error (with line and column) are tool errors.
- YAML needs PyYAML (the `iac-yaml` extra); without it a YAML query is a
  tool error that says so. A multi-document YAML file is the list of its
  documents. TOML uses `tomllib` (read-only); dates print as ISO strings.
- The same query is the MCP tool `dev_query {path, pointer?, format?,
  offset?, limit?}`; `path` may also be a `json:`/`yaml:`/`toml:` key.

Outline. A read with no `offset` and no `limit` of a `.json`, `.yaml`,
`.yml` or `.toml` file over 2000 lines or 64 kB returns the outline
instead of the text: the file header, `[ozagia] json:<rel>#`, the root's
type and size, each top-level key with type, size, preview and line, one
level below it (up to 8 keys, or an array's first item), and a last line
saying how to query a path or read the text (pass `offset`/`limit`). The
limits are `[reads] structured_outline_lines` and
`structured_outline_bytes` in `.ozagia.toml` (0 turns one off). A file
that does not parse (or YAML without PyYAML) is read as text. Shell reads
(`cat`, `head`, ...) and edits are unchanged.

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

`ozagia.intent.shell.classify(command)` (pure: it reads no files) maps a
simple command, or a compound command made of simple steps.

A simple command has no operators, redirects, substitutions, variable or
tilde expansion, unquoted globs (except in `--include=`/`--glob=`), newlines
or environment prefixes. The classes are:

| class | commands | what happens |
|---|---|---|
| pytest | `pytest`, `py.test`, `python[3] -m pytest`, `<path>/python -m pytest` | `ozagia.check.run_tests` with the named interpreter (a pytest script's `#!` line), the selectors made root-relative, the other flags, `timeout`/1000 s (default 120). Refused flags (cache, `--pdb`, `--junitxml`, ...) and ambiguous options fall back. |
| read | `cat F`, `head [-n N] F`, `tail [-n N\|+N] F`, `sed -n 'A[,B]p' F` | read of that range (no 2000-line cap) |
| grep | `grep [-rRnHiFE] [--include G] PAT [PATH]`, `rg [-nHiF] [-g G] PAT [PATH]`, `git grep [-niFE] PAT [PATH]` | grep; `-i` is case-insensitive. A basic regex is converted to the Rust/Python dialect (`\|` `\(` `\{` become operators, `+ ? \| { } ( )` literals); one with back-references, `\<`, `\>` or POSIX classes falls back. |
| jq | `jq [-r] [-c] [-S] [-M] FILTER FILE` (flags before the filter, also clustered as `-rc`), `cat FILE \| jq ...`, `jq ... < FILE`, `< FILE jq ...`, and after `cd DIR &&`. FILTER is a pipeline (up to 6 stages) of paths (`.`, `.a`, `.a.b`, `."key with space"`, `.["k"]`, `.[N]`, `.[-N]`, `.[N:M]`, `.[]`, `.a[]`) and `keys`, `length`, `type` (`ozagia.core.jq`) | jq's exact stdout, read-only (no graph refresh). Left to the real jq: any other filter or option (reason `jq filter '...' is not mapped` / `jq option X is not mapped`), several or no files, a result jq reports as an error (`.a` of an array, `.[]` of null, `keys` of a string, ...), a number with an exponent (jq 1.7 rewrites it), with jq older than 1.7 any number that is not an integer below 1e17, a computed non-integer (`length` of 2.5), a BOM, NaN, a lone surrogate escape, several JSON values, output over 1 MB. |
| compound | see below | the steps, in order |
| git_refs | `git commit\|push\|stash\|branch\|checkout\|switch\|reset\|rebase\|merge\|tag\|worktree\|cherry-pick\|revert\|am\|pull\|fetch\|bisect\|...` (`intent.shell.GIT_REF_MOVING`, shared with the Claude Code hook and `eval.agent.OPENCODE_GIT_DENY`), anywhere in the command: in any segment, after `VAR=value` prefixes, shell keywords and group openers (`{ ! if then do else elif while until`), through wrapper commands (`env [-i] [-u N] [-S '...']`, `command`, `builtin`, `exec`, `nice`, `timeout`, `nohup`, `setsid`, `stdbuf`, `ionice`, `chrt`, `taskset`, `sudo`, `doas`, `time`, `xargs`, `watch`, `flock`, `runuser`, `strace`, `ltrace`, `busybox`, `unbuffer`, `eval`, `function NAME { ... }`, `coproc`, `find -exec\|-execdir\|-ok\|-okdir`, `su\|script\|flock -c '<script>'`, repeated, with clustered options such as `sudo -iu root`), as `/path/to/git`, through `git -c alias.<x>=<ref-moving>`, inside a backtick or `$(...)` substitution, and inside `sh\|bash\|zsh\|dash\|ksh\|fish\|... -c '<script>'` (classified once more; a script nested deeper is refused when it names `git` and then a ref-moving subcommand) | tool error `Refused by ozagia: <command> changes git refs`. Listing forms (`git branch`, `git tag -l`, `git stash list`, `git worktree list`, `git bisect log`) are read-only. |
| git_read, other, complex | everything else | `handled: false` |

A compound command (`shell.plan_compound`, run by `intent.compound`) is a
sequence of pipelines joined by `&&`, `||`, `;` or newlines. Each pipeline
is one step, then any number of filters (`| tail -N`, `| tail -n +N`,
`| head -N`, `| sed -n 'A[,B]p'`, `| grep [-viEFw] [-A|-B|-C N] PAT`), with
optional `2>&1`, `2>/dev/null` and `>/dev/null`. The steps are:

- pytest, read and grep, as above; pytest may have `VAR=value` prefixes
  (they extend the test environment);
- read-only git (`diff`, `show`, `status`, `log`, `blame` and the listing
  forms; `--no-pager` and `-C <dir inside the root>` allowed). It runs as an
  argv without a shell, with `GIT_OPTIONAL_LOCKS=0` and `GIT_PAGER=cat`.
  These options make the command fall back: options that write files or
  run programs (`--output`, `--ext-diff`, `--textconv`), options that read
  files git does not track (`--no-index`, `--contents`, `-O`,
  `--ignore-revs-file`), and any other global option;
- `echo` with literal words, `pwd`, `ls` (paths inside the root);
- `cd DIR` before any other step, inside the root; later steps run there.

At least one step must be pytest, read, grep or git. Anything else falls
back as a whole: heredocs, subshells, `&`, other redirects, substitutions,
globs, `python -c`, and an unmapped command or filter. So does any problem
found before the first step runs (a directory or file outside the root, a
missing file, an interpreter not on PATH). A step that fails once running
gives its error text and status 1.

Redirects apply in order, as in bash. `2>&1 >/dev/null` sends stderr into
the pipeline and drops stdout, and `>/dev/null 2>&1` drops both. Without
`2>&1`, a git or `ls` step's stderr skips the filters and follows the
step's output. A `\r` is part of a word, as in bash, so CRLF commands fall
back.

Statuses follow bash: `&&` and `||` short-circuit and `;` continues. A
pipeline's status is that of its last command: 0 after a `head`/`tail`/
`sed` filter, and for a `grep` filter 1 when no line is left. pytest takes
its exit code, grep is 1 when nothing matches, and git takes its own code.
One timeout covers the whole command (`input.timeout`, else 120 s).

The output is each step's output in order:

- pytest, read and grep give their usual lines (below);
- `git diff` and `git show` add `[ozagia] touched: <keys>`;
- a final status other than 0 adds `[ozagia] exit <n>`.

Filters apply to a step's own text, never to its `[ozagia]` lines. A read
or grep step is filtered at the source:

- read: a contiguous result is rendered as a range read; otherwise the
  kept lines are shown as bash prints them, with `[ozagia] lines in: <keys>`;
- grep: the matches are filtered as grep prints them (`path:n:text`) and
  shown in the grep format.

A pytest step that pipes through a filter keeps its whole output (up to
`check.MAX_OUTPUT_LINES`), so the filter sees what bash would show it; one
that does not keeps the last 40 lines. On the v2
pilot's 1080 shell calls, 264 of the captured compound commands without
pytest gave bash's exit status (all of them), and the 176 git/echo/ls ones
gave bash's exact text; 12 of 12 sampled pytest compounds gave bash's
status.

pytest output is pytest's own output tail, then:

```
[ozagia] pytest failed: exit 1, 0.4 s, 1 passed, 1 failed, 0 error, 0 skipped
[ozagia] failed test:tests/test_mod.py::test_broken: assert 3 == 5
[ozagia]   frame test:tests/test_mod.py::test_broken tests/test_mod.py:9
```

A captured pytest runs with the timeout `input.timeout` (ms), else 120 s
(OpenCode's built-in shell default, so capture never lets a command run
longer than the built-in would).
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

`after(tool, input, output)` runs after a built-in call.

- For edit, write, patch and shell commands that can change files, it
  reingests `memory.pending_changes()` and appends the delta line. Read-only
  commands are skipped: read, grep, git_read, refused classes and compound
  commands without a pytest step. When more than `MAX_REFRESH_FILES` (200)
  files changed, it appends `[ozagia] many files changed; graph refresh
  deferred` instead.
- For `git diff` / `git show`, also as one segment of a command that fell
  back, it appends `[ozagia] touched: <keys>`. These are the key enclosing
  each `+` line, and the line before each deletion point, in the current
  file.
- For a command with a pytest segment, it maps pytest's short summary
  lines (`FAILED path::name - message`, `ERROR ...`) to `[ozagia] failed
  test:<key>: <message>` for tests the graph knows. Paths are taken
  relative to a leading `cd DIR &&`.

## Daemon

One process per repository. `socketserver.ThreadingUnixStreamServer`, a
thread per connection, one lock around `tool` and `after` (`DevMemory` is
not thread-safe), except a captured pytest: it reads no graph state and
runs without the lock, so edits and reads sent while tests run are answered
at once. A compound command with a pytest step also runs without it, and
takes the lock (`_lock`) only around the steps that read the graph. Requests are JSON lines, answered in order; a line over
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
uptime; `structured_queries`, `structured_outlines` and `jq_captures` count
the intents `read_structured`, `read_structured_outline` and `shell_jq`.

`ozagia.core.memory` takes its layout from `$OZAGIA_ROOT` when imported, so
`serve` sets it to `--root` first. `DevMemory`'s own journal goes next to a
`--dump`, else to the state dir.

## Permissions

With OpenCode 2.3's `ctx.permission.assert`, the plugin asks OpenCode's
permission service before the daemon carries a call out. It uses the
request the built-in would make, computed by the daemon
(`intent.permission_request`):

| tool | action | resources |
|---|---|---|
| read | `read` | the path relative to the root |
| edit, write | `edit` | the path relative to the root |
| grep, glob | `grep`, `glob` | the pattern |
| shell | `shell` | one per simple command, without `cd`/`pushd`/`popd` |

`save` (what "always allow" stores) is `["*"]` for read, grep, glob and
edit, as the built-ins use. For shell it is the exact commands asked about,
so a grant never covers more than the user saw. The built-in uses prefix
patterns such as `git diff *`. An edit or write sends `metadata.files` with
the unified diff of the change, as the built-in edit does, so the prompt
shows it.

When the call is a mutating one (edit, write or shell), the plugin works in
three steps:

1. It sends the call with `preflight: true`. The handler runs every check
   and stops before its first change (`Preflight`).
2. The daemon answers `preflight` with the permission. The plugin asserts
   it.
3. The plugin sends the call again without `preflight`.

For read, grep and glob, the daemon answers first, and the output is shown
only after the assert passes.

A declined, corrected or blocked permission (`Permission.DeclinedError`,
`CorrectedError`, `BlockedError`) becomes the tool's error. Any other
failure of `assert`, or a preflight answer without a request, hands the
call to the built-in, which asks for itself. The plugin then sends `after`
with `meta.fallback: "permission: ..."`. A passthrough is asked once, by the built-in. A refusal (ref-moving
git) or a tool error from preflight asks nothing, since it changes nothing.

## Known limits

- The shell resources come from ozagia's own split of the command
  (`shell.command_resources`), which approximates OpenCode's parser. A
  pattern rule that tells commands apart by something the split drops
  (redirections) can match differently.
- Verified live on OpenCode 2.3.0 with the local model:
  - an `edit` `deny` rule on one file refused the handled edit, while an
    edit of another file went through;
  - a `read` deny on one file and a `bash` `cat *` deny refused the handled
    read and the captured `cat`;
  - a captured compound `grep ... && echo found` ran.
- A captured pytest keeps the last 40 lines of its output (all of it when
  a compound step pipes it through a filter). A bare `pytest -v` with more
  output shows less than bash would; the `[ozagia]` lines carry every
  failure.
- A permission can be asked twice. This happens when the file or directory
  changes between the preflight and the real call (for example, an edit's
  `oldString` no longer matches) and the real call becomes passthrough. The
  built-in then asks again.
- Git refusal covers the wrappers and keywords listed above. A command
  hidden some other way (an alias defined in the user's git config, a
  script file the command runs) is not seen. The eval's `OPENCODE_GIT_DENY`
  permission rules match only plain `git <sub>*`.
