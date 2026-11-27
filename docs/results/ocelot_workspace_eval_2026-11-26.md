# Ocelot test planner evaluation

- date: 2026-11-26
- result: pass (offline planner replay; not a live model-task gain)
- root: `/home/matt/ozma/ocelot` (audit path
  `matt@192.168.40.19:~/ozma/ocelot`)
- snapshot: `24c56891`
- budget: 20 test file(s) displayed per case
- cases: 8 (affected 4, full 2, none 2)
- recall (affected): 100.0% (commit-touched coverage; not independent recall)
- expected misses: 0
- full gate required but expected affected: 0
- engine rebuild cases: 2

## Cases

| case | expected | plan | selected | expected | recall | covered |
| --- | --- | --- | --- | --- | --- | --- |
| a6eb3a60 sd re-arm + ClosestShape (engine, block 371000) | full | full | 20 of 1118 | 4 | n/a | yes |
| 64ce1af6 selector audit + similarity alias (engine, block 371000) | full | full | 20 of 1113 | 1 | n/a | yes |
| d3000ad5 Affect frame v0.2 (seed + script, block 370000) | affected | affected | 3 | 2 | 100% | yes |
| 77ba15a7 affect direction judgement (block 368000) | affected | affected | 1 | 1 | 100% | yes |
| 6ed6d3c5 affect map reuse + grid code (block 367000) | affected | affected | 1 | 1 | 100% | yes |
| c7fcde64 pin affect-frame G-0 range | affected | affected | 1 | 1 | 100% | yes |
| e67d7b73 block 371000 results only | none | none | 0 | 0 | n/a | yes |
| 1281a482 block 371000 preregistration only | none | none | 0 | 0 | n/a | yes |

### a6eb3a60 sd re-arm + ClosestShape (engine, block 371000)

- changed: `runners/dsl/python/substrate_rs/_native.abi3.so`, `runners/dsl/python/substrate_rs/_native.build.json`, `runners/dsl/src/jsonio.rs`, `runners/dsl/src/reflect.rs`, `runners/dsl/src/rewrite.rs`, `runners/dsl/src/shape.rs`, `runners/dsl/src/shape_decode.rs`, `runners/dsl/src/term.rs`, `runners/dsl/src/term_variants.rs`, `scripts/gen_sd_rearm_and_closest_shape.py`, `seeds/shape_decode_loop.json`, `tests/test_affect_frame_v02.py`, `tests/test_agnosia_migrated.py`, `tests/test_det1_migrated.py`, `tests/test_sd_rearm_closest_shape_migrated.py`
- full reasons: runners/dsl/python/substrate_rs/_native.abi3.so: unclassified path; runners/dsl/python/substrate_rs/_native.build.json: unclassified path; seed shape_decode_loop is core (fan-out 22); selected 1118 test files (cap 250)
- engine rebuild required before measuring
- plan warning: scripts/gen_sd_rearm_and_closest_shape.py: no test link in the graph

### 64ce1af6 selector audit + similarity alias (engine, block 371000)

- changed: `runners/dsl/python/substrate_rs/_native.abi3.so`, `runners/dsl/python/substrate_rs/_native.build.json`, `runners/dsl/src/shape_decode.rs`, `tests/test_similarity_migrated.py`
- full reasons: runners/dsl/python/substrate_rs/_native.abi3.so: unclassified path; runners/dsl/python/substrate_rs/_native.build.json: unclassified path; selected 1113 test files (cap 250)
- engine rebuild required before measuring

### d3000ad5 Affect frame v0.2 (seed + script, block 370000)

- changed: `docs/research/psychology_affect/AFFECT_FRAME_V02_RESULTS.md`, `docs/research/psychology_affect/INDEX.md`, `scripts/build_affect_frame_seed.py`, `seeds/affect_frame.json`, `seeds/affect_frame_canonical.json`, `tests/test_affect_frame_codebook.py`, `tests/test_affect_frame_v02.py`
- plan warning: scripts/build_affect_frame_seed.py: no test link in the graph

### 77ba15a7 affect direction judgement (block 368000)

- changed: `docs/research/psychology_affect/AFFECT_DIRECTION_JUDGEMENT_RESULTS.md`, `docs/research/psychology_affect/INDEX.md`, `tests/test_affect_direction_judgement.py`

### 6ed6d3c5 affect map reuse + grid code (block 367000)

- changed: `docs/research/psychology_affect/AFFECT_MAP_REUSE_GRID_CODE_RESULTS.md`, `docs/research/psychology_affect/INDEX.md`, `tests/test_affect_map_reuse_gridcode.py`

### c7fcde64 pin affect-frame G-0 range

- changed: `tests/test_affect_frame_v02.py`

### e67d7b73 block 371000 results only

- changed: `docs/research/engine_substrate/INDEX.md`, `docs/research/engine_substrate/SD_LOOP_REARM_AND_JSON_SIGNATURE_RESULTS.md`
- plan warning: documentation-only change: no pytest selected

### 1281a482 block 371000 preregistration only

- changed: `docs/research/engine_substrate/INDEX.md`, `docs/research/engine_substrate/SD_LOOP_REARM_AND_JSON_SIGNATURE_PREREG.md`, `docs/research/engine_substrate/SD_LOOP_REARM_SCOPE.txt`
- plan warning: documentation-only change: no pytest selected

## Notes

- a6eb3a60 sd re-arm + ClosestShape (engine, block 371000): Rust engine rebuild required before measuring
- a6eb3a60 sd re-arm + ClosestShape (engine, block 371000): scripts/gen_sd_rearm_and_closest_shape.py: no test link in the graph
- 64ce1af6 selector audit + similarity alias (engine, block 371000): Rust engine rebuild required before measuring
- d3000ad5 Affect frame v0.2 (seed + script, block 370000): scripts/build_affect_frame_seed.py: no test link in the graph
- e67d7b73 block 371000 results only: documentation-only change: no pytest selected
- 1281a482 block 371000 preregistration only: documentation-only change: no pytest selected
- affected recall uses the commit-touched tests as expectations and treats
  full-gate cases as covered by routing; it is not independent recall and does
  not establish a general recall benefit

No tests were executed. The planner and this evaluation are read-only; a
full-gate row is coverage guidance, not a recall claim. This was an offline
replay against the `24c56891` snapshot: no pytest, engine build, model call or
network request was made. It is not a live agent-workflow/model-task
evaluation; that run was not performed and remains approval-gated.
