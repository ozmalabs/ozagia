# Federated repository graphs

Status: base delivery implemented, verified 2026-11-24. Bounded discovery,
explicit members, qualified identities, snapshot provenance, read-only status,
search and node/edge inspection are in `core/federation.py`,
`tests/test_federation.py`, `tests/test_federation_surface.py` and
[`docs/federation.md`](../federation.md); declared cross-repository links and
checks are in `core/cross_repo.py` and
[`docs/cross-repo-checks.md`](../cross-repo-checks.md). The follow-on work in
the last section—version-aware relationships from dependencies or SCIP,
persistent identities across membership changes, incremental member caching,
and organization manifests/access control—is not implemented.

## Objective

Make cross-repository context easy on a machine with multiple checkouts, while retaining the existing single-repository experience. A federation is optional and read-only: discover nearby indexed sibling checkouts automatically, allow explicit membership, and query their existing snapshots as a virtual supergraph. Never silently index, execute project code, or write to another checkout.

## Research and design

Repository-local keys and integer IDs collide between snapshots. Qualify each node with a checkout identity, preserve its original key and provenance, and keep each snapshot and its freshness independent. Do not infer precise cross-repository edges from matching names. Sourcegraph's [cross-repository indexing documentation](https://sourcegraph.com/docs/code-navigation/writing-an-indexer) describes the importance of symbol identity and matching versions; [SCIP](https://github.com/scip-code/scip) provides an interchange model for precise symbol information. Automatic discovery must be bounded, nonrecursive, deterministic, and limited to indexed immediate siblings; explicit roots allow other layouts. Disclose membership and stale, missing or invalid snapshots. Repositories can contain sensitive information: discovery does not grant permission to write, and results must carry their source root.

## Delivery

1. Implement a standard-library federation core for bounded discovery and explicit members, independent snapshot loading, qualified identities and read-only status, search and node/edge inspection. Preserve errors and avoid executing adapters or mutating members.
2. Expose the capability through CLI and MCP, without altering single-repo graph mutation semantics. Document discovery boundaries, configuration, freshness, collision and access limitations.
3. Add focused tests for collisions, sibling discovery, explicit members, missing/stale graphs, edge provenance and no cross-repository writes. Run targeted checks and the repository suite, review both branches, and merge the work.

## Follow-on work

Version-aware cross-repository relationships based on declared dependencies or precise SCIP identities; persistent identities across membership changes; incremental caching; organization-wide manifests and access control. The initial implementation supports manually declared provenance-bearing links, but does not infer semantic relationships from federated search.
