# Bounded tasks

A bounded task is a versioned data file that states exactly what may run, where
it may read and write, how much it may spend, which gates it must pass and what
shape its output must have. `ozagia.core.tasks` validates that declaration,
runs a deterministic preflight, executes typed steps through an injected
executor, checks the gates, accounts the budget and writes an evidence record.

The runner is not an agent and not a chat session:

- it never calls a model;
- it never reaches the network;
- it never executes a tool itself (the optional `LocalExecutor` described below
  is a separate adapter the caller opts into);
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
test_policy = "affected"   # none | smoke | affected | package | all

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
grants = ["local-windows-vm"]   # optional; ids declared in ozagia/resources.toml

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
| `resources` | `memory_mb`, `disk_mb`, `pids`, `wall_s`, and optional `grants`. Defaults: 2048 MiB, 4096 MiB, 256 pids, 900 s. `grants` is a bounded list of declared resource-grant ids; an empty list is omitted from the canonical form so existing digests do not change. |
| `budget` | `tokens`, `model_calls`, `cost_usd`, `wall_s`. Defaults: 40,000; 0; 0.0; 900. |
| `gates` | `before` and `after` gate names. |
| `stop` | Declared stop conditions; the mandatory safety stops are always active. |
| `output` | Output schema name, for example `review_packet.v1`. |
| `output_schema` | Optional minimal JSON schema for the output value. |
| `approval` | `mutate` and `network`, each `ask`, `auto` or `deny`. |
| `requires_graph` | Whether a fresh graph snapshot is required. Declaring a graph tool implies it. |
| `test_policy` | Optional test-selection policy: a mode string or a table with `mode`, `fallback`, `max_tests` and `timeout_s`. See `docs/testing.md`. |

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

`TaskRunner(spec, root, executor=None, state_dir=None, approve_grants=False,
allow_unbounded_grants=False, allow_keep_grants=False, grant_reason=None,
grant_budget=None, grant_session=None)`:

1. **Validate** the specification and the supplied inputs.
2. **Preflight** (read-only): the expected worktree and branch through
   `core.worktree`, dirty/conflict/in-progress state, admission through
   `core.resources`, snapshot freshness when `requires_graph` is set, path
   containment and, when the task has `test` steps or a `test_policy`, a
   deterministic test plan from `core.testing`. Preflight constraints are
   enforced even when the corresponding `before` gate is not listed; the gate
   records the same check in the audit trail.
3. **Before gates**, each recorded as a gate event.
4. **Steps**, in declaration order. A step may not use an undeclared tool. A
   mutating step honours `approval.mutate`: `deny` always stops, `ask` stops
   unless the run is explicitly approved (`approve=True` or `--approve`), and
   `auto` proceeds. A `test` step receives the preflight plan as
   `step["args"]["test_plan"]` and `context["test_plan"]`. When the plan mode
   is `none`, the plan refused or the plan is unavailable, the `test` step is
   skipped and the run records `step_skipped` instead of calling the executor.
   The runner itself never executes a test.
5. **After gates**, unless a stop occurred.
6. **Output validation** against `output_schema`; schema failure fails the run
   even if `schema_valid` was not listed as a gate.
7. **Evidence** is written under the run directory.

`preview(inputs)` is read-only and returns the plan, preflight and declared
budgets. `run(..., allow_exec=False)` returns the preview. No run directory is
created for a preview.

### Resource grants

A task may declare `[resources] grants = ["id", ...]` (at most 16, unique,
valid ids). Loading validates every id against the repository's declared
grants; an unknown id is a validation error naming it. The task file supplies
only ids. It never supplies approval, credentials or an observed spend.

Preflight checks each grant read-only (`resources grants check`), with the
host's approval flags. A missing, unavailable, unknown or unapproved grant is a
typed preflight failure and the run refuses before the executor is called.
Nothing is claimed during validation or preflight.

Before the before gates, the runner claims every grant in declaration order.
The claim reason names the task id and run id, and the claim budget is the
task's `budget.cost_usd` (when positive) and the shorter of `budget.wall_s` and
`resources.wall_s`; a host can pass `grant_budget` to narrow or override it. A
task budget above the grant's declared cap is refused with
`budget_exceeds_grant`, not narrowed silently. An existing
active/expired/orphaned claim still refuses with `grant_in_use`. If
a later claim fails, earlier claims are released in reverse order and the run
refuses (status `refused`, stop `gate_failed`).

Every claim is released in `finally`, including on an executor exception or a
wall timeout. The runner does not observe the provider, so release records
`observed` cost/time as unknown and `cleanup: unknown`. A failed release is
reported in `unreleased` and the run warnings; it is never hidden.

Approval is a host decision and comes only from runner options or CLI flags:

| option / runner argument | meaning |
| --- | --- |
| `--approve-grant` / `approve_grants=True` | approve a grant with `requires_approval` |
| `--allow-unbounded-grant` / `allow_unbounded_grants=True` | also allow `admin` or an unbounded budget |
| `--allow-keep-grant` / `allow_keep_grants=True` | also allow `cleanup = "keep"` |
| `--grant-reason TEXT` / `grant_reason=` | host reason copied into each claim (redacted; credential-like text is refused) |

The task manifest gains a `grants` object when the spec declares grants:
`declared`, `approval` (flags and non-secret reason), `budget`, `preflight`
(with per-grant state and failures), `claims` (claim id, task/session, reason,
approval kind/reason, budget, observed, cleanup, release outcome), `refused`,
`released`, `unreleased` and `warnings`. A spec without grants has no `grants`
field and its digest is unchanged.

### Executor protocol

The executor is a callable `executor(step, context)` or an object with an
`execute(step, context)` method. `step` is the normalised step table. `context`
contains `run_id`, `root`, `task_id`, `inputs`, `write_scope`, `remaining`
(budget), `step_index` and `steps_total`. A `test` step's step table also has
`args.test_plan` and its context has `test_plan`; no other step is changed.

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

## Local executor (read-only)

`ozagia.core.task_exec.LocalExecutor` is the built-in executor behind
`--executor local` (or `--local`). It is an ordinary executor: the runner
still validates the task, checks preflight and gates, accounts the budget and
writes evidence.

```python
from ozagia.core.task_exec import LocalExecutor

executor = LocalExecutor(root)
executor(step, context)
```

`LocalExecutor(root, *, policy=None, graph_path=None, python=None)`:

- `policy` is a `LocalPolicy` or a mapping of its fields. Unknown keys,
  non-integers and non-positive caps are rejected at construction.
- `graph_path` defaults to `<root>/.ozagia/graph.json`. A relative path is
  resolved against `root`.
- `python` defaults to the interpreter running the process, so a test step
  does not depend on a `PATH` lookup.

Supported tools and arguments:

| Tool | Arguments | Behaviour |
| --- | --- | --- |
| `read` | `path` or `file`; `start`/`end` (1-based, inclusive) or `offset`/`limit` | Reads one UTF-8 regular file inside the step's declared paths. |
| `grep` | `pattern`; `regex`, `case_sensitive`, `max_matches`/`limit` | Literal search by default, or a Python regular expression, over the step's paths. |
| `glob` | `pattern` or `glob`; `max_results`/`limit` | Matches paths under the step's paths, against both the repository-relative path and the path relative to each step path. `*` and `?` stay inside one path component; `**` matches any number of directories (including none); a trailing `**` matches everything below that directory. |
| `graph_search` | `query`; `types`, `limit` | Read-only `DevMemory.search` over the existing graph snapshot. |
| `graph_impact` | `path` or `file`; `depth` | Reverse dependents and directly linked test files from the existing snapshot. |
| `test` | `test_plan` (supplied by the runner) | Runs `python -m pytest` on exactly the plan's selected files. |

Examples:

```toml
[[steps]]
id = "read-task"
tool = "read"
goal = "Read the runner's docstring."
paths = ["src/ozagia/core"]
[steps.args]
path = "src/ozagia/core/tasks.py"
start = 1
end = 80

[[steps]]
id = "find-tests"
tool = "glob"
goal = "Find task tests."
paths = ["tests"]
[steps.args]
pattern = "**/test_task*.py"
```

### Paths and caps

Step `paths` are the read scope. An empty list means the repository root (still
subject to the exclusions below). `read` requires its target to be inside that
scope; `grep` and `glob` walk only inside it; `graph_impact` accepts only a
path inside it.

The executor rejects absolute paths, `..` traversal, `.git`, `.hg`, `.svn`, the
state directory and every symlink component. Directory walks do not follow
symlinks. A `read` target must resolve inside the repository root, and every
`grep`/`glob` candidate comes from a validated scope root.

Caps are enforced by `LocalPolicy`:

| Field | Default | Effect |
| --- | --- | --- |
| `max_read_bytes` | 262144 | UTF-8 bytes returned by one `read`. |
| `max_read_lines` | 1000 | Lines returned by one `read`. |
| `max_file_bytes` | 4194304 | Files larger than this are refused by `read` and skipped by `grep`. |
| `max_grep_matches` | 200 | Matching lines returned by `grep`. |
| `max_grep_files` | 2000 | Files read by one `grep`. |
| `max_grep_bytes` | 8388608 | Bytes read by one `grep`. |
| `max_glob_results` | 1000 | Paths returned by `glob`. |
| `max_walk_entries` | 100000 | Files considered by a walk. |
| `max_graph_results` | 50 | Results from `graph_search`, dependents from `graph_impact`. |
| `max_graph_depth` | 3 | Maximum `graph_impact` depth. |
| `max_test_files` | 100 | Selected files one test step may run. |
| `test_timeout_s` | 900 | Hard cap on one pytest process. |
| `max_test_output_bytes` | 65536 | Pytest stdout/stderr returned. |
| `max_output_bytes` | 524288 | Tool output serialised into one result. |
| `allow_tests` | true | When false, `test` is blocked. |
| `allow_graph` | true | When false, both graph tools are blocked. |

Reaching a cap sets `truncated` or `output_truncated` in the tool output. A
cap is never a silent full result.

### Graph snapshots

The graph tools load an existing snapshot through
`ozagia.core.memory.DevMemory` and never call `build`, `refresh`, `reingest` or
`write_snapshot`. A missing snapshot, an unreadable snapshot or one whose
freshness is `stale`/`unknown` returns a structured error with
`error_code` (`graph_missing`, `graph_invalid`, `graph_stale` or
`graph_unverified`) and a `details` table naming the path and freshness. The
executor does not build a graph to make the call succeed.

### Test steps

The runner computes the deterministic test plan and attaches it to a `test`
step (`step["args"]["test_plan"]` and `context["test_plan"]`). The local
executor runs pytest only when the plan is available, not refused, not
`mode = "none"` and has at least one selected file. No plan, a refused plan, a
`none` plan or an empty selection returns `blocked`; it never falls back to the
full suite. Selected paths must be existing Python files inside the root.

The command is a fixed argument list, never a shell:

```text
<python> -m pytest -q -p no:cacheprovider --rootdir=<root> --basetemp=<temp> -- FILES...
```

The child environment is a small whitelist: `PATH`, locale and timezone
settings, a temporary `HOME`/`TMPDIR`, `PYTHONDONTWRITEBYTECODE=1`,
`PYTHONHASHSEED=0`, `PYTHONIOENCODING=utf-8`, `NO_COLOR=1` and
`GIT_TERMINAL_PROMPT=0`. Task variables, proxies, credentials and
`PYTEST_ADDOPTS` are not inherited. `-p no:cacheprovider` and
`PYTHONDONTWRITEBYTECODE=1` keep pytest from writing a cache or bytecode into
the repository.

The plan's `timeout_s` is capped by `policy.test_timeout_s`; a timeout returns
an error with `timed_out: true`, no exit code and the partial output. A
non-zero pytest exit is a completed execution: the result is `ok` with
`exit_code` and `output.ok = false`, so the task can report a failing test
instead of pretending the step crashed. A signal termination is an error.

### Blocked tools

`write`, `edit`, `patch`, `shell` and `model` return a structured `blocked`
result in this slice; so do known tools this executor does not implement
(`list`, `stat`, `lint`, `build`). An unknown tool is an `error`. A blocked
result has `status = "blocked"`, `output.blocked = true`, the tool name and a
reason; the runner records it and stops the run.

Approval still belongs to the runner. A mutating step with
`approval.mutate = "ask"` stops for `approval_required` unless the run is
approved (`--approve` or `approve=True`); `deny` always stops. Because
`LocalExecutor` blocks mutation, approving a mutating step only lets it reach
the executor's `blocked` result; it never turns a write, patch or shell into an
action. `approval.network` is likewise policy metadata: the local executor
makes no network call and provides no network tool.

### Result shape and safety

Every result uses only the keys accepted by the task runner: `status`,
`summary`, `usage`, `artifacts`, `evidence`, `output` and `state`. `usage`
reports wall time; token, model-call, cost, memory, disk and pid values are
never invented. `artifacts` is always empty and `evidence` contains labels and
kinds only. File text and grep lines are returned in `output` (bounded by the
caps above), not copied into evidence.

The executor makes no model calls and no network calls. It does not sandbox
the pytest child: tests can do whatever the test code does. Use test and task
classes you trust, and remember that a subset run is still not a full-suite
result.

## Test plan

A `test_policy` field or a `test` step makes preflight compute a plan with
`ozagia.core.testing` (see `docs/testing.md`). The plan is deterministic and
read-only: it names changed files, selected test files and graph node ids,
counts, omissions, fallback reason, graph freshness and `not_assessed`.

Preflight records the plan but never runs a test. A `test` step receives it in
its arguments. `mode = "none"` skips the step and records why; a refused plan
fails preflight only when the task actually declares a `test` step. The
manifest and `preflight` event keep the plan for later evidence, and
`what_was_not_assessed` repeats that no test result exists.

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
manifest.json    run status, spec digest, preflight, gates, budget, output,
                 resource grants (when declared)
artifacts.json   artifact metadata: path, sha256, size, kind, step
output.json      the validated output value, when one was supplied
```

Events include `run_started`, `preflight`, `step_started`, `step_finished`,
`step_skipped`, `evidence`, `artifact`, `gate`, `budget_exceeded`,
`repeated_state`, `gates_skipped`, `resource_grant_claimed`,
`resource_grant_released`, `resource_grant_refused` and `run_finished`. Inputs are recorded by
name and digest, never by value. The manifest records the specification
id/version/digest and title, not the objective or input values. When a test
plan was computed it is recorded in the preflight report, the `preflight`
event and the manifest under `test_plan`; it is a selection, never a test
result.

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
python -m ozagia.tasks_cli validate SPEC [--root R] [--json]
python -m ozagia.tasks_cli run SPEC --input K=V [--dry-run] [--allow-exec]
    [--executor local | --local] [--approve]
    [--approve-grant] [--allow-unbounded-grant] [--allow-keep-grant]
    [--grant-reason TEXT] [--root R] [--json]
python -m ozagia.tasks_cli list [--root R] [--limit N] [--json]
python -m ozagia.tasks_cli show [RUN] [--root R] [--json]
```

`run` is a preview unless `--allow-exec` is passed; `--dry-run` forces a
preview even when `--allow-exec` is present. By default the CLI has no
executor, so a real `--allow-exec` run without one is refused and records that
refusal. `--executor local` (or `--local`) selects the built-in read-only
[`LocalExecutor`](#local-executor-read-only). Tests and host integrations can
still inject an executor:

```python
from ozagia import tasks_cli
tasks_cli.main(["run", spec, "--allow-exec"], executor=my_executor)
```

`--executor local` takes precedence over an injected executor. `validate`
never accepts an executor option and never touches the state directory. A spec
that declares resource grants reads the repository's grant declarations (using
`--root` or the nearest repository/config root) to check the ids.

Exit status is `0` for a valid spec/preview or a successful run, `1` for an
invalid spec, a refused run or a failed/stopped run, and `2` for a usage,
read or write error. The examples under `examples/tasks/` validate and cover a
read-only code review, a repository impact report with a `history/` write and
a paper-notes extraction.

## Safety limits

- `core/tasks.py` itself makes no model calls, no network calls and starts no
  tool. Its preflight runs read-only git plumbing through `core.worktree`.
  The optional `LocalExecutor` starts a pytest child for a declared `test`
  step; it never starts a shell and never calls a model.
- The executor is **trusted**. The runner constrains declarations, reported
  tools and reported artifacts. It does not sandbox the Python process or
  inspect what the executor actually read. `LocalExecutor` validates paths and
  symlinks, but it is not an OS sandbox.
- `approval.network` is passed to the executor as policy; neither this build
  nor `LocalExecutor` makes a network call. The pytest child is not
  network-isolated.
- Resource values are self-reported by the executor and accounted; enforcement
  is not implemented here. The pytest timeout is a wall-clock timeout, not a
  process-group, memory or disk limit.
- Wall-time and approval checks happen between steps. A long-running executor
  call is not interrupted, except by `LocalExecutor`'s own test timeout.
- Evidence is metadata-only. It records labels and hashes, not prompts or file
  bodies. A tool's `output` may contain bounded file text or grep lines; it is
  recorded as the step output, not as evidence. It cannot detect a secret
  embedded in a short summary or evidence label; adapters must avoid that.
- Artifact hashing reads files under the repository to compute SHA-256; it
  does not copy their contents into the record. `LocalExecutor` reports no
  artifacts because it is read-only.
- The worktree policies are repository configuration, not a security boundary.

## What remains

- A model client and per-task model routing from the settings registry.
  `LocalExecutor` covers the read-only `read`/`grep`/`glob`/`graph_search`/
  `graph_impact`/`test` slice; mutating adapters and project-specific tools
  remain open.
- Resource enforcement: cgroup/rlimit limits, a watchdog, own OOM and
  `resource_event` feedback to the task record. The local test timeout is only
  a wall-clock cap.
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
