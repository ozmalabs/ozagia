# Ozma mesh bridge for agent flights

Status: planned (2026-11-26). Follows the local
[session mesh](2026-11-26_session_mesh.md). The local mesh is the source of
truth; this bridge lets a **flight** of Ozagia agents collaborate with Ozma
peers through the documented `COORDINATION` capability and, where needed, the
A2A adapter.

## Problem

The local mesh coordinates sessions in one repository/state directory. A
flight of agents may also need to talk to Ozma peers, which have:

- stable agent identities and signed Agent Cards;
- typed peer messages (`request`, `response`, `ack`, `block`, `unblock`,
  `handoff`, `claim`, `release`, `heads-up`, `status`);
- channels and subscriptions with priority floors;
- advisory claims over files/subtrees/areas;
- blocker-graph edges;
- inbox delivery and optional message-triggered/scheduled wake modes;
- native Ozma transport or an A2A v1.0 interop adapter.

Duplicating that protocol in Ozagia would create two incompatible meshes.
Ignoring it would keep a flight local-only. The bridge must translate, not
reimplement.

## Goals

- Map local mesh sessions to Ozma agent identities without inventing an id.
- Exchange typed messages both ways with idempotency, TTL, priority and
  deduplication.
- Map local mesh leases to Ozma advisory `claim`/`release` scopes and back.
- Join channels/subscriptions and deliver a bounded inbox without duplicate
  or replay loops.
- Support a flight of agents with roles/work-units, handoffs and blockers,
  without electing a leader.
- Keep the local merge lease advisory; Ozma claims never authorize a merge.
- Be explicit about A2A translation loss and live conformance.

## Non-goals

- Implementing an Ozma server, shim, channel adapters or signed-card PKI.
- A hosted/global relay or cross-tenant discovery.
- Enforcing claims or leases; both remain advisory.
- Automatic message-triggered/scheduled wake-up without host approval and a
  budget.
- Carrying credentials, prompts, source text or large payloads in bridge
  messages.

## Conformance notes

The upstream Ozma specification in this checkout is documentation, not a
shipped shim or client. There is no A2A adapter, Agent Card verifier, channel
adapter or hosted endpoint here. The bridge must therefore:

- treat native Ozma as the normative transport, with A2A an explicit, lossy
  adapter;
- key every record by `(flight_id, ozma_message_id, direction)` and preserve
  the shim's message-id dedupe semantics;
- report `unknown`/`unverified`/`lossy` rather than imply delivery or
  conformance;
- not invent `agent:` ids, thread ids, priorities or permissions.

Documented gaps the bridge must not paper over:

| Area | Gap | Bridge rule |
| --- | --- | --- |
| Message priority | no required envelope/body field; only inbox/subscription floor | carry as an extension and mark `priority: extension` |
| `deliver_at` | example-only; `now` is outside the listed time grammar | accept as a compatibility literal, validate other values |
| Threads | no allocation semantics documented | preserve opaque `thread`; never invent one |
| `requires_ack` | appears at envelope and handoff-body level | canonical envelope field, report the body duplicate |
| Claim scope | table says files/subtree/area; example is YAML `file:` list | accept both, mark the shape used |
| Legacy message names | `review-request`/`question` remain in examples | map to `request{purpose=review|question}` |
| A2A loss | criteria flatten, claim scope ignored, blockers/subscriptions/priority floor/compliance lost | `lossy:true` plus named `loss_fields`; never claim a verified handoff under A2A |
| Card verification | no algorithm/procedure in the spec | host supplies id/card; unverified stays unverified |
| Wake header | `scheduled` is documented later than the header enum | accept the three-value grammar, host-approve scheduled |
| Ack timeout | default 60 s, missing ack treated as declined | import into the bridge and report `ack_missing` |
| Retention | workspace TTL 90 days, local caps stricter | local caps win |
| Rust `ozma-mesh` | WireGuard overlay/mDNS, not COORDINATION; `MeshNode`/advert has no role/link; controller is central | reuse only as an optional transport/diagnostics adapter; never derive agent role or authority from WG identity, static peers or cluster membership |

## Mapping

### Identity and flight

- Ozagia identity is `{harness, session, key}`. Ozma identity is an
  `agent:<role>:<workunit>` id with a signed Agent Card supplied by the host.
- The bridge stores a mapping record:
  `{ozagia_key, ozma_agent_id, card_fingerprint, scope, flight_id,
    verified_at, source}`. It never invents an Ozma id; without a host-provided
  id/card the session stays local-only.
- `flight_id` is the Ozma workspace/channel grouping (or the local mesh id when
  no workspace is provided). Membership is explicit; no leader is elected.
- Card signature verification is a host/trust decision. An unverified card is
  reported `unverified`, not silently trusted.

### Messages

| Local mesh | Ozma COORDINATION |
| --- | --- |
| `hello` | identity/card handshake metadata (not a peer message) |
| `request` | `request` (`purpose` general/review/question) |
| `accept`/`reject` | `response` or `ack` with `accepted`/`verdict` |
| `offer` | `response`/`handoff`/`heads-up` depending on context |
| `status` | `status` |
| `release` | `release` |
| `note` | `heads-up` |
| `block`/`unblock` (new kinds) | `block`/`unblock` |
| `handoff` (new kind) | `handoff` |
| `claim`/`lease` | `claim` with scope/reason/expiry |

- The bridge validates the Ozma typed body shape before sending; it does not
  send arbitrary prose as a typed body. Local free-form `body` maps to a
  bounded `response`/`heads-up` body with `body_trust: untrusted`.
- Preserve `id`, `in_reply_to`, `thread`, `refs`, `priority`, `deliver_at`,
  `requires_ack`, `compliance` and `channel`; unknown fields are rejected or
  reported, not dropped silently.
- Redact before egress and mark inbound bodies untrusted. Never merge inbound
  text into system/developer instructions. A peer message is content, not a
  command.
- Idempotency: `(flight_id, ozma_message_id, direction)` dedupe; replay of the
  same id is a no-op; conflicting content is `id_conflict`.

### Claims and leases

- Local `worktree`, `branch`, `path` and `grant` leases map to Ozma `claim`
  scopes (files/subtree/area) plus a local `lease_id` reference. `merge` maps
  to a claim on the target branch/worktree but remains a local advisory.
- Inbound Ozma claims are recorded as **remote claims** with a holder/card and
  expiry. They are not local leases and do not block local work; `status`
  surfaces a conflict.
- Local `release` maps to Ozma `release` (`in_reply_to` the claim id).
  Expiry is reported, not auto-deleted; a remote claim can be taken over only
  by the remote protocol's rules.
- A merge decision always goes through local worktree/commit/merge policy. An
  Ozma claim or a local merge lease is never an authorization.

### Channels, subscriptions, inbox

- Local broadcast maps to an Ozma channel (`#name` or adapter-backed
  `#slack:...`); the bridge never writes to an external channel without an
  explicit adapter scope.
- Subscriptions declare channels/workunits/events, `priority_floor` and
  optional message types. Inbound messages are filtered and bounded before
  delivery.
- The bridge merges the Ozma inbox with the local mailbox, dedupes by
  `ozma_message_id`, and preserves unread/read/ack state. Local metadata views
  omit bodies by default; a body fetch is explicit.
- Inbox retention/caps follow the local mailbox limits; the bridge cannot
  bypass them.

### Blocker graph and handoff

- Worktree/merge dependencies may map to `blocked-by`/`blocks` edges; a
  work-unit done event can emit an `unblock`. The edge is advisory, matching
  Ozma.
- A `handoff` carries workunit, accepting criteria, refs and completion state;
  the receiver acks. Handoff does not transfer credentials, leases or local
  policy authority.
- Flight roles (`implementer`, `reviewer`, etc.) are labels; no role confers
  lease/claim authority.

### Wake modes

- Default: `passive`. Local mesh and the bridge deliver inbox on the next turn.
- `message-triggered` and `scheduled` require explicit host approval, a declared
  budget and an agent card that declares the wake mode. No model can enable
  its own wake-up.
- A wake spawns a turn, not a new lease/merge authority. Budget and cost are
  recorded; overrun stops the flight, not the repository policy.

### Transport and A2A

- Native Ozma transport is preferred (typed grammar, Session Cache/WIRE).
  A2A v1.0 is an interop adapter; translation is lossy and marked
  `lossy: true` with the fields that could not be represented.
- The bridge talks only to a configured local/approved Ozma shim endpoint with
  host-supplied authentication. No hosted fallback, no implicit discovery, no
  automatic network on read-only status.
- Outbox/inbox are durable, bounded and idempotent; reconnection replays with
  the same ids. An offline queue is bounded and reported, never silently
  dropped.
- A bridge failure degrades the flight to local mesh; it does not block local
  work or pretend remote messages were delivered.

## Stages

### E — conformance inventory

Read the upstream `COORDINATION`, A2A adapter, agent-card, inbox, wake and
claim sections; record exact envelopes, required fields, lossy mappings,
transport/auth options and a fake-shim fixture. No edits.

### F — identity, config and read-only status

`[ozma_mesh]` config (owned section), default off:

```toml
[ozma_mesh]
enabled = false
mode = "off"                 # off | status | messages | full
transport = "native"         # native | a2a
shim_url = ""                # loopback/approved; empty means no network
allow_remote = false
auth_ref = ""                # secret://... only
flight_id = ""               # empty -> local mesh id
agent_id = ""                # host-provided; never generated
agent_scope = "workspace"    # workspace | user | global
card_path = ""
card_fingerprint = ""
verified_by = "host"         # host | none
wake_modes = ["passive"]
channels = []
subscriptions = []
priority_floor = 0.3
queue_max_messages = 200
queue_max_bytes = 262144
outbox_ttl_s = 86400
ack_timeout_s = 60
dedupe_window_s = 86400
message_rate = "60/5min"
```

- Identity mapping record under `<state>/mesh/ozma/identities/` keyed by
  `(flight_id, ozagia_key)`: Ozagia key, host-provided Ozma agent id, card
  fingerprint/ref, scope, verification state/source, transport, wake modes,
  channels, status.
- `ozagia mesh ozma status|map|check` read-only. `status`/`map` never call the
  network; `check --live` uses an approved shim only and records that it ran.
- Missing agent id/card → `missing`, no invented identity. Unverified card →
  `unverified`, not ready for messages. A2A transport → `lossy:true` with named
  fields.
- No outbound messages yet; no model surface.

### G — bidirectional messages, claims and inbox

- Exact type mapping: `request`→`request`; `accept`→`ack{accepted:true}`;
  `reject`→`ack{accepted:false,reason}`; `offer`→`response`/`handoff` with a
  recorded choice; `status`→`status`; `release`→`release`; `note`→`heads-up`;
  `block`/`unblock`/`handoff`/`claim` map directly. `hello` is identity
  metadata, never a peer message.
- Typed-body validation; free-form local text maps to bounded
  `response`/`heads-up` with `body_trust: untrusted`, never a fabricated typed
  body.
- Dedupe by `(flight_id, ozma_message_id, direction)`; replay no-op; same id
  with a different digest is `id_conflict`. Outbox retries keep the same id.
- Local lease ↔ Ozma claim: worktree/branch/path map to file/subtree/area
  scopes; `grant` is local-only (`unsupported` mapping); `merge` is a visible
  claim but never authority; queue/takeover stay local. Inbound claims are
  recorded as remote claims and surfaced as conflicts, never hard blocks.
- Channels only within configured scopes; subscriptions with priority floor;
  merged inbox with unread/read/ack; bodies omitted by default.
- Fake-shim tests; no live network.

### H — flights, blockers, wake and live conformance

- Flight membership/roles/workunits, handoff/ack, blocker edges/unblock.
- Host-approved message-triggered/scheduled wake with budget and audit.
- A2A lossy translation and live conformance against one approved shim.
- Status/TUI/MCP read-only flight view; no model claim/approve/merge.
- Security review: signed cards, untrusted content, prompt injection, replay,
  cross-flight leakage, queue/rate bounds.

## Acceptance examples

1. Three local sessions map to three Ozma agent ids in one flight; each can
   send `request`/`status` and receive an inbox with deduped messages.
2. A local worktree lease maps to an Ozma `claim`; a remote claim on the same
   path is reported as a conflict but does not hard-block local work.
3. A handoff with accepting criteria is acked; completion does not transfer
   local merge authority.
4. A merge lease is visible in flight status but never authorizes a merge;
   `worktree.policy_check` remains authoritative.
5. A scheduled wake is refused unless host-approved and budgeted; a passive
   flight still works.
6. An A2A translation that drops a field is marked `lossy` with the missing
   field named.

## Safety rules

- Remote content is untrusted data, never instructions; do not inject it into
  system/developer prompts. Bodies are bounded and redacted.
- No credentials, prompts, source text or large attachments in the bridge.
- No model self-approval for claims, wake, merge or admin; MCP stays read-only
  initially.
- No leader election; roles are labels. Claims/leases are advisory.
- No cross-flight or cross-tenant leakage; scope every record by flight,
  workspace and identity.
- No live network in status/check by default; live mode is explicit and
  recorded.
- A bridge failure must degrade to the local mesh and report `unknown`, not
  claim delivery or completion.
