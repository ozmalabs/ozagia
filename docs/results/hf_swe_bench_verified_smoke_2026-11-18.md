# Hugging Face SWE-bench Verified smoke (2026-11-18)

**Status: bounded live smoke, not a benchmark claim.** These are three
instances from one repository, used to verify the Hugging Face ingestion and
retrieval-verification path end to end.

## Source

- Dataset: `SWE-bench/SWE-bench_Verified`
- Revision: `78f471bf655a3137b2e8a75af1501690ec009ec3`
- Config/split: `default` / `test`
- Licence: not stated on the dataset card or tags; recorded as `unknown`
- Task scope: gold changed-file localization only. No target tests, target
  code, or patches were executed, and no model was called.

## Commands

```bash
python -m ozagia.eval.hf_benchmarks fetch \
  --benchmark swe-bench-verified --allow-network --limit 3 \
  --cache-dir /tmp/opencode/ozagia-hf-bench
python -m ozagia.eval.hf_benchmarks convert \
  --benchmark swe-bench-verified --cache-dir /tmp/opencode/ozagia-hf-bench
python -m ozagia.eval.hf_benchmarks verify \
  --benchmark swe-bench-verified --allow-network --limit 3 \
  --budgets 500,1500 --cache-dir /tmp/opencode/ozagia-hf-bench \
  --out-json /tmp/opencode/hf-verify.json \
  --out-md /tmp/opencode/hf-verify.md
```

The three tasks were `astropy__astropy-12907`, `astropy__astropy-13033` and
`astropy__astropy-13236`, all from `astropy/astropy`, at three base commits.
All three checkouts were assessed; none were refused or omitted.

## Results

Mean recall over three assessed tasks:

| Arm | Budget 500 | Budget 1500 | Content recall @1500 |
|---|---:|---:|---:|
| `baseline` | 0.000 | 0.000 | 0.000 |
| `code_search` | 0.667 | 0.667 | 0.667 |
| `dev_repo_map` | 0.333 | 0.667 | 0.667 |
| `grep` | 0.667 | 0.667 | 0.000 |

`dev_repo_map` respected the 500-token budget in only one of three runs; the
mean at 500 is therefore based on a mixed set. At 1500, all three runs respected
the budget. `grep` selected the expected file at the file-listing tier but not
at the content tier; `code_search` and `dev_repo_map` surfaced content.

The source universe contained no missing expected files. All three tasks had
gold files present at their base commit.

## Limits

- Three tasks from one repository are not a representative SWE-bench sample.
- File recall is a prerequisite for a fix; it is not patch correctness or
  agent success.
- The dataset has no stated licence in its Hugging Face metadata; verify terms
  before redistributing rows or derived results.
- Downloaded rows and checkouts remain under the cache directory, not this
  repository.
