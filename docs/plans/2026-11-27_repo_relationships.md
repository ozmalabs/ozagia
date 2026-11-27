# Repository relationships across federation members

Status: implemented 2026-11-27; adapter-declared dynamic bridges (Phase 2 W5),
the persisted relationship supergraph cache (Phase 2 W6) and opt-in
family-aware ranking (Phase 2 W7) added 2026-11-27; targeted verification
passed 2026-11-27. The read-only report is in
`core/repo_relations.py`, with canonical API references and explicit API
mapping rollups in `core/api_relationships.py`, and the adapter bridge helper
in `core/repo_bridges.py`. `core/supergraph.py` persists that relationship
report, and `core/federation.py` implements the opt-in family rank boost. The
surfaces are `ozagia federation relations`, `ozagia federation check` /
`federation_check` (as `repository_relations`), `ozagia federation build` /
`graph`, and `federation search --family-boost` / the `federation_search` MCP
`family_boost` option. Tested by `tests/test_repo_relations.py`,
`tests/test_api_relationships.py`, `tests/test_repo_bridges.py`,
`tests/test_bigville_adapter.py`, `tests/test_supergraph.py` and
`tests/test_family_ranking.py`.
Follow-ons still not implemented: automatic dynamic-import discovery,
Git-remote lineage inference, lockfile/transitive resolution, a full
node-union supergraph, a docs-derived API-reference source, organization-wide
manifests/access control, and a real-repository paired evaluation of family
ranking. Dynamic bridges are implemented only as explicit, evidence-bearing
adapter declarations; Ozagia does not discover them by parsing an import
string. The persisted artifact is a bounded relationship cache, not a member
node union, and `family_boost` remains off by default.

## Objective

Make repository relationships first-class alongside flat federation
membership. A person or agent should be able to see that Bigville uses an
Ocelot runtime adapter, that two repositories form a family, or that one
repository's declared package depends on another's, without treating a flat
member list or a name match as a relationship. All edges must retain their
provenance and unknown evidence must stay unknown.

## Design

- Extend the anchor's `.ozagia/federation.json` with optional
  `relationships` and `families` arrays. Membership, reference authority and
  node-level `links` keep their existing meaning and validation.
- Relationship endpoints are selected member ids (declared id or derived
  alias), not qualified node keys. `relation` is a bounded string classified
  as dependency, lineage, integration, grouping, documentation or opaque.
  `provenance` is required; `evidence` is optional and never read.
- Families group members and are reported as a tag on each member. They are
  not dependency edges.
- Roll up resolved `dependency_links` package rows into one directed
  consumer → provider edge per pair. Keep package, version, manifest and
  aggregate status evidence. Unresolved and ambiguous package rows remain in
  `dependency_links` and do not become repository edges.
- Merge declared and derived edges in a `dependencies` view used only for
  strongly connected dependency cycles, topological order/levels and direct
  reverse dependencies. Lineage/integration/grouping declarations stay
  descriptive.
- Build repository-level API relationships from explicit `ApiMapping` nodes.
  Canonical API references prefer OpenAPI `operationId`, then OpenAPI
  method/path, then an explicit code reference, then a bare operation
  identity. A consumer or test edge requires an explicit producer mapping for
  the same reference; matching operations across snapshots never creates an
  edge. API edges are integration evidence, not package build dependencies.
- Accept explicit `RepoBridge` snapshot nodes emitted by adapters for a dynamic
  integration visible in the adapter's own checkout. The helper validates
  source/target aliases, a bounded relation, provenance, bounded evidence and
  `explicit: true`; it performs no file read or target import. `repo_relations`
  emits `origin: "adapter_bridge"` integration edges only when both endpoints
  are selected members, reports other targets as unpaired, deduplicates and
  keeps the edges out of the dependency view.
- Persist the relationship report from `cross_repo.relations` as a versioned,
  bounded `ozagia.supergraph.v1` cache with content and source hashes. It
  stores member metadata, relationship/family/API/bridge edges, API
  references/mappings and dependency analysis, not member nodes. Reads report
  `fresh`, `stale`, `absent` or `invalid` and never rebuild implicitly.
- Add an opt-in `family_boost` to federated lexical search. The default stays
  off; the enabled arm adds a bounded rank preference to hits from members that
  share a declared family with the anchor, retains every non-family hit and
  keeps deterministic tie-breaking. The CLI and MCP surface the same boolean.
- Never infer from repository names, symbol names, search results, dynamic
  imports, Git remotes or lockfiles. Do not run package managers, import
  members or write to a member. Reference synchronization remains the only
  possible network/write path and keeps its existing opt-out.

## Delivery

- `core/repo_relations.py`: manifest validation, package-edge rollup,
  dependency aggregation, cycle/topological/reverse analysis, bounded output.
- `core/api_relationships.py`: canonical contract-first references from
  OpenAPI operations and code locations; explicit producer/consumer/test
  mapping rollup; unpaired, ambiguity and bounds handling.
- `core/repo_bridges.py`: explicit adapter bridge validation and a stable
  `RepoBridge` node key; no filesystem or target access.
- `core/repo_relations.py`: read `RepoBridge` nodes into bounded
  `origin: "adapter_bridge"` integration edges, unpaired reporting and
  deduplication, separate from the dependency view.
- `addins/bigville_adapter.py`: declare the Bigville → Ocelot
  `dynamic_import` bridge from `bigville/backends/ocelot.py` evidence without
  reading or importing the Ocelot checkout.
- `core/supergraph.py`: persist the `cross_repo.relations` report as a bounded
  `ozagia.supergraph.v1` artifact with content/source hashes, atomic writes and
  `fresh`/`stale`/`absent`/`invalid` reads; it does not store member nodes.
- `federation_cli.py`: add `federation build` and `federation graph`
  (`--refresh`, `--out`) for the relationship-supergraph cache.
- `core/federation.py`, `federation_cli.py`, `integrations/mcp/server.py`:
  expose opt-in `family_boost` on federated search; default off.
- `evaluation/family_retrieval/`: paired fixture evaluation of the opt-in
  family arm, with no default change.
- `core/cross_repo.py`: include `repository_relations` in `check`; add
  `relations(root, roots=None)`.
- `federation_cli.py`: add `federation relations` with the existing
  `--root`, repeated `--repo`, `--json` and `--no-sync` options.
- `integrations/mcp/server.py`: describe repository relationships in
  `federation_check` (the tool already returns the section).
- `core/capabilities.py`: include the relation report in the federation check
  capability.
- Tests: declared edges/families, package rollup, no name/snapshot inference,
  cycles, DAG order/levels, invalid declarations, contract-first API
  references, explicit API mapping rollup, unpaired and rejected mappings,
  `cross_repo.check` integration and the CLI command; bridge validation,
  rejection, unpaired targets, deduplication, mutual bridges and Bigville
  adapter indexing; supergraph hashes, bounds, atomic writes and freshness;
  opt-in family ranking, default-off behavior and deterministic ties.
- Docs: federation membership/relationships, adapter bridges, family-aware
  retrieval, cross-repository checks, `docs/results/supergraph_2026-11-27.md`
  and `docs/results/family_retrieval_2026-11-27.md`.

## Worked example

The Bigville ↔ Ocelot manifest in
[`docs/federation.md`](../federation.md#repository-relationships) uses two
member ids, `bigville` (the anchor's derived alias) and `ocelot` (declared in
the manifest); mutual integration edges `bigville uses ocelot` and
`ocelot plugin_for bigville`; and a `simulation-stack` family containing both.
The two directions are distinct
provenance-bearing declarations supplied in the anchor manifest. They cite
`bigville/backends/ocelot.py`,
`tests/test_bigville_ocelot_fresh_process.py`,
`worlds/bigville_ocelot_adapter.py` and `docs/features/bigville.md`; evidence
strings are repository-relative and are never read or fetched. They are not
inferred from Bigville's dynamic import of Ocelot, module or repository names,
Git remotes or runtime calls. Because both relations are integration kinds,
the pair is reported without a dependency cycle, topological-order change or
reverse build dependency; the family groups members without adding an edge.

## Adapter-declared dynamic bridges (Phase 2 W5)

Adapters can now make a dynamic integration explicit without a target read,
import or inference. `core/repo_bridges.add_repo_bridge` validates:

- non-empty `source_repo` and `target_repo` aliases that differ;
- a bounded, normalized lowercase relation such as `dynamic_import`,
  `protocol`, `plugin` or `subprocess`;
- non-empty bounded `provenance`;
- at most 10 bounded evidence entries, each a string or
  `{path, line}` object;
- an optional bounded `symbol`;
- `explicit: true` (the helper does not accept an inference mode).

The helper emits a `RepoBridge` node with a stable
`repo_bridge:<source>::<target>::<relation>::<symbol>` key. It does not read a
file, resolve the symbol, import the target, execute code or touch the network.
`repo_relations` re-validates nodes read from member snapshots. A bridge with
`explicit: false`, missing provenance, unknown fields or invalid evidence is
reported under `bridges.errors` and creates no edge. Source and target must
match selected member aliases; an external, unindexed or unselected target is
listed under `bridges.unpaired` with `reason: "target_not_selected"` (or the
source equivalent), and no member is invented. Duplicate
`(source, target, relation, symbol)` bridges collapse into one edge with merged
provenance, evidence and declaring members. The section is bounded to 100
edges, 100 unpaired entries and 100 errors; `repository_relations.state`
becomes `partial` when it is partial.

Accepted bridges appear in the top-level `edges` list with
`origin: "adapter_bridge"`, `kind: "integration"` and the declared relation.
They never enter `dependencies`, cycles, `order`, `levels` or
`reverse_dependencies`; a mutual dynamic bridge therefore cannot create a
build-order cycle.

The Bigville adapter demonstrates the surface by emitting
`bigville -> ocelot dynamic_import` for
`worlds.bigville_ocelot_adapter`. It scans only Bigville's
`bigville/backends/ocelot.py` for the exact `import_module` call (and the
Bigville test file for a matching reference), records the path/line evidence,
and never reads or imports the Ocelot checkout. If that explicit declaration
is absent, no bridge node is emitted; arbitrary dynamic imports are still not
turned into edges.

## Persisted relationship supergraph (Phase 2 W6)

`ozagia.core.supergraph` persists the report from `cross_repo.relations` as a
versioned, bounded JSON artifact with schema `ozagia.supergraph.v1`. It stores
member metadata, declared/package/API/adapter-bridge edges, families, API
references and mappings, dependency order/cycles/reverse links, counts, notes
and bounded errors. It is a relationship cache, not a node union: each member's
`graph.json` remains the source for node and edge detail.

- Default path: `<anchor>/.ozagia/supergraph.json`, or the anchor's
  `OZAGIA_STATE_DIR` when set.
- `content_hash` covers the canonical graph body; `source_hashes` cover each
  selected member snapshot and the anchor federation manifest.
- `read` validates the schema, graph, hashes and bounds, then reports `fresh`,
  `stale`, `absent` or `invalid` with a rebuild command. It never repairs an
  artifact implicitly.
- `write`/`build` use a same-directory temporary file and `os.replace`; the
  artifact is limited to 8 MiB and 128 members, and no member checkout is
  written.
- `federation build`, `federation graph` and `federation graph --refresh`
  expose `build`, `write`, `read` and `refresh`; `read` does not rebuild unless
  `refresh` is requested. No MCP tool is added for the cache.

Freshness is byte-based: a git HEAD or working-tree change that does not
rewrite a snapshot or the anchor manifest does not make the artifact stale. A
stale cache rebuilds the whole relationship report; there is no per-member
incremental graph API.

## Opt-in family-aware ranking (Phase 2 W7)

`federation.search(..., family_boost=True)` adds an additive `0.15`
`rank_score` preference (capped at `1.0`) to hits from members that share a
declared family with the anchor. The lexical `score` is unchanged, non-family
hits remain in the results, and ties are deterministic. The default is
`family_boost=False`, and the same opt-in boolean is exposed as
`ozagia federation search --family-boost` and the `federation_search` MCP
`family_boost` option.

The paired fixture evaluation in `evaluation/family_retrieval/` moved four
constructed family answers from rank 2 to rank 1 while its control query was
unchanged (`docs/results/family_retrieval_2026-11-27.md`). A follow-up on real
Bigville, Ocelot and Ozagia snapshots moved four constructed near-tie family
answers from ranks 2, 2, 3 and 12 to rank 1, with the Ozagia control unchanged
(`docs/results/family_retrieval_real_2026-11-27.md`). A replay of the existing
curated Bigville retrieval tasks found most expected files outside the
federated lexical top 20 and no meaningful family gain
(`docs/results/family_retrieval_tasks_2026-11-27.md`), so that task set is not
a suitable family benchmark. These are mechanism and robustness checks, not
evidence of general retrieval benefit. The default stays off; changing it is
gated by a representative paired evaluation on real queries with no regression
and a meaningful gain.

## Limits

- Declared edges are assertions with provenance, not verified runtime calls.
- Package edges are current-manifest evidence; no lockfile, transitive or
  environment-marker resolution is added.
- API edges require explicit producer, consumer or test mappings. Matching
  operation identities, paths or names does not pair repositories, and a
  reference does not prove that a consumer calls a producer at runtime.
- An adapter bridge is an explicit assertion with source evidence, not proof
  that the target was installed, that the import succeeded, or that the
  runtime contract is compatible. Automatic dynamic-import discovery remains
  out of scope; only the adapter's known declaration is emitted.
- A relationship or bridge referring to a member omitted by explicit `--repo`
  selection is an error or unpaired entry, not a silently dropped edge.
- Cycles are strongly connected components; simple cycles are not enumerated.
- The relationship report itself is in-memory and bounded. The optional W6
  cache persists only the relationship artifact; there is no full node-union
  supergraph, incremental member cache or organization access-control model.
- Supergraph freshness is byte-based, and a stale artifact rebuilds the whole
  relationship report rather than applying per-member increments.
- Family ranking is opt-in and has fixture-level, real-snapshot near-tie and
  curated-task robustness checks. The curated-task replay did not show a family
  gain and exposed the limits of federated lexical retrieval on that task set.
  The default remains off pending a representative paired evaluation on real
  family-relevant queries.

## Verification

Verified 2026-11-27 in the batch worktree:

```sh
.venv/bin/python -m pytest -q tests/test_repo_relations.py tests/test_api_relationships.py tests/test_cross_repo.py tests/test_dependency_links.py tests/test_api_mappings.py tests/test_federation.py
# 77 passed
.venv/bin/ruff check src/ozagia/core/repo_relations.py src/ozagia/core/api_relationships.py src/ozagia/core/cross_repo.py src/ozagia/federation_cli.py tests/test_repo_relations.py tests/test_api_relationships.py
# All checks passed!
```

Edge-case coverage added or confirmed by those tests:

- mutual `uses` / `plugin_for` declarations stay two `integration` edges and
  produce no dependency pair, cycle, order change or reverse build dependency;
- `consumes_api` / `tests_api` edges stay out of `dependencies`, `order`,
  `levels` and `reverse_dependencies` even when a package edge points the other
  way;
- partially populated duplicate operations merge in either snapshot order
  without false ambiguity, and duplicate producer mappings roll into one edge
  with merged provenance;
- a rejected or orphan `ApiMapping` with no operation sets the API state to
  `partial` even when there are no references or accepted mappings;
- an explicit `--repo` / `roots` selection that omits a declared endpoint, or
  ignores a manifest id, reports the endpoint as not selected and creates no
  guessed edge.

Adapter-declared bridges (Phase 2 W5) verified 2026-11-27:

```sh
.venv/bin/python -m pytest -q tests/test_repo_relations.py tests/test_api_relationships.py tests/test_bigville_adapter.py tests/test_repo_bridges.py
# 50 passed
.venv/bin/ruff check src/ozagia/core/repo_bridges.py src/ozagia/core/repo_relations.py addins/bigville_adapter.py tests/test_repo_relations.py tests/test_bigville_adapter.py tests/test_repo_bridges.py
# All checks passed!
```

Bridge coverage:

- the helper emits one stable `RepoBridge` node with normalized relation,
  bounded provenance/evidence and `explicit: true`, and reuses the key for an
  identical repeated declaration;
- missing fields, out-of-bounds text/evidence, malformed evidence and a
  same-repository bridge are rejected by the helper; nodes with
  `explicit: false`, missing provenance or unknown fields are reported by
  `repo_relations` and create no edge;
- an external or explicitly unselected target is reported under
  `bridges.unpaired` with no fake member or edge;
- duplicate bridge nodes collapse to one edge with merged evidence and
  declaring members, bounded to 10 provenance and 10 evidence entries per
  edge;
- mutual bridges remain integration edges with no dependency pair, cycle,
  order/level change or reverse build dependency;
- the bridge appears in `federation relations` CLI JSON, and Bigville adapter
  indexing emits the `worlds.bigville_ocelot_adapter` bridge with path/line
  evidence while leaving the Ocelot checkout untouched; an unrelated dynamic
  import emits no bridge.

Persisted supergraph (Phase 2 W6) and family ranking (Phase 2 W7) results are
recorded in `docs/results/supergraph_2026-11-27.md` and
`docs/results/family_retrieval_2026-11-27.md`. The supergraph record reports 35
targeted tests passing and no Ruff findings; the family record reports four
constructed family answers moved from rank 2 to rank 1 with an unchanged
control query. The family result is a fixture-level mechanism check, not a
real-repository evaluation.

The full repository suite, docs generator check and repo-shape check remain
coordinator-owned (W4) and were not run as part of this workstream.

## Follow-on work

None of the following is implemented by this batch.

- Automatic dynamic-import discovery: bridges remain explicit adapter
  declarations with cited source evidence; parsing arbitrary import strings
  into repository edges is not implemented.
- Optional lineage evidence from declared Git remotes, validated against
  both members rather than inferred from URL similarity.
- A structured documentation API-reference source for members with no OpenAPI
  or code mapping, only when the document explicitly marks the operation; prose
  mentions are not promoted to references.
- A full node-union supergraph with cross-repository symbol search. The W6
  artifact caches relationship metadata and edges only; member nodes remain in
  each `graph.json`.
- A representative paired evaluation of family-aware ranking on real queries.
  The fixture, real-snapshot near-tie and curated-task checks verify the
  mechanism and its limits only; the default stays off until a representative
  evaluation with adequate retrieval shows no regression and a meaningful gain.
- Organization-wide manifests and an access-control model for federation
  membership. Current manifests are per anchor and do not carry organization
  policy.
