# Self-graph A/B evaluation plan — 2026-11-17

## Question and scope

Measure whether Ozagia's read-only self-graph makes it easier and faster for a
local coding agent to answer concrete questions about Ozagia's own capabilities,
surfaces, dependencies, tests and documentation, while preserving correctness.
Use OpenCode with the locally served `vllm/qwen3.8-27b` model. This evaluates
the landed M1 capability catalogue, static inventory and `dev_capabilities`
MCP tool. It does not evaluate model-side maps, task preflight, edits, or a
claim that the catalogue is exhaustive.

The primary comparison is a plain OpenCode session against the same session
with Ozagia's `dev_capabilities` read-only MCP tool. The plain arm retains the
checkout, ordinary shell/read tools, and the same public CLI, source and docs;
the task prompt does not tell either arm which answer to give or which files
contain it. Both arms receive identical prompts and acceptance instructions.
This measures the incremental value of the structured interface over ordinary
repository access, not the value of hiding the source from baseline.

## Preconditions and safety

- Run against one immutable Ozagia commit in disposable local clones. Record
  `git rev-parse HEAD`, dirty status, Python version, OpenCode version, model
  identifier, local endpoint/runtime and relevant config hashes. The current
  checkout is dirty; do not use it as a task worktree or overwrite its changes.
- Confirm `ozagia capabilities list --json`, `show` and `audit` work, and that
  the MCP inventory exposes `dev_capabilities`. Verify the tool dispatches to
  the same bounded read-only projection. Use a local copy of the current
  catalogue in each clone; do not reindex or refresh it during a session.
- Disable network tools and nonessential global OpenCode plugins for both
  arms; use identical model parameters, tool permissions, system configuration
  and session limits. Record any unavoidable global configuration. Never put
  secrets or private endpoint credentials in prompts or results.
- All requests stay on the local Qwen endpoint. Do not use a hosted fallback.
  No target code execution, install, network, daemon startup, write, edit or
  test run is needed. The model may read source, docs and run the documented
  read-only CLI; the MCP arm may call only list/show/audit. Use read-only shell
  restrictions for both arms and reject any run that attempts mutation or
  network access.
- Preserve raw OpenCode event logs and redacted result JSON under a new
  `/tmp/ozagia-self-graph-ab-<run-id>/` directory. Do not put logs in the
  repository. Redact prompt/config secrets before retaining or sharing logs.

## Tasks and held-out scoring

Create a small versioned task file and an independent assertion file before
running any model. The assertions are held out from the model and prompt; hash
both files and record the hashes in the run report. A human who did not author
the model answers scores against the assertions and cited repo evidence. Every
task asks for concise JSON with claims, evidence paths or capability IDs,
uncertainty/coverage caveat and an explicit `unknown` when evidence is absent.
Do not accept an answer solely because it is fluent or repeats the tool output.

Use six tasks spanning the self-graph and ordinary Ozagia discovery:

1. **Capability trace:** locate a named read-only graph capability and report
   its implementation, CLI/MCP/daemon exposure, dependencies, test and doc
   references. Assert each fact separately against the pinned catalogue and
   source; allow `unknown` where the graph labels coverage unknown.
2. **Audit interpretation:** report what the audit can establish, identify the
   declared-vs-observed distinction, and cite one concrete finding or state
   that the current audit has none. Score the exact output/state and whether it
   avoids calling partial coverage exhaustive.
3. **Capability comparison:** compare two preselected capabilities by
   side-effect class, authorization and exposed surfaces. Assertions specify
   the correct values and forbid inferring permission from exposure.
4. **General code navigation:** identify where the graph snapshot is built and
   where stale/unknown freshness is handled in an existing project-graph
   workflow. Score paths and supported explanation against source; the
   self-graph tool is not expected to answer unrelated project-graph details.
5. **General edit safety:** explain the existing edit transaction's preview,
   base-hash and atomic-write safeguards, with source/doc evidence. This tests
   ordinary Ozagia knowledge where self-graph assistance may not help.
6. **Negative/unknown control:** ask for a deliberately nonexistent capability
   or an unsupported completeness guarantee. Correct behavior is explicit
   not-found/unknown with no invented implementation or claim of exhaustiveness.

Select actual capability IDs and source facts from the pinned checkout before
authoring the assertion file. Keep tasks 1–3's scored facts independent of the
literal prompt wording. Do not reuse answers or models' prior output to tune
the task set after a run starts. If a fact changes at a new commit, rebuild the
assertions and version the dataset instead of silently editing expected output.

## Arms and controls

| Arm | Available assistance | Purpose |
| --- | --- | --- |
| `baseline` | OpenCode native tools, checkout, same task and normal shell/read access. No Ozagia MCP. | Ordinary repository-access baseline. |
| `self_graph` | Same as baseline plus the existing Ozagia MCP server and `dev_capabilities`. | Incremental value of the structured self-graph surface. |
| `cli_control` (smoke only) | Same as baseline, with the task prompt explicitly naming the equivalent read-only `ozagia capabilities` CLI commands. | Checks whether the MCP arm's effect is tool availability versus explicit command discoverability. |

The CLI control is not part of the primary estimate because its prompt differs;
report it separately. It is a useful first smoke if the MCP server cannot be
reliably launched by OpenCode. Do not compare self-graph to `graph_intent` or
the current `dev_memory` arm as-is: `dev_memory` builds a project index and
exposes a broader MCP tool set, so both the treatment and setup costs differ.

## Smallest actionable run sequence

1. **Offline contract check (no model):** run the existing self-graph unit
   suite and CLI list/show/audit twice, compare canonical JSON byte-for-byte,
   verify pagination, and confirm audit unknown/partial states remain visible.
   Confirm the MCP tool schema and handler produce equivalent payloads for the
   same list/show/audit requests. This is a gate, not A/B evidence.
2. **Harness smoke (one task, one paired repeat):** one task from each class
   (one of 1–3 and task 5), baseline and self_graph, one run each. Verify local
   model identity, tool launch/call logs, no unsafe calls, held-out scoring and
   all metric fields. Fix harness issues before the main sample; do not use
   smoke scores to select tasks.
3. **Pilot:** all six tasks, two arms, two repetitions = 24 sessions. Randomize
   arm order within each task/repetition using a recorded seed. This is the
   minimum useful live comparison for gross regressions and feasibility; it is
   not enough to establish small success-rate changes. If the paired pilot is
   noisy or has fewer than 10 verified outcomes per arm, expand to 5 repeats
   (60 sessions) before making a rollout claim. The six task prompts and model
   config remain frozen across the expansion.

Before live execution, the experiment operator separately checks local model
health with a no-inference endpoint/health check if supported. The actual
sessions require the existing explicit live-run authorization and the local
eval safeguards (`--live` plus `OZAGIA_EVAL_LIVE=1` if run through the harness).
No model call is part of this plan-writing task.

## Measurement and analysis

Record per session: verified assertion score and task pass/fail; critical false
claims; evidence citation precision; explicit unknown correctness; total wall
time; preparation/index time; time to first correct evidence and to verified
answer; input/output tokens reported by OpenCode; model calls; tool calls by
name; MCP calls, errors, denials and fallback; context/prompt size if available;
timeouts; and local GPU/model-server utilization or queue time when available.
Local inference has no meaningful `$0` cost: report GPU seconds or endpoint
compute time where measurable, otherwise mark compute cost unknown. Preserve
the driver-reported and independently wall-clock measures separately.

Primary outcomes are (1) verified task completion rate and (2) paired
time-to-verified-answer on tasks both arms complete. Secondary outcomes are
token use, evidence quality, tool-use overhead, setup cost and unknown-state
handling. Show each paired task/repetition, paired difference and median; show
all failures and timeouts. Do not average only successful runs without also
showing completion rate. A model self-report is not an outcome.

Report cold and warm separately. Cold means a new process and new clone with no
task-local graph or OpenCode session cache; warm means the same pinned inputs
and configuration after one unscored setup/session has primed ordinary OS and
model-server caches. Randomize arm order in each block, and alternate which arm
gets cold first. Do not claim an index-cache warm benefit: self-graph collection
is source inventory, and no project graph build is part of the primary arm.
Report startup/handshake, task execution and any one-time setup as separate
components. If endpoint cache state cannot be reset or observed, say so and
interpret warm/cold as process/workdir state only.

The existing `ozagia.eval.agent` supports OpenCode and paired baseline vs
`dev_memory`/`graph_intent`, but does not currently define a `self_graph` arm;
its `dev_memory` setup builds a project index. Add a narrowly scoped arm to the
harness (or an equivalent small runner) that installs only the MCP config and
does not call `prepare_dev_memory`. Reuse its task isolation, OpenCode event
timing/token parsing, and redacted report conventions where possible. Keep
this harness change outside this plan file and avoid using Ocelot break/fix
tasks: they test code repair, not Ozagia self-knowledge. If an arm is added,
record its code commit/hash, harness version and exact command.

Suggested report fields:

```text
run_id, commit, task_file_sha256, assertion_file_sha256, model, endpoint,
opencode_version, arm, task_id, repetition, cache_block, randomized_order,
prep_s, wall_s, time_to_first_evidence_s, time_to_verified_answer_s,
tokens_in, tokens_out, model_calls, tools_by_name, mcp_calls, mcp_errors,
unsafe_attempts, outcome, assertion_score, critical_false_claims,
evidence_precision, unknown_handling, timeout, gpu_seconds_or_unknown
```

## Confounders and interpretation

- Local Qwen output varies with sampling, prompt cache, batching and GPU
  contention. Fix sampling parameters and context limit; record unavailable
  settings. Interleave arms, use paired prompts, and avoid concurrent model
  workloads.
- MCP may alter tool-selection behavior and token overhead; that is part of
  the treatment. Keep descriptions and tool schemas frozen. Record calls and
  distinguish useful calls from malformed, repeated or unused calls.
- The capability catalogue is hand-declared and incomplete by design. Score
  correct provenance and honest partial/unknown reporting, not raw node count
  or a claim that audit proves all Ozagia behavior is represented.
- Self-graph tasks are intentionally favorable to capability search. The
  general navigation/edit-safety and negative-control tasks show where the
  treatment does not help and whether it encourages unsupported claims.
- With six tasks, results support only a feasibility/gross-effect decision.
  If success is tied, inspect paired time/tokens and operator effort, and add
  tasks or repeats before claiming general improvement. Preserve unsuccessful
  pilot results; do not replace them with a tuned follow-up without labeling it
  as a new experiment.

## Decision rule

Before the first live session, freeze scoring and choose a practical minimum
benefit: the self-graph arm must have no increase in critical false claims or
unsafe attempts, no lower verified completion rate, and show either a
repeatable reduction in time-to-verified-answer or materially better evidence
and unknown-state accuracy that outweighs its setup/tool overhead. Treat
one-off wins, tool-call counts alone, and six-task p-values as insufficient.
Publish raw paired outcomes, limitations and the exact artifact hashes before
changing defaults. If ordinary access ties or wins, keep the MCP tool available
as an optional deterministic interface and report the measured operator/tool
overhead without a broad performance claim.
