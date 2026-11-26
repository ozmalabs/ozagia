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
             "url": null, "dataset": null, "revision": null,
             "commit": null, "manifest": null},
  "version": null,
  "revision": null,
  "sha256": null,
  "digests": [],
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
  bytes, verified, derived}` entries for a directory.
* `license.status` stays `unknown` unless a licence was supplied explicitly.
  A filename or suffix is never evidence of a licence.
* `pinned` means "never a GC candidate".  `deletable` means the owner permits
  an explicit deletion; it does not mean automatic deletion.
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

# Ocelot's own data is external, pinned and never deletable by Ozagia.
ozagia assets register --path /srv/ocelot/wiki --kind corpus --owner ocelot \
  --pinned --reason "Ocelot Wikipedia parquet cache"

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
```

JSON output always has an `ok` field and a stable shape.  Exit status is 0 for
success or a plan, 1 for a refusal, a missing record or verification failure,
and 2 for bad arguments.  No command prints file contents.

## Ownership, pinning and deletion

* `owner` is explicit metadata, not an inference from the path.
* Ocelot, Hugging Face, user, upstream and external assets are never GC
  candidates.  They can still be registered, listed, verified and pinned.
* A GC candidate must be all of: `owner=ozagia`, `deletable=true`,
  `pinned=false`, and `state` in `present` or `verified`.
* A missing, partial, mismatched or unverified asset is not a candidate.
  Registering a declared `sha256` or `digests` leaves the asset `unverified`
  until `verify --hash` succeeds.
* Ordering prefers `cache`, then `dataset`/`model`, then other kinds, oldest
  `last_used_at` first.  The plan stops at `--max-bytes`/`--max-inodes`.
* `gc --apply` is refused in Stage B.  This stage never deletes files.

## Explicit vs discovered

Stage B only catalogs what the caller names:

* `register`/`adopt` with an explicit path;
* `scan --path P` inspects that same directory for `.ozagia-asset.json` (or
  reads a marker file directly).  It does not recurse and never scans `$HOME`
  or an arbitrary tree.

Ocelot's Wikipedia parquet set is the motivating example: Ozagia does not see
it in any of its own manifests, must not guess that it is an Ozagia dataset,
and must not delete it.  It can be recorded explicitly as
`owner=ocelot`, `pinned=true`, `deletable=false` and reported by
`ozagia assets status`.

## What Stage C adds

* Known-root discovery and parsers: Ocelot install manifests, the HF benchmark
  cache, the deep-review corpus, model metadata, research/eval caches and
  installer assets.
* Ocelot data only through an explicit `--path <ocelot>` scan or an
  Ocelot-provided data manifest, never by guessing that every `*.parquet` is a
  reference dataset.
* HF/corpus/model metadata with revision, licence and hashes.
* Read-only MCP tools (`dev_assets_list`, `dev_assets_status`); no write or
  delete over MCP.
* A small `ozagia status`/`doctor` summary that neither walks huge roots nor
  hashes by default.
* Policy and verification refinements for referenced assets and a considered
  `gc --apply` path.
