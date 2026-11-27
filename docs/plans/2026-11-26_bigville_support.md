# Bigville support plan

Goal: give coding agents source-backed context and useful checks for Bigville's
data-driven simulation, while keeping project knowledge outside Ozagia core
and never importing or modifying the target project.

1. Establish an offline retrieval baseline with representative scenario,
   simulation, determinism, client/API and Ocelot integration tasks. Record
   evidence and limitations; compare improvements against the same cases.
2. Provide a read-only project adapter for authored scenarios, seed and pack
   data, linking only relationships supported by source evidence. Preserve
   provenance and refresh correctly when inputs change.
3. Make project-declared behavioral constraints and verification commands
   available as source-backed task context. Treat them as guidance, not proof
   that checks passed; preserve conservative test-selection fallback.
4. Assess browser/API navigation and cross-repository Ocelot links using the
   existing language and federation capabilities. Implement or document the
   smallest reliable integration; avoid asserting inferred cross-repo edges.
5. Assess historical versus current documentation retrieval. Prefer explicit
   project configuration and evidence over assuming old documents are stale.
6. Run targeted tests, lint and the offline evaluation; record observed results
   and any remaining gaps. Live model evaluations require the existing live
   gates and human approval.

Work is delegated to Luna agents for the adapter, invariant context and
evaluation, with integration and verification coordinated in this checkout.

## Execution status

- Implemented the read-only adapter, including path-distinct seed manifests,
  declared dependencies, scenarios, packs and bounded client/server route
  evidence. Setup and federation limitations are in `docs/bigville_adapter.md`.
- Item 4's cross-repository link is now served by declared federation
  `relationships`/`families` in the anchor's `.ozagia/federation.json`
  ([federation](../federation.md#repository-relationships),
  [plan](2026-11-27_repo_relationships.md)), inspected with
  `ozagia federation relations` and included in `ozagia federation check` as
  `repository_relations`. The
  [Bigville adapter](../bigville_adapter.md#ocelot-federation) records a worked
  Bigville ↔ Ocelot manifest. This is declared, provenance-bearing evidence,
  not inferred: federation does not
  derive edges from Bigville's dynamic `import_module` call, module or
  repository names, search hits, Git remotes or runtime calls, so inferred
  cross-repository edges remain unasserted. Browser/API navigation remains the
  adapter's bounded route evidence; federation's API section stays `absent`
  until an OpenAPI operation or explicit `ApiMapping` is indexed.
- Implemented project-declared invariants and task-matched key-document
  priority, with bounded context and explicit evidence status. Configuration
  examples are in `docs/project_invariants.md`.
- Recorded the offline task set and controlled retrieval comparison under
  `evaluation/bigville_retrieval/`. The initial source-localisation tasks do
  not measure scenario-to-seed navigation; a separate bounded graph probe
  checks representative seed ambiguity, generated provenance and route links.
  Neither establishes a general retrieval benefit.
- Adapter links between data and tests remain navigation evidence. Literal
  mentions do not prove complete behavioral coverage and therefore do not
  bypass the configured fallback. JSON is treated as source data unless it has
  a recognized config filename, preventing unindexed or mixed data/docs
  changes from becoming docs-only plans. Pack/table inputs and runtime-loaded
  data remain outside any automatic test-coverage claim. The adapter exposes
  bounded advisory data-to-test evidence. Ozagia also accepts optional generic
  `[test_coverage]` rules only when the project explicitly marks them complete,
  names existing discovered tests, and supplies an evidence note; unmatched or
  ambiguous adapter evidence retains the configured fallback unless a
  matching complete project declaration covers that changed path.
- Scenario mentions now resolve against full manifest paths, including after
  nested scenario creation or deletion. Duplicate IDs remain distinguishable.
- Search ranking now uses node-type-local label frequencies, and ambiguous
  single-word anchors no longer automatically crowd out a full-query match.
  The 2026-11-27 comparison records improvements and remaining recall
  tradeoffs; it is a small offline evaluation, not a general performance claim.
- The adapter reports unresolved generated-table declarations rather than
  dropping them. A real-checkout audit found seven such declarations; their
  recorded generation hashes remain unverified. Refresh tests cover adding and
  removing table definitions and changing source mentions.
- A read-only test-evidence audit found six literal associations across 1,001
  Bigville data subjects. It does not justify declaring complete coverage; see
  `docs/results/bigville_test_coverage_audit_2026-11-27.md`.
- An additional local ranking stress probe found equal aggregate recall for
  global and type-local IDF, with different selected files in five runs. Its
  small, non-source-focused task set limits the conclusion; see
  `evaluation/ranking_audit/TYPE_LOCAL_IDF_EXTERNAL_FIXTURES_2026-11-27.md`.
