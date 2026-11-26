# Working on Ocelot: an Ocelot-specific Ozagia profile

Status: first slice implemented 2026-11-26; later phases proposed. The
implemented slice is a reusable optional integration
(`ozagia.integrations.ocelot.workspace`) plus the engine doctor, MCP tools and
CLI. It is not in Ozagia's core and does nothing unless an Ocelot project
config enables it. The Rust/Python/seed bridge, seed editing/validation,
research map and agent workflow integration remain proposed.

This plan is for features that make Ozagia useful **while working on
Ocelot**, not for Ocelot's runtime. Ocelot remains a separate MIT-licensed
checkout. Ocelot-specific knowledge lives in an adapter and a project config
in the Ocelot tree; Ozagia's core stays generic and never imports Ocelot.

## Audit source

- Ocelot checkout: `matt@192.168.40.19:~/ozma/ocelot`
- `main` at `24c56891`, clean working tree
- Current canonical checkout and several old worktrees on the same host
- No current `.ozagia.toml` and no current `tools/ozagia_ocelot_adapter.py`.
  The adapter fixture in this repository is stale: its IR edit engine imports
  `scripts.ocelot_ir_edit`, which the current tree no longer has.
  `scripts/graph_interrogate.py` still exists, but the current tree has no
  adapter that reaches it.

Measured tree shape at that commit:

| Area | Measured |
| --- | --- |
| `seeds/*.json` | 921 top-level manifests, 186 MB; `seeds/acquired/` absent here, `seeds/generated/` 5 files |
| Tests | 1,464 `test_*.py`; 760 `*_migrated.py`; full `-k migrated` gate is documented at 60–90 min |
| Seed-aware tests | 607 seed ids mentioned by tests; 184 have an exact `tests/test_<id>_migrated.py`; 288 are not mentioned at all |
| Seed fan-out | 339 seeds have dependents; max reverse fan-out 27 (`compiler_theory`), 23 (`thinking_budget`), 22 (`shape_decode_loop`) |
| Engine-aware tests | 887 test files reference `substrate_rs`, engine provenance, or `_native` |
| World-adapter tests | 691 test files reference `world_adapter`, `WorldAdapter`, or `*_world` |
| Rust engine | `runners/dsl/src/`: 84 `.rs` files, about 106k lines |
| Research | 592 `*_PREREG.md`, 722 `*_RESULTS.md`, 4,679 files under `docs/research/` |
| Scripts | 4,115 files under `scripts/`, 67 under `tools/` |

The important properties are not just size:

- Ocelot's tests are **sliced by convention**, not by package boundaries.
  `test_<capability>_migrated.py` is the main gate, but 288 seeds have no test
  mention and shared seeds fan out to many capabilities.
- The engine is a **PyO3 extension with a build record**, and the same
  `.so` may be shared by a worktree only while the Rust source is
  byte-identical. A wrong or stale engine silently produces plausible
  numbers. Ocelot already has `domains/engine_provenance.py`,
  `scripts/engine_build.sh` and a provenance gate.
- Ocelot's work is **seed/Rust/Python/research shaped**. A generic Python
  import graph sees only part of the picture; the seed manifest and the Rust
  engine are first-class inputs.

## Problems to solve

1. **Seed impact is invisible.** A changed seed can affect its own test,
   dependent seeds, boot profiles, world adapters, scripts and research
   gates. A path-based affected test run cannot see `depends_on` or
   `mentions_seed`.
2. **Test selection is manual and expensive.** The documented everyday rule
   is "touched seed's own migrated test plus anything importing the
   seeds/modules you touched". That is exactly a graph query, but it is
   currently done by memory and grep.
3. **Engine provenance is easy to get wrong.** Fresh worktrees lack
   `_native.abi3.so`; a `.pth` may point at the canonical tree; a plain `cp`
   can write through a symlink; missing `data/` turns tests green or skipped
   for the wrong reason.
4. **Rust-to-Python impact is missing.** A Rust `Term`/PyO3 primitive can be
   used by Python wrappers, world adapters, seed JSON and tests. Ozagia has a
   Rust graph and a Python graph, but no deliberate cross-language bridge.
5. **Shared seeds are a blast-radius problem.** `shape_decode_loop`,
   `compiler_theory` and `thinking_budget` have wide reverse dependency
   fan-out. A local seed edit can require the full gate, and nothing says so
   before the run.
6. **Research artifacts are not linked to code.** Preregistrations,
   measurement scripts, result JSON/Markdown and gate tests are separate
   files; the engine hash and commit pin are free text. A stale result or a
   missing gate is hard to spot mechanically.
7. **Seed authoring is error-prone.** Manifests have `local_id`, cross-seed
   references, `productions` for new edge types, rule Term shapes, registry
   and boot lists. A format-preserving, validating edit path would remove a
   class of hand-editing errors.

## Design rules

1. **Ocelot knowledge stays in Ocelot.** The adapter lives in the Ocelot
   checkout (`tools/ozagia_ocelot_adapter.py`) and is loaded through
   `[adapters] modules`. Ozagia core must not learn seed IDs, Ocelot paths or
   Ocelot grammar. A copy of the adapter is a realistic fixture in Ozagia's
   tests, not a second implementation.
2. **Read-only by default.** Indexing, planning, provenance and research
   queries never build the engine, run tests, import Ocelot, download
   anything or call a model. A captured `cargo test` or `pytest` happens only
   because the caller typed it.
3. **Preview-first edits.** Seed edits use the existing transaction path:
   base hash, atomic write, backup, no text-patch fallback. A seed scaffold
   is preview-only unless applied explicitly.
4. **Conservative test selection.** Every plan either explains why the
   affected slice is sufficient or names the full-gate fallback. Unknown or
   shared changes force the full gate. No "probably safe".
5. **Cite the engine, not just the tree.** Any result or plan that depends on
   the engine names the native artifact, source verdict (`own`, `shared`,
   `DIVERGED`) and build-record staleness.
6. **Do not index the caches.** `seeds/acquired/`, `data/`, `results/*`
   except enumerated baselines, `evaluation/reports/`, `runs/`, `var/` and
   `target/` stay out of the default graph. The top-level seed manifests are
   enough for development.
7. **Degrade honestly.** Missing engine artifact, missing datasets, missing
   build record, stale graph or an unavailable Ocelot reporter is a named
   state with a next command, never an empty success.

## Proposed features

### A. Ocelot workspace profile and adapter

Deliver an Ocelot-owned `.ozagia.toml` and adapter that declare:

- source roots: `domains`, `worlds`, `substrate`, `tests`, `scripts`,
  `tools`, `runners/dsl/src`; docs: `CLAUDE.md`, `docs/CODE_MAP.md`,
  `docs/SEED_DEVELOPMENT.md`, `docs/SCIENCE_RESEARCH.md`,
  `docs/features/**/*.md`, and the research index/prereg/results files;
- an expected Python (`<root>/.venv/bin/python`, then `python3`) for
  read-only Ocelot reporters;
- `[tests] select = "affected"` for captured pytest, with an Ocelot-aware
  planner on top;
- adapter indexers for seed manifests, engine provenance, research artifacts
  and the Rust/Python bridge.

This is phase 0 because every later feature needs the index and the adapter.

### B. Seed graph and impact

Index top-level `seeds/*.json` as graph data:

- `Seed` nodes: id, version, path, description hash, `depends_on`,
  `self_load_only`, `area`, `order`, `latency_class`, `capability_keys`;
- bounded item nodes: Rule, Concept, Production, Reflex, Microtheory, Frame
  and any other top-level item type, with name and seed;
- `Seed -depends_on-> Seed`, `Seed -contains-> item`,
  `File -defines-> Seed`, `File -mentions_seed-> Seed`,
  `Test -mentions_seed-> Seed`, `Doc -documents-> Seed`;
- boot/registry edges: seed ids in `substrate/boot_core.py`,
  `substrate/boot_all.py`, `substrate/boot_profiles.py` and
  `domains/seed_registry.py` link back to their Seed node;
- full manifest reads on demand, not all item contents in the first index.
  Large manifests (the largest is several MB) are capped, with the cap and
  omitted count reported.

Queries/tools:

- `ocelot_seed(id, include="summary|closure|items|tests|docs")` — one seed
  with its dependency closure and reverse dependents.
- `ocelot_seed_impact(id)` — direct and transitive dependents, boot/registry
  references, tests, docs, scripts and world/domain files; a blast-radius
  label (`local`, `shared`, `core`) derived from fan-out and boot membership.
- `ocelot_seeds` — census: seeds with no test mention, seeds with no
  registered item, seeds not in any boot list, dependency cycles, duplicate
  IDs, stubs.

This is the highest-value phase. It turns the documented "scoped slice" rule
into a deterministic query and makes the 288 unmentioned seeds visible.

### C. Ocelot test plan

Add a planner that accepts changed paths, a git range or seed IDs and returns
a **plan, not an execution**:

| Change | Plan |
| --- | --- |
| one seed manifest | exact `test_<id>_migrated.py`, tests mentioning the seed id/path, and tests of direct dependents; transitive dependents only if the seed's fan-out is small |
| shared seed (`shape_decode_loop`, `compiler_theory`, `thinking_budget`, boot seeds) | full `pytest tests -k migrated`, with the reason |
| `domains/` or `worlds/` Python | existing affected Python tests plus tests that name the module/world and the seeds it loads |
| `tests/` change | the changed test files; a deleted or renamed test is a gate change |
| `substrate/seed_loader.py`, `boot_core.py`, `boot_all.py`, `boot_profiles.py`, `world_adapter.py` | full migrated gate |
| `runners/dsl/src/**.rs` | `scripts/engine_cargo_test.sh` slice, engine provenance gate, all Python tests touching `substrate_rs`, and the affected domain tests; rebuild required first |
| `Cargo.toml` / engine build scripts | engine build, provenance gate, full migrated gate |
| docs/research only | no pytest plan; a research-link check and a note that no test was selected |
| unknown or unindexed path | full gate with the reason |

Output includes the exact commands, file/test counts, the justification for
each file, and the full-gate fallback command. It must never emit `-n auto`;
the documented safe cap is `-n 4`. It should expose an estimated duration
only when it has prior result data for those test files.

The planner should also expose `ocelot_test_plan --explain` showing why a test
was included or excluded, and a `--baseline PATH` option to compare against
`results/failure_baseline.json`.

### D. Engine and worktree doctor

Add `ocelot_status` / `ocelot_doctor` with read-only checks:

- current worktree, branch, HEAD, dirty paths and `git worktree list`;
- which `substrate_rs` package and `_native.abi3.so` the configured
  interpreter actually resolves (realpath), and whether the package is the
  headless worktree package;
- the build record `_native.build.json`, Rust source fingerprint and
  `build_is_stale` verdict, via Ocelot's own `domains.engine_provenance.py`
  reporter when available, otherwise a direct read-only comparison;
- whether the `.pth` target is the canonical tree, this tree, or missing;
- whether `data/` and the dataset paths required by `conftest.py` exist;
- whether `results/failure_baseline.json` is present;
- whether the tree is the canonical checkout while other agents may be
  working, and whether the current branch is detached;
- the next command for every failure (`scripts/engine_build.sh --lib-only`,
  `git worktree add ...`, etc.).

The doctor must refuse to say "ready to measure" when the engine is
`DIVERGED`, the build record is stale, or the engine is a shared build from
another tree with different Rust source. It should reproduce the known
hazards in fixtures: wrong-tree binary, stale build record, missing native
artifact, and `cp`-through-symlink is not attempted anywhere.

### E. Rust/Python/seed bridge

Ocelot's architecture crosses three languages/representations. Add a
syntactic bridge:

- Rust definitions: `#[pyfunction]`, `#[pymethods]`, `#[pyclass]`,
  `#[pymodule]`, `runners/dsl/src/py.rs` handoffs, and exported names in
  `runners/dsl/python/substrate_rs/*.py`;
- Python uses: `substrate_rs` imports and calls in `substrate/`, `domains/`,
  `worlds/`, `tests/`, `scripts/` and `tools/`;
- Seed uses: Term type names and primitive names in seed rules (for example
  `ShapeDecode`, `TransformDecode`, `DimInfer`), plus edge types in
  `productions`.

Queries:

- `ocelot_primitive(name)` — Rust definition, Python call sites, tests,
  seed rules/Term uses and docs.
- `ocelot_rust_impact(path or symbol)` — Python/seed/test blast radius and
  whether an engine rebuild is required.
- `ocelot_term_census` — Term types used by seeds vs variants defined in
  `term.rs`, unknown terms, generated `term_variants.rs` coverage, and
  Python-side Term factories.
- `ocelot_rs_parity` — `domains_rs` modules vs their `domains` counterparts
  and the parity-pin tests.

Cross-language edges are syntactic and labelled as such. No type inference,
no runtime imports in the core path, and a same-named unrelated symbol is not
linked.

### F. Seed-aware editing and validation

Extend the JSON edit adapter for seed manifests with preview-first
operations:

- append/replace/delete a rule, node, edge, production or reflex;
- allocate a free `local_id`;
- set `depends_on`, `version`, `description`, `self_load_only`, `area`,
  `order`, `latency_class` and `capability_keys`;
- update `domains/seed_registry.py` / boot lists only as a separate,
  explicitly requested edit;
- create `tests/test_<id>_migrated.py` from a minimal test skeleton, preview
  only.

Validation before and after:

- filename equals `id`; version is a string; `depends_on` resolves; no
  cycles; `core` is allowed as the built-in boundary;
- unique `local_id` values; `{"local": …}`, `{"role": "agent_root"}` and
  cross-seed `{"type": …, name: …}` references are structurally valid;
- every new edge type has a production; `productions` is not silently empty
  when the seed introduces a new edge;
- rules have the expected `match`/`where`/`effects` shape and known Term
  field names (schema-level; Ocelot's own loader remains authoritative);
- generated caches (`seeds/acquired/`, `seeds/generated/`) are never edited
  by hand.

### G. Research and evidence map

Index the research protocol as a read-only overlay:

- `*_PREREG.md`, `*_AMENDMENT.md`, `*_RESULTS.md`, `docs/research/**/INDEX.md`
  and the cited `results/*.json` / measurement scripts;
- edges: block/topic → prereg → measurement script → result → gate test →
  seed/Rust file;
- `ocelot_research(block|topic|path)` returns the chain, the recorded engine
  hash, commit pin, pinned artifact, gate test, missing links and stale
  results (recorded engine source differs from the current build record);
- `ocelot_claim_check(seed_range)` reads the burned-seed ledger and reports
  conflicts without writing.

This is deliberately narrow: link and report, never decide that a scientific
claim is true. Ocelot's protocol text remains the authority.

### H. Agent workflow integration and evaluation

Once A–D are usable, expose the profile through Ozagia's normal surfaces:

- OpenCode context hook at session start in an Ocelot repo: current
  worktree, engine provenance/doctor state, safe test cap, full-gate warning,
  and the relevant seed/research links;
- a task class/profile `ocelot` with default `affected` tests, engine
  rebuild gate after Rust, and evidence requirements;
- optional add-in status panel for seeds, engine, research, and the current
  test plan.

Evaluate on real work before broadening:

- replay 5–10 historical Ocelot blocks/rungs (e.g. block 371000) offline;
- measure test files selected vs the tests the actual commit touched, time
  saved, missed gate-relevant tests, engine misdetections, and tool-call
  success;
- seeded fault tests for wrong engine, stale build record, missing data and
  shared seed fan-out;
- no live model calls without explicit approval.

## Suggested delivery order

| Phase | Feature | Value | Risk | Depends on |
| --- | --- | --- | --- | --- |
| 0 | Ocelot config + adapter + baseline | Foundation | Low | — |
| 1 | Seed graph and impact | Highest: test selection and blast radius | Medium: 186 MB of manifests, huge seeds | 0 |
| 2 | Test plan | Highest: 60–90 min gate becomes a scoped command | Medium: false confidence if too narrow | 0, 1 |
| 3 | Engine/worktree doctor | High: prevents silently wrong measurements | Low–medium: reporter/venv differences | 0 |
| 4 | Rust/Python/seed bridge | High: Rust changes get Python/seed impact | Medium: syntactic bridge limits | 0, 1 |
| 5 | Seed editing/validation | Medium–high: fewer hand-edit errors | Medium: schema drift | 0, 1 |
| 6 | Research/evidence map | Medium: research workflow is central to Ocelot | Medium: free-text links | 0 |
| 7 | Agent integration + evaluation | Proves the profile helps | Medium: live host/model gates | 1–6 |

Start with 0–3. Do not start with a UI, a seed authoring wizard or a research
dashboard: those are downstream of the graph and test plan being correct.

## First slice — implemented 2026-11-26

Implemented:

- `src/ozagia/integrations/ocelot/workspace.py` — seed indexer, impact/census,
  conservative test planner, four MCP tools and graph queries.
- `src/ozagia/integrations/ocelot/workspace_doctor.py` — read-only engine,
  worktree, `.pth`, dataset and baseline doctor.
- `src/ozagia/integrations/ocelot/workspace_cli.py` — `status`, `seed`,
  `seeds` and `plan` commands.
- `docs/ocelot_workspace.md` — setup, tool surface and limits.
- `tests/test_ocelot_workspace.py`,
  `tests/test_ocelot_workspace_doctor.py`,
  `tests/test_ocelot_workspace_cli.py` — synthetic Ocelot fixture plus
  wrong-engine/stale-source/missing-data cases.

Measured on the canonical Ocelot checkout (`24c56891`) with a limited source
set (`domains`, `worlds`, `substrate`, `tests`, `runners/dsl/src`):

| Measurement | Result |
| --- | --- |
| Index | 37.3 s, 44,212 nodes, 194,685 edges |
| Seed nodes | 924 (920 unique manifests, 4 stubs, 1 partial 68 MB manifest) |
| Shared seeds | 24 |
| Seeds with no test mention | 333 |
| Seeds outside every boot list | 648 |
| Dependency cycles | 0 |
| Plans | `shape_decode_loop` → full gate; `embodied_motor_program` → 20 affected test files; shared `substrate/world_adapter.py` → full gate; docs → no tests |
| Doctor | engine source/hash matched; warned that the user-level `.pth` points at a different tree |

The remaining phases are the Rust/Python/seed bridge, seed editing and
validation, the research/evidence map, and agent integration/evaluation.

## Data and tool surface

Adapter node types (proposal):

| Node | Key/attrs |
| --- | --- |
| `Seed` | seed id; version, path, dependency names, boot membership, fan-out |
| `SeedItem` | seed id, item type, name, local id, bounded attrs |
| `EngineBuild` | artifact path, engine hash, Rust source fingerprint, build record, verdict |
| `RustPrimitive` | Rust path/qualname, PyO3 export, Python wrapper |
| `TermUse` | Term type, seed/rule/source location |
| `ResearchArtifact` | kind (prereg/amendment/result/index/script/result JSON), block/topic, pinned commit/engine |
| `ClaimRecord` | burned RNG range, owner/block, state |

Tool surface (start small, group where possible):

- `ocelot_status` — workspace, worktree, engine, datasets, baseline
- `ocelot_seed` — show / closure / impact / census / validate
- `ocelot_plan` — test plan for paths, ranges or seed IDs, with explanations
- `ocelot_engine` — primitive / term census / Rust impact / rebuild gate
- `ocelot_research` — chain and stale-result check
- `ocelot_edit` — preview-first seed operations (later phase)

Every tool returns citations (`file:line` or a graph key) and a `state`
(`ready`, `not_indexed`, `engine_diverged`, `missing_data`, `stale`) rather
than a bare success.

## Acceptance gates

The profile is not "done" until these pass on a fixture and on the canonical
Ocelot checkout:

1. **Index**: top-level seeds, tests, docs, Rust and Python index in a bounded
   time and size; `seeds/acquired/`, `data/`, `results/` and generated caches
   are excluded and reported as exclusions.
2. **Seed impact**: for a known shared seed (`shape_decode_loop`) and a local
   seed, the direct/transitive dependents, tests and boot references match a
   hand audit of the checkout.
3. **Test plan**: on a sample of historical changes, the plan includes every
   test file the change's own commit touched, or explicitly names the
   full-gate fallback and the reason.
4. **Engine doctor**: seeded fixtures for wrong-tree, stale, divergent,
   headless-package and missing-data states all fail with the correct
   next command; a healthy tree reports `ready`.
5. **Rust/Python bridge**: a sample of PyO3 exports and Term types resolves to
   the expected Python call sites/tests/seeds, with no false same-name edge.
6. **Edit validation**: a valid seed edit round-trips formatting; invalid
   ID/ref/cycle/production cases are refused with a precise error and no
   write.
7. **Research**: block 371000's prereg → script → results → gate chain is
   recoverable; a missing or stale link is reported, not hidden.
8. **No hidden calls**: the full suite proves that status, plan and query
   paths do not run builds, tests, network calls, model calls or target
   imports outside the adapter's explicit read-only reporter.

## Risks and non-goals

- **False confidence from a narrow plan.** The test planner is conservative
  by design; shared/core changes and unknown paths force the full gate. A
  plan that claims completeness without the full gate is a bug.
- **Adapter drift.** Ocelot's schema, boot lists and provenance code move
  quickly. The adapter must report its own version and refuse stale
  assumptions; the fixture tests pin the shapes.
- **Index size.** The full 186 MB seed tree and 50 MB docs are not indexed
  eagerly. Large manifests are bounded; full seed details are read on demand.
- **Not a general Ocelot agent.** This profile does not run Ocelot's engine
  or make research decisions. It gives the coding agent deterministic
  context and gates.
- **Not a replacement for `VISION_PROTOCOL.md` or `CLAUDE.md`.** Those
  remain authoritative. The profile points at them and never restates them as
  if they were code.
- **Not a distribution change.** Ocelot is not bundled, installed or
  downloaded by this plan. The existing explicit, checksummed Ocelot
  installer stays separate.

## Open questions

1. Should the Ocelot adapter call `domains.engine_provenance.py` in the
   Ocelot venv, or reimplement the build-record check in the adapter to avoid
   importing target code? The first is authoritative; the second is simpler
   and keeps the core rule. The plan prefers the reporter, with a direct
   read-only fallback.
2. How much of a large seed manifest should the index carry by default? The
   first slice should index metadata and item names, not every rule body.
3. Should the test planner learn from actual test durations and failure
   history, or stay deterministic and only report counts? Start deterministic;
   use `results/failure_baseline.json` as a separate comparison.
4. Where should the Ocelot config live during development: the Ocelot
   checkout (normal project config) or an Ozagia-side fixture plus
   `$OZAGIA_CONFIG`? The normal project config is the product path; fixtures
   should exercise it without depending on the remote host.
5. Does the OpenCode context hook have enough budget to include this state,
   or should it be a compact `ocelot_status` summary with links to the full
   tools? Start compact.
