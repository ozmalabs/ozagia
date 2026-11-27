# Federated repository graphs

Status: base delivery implemented, verified 2026-11-24; first version-aware
dependency-link slice added 2026-11-25; stable declared member ids added
2026-11-25; repository-level relationship, family and contract-first API
reference reports added 2026-11-27 with targeted test verification (see
[`2026-11-27_repo_relationships.md`](2026-11-27_repo_relationships.md)).
Bounded discovery, explicit members, qualified identities,
snapshot provenance, read-only status, search and node/edge inspection are in
`core/federation.py`, `tests/test_federation.py`,
`tests/test_federation_surface.py` and
[`docs/federation.md`](../federation.md); declared cross-repository links and
checks are in `core/cross_repo.py` and
[`docs/cross-repo-checks.md`](../cross-repo-checks.md). Declared package
dependencies are matched across members by `core/dependency_links.py` and
reported under `dependency_links`, with fixture tests in
`tests/test_dependency_links.py`. Manifest members may carry a bounded `id`
that becomes the qualified-key prefix for status, search, node, links, check
and dependency links; unannotated members and explicit roots keep derived
identities. Invalid, reserved, duplicate and colliding ids are member errors,
and a declared id survives a checkout path move. The remaining follow-on work
in the last section—precise SCIP identities, incremental member caching, and
organization manifests/access control—is not implemented.

## Objective

Make cross-repository context easy on a machine with multiple checkouts, while retaining the existing single-repository experience. A federation is optional and read-only: discover nearby indexed sibling checkouts automatically, allow explicit membership, and query their existing snapshots as a virtual supergraph. Never silently index, execute project code, or write to another checkout.

## Research and design

Repository-local keys and integer IDs collide between snapshots. Qualify each node with a checkout identity, preserve its original key and provenance, and keep each snapshot and its freshness independent. Do not infer precise cross-repository edges from matching names. Sourcegraph's [cross-repository indexing documentation](https://sourcegraph.com/docs/code-navigation/writing-an-indexer) describes the importance of symbol identity and matching versions; [SCIP](https://github.com/scip-code/scip) provides an interchange model for precise symbol information. Automatic discovery must be bounded, nonrecursive, deterministic, and limited to indexed immediate siblings; explicit roots allow other layouts. Disclose membership and stale, missing or invalid snapshots. Repositories can contain sensitive information: discovery does not grant permission to write, and results must carry their source root.

## Delivery

1. Implement a standard-library federation core for bounded discovery and explicit members, independent snapshot loading, qualified identities and read-only status, search and node/edge inspection. Preserve errors and avoid executing adapters or mutating members.
2. Expose the capability through CLI and MCP, without altering single-repo graph mutation semantics. Document discovery boundaries, configuration, freshness, collision and access limitations.
3. Add focused tests for collisions, sibling discovery, explicit members, missing/stale graphs, edge provenance and no cross-repository writes. Run targeted checks and the repository suite, review both branches, and merge the work.

## Follow-on work

Version-aware cross-repository relationships are partly delivered: declared
Python, Rust, Node and Go dependencies are matched by package name and compared
against a provider's declared version, and resolved package rows are rolled up
into repository-level edges with declared family/lineage/integration
relationships. Declared stable member ids are also delivered: a manifest member
may declare a bounded identity that keys qualified names and stored links,
independent of its current checkout path. Precise SCIP identities,
manifest/lock resolution beyond direct declarations, dynamic-bridge discovery,
incremental member caching and organization-wide manifests/access control
remain. The
implementation supports manually declared provenance-bearing links and declared
dependency links, but does not infer semantic relationships from federated
search.
