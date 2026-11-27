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
