# Bigville context ranking comparison (2026-11-27)

This note compares the general `dev_context` ranking before and after switching
term document frequency to a per-node-type corpus. It uses fresh, identical
Bigville graph snapshots for both code states and reports the separate
adapter-focused task set as a tradeoff check.

## Graph snapshots

Both snapshots were rebuilt from `/applications/data/projects/bigville` and
record HEAD `e243ea70eee2a9ca5a55dfe62b9e29214f82cc87`. The retrieval reports
record `stale=false` for all four snapshot/task combinations. The adapter was
registered while building and querying the adapter snapshot.

```sh
PYTHONPATH=src python3 -m ozagia.core.memory build \
  --root /applications/data/projects/bigville \
  --out /tmp/ozagia-bigville-eval/fresh-no-adapter.json

PYTHONPATH=src:addins OZAGIA_ADAPTERS=bigville_adapter:register \
python3 -m ozagia.core.memory build \
  --root /applications/data/projects/bigville \
  --out /tmp/ozagia-bigville-eval/fresh-with-adapter.json
```

## Ranking code states

- **Pre** used global document frequency across every graph node type. It
  included the ambiguous single-word anchor refinement and empty-snippet
  handling.
- **Post** computes term frequency within each node type. Added adapter types
  such as `BigvilleSeedItem`, `PackTable`, and `Scenario` therefore do not
  alter the label rarity scores used to rank `Symbol`, `Test`, and other types.
  It retains the same anchor and empty-snippet behavior.

Each code state was evaluated against both snapshots with the same task files,
arms, and budgets. The pre commands ran before the type-local IDF change; the
post commands ran after it.

```sh
# Pre, global DF
PYTHONPATH=src python3 -m ozagia.eval.retrieval \
  --root /applications/data/projects/bigville \
  --tasks evaluation/bigville_retrieval/tasks.json \
  --arms dev_context --budgets 300,700,1300 \
  --cache /tmp/ozagia-bigville-eval/tags-no-adapter.json \
  --memory /tmp/ozagia-bigville-eval/fresh-no-adapter.json \
  --out-json evaluation/bigville_retrieval/results_ranking_pre_general_no_adapter.json \
  --out-md /tmp/ozagia-bigville-eval/final-general-no.md

PYTHONPATH=src:addins OZAGIA_ADAPTERS=bigville_adapter:register \
python3 -m ozagia.eval.retrieval \
  --root /applications/data/projects/bigville \
  --tasks evaluation/bigville_retrieval/tasks.json \
  --arms dev_context --budgets 300,700,1300 \
  --cache /tmp/ozagia-bigville-eval/tags-with-adapter.json \
  --memory /tmp/ozagia-bigville-eval/fresh-with-adapter.json \
  --out-json evaluation/bigville_retrieval/results_ranking_pre_general_with_adapter.json \
  --out-md /tmp/ozagia-bigville-eval/final-general-yes.md

PYTHONPATH=src python3 -m ozagia.eval.retrieval \
  --root /applications/data/projects/bigville \
  --tasks evaluation/bigville_retrieval/adapter_tasks.json \
  --arms dev_context --budgets 300,700,1300 \
  --cache /tmp/ozagia-bigville-eval/tags-no-adapter.json \
  --memory /tmp/ozagia-bigville-eval/fresh-no-adapter.json \
  --out-json evaluation/bigville_retrieval/results_ranking_pre_adapter_no_adapter.json \
  --out-md /tmp/ozagia-bigville-eval/final-adapter-no.md

PYTHONPATH=src:addins OZAGIA_ADAPTERS=bigville_adapter:register \
python3 -m ozagia.eval.retrieval \
  --root /applications/data/projects/bigville \
  --tasks evaluation/bigville_retrieval/adapter_tasks.json \
  --arms dev_context --budgets 300,700,1300 \
  --cache /tmp/ozagia-bigville-eval/tags-with-adapter.json \
  --memory /tmp/ozagia-bigville-eval/fresh-with-adapter.json \
  --out-json evaluation/bigville_retrieval/results_ranking_pre_adapter_with_adapter.json \
  --out-md /tmp/ozagia-bigville-eval/final-adapter-yes.md

# Post, type-local DF
PYTHONPATH=src python3 -m ozagia.eval.retrieval \
  --root /applications/data/projects/bigville \
  --tasks evaluation/bigville_retrieval/tasks.json \
  --arms dev_context --budgets 300,700,1300 \
  --cache /tmp/ozagia-bigville-eval/tags-no-adapter.json \
  --memory /tmp/ozagia-bigville-eval/fresh-no-adapter.json \
  --out-json evaluation/bigville_retrieval/results_ranking_post_general_no_adapter.json \
  --out-md /tmp/ozagia-bigville-eval/typedidf-general-no.md

PYTHONPATH=src:addins OZAGIA_ADAPTERS=bigville_adapter:register \
python3 -m ozagia.eval.retrieval \
  --root /applications/data/projects/bigville \
  --tasks evaluation/bigville_retrieval/tasks.json \
  --arms dev_context --budgets 300,700,1300 \
  --cache /tmp/ozagia-bigville-eval/tags-with-adapter.json \
  --memory /tmp/ozagia-bigville-eval/fresh-with-adapter.json \
  --out-json evaluation/bigville_retrieval/results_ranking_post_general_with_adapter.json \
  --out-md /tmp/ozagia-bigville-eval/typedidf-general-yes.md

PYTHONPATH=src python3 -m ozagia.eval.retrieval \
  --root /applications/data/projects/bigville \
  --tasks evaluation/bigville_retrieval/adapter_tasks.json \
  --arms dev_context --budgets 300,700,1300 \
  --cache /tmp/ozagia-bigville-eval/tags-no-adapter.json \
  --memory /tmp/ozagia-bigville-eval/fresh-no-adapter.json \
  --out-json evaluation/bigville_retrieval/results_ranking_post_adapter_no_adapter.json \
  --out-md /tmp/ozagia-bigville-eval/typedidf-adapter-no.md

PYTHONPATH=src:addins OZAGIA_ADAPTERS=bigville_adapter:register \
python3 -m ozagia.eval.retrieval \
  --root /applications/data/projects/bigville \
  --tasks evaluation/bigville_retrieval/adapter_tasks.json \
  --arms dev_context --budgets 300,700,1300 \
  --cache /tmp/ozagia-bigville-eval/tags-with-adapter.json \
  --memory /tmp/ozagia-bigville-eval/fresh-with-adapter.json \
  --out-json evaluation/bigville_retrieval/results_ranking_post_adapter_with_adapter.json \
  --out-md /tmp/ozagia-bigville-eval/typedidf-adapter-yes.md
```

The eight retained JSON artifacts are:

- Pre: [general, no adapter](results_ranking_pre_general_no_adapter.json),
  [general, adapter](results_ranking_pre_general_with_adapter.json),
  [adapter tasks, no adapter](results_ranking_pre_adapter_no_adapter.json),
  [adapter tasks, adapter](results_ranking_pre_adapter_with_adapter.json).
- Post: [general, no adapter](results_ranking_post_general_no_adapter.json),
  [general, adapter](results_ranking_post_general_with_adapter.json),
  [adapter tasks, no adapter](results_ranking_post_adapter_no_adapter.json),
  [adapter tasks, adapter](results_ranking_post_adapter_with_adapter.json).

Fingerprints: pre general `914aa23543babd258df7139255b06e67febaaa26de5ceeb886863c6927974817`
(no adapter), `5ad11de6abf7098a6ec7b0cffab2ea959d0820b22996fbfbad603895395fd763`
(adapter); pre adapter-focused `1cc6372b4d1ca45b4a464cfddbe372717eb9bf9835e218f955f7f81720b07408`
(no adapter), `b476177d7a258e27406ab4a41dce72a00381bcd404ed528871c887d74f17cba5`
(adapter). Post general `477974eb5aa3175fcff728c0d3b5276c48b861c2ad125eb2889660a73c024a4d`
(no adapter), `bb7fe1700059e19f5e8467e62ca695d2c229b6136729d0286d100365c11c9b78`
(adapter); post adapter-focused `5d6dc9320eed7953034cbc72579533c1ad46ce60fa314f0b2c500a8e846598f4`
(no adapter), `9e6652511f34a36062dd539a2d0d744ebc092a802712e42f3ba8c620c6d74a9e`
(adapter).

## Mean content recall

| Task set | Ranking | Adapter | Tasks | 300 tokens | 700 tokens | 1300 tokens |
|---|---|---|---:|---:|---:|---:|
| General | Pre, global DF | Off | 8 | 0.125 | 0.1875 | 0.0625 |
| General | Pre, global DF | On | 8 | 0.0625 | 0.1875 | 0.0625 |
| General | Post, type-local DF | Off | 8 | 0.166667 | 0.1875 | 0.0625 |
| General | Post, type-local DF | On | 8 | 0.125 | 0.1875 | 0.125 |
| Adapter-focused | Pre, global DF | Off | 5 | 0.100 | 0.200 | 0.100 |
| Adapter-focused | Pre, global DF | On | 5 | 0.420 | 0.520 | 0.553333 |
| Adapter-focused | Post, type-local DF | Off | 5 | 0.100 | 0.200 | 0.100 |
| Adapter-focused | Post, type-local DF | On | 5 | 0.420 | 0.626667 | 0.526667 |

In the general set, the adapter-enabled 700-token regression disappears; the
post ranking also raises adapter-enabled recall at 1300 tokens. The adapter
enabled general run remains 0.041667 below its no-adapter counterpart at 300
tokens. In the adapter-focused set, post ranking improves the 700-token score
and lowers the 1300-token score by 0.026666 relative to pre ranking. The
adapter remains substantially ahead of no-adapter on this targeted set.

For `seed-manifest-recovery`, post ranking selects `bigville/runtime.py` with
the adapter at all three budgets; pre ranking selected it at 700 but missed it
at 300 and 1300.

## Why the change is supported

On the general `seed-manifest-recovery` query, the global-DF run without the
adapter ranks `load_seed_manifest` second in fuzzy search, while the adapter
run ranks it third after a matching test. Adapter nodes alter global term
frequencies and shift that Test/Symbol ordering. Type-local frequencies make
the ranking of existing Test and Symbol labels invariant to growth in unrelated
node types. The synthetic test
`test_search_ranking_is_stable_when_another_node_type_grows` adds 100
`AdapterNoise` nodes and verifies that existing File/Test result ordering is
unchanged.

## Limitations

- The general set contains eight curated tasks; the adapter-focused set has
  five. These are small task samples, not broad retrieval benchmarks.
- Eleven adapter-focused expected files are outside the generic source-file
  universe. The evaluator scores exact returned paths, but this limits
  comparison with source-only tasks.
- Type-local DF changes score scales between node types. Recall and precision
  are reported, but cross-type score calibration has not been evaluated as a
  separate objective.
- The Bigville working tree and recorded HEAD are one captured state. Timing,
  parser availability, and change quality are outside this comparison.
