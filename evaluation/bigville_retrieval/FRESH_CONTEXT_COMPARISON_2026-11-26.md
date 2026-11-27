# Fresh Bigville context comparison (2026-11-26)

This note records a same-state comparison of `dev_context` with and without
the Bigville adapter. It supersedes the older exploratory adapter-task figures
for freshness, but does not replace the existing broader arm reports.

## Inputs and commands

Both graph snapshots were rebuilt from `/applications/data/projects/bigville`
and record Bigville HEAD
`e243ea70eee2a9ca5a55dfe62b9e29214f82cc87`. Retrieval ran with freshness
checks enabled and `stale=false`. The adapter was registered during both its
graph build and retrieval run so the adapter snapshot passed the same
freshness check.

```sh
PYTHONPATH=src python3 -m ozagia.core.memory build \
  --root /applications/data/projects/bigville \
  --out /tmp/ozagia-bigville-eval/fresh-no-adapter.json

PYTHONPATH=src:addins OZAGIA_ADAPTERS=bigville_adapter:register \
python3 -m ozagia.core.memory build \
  --root /applications/data/projects/bigville \
  --out /tmp/ozagia-bigville-eval/fresh-with-adapter.json
```

The same retrieval command was run for each snapshot and task set, changing
only `--tasks`, `--memory`, and output names:

```sh
PYTHONPATH=src python3 -m ozagia.eval.retrieval \
  --root /applications/data/projects/bigville \
  --tasks evaluation/bigville_retrieval/tasks.json \
  --arms dev_context --budgets 300,700,1300 \
  --cache /tmp/ozagia-bigville-eval/tags-no-adapter.json \
  --memory /tmp/ozagia-bigville-eval/fresh-no-adapter.json \
  --out-json evaluation/bigville_retrieval/results_fresh_general_no_adapter.json \
  --out-md /tmp/ozagia-bigville-eval/unfiltered-general-no.md

PYTHONPATH=src:addins OZAGIA_ADAPTERS=bigville_adapter:register \
python3 -m ozagia.eval.retrieval \
  --root /applications/data/projects/bigville \
  --tasks evaluation/bigville_retrieval/tasks.json \
  --arms dev_context --budgets 300,700,1300 \
  --cache /tmp/ozagia-bigville-eval/tags-with-adapter.json \
  --memory /tmp/ozagia-bigville-eval/fresh-with-adapter.json \
  --out-json evaluation/bigville_retrieval/results_fresh_general_with_adapter.json \
  --out-md /tmp/ozagia-bigville-eval/unfiltered-general-yes.md

PYTHONPATH=src python3 -m ozagia.eval.retrieval \
  --root /applications/data/projects/bigville \
  --tasks evaluation/bigville_retrieval/adapter_tasks.json \
  --arms dev_context --budgets 300,700,1300 \
  --cache /tmp/ozagia-bigville-eval/tags-no-adapter.json \
  --memory /tmp/ozagia-bigville-eval/fresh-no-adapter.json \
  --out-json evaluation/bigville_retrieval/results_fresh_adapter_no_adapter.json \
  --out-md /tmp/ozagia-bigville-eval/unfiltered-adapter-no.md

PYTHONPATH=src:addins OZAGIA_ADAPTERS=bigville_adapter:register \
python3 -m ozagia.eval.retrieval \
  --root /applications/data/projects/bigville \
  --tasks evaluation/bigville_retrieval/adapter_tasks.json \
  --arms dev_context --budgets 300,700,1300 \
  --cache /tmp/ozagia-bigville-eval/tags-with-adapter.json \
  --memory /tmp/ozagia-bigville-eval/fresh-with-adapter.json \
  --out-json evaluation/bigville_retrieval/results_fresh_adapter_with_adapter.json \
  --out-md /tmp/ozagia-bigville-eval/unfiltered-adapter-yes.md
```

JSON reports are saved beside this note. The graph snapshots, tag caches,
and rendered per-run Markdown reports remain under `/tmp/ozagia-bigville-eval/`.

## Results

Mean recall and content recall are equal for these `dev_context` runs.

| Task set | Adapter | Tasks | Fingerprint | Recall at 300 | Recall at 700 | Recall at 1300 |
|---|---|---:|---|---:|---:|---:|
| General | Off | 8 | `543a908010c515fb118d1ee9e3d277cfd720564fca46c5e3734a9b6c7a42e2c2` | 0.125 | 0.1875 | 0.0625 |
| General | On | 8 | `a65713c36929707582982a8e5e2c2a6ca356d872533189e00e2629479665aec8` | 0.0625 | 0.125 | 0.0625 |
| Adapter-focused | Off | 3 | `12f42cc763a2ddc6c324ee358b14706815a69c11994f9cbac75f8466f44fcaa0` | 0.277778 | 0.333333 | 0.277778 |
| Adapter-focused | On | 3 | `68a528b99c984759817d5665d98562e8e4bbedc62486d25c1bb0c668c1555557` | 0.5 | 0.666667 | 0.5 |

For the general `seed-manifest-recovery` task, context without the adapter
includes `bigville/runtime.py` at 300 and 700 tokens; context with the adapter
does not. The adapter-focused set shows higher mean recall with the adapter
at every tested budget.

## Limits

- The general task set has eight hand-curated queries; the adapter-focused
  set has three. These results do not establish performance across other
  projects or query styles.
- Six adapter-focused expected JSON files are outside the generic source-file
  universe. The evaluator still scores exact returned paths, but this limits
  comparability with the normal source-only tasks.
- The Bigville working tree was captured at the same recorded HEAD in both
  fresh snapshots. Freshness validation passed, but this is one working-tree
  state and not a repeated-run variance study.
- Optional tree-sitter and language-server components could not be installed
  in the restricted environment. Both builds completed with the available
  parsers; parser availability and timings are not benchmark claims.
- Retrieval measures selected paths and content exposure, not task completion
  or correctness of changes made using the graph.
