# Ops receipts and resumable handoffs

`ozagia.core.ops` provides a small, append-only JSONL journal for caller-curated
operations receipts. A caller chooses the storage directory (for example,
`.ozagia/ops`); the journal uses the fixed filename `records.jsonl`.

Each version 1 record contains a UTC timestamp, harness and session identifiers,
environment, operation kind, a short summary, and bounded lists of findings,
changes, resource identifiers, approvals, and next steps. It may also include
bounded source keys. The schema is intentionally flat: nested metadata,
unknown fields, prompts, command output, and control characters are refused.
Obvious inline credentials are redacted before append.

```python
from ozagia.core.ops import append_record, read_records, resume_summary

append_record(".ozagia/ops", {
    "harness": "opencode",
    "session": "session-123",
    "environment": "staging",
    "kind": "job-status",
    "summary": "The rollout is still in progress.",
    "findings": ["Two of three replicas are ready."],
    "changes": [],
    "resources": ["deployment/api"],
    "approvals": ["Read-only status check approved."],
    "next_steps": ["Check rollout status later."],
    "source_keys": ["iac:deployment:api"],
})

records = read_records(".ozagia/ops", limit=20)
handoff = resume_summary(".ozagia/ops", environment="staging")
```

`append_record` serializes cooperating writers, appends one bounded JSON line,
and calls `fsync`. The file is never truncated or repaired. `read_records`
returns a sequence-like `ReadResult` with validated records and separate,
content-free diagnostics for corrupt lines. Reads are bounded to 4 MiB and 500
returned records (at most 10,000 lines scanned); diagnostics are capped at 50.
Individual records are limited to 16 KiB, text fields to 512 characters and
identifiers to 200 characters. Findings, changes, approvals, and next steps
allow at most 16 items each; resources and source keys allow 32 each.
`resume_summary` returns the latest receipt state, its next steps as open
items, and up to 10 recent changes and approvals. An optional environment
filter keeps handoffs scoped.

These are **receipts, not a command log or replay system**. They must not contain
prompts, command lines, full command output, credentials, or Secret values.
Callers should provide concise outcomes and identifiers, not raw tool results.
The API makes no network, cloud, or cluster calls, and the code-task
`DevSession` schema remains separate. The full MCP coding surface exposes
`dev_ops_record`/`dev_ops_resume`; the CLI exposes `ozagia ops record` and
`ozagia ops resume`. Harness-specific installers are separate.
