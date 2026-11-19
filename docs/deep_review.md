# Deep review

`ozagia deep-review --root REPO --out REPORT_DIR` composes a subject-based
Markdown and JSON review report. The default run is deterministic and offline:
it does not run tests, import the target, execute project code, or use the
network. Interpreter discovery and probes are disabled; dependency imports
are checked statically, and test-environment coverage is marked not checked.
`--index` may refresh the graph under the target's `.ozagia` state
directory; the report directory, including `evidence/`, is the only other
write.

## Command

```sh
ozagia deep-review --root REPO --out DIR \
  [--profile full|security|performance|failure-modes|data-structures|scalability|architecture|tests|dependencies|operations|docs|change|review-change] \
  [--range BASE..HEAD] [--index] [--baseline DIR] \
  [--security-tool NAME ...] [--security-network] [--local-advisories PATH] \
  [--fail-on blocking|major|never] [--budget-s S] [--max-findings N] \
  [--json] [--profile-report PATH ...] \
  [--run-bench JSON_ARGV ...] [--bench-timeout-s S] \
  [--model-pass --live --model M --base-url URL \
    [--max-tokens N] [--model-role ROLE ...] \
    [--model-budget-s S] [--model-max-requests N] \
    [--model-token-budget N] [--model-cost-budget USD] [--ocelot]]
```

For a reusable agent task definition, the built-in `review_my_change` task is
available through `ozagia tasks show review_my_change` (or `ozagia tasks list
--json`). It accepts a caller-supplied diff and declares read-only tools, zero
model calls, no network, and no mutation. Its task runner uses a caller-supplied
executor; it does not automatically invoke this CLI. For the deterministic
Markdown/JSON report, run `ozagia deep-review --range BASE..HEAD` when the
change is committed and the base/head revisions are available.

- `--profile` defaults to `full`; every other subject value selects one
  subject's collectors, not a different engine. `change` (alias
  `review-change`) is a CLI preset: it resolves to `full` plus a range review,
  defaults to `HEAD~1..HEAD` when `--range` is omitted, uses a compact
  `--max-findings 20` only when the caller did not supply a limit, keeps
  `--fail-on never` unless it is set explicitly, and leaves the model pass off
  unless requested. It refuses a non-Git root or a missing previous commit with
  exit 2. All subject files are still written, and unselected subjects report
  that no collector ran.
- `--out` defaults to `<root>/.ozagia/reviews/deep-<UTC stamp>/`. An output path
  inside the repository must be under `.ozagia/`; an explicit path outside the
  repository is allowed. Apart from the `--index` refresh, report writes stay
  in that directory, including `evidence/`.
- `--index` asks the review to refresh the graph before collection. Without a
  refresh, graph-dependent checks use the existing snapshot and report its
  freshness.
- `--range BASE..HEAD` adds deterministic change-set review when budget
  remains. It reads local Git history and any existing graph snapshot; it does
  not fetch, check out, refresh the graph, or run tests. A fresh graph supplies
  bounded changed-file import/caller/test blast-radius facts and security-impact
  candidates; a stale, missing or oversized graph records an explicit omission
  instead of inferring impact. Timeout or missing graph evidence is recorded as
  partial coverage rather than a clean result.
- `--baseline DIR` reads a previous report directory (or its `findings.json`,
  up to 32 MiB) and compares exact finding IDs. `identity_policy` is
  `canonical_subject_exact_finding_id_v1`; cross-subject matches with a stable
  rule identity produce one finding and appear in every matching subject report.
  A legacy baseline can retain the canonical subject ID; redundant IDs from
  secondary subject copies are reported as resolved. `move_policy` records that moved or renamed paths without
  a stable rule/symbol identity appear as both resolved and new, and
  `move_mapping` is `unknown`. No move matching is inferred. The comparison
  includes `new`, `resolved`, `unchanged`, `totals`, and per-subject and
  per-severity deltas. It does not establish that a finding was true or fixed.
- `--fail-on` defaults to `never`. `blocking` fails on blocking findings;
  `major` fails on major or blocking findings. `--max-findings N` is a global
  top-N view after severity, priority and blast-radius ranking, not a per-subject
  truncation. It limits the findings returned by the library result and the MCP
  summary; `findings.json`, subject reports and the `--fail-on` gate still use
  all findings. The manifest records the limit, total and returned count.
- `--security-tool NAME` is repeatable and runs only a named allowlisted
  scanner. The default is no external tools. `--security-network` is a separate
  explicit opt-in required for a scanner that declares network access; without
  it that tool is recorded as `refused` and is not started. Unknown,
  unavailable, refused and successful tools are recorded in security coverage
  and the manifest with name, kind, status, version, error, finding count and a
  report hash when one exists. Provenance also records the requested tool names,
  `network_allowed`, and per-status counts. Scanner failure becomes coverage,
  not a command crash, and scanner findings remain candidates, not proof of
  exploitability.
- `--budget-s S` defaults to 60 seconds and is a cooperative end-to-end budget,
  not a hard wall-clock guarantee (see below).
- `--json` prints the manifest to standard output; the report files are still
  written.
- `--profile-report PATH` is repeatable and adds read-only measured profile
  evidence. It never starts a profiler, imports the target, or calls a model
  (see below).
- `--run-bench '["python", "-m", "pytest", "-q"]'` explicitly runs a
  caller-supplied argument vector; repeat the option for additional commands.
  The value must be a JSON array of strings. Commands are started with
  `shell=False`, in the reviewed root, with stdin closed. `--bench-timeout-s`
  defaults to 30 seconds per command (maximum 300); the review's remaining
  `--budget-s` also limits execution. This is an explicit arbitrary-command
  execution boundary: commands can write files or access the network, so only
  supply commands you intend to run. No benchmark is run unless this option is
  present.
- Benchmark measurements are written separately to `bench-results.json`.
  The artifact contains a SHA-256 digest of each argv, a generated label,
  status, return code, duration and truncation flags. It does not store argv,
  stdout or stderr. Output is drained with a per-stream cap and redacted before
  it is discarded; command output does not enter findings or `--fail-on`.
  Measurements are single-run observations and do not affect finding IDs or
  the deterministic findings report.

Exit codes: `0` when a report was produced, `1` when `--fail-on` matched, and
`2` for invalid or refused input (unsupported profile, unreadable root,
malformed baseline, refused repository preflight, and similar errors).

The MCP tool `dev_deep_review` exposes the same offline report path with bounded
inputs. It writes reports only under `<root>/.ozagia/reviews/`; profile artifact
ingestion may also build a missing tag cache under `.ozagia`. It defaults to a
30-second collection budget (maximum 300), returns at most 50 findings (default
20), and does not run external security tools, the model pass or tests. Profile
report paths passed to it must stay inside the workspace.

## Output

Every run writes:

```text
README.md
00-method-and-scope.md
01-security.md
02-performance.md
03-failure-modes.md
04-data-structures.md
05-scalability.md
06-architecture.md
07-tests-and-coverage.md
08-dependencies.md
09-operations.md
10-documents-and-api.md
11-correctness.md
findings.json
findings-index.md
manifest.json
```

`sbom.json` is added when dependency inventory produced SBOM data. `evidence/`
is created when findings with a source-line location have a readable excerpt.
`model-review.json` is added when `--model-pass` is requested, including when
the pass is unavailable. `bench-results.json` is added when `--run-bench` is
used.

The subject files contain scope, status, findings, a metrics table, and coverage
(checks, skipped work, unknowns and omissions). Metrics include derived finding
and coverage counts plus allowlisted source-section counts and fixed status
values. Numeric values are bounded; no raw paths, logs, names, or arbitrary
section fields appear in the metrics table. `README.md` adds a scorecard, finding counts,
the top ten risks in rank order and a link to the full findings index.
`findings.json` is the canonical finding list; `findings-index.md` orders every
finding by priority.

Each finding carries a stable ID derived from subject, rule and location keys
rather than line numbers, plus severity, confidence, evidence kind, locations,
evidence and a priority block. Evidence file hashes are bounded to 1 MiB per
file; oversized files record `hash_omitted`. Symlinked or unreadable files are
not hashed. Caller reachability affects priority only when the graph is fresh
and a finding's location matches an exact symbol; otherwise the prioritization
block records why graph evidence was omitted. Priority also considers confidence,
bounded symbol degree in a fresh graph, and recorded per-file churn hotspots.
A file absent from the bounded hotspot list has unknown churn, not zero churn.
Use repeatable `--focus SUBJECT` to prioritize specific subjects within the same
severity. These signals affect ordering only, never severity or truth assessment;
missing graph, churn, or focus signals are recorded as unknown in the finding's
priority block.

Source excerpts are bounded: up to five lines of context on each side of the
reported line, a 40-line and 8 KiB cap, a 250-file cap per report and a 1 MiB
source-file cap. Each excerpt is written to `evidence/<finding-id>-L<line>.txt`
and linked through `excerpt_ref` and `excerpt_hash`. Known secret patterns are
redacted from excerpts; if secret scanning flags any line in the excerpt
context, the entire excerpt is omitted. Missing files, symlinks, oversized
sources and exhausted budgets also record `excerpt_omitted`. Redaction and
secret-pattern checks reduce exposure but cannot guarantee that arbitrary
source text contains no sensitive material.
Excerpt files are included in `output_hashes`.

`sbom.json` is a deterministic, CycloneDX-shaped inventory built from the
manifests and lockfiles that were read. Components carry an
`ozagia:cve_state=unknown` property and the document says licence assertions
were not assessed. It is inventory data, not a validated compliance SBOM or a
vulnerability result.

npm lockfile v1 inventory traverses nested installed dependencies as well as
top-level entries. Inventory limits still apply, and an oversized package list
is explicitly marked as truncated.

An explicit local advisory file can be supplied with
`--local-advisories PATH` for the `full` or `dependencies` profile. The bounded
JSON format is `ozagia.local_advisories.v1`, with an `advisories` list whose
rows contain `id`, `ecosystem`, `package`, and either
`affected_versions` as exact version strings or `affected_ranges` as OSV-style
`{"events":[{"introduced":"1.2.0"},{"fixed":"1.4.0"}]}` sequences
(optional `severity` and `summary`). It is never fetched or refreshed.
Matching uses parsed lockfile pins only; manifest ranges are never evaluated.
Range events are supported only for Node and Rust strict SemVer, and Go
`v`-prefixed strict SemVer (including pseudo-versions). Introduced is inclusive,
fixed is exclusive, `last_affected` is inclusive, and an open introduced event
has no upper bound. A `last_affected` ceiling is a last known affected version,
not evidence that later versions are fixed. A single event sequence cannot mix
`fixed` and `last_affected`; finite `limit` events impose an exclusive upper
bound, while `*` leaves that range unbounded. Malformed sequences are rejected. Exact Python versions remain validated as PEP 440 and
compared as written, with the Go `v` prefix normalized. Ruby Bundler
`Gemfile.lock` pins support exact-version matching only, using conservative
numeric versions with an optional prerelease suffix. Only specs under a `GEM`
source block are eligible for Ruby advisory matches; `GIT` and `PATH` specs are
inventoried with their source recorded but never matched, since their versions
may not identify a published registry release. Findings carry the local
dataset hash and remain `truth_assessed: false`. Composer `composer.lock`
packages are inventoried with normalized `vendor/package` identities;
advisories support conservative numeric versions and common release suffixes
exactly, but not branch/dev aliases or ranges. Unsupported package rows are
reported as omissions rather than silently dropped.
Gradle `gradle.lockfile` rows are inventoried as exact Maven `group:artifact`
coordinates and conservative static versions. Matching accepts exact versions
only; Maven ranges are unsupported. The lockfile does not record repository
URLs, so repository provenance remains unknown and reports state that an exact
coordinate match cannot establish package origin. Dynamic and malformed rows
remain explicit omissions. Maven versions use a conservative ASCII token
grammar, not Maven ordering semantics.
The report's overall CVE state stays
`unknown` even when exact-version or supported range matches are found, and a no-match result explicitly
does not mean the dependencies are clean. The same source is available to
`build_deep_review(..., local_advisories=PATH_OR_OBJECT)` for programmatic use.

An explicit local license policy can be supplied with `--license-policy PATH`
for the `full` or `dependencies` profile. The bounded
`ozagia.license_policy.v1` JSON object has non-empty `allow` and/or `deny`
arrays of supported SPDX identifiers. Evaluation checks lockfile license
declarations using a bounded SPDX expression subset: identifiers, parentheses,
`AND`, and `OR`. `AND` is denied if any component is denied and allowed only if
all components are allowed. `OR` is allowed only if all alternatives are
allowed and denied only if all alternatives are denied. Mixed or uncovered
alternatives remain `unknown`. `WITH`, unsupported identifiers, malformed or
over-bound expressions, and missing metadata remain `unknown`. Deny results
produce review findings, while allow results are only policy matches. This
conservative evaluator reports declared metadata against a local policy; it
does not assess legal meaning or establish compliance. The report keeps
`truth_assessed: false`. Policy records include a source hash and are never
fetched.

```json
{
  "schema": "ozagia.license_policy.v1",
  "allow": ["MIT", "Apache-2.0"],
  "deny": ["GPL-3.0-only"]
}
```

```json
{
  "schema": "ozagia.local_advisories.v1",
  "advisories": [{
    "id": "CVE-EXAMPLE-1",
    "ecosystem": "python",
    "package": "example-package",
    "affected_versions": ["1.2.3"],
    "severity": "major",
    "summary": "Example local record"
  }]
}
```

`manifest.json` records the schema, root, profile, revision, tool versions,
graph freshness, graph content hash and reason when unknown, per-subject
status/checks/skipped/omitted/unknown/findings, per-subject report hashes,
`status`, `complete`, `exhaustive: false`, `truth_assessed: false`, security
tool provenance, evidence summary, SBOM summary when present, budgets and
timings, truncation (including the global `--max-findings` view), output hashes,
baseline policy and comparison, and the range/profile/benchmark inputs used.
`plan_hash` is a SHA-256 digest of the canonical `plan` object, which records
resolved options and input digests without raw file contents or the report
output path. It includes the revision and verified graph digest, supplied
advisory/license/baseline/profile-report digests, project config digest, and a
bounded workspace snapshot digest. The snapshot is capped at 5,000 files,
20 MiB total and 1 MiB per file; `plan.inputs.workspace.state` explicitly
records complete, truncated, skipped or partial coverage, so a partial digest
does not imply that omitted inputs were equal. The plan excludes timestamps,
durations and output location. A
subject is `complete` only when its collector ran and recorded no omissions,
skips or unknowns and the graph is fresh; otherwise it is `partial`, or
`unknown` when no collector ran. A run is `complete` only when every selected
subject is complete. Completeness means coverage accounting finished, not that
the findings are true.

`00-method-and-scope.md` records tool versions, configured budgets, graph
freshness/hash, profile inputs and bounded per-subject omissions. Elapsed
timings stay in `manifest.json` so repeated runs remain comparable.

## Status

`ozagia-status` includes a read-only `reviews` section. It reads the newest
`deep-*` manifest under `<root>/.ozagia/reviews/` and reports the latest
directory, age, run status, `complete`/`exhaustive`/`truth_assessed` flags,
finding and subject counts, and a bounded subject-status map. No review
directory or no `deep-*` directory is `absent`; a missing, unreadable, malformed,
ambiguous or escaping manifest is `unknown`; a valid manifest is `ok`. It does
not create, refresh or rewrite a review, and it does not copy finding IDs,
evidence or subject contents. See [status](status.md).

## What is checked

- **Security:** repository security hygiene from `repo_review`, an offline
  scanner, secret-pattern checks, dependency-manifest inventory, and
  conservative same-function source-to-sink candidates in Python. External
  scanners run only when named with `--security-tool`; a scanner that requires
  network access is refused unless `--security-network` is also set. Tool
  provenance is recorded. Scanner findings are candidates, not proof of
  exploitability.
- **Performance, failure modes, data structures and scalability:** selected
  Python AST patterns (for example repeated nested iteration, silently
  suppressed exceptions, unconditional retry loops, mutable default arguments,
  materialized ranges and eager sequence conversion), plus a bounded inventory
  of Python data models and JSON/TOML keys for the data subject. Other
  languages and patterns remain unassessed.
  The failure-modes report includes a derived FMEA table for supported static
  candidates. It records the matched symptom/rule and mitigation, links to the
  finding evidence and any explicitly related test/record finding, and labels
  runtime trigger and critical-path status unknown. It does not assign
  likelihood or criticality. Tests are never associated by filename alone.
- **Architecture:** the existing repository-review architecture checks
  (hubs, cycles, coupling and related graph signals). Results depend on graph
  coverage and freshness.
- **Tests and coverage:** repository-review test-plan and test-record checks.
  Tests are never executed and line coverage is not measured.
- **Dependencies:** offline inventory of supported Python, Node, Rust, Go,
  Ruby Bundler and Composer lockfiles, with missing-lockfile, duplicate-declaration and
  exact version-conflict candidates. Declared licences and a deterministic
  CycloneDX-shaped SBOM are recorded, but CVE state remains `unknown`. An
  explicit `--local-advisories` file can match exact locked versions or
  supported OSV-style introduced/fixed/last_affected SemVer ranges against local advisory
  records; range matching supports Node/Rust and `v`-prefixed Go, while Python
  and Ruby are exact-version only. Composer `composer.lock` matches exact
  supported versions only; branch aliases and ranges are skipped. Ruby
  advisory matching is limited to Bundler `GEM` source pins; `GIT` and `PATH`
  pins are recorded but skipped. There is no fetch or registry
  lookup, advisory database, licence-compliance decision or validated compliance
  SBOM.
- **Operations:** inventory of common deployment configuration and runbook
  files, local runbook links and recognized operational topics, declared
  logging/metrics/tracing/observability signals, and a
  Dockerfile healthcheck candidates for a missing final-stage declaration or an
  explicit `HEALTHCHECK NONE`, accounting for literal named-stage inheritance.
  External base-image inheritance, alternate build targets,
  and external health checks remain unknown. Bounded lexical IaC/config rules
  flag Terraform public ingress, container privileged mode, Kubernetes
  `hostNetwork`/`hostPath`, and Compose all-interface port binds. Explicit
  telemetry disable settings are reported with rule IDs. Selected plain JSON
  Kubernetes manifests and selected Kubernetes/Compose YAML receive bounded
  structural checks for literal security settings, including host ports, host PID/IPC
  namespaces and explicitly configured root UIDs. Pod defaults can be
  overridden by individual containers. YAML checks use PyYAML from
  the optional `iac-yaml` extra. Terraform ingress checks use `python-hcl2`
  from the optional `iac-hcl` extra in a time- and memory-bounded subprocess.
  If a parser is absent or parsing is ambiguous, the report marks structural
  coverage unknown and may retain lexical candidates.
  Duplicate keys and YAML aliases are not structurally interpreted.
  Dynamic JSON remains unparsed; runtime monitoring remains `unknown`, and these
  candidates do not validate schemas or effective deployed behavior. Dynamic
  templates, Terraform expressions/modules/provider semantics and Compose
  `extends` references remain unknown and are not resolved.
- **Documents and API:** literal `from local.module import name` and single
  `import local.module` references in Markdown are checked against local Python
  modules/definitions, with coverage facts,
  stale local imports and bounded rename/removal candidates. External links,
  comma-separated imports, runtime re-exports, prose API mentions and
  versioned/removed API history are not assessed.
- **Correctness:** added only by `--range`; without it the subject reports no
  collector and remains unknown. A range review adds a test-coverage plan,
  redacted security-impact candidates and changed-file import/caller/test
  blast-radius facts from a fresh graph. Missing or stale graph evidence is an
  explicit omission, not inferred impact. Findings are change-set candidates,
  not verified behavioral defects.

## Checks and limits

The implemented subjects are static or inventory collectors. They do not import
the target, run tests, execute project code or access the network by default.
Optional external scanners require explicit selection, and network-requiring
scanners require a separate explicit opt-in; unknown, unavailable and refused
tools are recorded as coverage, not crashes. Dependency licence, version and
SBOM facts are local inventory: CVE state stays unknown, no advisory database is
queried, and no licence-compliance decision is made. Optional
`--local-advisories` matching uses exact locked versions and supported
introduced/fixed/last_affected SemVer ranges from a local file; it does not fetch data or
prove a dependency clean. Range matching is limited to Node/Rust and
`v`-prefixed Go strict SemVer pins; Python, Ruby and Composer are exact-version only.
Ruby exact matches include only Bundler `GEM` source pins; Git/path pins are
inventory-only. Composer branch/dev versions are not matchable. Runbook and
observability checks read repository declarations; runtime
telemetry is unknown. IaC/config checks cover selected JSON Kubernetes
structure, selected bounded Kubernetes/Compose YAML structure with the optional
`iac-yaml` extra, selected literal Terraform ingress structure with the
`iac-hcl` extra, and lexical patterns. No Kubernetes schema/API validation,
Terraform module/provider evaluation, interpolation resolution, or deployed-
state check is performed. Dynamic Kubernetes/Compose YAML, Terraform
expressions/modules, Compose `extends`, and templated/dynamic JSON are reported
as unknown. Documentation/API compatibility is limited
to single-line literal local imports and static public names; runtime re-exports and API
version history are not assessed. Range blast radius is reported only from a
fresh graph and is otherwise omitted. Trust-boundary analysis is conservative:
only a narrow, same-function source-to-sink shape is reported. Optional Ocelot
clustering applies to model candidates only and supplies candidate signals, not
truth. Measured profile reports and benchmark results are evidence about
observed cost or telemetry, not verdicts about defects. Absence of a finding is
not proof of safety, correctness or scalability.

The static candidate rules have an offline regression evaluator:
`.venv/bin/python -m ozagia.eval.deep_review_static`. It scores a small
reviewed corpus of positive examples and near misses and emits precision,
recall, finding noise, negative-case false alarm rate, and a corpus digest.
The recorded run is in
[`docs/results/deep_review_static_eval_2026-11-18.md`](results/deep_review_static_eval_2026-11-18.md).
These synthetic results are regression evidence only, not a production
accuracy estimate.

## Optional model pass

An optional pass runs independent model reviewers over a compact packet of at
most 60 finding labels and locations. `--model-pass` writes advisory candidates
to `model-review.json`, but the deterministic report remains authoritative:
model candidates are not added to `findings.json`, subject reports or the
`--fail-on` gate, and they never change the exit status. Model output is
untrusted: responses are strictly validated, bounded and redacted, and always
carry `truth_assessed: false`. A model candidate is stored with severity
`note`; its requested severity is retained separately as `candidate_severity`.
Candidates are role-mapped, deduplicated and may be clustered for review, but
they remain unverified until checked deterministically or by a person.

The pass requires both the `--live` flag and `OZAGIA_EVAL_LIVE=1`, plus `--model`
and `--base-url`. If either live gate or the provider configuration is missing,
the deterministic report is still written and `model-review.json` records that
the pass was unavailable. Provider or harness failures are reduced to an error
type without endpoint details, response excerpts or credentials. API
credentials use the `OPENAI_API_KEY` environment variable and are not written to
the report.

`--model-role` is repeatable and selects reviewer roles: `security`,
`performance`, `failures`, `data`, `scalability`, `architecture` and `tests`.
Candidates are mapped to the report subjects `security`, `performance`,
`failure-modes`, `data-structures`, `scalability`, `architecture` and
`tests-and-coverage` respectively. With no role option, all seven default roles
run.

Model budgets are separate from the review budget:

- `--model-budget-s S` bounds the pass wall-clock time; the default is 120
  seconds. The deadline is checked between requests and cannot preempt an
  in-flight provider call.
- `--model-max-requests N` caps reviewer requests; the default is 32.
- `--max-tokens N` caps completion tokens for each model response; when
  omitted, the provider's default applies.
- `--model-token-budget N` and `--model-cost-budget USD` cap total usage and
  cost when the provider reports them. They are checked between requests and
  after responses, so an in-flight/final response can take usage over the cap;
  the manifest records the requested and observed budgets, usage and reasons.
  Without provider usage, token/cost caps cannot be enforced.

`--ocelot` is opt-in and off by default. It clusters model candidates only,
using `cluster_section`; it never installs or downloads Ocelot, accesses the
network, calls a model or executes the target. If Ocelot is absent or fails,
the candidates are unchanged and exact-key fallback clusters plus the reason are
recorded. Clustering does not verify candidate truth.

The CLI also records narrow deterministic corroboration in `model-review.json`:
a candidate is `corroborated` only when it cites one unique deterministic
finding with the same exact subject and rule/type. That confirms rule identity
only; it does not verify wording, impact, severity or truth, and it never
changes deterministic findings or blocking status. The manifest gains a
`model_review` summary with artifact status and hash, candidate count,
validation, budgets, Ocelot state and corroboration counts; the artifact hash is
separate from the pre-model `output_hashes`.

The optional model output can also carry a bounded `python.mutable_default`
assertion with an exact path, symbol and source SHA-256. A deterministic
checker re-reads and parses the pinned source without importing it and records
whether that narrow assertion holds. It does not verify the candidate's prose,
impact or severity, and does not promote a finding to blocking.

## Budget behavior

`--budget-s` is a cooperative end-to-end time budget. Collection receives the
remaining time; normalization and evidence hashing check the deadline between
findings, and graph loading has a subprocess timeout. Independent static,
dependency, operations, docs, data and supplied-profile analyzers run with a
maximum of six concurrent workers. Each worker uses a fresh isolated Python
subprocess with `-I -B`, bounded JSON request/result frames and the shared review
deadline; results are merged in a fixed analyzer order. A single analyzer uses
the same isolated process wrapper. No collector process inherits project
`sys.path` entries or executes project code.
Repository review and the security scanner retain their existing collection
path. A failed or timed-out analyzer is recorded as unavailable without
discarding successful lanes. Hashing reads at most 1 MiB
per distinct evidence file. Python cannot preempt a filesystem operation
already in progress, nor report composition and the atomic write for one
artifact. The report is therefore finished after the deadline when needed to
leave a readable artifact set; the manifest records elapsed time, overrun,
partial status, and any normalization findings omitted. This is a best-effort
runtime limit, not a hard wall-clock guarantee. Evidence excerpts check the same
deadline and record omissions when it is exhausted. The optional model pass runs
after the deterministic report with its own budgets; it does not extend or
change the review budget.

When `--run-bench` is present, each command's timeout is also capped by the
remaining review budget. If no budget remains, remaining commands are recorded
as `budget_exhausted` without being started. A timed-out command is terminated;
on POSIX its process group is terminated as well. Process startup, termination
grace periods and filesystem writes add small overhead beyond the nominal
budget.

## Measured profile evidence

`--profile-report PATH` (repeatable) adds measured evidence from
`ozagia profile` reports or artifacts. Hotspots and baseline regressions become
performance findings with `evidence_kind: observed`; telemetry error spans
become failure-mode findings; metric inventory is attached to the
performance/scalability manifest sections. Ingestion is read-only over the
target: it does not start a profiler, import or execute the target, call a model
or use the network. A missing tag cache may be built under the state directory
for the graph join. The report says these are measurements, not verified
defects. See [profiling](profiling.md).

## Evaluation fixtures

The proposed, not-yet-validated realistic-corpus protocol, human review sample,
and go/no-go thresholds are in
[`results/deep_review_go_no_go_proposal_2026-11-18.md`](results/deep_review_go_no_go_proposal_2026-11-18.md).
The frozen-HEAD smoke and its limits are recorded in
[`results/deep_review_frozen_head_smoke_2026-11-18.md`](results/deep_review_frozen_head_smoke_2026-11-18.md).
Its blank top-ten human annotation sheet is
[`results/deep_review_frozen_head_annotation_sheet_2026-11-18.md`](results/deep_review_frozen_head_annotation_sheet_2026-11-18.md).
The proposal keeps `--fail-on` caller-selected and the default non-blocking;
existing synthetic measurements below are not production-quality evidence.

## Static-check fixture evaluation

Run `python -m ozagia.eval.deep_review_static` to score the current static
candidate rules against a frozen, synthetic Python corpus. It prints
deterministic JSON; `--out PATH` writes that JSON to a file. The corpus and
expected labels are in `tests/fixtures/deep_review_static/`. Metrics use
`(case, finding type)` pairs, so repeated instances of one rule within a single
snippet count as one detection. Empty predictions or labels are treated as
perfect for that denominator (the corpus includes positive examples for every
reported rule).

This is a regression fixture, not an estimate of real repository performance:
it has eight hand-written snippets, one positive and one near-miss negative per
rule. A perfect score only shows that the current patterns match these labels;
it does not establish broad precision or recall. Expand the corpus with
reviewed examples before using these numbers to compare analyzer quality.

For a slightly more realistic, multi-module seeded checkout, run
`python -m ozagia.eval.deep_review_fixture`. The fixture under
`tests/fixtures/deep_review_service/` includes positive and near-miss examples
across catalog, checkout, and batch-processing modules. The evaluator reports
file/rule precision and recall, scanned source bytes, candidate artifact size,
and one local static-scan duration. The captured run is
`docs/results/deep_review_service_fixture_2026-11-18.json`: 4/4 labeled
candidate pairs, 1.0 precision and recall overall and per rule, 995 source
bytes, and 2,113 candidate artifact bytes. The single-run timing is recorded in
the JSON and varies by host. These synthetic fixture measurements are not
production estimates; labels are hand-seeded. This static-fixture evaluator
does not time full report orchestration because its repository preflight
includes host resource admission; the end-to-end report evaluator below does.

## End-to-end report and label evaluation

For an end-to-end report and baseline comparison on the seeded checkout, run
`python -m ozagia.eval.deep_review_report`. The evaluator copies the fixture to
a temporary Git checkout, writes two reports there, scores both against
`labels.json` with `ozagia.eval.deep_review_labels`, and compares stable finding
IDs in the second report with the first. `--fixture`, `--labels`, `--budget` and
`--out` are available; JSON goes to stdout by default. All generated files stay
in the temporary directory except an explicit `--out` result path.

Labels are reviewed `(path, type[, subject])` expectations under `cases`,
`report_findings` and optional `range.expected`. `complete_subjects` names the
subjects whose labels are exhaustive for the fixture. Predictions are unique
`(path, type, subject)` pairs. An unmatched prediction in a completely labelled
subject is a false positive; elsewhere it is `unlabeled` noise. Findings with
no location cannot match and are counted separately. Metrics are `None` when
their denominator is absent.

Labels can specify `required_checks` and `requires_graph`; range labels can
also specify `baseline_required_checks` and `baseline_requires_graph`. A run
excludes a label when its required check did not run, was omitted, or needs a
fresh graph that was unavailable. Without coverage metadata, ambiguous
omissions fail closed. Results report excluded labels and unavailable subjects
instead of counting missing analysis as a false negative.

The evaluator does not enable the model pass, refresh an index, run tests,
import the fixture, or call the network. Host resource admission is part of
full report generation; if it refuses, the JSON records `preflight_refused` and
the failed checks instead of presenting an empty report as a successful run.
Repeatability and fixture-local label scores do not establish production truth.
The 2026-11-18 synthetic run is recorded in
[results](results/deep_review_phase4_2026-11-18.md).

## Comparison harness

Run the offline comparison harness:

```sh
python -m ozagia.eval.deep_review_comparison \
  --comparison range|model-pass|graph|all \
  [--fixture PATH] [--labels PATH] [--range-labels PATH] \
  [--budget S] [--out PATH]
```

`--comparison` defaults to `all`; JSON goes to stdout unless `--out` is given.
The whole-repo labels default to `<fixture>/labels.json`; range labels default
to `tests/fixtures/deep_review_range/labels.json`. The range label file is kept
outside the scanned checkout so its scripted change text is not reviewed as
part of the fixture.

- **`range`**: compares a whole-repo report with the same report plus
  `--range` on two deterministic commits. It reports changed files,
  new/resolved/unchanged stable IDs, change-set precision and recall, range
  graph freshness, and finding-ID deltas. The whole-repo report cannot emit
  range-only rule identities, so its change-set recall on range-only labels is
  expected to be low.
- **`model-pass`**: compares the deterministic report with an injected,
  scripted fake runner. No provider, live model or network is involved. It
  scores accepted, unique and supported candidates through accepted citations
  to deterministic findings that match reviewed labels, and reports candidate
  precision, candidate recall, added-noise rate, duplicate rate and token
  usage. It checks that deterministic findings are unchanged and reports
  `truth_assessed: false` and `live_provider_calls: 0`.
- **`graph`**: compares absent-graph and `index=True` runs on identical
  copies of the fixture. It reports label scores, precision over all
  predictions, report noise, priority evidence (freshness, nodes, edges,
  direct symbol evidence and reachable callers) and added/removed/shared
  finding IDs.
- **`all`**: runs all three and returns one JSON object.

## Metric definitions

- **Precision**: true positives divided by predictions whose subject has
  complete reviewed labels. Neither matched nor unmatched predictions outside
  a completely labelled subject are counted in this metric. If a subjectless
  label can match only some predictions across complete and incomplete subjects,
  ambiguous subjects are excluded from precision and listed in
  `ambiguous_precision_subjects`. Where subjectless labels leave attribution
  ambiguous between fully labeled subjects, aggregate precision remains
  measurable but affected per-subject prediction metrics are unknown and
  listed in `ambiguous_subject_attribution`.
- **Recall**: matched reviewed labels divided by reviewed labels.
- **False-positive rate**: false positives divided by all actual negatives.
  This remains unknown for report-label comparisons because the labels do not
  enumerate a negative universe. The `false_positive_rate` field is retained
  but now returns null rather than the formerly mislabeled discovery rate.
- **False-discovery rate**: false positives divided by predictions in
  completely labelled subjects; this corpus-local measure is available as
  `false_discovery_rate` when its denominator is nonzero.
- **Report-noise ratio**: unmatched reported `(path, type, subject)` pairs
  divided by all reported pairs. Unlabeled subjects contribute to noise but not
  to false positives.
- **Precision over all predictions** (graph comparison): true positives
  divided by every reported pair; unlabeled graph-added pairs count as misses.
- **Candidate precision / recall, added-noise rate and duplicate rate**
  (model-pass comparison): supported unique candidates divided by unique
  candidates; reviewed labels covered by accepted citations divided by reviewed
  labels; unsupported unique candidates divided by unique candidates; duplicate
  accepted candidates divided by accepted candidates.

## Offline evaluation boundary

`ozagia.eval.deep_review_report` and `ozagia.eval.deep_review_comparison` are
offline evaluation tools, not production review calls. They run on synthetic
fixtures and hand-reviewed labels. They do not call a live model or provider,
use the network, import the target, execute tests, or refresh an index outside
the temporary checkout. Timings and memory are single-host samples; tracemalloc
excludes native allocations and process RSS is not isolated to one report.
Synthetic scores are regression evidence, not production accuracy, and added
graph findings are measured candidates/noise, not proven quality improvements.
