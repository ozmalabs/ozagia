# v4: open issues and compound shell capture

Date: 16 Nov 2026. Branch `v4/open-issues` (worktree `../ozagia-v4`).
Scope: the open issues left after the v3 pilot, except Claude Code (still
logged out), plus capture of compound shell commands.

Status: implemented with review fixes, verified 2026-11-24. Git-refusal wrapper
peeling and compound shell capture are in `intent/shell.py`,
`intent/compound.py` and `intent/run.py`, with `tests/test_intent_compound.py`,
`tests/test_intent.py` and `tests/test_shell_capture.py`; the review fixes
listed at the end are in the same slice. The branch/merge notes below are
historical. The live checks in the verification section were not re-run for
this audit.

## Evidence

- v2 pilot, graph_intent arm: the daemon handled 43% of calls. Of the
  passthroughs, 245 had an operator, 104 were multi-line, 18 were `ls` and
  8 were git reads.
- All 1080 completed shell calls of the v2 pilot (all arms), grouped by
  shape. The common ones are `cd <workdir> && python -m pytest ... 2>&1 |
  tail -N` (130), `cd <workdir> && git diff ...` (57 single, 41 sequences),
  `cd <workdir> && grep ... | head -N` (43), git and read sequences joined
  by `&&`/`;` with `echo ---` separators, and `pwd && ls -la`. Multi-line
  `python -c` and heredocs (about 230) run arbitrary code and stay
  passthrough.
- OpenCode 2.3.0, from the binary:
  - A plugin's `setup(ctx)` gets `ctx.permission.assert(request)`. This is
    the Permission service the built-ins call. The request has
    `{sessionID, agent, action, resources, save, metadata, source}`.
  - The built-ins assert `read` [relative path], `edit` [relative path]
    (write too), `grep` [pattern], `glob` [pattern] and `shell` [one
    resource per parsed command].
  - Every built-in is registered `codemode: false`, so Code Mode's
    `execute` cannot call a built-in. The catalog is built from the
    registry after transforms.
  - The 13 Code Mode `execute` calls in the v1 and v2 pilots called only
    MCP tools, `goal_status` and `fetch`.
- Ocelot adapter cost, measured on a clone: an adapter pass takes 4.8 s
  (a seed reingest is 6.1 s in total), and a source reingest without a
  pass takes 0.37 s.

## Work

### W1. Git refusal through wrappers (`intent/shell.py`)

`_refs_in` only looks at the first word after `VAR=` prefixes. These forms
all pass through today: `env git commit`, `env -i git push`, `xargs git
checkout`, `command`, `exec`, `nice`, `timeout 10`, `nohup`, `sudo`,
`time`, `builtin`, `eval`, `find ... -exec git ...`, `{ git commit; }`,
`! git push`, `then git push`, `do git checkout`.

Change: peel wrappers and their options repeatedly before the basename
test. The wrappers are env (including `-S`), command, builtin, exec, nice,
timeout, nohup, time, setsid, stdbuf, ionice, chrt, taskset, sudo, doas
and xargs. Shell keywords and group openers (`{ ! then do else elif ( if
while until`) are peeled too. `eval` joins its arguments and classifies
them again. `find -exec/-execdir/-ok/-okdir` classifies the command that
follows. The daemon, the native loop and the Claude hook all call
`classify`, so all three are covered.

### W2. Compound shell capture (`intent/shell.py`, `intent/compound.py`, `intent/run.py`, daemon)

A compound command is captured when every part of it is something the
intent layer can carry out. The grammar is a sequence of pipelines joined
by `&&`, `||` or `;`. Each pipeline is one step followed by zero or more
`| tail|head -N` filters, with optional `2>&1` or `2>/dev/null`.

The steps are:

- `cd DIR`, inside the project root; it changes the directory for the
  steps after it;
- the existing captured classes (pytest, read, grep);
- `git` read commands (diff, show, status, log, blame and the listing
  forms), run as an argv without a shell, with `GIT_OPTIONAL_LOCKS=0`,
  refusing `--output` and `--ext-diff`;
- `echo` with literal words, `pwd`, and `ls` without globs.

Anything else is passthrough, as are heredocs, subshells, `&`, file
redirects, substitutions and globs. Exit statuses follow the shell:

- A pipeline with a filter has status 0.
- pytest takes its exit code.
- grep is 1 when nothing matches.
- read errors and git errors take their real codes.
- `&&` and `||` short-circuit, and `;` continues.

The output is each step's output in order. pytest, read and grep keep their
`[ozagia]` lines; git diff and show add `[ozagia] touched:` keys. Filters
apply to a step's own text, not to its `[ozagia]` lines. One timeout
(`input.timeout`, else 120 s) covers the whole command.

The daemon runs a compound command that contains pytest without the global
lock. The compound runner then takes the lock itself for each step that
reads the graph.

For passthrough compound commands, `after` adds context from the
built-in's output. It adds `touched:` keys when a segment is `git
diff`/`git show`, and `failed test:<key>` lines from pytest's `FAILED` and
`ERROR` summary lines when a segment is pytest.

The fallback: an unmapped or unsafe step makes the whole command
passthrough, which is today's behaviour.

### W3. Permissions for handled calls (`plugin.js`, daemon, intent)

Before the daemon carries out a mutating call (edit, write, captured
shell), the plugin asks OpenCode's permission service with the same
request the built-in would make. For read, grep and glob, the daemon
computes the result and the plugin asserts before returning it, so nothing
is shown when the assert fails.

- Protocol: a `tool` request with `"preflight": true` runs the handler up
  to its first side effect. It returns `{handled: false, preflight: true,
  permission: {...}}` when the handler would carry the call out, or the
  usual passthrough otherwise. The plugin asserts and then sends the real
  `tool` request.
- A rejected assert becomes the tool error, and the daemon is not asked to
  run the call.
- A passthrough reaches the built-in, which asserts once itself, so the
  user is never prompted twice.
- With no `ctx.permission.assert` (other harnesses, older OpenCode), the
  behaviour is as before. The `hello` message reports
  `permission_assert: false`.

### W4. Code Mode

No capture gap: built-ins cannot run inside Code Mode in 2.3.0. The plugin
reports in `hello` the `codemode` option of each tool it replaced. The
eval flags any replaced tool that is not `codemode: false`, so a future
OpenCode change shows up in the results.

### W5. TUI `--standalone` plugin load

The plugin sends `hello` from `setup`, so a process that loaded it is
visible without a tool call. Verified live: `opencode --standalone` (TUI)
under a pty, with a daemon socket, until a `hello` arrives, without
calling a model.

### W6. Adapter attributes on core nodes (`core/reingest.py`, `core/memory.py`)

The build and the adapter pass record what indexers write onto core nodes
(sym, test, doc, file; Repo is already kept) as an overlay: the node key,
the attribute, the value and the original value.

- When reingest replaces a node's attributes without an adapter pass, it
  reapplies the overlay.
- An adapter pass first restores the original values on nodes that were
  not re-parsed, then reruns the indexers and records a new overlay.

### W7. Adapter inputs (`adapters.py`, `core/reingest.py`, `core/memory.py`)

`add_indexer(fn, *, watch=(), texts=None)` declares what an indexer
depends on:

- `watch`: globs of files the indexer reads from disk.
  - New, changed and deleted matches are pending changes.
  - Untracked matches count in the tree digest.
  - A reingest of a match runs the adapter pass.
  - Matches never count as unhandled changes.
- `texts`:
  - `None` (the default, always correct): the indexer may read any
    indexed text, so a reingest of an indexed file runs the pass.
  - `False`: it reads none.
  - A regex: it depends on indexed files only through texts that match.
    A file that matched at the last pass or matches now runs the pass.

The Ocelot adapter declares `watch=("seeds/*.json",)` and `texts=` its
seed path regex, with a fallback for an ozagia without the keywords. The
fixture copy in `tests/fixtures` stays identical.

This fixes these cases:

- new tests that mention a seed;
- new test functions in a file that mentions a seed;
- a mention that is added or removed;
- a new untracked seed manifest;
- staleness reporting fresh when it is not.

The equivalence test (refresh compared with a fresh build) gains the seed
adapter, git, untracked seeds and seed mentions.

## Verification

- Unit tests for each item and the full suite plus ruff, run with
  `PYTHONPATH=src` from the worktree.
- Command coverage: the 1080 pilot shell calls classified before and after
  the change. This counts captured commands; it does not show that the
  results are identical.
- An equivalence spot check: run a sample of the newly captured pilot
  commands both through the compound runner and with `bash -c` on a clone,
  and compare exit statuses and the plain text without the `[ozagia]`
  lines.
- Live checks with the local model ($0):
  - an OpenCode run with a `deny` rule on one file (the edit must be
    refused and the file unchanged);
  - the TUI hello check;
  - a graph_intent pilot rerun on the 8 v2 tasks, 1 repeat, to compare the
    captured share and wall time with v2.

## Merge

`main` in `~/ozmadev/ozagia` has another session's uncommitted work in
`core/memory.py`, `core/reingest.py`, `adapters.py`, `cli.py` and other
files. The branch is pushed. It merges into `main` once that work is
committed, or now if the merge does not touch those files.

## Review (semantic_reviewer, NEEDS_CHANGES) and fixes

The review is at `/tmp/ozagia_review_v4/REVIEW.md`. Its findings and what
was done about each:

1. Shell `save` patterns were `<first word> *`, so "always allow" on
   `git diff` stored `git *`. `save` is now the exact commands, and `cd`,
   `pushd` and `popd` are not asked about.
2. Captured git steps read files outside the root (`--no-index`,
   `blame --contents`). Those options, `-O` and `--ignore-revs-file` now
   fall back.
3. A `VAR=x pytest` in a subdirectory gave a fake step error. It now falls
   back before anything runs.
4. With `texts=False`, the overlay went stale when a node was removed or
   re-added. Core keys an indexer looks up with `builder.find` are now
   recorded, and adding or removing one runs the pass. The docs state the
   regex rule the code implements.
5. Edit prompts had no diff. The preflight now sends `metadata.files` with
   the unified diff.
6. stderr was merged before `>/dev/null`. stdout and stderr are now
   routed separately, in redirect order.
7. `\r` was a separator. It is now part of a word.
8. Wrapper gaps:
   - added clustered options (`sudo -iu`, `env -iS...`), `function`,
     `coproc`, `watch`, `flock`, `runuser`, `strace`, `su|script|flock -c`
     and more shells;
   - segments now come from the bash-like lexer, so a mid-word `#` no
     longer hides a segment.
9. Smaller items:
   - `\{,n\}` now becomes `{0,n}`;
   - a filtered pytest step keeps its whole output;
   - a preflight answer without a permission goes to the built-in;
   - `isRefusal` matches only the Permission error tags;
   - the double prompt after a state change is documented;
   - ignored watched files count in the digest.
