# Test management and affected-test policy

`ozagia.core.testing` is a deterministic test-selection layer. It answers
*which test files should run for this change?* and records why. It never runs
them, never calls a model and never reaches the network. A plan is evidence
for a caller or a person, not a test result.

The existing captured-pytest selector in `ozagia.intent.affected` remains the
executor for interactive shell commands. This module adds the policy surface,
the graph-aware planner and the integration points for bounded tasks and code
review.

## Modes

From least to most inclusive:

| Mode | What is selected |
| --- | --- |
| `none` | No tests. A deliberate decision for docs-only, config-only or review-only work. |
| `smoke` | Test files the graph links to the changed files. |
| `affected` | Graph-linked tests, changed test files, tests that failed in the last known run, flaky tests and tests of the changed files' packages. |
| `package` | All tests in the changed files' packages/modules, still capped. |
| `all` | Every discovered test file, still capped and with omissions reported. |

A changed test file, a test file that failed in the last known run and a test
file containing a flaky test are **mandatory**: they are selected whole-file
and are never silently dropped by `max_tests`. If the mandatory set itself
exceeds the cap, the plan keeps the mandatory tests and reports
`cap_exceeded` with the optional tests that were omitted.

`none` is explicit: no test is selected even if a failed or flaky test is
known. The plan records that decision and `not_assessed` says the changed
files were not exercised.

## Policy precedence

Highest first:

1. a bounded task specification's own `test_policy`;
2. the repository `[testing.tasks]` entry for the task class;
3. a matching repository `[testing.paths]` glob for the task's paths;
4. the repository or user `[testing]` settings;
5. the built-in default.

The default for a bounded task is `affected`. An interactive captured pytest
uses `testing.captured`, which defaults to `all`: a typed `pytest` is not
silently reduced to a subset. An explicit `[tests] select` in `.ozagia.toml`
still wins over `testing.captured` (see below).

When several path globs match, the most inclusive mode wins for the task
(`none < smoke < affected < package < all`); within one path, the first
matching entry in TOML order wins.

```toml
[testing]
mode = "affected"
fallback = "all"          # all | refuse | affected_with_warning
max_tests = 50
timeout_s = 300
captured = "all"          # interactive default; [tests] select wins if set

[testing.tasks]
code_review = "none"
diff_review = "affected"
test_triage = "affected"
repo_scan = "smoke"

[testing.paths]
"docs/**" = "none"
"src/**" = "affected"
```

The scalar settings are registered in `ozagia.core.settings` and therefore
follow the normal user < repository < environment < override precedence. The
two override tables are open-ended; they are validated by the same parser and
are reported by `settings.testing_overrides(root)` (and as individual
`testing.tasks.<class>` / `testing.paths.<glob>` entries in `values()`).

## Captured pytest

`ozagia.intent.affected` remains the executor for an interactive captured
`pytest`. It reads, highest precedence first:

1. `[tests] select` in `.ozagia.toml` (`all` or `affected`) when the key is
   present. This setting keeps its original meaning and is never reinterpreted.
2. `testing.captured` from the settings registry (user, repository,
   environment or override), default `all`.
3. `all`.

`OZAGIA_TESTS_ALL=1` on the command or in the caller's environment always
runs the command as typed, before any of the above.

`testing.captured` accepts `none` (or its alias `off`), `smoke`, `affected`,
`package` and `all`. The executor only implements `all` and `affected`;
`testing.captured_select(mode)` maps the rest without overstating what ran:

| `testing.captured` | Captured selector | Effect |
| --- | --- | --- |
| `all` | `all` | Run the command as typed. |
| `affected` | `affected` | Run the linked tests and report the rest as not run. |
| `smoke` | `affected` | Use the affected subset (over-inclusion). |
| `package` | `all` | The affected selector is narrower than `package`; run everything rather than under-run. |
| `none`, `off` | `all` | Selection off: run the command as typed. |

`[tests] cache = true` still controls the test-result cache independently of
the selector. A malformed `.ozagia.toml` or an invalid `testing.captured`
falls back to `all`; a captured command is never silently made partial by a
broken config.

## Failure classes

`ozagia.core.failures.classify(returncode, timed_out, stdout, stderr, *,
command, first_party, interpreter, root)` gives a pytest (or other command)
result one class, with the lines that matched as `evidence` and a one-line
`detail`. It runs nothing and reads at most the first 16,000 and last
48,000 characters of each stream. The rules, first match wins:

| Class | Matches |
| --- | --- |
| `harness` | The runner itself failed: `FileNotFoundError`/`PermissionError` or `command not found` naming the command's program or the interpreter, `can't open file`, exit 126 or 127, a missing working directory. |
| `timeout` | The run timed out, whatever it printed. |
| `passed` | Exit 0 (checked here, before the rules below). |
| `environment` | `ModuleNotFoundError`/`ImportError` for a module that is not first-party (`module` names it), `No module named pytest`, `unrecognized arguments` (a plugin that is not installed), a missing shared library or executable. |
| `collection` | An import error of a first-party module, a syntax error during collection, `fixture 'x' not found`, `found no collectors` / `not found` for a node id, no tests collected, any other collection error. |
| `assertion` | Failed tests with assertion output (`E   assert`, `AssertionError`, `Failed:`). |
| `error` | Failed or errored tests without assertion output, `INTERNALERROR`, any other non-zero exit. |
| `passed` | Exit code unknown, and a pytest summary with passes only. |
| `unknown` | No exit code and nothing the rules read. |

So `ModuleNotFoundError: No module named 'numpy'` with `found no
collectors` for the node ids in that file is `environment`: the import
error is the cause, the missing collectors follow from it. A first-party
`ImportError` is `collection`, unless a third-party one is also present.

First-party names: the caller passes them (`first_party`); the daemon and
the eval harness pass `first_party_names(root, import_roots)`, the
top-level modules and packages (directories holding `.py` files) in the
root and in each configured import root (`src` by default when it exists).
Without `first_party`, they come from `root`; without a root, from the
relative paths of the output's traceback frames (`tests/test_x.py:6:`
gives `tests`, `src/pkg/a.py` gives `pkg`). A module that is not
first-party is third-party; a stdlib module the interpreter lacks counts
as third-party too.

For `environment`, the result names the `module`, the `interpreter` and,
when `ozagia.core.environments.who_has(root, module)` (W20) is importable,
the interpreters that have the module (`available_in`, the one that ran
excluded). Without W20, or when it raises, that detail is left out.

A captured pytest that did not pass gets a line right after its head:

```
[ozagia] pytest interrupted: exit 2, 0.23 s, 0 passed, 0 failed, 0 error, 0 skipped, 1 collection errors
[ozagia] failure class environment, module numpy: missing module numpy (third-party) under /usr/bin/python3; available in /repo/.venv/bin/python
```

The test record (`<state dir>/working_set/tests.json`) keeps, for
captured and compound runs, `failure_class` and `failure_module` (read
from that line, or `passed` for a head with status passed), and no other
output text. Records written before have neither field and load as before.
Goal criteria `test_passes`/`test_fails` give `unknown` with the class and
module when the deciding run's class is `environment` or `harness`, or
`collection` of a module that is not first-party; a pass still decides
(see `docs/goals.md`).

## Planning

```python
from ozagia.core import testing

plan = testing.plan(
    root,
    base="HEAD",            # or an explicit base commit
    head=None,              # None compares against the working tree
    changed=None,           # or an explicit repository-relative list
    task_class="code_review",
    graph_path=None,        # default <root>/.ozagia/graph.json
    max_tests=None,         # overrides the policy cap
)
```

Changed files come from an explicit list or from
`git diff --name-status base..head`. When the comparison is against the
working tree (or a `head` that is the checkout `HEAD`), untracked files are
included. Deleted files stay in the list so tests that imported them are
selected.

Graph evidence is read from an existing snapshot, read-only. It uses:

- `File -has_test-> Test` and `tests_module` / `mentions_file` edges;
- `intent.common.covering_tests` for each changed `File` node;
- `Test` nodes and their files for discovery and node ids;
- the changed test files themselves.

A snapshot is trusted only when `DevMemory.staleness()` reports `fresh`, the
recorded root matches, and the recorded HEAD matches the planned head when one
is known. `missing`, `stale`, `unknown` and `error` snapshots are reported in
`graph.freshness` and never used to claim affected coverage.

`tests/results.json` under the state directory supplies last-known results.
Tests that failed in any suite's latest run and tests containing a flaky node
are selected whole-file. A test file that no longer exists is recorded as
omitted rather than selected.

### Fallback

A changed file with no linked test uses `fallback`:

- `all` (default): select every discovered test file, record
  `fallback_reason` and say that affected coverage was not assessed.
- `refuse`: select nothing, set `refused` and record the reason. The CLI still
  exits `0` with the refusal report; a strict task with a `test` step fails
  preflight.
- `affected_with_warning`: select a best-effort subset (changed, failed,
  flaky, graph-linked and package tests), warn and explicitly say the result
  is not affected-only coverage.

A missing, stale or unreadable graph uses the same fallback. A graph that is
fresh but does not contain a changed file also counts as unlinked. Changing a
`conftest.py` is treated as unlinked because it can affect tests throughout
its tree.

Docs-only, config-only and empty changes select `none` for `smoke`,
`affected` and `package`. An explicit `all` still selects everything because
that is the command as typed.

### Output

The plan is a JSON-safe dictionary:

```json
{
  "schema": "ozagia.testing.plan.v1",
  "mode": "affected",
  "requested_mode": "affected",
  "mode_reason": null,
  "changed_files": ["src/app.py"],
  "changed_kinds": {"src/app.py": "source"},
  "selected_files": ["tests/test_app.py"],
  "selected_tests": [
    {"file": "tests/test_app.py",
     "node_ids": ["test:tests/test_app.py::test_f"],
     "sources": ["graph"]}
  ],
  "counts": {"changed_files": 1, "selected_files": 1,
             "selected_node_ids": 1, "omitted_files": 0,
             "max_tests": 50, "cap_exceeded": false},
  "omitted": [],
  "fallback": "all",
  "fallback_used": false,
  "fallback_reason": null,
  "refused": false,
  "graph": {"path": "...", "freshness": "fresh", "usable": true,
            "reason": "..."},
  "test_results": {"available": true, "failing": [], "flaky": []},
  "policy": {"mode": "affected", "fallback": "all", "max_tests": 50,
             "timeout_s": 300, "captured": "all", "sources": {}},
  "max_tests": 50,
  "timeout_s": 300,
  "executed": false,
  "truth_assessed": false,
  "claim": "test selection only; no test was executed",
  "not_assessed": ["no test was executed; this plan selects tests only", "..."]
}
```

`mode` is the effective mode: it becomes `all` when the `all` fallback is
used, and `none` for a docs-only or empty plan. `requested_mode` preserves the
policy's mode. `omitted` is bounded; `counts.omitted_files` is the full count
and `omitted_truncated` says when the list was cut.

`truth_assessed` is always `false`. A subset run is never described as a full
pass, and an empty plan is not an empty green result.

## CLI

```text
python -m ozagia.testing_cli policy [--task CLASS] [--path PATH] [--captured] [--root R] [--json]
python -m ozagia.testing_cli plan [--base B] [--head H] [--task CLASS] [--changed PATH] [--max-tests N] [--graph PATH] [--root R] [--json]
```

Examples:

```bash
# The resolved policy and where each value came from.
python -m ozagia.testing_cli policy --root . --json

# Tests linked to a commit range.
python -m ozagia.testing_cli plan --root . --base main --head HEAD

# A refusal is a plan, not a crash.
python -m ozagia.testing_cli plan --root . --changed src/unlinked.py --json | \
  jq '{mode, refused, fallback_reason}'
```

Exit status is `0` for a produced policy or plan, including a `refuse`
fallback, and `2` for a usage, configuration, IO or git error.

## Tasks

A bounded task may declare:

```toml
test_policy = "affected"
# or
[test_policy]
mode = "affected"
fallback = "refuse"
max_tests = 20
timeout_s = 120
```

`TaskRunner.preflight()` computes the plan, records it in the preflight report
and the manifest, and includes it in the `preflight` event. A `test` step
receives the plan in `step["args"]["test_plan"]` and in
`context["test_plan"]`. Under `none` (or a refusal/unavailable plan) the step
is skipped, not executed: the run records `step_skipped` and a skipped step in
the manifest. The runner itself still never runs a test.

## Code review

`review_diff()` adds `test_plan` to the packet, plus `tests_executed: false`.
It does not run tests. When the plan selects test files, the packet's
`not_assessed` names the planned tests as not run. A repository may set
`[testing.tasks] code_review = "none"` to record that review does not need a
test selection.

## Test-impact analogies

`ozagia.core.ocelot_code` can attach an optional `ocelot.impact` section to a
review packet when `ocelot_impact=True` is requested. It builds a change shape
from the reviewed files and the plan above, then groups similar shapes into
candidate test sets. The section reuses the plan; it never re-plans or runs a
test.

The same shape can be built directly:

```python
from ozagia.core import ocelot_code as OC

shape = OC.change_shape(
    root,
    diff="main..HEAD",       # or changed=["src/app.py"]
    with_test_plan=True,     # attaches core.testing.plan
)
result = OC.test_impact_clusters([shape])
```

`with_test_plan=True` stores a compact plan in `shape["test_plan"]` and its
selected files in `shape["planned_tests"]`. `test_impact_clusters` reports
`candidate_tests` and `planned_tests` per cluster, plus
`test_set_complete: false`, `tests_executed: false` and
`truth_assessed: false`. It records `not_assessed` saying that a similar test
set is not complete and a passing subset would not prove correctness.

Similarity is exact structural digest, structural overlap, or an optional
Ocelot analogy when a checkout resolves. Groups never cross shape families
(source, test, docs, config, mixed), so a test-only change is not claimed to
have the same impact as a source change. When Ocelot is missing, exact
digest/overlap clusters are still returned; `state` and `error` report why.

```bash
python -m ozagia.ocelot_code_cli impact --root . \
  --diff main..HEAD --test-plan --json
```

## Limits

- The plan selects files, not individual pytest outcomes for graph-linked
  tests. `node_ids` are graph keys for the tests in a selected file; the file
  is the unit a caller runs.
- Test discovery uses graph `Test` nodes and conventional names
  (`test_*.py`, `*_test.py`, `.test.`/`.spec.` suffixes). Tests outside those
  conventions are not invented.
- Package selection follows changed directories, `src/<pkg>` mirrored to
  `tests/<pkg>`, and graph coverage of files in the changed package. It is a
  heuristic, not language-specific import resolution.
- Graph relationships are only as good as the snapshot. A graph without Test
  nodes or import edges falls back rather than guessing.
- Test-impact analogies are candidates from structural similarity, not a
  completeness proof. They reuse a plan and never execute it; a different
  change with the same tests is not evidence those tests are sufficient.
- No model, network, graph build or test execution happens here.
- `timeout_s` is recorded for the caller that executes the plan; planning
  itself is bounded by file/graph limits and never sleeps.

## Tests

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_testing.py \
  tests/test_tasks.py tests/test_code_review.py tests/test_settings.py \
  tests/test_affected_tests.py tests/test_failures.py
.venv/bin/ruff check src tests
```
