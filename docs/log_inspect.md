# Local log inspection (`log_inspect`)

`python -m ozagia.log_inspect_cli` produces a bounded, redacted report from
log files a person explicitly names. It is a read-only local command: it does
not start a task, call a model, use the network, upload a bundle or apply a
proposal.

```sh
python -m ozagia.log_inspect_cli \
  --log ~/.local/share/opencode/log/opencode.log \
  --log .ozagia/daemon_journal.jsonl \
  --log .ozagia/eval/runs/run-1/result.json \
  --since 2026-11-01T00:00:00Z \
  --limit 20 \
  --json
```

Options:

| Option | Meaning |
| --- | --- |
| `--log PATH` | An explicit input file. Repeatable. `.gz` is read as gzip. |
| `--root R` | Resolve relative input paths against `R`. `R` is never scanned. |
| `--since ISO` | Drop records that have a timestamp older than this. Records without a timestamp are kept. |
| `--limit N` | Show at most `N` findings (default 20, hard maximum 100). |
| `--json` | Emit the report as JSON. |
| `--out FILE` | Also write the text or JSON report to `FILE` atomically. |
| `--task-spec FILE` | Write a bounded, read-only task specification describing the inspection. The spec is data; the command does not run it. |

Exit code `0` means a report was produced and every named input was readable.
Exit code `2` means an input was unreadable, a path was not a regular file, or
the request was invalid. A report is still printed when some inputs were
readable; `errors` in the report names the failures.

## Inputs

Only the files named by `--log` are read. There is no implicit scan of a home
directory, a repository, the current directory, `.ozagia/` or an eval run
root. A directory passed as an input is an error: list its files explicitly.

The parser recognises:

- **OpenCode logs**: key=value lines with `timestamp`, `level`, `message` and
  often `cause`, `role`, `service`, `tool` or `model`.
- **Daemon journals**: JSONL records from `daemon_journal.jsonl`
  (`op`, `tool`, `intent`, `handled`, `is_error`, `reason`, `fallback`,
  `ts`, `session`, `harness`).
- **Eval run JSON**: the `runs` array written by `ozagia.eval.agent`; each
  failing run becomes an observation with its task, arm, driver and backend
  model. JSONL eval records also work.
- **Arbitrary JSONL**: records are considered failures when they carry an
  error level, `is_error`, `ok: false`, `success: false`, `timed_out` or a
  non-empty `error`. Conversation records (`role` of user/assistant or a
  message/prompt/completion kind) are skipped.
- **Text**: lines with `ERROR`, `WARN`, `FATAL`, `EXCEPTION` or `TRACEBACK`
  severity markers. Timestamps, `tool=`, `intent=`, `harness=` and `model=`
  are used when present.

Malformed JSONL lines do not stop a run. They are counted and reported as a
low-severity `MalformedInput` cluster with a `capture_rule` proposal; the
malformed text itself is not copied into the report.

Reading is bounded per run by `max_files` (16 by default), and per input by
`max_bytes` (4 MiB after decompression) and `max_lines` (20,000). Gzip inputs
are decompressed with the same byte cap. The report records `bytes_read`,
`lines_read`, `truncated_bytes`, `truncated_lines` and any inputs skipped by
the file limit. The module caps are 64 files, 64 MiB per input and 200,000
lines per input. At most 50,000 failure observations are clustered in one
report; any excess is counted as `observations_dropped` and stated in
`not_assessed`.

## Redaction

Every value that reaches a report, signature or dimension passes through a
redactor first. It replaces and counts:

- API keys and provider tokens (`sk-...`, GitHub, AWS, Google, Slack, JWT),
- `Authorization` headers, bearer tokens and credential-bearing URLs,
- password/secret/token/API-key assignments,
- email addresses,
- prompt and conversation-message payloads, content, completion, input and
  argument fields,
- file-content, diff, patch, stdout, stderr, transcript and output fields,
- home-directory prefixes.

The report contains counts per category and per input, never the replaced
values. Conversation records are skipped entirely rather than summarised. A
log event's error summary may remain in a signature because it is needed to
cluster failures; payload fields named as prompt/content/file-content are
redacted, and the signature is length-bounded. Raw log lines are never
included in text or JSON output.

Redaction is practical, not a proof of anonymity. Paths, tool names, error
classes, model names and provider names may still identify a system. Treat a
report as local diagnostic data, not as a publication-ready artefact.

## Clustering and ranking

Observations are grouped deterministically by:

1. error type (for example `Session.StepFailedError` or `DaemonToolError`),
2. normalised message signature (stack frames, paths, ids and numbers
   removed),
3. tool, intent, harness, model and task class.

A finding records its stable id, severity, occurrence count, first and last
timestamp (when known), the dimensions, source kinds, per-source/per-severity
counts, failure-field counts and a count of redacted values. The likely
component is the most frequent component label in the cluster (for example
`daemon tool`, `opencode server`, `eval harness fix-bug` or `structured log
shell`).

Findings are ranked by severity first, then frequency, then recency. Stable
ids are derived from the cluster key, so rerunning the same inputs produces
the same ids even when ranks change. `--limit` truncates only display; the
report says how many clusters were observed in total.

Records without timestamps still participate. `--since` filters only records
whose timestamp is known; absence of a timestamp is not evidence that a
record is old.

## Proposals

Each finding carries a `proposed_action` object marked as a proposal:

```json
{
  "kind": "proposal",
  "action": "config_change",
  "summary": "Review configuration for this failure: ...",
  "confidence": "low",
  "auto_applied": false,
  "requires_approval": true,
  "network": false
}
```

The action is one of:

- `config_change`: review provider, model, credentials, plugin or timeout
  configuration;
- `task_class`: add or tune a bounded task class for a recurring eval failure;
- `capture_rule`: improve structured logging when malformed or missing
  structure prevents reliable clustering;
- `bug_report`: prepare a local bug report with a redacted excerpt and a
  reproduction.

A proposal is advisory text. It is not a diff, patch, command or automatic
edit. Nothing in this module applies it.

## Bounded task specification

`proposed_task(report)` and `--task-spec FILE` produce JSON that follows the
concepts in `ozagia.core.tasks`: an id, objective, named inputs, a `read`-only
tool allowlist, empty write scope, resource and budget limits, gates, an output
schema and `approval: {mutate: "deny", network: "deny"}`. It validates through
`core.tasks` but is never executed here. The actual input values are supplied
by whoever runs such a task, and the task's executor is injected by that host,
not by the log inspector.

## Privacy limits

- Local and read-only by default. There is no network call and no model call.
- No home-directory, repository or state-directory discovery.
- No automatic task execution, edit, patch, commit, merge, push or upload.
- No upstream bundle. Plan item 19's consent-based diagnostic export is a
  separate feature; this command does not create or send one.
- Redaction covers common secret shapes and payload field names. It cannot
  recognise every secret, especially one embedded in an unusual free-text
  error message. Review a report before sharing it.
- `truth_assessed` is always `false`. Findings describe observed clusters in
  the named inputs. Absence of a finding is not proof that no failure
  occurred, and a cluster is not a root-cause verdict.
