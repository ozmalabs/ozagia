# Federated repository graphs

Federation queries read snapshots that already exist in several checkouts.
They never build or refresh a member snapshot. By default they may fetch and
fast-forward clean checkouts explicitly designated as references in the anchor
manifest; ordinary members remain read-only. Use `--no-sync` to prevent those
updates for a query.

## Discovery and membership

Without `--repo`, the federation core checks only the anchor repository and
its immediate sibling checkouts for an existing Ozagia graph snapshot. It does
not recursively walk parent directories or descend into sibling directories.
Discovery is deterministic and limited to indexed repositories. Use repeated
`--repo` options to select other roots explicitly:

```sh
ozagia federation status --root ~/work/service-a
ozagia federation status --root ~/work/service-a \
  --repo ~/work/service-b --repo ~/libs/shared
```

The anchor selects the default discovery neighborhood. Explicit member roots
replace sibling discovery; the anchor remains included. Status reports
which roots were considered, which snapshots were usable, and freshness or
loading problems. An absent or invalid graph is reported; the command does not
repair it.

The anchor's `.ozagia/federation.json` may also declare linked checkouts with
an optional `members` array. Each item is either a relative or absolute
directory string (the original form) or an object with `path` and an optional
stable `id`:

```json
{
  "version": 1,
  "members": [
    "../service-b",
    {"path": "~/work/shared-library", "id": "shared"}
  ],
  "reference_repos": [
    {"path": "../shared-library"},
    {"path": "../frozen-api", "pin": "0123456789abcdef0123456789abcdef01234567"}
  ],
  "links": []
}
```

A declared `id` is the member's federation identity and the prefix of every
qualified key (`shared::sym:...`). It must be 1–64 characters, start with a
lowercase letter or digit, and use only `[a-z0-9._-]`. `anchor` and `self` are
reserved. Ids must be unique within a manifest and must not collide with the
anchor or another member's identity. A bad id is reported under
`links.member_errors`; the checkout stays listed with a derived identity and an
`identity_error` instead of being dropped or silently assigned a duplicate.
Because the manifest stores both path and id, moving a checkout is a two-field
update and the id keeps the same qualified prefix. Entries without an `id`,
automatic sibling discovery, reference repositories, and explicit CLI `--repo`
or MCP `roots` selections keep the deterministic derived alias described under
identity limits. Explicit roots ignore manifest ids.

When roots are discovered automatically, indexed immediate siblings and these
declared members are combined. Declared paths remain visible in status when a
directory or snapshot is missing or invalid. A malformed `members` value or
entry is reported under `links.member_errors`. The anchor is always included;
naming the anchor itself in `members` is reported as a member error rather
than creating a second member. Passing explicit CLI `--repo`
options or MCP `roots` selects only those roots plus the anchor, overriding
both discovery sources. A member declaration only selects an existing
snapshot; it never builds or refreshes one. Declaring a path can disclose its
location and snapshot metadata to the caller, so only add checkouts that are
appropriate for the connected client to inspect.

`members` only selects checkouts. It never authorizes an update. `reference_repos`
is a separate, opt-in list; each object has a relative or absolute `path` and
an optional full 40 or 64 character commit SHA `pin`. Unpinned references are
updated automatically when federation commands and MCP federation tools run.
Only a clean, attached checkout whose upstream is `origin/<current-branch>` is
eligible. Ozagia fetches that branch and fast-forwards only when the remote is
a descendant of local `HEAD`. Dirty, detached, missing-upstream, diverged, or
failed checkouts are left untouched and report an error. Pins prevent updates;
a checkout not at its pin reports `pin_mismatch`. No reset, stash, rebase,
project command, index refresh, or project import is run.

Automatic synchronization can access the network and change explicitly listed
checkouts. It has a 60 second per-Git-command timeout, disables Git hooks,
credential helpers, terminal prompts and the `ext` protocol, and caches results
for 30 seconds to avoid duplicate fetches during repeated tool calls. At most
six references are considered per automatic query. Use `--no-sync` on status,
search, node or check to opt out. Explicitly run the sync command to update
references and inspect its JSON report:

```sh
ozagia federation sync --root ~/work/service-a
ozagia federation sync --root ~/work/service-a --repo ~/work/shared-library
```

After a reference update its existing graph snapshot is stale until rebuilt
separately. Query reports expose synchronization state; they never imply that
the graph was refreshed.

Automatic sibling discovery can expose repository names, paths, snapshot
freshness, and indexed code metadata to the CLI caller or connected MCP
client. Use explicit `--repo` membership when that disclosure boundary matters.
The feature does not provide a cross-repository access-control system: the
calling process must already be allowed to read every selected state directory.
Sibling symlinks are not followed. The anchor honors `OZAGIA_STATE_DIR`;
other members use their own `<root>/.ozagia/graph.json`, so an absolute state
directory override is not mistakenly reused for every checkout. An explicit
empty MCP `roots` array selects only the anchor.

## Queries

```sh
ozagia federation search "shared cache" --root ~/work/service-a --limit 10
ozagia federation node 'repo-id::sym:src/cache.py::Cache.get' --root ~/work/service-a
ozagia federation check --root ~/work/service-a --repo ~/work/service-b
```

Search results and node inspection retain the source repository and its
qualified node identity. Snapshot-local edges remain attached to their source
snapshot. Federation does not infer semantic edges by matching symbol names;
the presence of several repositories in one query does not establish that one
calls, imports, or depends on another. A federation rooted at `<root>` may
declare links in `<root>/.ozagia/federation.json`:

```json
{
  "version": 1,
  "links": [
    {
      "source": "service-a::sym:src/api.py::fetch",
      "target": "shared::sym:src/client.py::Client.get",
      "relation": "uses",
      "provenance": "reviewed dependency map"
    }
  ]
}
```

Each endpoint is a qualified key returned by a federation query, and each
entry supplies a relation and provenance string. The version must be `1`; all
four link fields must be non-empty strings, and both endpoints must resolve
among the current members. These are explicit declarations; they do not alter
a repository snapshot. Status reports manifest state and validation errors.
Node inspection includes matching declarations under
`cross_repository_edges`, with their provenance and manifest path. Invalid or
unresolved declarations are reported rather than silently turned into
name-based matches.

The CLI prints JSON results. `--json` is accepted for scripting clarity and
prints the same complete JSON representation.

## MCP tools

The tools `federation_status`, `federation_search`, `federation_node`, and
`federation_check` use the server workspace as their anchor. Each accepts an
optional `roots` array to select explicit member roots and a `no_sync` boolean
to opt out of updating designated references. They are available on both the
full and graph tool surfaces. They do not refresh indexes or invoke project
adapters. Automatic updates may access the network and change the clean,
explicitly designated Git checkouts as described above.

`federation check` and `federation_check` run offline static checks for
declared API relationships, code-to-infrastructure links, declared package
dependency links, and repository-level relationships. API identity checks use existing snapshots; the
configuration check below also reads the linked current source files. The
report preserves the source repository, relationship
provenance, and missing or ambiguous evidence. A declaration alone does not
verify schema compatibility, runtime behavior, or deployed infrastructure;
unknown or unverifiable results remain unknown. Selected roots and declared
links can disclose repository metadata to the caller, so use explicit
membership where needed.

## Static code-to-Kubernetes configuration observations

`ozagia federation check` and `federation_check` include the read-only static
configuration report under `configuration` for explicitly declared
`configured_by` federation links. The same report is available from
`ozagia.core.cross_config.check(root, roots=None)`. It
accepts a link from a Python `File` node to a Kubernetes `InfraResource`, reads
the current Python source and referenced JSON manifest (or YAML when the
optional safe YAML parser is installed), then compares literal
`os.environ["NAME"]` lookups with the names in a single workload container's
`env` list. Optional `os.getenv` and `os.environ.get` lookups are not required
variables. The report retains qualified endpoints, provenance, repository
freshness states and source paths; it never returns environment values.

Dynamic lookups, parse/read errors, missing or ambiguous resources,
`envFrom`, multiple containers, unsafe paths and unavailable snapshots produce
unknown results. A missing name is reported as `observed_missing` from current
source, not as a deployment failure. Even matching names are only
`observed_match`: snapshot freshness is reported separately, and an unknown or
stale snapshot keeps the overall result unknown. This check does not establish
that the source is deployed, that optional environment access is covered, or
that runtime behavior is correct. It does not execute code, load projects,
refresh graphs or write to member repositories.

## Declared dependency links

`ozagia federation check` and `federation_check` also include a bounded
`dependency_links` section. The same report is available from
`ozagia.core.dependency_links.check(root, roots=None)`; `analyze(members,
member_states)` accepts already-selected federation records. It reads each
member's current declared manifests from the checkout root, matches package
names explicitly, and compares a consumer's requirement with a provider's own
declared package version. It does not infer links from symbol names, repository
names, search results, or fuzzy matching.

Supported root-level declarations are:

| Ecosystem | Manifest | Provider identity |
| --- | --- | --- |
| Python | `pyproject.toml` (PEP 621 project, optional dependencies, dependency groups, Poetry), `setup.cfg` (`metadata`, `options.install_requires`, extras), root `requirements*.txt` | `[project].name`/`[tool.poetry].name`, or `[metadata].name` |
| Rust | `Cargo.toml` (`dependencies`, `dev-dependencies`, `build-dependencies`, target and workspace dependencies) | `[package].name` and version, including `workspace.package` inheritance |
| Node | `package.json` (`dependencies`, `devDependencies`, `optionalDependencies`, `peerDependencies`) | root `name` and `version` |
| Go | `go.mod` `require` entries | `module` path; a main-module version is normally unavailable |

Other formats are reported as skipped, not guessed: executable `setup.py`,
`Pipfile`, Composer, Gemfile, Maven/Gradle, Dart and Elixir manifests. Lockfiles
are listed as skipped because declared-requirement comparison does not resolve
transitive versions. Workspace member packages outside the member root are not
enumerated. Files are read with a 1 MiB-per-file cap, at most 16 manifests per
member, 250 dependencies per manifest and 500 links in the report.

Matching is per ecosystem. Python names use PEP 503 normalization; Node and
Rust names are compared case-insensitively; Go module paths are exact. A member
never links to itself. Only direct dependencies are examined; transitive
dependencies are not followed.

The version comparison is conservative and standard-library only:

- Python supports `==`, `!=`, `===`, `~=`, `>=`, `>`, `<=`, `<`, comma-separated
  clauses, and trailing `.*` wildcards. PEP 508 environment markers and direct
  references are retained but not evaluated.
- Node and Rust support exact versions, `^`, `~`, comparators, and `x`/`*`
  wildcards. Rust's bare requirement is caret, as Cargo defines it; Node's bare
  requirement is exact or a component wildcard. `||`, hyphen ranges,
  `workspace:`, `file:`, `link:`, `git`, `npm:` aliases and URL forms are
  unresolved.
- Go `require` is treated as a minimum version, but a provider's version is
  usually absent from `go.mod`, so most Go links are `unknown`.
- Only numeric release components are compared. Pre-release, post-release,
  development and local version suffixes, epochs, non-numeric versions, and
  unparseable requirements produce `unknown`, never a guessed match.

Each link reports `status` as one of:

- `satisfied`: exactly one other member provides the package and the provided
  version satisfies the declared requirement.
- `mismatch`: exactly one other member provides it and the version does not
  satisfy the requirement.
- `unknown`: the provider version, requirement, marker or version syntax cannot
  be compared, or the provider version is unavailable.
- `ambiguous`: more than one member provides the same package name in the same
  ecosystem, or one member declares incompatible provided versions. Provider
  fields are left unset and candidate providers are listed; no candidate is
  chosen.
- `unresolved`: no other selected member provides the package.

A link includes `consumer`, `provider`, `package`, `ecosystem`, `requirement`,
`provided_version`, `source_manifest`, `source_line`, `provider_manifest`,
`provider_line`, and `provenance` (`declared_dependency`). The section also
contains `members` with each member's freshness and parsed/skipped manifests,
`counts`, `ambiguities`, and a `state` of `ok`, `partial` (parse errors or
truncation) or `absent` (no supported manifests).

Dependency links are current-manifest evidence, not snapshot evidence. A stale
or unknown graph snapshot remains visible in `member_states` and `members`, but
does not by itself change a link's version verdict. The dependency report itself
performs no writes, network access, package-manager invocation, target import,
project execution or index refresh. `ozagia federation check` still follows the
reference synchronization rules described above, so use `--no-sync` when no
checkout update is allowed.

```json
"dependency_links": {
  "state": "ok",
  "links": [
    {
      "consumer": "service-a",
      "provider": "shared-lib",
      "package": "shared-lib",
      "ecosystem": "python",
      "requirement": ">=1.2,<2",
      "provided_version": "1.5.0",
      "source_manifest": "pyproject.toml",
      "source_line": 7,
      "provider_manifest": "pyproject.toml",
      "provider_line": 3,
      "provenance": "declared_dependency",
      "status": "satisfied",
      "reason": "requirement_satisfied"
    }
  ],
  "counts": {"satisfied": 1, "mismatch": 0, "unknown": 0,
             "ambiguous": 0, "unresolved": 0}
}
```

## Repository relationships

Federation membership is a flat set of checkouts. Repository relationships
add a read-only, repository-level graph on top of it, without changing any
member snapshot. `ozagia federation relations` returns the relationship report
directly. `ozagia federation check` and the `federation_check` MCP tool include
the same relationship sections as `repository_relations`, with one difference:
in that embedded copy, member `state` values may be reference sync states
(`sync_error`, `sync_pending`, `index_stale`, `sync_unknown`) rather than
freshness-only states. `federation relations`,
`ozagia.core.cross_repo.relations()` and
`ozagia.core.repo_relations.check()` report federation freshness states only.
It combines four kinds of evidence:

- optional `relationships` and `families` declarations in the anchor's
  `.ozagia/federation.json`;
- directed dependency edges rolled up from the declared package links
  described above;
- explicit `ApiMapping` nodes already in member snapshots (see
  [API relationships and references](#api-relationships-and-references));
- explicit `RepoBridge` nodes emitted by adapters for a dynamic integration
  they can cite in their own source (see
  [adapter-declared dynamic bridges](#adapter-declared-dynamic-bridges)).

```json
{
  "version": 1,
  "members": [
    {"path": "../ocelot", "id": "ocelot"}
  ],
  "relationships": [
    {
      "source": "bigville",
      "target": "ocelot",
      "relation": "uses",
      "provenance": "Bigville's ocelot backend loads Ocelot's Bigville resident provider",
      "evidence": ["bigville/backends/ocelot.py",
                   "tests/test_bigville_ocelot_fresh_process.py"]
    },
    {
      "source": "ocelot",
      "target": "bigville",
      "relation": "plugin_for",
      "provenance": "Ocelot supplies the Bigville world adapter that backend loads",
      "evidence": ["worlds/bigville_ocelot_adapter.py",
                   "docs/features/bigville.md"]
    }
  ],
  "families": [
    {
      "id": "simulation-stack",
      "label": "Bigville/Ocelot simulation stack",
      "members": ["bigville", "ocelot"],
      "provenance": "reviewed integration map; declared bridge, not a build dependency"
    }
  ],
  "links": []
}
```

This is a worked Bigville ↔ Ocelot example, not an inference. The anchor
`bigville` is included automatically under its derived alias; the manifest
declares only `ocelot` with an explicit id. Naming the anchor in `members`
would be a member error. With that manifest,
`ozagia federation relations --root ~/work/bigville --no-sync`
reports two declared `integration` edges and a family, while the dependency
view stays neutral:

```json
{
  "edges": [
    {"source": "bigville", "target": "ocelot", "relation": "uses",
     "kind": "integration", "origin": "declared",
     "provenance": "Bigville's ocelot backend loads Ocelot's Bigville resident provider"},
    {"source": "ocelot", "target": "bigville", "relation": "plugin_for",
     "kind": "integration", "origin": "declared",
     "provenance": "Ocelot supplies the Bigville world adapter that backend loads"}
  ],
  "dependencies": [],
  "cycles": [],
  "order": ["bigville", "ocelot"],
  "levels": {"bigville": 0, "ocelot": 0},
  "reverse_dependencies": {"bigville": [], "ocelot": []},
  "families": [{"id": "simulation-stack",
                "members": ["bigville", "ocelot"]}]
}
```

(The excerpt omits unchanged report fields; real edges also carry `evidence`.)
Both directions are separate maintainer assertions with provenance; neither
direction creates the other. They are not inferred from Bigville's dynamic
import of Ocelot, from module or repository names, from Git remotes, or from
running either project. Evidence strings are repository-relative to their
source member, recorded and never fetched.
`uses` and `plugin_for` are integration kinds, so the reverse edge does not
become a dependency cycle and neither edge affects `order`, `levels` or
`reverse_dependencies`; only `depends_on`, `dev_depends_on` and
package-manifest edges do. The family entry groups the members without adding
an edge.

Relationship endpoints are selected member ids, not qualified node keys: use
the declared `id` when the member has one, otherwise its derived alias. Both
endpoints must resolve among the selected members; an explicit `--repo`
selection that omits one endpoint is reported as an error rather than a
guessed edge. Explicit `--repo` selection also ignores manifest ids, so use
normal manifest membership when a declaration names a declared id; otherwise
the name must match the derived alias. `source` and `target` must differ,
duplicate `(source, target, relation)` entries are rejected, and unknown
fields are errors. `provenance` is required and `evidence` is an optional
bounded list of strings that is never fetched or read.

`relation` is a bounded string. The report classifies known values
as `dependency` (`depends_on`, `dev_depends_on`), `lineage` (`forked_from`,
`upstream_of`, `sibling_of`), `integration` (`uses`, `integrates_with`,
`plugin_for`), `grouping` (`part_of`) or `documentation` (`documents`,
`documented_by`). Other values are preserved as `opaque`. Only dependency
edges participate in cycle, topological-order and reverse-dependency
analysis; lineage and descriptive edges remain visible but do not imply a
build order.

`families` are named groups, not edges. A family has a bounded `id` in the
same lowercase `[a-z0-9._-]` form as member ids, an optional `label`, one or
more selected member ids, and a required `provenance`. A member may appear in more than one family. Family membership
is reported on each member and as a top-level `families` list. Invalid family
entries are reported and omitted; valid entries are kept.

### Family-aware retrieval

Federated search can prefer hits from members that share a declared family
with the anchor, but only when the caller opts in:

```python
result = federation.search(root, "shared cache", family_boost=True)
```

`family_boost` defaults to `False`, and the default ordering is exactly the
lexical baseline. The flag is exposed on the library search API, the federation
CLI (`ozagia federation search QUERY --family-boost`) and the `federation_search`
MCP tool; every surface keeps it off unless the caller sets it. When enabled,
the anchor is the first selected member; hits from a member in one of the
anchor's families gain an additive `0.15` rank boost, capped at `1.0`. The
original lexical `score` is retained and `rank_score` carries the boosted value.
Non-family hits remain visible and are only re-ranked, never filtered.
Rank-score ties prefer family matches, then the baseline member-id/key order, so
repeated searches are deterministic.

Each hit reports `family_match` and its declared `families`. The result carries
`family_boost` and a `families` section with `state` (`disabled`, `absent`,
`ok`, `partial` or `invalid`), `by_member`, the declared records and normalized
`errors`. Missing or invalid declarations do not fail the search; they leave
the ranking unchanged and are reported.

The paired fixture evaluation in `evaluation/family_retrieval/` moves four
constructed family answers from rank 2 to rank 1 while a control query is
unchanged (`docs/results/family_retrieval_2026-11-27.md`). A follow-up check
uses real Bigville, Ocelot and Ozagia snapshots but still constructs near-tie
queries; it also moves all four family answers to rank 1 with an unchanged
control (`docs/results/family_retrieval_real_2026-11-27.md`). A replay of the
curated Bigville retrieval tasks found most expected files outside the
federated lexical top 20 and no meaningful family gain
(`docs/results/family_retrieval_tasks_2026-11-27.md`); that task set is not a
suitable family benchmark. These are mechanism and robustness checks, not
evidence of general retrieval benefit or the rate at which real queries are
near-ties. The default therefore stays off. Changing it is gated by a
representative paired evaluation on real queries with no regression and a
meaningful gain; that evaluation has not been run.

### API relationships and references

When member snapshots contain OpenAPI operations or explicit `ApiMapping`
nodes, the report adds an `apis` section with canonical API references and
repository-level API edges. A reference is built from the best available
source in each snapshot, in this order:

1. `openapi_operation`: `repo:file#operationId`, plus the OpenAPI
   `source_pointer` when the indexer recorded one. This is the strongest
   identity because it is the contract's own operation identifier.
2. `openapi_path`: `repo:file#METHOD path` when `operationId` is absent.
3. `code_reference`: an explicit `ApiCodeReference` (`repo:path#symbol`) when
   the member has no OpenAPI operation to use as a better source.
4. `operation_identity`: `member::METHOD path` as a display-only fallback when
   no file or operation identifier is available.

Each reference reports its `kind`, `authority` (`contract`, `contract_path`,
`code_mapping` or `operation_identity`), `source_rank`, optional
`source_pointer`, `alternates`, and the selected members that reported it.
References are ordered by source rank and never fetched, resolved over the
network or read from a working tree.

Repository API edges come only from explicit `ApiMapping` nodes already in the
snapshots (normally produced by the explicit registry described in
[API mappings](api-mappings.md)):

- `produces_api` marks a repository as a producer of that reference;
- `consumes_api` creates `consumer → producer` edges when a producer mapping
  exists for the same reference;
- `tests_api` creates `tester → producer` edges;
- a producer also gets `producer → contract` `implements_api` when the
  reference is an OpenAPI operation in another selected member.

API edges are emitted only between selected member identities. A mapping whose
explicit code `repo` namespace is not a selected alias remains visible in
`apis.mappings` but creates no repository edge; it is never retargeted by a
name match.

A consumer or test mapping with no producer mapping is listed under
`unpaired`; no edge is invented. Matching `operationId`, method or path across
snapshots without an explicit mapping never creates a repository edge.
Mappings must be explicitly `inference: false`; inferred or unmarked mappings,
mappings with an unresolved operation reference and mappings without a code
location are rejected and reported. Repeated operation evidence from several
snapshots merges into one reference: missing method/path/pointer fields are
filled from another copy, while conflicting populated fields are reported as
ambiguity. Duplicate mappings for the same reference and code location
collapse into one repository edge that keeps all provenance entries.

```json
"apis": {
  "state": "ok",
  "source_order": ["openapi_operation", "openapi_path",
                   "code_reference", "operation_identity"],
  "references": [
    {"id": "contracts:openapi.json#listPets", "kind": "openapi_operation",
     "authority": "contract", "repo": "contracts", "file": "openapi.json",
     "operation_id": "listPets", "method": "GET", "path": "/pets",
     "source_pointer": "#/paths/~1pets/get",
     "members": ["contracts", "service", "client"]}
  ],
  "edges": [
    {"source": "client", "target": "service", "relation": "consumes_api",
     "kind": "integration", "origin": "api_mapping",
     "reference": "contracts:openapi.json#listPets",
     "reference_kind": "openapi_operation", "authority": "contract",
     "provenance": [{"source": "registry.json", "pointer": "#/mappings/1"}]}
  ],
  "unpaired": [],
  "counts": {"references": 1, "mappings": 3, "edges": 3,
             "unpaired": 0, "errors": 0}
}
```

The API section is bounded to 2,000 references, 1,000 mappings, 1,000
repository edges, 200 unpaired mappings and 100 errors. API edges are
integration evidence and are not added to the package dependency view used for
cycles, levels or reverse build dependencies. The `federation check` API
`overall` verdict is unchanged; `repository_relations.state` becomes
`partial` when the API report has errors or truncation. API identity is
evidence from the snapshot, not proof that a consumer calls a producer at
runtime or that the contract is compatible.

### Adapter-declared dynamic bridges

Some integrations are visible only as a dynamic import, protocol lookup,
plugin entry or subprocess call in one member's source. Ozagia does not infer a
repository relationship from that code. Instead, an adapter that already reads
its own checkout may declare the bridge explicitly through
`ozagia.core.repo_bridges.add_repo_bridge`, which validates and emits one
snapshot node. The helper performs no filesystem read, module resolution,
target import, execution or network access; the adapter owns discovery and
cites repository-relative evidence.

A bridge node has this shape:

```json
{
  "type": "RepoBridge",
  "key": "repo_bridge:bigville::ocelot::dynamic_import::worlds.bigville_ocelot_adapter",
  "attrs": {
    "source_repo": "bigville",
    "target_repo": "ocelot",
    "relation": "dynamic_import",
    "provenance": "Bigville's ocelot backend dynamically imports Ocelot's Bigville resident adapter",
    "evidence": [
      {"path": "bigville/backends/ocelot.py", "line": 15},
      {"path": "tests/test_bigville_ocelot_fresh_process.py", "line": 62}
    ],
    "symbol": "worlds.bigville_ocelot_adapter",
    "explicit": true
  }
}
```

`source_repo`, `target_repo`, `relation` and `provenance` are required.
`relation` is normalized to lowercase and must start with a letter, using only
`[a-z0-9_]`; it is typically
`dynamic_import`, `protocol`, `plugin` or `subprocess`; it is a bounded
description, not a closed build-relationship enum. `evidence` is optional and
may contain strings or `{path, line}` objects. `symbol` is optional. The
helper always writes `explicit: true`; a node with `explicit: false`, missing
provenance, unknown attributes or out-of-bounds text is rejected and reported,
never converted into an edge.

`repo_relations` scans member snapshots and adds accepted bridges to the
top-level `edges` list with `origin: "adapter_bridge"` and
`kind: "integration"`. Both aliases must resolve among the selected members.
A bridge to an external, unindexed or explicitly unselected repository is
listed under `bridges.unpaired` with `reason: "target_not_selected"` (or the
source equivalent); it creates no edge and no placeholder member. Duplicate
nodes for the same `(source, target, relation, symbol)` collapse into one edge
with merged provenance, evidence and declaring members. The section is
bounded and reports its own state:

```json
"bridges": {
  "state": "ok",
  "edges": [
    {"source": "bigville", "target": "ocelot",
     "relation": "dynamic_import", "kind": "integration",
     "origin": "adapter_bridge", "explicit": true,
     "provenance": ["Bigville's ocelot backend dynamically imports Ocelot's Bigville resident adapter"],
     "evidence": [{"path": "bigville/backends/ocelot.py", "line": 15},
                  {"path": "tests/test_bigville_ocelot_fresh_process.py", "line": 62}],
     "members": ["bigville"],
     "symbol": "worlds.bigville_ocelot_adapter"}
  ],
  "unpaired": [],
  "errors": [],
  "truncated": false,
  "counts": {"nodes": 1, "edges": 1, "unpaired": 0, "errors": 0}
}
```

A bridge is integration evidence only. It stays out of `dependencies`, cycle
detection, `order`, `levels` and `reverse_dependencies`, so a mutual dynamic
bridge cannot create a build-order cycle. It does not prove that the target
was installed, that the import succeeded, or that the two repositories are
compatible. The Bigville adapter demonstrates the pattern by declaring
`bigville -> ocelot dynamic_import` for
`worlds.bigville_ocelot_adapter`, citing its own
`bigville/backends/ocelot.py`; it never imports Ocelot or reads the Ocelot
checkout. The separately declared `relationships` example above remains a
maintainer assertion about the integration, not a second copy inferred from
this bridge.

The bridge section is limited to 100 edges, 100 unpaired entries and 100
errors. Each bridge node has at most 10 evidence entries, 500 characters of
provenance/text and an 80-character relation; a deduplicated edge keeps at
most 10 provenance and 10 evidence entries. Entries are sorted
lexicographically before truncation, so the retained ten do not depend on
declaration order; truncation sets `bridges.truncated`, adds a message to
`bridges.notes`, and mirrors it in the report's top-level `notes` and
`truncated`. `repository_relations.state`
becomes `partial` when the bridge section is partial. Use
`--no-sync` for a cache-only query.

### Package-derived edges

Each resolved row in `dependency_links` is collapsed to one directed edge per
consumer/provider pair with `origin: "package_manifest"` and
`provenance: "declared_dependency"`. The edge keeps the package rows,
requirements, provided versions, source/provider manifests, optional/group
flags and an aggregate `status` (`mismatch` wins over `unknown`, which wins
over `satisfied`). Unresolved and ambiguous package rows do not create a
repository edge; they remain visible in `dependency_links`.

Declared, package-derived, explicit API mapping and accepted adapter bridge
edges are all kept in `edges`. The `dependencies` list merges only dependency
edges by source/target pair and records the contributing `origins` and
`relations`; API mapping and adapter bridge edges are integration evidence and
stay out of the package dependency view. Only that dependency view is used
for:

- `cycles`: groups of members in a dependency cycle, reported as strongly
  connected components rather than enumerating every simple cycle;
- `order` and `levels`: a deterministic topological order and longest-path
  levels when there is no cycle;
- `reverse_dependencies`: direct dependents of each selected member.

The report never infers a relationship from repository names, symbol names,
federated search hits, dynamic imports, Git remotes or lockfiles. A dynamic
import becomes an edge only through an explicit adapter `RepoBridge`
declaration with source evidence, as described above. Package edges come only
from the current declared manifests described under
[declared dependency links](#declared-dependency-links); transitive resolution
and package-manager execution do not occur. Declarations and package edges are
current-checkout evidence, not proof of a runtime call, deployment or
compatibility. The report performs no writes and no network access beyond the
optional reference synchronization described above; use `--no-sync` to keep a
query cache-only.

```sh
ozagia federation relations --root ~/work/bigville --no-sync
ozagia federation check --root ~/work/bigville --no-sync
```

The section is bounded: at most 100 declared relationships, 32 families, 64
members per family, 10 evidence strings per relationship, 20 package rows per
edge, 64 reverse-dependency entries per member and 100 adapter bridge edges.
Truncation is reported under `truncated`/`notes` or in the corresponding
subsection.

### Persisted relationship supergraph

`ozagia federation build` writes the relationship report from
`cross_repo.relations` as a bounded `ozagia.supergraph.v1` JSON artifact. The
default path is the anchor's `supergraph.json` state path
(`<anchor>/.ozagia/supergraph.json`, honoring `OZAGIA_STATE_DIR`); `--out`
overrides it. The artifact stores member metadata, declared/package/API/
adapter-bridge edges, families, API references and mappings, dependency
order/cycles/reverse links, counts, notes and bounded errors. It does not store
a full member node union; each member's `graph.json` remains the source for
node and edge detail.

`ozagia federation graph` reads an existing artifact without rebuilding.
`--refresh` rebuilds when the artifact is `absent`, `stale`, `invalid` or for a
different explicit `--repo` selection. `--no-sync` suppresses reference
fast-forward for both commands.

`ozagia federation relations --write` builds and writes the artifact, then
returns the relationship graph with a `persisted` metadata section.
`relations --cached` reads a fresh artifact instead of querying members live;
it exits non-zero for an absent, stale or invalid artifact and includes the
rebuild command. `--out` selects the artifact path for both.

Freshness is source-hash based: each selected member's snapshot bytes and the
anchor's `federation.json` bytes are hashed into the artifact, and `read`
recomputes them. Results are `fresh`, `stale`, `absent` or `invalid` and
include a shell-quoted rebuild command; `graph` never repairs an artifact
implicitly. A changed manifest marks every member contribution stale. Because
freshness is byte-based, a git HEAD or working-tree change that does not
rewrite a snapshot or the manifest does not make the artifact stale.

An explicit `--out` is refused inside a selected member checkout outside that
member's `.ozagia/` directory, and it refuses a member's `graph.json` or
`federation.json` target. Other `.ozagia` artifact paths are allowed. The
artifact is bounded to 8 MiB and 128 members. See
[the supergraph result](results/supergraph_2026-11-27.md) for recorded
verification and limits.

## Identity and freshness limits

Node keys are qualified by a member identity because local keys such as
`file:src/main.py` and numeric graph IDs can repeat across snapshots. Each
status member reports `identity_source`: `"declared"` when the id came from
the anchor manifest, `"derived"` otherwise. A declared id is stable across
path moves and should be preferred for stored links. Without one, the derived
alias uses the repo name recorded in the snapshot (or the checkout directory
name when absent); colliding derived aliases get a stable path-derived suffix.
A change in federation membership can introduce or remove a collision and
change a derived alias. Declared ids do not change silently: an invalid,
reserved, duplicate, anchor-colliding or member-colliding id is a member error,
and the affected member falls back to its derived alias with `identity_error`.
A qualified key identifies a node in the selected snapshot, not a permanent
identity across commits or graph rebuilds. The federation status response must
be consulted when freshness matters. Search and inspection are limited to
stored snapshot data; they do not guarantee that a checkout's current working
tree is indexed. Cross-repository semantic navigation requires explicit
identity and version evidence (for example compiler-produced SCIP package
identities); it is not provided by federated lexical search.
