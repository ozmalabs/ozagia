# External assets and reference data

Status: active plan (2026-11-26). Separate from the temporary-space plan:
external assets are **durable**, may be shared across repositories, may live
outside every checkout, and must never be treated as scratch.

## Why this is a separate class

`ozagia tmp` owns short-lived directories it can prove it created. External
assets are different:

- a Wikipedia parquet set used by Ocelot is not temporary and is not part of
  the target tree; Ocelot may own or download it independently;
- model weights, tokenizers and dataset snapshots can be tens of gigabytes;
- the same asset may be shared by several repositories;
- the licence, revision and checksum matter as much as the bytes;
- deleting the wrong one is expensive or impossible to reproduce offline.

Some components already keep their own manifests:

| Component | Current location | Provenance today |
| --- | --- | --- |
| Ocelot install | `$XDG_DATA_HOME/ozagia/ocelot/<version>` | archive SHA-256, tree SHA-256/bytes, licence, manifest |
| HF benchmark cache | `<state>/eval/hf/<benchmark>/<dataset_sha>` | dataset revision, licence, rows/meta hashes |
| Deep-review corpus | `<state>/eval/deep_review_corpus` | pinned commit, tree SHA-256, archive SHA-256 |
| Model metadata | `<state>/models/<source>.*` | source URL, licence, SHA-256, fetched-at, staleness |
| Research artifacts | `<state>/research/cache` | locator, content hash (implementation-dependent) |

What is missing is a single read-mostly catalog with size/inode accounting,
ownership, pinning, licence/verification state and a safe GC policy. Ocelot's
own data (for example a Wikipedia parquet set) is not in any Ozagia manifest
today; it must be represented as external/unowned rather than guessed.

## Goals

- One `ozagia assets` surface for `dataset`, `model`, `corpus`, `install`,
  `cache` and `other`.
- Record path, owner, scope, source, version/revision, SHA-256, allocated and
  apparent bytes, inodes, licence, timestamps, pinned/deletable and state.
- Auto-discover only known Ozagia-owned roots and manifests; everything else
  is explicit `register`/`adopt`.
- Never delete an unowned, unmarked, pinned or unverifiable asset.
- Keep `ozagia tmp` and `ozagia assets` disjoint: a temp sweep must not touch
  an asset, and an asset GC must not touch a temp workspace.
- Preserve offline-first behavior: no download or verification-by-network
  unless an explicit gated command is used.

## Non-goals

- A package manager or a model downloader.
- Copying Ocelot's data into Ozagia state or claiming ownership of it.
- Automatic deletion of shared caches or model weights.
- A full content-addressed object store; this is a catalog over existing
  layouts.

## Stage A — inventory and schema

Read-only inventory of every durable external asset root and manifest,
including Ocelot installs and Ocelot-owned data, HF benchmark caches,
deep-review checkouts, model metadata, research caches, eval caches and any
bundled/installer assets. For each: path, owner, identifiers, licence, size,
retention, deletion safety and existing surface.

Deliverable: an inventory table, an asset schema and a list of auto-discoverable
roots versus explicit-only roots.

## Stage B — core registry and CLI

`src/ozagia/core/assets.py` plus `src/ozagia/assets_cli.py`:

- user-level registry under `$XDG_DATA_HOME/ozagia/assets/registry/<id>.json`
  (fallback to the state dir only when the data home is not writable), one
  atomic file per entry;
- optional `.ozagia-asset.json` marker only for Ozagia-owned assets;
- `register`/`adopt` with bounded metadata; `--mark` only when owned;
- `list`, `status`, `scan`, `verify`, `pin`, `unpin`, `forget`, `gc`;
- `scan` only known roots/manifests by default; `--path` for an explicit
  tree; bounded walks using the temp-space accounting (allocated/apparent,
  inodes, partial/unknown);
- `verify` checks size and an optional SHA-256 without network;
- `gc` is a dry run by default and only considers `owner=ozagia`,
  `deletable=true`, unpinned, verified assets; Ocelot/HF/user/other owners
  are never deleted;
- JSON output stable; no file contents, prompts or secrets.

Acceptance: register a synthetic dataset and a model directory; status reports
both with sizes/inodes/licence; a pinned or `owner=ocelot` asset is never a GC
candidate; a missing path is `missing`, not silently zero.

## Stage C — integrations

1. Ocelot:
   - register installed versions from `.ozagia-ocelot.json` (version, archive
     SHA-256, tree SHA-256/bytes, licence, install time);
   - discover Ocelot data files only through an explicit `--path <ocelot>`
     scan or an Ocelot-provided data manifest, never by guessing that every
     `*.parquet` is a reference dataset;
   - represent Ocelot data as `owner=ocelot`, `deletable=false`,
     `pinned=true` by default; `ozagia assets gc` must never select it;
   - report the path/bytes/version in `ozagia assets status`.
2. HF benchmarks: register each `<benchmark>/<dataset_sha>` with revision,
   licence and row/meta hashes; owned by Ozagia but deletion only explicit and
   only if no report/reference pins it.
3. Deep-review corpus: register each pinned checkout with commit/tree SHA;
   owned, deletable only explicitly and never while referenced by a report.
4. Model metadata cache: register `<state>/models/*` as `cache`; model weights
   are explicit `register` unless a known model root is configured.
5. Read-only MCP tools `dev_assets_list`/`dev_assets_status`; no write or
   delete over MCP.
6. `ozagia status`/`doctor` may add a small assets summary, but must not walk
   huge roots or hash anything by default.

## Stage D — policy, verification and docs

- Pinning rules: a report, a goal, a live job or an explicit `pin` can keep an
  asset; unpinned does not mean deletable.
- Licence and unknown fields stay explicit; never infer a licence from a
  filename.
- Verification reports `verified`, `mismatch`, `missing` or `unknown`; a
  mismatch blocks GC.
- GC ordering: verified, unpinned, owned, unreferenced; prefer old `cache`
  before `dataset`/`model`; stop at the requested byte/inode budget.
- Document the Ocelot Wikipedia parquet case and the `ozagia tmp` boundary.
- Add capability metadata and roadmap/FEATURES updates.

## Safety rules

- Never scan the whole home directory by default.
- Never follow symlinks when measuring or deleting; never cross filesystems.
- Never delete `owner=ocelot`, `owner=huggingface`, `owner=user` or
  `owner=external` assets.
- Never delete an asset with an unverified or mismatched hash.
- A marker is only written for an Ozagia-owned asset after an explicit
  `--mark`; external assets are read-only records.
- The registry stores metadata, not contents, prompts or credentials.
- No network in `list`/`status`/`scan`/`verify`/`gc`; fetch stays in the
  existing explicitly gated download commands.

## Acceptance tests

- Synthetic dataset/model/corpus/install/cache entries register and list.
- A pinned asset and an `owner=ocelot` asset survive every GC plan.
- A missing or hash-mismatched asset is reported and not deleted.
- Ocelot install registration reads the manifest without importing Ocelot.
- Ocelot data is never auto-adopted from a filename.
- HF/corpus/model metadata entries report revision, licence and hashes.
- `ozagia tmp` audit/clean never selects an asset path.
- Bounded walk reports `partial`/`unknown` for a large or unreadable tree.
- JSON shape and exit codes are stable; no network is attempted.
