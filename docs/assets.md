# External assets

Durable reference data is a separate class from temporary space.  A dataset,
model, corpus, installed checkout, long-lived cache or tool tree may outlive a
repository, be shared by several checkouts, sit outside every checkout, and be
expensive or impossible to reproduce offline.  `ozagia assets` catalogs it.

## Temp vs assets

| | `ozagia tmp` | `ozagia assets` |
| --- | --- | --- |
| Lifetime | short, expiring | durable |
| Ownership | paths it can prove it created | explicit owner metadata |
| Location | known temp roots | anywhere outside the temp class |
| Cleanup | plan by default, `--apply` deletes owned entries | plan only in this stage; no deletion |
| Contents | never stored | never stored |

The two classes are disjoint.  An asset path may not be under
`<state>/tmp`, and `ozagia tmp` audit/clean never selects an asset.  See
[temp.md](temp.md) for the temporary-space rules.

## What is recorded

One JSON record per asset:

```json
{
  "schema": "ozagia.asset/1",
  "id": "ast_0123456789ab",
  "path": "/absolute/canonical/path",
  "kind": "dataset|model|corpus|install|cache|tool|other",
  "owner": "ozagia|ocelot|huggingface|user|upstream|external",
  "scope": "user|repo|shared|system",
  "source": {"kind": "url|huggingface|git|local|manifest|unknown",
             "url": null, "dataset": null, "repo": null,
             "revision": null, "commit": null, "manifest": null},
  "version": null,
  "revision": null,
  "sha256": null,
  "digests": [],
  "tree": {"sha256": null, "bytes": null, "file_count": null},
  "fetched_at": null,
  "bytes": 0,
  "apparent_bytes": 0,
  "inodes": 0,
  "hardlinks": 0,
  "allocation": "blocks|apparent-fallback|unknown",
  "walk": "complete|partial",
  "cross_device": false,
  "license": {"spdx": null, "status": "unknown|declared|verified",
              "source": null},
  "created_at": null,
  "verified_at": null,
  "last_used_at": null,
  "pinned": false,
  "deletable": false,
  "state": "present|missing|partial|verified|mismatch|unverified",
  "reason": "",
  "manifest_path": null,
  "marker": null
}
```

* `bytes` is allocated space (`st_blocks * 512` on POSIX); `apparent_bytes` is
  `st_size`.  A bounded walk that stops, an unreadable entry or a child on
  another filesystem is reported as `partial`, never as a complete total.
* `sha256` is optional.  `digests` holds manifest-provided `{path, sha256,
  bytes, verified, derived}` entries for a directory.  `tree` is optional
  manifest tree metadata (`sha256`, `bytes`, `file_count`); it is present only
  when a known manifest declares it and never includes per-file content.
* `license.status` stays `unknown` unless a licence was supplied explicitly.
  A filename or suffix is never evidence of a licence.
* `pinned` means "never a GC candidate".  `deletable` means the owner permits
  an explicit deletion; it does not mean automatic deletion.  `--no-delete` is
  the default; delete permission is granted explicitly with `--deletable`, and
  discovery never clears an existing pin.
* Records hold metadata only: no file contents, prompts, credentials or
  environment.

## Registry and markers

The registry is user-level:

1. `$XDG_DATA_HOME/ozagia/assets/registry/<id>.json`
2. when `XDG_DATA_HOME` is unset or not absolute,
   `~/.local/share/ozagia/assets/registry/<id>.json`
3. only when that location cannot be made writable,
   `<state>/assets/registry/<id>.json`

Directories are 0700 and files 0600.  Every record is written atomically with
a same-directory `mkstemp`, `fsync` and `os.replace`, so concurrent writers do
not need a global lock.

An optional `.ozagia-asset.json` marker inside a directory carries identity and
provenance.  It is written **only** for `owner=ozagia` and **only** when the
caller passes `--mark`/`mark=True`.  External and user-owned paths are
read-only records; no marker is ever written into them.  A marker contains no
file contents and no credentials.

## CLI

```sh
# Register an owned dataset and say it may be deleted explicitly.
ozagia assets register --path /data/wiki --kind dataset --owner ozagia \
  --source-url https://example.test/wiki --source-kind url \
  --revision r1 --license CC-BY-4.0 --deletable --reason "Wikipedia parquet set"

# Adopt an owned directory without writing a marker.
ozagia assets adopt /models/qwen --kind model --reason "local weights"

# Discover only the known Ozagia-owned roots and manifests.
ozagia assets discover
ozagia assets discover --no-research --json

# Ocelot's own data is external, pinned and never deletable by Ozagia.
ozagia assets register --path /srv/ocelot/wiki --kind corpus --owner ocelot \
  --pinned --reason "Ocelot Wikipedia parquet cache"

# Ocelot's structured Wikipedia snapshot, read-only provenance from its HF
# download metadata.  The command does not hash the parquet or write a marker.
ozagia assets register \
  --path /home/matt/ozma/data/wikipedia --kind dataset --owner ocelot \
  --scope shared --pinned --no-delete --from-hf-metadata \
  --reason "Ocelot structured Wikipedia snapshot"

# Inspect.
ozagia assets list --owner ocelot
ozagia assets list --kind model --state verified --json
ozagia assets status --top 10
ozagia assets status ast_0123456789ab

# Explicit marker discovery only (no home/tree walking).
ozagia assets scan --path /data/wiki --path /models/qwen

# Re-check existence and size, optionally reading contents for a hash.
ozagia assets verify ast_0123456789ab
ozagia assets verify --all --hash

# Keep or forget.  forget removes only the record, never the files.
ozagia assets pin ast_0123456789ab
ozagia assets unpin ast_0123456789ab
ozagia assets forget ast_0123456789ab

# Plan only.  --apply is refused in this stage.
ozagia assets gc --max-bytes 50000000000 --older-than 30

# The ownership, reference and GC policy matrix.
ozagia assets policy
ozagia assets policy --json
```

JSON output always has an `ok` field and a stable shape.  Exit status is 0 for
success or a plan, 1 for a refusal, a missing record or verification failure,
and 2 for bad arguments.  No command prints file contents.

## Ownership, pinning and deletion

`owner` is explicit metadata, not an inference from the path.  `ozagia assets
policy --json` reports this matrix without scanning anything:

| Owner | GC candidate | Deletion | Read-only from Ozagia |
| --- | --- | --- | --- |
| `ozagia` | yes, when every gate below passes | explicit approval only | no |
| `ocelot` | never | never | yes |
| `huggingface` | never | never | yes |
| `user` | never | never | yes |
| `upstream` | never | never | yes |
| `external` | never | never | yes |

A GC candidate must be all of: `owner=ozagia`, `deletable=true`,
`pinned=false`, `state` in `present` or `verified`, and **unreferenced** by
Ozagia's own state.  The complete future gate is spelled out here so a plan
never implies more than it has checked: ownership, deletable, unpinned,
verified, unreferenced, explicit approval.

* `pinned` means "never a GC candidate".  Pinning wins over every other rule;
  `unpin` only allows planning, never deletion.
* `deletable` means the owner permits an explicit deletion; it does not mean
  automatic deletion.  `--no-delete` is the default, delete permission is
  granted explicitly with `--deletable`, and discovery never clears an
  existing pin.
* A missing, partial, mismatched or unverified asset is not a candidate.
  Registering a declared `sha256` or `digests` leaves the asset `unverified`
  until `verify --hash` succeeds.  A tree whose walk stopped is `partial`.
* Ordering prefers `cache`, then `dataset`/`model`, then other kinds, oldest
  `last_used_at` first.  The plan stops at `--max-bytes`/`--max-inodes`.
* Every kept asset has a stable `reason` and a short `why` in `gc --json`:
  `owner`, `pinned`, `not-deletable`, `state`, `referenced`,
  `reference-scan`, `not-old-enough` or `budget`.  A selected candidate carries
  `reason: apply-deferred`, because this stage never deletes.
* `gc --apply` is refused in this stage.  The refusal names the future gate
  above; it is not an accidental click away from deletion.

## Reference checks

A recorded asset that Ozagia's own state still names must survive every GC plan.
`assets list`, `assets status` and `assets gc --json` add `referenced`,
`referenced_by` and `reference_scan` to each entry.  `ozagia.core.assets.
referenced_by(root, asset)` exposes the same check for one record.

The scan is bounded and metadata-only:

* It reads only known state paths under the target's state directory: `goals/`,
  `reviews/`, `reports/`, `evidence/`, `tests/`, `eval/` (top-level JSON only,
  never the cached HF or deep-review trees), `research/` and the JSON metadata
  of `tasks/`/`runs/` run directories.  A missing path is skipped.
* A match is an exact JSON string or scalar: the canonical asset path, the
  registry id, a revision, a commit, the dataset identifier, or a declared
  SHA-256 (including a tree or per-file digest).  Prose, substrings, prompts and
  report bodies are never matched, read back or printed.
* The scan never walks `$HOME` or the target repository, never follows a
  symlink, never opens an asset, never hashes content and never uses the
  network.
* Bounds cap files, bytes per file, total bytes, elapsed time, JSON depth and
  the number of scalars.  A bound that stops the check reports `partial`; the
  scan state is `partial` and the affected asset is kept.

A partial or unknown scan is a **caution, not evidence**: it is never reported
as `unreferenced`.  `gc_plan` keeps every asset whose reference check did not
complete and records `reason: reference-scan`, even when no reference was
found before the bound was hit.  The same rule applies to an unreadable record
or a symlink in a state path.


## Explicit vs discovered

`ozagia assets discover` reads only these exact known roots and manifests.  It
never varies them, never scans `$HOME` as a tree, never follows symlinks, never
reads file contents and never uses the network.  A missing root is skipped, not
an error.

| Root | Discovery rule | Recorded as |
| --- | --- | --- |
| Ocelot managed installs | `$XDG_DATA_HOME/ozagia/ocelot/*/.ozagia-ocelot.json` (fallback `~/.local/share`) | `kind=install`, `owner=ozagia`, `scope=user`, `pinned=true`, `deletable=false` |
| Ocelot sibling checkout | `<root>/../ocelot` is reported as `explicit-only`, never registered | explicit `register` only |
| HF benchmark cache | `<state>/eval/hf/<benchmark>/<dataset_sha>/meta.json` | `kind=dataset`, `owner=ozagia`, `scope=repo`, `pinned=false`, `deletable=true` |
| Deep-review corpus | `<state>/eval/deep_review_corpus/{,repositories/}*/.ozagia-deep-review-corpus.json` plus `corpus.json` | `kind=corpus`, `owner=ozagia`, `scope=repo`, `pinned=true`, `deletable=true` |
| Model metadata | `<state>/models/*.meta.json` | `kind=cache`, `owner=ozagia`, `scope=repo`, `pinned=false`, `deletable=true` |
| Research caches | `<state>/research/cache` and `<state>/research/fetch` | `kind=cache`, `owner=ozagia`, `scope=repo`, `pinned=true`, `deletable=false` |

The result is `assets`, `skipped`, `errors`, `partial`, `roots` and `limit`.
`roots` names every exact path inspected and whether it was `scanned`, `missing`,
`partial`, `explicit-only` or unavailable.  Discovery is idempotent: an existing
record is updated from its manifest, but its pin is not changed.  Unrelated
files and directories inside a known root are ignored.

The Ocelot install parser reads `.ozagia-ocelot.json` without importing Ocelot
or trusting the extracted tree.  It records the archive SHA-256, the manifest
tree SHA-256/bytes, the declared licence (the manifest's MIT declaration, or the
documented MIT constant) and up to `MAX_DIGESTS` per-file entries marked
`verified` from the manifest (not from a fresh hash).  If the manifest tree is
larger than that cap, the record keeps `sha256` and `tree` metadata but no
per-file list.  Re-discovery does not hash file contents; it may re-measure the
tree with the same bounded, symlink-free walk as any other asset.

Ocelot's Wikipedia parquet set is the motivating explicit example: Ozagia does
not see it in any of its own manifests, must not guess that every `*.parquet` is
an Ozagia dataset, and must not delete it.  Record it with `owner=ocelot`,
`pinned=true`, `deletable=false`; it then appears in `ozagia assets status` and
is never a GC candidate.

## Hugging Face download metadata

`register --from-hf-metadata` reads only
`<path>/.cache/huggingface/download/**/*.metadata` for an explicit dataset
directory.  Line 1 is the revision, line 2 is the SHA-256/etag and line 3 is a
timestamp.  The per-file digests map back to the data paths with `.metadata`
removed.  The actual data files are never opened or hashed, so a multi-gigabyte
parquet set costs only the metadata reads; every digest stays `verified=false`
until an explicit `verify --hash`.

Inconsistent revisions, escaping paths, symlink metadata and unreadable files
are refused.  `--from-hf-metadata` cannot be combined with `--sha256`, and it
never writes a marker into the external dataset.  The licence remains `unknown`
unless `--license` is supplied; a `*.parquet` suffix is never evidence of a
licence.

## MCP

The full MCP surface (not the `graph` or `engine` surfaces) lists two read-only
views:

* `dev_assets_list` filters `owner`, `kind`, `state`, `pinned` and `limit`;
* `dev_assets_status` reports totals by kind/owner/state, allocated and apparent
  bytes, inodes, pinned/deletable counts and the largest entries.

There is deliberately no MCP register, pin, verify-by-hash or delete tool.  The
CLI and the registry are the only write path.

## What remains

This stage is offline-tested against synthetic manifests.  Discovery and
reference checks perform no network access and read no file contents.
Reference checks are bounded and keep the asset on a partial scan.  `gc
--apply` is **not** implemented: `ozagia assets gc` is always a dry run and
`--apply` is refused until an explicit-approval gate covers ownership,
deletable, unpinned, verified and unreferenced state.
