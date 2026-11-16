# Bounded tasks

A bounded task is a versioned data file that states exactly what may run, where
it may read and write, how much it may spend, which gates it must pass and what
shape its output must have. `ozagia.core.tasks` validates that declaration,
runs a deterministic preflight, executes typed steps through an injected
executor, checks the gates, accounts the budget and writes an evidence record.

The runner is not an agent and not a chat session:

- it never calls a model;
- it never reaches the network;
- it never executes a tool itself;
- the caller supplies the executor, and a run with no executor is refused.

This is the safety and reproducibility layer that model and tool adapters will
use later, not a replacement for them.

## Specification

A specification is TOML or JSON. TOML is the normal form. The same fields are
accepted in both:

```toml
id = "code_review"
version = 1
title = "Code review"
objective = "Review a diff for correctness, tests and contract risk."
inputs = ["diff"]
tools = ["read", "grep", "graph_search"]
write_scope = []
requires_graph = true
stop = ["gate_failed", "budget_exhausted", "resource_limit", "no_progress", "repeated_state"]
output = "review_packet.v1"

[worktree]
expected_branch = "feat/code-review"   # optional; the repository mapping is used otherwise
base = "main"
require_clean = true
require_up_to_date = false

[resources]
memory_mb = 2048
disk_mb = 4096
pids = 256
wall_s = 900

[budget]
tokens = 40000
model_calls = 0
cost_usd = 0.0
wall_s = 900

[gates]
before = ["worktree_expected", "branch_expected", "drift_clear",
          "resources_fit", "snapshot_fresh"]
after = ["schema_valid", "evidence_present", "policy_commit"]

[approval]
mutate = "ask"      # ask | auto | deny
network = "deny"    # ask | auto | deny

[output_schema]
type = "object"
required = ["summary", "findings"]

[output_schema.properties.summary]
type = "string"
minLength = 1

[output_schema.properties.findings]
type = "array"

[[steps]]
id = "collect"
tool = "grep"
goal = "Find the changed files and symbols."
paths = ["."]

[steps.budget]
tokens = 8000

[[steps]]
id = "report"
tool = "read"
goal = "Assemble the review packet."
paths = ["."]
```

Required fields are `id`, `objective`, `tools`, `steps` and `output`. `title`
defaults to the objective; a missing title is fine, an empty one is not.
Version defaults to `1`.

Unknown keys are rejected at every level, including `steps`, `worktree`,
`resources`, `budget`, `gates`, `approval` and test steps. Unknown gates,
unknown stop conditions and unknown tools are errors, not warnings.

### Fields

| Field | Meaning |
| --- | --- |
| `id` | Stable task id, `[A-Za-z][A-Za-z0-9_.-]{0,63}`. |
| `version` | Positive integer. A specification edit should bump it. |
| `title` / `objective` | Short metadata and the full task statement. The objective is not copied into evidence. |
| `inputs` | Required input names. `repo_root`, `run_id` and `state_dir` are filled automatically when declared. Unknown inputs and missing inputs are refused. |
| `tools` | Allowlist drawn from the neutral tool set. A step may not use a tool outside this list. |
| `write_scope` | Repository-relative files or directories a row may create or change. Empty means read-only. |
| `steps` | 1-64 typed steps. Each step has `id`, `tool`, `goal`, optional `paths`, `write_paths`, `args` and `budget`. |
| `worktree` | `expected_branch`, `base`, `require_clean`, `require_up_to_date`. |
| `resources` | `memory_mb`, `disk_mb`, `pids`, `wall_s`. Defaults: 2048 MiB, 4096 MiB, 256 pids, 900 s. |
| `budget` | `tokens`, `model_calls`, `cost_usd`, `wall_s`. Defaults: 40,000; 0; 0.0; 900. |
| `gates` | `before` and `after` gate names. |
| `stop` | Declared stop conditions; the mandatory safety stops are always active. |
| `output` | Output schema name, for example `review_packet.v1`. |
| `output_schema` | Optional minimal JSON schema for the output value. |
| `approval` | `mutate` and `network`, each `ask`, `auto` or `deny`. |
| `requires_graph` | Whether a fresh graph snapshot is required. Declaring a graph tool implies it. |

### Neutral tools

| Class | Tools |
| --- | --- |
| Read | `read`, `glob`, `grep`, `list`, `stat`, `graph_search`, `graph_impact` |
| Model | `model` |
| Process | `test`, `lint`, `build` |
| Mutate | `write`, `edit`, `patch`, `shell` |

The set is deliberately small and generic. Project-specific adapters and a
task-class library are later work; an unknown tool is rejected rather than
silently passed through.

`test`, `lint`, `build` and `shell` are process tools in this model: the
injected executor owns their process-group and resource enforcement. This
build does not spawn them.

### Path scope

`write_scope` entries and step `paths`/`write_paths` are repository-relative.
Absolute paths, `..`, `.git` and the state directory are refused. At preflight
the paths are resolved, so a symlink that escapes the root is refused even
when its lexical path looks contained.

A mutating step must have a non-empty `write_scope`. Every artifact it reports
must resolve inside that scope; an artifact outside the root, inside `.git`,
inside the state directory or outside `write_scope` fails the run. A step cannot
add tools or paths that the task did not declare.

### Output schema subset

`output_schema` supports a small, deterministic JSON-schema subset:

- `type`: `object`, `array`, `string`, `number`, `integer`, `boolean`, `null`,
  or a list of those;
- `properties`, `required`, `additionalProperties` (boolean);
- `items`;
- `enum`;
- `minItems`, `maxItems`, `minLength`, `maxLength`;
- `minimum`, `maximum`;
- `description`, `title` and `default` are accepted metadata.

`$ref`, `oneOf`, `anyOf`, `allOf`, `pattern` and other keywords are rejected
at load time. Output is validated against the subset and must also be
JSON-serialisable and at most 4 MiB.

## Runner

```python
from ozagia.core.tasks import TaskRunner, load_spec

spec = load_spec("examples/tasks/code_review_dry_run.toml")
runner = TaskRunner(spec, root, executor=my_executor)
report = runner.run({"diff": "HEAD~1..HEAD"}, allow_exec=True)
```

`TaskRunner(spec, root, executor=None, state_dir=None)`:

1. **Validate** the specification and the supplied inputs.
2. **Preflight** (read-only): the expected worktree and branch through
   `core.worktree`, dirty/conflict/in-progress state, admission through
   `core.resources`, snapshot freshness when `requires_graph` is set, and path
   containment. Preflight constraints are enforced even when the corresponding
   `before` gate is not listed; the gate records the same check in the audit
   trail.
3. **Before gates**, each recorded as a gate event.
4. **Steps**, in declaration order. A step may not use an undeclared tool. A
   mutating step honours `approval.mutate`: `deny` always stops, `ask` stops
   unless the run is explicitly approved (`approve=True` or `--approve`), and
   `auto` proceeds.
5. **After gates**, unless a stop occurred.
6. **Output validation** against `output_schema`; schema failure fails the run
   even if `schema_valid` was not listed as a gate.
7. **Evidence** is written under the run directory.

`preview(inputs)` is read-only and returns the plan, preflight and declared
budgets. `run(..., allow_exec=False)` returns the preview. No run directory is
created for a preview.

### Executor protocol

The executor is a callable `executor(step, context)` or an object with an
`execute(step, context)` method. `step` is the normalised step table. `context`
contains `run_id`, `root`, `task_id`, `inputs`, `write_scope`, `remaining`
(budget), `step_index` and `steps_total`.

A result is a table:

```python
{
    "status": "ok",              # ok | error | blocked | no_progress
    "summary": "short label",    # bounded; never a prompt or file body
    "usage": {
        "tokens": 1200,
        "model_calls": 1,
        "cost_usd": 0.003,
        "wall_s": 2.5,
        "memory_mb": 310,
        "disk_mb": 12,
        "pids": 4,
    },
    "artifacts": [
        {"path": "history/2026-11-16/impact.md", "kind": "report"},
    ],
    "evidence": [
        {"label": "read src/ozagia/core/tasks.py", "kind": "read"},
    ],
    "output": {"summary": "..."},   # optional; the last supplied output wins
    "state": "optional fingerprint input",
}
```

Unknown result keys are rejected. In particular, `prompt`, `content`, `text`
and similar keys are not accepted: the runner does not persist raw prompts or
file contents. Artifacts are files; they are hashed (SHA-256) and sized, never
read into evidence.

`summary` and evidence labels are bounded text. They are metadata. The
executor is trusted to keep them short and non-secret; the runner cannot prove
that a 200-character label contains no excerpt, so adapters must not put file
bodies or prompts in labels.

## Gates

| Gate | Phase | Passes when |
| --- | --- | --- |
| `worktree_expected` | before/after | The root is a registered worktree, exists, is not prunable and matches the configured expected mapping. With no mapping the gate is `not_checked`. |
| `branch_expected` | before/after | HEAD is a branch and equals the spec's `expected_branch` (or the repository mapping when the spec does not name one). |
| `drift_clear` | before/after | No merge/rebase/cherry-pick/revert/bisect in progress, no conflicts, and no dirty paths when `require_clean` is set. Behind-base is a warning unless `require_up_to_date` is set. |
| `resources_fit` | before/after | Admission accepts the declared memory and disk against the host reserve. A disabled resource policy is `not_checked`. |
| `snapshot_fresh` | before/after | The graph snapshot is `fresh`. A task that does not require the graph reports `not_checked`. |
| `schema_valid` | after | An output was supplied and matches `output_schema`; without a schema, only JSON serialisability is checked. |
| `evidence_present` | after | At least one evidence label was recorded. |
| `policy_commit` | before/after | `core.worktree.policy_check(root, "commit")` passes. |
| `policy_merge` | before/after | Merge policy passes. |
| `policy_push` | before/after | Push policy passes. |

`before` and `after` have different allowed phases; for example
`schema_valid` in `before` is a validation error. Unknown gates are errors.
After gates are skipped when a step stop occurred, and the manifest records
`after_skipped`.

## Budgets, limits and stops

The runner accounts:

- **tokens, model calls, cost:** from executor `usage`;
- **wall time:** measured total run time (preflight, gates and steps), also
  compared with a step's reported wall time;
- **memory, disk, pids:** peaks from executor-reported `usage`, never invented.

The budget is enforced after each step and at the end of the run. A resource
limit wins over a budget limit when both are crossed. Per-step budgets may not
exceed the task budget. Memory/disk/pids are accounted from reported values;
they are not yet enforced by cgroups, rlimits or a watchdog.

Stop conditions:

- `gate_failed` — a preflight or gate refused the run;
- `budget_exhausted` — token, model-call, cost or wall budget crossed;
- `resource_limit` — memory, disk, pids or resource wall limit crossed;
- `no_progress` — the executor reported `status: "no_progress"`;
- `repeated_state` — two consecutive results have the same fingerprint
  (summary, state, output, artifact hashes and evidence labels);
- `approval_required` — a mutating step needs approval;
- `schema_invalid` — output failed the declared schema without a schema gate;
- `blocked` — the executor reported `status: "blocked"`;
- `error` — an executor exception, invalid result or write failure.

The mandatory stops are always active. A spec may list extra or the same
conditions; omitting a mandatory one produces a warning, not an escape hatch.

Statuses are `ok`, `refused` (preflight, input or missing-executor refusal),
`stopped` (budget, resource, no-progress, repeated state, approval, blocked)
and `failed` (gate failure after work, schema failure, executor error).

There is no hard preemption: a synchronous executor cannot be interrupted by
this layer. The wall limit is checked between steps. The process enforcement
layer (cgroups, rlimits, watchdog, own OOM) is later work.

## Evidence

A run lives in `<root>/.ozagia/tasks/<run-id>/` by default, or under an
explicit state directory. `state_dir` overrides the base state directory.

```text
events.jsonl     append-only, size-bounded event log
manifest.json    run status, spec digest, preflight, gates, budget, output
artifacts.json   artifact metadata: path, sha256, size, kind, step
output.json      the validated output value, when one was supplied
```

Events include `run_started`, `preflight`, `step_started`, `step_finished`,
`evidence`, `artifact`, `gate`, `budget_exceeded`, `repeated_state`,
`gates_skipped` and `run_finished`. Inputs are recorded by name and digest,
never by value. The manifest records the specification id/version/digest and
title, not the objective or input values.

No raw prompts, input values, model messages or file contents are written.
Artifacts are hashed, not copied. The output value is recorded because it is
the declared result; it is not a prompt or a file body.

`what_was_not_assessed` is always recorded. It names the runner's blind spots:
the executor's internal tool and model calls, unobserved network access, the
lack of enforcement, paths outside the root, skipped gates and unrun steps.

```python
from ozagia.core.tasks import list_runs, show_run

runs = list_runs(root)                 # newest first, bounded
detail = show_run(root, runs[0]["run_id"])
```

Corrupt or incomplete runs are reported as `invalid` or `incomplete`, not
silently skipped.

## CLI

```text
python -m ozagia.tasks_cli validate SPEC [--json]
python -m ozagia.tasks_cli run SPEC --input K=V [--dry-run] [--allow-exec] [--approve] [--root R] [--json]
python -m ozagia.tasks_cli list [--root R] [--limit N] [--json]
python -m ozagia.tasks_cli show [RUN] [--root R] [--json]
```

`run` is a preview unless `--allow-exec` is passed; `--dry-run` forces a
preview even when `--allow-exec` is present. The CLI has no built-in executor,
so a real `--allow-exec` run without one is refused and records that refusal.
Tests and host integrations inject an executor:

```python
from ozagia import tasks_cli
tasks_cli.main(["run", spec, "--allow-exec"], executor=my_executor)
```

Exit status is `0` for a valid spec/preview or a successful run, `1` for an
invalid spec, a refused run or a failed/stopped run, and `2` for a usage,
read or write error. The examples under `examples/tasks/` validate and cover a
read-only code review, a repository impact report with a `history/` write and
a paper-notes extraction.

## Safety limits

- No model calls, no network and no tool execution happen here. Preflight runs
  read-only git plumbing through `core.worktree`; nothing in this module starts
  a service, job or shell.
- The executor is **trusted**. The runner constrains declarations, reported
  tools and reported artifacts. It does not sandbox the Python process or
  inspect what the executor actually read.
- `approval.network` is passed to the executor as policy; this build has no
  network adapter and cannot enforce it.
- Resource values are self-reported by the executor and accounted; enforcement
  is not implemented here.
- Wall-time and approval checks happen between steps. A long-running executor
  call is not interrupted.
- Evidence is metadata-only. It records labels and hashes, not prompts or file
  bodies. It cannot detect a secret embedded in a short summary or evidence
  label; adapters must avoid that.
- Artifact hashing reads files under the repository to compute SHA-256; it
  does not copy their contents into the record.
- The worktree policies are repository configuration, not a security boundary.

## What remains

- Tool and model adapters: `read`/`grep`/`graph_search`/`test` implementations,
  a model client and per-task model routing from the settings registry.
- Resource enforcement: cgroup/rlimit limits, a watchdog, own OOM and
  `resource_event` feedback to the task record.
- File-ledger and `markdown_route` integration: every artifact gets a ledger
  row with the task id, reason and retention policy.
- A task-class library, approval surfaces and a UI for starting, watching and
  exporting runs.
- Scheduling, admission waiting and a shared queue; this build reports a
  refusal rather than waiting.
- A task-class manifest for browsing and copying classes into a repository's
  `ozagia/tasks/` directory.

Until those exist, bounded tasks are safe to validate, preview, exercise with a
deterministic test executor and use as the contract that adapters and the UI
will implement.
