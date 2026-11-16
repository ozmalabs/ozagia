# Graph views, apertures and static HTML export

A **view** is a deterministic projection of an existing graph. It is not an
index, an analysis or a truth claim: it selects and lays out graph material so
a person or another UI host can inspect it. The view adds no support,
consensus, confidence, freshness or provenance that the graph does not
record.

The first delivery has four layers:

```text
graph bundle -> aperture -> view model (JSON) -> renderer (HTML)
```

- `ozagia.core.apertures` defines and validates apertures.
- `ozagia.core.view` projects a graph into the versioned view model.
- `ozagia.view_html` renders that model as one self-contained HTML file.
- `ozagia.view_cli` is the read-only command.

No model is called, no network request is made, and the CLI never builds or
refreshes the index.

## Usage

Build or refresh the dev-memory snapshot first (separately):

```bash
python -m ozagia.core.memory build
```

Then emit JSON or HTML:

```bash
python -m ozagia.view_cli --aperture architecture --format json --root .
python -m ozagia.view_cli --aperture architecture --format html --out view.html
python -m ozagia.view_cli --aperture evidence --format html \
    --graph .ozagia/research/research-20261116.json --out evidence.html
```

The default graph is `<root>/.ozagia/graph.json`, or the state directory named
by `$OZAGIA_STATE_DIR`. `--graph PATH` selects any graph bundle. The default
root is the Git top level of the current directory.

```text
python -m ozagia.view_cli [--aperture ID] [--format json|html]
    [--root ROOT] [--graph PATH] [--out FILE]
    [--depth N] [--node NODE_ID] [--list-apertures]
```

- `--aperture` defaults to `architecture`.
- `--format` defaults to `json`. HTML goes to stdout unless `--out` is given.
- `--depth` and `--node` override the aperture scope for one run.
- `--list-apertures` prints the built-ins plus repository overrides.
- Exit status is `0` on success and `2` for a missing/invalid graph, an
  unknown aperture or a usage error.
- A missing graph is an error, not an empty view. Build the index first or
  pass `--graph`.

The command is also available as `ozagia-view` when the supervisor adds the
entry point to `pyproject.toml`.

## Input shapes

The view model understands both graph shapes without a flag.

**Dev-memory snapshot** (`version`, integer node ids):

```json
{
  "version": 2,
  "nodes": [
    {"id": 1, "type": "File", "key": "file:src/app.py",
     "attrs": {"name": "src/app.py", "path": "src/app.py"}}
  ],
  "edges": [
    {"src": 1, "type": "imports", "tgt": 2, "attrs": {}}
  ]
}
```

**Research graph bundle** (`schema_version`, string ids, top-level fields):

```json
{
  "schema_version": 6,
  "investigation_id": "research-20261116-000000-abcd1234",
  "events": [],
  "nodes": [
    {"id": "claim-0001", "type": "Claim", "wording": "X causes Y",
     "status": "open", "created_at": "2026-11-02T00:00:00Z"}
  ],
  "edges": [
    {"id": "edge-000002", "type": "supports", "src": "evidence-0001",
     "tgt": "claim-0001", "provenance": {"source": "review:plan-1"}}
  ]
}
```

A wrapped `{"bundle": ...}` object is accepted too. Node and edge fields that
are not in the view schema (`attrs` on a dev node, or any top-level research
field) are kept under `attrs`, so the inspector can show them and the view does
not discard authored data.

## Aperture schema, version 1

An aperture is data, not code. Every field is optional except `id` and
`title`; `validate_aperture` fills defaults and returns a normalised copy.
Unknown keys are errors, and every error names its field.

```toml
schema = 1
id = "architecture"
title = "Architecture"
description = "Modules, files, symbols and imports."
grain = "file"                 # module, file, symbol, claim, finding, day, ...

[resolves]                     # what this view can distinguish
nodes = ["Repo", "File", "Symbol", "Test", "Doc", "Commit", "Failure"]
edges = ["contains", "defines", "imports", "has_test", "changes"]
attrs = ["name", "path", "language", "kind"]

[scope]
root = "file:src/app.py"       # optional node id; null means the whole graph
depth = 2                      # hops from root; absent with a root means 1
node_types = []                # further optional filters
edge_types = []

[collapse]
unresolved = "other"           # only "other" is accepted
# Fold a resolved type into a category, or into the value of one of its attrs.
groups = { Commit = "history", Failure = "failure" }
by_attr = { Symbol = "file" }  # missing attr -> "other"
labels = { history = "History", failure = "Failures" }

[render]
layout = "layered"             # force | layered | timeline | matrix | table
seed = 7
encoding = { color = "group", size = "weight", label = "label" }

[limits]
max_nodes = 400
max_edges = 1200
truncate = "degree"            # degree | id
```

Rules:

- `resolves.nodes` and `resolves.edges` are the types the view distinguishes.
  A node or edge whose type is not listed becomes `"other"`; it is **not**
  dropped. `raw_type` keeps the original type.
- `resolves.attrs` is a hint for renderers and hosts. `attrs` in the view model
  keeps every recorded attribute so inspection does not lose data.
- `grain` names the intended unit of the view. It is descriptive metadata in
  this release; collapse rules do the concrete grouping.
- `scope.root` is a node id, not a repository path. With a root and no depth,
  the immediate neighbourhood (depth 1) is shown. A depth of 0 is the root
  alone. `node_types` and `edge_types` are optional filters.
- `collapse.groups` maps a resolved type to a literal group id.
  `collapse.by_attr` maps a resolved type to one of its attributes; an empty or
  missing value collapses into `"other"`. The default group is the lower-case
  type name. `collapsed` in `groups` counts nodes folded by a rule.
- `limits.max_nodes` and `limits.max_edges` are hard caps. With
  `truncate = "degree"`, the most connected scoped nodes are kept (ties by id);
  with `"id"`, ids are kept in sorted order. Every dropped node or edge is
  counted in `omitted`, with a reason.
- Unresolved types always collapse to the constant `"other"`; the schema
  rejects any other `collapse.unresolved` value because an unknown type must
  not become a wildcard.

The normalised aperture is available from `builtin_apertures()`,
`repo_apertures(root)`, `available_apertures(root)` and
`get_aperture(id, root=...)`.

## Built-ins

| Aperture | Resolves | Grain | Layout | Purpose |
| --- | --- | --- | --- | --- |
| `architecture` | Repo, files, symbols, tests, docs, commits, failures; contains/defines/imports and related edges | file | layered | Modules, files and imports. |
| `impact` | Changed files and symbols, callers, tests, failures | symbol | layered | What a change reaches. |
| `evidence` | Claims, evidence, observations, sources, holdings, references, reviews | claim | matrix | Claim/evidence/source structure. |
| `consensus` | Review plans, runs, findings, reconciliation, claims, arguments | finding | force | Agreement and dissent stay separate. |
| `timeline` | Dev history and research records, with recorded dates | day | timeline | Sessions, commits, edits, tests and fetches in date order. |

The caps are 250-500 nodes and 900-1500 edges depending on the aperture; see
`builtin_apertures()` for the exact values. The built-in `table` layout is
available to repository apertures and hosts even though no built-in aperture
selects it in this release.

## Repository apertures

Add or override an aperture in the versioned repository config directory:

```text
ozagia/apertures/*.toml
```

A file's `id` wins; without one, the filename stem is the id. An override of a
built-in inherits any field it does not name (tables merge recursively; lists
and scalars replace). A new id needs its own `title`; omitted fields take the
defaults (`resolves` empty, `render.layout = "force"`, the standard limits). A
malformed file reports its path and the offending field. The loader reads data
only and never imports the repository.

## View model, version 1

```json
{
  "schema": 1,
  "aperture": "architecture",
  "snapshot": {"root": "/repo", "head": "abc123", "freshness": null,
               "generated_at": "2026-11-16T10:00:00"},
  "nodes": [
    {"id": "1", "type": "File", "raw_type": "File",
     "label": "src/app.py", "group": "file", "attrs": {"path": "src/app.py"},
     "weight": 0.75, "key": "file:src/app.py"}
  ],
  "edges": [
    {"id": "e:1:imports:2", "src": "1", "tgt": "2", "type": "imports",
     "raw_type": "imports", "attrs": {}}
  ],
  "groups": [{"id": "file", "label": "Files", "count": 2, "collapsed": 0}],
  "legend": {"color": {"file": "#2f7d6d"},
             "encoding": {"color": "group", "size": "weight",
                          "label": "label"}},
  "layout": {"kind": "layered", "seed": 7},
  "provenance": {"sources": [".ozagia/graph.json"], "derived": true,
                 "graph": "dev-memory", "graph_schema": 2},
  "omitted": {"nodes": 0, "edges": 0, "reason": "none", "by": {}}
}
```

- `snapshot` reports what the graph records. `root` is the Repo node's `root`
  when present, otherwise the caller's root. `head` and `freshness` are copied
  only when the snapshot records them; the view never runs git and never
  guesses "fresh". `generated_at` is the snapshot's generation time, when
  recorded.
- `nodes` and `edges` are sorted by id. Ids are strings in the view even for a
  dev snapshot, which uses integer ids internally. `key` keeps the graph's
  stable node key.
- `weight` is a visual hint. It uses an authored numeric `weight` attr when one
  exists, otherwise normalised degree within the visible view. It is not a
  confidence or truth score.
- `groups.collapsed` counts nodes folded into that group by a collapse rule.
  `groups.count` counts all visible nodes in the group.
- `legend.color` is assigned by sorted group id from a fixed palette, so the
  same view has the same colours.
- `layout.seed` is fixed by the aperture. JavaScript is not needed for the
  SVG; the seed makes the Python force layout and any future host repeatable.
- `provenance` records the graph kind, its schema version, and any source paths
  the caller or graph supplied. `derived` is true because a view is derived.
  No evidence provenance is invented.
- `omitted.by` is an additive breakdown (`scope_nodes`, `scope_edges`,
  `node_cap`, `edge_endpoints`, `edge_cap`, `dangling_edges`). `reason` is a
  short summary such as `"node cap; edge cap"` or `"scope filter"`.

The same graph and aperture produce byte-identical JSON: sorted node, edge and
group order; sorted keys; no timestamps added by the view. `render_json` uses
sorted keys and fixed separators. Shuffling the input node and edge lists does
not change the output.

## Renderers

`render_html(view)` writes one document with inline CSS, inline SVG and a small
vanilla script. It has no external stylesheet, font, image, script or network
request. Node and edge labels, group labels, titles and provenance are escaped.
The embedded view JSON is escaped for a script element, and the inspector uses
`textContent`, never `innerHTML`.

| Layout | Rendering |
| --- | --- |
| `force` | Deterministic seeded force-directed SVG. |
| `layered` | Longest-path layers; cycles are placed in a final layer; an edgeless graph falls back to a grid. |
| `timeline` | Date columns from recorded `date`, `created_at`, `updated_at` and similar fields; rows by group. Undated nodes go to an `unknown` column. |
| `matrix` | Adjacency matrix with row/column labels and edge cells. |
| `table` | Accessible node and edge tables. |

The page always shows the aperture, layout and seed, the legend, the snapshot
and provenance, omitted counts and the reason. Clicking a node (or a table row)
opens the node inspector with its id, type, raw type, label, group, key, weight
and recorded attributes. The SVG is complete before JavaScript runs, so the
graph can be inspected with JavaScript disabled; only selection needs it.

## Limits and honesty

- Hard caps apply after scope filtering. Node caps keep the most connected
  scoped nodes by default; edge caps keep the sorted edge order. The view model
  always reports omitted nodes and edges and why.
- Dangling edges (an endpoint missing from the node list) are not silently
  dropped; they are counted under `dangling_edges`.
- A view does not establish that a claim is supported, that reviewers reached
  consensus, that an index is fresh, or that a source is trustworthy. It
  reports the recorded graph and the aperture's projection, and no more.
- The CLI is read-only. It does not build the graph, edit the repository, call
  a model or fetch anything.
