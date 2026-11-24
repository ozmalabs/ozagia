# Ozagia release roadmap

Status: proposed 2026-11-24. This is the canonical sequencing document for
releases. The dated files under `docs/plans/` remain the detailed designs; this
page records which of them are actually done, what each release is for, and the
evidence required to ship it.

Current release: **0.1.4** (2026-11-24). `main` is on **0.1.5.dev0**. The
project is alpha: the library, MCP server, OpenCode plugin and harnesses work,
but several integrations are implemented and not yet verified against a live
host.

## Where we are

### What has shipped

The 0.1.x line delivered the core platform and then Rust parity:

| Area | State at 0.1.4 |
| --- | --- |
| Graph and retrieval | Files, symbols, tests, docs, imports, references and call edges; incremental reingest; repo map with confidence-weighted fusion; lexical + graph search; callers, callees and impact |
| Languages | Python by stdlib AST; tree-sitter tags for Rust, Lua, C, C++, Go, Java, JavaScript, TypeScript/TSX; optional LSP and SCIP |
| Rust | Structurally at Python parity (tags, symbols, modules, imports, edits, LSP, `cargo test`, migration overlay) except the normalized SCIP cache and a Rust dataflow analyzer |
| Editing | Preview-first transactions, symbol/CST/JSON/Markdown/HCL/YAML edits, base-hash checks, backups |
| Review | Deterministic change and whole-repo review, security, dependencies, operations, docs; deep review with subjects, evidence excerpts, manifest and baseline deltas |
| Tasks, goals, evidence | Bounded tasks, machine-checkable goals, evidence store, task preflight, task profiles and critical-path primitives |
| Sessions and integrations | OpenCode plugin and TUI add-in; Codex, Kiro and Claude Code hooks/installers; agent harness and native loop; session profiles; status, console and read-only views |
| Research | Sources, references, claims, evidence, synthesis, theory, PDF/TEI, citation graph, manuscript bundles |
| Infrastructure | Source-only Terraform/Kubernetes/Helm/CloudFormation/Pulumi/GitOps graph, offline IaC checks, operations graph, runtime services/relays/tunnels behind explicit gates |
| Federation | Sibling/explicit-member snapshots, qualified identities, read-only cross-repo search and declared links |
| Model interface | Contracts, read-only self-graph/capabilities, task preflight and evidence store, task profile/critical path, model-facing maps (M3), a bounded structured edit/interaction path (M4), an offline prefetch planner/ledger, and partial M5 linked/research/action joins with optional Ocelot research projection; M6 has an offline harness and one live run invalid for the map arm |

The capability catalogue currently declares **102 capabilities across 14
domains and 156 surfaces** (`ozagia capabilities list --json`). That number is
an inventory, not a maturity claim.

### What the evidence actually supports

| Claim | Evidence | Confidence |
| --- | --- | --- |
| Incremental indexing and graph queries are fast on a small crate | Rust parity bench, 27 files, all targets met | Good for that crate; not a scale benchmark |
| Repo-map retrieval is useful | 23-task SWE-bench Verified stratified baseline: 0.935/0.967/0.967 file recall at 500/900/1500 tokens; 10-task Django holdout | Moderate: file localization only, one model-free instrument |
| Graph assistance helps an agent | Small local-model pilots (12 + 12 sessions; 12/13 vs 12/13 assertions; descriptive wall-time medians) | Weak: small N, agent-scored, not human-verified, not generalizable |
| Deep review is high quality | Synthetic fixture score only: 4/4 true positives, 8/8 labeled pairs; no false positives in the declared scope. The realistic corpus now has 12 verified repository pins, 50 assigned ranges and 62 deterministic reports, but no human label or score. | Weak for production: no completed human labels, no realistic score, no go/no-go decision |
| Rust edits/LSP/tests work | Live rust-analyzer tests and `cargo test` capture; parity bench | Good on the test crate; dataflow and macro/cfg expansion remain absent |
| Live host integrations work | OpenCode has live smoke runs. Codex, Kiro and Claude Code are implemented and untested live. OpenCode Desktop is untested. | Weak: integration status is the largest untested surface |
| Secrets never reach the model | Reference broker, providers, redactor and in-memory session store landed (`core/secrets.py`); runtime/job/daemon boundaries resolve refs only. The native prompt/UI capture flow and end-to-end canary coverage are not implemented. | Partial: boundary code is tested; the full session guarantee is not established |

Two problems distorted the picture at the start of this audit. Both were
corrected on 2026-11-24:

1. **Plan statuses had lagged the code.** The bounded-prefetch plan said
   "design only" although the planner/ledger exist (integration does not); the
   self-graph/service plan said "proposed" although M0–M4 had landed; the
   session-profile plan had no status but the module, CLI and daemon hook
   existed; the usage-features plan had no completion marker although most waves
   landed. The dated plans and this table now record what is implemented,
   partial or design-only.
2. **The suite was not hermetic on a small `/tmp`.** Job and admission tests
   inherited the production job budget, so `test_codex_integration.py` failed
   when `/tmp` had ~4.5 GiB free. They now inject small test-only limits and
   zero floors/reserves; production admission is unchanged. The suite also has
   whole-file sharding (`OZAGIA_TEST_SHARD=i/n`), used by CI and release as four
   shards. Verified in the declared `.venv`: full suite `6436 passed, 29
   skipped` in 441 s; shard collections summed to 6461, the full collection,
   and all four shards passed. A bare system Python with only `PYTHONPATH`
   remains unsupported for tests that spawn an isolated interpreter.

### The strategic position

The product review's recommendation still holds: Ozagia should be a **local,
inspectable context and evidence layer** for existing agents, not another agent
UI and not a hosted review service. Its differentiators are the graph, the
transactional edit path, provenance-aware evidence and honest degradation.
The main risk is over-claiming breadth before the evidence exists.

Therefore the releases are ordered around **trust first, safety second,
breadth third, live operations last**. New surface area is not a release theme.

## Plan state audit

| Plan | Documented state | Actual state | What remains | Release |
| --- | --- | --- | --- | --- |
| `2026-11-23_rust_parity.md` | Phases 0–6 done; Phase 7 implemented with limits | Matches | Normalized SCIP cache, Rust dataflow, `migrates_to` graph edges, test mapping, macro/cfg expansion | 0.6 |
| `2026-11-18_deep_review_orchestrator.md` | Phases 0–1 done; 2 largely; 3–4 partial | Matches | Realistic corpus and human labels, broader SCA/IaC/config depth, model-pass quality evaluation, go/no-go | 0.2 |
| `2026-11-17_self_graph_service_unification_model_api.md` | Implementation plan; M0–M4 done, M5–M6 partial | M0–M4 implemented; M5 partial (linked/research/action joins, Ocelot research projection, secret boundaries); M6 has an offline harness and one live run invalid for the map arm | M5 Ocelot/live-service integration; a valid M6 matched-arm result and opt-in rollout | 0.4 |
| `2026-11-17_session_secrets_ui.md` | Design only | Core broker, providers, redactor and session store implemented (`core/secrets.py`); the native prompt/UI capture flow is not | Native prompt, host rewrite, persistence UI, revoke/expiry, end-to-end canary review | 0.3 |
| `2026-11-17_session-profile-instrumentation.md` | Implemented in part | Core store, CLI, daemon events, OpenCode subscriber and TUI slot exist; Codex/Kiro/Claude capture does not | Complete lifecycle across hosts, status/console surfaces, coverage tests | 0.3 |
| `2026-11-18_bounded_graph_prefetch.md` | Offline primitives implemented; MCP integration design only | Offline planner, safety checks and ledger implemented (`core/prefetch.py`); no MCP lifecycle integration | Publication/freshness/cancellation integration **only if measured** | 0.3 (decision only) |
| `2026-11-17_opencode_intent_graph_retrieval.md` | Stages A–C done; D partial; E initial offline check | Matches | Broader shell/task intent linking, richer paired and end-to-end evaluation | 0.4 |
| `2026-11-17_federated_graphs.md` | Base delivery | Discovery, explicit members, qualified identities, status/search/node inspection, cross-repo checks landed | Version-aware relationships, persistent identity, incremental cache, org manifests/access control | 0.4 |
| `2026-11-16_sre_iac_extensions.md` | Offline workstreams implemented; live remains | Matches | Live cloud/cluster providers, semantic IaC validation, broader config families, real-host relay matrix, remote background services | 0.5 |
| `2026-11-16_iac_cloud_context.md`, `2026-11-16_runtime_services_tunnels.md` | Implemented offline | Opt-in source-only IaC context and the offline runtime/service/tunnel layer are implemented; live remote operations are not | Live cloud/cluster state, remote service ownership, measured savings | 0.5 |
| `2026-11-16_ui_and_agent_features.md` | Waves merged | Matches; "Not yet" list still open, except project-model config migration, which landed in 0.1.5 | Live Desktop smoke, live model/provider adapters, live security tool matrix/DAST | 0.1.5 / 0.3 |
| `2026-11-16_usage_features_plan.md` | Waves 1–4 implemented | Waves 1–4 implemented (Codex/Kiro, ledger, jobs, structured data, affected tests, lint, path resolution, ownership, replace, ops graph, research positions, fetch fallback) | Measure the claimed savings; live Codex/Kiro verification | 0.1.5 |
| `2026-11-16_v4_open_issues.md` | Implemented with review fixes | Matches | None required for this roadmap | — |
| `2026-11-17_cross_repo_checks.md`, `2026-11-17_env_failures_timing.md` | Implemented | Implemented | Version-aware relationships, scale/live timing validation | 0.4 / 0.1.5 |
| `2026-11-16_ozma_integration.md`, `2026-11-16_proactive_context.md`, `2026-11-16_repo_review_and_goals.md` | Third delivery / implementation notes | Implemented; HTTP surface, general watcher, live benchmark and live goal runs remain | As listed per document | 0.4 |
| `2026-11-17_self_graph_ab_testing.md`, `2026-11-17_self_graph_execution.md` | Pilot partly executed; execution plan historical | Pilot partly executed; execution plan superseded | A frozen valid paired run before changing defaults | 0.4 |
| Research plans (`reference_layer`, `deferred_research_work`, `paper_review`, etc.) | Landed stages A–D | Matches | Reproducible review package, labeled literature evaluation, OCR/PDF limits | 0.6 |
| `2026-11-15_*` research/position papers | Historical or re-homed | Not release work | Keep as design input | — |

## Roadmap at a glance

| Release | Theme | Outcome | Primary plans |
| --- | --- | --- | --- |
| **0.1.5** | Green and truthful | Every claim has a test or an explicit "untested"; main is green on a clean host; plan statuses match code | Usage feature measurement, UI "Not yet", integration matrix |
| **0.2.0** | Evidence-grade review | Deep review has a reproducible, human-labeled quality result and a published go/no-go; blocking remains caller-selected | Deep review Phase 2–4 |
| **0.3.0** | Safe, observable sessions | Secrets never enter model context; session/usage/job data is visible without content | Session secrets UI, session profile, status expansion |
| **0.4.0** | Connected repositories and model API | Multi-repo context and the model-facing API are proven on fixtures and matched evaluations, still opt-in | Federation follow-ons, self-graph M5–M6, Ozma context provider |
| **0.5.0** | Guarded live operations | Live AWS/Kubernetes/relay paths are usable under explicit approval, policy, audit and redaction | SRE/IaC extensions, relay backends |
| **0.6.0+** | Depth and research | Precise Rust layers, evidence-driven language expansion, auditable research packages | Rust Phase 7 leftovers, research P1 |

Rough sequencing, not commitments. 0.1.5 is a short stabilization release;
0.2–0.4 are multi-week efforts gated by external evidence and host APIs. No
calendar date is promised.

## Release principles and gates

Every release satisfies these before tagging:

1. **Green on a clean host.** Full suite and `ruff check src tests` pass in a
   fresh environment with documented resource assumptions. No test depends on
   incidental `/tmp` size.
2. **Status truth.** Every plan is marked implemented, partial or design-only
   against the code; this roadmap and `FEATURES.md` are updated.
3. **Evidence or a label.** New behaviour ships with tests and a result file
   under `docs/results/`, or is explicitly marked untested with the version and
   environment it was not tested against.
4. **No new mandatory dependency.** The core remains standard-library only;
   parsers, language servers, models and network providers stay optional and
   degrade clearly.
5. **No hidden writes or calls.** Tests, status and review paths do not write
   outside the state directory or call a model/network without an explicit
   gate. Secret canaries are part of the suite.
6. **Capability catalogue updated.** `capabilities.py`, docs and MCP/CLI
   surfaces agree; an unavailable capability is not advertised as permitted.
7. **Upgrade and removal work.** The bundle, installer, `self check`,
   `self upgrade` and `self uninstall` smoke tests pass for the release.
8. **Changelog and rollback.** Release notes state what changed, what was not
   tested, and the previous version to return to.

## 0.1.5 — Green and truthful

**Outcome:** a new user can trust the install and the documentation; a
maintainer can trust a green suite.

**Progress (2026-11-24, re-run 2026-11-25):** suite hermeticity and four-way
sharding landed (6461 collected items; full suite `6436 passed, 29 skipped` in
441 s; all four shards green); plan and feature statuses reconciled against
code and tests; the integration matrix and doctor/status health landed;
`ozagia config migrate` copies the four project-model tables; security
hardening added exact static-finding allowlisting and removed reference false
positives; release verification is recorded in
`docs/results/release_check_0.1.5_2026-11-24.md`. The tree is committed as
`4d3ff1f` (no tag), and the committed-delta review has no blocking findings;
see `docs/results/self_review_2026-11-24.md`. Remaining: live Codex, Kiro,
Claude Code and OpenCode Desktop sessions; a network-gated bundle
build/installer smoke; and a realistic deep-review quality result (0.2.0).

**Scope**

- Make the suite hermetic and bounded:
  - inject resource policy/limits into daemon and job tests instead of relying
    on host disk/memory (the current Codex job test fails on a 15 GiB `/tmp`);
  - split or shard the suite so the default run fits the CI budget; keep the
    full run available;
  - add a release-gate job that reports the exact test count and duration.
- Reconcile plan and feature statuses: bounded prefetch (primitives only),
  self-graph M0–M4, session profile, usage waves, deep-review phases, Rust
  Phase 7 limits, migration limits. Link each to code and tests.
- Publish a live-integration matrix with pinned versions: OpenCode TUI and
  Desktop, Codex hooks, Kiro hooks, Claude Code hooks, pyright and
  rust-analyzer. Each entry is `verified <version>` or `untested`.
- Tighten `ozagia doctor` / `ozagia-status`: missing tool vs unknown, snapshot
  freshness, integration version outside the tested range, capability
  catalogue drift.
- Fix the "Not yet" list where it is cheap: `ozagia config migrate` for
  `[environments]`, `[services]`, `[relays]`, `[runtime]`; document the
  remaining live-only gaps.
- Release notes and a short "what 0.1.x established / what it did not" page.

**Exit criteria**

- Full suite green on a clean host within the CI budget; no known red.
- Every active plan has an accurate status line.
- README and FEATURES state the same integration truth as the matrix.
- No user-visible feature change that requires a migration note.

**Non-goals:** new review logic, new languages, new live providers, secrets UI.

## 0.2.0 — Evidence-grade review

**Outcome:** `ozagia deep-review` is defensible in caller-selected CI, with
published per-subject quality and a recorded go/no-go. It still does not block
by default.

**Progress (2026-11-24):** candidate manifest (12 repositories, 50 slots),
reviewer/adjudicator label schema, offline verification, explicit-network
acquisition, deterministic scoring and a synthetic dry run landed. The corpus
is now acquired and pinned: 12 repositories verified at release-tag commits
with tree/archive digests and verified licenses, 50 real ancestor `base..head`
ranges assigned, and 62 deterministic whole-repo/range reports collected with
blank reviewer packets (external to this repository). No human label exists and
no gate is measured; score status remains `not_measured`.

On 2026-11-25 an internal noise audit of those 62 reports sampled the
highest-volume categories, fixed six generic false-positive or
duplicate-emission patterns (test-name fallbacks, internal workspace path
dependencies, explicit non-security hashes, cookie-jar receivers, trust-flow
code duplicates and deleted-binary duplicates), and regenerated all reports and
packets from the same pins (3,407 -> 3,234 report entries). The sample evidence,
holdout exclusion and remaining noise are recorded in
`docs/results/deep_review_noise_audit_2026-11-25.md`. A follow-up on the same day
fixed two more generic defects: documentation/API import checks now ignore
fenced, indented and inline code examples while still checking prose, and
non-source deletions (docs, images, generated assets) use the non-breaking
`non_breaking_change` type instead of `breaking_change` while source, config and
executable deletions stay breaking. Reports and packets were rebuilt again
(3,234 -> 3,218 entries; 69 breaking-change entries became 18 breaking plus 51
non-breaking). This is a noise/quality work note, not a quality result: there
are still 0 labels, no precision/recall and no measured gate.

**Scope**

- Build the frozen realistic corpus and labeling protocol from the go/no-go
  proposal: at least 10 repositories and 50 change ranges, multiple sizes and
  three language families, a separate holdout, two independent labels plus an
  adjudicator, and published label digests.
- Complete the deterministic subjects that remain partial:
  - SCA breadth: decide build-vs-integrate for Python/Ruby/Composer/Maven
    range matching; do not add a network advisory feed by default;
  - semantic IaC validation only where it can be source-only and honest;
  - broader configuration families, runbook/observability links and docs/API
    coverage already started.
- Run the comparison protocol: whole-repo and range reports, graph-aware vs
  graph-less, repeatability, budget and memory, stale/missing graph behavior.
- Evaluate `--model-pass` with a fake runner first; one live-provider study
  only with explicit approval. Candidates stay advisory and never change exit
  status.
- Publish precision, recall, noise, subject-level results, actionability and
  operational bounds. Record every miss and partial run.
- Add a stable "review quality" result page and link it from README, deep-review
  docs and `ozagia-status`.

**Exit criteria**

- The proposed gates are either met or explicitly revised with evidence.
- No stale or missing graph can produce a clean graph-dependent subject.
- Deterministic IDs and findings reproduce from frozen inputs.
- Every subject publishes coverage and unknowns; no aggregate hides a weak
  subject.
- The default remains `--fail-on never`.

**Non-goals:** enabling a blocking default, automatic fixes, hosted review,
model-only conclusions.

## 0.3.0 — Safe, observable sessions

**Outcome:** a user can answer a secret request without the value reaching the
model, and can see what a session actually did without storing its content.

**Scope**

- Implement the session-secret design:
  - `secret://session/<opaque>` refs and provider refs (`keyring`, `op`,
    `pass`, `.env` with explicit opt-in);
  - OpenCode prompt rewrite at the earliest hook plus the HTTP fallback;
    native secret dialog rather than a custom form;
  - daemon resolves only at the process boundary; tool output, chat, logs,
    profiles, jobs and maps carry refs only;
  - decline, cancel, expiry and revoke; rotation instead of reveal;
  - MCP clients use URL/out-of-band flow, never a form schema containing a
    secret;
  - canary tests across chat, logs, traces, edits, jobs and model-review
    artifacts.
- Finish session instrumentation:
  - complete tool lifecycle capture across OpenCode, Codex and Kiro;
  - bounded per-session store (model name, token counts, timing milestones,
    tool outcome counts; no prompts, paths or tool bodies);
  - status, console and TUI surfaces with stale/unknown states.
- Expand `ozagia-status` with sessions, jobs, admission refusals, provider
  connections, capability probes and snapshot size metrics, keeping the
  existing envelope and absent-vs-unknown rule.
- Surface cross-session ownership: who has uncommitted edits to a file, and
  which live session owns it.
- Decide bounded prefetch: integrate only if the offline acceptance tests show
  a real latency win without freshness risk. Otherwise close the plan as
  "measured, not worth it" and keep the planner primitives.

**Exit criteria**

- A secret canary never appears in model context, state files, logs, profiles,
  job output or errors.
- Profile data survives daemon teardown and is content-free by construction.
- Status distinguishes absent, unknown, stale and configured for every new
  section.
- Prefetch has a written keep/drop decision with measurements.

**Non-goals:** secret sync between machines, a plaintext long-term store,
automatic third-party authentication, inferred critical paths.

## 0.4.0 — Connected repositories and model API

**Outcome:** multi-repository context and the model-facing API are useful on
real fixtures, provenance-preserving and still opt-in.

**Scope**

- Federation follow-ons:
  - version-aware cross-repository relationships from declared dependencies or
    precise SCIP identities (no name-matching inference);
  - persistent identities across membership changes;
  - incremental member caching and freshness certification;
  - organization manifests and access control boundaries.
- Complete model API M5:
  - linked services and research projections in preflight and maps;
  - optional Ocelot adapter for one narrow, documented operation;
  - declared service/process/tunnel actions through the existing preview and
    confirmation semantics;
  - secrets by reference only at execution boundaries.
- Complete M6:
  - matched arms: plain, well-prompted baseline, existing Ozagia, Ozagia plus
    map/service path;
  - fixed tasks, repositories, models, permissions and independent checks;
  - at least one smaller and one larger model, cold and warm indexes, repeats;
  - headline measures: time to verified outcome and verified tasks per hour;
  - report component and amortized costs, including prefetch waste.
- Ozma context-provider proof of concept (the product review's P1):
  - versioned, read-only, typed API with pagination, errors, provenance and
    authorization before network exposure;
  - multi-service fixture proving endpoint → schema → consumer → test
    traceability, freshness after edits and checkout changes, and explicit
    unknowns.
- Expand shell/task intent linking and richer paired usefulness evaluation from
  the OpenCode intent plan.

**Exit criteria**

- Two-repository and research fixtures show provenance, stale/unknown states
  and no cross-repository writes.
- Compare-map and compare-service paths never promote unavailable or
  unauthorized services to allowed.
- Published matched-arm evaluation; if the well-prompted baseline wins, say so
  and keep the reproducibility findings.
- No automatic model routing, compressed wire format or blocking review.

**Non-goals:** general cross-repo semantic inference, mutating HTTP APIs,
coordinated multi-repo actions, organization-wide ingestion.

## 0.5.0 — Guarded live operations

**Outcome:** an operator can use live read paths for AWS, Kubernetes and relay
targets under explicit approval, with audit, redaction and rollback.

**Scope**

- Implement live read providers (AWS CLI/Kubernetes) behind explicit flags,
  policy and audit; fake-provider contract tests first, then one approved live
  run per provider with the environment recorded.
- Complete semantic IaC validation and the broader config families; keep
  source-only analysis the default.
- Map runtime telemetry (`podman`/`journalctl`/`kubectl` output) to operations
  graph nodes in `after` context.
- Verify relay/tunnel live paths (OpenSSH, SSM) on a real host with approval;
  document timeouts, reconnects, cleanup and failure behavior.
- Make live-state snapshots, diff and resume summaries usable end to end.
- Ensure secrets used by live actions resolve by reference only and never
  appear in receipts, traces or model messages.

**Exit criteria**

- Dry-run output and live output are diffable; every live action has a receipt
  and an explicit coverage/unknown statement.
- No live provider is enabled by configuration alone.
- Secret canaries pass across receipts, logs and snapshots.
- Documented rollback for every mutating action.

**Non-goals:** autonomous remediation, continuous cluster reconciliation,
credential storage, unattended cloud writes.

## 0.6.0+ — Depth and research

**Outcome:** depth where users actually work, chosen from measured demand.

**Candidate work**

- Rust precise layer: normalized SCIP cache, Rust dataflow/trust-boundary
  analysis, `migrates_to` graph edges, cross-language test mapping, conservative
  macro/cfg reporting.
- Language expansion: only languages found in real user repositories, each as
  a support package with a published capability matrix and install story.
- Research P1: reproducible review package (protocol, search strings, decisions,
  hashes, locators, flow counts), labeled-literature evaluation, OCR/PDF
  limits, optional semantic retrieval only if held-out queries justify it.
- Freshness: optional filesystem watcher with reconciliation fallback, and
  measured cold/warm/update latency on larger repositories.
- Multi-file LSP refactor plans as one reviewed transaction.
- A durable database backend only if single-snapshot memory/update measurements
  demand it.
- Bounded prefetch integration if 0.3's decision leaves it open.

**Exit criteria:** each item has a measured use case, a fixture, and a result
file; otherwise it stays on the backlog.

## Cross-cutting workstreams

| Workstream | Owns | Applies to |
| --- | --- | --- |
| Verification and evals | retrieval, agent, deep review, matched-arm protocols, result files | 0.1.5–0.6 |
| Documentation truth | plan statuses, FEATURES, README, capability catalogue, integration matrix | every release |
| Security and privacy | secret refs, redaction, canaries, policy, audit | 0.2–0.5 |
| Host integrations | OpenCode (TUI/Desktop), Codex, Kiro, Claude Code, MCP clients | 0.1.5–0.4 |
| Distribution | bundles, pins, installer, self check/upgrade/uninstall, smoke | every release |
| Performance and freshness | index bench, cold/warm, reingest, prefetch accounting | 0.1.5, 0.3, 0.6 |

## Deliberately deferred

These are not on the release path:

- a separate full agent UI or IDE replacement;
- bundling Ocelot, a model, an embedding model or a vector database;
- automatic model routing, automatic blocking review or autonomous remediation;
- a hosted review/search service or multi-tenant access control;
- bulk citation mirroring, paywall bypass or automatic support claims;
- importing the historical Ozma protocol wholesale as a normative runtime;
- adding languages or live providers ahead of measured demand;
- long-term plaintext secret storage or secret synchronization.

## Open decisions

1. **When is 1.0?** Suggested bar: 0.2's quality gate passed, secrets shipped,
   at least OpenCode plus one other host verified live, two consecutive
   releases without a schema break, and a documented upgrade path. Decide
   before 0.4.
2. **Which host is primary?** OpenCode is the only live-verified host. Codex is
   87% of observed tool calls but unverified. Fund verification by measured
   use, not by feature count.
3. **Build or integrate SCA and live cloud reads?** Prefer explicit local
   advisory files and existing CLIs; do not add a default network dependency.
4. **Does prefetch ever ship?** Only if 0.3's measurements show a real win. The
   current measured ceiling is small (~1.26 s of cache work on one synthetic
   tree) and the race surface is large.
5. **How much model-assisted review?** Model candidates remain advisory. A live
   quality study is a separate approved experiment, not a release requirement.
6. **Cadence.** 0.1.x shipped quickly; 0.2–0.4 are gated by human labeling and
   host APIs. Use pre-releases (`0.2.0rc1`) when the code is ready but the
   evidence is not.

## How to keep this roadmap honest

- Update the plan-state audit when a plan lands or is closed; do not leave a
  "proposed" plan whose code already shipped.
- Put measurements in `docs/results/` with commit, environment, commands and
  limitations. A fixture score is not a realistic-corpus score.
- Prefer closing a plan as "measured, not worth it" over quietly enabling a
  speculative path.
- If a release misses its exit criteria, move the feature, not the bar.
