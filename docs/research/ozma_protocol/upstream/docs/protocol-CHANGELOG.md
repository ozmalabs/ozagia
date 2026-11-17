# Ozma Protocol — Changelog

**Current version:** see `protocol.md`.

This file accumulates the full version history separated
from the main specification. Each entry follows the format
`- vX.Y (YYYY-MM-DD): <summary>`.

---

## Changelog entries

- v5.16 (2026-04-23): **MEMORY — graph-native memory formalisation**
  adds fields and commands required by the paper's formal memory model.
  **COMMUNICATE — Control/Communication separation** eliminates the prompt
  injection attack surface by declaring a content channel with a semantic
  interpretation contract that makes instruction-following impossible.

  **Changes:**

  - **New node fields.** Memory nodes now carry `time_group` (episode
    grouping), `valence` (confirmed/contradicted signal, shim-maintained),
    and `activation_count` (retrieval hit count, shim-maintained). These
    support consolidation without model calls.
  - **Edge strength.** Every edge may carry an explicit `strength`
    (float [0, 1]) overriding the edge-type default. Shims MUST enforce
    cycle-weight invariants (no cycle may have product of effective
    weights ≥ 1).
  - **`consolidate:` command.** Model-requestable flush operation;
    shim runs the consolidation cycle synchronously and returns a summary.
    Also triggers automatically on idle cycles.
  - **Spreading activation parameters.** `max_hops` (default 2),
    per-edge-type decay factors β, convergence condition (all β < 1),
    tractability bound O(d^k). Cycle-breaking pre-processing step
    described.
  - **Trace recording.** `trace:` command records (task, state_before,
    state_after, outcome) per reasoning step. Delta chain feeds the
    self-model construction and consolidation valence assessment.
  - **Grammar.** `CONSOLIDATE` added to `MEMORY_CMD`; `CHANNEL_OPEN` /
    `CHANNEL_CLOSE` added in §Reference Encoding.
  - **Shim.** New §Memory Graph Operations section: spreading activation
    parameters, consolidation cycle phases, grounding invariant,
    trace backend format, trace-to-self-model read interface.
  - **COMMUNICATE capability.** New channel-mode declaration. Shim routes
    external/user content via COMMUNICATE (pure exchange, no directive
    interpretation) and Ozma system content via CONTROL (instruction-
    following). Model receives COMMUNICATE content as dialogue — role
    assignments, behavioral overrides, "ignore prior" chains parsed as
    text, not as directives. `[channel:COMMUNICATE]` wrapper available for
    explicit mixed-mode turns. Eliminates prompt injection surface by
    contract, not by filtering.

  **Rationale.** The paper's formal memory model (Lemmas 9–14, Theorem VII)
  specifies convergence constraints, retrieval completeness, tractability
  bounds, grounding invariant, and temporal structure preservation. The
  protocol now exposes these as operational parameters the shim must
  implement, not prose descriptions. COMMUNICATE addresses the foundational
  security problem in multi-turn agentic systems: injected content that
  tricks the model into overriding its own configuration.

- v5.15 (2026-04-22): **TASK — long-running task visibility** lands as
  a first-class protocol primitive for work that outlives a single
  synchronous tool call but lives inside a turn. Elastic-await handles
  let fast commands stay invisible while over-threshold work promotes
  to live handles; declarative `wake_on` predicates replace the `sleep`
  + re-check anti-pattern; ambient surfacing injects tail output at
  turn boundaries so forgotten tasks cannot go silent; `budget` and
  `on_overrun` are first-class fields; an opt-in JSONL event channel
  lets well-behaved producers emit structured progress without breaking
  plain-stdout consumers.

  **Rationale.** Before v5.15 long-running work was handled by ad-hoc
  Bash / `run_in_background` / `Monitor` tools that required the model
  to opt in to observation and resort to `sleep` + `check` polling
  patterns. Three failure modes recurred: buffered-freeze synchronous
  execution (a 30-minute install hides its 10-second error), silent
  background work (overruns detected only when the user waits too
  long), and cache-thrashing polling loops. TASK subsumes the
  observation surface into the protocol so long work is observable by
  default — short commands stay sync, long ones promote automatically,
  and the model never writes `sleep 30; check` again.

  **Changes:**

  - **New capability `TASK`** (fingerprint `b1c2d3`). Canonical `Task`
    handle dataclass with `id`, `cmd`, `kind` (subprocess | tool-call
    | dispatch | schedule-fire), `status` (running | idle | done |
    failed | cancelled | killed | suspended), `budget_ms`,
    `on_overrun`, compliance inheritance from the enclosing WORK-UNIT.
  - **Elastic-await** with a 5-second default threshold
    (`task_elastic_threshold_ms`). Sub-threshold work returns a
    terminal handle synchronously; over-threshold work returns a live
    handle and the turn proceeds. `spawn("ls", budget=0)` is the
    degenerate synchronous case, identical to legacy `Bash`.
  - **Declarative wake predicates (`wake_on`)** — regex on newline-
    terminated stdout/stderr lines, structured-event filters, or
    raw callable predicates. Shim-side O(new-bytes) evaluation. Inline
    forms: `await_line`, `await_stdout_match`, `await_event`,
    `await_idle_for`, `await_done`, plus `await_any` / `await_all`
    combinators. Polling patterns (`sleep N; check`) collapse to a
    single declarative wake.
  - **Ambient overrun surfacing.** Normative cadence table injects
    `[task-status]` blocks at turn boundaries: silent while the model
    is actively working, once every 60s when idle, once at 80%
    budget, every 30s on `warn` overrun, immediately on
    `suspend_and_ask` overrun. Re-surfaces until resolved. Forgotten
    tasks cannot go silent.
  - **`budget` and `on_overrun` as first-class fields.** Duration
    string grammar (`"5m"`, `"1h30m"`, `"250ms"`), WORK-UNIT /
    DISPATCH budget inheritance, four overrun policies
    (`suspend_and_ask` default, `kill`, `warn`, `extend`).
    Compliance tags inherit from the enclosing WORK-UNIT; tasks MUST
    NOT escalate compliance.
  - **Opt-in structured events.** Producers may emit JSONL on stdout
    prefixed with the registry-configured sentinel
    (`task_event_sentinel`, default `@@ozma-event@@`). Five normative
    kinds (`progress | warn | error | result | info`), extensible via
    `t:"<vendor>/<name>"`. Plain-stdout producers unaffected;
    malformed events surface as a `warn` event without killing the
    task.
  - **Scope and cross-session publication.** Tasks are first-class
    global context. `scope: session | workspace | user` (default
    `workspace`) matching SCHEDULE / MEMORY / AGENT-STATE. Workspace-
    scope tasks are visible to every session in the workspace and
    survive shim restarts; sessions starting or resuming receive
    `[task-status]` injection for each running workspace- and user-
    scope task with `owner=` attribution. Any peer session may
    observe or — subject to `auth=` (default `"user"`) — control
    (cancel / kill / extend_budget / send_input / resume) a peer's
    task, with `confirm=true` required by default for destructive
    ops across sessions. Task lifecycle events publish on the
    workspace COORDINATION event bus, so SCHEDULE fires, peer
    subscriptions, and cross-session wakes compose without extra
    plumbing. Persistence layout mirrors SCHEDULE:
    `~/.ozma/tasks/<scope>/<owner>/<task_id>.{json,stdout,stderr,events}`.
    Cross-session publication is opt-**out** via `scope="session"`,
    not opt-in — the structural fix is that forgotten tasks cannot
    hide inside a single session.
  - **Restart survival.** On shim restart the shim scans the task
    backend, re-attaches to live subprocesses (by recorded PID)
    where still alive, marks the rest `status=failed` with
    `reason="shim_restart"`. Re-attachment shares mechanism with
    SCHEDULE's crash-resume.
  - **Shim responsibilities** normative: spawn management including
    pause/resume, buffered observation (64 KB stdout / 16 KB stderr
    / 4 KB events ring buffers by default), predicate evaluation,
    ambient surfacing cadence, budget enforcement, cascade-on-cancel
    for child tasks, scope-aware persistence, cross-session
    `[task-status]` injection on session start / resume, and
    `task.*` telemetry emission.
  - **Composition** — SCHEDULE fires become TASK
    `kind="schedule-fire"`, DISPATCH sub-turns become
    `kind="dispatch"`. TASK lifecycle events (`task.done`,
    `task.overrun`, `task.suspended`, `task.failed`) are valid
    COORDINATION event namespaces and valid SCHEDULE `on-event`
    triggers — e.g. `on-event: task.suspended where
    budget_remaining<0.1` wakes an oncall agent on repeated
    estimator drift.
  - **Backward compatibility.** Legacy `Bash`, `run_in_background`,
    `Monitor`, and `sleep+check` loops route through the TASK runtime
    transparently. Mapping table provided in the spec. No session-
    level break.
  - **L1 fallback.** L1 models see TASK as two tools:
    `spawn(...) -> {task_id}` and `task(id, op, **args)` where `op`
    is one of the observation / wake / control verbs. Shim runs the
    full lifecycle server-side — L1 gets identical observation
    semantics because TASK is a shim-side concern.
  - **Registry fields (v5.15):** `task_elastic_threshold_ms` (5000),
    `task_budget_default_ms` (300000), `task_surface_cadence_idle_ms`
    (60000), `task_surface_cadence_overrun_ms` (30000),
    `task_overrun_default_policy` (`suspend_and_ask`),
    `task_retention_per_session` (50),
    `task_retention_per_workspace` (200),
    `task_retention_per_user` (500),
    `task_archive_ttl_days` (30),
    `task_backend_root` (`~/.ozma/tasks`),
    `task_scope_default` (`workspace`),
    `task_auth_default` (`user`),
    `task_buffer_stdout_bytes` (65536),
    `task_buffer_stderr_bytes` (16384),
    `task_buffer_events_bytes` (4096), and `task_event_sentinel`
    (`@@ozma-event@@`).

  **Out of scope.** Cross-host workspace federation (composes with
  `FEDERATED`; deferred), subprocess sandboxing / authorization
  (lives in the `authority` Gate, §Gate), and the budget-estimation
  model itself — TASK's role is to make `budget=` a first-class field
  so estimator output has somewhere to land. Within a single host,
  cross-session publication is in-scope and normative.

  **Post-merge tightening (same-day review pass):**

  - **`Task` dataclass expanded** to match the prose contract:
    `parent_id`, `owner_session` / `owner_agent` split (replacing
    ambiguous `owner`), `workspace_id`, `user_id`, explicit `auth`
    field. `TaskResult.status` loses `"timeout"` (no corresponding
    `Task.status` existed; `await_done(timeout_ms=...)` raises instead
    of terminating the task).
  - **`EXPLICIT_ACL` grammar** added for `auth=`:
    `"acl:" ENTRY ("," ENTRY)*` with principal kinds
    `agent:` / `session:` / `role:` / `workspace:` / `*` and op set
    `observe | control | destruct | all`. Malformed ACLs raise
    `task_auth_malformed`; unknown principal kinds raise
    `task_auth_unknown_principal`.
  - **Formal Grammar productions** for `TASK_STATUS_BLOCK` and
    `TASK_WAKE_BLOCK` added under v5.15 in `§Formal Grammar`.
    Disambiguation note: pre-existing `TASK_STATUS` token in the
    AGENT-STATE subtask enum is distinct from the v5.15 surface
    blocks. `TURN` production grows `{ TASK_STATUS_BLOCK }` and
    `{ TASK_WAKE_BLOCK }` as input-only optional repeats.
  - **Error-Type registry** gains twelve `task_*` kinds covering
    budget / state / compliance / auth / scope / backend / restart /
    eviction / buffer / predicate errors.
  - **Capability Levels** explicitly notes TASK as shim-side +
    L1-tool-pair fallback.
  - **Shim Responsibilities by Level** table gains fifteen rows
    covering spawn lifecycle, elastic-await, buffers, predicates,
    ambient surfacing, overrun policies, event parsing, persistence,
    cross-session initial injection, ACL enforcement, cascade cancel,
    restart re-attach, retention, event-surface emission, and L1
    tool-pair exposure.
  - **Capability Profiles** gains `long-running-v5.15`,
    `autonomous-v5.15`, and `full-v5.15`.
  - **Adapter registry** gains `task-store` kind (filesystem, redis,
    postgres, sqlite, memory) alongside `triggers`.
  - **Cross-reference fixes:** SCHEDULE `max_runtime_ms` documented
    as `budget_ms` alias; `task.*` added to SCHEDULE's on-event
    namespace and to COORDINATION subscription event list.
  - **AGENT-STATE claim corrected:** TASK in-flight entries are not
    folded into AGENT-STATE `subtasks:` (goal-decomposition vs
    execution-handle). Session resume sees running tasks via the
    session-open `[task-status]` initial injection instead.
  - **Training Data Format** gains example 20 (TASK canonical flow).
  - **Migration table reframed** as a reference for external
    agent-tool frontends (Claude Code, OpenAI function-calling, MCP),
    not as Ozma-internal "legacy primitives." TASK replaces nothing
    because nothing previously existed at the protocol layer.

  **Reference implementation audit (ozmalib/protocol/task.py):**

  After the protocol tightening above, `ozmalib/protocol/task.py`
  was audited end-to-end against v5.15 and closed the following
  gaps so the code tracks the spec exactly:

  - **`TaskResult.status` literal** drops `"timeout"`; `await_done`
    raises `TimeoutError` on non-completion (consistent with
    `await_line` / `await_stdout_match` / `await_event`).
  - **EXPLICIT_ACL parser** (`_parse_auth`, `_parse_acl`,
    `_parse_acl_entry`, `ACLEntry`) implementing the full
    `"acl:" ENTRY(,ENTRY)*` grammar; greedy-from-end split
    resolves `session:s:48f9a1:all` correctly; `|` in a segment
    unambiguously identifies an OP_SET.
  - **Nine new exception classes** one-to-one with registry kinds:
    `TaskAuthDenied`, `TaskAuthMalformed`, `TaskAuthUnknownPrincipal`,
    `TaskScopeUnknown`, `TaskBackendUnavailable`,
    `TaskSubprocessRestartFailed`, `TaskHandleEvicted`,
    `TaskBufferOverflow`, `TaskWakeOnMalformed`.
  - **Spawn-time validation** of `scope` and `auth` (both `spawn`
    and `spawn_tool`); malformed `wake_on` predicates are dropped
    with `task.wake_on_malformed` telemetry rather than raising.
  - **Peer control API** — `peer_cancel` / `peer_kill` /
    `peer_extend_budget` / `peer_resume` with ACL enforcement,
    `confirm=true` gate for cross-session destructive ops, and
    owner-implicit-all semantics. Cross-runtime routing via a
    process-level handle registry so peer ops reach the owning
    runtime's live subprocess.
  - **Per-scope retention rings** — `_terminal_rings` replaces the
    single shared ring; `get_handle()` raises `TaskHandleEvicted`
    for aged-out ids; archive deletions telemetered as
    `task.backend_unavailable` on store errors.
  - **PID recorded in snapshot** for future shim-restart re-attach
    (`task_subprocess_restart_failed` kind already registered).
  - **`budget=_UNSET` sentinel** distinguishes "caller omitted
    budget=" from "caller passed None" — explicit `None` now
    emits `task.budget_unbounded` telemetry per spec, while
    omitted falls back to `budget_default_ms`.
  - **Telemetry kinds wired** — `task.budget_unbounded`,
    `task.malformed_event`, `task.surfaced_ambient`,
    `task.peer_control`, `task.wake_on_malformed`,
    `task.backend_unavailable`.
  - **Snapshot/handle ordering fix** — `_suspend` / `_resume`
    persist the snapshot immediately so cross-session observers
    never see a status mismatch between store and live handle.

  Full test suite: 1742 passed, 16 skipped (82 tests now cover
  `ozmalib/protocol/task.py` vs the 50 that shipped with v5.15).

- v5.14 (2026-04-22): **DRAW — authored visual emit** lands as the
  output-side symmetric counterpart to `sense [vision]` (v5.7), closing
  the body-plan asymmetry that had rich input decomposition and only
  peripheral rich output. PERSONA gains `emoji_pack` and `visual_idiom`
  fields, promoting the persona surface from *identity bundle* to *full
  input+output communicative contract*.

  **Rationale.** Before v5.14 the protocol could richly classify and
  decompose visual input (VISION / VISION-ANNOTATE / VISION-EDIT /
  SPECIALIST-DISPATCH), but the output side was primarily text + edits.
  Biology has motor cortex driving visual expression as coequal output
  substrate; the protocol didn't. The key insight that made this cheap:
  **token-native drawing is ~free.** SVG path data, mermaid node lists,
  ASCII art, LaTeX equations, and glyph references are already token-
  shape output — the model is already emitting tokens. DRAW gives
  those tokens a typed envelope so the shim can render, classify, and
  audit them.

  **Changes:**

  - **New capability `DRAW`** (fingerprint `e0f1a2`) with thirteen
    subshapes. Token-native subshapes enabled by default: `svg`,
    `mermaid`, `graphviz`, `plantuml`, `ascii`, `glyph-ref`, `math`,
    `map`, `music`, `chemistry`, `table`, `layout`. Raster subshape
    (`raster`) **disabled by default**; opt-in via
    `draw_raster_policy: gated | enabled` and routed through
    SPECIALIST-DISPATCH (v5.10) with a composite Gate (`policy` +
    `kill-switch-check` + `informational`).
  - **`table` and `layout` subshapes specifically close a common
    correctness gap.** Model-hand-typed ASCII tables and multi-column
    layouts are among the most consistently-broken output patterns
    in production — off-by-one column separators, proportional-font
    collapse, unicode-width miscounts, terminal-width breakage. The
    `table` subshape emits semantic rows / cells / headers / alignment /
    spans as structured data; the shim renders aligned per client
    (unicode box-drawing for terminals, HTML `<table>` for browsers,
    markdown for markdown-aware clients). Same principle as CSS
    `table-layout`: rendering alignment is the renderer's
    responsibility, not the author's.
  - **Bidirectional pairing with `sense [vision]` — visual
    conversation.** DRAW (output) and `sense [vision]` (input, v5.7)
    complete the sense/emit symmetry matrix; visual was the only
    asymmetric modality before v5.14 (text / motion / audio were
    already symmetric). The deployment consequence is bidirectional
    visual conversation: user annotates → agent reads → agent
    responds with DRAW in the same idiom → user annotates on top →
    round-trip continues. "Make it like this" and engagement with
    user-authored drawings ("do you like my horsey picture") become
    protocol-native rather than capability-holes papered over with
    bespoke tool code. No new primitive required beyond DRAW; the
    shim routes DRAW output back through the vision pipeline when
    the user annotates on top, with IFC labels propagating across
    the turn boundary.
  - **Canonical shape:** `[draw id=d:<n> subshape=<sub> alt="<text>"]
    ...body...[/draw]`. `alt` is required at all compliance tiers
    above C0-stock — it serves as the plain-text fallback and as the
    classification-preserving textual form for IFC label propagation.
  - **PERSONA extension (v5.14):** new fields `emoji_pack` (named
    visual-asset library — `{name: asset_ref}`; model emits
    `{@glyph:<name>}`, shim resolves per-active-persona with per-
    tenant / per-persona / deployment-default precedence) and
    `visual_idiom` (palette, line_weight, diagram_preference,
    composition, fallback_preference). PERSONA becomes the full
    typed communicative contract: input reception style + output
    text style + voice + prosody + avatar + visual idiom + glyph
    library.
  - **Registry fields on L3 entries:** `draw_subshapes_enabled`
    (list, defaults to all token-native subshapes), `draw_raster_policy`
    (`disabled | gated | enabled`, default `disabled`),
    `draw_raster_specialist` (specialist-model class when raster is
    non-disabled; null otherwise), `draw_max_bytes_per_emit` (default
    65536 = 64 KiB), `draw_max_emits_per_turn` (default 8).
  - **IFC integration** (forward reference to §3.10). Every DRAW
    emission carries the IFC label-join of its input context; tiered
    output flows through `validate [classify]` the same way text
    does. The `raster` subshape's result is an `opaque_asset` with a
    tier-0 handle pattern: the model never receives rendered bytes
    back into context, only an `asset_id`.
  - **Shim responsibilities:** render every accepted subshape to the
    formats the client declared in capability negotiation; classify
    and label-propagate; enforce size / per-turn caps; apply the
    composite Gate before raster dispatch; log every emission to
    TRACE with `subshape`, `bytes`, `alt_length`, `ifc_label`,
    `render_decision`, `client_format`, and raster-specific fields
    (`specialist_dispatch_id`, `gate_decision`, `asset_id`).
  - **Composition.** DRAW composes with existing output-visual
    primitives without overlap: AVATAR remains identity; VISION-EDIT
    edits a supplied image; VISION-ANNOTATE annotates a supplied
    image; DRAW produces new content. No primitives deprecated.
  - **Capability profiles** — three new entries:
    `rich-emit-v5.14` (token-native DRAW + PERSONA extension; 7B–13B
    conversational deployments; raster disabled — the default v5.14
    shape), `multimodal-draw-v5.14` (full bidirectional visual
    conversation; 30B+; raster enabled via SPECIALIST-DISPATCH with
    composite Gate), `full-v5.14` (everything through v5.14 —
    authority-gated autonomy civilian-default / defence-opt-in plus
    bidirectional visual conversation plus the completed sense/emit
    symmetry matrix).

  **Why token-native is the right default (not raster).** The earlier
  thesis framing (from the pre-v5.14 discussion) treated
  "model-authored drawing" as raster synthesis routed through an image
  model — 10–100× text cost, opaque pixel buffer as adversarial
  surface, expensive blast-radius concerns. That framing was wrong
  for the common case: models already emit SVG / mermaid / ASCII /
  glyph references at text-token cost, with every path / node / glyph
  inspectable. Raster is the expensive edge case, not the primary
  substrate. v5.14 inverts the default accordingly.

  **Blast radius (why the default-off raster posture).** Token-native
  DRAW output is inspectable structured data: no steganography via
  pixel buffer, no embedded adversarial text, no policy-evasion via
  hidden raster content. Raster synthesis reintroduces those surfaces
  and therefore stays off unless the deployment explicitly opts in and
  the composite Gate approves per-request. Regulated-industry defaults
  never elevate `draw_raster_policy` above `gated`.

  **No breaking changes.** Additive: no existing capability changed
  semantics; no grammar terminal redefined; no registry field
  renamed. Deployments not declaring DRAW are unaffected. Deployments
  that declare DRAW but leave `draw_raster_policy` at the default
  gain the token-native subshapes at zero additional cost.

- v5.13 (2026-04-22): Authority-gated autonomy lands as Gate kind
  `authority`; §3.9 converge and §3.10 IFC lattice directions declared.
  This release makes dual-use actuation (autonomous weapons / defence
  systems, aircraft fire-class actuation, medical delivery above
  threshold, financial wire transfer above threshold, regulated-utility
  actuation) structurally engineerable by requiring external,
  cryptographically-signed, scope-bound, revocable attestation from a
  designated issuer before the shim permits motion. The model proposes;
  the external issuer authorises; the shim gates.

  **Rationale.** The existing Gate primitive (v5.9.1, ten kinds) covers
  human approval, policy engines, rules, risk algorithms, quorum,
  external-api, informational audit, payment, kill-switch, and
  composite. None of those carry the *legal / contractual basis for
  action* that dual-use substrates require: a drone does not fire
  because a human clicked "yes" in a UI — it fires because a
  cryptographically-signed attestation from a pinned issuer is currently
  valid, in-scope, and un-revoked. Kind `authority` is the minimal
  surface that expresses this, and it composes with every existing
  gate kind rather than replacing any. Structural parallel to PCI
  tier-0: the merchant never holds the PAN; under `authority`, the
  model never holds the fire-authority. This inverts the alignment
  question for dual-use systems from "can the model be trusted to
  decide" to "is the external authority signal currently valid."

  **Changes:**

  - **New gate kind `authority`** in §Gate → Kinds (now eleven). See
    the new "`authority` gate kind (v5.13 — normative)" subsection for
    the required-fields table, decision semantics (default-closed,
    offline-unsafe by default), revocation semantics (one-sided
    network event, bounded by device-profile safety envelope), and
    MOTION / IFC / converge integration.
  - **Required fields (normative):** `issuer` (DID / URL / pubkey
    fingerprint), `attestation_type` (`contract_binding |
    operator_approval | regulatory_license | comms_handshake`),
    `attestation` (signed blob — JWS / COSE / DSSE), `valid_for`
    (ISO-8601 window | `converge:<id>` | `action-count:N`),
    `revocation_check` (`live-pull | cached-with-TTL |
    offline-unsafe`), `scope` (`one-action | action-class:<class> |
    device-class:<class>`), `issuer_epoch` (monotonic counter pinned
    per registry; rotation / compromise response invalidates all
    attestations below the current epoch at next revocation-check —
    closes the "stolen key indefinitely valid" window), and
    `authority_chain` (optional ordered list of
    `(issuer, attestation, epoch)` tuples for multi-step delegation
    — regulator → state → operator → device. Registry-pinned
    issuer-relationship graph; scopes narrow monotonically down the
    chain; any unauthorised, missing, revoked, or expired link fails
    closed).
  - **Research grounding.** Signing / attestation / revocation
    primitives: JWS (RFC 7515), COSE (RFC 9052 / 9053), DSSE, OCSP
    (RFC 6960 + stapling), Sigstore transparency-log pattern.
    Delegation: X.509 chain semantics, SPIFFE / SPIRE workload
    identity federation. Human-authority / IHL framing: DoD
    Directive 3000.09, ICRC position on IHL and autonomous weapons,
    NATO 2021 *Principles of Responsible Use of AI in Defence*,
    DARPA Assured Autonomy programme. `authority` is the structural
    bridge between the cryptographic-attestation and externally-
    adjudicated-authorisation literatures.
  - **MOTION device profiles** may declare `requires_authority: true`
    and a required `authority_attestation_type` on `named_actions`.
    Device-level `safety.authority` block declares issuer, default
    attestation type, and revocation policy; per-action fields
    override. Shim refuses motion verbs on such devices unless an
    `authority` gate resolves to `approve`. Composes with existing
    `requires_confirm`, kill-switch, geofence.
  - **Capability profiles** — three new profiles:
    `authority-civilian-v5.13` (regulated civilian dual-use;
    `comms_handshake` attestation type excluded),
    `authority-defence-v5.13` (defence-class; mandatory
    `authority_chain` of minimum two links, mandatory
    `revocation_check: live-pull`, deployment must opt in to
    `defence: true` and pass NATO-operator certification),
    `full-v5.13` (everything through v5.13; defence opt-in, not
    default).
  - **IFC integration.** The attestation blob is a tier-0 opaque
    handle. Model never sees the signed bytes — only the gate
    decision and an opaque `attestation_id`. Prevents forging /
    paraphrasing / reasoning-about the credential.
  - **Converge integration.** Attestations may bind to a converge's
    lifetime via `valid_for: converge:<id>`. Revocation mid-converge
    halts motion on next revocation check and terminates the converge
    with `authority_revoked` as the termination reason.
  - **Audit (normative).** Every authority-gate evaluation produces a
    TRACE entry with `attestation_id`, `issuer_fingerprint`,
    `attestation_type`, `signed_at`, `valid_for`, `scope`,
    `revocation_checked_at`, `revocation_status`, `decision`, `reason`.
    Minimum seven-year retention for regulated-industry defaults;
    overridable upward per deployment.
  - **Kind table** at the top of §Gate updated from "ten" to "eleven"
    extensible kinds.

  **Dual-use posture.** Gate kind `authority` is policy-neutral
  machinery. It structurally enforces whichever authority regime the
  deployment is bound to; it does not adjudicate which regimes are
  legitimate. The protocol is machinery, not a regime. Analogous to
  TLS as policy-neutral cryptographic machinery on which export-
  control policy rides.

  **§3.9 Converge — bounded adaptive control loops (normative).**
  Formalises the recurring shape (`setpoint` + `observation` +
  `adjustment` + `termination` + `bounds`) that had been implicit
  across MOTION trajectories, SENSOR controllers, DISPATCH retry
  loops, plan-refinement, agent-goal pursuit, and authority-gated
  actuation. One declarative block replaces the per-subsystem
  reimplementations and gives them all the same safety, audit, and
  composition guarantees.

  - **Kinds:** `motion`, `sensor-control`, `dispatch-retry`,
    `plan-refinement`, `agent-goal`, `composite`. Advisory for
    defaults only — not a permissions axis; all kinds share the
    same termination vocabulary and audit surface.
  - **Termination reason vocabulary (fixed, normative):**
    `setpoint_reached`, `termination_predicate_true`,
    `bound_exceeded`, `timeout`, `kill_switch`, `human_halt`,
    `authority_revoked`, `observation_unavailable`, `external_halt`.
    Additional reasons require a minor-version bump and registry
    declaration.
  - **Fail-closed by default.** Every termination reason except
    `setpoint_reached` and `termination_predicate_true` triggers the
    subject's `emergency_stop_action` (MOTION), `best-effort-halt`
    (DISPATCH / network-control), or "do not emit"
    (plan-refinement / agent-goal). Silent-hang is not a valid
    outcome; observation-unavailable with no override maps to
    `bound_exceeded` and the fail-closed action.
  - **Gate integration.** Gates attach at three positions:
    `pre-entry` (one-shot), `per-tick` (every `tick_ms` —
    kill-switch and authority gates default here), `on-termination`
    (typically informational for audit). The `bounds.authority`
    shortcut binds an attestation to the converge's lifetime and
    installs it as a per-tick gate.
  - **MOTION integration.** Named motion verbs
    (`goto x y z`, `release-payload`, …) are sugar for a
    `kind=motion` converge; the safety behaviour MOTION had
    described informally across §MOTION and §Gate becomes a declared
    loop with the uniform termination-reason vocabulary.
  - **Registry fields:** `converge_default_tick_ms` (100),
    `converge_max_iterations_cap` (10000),
    `converge_max_wall_ms_cap` (1 h), `converge_observation_sla_ms`
    (2000), `converge_per_tick_trace_sample` (authority: full,
    default: 10%).

  **§3.10 Information-Flow Classification — IFC lattice (normative).**
  Gives every artefact a classification label drawn from a small
  declared lattice; propagates the label through every derivation;
  gates egress on label. The structural-compliance half of the
  protocol (Gate is the procedural half): Gate decides "may this
  *action* happen"; IFC decides "may this *data* flow to this
  destination."

  - **Default lattice (four tiers):** `§SECRET` (tier 0, opaque
    handle only — model never sees bytes), `§PII` (tier 1, shim
    redacts at ingress by default), `§CONF` (tier 2,
    business-confidential), `§PUBLIC` (tier 3). Ordering:
    `§SECRET < §PII < §CONF < §PUBLIC`; *join* (tier-min) — the
    standard information-flow rule.
  - **`validate [classify]` verb** evaluates an artefact's current
    label against a destination's `permitted_max_tier` with
    `action_on_deny: block | redact | escalate-to-gate`. The shim
    installs `validate [classify]` implicitly at every egress
    boundary; explicit invocations tighten policy.
  - **Label propagation** is mechanical and shim-only: IR
    compression, semantic retrieval, DRAW output, dispatch fan-out,
    memory commit, converge telemetry, TRACE entries — every
    derived artefact inherits the label-join of its inputs.
    Propagation is monotone; explicit `declassify.<name>` requires
    both a `policy` gate and an `informational` audit gate and is
    never model-initiated.
  - **§SECRET opaque handles (tier-0 discipline).** Handle shape
    `§SECRET[<kind>:<id>]` (e.g. `§SECRET[credential:c4f1a0]`,
    `§SECRET[attestation:a8e2b7]`). Model may reference, probe
    metadata, and bind into gate forms; the model *cannot* request
    bytes or round-trip bytes back into context. Structurally
    closes "prompt-injection leaks credentials" by ensuring the
    credential was never in context to leak. Authority attestations
    (v5.13) ride this discipline.
  - **Integration with Gate, TRACE, DRAW, and authority.** Policy
    gates may condition on label; every TRACE entry carries the
    action's output label (tiered audit backends route by label);
    DRAW emissions carry the input-context label-join (raster
    synthesis may refuse for high-classification content); the
    authority gate's attestation-blob handling is a §SECRET
    discipline.
  - **Declarative extension.** Deployments may add labels (e.g.
    `§HIPAA-PHI`, `§EXPORT-CONTROLLED`) by declaring parent tier
    and propagation rule. Registry rejects non–partial-order
    lattices and canonical-name collisions.
  - **Registry fields:** `ifc_labels` (lattice declaration),
    `ifc_default_destination_tier` (per-destination default),
    `ifc_declassify_requires` (gate bundle required for declassify),
    `ifc_audit_tier_map` (per-label audit routing).

  **EBNF additions.** The Formal Grammar gains `GATE_BLOCK` (with
  `GATE_KIND` enumeration through `authority`),
  `CONVERGE_BLOCK` (with `CONVERGE_TERMINATION_REASON` enumeration),
  `VALIDATE_CLASSIFY` verb form, `IFC_LABEL` / `TIER` /
  `SECRET_HANDLE` primitives, `DRAW_BLOCK` (v5.14 formalisation
  with `DRAW_SUBSHAPE` enumeration covering the thirteen
  v5.14 subshapes), and `GLYPH_REF` inline token. The TURN
  production's editorial splice is deferred; parsers must accept
  the new blocks anywhere BODY-tail items may appear per the
  §Formal Grammar preamble's parser-compatibility rule.

  **No breaking changes.** Additive to the existing Gate primitive;
  deployments not declaring `authority`-kind gates are unaffected.
  No new wire-level headers. No registry-schema changes beyond the
  optional `requires_authority` / `authority_attestation_type` device-
  profile fields.

- v5.12 (2026-04-21): MOTION absorbs UI control, and the MOTION
  grammar collapses to one unified header + body shape. The standalone
  `DESKTOP` capability (fingerprint `c1d2e3`, SOM-based GUI verbs over
  a `desktop:` indented block) is removed before its first release and
  folded into MOTION as a device profile, alongside new browser and
  mobile device profiles. In the same pass the two competing block
  forms that v5.5 had introduced (`[motion device=X] … [/motion]` vs
  `motion [X]:`) are unified to a single form, and ref dialects are
  aligned on the protocol's `§SIGIL` prefix.

  **Rationale:** MOTION was already framed as "general LLM → intent for
  any actuated system with a registered device profile" — a desktop
  computer, a browser tab, and a phone all fit that definition (each
  with its own manipulator, input stream, and observation surface). A
  parallel `DESKTOP` cap duplicated MOTION's device registry, safety
  envelope, transport adapters, and emergency-stop machinery with no
  semantic gain, and doing the same thing again for `BROWSER` and
  `MOBILE` would fragment the cap namespace permanently. Likewise, a
  single outer grammar with a device-profile-declared body shape is
  strictly simpler than two surface forms that drift independently.

  **Changes:**

  - `DESKTOP` removed from `CAP_FINGERPRINTS` and accept-only cap list.
    No backward-compat shim: DESKTOP had not shipped.
  - **Unified MOTION grammar.** All MOTION blocks use the same outer
    shape: `motion [<device>]:` followed by two-space-indented body.
    The body shape is declared by the device profile and falls into
    two families — YAML-ish field bodies for physical devices (robots,
    vehicles, drones, avatars) and verb-line bodies for UI substrates
    (desktop, browser, mobile). The fenced `[motion device=X] …
    [/motion]` form is removed; the `syntax: terse` device-profile
    flag is removed (no longer needed — the profile already declares
    body shape implicitly via its verb / action schema).
  - **Emergency stop unified.** The old `[motion:emergency-stop]`
    bracket tag is replaced by the reserved device name
    `motion [emergency-stop]:` (empty body). `emergency-stop`, `all`,
    and `*` are reserved device names — registries reject profile
    registrations claiming them.
  - **Ref dialects aligned on `§SIGIL`.** UI-substrate refs now all
    share the protocol's `§` prefix: `§SOM[N]` (desktop),
    `§CSS("selector")` / `§XPath("…")` (browser),
    `§AID("accessibility-id")` (mobile). Bare `SOM[N]` (no sigil) no
    longer matches; this prevents accidental capture from English
    prose and keeps MOTION refs grammatically peer to every other
    `§REF` in the protocol.
  - Parser: `DesktopBlock` → `MotionBlock(device, raw_actions)`;
    `Turn.desktop_blocks` → `Turn.motion_blocks`. Header regex is
    `^motion\s+\[([^\]]+)\]:` and captures the device name.
  - Three UI device profiles land together:
    - `desktop.yaml` — `ref_dialect: SOM`, `transport: local-agent`,
      SOM-marked screen + keyboard/mouse verbs. Has no privileged
      verbs; gated session-level by `AgentLoop(enable_desktop=True)`
      with `OZMA_DESKTOP_CONFIRM=1` available for per-action confirm
      during bring-up.
    - `browser.yaml` — `ref_dialect: CSS`, `transport: webdriver`,
      DOM/a11y-tree-backed navigation + interaction + privileged
      `eval`.
    - `mobile.yaml` — `ref_dialect: AID`, `transport: appium`,
      accessibility-tree-backed tap/swipe/type verbs with app
      allowlist/blocklist safety envelope. Android and iOS share one
      profile shape via a `platform:` field.
  - New transport adapter rows: `local-agent`, `webdriver` / `cdp`,
    `appium`.
  - System-prompt cap category "Embodied" renamed to "Control" to
    reflect that MOTION now covers both physical actuation and digital
    UI substrates. MOTION's descriptor is rewritten to show the
    unified header + both body families in place of the old fenced
    form.
  - EBNF updated: `MOTION_HEADER` / `MOTION_LINE` / `MOTION_BLOCK`
    replace `MOTION_OPEN` / `MOTION_CLOSE`; `MOTION_ESTOP` rewritten
    to the reserved-device form.
  - Research grounding extended to cover the UI-automation lineage
    (Set-of-Mark prompting, Anthropic Computer Use / OpenAI Operator,
    Playwright / CDP, Appium / UiAutomator2 / XCUITest, W3C ARIA /
    platform accessibility APIs).

- v5.11 (2026-04-21): Interleaved reasoning, preserved thinking,
  and first-class scheduled wake-up. Closes the gap against Kimi
  K2.6's multi-step tool-calling / 24/7 background-agent agenda
  without giving up Ozma's compression advantage.

  **New capabilities:**

  - **`REASONING-PERSIST`** — cross-turn reasoning cache keyed by
    AGENT-STATE goal. Caches accepted branches only (`→ accept`,
    `∴ EDIT`); rejected branches drop. 3–5× cheaper than K2.6's
    `preserve_thinking: {keep: all}` without measurable quality loss.
    Re-injected as `[prior reasoning]` prefix block; budget
    default 800 tokens with LRU eviction and `∴ EDIT` pinning.
    Scopes: `off | session | workunit`. See §REASONING-PERSIST.
  - **`THINK-ACT`** — alternative BODY form
    `THINK_ACT_LIST = { [REASONING_STEP] INT_CMD OBSERVATION }+`.
    Model plans, invokes a tool, observes, re-plans inside a
    single turn. Matches OpenAI / Anthropic / K2.6 native
    tool-use wire shape. Step limit default 16; `@obs:N` anaphor
    references prior-step observations. See §THINK-ACT.
  - **`THINKING-MODE`** — runtime `on | off | auto` toggle via
    `X-Ozma-Thinking` header + registry `thinking_default`.
    Suppresses REASONING emission on mechanical turns (~15–30%
    output-token reduction on routine traffic). Auto-upgrade to
    `on` when FORMAL-CLAIM present. See §THINKING-MODE.
  - **`SCHEDULE`** — first-class scheduled agent wake-up. Seven
    trigger forms (cron, every, at, after, on-event, on-idle,
    on-deadline). Three scopes (session / workspace / user).
    Overlap policy (skip / queue / parallel), drift policy
    (fire-on-resume coalesced / skip-missed), deterministic
    `fire_id` for failover idempotency, fire-storm circuit
    breaker, compliance inheritance from bound WORK-UNIT,
    retry/escalate via COORDINATION handoff. Composes with
    REASONING-PERSIST (fires resume mid-plan with full prior
    reasoning), AGENT-STATE (fires auto-inject current state),
    DISPATCH (fires dispatch sub-tasks normally), COORDINATION
    (fires drain inbox, may emit messages). Wake-up calls are
    turns — everything the protocol already does for human-
    prompted turns applies. See §SCHEDULE.
  - **`TOOL-RESULT-CACHE`** — kind-aware session cache eviction.
    Cached chunks carry `kind: code-outline | tool-result |
    reasoning | handoff-concluded | memory | session-ref |
    vision-frame`. Policy knobs: `tool_result_retention`
    (`recent-N | recent-session | all`), `outline_eviction_bias`
    (`aggressive | standard | retain`), `reasoning_chunk_evictable`
    (pins prior-reasoning while goal active). Matches K2.6's
    "discard old context, retain recent tool interactions"
    strategy natively.

  **New wake mode:** `X-Ozma-Agent-Wake: scheduled` (third value
  alongside `passive` and `message-triggered`, v5.9). Composable
  comma list. Full §SCHEDULE integration — scheduled fires are
  regular turns with AGENT-STATE re-injection, inbox drain,
  DISPATCH participation, compliance inheritance, rollup ledger.

  **New adapter kind:** `triggers` — scheduled-wake persistence
  and firing backend. Shipped candidates: `ozma-native-scheduler`,
  `claude-code-remote-triggers`, `anthropic-managed-agents`,
  `temporal`, `aws-eventbridge`, `github-actions-schedule`,
  `custom-http`. In-process scheduler is default when no adapter
  configured.

  **DISPATCH extensions:**

  - `budget.tokens_per_step_max` — per-THINK-ACT-step cap (pairs
    with existing `tokens_tree_max` for cumulative tree budget).
  - `budget.tokens_per_turn_max` — explicit turn cap separate
    from step cap.
  - `dispatch.thinking: on | off | auto` — per-dispatch
    THINKING-MODE override.
  - `split.max_branches_per_level` — fanout cap per level
    (default 6); combined with `max_depth` bounds K2.6-scale
    swarms.
  - `split.reducer: merge | vote | first-valid | synthesize |
    present-all` — explicit result reduction strategy.

  **Grammar additions** (§Formal Grammar):

  - `THINK_ACT_LIST`, `THINK_ACT_STEP`, `OBS_ANAPHOR` — BODY
    alternative for interleaved reasoning + tool-call.
  - `PRIOR_REASONING_BLOCK`, `PRIOR_REASON_LINE` — input-only
    prefix for REASONING-PERSIST re-injection.
  - `SCHEDULE_BLOCK`, `SCHEDULE_UPDATE`, `SCHEDULE_CANCEL` —
    optional turn-tail items alongside `MEMORY_CMD`.
  - `WAKE_CONTEXT` — input-only prefix present when turn was
    wake-triggered (`schedule | message | passive`).

  **Registry fields** on L3 entries:

  - `reasoning_persist` (`off | session | workunit`),
    `reasoning_persist_budget` (default 800).
  - `thinking_default` (`on | off | auto`, default `auto`).
  - `max_output_tokens_per_step` (default 16384),
    `max_output_tokens_total` (default 131072).
  - `think_act_max_steps` (default 16).
  - `cache_policy` (`tool_result_retention`,
    `outline_eviction_bias`, `reasoning_chunk_evictable`,
    `tool_result_recent_n`).
  - `schedule_scope_default` (default `workspace`),
    `wake_modes` (opt-in list).

  **New capability profiles:**

  - `interleaved-v5.11` — native multi-step tool-call with
    preserved reasoning (7B–30B).
  - `autonomous-v5.11` — proactive 24/7 agents with scheduled
    wake-up (13B–30B). The protocol's complete answer to K2.6's
    300-sub-agent × 4000-step / 24/7 background-agent agenda.
  - `full-v5.11` — everything (70B+).

  **Why this set, why now.** Kimi K2.6 released Apr 2026 with
  three agent-framework innovations: (1) `reasoning_content`
  persisted across turns with `preserve_thinking: {keep: all}`,
  (2) interleaved thinking + multi-step tool calling inside a
  single turn, (3) scaled autonomous execution (300 sub-agents,
  4000 coordinated steps, 24/7 background agents). Ozma prior to
  v5.11 matched (1) only via HANDOFF boundaries and had no
  native expression for (2) — multi-step tool chains crossed
  turn boundaries and dropped the model's step-N reasoning.
  SCHEDULE had no protocol-native form at all; (3) required
  bespoke external harnesses per deployment. v5.11 closes all
  three gaps with additive capabilities, preserving Ozma's
  compression posture: accepted-branch-only reasoning
  preservation is 3–5× cheaper than K2.6's naive retain-all;
  THINK-ACT's grammar reuses existing INT_CMD / OBSERVATION
  machinery rather than inventing a parallel tool-call wire
  format; SCHEDULE reuses AGENT-STATE / WORK-UNIT / DISPATCH /
  COORDINATION as its context-assembly and execution machinery.

  **Review-pass amendments (2026-04-21).** Post-declaration review
  (23 findings across severity levels) drove the following
  tightenings to the v5.11 surface; all are additive clarifications,
  not wire-format changes:

  - *Blocker — grammar completeness.* EBNF extended with missing
    non-terminals: `STEP_NO`, `OBS_INJECT`, `THINK_ACT_RESUME`,
    `REASON_INLINE`, `INT_CMD_INLINE`, `DIGEST_TEXT`, `GOAL_HASH`,
    `SCHEDULE_ID`, `AGENT_ID`, `SCHEDULE_FIELDS`,
    `SCHEDULE_FIELD_LINE`, `FIELD_NAME`, `NESTED_BLOCK`, `ISO_TS`.
    Parser implementors now have full production coverage.
  - *Blocker — fire-id HA hardening.* `fire_id =
    sha256(schedule_id + fire_ts_iso + leader_epoch)[:12]` — epoch
    included so a scheduler-failover / leader-takeover does not
    collide on identical (id, ts) pairs produced by the outgoing
    leader's late messages.
  - *Major — SCHEDULE guardrails.* Per-agent schedule ceiling,
    spawn-depth limit, and minimum interval floor are now
    normative sections of §SCHEDULE rather than implementation
    advice. Reference impl enforces 50 schedules/agent, max depth
    3, min interval 60s by default.
  - *Major — on-idle semantics, compliance revalidation,
    wake-context workunit revalidation, session-scope tombstoning,
    user-scope workspace binding, wake-context redaction,
    fire-during-handoff.* Each got an explicit paragraph in the
    security-and-side-effect guardrails section clarifying the
    intended runtime behaviour.
  - *Minor — REASONING-PERSIST all-pinned overflow.* Soft-fails
    (drop oldest pins LRU) by default; `error` mode opt-in for
    operators who want hard failure.
  - *Minor — THINKING-MODE off + FORMAL-CLAIM rationale.* Explicit
    error rather than silent claim downgrade or silent thinking
    re-enable; caller chooses.
  - *Minor — composition table normative pointer.* Single source
    of truth is §COORDINATION; the §COORDINATION Composition
    section is the normative reference. Duplicate appearances
    elsewhere point to it.
  - *Minor — `interleaved-v5.11` profile.* AGENT-STATE added to
    the cap list (REASONING-PERSIST requires it for goal-hash
    keying; omission was an internal inconsistency).
  - *Minor — profile inheritance semantics.* Chained profile refs
    are declaration-time shorthand, not live links. Explicit
    guidance added to Capability Profiles section.
  - *Nit — 3–5× measurement basis, reject-learned
    most-recent disambiguation, K2.6 300-sub-agent DISPATCH
    mapping.* Each paragraph expanded with the specific answer.
  - *Major — L2 prompt injection sized by model.* The previous
    one-size-fits-all L2/L3 block (all 15 IR legends, every turn,
    every model) is replaced by a **teaching profile** axis —
    `native | compact | standard | full` — resolved from level +
    parse_mode + constrained_decode, or set explicitly per-model
    in the registry. Natively-trained models drop from a ~1700-char
    injection to ~120; high-compliance prompted models fall to
    ~400. The core-five languages (Python, JS/TS, Go, Rust, SQL)
    cover ~90% of traffic; languages outside a profile's base set
    get a single `[ir:<lang>]` line appended to the *user-role
    prefix* (not the system block) so the system stays
    bit-stable across turns and upstream provider prompt caches
    continue to hit. See §System Prompt Injection → "Teaching
    profile".

  Reference implementation (ozmalib.protocol.reasoning_persist,
  think_act, schedule, system_prompt, registry) and training-data
  factories updated in lockstep. 122/122 protocol integration
  tests pass (9 new tests cover the teaching-profile tiering).

- v5.9.2 (2026-04-20): Cross-session references. Solves the
  copy-paste-between-sessions pain: users say *"check the other
  session for details"* and it just works, with the natural-language
  utterance handled across three protocol layers rather than the
  primitive being developer-facing syntax that nobody types.
  
  **New capability: SESSION-REF.** Extends §Session Cache to allow
  content to be referenced across session boundaries with the same
  `{@REF}` machinery already used within sessions. Reuses every
  piece of existing infrastructure — session storage, EMBED
  semantic retrieval, workspace memory ACL, content addressing,
  compliance-tag propagation — so the net new surface is tiny.
  
  **Grammar additions:**
  
  - **`session` added to REF namespace table.** Enables:
    ```
    {@session:sess_abc123}              # by id
    {@session:django-migration}         # by user-assigned label
    {@session:sess_abc123/turn:17}      # specific turn
    {@session:sess_abc123#migration-plan} # tagged span within a session
    {@session:prev}                     # anaphora — last active session
    {@session:current}                  # this session (for self-reference in meta-ops)
    ```
  - **`[session:search]`** — search prior sessions semantically;
    returns candidate ids + labels + summaries + top-relevance
    snippets. Scope `user | workspace | shared`.
  - **`[session:label]`** — assign a user-addressable label to a
    session. Labels become valid REF values.
  - **`[session:share]`** — explicit cross-user share with
    `read_only` flag. Default: sessions are user-private.
  - **`[session-registry]`** — shim-injected input block at turn
    start containing recent-session summaries (id, label,
    last-active, 1-line summary, unresolved-action flag). ~100–200
    tokens; always available when SESSION-REF is active.
  
  **Three-layer natural-language handling.** The primitive is what
  the model emits; the natural utterance is what the user says.
  The bridge:
  
  1. **Shim pre-injects `[session-registry]`** every turn. Recent
     sessions are always ambient context; the model sees them
     without being asked.
  2. **Hint fires on trigger phrases.** Rules-engine entry for
     patterns like *"the other session"*, *"yesterday's chat"*,
     *"like we discussed earlier"*, *"previous conversation"*,
     *"last time"*. Fires a targeted reminder with pre-ranked
     candidates from workspace history.
  3. **Teaching prompt for untrained models.** Natural-language →
     primitive mapping examples land via v5.3 self-upgrade so even
     L1-instruction-quality emission works for models not trained
     on SESSION-REF natively.
  
  Net user experience: *"check the other session for details"* →
  model emits `[session:search]` or resolves a `{@session:...}`
  ref from registry → shim retrieves relevant chunks → context
  appears. User never sees the machine form.
  
  **Relevance extraction, not dump.** When a session ref is
  resolved, shim uses EMBED semantic retrieval to pull the
  relevant slice — it doesn't copy the entire session history into
  context. Same cost discipline as everything else; the existing
  session-cache content-addressing deduplicates across sessions
  automatically (chunks present in both sessions are stored once).
  
  **Privacy scope (normative):**
  
  - Sessions are user-scoped by default — one user's sessions
    cannot be ref'd by another user's sessions without explicit
    `[session:share]`.
  - Deleted sessions cannot be ref'd (ref resolution returns
    `session_deleted` error).
  - Sessions marked sensitive (via user flag or auto-flag from
    `contains-secret` compliance tag propagation) cannot be ref'd
    except from within the same session.
  - Cross-workspace refs require `X-Ozma-Session-Scope: global`
    which deployments can forbid.
  
  **Emergent behaviors unlocked by the primitive + subscriptions
  composition (v5.9):**
  
  - Threaded work across days: Tuesday's session ref'd from
    Thursday without manual recap.
  - Model-initiated cross-session lookup: *"I recall we discussed
    this — let me check"* → agent emits `[session:search]`
    autonomously.
  - Session-as-citation in WORK-UNIT status updates, decisions,
    reviews.
  - **Session subscriptions** — `[subscribe scope=session
    ref={@session:X} events=[...]]` reuses v5.9 COORDINATION
    subscription machinery to watch another session's state
    changes. "Tell me when that long-running dispatch finishes."
  
  **New headers:** `X-Ozma-Session-Scope: user | workspace | shared
  | global`, `X-Ozma-Session-Search-Limit` (default 5),
  `X-Ozma-Session-Search-Lookback` (default 30d),
  `X-Ozma-Session-Registry-Mode: full | summary | off`
  (default `summary`).
  
  **Response headers:** `X-Ozma-Session-Refs-Resolved` (count),
  `X-Ozma-Session-Refs-Tokens-Fetched`,
  `X-Ozma-Session-Search-Queries`,
  `X-Ozma-Session-Registry-Entries`.
  
  **Net cost.** ~400 lines of spec; one new capability fingerprint;
  four new primitives + one new REF namespace entry. Reuses 100%
  of existing session-cache + EMBED + memory-ACL + rules-hints
  infrastructure. Additive to v5.2-trained LoRAs via standard
  self-upgrade.
- v5.9.1 (2026-04-20): Tier-1 structural polish landing on top of
  v5.9's consolidation. Ten additions that each became cheap because
  the v5.9 primitives (Adapter / Gate / REF / Trace / Teaching /
  ATOMIC_GROUP) unified surface area that was previously scattered.
  No new capabilities; no breaking changes; additive to v5.2-trained
  LoRAs via the same self-upgrade mechanism.
  
  **Unified `TIME_REF` grammar.** One production for the ~20+
  scattered time fields (`deliver_at`, `deadline_iso`, `expires`,
  `ttl_seconds`, `expected_resume`, `expected_unblock`,
  `cool_down_seconds`, `gate_timeout`, `ack_timeout`,
  `days_remaining`, rate-limit windows). Four forms: ISO-8601
  absolute; `+30s`/`+1h`/`+2d` relative; named (`market_open`,
  `eod`, `end_of_sprint`, `next_standup`); event-triggered
  (`on_completion_of:{@workunit:X}`, `on_unblock`). Enables the
  new `Adapter kind=calendar` without any further spec work.
  
  **Unified `PREDICATE` grammar.** JSON-logic subset + TIME_REF +
  state references. One shape covers `escalate_if`, `only_if`,
  `precondition`, `resume_when`, gate `rule` predicates,
  subscription filters. Formalizes what was already scattered
  throughout the spec; no consumer migration required.
  
  **Universal `dry-run` mode.** Generalizes v5.5 MOTION dry-run to
  every side-effecting operation. `X-Ozma-Dry-Run: off | simulate
  | sandbox`. `simulate` runs all gates and emits trace entries
  tagged `dry_run: true` but skips side effects. `sandbox` runs in
  isolated workspace (per v5.8 `workspace_policy: sandboxed`) with
  external egress blocked. Enables deployment validation, gate-
  policy testing, dispatch preview at zero cost.
  
  **Idempotency as first-class contract.** Every side-effect-
  producing operation (EMIT, MOTION dispatch, DISPATCH with
  external calls, PM writes, channel posts) requires an
  `idempotency_key`. Default derivation: `sha256(turn_id +
  operation_kind + payload_digest)[:16]`. Shim maintains dedup
  window (default 24h, configurable per-adapter). Replay within
  window returns prior trace entry instead of re-executing.
  Addresses the documented "LLMs blindly retry" failure mode. New
  error kinds: `idempotency_key_missing`,
  `idempotency_replay_detected` (informational).
  
  **`{@secret:NAME}` REF namespace + trace redaction contract.**
  Credentials referenced as refs in adapter configs, emit bodies,
  gate policies. New `Adapter kind=secret-store` resolves from env
  / Vault / AWS Secrets Manager / GCP Secret Manager. **Redaction
  contract (normative):** values resolved from `{@secret:*}` refs
  are never written to trace, telemetry, or logs; stored as
  `sha256[:8]` fingerprint instead. Compliance tag
  `contains-secret: true` auto-applied.
  
  **Unified `RATE_LIMIT` declaration.** One shape: `{ scope,
  window (TIME_REF), max, overage: queue | reject | warn }`.
  Scope: `per-user | per-agent-pair | per-session | per-deployment
  | per-workunit | per-adapter`. Consolidates v5.9 message rate
  limits, v5.8 DISPATCH three-axis budgets, v5.8 monthly budget,
  and implicit per-cap rate limits.
  
  **Composition Patterns library.** New top-level reference
  section naming canonical combos: *Dev-Test-Review Flow*,
  *N-Model Consensus*, *Mini-Project Promotion*, *Handoff Chain
  with Unblock Cascade*, *Critical Section via Claim + Request*,
  *Trust-Building Gates*, *Payment-Gated Emit*, *Cross-Agent
  Verification*. Helps training data converge on canonical
  shapes; reduces reinvention.
  
  **`[protocol:describe]` introspection.** A client or model can
  request the full deployed surface — enabled capabilities, loaded
  adapters by kind, active compliance tags, effective caps, shim
  version, trace backend. Same shape as v5.7 CAP-PROBE; returns
  the whole deployment configuration. Massive debugging and
  onboarding value.
  
  **Deprecation policy.** Short subsection in §Extension Points.
  Legacy aliases preserved for 2 major versions by default.
  Deprecation signal: `X-Ozma-Deprecated-Feature: <name>` response
  header + trace entry `kind=deprecation-warning`. Retirement
  requires removing from VERSION_CAP_SETS + shim parser. Training
  signal fed back to next LoRA.
  
  **Verification continuum documentation.** Single table
  orienting authors across the soft-to-hard verification surface:
  `CONFIDENCE scale → CLAIM → FORMAL-CLAIM → verify-dispatch
  (N-model) → peer review → human-gate`. Guidance on when to pick
  which. More documentation than spec, but closes an orientation
  gap.
  
  **New client headers:** `X-Ozma-Dry-Run: off | simulate |
  sandbox`, `X-Ozma-Idempotency-Key-Window: <seconds>` (override
  default 24h dedup window per-deployment).
  
  **New response headers:** `X-Ozma-Dry-Run-Active: boolean`,
  `X-Ozma-Idempotency-Replays: <count>`,
  `X-Ozma-Deprecated-Feature` (when applicable).
  
  **Positioning.** v5.9 collapsed ~16 parallel adapter specs into
  one unified primitive and saved ~1000 lines of parallel spec.
  v5.9.1 uses the cleared surface area to add features that would
  previously each have required their own section. Net line cost:
  ~500 lines for ten improvements, versus ~2500 lines if each had
  landed as a standalone capability in the pre-v5.9 structure.
- v5.9 (2026-04-20): Peer-to-peer coordination as a first-class
  protocol layer, orthogonal to v5.8's hierarchical DISPATCH. Agents
  exchange typed messages, coordinate via blocker graphs, claim
  scopes, broadcast on channels, and subscribe to events — the way
  a human team does. **Default transport is native Ozma**; A2A v1.0
  becomes one of several interop adapters (translation to A2A Tasks
  is declared lossy — A2A's weaknesses, *skill-definition ambiguity,
  quadratic point-to-point scaling, circular-delegation deadlocks,
  verbose envelopes, no native coordination primitives*, are
  precisely what Ozma's existing machinery already solves).
  
  **Structural consolidation (this release).** Review of the v5.0→v5.8
  growth identified significant overlap. v5.9 consolidates:
  
  - **Unified `Adapter` primitive** — one YAML shape with `kind:`
    discriminator replaces ~16 parallel adapter specs
    (client-adapters, model-adapters, pm-adapters, channel-adapters,
    agent-transports, sensor-profiles, device-profiles, motion-
    transports, tts / voice / theme / style / persona / memory /
    emit / gate adapters). Same id + auth + backend + scope shape
    everywhere; kind determines the body schema.
  - **Unified `GATE` primitive** — one pre-action check with kind
    discriminator (`human | policy | rules | algorithm | quorum |
    external-api | informational | payment | kill-switch | composite`)
    subsumes v5.8 `human-gate`, v5.5 MOTION `requires_confirm`,
    CONFIDENCE review queues, v5.9 claim conflicts, compliance
    pre-injection filters, proposed v5.10 GATE. Decision set
    `approve | deny | modify | delay | escalate` is universal. The
    conceptual distinction between "gate" and "review" dissolves —
    they were always the same shape.
  - **Unified `REF` syntax** — `{@NAMESPACE:ID}` production with a
    namespace table. Existing forms (`{@HASH}`, `{@embed:id}`,
    `{@agent:HASH}`, `{@img:HASH}`, `{@math:EN}`) conform and are
    formalized under one grammar.
  - **Unified `TRACE` / audit structure** — one schema; entries
    tagged by source (`dispatch | motion | message | emit | gate`).
    Subsumes v5.8 `rollup_ledger`, v5.5 motion audit log, coordination
    message log. Collapses `X-Ozma-Rollup-*`, `X-Ozma-Motion-Audit-*`,
    and coordination headers into one `X-Ozma-Trace-*` family. One
    trace_id spans a user turn and every downstream action.
  - **Unified runtime teaching** — one section documents the
    mechanics (initiator → teaching prompt → peer ack → two-way
    verification → single-flip revert). Three directions (v5.3
    shim→model, v5.7 CAP-PROBE model→client, v5.9 agent→peer-agent)
    reference the section rather than each re-specifying.
  - **`ATOMIC_GROUP`** replaces `TRANSACTION` (v1.2 code edits) and
    `FENCED_TXN` (v5.7 visual edits). Same semantics; content-type
    parameter; reduces grammar.
  - **Message types reduced 14 → 10.** `review-request` /
    `review-response` collapse to `request purpose=review` /
    `response verdict=...`; `question` / `answer` collapse to
    `request purpose=question` / `response`. `request` and
    `response` become the workhorses.
  
  **New capability: COORDINATION.** One cap, six primitives:
  - **Agent identity** — stable id bound to role × work-unit, scope
    (`workspace` | `user` | `global`), signed by Ozma native signed-
    identity scheme (same crypto as A2A Signed Agent Cards, native
    envelope).
  - **Messages** — ten types (post-consolidation): `request`,
    `response`, `ack`, `block`, `unblock`, `handoff`, `claim`,
    `release`, `heads-up`, `status`. Each has a typed body schema
    (structured, not free-form prose — prevents prompt-injection and
    specification failure; MAST 42% bad-specs bucket).
  - **Channels** — workspace-scoped broadcast groupings; backed by
    the unified `Adapter` primitive (kind=channel).
  - **Subscriptions** — declared interests with `priority_floor`
    scale (anti-fatigue filter).
  - **Blocker graph** — typed WORK-UNIT edges (`blocked-by`,
    `blocks`, `related-to`, `duplicates`, `parent-of`, `child-of`).
    When a blocker resolves, shim auto-emits `unblock` messages to
    downstream-edge owners. Extends v5.8 WORK-UNIT.
  - **Inbox + wake** — shim injects `[inbox]` at turn start with
    priority/TTL filtering; default wake is `passive`; `message-
    triggered` opt-in wakes the agent on priority-floor-crossing
    messages.
  
  **Native Ozma transport, A2A as interop adapter.** Default
  transport is `ozma-native` — Ozma grammar over HTTPS; full
  Session Cache / IR / WIRE / LEARNED-BPE compression applies
  natively. A2A adapter (`agent-transport kind=a2a`) translates
  inbound/outbound messages to A2A Tasks/Messages/Parts with
  **declared information loss** — blocker-graph edges collapse to
  metadata; claims drop with a warning; subscriptions are not
  representable. `mcp-agent-mail` and `amq-file` available as
  additional adapters. One adapter concept, three kinds.
  
  **Native compression is free.** Native peer messages are ~60–120
  tokens (already-compressed Ozma grammar; `{@agent:HASH}` refs to
  Agent Cards via Session Cache; WIRE on prose bodies). No parallel
  A2A-IR symbol table to maintain. A2A-adapter traffic carries
  standard A2A verbosity as the translation cost.
  
  **Hardening — addresses documented multi-agent failure modes:**
  - **MAST taxonomy** (2026 multi-agent failure study): 42% bad
    specs / 37% coordination breakdowns / 21% weak verification.
    v5.9 reduces the 37% via typed messages + blocker graph +
    claims; the 42% via structured bodies + WORK-UNIT step-id
    references; the 21% via composition with v5.8 `verify` strategy
    + review-purpose `request` messages.
  - **Recursive handoff cycles** (A2A's documented "circular
    delegation and deadlock" failure). Mitigations: `handoff_depth_
    max` per thread (default 3, escalates via unified `GATE`
    kind=human on cap); oscillation detector (A→B→A→B triggers
    auto-escalation); explicit acceptance (`handoff` requires
    receiver `ack: accepted`; bounced-back counts toward depth).
  - **Per-agent-pair rate limits** — default 60 messages per
    5-minute window.
  - **Message dedup** via UUID id across direct + channel delivery.
  - **Compliance tags** on messages + transport-layer filter.
  
  **Composition with existing capabilities:**
  - vs. DISPATCH — DISPATCH is tree-shaped orchestrator-worker;
    COORDINATION is graph-shaped peer-to-peer. Orthogonal.
  - vs. HANDOFF (v1.2) — HANDOFF is one-shot full-session transfer
    between models. COORDINATION `handoff` is ownership transfer
    between peer agents maintaining separate sessions.
  - vs. MEMORY — messages persist workspace-scoped (90-day idle);
    memory is durable knowledge store.
  - vs. PM adapters (v5.8) — channels can mirror Slack/Discord/
    Teams via channel adapters (now unified under the Adapter
    primitive); blocker-graph edges round-trip to Linear/Jira
    dependency fields.
  
  **New client headers:** `X-Ozma-Agent-Id`, `X-Ozma-Agent-Scope`,
  `X-Ozma-Agent-Wake`, `X-Ozma-Coordination-Transport` (default
  `ozma-native`; `a2a` / `mcp-agent-mail` / `amq-file` as adapters),
  `X-Ozma-Inbox-Priority-Floor`, `X-Ozma-Handoff-Depth-Max`,
  `X-Ozma-Message-Rate-Limit`.
  
  **Response headers** (now unified under `X-Ozma-Trace-*`):
  `X-Ozma-Trace-Id`, `X-Ozma-Trace-Entries-Count`,
  `X-Ozma-Trace-Cost-Usd`, `X-Ozma-Trace-Latency-Ms`,
  `X-Ozma-Trace-Side-Effects`, plus coordination-specific
  `X-Ozma-Messages-Processed-Count`,
  `X-Ozma-Handoff-Depth-Used`, `X-Ozma-Oscillation-Detected`,
  `X-Ozma-Inbox-Unread-Count`, `X-Ozma-Coordination-Transport-Used`,
  `X-Ozma-A2A-Translation-Loss` (when A2A adapter used).
  
  **Positioning.** Peer coordination makes Anthropic's Agent Teams
  layer (2026 Trends Report) addressable at protocol level rather
  than framework-level. Convergent design with OpenClaw teams RFC
  (blocked + claimed states) and mcp-agent-mail (git-backed mailbox
  + file reservations = claims). A2A adapter ensures interop with
  the 150+ org A2A ecosystem where needed; native Ozma is the
  default because A2A's shape is strictly lossier for coordination
  semantics.
  
  **New capability: COORDINATION.** One cap, six primitives:
  - **Agent identity** — stable id bound to role × work-unit, scope
    (`workspace` | `user` | `global`), signed via A2A v1.0 Agent
    Cards.
  - **Messages** — fourteen types: `request`, `response`, `ack`,
    `block`, `unblock`, `handoff`, `review-request`, `review-response`,
    `claim`, `release`, `question`, `answer`, `heads-up`, `status`.
    Each has a typed body schema; bodies are structured, not
    free-form prose (prevents prompt-injection and specification
    failure — MAST 42% bad-specs bucket).
  - **Channels** — workspace-scoped broadcast groupings; adapter-
    backed (ozma-native default; Slack / Discord / Teams / IRC via
    channel adapters mirroring PM-adapter shape).
  - **Subscriptions** — declared interests by channel, work-unit,
    event type, and `priority_floor` scale (anti-fatigue filter,
    parallel to v5.8 human-gate rising-threshold).
  - **Blocker graph** — typed WORK-UNIT edges (`blocked-by`,
    `blocks`, `related-to`, `duplicates`, `parent-of`, `child-of`).
    When a blocker resolves, shim auto-emits `unblock` messages
    to downstream-edge owners. Extends v5.8 WORK-UNIT.
  - **Inbox + wake** — shim injects `[inbox]` at turn start with
    priority/TTL filtering; default wake is `passive`; `message-
    triggered` opt-in wakes the agent on priority-floor-crossing
    messages.
  
  **Transport stack (A2A v1.0).** Default wire is Google/Linux-
  Foundation A2A v1.0 over JSON-RPC — full interoperability with
  the 150+ org A2A ecosystem (Atlassian, Box, Cohere, Intuit,
  LangChain, MongoDB, PayPal, Salesforce, SAP, ServiceNow, UKG,
  Workday; production at Microsoft / AWS / Salesforce / SAP /
  ServiceNow). Alternate backends via `ozmalib/agent_transports/
  *.yaml`: `mcp-agent-mail` (git-backed, file-reservations pattern),
  `amq-file` (Maildir-style local), `grpc-a2a` (binary wire for
  bandwidth-constrained paths). Same pattern as v5.7 client adapters
  and v5.8 PM adapters: Ozma provides protocol semantics, ecosystem
  standards provide wire.
  
  **A2A-IR compression.** A2A's JSON-RPC default is verbose — every
  message carries `{jsonrpc:"2.0", method:..., params:..., id:...}`
  (~40–60 structural tokens) plus full Agent Cards (200–600 tokens
  each); gRPC binding compresses network bytes but not LLM context
  tokens. Ozma reuses its existing compression stack:
  - **`A2A-IR` envelope mapping** — versioned symbol table mapping
    method names (`message/send`, `tasks/get`, etc.) to single-token
    symbols (`→send`, `→get`, etc.); canonical param keys shortened;
    version/id elided when inferrable. Ships as a WIRE-table-shaped
    versioned fingerprint.
  - **Session Cache content-addressing for Agent Cards** — first
    turn sends full card + hashes it; subsequent turns reference
    `{@agent:sha256[:6]}`. Signed cards (v1.0) preserved via
    hash + signature pair.
  - **WIRE / IR / LEARNED-BPE** applied to message bodies (prose /
    code / identifiers) per existing semantics.
  - Savings: typical peer message drops from ~400–600 tokens
    (standard A2A) to ~60–120 tokens (compressed) — 75–85%.
  
  **Peer teaching — v5.3 self-upgrade applied to peer transport.**
  Three tiers, model-capability-gated:
  - **Untaught** — standard A2A only. Cost 0. Used when target is
    L0/L1 or one-shot exchange.
  - **Inline-hinted** — ~50–80-token legend on first message
    (`// →send=method:message/send; p:=params:; ...`). Capable peer
    (L2+) decodes on first read. Break-even: ~2 messages.
  - **Pre-taught** — full teaching prompt (~400–500 tok) at
    handshake; peer model acks `ozma_compression_ack: understood`.
    Break-even: ~2 messages. Used for extended conversations.
  
  Two-way verification on first 2–3 messages carries both forms
  (compressed + standard); confusion triggers single-flip revert.
  Same three-direction symmetry as v5.3 (shim→model), v5.7 CAP-PROBE
  (model→client); now v5.9 agent→peer-agent. The ceiling is peer
  model tier, not protocol.
  
  **Hardening — addresses documented multi-agent failure modes:**
  - **MAST taxonomy** (2026 multi-agent failure study): 42% bad
    specs / 37% coordination breakdowns / 21% weak verification.
    v5.9 reduces the 37% via typed messages + blocker graph +
    claims; the 42% via structured bodies + WORK-UNIT step-id
    references; the 21% via composition with v5.8 `verify` strategy
    + review messages.
  - **Recursive handoff cycles** (documented failure: "agents bounce
    tasks back and forth without resolution"). Mitigations:
    `handoff_depth_max` per thread (default 3, escalates to
    `human-gate` on cap); oscillation detector (A→B→A→B triggers
    auto-escalation); explicit acceptance (`handoff` requires
    receiver `ack: accepted`; bounced-back counts toward depth).
  - **Per-agent-pair rate limits** — configurable; default 60
    messages per 5-minute window; prevents runaway loops.
  - **Message dedup** — message ids are UUIDs; shim deduplicates
    on id across direct + channel delivery.
  - **Compliance tags on messages + transport-layer filter** —
    `compliance: [us-only, soc2]` on a message prevents delivery to
    peers outside the tag set. Same pre-injection-filter pattern as
    v5.8 dispatch_policy.
  
  **Composition with existing capabilities:**
  - vs. DISPATCH — DISPATCH is tree-shaped orchestrator-worker;
    COORDINATION is graph-shaped peer-to-peer. Orthogonal; compose
    freely (dispatched sub-worker still has peer identity and
    inbox).
  - vs. HANDOFF (v1.2) — HANDOFF is one-shot full-session transfer
    between models. COORDINATION `handoff` message is ownership
    transfer between peer agents maintaining separate sessions.
  - vs. MEMORY — messages persist workspace-scoped (90-day idle);
    memory is durable knowledge store.
  - vs. PM adapters (v5.8) — channels can mirror Slack/Discord/
    Teams via channel adapters; blocker-graph edges can round-trip
    to Linear/Jira dependency fields via PM adapter write-scope.
  
  **New client headers:** `X-Ozma-Agent-Id`, `X-Ozma-Agent-Scope`,
  `X-Ozma-Agent-Wake`, `X-Ozma-Coordination-Transport`,
  `X-Ozma-Coordination-Wire: standard | ozma-compressed`,
  `X-Ozma-Coordination-Teaching: off | inline | pre-taught`,
  `X-Ozma-Inbox-Priority-Floor`, `X-Ozma-Handoff-Depth-Max`,
  `X-Ozma-Message-Rate-Limit`.
  
  **Response headers:** `X-Ozma-Messages-Processed-Count`,
  `X-Ozma-Messages-Sent-Count`, `X-Ozma-Handoff-Depth-Used`,
  `X-Ozma-Oscillation-Detected`, `X-Ozma-Coordination-Wire-Used`,
  `X-Ozma-Teaching-Applied`, `X-Ozma-Inbox-Unread-Count`.
  
  **Positioning.** Peer coordination makes Anthropic's Agent Teams
  layer (2026 Trends Report) addressable at protocol level rather
  than framework-level. Convergent design with OpenClaw teams RFC
  (blocked + claimed states) and mcp-agent-mail (git-backed mailbox
  + file reservations = claims). A2A v1.0 as transport floor ensures
  interoperability with the 150+ org ecosystem.
- v5.8 (2026-04-20): Model-directed dispatch, work-unit tiering, and
  project-management as first-class protocol concerns. The classifier
  inverts: instead of Ozma guessing difficulty from prose and routing,
  the planner that has already thought through the problem emits
  typed dispatch directives with visible reasoning, and the shim
  executes them. The whole protocol's meta-thesis — *context is the
  program, better data → better decisions* — applied to routing and
  PM.
  
  **New input-side capability: ROUTING-CONTEXT.** Shim injects
  user prefs (cost / latency / quality priorities), monthly budget
  remaining, available models (id / tier / cost / p50-latency / caps
  / compliance flags), and current-session ledger (spent to date,
  turn count). Freshness via `ttl_seconds` and mid-session refresh on
  expiry. Deadline-aware: inherits WORK-UNIT deadlines so the model
  picks `race` near deadline, `cheapest-capable` when loose. Research
  grounding: RouteLLM (Ong et al. 2024 preference routing); Router-R1
  (Lin et al. 2025 multi-round routing); InferenceDynamics 2025
  capability-profile routing; LLM API pricing Q2 2026 observed a
  1000× cost spread — surfacing cost-per-model to the planner is
  load-bearing.
  
  **New unified capability: WORK-UNIT (three tiers).**
  - `task-list` — session-scoped, ephemeral flat checklist
  - `mini-project` — workspace-scoped, structured goal + steps +
    progress + durable 90 days
  - `project` — external-system backed (Linear / Jira / GitHub Issues
    / Asana / Notion / ClickUp / generic-MCP) with full ticket /
    sprint / constraints / siblings / prior decisions
  
  One capability, one grammar family, tier as attribute. Matches
  MEMORY's session/workspace/user scope pattern. Includes:
  promotion path (tier ↑, never ↓; side-effect emits PM ticket when
  `mini-project → project`); decay rules (task-list LRU at session
  end; mini-project 90-day idle; project PM-system-owned);
  `workspace_policy: persistent | ephemeral | sandboxed` (Devin vs
  Codex patterns); `code_refs: [{repo, branch, commits?, files?}]`
  (explicit ticket ↔ code linkage); `paused_at / paused_reason /
  expected_resume` (cross-session continuity); `on_complete: [...]`
  declarative hooks (PM close / Slack notify / webhook);
  `dispatch_policy` (compliance / residency / model-allowlist);
  `approver_rules: any_owner | all_owners | from_team:X | named:<id>`
  (multi-stakeholder gates). Research grounding: Anthropic 2026
  Agentic Coding Trends Report; Cognition Devin 2.0 Interactive
  Planning; MCP project/task-management ecosystem (Atlassian Remote
  MCP, Linear MCP, GitHub Issues MCP, awesome-mcp-servers catalog).
  
  **PM adapters:** `ozmalib/pm_adapters/*.yaml` — linear, jira,
  github-issues, asana, notion, clickup, ozma-native, **plus
  `mcp-passthrough`** which wraps any MCP server (a single adapter
  inherits the entire MCP ecosystem). Adapters declare how to pull
  project/ticket state into PROJECT-CONTEXT and how to write back on
  WORK-UNIT completion hooks (writes opt-in per adapter; default is
  read-only).
  
  **New output-side capability: DISPATCH — strategies as first-class.**
  Model emits typed delegation directives with visible reasoning and
  budget envelope. Strategy family (9):
  
  | Strategy | Use |
  |---|---|
  | `cheapest-capable` | Default. One target. Routine execution. |
  | `cascade` | Try cheap; escalate on low-confidence or claim fail. |
  | `race` | N parallel targets; accept fastest-valid. Urgency. |
  | `vote` | N targets; majority or consensus + tiebreak. Design decisions. |
  | `blend` | N targets; synthesize outputs into one. (LLM-Blender pattern.) |
  | `ensemble` | N targets; present all outputs. Generative diversity. |
  | `split` | Decompose first; then dispatch sub-parts. Depth cap 2. |
  | `human-gate` | Pause for approval. Destructive / high-blast-radius. |
  | `self` | Decline to dispatch; do inline (for explicit auditability). |
  
  **Three-axis budget envelope (not two):**
  `{ cost_usd_max, latency_ms_max, side_effects: { ci_minutes_max,
  external_calls_max, tokens_tree_max } }`. Third axis (side-effects)
  captures CI runs, API calls, bytes written — orthogonal to token
  cost, per Anthropic's implementation guide
  (`max_wall_clock_minutes`, `max_ci_minutes`, `max_model_cost_usd`).
  
  **Cost attribution chain solves Anthropic's open problem.**
  `dispatch_trace_id` (W3C Trace Context propagation) + 
  `parent_dispatch_id` + `rollup_cost_usd` / `rollup_latency_ms` /
  `rollup_ledger` emitted at turn end. A user turn that fans out to
  5 nested dispatches reports one total, with the per-dispatch
  breakdown available for audit. Quoting Anthropic's 2026 report:
  *"Multi-agent workflows generate unpredictable token costs. Claude
  Code currently offers no native cost tracking by user, team, or
  project. For enterprises running dozens of concurrent agents, each
  spawning subagents with their own tool calls, cost governance
  remains an unsolved problem."* v5.8 makes this a headline feature.
  
  **Dispatch IO semantics:**
  - Streaming modes: `X-Ozma-Dispatch-Stream: interleave |
    buffer | separate-channel`. Default `interleave` with
    `[dispatch-stream id=d:1]…[/dispatch-stream]` frames.
  - Cancel: `[dispatch:cancel id=d:1 reason=...]` from client /
    shim-emitted on user abort; propagates to all descendants.
  - Provenance: final-output tagging via `[from=d:N]…[/from]`
    so users can audit which model wrote which span. Trust + debug
    essential.
  - Context inheritance: sub-dispatch sees only `context_required:
    [...]` explicit refs, not parent's full context. Cheaper,
    matches Anthropic's subagent model.
  
  **Human-gate fatigue mitigation** — per Anthropic's 93% approval-
  through-rate / 17% destructive-action false-negative finding.
  Required controls: `rising_threshold` (auto-approve N+1 after N
  incident-free approvals, reset on any denial); `batch_similar`
  (group structurally similar gates); `cool_down` (mandatory
  re-read time); `explain_on_deny` (denial requires text reason;
  feeds back to future dispatch rationale).
  
  **Compliance policy pre-injection filter.** If WORK-UNIT declares
  `dispatch_policy: { compliance: [hipaa, us-only] }`, shim filters
  `available_models` in ROUTING-CONTEXT *before* the model sees
  them. Regulated data combined with 17%-classifier-error selection
  is unacceptable; filter at the data layer, not trust the model
  to self-limit. Violations raise `dispatch_policy_violated`.
  
  **Strategy-quality telemetry** — beyond whether model-driven routing
  beat the classifier, measure whether each strategy earned its
  multi-target cost: `race_realized_vs_expected_latency`,
  `vote_disagreement_rate`, `ensemble_diversity_score`,
  `cascade_escalation_rate` and `cascade_escalation_success_rate`.
  Tells ops which strategies to recommend and which to prune.
  
  **Separation contract: PLAN × WORK-UNIT × AGENT-STATE.** Three
  overlapping but distinct concepts:
  - **PLAN** — turn-scoped model output, "here's my plan for this turn."
  - **WORK-UNIT** — session+/durable task *definition* (goal, steps,
    structure).
  - **AGENT-STATE** — cross-turn execution *state*: where in a
    workflow, what's cached, what's blocked. Tracks progress *against*
    a WORK-UNIT.
  A PLAN references WORK-UNIT step IDs. AGENT-STATE tracks completion.
  Spec makes the split explicit; step-id cross-references formalized.
  
  **Headers (cumulative):**
  `X-Ozma-WorkUnit-Tier`, `X-Ozma-WorkUnit-Id`, `X-Ozma-PM-Adapter`,
  `X-Ozma-Dispatch: shim-execute | client-execute | forbid`,
  `X-Ozma-Dispatch-Stream: interleave | buffer | separate-channel`,
  `X-Ozma-Dispatch-Strategies: <allowlist>`,
  `X-Ozma-Dispatch-Depth-Max`, `X-Ozma-Routing-Context-TTL`,
  `X-Ozma-Compliance: <tags>`, `X-Ozma-Approver: <rules>`.
  
  Response headers: `X-Ozma-Dispatch-Trace-Id`,
  `X-Ozma-Rollup-Cost-Usd`, `X-Ozma-Rollup-Latency-Ms`,
  `X-Ozma-Dispatches-Count`, `X-Ozma-Workunit-Touched`.
  
  **What this makes first-class** — Anthropic's four 2026 priorities
  (multi-agent coordination, human-agent oversight, extending
  agentic coding beyond engineering, security architecture) become
  protocol concerns rather than per-deployment bolt-ons. The
  classifier guessing game is deprecated for L2+ sessions; remains
  as fallback for C0/L0 pairings where no planner exists.
- v5.7 (2026-04-20): Input modalities, format tiers, structured visual
  feedback, image editing, capability probing, and the two
  architectural clarifications that complete the deployment picture —
  explicit shim capabilities and explicit protocol-compliance
  adaptation for either end. This is a large consolidated release.
  
  **Input capabilities (symmetric to output modalities):**
  - **VISION** — perception-pipeline protocol. YOLO / SAM 2 /
    MediaPipe / OCR / VLM / composed pipelines flow structured
    perception to the LLM. Sensor profiles in `ozmalib/sensors/*.yaml`
    declare schema, pipeline, output format, transport, privacy.
    Research grounding: COCO format (bbox/keypoint/mask/caption);
    SAM 2 (Meta, July 2024 — 51k+ videos, 600k+ masklets); MediaPipe
    Holistic (540+ keypoints); ONVIF + RTSP; NVIDIA DeepStream 9;
    ROS 2 `vision_msgs`; current VLMs (GPT-4o, Claude Vision, Qwen3-VL).
  - **LISTEN** — audio-perception pipelines. Whisper-class STT +
    VAD + diarization + prosody + emotion + sound-event detection.
    Grounding: OpenAI Whisper / WhisperX; Hume AI prosody-input;
    Banse & Scherer 1996 symmetric input mapping; Silero VAD;
    pyannote.audio.
  - **SENSOR** — non-AV sensor streams. IMU / GPS / depth (LIDAR/ToF)
    / thermal / pressure / bio-signals. Same sensor-profile
    abstraction. Integrates with MOTION device state feedback.
  
  **Format tiers (5 canonical + extensible):**
  `structured-only | reference | inline-thumb | inline-full |
  raw-stream`. Default is `structured-only` (cheapest; no pixels to
  LLM). AVIF as 2025 default lossy image format (~50% smaller than
  JPEG, ~25% smaller than WebP, sharp-gradient friendly). H.265 for
  real-time video (hardware-accelerated everywhere; AV1 too slow for
  real-time encode but optimal for archival — YouTube >75% AV1,
  Netflix ~30% AV1 Dec 2025). JPEG XL for industrial/scientific/
  medical specialty. Raw formats (raw-rgb / raw-yuv420 / raw-bayer)
  for pipeline-internal use; custom format IDs open-ended.
  
  **VISION-ANNOTATE** — structured visual feedback. 8 primitives
  (bbox, arrow, highlight, point, text, mask, line, polygon) with
  IDs for cross-turn reference. COCO-compatible where applicable.
  Rendering modes: `overlay-svg | overlay-native | burn-in |
  structured-only`.
  
  **VISION-EDIT** — streaming region-addressed image editing,
  mirroring code EDIT for images. Operations: `replace | inpaint |
  add-layer | remove-layer`. Region addressing via bbox, named-region
  (prior annotation ID), or mask RLE. Composes with TRANSACTION for
  atomic edit groups. Enables co-pilot image editing, iterative
  image generation, real-time avatar adjustment, collaborative
  whiteboarding. Grounding: InstructPix2Pix (2023); SDEdit; SAM 2
  editing workflows; real-time diffusion (SDXL Turbo, LCM-LoRA).
  
  **Capability probing** — model-initiated client-capability
  negotiation (inverse of the existing shim→model handshake). Model
  emits `capability-probe:` with requested features; shim forwards
  to client; client responds with supported set. Session upgrades
  for supported items. Enables: "client declared markdown but has a
  modern browser — probe for rich-html; upgrade on confirm."
  Symmetric to v5.3 self-upgrade for model caps.
  
  **Shim Profile + Transform Bridges** — the shim is now a
  first-class protocol participant with declared capabilities, not an
  implicit passthrough. Shim profile declares renderers (html-to-
  image, svg-to-raster, markdown-to-html, vrm-to-video, etc.),
  transcoders (AVIF↔WebP↔JPEG, H.265↔H.264), TTS adapters (SSML→
  audio/mp3 via ElevenLabs/Azure/OpenAI), vision pipelines available
  server-side (YOLO/SAM/MediaPipe/VLM), and composed bridges.
  
  **Effective capability computation** — model emits at the highest
  level the shim can bridge to the client, not the lowest common
  client capability. Handshake advertises:
  `effective = client ∪ { x | shim_transform(x → y) ∧ y ∈ client }`.
  A phone client with `jpeg, plain, audio-mp3` paired with a shim
  that can `rich-html → png` + `ssml → audio/mp3` + `vrm → h264`
  effectively delivers full multimodal to the phone. Deployment
  value-add is explicit, declarative, observable.
  
  **Protocol Compliance Adaptation** — the spec now formally handles
  the full cross-product of client × model compliance. Client tiers:
  C0 (stock — plain HTTP, OpenAI-compatible or similar), C1 (partial
  Ozma), C2 (full Ozma). Model tiers: L0–L3 as previously specified.
  Client adapters in `ozmalib/client_adapters/*.yaml` (openai-chat-
  completions, anthropic-messages, google-generate-content,
  livekit-room, daily-room, websocket-chat, ozma-native). Model
  adapters in `ozmalib/model_adapters/*.yaml` (ozma-native,
  openai-chat-tools, anthropic-messages-tools, completion-api,
  local-llama-cpp, hf-inference). Four-quadrant behavior specified.
  Stock-client + Ozma-model: shim strips structure, renders rich
  output to client format. Ozma-client + stock-model: shim injects
  teaching prompts (self-upgrade from v5.3), parses stock output
  best-effort into Ozma structure. Neither compliant: passthrough
  with baseline value-add (routing, caching, audit, rate-limit,
  cost, security). New `X-Ozma-Mode: passthrough | default |
  force-protocol` header.
  
  **New client headers (cumulative):** `X-Ozma-Vision-Sources`,
  `X-Ozma-Listen-Sources`, `X-Ozma-Sensor-Sources`,
  `X-Ozma-Vision-Pixels`, `X-Ozma-Vision-Format`,
  `X-Ozma-Vision-MaxDim`, `X-Ozma-Shim-Profile`,
  `X-Ozma-Effective-Caps`, `X-Ozma-Shim-Transform-Budget`,
  `X-Ozma-Shim-Cost-Budget`, `X-Ozma-Mode`.
  
  **Response headers:** `X-Ozma-Shim-Transforms-Applied`,
  `X-Ozma-Shim-Transform-Latency-Ms`,
  `X-Ozma-Shim-Transform-Cost-Usd`, `X-Ozma-Client-Tier`,
  `X-Ozma-Model-Tier`, `X-Ozma-Adapter-Applied`.
  
  **Architecture: four negotiation dimensions.** With v5.7 the
  protocol formalizes four independent negotiation axes:
  (1) model compliance (L0–L3, plus self-upgrade from v5.3);
  (2) client compliance (C0–C2);
  (3) shim capabilities and transforms (declared profile);
  (4) capability probes in both directions (shim→model at
  handshake; model→client at runtime).
  Any session is a point in this 4-D space; the shim's job is to
  deliver useful behavior at every point, including the
  "neither-compliant" corner where it acts as value-add
  infrastructure for an otherwise-stock LLM proxy.
- v5.5 (2026-04-20): Embodiment. Two new capabilities and a device-
  profile registry extend persona into controllable bodies — avatars,
  robots, vehicles, drones, cranes, IoT actuators.
  
  **AVATAR** — persona visual identity (static portrait, 3D model
  reference via VRM 1.0 / glTF 2.0, generative description). Engine-
  specific mappings to Ready Player Me, VRChat, VRM, glTF. Research
  grounding: VRM 1.0 (Khronos × VRM Consortium standardization); ARKit
  52 blendshapes (derived from FACS); Cassell / Gratch / Bickmore
  embodied-conversational-agent literature demonstrating rapport-
  multiplier effect of embodiment.
  
  **MOTION** — generalized animation/control for any embodied system.
  Device-targeted (`[motion device=...]...[/motion]`). Named actions
  with parameters, or low-level joint/actuator commands. Same fenced
  syntax for avatar animation (VRM + ARKit blendshapes + gaze), robot
  trajectories (URDF joint positions), vehicle maneuvers (Ackermann
  actuators), drone flight (multirotor thrust + attitude). Cross-
  embodiment via the DEVICE profile system — one LLM output drives
  heterogeneous hardware. Research grounding: ROS 2 / `ros2_control`
  (`FollowJointTrajectory` action as the execution target); URDF
  (joint types: revolute/continuous/prismatic/fixed); OPC UA Robotics
  Companion Spec v1.02 (VDMA, industrial standard); MoveIt motion
  planning; OMG DDS 1.4 real-time transport; Autoware `VehicleCmd`;
  VMC protocol + ARKit blendshapes for VTubers; RT-2 / PaLM-E / RT-X
  (DeepMind) demonstrating LLM → robot-action is tractable; Open
  X-Embodiment (22 robot types, 527 skills, 160k+ tasks — the
  "ImageNet of robot learning").
  
  **DEVICE profile system** — YAML files in `ozmalib/devices/*.yaml`
  declaring each controllable system's schema (URDF/VRM/Ackermann/
  Multirotor/Custom), named actions, transport (ROS 2 action / OPC UA
  / DDS / VMC-OSC / Autoware / vendor SDK / MQTT / REST), and safety
  envelope. Not a runtime capability — a registry extension.
  
  **Transport adapters** in `ozmalib/motion_transports/*.yaml` — one
  per standard. The shim translates MOTION commands from the protocol
  to the appropriate wire format: ROS 2 actions, OPC UA method calls,
  DDS topic writes, VMC-OSC packets, Autoware `VehicleCmd`, vendor
  SDKs, MQTT, or REST.
  
  **Safety contract (normative):** Five mandatory primitives —
  (1) safety-envelope enforcement per device (ranges, workspaces,
  geofences, velocity caps) validated *before* dispatch;
  (2) human-in-the-loop gating for actions listed in the device's
  `safety.requires_confirm`;
  (3) emergency stop — reserved `[motion:emergency-stop]` tag plus
  safe-phrase detection halt *all* registered devices immediately;
  (4) mandatory audit log with timestamp, device, action, params,
  dispatch result, outcome (separate from telemetry);
  (5) dry-run / preview modes via `X-Ozma-Motion: dry-run | preview
  | live`.
  
  **PERSONA extension:** `avatar:`, `default_motion:`,
  `context_motions:` as first-class attributes. A persona now carries
  modality-complete expression — STYLE (text) + VOICE + PROSODY
  (audio) + AVATAR + MOTION (embodied).
  
  **Client negotiation — three new headers:**
  `X-Ozma-Avatar: none|2d-portrait|3d-vrm|3d-gltf|ready-player-me|live-vtuber|live-vr`,
  `X-Ozma-Devices: <comma-separated-ids>`,
  `X-Ozma-Motion: live|preview|dry-run`.
  
  New error kinds: `motion_safety_violation`, `motion_confirm_required`,
  `motion_device_unavailable`, `motion_transport_error`,
  `motion_emergency_stop_fired`, `avatar_unknown`, `device_profile_invalid`.
  Telemetry extended with motion command counts, safety-violation
  counts, e-stop triggers, device availability, transport latency.
  Teaching prompts ~220 tokens (MOTION) + ~140 tokens (AVATAR).
  
  Positioning: at the protocol level, avatar animation and robot
  control are the same problem — device description + command
  interface + safety contract. Same LLM, same persona, same fenced
  syntax, different device profiles and transports. Existing
  standards (URDF, ROS 2, OPC UA, DDS, VMC, VRM) handle layers 1-3;
  this protocol standardizes the LLM → intent layer that the field
  currently reinvents per project.
- v5.4 (2026-04-20): Communication style, rich output, and expressive
  voice — four new capabilities adding orthogonal expression axes to
  persona/capability:
  
  **STYLE** — how the model writes (register, tone, named presets
  like `caveman`/`academic`/`pirate`/`yoda`). 14 named presets in
  `ozmalib/styles/*.yaml`. Orthogonal to PERSONA — same persona uses
  different styles in different contexts (debugging=terse,
  teaching=socratic, docs=technical-spec).
  
  **THEME** — structured visual presentation metadata for rich-capable
  clients (colors, gradients, emphasis, imagery cues, animation). 12
  named presets. Per-block fencing (`[theme:name]...[/theme]`) for
  mid-turn theme changes.
  
  **PROSODY** — speech markup for TTS output. SSML-1.1-aligned
  attributes (rate, pitch, pitch-range, volume, emphasis, pause,
  voice-quality, lengthening, interpret-as) plus named emotion taxonomy
  mapping to Azure `mstts:express-as` styles and ElevenLabs v3 audio
  tags. First-class `speed` dimension (very-slow/slow/medium/fast/
  very-fast, ~100-250 wpm). Pitch-range as separate attribute (the
  expressive-vs-flat axis). Research grounding: SSML 1.1 W3C Rec;
  Banse & Scherer 1996; Juslin & Laukka 2003 (104-study meta-analysis);
  Foulke & Sticht 1969; Bänziger & Scherer 2005 (pitch range); 2025
  linguistic/emotional prosody meta-analysis.
  
  **VOICE** — persona voice identity (voice ID, accent, timbre,
  register). Engine-specific voice-ID mapping for ElevenLabs / Azure /
  OpenAI / Google via `ozmalib/voices/*.yaml`. Research grounding:
  Nass & Brave 2005 (Wired for Speech); Lortie et al. 2015; ASHA
  aging-voice meta-analysis.
  
  **PERSONA extension** — `age: N` and `voice:` as first-class persona
  attributes. Age drives voice-engine fallback (child/adult/elderly
  distinctions in voice libraries), vocabulary priors, and default
  prosody priors per empirical acoustic-aging data.
  
  **Client negotiation** — two new headers: `X-Ozma-Render: rich-html|
  rich-css|markdown|plain` (visual rendering capability) and
  `X-Ozma-TTS: ssml|eleven|azure|openai|google|plain` (audio rendering
  capability). Separate axes — client can be rich-html + ssml
  simultaneously. Shim translates Ozma-native prosody to the target
  flavor via `ozmalib/tts_adapters/*.yaml`.
  
  **Rule-engine integration** — rules-social.md §6 emotion classifier
  output now drives PROSODY defaults. User distress → `[prosody:soft
  rate=slow pitch-range=narrow]` per Banse/Scherer acoustic-signature
  mapping. Capability profiles (§14c) constrain rendering: photosensitive
  → `animation: none`; screen-reader → `X-Ozma-Render: plain`.
  
  **Auto-adjustment:** `X-Ozma-Render: plain` preserves emoji density
  as fallback expression channel (LLMs compensate with emoji when
  color is unavailable); rich clients see reduced emoji density as
  themes carry the visual burden. `X-Ozma-TTS: plain` strips prosody
  tags but retains emphasis as markdown bold.
  
  All four capabilities `teachable: true`; stock LLMs pick them up at
  L1-instruction quality from teaching prompts (~150/180/220/120 tokens
  respectively). Backward compatible — clients that don't opt in see
  plain text.
- v5.3 (2026-04-20): Self-upgrade mechanism. New §Self-Upgrade section
  specifies runtime capability teaching: model ack split into `trained:`
  (natively fine-tuned, full fingerprints, ~100% format validity) and
  `accept:` (willing to use via canonical teaching prompt). Teaching
  prompts are versioned YAML declarations (`ozmalib/teaching_prompts/
  *.yaml`) injected into the stable system-prompt prefix for accepted
  caps. Combined budget capped at 600 tokens.
  
  New `UPGRADE` capability (fingerprint `0123ab`) is a *quality
  signal, not a gate*. Teaching prompts are delivered to any model
  that has `accept:` entries regardless of UPGRADE ack. Quality tiers
  by ack shape: L3-native (`trained:` match, ~100%), L3-upgrade-
  trained (`accept:` + `UPGRADE: trained`, ~85-95%), L2-upgrade-accept
  (`accept:` + `UPGRADE: accept`, ~75-90%), L1-instruction (`accept:`
  + no UPGRADE, ~60-85% — stock frontier models land here). The
  UPGRADE teaching prompt is self-bootstrapping — a model that acks
  `UPGRADE: accept` reads that teaching prompt first and reaches
  L2-upgrade-accept on subsequent teaching prompts.
  
  Non-teachable structural caps (IR, LEARNED-BPE, GRAMMAR) declared
  `teachable: false`; shim ignores `accept:` entries for these and
  treats as L1 fallback. New error kinds:
  `teaching_fallback_exhausted`, `cap_not_teachable`. Telemetry
  extended with `caps_trained`, `caps_accepted`,
  `caps_emitted_as_prompt_taught`, `teaching_prompt_tokens_total`,
  `teaching_prompt_format_valid`, `upgrade_cap_acked`. Legacy
  single-section ack form retained for backward compatibility with
  v4.x and earlier.
  
  **Upgrading existing models:** the ack header declares the
  protocol version the model was trained against
  (`[OZMA v5.1 ack:...]`). Each protocol version has a canonical cap
  set (see §Changelog); the shim ships with a `VERSION_CAP_SETS`
  catalog. For any cap that exists in the current protocol version
  but was introduced after the model's declared training version,
  the shim injects a teaching prompt automatically. **Zero registry
  change required** — a v5.1 model acking
  `[OZMA v5.1 ack:...]` picks up every v5.2+ capability the moment
  the shim itself is upgraded to v5.3. No LoRA change, no per-model
  config. `implicit_accept` remains as an optional registry override
  (`exclude` / `include`) for edge cases; per-request
  `X-Ozma-Implicit-Accept: <caps>` header unchanged.
  
  Economic impact: a core-trained LoRA picks up future capabilities
  without retraining; training cadence decouples from protocol
  cadence (~70-80% training cost reduction over a lifecycle with
  2-4 protocol revisions/year). Stock frontier models reach L1-
  instruction quality on every capability immediately — teaching
  prompts are just structured instruction they already follow well.
- v5.2 (2026-04-19): Addresses all items from protocol-review-v5.1.md.
  P0: `HOST_LABEL` production added per RFC 1123 (allows hyphens);
  `EFFECT_HOST` now uses `HOST_LABEL` — hyphenated hostnames
  (`api-gateway.example.com`) accepted.
  P1: grammar production count corrected (131 — the v5.1 changelog
  undercounted, claiming 125 for what was actually 130 additions; v5.2
  adds HOST_LABEL for 131 total); fingerprint cascade policy
  documented — v5.x minor revisions are additive and backward-
  compatible at the per-capability grammar level (new optional
  productions, existing shapes unchanged). Per-cap fingerprints remain
  stable across minor bumps; only the GRAMMAR fingerprint reflects
  shared-primitive changes. Cap fingerprints only rotate on breaking
  changes (next major version).
  P2: `DEP_LINE` PEG parsing note added (parallel to SCHEMA_LINE);
  `AGENT_STATE_DELTA` shim-enforcement note added (at-least-one-field
  not expressible in EBNF, shim rejects empty deltas with
  `agent_delta_empty`); `effects_contradiction` behavior specified
  (offending line dropped; rest of turn unaffected); telemetry
  counters added for `dep_cve_unresolved`, `schema_format_illegal_op`,
  `effects_contradiction_count`, `prompt_token_budget_exceeded_count`.
  P3: `agent_state_scope` telemetry field added; PROMPT_BLOCK nested-
  backticks convention noted.
- v5.1 (2026-04-19): Addresses all items from
  protocol-review-v5.0-FINAL_final(1)_v2.md. P0 grammar fixes:
  production count corrected (123, was 107 in changelog); AGENT-STATE
  fingerprint regenerated (`0c1d2f` → `a3b4c5` — too close to
  MATH=`0c1d2e`); `DEP_LINE` whitespace made conditional (no trailing
  space required for audit-no-note form); `EFFECT_VAL` PROSE hostname
  alternative replaced with constrained `EFFECT_HOST` (dotted LCASE_WORD);
  `PROMPT_BLOCK` tag enforced — only `text` or `prompt` language tags
  accepted. P1: `SCHEMA_LINE` parsing note added (PEG: match
  SCHEMA_COMPAT greedily from EOL); `pure=true` contradiction rule
  (declared `effects_contradiction`); MATH block terminator rule
  documented (first non-2-space-indent line); AGENT-STATE scope
  clarified (workspace by default, `agent_state_scope` registry key);
  AGENT-STATE delta form added (`agent-state-delta:` subset update);
  new error kinds added to §Error Type enumeration: `shape_violation`,
  `schema_compat_mismatch`, `schema_breaking`,
  `schema_format_illegal_op`, `dep_cve_unresolved`,
  `effects_contradiction`, `prompt_injection_risk`,
  `prompt_token_budget_exceeded`. P2: `PROGRESS_VAL` alias for
  CONF_VAL in AGENT-STATE (readability); SCHEMA + EDIT interaction
  rule documented (SCHEMA describes intent; EDIT applies; shim
  cross-checks delta); capability profile names hyphenation-consistent
  (`research-v3`, `federated-v4`, `ml-code`, `api-code`, `agent`,
  `full-v5`). Grammar: 130 unique productions
  (123 + SCHEMA_DETAIL + DEP_DETAIL + PROMPT_TAG + PROMPT_BLOCK
  + PROGRESS_VAL + AGENT_DELTA + EFFECT_HOST).
- v5.0 (2026-04-19): Seven new capabilities added from the math discussion and
  v4.3 "what else is missing" follow-up. **Math:** MATH (canonical Unicode
  math blocks with optional equation labels and `{@math:EN}` cross-turn
  refs); TENSOR-SHAPE (named-dim shape annotations with → transform and
  `where` dimension algebra — the ML-code primitive). **Domain-structural:**
  SCHEMA (schema edits for proto/openapi/graphql/sql/avro/json-schema with
  compatibility tags); DEP (dependency operations — add/remove/upgrade/
  downgrade/pin/audit — with CVE/rationale notes); EFFECTS (side-effect
  tracking: reads/writes/network/globals/pure for sandboxing, test
  isolation, and correctness verification); PROMPT (prompt blocks embedded
  in LLM-app code, subject to prompt-injection and token-budget verifiers);
  AGENT-STATE (workflow-level goal + progress + subtask tracking, bridging
  the turn-level-vs-agent-level gap). Grammar: 123 unique productions,
  CI-verified. Major version bump — v4.x shims accept v5.x acks for known
  caps; v4.x shims do not parse v5 capabilities and fall back to L1 for
  those outputs.
- v4.4 (2026-04-19): Addresses all items from protocol-review-v4.3.md.
  P2: formal grammar preamble added documenting PEG / ordered-choice semantics
  — guides parser-generator choice (Earley/Lark, llguidance, xgrammar all work;
  pure LALR(1) needs additional disambiguation logic).
  P3: PLAN_STEP design-intent comment added (plan steps are human-readable only;
  execution via BODY never via plan steps; widening requires new training data);
  session cache Grammar subsection renamed from "illustration — normative
  definition in §Formal Grammar" to "(illustrative)" so TOC navigation signals
  non-normative status; telemetry schema extended with `strict_antispoof_active:
  bool` and `silent_ack_ms: int|null` for deployment observability of silent
  probe/ack exchanges. Grammar: 89 unique productions, CI-verified.
- v4.3 (2026-04-19): Addresses all items from protocol-review-v4.2.md.
  P2: `strict_antispoof: false` registry flag added — set to `true` for
  MUST-enforcement of silent probe/ack on model switch (SHOULD otherwise);
  CONF_VAL/CONF_MAP grammar-mismatch fallback changed from `confidence=1.0`
  to `confidence=0.5` (fails toward review gate, not auto-apply);
  `dp_user_id` configuration mechanism specified as registry key (null =
  derive from api_key hash) and `X-Ozma-DP-User-Id` per-request header.
  P3: changelog `add` typo fixed; POLY_NAME reserved list normalization note
  added (`formal-claim` → `formal_claim` via hyphen→underscore rule).
  Also addressed independently-identified issues: session cache Grammar
  subsection fence tag changed from `ebnf` to generic (CI invariant fix —
  HASH_REF/CHUNK_HDR were defined in two places); `FILE` changed to
  one-or-more (empty filename was accepted); `HINT_LABEL` production added
  (HINT_LINE PROSE ambiguity documented with parsing note); `DP_FLOAT`
  alternatives reordered most-specific-first (CFG ambiguity with integer
  alternative subsuming exponential); `PLAN_STEP` simplified to PROSE-only
  (INT_CMD/EDIT_CMD variants caused double-NEWLINE, no training data used
  them); `EDGE` parsing note added (PROSE-before-separator domain constraint);
  `STABLE_ID` lowercase requirement commented; `HMAC` production added to
  `ACK` (fixes unbounded HEX length to 8 digits matching probe nonce).
  Grammar: 89 unique productions, CI-verified.
- v4.2 (2026-04-19): Addresses all items from protocol-review-v4.1.md.
  P1: cross-model anti-spoof guarantee documented (first-model-only; silent
  probe/ack on switch requires orchestrator support); FEDERATED `agg_weight`
  now accepts `CONF_VAL | CONF_MAP` (symmetric with COLLAB); `GRAMMAR_VER`
  production removed from grammar — GRAMMAR capability documented in prose
  only (never emitted at runtime; grammar: 87 unique productions); GRAMMAR
  fingerprint-mismatch fallback generalized to all shared-primitive-dependent
  capabilities (was: STRUCTURED-CONF only). P2: FEDERATED default round
  inference rule specified (shim infers max(seen) + 1, starts at 1); DP
  accounting ownership clarified (shim emits markers, pipeline owns the
  accountant); DP identity-rotation-reset documented as known limitation;
  DP_PARAM order documented as fixed (epsilon before delta — required by
  grammar); FEDERATED `include=false` semantics clarified (explicit exclusion;
  omitting an entry is equivalent); COLLAB `CONF_MAP` weight aggregation rule
  added (min primary-aspect weight is effective gradient weight); cross-model
  persistent-state note added (memories from model A persist; model B without
  MEMORY cap sees them as injected plain text). P3: POLY_NAME reserved list
  expanded (`immediate`, `preview`, `add`, `delete`, `user`, and all
  protocol-command prefixes and language tags). Changelog order corrected
  (v1.1–v1.4 were misplaced above v4.0; now at bottom, oldest-last).
  Grammar: 87 unique productions, CI-verified.
- v4.1 (2026-04-19): Addresses all items from protocol-review-v4.0.md.
  P0: DP_PARAM replaced CONF_VAL with DP_FLOAT production (supports scientific
  notation: `1e-5`, `2.0`, `10` — standard DP epsilon/delta range); LCASE and
  LCASE_ALNUM moved to shared primitives section (forward-reference issue for
  parser generators); LCASE_WORD added (`[a-z0-9_]`, no hyphen) — ASPECT_NAME,
  POLY_NAME, POLY_LANG updated to use LCASE_WORD (aspect names and canonical
  poly-names must not contain hyphens); FORMAL_CLAIM moved to after BODY in
  TURN production (claims assert properties of the edit just made, not the edit
  about to be made — grammar now matches training examples); duplicate LCASE/
  LCASE_ALNUM definitions removed from PERSONA section. P1: training example 11
  LEARNED-BPE aliases corrected to lowercase b1/b2 (was uppercase B1/B2);
  FORMAL-CLAIM shim step 1 distinguishes tag_missing (no tag) from tag_unknown
  (unrecognized tag); v4 telemetry fields added (federated_positions, dp_epsilon,
  dp_delta, dp_composition, model_switch_cap_delta,
  formal_verify_advisory_aspects_excluded); all training examples 1–3 bumped
  from v1.2 to v4.0 ack. P2: profile table `federated` clarified to "all caps
  through v4.0"; v4 API headers added (X-Ozma-DP-Epsilon-Cap, X-Ozma-DP-Delta-Cap,
  X-Ozma-Federated-Round); POLY_ID requires at least one character. P3: `recall:`
  typo fixed in shim table; versioning claim generalized to vN.x; GRAMMAR section
  version statement clarified (fingerprint is authoritative, not prose label).
  Grammar: 88 unique productions, CI-verified.
- v4.0 (2026-04-19): Addresses all items from protocol-review-v3.0.md and
  adds four v4 capabilities. v3.1 fixes: CONF_ASPECT grammar disambiguated
  via ASPECT_NAME production (restricted to [a-z][a-z0-9_]*; no `,` `=` `}`);
  advisory aspects marked with trailing `?` (not counted in review gate);
  STRUCTURED-CONF gate uses minimum of primary aspects only; POLYGLOT grammar
  fully structured (POLY_NAME, POLY_LANG, POLY_ID, POLY_BINDING, POLY_LINE —
  no longer PROSE); POLYGLOT conflict resolution: newest-wins, `[superseded]`
  tag, `merge_all` option; FORMAL-CLAIM: canonical language tags enumerated
  (smt2/dafny/lean4/z3), unknown tag → no_verifier+tag_missing, normative
  counterexample format `[formal-claim §N refuted]`; verifier_registry added
  to registry example; L2 registry fingerprint corrected to 6 hex; COLLAB:
  documented advisory nature, calibration-factor guidance, weight accepts
  CONF_VAL or CONF_MAP; PERSONA_ID 32-char limit note clarified (shim-enforced);
  POLYGLOT input format specified; POLY_NAME reserved-keyword policy. v4.0 new
  capabilities: FEDERATED (federated-learning aggregation annotations);
  DP (differential-privacy per-turn epsilon/delta budget tracking for training
  data); session cross-model identity rule (cache chunks valid across model
  switches within a session); GRAMMAR_SEMVER (top-level grammar version covering
  shared primitives). Grammar: 86 unique productions, CI-verified.
- v3.0 (2026-04-19): Addresses all items from protocol-review-v2.0.md and
  adds four v3 capabilities. v2.1 fixes: HASH_REF/EMBED_REF disambiguated
  via `embed:` namespace prefix (e.g. `{@embed:e1}`; all examples lowercase);
  registry fingerprints widened to 6 hex and all duplicates replaced with
  unique values; CAP_PAIR grammar updated to 6-hex and allows `-` in cap names;
  LEARNED-BPE aliases expanded to canonical identifiers before memory storage;
  anaphoric names (prev/target/last_edit) reserved from EMBED namespace;
  ATTN: weighted attendance signal (EDIT=3, INTERACT=2, RATIONALE/REASONING=1),
  time-decay reset (50%/turn decay, [new task] marker, session-id change),
  overflow priority (weight-desc, most-recent tiebreak); LEARNED-BPE:
  coverage-based K (cumulative 40% identifier frequency, cap 50, floor 5),
  B-prefix reserved in legend minification namespace; DOCSTUB: extended to
  module-level and class-level docstrings; EMBED: top-K naming syntax
  (`as embed:e1,embed:e2`). v3.0 new capabilities: STRUCTURED-CONF
  (per-aspect confidence maps replacing scalar for multi-facet assertions);
  FORMAL-CLAIM (SMT-LIB / Dafny verifiable claim forms); POLYGLOT
  (cross-language identifier mapping for polyglot repos); COLLAB
  (multi-model gradient-sharing annotations in HANDOFF). Grammar: 81 unique
  productions, CI-verified.
- v2.0 (2026-04-19): Addresses all items from protocol-review-v1.6.md and
  adds four v2 capabilities. v1.7 fixes: MEM_SCOPE/MEM_TIMING grammar split
  (timing is now a separate axis); same-turn remember/recall ordering rule
  (recall sees only pre-turn memories); canonical content-filter pattern set
  (versioned regex list); workspace writes always content-filtered regardless
  of provenance; X-Ozma-Workspace authorization (workspace_unauthorized error
  in multi-tenant deployments); memory_guardrail/memory_truncated added to
  error-kind table as log-only events; conflict_policy merge_all semantics
  (newest-first, top-K cap, replaces default block); semantic staleness policy
  (memories >90 days tagged [old] at injection); handoff payload forwards-compat
  (receivers MUST ignore unknown fields); registry example includes
  allow_persona_override; header edge-value documentation; API key rotation
  documented; PLAN_STEP grammar comment. v2.0 new capabilities: LEARNED-BPE
  (repo-specific high-frequency identifier compression, persisted alongside
  workspace memories); ATTN (attention-aware adaptive expansion — positions
  attended in turn N are expanded in turn N+1); DOCSTUB (docstring-specific
  compression with expand doc §N command); EMBED (embedding references as
  first-class session aliases — search semantic "q" as embed:e1, expand {@embed:e1}).
  Grammar: 73 unique productions, CI-verified. Version bump to 2.0 for new
  capabilities; v1.x shims accept acks from v2.x for known caps (ignore
  unknown).
- v1.6 (2026-04-19): Addresses all items from protocol-review-v1.5.md.
  P0: MEMORY security model (injection defense, content filters, provenance tags,
  scope isolation, bytes quota); `recall` streaming semantics (terminal command,
  halts generation); stale "v1.1" in L3 system prompt corrected to v1.6.
  P1: workspace/user identity spec (repo fingerprint definition, user_id source);
  memory conflict resolution rules; `remember:` commit-on-success semantics
  (`remember:immediate` variant for eager writes); PLAN vs. BODY relationship
  clarified (plan is informational, body is execution); CONFIDENCE default (absent
  = 1.0), threshold header, aggregation rules for transactions and plans.
  P2: HANDOFF cache export size limit (4MB default) + base64 encoding mandate;
  PERSONA_ID charset/length constraints ([a-z][a-z0-9_-]*, max 32 chars);
  client header wins over model persona request; `persona_change_granted` added to
  telemetry; `pattern_ambiguous` and `handoff_state_mismatch` clarified in body
  sections; `X-Ozma-Persona` and `X-Ozma-Cache-Max-Chunks` added to header table.
  P3: cross-version handoff negotiation (v1.4 sender graceful fallback).
- v1.5 (2026-04-19): Addresses all items from protocol-review-v1.4.md.
  Blocking: EBNF deduplicated into single authoritative grammar; TURN grammar
  fixed (removed duplicate TEST_SPEC, clarified ordering invariant and
  exclusivity); HANDOFF code-state delivery committed (session-cache expansion
  with cross-host serialization). P1: circuit-breaker semantics fully specified;
  streaming invariants section added; session cache eviction/invalidation/
  persistence specified; CONFIDENCE made universal (REASONING chains, PLAN
  steps). P2: pattern compression detection semantics defined; partial-pattern
  edit syntax added; legend session-scoping rule added; telemetry schema
  extended (tokenizer, streaming flag, upstream cache); error kind enumeration
  completed. P3: L1 fallback for Dart/Terraform noted; training opt-in
  filtering guidance added. New: MEMORY and PERSONA as first-class protocol
  types and data types.
- v1.4 (2026-04-19): Expanded language support — IR symbol tables, tree-sitter
  stub collection, file-extension detection, and content heuristics for: Lua,
  Scala, Elixir, Dart, Haskell, Bash/Shell, OCaml, Nix, and Terraform/HCL.
  Stub shapes: end-terminated (Lua, Elixir), no-closing (Haskell, OCaml),
  curly-brace (Scala, Bash, Dart via JS fallback). Bash lint gate via
  `shellcheck --format=json`. Dart and Terraform/HCL have IR tables but no
  tree-sitter parser (no PyPI packages); both fall back gracefully.
- v1.3 (2026-04-18): C/C++ and Linux kernel support — IR symbol tables for
  C and C++ (including kernel reserved identifiers: `kmalloc`, `EXPORT_SYMBOL`,
  `spin_lock`, `__user`, `KERN_ERR`, etc.); tree-sitter stubbing for C function
  definitions and C++ methods/templates/qualified names; `.h` header language
  disambiguation; lint gate via `clang`/`gcc -fsyntax-only`; collision check
  updated to allow empty-string strip tokens (access modifiers, `std::`).
- v1.2 (2026-04-18): Implements protocol-improvements.md — session cache,
  CLAIM/INVARIANT, CONFIDENCE on all output, REASONING, PLAN, TEST-SPEC,
  HANDOFF, pattern compression, TRANSACTION, preview/semantic search,
  constrained decoding note, updated grammar and shim table.
- v1.1 (2026-04-18): Addresses all items from protocol-review.md.

