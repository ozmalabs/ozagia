# Persisted relationship supergraph (W6)

Status: implemented 2026-11-27. Member source files are read-only; the only
write is the requested artifact, and an explicit artifact inside a selected
member is confined to that member's `.ozagia/` directory.

## What it is

`ozagia.core.supergraph` persists the report from `cross_repo.relations` as a
versioned, bounded JSON artifact. It is the **relationship supergraph**, not a
node union: member metadata, declared/package/API/adapter-bridge edges,
families, API references and mappings, dependency order/cycles/reverse links,
counts, notes and bounded errors are stored. Every member's own `graph.json`
remains the source of node and edge detail.

- Schema: `ozagia.supergraph.v1`.
- Default path: `state_path(root, "supergraph.json")`, i.e.
  `<root>/.ozagia/supergraph.json`, or the anchor's `OZAGIA_STATE_DIR` when set.
- Top-level keys: `schema`, `root`, `generated_at`, `content_hash`,
  `source_hashes`, `graph`, `counts`, `artifact`.
- `content_hash` is the SHA-256 of the canonical JSON graph body. It does not
  include `generated_at`, source hashes or artifact metadata, so an unchanged
  relationship graph keeps the same hash.
- `artifact` records the path, byte count, member count, 8 MiB limit and the
  federation selection (`manifest` or explicit `roots`).

## Freshness

`source_hashes` maps each member id to the SHA-256 of:

- that member's snapshot bytes (anchor: its configured state `graph.json`;
  other members: `<root>/.ozagia/graph.json`);
- the anchor's `federation.json` manifest bytes.

`read` validates the JSON, schema, required keys, graph members, source-hash
shape and content hash, then recomputes the source hashes. It reports:

| state | meaning |
| --- | --- |
| `fresh` | Artifact validates and every source hash still matches. |
| `stale` | A snapshot, the anchor manifest or the artifact root changed, or a member source is unreadable; `changed` lists member ids and `changed_sources` names the fields. |
| `absent` | No artifact at the requested path. |
| `invalid` | Unreadable or oversized artifact, malformed JSON, wrong schema/keys, malformed members, content-hash mismatch. |

Every result includes a shell-quoted `rebuild` command. `read` never repairs an
artifact. A manifest change marks every member contribution stale because the
manifest is part of each contribution's source.

## Writes and bounds

`write` builds the graph, then writes a same-directory temporary file, flushes,
`fsync`s and calls `os.replace`. A failed replace leaves an existing artifact
byte-for-byte intact and removes the temporary file. Parent directories are
created only for the requested output path. The anchor state directory is the
default.

Explicit `--out` paths are resolved relative to the current directory and
checked after symlink resolution. A path inside a selected member checkout is
allowed only when it is a file under that member's `.ozagia/` directory. It is
always rejected when it names the member's `graph.json` or the anchor's
`federation.json` (including the anchor's configured state paths outside the
checkout). Rejection happens before directory creation or any write, so source
files remain byte-identical. The default anchor state path, including an
explicit `--out` naming it, stays allowed.

Bounds are `8 MiB` per artifact and `128` members; larger results raise
`SupergraphError` instead of writing a truncated graph. Strings in error text
are capped at 500 characters. Snapshot hashing streams in 1 MiB chunks, and
`read` rejects an artifact larger than the cap before parsing it.

## Commands

```sh
# Build and atomically persist the default artifact.
ozagia federation build --root /path/to/anchor

# Build a specific artifact, optionally with explicit members.
ozagia federation build --root /path/to/anchor --repo /path/to/member \
  --out /tmp/supergraph.json

# Read without rebuilding. Absent/stale/invalid results include the rebuild command.
ozagia federation graph --root /path/to/anchor

# Read, rebuilding only when the artifact is absent, stale, invalid or for a
# different explicit selection.
ozagia federation graph --root /path/to/anchor --refresh
```

All commands accept `--json`; `--no-sync` suppresses reference fast-forward for
`build`/`graph` like the other non-sync queries. `graph` without `--refresh`
never calls `cross_repo.relations`.

Python API: `build(root, roots=None)`, `write(root, roots=None, out=None)`,
`read(root, out=None)` and `refresh(root, roots=None, out=None)`.
`refresh` returns `cache: "hit"`/`"miss"` and `rebuilt: true|false`.

## Verification

```sh
.venv/bin/python -m pytest -q tests/test_supergraph.py \
  tests/test_federation.py tests/test_federation_surface.py
.venv/bin/ruff check src/ozagia/core/supergraph.py \
  src/ozagia/federation_cli.py tests/test_supergraph.py
```

Result: 37 targeted tests pass; Ruff reports no findings.

## Limits and follow-ons

- Full member node/edge union and cross-repository symbol search are out of
  scope. Use the existing federation `search`/`node` queries for graph detail.
- Only the anchor federation manifest is hashed. Per-member `federation.json`
  files are not relationship inputs in the current federation model.
- Freshness is byte-based: a git HEAD or working-tree change that does not
  rewrite `graph.json` or the anchor manifest does not make the artifact stale.
- A stale cache rebuilds the whole relationship report. Source hashes identify
  which contributions changed, but `cross_repo.relations` has no per-member
  incremental API, so no partial graph is synthesized.
- Explicit-output protection is name-based for relationship sources: every
  selected member's `.ozagia/graph.json` and `.ozagia/federation.json`, plus
  the anchor's configured snapshot and manifest, are never overwritten. Other
  files under a member's `.ozagia/` directory are allowed artifact targets.
- `read` does not infer a new membership list; a manifest change makes the
  artifact stale. `refresh` also compares the explicit `--repo` selection
  recorded in artifact metadata.
- No MCP tool is added; the existing `federation_check` surface and the
  `cli:ozagia.federation` capability still cover the relationship queries.
- The artifact is a cache: deleting it only costs a rebuild.
