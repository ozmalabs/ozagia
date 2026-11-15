# The `graph` MCP surface

The MCP server has two tool surfaces:

- `full` (default): the `dev_*` tools.
- `graph`: eight verbs that name every node by its stable key
  (`ozagia.core.keys`) and never by an integer id.

Adapter MCP tools are listed and callable on both surfaces.

```
OZAGIA_SURFACE=graph python -m ozagia.integrations.mcp.server
python -m ozagia.integrations.mcp.server --surface graph   # overrides the env
```

On the `graph` surface, `tools/list` returns `find`, `node`, `expand`,
`impact`, `edit`, `create`, `check`, `note` and the adapter tools, and only
those can be called. A `dev_*` call is answered with JSON-RPC error -32602
(unknown tool). Arguments go through the server's validator, and every input
schema has `additionalProperties: false`. The eight tool descriptions,
including their parameter descriptions, come to about 400 tokens (estimated
as characters / 4). A test holds them to at most 1,500.

Implementation: `src/ozagia/integrations/mcp/graph_surface.py`.

## Keys

```
file:<path>                 a file (also a text file the graph does not index)
sym:<path>::<qualname>      a function, class or method
test:<path>::<qualname>     a test function
doc:<path>                  a Markdown document
text:<path>#L<a>-L<b>       a chunk of an unparsed text file (YAML, TOML, ...)
repo:                       the repository
<kind>:...                  adapter nodes, e.g. seed:<id>
```

Every path and key has to resolve inside the repository root. Anything else is
refused with `{ok: false, error}`. Errors that come from a bad argument
(unknown key, a path outside the root, a stale base hash) are returned as
`{ok: false, error}` and are not raised.

## Verbs

### `find(query, kinds?, limit=20)`

The hits from three ranked lists are fused by reciprocal rank (k = 60, the
constant `ozagia.core.search` uses):

1. the node whose key is the query, if there is one;
2. Symbol, Test, Doc and adapter nodes whose name, qualname, title or id is
   the query (case-sensitive, then case-insensitive) or starts with it,
   ordered by match class, then name length, then key;
3. `DevMemory.code_search` (BM25 plus PageRank). Each file hit contributes
   its matched symbols, then the file.

Hit: `{key, kind, name, file, line, signature, summary, score}`. `kind` is
the key prefix. `signature` is the definition's header line from the source.
`summary` is the first line of the docstring, description or title. Fields
with no value are left out. Ties are broken by key, so the order is the same
on every call. `kinds` filters by key prefix (`["sym", "test"]`).

### `node(key, view="outline")`

- `outline`: `kind, name, file, lines: [start, end], signature, summary`, and
  `edges`, the neighbour counts by edge type. An in-edge type has a `<`
  prefix, for example `{"defined_in": 1, "<defines": 1}`. A Python symbol's
  range starts at its first decorator.
- `source`: the outline plus `source`, the node's own verbatim text (a symbol
  body, a whole doc, a text chunk, an adapter node's snippet), and
  `truncated`. The text is capped at 2,000 lines (`MAX_SOURCE_LINES`).
- `full`: the source view plus `neighbours`, up to 12 neighbour keys per edge
  type. Only `full` returns a `file:` key's text. `source` returns a
  `source_note` for a file key instead.

The `file:` key of a text file the graph does not index (YAML, shell, ...)
resolves too. Its outline lists the file's chunk keys under `chunks`.

### `expand(keys, edges?, depth=1, limit=50)`

A breadth-first walk from `keys` along the given edge types, following both
directions. `edges` defaults to every graph edge type. The names `callers` and
`callees` follow the syntactic symbol graph, reported as
`[caller, "calls", callee]`. Neighbours are visited in key order. The walk does
not continue through `repo:`, which links to every file.

Result: `{nodes: [{key, kind, name, file, line}], edges: [[src, type, tgt]],
truncated}`. `nodes` starts with the start keys. `limit` caps the number of
nodes; `truncated` is true when the walk reached more nodes than that.
`edges` lists only edges between returned nodes.

### `impact(keys, depth=2, limit=50)`

- `symbols`: transitive callers in the symbol graph, up to `depth` hops. A
  `file:` key starts from every symbol defined in that file.
- `files`: dependents in the file reference graph (the graph behind
  `dev_impact`), plus the files of the affected symbols.
- `tests`: tests found as callers in the symbol graph, plus Test nodes with a
  `tests_module` or `tested_by_file` edge to a changed or affected file.

Each item is `{key, depth, why}`. `why` is one edge path that starts at the
item and ends at the changed key, for example
`["sym:pkg/user.py::quadruple", "calls", "sym:pkg/core.py::double"]`. Each list
is ranked by `(depth, key)` and cut at `limit`. `counts` gives the full sizes
and `truncated` says whether any list was cut.

### `edit(key_or_file, old_string, new_string, replace_all=false, apply=false, base_sha256?)`

A wrapper around `DevMemory.span_edit`. When `key_or_file` is a key with a
line range (`sym:`, `test:`, `text:`), every match has to lie inside that
range. Otherwise the edit is refused with `range` and the match keys. A key
without a line range (`file:`, `doc:`, most adapter keys) only selects the
file. A file in ozagia's state directory or with a `.git` component (any
depth, also through a symlink) is refused.

The result contains `ok, applied, file, keys, parse_check, broken_references,
broken_references_truncated, diff_view` (capped at 200 lines, with
`diff_truncated`), `new_sha256, backup, graph_delta`. A failed edit returns
`error`, plus `nearest` (no match), `matches` (ambiguous or out of range) or
`parse_error`.

### `create(content, path? | parent_key?, apply=false)`

Give exactly one of `path` and `parent_key`.

- `path`: a new file. The call is refused if the file exists (use `edit`), or
  if the path is in the state directory or `.git`. The content is
  parse-guarded (`.py`, `.json`, and `.rs`/`.lua` when the grammar is
  installed). The file is written atomically with no-clobber semantics: a temp
  file is written, then hard-linked into place.
- `parent_key`: a `file:` or `sym:` key of a Python file. `content` must be
  one or more `def`/`class` statements and is dedented first.
  - A `file:` key appends them at the end of the file, two blank lines below
    the code above.
  - A top-level function key inserts them after that function.
  - A class key appends them at the end of the class body, with the body's
    indentation.
  - A nested function key is refused.

  The whole new file must parse. The write goes through
  `ozagia.edit.txn.write_source`, with a backup.

The default is a preview. With `apply=true` the file is reingested and the
result has `keys` (the added keys) and `graph_delta`. Each applied create is
journaled with kind `create`.

### `check(keys?, tests?, timeout_s=300)`

Runs `ozagia.check.run_tests` with the target's interpreter:
`$OZAGIA_RUN_PYTHON` when set, else `<root>/.venv/bin/python` when it exists,
else the server's own interpreter (`check.default_python`). Set
`OZAGIA_RUN_PYTHON` when the target's environment is elsewhere, or the tests
run with ozagia's tool python. The selectors:

- `tests`: test keys and test-file keys (through `resolve_selectors`) and
  pytest node ids, whose path must exist inside the root.
- `keys`: a test key or a test-file key is run directly. For any other key,
  the surface runs the test files of the Test nodes that have a
  `tests_module` or `tested_by_file` edge into the key's file. These are
  sorted and capped at `MAX_CHECK_TEST_FILES = 20`; `test_files_truncated`
  reports the cut.

Result: `ok, status, seconds, counts, tests: [{key, param?, outcome, message?,
frames?}]` (frames as keys), `collection_errors, selectors, ignored`. If no
selector remains, the result is `{ok: false, error: "no tests to run"}`.

### `note(text, keys?)`

Appends a journal entry with kind `note` and the given keys. Every key has to
resolve. Returns `{ok, entry}`.

## Caches

The symbol graph and the file reference graph are built from the tag cache
once per server process, the first time `expand` (with `callers`/`callees`)
or `impact` needs them. The result of that call reports
`symbol_graph_seconds`. An applied `edit` or `create` rebuilds them on next
use. Changes made outside this surface, for example from a shell, are not
picked up until the server restarts. `find`'s name index is rebuilt when the
graph's node set changes.
