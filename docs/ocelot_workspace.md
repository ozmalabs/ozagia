# Ocelot workspace profile

`ozagia.integrations.ocelot.workspace` is an optional adapter for working *on*
an Ocelot checkout. It indexes Ocelot seed metadata, seed dependencies,
boot/registry references, seed-aware tests and docs, and engine-aware Python
files. It adds a conservative test planner, a Rust/Python/seed bridge, a
research/evidence map and seven MCP tools:

- `ocelot_status` — worktree, engine provenance, datasets and failure baseline
- `ocelot_seed` — one seed with dependencies, dependents, tests and docs
- `ocelot_seeds` — seed census: shared seeds, missing tests, cycles and stubs
- `ocelot_plan` — affected tests or a full-gate fallback for changed paths
- `ocelot_engine` — PyO3 primitive, Rust-change impact or Term census
- `ocelot_research` — prereg → measurement → results → gate chain, or census
- `ocelot_seed_validate` — read-only structural seed validation

The adapter is read-only. It never imports Ocelot, builds the Rust engine, runs
Ocelot tests, calls a model or downloads anything. It is not bundled into
Ocelot; the Ocelot checkout must explicitly enable it.

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
`include: summary|closure|tests|docs|sources|all`. The result includes the
seed's declared dependencies, reverse dependents, closure, blast-radius class
(`local`, `shared`, `core`), boot files, tests and docs.

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

A shared/core seed, `substrate/` or `runners/dsl` engine change, test
configuration change, a selection over the cap, or an unclassified path
returns `mode: "full"` and the full migrated command. The planner never emits
`-n auto`; Ocelot's own protocol caps xdist at `-n 4` because `auto` has
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
for a change blast radius, or `{"op": "term_census"}` for defined/used/unknown
and unused Term names. The map is syntactic: it does not infer dynamic calls
or types, and a `pymethod` is qualified by the Python class name.

## Seed validation

`ocelot_seed_validate` takes `{"id": "seed_id"}` and checks the manifest
without running Ocelot's loader: filename/id, version, dependencies and
cycles, `local_id` uniqueness, edge reference forms, production coverage,
rules and reflexes. Legacy declarative rules and absent legacy metadata are
warnings, not failures. Ocelot's loader remains authoritative.

## Research map

`ocelot_research` takes a document path, a block number such as `371000`, or a
topic substring, and returns the prereg/amendment/results/index chain plus
linked scripts, tests, seeds, engine hashes, commit pins, missing gate tests
and stale-engine flags. With no target it returns a census of missing
prereg/result links and gates. Only `*_PREREG.md`, `*_AMENDMENT.md`,
`*_RESULTS.md` and `INDEX.md` under `docs/research` are indexed; the rest of
that tree is deliberately left out. A recorded old engine hash is reported as
stale; the tool never claims a scientific result is true.

## CLI

```bash
ozagia-ocelot-workspace status --root .
ozagia-ocelot-workspace seed shape_decode_loop --root . --include all
ozagia-ocelot-workspace seeds --root . --json
ozagia-ocelot-workspace plan --root . --changed seeds/sd_loop.json
ozagia-ocelot-workspace plan --root . --base origin/main --head HEAD
ozagia-ocelot-workspace plan --root . --seed shared_seed --json
ozagia-ocelot-workspace engine --root . --op term_census --json
ozagia-ocelot-workspace engine --root . --op primitive --target Substrate
ozagia-ocelot-workspace research --root . 371000
ozagia-ocelot-workspace validate --root . shape_decode_loop
```

`status` can run before the graph exists. The graph-backed commands require
`ozagia index --root .`; the CLI reports the missing snapshot and the exact
command.

## Limits

- Seed item data is bounded to names and counts. Full rule bodies stay in the
  manifest; read the file when editing. A manifest over the size cap
  (`32 MiB`) contributes head metadata and is marked `partial` rather than
  being parsed in full on every index.
- Seed mentions use manifest paths, quoted seed ids, and long bare ids. A
  test that only uses an unquoted short id may not be linked.
- Test selection is syntactic and conservative. Shared seeds, boot/seed-loader
  changes and engine changes intentionally expand to the full migrated gate.
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
