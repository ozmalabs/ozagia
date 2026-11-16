# Goals

A goal is a repo-level record of work to do, addressed by graph keys.
It names the nodes the work is about (`targets`), what the work may
change (`scope`), and criteria a machine can check. Whether a goal is
met is decided by evaluating those criteria against the graph, the
working tree and the test record, never by what an agent says it did.

Goals belong to the repository, not to a session. Sessions claim them.
Code: `src/ozagia/core/goals.py`; CLI: `src/ozagia/goals_cli.py`.

## The record

Stored in `.ozagia/goals/goals.json` (schema version 1). Writes take a
file lock (`.ozagia/goals/.lock`) and replace the file atomically
(temporary file, fsync, rename). The lock is required: without `fcntl`, on
a filesystem where `flock` fails, or when another writer holds it for 30 s,
the command fails with an error instead of writing unlocked. A store with
another schema version is refused, not overwritten. Readers that never
write (the daemon's hints, the brief) read without the lock, through a
cache keyed on the file's mtime, ctime, size and inode, so an edit costs a
`stat` when the store has not changed.

Bounds: at most 500 goals that are not dropped (adding another is
refused) and 200 dropped goals (the oldest are removed on the next write);
targets and scope entries of at most 1,000 characters, criterion arguments
of at most 1,000 (a finding id 200), at most 100 targets, 100 scope entries
and 50 criteria per goal.

```
{"version": 1, "next_id": 3, "goals": [
  {"id": "g1", "title": "...", "why": "free text",
   "status": "open",                  # open|met|regressed|blocked|dropped
   "targets": ["sym:pkg/core.py::helper"],
   "scope": ["pkg/**", "file:pkg/core.py"],
   "criteria": [{"id": "c1", "kind": "test_passes",
                 "args": {"key": "test:tests/test_core.py::test_run"},
                 "last": {"status": "pass", "evidence": {...},
                          "reason": "...", "at": 1763300000.0}}],
   "claimed_by": [{"harness": "claude", "session": "...", "at": ...}],
   "history": [{"at": ..., "event": "created", "by": "cli", "detail": {...}}],
   "source": {"review_packet": "review.json", "finding_id": "..."},
   "created_at": ..., "updated_at": ..., "checked_at": ...}]}
```

`history` records every event: `created`, `claimed`, `released`,
`checked` (with counts by result), `met`, `regressed`, `blocked`,
`unblocked`, `dropped`. A `checked` event with the same counts and author
as the one before it updates that one's time and `repeat` count instead
of adding an entry. The newest 200 are kept, plus `created`.

## Criteria

Each criterion evaluates to `{status, evidence, reason}`, where status is
`pass`, `fail`, `stale` or `unknown`. A check stores the result on the
criterion (`last`).

| kind | args | passes when |
|---|---|---|
| `test_passes` | `key` (`test:`) | the newest deciding captured run passed the test, and nothing it depended on changed since |
| `test_fails` | `key` (`test:`) | that run failed it (an error is not a failure) |
| `symbol_exists` | `key` (`sym:`) | the symbol is in the graph |
| `symbol_absent` | `key` (`sym:`) | it is not |
| `calls` | `caller`, `callee` (`sym:`) | the symbol graph has the edge caller → callee |
| `not_calls` | `caller`, `callee` | it has no such edge |
| `no_callers` | `key` (`sym:`) | no symbol-graph edge points at the symbol |
| `file_exists` | `key` (`file:`) | the file is in the working tree |
| `file_absent` | `key` (`file:`) | it is not |
| `unchanged` | `key` (any key; `hash` is taken at creation) | the key's content hash still equals the stored one |
| `lint_clean` | `key` | never: always `unknown` (see below); `goal add` warns |
| `review_resolved` | `packet`, `finding_id` | the finding id is absent from the review packet |
| `ops_has` | `key` (`ops:`), optional `attr`, `value` (needs `attr`) | the ops node exists; has a non-empty `attr`; whose value equals (or, for a list, contains) `value` |

Details:

- **Test results** come from the root's test record
  (`.ozagia/working_set/tests.json`, written by the daemon from pytest runs
  it captured or saw). The record keeps the last 50 runs: start time,
  selectors (paths and node ids only), failed and errored test keys, and
  whether the run completed, was filtered or stopped early, its `source`
  and, for a captured run, the working-tree fingerprint at its start.
- **Only captured runs count.** A run is `captured` when the daemon ran
  the pytest itself (a `tool` shell call that became `shell_pytest`); its
  text is read from the daemon's own head line on, so output printed
  before it, a test's included, cannot forge a summary. A captured
  compound command (`pytest ...; echo ...`) is `compound`, and output a
  harness hands to `after` is `reported`; neither is evidence. Runs with
  `--version`, `--help`, `--co`/`--collect-only`, `--fixtures`,
  `--setup-plan`, `--setup-only` or `--markers` did not complete.
- **Deciding.** The newest captured run that failed the test or had it
  error decides; else the newest that selected it and completed, was not
  filtered and did not stop early after a failure. A run is filtered by
  `-k`, `-m`, `--deselect`, `--lf`, `--ignore`, `--ignore-glob`, `-o`/`-c`
  config overrides, `--pyargs`, `--rootdir`, affected-test subsets, the
  same options or any positional argument in `PYTEST_ADDOPTS` or the
  project's `addopts`, or a selector the record could not keep (not
  path-like, over 300 characters, more than 200). A run with more than 200
  failed or errored tests is kept as incomplete. When the newest captured
  run that selected the test unfiltered did not finish (a collection or
  import error, a timeout), the result is `unknown`. No deciding run:
  `unknown`.
- **Gone tests.** A test key that no longer resolves in the refreshed
  graph (deleted or renamed) is `unknown`, whatever the record says.
- **Errors.** A test that errored (setup, fixture) makes `test_fails`
  `unknown` and `test_passes` `fail`.
- **Stale** test results: see "Freshness of test results" below.
  `review_resolved` is `stale` when the packet's mtime is older than the
  newest mtime among the targets' files. Stale is not met: rerun the test
  or the review.
- **Calls** use the syntactic symbol graph (`docs/symbol_graph.md`), built
  from the tag cache, which needs the tree-sitter extra. Resolution is by
  name and imports, with no type inference. A caller or callee missing
  from both the symbol graph and the graph was removed, so there is no
  edge. One missing from the symbol graph but still in the graph (the tag
  parser skipped its file: a syntax error, an unsupported language) gives
  `unknown`, as does an empty or unavailable symbol graph.
- **Symbols** are looked up in the graph. `goal check` first takes pending
  file changes into its in-memory graph (`DevMemory.refresh(write=False)`;
  the snapshot on disk is not rewritten). When there is no graph (an MCP
  server without one) or the refresh raised, every kind that reads the
  graph (test kinds, symbol kinds, calls kinds, `ops_has`, `unchanged` of
  a non-file key) is `unknown`, with the reason. `--no-refresh` skips the
  refresh and marks each such result `"graph": "not refreshed"` in its
  evidence.
- **Content hashes** (`unchanged`): file bytes for `file:`/`doc:` keys; the
  lines of a `text:` range or of a symbol's or test's span for `sym:`/
  `test:` keys; the node's attributes for other keys.
- **Lint.** `ozagia.intent.lint` reports findings on changed lines in the
  edit's reply and stores nothing, so there is no last lint result to read.
  `lint_clean` is accepted (its key must resolve) and always evaluates to
  `unknown` with that reason, until a lint record exists.
- **Review packets** have the W17 schema `ozagia.repo_review.packet.v1`:
  `schema`, `findings` with `id`, `keys` and `suggested_goal {title,
  targets, criteria}`. A packet must be a regular file under the state
  dir's `reviews/` directory (`.ozagia/reviews/`; symlinks are resolved
  first, so a link to a file outside is refused) of at most 16 MB. It is
  opened without following a final symlink and without blocking, so a
  FIFO or a device is refused instead of hanging the server. Other paths
  are refused at creation and give `unknown` at a check. The path is
  stored root-relative when it is inside the root; the criterion reads
  that file at each check. Which review run may resolve a finding is left
  to the repo-review integration (W19).

There is no shell-command criterion: commands would run outside harness
sandboxes, the reason background jobs are opt-in.

## Creation and status

Creation resolves every key against the graph: targets, key entries of
`scope` and each criterion's key arguments. An unknown key is refused,
and nothing is written. The exception is the subject of `symbol_exists`
and `file_exists`, where absence is the point. A goal needs a title and
at least one criterion. Scope globs are compiled at creation; an invalid
one (`[z-a]`, `[]x`) is refused. `ops_has` with a `value` but no `attr`
is refused. A goal with `lint_clean` is accepted with a warning (in the
returned goal's `warnings`, and on stderr for the CLI): it cannot be met
until ozagia keeps lint results.

`scope` entries are graph keys or root-relative path globs (`**` crosses
directories, a trailing `/` or `/**` matches everything below). A
`file:`/`doc:` key or a glob admits the whole file. A `sym:`/`test:` key
admits an edit of its file when every changed or enclosing key the edit
reports in that file is that symbol, a member of it, or an enclosing
symbol.

Status moves only by:

- evaluation: `open` or `regressed` → `met` when every criterion passes;
  `met` → `regressed` when a criterion fails (a `stale` or `unknown` result
  does not regress a goal);
- commands: `drop`, `block` (a reason is required) and `block --clear`
  (back to `open`; the next check decides `met`).

A blocked or dropped goal is still evaluated when named in a check; its
status does not change. A check without ids skips dropped goals.

## Freshness of test results

A test result holds only while nothing it depended on, as far as the goal
says, has changed since the run started. The files compared are the
files of the goal's targets (for an `ops:` key, the path before `#`), of
its scope keys, the files matching its scope globs (among the files git
tracks or sees untracked now, the files in the run's commit, and the
files dirty at the run), and the test's file.

When the daemon is about to run a captured pytest, it takes a working-tree
fingerprint (`src/ozagia/core/fingerprint.py`) and stores it with the run,
whose time is the start time:

- `head`: the commit (`git rev-parse HEAD`);
- `dirty`: for each file `git status --porcelain` lists (untracked files
  included, the state dir left out), its git blob id (`git hash-object
  --stdin-paths`, one call), or null for a deleted file;
- `status`: a digest of the status output plus each dirty file's
  mtime_ns and size, kept as a cheap change signal only;
- `unhashed`/`overflow`: dirty files past the caps (500 files, 64 MB),
  whose content is unknown.

At a check, each compared file's content now is hashed the same way and
compared with its content at the run's start: the blob in `dirty`, else
the blob in `head`'s tree (`git ls-tree`), else absent. Any difference,
including a file deleted or created since, makes the result `stale`, with
the changed files in the evidence. A compared file over the run's hashing
caps is `stale` too.

Why this design:

- On the daemon's hot path it costs three git commands per captured
  pytest run (and nothing per edit). Hashing the files under the run's
  selectors and everything they import at run time would cost more and
  still miss config files.
- Content hashes, not mtimes, decide. Setting an mtime back (`touch -d`,
  `os.utime`) after an edit does not hide it, and an edit made while a
  long run was in progress is caught, because the fingerprint is taken
  before the run.
- Hashing at check time is bounded by the goal (at most 5,000 files;
  more gives `unknown`).

Limits:

- Only files the goal names count. An edit to a module the test imports
  that is outside the goal's targets and scope does not make the result
  stale; give the goal a scope that covers what the test exercises.
- Outside a git repository there is no fingerprint, and test criteria are
  `unknown`.
- Runs recorded before this change, `after` runs and compound runs have no
  fingerprint and are not evidence.
- A dirty symlink is compared as "not a regular file": retargeting it is
  not seen.
- Skipped tests are not told apart from passed ones (the record keeps no
  per-test outcomes beyond failures and errors).

## Commands

```
ozagia goal add --title T [--why W] [--target KEY ...] [--scope KEY|GLOB ...]
                [--criterion kind:args ...] [--file goal.toml]
ozagia goal list [--all | --status S]
ozagia goal show ID
ozagia goal check [ID ...] [--no-refresh]
ozagia goal claim ID --harness H --session S [--release]
ozagia goal drop ID [--reason R]
ozagia goal block ID --reason R | --clear
ozagia goal from-review PACKET FINDING_ID
```

All take `--root` and `--json`. `--criterion` is `kind:args`: arguments
are whitespace-separated in the kind's order, or `name=value`; a bare path
gets the kind's prefix (`test_passes:tests/t.py::test_x`):

```
ozagia goal add --title "Route retries through backoff" \
  --target sym:src/pkg/net.py::fetch --scope 'src/pkg/**' \
  --criterion 'calls:sym:src/pkg/net.py::fetch sym:src/pkg/retry.py::backoff' \
  --criterion test_passes:tests/test_net.py::test_fetch_retries \
  --criterion no_callers:sym:src/pkg/net.py::old_retry
```

A goal file has the same fields:

```toml
title = "Pin the web image"
why = "unpinned images change under us"
targets = ["ops:compose/compose.yaml#service/web"]

[[criteria]]
kind = "ops_has"
key = "ops:compose/compose.yaml#service/web"
attr = "image"
value = "nginx:1.25"
```

`from-review` turns a finding's `suggested_goal` into a goal (the
finding's `keys` are the targets when the suggestion has none), adds
`review_resolved` for the finding, and records the finding as the goal's
`source`. A second goal for the same finding is refused while the first is
not dropped.

## MCP tools

`goal_list`, `goal_show`, `goal_check`, `goal_add` and `goal_claim`
(coding and combined modes). An MCP server does not know the harness's
session id, so `goal_claim` takes `harness` and `session`.

## Daemon and harnesses

- The `goals` op is a read-only summary of the store as last checked
  (see `ozagia.daemon.protocol`); it evaluates nothing.
- After a `tool` or `after` answer that edited files, a session that
  claimed a goal gets one line per goal whose targets the edit touched,
  naming the criteria now affected (test and review criteria, which go
  stale, and criteria about the edited files):

  ```
  [ozagia] goal g1 "Route retries through backoff": this edit touched target sym:src/pkg/net.py::fetch; criteria affected: c1 calls ...; c2 test_passes ...; re-check with goal_check (ozagia goal check g1)
  ```

  This detail is given for the first three claimed goals; one more line
  names any other claimed goals whose targets the edit touched. One
  advisory line names
  edited files outside the union of the scopes of every goal the session
  claimed (none when a claimed goal has no scope, since its work is not
  bounded):

  ```
  [ozagia] goal scope note: docs/notes.md is outside the scope of the goals this session claimed (g1, g2) (advisory)
  ```

  Nothing is blocked. Claims are matched by (harness, session); a request
  without a harness matches no claim.
- The session brief (`working_set` op with `since`, and the hooks' own
  fallback brief) and the working set text given back after a compaction
  list the goals that are open or regressed, or claimed and not met or
  dropped, with their unmet criteria from the last check (`unchecked`
  before any), the session's claims first. In the brief they come after
  the failing tests and before running jobs, with a reserved share: at
  most a third of the brief's 1,500-character cap (and at most 450
  characters), kept when the brief is cut (the text before them is cut
  instead). In the working set they take at most a quarter of its cap,
  are read before the daemon takes its graph lock, and do not by
  themselves make a working set non-empty. With `free_text: false` (the
  OpenCode plugin) the lines leave out goal titles: ids, statuses, claim
  state and criterion labels only.

## Not in v1

Live model runs, a UI view, blocking edits on scope, shell criteria, and
reading or writing `.opencode/goals` (a third-party OpenCode plugin's
per-session lists; ozagia never touches it).
