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
entry is reported under `links.member_errors`. Passing explicit CLI `--repo`
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
declared API relationships, code-to-infrastructure links, and declared
package dependency links. API identity checks use existing snapshots; the
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
