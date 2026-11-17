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
an optional `members` array. Each item is a relative path resolved from the
anchor checkout or an absolute directory path:

```json
{
  "version": 1,
  "members": ["../service-b", "~/work/shared-library"],
  "reference_repos": [
    {"path": "../shared-library"},
    {"path": "../frozen-api", "pin": "0123456789abcdef0123456789abcdef01234567"}
  ],
  "links": []
}
```

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
declared API relationships and code-to-infrastructure links. API identity
checks use existing snapshots; the configuration check below also reads the
linked current source files. The report preserves the source repository, relationship
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

## Identity and freshness limits

Node keys are qualified by a checkout ID because local keys such as
`file:src/main.py` and numeric graph IDs can repeat across snapshots. A
checkout ID uses the repo name recorded in the snapshot (or the checkout
directory name when absent); colliding IDs get a stable path-derived suffix. A
change in federation membership can introduce or remove a collision and change
these IDs. A qualified key identifies a node in the selected snapshot, not a permanent
identity across renames or commits. The federation status response must be
consulted when freshness matters. Search and inspection are limited to stored
snapshot data; they do not guarantee that a checkout's current working tree is
indexed. Cross-repository semantic navigation requires explicit identity and
version evidence (for example compiler-produced SCIP package identities); it
is not provided by federated lexical search.
