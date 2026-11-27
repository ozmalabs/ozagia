# Cross-repository static checks

`ozagia.core.cross_repo.check(root, roots=None)` returns a JSON-serializable
report over the selected existing federation snapshots. It uses only
explicit links and family/relationship declarations in the anchor's
`.ozagia/federation.json` plus declared package manifests; it does not match
names or invent relationships. `roots` follows federation selection rules.
Each result includes qualified endpoints, relation, source manifest and
provenance. Endpoints use a member's declared stable id when the anchor
manifest supplies one, otherwise the derived alias. Invalid, reserved or
duplicate ids are reported in `member_errors`, and links that use an
unresolved identity become unknown. The report also includes each member's
state and `member_errors`, even when there are no declared links. Invalid
declarations become unknown checks. Any non-fresh member snapshot makes a
comparison's `result` unknown;
`evaluated_result` retains the static comparison and `comparison_scope` says
which attributes were compared. Current federation freshness is conservative
and may be `unknown` when only Git HEAD was checked. `overall` is `no_checks`
when there is no evidence and all selected members are usable, `incomplete`
when a member is missing or invalid, `unknown` when member freshness/evidence
is unverifiable, `fail` when a verified comparison mismatches, and `complete`
when all available checks are verified with no mismatch. These labels describe
static evidence only.

Declared reference members include their pin and cached sync status. A failed
or pending sync, an unverified sync, or an updated checkout whose graph still
needs reindexing makes its checks unknown and the overall report unknown (or
incomplete when its snapshot is unavailable). Federation checks request
updates by default; use `--no-sync` or the no-sync context to inspect the
stored state without updating references.

Supported declarations are:

- `produces_api`, `consumes_api`, `tests_api`: source must be `Symbol`, `File`,
  or `ApiCodeReference`; target must be `ApiOperation`. A valid explicit link
  reports `declared`, meaning coverage was asserted, not verified.
- `same_api_operation`: both endpoints must be `ApiOperation`. It compares
  their explicit `method` and `path` attributes, reporting pass, mismatch, or
  unknown when attributes are absent. It does not compare schemas, auth,
  parameters, responses, or runtime behavior.
- `deployed_by`: source must be a code node (`Symbol`, `File`, or
  `ApiCodeReference`) and target an `InfraResource`. It compares `service`
  attributes only when both snapshots provide them. Missing service identity is
  unknown. A match does not prove the service is deployed or healthy.
- `configured_by`: source must be a Python `File` and target a Kubernetes
  `InfraResource`. The `configuration` report reads both current source files
  and compares literal required environment reads with declared container
  environment names. See [federation](federation.md) for uncertainty and
  freshness limits. A missing name is a source observation, not a verified
  deployment failure.

Unknown relations, unsupported node types, missing endpoints and unavailable
comparison attributes are unknown. This is static evidence, not provider
verification, test execution, deployment state, or a general compatibility
claim. The check itself performs no target imports or indexing. Federation
selection may fetch and fast-forward explicitly designated `reference_repos`;
use CLI `--no-sync` to suppress that behavior. See
[federation membership and reference synchronization](federation.md#discovery-and-membership).

## Declared dependency links

`cross_repo.check` also includes `dependency_links`, built by
`ozagia.core.dependency_links` from each selected member's root manifests. It
matches only explicit package declarations within one ecosystem and compares a
consumer's declared requirement with a provider's declared version. Supported
manifests, version semantics, status values and limits are documented under
[declared dependency links](federation.md#declared-dependency-links). This is
separate from the API/configuration checks: a dependency result does not make
the API `overall` verdict pass or fail, and snapshot freshness is reported per
member without changing a manifest-derived version verdict. No lockfile
resolution, package manager, network call or member write occurs.

## Repository relationships

`cross_repo.check` also includes `repository_relations`, a repository-level
view built from optional `relationships`/`families` declarations in the anchor
manifest, resolved package rows above, and explicit `ApiMapping` nodes in the
member snapshots. It reports declared family/lineage/integration edges,
package-derived consumer → provider edges, contract-first API references,
producer/consumer/test API edges, dependency cycles, a topological order,
levels and reverse dependencies.
`ozagia federation relations` and
`ozagia.core.repo_relations.check(root, roots=None)` expose the relationship
report without the API/configuration checks. `cross_repo.check` embeds the
same relationship sections as `repository_relations`, but its
`repository_relations.members[].state` values can be reference sync states
(`sync_error`, `sync_pending`, `index_stale`, `sync_unknown`) in addition to
freshness states; the standalone report uses federation freshness states only.
Only declared `depends_on` /
`dev_depends_on` and package-manifest edges feed `dependencies`, `cycles`,
`order`, `levels` and `reverse_dependencies`. Integration declarations such as
`uses` and `plugin_for`, and every `produces_api` / `consumes_api` / `tests_api`
mapping edge, remain descriptive evidence and never create a build cycle or
reorder members. Like `dependency_links`, this section is
static evidence: it does not change the API `overall` verdict and does not
infer edges from names, dynamic imports, Git remotes, search results or
transitive package resolution. See
[repository relationships](federation.md#repository-relationships) for the
manifest shape and limits.
