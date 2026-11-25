# MCP tools — graph surface

> **Generated file.** Built by `scripts/docs/generate_docs.py` from `src/ozagia/integrations/mcp/server.py`, `src/ozagia/integrations/mcp/graph_surface.py`, `src/ozagia/integrations/mcp/sre.py`, `src/ozagia/research/mcp.py` (ozagia `0.1.5.dev0`). Do not edit by hand: change the source code and regenerate.

12 tools.

> The compact graph-verb surface (`graph_surface.py`) plus the federation tools; the OpenCode capture plugin uses it.

### `check`

Run pytest on tests (test keys or pytest ids) and on the tests covering other keys. Returns status, counts, per-test {key, outcome, message, frames} with frames as keys.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `keys` | array |  |  | — |
| `tests` | array |  |  | — |
| `timeout_s` | number |  | 300 | — |
### `create`

Create a NEW file (path), or append a definition to a Python file (parent_key file:/sym:; a class key adds a method). Parse guarded. Preview unless apply=true; returns new keys and graph_delta. Use edit for existing text.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `path` | string |  |  | — |
| `parent_key` | string |  |  | — |
| `content` | string | yes |  | — |
| `apply` | boolean |  | false | — |
### `edit`

Replace exact text in a file, named by path or by a node key (the match must lie inside that node). Refused if it breaks the parse. Preview unless apply=true; returns keys, graph_delta, broken_references, diff_view, new_sha256.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `key_or_file` | string | yes |  | — |
| `old_string` | string | yes |  | Unique unless replace_all. |
| `new_string` | string | yes |  | — |
| `replace_all` | boolean |  | false | — |
| `apply` | boolean |  | false | — |
| `base_sha256` | string |  |  | — |
### `expand`

Breadth-first walk from keys along edge types (default all graph edges, both directions; "callers"/"callees" follow the call graph). Returns {nodes, edges: [[src, type, tgt]], truncated}.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `keys` | array | yes |  | — |
| `edges` | array |  |  | — |
| `depth` | integer |  | 1 | — |
| `limit` | integer |  | 50 | — |
### `federation_check`

Read-only static checks for declared cross-repository API and code-to-infrastructure relationships. Designated references may be fetched and fast-forwarded over Git; set no_sync to opt out. Indexes are not refreshed; missing evidence remains unknown.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `roots` | array |  |  | Optional explicit member roots; an empty array selects only the anchor. |
| `no_sync` | boolean |  | false | — |
### `federation_node`

Read-only inspection of a qualified federated node and its snapshot-local edges. Use the qualified key returned by federation search. Designated references may be fetched and fast-forwarded; set no_sync to opt out. Node inspection does not infer links.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `qualified_key` | string | yes |  | — |
| `roots` | array |  |  | Optional explicit member roots. |
| `no_sync` | boolean |  | false | — |
### `federation_search`

Read-only lexical search over discovered or explicitly selected repository snapshots. Designated references may be fetched and fast-forwarded over Git; set no_sync to opt out. Results retain provenance; no index is refreshed.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `query` | string | yes |  | — |
| `roots` | array |  |  | Optional explicit member roots. |
| `limit` | integer |  | 20 | — |
| `no_sync` | boolean |  | false | — |
### `federation_status`

Read-only status of indexed repositories in the bounded sibling federation, or in explicitly supplied roots. Reports membership and snapshot freshness. Only paths explicitly listed in the anchor manifest's reference_repos may be fetched and fast-forwarded; this uses the network and changes those clean checkouts. Set no_sync to true to opt out. Indexes are not refreshed.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `roots` | array |  |  | Optional explicit member roots. |
| `no_sync` | boolean |  | false | — |
### `find`

Search the repo graph by name and code text. Returns ranked hits {key, kind, name, file, line, signature, summary, score}. Keys name nodes in every other verb: file:<path>, sym:<path>::<qualname>, test:<path>::<qualname>, doc:<path>, text:<path>#L<a>-L<b>.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `query` | string | yes |  | — |
| `kinds` | array |  |  | Key kinds to keep, e.g. ["sym","test"]. |
| `limit` | integer |  | 20 | — |
### `impact`

What a change to these keys can affect: transitive callers, dependent files, and the tests covering them, each with `why` (one edge path back to the changed key). Each list is ranked by depth and cut at limit; counts gives the totals.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `keys` | array | yes |  | — |
| `depth` | integer |  | 2 | — |
| `limit` | integer |  | 50 | — |
### `node`

One node by key. outline: kind, name, file, lines, signature, summary, edge counts. source: plus its verbatim text (symbol body, doc, text chunk). full: plus neighbour keys; a file: key gives the file text only with full.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `key` | string | yes |  | — |
| `view` | string |  | "outline" | — |
### `note`

Record a note naming keys in the dev journal.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `text` | string | yes |  | — |
| `keys` | array |  |  | — |
