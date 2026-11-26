# Session mesh

`ozagia mesh` lets sessions that already work in one repository see each other,
leave bounded messages and coordinate advisory leases.  It is local
coordination, not a leader election, a security boundary or a distributed
system.  A process that ignores the mesh can still edit or merge; existing
worktree, commit, merge and approval policy remains authoritative.

This document describes the filesystem layer (layer 0), the read-only
awareness it enables (layer 1), and the link/delegation handshake.  Model
tools, a read-only MCP surface, the optional daemon operation and harness
registration are later slices.

## Scope

Everything is scoped to one repository and one mesh id:

- `repo_id` is `r_` plus the first 16 hex characters of the SHA-256 of the
  repository's real git common directory (or the real root when it is not a
  git checkout).  Every record stores it.
- `mesh_id` comes from `[mesh] id` in `.ozagia.toml` or
  `ozagia/config.toml`, or from `OZAGIA_MESH_ID`; the default is `default`.
  It must match `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`.
- Every session, message, lease, link and delegation record stores both ids.  A
  read uses a record only when both match.  A record or state directory from
  another repository or mesh id is reported as a scope mismatch, never merged.

State lives under:

```text
<state dir>/mesh/<mesh_id>/
  mesh.json       scope guard (schema, repo_id, mesh_id)
  sessions/       one registration per session key
  leases/         one record per lease; .lock serializes mutations
  mail/<sk>/      one envelope per delivered message
  links/          one record per 1:1 link handshake
  delegations/    one record per bounded delegation
  audit/          bounded redacted events (never message bodies)
  rate/           reserved for rate limits
```

Directories are `0700` and files `0600`.  Writes are atomic
(`mkstemp`/`fsync`/`os.replace`).  Lease, link and delegation mutations take a
short exclusive `flock` on `leases/.lock` with a five-second timeout; the
single lock serialises the cross-record invariants (a delegation is only valid
while its link is active, and releasing a lease consumes a delegation).  No
symlinked state or mesh directory is followed, and no path outside this one
state directory and mesh id is scanned.

A session key is:

```text
sk = "sk_" + sha256(repo_id + "\0" + mesh_id + "\0" + harness + "\0" + session)[:24]
```

## Identity and liveness

A registration records the harness, session id, PID, PID start time, boot id,
started/heartbeat times, exposure, worktree, branch and capabilities.  The
stored record keeps `role: "peer"` as a placeholder; every view derives the
display role from **active accepted links** instead (see below).  A session
with no active link is `peer`.

Liveness is deliberately conservative:

- an explicit `unregister` makes the session `ended`;
- a recorded boot id that differs from the current boot id makes it `foreign`;
- fresh heartbeat plus a matching PID start time is `live`;
- an old heartbeat with a matching PID is `stale`, however old it gets;
- a dead or reused PID with a heartbeat older than the dead threshold is
  `dead`;
- contradictory or unavailable evidence is `stale` or `unknown`, never
  `dead`.

On Linux the checks read `/proc/<pid>/stat` (starttime) and
`/proc/sys/kernel/random/boot_id`.  Elsewhere `os.kill` is only a fallback:
a successful probe reports `unknown`, not live.  The thresholds are 15 seconds
for a heartbeat, 90 seconds for stale and 300 seconds for dead.

Registering the same live identity again is idempotent.  A different PID or
start time is refused with `session_already_registered` while the old
registration is live or unknown; once it is stale, dead or ended, the new
process may register.  `unregister` marks the record ended and does not delete
it.  `--release-leases` also releases that session's active leases.

## Messages

Every message is one bounded JSON envelope with a schema, scope, `msg_<16hex>`
id, sender, recipient (or broadcast), kind, subject, body, reply/lease/evidence
references, timestamps, expiry and redactions.  Bodies are capped at 1024
characters and subjects at 120.  Control characters are rejected.  Inline
`secret=` references are refused.

Subject and body are redacted before writing with the security rules and, when
it is importable, the secrets detector.  Bodies are marked `untrusted` and are
never copied into metadata, status, peer listings or audit records.  Use
references (paths, keys, lease ids) rather than pasting source text.

Delivery is a per-session mailbox under `mail/<sk>/`.  A direct send requires a
live peer.  A broadcast is copied only to live peers in the same scope; this
slice still uses the whole mesh as the broadcast set, not the link graph, so a
broadcast does not imply a link or any authority.  Nothing is auto-replied.
Mailbox limits are 100 messages, 256 KiB and 50 unread; a send that would
exceed one is refused with `mailbox_full` rather than dropping unread mail.

`inbox` lists metadata only.  `read` returns the body and marks the message
read unless `--peek` is given.  `ack` marks it read and returns the unread
count.  `prune --expired` or `prune --delivered-before S` is an explicit plan
unless `--apply` is given, and it never deletes unread messages.

## Leases

A lease is a durable JSON record, not a held `flock`.  It names a resource
(`worktree`, `branch`, `merge`, `path`, `grant` or `other`), a mode (`read`,
`write` or `merge`), a holder, TTL, expiry, renewal count, FIFO queue, takeover
record and bounded history.

Resources are normalized:

```text
worktree:<realpath>
branch:<branch>
merge:<branch>
path:<root-relative path>
grant:<label>
other:<namespace:label>
```

`write` and `merge` are exclusive.  An incoming write or merge conflicts with
any active lease on the same resource, including reads; read leases coexist
with reads and conflict only with write/merge.  A conflicting request is
queued (bounded at 32) and returns its position.  Repeating the same request
returns the existing holder or position.  Nothing is silently stolen.

Effective state is computed on reads and never written by a read:

- `released` and `revoked` are terminal;
- `expired` means the TTL passed or the holder ended or died;
- `stale` means the holder is stale or unknown;
- `active` is the remaining live case.

`renew` extends a held lease up to the per-lease TTL, a maximum renewal count
and a maximum lifetime; an effectively expired lease cannot be renewed.
`release` is holder-only, leaves the record in place and promotes the first
live queued request.  Stale or dead queued requests are skipped with a history
entry.  The promoted request receives a new lease id; the released record and
its history remain.

`takeover` is explicitly administrative: it needs `--confirm` and a reason,
works only on an effectively expired lease, refuses a live holder, re-checks
under the lock and records the old holder in `took_over`.  Leases are never
deleted by expiry or takeover.

`lease list` and `lease status` report effective state, holder, expiry and
queue.  `mesh status` adds active, queued, expired, orphaned, stale and
released lease counts, plus compact links (id, peer, relation, state, expiry)
and delegations (id, actions, scope, state, uses remaining, expiry).  It never
includes message bodies, credentials or source text.

## Links and derived roles

A link is one durable, bidirectional edge between two sessions:

```json
{
  "schema": 1, "repo_id": "r_...", "mesh_id": "default",
  "link_id": "lnk_...",
  "from": {"harness": "opencode", "session": "a", "key": "sk_..."},
  "to":   {"harness": "opencode", "session": "b", "key": "sk_..."},
  "relation": "sub",
  "state": "pending",
  "created_at": "...", "accepted_at": null, "expires_at": null,
  "revoked_at": null, "ttl_s": 3600, "capabilities": [],
  "reason": "", "idempotency_key": null, "history": []
}
```

`relation` is written from the requester's (`from`) perspective:

| requester relation | requester sees | target sees |
| --- | --- | --- |
| `sub` | subordinate to the target | `super` |
| `super` | superior to the target | `sub` |
| `peer` | symmetric | `peer` |

A link is a 1:1 edge, not a hierarchy.  Several edges form a forest or DAG;
no leader is elected and no lease authority flows along an edge.

### States and expiry

Stored states are `pending`, `active`, `declined` and `revoked`.  Effective
states add `expired` and `stale` and are computed on every read; a read never
rewrites the record.

- `pending`: requested, waiting for the target to accept.
- `active`: accepted by the target; `accepted_at` is set.
- `declined`: the target declined; terminal.
- `revoked`: either side revoked; terminal.
- `expired`: the pending TTL (`created_at + ttl_s`) or the accepted
  `expires_at` passed.
- `stale`: an endpoint is stale, dead, ended, foreign or missing.  A stale
  link is reported `stale`, never `dead`; it is not usable and does not
  contribute to the derived role while stale.  Expired, declined and revoked
  links are closed and require a new `request`/`accept`; a stale link whose
  peer becomes live again within its TTL is reported active again.  Nothing is
  silently rewritten by a read.

A request's TTL defaults to one hour and is bounded at 24 hours.  Acceptance
sets `expires_at = accepted_at + ttl_s`.  A pending request that expires is
still a `pending` record; it is simply reported `expired`.  Expired, declined
and revoked records are kept as history.

`link request --to H:S --relation peer|sub|super [--ttl S] [--reason R]
[--idempotency-key K]` requires a live, registered, same-mesh target that is
not the caller.  Repeating an identical pending or active request returns the
existing link.  Reusing an idempotency key with different parameters is refused
with `id_conflict`.  A request for a different relation on the same pair while
a link is pending or active is refused with `link_conflict`.

`link accept LINK_ID` is target-only and idempotent.  `link decline LINK_ID` is
target-only and ends a pending request.  `link revoke LINK_ID` is a unilateral
exit available to either endpoint and is idempotent.  `link list` and
`link show` include the peer, the reciprocal relation, effective state, expiry,
peer liveness and bounded history.

### Role is derived, never granted

`role` on a registration is a display value derived only from **active**
accepted links, as seen by that session:

- no active link, or only `peer` links: `peer`;
- at least one active `sub` and no active `super`: `sub`;
- at least one active `super` and no active `sub`: `super`;
- both active `sub` and `super`: `mixed`.

Registration never sets this value from input and linking never changes the
stored placeholder.  A role is coordination metadata only: it is not an
authorization and never releases a lease, exposes a mailbox, authorizes a merge
or permits takeover.  Cross-mesh and cross-repository links are invisible.

## Delegations

A delegation is a separate, bounded record; a link alone is never enough:

```json
{
  "schema": 1, "repo_id": "r_...", "mesh_id": "default",
  "delegation_id": "dlg_...",
  "grantor": {"harness": "opencode", "session": "a", "key": "sk_..."},
  "grantee": {"harness": "opencode", "session": "b", "key": "sk_..."},
  "link_id": "lnk_...",
  "actions": ["lease_release"],
  "lease_id": "lease_...", "resource": "branch:main", "mailbox": false,
  "state": "active",
  "created_at": "...", "expires_at": "...", "ttl_s": 3600,
  "max_uses": 1, "used": 0, "reason": "", "history": []
}
```

Actions are a closed set:

- `lease_release` is scoped to one exact `lease_id`.  The grantor must
  currently hold that lease (active or stale); the grantee may release only
  that lease.
- `mailbox_read` is scoped to the grantor's own mailbox.  It requires the
  `mailbox` flag and lets the grantee list metadata and read messages from the
  grantor's mailbox.  Metadata listing does not consume a use; reading a
  message does.
- `merge_on_behalf` is a reference/handoff only.  It is never consulted by
  merge or authority code and never authorizes a git merge, a lease release or
  a takeover.

Only the resource owner/holder can grant.  The grantee must have an **active**
link with the grantor; a pending, expired, stale, revoked or absent link is
refused (`link_pending`, `link_expired`, `link_stale`, `not_linked`).  A role
alone never satisfies a check.

Expiry and `max_uses` are enforced on use.  A delegation is effectively
`expired` when its `expires_at` passes, when `used >= max_uses`, or when its
link is no longer active.  `delegation revoke` is grantor-only and idempotent.
Revocation and exhaustion do not undo a lease already released or a message
already read.  Effective state and history are reported; the stored record is
not rewritten by a read.

`lease release LEASE_ID --delegation DLG` allows a grantee with a valid
`lease_release` delegation for that exact lease.  `inbox --delegation DLG` and
`read MSG_ID --delegation DLG` allow a grantee with `mailbox_read`.  Without a
delegation, only the holder may release and only the mailbox owner may read.

## Worktrees and merges

`mesh worktree request --branch B [--path P]` discovers existing git
worktrees read-only, requires a matching existing worktree, and acquires write
leases on the worktree and its branch.  It never creates or removes a
worktree.

`mesh worktree offer` validates that a discovered worktree is readable.  With
`--lease` it also acquires read leases; with `--to` or `--broadcast` it sends an
`offer` message.

`mesh merge request --target BRANCH|PATH` normalizes the target to a branch and
acquires or queues the exclusive `merge:<branch>` lease.  It never runs
`git merge` and never calls the merge policy check.  The result is advisory:
the holder still needs the repository's normal merge policy and approvals.

## Commands

```text
ozagia mesh status|peers [--json] [--all] [--strict]
ozagia mesh register --harness H --session S [--pid N] [--worktree P]
                     [--branch B] [--exposure none|read|coordinate|supervise]
ozagia mesh unregister [--harness H --session S] [--release-leases]
ozagia mesh heartbeat [--harness H --session S]
ozagia mesh send (--to H:S | --broadcast) --kind K --subject S
                 [--body TEXT | --body-file F] [--in-reply-to ID]
                 [--lease ID] [--ttl S]
ozagia mesh inbox [--unread] [--delegation ID] [--json]
ozagia mesh read MSG_ID [--peek] [--delegation ID] [--json]
ozagia mesh ack MSG_ID [--json]
ozagia mesh lease list|status [LEASE_ID] [--json]
ozagia mesh lease acquire --kind K --id RESOURCE --mode read|write|merge
ozagia mesh lease renew|release LEASE_ID [--delegation ID]
ozagia mesh lease takeover LEASE_ID --confirm --reason TEXT
ozagia mesh worktree request --branch B [--path P]
ozagia mesh worktree offer [--branch B] [--path P] [--lease]
ozagia mesh merge request --target BRANCH|PATH
ozagia mesh link request --to H:S --relation peer|sub|super
                         [--ttl S] [--reason R] [--idempotency-key K]
ozagia mesh link accept|decline|revoke LINK_ID [--reason R]
ozagia mesh link list|show [LINK_ID]
ozagia mesh delegation grant --to H:S
                              --action lease_release|mailbox_read|merge_on_behalf
                              [--lease ID] [--mailbox] [--ttl S]
                              [--max-uses N] [--reason R]
ozagia mesh delegation revoke DELEGATION_ID [--reason R]
ozagia mesh delegation list|show [DELEGATION_ID]
ozagia mesh prune (--expired | --delivered-before S) [--apply]
```

Identity defaults to `OZAGIA_MESH_HARNESS` and `OZAGIA_MESH_SESSION`.  Reads
such as `status`, `peers`, `lease list/status`, `link list/show` and
`delegation list/show` need no identity or exposure.  Registration, heartbeat,
unregister, read, inbox and ack need an identity.  Sends, lease
acquire/renew/release/takeover, worktree request/offer mutations, merge
requests, link request/accept/decline/revoke and delegation grant/revoke also
require the registered session's exposure to be `coordinate` (or `supervise`).
`register` defaults to `none`.

Exit status is `0` for success, `1` for a valid refusal (queued, held, expired
holder, unknown peer, mailbox full, missing coordinate exposure, link conflict
or delegation denial) and `2` for usage, configuration, IO, lock or schema
errors.  `--strict` on `status` or `peers` returns `1` when a stale, dead,
foreign or orphaned entry is present.

Link and delegation refusals use stable codes, including `unknown_link`,
`unknown_delegation`, `not_target`, `not_party`, `not_grantor`, `not_linked`,
`link_pending`, `link_conflict`, `link_expired`, `link_stale`, `link_closed`,
`self_link`, `self_delegation`, `peer_not_live`, `id_conflict`,
`delegation_denied`, `delegation_expired`, `delegation_used` and
`delegation_revoked`.

## Layers and what comes next

Layer 0 is this filesystem registry: sessions, messages, leases, links,
delegations, worktree and merge requests.  Layer 1 is read-only awareness:
status, peer, queue, link/role and unread counts in a prompt or a read-only
surface.  Neither layer grants authority.

B2 is implemented: mutual link handshakes, derived `peer`/`sub`/`super`/`mixed`
roles, bounded delegation records and the `link`/`delegation` commands.
Link-scoped broadcasts, model-facing summaries and tool surfaces are later
slices.

The next slices add:

- **C:** harness registration, periodic heartbeats, cleanup on every exit path
  and a compact topology/lease summary in model context; read-only MCP and
  status surfaces.
- **D:** opt-in model coordination and supervision tools, rate limiting and
  audit for layer 2/3, a read-only MCP inbox/status surface, and an optional
  daemon `mesh` operation.  Model tools still cannot approve a takeover, merge
  exception or administrative grant.

The merge lease remains advisory in every layer.  Existing policy, approvals
and the worktree/commit/merge gates decide whether a merge is allowed.
