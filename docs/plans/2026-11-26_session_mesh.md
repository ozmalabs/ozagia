# Session mesh: peers, leases and merge negotiation

Status: active plan (2026-11-26). A mesh lets Ozagia sessions that can already
see each other coordinate without a leader. It is local coordination, not an
authorization or security boundary.

## Problem

Two Ozagia sessions working in the same repository can currently edit the same
files and discover each other only indirectly through ownership warnings. There
is no way to:

- send a bounded message to another live session;
- ask for a worktree, branch or codebase lease;
- reserve the merge step so only one session merges at a time;
- negotiate who does what without inventing a fixed hierarchy.

## Goals

- Discover live sessions for one repository/state directory.
- Send bounded, addressed messages with expiry and delivery receipts.
- Acquire, renew, release and queue leases on worktrees, branches, merge
  targets, paths and declared resource grants.
- Let peers negotiate a worktree each, then let whoever is ready acquire the
  merge lease, merge and release.
- Survive crashes: heartbeat, lease TTL, stale-holder takeover and orphan
  reporting.
- Keep everything local and opt-in; no network, no credentials, no model-only
  authority.

## Non-goals

- A leader/election protocol. The lease order is the only authority.
- A distributed consensus system across machines.
- Enforcing git operations. A merge lease is advisory to cooperative agents;
  existing worktree/commit/merge policy and approvals still decide whether a
  merge is allowed.
- Letting a model self-authorize a merge outside policy or approve its own
  administrative actions.
- Carrying prompts, source text, credentials or large payloads in messages.

## Exposure layers

The mesh must be useful without a model, then expose progressively more only
when a session opts in:

| Layer | Surface | What is available | Gate |
| --- | --- | --- | --- |
| **0 — core** | library, CLI, filesystem | register, heartbeat, peers, messages, leases, worktree/merge requests | none; pure Ozagia |
| **1 — read** | prompt context, status/TUI, read-only MCP | self, peers, subs/supers, active leases, queue positions, unread counts | session opt-in; metadata only |
| **2 — coordinate** | native tool/CLI | send bounded messages; request/renew/release leases; request worktree/merge | explicit per-session `mesh_model=coordinate`, rate/queue limits, audit |
| **3 — supervise** | native tool/CLI | offer/accept a sub/super link, delegate a request, see descendant topology | mutual handshake; host approval; no automatic authority |

Rules:

- Layer 0 is the whole mesh. Higher layers call the same registry, messages and
  leases; they do not get a separate state or bypass.
- A model never approves its own admin grant, takeover or merge exception.
  Existing policy/approval remains authoritative; a merge lease is still
  advisory.
- A model cannot conscript another session. A sub/super link is a mutual
  handshake (`request`/`accept`/`decline`/`revoke`), recorded on both sides and
  revocable without affecting leases already held.
- A super may ask a sub to do work or offer a lease, but cannot release the
  sub's lease, merge on its behalf or read its mailbox without explicit
  delegation. Delegation is a separate, bounded record.
- The prompt receives a compact topology summary only: self, role, linked
  peers/subs/supers, lease ids/resources/expiry, queue positions and unread
  counts. No message bodies, paths outside the mesh summary, credentials or
  source text.
- Layer 2/3 tool calls are rate-limited and bounded; a loop or repeated
  negotiation is reported, not amplified.

### Topology and link grammar

- `role` is **derived from accepted links**, not set at registration. The
  default is an unlinked `peer`.
- A link is a 1:1 edge, not a global hierarchy. Multiple edges form a forest or
  DAG; no leader is elected and no lease authority flows from a link.
- Command shape: `mesh link request|accept|decline|revoke|list|show`.
  `--relation peer|sub|super` is from the requester's perspective: `sub` means
  the requester is subordinate to the target, `super` means the reverse.
- Link record: `link_id`, `from`, `to`, `relation`, `state`
  (`pending|active|declined|revoked|expired|stale`), `created_at`,
  `accepted_at`, `expires_at`, `revoked_at`, `capabilities`, `reason`,
  `delegation?`, bounded `history`.
- `accept` is mandatory before either registration's role/parent changes.
  `decline` ends a pending request; `revoke` is a unilateral exit. A stale link
  is reported `stale`, not `dead`; re-linking requires a new request/accept.
- A link never releases a lease, reads a mailbox, authorizes a merge or permits
  takeover. Delegation is a separate, bounded, revocable record granted by the
  resource/mailbox owner to a linked peer.
- Broadcasts go to live linked peers/subs/supers, never to a global list.
- The Rust `ozma-mesh` build is a WireGuard overlay, not this coordination
  protocol. Its peer/role-less `MeshNode` and signed advertisement are not
  agent identity or link authority; only its transport/discovery/diagnostics
  may be reused behind an explicit adapter.

### Links and delegation records

```
<state>/mesh/<mesh_id>/links/lnk_<16hex>.json
<state>/mesh/<mesh_id>/delegations/dlg_<16hex>.json
```

Delegation actions are a closed set (`lease_release`, `mailbox_read`,
`merge_on_behalf`), scoped to an exact lease/resource or mailbox, with
`expires_at`, `max_uses`, `used` and a revoke history. Only the holder/owner can
grant it; a super cannot grant rights over a sub's lease or mailbox. Role alone
never satisfies an access check.

## Next: Ozma mesh bridge

The local mesh is the source of truth. After the core and model layers, the
[Ozma mesh bridge](2026-11-26_ozma_mesh_bridge.md) maps flights of Ozagia
agents to Ozma `COORDINATION`: agent identity/cards, typed messages, claims,
channels, subscriptions, inbox, blockers, wake modes and the A2A adapter. The
bridge translates; it does not reimplement Ozma or make remote claims
authoritative.

## Model

### Scope and identity

- Mesh root: `<state dir>/mesh/`, per repository; optional `mesh_id` in config
  partitions the same state dir.
- Session identity is `{harness, id}` from the existing ownership layer. A
  registration records pid, started/heartbeat, host, capabilities, worktree
  path/branch, status, mailbox cursor and active leases.
- Discovery is local: same state directory, same mesh id, live heartbeat.
  `$HOME`/other repositories are not scanned.

### Messages

- One bounded JSON envelope per message: `id`, `from`, `to` (session or
  broadcast), `kind` (`hello|request|offer|accept|reject|status|release|note`),
  `subject`, `body` (bounded), `in_reply_to`, `created_at`, `expires_at`,
  `lease` (optional), `evidence` (optional).
- Delivery is a mailbox per session; delivery/read state is recorded. A
  broadcast is copied to live peers, never to a growing global list.
- Bodies are length-bounded and redacted with the existing secret redactor;
  prompts/source text are discouraged and never required. Attachments are
  references (paths/keys/lease ids), not file contents.
- Retention: delivered messages older than a short TTL are removed; unread
  messages are bounded by count/bytes.

### Leases

A lease is a file under `<state>/mesh/leases/`:

```json
{
  "schema": 1,
  "lease_id": "lease_...",
  "resource": {"kind": "worktree|branch|merge|path|grant|other",
               "id": "worktree:/path", "mode": "read|write|merge"},
  "holder": {"harness": "opencode", "session": "ses_..."},
  "state": "pending|active|released|expired|revoked",
  "created_at": "...",
  "expires_at": "...",
  "renewed_at": "...",
  "queue": [{"harness": "...", "session": "...", "at": "..."}],
  "took_over": null,
  "reason": "..."
}
```

- Acquire is serialized by a short `flock` on `mesh/leases/.lock`; only one
  active lease per `(resource.kind, resource.id, mode)` when the mode is
  exclusive (`write`/`merge`). `read` leases may coexist.
- If held, the request is queued and returns the holder and queue position.
  Queue order is first-come, first-served; no priority/hierarchy.
- Renew extends up to a cap. Release removes the active holder and promotes
  the next queued request only when it is still live.
- Expiry is based on the lease TTL and the holder's heartbeat. An expired
  lease is reported as `expired`; a live takeover records `took_over` with the
  old holder and time. Expired leases are not silently deleted.
- A `merge` lease on a target branch/worktree is exclusive. The holder merges
  and releases; other sessions can then merge or rebase. A merge lease does
  not by itself authorize the git operation; worktree/commit/merge policy
  still applies.

### Worktree and merge negotiation

- `mesh worktree request --branch BRANCH [--path PATH]` sends a request and
  acquires a worktree/branch lease for a configured or discovered worktree.
  Existing worktrees are preferred; creating a new one remains a separate
  preview-first mutation.
- A session can advertise a free worktree with `mesh worktree offer`.
- `mesh merge request --target BRANCH|PATH` queues for the exclusive merge
  lease. The first live holder proceeds; others see `holder`, `queue` and
  `expires_at`.
- A completed session releases its worktree lease and, if it merged, the merge
  lease. The next queued session may proceed.

## Stages

### A — inventory and protocol

Inventory `owners`, `session`, `worktree`, `goals`, temp leases, daemon
session/ownership, harness result shapes, MCP/add-in surfaces and the state
layout. Produce the exact envelope/lease schemas, lock path, stale thresholds
and the CLI names. No edits.

### B1 — core mesh registry, messages and leases

Implemented in `src/ozagia/core/mesh.py`, `src/ozagia/mesh_cli.py`:

- `register/unregister/heartbeat/peers/status` with PID/heartbeat liveness;
- `send/inbox/read/ack` with bounded bodies, redaction, expiry and
  broadcast-to-live-peers;
- `lease_acquire/lease_renew/lease_release/lease_list/lease_status` with a
  `flock` lock, queue, TTL, expiry and takeover;
- `worktree_request/offer` and `merge_request` built on leases;
- `ozagia mesh peers|status|send|inbox|read|lease ...|worktree ...|merge ...`
  with `--json`, stable exit codes, no network.
- State under `<state>/mesh/`; atomic writes, 0700 dirs/0600 files; no
  credentials, source text or large bodies.
- `role` is always `peer` and `links` is empty in this slice.

Acceptance: two synthetic sessions register; one sends a message and the
other reads it; a worktree lease held by A refuses B until release; an expired
lease is reported and can be taken over; a merge lease is exclusive; no
cross-mesh message/lease is visible.

### B2 — links, dynamic sub/peer/super and delegation

Add link and delegation records plus `mesh link request|accept|decline|revoke|
list|show` and `mesh delegation grant|revoke|list|show`:

- reciprocal role derivation after `accept`;
- pending/active/declined/revoked/expired/stale states and TTL/stale reporting;
- idempotent request/accept/revoke and `link_conflict` for incompatible
  concurrent requests;
- a link never confers lease/mailbox/merge authority; delegation is separate,
  scoped, expiring and max-use bounded;
- `role` remains a derived display value; a session with no link is `peer`.

Acceptance: A requests `sub` of B, B accepts, both registrations show the
reciprocal relation; A cannot release B's lease or read B's mailbox without a
delegation; either side can revoke; a stale link is not `dead`; a second
incompatible link is refused; cross-mesh links are invisible.

### C — harness and read-only model awareness (layer 0–1)

- `ozagia agent` / eval harness registers the session in the mesh, runs a
  heartbeat while the session is alive, unregisters on exit and passes only a
  compact topology/lease summary to the model prompt context. Default is
  layer 1: awareness only, no send/lease tool.
- Task/eval records include mesh session id, peer/lease events and whether a
  worktree/merge lease was held; cleanup releases on every exit path.
- Conflict warnings can cite a live worktree/merge lease rather than only
  post-hoc edit history.
- Read-only CLI/MCP/status surfaces list self, peers, subs/supers, leases,
  queues and unread counts.

### D — model coordination, supervision and safety (layers 2–3)

- Opt-in per-session `mesh_model=coordinate|supervise` (or equivalent host
  capability). Default `none`/read-only.
- Layer 2 native tool/CLI: bounded `mesh send`, `lease request/renew/release`,
  `worktree request/offer`, `merge request`; rate- and queue-limited; audited;
  no approve/takeover/admin.
- Layer 3: mutual sub/super handshake, delegation records and descendant
  topology. A super cannot release a sub's lease or read its mailbox without
  an explicit delegation record; a sub can decline/revoke.
- MCP: read-only `dev_mesh_status`/`dev_mesh_inbox`; no send/lease/approve by
  default. A host-confirmed surface is required before any MCP write.
- Optional daemon `mesh` op; filesystem remains the fallback.
- TUI/status shows topology, live peers, active leases, queued requests,
  stale sessions, orphaned leases and model exposure level.
- Safety: local-only, mesh id opt-in, bounded rate/queue, redaction, no
  credentials, no target execution, no model self-approval, no conscription.
  A merge lease is advisory; existing policy/approval remains authoritative.

## Safety rules

- Mesh is local coordination; do not imply distributed consensus or security
  enforcement.
- Leases are advisory. A process that ignores the mesh can still edit/merge;
  existing ownership warnings and policy checks remain the guardrail.
- No credentials, prompts, source text or large payloads in messages or
  leases. Redact before writing.
- No automatic deletion of expired messages/leases until TTL policy is
  explicit; report orphans.
- A model may request a lease or send a message only through an opt-in,
  rate-limited surface; it cannot approve, take over or merge outside policy.
- A stale session is not a dead one until heartbeat/PID checks agree; prefer
  reporting over takeover.
- Never scan other repositories or users; scope to one state dir and mesh id.

## Acceptance examples

1. Two sessions register; A sends B a `worktree` request; B accepts and leases
   its worktree; A sees the holder and queue.
2. A holds a merge lease; B's merge request is queued. A merges and releases;
   B is promoted and can merge.
3. A crashes while holding a worktree lease; after heartbeat/TTL expiry the
   lease is `expired`, A is shown stale, and B can take it over with a recorded
   `took_over` event.
4. A lease/message from another mesh id or repository is invisible.
5. A message body containing a secret canary is redacted and the canary never
   appears in state, logs, status or another session's inbox.
