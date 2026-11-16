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
  parse error (with line and column) are tool errors. Like every read
  answer (node-key errors too), they carry the read permission for the
  file, so the OpenCode plugin asks for it and shows the error instead of
  running the built-in read on the literal `json:...` path.
- YAML needs PyYAML (the `iac-yaml` extra); without it a YAML query is a
  tool error that says so. A multi-document YAML file is the list of its
  documents. TOML uses `tomllib` (read-only); dates print as ISO strings.
- The same query is the MCP tool `dev_query {path, pointer?, format?,
  offset?, limit?}`; `path` may also be a `json:`/`yaml:`/`toml:` key.

Outline. A read with no `offset` and no `limit` of a `.json`, `.yaml`,
`.yml` or `.toml` file over 2000 lines or 64 kB returns the outline
instead of the text: the file header, `[ozagia] json:<rel>#`, the root's
type and size, each top-level key with type, size, preview and line, one
level below it (up to 8 keys, or an array's first item), a line saying
how to query a path or read the text (pass `offset`/`limit`), then the
head of the text: lines 1-200 in read format (at most 32 kB, a line over
2000 characters cut with a note), so a read before an edit still sees the
start of the file. For YAML the note says the outline omits comments and
shows anchors, aliases and merge keys resolved; for TOML that it omits
comments (comments at the head are in the text that follows). The
limits are `[reads] structured_outline_lines` and
`structured_outline_bytes` in `.ozagia.toml` (0 turns one off). Files over
8 MB (2 MB for YAML when PyYAML has no libyaml `CSafeLoader`) are read as
text, as before, so the parse stays well inside the plugin's read timeout.
A file that does not parse (or YAML without PyYAML) is read as text. Shell
reads (`head`, `sed -n`, ...) and edits are unchanged.

Outline-first reads of other files. A read with no `offset` and no `limit`
of any other text file over 400 lines or 40 kB (`[reads] outline_lines` and
`outline_bytes`; 0 turns one off, both 0 turn it off) returns an outline,
and so does a shell `cat F` (intent `shell_read_outline`; `nl`, `head`,
`sed -n` and pipelines are unchanged):

```
Read file /repo/pkg/big.py, outline of 815 lines, 21456 bytes
[ozagia] file:pkg/big.py  imports: 1  imported by: 0  tests: 0
[ozagia] L4-6 sym:pkg/big.py::f1
[ozagia] L400-405 sym:pkg/big.py::Box
[ozagia]   L401-402 sym:pkg/big.py::Box.a
[ozagia] an outline instead of the whole text: the file is over 400 lines or 40960 bytes. Read a key above for one symbol or section, or pass offset/limit (a line range) for the text. Lines 1-120 follow
1: import os
...
```

- Entries: Python defs and classes with their methods (one level, keys as
  in the read headers); for another language the graph's symbols of the
  file; Markdown and TeX sections (below); else the text chunks
  (`text:<rel>#L<a>-L<b>`). At most 200 entries, then `+N more entries`.
- The head is lines 1-120 in read format, at most 32 kB, a line over 2000
  characters cut with a note. The read ledger counts only the lines shown
  in full.
- Intent `read_outline`; `meta.bytes_saved` estimates the text it kept out
  (the plain read's output size minus the outline's).

Section keys. A Markdown or TeX section is `doc:<rel>#<slug>`
(`ozagia.core.sections`). The slug is GitHub's anchor: the title
lowercased, punctuation dropped, spaces as `-`, a repeated slug gets `-1`,
`-2`, ... in file order. A section runs from its heading to the line before
the next heading of the same or a higher level, so it holds its
subsections. Reading the key returns that whole range with
`[ozagia] doc:<rel>#<slug>` as the header; an unknown slug is a tool error
that lists the nearest section keys. The path alias
`doc:<rel>#<a>/<b>` (a heading's title slug after its enclosing headings'
slugs, e.g. `#cli/usage`) is accepted too and reads under the slug key it
stands for; it is never shown. Key edits (`edit` with `key`) and the region
keys of patch-miss replies use the same slugs and line ranges, so a key an
outline, read or region reply shows can be read and key-edited. We chose this over the positional
`text:<rel>#L<a>-L<b>` because a slug depends only on the titles, not on
line numbers, so a key stays valid across edits elsewhere in the file.
Markdown headings are ATX headings outside fenced code (the rule of
`edit.markdown`). TeX headings are `\part`, `\chapter`, `\section`,
`\subsection`, `\subsubsection` and `\paragraph` (starred or not) at the
start of a line outside `%` comments.

Read ledger and repeats (`ozagia.intent.ledger`). The daemon keeps a ledger
per session (`harness`, `session`, and the request's `sub`): for every read
the model received (the read tool, `cat`, `head`, `tail`, `sed -n`, `nl`,
`nl -ba F | sed -n 'a,bp'`, a leading `cd`), the file's sha256 and line
range. A read of a range inside one recorded range of the unchanged file,
with no compaction since, is a repeat. By the repeat policy (`[reads]
repeat`, `[reads] repeat_by_harness`, else the harness default):

- `stub`: one line instead of the text (intent `read_stub` or
  `shell_read_stub`):
  `[ozagia] <rel> unchanged since read N (lines a-b, sha <12 hex>); keys:
  <up to 5>; read with a different range or after an edit to get text`.
- `note`: the text, then `[ozagia] unchanged since read N (lines a-b, sha
  ...)`.
- `off`: no ledger.

A read that overlaps an earlier one without being inside it gets the text
and `[ozagia] lines x-y were already read unchanged (read N)`.

What counts as received:

- A served read is staged with a token (`meta.ack`) and recorded only when
  the caller acknowledges delivery: the OpenCode plugin sends the token in
  `acks` on its next request for the session, after the permission check
  passed and the output went to the model. A refused permission, a failed
  fallback, a late answer or a lost ack leaves it unrecorded.
- A read the harness ran (`after`) is recorded only as far as its `output`
  shows the file's current lines, contiguous from the first line of the
  range (line-number prefixes `<n>: `, `<n>\t`, `<n>→` are accepted when the
  number matches; up to 10 header lines are skipped). A truncated output
  (Claude Code's Bash, Codex exec) records the matched prefix; an output
  that differs from the file (edited between the read and the hook)
  records nothing from the first differing line on. Claude Code's Read
  also sends `file.startLine`/`numLines` as `delivered`, which bounds the
  range first.
- Under the hook harnesses (Claude Code, Codex) a shell read with a `cd` is
  not tracked: Claude Code's shell keeps its directory between calls and
  the hook's `cwd` follows it.

The default is `note` for every harness. `stub` is opt-in (for example
`repeat_by_harness = {opencode = "stub"}`) and experimental until the live
eval shows no drop in success rate. Even when configured, a session gets
`note` until its harness has confirmed that it reports compaction: the
OpenCode plugin sends `compaction_hook: true` (in `hello` and on its
requests) when its `compaction` session hook registered; the Claude Code
and Codex hooks send it with their SessionStart `session_event`. Claude
Code additionally needs an `agent_id` seen in the session, since a version
that sends none would merge a subagent's ledger into its parent's. Codex is
a poor candidate: subagent hooks carry the parent's session id.

`stub` stays opt-in because context can shrink without any event the
ledger hears about: OpenCode's tool-output pruning (`compaction.prune`) and
message revert (`/undo`), and Claude Code's rewind, remove earlier tool
results without a compaction hook or `SessionStart`. Under `note` such a
loss costs nothing; under `stub` the model is told it has text it no
longer holds.

A `session_event` that fails is carried forward: the hooks leave a marker
in the state dir (`ledger_reset/`), the plugin keeps the session in
memory, and the session's next ledger request carries `reset_pending:
true`, which the daemon applies as a `compact` before the request.

A `write` that replaces an existing file of which the session's ledger
holds only part (an outline's head, some ranges) of the current text
appends `[ozagia] warning: <rel> was replaced as a whole, but this session
has read only lines ... of its N lines (an outline or a partial read);
check that nothing outside them was lost`. The write is not refused.

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

An edit with `key` (and `newString`, `base_sha256`) instead of
`oldString` replaces the whole text of that node (`ozagia.edit.key_edit`:
`sym:`/`test:` symbols, `doc:<file>#<slug>` Markdown and TeX sections as
reads and outlines name them (`ozagia.core.sections`; the `a/b` alias too),
`text:<file>#L<a>-L<b>` line ranges, `json:<file>#<pointer>` values).
`base_sha256` is the sha256 of the node's current text, as region replies,
edit-miss replies and refusals give it. The file's sha256 is taken only
when the node is the whole file (`file:`, `doc:<file>`): a key edit cannot
tell whether a file sha came from a read of the whole node or from a reply
that showed part of it, and the edit replaces all of it. A missing, stale
or file-only base is a tool error with the node's current text and both
hashes. A `sym:` qualname with several defs (a property getter and setter)
is refused with each def's line range; edit one by its `text:` key. Other
languages' symbols use the graph's line range, after a reingest when the
file changed since the graph took it in.

```
Edited /repo/pkg/mod.py (node sym:pkg/mod.py::helper, lines 5-10)
[ozagia] in: sym:pkg/mod.py::helper
[ozagia] sha256 <new file sha> (the file now); node sha256 <new node sha> (base_sha256 for the next key edit of this node)

Edit refused: stale base: <sha> is neither the node's sha256 nor the file's; ...
[ozagia] sym:pkg/mod.py::helper L5-L10  file sha256 <sha>  node sha256 <sha>
5: def helper(a, b=1):
...
```

Intents `edit_key` and `edit_key_stale`. The parse guard and broken
references work as for an exact edit.

An empty `oldString` writes `newString` as the file. `write` goes through
`ozagia.edit.txn` (backup of an existing file; atomic create of a new one,
parent directories made), then `reingest`:

```
Wrote file successfully.
[ozagia] replaced file:pkg/use.py  backup: /repo/.ozagia/edits/backups/...
[ozagia] delta: 1 added, 1 removed, 1 changed, edges +2 -3: +sym:pkg/use.py::other, -sym:pkg/use.py::run, ~file:pkg/use.py
```

### Lint inside the edit

After a handled edit, key edit or write, and in `after` for a built-in
edit, write or Codex patch, the project's linter runs on the changed files
only (`ozagia.intent.lint`) and its findings on the changed lines follow
the other lines, at most 10, then a count of the rest:

```
[ozagia] lint F821 L9 sym:pkg/mod.py::target: Undefined name `undefined_b`
[ozagia] lint rustfmt L4 file:src/lib.rs: rustfmt would reformat L4-4
[ozagia] lint: 5 more findings on the changed lines
[ozagia] lint: ruff timed out after 3 s
```

- ruff (`ruff check --output-format json --no-fix --no-cache
  --force-exclude`) for `.py` files when a `pyproject.toml` with
  `[tool.ruff]`, a `ruff.toml` or `.ruff.toml` sits in the file's directory
  or above it, or `.ozagia.toml` has `[lint] python = "ruff"`; the
  executable is `<root>/.venv/bin/ruff`, else `ruff` on `PATH`.
- `rustfmt --check --edition <edition> --color never` for `.rs` files when a
  `Cargo.toml` is above the file (its edition; 2015 when unset), and
  rustfmt is on `PATH`. Each run of lines rustfmt would change is one
  finding.
- `[lint] enabled = false` turns it off; `enabled = true` lints without a
  ruff or Cargo configuration; `python`/`rust = "off"` exclude a language;
  `timeout_s` (default 3) is the budget of one call's linter runs.
- Changed lines: a handled call diffs the file before and after (a new
  file: every line). In `after`: a write lints the whole file, an edit the
  lines its `newString` occupies, a patch the lines its chunks added (an
  added file: every line); else the line ranges of the delta's added and
  changed keys.
- Linters never modify files and write no cache. A missing linter, a
  failing run or unparsable output adds nothing; lint never fails the edit.
  `meta` has `lint_runs`, `lint_findings` and `lint_timeouts` when a linter
  ran; the daemon's stats total them.

## shell

`ozagia.intent.shell.classify(command)` (pure: it reads no files) maps a
simple command, or a compound command made of simple steps.

A simple command has no operators, redirects, substitutions, variable or
tilde expansion, unquoted globs (except in `--include=`/`--glob=`) or
newlines. A read, grep, files or find command may have `VAR=x` prefixes
of harmless variables only (`LC_*`, `LANG`, `NO_COLOR`, `TERM`, `PAGER`,
`GIT_PAGER`, `COLUMNS`): they do not change what the capture prints, and
a locale variable falls back for grep and git grep (it changes their regex
semantics) and for find `-iname`/`-ipath` (case folding). rg ignores the
locale. Any other prefix falls back, except on pytest. A read-only command
(read, grep, files, find, ls, git) may run inside `timeout [-s SIG] [-k D]
[--preserve-status] [--foreground] N[smhd]`; pytest inside `timeout` falls
back. The classes are:

| class | commands | what happens |
|---|---|---|
| pytest | `pytest`, `py.test`, `python[3] -m pytest`, `<path>/python -m pytest` | `ozagia.check.run_tests` with the named interpreter (a pytest script's `#!` line), the selectors made root-relative, the other flags, `timeout`/1000 s (default 120). Refused flags (cache, `--pdb`, `--junitxml`, ...) and ambiguous options fall back. |
| read | `cat F`, `head [-n N] F`, `tail [-n N\|+N] F`, `sed -n 'A[,B]p' F`, `nl [-ba\|-bt] F`, `tail -c [+]N F` | read of that range (no 2000-line cap). `nl` lines are numbered as nl prints them (`%6d` and a tab; with `-bt`, the default, an empty line is 7 spaces and takes no number); a file with a byte order mark, a carriage return or an nl section delimiter line (`\:`, `\:\:`, `\:\:\:`) falls back. `tail -c` shows the bytes verbatim after `Read file <abs>, bytes a-b of n`, then `[ozagia] lines in: <keys>`; bytes that start inside a UTF-8 character fall back. |
| grep | `grep [-rRnHiFE] [-A\|-B\|-C N\|-N] [--include G] PAT [PATH...]`, `rg [-nNHiF] [-A\|-B\|-C N] [-g G]... [-t T] [-T T] PAT [PATH...]`, `git grep [-niFE] PAT [PATH]` | grep; `-i` is case-insensitive. A basic regex is converted to the Rust/Python dialect (`\|` `\(` `\{` become operators, `+ ? \| { } ( )` literals); one with back-references, `\<`, `\>` or POSIX classes falls back. Several paths are searched in the order given (matches by path and line within each); overlapping paths, grep `--include` with a file operand (grep filters it, rg does not) and `rg --hidden` fall back. `-A/-B/-C` add context lines (`  Line n- text`) and a `  --` line between groups of one file; `-A`/`-B` override `-C` whatever the order, as in both tools. rg searches as rg does (hidden and ignored files skipped; `-g`, negated `-g !G`, `-t` and `-T` passed to rg). grep and git grep are searched with rg too (hidden files included, `.gitignore` respected: this differs from grep for ignored files, and from git grep for untracked ones). |
| files | `rg --files [PATH...] [-g G]... [-t T] [-T T] [--hidden] [--no-ignore[-vcs]] [-u\|-uu\|-uuu] [--sort path]` | runs rg itself (paths inside the root), sorted by path; rg's own order is that of its parallel walk, so this is one of the orders it can print. Intent `shell_files`. |
| find | `find [-P] [PATH...] [-maxdepth N] [-mindepth N] [EXPR]` with `-name -iname -path -ipath -type f\|d\|l -newer F -print -prune -true -false`, `!`/`-not`, `-a`, `-o`, `\( \)` | evaluated as GNU find without `-L`: directory order (`os.scandir`), the start path first, symlinks not followed, the implicit `-print` when there is no `-print`, paths printed as find prints them. A glob range is accepted only inside a-z, A-Z or 0-9; POSIX classes, `-maxdepth` after a test (find warns on stderr), any other primary or action, a symlink start path, more than 20000 entries and a directory with more than 10000 entries (gnulib fts sorts those by inode) fall back. A `?` or bracket glob tested on a non-ASCII name falls back unless the locale (`LC_ALL`, `LC_CTYPE`, `LANG` from the prefix or the daemon's environment) is UTF-8 and the name is UTF-8 (in a C locale fnmatch matches one byte), and `-iname`/`-ipath` on a non-ASCII name falls back. `.git` and the state directory are walked like any directory. |
| jq | `jq [-r] [-c] [-S] [-M] FILTER FILE` (flags before the filter, also clustered as `-rc`), `cat FILE \| jq ...`, `jq ... < FILE`, `< FILE jq ...`, and after `cd DIR &&`. FILTER is a pipeline (up to 6 stages) of paths (`.`, `.a`, `.a.b`, `."key with space"`, `.["k"]`, `.[N]`, `.[-N]`, `.[N:M]`, `.[]`, `.a[]`) and `keys`, `length`, `type` (`ozagia.core.jq`) | jq's exact stdout, read-only (no graph refresh). Left to the real jq: any other filter or option (reason `jq filter '...' is not mapped` / `jq option X is not mapped`), several or no files, a result jq reports as an error (`.a` of an array, `.[]` of null, `keys` of a string, ...), a number with an exponent (jq 1.7 rewrites it), with jq other than 1.7 or 1.8 any number that is not an integer below 2^53 (jq 1.6 prints doubles; versions after 1.8 are unchecked), an integer literal over 20 digits that would need converting, a computed non-integer (`length` of 2.5), a BOM, NaN, a lone surrogate escape, several JSON values, input nested deeper than jq 1.7's parser limit (256 slots: one per array, two per object member; jq 1.8 allows 10000, the 256 limit is kept), output over 1 MB, and any `jq` command when no `jq` is on the daemon's `PATH` (reason `jq is not installed`, so the shell says `command not found`). The `jq` version is re-read when `PATH` or the binary changes. |
| ruff | `ruff check [PATH...] [--select X]... [--output-format concise\|full]` (also `--select=X`, `--output-format=X`, `--`), the executable as `ruff` on `PATH` or a path such as `.venv/bin/ruff` | runs ruff itself (with `--no-fix`; paths inside the root): its exact output, stdout and stderr as one stream, then `[ozagia] lint <rule> <file>:<line> <key>` per finding (at most 30; from a second run with `--output-format json`) and `[ozagia] exit <n>` when ruff exits non-zero. Left to the shell: any other option (`--fix`, `--watch`, `--output-format json`, ...), `ruff format` and other subcommands, a ruff configuration nearest the working directory or a path that sets `fix = true`, and a missing executable. |
| compound | see below | the steps, in order |
| git_refs | `git commit\|push\|stash\|branch\|checkout\|switch\|reset\|rebase\|merge\|tag\|worktree\|cherry-pick\|revert\|am\|pull\|fetch\|bisect\|...` (`intent.shell.GIT_REF_MOVING`, shared with the Claude Code hook and `eval.agent.OPENCODE_GIT_DENY`), anywhere in the command: in any segment, after `VAR=value` prefixes, shell keywords and group openers (`{ ! if then do else elif while until`), through wrapper commands (`env [-i] [-u N] [-S '...']`, `command`, `builtin`, `exec`, `nice`, `timeout`, `nohup`, `setsid`, `stdbuf`, `ionice`, `chrt`, `taskset`, `sudo`, `doas`, `time`, `xargs`, `watch`, `flock`, `runuser`, `strace`, `ltrace`, `busybox`, `unbuffer`, `eval`, `function NAME { ... }`, `coproc`, `find -exec\|-execdir\|-ok\|-okdir`, `su\|script\|flock -c '<script>'`, repeated, with clustered options such as `sudo -iu root`), as `/path/to/git`, through `git -c alias.<x>=<ref-moving>`, inside a backtick or `$(...)` substitution, and inside `sh\|bash\|zsh\|dash\|ksh\|fish\|... -c '<script>'` (classified once more; a script nested deeper is refused when it names `git` and then a ref-moving subcommand) | tool error `Refused by ozagia: <command> changes git refs`. Listing forms (`git branch`, `git tag -l`, `git stash list`, `git worktree list`, `git bisect log`) are read-only. |
| git_read, other, complex | everything else | `handled: false` |

A compound command (`shell.plan_compound`, run by `intent.compound`) is a
sequence of pipelines and subshells joined by `&&`, `||`, `;` or newlines.
A subshell `( SEQUENCE )` (one level, no pipe or redirect after it) runs
its own sequence: a `cd` inside it ends with it, and its status is that of
its last command. Each pipeline is one step, then any number of filters
(`| tail -N`, `| tail -n +N`, `| head -N`, `| sed -n 'A[,B]p'`,
`| grep [-viEFw] [-A|-B|-C N] PAT`, `| wc -l|-c|-w`, `| sort [-u] [-n]
[-r]`, `| uniq [-c]`, `| cut [-d X] -f LIST [-s]`), with optional `2>&1`,
`2>/dev/null` and `>/dev/null`. The steps are:

- pytest, read, grep, files and find, as above; pytest may have any
  `VAR=value` prefixes (they extend the test environment), the others
  the harmless ones and `timeout N`;
- read-only git (`diff`, `show`, `status`, `log`, `blame` and the listing
  forms; `--no-pager` and `-C <dir inside the root>` allowed). It runs as an
  argv without a shell, with `GIT_OPTIONAL_LOCKS=0` and `GIT_PAGER=cat`.
  These options make the command fall back: options that write files or
  run programs (`--output`, `--ext-diff`, `--textconv`), options that read
  files git does not track (`--no-index`, `--contents`, `-O`,
  `--ignore-revs-file`), and any other global option;
- `echo` with literal words, `pwd`, `ls` (paths inside the root; harmless
  prefixes go to its environment, as they do for git);
- `cd DIR` before any other step of its sequence, inside the root; later
  steps run there. `cd ~` and `cd ~/DIR` use `HOME` (the daemon's, or the
  native loop's `_test_env`) and are captured when that is inside the
  root.

At least one step must be pytest, read, grep, files, find or git. Anything
else falls back as a whole: heredocs, nested subshells, `&`, other
redirects, substitutions,
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
`sed`/`wc`/`sort`/`uniq`/`cut` filter, and for a `grep` filter 1 when no
line is left. pytest takes its exit code, grep is 1 when nothing matches,
git and `rg --files` take their own code, and find is 0.
One timeout covers the whole command (`input.timeout`, else 120 s). A
step's own `timeout N` limits a git, ls or `rg --files` subprocess to N
seconds (status 124; the partial output of a killed command is not
reproduced), and gives status 124 and no output to an emulated step that
took longer than N.

The daemon counts shell calls in `stats.shell` (captured, refused,
passthrough, `capture_rate`, the captured intents and the most common
passthrough reasons); the agent eval records `shell_captured` and
`shell_passthrough` per run and `shell_capture_rate` per arm.

The output is each step's output in order:

- pytest, read and grep give their usual lines (below);
- `git diff` and `git show` add `[ozagia] touched: <keys>`;
- a final status other than 0 adds `[ozagia] exit <n>`.

Filters follow the text as bash passes it: a last line without a newline
stays so through `head`, `tail` and `sed`, so `wc -l` and `wc -c` count as
wc does; grep, sort, uniq and cut end every line. `sort` compares bytes
and is captured only when `LC_COLLATE` (and for `-n`, `LC_NUMERIC`) is C,
POSIX or C.UTF-8, from a `LC_ALL=C sort` prefix or the daemon's
environment; other locales collate differently, so the command falls
back. `sort -n` reads a leading number after blanks (`-`, digits, a `.`
fraction; anything else is 0) and orders equal numbers by bytes unless
`-u`; `-r` reverses both; `-u` keeps the first line of each run of equal
keys. `uniq` compares bytes and `uniq -c` prints `%7d `. `cut -f` prints
fields in file order and a line without the delimiter whole (dropped with
`-s`); the delimiter is one ASCII character. `wc -w` splits at ASCII white
space, and in a UTF-8 locale also at the Unicode spaces glibc and
coreutils count. A pipeline through wc, sort, uniq or cut shows the
command's own text (grep as `path:n:text`, context `path-n-text`, `--`
separators; rg's line numbers only with `-n`, as when its output is not a
terminal) and no graph lines. pytest and git grep piped into them fall
back, and so does `grep -r` over a directory (grep also searches ignored,
`.git`, binary and large files). In this mode rg searches files of any
size, and a line that is not UTF-8, ends with a carriage return or is a
binary-file notice makes the command fall back; the search runs before the
first step, and after a pytest step it falls back. `grep -r` over a
directory piped into `head`, `tail`, `sed` or a `grep` with context also
falls back: grep prints files in readdir order, the capture by path.

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
```

Affected tests and the result cache (`ozagia.intent.affected`, off by
default; `[tests] select = "affected"`, `[tests] cache = true`). A captured
pytest with no test selector (or only directory selectors) and only
reporting options runs the tests linked to the files changed since the last
fully green run of that suite (git working-tree changes, the import walk of
`ozagia.intent.affected`, the graph's covering tests), and says so after
the head line:

```
[ozagia] ran 2 affected tests of 3; not run: 1 test in 1 file (tests/test_other.py); run all: OZAGIA_TESTS_ALL=1 python -m pytest -q
```

When it cannot decide (no green run yet, conftest.py, a non-Python file or
the pytest config changed, a changed file no test is linked to, no change
at all, `OZAGIA_TESTS_ALL=1`) it runs the command as typed and says
`[ozagia] ran all tests: <reason>`. With the cache, a test whose inputs are
unchanged since it passed is reported as `[ozagia] cached pass <file>: ...`
instead of run; failures are never cached. Slow (> 5 s) and flaky tests are
listed at the end (`[ozagia] slow test ...`, `[ozagia] flaky test ...`).
`select = "all"` is the default because changing what a command runs is
surprising. `meta.tests` has the counters `affected_runs`,
`tests_skipped_unaffected`, `cached_passes`, `full_runs_forced`, which the
daemon's `stats` sums. `cargo test` is not covered (not a captured command,
and the Rust graph has no crate or test-target nodes).

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
- For an edit that failed while its `oldString` (or one of `edits`) is not
  in the file, it appends the nearest region (`ozagia.intent.patch`):
  `[ozagia] oldString is not in <file>; nearest region L<a>-L<b> <key>
  (similarity r), sha256 <file sha>`, `[ozagia] node L<c>-L<d> node sha256
  <sha>` (the key's whole node, and the base a key edit of it takes), the
  differing lines as `[ozagia] -<oldString line>` / `[ozagia] +<file
  line>`, then the region's lines as `<n>: <text>` (at most 60). Intent
  `after_edit_miss`, `meta.region_replies` 1. The OpenCode plugin sends
  this `after` when the built-in edit throws, with `meta.builtin_error:
  true`, and throws the error with the append added; a request meta
  `builtin_error` decides whether the edit failed. Without it (the Claude
  Code and Codex hooks) the output must report a failure ("not found",
  "could not find", "no match", ...) and no `newString` may be in the file,
  so a successful edit whose output carries such words in diagnostics gets
  no region.
- For a command with a pytest segment, it maps pytest's short summary
  lines (`FAILED path::name - message`, `ERROR ...`) to `[ozagia] failed
  test:<key>: <message>` for tests the graph knows. Paths are taken
  relative to a leading `cd DIR &&`.
- With a session, a read or shell read the built-in ran is recorded in the
  read ledger as far as `output` shows the file's lines (intent
  `after_read`; `after_read_unconfirmed` when nothing matched and nothing
  was recorded); a repeat or overlap appends its note line (see read).

- For a built-in read or edit of a path that does not exist, and for the
  paths of "No such file or directory" lines in a shell output (`cat`,
  `sed`, `head`, `nl`, `ls`, `rg`, `cd`, Python), it appends the missing
  path line (see "Missing paths"); `meta.path_suggestions` counts them.
- For a built-in edit, write or patch with a session, the files it named
  that changed are journaled in DevMemory's journal as `builtin_edit` with
  the session (see "Ownership"), and listed in `meta.edited_files`.

`check_read(tool, input)` (the daemon's `read_check`) tells a hook whether
a read the harness is about to run repeats an unchanged range: `{"repeat",
"policy", "stub"}`, with `stub` only under the `stub` policy. It records
nothing; the call's `after` does.

## Missing paths

A read or edit of a path that does not exist gets up to 5 existing paths
(`ozagia.intent.paths`): the same path inside this root when an absolute
path points into another worktree of the repository; the path with each
missing component replaced by the closest name in its directory; close
names in the same directory (`difflib`); graph files with the same base
name; the same relative path in the other worktrees (`git worktree list
--porcelain`, the main checkout included). One line names them:

    [ozagia] no such file <path>; did you mean: <path> (file:<rel>), <abs> (worktree <branch>)

A served read answers `File not found: <abs path>` and that line as a tool
error (intent `read_missing`); a served edit `File <abs path> not found`
and the line (`edit_missing`); a path refused as outside the root gets the
line after the refusal. Without a candidate a served read stays
passthrough, so the built-in reports the miss. Nothing is ever read or
edited in place of the missing path. The hooks: Claude Code denies a
`Read` of a missing path with the line; Kiro blocks (exit 2) a read whose
every path is missing under the event's cwd, when there is a candidate;
Codex gets it from `after` of the failed `cat`/`sed`. They ask the daemon
(`path_check`) when one runs, else check locally (no graph names).

## Ownership

DevMemory journals each edit made while a session acts with `session:
{harness, id}`, `epoch` and `head` (`ozagia.core.owners`): handled edits
and writes, built-in edits reported by `after`, and MCP edits (session
`mcp pid<N>`). The daemon journal's `files` field lists the files a
request edited. A session is live while the daemon has seen a request or
`hello` from it in the last 15 minutes (read back from the daemon journal
when it starts); an MCP session while its process runs.

Before a handled edit or write, each other live session whose last edit of
the file is not yet committed (the file is dirty, and no commit after the
HEAD of that edit touched it) adds a line to the result:

    [ozagia] warning: <file> has uncommitted edits by another live session (<harness> <short id>, <age>)

It never blocks the edit. Hooks ask the daemon (`owner_check`) before an
edit where it is cheap: Claude Code `Edit`/`MultiEdit`/`Write` and Codex
`apply_patch` (`additionalContext`), Kiro write tools (stdout).
`ozagia who <path>` and MCP `dev_who {path}` list the sessions that edited
a file: edits, first and last time, live or not, committed or not, with the
file's git state.

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
keys (≤20), reason, fallback, session, harness, files`. `stats` returns counts by
op, tool, intent and handled; `passthrough` (not handled by design),
`expired`, `after_expired`, `late` (handled after the deadline),
`plugin_fallbacks` (`after` requests with `meta.fallback`),
`unknown_outcomes`, `fallbacks` (= `plugin_fallbacks`: one per call the
plugin could not get answered; `expired` and failed `tool` requests are the
daemon's view of some of the same calls), errors, hellos and their sessions, mean and max ms and
uptime; `structured_queries`, `structured_outlines` and `jq_captures` count
the intents `read_structured`, `read_structured_outline` and `shell_jq`.
`lint_runs`, `lint_findings` and `lint_timeouts` total the lint
counters of edit and `after` metas; `ruff_captures` counts `shell_ruff`.
The read ledger adds `repeats_seen` (repeats stubbed or noted),
`stubs_served`, `notes_served`, `outlines_served` (intents `read_outline`
and `shell_read_outline`), `bytes_saved` (the estimates in
`meta.bytes_saved`) and `ledger` (`sessions`, `evicted`, `events` by kind).
The ledgers live in the daemon only: at most 64 sessions (least recently
used dropped), 256 files per session and 32 ranges per file. A
`session_event` (`compact`, `start`, `end`) clears or drops every ledger of
a (harness, session).

For the patch checks, `patch_checks` and `patch_misses_caught` count `patch_check_ok` plus
`patch_check_miss`, and `patch_check_miss`; `region_replies` sums
`meta.region_replies` (patch hunks and edit misses); `key_edits` counts
`edit_key`.

The `patch_check` tool (`input.patch`, optional `workdir`) checks an
apply_patch text against the current files and never writes (see
docs/codex.md, "Patch check"). Its output is the region reply, or `the
patch matches the current files`; intents `patch_check_ok`,
`patch_check_miss` and `patch_check_unparsed`.

Path resolution and ownership add `path_suggestions` (answers that named
existing paths for a missing one), `ownership_warnings` and
`live_sessions`.

Background jobs add `jobs_started`, `waits`, `wait_seconds` and `polls_avoided` (waits that
returned new output or the job's end) count background jobs, and `jobs`
has the rest of their counters (`jobs_running`, `job_stops`, `ps_captures`,
`sleep_hints`, `background_hints`, `job_events`).

Background jobs (`ozagia.daemon.jobs`, ops `job_start`, `job_wait`,
`job_list`, `job_log`, `job_stop`, `job_events` in `ozagia.daemon.protocol`)
live in the daemon. Starting them is opt-in per project (`[jobs] enabled =
true` in `.ozagia.toml`, or `OZAGIA_JOBS=1` in the daemon's environment),
since a job runs with the daemon's privileges outside any harness sandbox.
The idle timeout does not fire while one runs (a job runs until its whole
process group has exited). On exit the daemon stops its jobs (SIGTERM to
each process group, SIGKILL after 2 s) except those started with `detach`,
which it leaves running and logs; a daemon that died without that leaves
them to the next one, which stops the non-detached ones when it starts.
In `shell`, a `pgrep [-f] [-a|-l] PAT` whose pattern matches a job's
command (or, without `-f`, a running job's process name), or a `ps -p
PID[,PID] [-o pid=|args=|cmd=|command=|comm=]` where every PID is a job's,
is answered from /proc as pgrep or ps prints it, plus one `[ozagia] job
...` line per job (intent `shell_ps`); every other `ps`/`pgrep` runs as
usual. `after` for `shell` adds an `[ozagia]` line pointing to `job_wait`
for a `sleep N` (N ≥ 20 s) while a job runs, and, when jobs are enabled,
one pointing to `job_start` for a background start (`&`, `nohup`, `setsid`,
`disown`), each at most 3 times per session. A `ps -p` about an ended job's
pid runs as usual (the pid may be another process's by now).

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
