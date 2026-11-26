# `ozagia-bench`

> **Generated file.** Built by `scripts/docs/generate_docs.py` from `src/ozagia/eval/bench.py` (ozagia `0.1.5.dev0`). Do not edit by hand: change the source code and regenerate.

This command's module does not expose a `build_parser()`;
its usage synopsis comes from the module docstring:

```
ozagia.eval.bench — timing bench for the warm repo index.

Measures, in one process, the cost of the repo-map tag cache, repo_map,
code_search and impact. The first call of each query may build a memo; the
second call shows the warm cost that the plan targets refer to.

    .venv/bin/python -m ozagia.eval.bench --root . --cold --workers 4
    .venv/bin/python -m ozagia.eval.bench --root . --query "tag cache"
    .venv/bin/python -m ozagia.eval.bench --root ~/ocelot --preset ocelot

The repo-map tasks, search queries and impact files come from `--preset`
(default `generic`), each replaced by `--task`, `--query` and `--impact-file`
when given. The generic impact files are the two largest indexed files of the
target, so the bench runs on any repository.

Output is one JSON object on stdout. ``--cold`` deletes the tag cache first,
so the update step is a full cold index. Without it, the update reuses the
cache left by an earlier process. The bench writes only to ``--cache``
(default ``/tmp/ozagia_index_bench/tags.json``), never under the target
repository. ``--check`` exits 1 when a target is missed. The plan does not use
it as a gate: a missed target is a reported result.

Stdlib only.
```
