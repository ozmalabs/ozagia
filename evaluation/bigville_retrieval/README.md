# Bigville offline retrieval evaluation

This is an offline file-localisation evaluation for the read-only checkout at
`/applications/data/projects/bigville`. The task labels are frozen in
[`tasks.json`](tasks.json): each query names the expected Bigville source and
test files. The evaluation code operates on those paths and source text; it
does not import Bigville, run its code or tests, or write into that checkout.

The eight tasks cover session checkpoints, population reports, lazy
affordances, tick resource conflicts, seed graph recovery, backend decisions,
scenario loading, and public game construction. They were curated from
Bigville implementation and focused test files. They are a small convenience
sample, not a representative benchmark of the whole project.

## Reproduce

From the Ozagia checkout, the baseline arms are the sorted path listing,
lexical grep, code search, and repository map:

```sh
PYTHONPATH=src python3 -m ozagia.eval.retrieval \
  --root /applications/data/projects/bigville \
  --tasks evaluation/bigville_retrieval/tasks.json \
  --arms baseline,grep,code_search,dev_repo_map \
  --budgets 300,700,1300 \
  --cache /tmp/ozagia-bigville-eval/tags.json \
  --out-json evaluation/bigville_retrieval/results.json \
  --out-md evaluation/bigville_retrieval/RESULTS.md
```

For a controlled adapter comparison, build two fresh graph snapshots from the
same Bigville checkout state: one without the Bigville adapter and one with
it. Then use the same six arms and budgets for each run. The snapshots and tag
caches are written under `/tmp`:

```sh
PYTHONPATH=src python3 -m ozagia.core.memory build \
  --root /applications/data/projects/bigville \
  --out /tmp/ozagia-bigville-eval/graph-no-adapter.json

PYTHONPATH="$(pwd)/src:$(pwd)/addins" \
OZAGIA_ADAPTERS=bigville_adapter:register \
python3 -m ozagia.core.memory build \
  --root /applications/data/projects/bigville \
  --out /tmp/ozagia-bigville-eval/graph-with-adapter.json

PYTHONPATH=src python3 -m ozagia.eval.retrieval \
  --root /applications/data/projects/bigville \
  --tasks evaluation/bigville_retrieval/tasks.json \
  --arms baseline,dev_context,dev_repo_map,both,grep,code_search \
  --budgets 300,700,1300 \
  --cache /tmp/ozagia-bigville-eval/tags-no-adapter.json \
  --memory /tmp/ozagia-bigville-eval/graph-no-adapter.json \
  --out-json evaluation/bigville_retrieval/results_control_no_adapter.json \
  --out-md evaluation/bigville_retrieval/RESULTS_CONTROL_NO_ADAPTER.md

PYTHONPATH="$(pwd)/src:$(pwd)/addins" \
OZAGIA_ADAPTERS=bigville_adapter:register \
python3 -m ozagia.eval.retrieval \
  --root /applications/data/projects/bigville \
  --tasks evaluation/bigville_retrieval/tasks.json \
  --arms baseline,dev_context,dev_repo_map,both,grep,code_search \
  --budgets 300,700,1300 \
  --cache /tmp/ozagia-bigville-eval/tags-with-adapter.json \
  --memory /tmp/ozagia-bigville-eval/graph-with-adapter.json \
  --out-json evaluation/bigville_retrieval/results_control_with_adapter.json \
  --out-md evaluation/bigville_retrieval/RESULTS_CONTROL_WITH_ADAPTER.md
```

The graph build may report that optional tree-sitter and language-server
components could not be installed in a restricted environment. The observed
run completed with the available parsers; it did not require network access.
The snapshot records its source checkout HEAD, and the retrieval run checks
that the snapshot is fresh.

## Recorded runs

The final controlled run indexed 501 source files and completed 144 runs in
each condition. Both snapshots passed the freshness check and recorded
Bigville HEAD `e243ea70eee2a9ca5a55dfe62b9e29214f82cc87`. The adapter snapshot
included 14 `Scenario`, 391 `BigvilleSeedManifest`, and 36 `BigvilleRoute`
nodes. The no-adapter fingerprint is
`ca36fa55b631dee86ea30ee75fcb6a42b77c3a0e84f6ac0580b74508ac87904f`; the
adapter fingerprint is
`73d341d1cd3d5c9ebbabe987117ddc0f58a93e79d5f2af28b3c675260349f99c`.

Mean content recall at 300/700/1300 tokens (no adapter → adapter):

| Arm | 300 | 700 | 1300 |
|---|---:|---:|---:|
| `baseline` | 0 / 0 | 0 / 0 | 0 / 0 |
| `dev_context` | 0.125 / 0.063 | 0.188 / 0.125 | 0.063 / 0.063 |
| `dev_repo_map` | 0.292 / 0.292 | 0.292 / 0.292 | 0.333 / 0.333 |
| `both` | 0.292 / 0.292 | 0.354 / 0.354 | 0.396 / 0.396 |
| `grep` | 0 / 0 | 0 / 0 | 0 / 0 |
| `code_search` | 0.292 / 0.292 | 0.396 / 0.396 | 0.521 / 0.521 |

The adapter reduced `dev_context` content recall at 300 and 700 tokens, with
no difference at 1300 tokens. It did not change the union or any other arm's
content recall. The tasks mostly target source and test behavior, not scenario
to seed navigation, so this does not measure the adapter's intended graph
query and navigation capability. `both` uses the full budget for each
component, so its total rendered tokens can exceed the nominal budget.

## Adapter-focused retrieval probe

[`adapter_tasks.json`](adapter_tasks.json) adds six tasks for two
scenario-to-seed ambiguity examples, two generated-seed-to-pack-table
provenance examples, a duplicated seed dependency, and browser-to-server route
navigation. The expected files are the source evidence a caller needs after
following those graph relationships. This is a separate targeted set; it does
not replace the eight general source-localisation tasks. Reproduce it with the
same two-arm commands above, substituting `adapter_tasks.json` and writing to
`adapter_results_no_adapter.json` / `adapter_results_with_adapter.json`.

In the recorded probe, `dev_context` mean content recall at 300/700/1300 tokens
was 0.083/0.083/0.083 without the adapter and 0.406/0.578/0.494 with it. Both
runs passed freshness checks at the same Bigville HEAD. The adapter retrieval
command must set `PYTHONPATH` to include `addins` and
`OZAGIA_ADAPTERS=bigville_adapter:register`, as in the controlled command
above, so freshness checks use the same adapter watch set as the snapshot
build. Some expected files are JSON data outside the generic source-file
universe; the runner warns about them but scores a hit when the adapter returns
the exact path. This deliberately targeted six-task sample only suggests that
the adapter helps on these navigation queries; it is not a general retrieval
claim. The separate navigation assertions check that the captured graph
contains the intended links.

See [`RESULTS_CONTROL_NO_ADAPTER.md`](RESULTS_CONTROL_NO_ADAPTER.md) and
[`RESULTS_CONTROL_WITH_ADAPTER.md`](RESULTS_CONTROL_WITH_ADAPTER.md) for
per-task results and precision. The earlier separate exploratory runs remain
in [`RESULTS.md`](RESULTS.md), [`RESULTS_ADAPTER.md`](RESULTS_ADAPTER.md),
and their JSON reports.

## Adapter navigation assertions

The file-localisation task set does not score the adapter's primary graph
links. [`navigation_eval.py`](navigation_eval.py) reads an adapter-enabled
snapshot and makes six direct assertions against its nodes and edges:

1. `house_argument`'s `conversation` reference stays ambiguous and points to
   both the legacy and packaged manifest paths.
2. The generated council charter links to the declared `CHARTERS` table and
   preserves its recorded source hash.
3. A second scenario preserves ambiguity for two separate seed IDs.
4. A terrain movement seed links to its declared world table and hash.
5. A bell schedule dependency retains both manifest paths and marks the ID
   ambiguous.
6. The browser's `GET /api/management/v1/backend-profiles` request is joined
   to the explicit server handler, with both source files cited.

Run it against the adapter snapshot from the controlled comparison:

```sh
PYTHONPATH=src python3 evaluation/bigville_retrieval/navigation_eval.py \
  --graph /tmp/ozagia-bigville-eval/graph-with-adapter.json \
  --out evaluation/bigville_retrieval/navigation_results.json
```

The recorded snapshot contains 14 scenarios (3 with ambiguous seed IDs), 391
path-distinct seed manifests (211 IDs, 180 duplicated IDs), 31 manifests
declaring generation provenance, 27 generated-to-table edges, 7 unresolved
generated table declarations, 596 pack table nodes, and 36 route nodes. The
unresolved declarations name `LAWS` in four manifests and `POLICIES` in three;
they are visible in `unresolved_generated_tables`, not treated as linked
tables. All six assertions passed; their resolved paths,
method/path evidence and graph HEAD are in
[`navigation_results.json`](navigation_results.json). The adapter records a
generated hash as provenance; the assertion does not regenerate seeds or
verify that current table bytes match that hash. The route sample validates
one explicit server/client join, not full route coverage.

## Limits

- The general set has eight tasks and the adapter-focused set has six; both
  describe one captured Bigville checkout state.
- The query wording was authored after inspecting target files, which can
  favor lexical retrieval even though expected paths are not named in tasks.
- Retrieval recall is not evidence of successful code changes or correct
  simulation outcomes.
- The controlled runs used the same Bigville HEAD and task set. Bigville had
  uncommitted changes; the retrieval snapshots include working-tree content
  and the runner checks its working-tree digest as well as HEAD and root.
- The file-localisation query wording does not target the adapter graph links;
  the representative navigation checks cover only the six paths listed
  above.
- Timing and parser-cache measurements are specific to this run and are not
  performance claims.
