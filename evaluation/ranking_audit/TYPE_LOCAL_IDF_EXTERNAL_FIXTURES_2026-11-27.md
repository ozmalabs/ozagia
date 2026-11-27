# Type-local IDF audit on existing offline fixtures (2026-11-27)

## Scope and method

Compared global-IDF search with type-local-IDF search on the same fresh graph
and task inputs. The graph was built from this checkout (21,378 nodes, 68,873
edges) and records HEAD `094fd96e685543793d5531a296f06154bdbe558c`; both reports
passed the freshness check.

The 20-task stress probe reuses prompts in `evaluation/m6_live/tasks.json`.
Each retrieval task uses the prompt as its query and the union of `evidence`
paths in that task's `required` assertions plus `forbidden[].contrary.path`
from `evaluation/m6_live/assertions.json` as expected paths. The derived task
input is preserved in
[m6_evidence_path_probe_tasks.json](m6_evidence_path_probe_tasks.json). This
adapts an answer-evidence task set into a path-retrieval probe; it is not an M6
study result or a curated file-retrieval benchmark.

The pre run used the current code with `DevMemory._idf` temporarily delegated
to the prior global formula. The post run used the normal type-local method.
The graph and input tasks were the same in both runs.

## Results

| Probe | Ranking | Tasks | Recall/content recall at 300 | At 700 | At 1300 | Mean precision at 300 / 700 / 1300 |
|---|---|---:|---:|---:|---:|---|
| M6 evidence-path probe | Global IDF | 20 | 0.2125 | 0.0500 | 0.2625 | 0.166667 / 0.016667 / 0.074167 |
| M6 evidence-path probe | Type-local IDF | 20 | 0.2125 | 0.0500 | 0.2625 | 0.166667 / 0.016667 / 0.074167 |
| Synthetic retrieval fixture | Global IDF | 2 | 1.0 at 100 and 500 | — | — | Same file lists as type-local |
| Synthetic retrieval fixture | Type-local IDF | 2 | 1.0 at 100 and 500 | — | — | Same file lists as global |

The M6 probe's selected paths differed in five of 60 runs. Type-local IDF
added one file in each changed run:

- `src/ozagia/core/test_coverage.py` for `selfgraph-readiness-separation` at
  300 and 1300 tokens.
- `tests/fixtures/deep_review_static/mutable_list_default/case.py` for
  `maps-types-and-budgets` at 300, 700, and 1300 tokens.

These additions did not change recall or reported mean precision. Mean
selected files increased by 0.05–0.1; mean estimated tokens increased by
1–3.55. The first file is plausibly related to the coverage query; the second
is a weak match. These probes did not demonstrate an aggregate regression.

Full report artifacts:

- [Global IDF M6 probe](global_idf_m6_evidence_probe.json)
- [Type-local IDF M6 probe](type_local_idf_m6_evidence_probe.json)

The synthetic fixture is `tests/test_eval_retrieval.py::_fixture`; global and
type-local runs had the same fingerprint
`19dfc20ea13beb4471880e2ddad1f8df8a489e1ff55c8141709f7da4a1fbf4f1` and
recovered each expected implementation file at both budgets.

## Reproduction

Build the fresh graph and derive the task input from M6's frozen prompts and
assertions:

```sh
PYTHONPATH=src .venv/bin/python -m ozagia.core.memory build \
  --root . --out /tmp/ozagia-ranking-audit-graph.json

.venv/bin/python - <<'PY'
import json
from pathlib import Path

tasks = json.loads(Path("evaluation/m6_live/tasks.json").read_text())
assertions = json.loads(Path("evaluation/m6_live/assertions.json").read_text())["tasks"]
rows = []
for task in tasks["tasks"]:
    evidence = assertions[task["id"]]
    paths = {p for row in evidence.get("required", [])
             for p in row.get("evidence", [])}
    paths.update(row.get("contrary", {}).get("path")
                 for row in evidence.get("forbidden", []))
    paths.discard(None)
    rows.append({"id": task["id"], "task": task["prompt"],
                 "expected_files": sorted(paths)})
Path("/tmp/ozagia-ranking-audit-m6-tasks.json").write_text(
    json.dumps({"tasks": rows}, indent=2))
PY
```

The comparison used `ozagia.eval.retrieval.evaluate` with
`arms="dev_context"`, `budgets="300,700,1300"`,
`memory_path="/tmp/ozagia-ranking-audit-graph.json"`, and the derived task
file. For the pre run, `DevMemory._idf` was temporarily replaced by:

```python
lambda self, token, type_name=None: (
    math.log((self._n_labels + 1) / (self._df.get(_stem(token), 0) + 1)) + 1.0
)
```

Then the original method was restored before the post run. Both reports record
the same HEAD and `stale=false`.

## Limits

- Fifteen expected paths are outside the 826-file generic source universe.
  They are answer-evidence references, often documentation, so this probe is
  not directly comparable to a source-only retrieval benchmark.
- The synthetic fixture has two tasks whose wording closely matches expected
  files; it checks stability on a tiny repository, not general ranking
  quality.
- Type-local IDF changes score scales between node types. These probes show no
  aggregate recall regression but do not establish cross-type calibration on
  other repositories or query sets.
