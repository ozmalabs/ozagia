# Explicit API mappings

`ozagia.core.api_mappings.index_api_mappings` adds maintainer-supplied API
producer, consumer, and test relationships to a graph. It uses only the
standard library and does not scan repositories or infer relationships. API
labels are resolved to indexed OpenAPI operations before a mapping is added;
unresolved or ambiguous labels are reported and skipped.

The version 1 JSON registry has a `mappings` array. Each entry contains
`relation` (`produces_api`, `consumes_api`, or `tests_api`), `repo`, `path`,
`symbol`, and `api`. Repo names resolve through the caller's `repo_roots`
mapping. Paths must identify existing files contained by that repository,
including after symlink resolution. The API reference has the form
`contracts:openapi.json#listPets`. Its repository, spec path, and operation ID
must match exactly one `ApiOperation` already in the builder. Index the spec
with the same repository namespace first:

```python
index_openapi_document(builder, file_node, "openapi.json", spec_text,
                       repo="contracts")
```

The contract repository must also be present in `repo_roots`, and the spec
path must resolve to a file beneath it. Every mapping records the registry
source and JSON pointer as provenance and marks its assertion as explicit.

Example:

```python
from pathlib import Path
from ozagia.core.api_mappings import index_api_mappings

index_api_mappings(builder, registry_text, "integration/api-links.json", {
    "contracts": Path("/work/contracts"),
    "service": Path("/work/service"),
    "client": Path("/work/client"),
})
```

Invalid entries are reported in `errors` and skipped. Errors distinguish
malformed references, missing or unsafe spec paths, unindexed operations, and
ambiguous operation IDs. The result's `valid` field describes only the
registry envelope; matching an operation verifies identity, not the truth of
the maintainer's producer, consumer, or test assertion.

## Repository-level rollup

`ozagia federation relations` and the `repository_relations` section of
`ozagia federation check` read the `ApiOperation`, `ApiCodeReference` and
`ApiMapping` nodes already in each member snapshot. They build one canonical
API reference per operation from the best available source:

1. an OpenAPI `operationId` with its file and `source_pointer`;
2. an OpenAPI method/path with its file;
3. an explicit `ApiCodeReference` when the member has no OpenAPI operation;
4. a bare method/path operation identity as a display fallback.

Explicit mappings are then rolled up into repository edges: consumers and
tests point at a producer for the same reference, and a producer points at a
separate contract repository with `implements_api`. A consumer or test without
a producer is reported as `unpaired`, never paired by name or path. Matching
operations across snapshots without a mapping do not create an edge.

Repeated evidence is merged by canonical reference. A partially populated
operation in one snapshot fills any missing `method`, `path` or
`source_pointer` values from another copy instead of creating a second
reference or a false ambiguity; two populated values that disagree are
reported as ambiguity. Duplicate mappings with the same relation, reference,
code location and provenance pointer are deduplicated, and several mappings
for the same reference and code pair become one repository edge that keeps all
provenance entries. API edges remain integration evidence: they are never
added to `dependencies`, `cycles`, `order`, `levels` or
`reverse_dependencies`. See
[repository relationships](federation.md#api-relationships-and-references)
for the report shape, bounds and limits.
