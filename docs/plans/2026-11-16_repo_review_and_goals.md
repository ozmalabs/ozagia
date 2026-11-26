# Whole-repo review and graph-addressed goals

Date: 16 Nov 2026. Status: implemented, verified 2026-11-24.
`core/repo_review.py`/`ozagia review-repo` and `core/goals.py`/`ozagia goal`
are on `main` with their MCP/CLI surfaces; tests are
`tests/test_repo_review.py`, `tests/test_goals.py` and
`tests/test_review_goals.py`, docs are [`docs/repo_review.md`](../repo_review.md)
and [`docs/goals.md`](../goals.md). The design below is the original
specification; live harness runs for goals remain unverified.

Two features requested after wave 4:

1. A function that reviews a whole repository: architecture, tests, health,
   risks and operations, not just a diff.
2. A goal system. Because ozagia has a code graph, a goal can name the
   files, symbols, tests and ops units it is about, and its completion can
   be checked against the graph and the test record instead of inferred from
   prose.

## What exists

- `core/code_review.py` (`ozagia-review`): deterministic, graph-aware
  review of a commit range only. Packet `ozagia.code_review.packet.v1`,
  severities blocking/major/minor/note, `truth_assessed=false`, preflight
  with resource admission, `run_injected_review` seam for a model pass.
- `harness/review.py` `run_review_matrix`: role × model review of any
  packet, live only with `--live` and `OZAGIA_EVAL_LIVE=1`.
- `task_specs/coding/repo_scan.toml`: a task spec for a repo scan with no
  executor of its own.
- `docs/plans/2026-11-15_independent_product_review.md`: a manual
  whole-system review; the shape a generated review should approach.
- Graph facilities: `core/repo_map.py` (reference graph, PageRank,
  `impact`), `core/symbol_graph.py` (callers/callees/impact),
  `core/owners.py`, `core/ops_graph.py`, `core/testing.py`,
  `intent/affected.py`, `core/working_set.py` (`TestRecord`,
  `session_brief`), `core/sections.py`, `core/keys.py`.
- Goals: none. `TaskStep.goal` in `core/tasks.py` is a free-text string.
  The untracked `.opencode/goals` belongs to a third-party OpenCode plugin
  (per-session lists, no graph references); ozagia must not read or write it.

## W17: repo review (`core/repo_review.py`, `ozagia review-repo`)

Deterministic by default. No model calls, no network, no writes to the
repository. Reads the recorded graph (`.ozagia/graph.json`); if it is
missing or stale, says so and degrades rather than rebuilding silently
(`--index` rebuilds first).

Sections, each reporting what it checked and what it could not check:

- **architecture**: package/module dependency graph from the file
  reference graph; import cycles between top-level packages; hub modules
  (in/out degree, PageRank top N); layering violations if a
  `[review] layers` order is configured.
- **tests**: source modules and public symbols with no test edge
  (`tests_module`, `mentions_file`, `tested_by_file`); tests failing in the
  `TestRecord`; test-to-source ratio per package.
- **health**: very large files and functions (line counts from symbol
  spans), TODO/FIXME/XXX density, symbols with no callers and no external
  references (labelled heuristic: dynamic dispatch, entry points and
  `__all__` exports are excluded and listed as not checked).
- **churn and ownership**: git churn hotspots over a window (default
  90 days) crossed with test gaps; files touched by many sessions
  (`core/owners.py`).
- **docs**: doc references (`mentions_file`, code spans naming paths or
  symbols) that no longer resolve.
- **security hygiene**: tracked files with sensitive names, dangerous
  tokens (reuse `code_review` tables), committed `.env`-style files. Not a
  security audit; the packet says so.
- **ops**: from the ops graph, images without a pinned tag or digest,
  services without restart policy or health check, published ports.
- **dependencies**: declared dependencies (pyproject, package.json,
  Cargo.toml, go.mod) and whether a lock file exists.

Output: packet `ozagia.repo_review.packet.v1` with `sections`, typed
`findings` (`id`, `section`, `type`, `severity`, `keys` = graph keys the
finding is about, `evidence`, `confidence`, `suggested_goal`), `checked`,
`not_checked`, graph snapshot info and `truth_assessed=false`. Finding ids
are stable hashes of (type, keys) so two runs can be diffed
(`--compare previous.json` reports new/resolved/unchanged).

Limits: resource admission through `core.resources`, item caps per
section, a wall-clock budget; a truncated section says it was truncated.

Optional model pass: `--model-pass` builds a compact packet and hands it to
`harness/review.run_review_matrix` through an injected runner; live calls
keep the existing `--live` + `OZAGIA_EVAL_LIVE=1` gate. Model output is
recorded as separate, attributed findings, never merged into deterministic
ones.

Surfaces: `ozagia review-repo [--json] [--out] [--section …] [--compare]
[--fail-on]`, MCP `dev_review_repo` (summary plus top findings within a
budget, full packet path), docs `docs/repo_review.md`.

## W18: goals (`core/goals.py`, `ozagia goal`)

A goal is a repo-level record stored in `.ozagia/goals/goals.json`
(atomic write, file lock, schema version 1). It is not per session;
sessions claim goals.

```
id, title, why (free text), status (open|met|regressed|blocked|dropped),
targets:  [graph keys the work is about]
scope:    [graph keys or path globs the work may change]
criteria: [{id, kind, args}]   # machine-checkable
claimed_by: [{harness, session, at}]
history:  [{at, event, by, detail}]
source:   optional {review_packet, finding_id}
```

Criteria kinds, each evaluated from the graph, the git tree or the test
record, never from the agent's own claim:

- `test_passes` / `test_fails` (`test:` key; from `TestRecord`, with the
  run time; a criterion older than the last edit to its targets is
  `stale`, not met)
- `symbol_exists` / `symbol_absent` (`sym:` key)
- `calls` / `not_calls` (symbol edge present or absent)
- `no_callers` (symbol has no call edges, e.g. after removing a use)
- `file_exists` / `file_absent`
- `unchanged` (key content hash equals the hash at goal creation)
- `lint_clean` (last lint result for the file, from `intent/lint`)
- `review_resolved` (a repo-review finding id absent in the latest packet)
- `ops_has` (an ops-graph node/attribute, e.g. a pinned image)

There is no free shell-command criterion in v1; commands run outside
harness sandboxes, the same reason jobs are opt-in.

Creation resolves every key against the graph. Unknown keys are refused,
except as the subject of `symbol_exists`/`file_exists`, where absence is
the point. `ozagia goal check` re-evaluates all criteria and records each
result with its evidence (key, edge, test run time, hash). Status changes
only through evaluation (`met` when all pass, `regressed` when a met goal
fails again) or explicit user commands (`drop`, `block` with a reason).

Surfaces:

- CLI `ozagia goal add|list|show|check|claim|drop|block|from-review`.
  `add` takes `--target`, `--scope`, `--criterion kind:args` (repeatable)
  or `--file goal.toml`.
- MCP `goal_list`, `goal_show`, `goal_check`, `goal_add`, `goal_claim`.
- Daemon op `goals` (read-only summary) and an after-edit hint: when an
  edit touches a claimed goal's targets, the `[ozagia]` line names the
  goal and the criteria now affected; an edit outside a claimed goal's
  scope gets a one-line scope note (advisory, never a block).
- `session_brief` and the compaction context list open/claimed goals with
  their unmet criteria, within the existing brief cap.
- `from-review <packet> <finding-id>` turns a review finding's
  `suggested_goal` into a goal, with `review_resolved` as a criterion.

## Order

W17 and W18 run in parallel in their own worktrees; W18 treats
`review_resolved` and `from-review` as reading a packet file whose schema
is fixed above. Then a short W19 integration step wires `from-review`
against real W17 output and adds an end-to-end test (review → goal → edit
→ check → met). Each workstream gets a semantic review and a fix round
before merge, with the full pytest and ruff gate.

## Not in scope

Live model runs; reading or writing `.opencode/goals`; blocking edits on
scope; a UI view (the research UI can list goals later).
