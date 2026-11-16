# Task class library

A task class is a versioned, data-defined bounded task. The library ships a
small set of useful and safe classes, groups them by domain, and lets a
repository override a built-in class in its own config.

Every built-in is a plain TOML file validated by
`ozagia.core.tasks.load_spec`; nothing here calls a model, reaches the network
or executes a task. The runner is still `ozagia.core.tasks.TaskRunner`, with an
executor supplied by the caller.

## Catalogue

Thirteen classes at library version 1:

| Domain | Task class | Inputs | Output | Writes | Purpose |
| --- | --- | --- | --- | --- | --- |
| Research | `paper_triage` | `paper_path`, `review_question` | `screening_recommendation.v1` | none | Include/exclude/maybe recommendation with reasons and locators. |
| Research | `claim_extract` | `document_path` | `claims.v1` | none | Claims with locators, uncertainty and open questions. |
| Research | `reference_screen` | `reference_path`, `review_question` | `screening_decisions.v1` | none | Title/abstract or full-text screening decisions with reasons. |
| Research | `evidence_table` | `claim`, `source_paths` | `evidence_table.v1` | `history/` | Rows with source locators and a support/contradict/neutral relation. |
| Research | `theory_compare` | `theory_a`, `theory_b` | `theory_comparison.v1` | none | Compatibility, shared assumptions and conflicts; never a truth verdict. |
| Research | `manuscript_check` | `manuscript_path`, `references_path` | `manuscript_check.v1` | none | Unresolved citations, claim checks and issues with locators. |
| Coding | `diff_review` | `diff` | `review_packet.v1` | none | Typed findings for a diff, with evidence and an explicit not-assessed list. |
| Coding | `test_triage` | `failure_log` | `test_triage.v1` | none | Failing test to cause, covering tests and a bounded proposed fix. |
| Coding | `impact_analysis` | `diff` | `impact_report.v1` | none | Changed graph keys, dependents, covering tests and a risk rating. |
| Coding | `repo_scan` | none | `repo_scan.v1` | none | Advisory opportunities with expected effect, effort, risk and evidence. |
| Ops | `log_inspect` | `log_path` | `failure_clusters.v1` | none | Failure clusters, evidence lines and proposed fixes. |
| Ops | `model_classify` | `task_class`, `privacy`, `input_size` | `model_choice.v1` | none | A model recommendation with confidence and reason, from local config. |
| Ops | `config_validate` | `config_path` | `config_check.v1` | none | Invalid keys, conflicts and suggested values. |

All classes declare `model_calls = 0` and `network = "deny"`. The only writer
is `evidence_table`, which has `write_scope = ["history"]` and
`approval.mutate = "ask"`; it stops for approval unless the run is explicitly
approved. `diff_review`, `impact_analysis` and `repo_scan` need a fresh graph
snapshot because they use graph tools; the other classes work with ordinary
file reads.

## Locations

Built-ins are package data:

```text
src/ozagia/task_specs/
  research/paper_triage.toml
  research/claim_extract.toml
  ...
  coding/diff_review.toml
  ...
  ops/log_inspect.toml
  ...
```

A repository overrides a built-in in its versioned config, never in the state
directory:

```text
<root>/ozagia/tasks/<id>.toml
```

`ozagia/` is project configuration and is meant to be committed. `.ozagia/`
remains local state and is never consulted for task classes.

The file name is a convention, not the identity: the `id` inside the spec is
authoritative. Use `<id>.toml` so duplicate detection and `copy` stay
predictable.

## Catalog metadata

`catalog()` and `list_tasks()` return metadata without the full spec:

```json
{
  "id": "diff_review",
  "domain": "coding",
  "version": 1,
  "title": "Diff review",
  "description": "Review a diff for correctness, test coverage and contract risk, ...",
  "source": "builtin",
  "path": "/repo/src/ozagia/task_specs/coding/diff_review.toml",
  "digest": "sha256:...",
  "overrides": false,
  "builtin_version": 1,
  "warnings": [],
  "defaults": {
    "worktree": {"expected_branch": null, "base": "main", "require_clean": true, "require_up_to_date": false},
    "resources": {"memory_mb": 1024, "disk_mb": 512, "pids": 64, "wall_s": 300},
    "budget": {"tokens": 40000, "model_calls": 0, "cost_usd": 0.0, "wall_s": 300},
    "gates": {"before": ["worktree_expected", "..."], "after": ["schema_valid", "..."]},
    "approval": {"mutate": "deny", "network": "deny"},
    "output": "review_packet.v1",
    "tools": ["read", "grep", "graph_search"],
    "write_scope": [],
    "requires_graph": true,
    "steps": 3
  }
}
```

`get_task(id)` returns the metadata plus the normalised specification
(`task.to_dict()["spec"]`). The `spec` is the same shape `core.tasks` exposes,
so a caller can pass the path to `TaskRunner` or inspect every step.

## Loading and overrides

Order is deterministic: built-ins load first, then accepted repository
overrides replace them by id. A repository file must override a built-in. It
inherits the built-in's domain; it cannot invent a domain.

An override is accepted when:

- its `id` matches a built-in;
- its `version` is greater than the built-in version; or
- its `version` equals the built-in version and its content digest is
  identical (an unchanged copy is a no-op, not a conflict).

It is rejected with a validation error when:

| Code | Condition |
| --- | --- |
| `invalid_spec` | `core.tasks` rejects the file; the error names every field. |
| `unknown_override` | The id does not match a built-in. New classes are not part of this release. |
| `duplicate_override` | Two files in `ozagia/tasks/` carry the same id. |
| `duplicate_builtin` | Two built-in files carry the same id; an installation defect. |
| `version_conflict` | Same version as the built-in, different content. Bump the version. |
| `stale_override` | Lower version than the built-in. Bump the override version. |
| `unknown_domain` / `library_layout` | A built-in path is not `<domain>/<id>.toml`. |

Warnings do not fail validation. `core.tasks` emits one for `evidence_table`
because its mutating step stops for approval unless the run is explicitly
approved; that is intended.

## CLI

```text
python -m ozagia.task_library_cli list [--domain research|coding|ops] [--root R] [--json]
python -m ozagia.task_library_cli show ID [--root R] [--json]
python -m ozagia.task_library_cli catalog [--root R] [--json]
python -m ozagia.task_library_cli validate [--root R] [--json]
python -m ozagia.task_library_cli copy ID --to PATH [--dry-run|--apply] [--force] [--root R] [--json]
```

`--root` defaults to the repository root discovered from the current working
directory. Exit status is `0` for success, `1` for a library or spec
validation error, and `2` for a usage, lookup or IO error.

Examples:

```console
$ python -m ozagia.task_library_cli list --domain coding
Task classes: 4 (4 built-in, 0 repo overrides)

coding (4)
  diff_review  v1  Diff review
      Review a diff for correctness, test coverage and contract risk, ...

$ python -m ozagia.task_library_cli validate --json
{
  "ok": true,
  "counts": {"builtin": 13, "repo": 0, "total": 13},
  ...
}
```

## Copy workflow

`copy` is preview-first. A dry run is the default; the file is written only
with `--apply` (or `copy_task(..., dry_run=False, confirm=True)`). The
destination must resolve inside `<root>/ozagia/tasks`; a directory destination
gets `<id>.toml` appended.

```console
# Preview the diff; nothing is created.
python -m ozagia.task_library_cli copy paper_triage \
  --to ozagia/tasks/paper_triage.toml

# Write it. Then edit the copy and bump its version.
python -m ozagia.task_library_cli copy paper_triage \
  --to ozagia/tasks/paper_triage.toml --apply

$EDITOR ozagia/tasks/paper_triage.toml   # version = 2, edit the steps

python -m ozagia.task_library_cli validate
```

Rules:

- An identical destination is reported as `unchanged` and is not rewritten.
- A destination with different content is refused without `--force`.
- If the destination itself is a broken override, `--force` repairs it from
  the built-in source and reports the previous library errors in the result.
  Other library errors still refuse the copy.
- Nothing outside `ozagia/tasks` is written, and no directory is created
  during a dry run.
- The copy keeps the built-in id. Editing the id makes the file an unknown
  override; editing the content without bumping the version makes it a
  version conflict. Both are reported by `validate`.

## Programmatic API

```python
from ozagia.core import task_library as library

tasks = library.list_tasks(root)              # catalog entries
task = library.get_task("diff_review", root=root)
spec = task.spec                              # a core.tasks.TaskSpec
report = library.validate_library(root)       # ok, errors, warnings, issues
meta = library.catalog(root)                  # domains, counts, entries

preview = library.copy_task(
    root, "paper_triage", "ozagia/tasks/paper_triage.toml")
library.copy_task(
    root, "paper_triage", "ozagia/tasks/paper_triage.toml",
    dry_run=False, confirm=True)
```

`load_library(root)` returns the full `Library` object, including structured
`Issue` records and both counts. `validate_library()` returns a report instead
of raising, so a caller can show every problem at once. `list_tasks`,
`get_task` and `catalog` raise `TaskLibraryError` when the library is invalid;
`TaskLibraryError.errors` is non-empty for validation failures and empty for
unknown ids, bad domains and IO failures.

## What remains

- **Model and tool adapters.** The specs declare typed steps and budgets, but
  the runner has no built-in executor. A live model, graph search, test runner
  or file writer is supplied by the caller. Prompt references and per-step
  prompt files are not part of this release.
- **Runner and UI integration.** `TaskRunner` already accepts any spec path;
  the library does not start runs, schedule them or render their evidence. The
  OpenCode add-in now browses the catalogue and copies a preview run command
  ([opencode_addin.md](opencode_addin.md)), but starting, watching and exporting
  runs is still to come.
- **New classes and domains.** Repository files currently override built-ins
  only. Allowing new ids needs an explicit domain declaration and a catalog
  entry; the strict unknown-override error is deliberate for now.
- **Model routing.** `model_classify` is advisory and reads local config; the
  model registry and per-task defaults from the settings work are later work.
- **Approval surfaces.** `evidence_table` stops for approval through
  `core.tasks`; a UI that shows the diff and records the approval is not built.
- **More checks.** The library validates specs and override conflicts. It does
  not prove that an executor implements the declared tools, that a prompt is
  safe, or that a run's output is true.
