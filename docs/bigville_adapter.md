# Bigville Ozagia adapter

`addins/bigville_adapter.py` adds graph nodes for Bigville's scenario and seed
manifests, pack manifests and source-evidenced browser/API routes. It reads
both `seeds/` and `bigville/seeds/` because they are distinct source locations.
Bigville's generated seed metadata and package configuration identify
`bigville/seeds/` as the canonical packaged seed directory; root `seeds/`
remains a separate legacy/source tree. The same ID can therefore have two
path-distinct manifest nodes. ID-only scenario and dependency references link
to all candidates and record `ambiguous_id_only` rather than choosing one.
Bigville's `manifest_for()` returns only the ID, so the runtime reference does
not resolve duplicate paths. Scenario nodes list candidate paths under
`seed_manifest_candidates`; literal source mentions remain path-specific.
Literal `scenarios/<path>.json` mentions resolve by that repository-relative
path, independently of the manifest's `id` field. A unique scenario ID keeps
the historical `bigville_scenario:<id>` node key. If multiple scenario files
share an ID, their keys include the path (`bigville_scenario:<id>::<path>`)
so both records remain addressable; a key can therefore change when an ID
transitions between unique and duplicated.

The adapter links scenario declarations to seed candidates, seed dependency
declarations to manifest candidates, and pack table names to existing table
files. Rule and concept items retain their names, kind, manifest path, and
bounded textual fields such as `head`, `body`, and `definition`. Missing
references remain unresolved attributes; the adapter never
creates placeholder seed nodes. Generated manifests expose their declared
pack/table/hash provenance, but `freshness` is explicitly
`unverified_declared_generation`: the adapter does not recompute the generator
hash. Non-generated manifests carry `not_declared_generated`. Malformed JSON
is skipped and symlink targets outside the checkout are ignored. A declared
generated table with no matching pack table is surfaced in
`unresolved_generated_tables` instead of silently disappearing. Literal
source, test and documentation references retain the named seed path, which
distinguishes `seeds/x.json` from `bigville/seeds/x.json`.

Route nodes are deliberately bounded: direct server path comparisons and
literal browser requests are treated as explicit evidence. Dynamic management
routes are represented as path patterns. A client request without a matching
server check is labeled `client_only_unverified`; the adapter does not infer
that an endpoint is implemented from a browser call. No web framework, Bigville
module or simulation is imported or executed.

The adapter can be enabled for `/applications/data/projects/bigville` without
adding an Ozagia config file to that checkout. From this Ozagia checkout:

The adapter's scenario/test mentions are navigation evidence only. They do
not establish that a test exercises every behavior affected by scenario,
seed, pack, or table data, so they do not bypass the configured affected-test
fallback. Duplicate seed IDs and generated-pack provenance add further
uncertainty. JSON changes are source changes for test planning, so unindexed
JSON and mixed JSON/docs changes cannot be dropped by the docs/config-only
shortcut. A future project may opt into narrower planning only after it
declares complete data-to-test coverage in validated project configuration.
`bigville_data_test_evidence` reports scenario, seed, and pack-table paths,
observed test-file mentions, duplicate-ID ambiguity, and unverified generation
provenance. Every row is marked `advisory_only`; it is useful for review but
does not claim a complete coverage relationship. A project can add reviewed
`[[test_coverage.rules]]` entries to `.ozagia.toml` using the generic contract
documented in `docs/testing.md`. Ambiguous adapter evidence alone keeps the
fallback; only a matching, explicit complete declaration can authorize a
narrower plan.

```sh
PYTHONPATH="$(pwd)/src:$(pwd)/addins${PYTHONPATH:+:$PYTHONPATH}" \
OZAGIA_ADAPTERS=bigville_adapter:register \
OZAGIA_LANGUAGE_AUTOINSTALL=0 \
python -m ozagia.core.memory build \
  --root /applications/data/projects/bigville \
  --out /tmp/bigville-graph.json --docs none --no-git --no-language-install
```

The output path is outside Bigville, and language installation is disabled for
this invocation. The graph includes `Repo -has_scenario-> Scenario`,
`Scenario -references_seed_manifest-> BigvilleSeedManifest`,
`BigvilleSeedManifest -declares_dependency_candidate-> BigvilleSeedManifest`,
`Pack -contains_table-> PackTable`, and route evidence edges from client and
server files. When the Ocelot runtime import is present it also includes
`Repo -declares_repo_bridge-> RepoBridge`; see
[Adapter-declared dynamic bridge](#adapter-declared-dynamic-bridge).
`bigville_scenarios` lists scenario summaries,
`bigville_seed_manifests` lists path-distinct seed records (filter with
`seed_id="..."` or `path_prefix="bigville/seeds/"`), and `bigville_routes`
lists method, path, evidence grade and source files.

## Ocelot federation

Ozagia federation reads a member snapshot from that checkout's
`.ozagia/graph.json`. The current Bigville graph build above writes to `/tmp`,
so Ocelot cannot consume it through federation without copying or building a
snapshot into Bigville's `.ozagia` state directory. The adapter does not create
that state directory or an Ocelot federation manifest. Federation can use an
explicit `--repo` path without editing a manifest, once a graph exists at the
member's `.ozagia/graph.json`:

```sh
PYTHONPATH="$(pwd)/src:$(pwd)/addins${PYTHONPATH:+:$PYTHONPATH}" \
OZAGIA_ADAPTERS=bigville_adapter:register \
OZAGIA_LANGUAGE_AUTOINSTALL=0 \
.venv/bin/python -m ozagia.core.memory build \
  --root /applications/data/projects/bigville \
  --out /applications/data/projects/bigville/.ozagia/graph.json \
  --docs none --no-git --no-language-install

ozagia federation status --root /applications/data/projects/ocelot \
  --repo /applications/data/projects/bigville --no-sync
ozagia federation search "bigville scenario" \
  --root /applications/data/projects/ocelot \
  --repo /applications/data/projects/bigville --no-sync
```

The first command intentionally writes an Ozagia graph in Bigville's state
directory and is an opt-in setup; the command in the previous section remains
the read-only recipe. Keep Bigville out of `reference_repos`, which authorizes
network fetch and fast-forward attempts. The concrete Ocelot checkout currently
has no graph snapshot, so it too must be indexed before it can contribute
source nodes to federation.

Federation joins indexed snapshots; it does not infer cross-repository code
relationships. The dynamic Bigville ↔ Ocelot bridge is recorded explicitly in
the anchor's `.ozagia/federation.json` with `relationships` and `families`,
then inspected with `ozagia federation relations` (the same report appears as
`repository_relations` in `ozagia federation check`):

```json
{
  "version": 1,
  "members": [
    {"path": "../bigville", "id": "bigville"}
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

Here Ocelot is the anchor, so it is selected automatically and is not repeated
in `members`; its alias `ocelot` is derived from the checkout name. Only
Bigville is declared, with a stable id; both relationships use the derived
`ocelot` alias as the Ocelot endpoint. With the checkouts at
`/applications/data/projects/bigville` and `/applications/data/projects/ocelot`,
`../bigville` resolves from the Ocelot anchor to the Bigville checkout. The
anchor still needs its own `.ozagia/graph.json`. Inspect the declarations with:

```sh
ozagia federation relations --root /applications/data/projects/ocelot --no-sync
```

Do not add `--repo` when relying on the declared `bigville` id: explicit roots
ignore manifest ids and fall back to derived checkout aliases, which may differ
from the declared ids. A separate workspace anchor can instead declare both
members, but it must be indexed too. Each edge's evidence strings are
repository-relative to its source member and are recorded, not fetched. Both
directions above are maintainer assertions: `bigville uses ocelot` records the
backend/provider bridge, and `ocelot plugin_for bigville` records that Ocelot
supplies the adapter Bigville loads. These declared edges do not create a static
code import edge, package dependency, runtime call or deployment claim; the
adapter's separate `RepoBridge` record documents the dynamic import explicitly
below. The reverse edge is an integration relation, so it does not form a
dependency cycle or change `order`, `levels` or `reverse_dependencies`; the
family groups the repositories without adding an edge.

### Adapter-declared dynamic bridge

The adapter also records the runtime import as an explicit snapshot node. When
Bigville is indexed, `index_repo_bridges` looks for the exact
`import_module("worlds.bigville_ocelot_adapter")` call in
`bigville/backends/ocelot.py` and a matching module reference in
`tests/test_bigville_ocelot_fresh_process.py`. If the backend declaration is
present, the adapter calls `add_repo_bridge` and emits:

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

Line numbers are recorded from the files the adapter actually read; they are
illustrative here and can move. The helper reads nothing itself, and the adapter
never reads or imports the Ocelot checkout: `worlds.bigville_ocelot_adapter` is
recorded as a symbol string. If the import declaration is absent, no bridge
node is emitted. The adapter recognizes this one known integration; it does
not turn arbitrary dynamic imports into repository relations.

A federation report over selected `bigville` and `ocelot` members accepts the
bridge as a separate integration edge:

```json
"edges": [
  {"source": "bigville", "target": "ocelot", "relation": "dynamic_import",
   "kind": "integration", "origin": "adapter_bridge", "explicit": true,
   "provenance": ["Bigville's ocelot backend dynamically imports Ocelot's Bigville resident adapter"],
   "evidence": [{"path": "bigville/backends/ocelot.py", "line": 15},
                {"path": "tests/test_bigville_ocelot_fresh_process.py", "line": 62}],
   "members": ["bigville"],
   "symbol": "worlds.bigville_ocelot_adapter"}
],
"dependencies": [],
"cycles": [],
"bridges": {"state": "ok", "counts": {"nodes": 1, "edges": 1,
                                      "unpaired": 0, "errors": 0}}
```

The bridge stays out of `dependencies`, cycles, `order`, `levels` and
`reverse_dependencies`. If Ocelot is not indexed or not selected, the report
lists the target under `bridges.unpaired` with
`reason: "target_not_selected"` and creates no fake member or edge. The bridge
records evidence for a dynamic import; it does not prove that Ocelot was
installed, that the import succeeded, or that the runtime contract is
compatible.

Bigville's browser/API navigation remains bounded route evidence: the adapter
matches literal client requests and server path checks and labels a client-only
request `client_only_unverified`. It emits no OpenAPI operation or `ApiMapping`
nodes, and neither checkout currently supplies an indexed OpenAPI contract or
explicit mapping. The federation API section (`apis`) therefore stays `absent`
for this example until such evidence is indexed; route evidence alone does not
create API producer/consumer/test relationships.
