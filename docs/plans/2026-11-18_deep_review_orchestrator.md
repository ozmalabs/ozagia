# Deep review orchestrator — plan (2026-11-18)

Status: implemented in part, 2026-11-18. This document turned Ozagia's existing
review, security, architecture, test, evidence and model-review building blocks
into one `ozagia deep-review` run that produces a multi-file, subject-based
report. Phase 0 and Phase 1 are implemented. Phase 2 is largely implemented:
optional allowlisted scanners with an explicit network gate and per-tool
provenance, conservative trust-boundary candidates, offline dependency, licence
and version inventory with deterministic SBOM and CVE state unknown, runbook and
observability declarations, bounded lexical IaC/config candidates,
documentation/API coverage (including literal local module imports), and
fresh-graph range blast radius. Local OSV-style introduced/fixed/last_affected range matching
is implemented for Node and Rust strict SemVer pins and Go `v`-prefixed strict
SemVer pins; range events support introduced/fixed/last_affected and strict SemVer limits. Python, Ruby,
Composer and Maven remain exact-version only; Gradle `gradle.lockfile` inventory
and conservative Maven coordinate matching are supported, with repository
origin unknown. Ruby Bundler `Gemfile.lock` inventory and
exact local-advisory matching for registry (`GEM`) pins are supported; Git and
path source pins are inventory-only. Composer `composer.lock` inventory and
conservative exact local-advisory matching are supported. Broader SCA coverage
and advisory-dataset completeness, licence-expression/compliance assessment
and semantic IaC validation remain. Phase 3 is partial: the optional gated `--model-pass` has
strict validation, role-to-subject mapping, time/request/token/cost budgets,
redaction, dedupe, narrow deterministic corroboration and optional Ocelot
candidate clustering with exact fallback; truth/severity promotion and live
quality evaluation remain. Phase 4 is partial: synthetic comparison harnesses,
baseline and status integration, a `review_my_change` task, a bounded
working-checkout smoke and an unfilled human-review annotation sheet exist; a
clean realistic-corpus comparison with completed human labels and published
go/no-go thresholds remains. The design below is unchanged; each phase section
marks what is implemented and what remains.

The goal is not another linter. It is an **evidence-first review atlas**: every
subject gets its own Markdown file, every finding carries locations, evidence,
confidence and coverage, and the run says explicitly what was checked, what
was omitted and what remains unknown.

## Objective

One read-only command reviews a checkout (or a commit range) across subjects:

1. security and trust boundaries;
2. performance and hot paths;
3. failure modes and resilience;
4. data structures and invariants;
5. scalability and resource bounds;
6. architecture and coupling;
7. tests and coverage;
8. dependencies and supply chain;
9. operations, configuration and observability;
10. documentation and public API;
11. correctness and change semantics;
12. a cross-subject summary, findings index and machine-readable manifest.

Output is a directory of Markdown files plus canonical JSON. The default run is
deterministic and offline. Optional model reviewers and optional external
tools are labelled as such and never become the only basis for a blocking
finding.

## What Ozagia already has

| Capability | Existing surface | Reuse in deep review |
| --- | --- | --- |
| Whole-repo review | `ozagia.core.repo_review`, `ozagia review-repo` (`architecture`, `tests`, `health`, `churn`, `docs`, `security`, `ops`, `dependencies`) | Subject collectors, graph-aware checks, packet/compare/`--fail-on` semantics |
| Diff review | `ozagia.core.code_review`, `code_review_cli` | Change-set correctness, security section, test-runner seam, Ocelot seams |
| Security | `ozagia.core.security`, `security_cli` | Secrets, dependency manifests, source patterns, optional external tools, `truth_assessed=false` |
| Architecture | `ozagia.core.architecture` | Hubs, cycles, coupling, dead-code candidates, test-to-source ratios |
| Repo scan | `ozagia.core.repo_scan` | Ranked opportunities for missing tests/docs/modules/hotspots |
| Tests | `ozagia.core.testing`, `ozagia.core.failures`, test records | Affected-test plans, failing/flaky records, failure classification |
| Review packet + model pass | `ozagia.harness.review`, `review-repo --model-pass` | Optional multi-role model review over a compact packet; validation/recording |
| Evidence and preflight | `core.evidence`, `core.task_preflight`, `core.services` | Facts, freshness, evidence kind, readiness and deferred-work vocabulary |
| Self-graph / capabilities | `core.capabilities`, `core.self_graph`, `dev_capabilities` | Declare `cap:review.deep`; report subjects, checks, tests/docs and coverage gaps |
| Maps / model API | `model_api` (`graph`, `outline`, `capabilities`, `work`) | Bounded, versioned inputs for optional model reviewers and summaries |
| Ocelot (optional) | `core.ocelot_code`, `research/ocelot_projection` | Finding clusters, change-shape analogies and candidate test impact |
| Performance tooling | `eval.bench`, `eval.timing`, `core.resources`, `core.cgroup` | Measurement plumbing, admission facts and report timings; no application profiling yet |
| Session/context | `ozagia agent`, plugin, repo-map delivery | Optional model reviewers can receive a ranked map; the review itself stays model-free by default |

The missing piece is the **orchestrator and report contract**, plus dedicated
analyzers for performance, failure modes, data structures and scalability.
Everything else already exists and should be composed, not reimplemented.

## What other products do, and what to borrow

| Product | Useful design | Borrow | Avoid |
| --- | --- | --- | --- |
| CodeRabbit ([architecture](https://docs.coderabbit.ai/overview/architecture), [review](https://www.coderabbit.ai/review), [security](https://docs.coderabbit.ai/security)) | Multiple specialized/verification agents, 50+ deterministic analyzers, code graph and blast radius, sandbox reproduction, category badges, change stack, security map → hunt → verify → fix | Split review into subject lanes; deterministic layer first; independent verification; blast radius; explicit change grouping | Posting only inline comments; model-only conclusions; hosted-only assumptions |
| Qodo / PR-Agent | Multi-agent focus (bugs, security, quality, tests), rules enforcement, test generation; open-source lineage | Per-subject agents with narrow prompts; rule/policy inputs; test-gap follow-up as a task, not an auto-edit | Treating generated tests as verified without running them |
| Greptile ([comparison](https://www.greptile.com/content-library/best-ai-code-review-tools)) | Repository graph, parallel agents, cross-file blast radius, house standards | Graph-first context; rank findings by blast radius and ownership | Maximizing recall at the cost of noise; no deterministic evidence |
| Graphite / Graphite Agent | Stacked changes and merge queue; agent applies fixes in the diff | A later "fix plan" layer that creates bounded tasks/goals; change-set framing for large work | A review product that only works with one VCS workflow |
| SonarQube ([metrics](https://docs.sonarsource.com/sonarqube-server/user-guide/code-metrics/metrics-definition), [comparison](https://safeguard.sh/resources/blog/semgrep-vs-codeql-vs-sonarqube-compared)) | Security/reliability/maintainability axes, coverage, complexity, duplication, technical debt, quality gates, new vs overall code, SAST/taint/secrets/IaC | A scorecard with per-subject status; severity and remediation effort; change-set deltas; quality-gate vocabulary | Treating one aggregate rating as truth; noisy debt ratios |
| CodeScene ([Code Health](https://codescene.com/product/code-health)) | Code Health from 25+ factors, hotspots, change coupling, knowledge mapping | Hotspot-weighted findings and churn/ownership; report the worst files first | Proprietary score as the only output |
| Semgrep / CodeQL | Rule-based patterns, dataflow/taint, interprocedural queries, variant analysis | Deterministic rule IDs, dataflow-aware security candidates, variant search once a pattern is found | Treating a pattern match as a confirmed exploit |
| Snyk / supply-chain tools | Dependency CVEs, licences, SBOM | Offline manifest/lockfile inventory and explicit unknown CVE state | Network lookups by default |
| Aider repo map ([docs](https://aider.chat/docs/repomap.html)) | Ranked, token-budgeted map before the model decides what to read | Use the existing repo map for the optional model pass and for report prioritisation | Assuming map context replaces source verification |

The common lesson across these products: **deterministic checks plus a
repository graph plus a verification/reporting layer** beats a single
"review this diff" prompt. The common failure is noise and over-claiming.
Ozagia should differentiate on offline/self-hosted operation, explicit
evidence grades and unknowns, graph-native architecture/data/scalability
analysis, and reports that are useful without a hosted service.

## Product contract

### Command

```sh
ozagia deep-review [--root R] [--out DIR] [--profile full|security|performance|...]
                   [--range BASE..HEAD] [--index] [--budget-s S]
                   [--baseline PREVIOUS_DIR] [--fail-on blocking|major|never]
                   [--max-findings N] [--json]
                   [--model-pass [--model PROVIDER/MODEL] [--max-tokens N]]
```

- **Off by default for models:** no network, no model and no code execution
  unless an explicit flag selects it. `--index` may refresh the graph; it is
  the only write besides the report directory.
- **Whole repo by default.** `--range` turns on change-set subjects and
  compares against the base commit; whole-repo checks still run unless a
  profile excludes them.
- **Profiles** select subject collectors, not a different engine. `full` is
  the default.
- **Output:** a directory (default
  `<root>/.ozagia/reviews/deep-<stamp>/`) with Markdown reports, a canonical
  `findings.json`, a `manifest.json`, and bounded evidence files. `--out`
  writes exactly there; no other repository writes.
- **Exit:** `0` when a report was produced, `1` when `--fail-on` matches a
  finding, `2` for invalid input, an unreadable root or a refused preflight.
  Findings are data, not a command failure by default.
- **MCP:** add `dev_deep_review` read-only, bounded summary/paths, writing only
  under `.ozagia/reviews/`. It must not start a model or run tests.
- **Capability catalogue:** `cap:review.deep` declares the CLI/MCP surfaces,
  subject ownership, limits and tests/docs; the self-graph audit must see it.

### Output layout

```text
deep-2026-11-18T.../
  README.md                     # summary, scorecard, top risks, index
  00-method-and-scope.md        # inputs, graph, tools, budgets, unknowns
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
  12-findings-index.md          # every finding, grouped by severity/subject
  findings.json                 # canonical machine-readable findings
  manifest.json                 # schema, hashes, coverage, statuses, reports
  evidence/                     # bounded excerpts, command records, hashes
```

Every subject file starts with scope and status, then a findings table, then
subject metrics, evidence, omissions and next actions. The README links to
each file and shows a scorecard: subject, status (`complete|partial|unknown`),
findings by severity, confidence mix and coverage.

Subject metrics are derived from finding/coverage counts and an explicit
allowlist of numeric or fixed status fields from each collector. They are
bounded and never render raw paths, logs, names or arbitrary collector values.

### Finding contract

```json
{
  "id": "dr-<stable-hash>",
  "subject": "security",
  "type": "untrusted_path_to_shell",
  "title": "...",
  "severity": "blocking|major|minor|note",
  "confidence": "high|medium|low|unknown",
  "evidence_kind": "declared|inferred|observed|verified",
  "status": "open|triaged|fixed|accepted|unknown",
  "locations": [{"path": "src/x.py", "symbol": "f", "line": 42,
                 "start": 42, "end": 55}],
  "evidence": [{"kind": "static|measured|recorded|model",
                "source": "ozagia.core.security",
                "method": "scan",
                "hash": "sha256:...",
                "excerpt_ref": "evidence/..."}],
  "impact": "...",
  "recommendation": "...",
  "effort": "small|medium|large|unknown",
  "owner": "module/path or null",
  "tags": ["owasp:a03", "cross-file"],
  "related_ids": ["dr-..."]
}
```

Rules:

- IDs derive from subject + type + stable location/symbol + rule key, not line
  numbers; a line move does not create a new finding.
- Every finding has at least one location or an explicit `scope` reason; every
  subject records what was not checked.
- `verified` is reserved for a check that actually ran and its evidence was
  re-read. Static inspection is `observed`/`inferred`, and a model claim is
  `declared` or `inferred`, never verified by itself.
- Severity is impact × confidence, with a separate `truth_assessed: false`.
  No exploitability or correctness claim without evidence.
- No secrets, raw prompts or unrestricted logs in findings or evidence.
- Findings are deduplicated across subjects by stable key and cross-linked
  (`related_ids`) rather than duplicated.

### Coverage and status

`manifest.json` carries:

- root, revision, range, profile, graph path/hash/freshness, tool versions;
- per-subject `{status, checks, skipped, omitted, unknown, findings}`;
- per-subject report path/hash and finding IDs;
- budgets, timings and truncation;
- `complete`, `partial`, `unknown` at the run level;
- `exhaustive: false` always.

A missing/stale graph, absent optional tool, unavailable test record or
unreadable manifest produces `partial`/`unknown` with a reason; it never
silently becomes a clean subject and never runs a graph-dependent check on an
old snapshot.

## Architecture

### New modules

```text
src/ozagia/core/deep_review/
  __init__.py          # public build_deep_review, DeepReviewError
  contract.py          # finding/subject/coverage/manifest schemas, stable IDs
  orchestrator.py      # plan, collect, normalize, dedupe, rank, compose, write
  collect.py           # adapters over existing repo_review/security/architecture/testing
  analyzers/
    security.py        # trust boundaries, authz, input validation, secrets, deps, IaC
    performance.py     # static hot paths, complexity, I/O-in-loop, caches, benchmarks
    failures.py        # error handling, retries/timeouts, cancellation, partial writes
    data.py            # data-model inventory, schemas, invariants, growth, serialization
    scalability.py     # locks, queues, backpressure, fan-out, caps, memory/disk growth
    architecture.py    # boundaries, cycles, coupling, ownership, blast radius
    tests.py           # failing/flaky/affected/coverage/mutation evidence
    dependencies.py    # manifests, lockfiles, licences, SBOM, offline CVE state
    operations.py      # config, runbooks, limits, observability, rollback
    docs.py            # stale references, public API and compatibility
  reports.py           # Markdown renderer, README/index, scorecard, TOC, links
  model_pass.py        # optional harness/review roles over compact packets
src/ozagia/deep_review_cli.py
tests/test_deep_review.py
tests/fixtures/deep_review/
docs/deep_review.md
```

No new store. The report directory is the durable artifact; `latest` is a
small pointer/copy only if explicitly requested. Report composition is pure and
deterministic after collection; timestamps and durations live in the manifest,
not in finding hashes.

### Pipeline

1. **Plan.** Validate the profile, root, range, output path and budgets.
   Resolve graph path/freshness, test record, project config, toolchain and
   optional tools. Record a plan hash.
2. **Collect.** Run subject collectors with per-subject timeouts and bounded
   concurrency. A collector returns findings plus checks/omissions/unknowns.
   Reuse `repo_review` sections and `security` rather than duplicating rules.
3. **Normalize.** Convert each collector's findings to the common schema;
   attach evidence, confidence, stable ID and cross-references.
4. **Deduplicate/rank.** Deduplicate by stable key; merge evidence; rank by
   severity, confidence, blast radius (graph degree/churn), reachability and
   user-supplied focus. Keep all raw findings in `findings.json`; the top-N
   is a view, not a deletion.
5. **Optional verify/model pass.** With `--model-pass`, render compact subject
   packets through `harness/review` roles, validate JSON, attach model
   evidence (`kind=model`, `truth_assessed=false`) and mark candidates
   `declared`/`inferred`. A deterministic check or person must promote them.
6. **Compose.** Render each subject Markdown from the same normalized data;
   build README, scorecard, TOC, findings index and manifest. Atomic writes;
   bounded evidence excerpts.
7. **Compare/exit.** With `--baseline`, compute new/resolved/unchanged by
   stable ID and subject; apply `--fail-on` only to the current run.

### Analyzer details

**Security.** Reuse `core.security` findings and `security_cli` (secrets,
dependency manifests, source patterns, optional bandit/gitleaks). Add
trust-boundary and authz facts from the graph: entry points, routes, commands,
filesystem/network boundaries, secret references, unsafe deserialization,
path traversal and shell construction. Run optional allowlisted tools only
when configured; record version and report hash. No CVE network lookup by
default; SCA is manifest/SBOM inventory plus explicit unknown.

**Performance.** Static candidates first: hot files from PageRank/churn,
nested loops, repeated I/O, synchronous work in async paths, unbounded caches,
per-request full scans, expensive serialization/parsing, missing pagination
and large allocations; existing `performance_risk` checks feed in. Optional
`--run-bench` accepts explicit caller-supplied commands with timeouts and
records measured results; otherwise all performance claims are `inferred`.
Report complexity and hot-path evidence, not a fabricated score.

**Failure modes.** Detect swallowed exceptions, broad catches that return
success, missing timeouts/cancellation, unbounded retries, non-idempotent
retries, partial writes, missing cleanup, resource leaks, concurrency races,
startup/shutdown ordering, degraded-mode behaviour and fail-open/closed
choices. Build a small FMEA table for critical paths (symptom, trigger,
detection, mitigation, evidence); link to tests/records where they exist. Do
not claim a failure is impossible because no test covers it.

**Data structures.** Inventory classes/schemas/records/JSON/YAML/TOML keys,
graph node/edge types and persistence formats; inspect mutability, identity,
canonical ordering, versioning/migration, retention/redaction and unbounded
growth. Report invalids and cross-store joins (same identity in multiple
stores, missing schema version, hash drift) using `contracts` and graph
metadata. No target import; static only unless an explicit safe fixture runs.

**Scalability.** Static reasoning over locks/shared state, queues, worker
pools, fan-out, batch sizes, pagination/cursors, resource admission, caches and
hard caps; use `core.resources`, `cgroup`, `task_profile/critical_path` and
graph size for bounds. Flag unbounded work and missing backpressure; separate
measured facts from assumptions. Optional load/bench runs only with explicit
commands and limits.

**Architecture.** Reuse `core.architecture` and `repo_review` architecture
sections; add blast-radius facts (PageRank/hubs, importers, affected packages),
layering/cycles, ownership/churn and dead-code candidates. Link to ADRs/docs
and note missing boundaries.

**Tests.** Reuse `core.testing` affected plans, failing/flaky test records and
`failures` classification. Report test gaps by package and critical path;
optional coverage/mutation commands only when configured. A test plan is not
an executed test; a passing record without freshness is not current evidence.

**Dependencies.** Parse manifests and lockfiles offline (pyproject, requirements,
package-lock, Cargo.lock, go.mod, Gradle, etc. as supported), list licences,
duplicate/conflicting versions, optional offline CVE database and SBOM.
Network/OSV lookups are opt-in and recorded.

**Operations/docs/correctness.** Reuse repo_review's ops/docs/dependencies and
code_review's change findings; add runbook/link/config/observability checks
and change-set semantics (breaking public API, missing migration, test impact).

## Phases and deliverables

### Phase 0 — contract and skeleton (first slice)

**Status: implemented** (2026-11-18).

- `core/deep_review/contract.py`, `orchestrator.py`, `collect.py`,
  `reports.py`; `deep_review_cli.py`; `ozagia deep-review` command.
- Compose existing `repo_review` sections, `security`, `architecture` and
  `testing`; write all subject files, `findings.json`, `manifest.json`,
  `README.md` and `findings-index.md`.
- Profiles: `full` and a single-subject profile; `--out`, `--index`,
  `--baseline`, `--fail-on`, budgets; no model/network.
- Fixture repo with known issues and a stale/missing-graph case; deterministic
  output/hash tests; CLI/MCP surface declaration in the capability catalogue.
- Acceptance: a complete report directory from an existing graph; partial
  status without a graph; no writes outside `--out`; tests/ruff.

### Phase 1 — performance, failure modes, data and scalability analyzers

**Status: implemented for the static analyzers, fixtures, cross-subject dedupe,
blast-radius ranking, bounded parallel analyzer collection, and the explicit
opt-in `--run-bench` argv interface.**

- Static analyzers with graph-aware evidence and explicit confidence.
- Independent static, dependency, operations, docs, data and supplied-profile
  analyzers use at most six isolated `python -I -B` workers, a shared deadline
  and fixed merge order; timed-out or failed lanes remain explicit coverage
  omissions.
- Fixtures for each pattern; cross-subject dedupe and blast-radius ranking.
- Optional `--run-bench` argv interface with timeouts and recorded summary
  artifact; default remains static. Results omit argv and captured streams.
- Acceptance: seeded findings land in the right subject/file; missing
  evidence is `unknown`; no model/network; deterministic report hashes.

### Phase 2 — security depth, dependencies and operations

**Status: largely implemented.** Implemented: optional allowlisted scanners
with explicit `--security-tool`/`--security-network` gates and per-tool
provenance; conservative Python trust-boundary candidates; offline
manifest/lockfile/licence/version inventory with deterministic SBOM and
opt-in exact-version or supported OSV-style range matching against explicit
local advisory data, including exact-version-only RubyGems-source Bundler and
Composer matching (overall CVE
state remains unknown); runbook links/topics, observability declarations,
lexical GitHub Actions workflow candidates for undeclared permissions,
`pull_request_target`, and non-commit-pinned actions; bounded Kubernetes/Compose YAML structural checks when optional PyYAML is
installed, bounded literal Terraform ingress checks in an isolated subprocess
when optional `python-hcl2` is installed, and lexical IaC/config candidates; docs/API
coverage and stale-reference candidates; fresh-graph range blast radius and
security-impact candidates; Gradle lockfile inventory and exact local Maven
coordinate matching (repository provenance remains unknown); read-only profile
reports. Remaining: broader
offline SCA coverage and advisory-dataset completeness, broader licence-expression
coverage and legal compliance assessment (a bounded caller-policy evaluator
for SPDX `AND`/`OR` expressions is implemented),
broader semantic IaC/config validation (Terraform module/provider evaluation
and broader config families remain), runtime telemetry verification and broader
observability checks.

- Trust-boundary/authz/dataflow candidates, optional allowlisted scanners,
  offline SCA/SBOM/licence inventory, IaC/config checks, runbook/observability
  checks, change-set security blast radius.
- Acceptance: optional tools recorded by version/hash; no network by default;
  a clean scan still reports `truth_assessed=false` and coverage limits.

### Phase 3 — model review, verification and Ocelot

**Status: partial.** Implemented: separately gated `--model-pass` with strict
schema validation, role-to-subject mapping, dedupe, candidate severity `note`,
redaction, `truth_assessed: false`, time/request/token/cost budgets, and
advisory `model-review.json` candidates that never block. Optional `--ocelot`
clusters model candidates only, with exact fallback and no install, network or
model call. Narrow deterministic corroboration records exact cited subject/type
matches but confirms rule identity only. Remaining: promotion of model wording,
impact and severity, and live-model quality evaluation.

- `--model-pass` with the existing independent role matrix: security,
  performance, failures, data, scalability, architecture, tests. Compact
  packets, bounded context, strict JSON, validation, dedupe, evidence kind
  `model`, no blocking. Optional Ocelot clustering only as candidate signals.
- Acceptance: model claims never become verified/blocking without a
  deterministic check; provider failure leaves the deterministic report
  intact; no secrets in packets.

### Phase 4 — evaluation and rollout

**Status: partial.** Implemented: seeded static, fixture, end-to-end report and
comparison harnesses (range, scripted model-pass, graph-aware versus
graph-less); baseline and status integration; `--fail-on`; a `review_my_change`
task; documentation and capability integration; a bounded working-checkout
smoke and an unfilled human-review annotation sheet. Synthetic results are in
`docs/results/deep_review_phase4_2026-11-18.md`; the working-checkout run and
sample sheet are in `docs/results/deep_review_real_checkout_2026-11-18.md` and
`docs/results/deep_review_real_checkout_review_sample_2026-11-18.md`. A frozen
HEAD smoke and its caveats are recorded in
`docs/results/deep_review_frozen_head_smoke_2026-11-18.md`, with an unfilled
top-ten human annotation sheet at
`docs/results/deep_review_frozen_head_annotation_sheet_2026-11-18.md`. Remaining:
clean frozen realistic-corpus precision/recall and cost comparisons, completed
human review, and validation of the proposed go/no-go thresholds in
`docs/results/deep_review_go_no_go_proposal_2026-11-18.md`. That proposal is
reviewable but unvalidated and does not authorize a blocking default.

- Seeded corpus and realistic fixture repositories; measure recall, precision,
  false-positive rate, report-noise ratio, duration, memory and report size.
- Compare whole-repo vs diff, graph-aware vs graph-less fallback, and optional
  model pass vs none. Keep quality gates caller-defined until measured.
- CI entry point (`--fail-on`), baseline comparison, docs, capability/status
  integration, and a "review my change" profile for the agent.
- Acceptance: published reproducible comparison, no hidden writes/network,
  clear go/no-go thresholds before any default blocking.

## Testing and evaluation

- Unit tests for contract/IDs/dedupe/coverage/status/hash determinism.
- Fixture tests for each subject and cross-subject patterns.
- Integration tests: real temp git repo, fresh and stale graph, missing test
  record, optional tool unavailable, partial subject failure, concurrent
  collectors and budget exhaustion.
- CLI tests: profiles, `--out`, baseline diff, `--fail-on`, `--json`,
  `--index`, invalid inputs, no writes outside output.
- Report tests: every subject file has scope/status/findings/coverage;
  README links resolve; manifest/report hashes match; no secrets.
- Eval: seeded corpus and precision/recall audit; baseline duration/memory;
  humans review a sample of top findings before calling the report useful.
- Optional model tests use fake runners only; live runs are separately gated.

## Non-goals and guardrails

- No claim that a clean report proves security, correctness or scalability.
- No untrusted target execution by default; no network; no model by default.
- No auto-fix in the first implementation. The later path is a preview/task/
  goal with the existing edit transaction and approvals.
- Not a replacement for a SAST/SCA service or a human review; optional tools
  are evidence sources, not verdicts.
- No universal quality gate. Thresholds are caller-defined and calibrated on
  fixtures before use.
- No hidden second store; reports are files under the chosen output/state dir.
- No duplicate implementations of repo_review/security/architecture rules;
  deep review composes and adds cross-subject analysis.

## Open decisions

- Default output location and retention (state dir vs caller path).
- Whether the first slice should include a single-subject `--profile` or only
  `full`.
- Which optional external tools are allowed and how their versions/hashes are
  pinned.
- Model-pass provider/budget policy and whether it may run in the same
  command or only separately.
- Data-structure analysis depth (AST/schema parsers vs graph metadata only).
- Baseline comparison identity rules when files move or symbols rename.
- Severity/confidence calibration and the first `--fail-on` default.
- Cross-repo/federation scope for a later profile.

## Success measures

- A reviewer can open `README.md`, see the top risks and know which subject
  files to read in under a minute.
- Every advertised subject produces a file with checks, findings, evidence and
  explicit omissions; a partial run is visibly partial.
- Seeded fixture findings are found in the right subject with no stale-graph
  contamination; false positives and noise are measured, not hidden.
- The same inputs produce identical findings, IDs and report hashes (apart from
  recorded timestamps).
- The optional model pass adds candidates without changing deterministic
  evidence or blocking status.
- Run time and report size stay within an explicit budget on Ozagia itself.
