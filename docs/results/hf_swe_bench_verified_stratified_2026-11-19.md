# Hugging Face SWE-bench Verified stratified retrieval baseline (2026-11-19)

**Status: bounded stratified baseline, not a representative SWE-bench score.**
This run verifies Ozagia retrieval against real SWE-bench gold files across
multiple repositories, using a deterministic sample rather than a random
sample.

## Source

- Dataset: `SWE-bench/SWE-bench_Verified`
- Revision: `78f471bf655a3137b2e8a75af1501690ec009ec3`
- Config/split: `default` / `test`
- Licence: not stated on the dataset card or tags; recorded as `unknown`
- Selection: first two task IDs per repository across the 12 repositories in
  the dataset, giving 23 tasks. This is stratified for coverage, not random.
- All 23 tasks were assessed. No task was omitted or refused.

Repositories: astropy, Django, matplotlib, seaborn, Flask, requests, xarray,
pylint, pytest, scikit-learn, Sphinx and SymPy.

## Command

```bash
python -m ozagia.eval.hf_benchmarks verify \
  --benchmark swe-bench-verified --allow-network \
  --tasks /tmp/opencode/ozagia-hf-stratified/tasks.json \
  --sources /tmp/opencode/ozagia-hf-stratified/sources.json \
  --limit 100 --budgets 500,900,1500 \
  --arms baseline,code_search,dev_repo_map,grep \
  --cache-dir /tmp/opencode/ozagia-hf-stratified-cache \
  --out-json /tmp/opencode/ozagia-hf-stratified/verify.json \
  --out-md /tmp/opencode/ozagia-hf-stratified/verify.md
```

## Results

Mean over 23 assessed tasks. `Content` counts the gold file only when actual
code was shown, not merely a path. Precision is over selected files. Evidence
coverage is the share of unique task terms present in the rendered text;
density is those terms per 1000 tokens.

| Arm | Budget | Recall | Content recall | Precision | Evidence coverage | Evidence symbols | Evidence/1k | Budget respected |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `baseline` | 500 | 0.261 | 0.000 | 0.005 | 0.119 | 0.0 | 49.28 | 23/23 |
| `code_search` | 500 | 0.812 | 0.812 | 0.049 | 0.434 | 93.4 | 332.20 | 23/23 |
| `dev_repo_map` | 500 | **0.935** | **0.899** | 0.087 | 0.193 | 10.9 | 90.07 | 23/23 |
| `grep` | 500 | 0.891 | 0.000 | 0.019 | 0.146 | 0.0 | 59.56 | 23/23 |
| `baseline` | 900 | 0.522 | 0.000 | 0.007 | 0.141 | 0.0 | 38.88 | 23/23 |
| `code_search` | 900 | 0.891 | 0.891 | 0.030 | 0.502 | 151.6 | 275.57 | 23/23 |
| `dev_repo_map` | 900 | **0.967** | **0.957** | 0.068 | 0.338 | 17.7 | 104.18 | 23/23 |
| `grep` | 900 | 0.935 | 0.000 | 0.012 | 0.181 | 0.0 | 49.65 | 23/23 |
| `baseline` | 1500 | 0.565 | 0.000 | 0.006 | 0.166 | 0.0 | 33.34 | 23/23 |
| `code_search` | 1500 | 0.913 | 0.913 | 0.024 | 0.546 | 190.4 | 248.56 | 23/23 |
| `dev_repo_map` | 1500 | **0.967** | **0.967** | 0.059 | 0.387 | 24.6 | 104.05 | 23/23 |
| `grep` | 1500 | 0.957 | 0.000 | 0.009 | 0.204 | 0.0 | 40.97 | 23/23 |

`dev_repo_map` rows above are the content-aware, confidence-gated run. Before
content fusion its recall/content was 0.261/0.261 at 500, 0.406/0.406 at 900
and 0.587/0.587 at 1500; it also exceeded the 500-token budget in 10 of 23
runs. The improvement comes from lexical candidate fusion plus a bounded map
header and cheaper tiers, not from increasing the token budget.

The first content-aware version raised recall to 0.870/0.935/0.978 but spent
its budget on an 80-96 file tail, so precision fell to 0.074/0.034/0.016. The
current version keeps 14/20/22 files. The confidence gate trades 1.1 recall
points at 900 and 1500 for +39% and +28% precision. With line-IDF weighting,
density rises 3%/8%/6% (87.23→90.07, 96.67→104.18, 98.24→104.05) while
evidence coverage stays effectively flat.

## What this says

- `dev_repo_map` has the highest file and content recall at every budget:
  0.935/0.899 at 500, 0.967/0.957 at 900 and 0.967/0.967 at 1500. It stays
  within budget in every run.
- `code_search` has the highest evidence density (332.20/275.57/248.56 per 1k
  tokens) because every delivered line is a query snippet and it exposes many
  matched definitions (93/152/190 symbols). The map trades some density for
  structure: paths, definitions and a broader candidate set, with 11/18/25
  matched symbols.
- The confidence gate lifts precision from 0.080/0.049/0.046 to
  0.087/0.068/0.059. At 900 and 1500 it costs 1.1 recall points, while
  evidence coverage stays flat (0.192→0.193, 0.332→0.338, 0.391→0.387) and
  density rises 3%/8%/6% (87.23→90.07, 96.67→104.18, 98.24→104.05). The
  removed files were low-confidence and redundant by task terms.
- Evidence density is unit-scaled: term weights use line IDF and one matched
  definition counts as one unit. Compare arms within this table, not against
  the earlier unweighted density numbers.
- `grep` finds the right file path often (0.957 at 1500) but never shows
  content, so it is a file-listing baseline, not a context-delivery result.
- Flat precision is bounded by the gold set size: a task with one expected
  file can score at most `1 / selected_files`, so 14-22 files cannot yield
  high flat precision. The map is a ranked reading list, not a one-file
  shortlist.
- `baseline` sorted listing improves with budget but still delivers no content
  and much lower file recall than the 1500-token `grep`/`code_search` arms.

## Held-out check

The 100 rows after the stratified sample are all Django tasks. Ten were
materialised and evaluated with the same command; they were not used to tune
the ranking:

| Budget | Recall | Content recall | Precision | Evidence coverage | Evidence symbols | Evidence/1k | Budget respected |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 500 | 0.900 | 0.700 | 0.068 | 0.263 | 12.5 | 114.42 | 10/10 |
| 900 | 0.900 | 0.900 | 0.054 | 0.405 | 19.4 | 111.19 | 10/10 |
| 1500 | 0.900 | 0.900 | 0.048 | 0.415 | 25.1 | 103.33 | 10/10 |

The confidence gate costs one partial hit in this ten-task holdout at 900 and
1500 (recall 1.000 before the gate). `min_confidence=0` restores the recall-first
behavior. This is a single-repository holdout, so it checks task-to-task
generalization, not cross-repository generalization. The 23-task suite covers
the latter.

## Repo-map change

`dev_repo_map` previously ranked only paths and symbol names. The new path:

1. takes bounded BM25 content candidates from `code_search` (80 results);
2. adds their files to the PageRank candidate subgraph;
3. scores exact definition names and path segments above substring hits, and
   demotes tests/docs/vendored files;
4. fuses graph order and lexical order with confidence-weighted rank fusion,
   where each signal's contribution decays exponentially with rank;
5. bounds the task header to 200 characters;
6. keeps candidates above 18% of the top fused score, capped at 32 files;
7. below the confidence line (35% of the top score at small budgets, 30%
   above), keeps a file only when it adds task evidence not already covered;
8. renders one full body, more compact outlines, matching-line snippets for
   lexical hits when budget permits, then cheap listings.

`repo_map(..., content_search=False)` retains the graph/name-only behavior;
`min_confidence=0` disables the evidence gate.

## Limits

- 23 deliberately selected tasks from 12 repositories are not the full
  500-instance dataset and are not a random sample.
- This measures gold-file localization only. It does not run the target tests,
  apply the gold patch, execute project code, call a model, or establish
  SWE-bench resolution rate.
- Token estimates are the retrieval evaluator's bounded estimates, not a
  provider tokenizer.
- The dataset has no stated licence in its Hugging Face metadata; verify terms
  before redistributing rows or derived results.
- Downloaded rows and checkouts remain under the cache directory, not this
  repository.
