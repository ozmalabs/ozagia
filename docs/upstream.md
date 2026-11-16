# Consent-based upstream field-improvement bundles

`ozagia.core.upstream` prepares a local diagnostic bundle from evidence a
person explicitly selects. It is an export, not a reporting service:

- `preview` reads only the named evidence and writes nothing;
- `write` writes a directory or zip **only** after explicit consent;
- `issue` writes a Markdown issue draft and posts nothing;
- no model is called, no network connection is opened, nothing is uploaded,
  and no issue or pull request is created.

The bundle exists so a person can review a redacted, reproducible report and
then open an issue manually. It is not a telemetry feature and there is no
background sender.

## Commands

```sh
python -m ozagia.upstream_cli preview \
  --root /path/to/repo \
  --title "Daemon tool fails after provider timeout" \
  --summary "Observed on three eval runs." \
  --evidence .ozagia/log-reports/2026-11-16.json \
  --evidence review-packet.json \
  --json

python -m ozagia.upstream_cli write \
  --root /path/to/repo \
  --title "Daemon tool fails after provider timeout" \
  --summary "Observed on three eval runs." \
  --evidence .ozagia/log-reports/2026-11-16.json \
  --evidence .ozagia/tasks/run-2026-11-16 \
  --out /tmp/ozagia-bundle \
  --consent

python -m ozagia.upstream_cli issue \
  --root /path/to/repo \
  --title "Daemon tool fails after provider timeout" \
  --evidence .ozagia/log-reports/2026-11-16.json \
  --out /tmp/ozagia-issue.md
```

`--evidence` may be repeated and accepts several paths after one flag.
`--out` for `write` is either a new or empty directory, or a path ending in
`.zip`. The destination parent directory must already exist.

Exit status:

| Code | Meaning |
| --- | --- |
| `0` | A preview, bundle or issue draft was produced. |
| `1` | Refusal: no explicit `--consent`, an unsafe destination or an issue destination that cannot be used. |
| `2` | Usage, root, evidence-read or IO error. |

Without `--consent`, `write` exits `1` and writes nothing. Consent is not a
default and is not inferred from a previous preview.

## Python API

```python
from ozagia.core import upstream

preview = upstream.preview(
    root,
    title="Daemon tool fails after provider timeout",
    summary="Observed on three eval runs.",
    evidence=[".ozagia/log-reports/2026-11-16.json"],
)

print(upstream.format_preview(preview))

result = upstream.write_bundle(
    root,
    "/tmp/ozagia-bundle",
    consent=True,
    preview=preview,
)
print(upstream.format_bundle(result))
```

`write_bundle(..., consent=True, title=..., evidence=...)` builds a fresh
preview when `preview` is omitted. A supplied preview wins; its content is
written as reviewed. `write_issue(...)` writes only the Markdown draft.

## Evidence allowlist

Only explicit files and task-run directories are read. There is no directory
scan, no home-directory scan and no state-directory discovery. Accepted
evidence shapes:

| Shape | Detection | Summary |
| --- | --- | --- |
| Log report | JSON from `log_inspect`: `tool: "log_inspect"` and a `findings` list. | Input counts, redaction counts, ranked findings, signatures, dimensions and proposed actions. |
| Code-review packet | `schema: "ozagia.code_review.packet.v1"`. | Range, worktree branch/dirty state, graph freshness, diff counts, findings, omissions and the test plan. |
| Task run | A directory containing `manifest.json`, `events.jsonl`, `artifacts.json` or `output.json`, or a `manifest.json` file. | Run id, spec id/version/digest, status, stop, preflight, gates, step metadata, artifact hashes/sizes, budget use and output name/digest. |
| Repo-scan report | A JSON object with `opportunities` and `not_assessed` (optionally wrapped under a `repo_scan.v1` output). | Opportunity title, kind, expected effect, effort, risk and evidence label. |
| Status snapshot | A JSON object with `workspace`, `resources`, `files`, `runtime`, `daemon`, `doctor` and `config`. | Workspace/worktree state, host memory/load/disk, file counts, daemon state, snapshot state and config state. |

Anything else is listed in `excluded` with a reason and is not copied. Raw
`.log` files, arbitrary JSON documents, empty directories, directories that
are not task runs, duplicates, oversized inputs and inputs that would exceed
the bundle byte limit are excluded.

Bounds:

| Limit | Value |
| --- | --- |
| Evidence paths per preview | 16 |
| Input bytes after decompression | 2 MiB |
| Bundle bytes | 8 MiB (evidence is capped lower to leave room for README, issue and manifest) |
| Findings shown per evidence input | 10 |
| Total selected findings | 20 |
| Config changes shown | 100 |

A task-run directory is read through its `manifest.json` only. `events.jsonl`,
`output.json` and artifact contents are not copied. The output value is
represented by its name and digest only.

## What is included

A preview and a bundle can include:

- the title and an optional short summary;
- a Python/OS/ozagia version summary (no hostname, username, working directory
  or environment variables);
- the project configuration **diff**: every setting whose source is not the
  built-in default, with its redacted value, default and source layer. Raw
  config text and comments are not copied;
- task class ids found in the evidence;
- failure signatures from log findings and failed task runs;
- short, deterministic reproduction commands, marked as suggestions that were
  not executed;
- the top selected findings with severity, kind, count and location;
- one redacted JSON summary per included evidence input.

Every preview lists `included` (with the exact redacted content that a bundle
would write), `excluded` (with a reason) and the fixed `excluded_categories`.

## What is excluded

The export never copies:

- raw log lines, raw JSONL records or conversation records;
- prompts, messages, completions or other conversational payloads;
- file contents, diffs, patches, transcripts, `stdout`, `stderr` or output
  bodies;
- secrets, API keys, tokens, passwords, authorization headers or
  credential-bearing URLs;
- email addresses;
- home-directory prefixes (they appear as `~`);
- raw process argv, command lines or environment variables;
- unrecognised evidence files.

The summaries pass through `ozagia.core.log_inspect.Redactor`. Values that
reach the preview are redacted and length-bounded, and the report records
redaction counts by category. Raw field names such as `prompt`, `content`,
`diff`, `file_text`, `stdout` and `stderr` are replaced with counted
placeholders when they appear in selected metadata.

Redaction is practical, not a proof of anonymity. Relative paths, repository
and branch names, model names, provider names, tool names and error classes can
still identify a system. Read the preview and the bundle before sharing either.

## Bundle layout

A directory bundle:

```text
manifest.json    deterministic metadata, consent record and SHA-256 hashes
README.md        what is included/excluded and how to review it
issue.md         Markdown issue draft; nothing posts it
evidence/
  01-log_report.json
  02-task_run.json
```

A zip bundle contains the same paths. Zip entries are written in a fixed order
with fixed timestamps and permissions, so the same preview content produces
byte-identical archives. Bundle files are created with mode `0600` where the
operating system supports it.

`manifest.json` records:

- schema and tool name;
- title, summary and redacted root;
- the consent record (`granted: true`, scope `local-bundle-only`,
  `network: false`, `uploaded: false`, `issue_created: false`);
- environment and config summaries;
- task classes, failure signatures, reproduction commands and selected
  findings;
- the included evidence table with source hashes, bundled path, bundled hash
  and byte size;
- exclusions, excluded categories, redaction counts, limits and the
  `not_assessed` list;
- a file table with SHA-256 and byte size for every file except the manifest
  itself.

Hashes are deterministic for the same preview. The manifest contains no
current timestamp, no destination path and no random value. The manifest does
not list its own hash; hash it separately if needed.

## Consent

`preview` always sets `consent_required: true` and `consent.granted: false`.
A write requires the caller to pass `consent=True` in Python or `--consent` on
the command line. A truthy value other than `True` is refused.

The consent record in the bundle states only what happened locally:

```json
{
  "granted": true,
  "scope": "local-bundle-only",
  "network": false,
  "uploaded": false,
  "issue_created": false
}
```

There is no consent for uploading, posting or contacting a maintainer. Those
remain manual actions.

## Reviewing and opening an issue manually

1. Run `preview` and read the text or JSON. Check every `included` entry and
   every `excluded` reason.
2. Run `write --consent` only when the preview is acceptable.
3. Open `manifest.json` and check the consent record and file hashes.
4. Read `issue.md` and the files under `evidence/`. Remove or rewrite anything
   you do not want to share.
5. Open your issue tracker and create an issue yourself. Paste the reviewed
   Markdown. Attach the bundle only if you are comfortable sharing every file
   in it.
6. If the project asks for a minimal reproduction, use the suggested commands
   as a starting point; they were not run by this export.

Nothing in the bundle opens a browser, authenticates, calls a provider, uploads
a file, opens an issue or opens a pull request.

## Limits and non-goals

- The export is local and read-only until an explicit write. There is no
  daemon, background sender, scheduled upload or implicit state.
- It does not establish root cause. Findings are observed clusters; a task
  run's failure is reported as recorded. `truth_assessed` is always `false`.
- It does not read raw logs. To produce a log report, run
  `python -m ozagia.log_inspect_cli` first and pass its JSON report.
- It does not run tests, lint, models, git commands or the commands it
  suggests.
- It cannot prove redaction completeness. Review paths, model names, tool
  names and free-text metadata before sharing.
- It refuses destinations that are the repository root, the filesystem root,
  inside `.git`, an ancestor of the repository, a missing parent, a non-empty
  directory or an existing file when a directory is expected.
- It never writes outside the destination. Bundle paths are fixed and checked
  for containment.

## Tests

```sh
PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_upstream.py
.venv/bin/ruff check src tests
```

The tests cover redaction of secrets, prompts, emails and home paths; the
evidence allowlist and bounds; task-run output omission; config diffs; preview
no-writes; consent refusal; deterministic directory and zip manifests; hashes;
unsafe destinations; the issue draft; CLI exit codes; and a socket monkeypatch
that proves no network access.
