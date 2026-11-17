# Cross-repository static checks

`ozagia.core.cross_repo.check(root, roots=None)` returns a JSON-serializable,
read-only report over the selected existing federation snapshots. It uses only
explicit links in the anchor's `.ozagia/federation.json`; it does not match
names or invent relationships. `roots` follows federation selection rules.
Each result includes qualified endpoints, relation, source manifest and
provenance. Invalid declarations become unknown checks. Any non-fresh member
snapshot makes an otherwise positive check unknown; current federation
freshness is conservative and may be `unknown` when only Git HEAD was checked.

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

Unknown relations, unsupported node types, missing endpoints and unavailable
comparison attributes are unknown. This is static evidence, not provider
verification, test execution, deployment state, or a general compatibility
claim. The core performs no target imports, network calls, indexing, or writes.
