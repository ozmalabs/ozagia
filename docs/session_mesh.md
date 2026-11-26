# Session mesh

`ozagia mesh` lets sessions that already work in one repository see each other,
leave bounded messages and coordinate advisory leases.  It is local
coordination, not a leader election, a security boundary or a distributed
system.  A process that ignores the mesh can still edit or merge; existing
worktree, commit, merge and approval policy remains authoritative.

This document describes the filesystem layer (layer 0) and the read-only
awareness it enables (layer 1).  Model tools, a read-only MCP surface, the
optional daemon operation and harness registration are later slices.

## Scope

Everything is scoped to one repository and one mesh id:

- `repo_id` is `r_` plus the first 16 hex characters of the SHA-256 of the
  repository's real git common directory (or the real root when it is not a
  git checkout).  Every record stores it.
- `mesh_id` comes from `[mesh] id` in `.ozagia.toml` or
  `ozagia/config.toml`, or from `OZAGIA_MESH_ID`; the default is `default`.
  It must match `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`.
- Every session, message and lease records both ids.  A read uses a record
  only when both match.  A record or state directory from another repository
  or mesh id is reported as a scope mismatch, never merged.

State lives under:

```text
<state dir>/mesh/<mesh_id>/
  mesh.json       scope guard (schema, repo_id, mesh_id)
  sessions/       one registration per session key
  leases/         one record per lease; .lock serializes mutations
  mail/<sk>/      one envelope per delivered message
  links/          reserved for the link handshake
  audit/          bounded redacted events (never message bodies)
  rate/           reserved for rate limits
```

Directories are `0700` and files `0600`.  Writes are atomic
(`mkstemp`/`fsync`/`os.replace`).  Lease mutations take a short exclusive
`flock` on `leases/.lock` with a five-second timeout.  No symlinked state or
mesh directory is followed, and no path outside this one state directory and
mesh id is scanned.

A session key is:

```text
sk = "sk_" + sha256(repo_id + "\0" + mesh_id + "\0" + harness + "\0" + session)[:24]
```

## Identity and liveness

A registration records the harness, session id, PID, PID start time, boot id,
started/heartbeat times, exposure, worktree, branch, role and links.  The role
is always `peer` in this slice and links are empty.

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
live peer.  A broadcast is copied only to live peers in the same scope; in this
slice there are no links yet, so "same mesh" is the broadcast set.  Nothing is
auto-replied.  Mailbox limits are 100 messages, 256 KiB and 50 unread; a send
that would exceed one is refused with `mailbox_full` rather than dropping
unread mail.

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
released counts.

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
ozagia mesh inbox [--unread] [--json]
ozagia mesh read MSG_ID [--peek] [--json]
ozagia mesh ack MSG_ID [--json]
ozagia mesh lease list|status [LEASE_ID] [--json]
ozagia mesh lease acquire --kind K --id RESOURCE --mode read|write|merge
ozagia mesh lease renew|release LEASE_ID
ozagia mesh lease takeover LEASE_ID --confirm --reason TEXT
ozagia mesh worktree request --branch B [--path P]
ozagia mesh worktree offer [--branch B] [--path P] [--lease]
ozagia mesh merge request --target BRANCH|PATH
ozagia mesh prune (--expired | --delivered-before S) [--apply]
```

Identity defaults to `OZAGIA_MESH_HARNESS` and `OZAGIA_MESH_SESSION`.  Reads
such as `status`, `peers` and `lease list/status` need no identity or exposure.
Registration, heartbeat, unregister, read, inbox and ack need an identity.
Sends, lease acquire/renew/release/takeover, worktree request/offer mutations
and merge requests also require the registered session's exposure to be
`coordinate` (or `supervise`).  `register` defaults to `none`.

Exit status is `0` for success, `1` for a valid refusal (queued, held, expired
holder, unknown peer, mailbox full, missing coordinate exposure) and `2` for
usage, configuration, IO, lock or schema errors.  `--strict` on `status` or
`peers` returns `1` when a stale, dead, foreign or orphaned entry is present.

## Layers and what comes next

Layer 0 is this filesystem registry: sessions, messages, leases, worktree and
merge requests.  Layer 1 is read-only awareness: status, peer, queue and unread
counts in the prompt or a read-only surface.  Neither layer grants authority.

The next slices add:

- **B2:** mutual link handshakes, roles (`peer`/`sub`/`super`), bounded
  delegation records, and link-scoped broadcasts.  `links/` and the empty
  role/link fields are reserved for this.
- **C:** harness registration, periodic heartbeats, cleanup on every exit path
  and a compact topology/lease summary in model context; read-only MCP and
  status surfaces.
- **D:** opt-in model coordination and supervision tools, rate limiting and
  audit for layer 2/3, a read-only MCP inbox/status surface, and an optional
  daemon `mesh` operation.  Model tools still cannot approve a takeover, merge
  exception or administrative grant.

The merge lease remains advisory in every layer.  Existing policy, approvals
and the worktree/commit/merge gates decide whether a merge is allowed.
