# `ozagia-eval-benchmarks`

> **Generated file.** Built by `scripts/docs/generate_docs.py` from `src/ozagia/eval/hf_benchmarks.py` (ozagia `0.1.5.dev0`). Do not edit by hand: change the source code and regenerate.

This command's module does not expose a `build_parser()`;
its usage synopsis comes from the module docstring:

```
Hugging Face benchmark ingestion and retrieval verification.

This module turns public benchmark datasets (currently SWE-bench Verified and
SWE-bench Lite) into the explicit task format consumed by
:mod:`ozagia.eval.retrieval`, and verifies which files Ozagia's retrieval arms
surface for each issue.

It is deliberately narrow:

* Network access is opt-in (``allow_network=True``). Without it the module
  refuses before opening a socket. ``fetch`` downloads bounded dataset pages;
  ``verify`` downloads GitHub archive tarballs only for repositories that are
  not already cached.
* Nothing here executes target repository code, runs tests, calls a model, or
  claims that an agent fixed an issue. The measured question is file
  localisation: do the gold patch's files appear inside a token budget?
* Data is cached under ``<state>/eval/hf/<benchmark>/``. Raw rows and their
  metadata are written atomically, with the dataset revision in the path so a
  later dataset update cannot silently mix revisions.

Cache layout::

    <state>/eval/hf/<benchmark>/<dataset_sha>/rows.json
    <state>/eval/hf/<benchmark>/<dataset_sha>/meta.json
    <state>/eval/hf/<benchmark>/<dataset_sha>/tasks.json
    <state>/eval/hf/<benchmark>/<dataset_sha>/sources.json
    <state>/eval/hf/<benchmark>/<dataset_sha>/runs/<owner>/<name>/<commit>/...
    <state>/eval/hf/<benchmark>/repos/<owner>/<name>/<commit>/...

``--cache-dir`` replaces the ``<state>/eval/hf`` prefix. Tests use local rows
and local tarballs, never the network.

CLI::

    python -m ozagia.eval.hf_benchmarks list
    python -m ozagia.eval.hf_benchmarks fetch --benchmark swe-bench-verified \
        --allow-network --limit 10
    python -m ozagia.eval.hf_benchmarks import --benchmark swe-bench-verified \
        tests/fixtures/hf_benchmarks/rows_verified.json
    python -m ozagia.eval.hf_benchmarks convert --benchmark swe-bench-verified
    python -m ozagia.eval.hf_benchmarks verify --benchmark swe-bench-verified \
        --allow-network --budgets 500,900,1500
```
