# Infrastructure context (experimental)

The optional infrastructure adapter adds source-level Terraform/OpenTofu,
Kubernetes, Helm, Kustomize, CloudFormation, ARM JSON, Pulumi YAML and GitOps facts to the
graph and to proactive context.
It provides partial static analysis, not deployment state, validation, plan
execution, security scanning, or a complete dependency graph. HCL extraction
uses a limited conservative parser; it masks heredoc bodies, resolves
statically named local module directories, links parent module-output
references to child outputs, records provider aliases and Terraform address
aliases, and recognizes `moved`, `import`, `removed`, `check` and `ephemeral`
blocks plus `count`/`for_each` instance modes. Interpolation expressions inside
quoted strings or heredocs are retained as references while literal template
text is not. Terraform JSON extraction reads explicit `${...}` expressions and
supports the same resource/meta groups, but dynamic expressions, remote modules
and unusual syntax remain unresolved. Mixed `.tf` and `.tofu` files in one
module scope are both indexed and produce an explicit diagnostic because
documented OpenTofu dialect precedence is not evaluated.
Kubernetes selector
relationships are inferred only when both manifests specify the same namespace;
RBAC `roleRef` and ServiceAccount subject links resolve only across known
namespaces and roles, and User/Group subjects stay unresolved. Custom resources
link to a uniquely declared CRD only when group, kind and scope agree; unknown
or ambiguous CRD declarations stay unresolved. Bicep compilation
is not performed. A standalone CDK assembly reader accepts supplied artifacts;
it does not synthesize or automatically load an app. See the
[HCL parser evaluation](research/hcl_parser_evaluation.md) for why an external
grammar is not yet used.

Enable in the target project's `.ozagia.toml` before building the graph:

```toml
[adapters]
modules = ["ozagia.integrations.iac.adapter:register"]
```

YAML manifests require the optional `ozagia[iac-yaml]` extra. JSON and the
partial HCL extractor work without it. Rebuild an existing graph after enabling
the adapter. The `infra_context`, `infra_locate`, `infra_dependencies`, and
`infra_impact` MCP tools are then available; graph queries use corresponding
`infra.context`, `infra.locate`, `infra.dependencies`, and `infra.impact` names.
Use `infra_status` (or `infra.status`) to inspect extraction diagnostics,
resource counts by family, edge counts, extractor versions and query-cache
hits, misses and entries.
The `infra_import_sarif` tool accepts a supplied SARIF 2.1 report and returns
limited finding metadata without running a scanner or storing the report.
`infra_import_cdk` accepts a supplied CDK assembly manifest and optional
template texts, then reports bounded stack/resource metadata with import
provenance; it never synthesizes the app, runs CDK, or loads assets.
`infra_context` accepts optional `history` (at most four recent text turns),
`max_chars` and `limit`; history is a weaker hint than the current request.
Name and address matching uses identifier components and exact/qualified forms,
not arbitrary substrings, and a bare path coincidence is not enough to emit a
hint.
Context can also be included by the OpenCode proactive hook when its snapshot
is fresh and a query matches infrastructure identifiers. Code results and
infrastructure facts share one character budget, with infrastructure limited to
a reserve while code results remain; leftover reserve returns to code. Results
are bounded and include source paths and graph relationship provenance.
Missing, ambiguous, dynamic and external relationships must not be assumed
absent. Token counts are not measured by a model tokenizer; they are estimated
as characters / 4, and no tokenizer is run.

The adapter performs local source discovery only. It does not execute Terraform,
Helm, Kustomize, providers or project code; read a cloud account or Kubernetes
cluster; or ingest state and variable files. It stores selected structural
identifiers and paths rather than configuration values. Generic ozagia tools
can still read a file explicitly requested by a caller, so do not treat that as
a general-purpose secret redaction boundary. Project source may contain
sensitive values. No prediction of token or time savings is established until a
matched live evaluation is performed.

A synthetic cold/warm/incremental benchmark is available:

```sh
.venv/bin/python -m ozagia.eval.iac_bench --files 300 --query-count 20
```

It writes only to `/tmp/ozagia_iac_bench/` by default, measures enabled and
disabled builds, context budgets, refresh time and secret-canary absence, and
labels the report as synthetic. A bounded, process-local cache of parsed
per-file graphs (keyed by root, path, dialect and content digest) makes a
same-process refresh reuse unchanged files; it never stores source text. A new
process, or a changed file, parses again. Infrastructure queries use a token
index rebuilt only when the graph changes and a bounded per-graph-version query
cache. Cache keys are digests, so prompt/history text is not retained; the
adapter's freshness check still runs before a cached result is served. Context
scoring still grows with the number of matching declarations. Measured numbers
are in
[the 2026-11-16 benchmark](results/iac_bench_2026-11-16.md); they are not
production latency or model-token evidence.
