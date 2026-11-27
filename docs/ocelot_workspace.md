# Ocelot workspace profile

`ozagia.integrations.ocelot.workspace` is an optional adapter for working *on*
an Ocelot checkout. It indexes Ocelot seed metadata, seed dependencies,
boot/registry references, seed-aware tests and docs, and engine-aware Python
files. It adds a conservative test planner, a Rust/Python/seed bridge, a
research/evidence map, preview-first seed editing, a compact agent context and
eleven MCP tools:

- `ocelot_status` — worktree, engine provenance, datasets and failure baseline
- `ocelot_context` — compact session-start doctor + plan + seed/research links
- `ocelot_seed` — one seed with dependencies, dependents, tests and docs
- `ocelot_seeds` — seed census: shared seeds, missing tests, cycles and stubs
- `ocelot_plan` — affected tests or a full-gate fallback for changed paths
- `ocelot_engine` — PyO3 primitive, Rust-change impact, Term census or parity
- `ocelot_research` — prereg → measurement → results → gate chain, or census
- `ocelot_claim_check` — declared burned-seed ledger overlap check
- `ocelot_seed_validate` — read-only structural seed validation
- `ocelot_seed_edit` — preview-first semantic seed editing
- `ocelot_boot_edit` — explicit boot-list/`seed_registry.py` editing

The adapter is read-only except for `ocelot_seed_edit` and
`ocelot_boot_edit` with an explicit `write: true`. It never imports Ocelot,
builds the Rust engine, runs Ocelot tests, calls a model or downloads
anything. It is not bundled into Ocelot; the Ocelot checkout must explicitly
enable it.

## Enable it

Add a project config to the Ocelot checkout:

```toml
[index]
source_dirs = ["."]
test_dirs = ["tests"]
docs = [
  "README.md",
  "CLAUDE.md",
  "docs/CODE_MAP.md",
  "docs/SEED_DEVELOPMENT.md",
  "docs/SCIENCE_RESEARCH.md",
  "docs/features/**/*.md",
  "docs/research/**/*_INDEX.md",
  "docs/research/**/*_PREREG.md",
  "docs/research/**/*_RESULTS.md",
  "docs/research/**/*_AMENDMENT.md",
]

[tests]
select = "affected"
cache = true

[adapters]
modules = ["ozagia.integrations.ocelot.workspace:register"]
```

Then build the graph once:

```bash
ozagia index --root .
```

`seeds/acquired/`, `seeds/generated/`, `data/`, `results/`, `evaluation/`,
`runs/` and `var/` are deliberately not seed-indexed. Only top-level
`seeds/*.json` manifests are development source; generated caches are not.

The indexer records `OcelotSeed` nodes, `OcelotPrimitive` and `OcelotTerm`
nodes, `OcelotResearch` documents, and edges for dependencies, boot/registry
references, seed/test/doc mentions, engine primitives, Term use, research
references and gate tests. It stores bounded metadata and the first few item
names per seed, not every rule body. Reingest re-runs the matching adapter
indexer when a seed manifest, boot file, build record, Rust/Python engine file
or research protocol document changes.

## MCP tools

`ocelot_seed` takes `{"id": "shape_decode_loop"}` and optional
`include: summary|closure|tests|docs|sources|terms|all`. The result includes
the seed's declared dependencies, reverse dependents, closure, blast-radius
class (`local`, `shared`, `core`), boot files, tests, docs and, with
`include: "terms"` or `"all"`, up to 100 referenced Term names.

`ocelot_seeds` is the census. It reports shared seeds, seeds with no test
mention, seeds outside every boot file, dependency cycles, dependency stubs
whose manifest is missing, and partial manifests whose head metadata was
indexed but whose full body exceeded the size cap. The `entries` list is
capped; `seed_count` and the summary lists still cover every indexed seed.

`ocelot_plan` takes either `changed` paths, `base`/`head`, or `seeds` ids and
returns a plan, not an execution:

```json
{
  "mode": "affected",
  "tests": ["tests/test_sd_loop_migrated.py"],
  "commands": ["/repo/.venv/bin/python -m pytest tests/test_sd_loop_migrated.py -q"],
  "engine_rebuild": false,
  "full_gate": false,
  "warnings": []
}
```

A shared/core seed, a test-configuration change, a selection over the cap, or
an unclassified path returns `mode: "full"` and the full migrated command.
Shared boot/loader, boot-profile, world-adapter, seed-registry and
engine-provenance files, plus the engine build/test scripts, also force it.
A `runners/dsl` Rust source change adds the cargo test command and requires an
engine rebuild first, but remains `affected` when the selected tests fit the
cap; `runners/dsl/Cargo.toml` also forces the full gate. The planner never
emits `-n auto`; Ocelot's own protocol caps xdist at `-n 4` because `auto` has
exhausted the host.

`ocelot_status` reads the checkout without importing it:

- git HEAD, branch, dirty paths and registered worktrees;
- the native `substrate_rs/_native.abi3.so`, its realpath and symlink target;
- the `_native.build.json` build record and a content fingerprint of the
  current `runners/dsl/src/**/*.rs` plus `runners/dsl/Cargo.toml`;
- stale Rust files and engine-hash mismatch;
- headless worktree packages and `.pth` targets;
- `data/` and `results/failure_baseline.json` presence.

It reports `ready`, `warn` or `blocked` with next commands. `blocked` means do
not trust a measurement until the engine is rebuilt; `warn` covers missing
datasets/baseline and ambiguous `.pth` state.

## Agent context

`ocelot_context` is the session-start surface. It combines a compact doctor
(worktree HEAD/dirty state, engine artifact and source match, datasets,
baseline), a conservative test plan for the changed paths or seed ids, the
changed seeds' blast radius and direct dependents, and a research chain when a
changed document is indexed. `max_tests` bounds the selected test paths that
are shown (default 20, maximum 200); the plan's full count is still reported.
`next_commands` and `agent_note` make the operational consequence explicit:
rebuild the engine before measuring, use the full migrated gate when required,
and never claim full coverage for an affected slice. The payload never
executes those next commands; the doctor only performs read-only git and file
checks.

In an Ocelot checkout with the adapter registered, the OpenCode daemon appends
the same compact context to the first hint of a session as an
`[ozagia ocelot]` block. It includes the doctor state, plan mode/test count,
changed seed links and the read-only verification instruction, and is capped
at 1,800 characters. The block appears once per OpenCode session; a hint without a session identifier
uses one anonymous daemon-lifetime slot, so it is not repeated on every
request. Non-OpenCode hints and repositories without the adapter are unchanged.
The plugin already caches hints per session, so this is the session-start
context surface.

## Engine bridge

`ocelot_engine` is a syntactic map of the Rust/Python/seed boundary:

- `#[pyfunction]`, `#[pyclass]` and simple `#[pymethods]` become
  `OcelotPrimitive` definitions;
- `enum TermNode` variants and `jsonio.rs` wire names become `OcelotTerm`
  definitions, with term aliases;
- Python `from substrate_rs import X`, `substrate_rs.X` and
  `substrate_rs._native.X` become `File/Test -ocelot_uses_primitive->` edges;
- seed `"type"` values under rules, reflexes and production guards become
  `OcelotSeed -ocelot_uses_term->` edges.

Use `{"op": "primitive", "target": "Substrate"}` for a definition plus Python
and test call sites, `{"op": "rust_impact", "target": "runners/dsl/src/term.rs"}`
for a change blast radius, `{"op": "term_census"}` for defined/used/unknown
and unused Term names, or `{"op": "rs_parity"}` for `domains_rs/*.py` modules
paired with their `domains/*.py` counterpart, tests and missing-Python status.
The map is syntactic: it does not infer dynamic calls or types, and a
`pymethod` is qualified by the Python class name.

## Seed validation

`ocelot_seed_validate` takes `{"id": "seed_id"}` and checks the manifest
without running Ocelot's loader: filename/id, version, dependencies and
cycles, `local_id` uniqueness, edge reference forms, production coverage,
rules and reflexes. Legacy declarative rules and absent legacy metadata are
warnings, not failures. Ocelot's loader remains authoritative.

## Seed editing

`ocelot_seed_edit` takes `{"id": "...", "operations": [...]}` and previews by
default; `write: true` applies the edit. Operations are semantic, not JSON
text:

- `set_metadata` for `version`, `description`, `self_load_only`, `area`,
  `order`, `latency_class`, `capability_keys` and `depends_on`;
- `append` a `rule`, `node`, `edge`, `production`, `reflex` or `concept`;
- `replace` / `delete` by `index`, or by `name` for rules, concepts, nodes and
  reflexes;
- `add_node` / `add_edge`, which allocate the smallest free positive
  `local_id` when it is absent, `null` or `0`.

Edits go through the span-preserving JSON adapter, so the rest of the manifest
keeps its key order and formatting. Preview returns the diff, base/new hashes,
the translated path operations, and prospective validation. Apply writes
atomically with a backup, re-validates the written manifest, and restores the
backup if validation reports errors. The result also previews a minimal
`tests/test_<id>_migrated.py` skeleton (`test_skeleton` in JSON), including an
"already exists" warning; it is never written. Unknown operations or metadata
fields, ambiguous name selectors, duplicate names, escaping paths, symlinked
manifests and `seeds/generated/` or `seeds/acquired/` are refused. Boot lists
and `domains/seed_registry.py` are deliberately not edited by this tool.

Boot lists and the seed registry have their own explicit editor,
`ocelot_boot_edit`, because they are boot-path changes rather than manifest
content:

- `add_boot` / `remove_boot` for one seed in `CORE_SEEDS` or
  `OPTIONAL_CORE_SEEDS` (`substrate/boot_core.py`) and
  `CONVERSATION_SEED_ANCHORS` or `CONVERSATION_SELF_IMPROVEMENT_ANCHORS`
  (`substrate/boot_profiles.py`);
- `add_registry` / `remove_registry` for one
  `REGISTRY.register(Seed(...))` entry in `domains/seed_registry.py`; the
  manifest's `id`, `version` and `depends_on` must agree, and the standard
  `_installer_from_manifest(<id>)` installer is used.

The editor parses with `ast` and splices only the target tuple/frozenset
element or registry statement, so comments, unrelated formatting and later
union assignments stay byte-identical. It is preview-first, accepts a single
base hash or a `{path: hash}` mapping, and on `write: true` re-parses every
written file and restores all backups if any check fails. Unknown files, names
or operations, duplicate adds, missing removals, symlinked paths and
`seeds/generated|acquired`-only ids are refused. These are still separate,
explicit operations: a seed edit never changes a boot list on its own.

## Research map

`ocelot_research` takes a document path, a block number such as `371000`, or a
topic substring, and returns the prereg/amendment/results/index chain plus
linked scripts, tests, seeds, engine hashes, commit pins, missing gate tests
and stale-engine flags. With no target it returns a census of missing
prereg/result links and gates. Only `*_PREREG.md`, `*_AMENDMENT.md`,
`*_RESULTS.md` and `INDEX.md` under `docs/research` are indexed; the rest of
that tree is deliberately left out. A recorded old engine hash is reported as
stale; the tool never claims a scientific result is true.

`ocelot_claim_check` takes `{"lo": 371000, "hi": 371099}` and reports overlap
with the declared ranges in `VISION_PROTOCOL.md`'s burned-seed ledger. It reads
the table only: Ocelot's `scripts/seed_claim.py` remains the measured-draw
authority, and a clean result is not proof that a range is free.

## CLI

```bash
ozagia-ocelot-workspace status --root .
ozagia-ocelot-workspace doctor --root . --json
ozagia-ocelot-workspace seed shape_decode_loop --root . --include all
ozagia-ocelot-workspace seeds --root . --json
ozagia-ocelot-workspace plan --root . --changed seeds/sd_loop.json
ozagia-ocelot-workspace plan --root . --base origin/main --head HEAD
ozagia-ocelot-workspace plan --root . --seed shared_seed --json
ozagia-ocelot-workspace engine --root . --op term_census --json
ozagia-ocelot-workspace engine --root . --op primitive --target Substrate
ozagia-ocelot-workspace engine --root . --op rs_parity --json
ozagia-ocelot-workspace research --root . 371000
ozagia-ocelot-workspace claim --root . 371000 371099
ozagia-ocelot-workspace validate --root . shape_decode_loop
ozagia-ocelot-workspace context --root . --changed seeds/sd_loop.json --max-tests 50
ozagia-ocelot-workspace edit --root . my_seed --ops operations.json
ozagia-ocelot-workspace edit --root . my_seed --ops operations.json --write
ozagia-ocelot-workspace boot-edit --root . --ops boot-operations.json
ozagia-ocelot-workspace boot-edit --root . --ops boot-operations.json --write
```

`doctor` is an alias for `status`. `status`/`doctor`, `edit` and `boot-edit`
do not need the graph; the other commands require `ozagia index --root .`. The
CLI reports the missing snapshot and the exact command rather than building it
implicitly. `edit` and `boot-edit` also accept `--base-sha256` to refuse an
unexpected base file, and every command accepts `--json`.

The planner evaluation is a separate offline command; it loads the existing
snapshot and never executes pytest:

```bash
python -m ozagia.integrations.ocelot.workspace_eval \
  --root /path/to/ocelot \
  --cases /path/to/ozagia/docs/results/ocelot_workspace_eval_cases_2026-11-26.json \
  --out /tmp/ocelot-eval.md
```

Run it from any working directory: `--root` is the Ocelot checkout and
`--cases` is the case file in the Ozagia repository, so both are explicit
paths rather than paths relative to the Ocelot checkout. It exits 0 when the
evaluation passes, 1 when the report says `ok: false`, and 2 for invalid
input or I/O errors.

The 2026-11-26 offline run used the snapshot built from Ocelot `24c56891`
(`/home/matt/ozma/ocelot`; the audit records the host-qualified path
`matt@192.168.40.19:~/ozma/ocelot`) and replayed eight historical commits. It
covered every test file each commit touched (directly for affected plans, via
the full gate for engine commits), required the full gate for both engine
commits, chose no tests for the two documentation-only commits, and raised no
mode mismatch. This is a planner replay, not a live model-task gain. The
replay metric is commit-touched test coverage plus full-gate routing, not
independent recall, and it does not establish a general recall benefit. See
[`docs/results/ocelot_workspace_eval_2026-11-26.md`](results/ocelot_workspace_eval_2026-11-26.md).

## Limits

- Seed item data is bounded to names and counts. Full rule bodies stay in the
  manifest; read the file when editing. A manifest over the size cap
  (`32 MiB`) contributes head metadata and is marked `partial` rather than
  being parsed in full on every index.
- Seed mentions use manifest paths, quoted seed ids, and long bare ids. A
  test that only uses an unquoted short id may not be linked.
- Test selection is syntactic and conservative. Shared seeds, boot/seed-loader
  changes and `runners/dsl/Cargo.toml` intentionally expand to the full
  migrated gate; a Rust source change requires the engine rebuild and cargo
  test slice.
- The doctor reads the build record; it does not load the engine. A process
  that imports a different shadowed `.so` is outside this read-only check.
- The engine bridge is syntactic: it maps imports, PyO3 attributes, Rust Term
  variants and seed `type` values; it does not resolve aliases, dynamic calls
  or Python factory functions that have no Rust definition.
- Research links and staleness compare text/path references and recorded
  engine hashes; commit pins are reported but not checked against Git.
- Seed validation is a structural check, not Ocelot's loader. Legacy
  declarative manifests are accepted with warnings. Ocelot's
  `domains.engine_provenance.py`, `VISION_PROTOCOL.md` and `CLAUDE.md` remain
  authoritative. This profile points at them; it does not replace them.
- Seed editing validates before preview and after apply, but a structurally
  valid manifest can still fail Ocelot's loader or a runtime gate. The editor
  does not run Ocelot, allocate a burned seed range or write tests. Boot
  registries are changed only by the separate, explicit `ocelot_boot_edit`.
- Boot editing is syntactic and structural: it re-parses the Python file but
  does not import Ocelot or execute the registry. A union assignment later in
  `boot_profiles.py` can add a seed that a removal in the base literal did not
  remove; the preview shows the exact edit and the file's own tests remain the
  authority.
- A real-checkout apply of `ocelot_seed_edit` or `ocelot_boot_edit` is an
  explicit person-approved action. `write: true` (or `--write`) is the
  caller's request, not standing approval. There is no separate runtime
  approval token; obtain a person's approval before applying to a real
  checkout. Tests exercise preview and apply/rollback paths only on synthetic
  fixtures; the real-checkout probes recorded here were preview-only.
- The burned-seed check reads the declared ledger, not actual RNG draws. A
  clean result can still miss an unrecorded or differently-consumed range.
- The planner evaluation is offline and historical. It compares the selected
  files with the test files the commit touched; it does not execute the tests
  or prove that the slice would have caught the defect. It is not a live
  agent-workflow/model-task evaluation; that run was not performed and remains
  approval-gated.
