# Created-file ledger, Markdown routing and cleanup

`ozagia-files` records files that ozagia can attribute to a session, why they
exist and what should happen to them. It also routes new Markdown so one-off
notes do not accumulate in the repository root or `docs/`.

The ledger stores metadata, never file contents or prompts. Cleanup in this
build is a dry-run listing; moving files to trash is a later, explicitly
confirmed action.

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
and deletes keep `moved_from` or `status: deleted`.

## Commands

```text
ozagia-files list [--classification KIND] [--json]
ozagia-files scan [--session S] [--task T] [--reason TEXT] [--json]
ozagia-files explain PATH [--json]
ozagia-files orphans [--json]
ozagia-files cleanup [--older-than-days N] [--json]
ozagia-files route PATH [--content-file F] [--json]
```

`scan` records currently untracked/added files. It classifies by path and
front matter without rescanning every document for duplicates. `cleanup` is
dry-run only in this build; `--apply` is refused. `route` checks duplicates
and exits `1` when the path should be moved elsewhere.

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

## Cleanup

`ozagia-files orphans` lists unreferenced notes, scratch files and files with
`reason: unknown`. `ozagia-files cleanup` lists candidates with size, age,
reason, references and a recommendation.

- Referenced files (graph, docs, tests, front matter) are never candidate for
  deletion.
- `history/` and living docs are keep-by-default.
- Scratch files can be recommended for trash; notes and unknown files are
  `ask`.
- Cleanup runs before `worktree remove` and when the resource monitor is short
  on disk.

The current build does not move or delete anything. The apply/trash/restore
path lands with the integration into edit/write/patch and session hooks.

## Limits

- No full filesystem DLP and no content indexing beyond the existing research
  ingest.
- Files created outside the session manifest are not tracked; they are reported
  as unknown if a later scan sees them.
- Near-duplicate detection is deterministic and bounded. It can suggest an
  append; it does not rewrite a document.
