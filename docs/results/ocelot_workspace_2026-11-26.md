# Ocelot workspace profile — first-slice measurement

Date: 2026-11-26. Read-only measurement on the canonical Ocelot checkout.

## Source

- Ocelot: `matt@192.168.40.19:~/ozma/ocelot`, `main` at `24c56891`
- ozagia: `0.1.5.dev0` wheel built from this tree, installed in a temporary
  Python 3.13.18 virtualenv
- Index roots: `domains`, `worlds`, `substrate`, `tests`, `runners/dsl/src`
- Docs: none for this measurement; `seeds/acquired/`, `seeds/generated/`,
  `data/`, `results/` and evaluation output were not indexed

## Index

| Measurement | Result |
| --- | --- |
| Build time | 37.3 s |
| Nodes | 44,212 |
| Edges | 194,685 |
| Files | 4,055 |
| Test nodes | 15,014 |
| OcelotSeed nodes | 924 |
| OcelotEngine nodes | 1 |

Seed census:

| Measurement | Result |
| --- | --- |
| Unique seed manifests | 920 |
| Dependency stubs | 4 |
| Partial manifests | 1 (`embodied_motor_program`, 68 MB, head metadata only) |
| Shared seeds | 24 |
| Seeds with no test mention | 333 |
| Seeds outside every boot list | 648 |
| Dependency cycles | 0 |
| Index warnings | 2 (`llvm_ir` filename/id mismatch and duplicate id) |

High-fan-out examples:

| Seed | Fan-out | Test files | Blast radius |
| --- | ---: | ---: | --- |
| `compiler_theory` | 27 | 40 | core |
| `thinking_budget` | 23 | 20 | core |
| `shape_decode_loop` | 22 | 29 | core |
| `embodied_motor_program` | 1 | 11 | local (partial manifest) |

## Plans

| Change | Plan |
| --- | --- |
| `seeds/embodied_motor_program.json` | affected, 20 test files |
| `seeds/shape_decode_loop.json` | full migrated gate (core, fan-out 22) |
| `seeds/thinking_budget.json` | full migrated gate (core, fan-out 23) |
| `substrate/world_adapter.py` | full migrated gate (shared/core file) |
| `runners/dsl/src/lib.rs` | engine rebuild + cargo test slice + full migrated gate (selection exceeded the 250-file cap) |
| `docs/research/engine_substrate/INDEX.md` | no pytest selected |
| unknown path | full migrated gate with the unclassified-path reason |

## Doctor

The canonical checkout's engine was sound:

- native artifact present, not a symlink;
- build record and Rust source fingerprint matched;
- engine SHA-256 matched the build record;
- no stale Rust files;
- `data/` and `results/failure_baseline.json` present.

The doctor returned `warn`, correctly, because the user-level
`~/.local/lib/python3.14/site-packages/substrate_rs.pth` points at a different
tree:
`/home/matt/ozma/internal/research/experiments/gamma-substrate/runners/dsl/python`.
That is a real local `.pth` hazard, not a graph failure.

## Limits

This is an index/query measurement, not a model-task gain or a full Ocelot
repository index. It used a bounded source set and no docs. The synthetic
fixture tests cover wrong-engine, stale-source, missing-native, headless,
`.pth`, dataset and baseline states; those cases were not reproduced on the
canonical checkout because its engine is healthy.
