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
(temporary file, fsync, rename). A store with another schema version is
refused, not overwritten.

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
`unblocked`, `dropped`. The newest 200 are kept, plus `created`.

## Criteria

Each criterion evaluates to `{status, evidence, reason}`, where status is
`pass`, `fail`, `stale` or `unknown`. A check stores the result on the
criterion (`last`).

| kind | args | passes when |
|---|---|---|
| `test_passes` | `key` (`test:`) | the test record's newest deciding run passed the test |
| `test_fails` | `key` (`test:`) | that run failed it |
| `symbol_exists` | `key` (`sym:`) | the symbol is in the graph |
| `symbol_absent` | `key` (`sym:`) | it is not |
| `calls` | `caller`, `callee` (`sym:`) | the symbol graph has the edge caller → callee |
| `not_calls` | `caller`, `callee` | it has no such edge |
| `no_callers` | `key` (`sym:`) | no symbol-graph edge points at the symbol |
| `file_exists` | `key` (`file:`) | the file is in the working tree |
| `file_absent` | `key` (`file:`) | it is not |
| `unchanged` | `key` (any key; `hash` is taken at creation) | the key's content hash still equals the stored one |
| `lint_clean` | `key` | never: always `unknown` (see below) |
| `review_resolved` | `packet`, `finding_id` | the finding id is absent from the review packet |
| `ops_has` | `key` (`ops:`), optional `attr`, `value` | the ops node exists; has a non-empty `attr`; whose value equals (or, for a list, contains) `value` |

Details:

- **Test results** come from the root's test record
  (`.ozagia/working_set/tests.json`, written by the daemon from pytest runs
  it captured or saw). The record now also keeps the last 50 runs: time,
  selectors (paths and node ids only), failed test keys, and whether the
  run completed, was filtered (`-k`, `-m`, affected-test subsets) or
  stopped early. The newest run that failed the test, or that selected it
  and completed unfiltered, decides. No such run: `unknown`.
- **Stale.** `test_passes`, `test_fails` and `review_resolved` are `stale`
  when their evidence (the run time, the packet's mtime) is older than the
  last change to the goal's targets (the newest mtime among the targets'
  files, and for test criteria the test's own file). Stale is not met:
  rerun the test or the review.
- **Calls** use the syntactic symbol graph (`docs/symbol_graph.md`), built
  from the tag cache, which needs the tree-sitter extra. Resolution is by
  name and imports, with no type inference. A removed caller or callee
  means no edge. An empty or unavailable symbol graph gives `unknown`.
- **Symbols** are looked up in the graph. `goal check` first takes pending
  file changes into its in-memory graph (`DevMemory.refresh(write=False)`;
  the snapshot on disk is not rewritten); `--no-refresh` skips that.
- **Content hashes** (`unchanged`): file bytes for `file:`/`doc:` keys; the
  lines of a `text:` range or of a symbol's or test's span for `sym:`/
  `test:` keys; the node's attributes for other keys.
- **Lint.** `ozagia.intent.lint` reports findings on changed lines in the
  edit's reply and stores nothing, so there is no last lint result to read.
  `lint_clean` is accepted (its key must resolve) and always evaluates to
  `unknown` with that reason, until a lint record exists.
- **Review packets** have the W17 schema `ozagia.repo_review.packet.v1`:
  `schema`, `findings` with `id`, `keys` and `suggested_goal {title,
  targets, criteria}`. The packet path is stored root-relative when it is
  inside the root; the criterion reads that file at each check, so
  rewriting it with a new review updates the result.

There is no shell-command criterion: commands would run outside harness
sandboxes, the reason background jobs are opt-in.

## Creation and status

Creation resolves every key against the graph: targets, key entries of
`scope` and each criterion's key arguments. An unknown key is refused,
and nothing is written. The exception is the subject of `symbol_exists`
and `file_exists`, where absence is the point. A goal needs a title and
at least one criterion.

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

  and, for a claimed goal with a scope, one advisory line naming edited
  files outside it:

  ```
  [ozagia] goal g1 scope note: docs/notes.md is outside the claimed goal's scope (advisory)
  ```

  Nothing is blocked. Claims are matched by (harness, session).
- The session brief (`working_set` op with `since`, and the hooks' own
  fallback brief) and the working set text given back after a compaction
  list the goals that are open or regressed, or claimed and not met or
  dropped, with their unmet criteria from the last check (`unchecked`
  before any), the session's claims first. They take at most a third of
  the brief's 1,500-character cap (and at most 450 characters) and a
  quarter of the working set's, and do not by themselves make a working
  set non-empty.

## Not in v1

Live model runs, a UI view, blocking edits on scope, shell criteria, and
reading or writing `.opencode/goals` (a third-party OpenCode plugin's
per-session lists; ozagia never touches it).
