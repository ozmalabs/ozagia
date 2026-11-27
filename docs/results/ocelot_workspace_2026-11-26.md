# Ocelot workspace profile — first- and second-slice measurement

Date: 2026-11-26. Read-only measurement on the canonical Ocelot checkout.

## Source

- Ocelot: canonical checkout `/home/matt/ozma/ocelot`, `main` at `24c56891`
  (the audit records the host-qualified path
  `matt@192.168.40.19:~/ozma/ocelot`)
- ozagia: `0.1.5.dev0` wheel built from this tree, installed in a temporary
  Python 3.13.18 virtualenv
- Index roots: `domains`, `worlds`, `substrate`, `tests`, `runners/dsl/src`
- Docs: no general docs were indexed; the adapter's targeted research overlay
  indexed 1,332 protocol documents. `seeds/acquired/`, `seeds/generated/`,
  `data/`, `results/` and evaluation output were not indexed

## Index

| Measurement | Result |
| --- | --- |
| Build time | 42.7 s |
| Nodes | 48,028 |
| Edges | 217,487 |
| Files | 5,846 |
| Test nodes | 15,014 |
| OcelotSeed nodes | 924 |
| OcelotPrimitive nodes | 439 |
| OcelotTerm nodes | 255 |
| OcelotResearch nodes | 1,332 |
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

## Engine bridge

The syntactic bridge indexed:

- 439 `OcelotPrimitive` nodes: `#[pyfunction]`, `#[pyclass]` and simple
  `#[pymethods]` definitions, plus Python-used names that have no local Rust
  definition;
- 255 defined `OcelotTerm` names from `TermNode`, with `jsonio.rs` wire names;
- 139 Terms used by 504 seeds, 0 unknown Terms, 116 defined Terms unused;
- `Substrate` with 102 Python source files and 3,720 test links;
- a `runners/dsl/src/term.rs` impact query returning 255 Terms and 504 seeds.

## Research map

The targeted research overlay indexed 1,332 documents (not the whole
`docs/research` tree) and linked prereg/amendment/results/index chains,
scripts, gate tests and seed manifests. Block `371000` resolves to four
documents (index, prereg, amendment, results), five scripts, three gate tests
and two seeds; old recorded engine hashes are reported as stale rather than
silently treated as current.

## Seed validation

Structural validation accepted the representative seeds
`shape_decode_loop`, `embodied_motor_program` and `abandon_on_stuck` with no
errors. Six manifests remain structurally invalid under the strict checks;
legacy manifests with absent id/version/depends_on now produce warnings, not
errors, because Ocelot's loader accepts them. Production-coverage warnings on
shared seeds are reported but do not fail validation.

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

## Agent context and seed editing

The session-start context was run against the canonical checkout with the
changed paths from commit `d3000ad5`. It reported:

- doctor state `warn` (the known user-level `substrate_rs.pth` points outside
  the worktree), engine `ready`, native artifact present and source match
  `true`;
- plan `affected`, 3 test files, with the changed seed `affect_frame` (local,
  fan-out 1, dependent `affect_frame_canonical`);
- the block 370000 research chain, flagged as recording a stale engine hash;
- the next commands, including the exact pytest command, and the note
  "affected slice only; do not claim full coverage".

A preview-only edit of `seeds/affect_frame_canonical.json` replaced the
description and appended a `ShapeSignature` node. Preview returned `ok: true`,
the correct base/new SHA-256 values, a two-operation diff, prospective
validation with no errors, and the original file bytes were unchanged. The
`add_node` operation allocated a free `local_id`; no write occurred. The
result also previewed a `tests/test_<id>_migrated.py` skeleton without creating
the file.

The OpenCode session hint was exercised against the real adapter. With no
changed paths it reported `state: warn`, `plan: none`, `test_count: 0`, the
engine as ready, and the known external `.pth` warning. The marked block was
677 characters before the `[ozagia ocelot]` header/instruction (837 with it),
was idempotent on a second augmentation, and left a Codex hint unchanged.

A combined preview of the explicit boot/registry editor touched three real
files without writing:

- add `affect_frame` to `CORE_SEEDS`;
- remove `conversation_reply` from `CONVERSATION_SEED_ANCHORS`;
- register `affect_frame` at manifest version `0.2.0` with its two
  dependencies;
- unregister `decision_core`.

The preview returned `ok`, 51 diff lines, all four change summaries and
`validation_before`/`validation_after` `ok`; all three files were byte-identical
afterwards.

## Planner evaluation

Eight historical commits were replayed offline with the graph snapshot built
from `24c56891`. Cases and results:

| Commit | Change | Expected | Plan | Selected total | Missed |
| --- | --- | --- | --- | ---: | --- |
| `a6eb3a60` | engine + seed + tests (block 371000) | full | full | 1,118 | 0 |
| `64ce1af6` | engine + test (block 371000) | full | full | 1,113 | 0 |
| `d3000ad5` | seed + script + tests (block 370000) | affected | affected | 3 | 0 |
| `77ba15a7` | results + test (block 368000) | affected | affected | 1 | 0 |
| `6ed6d3c5` | results + test (block 367000) | affected | affected | 1 | 0 |
| `c7fcde64` | test pin only | affected | affected | 1 | 0 |
| `e67d7b73` | results + index only | none | none | 0 | 0 |
| `1281a482` | prereg + scope only | none | none | 0 | 0 |

"Selected total" is the planner's full test count; the evaluation report
displays at most 20 per case under its 20-file budget, so the two engine cases
show "20 of 1,118" and "20 of 1,113" there. Aggregate: 8/8 cases covered,
commit-touched affected recall 1.0 (5/5 expected test files),
0 expected misses, 0 mode mismatches, 2 engine rebuilds, 0
full-gates-required-but-expected-affected. The two engine commits correctly
required the full migrated gate; the two documentation-only commits selected
no tests. The one over-selection was `test_affect_map_reuse_gridcode.py` on
`d3000ad5`, because the changed seed mentions it; extra tests are acceptable.
Scripts without a graph test link produced explicit warnings rather than
silent coverage.

The "affected recall 1.0" figure is not independent recall: the expected tests
are the test files each commit touched, and full-gate rows are counted covered
by routing. The metric therefore checks commit-touched test coverage plus
full-gate routing; it does not establish a general recall benefit.

The case file is
[`ocelot_workspace_eval_cases_2026-11-26.json`](ocelot_workspace_eval_cases_2026-11-26.json)
and the generated report is
[`ocelot_workspace_eval_2026-11-26.md`](ocelot_workspace_eval_2026-11-26.md).

## Research ledger and parity

- `ocelot_claim_check` read the real burned-seed ledger. `7000–7999` and
  `9000–9005` both reported conflicts against declared ranges; the result
  includes the ledger line and its "used for" text.
- `ocelot_engine --op rs_parity` paired the single `domains_rs/conway.py`
  mirror with `domains/conway.py` and linked `tests/test_conway.py` and
  `tests/test_conway_parity.py`.

## Limits

This is an index/query and offline planner-replay measurement, not a live
agent-workflow or model-task gain, and not a full Ocelot repository index. The
live evaluation was not run and remains approval-gated by `--live` /
`OZAGIA_EVAL_LIVE=1` and a person's approval. The measurement used a bounded
source set and no general docs; only the targeted research overlay indexed
protocol documents. The seed-edit and boot/registry probes were
preview-only; no real manifest, boot list or registry file was written. The
planner evaluation compares selected files with the test files the commits
touched, but it did not execute those tests, so it does not prove that the
slice would have caught each defect. The synthetic fixture tests cover
wrong-engine, stale-source, missing-native, headless, `.pth`, dataset and
baseline states; those cases were not reproduced on the canonical checkout
because its engine is healthy.
