# Deterministic graph-aware code review

`ozagia.core.code_review` reviews a git commit range without an agent, a model
or a network call. It reads the diff with git plumbing, runs a small preflight,
turns the diff and an existing graph snapshot into typed findings, and emits a
review packet that states what was checked and what was not.

The first slice is deliberately narrow: it never builds the graph, never runs
tests or linters, and never writes into the repository. The only write is an
explicitly requested `--out` file.

## Usage

```sh
python -m ozagia.code_review_cli \
  --root /path/to/repo \
  --range HEAD~1..HEAD \
  [--graph .ozagia/graph.json] \
  [--json] \
  [--out review-packet.json] \
  [--fail-on blocking|major|never]
```

- Default output is text. `--json` emits the packet as sorted JSON.
- `--out` writes the chosen format atomically (a temporary file in the same
  directory, then `os.replace`). It refuses a missing output directory. A
  refused preflight writes no output file.
- `--fail-on` defaults to `blocking`. Exit status is `1` only when a finding is
  at or above that severity; `never` always exits `0` after a successful
  review.
- Usage errors, a bad root or range, a git failure, an explicit `--graph` path
  that does not exist, and a refused preflight exit `2`.
- `--graph` is resolved relative to the repository root. The default is
  `<root>/.ozagia/graph.json`; when that file is absent the review still runs
  and reports `graph.freshness = "missing"`.

Operational flags:

```text
--memory-mb N          expected test/build memory for admission (default 2048)
--disk-mb N            expected test/build disk for admission (default 4096)
--max-file-bytes N     large-file threshold (default 1000000)
--worktree-base NAME   branch expected for the main worktree (default main)
```

Python API:

```python
from ozagia.core import code_review as R

packet = R.review_range(root, "main..HEAD")
packet = R.review_diff(root, base="HEAD~1", head="HEAD",
                       graph_path=".ozagia/graph.json")
status = R.exit_code(packet, "blocking")
```

`review_diff` raises `CodeReviewError` for an unusable root or range and
`PreflightError` when the preflight refuses the run. `PreflightError.report` is
a JSON-safe refusal object; nothing is reviewed.

## Preflight

Before reading the diff, the harness checks:

1. **Root.** The path must exist and be the git top level. A subdirectory, a
   non-repository or a missing path is an error.
2. **Worktree and branch.** `ozagia.core.worktree` reports the current
   worktree, its expected branch from `ozagia/worktrees.toml`, the
   `ozagia-wt/<name>` -> `feat/<name>` pattern, or the main worktree. A
   mismatch refuses the run. A detached HEAD is a warning, not a refusal.
3. **No interrupt.** An in-progress merge, rebase, cherry-pick, revert or
   bisect refuses the run.
4. **No unresolved conflicts.** `git ls-files -u` and the porcelain status are
   checked. Any conflicted path refuses the run.
5. **Range.** Both revisions must resolve to commits and the base must be an
   ancestor of the head. A diverged pair refuses rather than producing a
   misleading two-ended diff.
6. **Resources.** `ozagia.core.resources.check_admission(kind="task")` checks
   the host reserve against the expected test/build footprint. A shortfall
   refuses the run before any diff or graph work.

A dirty worktree is reported but does not refuse a review: the review reads
committed objects, not the dirty files. Resource enforcement itself is the
resource module's job; this harness only performs the admission check and
records it in the packet.

## Deterministic findings

Findings have an id (`CR-001`), type, severity (`blocking`, `major`, `minor`,
`note`), file, line when known, message, evidence list, graph keys, confidence
and a confidence note. Agreement between reviewers is never treated as proof.

| Type | Deterministic source | Typical severity |
| --- | --- | --- |
| `breaking_change` | Deleted source file (imports/callers may still refer to it). | major |
| `missing_test` | Changed source (not generated or binary) with no changed or covering test (graph edge or test filename at the reviewed commit). A deleted test file. | major |
| `unhandled_error` | Conflict marker in an added line (`<<<<<<<`, `=======`, `>>>>>>>`). | blocking |
| `stale_reference` | A changed or deleted symbol whose recorded `calls` callers are not in the diff; a stale graph snapshot. | major / minor |
| `scope_creep` | Generated file changed (`package-lock.json`, lockfiles, `dist/`, `vendor/`, minified assets, generated markers). | minor |
| `security_sensitive` | Sensitive path token, or an added dangerous construct (`eval(`, `exec(`, `os.system(`, `shell=True`, `pickle.loads(`, unsafe YAML load). A review flag, not a vulnerability claim. | minor |
| `performance_risk` | File above the large-file threshold. A size flag, not a measured regression. | minor |
| `unknown` | Binary file changed; `git diff --check` whitespace error. | note / minor |
| `duplicate_logic`, `contract_mismatch` | Not assessed in this slice; listed in `not_assessed`. | - |

Other checks:

- **Binary** files are detected from `git diff --numstat` (`-/-`) or a `B`
  name-status. Their content is not reviewed.
- **Large** files use the size of the blob at the reviewed head, not the
  current checkout.
- **Generated** files combine path/name rules with added lines containing
  `Code generated`, `DO NOT EDIT` or `@generated`.
- **Whitespace** errors come from `git diff --check`; the duplicate
  "leftover conflict marker" line is folded into the conflict finding.
- **Missing test** first uses graph `tests_module`, `mentions_file`,
  `tested_by_file` and `has_test` edges. When the graph is not usable, or the
  changed file is not in the snapshot, it falls back to test file names at the
  reviewed commit (`test_<stem>.py`, `<stem>_test.py`, `<stem>_test.go`). A
  matching test file need not be changed to count as covering; the packet
  records its source as `graph` or `filename`.
- **Caller omissions** use only recorded `calls` edges pointing at symbols
  defined in changed source files. Test-file callers are excluded because
  covering tests are reported separately. Callers in the diff are not
  reported. This is syntactic graph evidence, so confidence is `medium`.
- Findings are deduplicated and sorted by severity, type, file, line and
  message before ids are assigned. The same inputs produce the same finding
  order and ids.

## Graph freshness

The harness loads an existing snapshot with `ozagia.core.memory.DevMemory` and
does not call `build_index`, `reingest`, `refresh`, `symbol_graph` or any other
method that writes. The snapshot is usable only when freshness is `fresh`:

- the snapshot's recorded repository root is this root;
- its recorded HEAD equals the reviewed head commit;
- the reviewed head is the checkout HEAD;
- `DevMemory.staleness()` reports `fresh`, meaning the recorded working-tree
  digest still matches.

`missing`, `stale`, `unknown` and `error` all fall back to file-level checks.
In those states graph keys are not attached to findings, graph-derived caller
and test checks do not run, and the packet says why in `graph.reason`,
`omissions` and `not_assessed`. A stale graph also produces a
`stale_reference` finding (severity `minor`).

The graph is only as rich as the snapshot. Caller checks require `calls` edges
between `Symbol` nodes. Covering tests require `File`/`Test` nodes and
`tests_module`, `mentions_file` or `has_test` edges. If a changed file or its
symbols are absent from the snapshot, the packet reports that rather than
claiming coverage.

## Packet

Important fields:

```json
{
  "schema": "ozagia.code_review.packet.v1",
  "schema_version": 1,
  "range": {"spec": "BASE..HEAD", "base": "...", "head": "..."},
  "base_commit": "...",
  "head_commit": "...",
  "worktree": {"path": "...", "branch": "...", "dirty": {}, "clean": true,
               "detached": false, "operation": null, "expected": {},
               "base": {}},
  "preflight": {"ok": true, "checks": []},
  "graph": {"path": "...", "available": true, "freshness": "fresh",
            "usable": true, "snapshot_head": "...", "head": "...",
            "call_edges": 0, "reason": "..."},
  "resources": {"ok": true, "requested": {}, "available": {}, "estimate": {},
                "shortfall": {}, "suggestions": [], "note": "..."},
  "diff": {"files": 3, "additions": 10, "deletions": 2},
  "files": [{"path": "...", "status": "M", "file_kind": "source",
             "binary": false, "deleted": false, "large": false,
             "generated": false, "size": 123, "graph_keys": [],
             "symbols": [], "covering_tests": []}],
  "findings": [],
  "test_plan": {"mode": "affected", "selected_files": ["tests/test_app.py"],
                "executed": false, "truth_assessed": false,
                "not_run_reason": "..."},
  "tests_executed": false,
  "summary": {"findings": 0, "blocking": 0, "major": 0, "minor": 0, "note": 0},
  "omissions": [],
  "not_assessed": [],
  "model_review": {"status": "not_run", "truth_assessed": false},
  "security": {"provided": true, "findings": [], "truth_assessed": false},
  "truth_assessed": false
}
```

`security` is present only when the caller supplies a security report.

## Optional security findings

A caller that already ran `ozagia.core.security.scan` may pass the report:

```python
packet = R.review_range(root, "main..HEAD", security_report=security_report)
```

The packet then contains a `security` section with the supplied findings and
their original `sec-...` ids, summary, `not_assessed` entries and
`truth_assessed: false`. The review does **not** run a scan or a tool. A
`not_assessed` entry records that exploitability and the scan's currentness
were not established. `exit_code` includes the supplied findings: critical
maps to `blocking`, high to `major`, medium to `minor`, low/info to `note`.
This is a review flag, not a vulnerability claim.

`truth_assessed` is always `false`. A finding is evidence for a human or a
later reviewer, not a proof that the change is wrong. The packet does not
invent consensus, test results, or freshness.

## Test plan

The packet includes a deterministic `test_plan` from
`ozagia.core.testing` (see `docs/testing.md`). It is computed from the reviewed
diff and an existing graph snapshot; it selects test files, records graph node
ids, omissions, fallback reason and graph freshness. Tests are **not run**:
`test_plan.executed` and `tests_executed` are `false`, `not_run_reason` says
so, and the packet adds an entry to `not_assessed` when tests were planned.
The text form prints a one-line summary.

A repository may set `[testing.tasks] code_review = "none"` when a review
should record that no test selection is needed. The packet still reports the
policy and the reason; it never invents a pass.

## Optional model review

This slice does not call a model. `packet["model_review"]` records
`status: "not_run"` and the reason. The seam for a later independent-review
slice is:

```python
from ozagia.core.code_review import review_range, run_injected_review

def runner(packet):
    # The caller owns the provider, budget, approval and network policy.
    return [...]

packet = run_injected_review(review_range(root, "main..HEAD"), runner)
```

Injected findings are labelled `source: "model"`, get the same validation and
id assignment, and remain unverified. `run_injected_review` does not call a
model itself and does not set `truth_assessed` to true, regardless of how many
reviewers agree.

## Limits and non-goals

Not in this slice:

- commit/merge/push policy checks beyond the worktree and branch expectation;
- live or optional model calls, Ocelot reconciliation, or multi-model
  agreement;
- running tests, builds, linters or type checkers (a test plan is recorded,
  but nothing is executed; only resource admission is checked);
- semantic signature/behaviour compatibility, duplicate-logic detection, or
  contract-mismatch detection;
- `markdown_route` and file-ledger integration for the output path;
- writing any repository file. `--out` is the caller's explicit path;
- fetching, network access, graph builds or graph reingestion.

Binary content, generated output, and changes in files absent from the graph
are reported as not assessed rather than guessed.

## Tests

```sh
PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_code_review.py
.venv/bin/ruff check src tests
```

The tests build temporary git repositories with base/head commits, a deleted
file, a missing test, conflict markers, binary, large and generated files,
whitespace errors, a fresh graph, a stale graph, a missing graph, a wrong
branch, an unresolved merge, a resource refusal, CLI JSON/exit codes and the
injected-runner seam. No test uses a model or the network.
