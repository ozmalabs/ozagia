# Rust parity bench — 2026-11-23

Status: Phase 7 smoke measurement of the Rust index, symbol graph and SCIP
fixture. This is a small-crate benchmark, not a performance guarantee.

## Target and environment

| Item | Value |
| --- | --- |
| Repository | `~/ozma-ai/rust` (`ozma` crate) |
| Commit | `33a39c8` |
| Rust source files | 27 (1.9 MB) |
| Platform | Linux 6.8.0-147-generic, x86_64, 24 CPUs |
| Python | 3.13.5 |
| Rust | rustc/cargo 1.87.0 (Homebrew) |
| rust-analyzer | 1.97.1 (8bab26f 2026-07-14) |
| Tag parser | `tree-sitter-rust` |
| Bench cache | `/tmp/opencode/ozagiabench/ozma_rust_tags.json` |

Commands:

```bash
PYTHONPATH=src .venv/bin/python -m ozagia.eval.bench \
  --root ~/ozma-ai/rust --cold \
  --cache /tmp/opencode/ozagiabench/ozma_rust_tags.json

PYTHONPATH=src .venv/bin/python -m ozagia.eval.bench \
  --root ~/ozma-ai/rust \
  --cache /tmp/opencode/ozagiabench/ozma_rust_tags.json
```

The resolution counts were measured in one process with
`rust_modules.discover`, `rust_tests.discover`, `update_tag_cache` and
`build_symbol_graph`. The full JSON is in
`docs/results/rust_parity_bench_2026-11-23.json`.

## Index and queries

| Metric | Cold | Warm |
| --- | ---: | ---: |
| Tag-cache update | 0.305 s | 0.016 s |
| Files parsed / reused | 27 / 0 | 0 / 27 |
| Repo-map median (second call) | 0.019 s | 0.019 s |
| Code-search median | 0.025 s | 0.026 s |
| Impact median | 0.003 s | 0.002 s |
| Code-search new process | 0.101 s | 0.099 s |
| All bench targets met | yes | yes |

The cold update parsed all 27 files with no failures. The warm update reused
all 27 cached files. The repo-map, search and impact targets were met with
large margins on this crate.

## Module, test and symbol resolution

| Metric | Value |
| --- | ---: |
| Cargo targets | 1 |
| Crates | 1 (`ozma`) |
| Rust module contexts | 35 |
| Rust test targets | 1 |
| Rust tests mapped | 41 |
| Graph files | 27 |
| Graph definitions | 393 |
| Graph references | 1,878 |
| Graph edges | 408 |
| Resolved local | 208 |
| Resolved import | 31 |
| Resolved name | 133 |
| Unresolved references | 1,506 |
| Ambiguous module paths | 0 |

`rust_modules.discover` and `rust_tests.discover` took 0.050 s and 0.047 s;
the warm tag cache took 0.015 s and `build_symbol_graph` 0.054 s.

The unresolved count is expected for a syntactic graph: it includes external
`std`/crate paths, built-in macros, local bindings and references the name
heuristic deliberately refuses to guess. It is not a count of index failures.
The precise layer remains rust-analyzer/SCIP.

## SCIP fixture

`tests/fixtures/scip/rust_index.scip.json` is a real rust-analyzer index of
`tests/fixtures/py_rust_migration/rust`, exported from the protobuf index and
sanitized so the project root is `file:///repo`.

| Metric | Value |
| --- | ---: |
| Documents | 3 |
| Definitions | 29 |
| References | 25 |
| Local definitions | 8 |
| Local references | 5 |
| Symbols | 31 |
| Defined symbols | 21 |
| Cross-file edges | 2 (`src/lib.rs` → `src/models.rs`, `src/service.rs`) |
| Protobuf index | 7,424 bytes |
| JSON fixture | 19,835 bytes |

`tests/test_scip.py::test_rust_analyzer_scip_fixture_round_trip` loads the
fixture, imports the graph payload, round-trips the file summary and checks
the two cross-file edges. The Sourcegraph `scip` CLI was not installed; the
JSON was produced with protoc-generated `scip_pb2` bindings. See
`docs/scip.md` for the recipe and the protobuf fallback.

## Limits

- 27 files and one crate; this does not measure a large multi-crate
  workspace, dependency-heavy build or incremental edit loop.
- Timings are single-machine smoke values, not a CI gate.
- The cold measurement excludes OS page-cache warm-up and uses a dedicated
  temporary tag cache.
- SCIP coverage is the importer and fixture round-trip, not a full
  rust-analyzer/SCIP integration in the graph snapshot.
- Rust dataflow/trust-boundary analysis remains a separate plan.
