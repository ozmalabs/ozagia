# Infrastructure and cloud context plan

## Outcome and scope

Help an agent answer “what defines this infrastructure, what depends on it,
and what might this change affect?” using compact, source-attributed context
before it starts opening many files. First cover Terraform/OpenTofu and
Kubernetes, then Helm/Kustomize and deployment links; add CloudFormation,
Pulumi, CDK and other formats in measured increments. Static source describes
intent, not deployed state. Never report it as live state or promise that an
impact traversal is complete.

This is a proposal, not a claim of existing support or measured savings.
Prioritize context quality and end-to-end agent results over counting intercepted
commands. The Ocelot pilot showed why: more captured work and context can
increase total tokens and elapsed time.

## Current starting point

* `src/ozagia/index/tag_languages.py` has no HCL or YAML language backend;
  `core/search.py` discovers source suffixes from that registry, so merely
  building an IaC graph will not make infrastructure files searchable by the
  current proactive-context search.
* `adapters.py` provides indexer, graph-query and MCP-tool registration.
  Existing adapter reingest (`core/reingest.py`) removes and rebuilds adapter
  nodes on a relevant change; it is a useful first implementation but needs a
  benchmark and may need per-file ownership and invalidation before large
  repositories can be called incremental.
  Adapter indexers currently receive only the existing source categories, so
  an IaC adapter must discover eligible files under `ctx.root` while respecting
  skips, or the index context must gain a generic text-file discovery API.
  Repo-map tags do not themselves create typed DevMemory resource nodes.
* `core/openapi.py` offers a precedent for bounded parsing and an optional safe
  YAML extra, but multi-document Kubernetes YAML, CloudFormation intrinsic
  tags, spans, duplicate keys and alias policy need explicit support; do not
  assume its current parser handles them.
* `core/proactive_context.py` currently searches code with a character cap,
  not a model tokenizer. The OpenCode hook inserts context on each request.
  Infrastructure retrieval needs a separate domain-aware candidate source,
  relevance gate and an end-to-end token budget, not a second unconditional
  block of context.

## Graph and context contract

Create versioned, namespaced identities including repository-relative path,
module or deployment scope, dialect, and declaration identity. Include source
spans/pointers, extractor version, source digest, provenance, graph freshness,
confidence and unresolved references. Deterministic keys must not collide for
nested Terraform module instances, environments, Kubernetes namespaces,
clusters or duplicate manifest names. Preserve links to the owning file;
remove old nodes and inbound/outbound derived edges on deletion or rename.
Label edges `declared`, `inferred`, or `resolved from imported artifact` and
retain the scope and evidence of each inference. Distinguish source structure,
planned changes and observed live state; no inferred selector or name match
implies a deployed relationship.

The smallest useful queries are:

* `infra.locate`: resolve an address, type, label, namespace, module or service
  to declarations and source locations, including ambiguous matches.
* `infra.dependencies` and `infra.impact`: bounded upstream/downstream traversal
  with depth/result limits, unresolved edges and explicit completeness status.
* `infra.context`: combine the current user request and bounded recent history
  with the relevant declarations, relationships, definitions and evidence;
  output compact citations before excerpts. If ranking confidence is low,
  freshness is unknown, or the token budget is exhausted, return no hint and a
  reason rather than unrelated content. Distinguish estimated characters from
  real tokenizer counts. Do not store prompt/history in journals.

Expose the same read-only contracts through `DevMemory`, the daemon and MCP;
connect to the existing OpenCode hook when present. Cache by query, graph
version, environment and budget; invalidate on owned changes and bound TTL for
external changes. Measure cache misses and lock wait. Never log raw IaC source,
secrets, prompt, history, plan or state values.

## Work packages and gates

### 0. Evaluation and safety foundation

1. Assemble small, licensed or synthetic repositories with labeled questions
   and expected source locations/edges: multiple Terraform modules, OpenTofu
   dialect/version differences, Kubernetes namespaces, Helm/Kustomize overlays,
   and a mixed service-to-deployment case. Include misleading names, unknown
   variables, remote modules, provider aliases, `for_each`/`count`, selectors,
   templates, custom resources, invalid YAML/HCL and secret canaries. Freeze
   the dataset and hold out tasks before tuning.
2. Establish a baseline with the current agent integration and common native
   tool workflows. Record cold/warm build and query wall time, p50/p95 latency,
   memory/index bytes, dependency-edge precision/recall, top-k source recall,
   ambiguity and stale/unsupported rates, source bytes returned, tool calls,
   task correctness and actual input/output/cached model tokens and elapsed time.
   Separate one-time indexing cost from amortized task costs. Compare the same
   tasks, model, tool versions and environment; report distributions and
   failures, not just averages. Non-live deterministic evaluation comes first;
   any model pilot follows the repository's explicit live approval gates.
3. Implement default-deny ingestion of `.tfstate*`, saved plans, backend
   credentials, kubeconfigs, private keys, Kubernetes `Secret` data/stringData,
   known environment/credential files and other secret-bearing artifacts. Treat
   variables, YAML values, comments and generated artifacts as potentially
   sensitive; use allowlisted structural fields and metadata rather than
   relying on regex redaction. Ensure exclusions apply consistently to graph,
   tag cache, search snippets, hints, dumps, logs and backup paths. Tests seed
   canaries and inspect all output and persistent files. No raw secret value is
   required to answer structural graph questions.

**Gate:** baseline reproducible; unsupported types and missing optional parsers
reported; canary and isolation tests pass before infrastructure ingestion is
on by default. Specify resource budgets and acceptance thresholds from the
baseline before fitting/running the comparative evaluation.

### 1. Terraform and OpenTofu source graph

Implement an optional HCL parser backend; choose and pin a maintained parser
only after verifying its grammar, licensing, spans and malformed-input behavior.
Keep core stdlib-only and make missing parsers visible. Support `.tf`,
`.tf.json`, `.tofu` and `.tofu.json` as distinct dialect-aware inputs where
applicable; honor OpenTofu's documented precedence rather than merging files
blindly. Start with modules, providers and aliases, managed resources, data
sources, variables, locals, outputs and explicit/implicit references. Resolve
local module sources only within the repository without executing code or
fetching modules. Model dynamic expressions, module instances and remote module
sources as unresolved when static analysis cannot determine them. Capture
source spans and safe identifiers, not arbitrary attribute values.

Add IaC file discovery independent of programming-language tags, task-aware
search and graph-aware rank fusion. A request mentioning a Terraform address
should find its declaration, direct input/output references, parent module and
relevant affected nodes inside one shared budget. Do not substitute `terraform
plan`, `show`, `graph`, `validate` or provider operations with graph output:
their runtime semantics, options and failure behavior differ. Use existing
graph/CLI output as an optional comparison oracle on inert test fixtures.

**Gate:** fixture recall and edge precision meet thresholds fixed in package 0;
exact deletion/rename reingest matches cold build; no false claim of complete
resolution; cold and warm performance measured against baseline.

### 2. Kubernetes, Helm and Kustomize

Add opt-in, bounded YAML/JSON parsing for multi-document Kubernetes manifests.
Recognize `apiVersion`, `kind`, metadata name/namespace and a deliberately
small allowlist of structural references. Start with workload-to-Service
selector matches (explicitly inferred, respecting namespace and selector
semantics), workload-to-ServiceAccount, ConfigMap/Secret *names only*, ingress
to Service, RBAC subjects/role references, and CRD declaration to custom
resource where schema scope is known. Scope unknown and templated names remain
unresolved, not guessed. Never ingest Secret payloads, env literal values or
arbitrary annotations.

Model Helm charts, values-file paths, chart dependencies and templates as
sources; do not treat unevaluated templates as Kubernetes objects. Model
Kustomize bases, overlays, patches and image references without fetching remote
bases or executing plugins. Link rendered results to source only when there is
reliable provenance; mark generated and imported material as such. Bridge
application repo files to deployments through verifiable service, image and
manifest references, with ambiguity surfaced.

**Gate:** namespace isolation, selector correctness, multi-doc parsing, alias
limits, duplicate keys, malformed/templated YAML, unavailable schemas and
external links covered; compare clean build with edits and deletes; secret
canaries stay out of caches and hints.

### 3. Cloud and deployment ecosystem

Add CloudFormation resource, parameter, output, intrinsic-reference and stack
relationships using a dedicated safe parser with an explicit whitelist for
CloudFormation tags; resolve only static references. Support Azure Bicep/ARM
and Google deployment or Config Connector manifests as separate adapters after
fixture demand is established. Integrate AWS CDK and Pulumi first through
existing language tags and explicitly supplied, versioned synthesized
artifacts, with source-to-generated links only where evidence exists. CDK
synthesis and Pulumi programs can execute project code: never run them as part
of indexing. No hidden imports of the target project.

Then add Argo CD/Flux application-to-source-to-cluster-target metadata,
Crossplane compositions/claims, GitHub Actions or equivalent deployment
workflow links, Dockerfile image provenance, and Ansible inventories/playbooks
in prioritized slices. Do not infer live cluster membership or execution
success from declarative CI/GitOps configuration.

**Gate:** each adapter has format/version fixtures, scope/ambiguity tests,
explicit unsupported states, secrets checks, incremental rebuild equivalence,
and demonstrated benefit on a held-out task before default enablement.

### 4. Opt-in external-tool and cloud observations

Prefer importing user-supplied sanitized artifacts over running tools. Accept
structured findings from existing scanners, schema exports and plan summaries
only through versioned readers with provenance, size/depth limits, and
allowlisted fields; preserve rule identity, severity, location, tool version
and observation time. Treat artifacts as untrusted and potentially sensitive.
Do not persist or expose plan/state values by default: Terraform's JSON output
can reveal sensitive values even when terminal output hides them.

For optional command integrations, use a reviewed executable/argument
allowlist, a caller-selected working directory, explicit timeout and output
limits, and clear opt-in. Distinguish offline parsers, local executables that
may run providers/plugins or project code, and authenticated remote reads.
Do not silently call a provider, discover credentials, fetch dependencies,
contact a cluster, or run `terraform init/plan/validate`, `pulumi preview`,
`cdk synth`, Helm rendering or scanner hooks during indexing. Optional cluster
reads must identify context, namespace, identity, data scope and freshness;
request only necessary RBAC permissions and exclude Secrets. Server-side dry
run can contact the API and admission components, so it belongs to the
explicit remote category. No interception of apply/destroy, state mutations,
cluster modifications, arbitrary pipelines or commands whose behavior cannot
be preserved. Keep graph queries distinct from native command output.

**Gate:** injection, malformed artifact, credential leakage, timeout, changed
context and permissions tests; no writes outside `.ozagia/` except an edit the
caller requested, and all such edits use the existing transaction machinery.

## Companion tools and integration strategy

| Tool | What it already provides | Proposed ozagia relationship |
| --- | --- | --- |
| [terraform-ls](https://github.com/hashicorp/terraform-ls/blob/main/docs/features.md) | Terraform editor definitions, references, diagnostics and completions. | Reuse its LSP results as optional, versioned evidence; retain repository-wide context and impact navigation in ozagia. |
| [Checkov](https://www.checkov.io/2.Basics/CLI%20Command%20Reference.html), [Trivy](https://trivy.dev/docs/dev/docs/scanner/misconfiguration/), [KICS](https://docs.kics.io/latest/integrations/) | IaC security and policy scanning with CLI/CI integrations. | Import supported structured reports, preserve provenance, and link findings to graph nodes; do not duplicate their rule libraries. |
| [Infracost](https://www.infracost.io/docs/faq/) | Infrastructure cost estimation and policy. | Import an explicitly supplied estimate, identify assumptions and link it to affected resources; do not imply a static source graph can calculate a bill. |
| [kube-linter](https://github.com/stackrox/kube-linter/blob/main/docs/README.md), [cfn-lint](https://github.com/aws-cloudformation/cfn-lint) | Kubernetes and CloudFormation validation and lint rules. | Surface report locations and affected dependencies; use the native tools as validation authorities. |
| [Terraform graph](https://developer.hashicorp.com/terraform/cli/commands/graph), [HCP Infragraph](https://developer.hashicorp.com/hcp/docs/infragraph) | Terraform dependency visualizations and hosted infrastructure metadata. | Add cross-format, source-level navigation and compact agent context; treat hosted/runtime observations as a separately sourced, opt-in view. |

* Terraform/OpenTofu graph and provider-schema JSON are primary references and
  useful opt-in oracles; language-server diagnostics can be surfaced with
  source locations. Graph output is dependency ordering, not a replacement for
  runtime state. Terraform provider schemas are versioned; reject unsupported
  major versions. [Terraform graph](https://developer.hashicorp.com/terraform/cli/commands/graph),
  [provider schema](https://developer.hashicorp.com/terraform/cli/commands/providers/schema),
  [OpenTofu compatibility](https://opentofu.org/docs/intro/migration/).
* Kubernetes OpenAPI/`kubectl explain` supplies schema information for a
  particular server and version, not proof of the contents of a repository;
  import offline schemas first and attach cluster/version provenance for live
  use. [kubectl explain](https://kubernetes.io/docs/reference/kubectl/generated/kubectl_explain/).
* Reuse security, policy, cost and lint findings from specialist tools rather
  than reproducing their engines; provide navigation from a finding to source,
  affected nodes and related changes. A finding is an observation of a
  particular scan, not a guarantee that the system is safe or correctly priced.
* Terraform/OpenTofu plans and state, and Pulumi exports, may contain secrets;
  avoid ingesting raw values. [Terraform show](https://developer.hashicorp.com/terraform/cli/commands/show),
  [OpenTofu state](https://opentofu.org/docs/language/state/sensitive-data/),
  [Pulumi export](https://www.pulumi.com/docs/iac/cli/commands/pulumi_stack_export/).
* The AWS CDK cloud assembly is a versioned synthesized artifact; an app can
  execute to produce it. Ingest a supplied assembly, not the app itself.
  [AWS CDK assembly](https://docs.aws.amazon.com/cdk/api/v2/docs/aws-cdk-lib.cloud_assembly_schema-readme.html),
  [AWS CDK execution](https://docs.aws.amazon.com/cdk/v2/guide/deploy.html).

## Rollout decision

Ship a format behind a flag until deterministic fixtures, security checks and
benchmark gates pass. The first live pilot must compare matched infrastructure
tasks with and without proactive context, measure total tokens and wall time,
and examine incorrect or missing relationships. Keep the feature disabled by
default if task quality or end-to-end efficiency regresses materially. Publish
results including cache state, tool/model versions, dataset and limitations.

## Implementation progress

Follow-up recommendations from real SRE/IaC sessions are planned in
[the SRE/IaC extension plan](2026-11-16_sre_iac_extensions.md).

An experimental opt-in adapter now indexes bounded, partial static source
facts for Terraform/OpenTofu, Kubernetes, Helm/Kustomize, CloudFormation, ARM
JSON and GitOps. Infrastructure locate, context, dependencies, impact and
status are exposed through adapter graph queries and MCP; matched facts share
one proactive-context character budget with code results. Context ranking uses
the current request as a stronger hint than bounded history, reports ambiguity
and a reason when it declines to emit, and distinguishes a characters/4
estimate from a real tokenizer count. The HCL extractor masks heredocs, resolves
statically named local module directories, links module-output references to
child outputs, records provider aliases, recognizes `moved`, `import`,
`removed`, `check` and `ephemeral` blocks and `count`/`for_each` instance modes,
and exposes Terraform address aliases (`var.*`, `module.*`, `local.*`,
`output.*`, `provider.*`). Interpolation expressions inside quoted strings and
heredocs become references without retaining literal template text, and
`var.*` references resolve to variable declarations. Terraform JSON extraction
reads explicit `${...}` expressions and the same resource/meta groups. Mixed
`.tf`/`.tofu` module scopes are reported rather than silently merged. External HCL grammars were
evaluated and not adopted because the candidate did not expose public source
spans (see [the evaluation](../research/hcl_parser_evaluation.md)). Kubernetes
extraction includes bounded RBAC role and ServiceAccount-subject links plus
custom-resource-to-CRD links when group, kind and scope agree; external
User/Group subjects, unknown namespaces and ambiguous CRDs remain unresolved.

The new-file refresh path, exact deletion/rename reingest and a synthetic
end-to-end retrieval evaluation at two budgets are tested. A bounded
process-local parse cache reuses unchanged per-file graphs on same-process
refresh (keyed by root, path, dialect and content digest; no source text is
stored), and a per-memory token index serves locate/dependency/context candidate
lookups without scanning every node. A bounded per-graph-version query cache
stores locate/context/traverse results by digest key and invalidates on graph
mutation;
`infra.status` and the benchmark report cache hits, misses and entries. A
deterministic synthetic cold/warm/incremental benchmark measures
enabled-versus-disabled build overhead, snapshot size, peak memory, query
percentiles and canary absence ([results](../results/iac_bench_2026-11-16.md)).
A CDK assembly reader is available as an explicit artifact API and as the
`infra_import_cdk` MCP tool; it never synthesizes or loads an app. See
[usage](../iac_context.md) and
[fixture results](../results/iac_fixture_2026-11-16.md).

The safety/evaluation and rollout gates remain open: the HCL parser is partial,
local module resolution links directory declarations rather than evaluating
module instances, the extractors do not cover every format or semantic
relationship, there is no approved live cloud integration or matched model
comparison, and production precision, large-repository overhead and total token
savings are not established. A task-level model comparison is not implemented.
The daemon now reports graph-lock wait mean/max/count alongside its request
timings. The synthetic benchmark
shows measurable adapter build overhead and refresh cost; do not present it as
an acceleration claim. The feature remains opt-in. Do not treat the phase
labels above as completed acceptance gates.
