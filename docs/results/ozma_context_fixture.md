# Ozma context fixture benchmark

This offline fixture measures retrieval context for one synthetic checkout
task. It compares four retrieval arms at 100 and 300 estimated tokens:

| Arm | Meaning |
| --- | --- |
| `baseline` | Sorted source paths, truncated to budget |
| `grep` | Lexical task term ranking |
| `dev_context` | `DevMemory.context` over a prebuilt snapshot |
| `code_search` | Lexical code search rendered with snippets |

Relevant paths are fixed in `evaluation/ozma_context/tasks.json`. The fixture
does not contain Ozma code and does not measure the Ozma assembler, prompt
deduplication, a language model, task completion, or end-to-end quality. The
`dev_context` arm measures the underlying context operation; it does not time
the Ozma provider wrapper's freshness check. Token counts are ozagia's rough
character based estimates and do not include transport or prompt overhead.

Run from the ozagia checkout with the development environment installed:

```sh
tmp="$(mktemp -d)"
cp -R evaluation/ozma_context/fixture "$tmp/repo"
git -C "$tmp/repo" init -q
git -C "$tmp/repo" config user.email eval@example.test
git -C "$tmp/repo" config user.name 'Fixture Eval'
git -C "$tmp/repo" add .
git -C "$tmp/repo" commit -qm fixture
.venv/bin/ozagia index --root "$tmp/repo" --rebuild
.venv/bin/python -m ozagia.eval.retrieval \
  --root "$tmp/repo" \
  --tasks evaluation/ozma_context/tasks.json \
  --memory "$tmp/repo/.ozagia/graph.json" \
  --cache "$tmp/repo/.ozagia/eval_tags.json" \
  --budgets 100,300 \
  --arms baseline,grep,dev_context,code_search \
  --out-json "$tmp/cold.json" --out-md "$tmp/cold.md"
# Repeat the retrieval command, writing warm.json/warm.md, to observe cache reuse.
```

Index preparation is intentionally separate from retrieval: the fixture is
copied and indexed before either retrieval run. The first retrieval can warm
the tag cache; the repeat reports reuse. Elapsed times are machine dependent
and are diagnostic only. The checked-in fixture, explicit labels, selected
arms, and budgets make retrieval selection and estimated context reproducible.
No model calls or network access are made.

The focused regression test is `tests/test_ozma_context_fixture_eval.py`; it
checks that all four arms run against the fixture, honor their requested
budgets, and produce JSON serializable output. It does not assert timing.

## Optional actual assembler comparison

When a local Ozma backend checkout is available, the following separate
experiment exercises the real provider and `ozma.context.assembler.assemble`
without adding that checkout to ozagia's default test dependencies:

```sh
tmp="$(mktemp -d)"
cp -R evaluation/ozma_context/fixture "$tmp/repo"
git -C "$tmp/repo" init -q
git -C "$tmp/repo" config user.email eval@example.test
git -C "$tmp/repo" config user.name 'Fixture Eval'
git -C "$tmp/repo" add .
git -C "$tmp/repo" commit -qm fixture
.venv/bin/ozagia index --root "$tmp/repo" --rebuild
OZAGIA_ROOT="$tmp/repo" PYTHONPATH="../ozma-ai/backend" .venv/bin/python \
  evaluation/ozma_context/compare_assembler.py \
  --root "$tmp/repo" --ozma-backend ../ozma-ai/backend \
  --budgets 100,300 > "$tmp/four-arms.json"
```

The four arms are `plain` (no retrieved context), `ozagia` (the versioned
provider alone), `ozma` (the Ozma assembler with the integration disabled),
and `combined` (the Ozma assembler with the integration enabled). The output
includes context size estimates, named expected-path recall, and diagnostic
elapsed times. Each run creates a fresh SQLite session store and uses the
prebuilt Ozagia graph snapshot; it does not model Ozma startup or indexing
preparation cost. Record both checkout revisions with any published output.
The Ozma checkout and its optional dependencies can change
assembler behavior, so this remains an opt-in experiment rather than a
default-suite test. It measures selected context and path mentions only, not
tokenizer parity, coding performance, or end-to-end quality. The prompt is
chosen to avoid the assembler's prefetch triggers; no model or network call is
made.

## Cold and warm preparation wrapper

To create a disposable fixture, time cold index construction and a subsequent
warm snapshot check, and run the four-arm comparison in one command:

```sh
.venv/bin/python evaluation/ozma_context/run_fixture.py \
  --ozma-backend ../ozma-ai/backend > /tmp/ozagia-ozma-eval.json
```

The JSON records both Git revisions and notes that these do not capture dirty
working-tree changes. A local run on 2026-11-16 measured 0.185 seconds for
cold preparation and 0.138 seconds warm, including process startup. At the
300-token setting the fixture named 0/3 expected paths in the plain arm, 2/3
in Ozagia, 0/3 in Ozma, and 3/3 in the combined arm; the arms are not
equal-cost controls. These are a single synthetic fixture and machine, not
evidence of task completion or general performance.

An additional `ozagia.eval.bench --preset ozagia --cold --check` run on this
working tree (191 cached files) completed cold tag indexing in 0.596 seconds,
with median warm repo-map queries at 0.015 seconds and code-search queries at
0.230 seconds; all configured timing gates passed. It is a point measurement,
not a large-repository scalability study. It does not by itself establish a
need for a watcher or a different storage backend.

For a larger reference point, an offline fake-driver Ocelot task at base
`54e5eb5fae4592444136b9d70fde08e94cb8a914` took 50.8 seconds to prepare the
graph-intent arm and left about 84 MiB of logs and index artifacts. The fake
run exercised setup, not a model; filesystem cache and machine load were not
controlled. The Ocelot retrieval report describes approximately 6,800 source
files. This is enough to prioritize reusing prepared snapshots and measuring
incremental refresh on larger projects; it is not sufficient to choose a
watcher, database engine, or a production performance target.
