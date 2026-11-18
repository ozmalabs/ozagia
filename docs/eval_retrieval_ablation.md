# Offline retrieval ablations

`ozagia.eval.retrieval_ablation` compares two candidate scopes and two ranking
orders using `code_search` only:

- `whole_repo`: every source file returned by the repo-map source-file
  enumerator;
- `diff`: the task's explicit `diff_files` candidate list;
- `aware`: code_search's BM25 plus PageRank RRF ordering;
- `less`: the same code_search result records ordered by BM25 rank alone.

Each task must freeze `expected_files` and `diff_files` in its JSON record.
The diff list represents the files changed in the hypothetical task, including
any chosen distractors. The harness does not run git, inspect a patch, or infer
which files changed. Both scopes use the same task wording, token budget,
snippet renderer, and file-level recall/precision measures. Graph-less ordering
is derived from each search call's complete result set, so this comparison
isolates the ranking contribution; it does not remove graph construction or
the repo-map parser from execution cost.

Example task record:

```json
{
  "id": "retry-budget",
  "task": "Find the retry budget calculation",
  "expected_files": ["src/retry.py"],
  "diff_files": ["src/retry.py", "tests/test_retry.py"]
}
```

Run an offline evaluation with an explicitly prepared task set:

```bash
.venv/bin/python -m ozagia.eval.retrieval_ablation \
  --root /path/to/fixture \
  --tasks /path/to/tasks.json \
  --budgets 500,900 \
  --out /tmp/retrieval-ablation.json
```

The report contains every selected file, its hits, recall, precision, estimated
tokens, aggregate means, and a deterministic fingerprint. It performs no
model calls, network access, edits, or tests. Tests use a small local fixture.

## Limits

This is a file retrieval ablation, not an end-to-end code-change evaluation.
`diff_files` are curated candidate sets rather than observed git diffs, so
results measure retrieval under a known scope and can overstate performance if
the list was authored with knowledge of the answer. The graph-less rank still
uses the search index and graph cache to obtain the same candidate records;
only the final ranking contribution is removed. The evaluator does not measure
the cost of independently building BM25 versus graph indexes, edit quality,
test outcomes, or agent behavior. Small task sets are descriptive evidence,
not statistical proof.
