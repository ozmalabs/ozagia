# Family-aware federation retrieval check

This is a small paired offline check of the opt-in
`federation.search(..., family_boost=True)` arm. It creates three temporary
member snapshots under a private temporary directory (anchor, declared family
member and an unrelated distractor), declares one family in the anchor's
manifest, and runs the same frozen queries with the arm off and on.

It is a fixture-level mechanism check, not a retrieval benchmark. The family
queries are constructed with a 1/7 lexical gap that the bounded `0.15` boost can
close; one control query is already lexically decisive. The default remains
off, and this fixture is not evidence of a general retrieval benefit.

```sh
.venv/bin/python evaluation/family_retrieval/family_retrieval.py
```

The script writes `docs/results/family_retrieval_2026-11-27.json` and
`docs/results/family_retrieval_2026-11-27.md`, including per-query results,
aggregate hit@1/3/5 and MRR, fixture checks and limits. No project checkout is
imported or written, no graph files are committed, and no network or model call
is made.

## Real-repository snapshots (2026-11-27)

[`real_repos.py`](real_repos.py) is the follow-up check on actual checkouts. It
rebuilds read-only graph snapshots for Bigville, Ocelot and Ozagia under
`/tmp/opencode/family-real` (override with `--workdir`), declares Bigville and
Ocelot in one `simulation-stack` family in the temporary Bigville anchor, and
runs the same four constructed family queries plus one Ozagia control with
`family_boost` off and on. Ozagia is the unrelated distractor.

```sh
.venv/bin/python evaluation/family_retrieval/real_repos.py
```

The script writes `docs/results/family_retrieval_real_2026-11-27.json` and
`docs/results/family_retrieval_real_2026-11-27.md`. It records per-query ranks,
distractor ranks, hit@1/3/5, MRR, snapshot HEADs, build commands, source checks
and a before/after probe of the target checkouts' state files. The snapshots are
built with `--docs features --no-git --no-language-install`; adapter indexers
are not loaded, so no target project adapter is imported. A neutral per-member
config is written under the work directory and passed through `OZAGIA_CONFIG`.

### Reproduction notes and limits

- The four family queries are real-file queries constructed as near-ties: the
  Bigville answer is one query term behind its Ozagia distractor, inside the
  fixed `0.15` boost. They test the mechanism in that regime, not the general
  rate of family-relevant queries.
- The query set is small and was authored after inspecting the real document
  titles and paths. It is not a random or representative sample.
- Answers are `Doc` nodes; the matching `File` nodes can also appear and are
  not counted as the answer.
- The temporary snapshot roots differ from the recorded checkout roots, so
  federation reports `stale` with `snapshot root differs from checkout path`.
  This is expected for the read-only snapshot layout; search still ranks the
  loaded graphs. The real checkouts are not written.
- Lexical search only: no document-body indexing, embeddings, model calls or
  network access. The script excludes its own previous JSON/Markdown reports
  from the temporary Ozagia snapshot so repeated runs see the same corpus.
- The default remains `family_boost=False`. This run is evidence about the
  ranking mechanism, not authorization to change the default; that still
  requires a representative paired evaluation on real queries.

### Curated Bigville-task replay

[`real_tasks.py`](real_tasks.py) replays the existing
`evaluation/bigville_retrieval/tasks.json` queries over the same temporary
federation. It is a negative/robustness check: most expected files fall outside
the federated lexical top 20, the two visible ranks barely move, and the
controls are unchanged. The curated task set was designed for richer
single-repository retrieval, so it is not a suitable family-ranking benchmark.
See `docs/results/family_retrieval_tasks_2026-11-27.md`; the default remains
off.
