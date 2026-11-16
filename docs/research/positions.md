# User positions and the review ledger

Two records help a research or writing session pick up where an earlier one
stopped: what the user said they hold, and which review findings are still
open.

## User positions

When the user corrects the agent's reading ("the chapter argues X, not Y")
or states a stance, record it with `research_position_record`:

```json
{
  "session_id": "research-…",
  "conversation_session_id": "opencode-ses_42",
  "stance": "Chapter 3 treats attention as precision weighting, not a faculty.",
  "topic": "attention as precision weighting",
  "correction_of": "The summary said chapter 3 defends a faculty view.",
  "quote": "attention is precision weighting",
  "concerns": ["claim-0004", "source-0002"],
  "documents": ["notes/chapter3.md"]
}
```

`session_id` is the research investigation; `conversation_session_id` is the
agent session the user spoke in. `stated_at` defaults to the time of the call.
The position is stored as:

- a `Source` of type `conversation`, one per conversation session id
  (locator `{"kind": "conversation", "conversation_session_id": …}`);
- a `Holding` with `epistemic_role: user_position`, `authority: personal`
  and `use_constraints: ["user_stance", "not_external_evidence"]`, holding the
  stance, topic, date, optional quote and optional `correction_of`;
- a `concerns` edge to each record it is about.

Only what the call passes is stored. Nothing reads a transcript; pass a quote
only when the user approved that span.

A position needs a `topic`, `concerns` or `documents` so it can be found
again. `research_position_supersede` records a new position that replaces an
active one (linked with `supersedes`; topic, concerns and documents carry over
when omitted). `research_position_retract` marks one retracted; its Holding
becomes `rejected` with the reason in its rationale. Both are amendment
events, so the earlier stance stays in the event log. `research_position_list`
lists positions by status (`active` by default, `superseded`, `retracted` or
`all`), by a record they concern (`node_id`) or by `document`, with counts per
status.

### In research context

`research_context` (and the combined-mode `research_dev_context`) adds a
`user_positions` block next to the sourced results. It holds active positions
that touch the query:

- `concerns_retrieved_source`: the position concerns a record whose source
  path is among the retrieved passages;
- `names_retrieved_document`: one of its `documents` was retrieved;
- `concerns_listed_record`: it concerns a record listed in the call's
  `node_ids`;
- `topic_similarity`: its topic, stance and concerned records are similar to
  the query under the deterministic `hash-bow` embedder (score ≥ 0.2 after
  dropping function words).

Each item carries `label: "user position"`, its `date` and the conversation
it came from. The block is marked `separate_from_evidence: true`. A position
is the user's stance; it is never listed as a result and never supports or
contradicts a claim.

## Review ledger

A review run records what one reviewer said once (see [review](review.md)).
The ledger tracks each issue until it is closed, across sessions. A
`LedgerFinding` has:

- `document` (workspace-relative path) and an optional `anchor`: a Markdown
  or TeX section slug, a `doc:<rel>#<slug>` key, or a path alias such as
  `methods/sampling` (see `ozagia.core.sections`); an optional `quote`;
- `severity` (`blocking`, `major`, `minor`, `note`) and `text`;
- `status`: `open`, `addressed`, `wont_fix` (`wont-fix` is accepted) or
  `disputed`;
- `history`: each status change with the previous status, note, time and
  conversation session id.

`research_review_finding_add` adds findings given in the call, or imports a
recorded run's findings with `run_id` (each `ReviewFinding` once; a finding
about a claim takes the claim's source path as its document). All findings in
a call are validated before any is recorded. When the document's text is
given (`document_text`) or the file is readable inside the workspace, the
ledger stores a baseline: the document hash, the anchored section's hash and
lines, and whether the quote was present. An anchor is stored as the section's
slug and `doc:` key.

`research_review_finding_status` changes a status. A note and the conversation
session id are required; setting the current status again is refused.
Reopening is a status change like any other.

`research_review_outstanding` returns findings in the given `statuses`
(default `open`), grouped by document, most severe first, with counts by
severity and by status across the whole ledger.

`research_review_rereview` takes a new version of a document (`text`, or the
workspace file) and proposes, for each open finding on it:

| reason | verdict |
|---|---|
| `anchor_missing`: the section is gone | `may_be_addressed` |
| `quote_gone`: the quoted text is no longer in the span | `may_be_addressed` |
| `anchor_changed`: the span's hash differs from the baseline | `may_be_addressed` |
| `anchor_changed_quote_present` | `likely_open` |
| `anchor_unchanged` | `likely_open` |
| `quote_present` (no baseline hash) | `likely_open` |
| `no_baseline`: no hash and no quote | `unknown` |

It records nothing and closes nothing (`auto_closed: false`). Read the span,
then mark the finding.

Positions and ledger findings live in the investigation's graph file under
`.ozagia/research/` (or `OZAGIA_STATE_DIR`), like every other research record.
Workspace documents are only read, never written.
