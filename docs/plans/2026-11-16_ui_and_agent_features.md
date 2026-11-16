# UI, bounded agents and self-improvement: priority and architecture

**Date:** 2026-11-16. **Status:** proposal for review. This ranks the requested
UI, configuration, agent and Ocelot work, and fixes the first delivery.

## What already exists

Do not rebuild these:

| Capability | State | Consequence for this plan |
| --- | --- | --- |
| Reference management | Landed. `Reference` nodes, BibTeX/BibLaTeX/CSL-JSON/RIS, DOI/arXiv/PMID/PMCID normalisation, dedupe, export, citation checks. | Expose it in the UI; do not add a reference-manager backend. |
| Paper fetching | Landed. arXiv, Crossref, OpenAlex, Unpaywall and Europe PMC; preview/apply; content-addressed cache; SSRF guard; Zotero read-only. | Add fetch/screening views and provider status only. |
| Structured review | Landed. Plans, blind runs, validated findings, agreement, consensus/dissent, report and bundle. | Reuse for the code-review harness. |
| Ocelot reconciliation | Landed. `review_reconcile` with `subprocess`, `inprocess` and `auto` modes; structural clustering of findings; MIT attribution. | Extend to code review and other graph shapes, not to a new reconciliation engine. |
| Daemon, doctor, installers | Landed. Daemon stats, background jobs, `ozagia doctor`, OpenCode/Codex/Claude/Kiro install paths. | Build the status snapshot on top; do not invent a second process model. |
| Runtime services and tunnels | Landed. `core/runtime.py`, `ozagia runtime|service|process|tunnel`, MCP `dev_runtime_status`; declarative services, verified PIDs, relays, stale/unknown states. | This is the process/connection substrate. The status snapshot must reuse it; management actions are gated. |
| Worktree/branch checks | Not implemented. The in-flight W11 (`feat/paths-owners`) adds `core/owners.py` and `ozagia who` for file ownership; branch/worktree checks are separate. | New deterministic git module; no model calls. |
| Resource monitoring | Not implemented. Jobs and services have verified process groups and start tokens; no host/disk sampling, admission control or OOM watchdog. | New core module; reuse the process groups, add cgroup/rlimit/watchdog enforcement. |
| File creation tracking | Not implemented. Docs live in `docs/` with no `history/` policy, ledger or cleanup flow. | New ledger, Markdown router and dry-run/trash cleanup. |
| Graph export | Markdown report and raw graph bundle only. | This is the main gap: no versioned *view* model, no apertures, no renderers. |
| UI | None. | The first UI is a client of a new presentation layer. |
| Bounded tasks, task classes, model routing | None. | New work, but it reuses the harness, review and edit transaction. |

## Concurrent work and branch reservations

Checked 2026-11-16 16:58 against `main` at `070055b`. W8-W11 merged and the
four feature worktrees were removed while this plan was being written. Only a
detached `verify` worktree and the old `iac-hcl-cache` worktree remain. Counts
are a snapshot; the worktree check is the live source.

| Worktree | Branch | State at 16:58 | Work | Overlap with this plan |
| --- | --- | --- | --- | --- |
| `~/ozmadev/ozagia-wt/verify` | detached at `main` | Clean; intentionally detached | Verification worktree. | `worktree check` warns, not fails; policy still refuses a detached HEAD. |
| `~/.local/share/opencode/worktree/87d505/iac-hcl-cache` | `iac-hcl-cache` | Merged; 64 behind `main`; clean except `.opencode/` | Runtime services, tunnels, SRE MCP. | Status and management actions. |
| main checkout | `main` | 15 modified + 23 untracked | Research/Ocelot work plus the new read-only modules. | Code review, research UI, Ocelot enhancements. |

No feature branch is unmerged; the W8-W11 worktrees are gone. The detached
`verify` worktree is a verification fixture, not a mistake: `check` warns,
while commit/merge/push policy refuses it. `iac-hcl-cache` is merged and
behind; it is archival, not a wrong branch.

Full suite after the merge fix: **3221 passed, 21 skipped**. The earlier
`test_kiro_hooks.py::test_other_events_print_nothing[PostCompact]` failure was
transient and is resolved; the new read-only modules are covered by their own
tests.

### Sequencing consequences

- The four feature branches merged. `config.py` now has `[tests]` and `[lint]`;
  the settings registry must merge those keys rather than replacing the parser.
- `cli.py` now has `ozagia who`. Keep the new `ozagia-view`,
  `ozagia-worktree`, `ozagia-resources` and `ozagia-files` entry points
  separate until their CLI integration is reviewed.
- `daemon/server.py` now has W8/W9/W10/W11. The status snapshot reads daemon
  `stats` and the new ops; it does not edit the server.
- Reuse, do not duplicate: `core/owners.py` and `intent/paths.py` for
  ownership/path resolution; `intent/affected.py` for affected tests;
  `intent/lint.py` for lint gates; `core/working_set.py` for session evidence.
- File-ledger integration into `intent/edit.py`, the hooks and
  `daemon/server.py` can proceed now that the W8-W11 merge is green; the
  read-only ledger and `markdown_route` already work.
- `verify` is a read-only verification worktree. Do not edit it.
- The new read-only modules are untracked in the main checkout and should be
  committed separately from the merged feature work.

## Decision summary

1. **Do not build a standalone agent UI first.** OpenCode already owns the
   session, model, tool and permission surfaces. Duplicating them is the
   largest, least differentiating option.
2. **Build one host-agnostic presentation layer.** A versioned graph view
   model, data-defined apertures, static HTML export and a read-only local
   API. Every UI host consumes the same layer.
3. **Ship a standalone *console*, not a standalone *agent*.** It shows the
   graph, evidence, status and bounded-task runs. It opens OpenCode for
   open-ended work.
4. **Use the OpenCode add-in as the first interactive host.** It reuses
   sessions, models and permissions, and is the lowest-effort path to a live
   UI. The GUI plugin API is experimental, so the presentation layer remains
   usable without it.
5. **Reference management and fetching are not build items.** They are
   already implemented; the work is exposure, status and provenance.
6. **Bounded task mode is the strategic centre.** It is what makes ozagia
   different from an open-ended agent: explicit scope, budgets, gates,
   evidence and reproducible outputs.
7. **Worktree, branch and policy checks are deterministic core behaviour, not
   an agent.** They run before review, bounded tasks and any merge/push, and
   they report the exact git command behind every finding.
8. **Resources and file hygiene are deterministic core behaviour too.**
   Admission, limits, cleanup and Markdown routing are checks, not prompt
   advice; they fail early and report evidence.
9. **One-off Markdown goes to `history/`.** Root and `docs/` are for living or
   named documents; generated reports use stable paths, and `history/` keeps
   dated one-offs with their reason.

## UI host assessment

| Host | Value | Effort | Risk | Verdict |
| --- | --- | --- | --- | --- |
| Static HTML/JSON export | High for research, review and sharing; no server; archival. | S | Low | **Build first.** The reference implementation of every view. |
| OpenCode GUI add-in (panel, settings page, commands, slots) | High: live sessions, models, permissions, settings, graph panel. | M | Medium: GUI API is experimental and the supported host is the OpenCode app/desktop shell; confirm browser-tab support before committing. | **First interactive host.** |
| Standalone local console | Medium-high: full control, research-specific workflows, no OpenCode dependency. | M-L | Medium: another shell to maintain; auth and packaging. | **Later, as a thin shell over the same API.** |
| Ozma-ai web UI mount | Medium: existing FastAPI app, auth and multi-user surface. | M | Medium: couples ozagia to Ozma's release and deployment. | **Only if Ozma is the product host.** |
| VS Code extension | Medium for coding, low for research. | M | Low-medium. | Not a priority; coding-only. |
| OpenCode TUI plugin | Medium for status and commands, no rich graph views. | S | Low. | Cheap companion, not the main UI. |

The host decision is therefore not "standalone *or* add-in". It is:

```text
graph view model + apertures + renderers
        |                         |
  static HTML export        read-only local API
                                  |
                     OpenCode panel / standalone console
```

## Priority

Priority is strategic value. Wave is the recommended sequence; a high-value
item can be later when it depends on a foundation or carries high risk.
Effort: S = 1-3 days, M = 1-2 weeks, L = 3-6 weeks of focused agent work.

| # | Item | Value | Effort | Risk | Depends on | Wave |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | Graph view model + apertures + static export | 5 | M | Low | — | 1 |
| 2 | Settings registry + versioned `ozagia/` repo config | 4 | M | Medium | `config.py`/`cli.py` after W10/W11 | 1 |
| 3 | Worktree/branch management + disk awareness | 5 | S-M | Low | 2 for policy schema; W11 for ownership | 1 |
| 4 | Host resources, admission control and own OOM | 5 | L | Medium-high | 2; jobs/runtime; 3 for disks/mounts | 1-2 |
| 5 | File creation ledger + Markdown routing/cleanup | 4 | M | Low-medium | 2, 3; edit/hooks after reservations | 1-2 |
| 6 | Unified status snapshot (workspace, resources, litter, processes, connections) | 4 | S-M | Low | Runtime (landed); 3, 4, 5; W8 | 1-2 |
| 7 | Graph-based code review harness | 5 | M | Medium | 1, 2, 3, 4, 5; W9 | 2 |
| 8 | OpenCode add-in MVP (graph panel, status, settings, commands) | 4 | M | Medium | 1-6; host confirmation | 2 |
| 9 | Research UI exposure (references, fetch, screening, evidence, review) | 4 | S-M | Low | 1, 6 | 2 |
| 10 | Bounded task mode (scope, budgets, gates, evidence) | 5 | L | Medium | 2, 3, 4, 5; W8-W11 merged | 3 |
| 11 | Task class library (research, coding, ops) | 4 | M | Low | 2, 10 | 3 |
| 12 | Model registry, per-task defaults, optional classifier | 3 | M | Low | 2, 11 | 3 |
| 13 | Inspect agent logs and propose improvements | 3 | M | Medium-high | 10, 11 | 4 |
| 14 | Process/connection management actions | 3 | M | Medium | 4, 6, 8; runtime (landed), W11 | 4 |
| 15 | Scan repo and build modules (advisory first) | 4 | L | High | 4, 5, 7, 10, 11 | 4 |
| 16 | Ocelot pinned installer and default auto-detect | 3 | S-M | Medium | 2 | 4 |
| 17 | Ocelot enhancements for coding and other tasks | 3 | M | Medium | 7, 15 | 4 |
| 18 | Standalone console | 3 | L | Medium | 1, 6, 8, 9 | 5 |
| 19 | Upstream field-improvement submission | 2 | S-M | Medium | 13, 15 | 5 |

### Why this order

- **1 before every UI host.** Without a stable view model, each host grows its
  own graph queries and encoding rules. With it, a panel, a static report and a
  standalone console show the same graph.
- **2 before task classes and model routing.** Tasks, apertures, models and
  limits need one typed configuration surface with precedence and provenance.
  The schema and new module can start now; the `config.py` integration waits
  for the in-flight W10/W11 branches.
- **3 before 7 and 10.** Review and bounded tasks must verify the worktree,
  branch, drift and policy before they run. The check is deterministic and
  cheap; running it late is how a review or merge happens on the wrong base.
  Worktree placement also checks the disk and tmpfs before a build fills it.
- **4 before 10 and 14.** Admission control and limits belong inside bounded
  tasks and process management, not bolted on after an OOM. Observation and
  refusal can ship before enforcement; the watchdog and history follow.
- **5 before 14 and 15.** The file ledger and Markdown routing feed cleanup,
  repo scanning and the UI. Without them, every later feature creates more
  litter than the user can classify.
- **6 before a live UI.** "Processes and connections" is first a data problem:
  daemons, jobs, sessions, runtime services, tunnels, worktrees, resources,
  files, MCP servers, providers and integrations need one status shape.
  Management actions come later.
- **Branch reservations shape the start.** The four usage-plan branches are
  active with uncommitted work. The first slice avoids their files: the view
  model, worktree checks, resource sampler and renderer are new modules, and
  the status snapshot only reads existing surfaces. Settings and CLI wiring
  wait for the active branches to merge.
- **7 before 15.** Code review is the safest, highest-value demonstration of
  graph analysis. It exercises the same finding/reconciliation machinery that
  a repo-scanning agent would need, without letting an agent write code.
- **10 before 13 and 15.** Log inspection and repo scanning are bounded tasks.
  Building them as open-ended agents would repeat the problem bounded mode is
  meant to solve.
- **16 is not "bundle Ocelot in the wheel".** Ocelot has platform-specific
  runtime artifacts and seed files. The safe default is auto-detect an existing
  checkout; installation is explicit, pinned and checksummed.

## Wave 1: workspace and presentation foundation

### 1. Graph view model and apertures

An **aperture** is a data-defined projection of the graph, not a hard-coded
view. It follows Ocelot's observer aperture: an observer declares which
dimensions it `resolves` and at what `grain`; unresolved dimensions collapse to
a constant instead of becoming wildcards. For visualization:

- `resolves`: which node types, edge types and attributes this view can
  distinguish.
- `grain`: the unit of each dimension (individual node, file, symbol, claim
  family, reviewer, source, day).
- `scope`: root node, depth, filters, snapshot.
- `collapse`: how unresolved or over-grain nodes are grouped.
- `render`: layout and encoding hints (force, layered, timeline, matrix,
  Sankey, treemap, table).
- `limits`: node/edge caps and truncation policy.

Apertures are stored as data: built-ins ship with ozagia; a repository may add
or override them in its config directory. The same aperture can be rendered by
different visualizers and with different visual settings.

Proposed view model:

```json
{
  "schema": 1,
  "aperture": "consensus",
  "snapshot": {"root": "/repo", "head": "abc123", "freshness": "current"},
  "nodes": [
    {"id": "claim:12", "type": "Claim", "label": "...", "group": "claim",
     "attrs": {"status": "supported"}, "weight": 0.8}
  ],
  "edges": [
    {"id": "e1", "src": "run:3", "tgt": "finding:7", "type": "found",
     "attrs": {"severity": "major"}}
  ],
  "groups": [{"id": "claim", "label": "Claims", "collapsed": 4}],
  "legend": {"color": {"consensus": "#2a7", "dissent": "#c33"}},
  "layout": {"kind": "grouped-force", "seed": 7},
  "provenance": {"sources": ["review:plan-1"], "derived": true},
  "omitted": {"nodes": 0, "edges": 12, "reason": "edge cap"}
}
```

Built-in apertures for the first release:

| Aperture | Resolves | Grain | Renderer |
| --- | --- | --- | --- |
| `architecture` | modules, files, symbols, imports | module/file | layered or grouped force |
| `impact` | changed symbols, callers, tests, failures | symbol | layered DAG |
| `evidence` | claims, evidence, sources, holdings | claim/evidence | Sankey or bipartite |
| `provenance` | claim, passage, source, reference, fetch | source | layered chain |
| `citation` | references, citation edges, retractions | work | ego-network |
| `consensus` | review runs, findings, families, targets | finding family | grouped force/matrix |
| `timeline` | sessions, edits, tests, reviews, fetches | day/session | timeline |
| `screening` | references, decisions, stages, reasons | reference | table/flow |
| `settings` | config keys, sources, scopes, defaults | key | table |

The first slice ships `architecture`, `impact`, `evidence`, `consensus` and
`timeline`, a JSON schema, a deterministic projection, and a single-file HTML
renderer. The renderer may start with SVG and a small force/layered layout; no
frontend build step is required for the export.

Acceptance:

- `ozagia view --aperture <id> [--format json|html] [--root <node>] [--depth N]`
  emits a versioned view model and a self-contained HTML file.
- The same snapshot and aperture produce byte-identical JSON (fixed ordering,
  no timestamps) and a deterministic layout seed.
- Unknown types are reported as `other`, never silently dropped.
- Truncation reports `omitted` counts and why.
- Each node/edge keeps its graph key and provenance; the view never invents
  support, consensus or freshness.
- Tests cover empty graphs, cycles, missing attributes, caps, and an
  aperture override.

### 2. Settings registry and repository config directory

One typed registry for every setting: graph/index, worktree/policy, models,
tasks, research, review, UI, integrations and limits. Every value exposes:

```text
key, type, default, current, source (default|user|repo|env|flag), scope,
validation, description, restart_required
```

Precedence: built-in default < user config < repository config < environment
< explicit CLI/API argument. Writes are atomic and preview-first; the UI shows
the exact diff and which file will change.

The repository config is **`ozagia/`** (no leading dot), versioned and separate
from the state directory **`.ozagia/`**. The no-dot name is deliberate: it
cannot collide with `.ozagia/` on case-insensitive filesystems (macOS APFS,
Windows), and it reads as project configuration rather than local state. The
loader reads data files only; `ozagia/` must not contain `__init__.py` and is
never imported as a Python package.

Proposed layout:

```text
ozagia/
  config.toml           # overrides; same schema as the registry
  apertures/*.toml      # view definitions
  tasks/*.toml          # task classes: scope, tools, budgets, gates, output
  models.toml           # defaults, per-task routing, caps
  prompts/*.md          # versioned prompts referenced by task classes
  review.toml           # review policy, roles, minimum reviewers
  worktrees.toml        # expected worktree/branch mapping
  policies.toml         # commit/merge/push policy
  markdown.toml         # routing, root allowlist, history layout, budgets
  files.toml            # ledger retention, cleanup/trash policy
  adapters.toml         # optional local indexers/edit engines
  modules/              # opt-in local Python modules, never auto-imported
```

Acceptance:

- `ozagia config schema` prints the registry as JSON.
- `ozagia config get/set --scope repo|user --dry-run` validates and shows a
  diff.
- Existing `.ozagia.toml` still loads; a migration command rewrites it into
  `ozagia/config.toml` without changing behaviour.
- `ozagia/` is versioned, `.ozagia/` is gitignored, and a test asserts the
  loader never writes state into the versioned directory.
- Unknown keys are rejected with a path and a suggestion.
- `ozagia doctor` reports every active setting's source.

### 3. Worktree and branch management

This is deterministic git, not an agent. It answers five questions before any
other task starts:

- **Am I in the right worktree?** Expected path and branch from
  `ozagia/worktrees.toml`, a directory pattern (`ozagia-wt/<name>` ->
  `feat/<name>`), the target root, the session record and the task class.
- **Has the worktree drifted?** Ahead/behind upstream and the base branch,
  dirty/staged/untracked/conflicted files, stashes, detached HEAD, in-progress
  merge/rebase/cherry-pick/revert/bisect, stale or prunable registrations,
  missing paths, unpushed commits, and overlapping uncommitted changes in
  another worktree.
- **Is there room here?** Mount, filesystem type, free bytes/inodes and
  tmpfs. A worktree under `/tmp` on tmpfs consumes memory, not disk; the
  worktree check and create path apply the resource policy before writing.
- **Does the commit/merge/push policy pass?** Data-driven rules; every result
  carries the exact git command and its output digest.
- **Is a mutation safe?** All actions are preview-first and re-check the policy
  after the preview.

The four active `feat/*` worktrees are the first real fixture: a check run
today must report the expected branch, zero commit drift against `main`, no
upstream, and their uncommitted file sets. Dirty is in-progress for
`worktree check`; it blocks `policy check --for merge|push`.

Data per worktree:

```json
{
  "path": "/home/matt/ozmadev/ozagia-wt/working-set",
  "head": "3986471",
  "branch": "feat/working-set",
  "detached": false,
  "filesystem": {"mount": "/home", "fstype": "ext4", "free_gb": 12.4,
                 "free_pct": 8.1, "is_tmpfs": false},
  "expected": {"branch": "feat/working-set", "source": "worktrees.toml"},
  "upstream": null,
  "ahead": 0,
  "behind": 0,
  "base": {"branch": "main", "ahead": 0, "behind": 0, "merge_base": "3986471"},
  "dirty": {"staged": 0, "unstaged": 10, "untracked": 1, "conflicted": 0},
  "stashes": 0,
  "operation": null,
  "policy": [{"id": "commit.branch", "level": "ok", "command": "git symbolic-ref --short HEAD"}]
}
```

Expected mapping, when a directory pattern is not enough:

```toml
[worktrees.main]
path = "."
branch = "main"
protected = true

[worktrees.working-set]
path = "../ozagia-wt/working-set"
branch = "feat/working-set"
base = "main"
allow_dirty = true          # active task; dirty blocks merge/push, not check
```

Checks use git plumbing only: `worktree list --porcelain`, `status
--porcelain=v2 --branch`, `rev-list --left-right --count`, `merge-base`,
`for-each-ref`, `stash list`, `ls-files -u`, `rev-parse` for the operation
files, and `merge-tree` for a conflict preview. No model call and no network
by default; `fetch` is explicit. Every check is point-in-time and carries
`checked_at`; review, merge, push and bounded-task preflights re-run it rather
than trusting a cached status tile.

Policy examples:

- **commit:** branch pattern, no direct commits to `main` or another protected
  branch, author identity, signed commits when required, message pattern, no
  merge commits, no `fixup!`/`squash!` commits, no whitespace errors, no
  generated/large/binary files, no unstaged changes when `--staged-only` is
  set.
- **merge:** target branch, clean source and target, no conflicts, fast-forward
  or merge-commit mode, required tests/review record present, branch up to date,
  no branch checked out elsewhere.
- **push:** remote and branch allowlist, no force-push to a protected branch,
  no direct push to `main`, upstream set, no tags unless named, atomic
  multi-ref only.

Branch/worktree mutations are preview-first: delete a branch only when it is
merged, not checked out, has no unpushed commits and is not protected; remove a
worktree only when clean or explicitly forced; prune only registrations git
already reports as prunable. The first release does not silently switch a live
session's branch or move a worktree; it reports the mismatch and opens the
expected path.

Commands and MCP:

```text
ozagia worktree list|status|check|prune [--json]
ozagia branch status|drift|check [--json]
ozagia policy check --for commit|merge|push [--json]
ozagia worktree create|remove|lock|unlock|move ...   # preview-first
ozagia branch create|switch|rename|delete ...        # preview-first
```

MCP read-only: `dev_worktree_status`, `dev_branch_check`,
`dev_policy_check`. Mutating actions stay CLI/UI with confirmation: a tool call
must not silently commit, merge or push. Optional `pre-commit`,
`pre-merge-commit` and `pre-push` hooks call the same checks; installation is
opt-in and reversible.

Until the settings registry lands, `core/worktree.py` reads
`ozagia/worktrees.toml` and `ozagia/policies.toml` with a small standalone
loader. The registry later owns validation and precedence; the worktree module
keeps the git checks.

Integration:

- status snapshot: a workspace tile with worktrees, branches, drift and policy
  results;
- code review: preflight refuses a stale/wrong worktree and reviews the
  expected `base...head` range;
- bounded tasks: gates `worktree_expected`, `branch_expected`, `drift_clear`,
  `policy_commit|merge|push`;
- harness `hello`: after W11's protocol change, carry the worktree path and
  branch so a session started in the wrong place warns at session start;
- W11 (`feat/paths-owners`): share the cross-session ownership record; this
  feature adds branch/worktree ownership, not a second owner table;
- `.ozagia/`: cache only the last check; git remains the source of truth.

Acceptance:

- `ozagia worktree check --json` on the four active worktrees reports the
  expected branch, zero commit drift against `main`, their dirty counts and no
  wrong-branch or stale-registration errors.
- A deliberately wrong checkout, detached HEAD, a stale registration and an
  in-progress rebase each produce a distinct finding with the exact command.
- A worktree on tmpfs `/tmp` reports `is_tmpfs: true` and is refused or warned
  by `resources.worktrees.avoid_tmpfs`; a normal filesystem with enough free
  space passes.
- Dirty files are reported by `worktree check` but block
  `policy check --for merge|push`.
- `policy check --for merge` refuses a conflicted merge preview and a missing
  required check.
- No check calls a model, fetches, or writes to the repository.
- The same checks run from the status snapshot and the review/task preflight.

### 4. Host resources, admission control and own OOM

Agents do not see the host. This is the deterministic layer that does: it
observes memory, disk, CPU/load and I/O pressure, attributes what it can to
ozagia-started work, estimates what a new run needs, refuses or delays unsafe
runs, and stops ozagia's own over-limit process groups before the kernel OOM
killer picks a victim.

It is **not** a system-wide OOM killer. It never signals a process it did not
start, and it never treats a host-wide reading as proof that a particular
session caused it. Attribution is explicit.

#### Disk and worktree awareness

- Resolve the mount and filesystem for a path with `statvfs`/`stat`:
  mount point, `fstype`, free bytes, free inodes, device id.
- Detect **tmpfs**: a worktree under `/tmp` on tmpfs consumes memory, not disk.
  The policy can warn or refuse (`avoid_tmpfs`), and the status UI labels it.
- Estimate repository/worktree size from `git count-objects -v`, the existing
  worktree's size and known cache directories, not an unbounded `du`.
- Worktree create/check reports mount, fstype, free space/inodes and whether
  the minimum-free policy passes. A full disk or a tmpfs worktree is a
  first-class finding, not a generic failure.
- Disk cleanup suggestions are explicit and sized (`.venv`, `target`,
  `node_modules`, `__pycache__`, `.pytest_cache`); nothing is deleted without
  confirmation.

#### Host and process observation

- Linux first: `/proc/meminfo`, `/proc/pressure/{cpu,memory,io}`, cgroup v2
  `memory.current/max/events`, `/proc/<pid>/stat`, `statm` and
  `smaps_rollup`, `os.getloadavg`, CPU count.
- macOS/Windows: portable host totals and per-process RSS via an optional
  `psutil` extra, or platform tools. Missing metrics are reported as
  `unknown`, never guessed.
- Jobs and services already have verified process groups and start tokens
  (`daemon/jobs.py`, `core/runtime.py`). Sample their group descendants; use
  a cgroup per job when delegation is available.
- A session maps to the jobs/services/process groups it started. External
  processes are visible host-wide but not attributed to a session.
- Sampling is cheap and bounded: one host sample per interval, per-process
  samples only for live ozagia-owned groups, peak values kept.

#### History and expectations

Record a bounded history in `.ozagia/resources/` keyed by task class or a
hashed command signature (never raw argv, which may contain secrets):

```text
key, task_class, samples, p50_mb, p90_mb, max_mb,
p50_disk_mb, p90_disk_mb, p50_s, p90_s, exit_codes, host_baseline
```

The estimate is the p90 plus a safety margin, capped by the current headroom.
With too few samples it returns `confidence: "low"` and falls back to the
configured default or refuses an unusually large request. The host baseline is
an EWMA of normal memory/disk/load, so a busy desktop is not mistaken for an
agent's usage.

#### Admission control

Before spawning a job, bounded task, test run or worktree operation:

1. compute current headroom after the configured OS reserve;
2. estimate the request from history or the task class's declared budget;
3. if the estimate does not fit, do not spawn;
4. if another ozagia-owned job is expected to free enough within the wait
   window, return `retry_after_s` and suggestions;
5. wait only when the caller explicitly asks (`--wait-for-resources`), with a
   deadline and cancellation.

A refusal is structured, not a traceback:

```json
{
  "ok": false,
  "reason": "insufficient_memory",
  "requested_mb": 4096,
  "available_mb": 1200,
  "expected": {"p90_mb": 3800, "samples": 14, "confidence": "medium"},
  "shortfall_mb": 2896,
  "retry_after_s": 90,
  "suggestions": ["stop job j-11", "retry in ~90 s", "use a 2 GiB limit"]
}
```

#### Enforcement and own OOM

- Limits: memory, process count, wall time, optional per-file size, and a
  best-effort disk budget for the job's worktree/cwd.
- Mechanism: cgroup v2 `memory.max`/`pids.max` when delegated; otherwise
  `resource.setrlimit` plus a watchdog sampling RSS and the filesystem.
- Kill order: SIGTERM to the verified process group, grace period, SIGKILL.
  Verify the PID start token and process-group ownership first, exactly as
  jobs do today. A stale token is never signalled.
- Set `oom_score_adj` on ozagia-owned children so that, if the kernel OOM
  killer runs despite the watchdog, it prefers ozagia's job over the user's
  shell or desktop. Restore the value for reused processes where possible.
- Keep an OS reserve (memory/disk). If the host is already below the reserve,
  refuse new work rather than starting something that will fail.
- Disk enforcement is best-effort: a process can write through paths ozagia
  cannot attribute. The watchdog stops a job when its worktree/filesystem
  crosses the reserve and reports the evidence; it does not claim exact usage.
- Feed a `resource_event` back to the originating session: job id, session,
  command class, peak/limit, host headroom, action taken, and the suggested
  retry. The daemon adds a `[ozagia]` line; MCP `job_wait`/`job_log` and the
  UI show the same event.

#### Configuration

```toml
[resources]
enabled = true
sample_s = 2
watchdog = "auto"          # cgroup, rlimit, watch, off
reserve_memory_pct = 10
reserve_disk_pct = 5
reserve_disk_gb = 1
history = true

[resources.limits]
job_memory_mb = 2048
job_pids = 256
job_disk_mb = 4096

[resources.admission]
min_free_memory_pct = 15
min_free_disk_pct = 10
min_free_disk_gb = 2
wait_max_s = 300

[resources.worktrees]
root = "../ozagia-wt"
min_free_gb = 5
avoid_tmpfs = true
```

#### Commands and MCP

```text
ozagia resources status|watch|history|explain [--json]
ozagia resources check --for job|task|worktree ... [--json]
```

MCP read-only: `dev_resources_status`, `dev_resources_check`,
`dev_resources_explain`. Killing a process or cancelling a job remains a
guarded action in the job/service command layer.

#### Integration

- worktree manager (item 3): disk/mount/tmpfs checks before create, and in
  `worktree check`;
- status snapshot (item 6): host, per-session and per-job resources, pressure,
  alerts and the last resource event;
- bounded task mode (item 10): `memory_mb`, `disk_mb`, `pids`, `wall_s` in the
  task budget and admission/resource gates;
- jobs/services/LSP/test/lint: register their process groups; route new spawns
  through one `resources.spawn` wrapper where practical;
- code review (item 7): refuses a run whose expected test/build will not fit;
- UI: a resources tile and a "why was this refused?" explanation.

#### Acceptance

- A job exceeding its memory limit is stopped with SIGTERM/SIGKILL, the daemon
  survives, and the session receives a `resource_event` naming the peak, limit
  and job.
- Admission refuses a 4 GiB job on a host with 1.2 GiB headroom and spawns
  nothing; the error names the shortfall and a suggestion.
- A worktree under a tmpfs `/tmp` is detected and refused or warned by policy;
  a worktree on a normal filesystem with enough free space passes.
- History predicts p90 from prior runs; with too few samples it reports low
  confidence and uses the conservative default.
- A job expected to finish in 90 s produces `retry_after_s: 90`;
  `--wait-for-resources` waits and starts, and cancellation stops the wait.
- A stale PID/start token is never signalled; an unrelated process is never
  signalled.
- On a platform without cgroups, observation and admission still work;
  enforcement falls back to rlimit/watch and reports the degradation.

#### Non-goals

- No system-wide OOM killer, no killing other users' processes, no global
  scheduler.
- No GPU/network QoS in the first release.
- No guarantee against a kernel OOM caused by processes outside ozagia; the
  claim is bounded to ozagia-owned work and early refusal.

### 5. File creation ledger and markdown routing

Agents leave files behind. This records every file ozagia can attribute to a
session, why it exists, how it was made, and what should happen to it. It also
routes new Markdown so one-off notes do not accumulate in the repository root
or `docs/`.

#### Ledger

Capture:

- handled `edit`/`write`/`patch`: path, new/changed, diff summary, tool,
  session, task class and reason;
- jobs/services and captured shell: simple `touch`, `mkdir`, redirects, `cp`,
  `mv`, `git apply`; otherwise a before/after worktree manifest
  (`git status --porcelain -z`, bounded, caches excluded);
- session start/end: a manifest diff catches files created outside handled
  calls; those get `reason: unknown` and a cleanup prompt.
- ignored/cache trees are summarized by top-level directory (size, known
  generator), not one ledger row per file. Known generated outputs are
  classified by rule.
- moves and deletes keep the record: `moved_from` or `status: deleted` with the
  actor and tool, so cleanup can explain what happened.

Record metadata, not content:

```json
{
  "id": "file-...",
  "path": "history/2026-11-16/ocr-comparison.md",
  "worktree": "/home/matt/ozmadev/ozagia-wt/ocr",
  "hash": "sha256:...",
  "size": 4210,
  "created_at": "2026-11-16T16:30:00+11:00",
  "created_by": {"session": "ses_...", "harness": "opencode",
                 "task": "ocr.compare", "tool": "write", "command": null},
  "reason": "one-off comparison of OCR backends",
  "classification": "history",
  "status": "active",
  "references": ["doc:docs/research/pdf.md"],
  "duplicate_of": null,
  "cleanup": {"policy": "keep", "after": null}
}
```

No raw prompt or file content is stored; the reason is a short label, and the
hash plus diff summary is enough for cleanup. The reason comes from the task
class, a reserved `_reason` on the handled call, or the shell command context;
otherwise it is `unknown`. The ledger lives in `.ozagia/files/` and is bounded
per worktree.

#### Markdown routing

Defaults:

| Kind | Destination |
| --- | --- |
| root README/CHANGELOG/LICENSE/CONTRIBUTING/AGENTS | root allowlist |
| living guide | `docs/` |
| dated plan | `docs/plans/YYYY-MM-DD_topic.md` |
| dated result/report | `docs/results/<topic>_<date>/README.md` |
| one-off analysis/note | `history/YYYY-MM-DD/<slug>.md` |
| scratch, not worth keeping | `.ozagia/scratch/<session>/` |

`markdown_route(path, content, context)` returns `kind`, `suggested_path`,
`reason`, `duplicate_of`, `append_to` and `confidence`. It is deterministic:
path/name rules, front matter, title and headings, size, hashes and
near-duplicate shingles. No model call.

New Markdown under `history/` gets front matter:

```markdown
---
created: 2026-11-16
session: ses_...
task: ocr.compare
reason: one-off comparison of OCR backends
status: historical
retention: keep
related: [docs/research/pdf.md]
---
```

Routing policy:

```toml
[markdown]
history = "history"          # versioned, date-partitioned
history_index = true
new_file_budget = 3          # per session/task; warn, then require a reason
duplicate_threshold = 0.85   # near-duplicate suggestion
root_allow = ["README.md", "CHANGELOG.md", "LICENSE", "CONTRIBUTING.md",
              "AGENTS.md", "SECURITY.md"]

[files]
ledger = true
trash = ".ozagia/trash"
retention_days = 30          # for scratch; history is keep-by-default
```

Content classification goes beyond the filename. Front matter (`kind`,
`status`, `retention`, `topics`, `related`) and headings such as `Decisions`,
`Evidence`, `Claims`, `Open questions` and `Results` identify blocks that
belong in a record rather than prose. When a note is ingested, those blocks are
routed to the existing records (`Decision`, `Claim`, `Reference`, `Session`)
with a backlink to the Markdown file; the file keeps the narrative and the
record keeps the structured data. This is a deterministic parser over headings
and explicit markers, reusing `document_claim_ingest` and the reference parser,
not a new NLP model.

To slow accumulation:

- root Markdown outside the allowlist is refused or routed to
  `history/YYYY-MM-DD/`;
- a new file needs a reason; without one, route to `history/` or ask;
- if a same-topic document exists, suggest appending a dated section instead
  of creating another file;
- a session or task has a new-Markdown budget (default 3); exceeding it warns
  and requires a reason;
- generated reports use one stable path, not a new numbered file each run;
- `history/` gets a generated `index.md` from the ledger/front matter, not a
  hand-maintained list.

#### Cleanup

`ozagia files list|explain|orphans|cleanup` and MCP `dev_files_list`,
`dev_files_explain`, `dev_files_cleanup`. Cleanup is dry-run by default; apply
moves the file to `.ozagia/trash/` with its ledger record, never deletes it
outright. Referenced files (graph, docs, tests, front matter) are never
deleted; a file with `reason: unknown` is always `ask`. Cleanup runs before
`worktree remove` and when the resource monitor is short on disk.

#### Integration

- worktree remove/merge: cleanup candidates before the worktree disappears;
- status snapshot (item 6): litter count, orphan count, history/scratch size;
- resource monitor (item 4): disk cleanup suggestions;
- bounded tasks (item 10): every output artifact gets a ledger row and a
  declared retention;
- code review (item 7) and research UI (item 9): reports route through
  `markdown_route`.

#### Acceptance

- A new root `.md` outside the allowlist is routed or refused with the
  suggested `history/YYYY-MM-DD/` path; an allowlisted root doc passes.
- A one-off Markdown note gets front matter and a ledger row; `ozagia files
  explain` shows the session, task, tool and reason.
- A file created by a shell command or job is captured at session end with
  `reason: unknown` and appears in cleanup.
- `ozagia files cleanup --dry-run` lists size, reason, references and policy;
  `--apply` moves to trash; a referenced file is refused; deletion needs
  confirmation.
- A duplicate or near-duplicate Markdown file suggests append/merge rather
  than a new file.
- A Markdown file with explicit Decisions/Evidence/Claims sections yields
  records with backlinks to the file; the narrative is not duplicated.
- The ledger stores no raw prompt or file content.

#### Non-goals

- No full filesystem DLP or content indexing beyond the existing research
  ingest.
- No automatic deletion, and no cleanup of files ozagia did not observe
  without marking them `unknown`.
- No tracking of files outside the repository/worktrees or outside the
  session manifest.

### 6. Unified status snapshot

One read-only shape for:

- **Workspace:** worktrees, branches, expected mapping, ahead/behind base and
  upstream, dirty/untracked/conflicted state, in-progress git operation and
  policy results.
- **Resources:** host memory/disk/load/pressure, mounts and tmpfs worktrees,
  per-session and per-job usage, limits, admission refusals and the last
  resource event.
- **Files:** created-file ledger, litter/orphan counts, `history/` and scratch
  size, last cleanup candidates and unclassified files.
- **Processes:** runtime services and raw processes from
  `core.runtime.runtime_status` (configured, starting, running, exited, failed,
  stopped, stale, unknown), daemons (root, pid, uptime, socket, idle timeout),
  background jobs, index builds, review runs and bounded tasks.
- **Connections:** runtime tunnels (configured, not connected, held by this
  process, stale lease), MCP servers, model providers, research providers,
  Zotero, OpenCode/Codex/Claude/Kiro/Ozma integrations.
- **Capabilities:** tree-sitter languages, LSP, SCIP, PDF/OCR, GROBID, Pandoc,
  Ocelot, semantic index.
- **Health:** configured/available/connected/degraded/unknown, version, last
  error, last seen, permissions.
- **State:** snapshot root/head/freshness, graph size, state-dir size.

This composes the landed `core.runtime` snapshot, the worktree checks, the
resource sampler, the file ledger, `doctor`, daemon `stats`, config and
integration probes. It is not a second service manager and does not redefine
runtime states. A configured service is not "running"; a persisted tunnel
lease not held by this process is "stale", not "connected". It must distinguish
"absent" from "unknown" and never claim a stale index is current.

Acceptance: `ozagia status --json` returns the snapshot; the UI renders it
without shelling out per tile; `runtime_status`/`dev_runtime_status` values are
preserved; the four active worktrees appear with the expected branch, zero
commit drift and their dirty counts marked in progress; a host resource alert,
an admission refusal, a killed-job event, a litter/orphan count and a cleanup
candidate are visible; tests cover a configured-but-not-started service, a
stale tunnel lease, a missing daemon, a stale snapshot, an unreachable
provider and an unsupported optional tool.

## Wave 2: visible value

### 7. Graph-based code review harness

A bounded review of a diff or commit range, not an open-ended agent. It starts
with the worktree preflight from item 3: expected worktree and branch, clean
enough to review, a passing commit policy, and a resource admission check for
the tests/build it will run. A wrong worktree, a stale base, unresolved
conflicts or an expected OOM/disk failure refuse the run before a model is
called.

```text
worktree/branch/policy preflight
         -> git diff -> changed files -> graph symbols -> callers/callees
         -> covering tests -> deterministic checks (lint, types, tests)
         -> graph risk findings -> optional independent model reviewers
         -> reconciliation -> review packet (findings, evidence, omissions, dissent)
```

Findings are typed: `breaking_change`, `missing_test`, `unhandled_error`,
`stale_reference`, `scope_creep`, `duplicate_logic`, `contract_mismatch`,
`security_sensitive`, `performance_risk`, `unknown`. Each carries file/line,
node keys, an evidence path, severity and a confidence note. The harness never
turns agreement into proof.

Reuse `research.review` for plans, independent runs, validation, agreement and
reconciliation. The existing Ocelot adapter clusters findings by issue family
and target; the code-review harness supplies those fields from the diff.

Acceptance:

- `ozagia review code --range HEAD~1..HEAD [--live]` emits a packet and exits
  non-zero only on configured blocking findings.
- Deterministic findings work without a model. Live review is gated by
  `OZAGIA_EVAL_LIVE=1` and an explicit `--live`.
- A finding with no evidence path is reported as `unverified`, not dropped.
- A wrong worktree/branch, unresolved conflicts, a failing preflight policy or
  an expected resource shortfall refuses the run and reports the exact check.
- The packet records the worktree path, base/head commits, policy results and
  resource estimate.
- The packet output path is chosen by `markdown_route` and recorded in the
  file ledger; a one-off review does not land in the root or an unrelated
  `docs/` page.
- Tests cover renamed files, binary files, generated files, deletions, a
  missing test, a stale base hash and a wrong-branch preflight.

### 8. OpenCode add-in MVP

A GUI plugin with four contributions:

- **Graph panel:** aperture picker, renderer, filters, node inspector, "open in
  editor", "show evidence". Reads the view model from the local API or a
  generated bundle.
- **Status panel:** worktrees and branches with drift/policy badges, host and
  per-session resources, file litter/orphans, daemon, snapshot freshness,
  runtime services and tunnels, jobs, providers, integrations.
- **Settings page:** the registry, with source badges and dry-run writes.
- **Commands:** "Check worktree", "Check policy", "Explain resources",
  "Explain file", "Clean up files", "Review this diff", "Show impact",
  "Open evidence", "Start bounded task", "Reindex".

The plugin talks to a small TypeScript server part that invokes the `ozagia`
CLI/JSON API. It does not import Python. If the GUI API cannot load in the
target web host, the same panel is served by the standalone console; no view
logic is duplicated.

Acceptance: the panel renders a view model in the OpenCode app, the settings
page reads real values, and a missing daemon, stale snapshot, resource refusal
or litter/orphan row shows an actionable state rather than an empty graph.

### 9. Research UI exposure

No new research backend. Add views over existing operations:

- Reference library: identities, versions, citekeys, retraction status,
  unresolved keys, duplicates.
- Fetch: provider, candidate, licence, hash, cache path, preview/apply.
- Screening: stage, decision, reason, reviewer, PRISMA counts.
- Evidence: claim → evidence → source/passage locator table.
- Review: plan, runs, findings, agreement, consensus/dissent, dissent
  preserved.
- Manuscript: checks, bundles, unresolved citations.

This is the fastest visible win because the operations already exist. The
first version can be read-only; writes stay behind the existing service
operations and confirmation.

## Wave 3: bounded agency

### 10. Bounded task mode

A deterministic task runner, not a chat session. A task class declares:

```toml
id = "code_review"
version = 1
objective = "Review a diff for correctness, tests and contract risk."
inputs = ["diff", "repo_root"]
tools = ["read", "grep", "glob", "graph_search", "test"]
write_scope = []                 # no writes in this class
worktree = {expected_branch = "feat/code-review", require_clean = true}
budget = {tokens = 40000, wall_s = 900, model_calls = 8, cost_usd = 0.50}
resources = {memory_mb = 2048, disk_mb = 4096, pids = 256}
gates = {before = ["snapshot_fresh", "worktree_expected", "branch_expected",
                   "drift_clear", "resources_fit"],
         after = ["schema_valid", "evidence_present", "policy_commit",
                  "resource_peak_recorded"]}
stop = ["gate_failed", "budget_exhausted", "resource_limit", "no_progress"]
output = "review_packet.v1"
approval = {mutate = "ask", network = "ask"}
```

The runner:

1. validates the task class and inputs;
2. checks preconditions (snapshot freshness, expected worktree/branch, drift
   and policy, resource admission, path containment, permissions);
3. executes typed steps, each with a tool allowlist and budget;
4. records every step, tool call, model call, gate result and artifact;
5. stops on a gate failure, budget, repeated state or no progress;
6. emits the declared output schema plus an evidence log.

The model is called only inside steps that declare it. A step cannot widen its
own tool or path scope. Mutating steps are preview-first and require approval
unless the task class explicitly says `mutate = "auto"` and the caller has
confirmed the scope. Every output artifact gets a file-ledger row with the task
class, reason and retention; Markdown outputs go through `markdown_route` so a
one-off result does not become a new root file.

Acceptance: a task cannot read outside its scope, exceed its token/model/time
budget, exceed its resource budget, call an undeclared tool, run in the wrong
worktree/branch, start with unresolved drift or when admission refuses, or
return an output that fails its schema. A killed step returns a `resource_event`
to the task record. Every declared artifact has a ledger row and retention
policy. The same inputs, model version and seed produce the same step sequence
and artifact hashes (model output may differ; it is recorded, not asserted).

### 11. Task class library

Start with classes that are useful and safe:

| Domain | Task class | Output |
| --- | --- | --- |
| Research | `paper_triage` | screening recommendation + reasons |
| Research | `claim_extract` | claims with locators and uncertainty |
| Research | `reference_screen` | include/exclude + stage + reason |
| Research | `evidence_table` | rows with source locators |
| Research | `theory_compare` | compatibility matrix + assumptions |
| Research | `manuscript_check` | unresolved citations, claims, checks |
| Coding | `diff_review` | review packet |
| Coding | `test_triage` | failing test → cause → covering tests |
| Coding | `impact_analysis` | changed keys → dependents → tests |
| Coding | `repo_scan` | opportunities with expected effect/effort |
| Ops | `log_inspect` | failure clusters + proposed fixes |
| Ops | `model_classify` | model choice + confidence + reason |
| Ops | `config_validate` | invalid keys, conflicts, suggested values |

Each class is versioned data with a prompt reference and an output schema. The
library is browsable and runnable from the UI; a user can copy a class into the
repo config and edit it.

### 12. Model registry and selection

First ship a registry and explicit defaults, not an autonomous router:

- `ozagia models --json` lists provider/model, context, cost, capabilities,
  local/remote, status and whether the provider is configured.
- `models.toml` sets `default`, `by_task_class`, `fallback`, `local_only` and
  `max_cost_usd`.
- A task class may pin a model or accept the default.
- The classifier is itself a bounded task (`model_classify`): features are
  task class, input size, language, graph size, required tools and privacy
  level; output is a model id, confidence and reason.
- Auto-routing is enabled only after an offline evaluation shows it beats the
  default on success rate and cost. Until then the classifier is advisory.

## Wave 4: leverage and expansion

### 13. Inspect agent logs

A bounded `log_inspect` task over local, opt-in sources: OpenCode logs, daemon
journal, eval results, git state and task records. It redacts prompts, file
contents, tokens and keys before analysis. It clusters failures by tool,
intent, harness, model and task class; it reports evidence and a proposed
change (config patch, new task class, missing capture rule, bug report).

Default is local and read-only. A proposal is a diff or a report, never an
automatic edit. Upstream submission is a separate, explicit bundle with a
redaction preview and consent.

### 14. Process and connection management

Read-only status comes first. Actions are added per resource with preview,
audit and a clear owner:

- runtime services/processes: start, run, stop, restart, logs, using the
  landed `ServiceManager`; never signal a PID whose start token or process
  group cannot be verified;
- runtime tunnels: start, stop, status, with the existing
  `--allow-live`/`--allow-network` gates; a lease not held by this process is
  stale, not stoppable;
- daemon: start, ensure, stop, restart, set idle timeout;
- integrations: install, uninstall, verify, show the trust step;
- providers/MCP: test, enable/disable, set default, show auth state;
- resources: show per-job/session usage and limits, explain a refusal, adjust
  a limit within policy, clear a stale resource event;
- files: explain a created file, list orphans, clean up to trash and restore;
  dry-run by default, one confirmation per batch;
- jobs/tasks: cancel, retry with a different resource profile, open log,
  export evidence;
- index: rebuild, refresh, verify.

Do not add a parallel process manager or tunnel manager. The UI calls the
existing managers through a bounded, audited command layer. Destructive actions
require confirmation and never run from a passive status refresh.

### 15. Scan repo and build modules

Two stages, deliberately separated:

1. **Advisory scan.** Use the graph, tests, history, architecture summary and
   eval results to rank opportunities (missing tests, hotspot complexity,
   duplicate logic, slow paths, weak boundaries, missing adapters, retrieval
   gaps, orphaned files, duplicate Markdown, stale `history/`). Output:
   expected effect, effort, risk, evidence, proposed module.
2. **Module build.** Only after a person selects an opportunity. The module is
   scaffolded behind a preview, with tests, lint, review and a rollback path.
   It lands as a separate package or a repo-config module, never as an
   unreviewed edit to ozagia core.

The scan is a bounded task. The build is a bounded task with a write scope and
a review gate. "Submit upstream" is a separate export, not a side effect.

### 16. Ocelot distribution

Do not silently download code on first run. Preferred order:

1. `OZAGIA_OCELOT` or an explicit `ocelot_root`;
2. a sibling checkout (development);
3. `ozagia ocelot install --version <pinned>` into a user tools directory,
   with a checksum, platform check and offline mode;
4. otherwise report "not installed" and use exact reconciliation.

`ozagia doctor` reports the resolved Ocelot path, version, mode and licence.
The installer may be offered as a default *suggestion* in the UI, but the
network call is explicit.

### 17. Ocelot enhancements

After the review harness and scan exist, use Ocelot's structural machinery for:

- cross-model code-review consensus (same issue family + target);
- duplicate finding/issue clustering across reviewers and repos;
- architecture pattern and anti-pattern analogies;
- task-class similarity for model routing;
- retrieval reranking over graph shapes;
- test-impact analogies (similar change shapes → similar test sets).

Every enhancement must state what it does not establish. Structural analogy is
a candidate, not a truth verdict.

### 18. Standalone console

Only after the add-in and static export prove the views. It is a thin local
shell over the same API: graph, evidence, status, task launcher and settings.
"Open in OpenCode" is the escape hatch for open-ended work. Do not rebuild
chat, tool execution or model management.

### 19. Upstream field improvements

A consent-based export of a diagnostic bundle: redacted logs, config diff,
task class and observed failure, with a reproducible fixture where possible.
It creates an issue or PR draft; it never pushes automatically.

## Guardrails

- The core stays standard library only; UI and visualization extras are
  optional.
- No silent network: Ocelot, research providers and upstream submission are
  explicit.
- No automatic merge, push or upstream write.
- No agent interprets git state when a deterministic check can answer it.
- No force-push, history rewrite or branch deletion without explicit
  confirmation; protected branches are never force-pushed.
- A wrong worktree, a detached HEAD or unresolved drift blocks review and
  bounded tasks rather than being worked around.
- Resource enforcement only signals processes ozagia started, after verifying
  the PID start token and process group; unrelated processes are never
  signalled.
- Keep an OS memory/disk reserve. If the host is already below it, refuse new
  work rather than starting something likely to fail.
- Delay is explicit and cancellable; never wait silently for resources.
- Do not claim exact per-process disk usage when writes cannot be attributed;
  report the filesystem evidence and the limit that was crossed.
- Never delete a file without confirmation; cleanup moves to `.ozagia/trash/`.
  Referenced files and files with `reason: unknown` are never auto-deleted.
- One-off Markdown goes to `history/YYYY-MM-DD/`, not the repository root or
  an unrelated `docs/` page.
- The file ledger stores metadata and hashes, not raw prompts or file content.
- No truth claim from agreement, citation, analogy or a model's confidence.
- No versioned config inside the state directory; no case-colliding paths.
- Every view reports freshness, provenance, omissions and truncation.
- Every bounded task reports budget use, gates, evidence and what it did not
  assess.

## First slice

The first deliverable is Wave 1, items 1-4 (view, settings skeleton, worktree
and resource observation/admission), plus the read-only file-ledger/Markdown
router from item 5 and the status composition from item 6. It is split so the
active worktrees are not disturbed:

**1a. Start now (new files, no in-flight-file edits):**

1. `src/ozagia/core/apertures.py`: aperture schema, validation, projection.
2. `src/ozagia/core/view.py` or equivalent: deterministic view model, caps,
   provenance, renderer hints.
3. `src/ozagia/view_html.py`: self-contained HTML renderer for the first five
   apertures.
4. `src/ozagia/view_cli.py` and an `ozagia-view` console script, so `cli.py`
   is untouched until the in-flight branches merge.
5. `src/ozagia/core/worktree.py`: discovery, expected-branch mapping, disk
   mount/tmpfs checks, drift and commit/merge/push policy checks using git
   plumbing only.
6. `src/ozagia/worktree_cli.py` and an `ozagia-worktree` console script.
7. `src/ozagia/core/resources.py` and `src/ozagia/resources_cli.py` plus an
   `ozagia-resources` console script: host memory/disk/load sampling, mount
   and tmpfs detection, history lookup and admission checks. **Read-only in
   1a: it reports and refuses, it does not kill.**
8. `src/ozagia/core/files.py` and `src/ozagia/files_cli.py` plus an
   `ozagia-files` console script: session manifest scan, ledger records,
   `markdown_route`, `list`/`explain`/`orphans` and cleanup **dry-run**.
   Read-only in 1a: no edit/hook integration and no apply/trash yet.
9. `src/ozagia/core/status.py` and `src/ozagia/status_cli.py` plus an
   `ozagia-status` console script: composed status snapshot over
   `core.runtime.runtime_status`, worktree checks, resource sampling, file
   ledger, daemon `stats`, `doctor` and integration probes.
10. Tests, `docs/view.md`, `docs/worktree.md`, `docs/resources.md`,
    `docs/files.md` and `docs/status.md`.

**1b. After the four usage-plan branches merge (at minimum `feat/edit-lint`
and `feat/paths-owners`, which touch config and CLI):**

11. Settings registry skeleton with `ozagia config schema|get|set --dry-run`,
    integrated into `config.py` and `cli.py`.
12. Wire `ozagia view`, `ozagia worktree`, `ozagia resources`, `ozagia files`
    and `ozagia status` into the main CLI.
13. `core/resources.py` enforcement: cgroup/rlimit/watchdog limits, own OOM,
    `resource_event` feedback and history updates. This needs the merged
    `daemon/server.py` and `daemon/jobs.py`.
14. File-ledger integration into edit/write/patch/hooks, plus cleanup
    apply/trash/restore. This needs the merged `intent/edit.py` and hooks.
15. `docs/settings.md`, the enforcement section of `docs/resources.md` and the
    apply/trash section of `docs/files.md`.

Acceptance: a fresh clone can run `ozagia-view` to produce an HTML graph of
its architecture, `ozagia-worktree check` to report the four active worktrees
(including their dirty counts and zero commit drift), `ozagia-resources status`
to report host memory/disk/load and detect a tmpfs `/tmp`,
`ozagia-resources check --for worktree ...` to refuse a path with insufficient
free space — without killing anything, and `ozagia-files list` /
`ozagia-files explain` to show created files with their reason and suggested
`history/` route. `ozagia-status` shows workspace, resources, file litter and
runtime state. All of this works with no server, no network and no frontend
build step while the four usage-plan worktrees are unmerged.

## Implementation status (2026-11-16)

The read-only part of 1a landed as new files:

- `core/worktree.py`, `worktree_cli.py`, `ozagia-worktree`: discovery,
  expected branch/pattern, drift, conflicts, in-progress operations, filesystem
  facts and commit/merge/push policy checks. `check` warns on a detached HEAD;
  policy refuses it.
- `core/resources.py`, `resources_cli.py`, `ozagia-resources`: host memory,
  disk/mount/tmpfs, load/pressure, process groups, bounded history, estimates
  and admission checks. It reports and refuses; it does not kill.
- `core/files.py`, `files_cli.py`, `ozagia-files`: ledger scan, explain,
  orphans, dry-run cleanup, Markdown routing and front-matter classification;
  integration now records handled writes/creates, bounded `after` scans and
  session start/end scans, with `files_recorded`, `file_ledger_scans` and
  `file_ledger_errors` counters (`feat/file-ledger`, merged as `01807a9`).
- `core/status.py`, `status_cli.py`: composed read-only workspace, resources,
  files, runtime, daemon, doctor and config status (`feat/status-snapshot`,
  merged as `96a6a22`).
- `core/apertures.py`, `core/view.py`, `view_html.py`, `view_cli.py`: data
  apertures, deterministic view models, self-contained HTML and a read-only
  CLI (`feat/view-apertures`, merged as `e139994`).
- `core/settings.py`, `settings_cli.py`: typed registry, user/repo/env
  precedence, validation, atomic preview-first writes and the versioned
  `ozagia/config.toml` (`feat/settings-registry`, merged as `11b33ef`).
- `tests/test_worktree.py`, `tests/test_resources.py`, `tests/test_files.py`,
  `tests/test_status.py`, `tests/test_view.py`, `tests/test_settings.py`;
  `docs/worktree.md`, `docs/resources.md`, `docs/files.md`, `docs/status.md`,
  `docs/view.md`, `docs/settings.md`; console scripts for view, status,
  settings, worktree, resources and files in `pyproject.toml`.

Checks after all four merges: `3343 passed, 21 skipped`;
`ruff check src tests` passes. A concurrent TUI-brand merge added
`.opencode/plugins/ozagia/tui.js`; the harness exclude patterns and tests were
updated for it, and the full suite is green.

Not yet: resource enforcement/OOM, cleanup apply/trash, command-derived path
extraction for `patch`/`shell`, a true before/after session manifest (the
current scan attributes pre-existing untracked files to the session with
`reason: unknown`), `ozagia config` CLI/`config.py` integration, migration from
`.ozagia.toml`, doctor source reporting, and the UI hosts.

### Parallel workstreams (2026-11-16)

Four agents are running from base `865b0ed`, each in its own worktree:

| Worktree | Branch | Scope |
| --- | --- | --- |
| `~/ozmadev/ozagia-wt/view-apertures` | `feat/view-apertures` | Aperture schema, view model, static HTML, `view_cli` |
| `~/ozmadev/ozagia-wt/settings-registry` | `feat/settings-registry` | Typed settings registry, `ozagia/` config, `settings_cli` |
| `~/ozmadev/ozagia-wt/status-snapshot` | `feat/status-snapshot` | Read-only composed status, `status_cli` |
| `~/ozmadev/ozagia-wt/file-ledger` | `feat/file-ledger` | Ledger capture in edits/hooks/sessions |

Status: all four are merged: `status-snapshot` as `96a6a22`, `view-apertures`
as `e139994`, `file-ledger` as `01807a9`, and `settings-registry` as `11b33ef`.
The supervisor reviewed each branch, ran targeted tests and ruff, merged one
branch at a time, then ran the full suite from the main checkout.

## Open decisions

1. **Config directory (resolved).** `ozagia/` is versioned project config;
   `.ozagia/` remains local state. No case-insensitivity guard is needed.
2. **OpenCode host.** Confirm whether the target is OpenCode Desktop, the
   browser web UI, or both, and which GUI API version is acceptable.
3. **First renderers.** SVG force/layered first, or a vendored graph library?
   The former keeps the core dependency-free; the latter is faster to polish.
4. **Worktree policy scope.** Check-only plus preview actions in the first
   release, or guarded commit/merge/push actions too? Recommendation:
   check-only first; add guarded mutations after the checks are trusted.
5. **Expected worktree mapping.** Explicit `ozagia/worktrees.toml` or a
   directory pattern (`ozagia-wt/<name>` -> `feat/<name>`)? Recommendation:
   support both, with the explicit mapping taking precedence.
6. **Resource enforcement default.** Observe and refuse everywhere; enforce
   limits for jobs/tasks by default, with cgroup v2 when delegated and
   rlimit/watchdog otherwise? Recommendation: yes, with limits opt-out and a
   visible platform capability report.
7. **Resource history retention.** Bounded per-key samples with hashed command
   signatures and no raw argv. Recommendation: keep p50/p90/max and exit
   codes, cap the store, and never persist secret-bearing arguments.
8. **History directory.** `history/` at the repository root, versioned, with
   `history/YYYY-MM-DD/` and a generated `index.md`; true scratch stays in
   `.ozagia/scratch/`. Confirm this is the preferred layout.
9. **Cleanup default.** Dry-run plus `.ozagia/trash/`, one confirmation per
   batch, and no automatic deletion. Recommendation: yes; deletion of a
   referenced or `unknown` file is never automatic.
10. **Scan write policy.** Advisory only for the first release, or module
    scaffolding with a review gate?
11. **Ocelot install default.** Suggest-on-first-use versus an explicit
    `ozagia ocelot install`; this plan recommends explicit install.
