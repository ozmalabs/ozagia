# Ocelot structural clustering for code findings

`ozagia.core.ocelot_code` adapts code-review findings and repo-scan
opportunities to the optional Ocelot worker used by research review, and adds
two structural projections: change shapes for test-impact analogies and
architecture patterns over scan opportunities or graph file/symbol shapes. It
is an additive, read-only layer:

- Ocelot remains a separate MIT-licensed checkout. ozagia does not bundle it,
  import it in the normal path, install it or download it.
- Ocelot is resolved through `ozagia.integrations.ocelot.resolve` (or an
  explicit `ocelot_root`). A missing checkout is a reported state, not a review
  failure.
- Findings are always grouped by the exact
  `(family, target_type, target_id)` key. Two findings with a different issue
  family or target are never merged, and no support or truth edge is created.
- Change shapes are only grouped by exact digest/structural overlap or by an
  optional Ocelot analogy link. A shape family is never merged with another
  (source, test, docs, config, mixed).
- Architecture patterns are candidate clusters, never anti-pattern verdicts.
- `truth_assessed` is always `false`. A structural analogy is a candidate
  signal, not a correctness verdict, and no test set is described as complete.

The module is used by `core/code_review.py`, `core/repo_scan.py` and
`python -m ozagia.ocelot_code_cli`.

## Resolution

`cluster_findings(..., root=R)` uses the installer's resolution order:

1. an explicit `ocelot_root` argument;
2. `$OZAGIA_OCELOT`;
3. a sibling checkout, `<root>/../ocelot`;
4. the newest managed install under `<data home>/ozagia/ocelot/<version>/`;
5. otherwise `not_installed` with no download attempt.

The optional worker interpreter is `OZAGIA_OCELOT_PYTHON` when set, otherwise
`<ocelot_root>/.venv/bin/python`. Mode `auto` tries an in-process call and
falls back to the subprocess; `inprocess` or `subprocess` can be requested
explicitly. In-process mode needs `substrate_rs` and the Ocelot tree on
`sys.path`; the subprocess mode uses the checkout's own environment.

## API

```python
from ozagia.core import ocelot_code as OC

result = OC.cluster_findings(
    findings,
    root="/repo",
    ocelot_root=None,          # optional explicit checkout
    mode="auto",               # auto | inprocess | subprocess
    timeout=120,
)
```

`findings` may be code-review findings, repo-scan opportunities, or rows that
already carry worker fields. The adapter fills:

```json
{
  "finding_id": "CR-001",
  "run_id": "code_review",
  "target_type": "File",
  "target_id": "src/app.py",
  "family": "evidence",
  "severity": "major",
  "wording": "source file src/app.py changed without a test"
}
```

- `finding_id` preserves `finding_id` or `id`; missing ids are synthesized in
  input order, and duplicate ids get a `~2` suffix.
- `run_id` preserves `run_id`, `reviewer_id` or `source`. The default is
  `code_review` for review findings and `repo_scan` for scan opportunities.
  Ocelot compares findings across runs, so a single run yields no analogies;
  a caller that records run ids per reviewer gets cross-reviewer comparisons.
- `target_type`/`target_id` prefer an explicit value, then a file, a sorted
  file set, a symbol key, a graph node, and finally a per-finding target.
- `family` uses the structural families understood by the shared worker. Code
  types and scan categories map as follows:

| Code type / scan category | Family |
| --- | --- |
| `duplicate_logic`, `duplicate_modules`, `duplicate_docs` | `duplicate` |
| `missing_test`, `missing_tests`, `test_policy` | `evidence` |
| `breaking_change`, `contract_mismatch` | `claim_structure` |
| `stale_reference`, `orphaned_files`, `stale_history` | `provenance` |
| `scope_creep`, `non_breaking_change`, `weak_boundaries` | `scope` |
| `unhandled_error`, `security_sensitive`, `performance_risk`, `unknown`, remaining scan categories | `other` |

Unknown types fall back to the research adapter's
`canonical_issue_family`; an unmapped type becomes `other`.

- `severity` preserves a supplied value. Repo-scan opportunities without one
  map `risk`: high to `major`, medium to `minor`, low to `note`.

### Result

```json
{
  "schema": "ozagia.ocelot_code.clusters.v1",
  "requested": true,
  "available": true,
  "state": "ready",
  "mode": "auto",
  "actual_mode": "subprocess",
  "engine": "ocelot:analogy_search",
  "ocelot": {"path": "/path/to/ocelot", "version": "1.2.3",
             "source": "sibling"},
  "clusters": [
    {"key": {"family": "duplicate", "target_type": "File",
             "target_id": "src/x.py"},
     "findings": ["CR-001", "CR-004"], "size": 2,
     "origin": "exact+ocelot", "analogy_count": 1}
  ],
  "ocelot_clusters": [["CR-001", "CR-004"]],
  "analogies": [],
  "finding_count": 2,
  "cluster_count": 1,
  "truncated": false,
  "omitted_findings": 0,
  "truth_assessed": false,
  "error": null,
  "note": "..."
}
```

`clusters` always contains the exact fallback grouping, including singletons.
`origin` is `exact` when only the key matched and `exact+ocelot` when the
worker also linked members of that key. `ocelot_clusters` is the worker's
proposal after filtering unknown ids and splitting any cluster that crossed a
family or target boundary. `analogies` keeps only links whose endpoints are
known and share the same key. At most 500 findings are sent to the worker;
`truncated` and `omitted_findings` report any remainder rather than hiding it.

The same shape is produced for the fallback states:

| state | meaning |
| --- | --- |
| `ready` | Ocelot ran; `available` is true and `engine` names the worker |
| `not_installed` | no checkout resolved; exact clusters only |
| `unavailable` | an explicit checkout is missing, or resolution was incomplete |
| `error` | the worker or resolver failed; exact clusters only |
| `no_findings` | nothing to cluster; Ocelot was not called |

`cluster_findings` never raises because Ocelot is absent or fails. Invalid
arguments (an unknown mode, a non-positive timeout) raise `OcelotCodeError`.

## Code review

`review_diff` and `review_range` accept:

```text
ocelot=False|True|callable   optional section, default off
ocelot_root=PATH            explicit checkout
ocelot_mode=auto|inprocess|subprocess
ocelot_timeout=120
ocelot_clusterer=CALLABLE   injection seam
```

When requested, the packet gets an `ocelot` section with the result above,
the existing `findings` stay unchanged and no finding severity changes. When
Ocelot is unavailable, the section says so and the packet adds an omission;
the review still succeeds. The CLI flags are `--ocelot`, `--ocelot-root`,
`--ocelot-mode` and `--ocelot-timeout`.

The section is computed from the packet's deterministic findings. A caller
that later merges injected reviewer output with `run_injected_review` can call
`cluster_findings` again on the merged findings; give each reviewer a distinct
`run_id` or `reviewer_id` so Ocelot can compare their shapes across runs.

## Repo scan

`scan(..., ocelot=False, ocelot_root=None, ocelot_mode="auto",
ocelot_timeout=120, ocelot_clusterer=None)` clusters only the
`duplicate_modules`, `duplicate_docs`, `weak_boundaries` and `security`
opportunities that are shown in the report. The report gets the same `ocelot`
section and a warning when a requested checkout is unavailable; the scan still
returns `status: ok`/`degraded`/`unavailable` from its normal graph rules.
`python -m ozagia.repo_scan_cli scan --ocelot ...` exposes the same options.

## Test-impact shapes

```python
from ozagia.core import ocelot_code as OC

shape = OC.change_shape(
    root,
    diff="main..HEAD",        # or changed=["src/app.py", ...]
    graph_path=None,          # default <root>/.ozagia/graph.json
    with_test_plan=False,     # optional core.testing.plan attachment
)
```

`change_shape` returns a deterministic structural description:

- `changed_files` and `changed_kinds` (`source`, `test`, `docs`, `config`);
- `family`: `source`, `test`, `docs`, `config`, `mixed` or `empty`;
- graph `symbols` (file, qualname, lines) and `symbol_names`;
- incident graph `edge_types`;
- `test_links`, each with a file, candidate test files, graph node ids and a
  source (`graph`, `filename` or `changed_test`);
- `package_dirs` and graph `import_dirs`;
- `digest`, a SHA-256 over the structural fields only.

A graph snapshot is used only when it is fresh for the checkout. A missing,
stale or unreadable snapshot is reported in `graph`; symbols, edge types and
graph test links are then not assessed and filename-convention test links are
used. The range, root and optional test plan are excluded from the digest, so
the same structural change has the same digest on another checkout.

With `with_test_plan=True` the shape also carries a compact
`core.testing.plan` result in `test_plan` and `planned_tests`. That is a
selection only: `executed` and `truth_assessed` remain false.

## Test-impact clusters

```python
result = OC.test_impact_clusters(
    shapes,                   # change_shape results or shape mappings
    ocelot_root=None,         # optional explicit checkout
    mode="auto",
    timeout=120,
    clusterer=None,           # injection seam; no Ocelot needed
    min_overlap=0.5,
    root=None,                # optional sibling-resolution root
)
```

Shapes are grouped by:

1. exact structural digest;
2. structural overlap (Jaccard over files, directories, packages, symbols,
   edge types, test links, suffixes and kinds), default threshold `0.5`;
3. optional Ocelot analogy links, accepted only when both shapes have the same
   family and share at least one structural attribute.

Ocelot is called with one worker run per shape and only when a checkout
resolves. When it is missing or fails, the exact digest/overlap clusters are
returned and `state`, `available`, `engine` and `error` report why. A
`clusterer` callable can replace the Ocelot call; its result is filtered the
same way. Its `shape_ids` are always the caller's ids.

Each cluster reports `shape_ids`, `family`, `origin` (`exact`, `overlap`,
`ocelot` or a `+ocelot` combination), `shared` attributes, `test_links`,
`candidate_tests`, `planned_tests`, `confidence` and `evidence`. Every cluster
has `truth_assessed: false` and `test_set_complete: false`; the result has
`tests_executed: false`. A suggested test set is a candidate from analogous
change shapes, never a completeness or coverage claim.

## Architecture patterns

```python
patterns = OC.architecture_patterns(
    opportunities,            # scan opportunities, paths, shapes, or a report
    root="/repo",
    graph_path=None,
    ocelot_root=None,
    mode="auto",
    clusterer=None,
)
```

`architecture_patterns` accepts repo-scan opportunities, repository-relative
paths, file/symbol mappings, a report-like mapping with an
`opportunities`/`items`/`files` list, or no items. With no items it builds file
shapes from a fresh graph (symbols, imports, imported-by and edge types); a
missing graph falls back to the repository file list without import evidence.

Opportunities are grouped by category plus structural attributes; files by
role plus directory, suffix, package, import, edge and symbol-shape attributes.
Different families are never merged. Ocelot analogy search follows the same
optional path and the same exact fallback. Each result is a candidate pattern
with `candidate: true`, `truth_assessed: false` and an explicit note that it
does not establish a defect, intent or completeness.

## Code review and repo scan integration

`review_diff`/`review_range` add `ocelot.impact` and `ocelot.patterns` only
when requested:

```python
packet = R.review_range(
    root, "main..HEAD",
    ocelot_impact=True,      # or an injected clusterer callable
    ocelot_patterns=True,    # or an injected clusterer callable
    ocelot_root=None, ocelot_mode="auto",
)
```

The existing `ocelot` section remains the finding-cluster section. When only
impact/patterns are requested it is a container with
`state: "not_requested"` and empty finding clusters. The nested impact section
uses the review's changed files and its existing `test_plan`; it never runs a
test. The nested patterns section uses the changed files as file shapes. An
unavailable Ocelot or a clusterer error is reported inside the section and as
an omission; the review still succeeds.

`scan(..., ocelot_patterns=True, ...)` adds `ocelot.patterns` over the
opportunities shown in the report (finding clustering is still controlled by
`ocelot`). `python -m ozagia.repo_scan_cli scan --ocelot-patterns ...` exposes
the same option. `python -m ozagia.code_review_cli --ocelot-impact
--ocelot-patterns ...` exposes the review flags.

## CLI

```bash
python -m ozagia.ocelot_code_cli cluster \
  --root /repo --findings review-packet.json \
  [--ocelot-root /path/to/ocelot] \
  [--mode auto|inprocess|subprocess] [--timeout 120] [--json]

python -m ozagia.ocelot_code_cli impact --root /repo \
  --diff main..HEAD [--diff OTHER..RANGE] [--changed PATH] \
  [--test-plan] [--graph .ozagia/graph.json] \
  [--ocelot-root /path/to/ocelot] [--json]

python -m ozagia.ocelot_code_cli patterns --report scan-report.json \
  [--ocelot-root /path/to/ocelot] [--json]

python -m ozagia.ocelot_code_cli patterns --root /repo \
  [--graph .ozagia/graph.json] [--ocelot-root /path/to/ocelot] [--json]

python -m ozagia.ocelot_code_cli status [--root /repo]
```

`cluster --findings` accepts a JSON list, a review packet (`findings`), a scan
report (`opportunities`) or an object with an `items`/`rows` list. `impact`
accepts repeatable `--diff` ranges and `--changed` paths; each becomes one
shape. `patterns --report` reads a scan report or list-shaped JSON; with
`--root` it uses graph file/symbol shapes (or the file-list fallback).

Exit status is `0` when clustering completed, including `not_installed`,
`unavailable`, `error` and `no_shapes` results: the exact fallback is still a
result and no review failed. Usage, IO, invalid JSON and resolver/installer
errors exit `2`. `status` reads the installer resolver only and exits `0` for
`not_installed`.

## What this does not establish

- That two findings are the same defect, or that a finding is correct.
- That a structural analogy is causal, or that agreement is consensus.
- That an analogous test set is complete, sufficient or even relevant. It is a
  candidate selection from similar shapes. No test is executed by these
  functions, no coverage is measured, and a passing subset is not a full pass.
- That a change-shape cluster has the same behaviour. The digest covers files,
  symbols, edge types, test links and directories, not semantics.
- That an architecture pattern is an anti-pattern, a defect or the author's
  intent. Patterns are candidate clusters from structural similarity; the
  input set and the pattern set are not complete.
- That Ocelot is installed, trusted or current. Resolution is offline; an
  install is always an explicit, pinned action through
  `ozagia.integrations.ocelot`.

## Tests

```sh
PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_ocelot_code.py
PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_code_review.py tests/test_repo_scan.py tests/test_testing.py
.venv/bin/ruff check src tests
```

Tests use fake worker output, injected clusterers and monkeypatched resolution.
A real sibling checkout test is skipped when no Ocelot checkout is available.
No test uses the network, installs Ocelot or calls a model. The test-impact
tests cover same/different digests, cross-family filtering, exact fallback,
injected Ocelot analogies, optional test plans, CLI exit codes and the absence
of completeness claims.
