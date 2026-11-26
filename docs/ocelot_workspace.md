# Ocelot workspace profile

`ozagia.integrations.ocelot.workspace` is an optional adapter for working *on*
an Ocelot checkout. It indexes Ocelot seed metadata, seed dependencies,
boot/registry references, seed-aware tests and docs, and engine-aware Python
files. It adds a conservative test planner and four MCP tools:

- `ocelot_status` — worktree, engine provenance, datasets and failure baseline
- `ocelot_seed` — one seed with dependencies, dependents, tests and docs
- `ocelot_seeds` — seed census: shared seeds, missing tests, cycles and stubs
- `ocelot_plan` — affected tests or a full-gate fallback for changed paths

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

The indexer records `OcelotSeed` nodes, `ocelot_depends_on`,
`ocelot_boots`, `ocelot_mentions_seed`, `ocelot_documents`,
`ocelot_uses_engine` and `ocelot_engine_source` edges. It stores bounded
metadata and the first few item names per seed, not every rule body. Reingest
re-runs the adapter when a seed manifest, boot file, build record, or a text
that mentions a seed or `substrate_rs` changes.

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

## CLI

```bash
ozagia-ocelot-workspace status --root .
ozagia-ocelot-workspace seed shape_decode_loop --root . --include all
ozagia-ocelot-workspace seeds --root . --json
ozagia-ocelot-workspace plan --root . --changed seeds/sd_loop.json
ozagia-ocelot-workspace plan --root . --base origin/main --head HEAD
ozagia-ocelot-workspace plan --root . --seed shared_seed --json
```

`status` can run before the graph exists. `seed`, `seeds` and `plan` require
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
- Ocelot's `domains.engine_provenance.py`, `VISION_PROTOCOL.md` and
  `CLAUDE.md` remain authoritative. This profile points at them; it does not
  replace them.
