# Self-graph execution plan — 2026-11-17

Execution companion to [the self-graph plan](2026-11-17_self_graph_service_unification_model_api.md).
That document is the design authority; this one maps its milestones to files, tests,
surfaces, agent work packages and merge order. Nothing here claims a milestone is
implemented.

## Baseline at preparation time

- `main` at `487793a` ("add self grpah plan"), 2026-11-17 17:59 +1100.
- The checkout is **dirty**: 30 modified files and 13 untracked files (14 with
  this document) from the session-profile / harness / add-in work. Agents must
  not build on top of that working tree. Use a worktree per work package.
- `.venv` is Python 3.13.5.
- Full suite: `4822 passed, 3 failed, 21 skipped` in 8m19s.
  - `test_codex_hooks.py::test_session_brief_outside_git` fails because the dirty
    worktree adds changed-file lines to the session brief.
  - Two `test_codex_integration.py` tests fail against a stale live fixture
    daemon (`job_start`/`by_harness`), not against graph code.
  - Treat this exact failure set as the accepted baseline; a work package that
    changes it must explain why.

## Scope and ordering

Implement the host-side spine first, in the order the plan already fixes:

| Wave | Work package | Depends on | Why now |
| --- | --- | --- | --- |
| 1 | WP0 contracts, IDs, canonical JSON, fixtures (M0) | — | Every later schema and hash needs one authority. |
| 2 | WP1 capability registry + read-only self-graph (M1) | WP0 | First demonstrable "Ozagia knows itself" slice. |
| 2 | WP3 offline profile + timing DAG (M2a start) | WP0 | Pure new modules and fake-clock fixtures; parallel-safe. |
| 3 | WP2 service envelope + task preflight (M2) | WP1 | Joins readiness, freshness, tests, interpreter, resources. |
| 4 | WP4 task profile, report, critical path (M2a) | WP2, WP3 | Needs real run/evidence refs. |
| 5 | WP5 model-side API and map lifecycle (M3) | WP2, WP4 | Projectors need the preflight and profile contracts. |
| 6 | WP6 structured edit → preview → evidence (M4) | WP5 | Uses existing `edit/txn.py`; no new edit path. |
| later | WP7 linked repos, research, Ocelot (M5) | WP2, WP5 | Read-only fixtures can start earlier; actions cannot. |
| later | WP8 evaluation and rollout (M6) | WP6 + approval | Live runs are gated separately. |

M5/M6 are explicitly out of the first execution run. M2a's prefetch planner is
also out of the first run: profile and critical path land first, then prefetch
against a working report.

## M0 decisions to freeze with fixtures

These are the plan's open items. Proposed defaults are what agents should encode
unless a reviewer overrides them before WP0 merges. Each needs a positive and a
negative fixture.

| Decision | Proposed default | Where it lives |
| --- | --- | --- |
| Repository identity | `repo:<owner>/<name>` from the normalized `origin` URL; `checkout:<repo>@<sha256(realpath)[:12]>`; `snapshot:<checkout>@<graph meta hash>`. A move changes the checkout ID, not the repo ID, and is reported as a move, not a match. | `core/contracts.py` |
| Qualified node keys | Keep existing unqualified local keys. Add `project:<repo>@<checkout>:<snapshot>/<key>` only at projection boundaries; never rewrite stored keys. | `core/contracts.py`, docs |
| Canonical JSON | Restricted JSON: `dict[str, ...]`, `list`, `str`, `bool`, `None`, `int` in the signed 64-bit range. Reject floats, NaN, bytes, tuples. Sorted keys, compact separators, UTF-8, `ensure_ascii=False`. SHA-256 over the encoded bytes. | `core/contracts.py` |
| Schema fingerprint | SHA-256 over canonical `{namespace, version, types, required, constraints, sensitivity}`. Instance hash over the canonical instance without `created_at` and transport-only fields. | `core/contracts.py` |
| Compatibility | Version `1` only in this release. Unknown non-`x-` fields are errors. A reader rejects a different major version. Deprecation window: one minor release, recorded in the doc. | `core/contracts.py` |
| Vocabulary | Freshness `current/stale/unknown/absent`; evidence `declared/inferred/observed/verified`; criterion result `declared/pending/passed/failed/unknown/invalid/not_assessed`; readiness per dimension `true/false/unknown`. A fresh child cannot be inferred from a stale parent. | `core/contracts.py` |
| Trace clock | Monotonic deltas for durations, wall clock only for cross-process ordering, explicit `clock: system/monotonic/injected`, unknown when unavailable. Never infer an edge from timestamps alone. | `core/contracts.py`, WP3 |
| Budgets | Map JSON ≤ 64 KiB, ≤ 200 nodes, depth ≤ 2 by default, all caller-raisable only to a hard bound. Prefetch: 4 concurrent, 2 s CPU, 8 MiB IO, cancellable. | WP5, WP4 |
| Registry truth | The explicit built-in catalogue in `core/capabilities.py` is authoritative for declared capabilities. CLI/MCP/daemon/toolchain/status are **observed surfaces**, joined read-only. Unknown coverage is a first-class value. | WP1 |
| Evidence lifetime | Store hashes and redacted refs. A deleted run/worktree becomes `absent` with a reason; never silently dropped or treated as current. | WP2 |
| Live-eval thresholds | Defer to M6; agree numbers before any live run. Suggested starting point: non-inferior task success and ≥10% lower time-to-verified-outcome on ≥20 paired tasks, with the prompted baseline reported. | WP8 |

## Work packages

Each package lands contracts, code, tests and docs together, and leaves existing
clients working. Agent ownership is by file, not by intent: two agents must not
edit the same file in one wave.

### WP0 — contracts and fixtures (M0)

- **Files:** new `src/ozagia/core/contracts.py`, `docs/self_graph_contracts.md`,
  `tests/test_contracts.py`, `tests/fixtures/selfgraph/`.
- **API:** `canonical_json(value)`, `content_hash(value)`, `schema_fingerprint(schema)`,
  `parse_id(text)`, `qualify(...)`, `freshness(...)`, `evidence(...)`,
  `Fact`, `Relation`, `ServiceError` codes, `truncation` envelope.
- **Fixtures:** small project, indexed sibling, missing dependency, failing test,
  stale graph, missing/untrusted tool. Ozagia's own inventory is a fixture built
  from `_TOOLS`, `build_parser()` and the daemon operations, not a frozen blob.
- **Acceptance:** identical output across runs and `PYTHONHASHSEED` values; floats,
  NaN, duplicate keys after normalization, malformed IDs and wrong major versions
  fail explicitly; no I/O, network, model call or target import.

### WP1 — capability registry and self-graph (M1)

- **Files:** new `src/ozagia/core/capabilities.py`, `src/ozagia/core/self_graph.py`,
  `src/ozagia/capabilities_cli.py`, `tests/test_capabilities.py`,
  `tests/test_self_graph.py`, `docs/capabilities.md`; additive edits to
  `src/ozagia/cli.py`, `src/ozagia/integrations/mcp/server.py`,
  `src/ozagia/daemon/protocol.py` (an `OPERATIONS` tuple), `docs/memory_mcp.md`.
- **Catalogue:** capabilities by domain (`graph`, `edit`, `test`, `goals`,
  `tasks`, `environment`, `resources`, `toolchain`, `status`, `federation`,
  `research`, `runtime`, `actions`, `eval`), each with purpose, owner module,
  side-effect class, exposure, implementation ref, prerequisites, limits,
  authorization, tests and docs.
- **Adapters:** MCP tool list and handler map; CLI parser walk; daemon operations;
  toolchain registry/readiness; `core/status.py` sections; dynamic adapter tools
  marked `origin: adapter`, `coverage: unknown` unless declared.
- **Audit:** every declared capability → implementation → surface → dependency →
  test/doc edge has a state. Detect (a) a declared handler missing from the
  runtime map and (b) a runtime handler with no declared mapping.
- **Surfaces:** `ozagia capabilities list|show|audit [--json] [--limit] [--cursor]`;
  MCP `dev_capabilities` (read-only, bounded); no daemon op in this package.
- **Acceptance:** locate a capability and its surfaces on the Ozagia fixture;
  both audit defects are reported with provenance; partial/error states survive;
  no snapshot rewrite, no auto-install, no probe that executes target code.

### WP2 — service envelope, task preflight and evidence (M2)

- **Files:** new `src/ozagia/core/services.py`, `src/ozagia/core/task_preflight.py`,
  `src/ozagia/core/evidence.py`, `src/ozagia/task_preflight_cli.py` (or a
  `preflight task` action), `tests/test_services.py`, `tests/test_task_preflight.py`,
  `tests/test_evidence.py`, `docs/task_preflight.md`, `docs/services.md`;
  additive edits to the task/goal CLI docs if a surface is exposed there.
- **Envelope:** `{schema, service, version, status, data, error, provenance,
  side_effect, started_at, duration_ms, truncated}`; typed errors; caller-owned
  timeouts; no retries with side effects.
- **Preflight inputs:** goal + criteria, scope and checkout(s), graph freshness,
  capability readiness (WP1), interpreter candidates (`core/environments.py`),
  resource admission (`core/resources.py`), affected-test plan (`core/testing.py`).
- **Preflight output:** bounded canonical JSON in which every fact carries source,
  observed time, freshness and evidence kind; a plan is never a passed test; a
  missing interpreter or stale graph cannot produce a green assessment.
- **Evidence:** first a read-only collector joining goal criterion results ↔ task
  runs ↔ test records ↔ interpreter/environment evidence. Durable writes come
  second, reusing `locked_store`/atomic writes, with idempotency keys, bounded
  retention and crash/partial-run tests. No prompts, secrets or raw logs.
- **Acceptance:** a fixture traces a failing test through environment selection to
  a goal assessment; stale graph/missing interpreter is `unknown` or `failed`,
  never green; existing CLI/MCP results and edit behavior unchanged.

### WP3 — offline profile and timing DAG (M2a start)

- **Files:** new `src/ozagia/core/task_profile.py`, `src/ozagia/core/critical_path.py`,
  `tests/test_task_profile.py`, `tests/test_critical_path.py`,
  `docs/task_profile.md`.
- **API:** profile assembly over injected records; `critical_path(dag)`; what-if
  schedule over model-call estimates; explicit unknown duration/edge states;
  cycle and cancellation handling; fake clock.
- **Acceptance:** deterministic branching workload picks the correct critical
  path, never sums parallel spans, predicts a fast-profile and a resource-limited
  run against known schedules, and labels missing queue/token evidence unknown.
- **Parallel note:** write the DAG and report primitives against synthetic
  fixtures now; WP4 wires them to real task/goal/daemon records.

### WP4 — task profile, report and prefetch (M2a remainder)

- **Files:** `src/ozagia/core/task_profile.py`, `src/ozagia/core/prefetch.py`,
  `src/ozagia/tasks_cli.py` (a `report` action), `tests/test_prefetch.py`,
  `docs/task_profile.md`, `docs/prefetch.md`.
- **Acceptance:** the plan's M2a list: regenerable bounded report, failed/partial
  run, unknown criteria, secret canary test, prefetch overlap/no-outcome-change,
  stale/scope-change/cancel/error paths never served as current, unused prefetch
  reported.

### WP5 — model-side API and maps (M3)

- **Files:** new `src/ozagia/model_api/` (`__init__.py`, `schema.py`, `project.py`,
  `cache.py`, `negotiate.py`, `errors.py`), `tests/test_model_api.py`,
  `docs/model_api.md`; read-only MCP access added additively.
- **Acceptance:** schema/content-hash round trips; malformed/oversized/cross-scope/
  stale references fail closed or fall back explicitly; model swap and cache miss
  cannot reuse an unauthorized map; plain and structured clients agree on facts
  and omissions; cache lives under `.ozagia/`; no live model.

### WP6 — structured edit to preview (M4)

- **Files:** new `src/ozagia/model_api/edit.py`, `src/ozagia/model_api/interact.py`,
  `tests/test_model_api_edit.py`, `docs/model_api.md`; no changes to
  `edit/txn.py` semantics.
- **Acceptance:** fake-model proposal → preview → authorized transaction → refresh
  → test result → goal assessment; stale base, declined write, partial failure,
  retry and duplicate-action cases; no text-patch fallback; no command execution.

### WP7/WP8 — linked services/research/Ocelot and evaluation

Not dispatched in the first run. WP7 read-only fixtures may start after WP2.
WP8 requires WP6, agreed thresholds and explicit `--live` approval.

## Agent execution model

- One worktree per work package, created with `tools.opencode.worktree_create`.
  The shared checkout is dirty; never run a writer agent in it.
- An agent owns the files listed for its package. Surface files (`cli.py`, MCP
  `_TOOLS`/handlers, daemon `OPERATIONS`, `docs/memory_mcp.md`) are owned by the
  package that first needs them in a wave; later packages rebase instead of
  editing concurrently.
- Each agent must run, in its worktree:
  `.venv/bin/python -m pytest -q tests/test_<package>.py` plus the direct
  compatibility tests it touches, and `.venv/bin/ruff check src tests`.
- Merges are sequential in wave order. A merge is accepted only with its docs,
  tests and an explicit statement of what remains `unknown`.
- Parallelism is limited to packages with disjoint files: WP1 and WP3 after WP0.
  WP2, WP4, WP5 and WP6 run after their dependency merges.
- Do not commit the pre-existing session-profile work. If a rebase needs it, stop
  and ask.

## Stop conditions

An agent stops and reports rather than working around any of these:

- the package needs a target-project import, network call, model call, install,
  daemon start or a write outside the state directory;
- an existing store (`goals`, `tasks`, sessions, graph keys) would need a schema
  change before a read-only projection exists;
- canonicalization/hash vectors or ID rules are ambiguous;
- an existing client, MCP tool list or edit semantic would change;
- a test needs a live service, a real cloud account or a live model;
- the change would place secret values, prompts or raw logs in a map or trace.

## Verification for the first run

```sh
# per package
.venv/bin/python -m pytest -q tests/test_contracts.py tests/test_capabilities.py \
  tests/test_self_graph.py
.venv/bin/ruff check src tests

# integration acceptance
.venv/bin/python -m ozagia capabilities list --json
.venv/bin/python -m ozagia capabilities audit --json
.venv/bin/python -m ozagia capabilities audit --json > /tmp/a.json
.venv/bin/python -m ozagia capabilities audit --json > /tmp/b.json
cmp /tmp/a.json /tmp/b.json

# full suite, compare against the baseline failure set
.venv/bin/python -m pytest -q
```

## Ready-to-dispatch briefs

**Wave 1 / WP0.** Mission: freeze the v1 contract layer with fixtures, no
production surface. Read the self-graph plan's "Core contracts" and this file's
M0 table. Do not edit existing modules. Deliver contracts module, tests, fixture
tree and doc; report the exact canonical-hash vectors and every decision encoded.

**Wave 2 / WP1.** Mission: make Ozagia's declared capabilities inspectable and
auditable read-only. Build the explicit catalogue first, then join observed CLI,
MCP, daemon and toolchain surfaces; report coverage gaps instead of guessing.
Add `ozagia capabilities` and MCP `dev_capabilities`; no mutation, probe or
daemon start.

**Wave 2 / WP3.** Mission: implement the fake-clock timing DAG, critical path,
what-if schedule and bounded report primitives against synthetic records. Never
infer dependencies from timestamps; missing durations stay unknown. No wiring to
live tasks yet.

**Wave 3 / WP2.** Mission: compose a read-only task preflight from existing
owners and add the read-only evidence collector. Do not run tests, refresh the
graph or write goal/task records. A stale graph or missing interpreter must be
visible and must not yield a green assessment.

**Wave 5 / WP5.** Mission: bounded, versioned, cacheable read-only maps for
`graph`, `outline`, `capabilities` and `work/evidence`, with host policy and
explicit fallback. No live model, no authority transfer, no cache reuse across
scope or model.

**Wave 6 / WP6.** Mission: one structured edit proposal and one context request,
translated into the existing preview/transaction path. No new patch fallback, no
command execution, no model-generated success verdict.
