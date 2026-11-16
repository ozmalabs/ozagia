# Advisory repository scan

`ozagia.core.repo_scan` and `python -m ozagia.repo_scan_cli` rank development
opportunities from an **existing** graph snapshot, bounded git reads,
filesystem measurements and a few read-only state files.

This is stage one of plan item 15. It is deliberately advisory: a scan does not
build or refresh the index, edit a file, run a test, call a model, use the
network or claim that a finding is correct. Stage two (building a module) is a
separate, person-approved bounded task with a preview, tests and a rollback
path.

## Inputs

Read-only inputs:

- `<root>/.ozagia/graph.json` (or `OZAGIA_STATE_DIR`), loaded once. The scan
  never writes, rebuilds or refreshes it.
- bounded `git` calls: `rev-parse HEAD` and `git status --porcelain -uall`.
- bounded filesystem walks of the configured source directories, `history/`
  and known cache/build directories. Symlinks are not followed.
- `.ozagia.toml` through `ozagia.config.load_project_config` (source/test
  directories, adapters, test selection policy).
- optional state readings: `.ozagia/tests/results.json` (last known failures
  and flaky tests), the created-file ledger (`.ozagia/files/ledger.jsonl`),
  review packets under `.ozagia/reviews/*.json` and an already-produced
  security report at `.ozagia/security/latest.json`.
- bounded source reads for library hints: at most 20 changed/hotspot source
  files, their imports, and the manifests beside them (`pyproject.toml`,
  `requirements*.txt`, `package.json`, `Cargo.toml`). No hint event is
  recorded.

The graph's commit nodes provide churn; `--since N` bounds the churn window and
the age used for stale `history/` notes. `--top N` bounds the number of
opportunities in the report. Per-category caps apply before the global cap.

### Graph states

| state | meaning | scan status |
| --- | --- | --- |
| missing | no snapshot at the expected path | `unavailable` |
| unreadable / invalid | file exists but cannot be parsed or has no node/edge lists | `degraded` |
| stale | snapshot root or HEAD differs from the checkout | `degraded` |
| unknown | root/HEAD/working-tree freshness cannot be verified | `degraded` |
| empty | the snapshot contains no non-Repo nodes | `degraded` |
| fresh | root and HEAD match and the working tree is clean | `ok` |

Filesystem and configuration checks still run when the graph is missing or
stale. Graph-derived categories are omitted or have their confidence reduced,
and the report records that in `not_assessed`. The CLI exits 0 for an
unavailable graph: that is a reported state, not a tool failure.

## Categories

- `missing_tests` — source files with no test linked by
  `Test -tests_module|mentions_file-> File`. Changed files rank first.
- `hotspots` — large symbols, many symbols, high import degree, recent churn
  or large file size. These are syntactic proxies; no profiler runs.
- `duplicate_modules` — identical source files by recorded SHA-256, and
  near-duplicates by token-shingle Jaccard similarity.
- `duplicate_docs` — identical or near-identical Markdown, including
  `history/` notes.
- `weak_boundaries` — files with high import fan-in or fan-out.
- `orphaned_files` — source files no indexed file imports (excluding tests,
  package markers and conventional entrypoints), plus created-file ledger
  entries with no recorded reference.
- `stale_history` — files under `history/` older than the scan window.
- `generated_clutter` — measured cache/build/environment directories and
  `.coverage`; cleanup always requires confirmation.
- `unindexed_languages` — source files in languages the built-in index does
  not parse (the built-in index covers Python, Rust and Lua).
- `missing_adapters` — no `[adapters]` module despite unindexed languages, or
  a configured adapter that contributed no nodes / left files unindexed.
- `test_policy` — recorded failing or flaky tests, `[tests] select = "all"`
  while changes are local, and a graph with no Test nodes.
- `code_review` — blocking/major findings from existing review packets and
  known-failure nodes from the graph.
- `library_reuse` — bounded capability hints over changed files first, then
  the highest-churn/highest-degree source files: a hand-written helper may
  match a standard-library primitive or a known package. At most five hints
  become opportunities. A hint names the package, capability, availability
  and confidence; it is not a refactor and adds no dependency. With no hint
  the category records an explicit no-candidate note, not a clean bill. The
  report also has a `library_hints` object with the mode, state, sampled files
  and counts. `--no-library-hints` skips the category.
- `security` — high-confidence findings from an explicit security report
  passed to the API or found under `.ozagia/security/latest.json`. The scan
  never runs a security tool, scanner or network call itself. Without a
  report the report says security was not assessed rather than clean.

## Opportunity shape

Each opportunity has:

- `id` — stable for the same evidence (for example `hotspot:src/app.py` or a
  content digest for a duplicate pair);
- `category`, `title`, `rank`, `score`;
- `evidence` — structured entries of kind `file` (with optional line),
  `graph_key`, `command` or `metric`;
- `expected_effect`, `effort` (`S`/`M`/`L`), `risk`, `confidence`;
- `proposed_module` and `action`;
- `not_assessed` — what the signal does not establish.

Ordering is deterministic: score descending, then category order, then id.
The report includes `summary`, `omitted.by_category`, `omitted.total`, the
per-category cap and the global cap. Re-running a scan on an unchanged
checkout produces byte-identical JSON. The `library_hints` object records
whether hints were enabled, the mode, the sampled files and hint/suppressed
counts; it never stores source text.

## Optional Ocelot clustering

When `ocelot=True` or `--ocelot` is supplied, the scan also sends the
`duplicate_modules`, `duplicate_docs`, `weak_boundaries` and `security`
opportunities that are shown to `ozagia.core.ocelot_code`. The report gains an
`ocelot` section with `state`, `available`, `mode`, `engine`, clusters,
analogies and `truth_assessed: false`. Clusters are keyed by issue family and
target; different families or targets are never merged.

Ocelot is optional and read-only. A missing or failing checkout is reported as
`not_installed`, `unavailable` or `error` with exact fallback clusters, plus a
warning; the scan is not failed and nothing is installed or downloaded. The
scan's graph/security status is unchanged.

When `ocelot_patterns=True` or `--ocelot-patterns` is supplied, the report also
gains `ocelot.patterns`: candidate architecture patterns over the opportunities
shown. This is independent of finding clustering and is off by default. Every
pattern has `candidate: true` and `truth_assessed: false`; a pattern is not an
anti-pattern or defect verdict and completeness is not assessed. An unavailable
checkout is reported with exact signature/overlap patterns, not installed. See
`docs/ocelot_code.md`.

## CLI

```bash
PYTHONPATH=src python -m ozagia.repo_scan_cli scan --root . --since 30 --top 25
PYTHONPATH=src python -m ozagia.repo_scan_cli scan --root . --json --out /tmp/scan.json
PYTHONPATH=src python -m ozagia.repo_scan_cli scan --root . --ocelot
PYTHONPATH=src python -m ozagia.repo_scan_cli scan --root . --ocelot-patterns
PYTHONPATH=src python -m ozagia.repo_scan_cli scan --root . --no-library-hints
PYTHONPATH=src python -m ozagia.repo_scan_cli list-categories
```

`--ocelot` (with optional `--ocelot-root`, `--ocelot-mode` and
`--ocelot-timeout`) adds structural clusters for the duplicate, weak-boundary
and security opportunities that are shown. `--ocelot-patterns` adds candidate
architecture patterns; both flags are off by default. `--no-library-hints`
skips the bounded `library_reuse` category; it is on by default. An unavailable
checkout is reported, not installed, and never fails the scan. See
`docs/ocelot_code.md` and `docs/library_hints.md`.

Exit codes: `0` for a completed scan (including an unavailable graph) and
`list-categories`; `2` for usage or IO errors. `--out` is the only write and
happens only when the caller asks for it.

## Limits

- The graph is a snapshot. A stale or missing graph is reported, never
  repaired.
- Churn is limited to the commit nodes the snapshot contains (the builder's
  history limit) and the `--since` window.
- Filesystem walks, graph size, status entries, reads, duplicate candidates
  and review packets all have hard bounds. Truncation is reported in
  `warnings` and can lower confidence.
- Generated-directory sizes are measured with a bounded walk; a truncated
  measurement is an undercount, not exact per-process attribution.
- Duplicate detection is textual; it does not prove semantic equivalence.
- Retrieval, agent and performance eval result files under `evaluation/` are
  not interpreted; run those eval commands separately and compare their own
  reports.
- The import graph is syntactic. Dynamic imports, plugins, subprocess entry
  points and external consumers are not visible.
- No test is executed. A linked test may be weak, skipped or failing for
  unrelated reasons.
- No security, licence, dependency or supply-chain analysis is performed by
  this scan itself. A supplied security report is ranked as advisory evidence;
  a missing report is recorded as not assessed.
- Library hints are heuristic and cover only the bounded sample of changed and
  hotspot files. A missed hint is expected, a false positive is possible, and
  a hint never proves semantic equivalence or that a dependency change is
  acceptable. No package is installed and no code is refactored.

A scan never edits the repository. Cleanup actions described in the report
require explicit confirmation and, for ledger-managed files, the existing
trash/restore path.
