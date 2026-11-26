# Session mesh

`ozagia mesh` lets sessions that already work in one repository see each other,
leave bounded messages and coordinate advisory leases.  It is local
coordination, not a leader election, a security boundary or a distributed
system.  A process that ignores the mesh can still edit or merge; existing
worktree, commit, merge and approval policy remains authoritative.

This document describes the filesystem layer (layer 0), the read-only
awareness it enables (layer 1), the link/delegation handshake, the harness
registration that exposes a bounded summary to the model, and the opt-in
layer-2/3 model coordination and supervision tool.  The Ozma bridge is a later
slice.

## Scope

Everything is scoped to one repository and one mesh id:

- `repo_id` is `r_` plus the first 16 hex characters of the SHA-256 of the
  repository's real git common directory (or the real root when it is not a
  git checkout).  Every record stores it.
- `mesh_id` comes from `[mesh] id` in `.ozagia.toml` or
  `ozagia/config.toml`, or from `OZAGIA_MESH_ID`; the default is `default`.
  It must match `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`.
- Joining the mesh is opt-in: `[mesh] enabled` defaults to false, and
  `OZAGIA_MESH=1` (or true/yes/on) enables it for a process.  The `ozagia
  agent` CLI adds `--mesh` / `--no-mesh`; an explicit flag wins over the
  configuration and the environment.  `[mesh] model` (or
  `OZAGIA_MESH_MODEL`, or `--mesh-model`) chooses the model exposure:
  `none`, `read` (the default), `coordinate` or `supervise`.  The explicit
  CLI flag wins over the environment, which wins over the file.  Only the
  host sets it; a model request or tool argument cannot raise it.  Unknown
  `[mesh]` keys, a non-boolean `enabled` and an unknown model value are
  configuration errors.  Reads such as `status`, `peers` and `lease list`
  remain available whether or not joining is enabled.
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
  rate/           reserved for rate limits (the daemon keeps bounded
                  counters in memory)
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

## Harness registration and read-only awareness

`ozagia agent` (and `run_agent(..., mesh=True)`) can register its run as a
mesh participant.  The identity is deliberately not an OpenCode `ses_...`
id:

```text
harness  = "opencode-runner"
session  = "run:<run root name>"
pid      = the harness process
worktree = the checkout the agent runs in (a clone when --clone is used)
branch   = the checked-out branch when git can name it cheaply
exposure = none | read | coordinate | supervise ([mesh] model; default read)
source   = "host"
```

The exposure is the host's opt-in.  `none` registers with no summary and no
tool, `read` keeps the bounded metadata summary, and `coordinate`/`supervise`
also give the OpenCode process `OZAGIA_MESH_MODEL`; its plugin registers the
actual `c.sessionID` with the daemon and exposes the bounded `ozagia_mesh`
tool.  `--mesh-model` wins over `OZAGIA_MESH_MODEL`, which wins over
`[mesh] model`.  When mesh is disabled no model tool or model environment is
passed, even if a model value is configured.

Registration always writes under the **source** repository's state directory,
so a clone and its source meet in one mesh; the clone's own `.ozagia/mesh` is
not used.  While the run is alive a heartbeat refreshes the registration at
the mesh interval (15 seconds by default).  On every exit path - normal exit,
timeout, error, Ctrl-C - the harness releases the leases held by that identity,
cancels its queued requests and unregisters the session.  Mesh problems fail
open: a registration, heartbeat or summary failure is reported in the result
(`mesh_warning`, `mesh_cleanup_errors`) and never aborts the agent run; a
failed registration is reported as unregistered, never as a success.

The run result carries `mesh_enabled`, `mesh_model`, `mesh_id`,
`mesh_session`, `mesh_key`, `mesh_exposure`, `mesh_registered`,
`mesh_peer_count`, `mesh_lease_ids`, `mesh_warning` and
`mesh_cleanup_errors`.

### Summary contract

Only when the registration succeeded, the OpenCode process gets
`OZAGIA_MESH_SUMMARY`: one bounded (at most 700 characters) metadata string
with:

- self key, harness/session, derived role and exposure;
- live and stale peer counts (the session itself is not a peer);
- at most four link summaries (peer, relation, state);
- at most four active/stale leases (resource kind/id, mode, expiry, queue
  length, and whether this session holds it);
- the session's unread message count.

It contains no message bodies or subjects, no credentials, no source text and
no paths beyond the registered worktree/branch and lease resources.  The
OpenCode plugin appends one `[ozagia mesh]` block to the model request's
system context, marked as untrusted metadata; it reads no mesh file for it.
At `read` exposure it registers no tool; at `coordinate`/`supervise` it also
registers the layer-2/3 tool described below.  A missing or oversized value adds
nothing.  The summary is a snapshot taken at session start; the filesystem
registry remains the source of truth.

### Status and MCP (read-only)

`ozagia status` adds a `mesh` section with the same metadata: counts, self,
peers, links/roles, leases, delegations and unread.  It is absent when neither
mesh state nor `[mesh]` configuration exists, and `unknown` when the state
cannot be read.  It never creates `.ozagia/mesh`, starts a daemon or opens a
network connection.

The full/coding MCP surface exposes `dev_mesh_status` and `dev_mesh_inbox`.
The first returns the same bounded metadata and resolves self from
`OZAGIA_MESH_HARNESS`/`OZAGIA_MESH_SESSION` when set.  The second returns inbox
rows only (`id`, `from`, `kind`, `created`, `expires`, `read`, `acked`); it
never returns a subject or body and never acks.  There is deliberately no MCP
send, lease, release, claim or approve tool, and the graph/engine surfaces do
not list either tool.

## Layer 2/3: model coordination and supervision tool

`coordinate` and `supervise` are host opt-ins.  The harness passes
`OZAGIA_MESH_MODEL` to OpenCode, and only then does the plugin register the
actual `c.sessionID` (`harness: "opencode"`) with the daemon and add the native
`ozagia_mesh` tool.  At `read` there is no tool.  When mesh is disabled, or the
permission API is absent, there is no registration and no tool.  The model
cannot raise its own exposure: the tool has no register or exposure input.

The tool sends `{op: "mesh", action, ...}` to the daemon.  The daemon ignores
any caller-supplied actor, uses the message's `harness`/`session`, accepts
only `opencode` or `opencode-runner`, and verifies the stored registration.
Actions:

| action | meaning | exposure |
| --- | --- | --- |
| `status` | compact self, live/stale peers, active/stale leases, links and unread counts | `read`+ |
| `send` | one bounded message to `to` or `broadcast`; no bodies in the answer | `coordinate`+ |
| `lease_acquire` | `resource` as `kind:id`, `mode`, `ttl_s`, `reason` | `coordinate`+ |
| `lease_renew` / `lease_release` | an owned `lease_id` | `coordinate`+ |
| `worktree_request` / `worktree_offer` | an existing worktree by `branch`/`path` | `coordinate`+ |
| `merge_request` | queue for the advisory merge lease on `target` | `coordinate`+ |
| `link_request` | `to`, `relation` (`peer`/`sub`/`super`), optional `ttl_s`, `reason`, `idempotency_key` | `supervise` |
| `link_accept` / `link_decline` / `link_revoke` | `link_id`; decline and revoke accept a `reason` | `supervise` |
| `link_list` / `link_show` | compact links (id, peer, relation, state, expiry); no history or reason | `supervise` |
| `delegation_grant` | `to`, `actions` (`lease_release`/`mailbox_read`/`merge_on_behalf`), exact `lease_id` or `mailbox`, `ttl_s`, `max_uses`, `reason` | `supervise` |
| `delegation_revoke` | `delegation_id` and an optional `reason` | `supervise` |
| `delegation_list` / `delegation_show` | compact delegations (id, parties, actions, scope, state, uses, expiry) | `supervise` |
| `topology` | compact self/role, peers, links, delegations and session/link/delegation/lease counts | `supervise` |

The daemon also accepts lifecycle actions `register` and `heartbeat` from the
plugin; these are not in the model tool's enum.  `coordinate` is unchanged:
link, delegation and topology actions are in the static tool schema but the
daemon refuses them with `not_permitted`, so a coordinate session fails closed
instead of silently hiding them.  The daemon never exposes `approve`,
`takeover`, `lease_takeover`, `merge_apply`, `admin`, `prune`, `unregister` or
worktree creation/removal.  A merge lease remains advisory, and a
`merge_on_behalf` delegation is a reference only: no link, delegation or merge
policy code consults it, so it never authorizes a git merge, a lease release or
a takeover.  The normal merge policy and approvals still decide whether a merge
may happen.

A link is mutual: a request is only usable after the target accepts, either
side can revoke, and a revoked or expired link closes without affecting leases
already held.  A delegation is separate and bounded: only the lease holder or
mailbox owner grants it, only to an actively linked session, for an exact
lease/mailbox scope, with a TTL and `max_uses`.  Role alone never satisfies a
check, and neither side can delegate another session's lease or mailbox.

Limits are per actor and action group, in the daemon's bounded in-memory
counters: 30 messages, 20 lease actions, 10 worktree/merge requests and 10
link/delegation actions per five minutes.  List/show/topology reads do not
consume the supervision bucket.  A refusal is `rate_limited` with `retry_s`;
other stable codes include `not_permitted`, `unknown_session`,
`unknown_lease`, `unknown_link`, `unknown_delegation`, `link_pending`,
`link_conflict`, `link_expired`, `link_stale`, `not_linked`, `not_grantor`,
`delegation_denied`, `delegation_expired`, `delegation_used`,
`self_link`, `self_delegation`, `peer_not_live`, `live_holder`, `queue_full`,
`mailbox_full`, `invalid_action`, `invalid_resource`, `invalid_exposure`,
`invalid_lease_id` and `session_not_live`.  Each successful action appends one
mesh audit record with the actor, action and link/delegation/message ids only;
message bodies, subjects and reasons never reach the answer, journal, status,
prompt or audit.  All state stays under `<state>/mesh/<mesh_id>/`; no network
is used.  The plugin heartbeats on tool calls (and with an unref'd bounded
timer) and stops its timer on teardown; a session that disappears is reported
stale and then dead, never silently deleted.


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

`ozagia agent --mesh|--no-mesh` controls the harness registration.
`--mesh-model none|read|coordinate|supervise` sets the model exposure; only
`coordinate`/`supervise` add the native tool.  The `ozagia mesh` commands
remain the explicit layer-0/2/3 surface; their mutation commands require
`coordinate` (or `supervise`) exposure, and the native tool gates its
supervision actions to `supervise`.

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

Stage C is implemented for layer 1: `ozagia agent --mesh` registration,
heartbeat and cleanup, the bounded `OZAGIA_MESH_SUMMARY` prompt block, the
status `mesh` section and the read-only `dev_mesh_status`/`dev_mesh_inbox`
MCP tools.

Layer 2 is implemented for `coordinate`/`supervise`: the daemon `mesh` op, the
host-only `--mesh-model`/`OZAGIA_MESH_MODEL`/`[mesh] model` opt-in, the native
`ozagia_mesh` tool, rate limits and audit.  Layer 3 is implemented for
`supervise`: mutual link request/accept/decline/revoke, bounded delegation
grant/revoke/list/show and a compact topology action, all through the same
tool.  A model still cannot approve a takeover, merge exception or
administrative grant, and no link or delegation carries merge authority.

The next slice adds:

- The [Ozma mesh bridge](plans/2026-11-26_ozma_mesh_bridge.md), which maps the
  local records to Ozma coordination without making remote claims
  authoritative.

The merge lease remains advisory in every layer.  Existing policy, approvals
and the worktree/commit/merge gates decide whether a merge is allowed.
