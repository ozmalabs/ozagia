# Federated repository graphs

Federation queries read snapshots that already exist in several checkouts. They
do not build, refresh, or otherwise write a member snapshot. The feature is
opt-in at query time and leaves ordinary single-repository graph commands
unchanged.

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

Automatic sibling discovery can expose repository names, paths, snapshot
freshness, and indexed code metadata to the CLI caller or connected MCP
client. Use explicit `--repo` membership when that disclosure boundary matters.
The feature does not provide a cross-repository access-control system: the
calling process must already be allowed to read every selected state directory.

## Queries

```sh
ozagia federation search "shared cache" --root ~/work/service-a --limit 10
ozagia federation node 'repo-id::sym:src/cache.py::Cache.get' --root ~/work/service-a
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

The read-only tools `federation_status`, `federation_search`, and
`federation_node` use the server workspace as their anchor. Each accepts an
optional `roots` array to select explicit member roots. They are available on
both the full and graph tool surfaces. They do not refresh indexes or invoke
project adapters.

## Identity and freshness limits

Node keys are qualified by a checkout ID because local keys such as
`file:src/main.py` and numeric graph IDs can repeat across snapshots. A
checkout ID uses the repo name recorded in the snapshot (or the checkout
directory name when absent); colliding IDs get a stable path-derived suffix. A
qualified key identifies a node in the selected snapshot, not a permanent
identity across renames or commits. The federation status response must be
consulted when freshness matters. Search and inspection are limited to stored
snapshot data; they do not guarantee that a checkout's current working tree is
indexed. Cross-repository semantic navigation requires explicit identity and
version evidence (for example compiler-produced SCIP package identities); it
is not provided by federated lexical search.
