# `ozagia-eval-retrieval`

> **Generated file.** Built by `scripts/docs/generate_docs.py` from `src/ozagia/eval/retrieval.py` (ozagia `0.1.5.dev0`). Do not edit by hand: change the source code and regenerate.

This command's module does not expose a `build_parser()`;
its usage synopsis comes from the module docstring:

```
ozagia.eval.retrieval — deterministic, offline retrieval evaluation.

This is the measurement instrument for the repo map. It answers one narrow
question: *given an explicit task and a token budget, which repository files do
the retrieval arms surface, and how many of the known-relevant files do they
recover?*

It does not run an LLM, call the network, edit source, or infer ground truth at
evaluation time. Ground truth is the explicit ``expected_files`` list in the
task file (``evaluation/repo_map/tasks.json`` for v1,
``evaluation/repo_map/tasks_v2.json`` for v2, or any file passed with ``--tasks``).

Arms per (task, budget):

  baseline      first source files by sorted path, rendered as a plain listing
  dev_context   ozagia.core.memory.DevMemory.context(...)
  dev_repo_map  ozagia.core.repo_map.repo_map(...)
  both          union of the dev_context and dev_repo_map file sets
  grep          lexical baseline: files ranked by distinct task terms they contain
  code_search   ozagia.core.search.code_search(...) results with snippets

The two retrievers are executed once per (task, budget); ``both`` is their
union and its elapsed time/token cost is the sum of the two components. The
budget is therefore applied to each retrieval component, not to the union.

Every selected file carries a tier. ``content`` means the arm showed code for
that file (a code_search snippet, a dev_context item with snippet text, or a
repo-map ``full``/``outline`` entry). ``listing`` means the arm only named the
path. ``recall`` counts a hit at any tier; ``recall_content`` counts only
content-tier hits.

Usage:

    .venv/bin/python -m ozagia.eval.retrieval \
        --tasks evaluation/repo_map/tasks_v2.json \
        --memory .ozagia/graph.json \
        --out-json evaluation/repo_map/results_v2.json \
        --out-md evaluation/repo_map/RESULTS_V2.md
```
