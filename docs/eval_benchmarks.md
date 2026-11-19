# Hugging Face benchmark retrieval verification

`ozagia.eval.hf_benchmarks` fetches public benchmark datasets from Hugging
Face, converts the gold code patch to explicit retrieval tasks, and measures
whether Ozagia's retrieval arms surface those files inside a token budget.

The first registry entries are **SWE-bench Verified** and **SWE-bench Lite**.
This is a file-localisation instrument. It does not run a target repository's
tests, execute project code, call a model, or measure SWE-bench resolution
rate.

A three-task live smoke is recorded in
[`results/hf_swe_bench_verified_smoke_2026-11-18.md`](results/hf_swe_bench_verified_smoke_2026-11-18.md).
A larger 23-task stratified run across 12 repositories and three budgets is in
[`results/hf_swe_bench_verified_stratified_2026-11-19.md`](results/hf_swe_bench_verified_stratified_2026-11-19.md).
Neither is a representative SWE-bench score; they verify the path and provide
a first retrieval baseline.

## Exact commands

List the registry (dataset, config, split, endpoints, licence status, row cap):

```bash
.venv/bin/python -m ozagia.eval.hf_benchmarks list
.venv/bin/python -m ozagia.eval.hf_benchmarks list --json
```

Fetch a bounded page set. Network is refused unless `--allow-network` is
present:

```bash
.venv/bin/python -m ozagia.eval.hf_benchmarks fetch \
  --benchmark swe-bench-verified --allow-network --limit 10
.venv/bin/python -m ozagia.eval.hf_benchmarks fetch \
  --benchmark swe-bench-lite --allow-network --limit 100 --offset 0
```

Import a local rows JSON without any network access. The file may be an HF
`rows` API response (`{features, rows:[{row: ...}]}`) or a cache
`rows.json` produced by this module:

```bash
.venv/bin/python -m ozagia.eval.hf_benchmarks import \
  --benchmark swe-bench-verified tests/fixtures/hf_benchmarks/rows_verified.json
```

Convert cached rows to retrieval tasks and a source sidecar:

```bash
.venv/bin/python -m ozagia.eval.hf_benchmarks convert \
  --benchmark swe-bench-verified
```

Write the tasks elsewhere (the source sidecar is written beside it):

```bash
.venv/bin/python -m ozagia.eval.hf_benchmarks convert \
  --benchmark swe-bench-verified --out-json /tmp/swe-tasks.json
```

Verify retrieval. Existing cached checkouts are reused without network;
missing checkouts require `--allow-network`. Verification defaults to the first
10 tasks (maximum 100) so a smoke run stays bounded; pass `--limit` explicitly
for a larger run:

```bash
.venv/bin/python -m ozagia.eval.hf_benchmarks verify \
  --benchmark swe-bench-verified --allow-network \
  --budgets 500,900,1500 \
  --arms baseline,code_search,dev_repo_map,grep \
  --out-json evaluation/swe_verified.json \
  --out-md evaluation/swe_verified.md
```

Plan without downloading or evaluating (still no network access):

```bash
.venv/bin/python -m ozagia.eval.hf_benchmarks verify \
  --benchmark swe-bench-verified --dry-run
```

Use a dedicated cache root, for example for a smoke test that must not touch
the repository state dir:

```bash
.venv/bin/python -m ozagia.eval.hf_benchmarks fetch \
  --benchmark swe-bench-verified --allow-network --limit 1 \
  --cache-dir /tmp/ozagia-hf-smoke
```

The equivalent console script is `ozagia-eval-benchmarks`.

## Data provenance and licence caveat

Each fetch records the Hugging Face dataset id, the dataset revision `sha` from
`https://huggingface.co/api/datasets/<dataset>`, the config and split, the
source URL, fetch time, row count, rows-file SHA-256, and the licence metadata.

SWE-bench Verified and SWE-bench Lite do not state an explicit licence in the
dataset tags or `cardData` at the time this harness was written. The registry
therefore records:

- `license: unknown`
- `license_status: not stated on dataset card or tags`

This is deliberate: the harness does not guess a licence from the repository
name, the paper, or a neighbouring dataset. If the Hugging Face metadata later
contains an explicit `license:<id>` tag or `cardData.license`, `fetch` records
that value and its provenance instead.

Users are responsible for checking the dataset and upstream GitHub repository
terms before redistributing data or results. Cached rows and checkouts are not
committed by this repository; the default `.ozagia/` state directory is
git-ignored.

## Cache layout

The default root is `<state>/eval/hf`, where `<state>` is normally
`<repo>/.ozagia` (or `$OZAGIA_STATE_DIR`). `--cache-dir` replaces the
`<state>/eval/hf` prefix.

```text
<state>/eval/hf/<benchmark>/<dataset_sha>/rows.json
<state>/eval/hf/<benchmark>/<dataset_sha>/meta.json
<state>/eval/hf/<benchmark>/<dataset_sha>/tasks.json
<state>/eval/hf/<benchmark>/<dataset_sha>/sources.json
<state>/eval/hf/<benchmark>/<dataset_sha>/runs/<owner>/<name>/<commit>/...
<state>/eval/hf/<benchmark>/repos/<owner>/<name>/<commit>/...
```

`rows.json` stores the validated row objects and the features list. `meta.json`
stores the dataset id and revision, config, split, fetch/import time, origin,
licence status, row count, SHA-256, byte size, and source URLs. The dataset
revision is part of the path so a newer dataset revision cannot silently mix
with older rows.

Writes use a temporary file plus `os.replace`. `load_cached_rows` re-computes
the rows digest and rejects a `meta.json` whose SHA-256 or row count does not
match. Import also rejects a rows file whose dataset, config, or split does not
match the registry entry.

If an imported rows file has no revision `sha`, the cache uses a
`local-<digest>` label derived from the source file bytes. Pass `--dataset-sha`
to give it a stable, meaningful revision label.

## Network, SSRF and bounds

Network access is always explicit and fails closed:

- `fetch`, metadata fetches, and archive downloads require
  `--allow-network` / `allow_network=True`. Without it the operation raises a
  structured `network_not_allowed` refusal before opening a socket.
- Dataset URLs must be HTTPS on `datasets-server.huggingface.co` or
  `huggingface.co` only. GitHub archive URLs must be HTTPS on `github.com` or
  `codeload.github.com` only. URLs with credentials, non-default ports,
  control characters, or an unlisted host are rejected.
- Redirects are followed manually, at most three hops, and every hop is
  re-validated against the same host allow-list.
- Fetch requests use a bounded `limit` (default 10, maximum 100) and pages of
  at most 100 rows. Responses are capped per page (32 MiB for rows, 4 MiB for
  metadata) while streaming, with a 64 MiB total rows cap per fetch.
- GitHub archives are capped at 512 MiB compressed. Extraction is capped at
  2 GiB and 200,000 members.
- No provider credential, token, or cookie is read, sent, or stored.

Archive extraction is manual, not `extractall`:

- only regular files and directories are accepted;
- absolute paths, `..` traversal, backslashes, symlinks, hard links, devices,
  and FIFOs are rejected;
- every target is checked to stay under the extraction directory;
- the single GitHub top-level directory is stripped;
- files are created with `O_EXCL` (and `O_NOFOLLOW` where available);
- the archive is extracted into a temporary directory and moved into the cache
  only after success.

## What the SWE-bench tasks mean

For each row, the harness parses the gold code diff (`patch`) for changed file
paths from `diff --git a/x b/x` headers, falling back to `--- a/x` / `+++ b/x`
file headers. `/dev/null` sides are excluded. Paths are repository-relative,
deduplicated, and sorted. Test-file changes from `test_patch` are never used
as ground truth.

The retrieval task text is the row's `problem_statement`, bounded to 8,000
characters. The task id is the sanitized `instance_id`. A row is omitted, with
its reason recorded, when it has no problem statement, no parseable code files,
no repository/base commit, or no usable instance id.

`convert` writes:

- `tasks.json`: `{version, tasks:[{id, task, expected_files}]}`, compatible
  with `ozagia.eval.retrieval`;
- `sources.json`: task id → `{repo, base_commit, instance_id}`.

## Verify and aggregation

`verify` groups tasks by `(repo, base_commit)`, reuses a cached checkout when
its marker is valid, and otherwise downloads
`https://github.com/<repo>/archive/<base_commit>.tar.gz` after the explicit
network gate. A checkout failure is recorded as **not assessed** and does not
stop other repositories.

For each checkout it writes a per-repo tasks file under
`.../<dataset_sha>/runs/<owner>/<name>/<commit>/tasks.json` and calls
`ozagia.eval.retrieval.evaluate` with:

- `root` = the cached checkout;
- `tasks_path` = the per-repo tasks file;
- `--budgets` (default `500,900,1500`);
- `--arms` (default `baseline,code_search,dev_repo_map,grep`).

The default arms deliberately exclude `dev_context` and `both`, so verification
does not require a dev-memory graph snapshot.

The JSON report contains per-checkout and per-repository aggregates, overall
weighted recall and content-tier recall, task counts, budgets, universe misses,
the retrieval fingerprint, and provenance (dataset revision, rows SHA-256,
licence status, tasks/sources paths). The Markdown report is a readable
summary with the same provenance and limitations.

`--dry-run` performs conversion/materialisation planning, writes the task
files, and reports whether each checkout is cached or would be downloaded. It
never downloads and never evaluates.

## Scope and limitations

- **Retrieval, not patch correctness.** A hit means a gold-patch file was
  surfaced inside the budget. It says nothing about whether a generated patch
  would be correct or pass tests.
- **No target execution.** The harness never imports a target repository,
  runs its tests, installs dependencies, or evaluates a patch.
- **No model calls.** There is no model, agent, or LLM in this path.
- **Dataset revision drift.** Results are pinned to the cached dataset `sha`.
  Re-fetching a newer revision creates a separate cache directory; compare
  revisions explicitly.
- **Gold-file localisation only.** For SWE-bench, expected files are the files
  changed by the gold code patch. Test patches and the benchmark's
  `FAIL_TO_PASS` / `PASS_TO_PASS` lists are not used.
- **Large repositories.** Checkout and indexing time varies widely by
  repository. Token estimates are the retrieval modules' own heuristics, not a
  model tokenizer.
- **Missing files at the base commit.** `retrieval.evaluate` requires every
  expected file to exist in the checkout. `verify` filters expected files that
  are absent at the base commit and records the task/files as not assessed
  rather than scoring zero. This can happen for additions or unusual renames.
- **Source-universe misses.** `retrieval.evaluate` only indexes its source-file
  universe. Gold paths outside that universe (for example non-source files)
  are reported as universe misses and can lower recall.
- **Checkout failures are not scores.** A failed download, extraction, or
  evaluation is recorded as not assessed and included in the report counts.
- **Retrieval arms remain deterministic controls.** `baseline` is a sorted-path
  listing and `grep` is a lexical control; they are not semantic systems.

## Tests

The focused suite is offline:

```bash
.venv/bin/python -m pytest -q tests/test_eval_hf_benchmarks.py
.venv/bin/ruff check src/ozagia/eval/hf_benchmarks.py \
  tests/test_eval_hf_benchmarks.py
```

Tests cover the registry, network refusal before any opener call, import and
digest validation, patch parsing (`diff --git`, `/dev/null`, duplicates,
renames, unsafe paths), conversion omissions and the sources sidecar, safe
local tarball extraction, verify aggregation with monkeypatched evaluation, and
CLI exit/refusal behaviour. No test contacts the network.
