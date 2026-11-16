# SRE and IaC extension plan

Status: proposal for work after
[the infrastructure context plan](2026-11-16_iac_cloud_context.md). No item
below is implemented by this document. Live-cloud and cluster steps remain
opt-in and separately gated.

Source: [the SRE/IaC recommendations](/tmp/ozagia_sre_iac_feature_recommendations.md),
which analyzed 837 local Kiro/OpenCode sessions (413 keyword-matched, mostly
TipMi) and a single probe of `/home/matt/tipmi/tipmi-iac`. The history is
keyword-filtered and not a matched evaluation; the probe had 175 uncommitted
changes and one run. The counts below are evidence for prioritization, not
proof of production demand or of a token/time improvement.

## What already exists

- The opt-in IaC adapter indexes bounded, source-only Terraform/OpenTofu,
  Kubernetes, Helm/Kustomize, CloudFormation, ARM, Pulumi and GitOps facts;
  it exposes context, locate, dependencies, impact and status through graph
  queries and MCP.
- `ozagia install` targets OpenCode and Claude Code only. There is no Kiro
  installer, steering or hook.
- Shell capture classifies pytest, read, grep, glob, git, ls and find. It does
  not provide a wait-for-job operation.
- The graph has no first-class environment, service-to-deployment chain,
  Terraform root/remote-state edge, or live-state snapshot.
- Edit engines cover Python, Markdown and JSON; YAML is refused and HCL has
  no edit adapter. `edit/txn.py` is the transaction boundary.
- `DevSession` records code-task facts. It does not record environment-scoped
  operations, cloud resource changes or a resumable ops handoff.
- `doctor` is local-only and does not validate AWS CLI command names.
- The IaC adapter reports templated Helm YAML as invalid YAML and does not
  skip `*.backup.last` directories by default.

## Priorities

1. **Kiro integration first.** It is small, removes a harness blind spot, and
   is required before any Kiro-side measurement of the remaining items.
2. **Real-root graph accuracy next.** The TipMi probe found missing
   cross-root links, duplicate backup resources, missing service chains,
   environment-name collisions and templated Helm diagnostics. These are
   offline fixes with immediate retrieval value.
3. **Opt-in job waiting and ops records.** They address the largest repeated
   costs in the history (sleep/poll loops, repeated commands, handoffs), but
   need explicit command/integration safety.
4. **Structural HCL/YAML edits and pre-plan checks.** Useful and local, but
   larger than the graph fixes and should follow the edit-transaction and
   static-check design.
5. **Live environment routes and snapshots last.** They need live cloud or
   cluster reads and remain allowlisted, read-only and explicitly approved.

## Workstreams

### A. Kiro integration

**Evidence:** Kiro ran 64 SRE sessions, including the largest ops session
(4,710 tool calls); no ozagia tool was called in any of the 837 sessions.

**Deliverable:** `ozagia install kiro` writing only Kiro-specific project
files, with idempotent install/uninstall and a `--workdir` option.

- First pin the Kiro version and verify its MCP, steering and
  PreToolUse/PostToolUse configuration locations and event schema against
  its documentation. Do not guess file paths in released code.
- Factor the harness-neutral parts of
  `integrations/claude_code/hook.py` (tool-name mapping, daemon request,
  error fallback, append handling) into a shared hook module. Implement Kiro
  as a thin adapter; reuse the Claude Code mapping where Kiro names match.
- Install an MCP entry that runs the existing read-only graph surface, plus
  optional steering text describing graph tools and the `[ozagia]` marker.
- Hooks must fail open: a daemon or hook error never blocks the agent. Log
  one bounded JSON line under the state dir, never the prompt or tool output.
- Tests: temp workdir, install twice, uninstall removes only ozagia entries,
  existing Kiro settings survive, malformed settings produce a clear error,
  and no network or cloud call is made.

**Gate:** install/uninstall and hook round-trip pass against the pinned Kiro
schema; a Kiro smoke session is measured only under the live-eval gates in
the repository `AGENTS.md`.

### B. IaC graph accuracy on real roots

These extend `core/terraform_graph.py`, `core/deployment_graph.py`,
`core/infra_discovery.py` and the adapter. All remain source-only.

#### B1. Cross-root `terraform_remote_state` links

**Evidence:** a dependencies query on `data.terraform_remote_state.iam`
returned nothing; 82 of 123 live roots read remote state.

- Parse `data "terraform_remote_state" "<name>"` declarations and their
  static `backend`, `config`, `workspace`, and `defaults` references without
  reading state or backend endpoints.
- Add a Terraform root identity (directory with `.tf`/`.tofu` files and
  optional `.terraform.lock.hcl`) as a graph node. Do not treat nested
  modules as roots unless the project config says so.
- Resolve a consumer to a producer only by an explicit
  `[[iac.remote_states]]` mapping or a unique static backend/key match
  (`bucket`, `key`, `workspace`, `region`) supplied in project config. Emit
  `remote_state` edges with the evidence and a confidence; leave ambiguous,
  missing and dynamic mappings unresolved.
- Support `terraform_remote_state` output references (`data...outputs.x`) as
  references to the remote-state data node. Do not fetch or evaluate them.
- Add a two-root fixture, a unique-match case, an ambiguous-match case, and a
  secret-canary case proving state/backend values are not retained.

#### B2. Skip backup and generated directories by default

**Evidence:** 2,111 of 9,811 resources (21%) came from `*.backup.last`
directories and appeared as duplicate hits.

- Extend `core/infra_discovery.py` with a default skip list for
  `*.backup.last`, `*.backup`, `*.bak` and `*.orig` directories/files, while
  keeping `.terraform`, `.terragrunt-cache`, `.pulumi` and `.kube` skipped.
- Make the list explicit and inspectable: `[iac] skip_globs` in
  `.ozagia.toml`, merged with the defaults. Report skipped counts and paths
  (no contents) in `infra.status`.
- Add tests that a backup copy never becomes a node, an explicitly
  configured source directory still works, and a rename/deletion refresh
  does not leave old nodes.

#### B3. Environment facet instead of environment-name matching

**Evidence:** context for “tipmi-api pods killed in dev” returned unrelated
locals named `dev`.

- Add `[environments.<name>]` metadata: display name, paths/roots, account
  alias, region, cluster, namespace, risk level and pipeline names. This is
  configuration, not credentials or live discovery.
- Attach an `environment` facet to nodes from configured path prefixes,
  Kubernetes namespaces, or explicit service mappings. Do not infer an
  environment from a bare path word.
- In context ranking, an environment term from the configured set must match
  the node's environment facet; it must not match an unrelated local,
  resource name or path substring. Keep environment terms out of the generic
  identifier score.
- Add fixtures with locals/variables named `dev`, `stg`, `pprd`, `prd` and
  resources in those environments; assert only the requested environment's
  deployment is returned.

#### B4. Templated Helm manifests are templates, not invalid YAML

**Evidence:** 44 diagnostics reported templated Helm manifests as invalid
YAML.

- Before Kubernetes YAML parsing, detect Go-template markers (`{{`/`{%`) or
  a file under a chart's `templates/` directory. Emit a
  `template_source`/`helm_template_source` node or diagnostic, never an
  invalid-YAML diagnostic.
- Keep already-supported conventional chart/values/template links. Do not
  render templates, run Helm, fetch dependencies or evaluate values.
- Add a chart fixture with `{{ .Values.image }}`, a values file and a
  rendered-looking manifest; assert no invalid-YAML diagnostic and that the
  template is still discoverable as source.

#### B5. Service-to-deployment chain from explicit mappings

**Evidence:** only five chart-values edges existed; locating “tipmi-api pprd
deployment” returned nothing; expected paths such as
`modules/iam/main.tf` and `helm/charts/microservice/values.yaml` were guessed
and did not exist.

- Do not guess service identity from arbitrary values. Add an explicit
  `[services.<name>]` config map with optional `chart`, `values_globs`,
  `image`, `buildspec`, `pipeline`, `namespace` and `environment` fields.
- The adapter creates `Service` nodes and edges to matching
  Helm/Kustomize/Deployment/ServiceAccount/ingress nodes and configured
  pipeline/build files. Mark source-derived links `declared` and
  config-derived links `declared` with `evidence = "project config"`;
  ambiguous or missing targets stay unresolved.
- Provide a project-adapter path for a repository that needs more than the
  generic mapping.
- Add a fixture with a service, chart, two environment values files, a
  buildspec and a pipeline; assert a locate for `<service> <env>
  deployment` returns the source files and the exact missing target does not
  become an invented path.

### C. Long-running job wait operation

**Evidence:** about 4,900 SSM send/get calls, 656 CodeBuild batch reads,
494 CodePipeline state reads, 1,361 sleep/poll commands totalling 207,837 s
of sleep, 61 commands killed at the runner limit, and 29 broken hand-written
poll loops.

**Deliverable:** an opt-in `ozagia job wait` and MCP `dev_job_wait`, plus a
matching shell-capture hint. It waits for one explicitly named job and
returns a short result.

- Support only reviewed providers: SSM command invocation, CodeBuild build,
  CodePipeline execution and `kubectl rollout status`. The first three are
  authenticated remote reads; the last is a cluster read. A local fake
  provider is the default for tests.
- Configuration is an explicit allowlist of provider, executable, argument
  shape and maximum timeout. Do not discover credentials, choose profiles,
  run `terraform init/plan/apply`, or call undocumented endpoints.
- API: `job_kind`, `job_id` (or pipeline/build/command identifiers),
  `timeout_s`, `poll_interval_s`, `max_output_lines`. Return status, terminal
  phase, elapsed time, last bounded log lines and a source/provenance field.
- Poll with exponential backoff and a hard deadline; stop on terminal state;
  never return raw environment, tokens, full logs or arbitrary metadata.
  On timeout return a non-terminal status, not an error that hides the job.
- Commands are argv arrays, never shell strings. Use the existing daemon
  lock only around graph work; waiting must not hold it.
- In shell capture, recognize a successful `send-command`/start call and
  append a compact hint with the job ID; do not replace or intercept the
  command. Keep the existing “no silent provider call” rule.
- Tests: fake terminal success/failure, malformed provider output, timeout,
  cancellation, disallowed provider/executable, oversized output, and a
  credential-canary test proving nothing is logged or persisted.

**Gate:** no live provider call in tests; a live trial needs explicit
approval, a named account/region, read-only permissions and a recorded
session. Do not claim sleep/time savings from one trial.

### D. Environment profiles and cluster routes

**Evidence:** 107 sessions re-checked identity/kubeconfig early; 390
`sts get-caller-identity` and 414 `eks update-kubeconfig` calls; 257
`describe-instances` calls looked up bastions; 1,416 `kubectl` commands were
hand-wrapped in SSM; one bastion was used 3,711 times; 56 commands failed on
expired AWS SSO sessions.

**Offline part:** B3's `[environments]` nodes and a bounded
`infra.environments`/`dev_environment` hint. Include account alias, region,
cluster, namespace, bastion reference, access method, pipelines and risk.
Never store secrets, private keys or full kubeconfigs. The hint must say the
data is declarative and may be stale.

**Live part (gated):**

- `ozagia env check` performs at most one allowlisted identity probe, caches
  the result for a short TTL, and reports an expired SSO session once with a
  login instruction. It must not retry in a loop or read credential files.
- `ozagia env kubectl` constructs a reviewed `ssm send-command` for
  `kubectl` only, with pinned document name, timeout, output cap and
  configured bastion/namespace. Default is a dry-run that prints the request
  without sending it. No arbitrary shell through the bastion.
- Distinguish three categories in status and docs: offline parser, local
  executable that may run providers/plugins, and authenticated remote read.
  Do not silently fetch dependencies or contact a cluster.

**Gate:** dry-run output is byte-identical to the reviewed request; a live
read needs explicit approval and a named context/namespace; no writes, no
Secret reads, no apply/destroy, no state mutation.

### E. Read-only live-state snapshots and diffs

**Evidence:** ELB target health, ECR images and repeated status reviews;
`pprd-status.md` was read 98 times and often stale.

**Do not start with live commands.** First accept a supplied, sanitized
snapshot artifact with a version and provenance (for example a small JSON
document with deployed image, desired/ready replicas, target health and last
pipeline run). Import it through a versioned reader and attach it to graph
nodes by explicit environment/service keys. Store only allowlisted fields
under `.ozagia/live/` with source, observation time and TTL; never store
credentials, Secret values or arbitrary resource properties.

Then add opt-in read-only providers for named services/regions, with an
explicit allowlist, pagination/output bounds, one observation per run and no
writes. Add `live diff` since the previous snapshot and a generated status
summary; mark every value with its observation time and “not live now” when
older than the TTL.

Tests use supplied artifacts and a fake provider. The live gate is separate:
read-only IAM permissions, named account/region/environment, explicit
approval, and an auditable record. Do not infer deployed state from source
when no snapshot exists.

### F. Structural HCL and YAML edits, and pre-plan checks

**Evidence:** about 600 patch-verification failures on `.pkr.hcl`,
`roles.auto.tfvars` and Markdown; edit engines did not cover HCL/YAML. Plans
failed on undeclared locals, uninstalled modules and missing providers.

- Add an HCL edit adapter addressed by block/attribute path:
  `set_attribute`, `remove_attribute`, `add_block`, `remove_block` and a
  bounded variable/locals replacement. Preserve comments and formatting;
  refuse ambiguous paths; preview by default; use `edit/txn.py` for the base
  hash, atomic write, backup and journal.
- Add a YAML path adapter. Prefer an optional round-trip parser (for comment
  preservation) behind an `edit-yaml` extra; without it, refuse with a clear
  message rather than falling back to a text patch. JSON keeps its existing
  span-preserving path.
- Add `ozagia check iac [--root <root>]`: graph-based static checks for
  undeclared locals/variables, missing local module directories, missing
  provider requirements and references that do not resolve. Report source
  locations and evidence; do not run `terraform init`, `plan`, `validate`,
  providers, plugins or project code.
- Tests: preview/apply/rollback, comments preserved, ambiguous block path,
  base-hash conflict, missing parser extra, and canary values not returned.

**Gate:** every write goes through `edit/txn.py`; no target writes outside
the requested edit and the state directory; no provider execution.

### G. Resumable ops records

**Evidence:** 57 resume/OOM/handoff prompts; 21% of shell commands exactly
repeat an earlier command in the same session; `dev_session_*` tracks code
tasks, not environment-scoped operations.

- Add append-only ops records under `.ozagia/ops/` with a versioned schema:
  timestamp, harness/session, environment, command/job kind, bounded result,
  resource identifiers supplied by the caller, findings, changes, approvals
  and next steps. Never store prompts, full command output, credentials or
  Secret values.
- Add `dev_ops_record`/`dev_ops_resume` MCP tools and matching CLI commands.
  Resume returns a bounded summary plus links to source nodes/snapshots; it
  does not replay commands.
- Keep code-task `DevSession` separate. A record may reference a session ID;
  do not merge the schemas.
- Tests: replay/read across Kiro/OpenCode-shaped callers, redaction canaries,
  bounded retention, corrupt records refused explicitly, no network.

### H. Offline AWS CLI validation and optional emulator pre-flight

**AWS CLI validation (offline, low risk):**

- Optional `botocore` extra. Validate `aws <service> <operation>` names and
  basic enum shapes against the local service model; never import credentials
  or make a network call. If the extra is absent, report “unavailable”.
- Surface a hint only; do not replace the user's command. Add tests with a
  fake model and a missing-extra path.

**AWS Emulator pre-flight (opt-in, later):**

- Keep this out of indexing and out of normal shell capture. A separate
  `ozagia preflight` command may start a pinned emulator in a scrubbed
  environment (`AWS_EC2_METADATA_DISABLED=true`, unset profiles/keys/tokens,
  telemetry off), apply roots in graph-derived dependency order, and map
  validation errors back to source nodes.
- Never point pre-flight at a real account by default. Label services as
  covered/uncovered by the emulator's fidelity data; do not treat an
  emulator pass as real IAM or Kubernetes behavior. Pin a version and
  document provider-version limits. Until cross-root edges and dry-run
  tests exist, this is a proposed experiment, not a dependency.

## Cross-cutting contracts

- **Configuration:** add `[environments]`, `[services]`, `[iac]` (remote
  states, skip globs), `[live]`, `[jobs]` and `[edit]` sections with strict
  key allowlists and actionable errors. `load_project_config` and `doctor`
  report them; no secret fields.
- **Graph:** new node families (`Environment`, `TerraformRoot`, `Service`,
  `RemoteState`, `LiveSnapshot`, `OpsRecord` where appropriate) use
  namespace/versioned keys and repository-relative paths. Edges record
  provenance (`declared`, `inferred`, `resolved from imported artifact`) and
  evidence. Removing/renaming a source removes its derived nodes and edges.
- **Queries/MCP:** keep read-only, bounded and freshness-aware contracts.
  Add `infra.environments`, `infra.services`, `infra.roots`,
  `dev_job_wait`, `dev_ops_record`, `dev_ops_resume` and live-snapshot
  queries only when their implementation exists; do not publish placeholder
  tools.
- **Security:** source-only indexing remains default; live commands are
  opt-in, allowlisted, read-only by default, bounded and separate from source
  graph claims. Secret canaries must be absent from dumps, queries, hints,
  journals, logs and snapshots. Prompt/history text is never persisted.
- **Performance:** carry forward the per-file parse cache, token/type index
  and query cache. New providers must report cold/warm latency and cache
  misses; waiting must not hold the graph lock.
- **Failure behavior:** missing parsers, botocore, Kiro settings, optional
  YAML editor, credentials and permissions produce explicit unsupported or
  unavailable states, never silent fallbacks or guessed data.

## Evaluation

Build an offline fixture corpus that mirrors the observed failures:

- two Terraform roots with remote-state producer/consumer, unique and
  ambiguous mappings, remote modules and local modules;
- backup copies, generated state, templated Helm, values per environment,
  chart templates and Kustomize overlays;
- environments whose names collide with locals/resources; service mappings
  with missing and duplicate targets;
- invalid YAML/HCL, unknown variables, provider aliases, `for_each`/`count`,
  CRD/CR, RBAC and secret canaries;
- tasks for locate/context/dependencies/environment/service queries with
  frozen expected sources and edges.

Measure source recall/precision, edge precision/recall, ambiguity and
unsupported rates, path-guess errors, context characters and (clearly
labelled) estimated tokens. Then, only with approval, compare matched Kiro and
OpenCode sessions with and without the new features, recording tool calls,
input/output tokens, elapsed time, approval prompts, repeated commands,
failed paths, task correctness and cold/warm preparation. Do not treat tool
call counts as value; the earlier pilot showed more captured calls can cost
more tokens.

## Implementation order

1. Kiro install/hook/MCP skeleton and shared harness-neutral hook extraction.
2. Offline graph accuracy: backup skips, templated Helm classification,
   environment facet, remote-state edges, service mappings.
3. Offline environment hints and `infra.environments`/`infra.services`
   queries.
4. Opt-in job wait plus shell hint and ops records.
5. HCL/YAML edits and static pre-plan checks.
6. Live environment probe/cluster route, live snapshots and diffs.
7. AWS CLI validation, then the emulator pre-flight experiment.
8. Matched evaluation and rollout decision.

Each step lands with tests, docs, secret canaries, `ruff` and the full suite.
Do not enable a new live provider or call a cluster/account merely because
this plan exists; each live phase needs its own explicit approval and scope.

## Implementation progress (2026-11-16)

Kiro is being integrated on the main line separately; this branch does not
duplicate it. The remaining offline workstreams are implemented:

- **IaC accuracy:** Terraform root nodes and remote-state edges (explicit and
  unique static matching), default backup/generated skips with status
  reporting, environment facets, templated Helm classification, and
  config-driven service-to-deployment links.
- **Jobs:** library and CLI wait providers for SSM, CodeBuild, CodePipeline and
  `kubectl rollout`, disabled by default; shell capture emits a wait hint for
  newly started jobs.
- **Environments:** declarative `[environments]` profiles, `ozagia env
  list/show`, `dev_environments`, and environment-aware context/locate filters.
- **Live snapshots:** strict imported snapshot schema, TTL/diff helpers,
  `.ozagia/live/` save/read, and MCP/CLI import/diff/save tools. No live
  provider is implemented.
- **Edits/checks:** HCL block/attribute editing, optional comment-preserving
  YAML editing, and source-only `ozagia iac check`/`dev_check_iac`.
- **Ops records:** append-only redacted receipts and environment-scoped
  resume summaries, exposed through MCP and CLI.
- **Offline validation/preflight:** `dev_aws_cli_check`, AWS CLI shell hints,
  and a dry-run emulator plan. Execution requires an injected runner and two
  explicit opt-ins; this branch does not run an emulator.

Validation on this branch: full suite `1779 passed, 39 skipped` (the optional
`ruamel.yaml` round-trip cases are included locally); `ruff check src tests`
passed; benchmark and scale numbers are in
[the benchmark results](../results/iac_bench_2026-11-16.md).

Still gated or unimplemented: live job-provider trials, the live environment
identity/cluster route, live snapshot collection, real emulator execution,
Kiro-side measurement on the main line, and matched model/token evaluation.
No live cloud, cluster, model or emulator call was made while implementing
this progress.
