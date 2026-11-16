# Created-file ledger, Markdown routing and cleanup

`ozagia-files` records files that ozagia can attribute to a session, why they
exist and what should happen to them. It also routes new Markdown so one-off
notes do not accumulate in the repository root or `docs/`.

The ledger stores metadata, never file contents or prompts. Cleanup is
preview-first: a dry-run issues a one-time token, `--apply` moves planned
files under `.ozagia/trash/<timestamp>/` (never deletes), and `restore` moves
them back with conflict checks. A separate, explicit `trash purge` can delete
old trash entries; nothing is purged automatically.

## Ledger

Records live in `.ozagia/files/ledger.jsonl` and are bounded. Each record
contains:

```json
{
  "path": "history/2026-11-16/ocr-comparison.md",
  "worktree": "/repo",
  "hash": "sha256:...",
  "size": 4210,
  "created_at": "2026-11-16T16:30:00+11:00",
  "created_by": {"session": "ses_...", "harness": "opencode",
                 "task": "ocr.compare", "tool": "write"},
  "reason": "one-off comparison of OCR backends",
  "classification": "history",
  "status": "active",
  "references": ["doc:docs/research/pdf.md"],
  "duplicate_of": null,
  "cleanup": {"policy": "keep", "after": null}
}
```

The reason comes from the task class, a reserved `_reason` on a handled call,
or the shell command context. If it cannot be determined, it is `unknown` and
cleanup always asks.

Ignored/cache trees are summarized by directory, not one row per file. Moves
and deletes keep `moved_from`, `moved_to`, `removed_by`, or `status:
deleted`. A trashed file's latest row has `status: trashed`, its `trash_path`
and a `decision` (`trashed` or `forced`). A restored file is active again
with `restored_from`, `restored_at` and `decision: restore`.

## Session manifests

Session start writes a bounded manifest to
`.ozagia/files/sessions/<hash>.json` (the hash is of harness plus session).
Each entry stores the relative path, size, SHA-256 hash (or why there is
none: `large`, `bounded`, `symlink`, `unreadable`, `missing`, `not-file`) and
status (`tracked`, `untracked`, `added`, `modified`, `renamed`, `deleted`). Tracked and
untracked files are included; `.git`, `.ozagia` and cache/build trees are
skipped. Manifests hold no file contents.

Session end compares the worktree with that baseline and records only what
changed:

- a new path is a session creation and gets the session creator;
- a modified untracked file already present at the baseline is recorded but
  not attributed to the session's creation;
- a pre-existing untracked file that did not change is not recorded at all;
- a move is paired by content hash and keeps `moved_from`/`moved_to`; the
  destination is metadata, not a new creation;
- a deleted untracked or previously recorded file keeps a `status: deleted`
  tombstone with `removed_by`.

If no baseline exists (old daemon, crash, pruned state), the fallback records
the current untracked/added files with `baseline: missing` and
`attribution: false`: nothing is attributed to the session. A closed
manifest is not applied twice when the end event is replayed. At most 64
manifests are kept (oldest pruned) and each is capped at 5000 files with a
64 MiB hashing budget; paths over the budget are marked, not read.

## Integration with the daemon, edits and hooks

- A handled `write`, and an `edit` that creates the file, records it after
  the tool succeeds: session, harness, tool and the reason from the reserved
  `_reason` input field (else `unknown`). An edit of an existing file is not
  recorded as a creation. Key edits are not creations.
- `after` for a built-in `edit`, `write`, `patch` or `shell` records the
  newly created files among `meta.changed_paths`, plus the file an edit or
  write names. It runs one bounded `git status` pathspec check for those
  paths only, never a whole-repository scan.
- `session_event` `start` writes the manifest; `end` records the delta.
  `compact` does nothing. A missing baseline uses the unattributed fallback
  described above.
- Records are deduplicated by path and hash: an unchanged file already in
  the ledger is not recorded again. The ledger stores hashes, sizes and
  labels, never file contents or prompts.
- Daemon `stats` adds `files_recorded` (rows appended), `file_ledger_scans`
  (bounded scans/manifest operations), `file_ledger_errors` (ledger/git
  failures; the tool call itself never fails for them),
  `file_manifest_sessions` (start snapshots and end deltas) and
  `file_manifest_errors` (manifest failures).
- Opt out of the daemon's `after` and session work per request with
  `file_ledger: false`, or for a daemon with `OZAGIA_FILE_LEDGER=0`.
- MCP read-only `dev_files_list` and `dev_files_explain` list and explain
  ledger rows (no writes, no cleanup).

Programmatic entry points: `record_file(root, rel, ...)` for one file,
`record_paths(root, paths, ...)` for several, `scan_new_files(root, ...)`
for untracked/added files (`paths=` restricts it), and
`session_snapshot(root, session, ...)` / `session_delta(root, session, ...)`.

## Commands

```text
ozagia-files list [--classification KIND] [--json]
ozagia-files scan [--session S] [--task T] [--reason TEXT] [--json]
ozagia-files explain PATH [--json]
ozagia-files orphans [--json]
ozagia-files cleanup [--older-than-days N] [--include-referenced] [--json]
ozagia-files cleanup --apply --confirm TOKEN [--force] [--json]
ozagia-files trash list [--json]
ozagia-files trash purge [--older-than-days N] [--confirm TOKEN] [--json]
ozagia-files restore ID [PATH ...] [--json]
ozagia-files route PATH [--content-file F] [--json]
```

`scan` records currently untracked/added files. It classifies by path and
front matter without rescanning every document for duplicates. `cleanup` is
a dry-run that prints a one-time token; `--apply --confirm TOKEN` performs it.
`trash list` shows runs (newest first) with age, size, restored/purged counts
and retention state. `trash purge` is a dry-run that prints a one-time token;
`--confirm TOKEN` deletes the planned trash entries. `restore` moves files
back; it is unchanged by purge. `route` checks duplicates and exits `1` when
the path should be moved elsewhere.

## Markdown routing

| Kind | Destination |
| --- | --- |
| Root README/CHANGELOG/LICENSE/CONTRIBUTING/AGENTS | root allowlist |
| Living guide | `docs/` |
| Dated plan | `docs/plans/YYYY-MM-DD_topic.md` |
| Dated result/report | `docs/results/<topic>_<date>/README.md` |
| One-off analysis/note | `history/YYYY-MM-DD/<slug>.md` |
| Scratch, not worth keeping | `.ozagia/scratch/<session>/` |

`markdown_route(path, content, context)` is deterministic. It reads path rules,
front matter, the title, headings, hashes and word shingles. It returns
`kind`, `suggested_path`, `reason`, `duplicate_of`, `append_to` and
`confidence`.

New `history/` files should carry front matter:

```markdown
---
created: 2026-11-16
session: ses_...
task: ocr.compare
reason: one-off comparison of OCR backends
status: historical
retention: keep
related: [docs/research/pdf.md]
---
```

To slow accumulation:

- root Markdown outside the allowlist is refused or routed to `history/`;
- a new file needs a reason;
- a same-topic document suggests appending a dated section instead;
- a session/task has a new-Markdown budget (default 3);
- generated reports use one stable path;
- `history/` gets a generated `index.md` from the ledger, not a hand-maintained
  list.

Markdown content can also be classified by headings (`Decisions`, `Evidence`,
`Claims`, `Open questions`, `Results`) and routed to the existing records with
a backlink to the file. The narrative stays in the file; structured data goes
to the graph.

## Cleanup, trash and restore

`ozagia-files orphans` lists unreferenced notes, scratch files and files with
`reason: unknown`. `ozagia-files cleanup` lists candidates with size, age,
reason, references and a recommendation, and writes a one-time plan under
`.ozagia/files/cleanup/`.

- Referenced files (graph, docs, tests, front matter) are excluded from a
  plan unless `--include-referenced`; even then they are applied only with
  `--force`, and a forced move is recorded as `decision: forced`.
- Files with `reason: unknown`, an unknown classification or no ledger
  record are refused unless `--force`; a force is recorded. An explicitly
  named file outside the plan is also refused unless forced.
- `history/` and living docs are keep-by-default.
- Scratch files are recommended for trash; notes and unknown files are
  `ask`.
- The token is consumed by the first apply attempt (even if every candidate
  is refused), expires after 15 minutes by default, and cannot be replayed.
  Dry-run the same filter again for a new token.
- `cleanup --apply` moves files to
  `.ozagia/trash/<timestamp>/<original path>` with `shutil.move`, preserving
  content and metadata. It never deletes. A metadata-only `manifest.json`
  beside the files records path, size, hash, classification, reason and the
  decision.
- The ledger's latest row for a moved file becomes `status: trashed` with
  `trash_path`, `trashed_at` and a `decision`; the creation record, reason
  and references are preserved.
- `restore ID [PATH ...]` moves files back. It refuses a missing trash copy,
  a path outside the root, a symlink, `.git`/`.ozagia` destinations, an
  existing destination and an already restored entry. The ledger row becomes
  active with `restored_from` and `restored_at`.
- `trash purge` is the only deletion path. It deletes old trash entries, never
  repository files, and has its own one-time token under
  `.ozagia/files/purge/`. It is a dry run unless `--confirm TOKEN` is given.
- Cleanup runs before `worktree remove` and when the resource monitor is
  short on disk.

Safety is checked per file: paths must stay inside the repository, every
component is checked for symlinks, `.git` and `.ozagia` are never moved, the
file must still match the plan's hash (unless forced), and a trash
destination that already exists is refused. `list_trash` reads at most 200
runs.

## Trash retention and explicit purge

`files.retention_days` is the trash retention. It is a typed setting (user or
repository `ozagia/config.toml`, default 30 days). It labels a run `expired`
when the run manifest's `created_at` is at least that many days old. It never
deletes anything: there is no automatic purge at startup, during cleanup or
from the resource monitor. Ledger cleanup keeps its own explicit
`--older-than-days` option.

`ozagia-files trash purge` is a dry run by default. It considers whole runs
with a valid manifest that are old enough, lists the entries that are safe to
delete, and writes a one-time plan. `--older-than-days N` overrides the
setting for this plan. Apply it with:

```text
ozagia-files trash purge --confirm TOKEN
```

The token is consumed by the first apply attempt, expires after 15 minutes by
default, and cannot be replayed; dry-run again for a new token. An apply
re-reads the plan and each manifest and refuses entries that were restored or
purged since the plan, whose manifest changed, whose size or hash changed, or
whose path is unsafe.

Only entries still in the trash are eligible. Restored entries, purged
entries and unknown entries (missing or corrupt manifest, missing file,
unsafe path, invalid timestamp) are never deleted, and a run with no
remaining entries is not a candidate. A purge marks each manifest entry
`purged_at` with `decision: purged`, removes files and empty subdirectories
inside `.ozagia/trash/<id>/`, and appends a metadata-only row to
`.ozagia/trash/audit.jsonl` (repository path, trash path, size, hash, run,
age, retention). A run whose entries are all purged is removed with its
manifest; a run with restored or unknown entries keeps its manifest. The
audit is the durable record either way. Repository files are never a deletion
target and `restore` is unchanged.

Safety: the purge root is `.ozagia/trash`. A symlinked trash root, run or
path component; a `.git`, state-directory or repository-root target; an
absolute or `..` trash path; and any path that resolves outside the run are
refused. The dry run reports unknown or ineligible runs in its `skipped`
list, and apply ignores anything that was not in its plan.

## Limits

- No full filesystem DLP and no content indexing beyond the existing research
  ingest.
- Session manifests include tracked and untracked files but skip ignored
  cache/build trees, `.git` and `.ozagia`. A file that never appears in the
  manifest (beyond its 5000-file or 64 MiB hash budget, or created after the
  session end) is not attributed.
- A built-in `patch` or `shell` that creates a file outside
  `meta.changed_paths` is not recorded until a session delta sees it.
- Move pairing is by content hash and size: a move that also changes content
  appears as a delete plus a new file.
- The ledger, manifests, cleanup plans, purge plans and trash manifests are
  metadata-only, and the trash audit stores metadata, not file contents.
  Near-duplicate detection is deterministic and bounded. It can suggest an
  append; it does not rewrite a document.
- Cleanup never deletes: apply moves to trash, restore moves back. `trash
  purge` is the only deletion path, is always explicit, and deletes only
  entries inside `.ozagia/trash/`.
