"""
types.py — Structured output types for the Ozma wire protocol.

Each type corresponds to a grammar production in the protocol spec.
All types are plain dataclasses — no parsing logic here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


# ---------------------------------------------------------------------------
# EDIT — position-addressed code edit
# ---------------------------------------------------------------------------

class EditOp(Enum):
    REPLACE  = "edit"      # replace body of §REF
    DELETE   = "delete"    # remove §REF
    ADD_BEFORE = "add:before"
    ADD_AFTER  = "add:after"
    ADD_FILE   = "add:file"  # append to end of file


@dataclass
class EditCmd:
    op: EditOp
    ref: str            # §N ordinal, §STABLEID, or file.py:§N
    body: str           # code body (empty for delete)
    confidence: float = 1.0
    preview: bool = False
    file: str | None = None   # explicit file prefix


# ---------------------------------------------------------------------------
# INTERACT — interaction commands
# ---------------------------------------------------------------------------

class InteractVerb(Enum):
    EXPAND    = "expand"
    CALLERS   = "callers"
    CALLEES   = "callees"
    OUTLINE   = "outline"
    SEARCH    = "search"
    SEMANTIC  = "semantic"
    RECALL    = "recall"
    PERSONA   = "persona"
    DOCEXPAND = "expand doc"


@dataclass
class InteractCmd:
    verb: InteractVerb
    target: str          # §REF, {@HASH}, filename, query string
    raw: str = ""        # original line for diagnostics


# ---------------------------------------------------------------------------
# PLAN — typed execution plan
# ---------------------------------------------------------------------------

@dataclass
class PlanStep:
    label: str           # p1, p2, …
    description: str
    confidence: float = 1.0
    # Agentic routing fields — emitted by capable models, drive strategy selection
    complexity: str = ""        # "trivial"|"simple"|"moderate"|"complex"|"expert"
    category: str = ""          # "api"|"auth"|"db"|"frontend"|"infra"|"ml"|"cli"|…
    file: str = ""              # output file hint ("models.py", "api/routes.py")
    depends_on: list[str] = field(default_factory=list)  # step labels ("p1", "p2")


@dataclass
class PlanBlock:
    plan_id: str         # P1, P2, …
    steps: list[PlanStep] = field(default_factory=list)


# ---------------------------------------------------------------------------
# CLAIM / INVARIANT
# ---------------------------------------------------------------------------

@dataclass
class ClaimBlock:
    position: str        # §REF or "" for module-level
    assertion: str       # the claim text
    confidence: float = 1.0
    is_invariant: bool = False
    formal_lang: str | None = None   # smt2, dafny, lean4, z3
    formal_body: str | None = None


# ---------------------------------------------------------------------------
# REASONING — chain-of-thought with per-hypothesis confidence
# ---------------------------------------------------------------------------

@dataclass
class ReasoningStep:
    hypothesis: str
    verdict: str         # "accept", "reject", "defer"
    confidence: float = 1.0


@dataclass
class ReasoningBlock:
    steps: list[ReasoningStep] = field(default_factory=list)
    conclusion: str = ""


# ---------------------------------------------------------------------------
# MEMORY — remember/recall
# ---------------------------------------------------------------------------

class MemScope(Enum):
    SESSION   = "session"
    WORKSPACE = "workspace"
    USER      = "user"


@dataclass
class MemoryWrite:
    content: str
    scope: MemScope = MemScope.WORKSPACE
    position: str | None = None   # §REF if workspace-scoped to position
    immediate: bool = False        # remember:immediate


# ---------------------------------------------------------------------------
# Phase 2: Code-intelligence caps
# ---------------------------------------------------------------------------

@dataclass
class TestSpec:
    ref: str
    lang: str    # "python", "json", "yaml", etc.
    body: str


@dataclass
class EffectAnnotation:
    ref: str
    effects: dict[str, list[str]]   # {"io": ["file","network"], "db": ["read"]}


@dataclass
class DepEdge:
    from_ref: str
    to_ref: str
    qualifier: str = ""   # "transitive", "circular!", "weak", etc.


@dataclass
class SchemaDecl:
    ref: str
    lang: str    # "json-schema", "pydantic", "zod", "protobuf"
    body: str


@dataclass
class OutlineItem:
    ref: str
    description: str


@dataclass
class OutlineBlock:
    items: list[OutlineItem] = field(default_factory=list)


@dataclass
class HintBlock:
    tag: str
    content: str


@dataclass
class DocStub:
    ref: str
    body: str


# ---------------------------------------------------------------------------
# Phase 3: Agent & orchestration caps
# ---------------------------------------------------------------------------

@dataclass
class SubPrompt:
    label: str
    body: str


@dataclass
class HandoffPacket:
    target_model: str
    body: str   # JSON string


@dataclass
class AgentStateBlock:
    scope: str   # "workspace" | "session" | custom label
    body: str


@dataclass
class MotionBlock:
    """A block of MOTION commands targeted at a named device.

    Covers the full MOTION cap surface: physical actuators (robots,
    vehicles, drones), avatars, and digital UI substrates (desktop is
    the first; browser/mobile follow). The device name resolves to a
    device profile that declares syntax, ref dialect, and safety
    envelope. `raw_actions` holds the verbatim body lines — the
    device-profile adapter is responsible for parsing them into concrete
    commands (e.g. DesktopAction for device=desktop, joint trajectories
    for URDF robots).
    """
    device: str
    raw_actions: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Phase 4: Math / ML / multi-model / privacy caps
# ---------------------------------------------------------------------------

@dataclass
class MathBlock:
    label: str
    body: str


@dataclass
class TensorShape:
    ref: str
    dims: dict[str, int]    # {"B": 32, "T": 512, "D": 768}
    dtype: str | None = None


@dataclass
class GraphEdge:
    from_ref: str
    to_ref: str
    label: str = ""


@dataclass
class GraphBlock:
    label: str
    edges: list[GraphEdge] = field(default_factory=list)


@dataclass
class TreeNode:
    ref: str
    label: str
    children: list["TreeNode"] = field(default_factory=list)


@dataclass
class TreeBlock:
    label: str
    root: "TreeNode | None" = None


@dataclass
class StructuredConf:
    ref: str
    breakdown: dict[str, float] = field(default_factory=dict)


@dataclass
class DPBudget:
    epsilon: float
    delta: float
    mechanism: str = "gaussian"


@dataclass
class GrammarBlock:
    name: str
    body: str


@dataclass
class AttnAnnotation:
    ref: str
    weights: dict[str, float] = field(default_factory=dict)


@dataclass
class EmbedBlock:
    ref: str
    values: list[float] = field(default_factory=list)

    @property
    def dims(self) -> int:
        return len(self.values)


@dataclass
class CollabBlock:
    author: str
    attribution: str = ""
    wrapped_edit_ref: str | None = None


@dataclass
class FederatedRoute:
    target: str
    region: str | None = None
    body: str = ""


@dataclass
class PolyglotBlock:
    langs: list[str] = field(default_factory=list)
    bodies: dict[str, str] = field(default_factory=dict)


@dataclass
class LearnedBPEBlock:
    body: str   # raw JSON


# ---------------------------------------------------------------------------
# v5.11: REASONING-PERSIST — cross-turn reasoning cache
# ---------------------------------------------------------------------------

class PersistScope(Enum):
    OFF       = "off"
    SESSION   = "session"
    WORKUNIT  = "workunit"


@dataclass
class ReasoningPersistEntry:
    """One cached reasoning branch — accept or ∴ EDIT only."""
    turn_no: int                # absolute turn number in the session/workunit
    hypothesis_id: str          # "h1", "h2", …
    text: str                   # hypothesis body (WIRE-compressed by shim)
    verdict: str                # "accept" | "edit" | "reject-learned"
    edit_ref: str | None = None # §REF when verdict == "edit"
    step_id: str | None = None  # "s1", "s2" … when reasoning came from THINK-ACT step
    tokens: int = 0             # estimated (used for budget)


@dataclass
class PriorReasoningBlock:
    """The `[prior reasoning ↦ goal:HASH turns=N budget=X/Y]` header block."""
    goal_hash: str
    turns: int
    budget_used: int
    budget_max: int
    entries: list[ReasoningPersistEntry] = field(default_factory=list)


# ---------------------------------------------------------------------------
# v5.11: THINK-ACT — interleaved reasoning + tool-call body
# ---------------------------------------------------------------------------

@dataclass
class ThinkActStep:
    """One step inside a THINK_ACT_LIST body."""
    step_no: int                              # 1-indexed
    reasoning: ReasoningStep | None = None    # optional reasoning-step prefix
    interact: InteractCmd | EditCmd | None = None  # the tool call / edit
    observation: str | None = None            # shim-injected [observation step=N]
    tokens_out: int = 0                       # model tokens produced for this step
    truncated: bool = False                   # hit max_output_tokens_per_step


@dataclass
class ThinkActBlock:
    """Turn body = single THINK_ACT_LIST (alternative to INT_LIST / EDIT_LIST)."""
    steps: list[ThinkActStep] = field(default_factory=list)
    step_cap_reached: bool = False   # exceeded think_act_max_steps
    total_cap_reached: bool = False  # exceeded max_output_tokens_total


# ---------------------------------------------------------------------------
# v5.11: SCHEDULE — first-class scheduled wake-up
# ---------------------------------------------------------------------------

class TriggerForm(Enum):
    CRON         = "cron"
    EVERY        = "every"
    AT           = "at"
    AFTER        = "after"
    ON_EVENT     = "on-event"
    ON_IDLE      = "on-idle"
    ON_DEADLINE  = "on-deadline"


class OverlapPolicy(Enum):
    SKIP     = "skip"
    QUEUE    = "queue"
    PARALLEL = "parallel"


class DriftPolicy(Enum):
    FIRE_ON_RESUME = "fire-on-resume"
    SKIP_MISSED    = "skip-missed"


@dataclass
class TriggerSpec:
    """One of seven trigger forms. `expr` varies per form."""
    form: TriggerForm
    expr: str                      # "0 9 * * MON-FRI" | "15m" | "2026-04-22T14:00:00Z" | ...
    timezone: str = "UTC"          # IANA tz (cron/at require explicit if not UTC)
    where: dict[str, str] = field(default_factory=dict)  # on-event predicate filters


@dataclass
class ScheduleBudget:
    """DISPATCH budget envelope, per fire."""
    cost_usd_max: float | None = None
    latency_ms_max: int | None = None
    tokens_per_turn_max: int | None = None


@dataclass
class RetrySpec:
    backoff: str = "exponential"   # "exponential" | "linear" | "fixed"
    max_attempts: int = 3
    initial: str = "5m"            # parseable duration


@dataclass
class EscalateSpec:
    after: int                     # attempts before escalation
    to: str                        # agent:<id>


@dataclass
class CircuitBreaker:
    threshold: int = 5             # fires before trip
    window: str = "1h"             # time window
    failure_rate: float = 0.8      # trip if rate >= this


@dataclass
class ScheduleBlock:
    """Parsed `[schedule ...]` declaration."""
    id: str                         # "s:<STABLE_ID>"
    as_agent: str                   # "agent:<id>" — owner
    trigger: TriggerSpec
    purpose: str = ""
    scope: MemScope = MemScope.WORKSPACE
    max_runtime_ms: int | None = None
    overlap: OverlapPolicy = OverlapPolicy.SKIP
    queue_cap: int = 10             # for overlap=queue
    parallel_limit: int = 2         # for overlap=parallel
    drift: DriftPolicy = DriftPolicy.FIRE_ON_RESUME
    workunit: str | None = None     # context.workunit id
    inject: list[str] = field(default_factory=list)  # context.inject blocks
    budget: ScheduleBudget = field(default_factory=ScheduleBudget)
    thinking: str = "auto"          # "on" | "off" | "auto"
    on_fire_prompt: str = ""
    retry: RetrySpec | None = None
    escalate: EscalateSpec | None = None
    circuit_breaker: CircuitBreaker = field(default_factory=CircuitBreaker)
    compliance: list[str] = field(default_factory=list)
    raw: str = ""                   # original text for round-trip


@dataclass
class ScheduleUpdate:
    """`[schedule:update id=s:xx]` — partial update; preserves id + history."""
    id: str
    trigger: TriggerSpec | None = None
    on_fire_prompt: str | None = None
    overlap: OverlapPolicy | None = None
    drift: DriftPolicy | None = None
    budget: ScheduleBudget | None = None
    purpose: str | None = None


@dataclass
class ScheduleCancel:
    id: str
    reason: str = ""


@dataclass
class WakeContext:
    """The `[wake source=... id=... fire_ts=...]` block prepended on fire turns."""
    source: str                    # "schedule" | "message-triggered" | "manual"
    schedule_id: str | None = None
    fire_ts: str = ""              # ISO8601
    fire_id: str = ""              # sha256(schedule_id + fire_ts_iso)[:12]
    reason: str = ""               # trigger expr
    missed_count: int = 0
    last_fire_ts: str | None = None
    workunit: str | None = None
    retry: bool = False            # X-Ozma-Wake-Retry


@dataclass
class FireRecord:
    """One execution of a schedule. Written to schedule history store."""
    fire_id: str
    schedule_id: str
    fire_ts: str
    started_at: float              # unix timestamp
    completed_at: float | None = None
    status: str = "started"        # started | ok | failed | overlap-skipped | drift-skipped | queue-overflow
    trigger_form: str = ""
    trigger_expr: str = ""
    missed_count: int = 0
    overlap_action: str = "none"
    duration_ms: int | None = None
    cost_usd: float = 0.0
    failure_reason: str | None = None
    dispatches_in_fire: int = 0
    messages_emitted: int = 0


# ---------------------------------------------------------------------------
# ACK — handshake acknowledgement
# ---------------------------------------------------------------------------

@dataclass
class AckHeader:
    version: str
    hmac: str
    # Two-section form: trained caps with fingerprints (from `trained:` line)
    caps: dict[str, str]         # cap_name → fingerprint (trained caps only)
    # Caps declared via `accept:` (no fingerprints; model will accept runtime teaching)
    accept_caps: list[str] = field(default_factory=list)
    # True when parsed from an [OZMA-EDGE ack: ...] header (no hmac, no fingerprints)
    is_edge: bool = False
    # v6.0 — wire format the model chose for this session. Echoes the probe's
    # wire:<format>; defaults to "chars" when absent (all v5.x acks). The shim
    # falls back to "chars" if the echoed format isn't one both sides support.
    wire_format: str = "chars"


# ---------------------------------------------------------------------------
# Turn — complete parsed model turn
# ---------------------------------------------------------------------------

@dataclass
class Turn:
    """A fully parsed model response turn."""
    raw: str                             # original model output
    text: str                            # prose (non-protocol text)
    ack: AckHeader | None = None
    edits: list[EditCmd] = field(default_factory=list)
    interacts: list[InteractCmd] = field(default_factory=list)
    plans: list[PlanBlock] = field(default_factory=list)
    claims: list[ClaimBlock] = field(default_factory=list)
    reasoning: ReasoningBlock | None = None
    memory_writes: list[MemoryWrite] = field(default_factory=list)
    # Phase 2: code-intelligence
    test_specs: list[TestSpec] = field(default_factory=list)
    effects: list[EffectAnnotation] = field(default_factory=list)
    dep_edges: list[DepEdge] = field(default_factory=list)
    schemas: list[SchemaDecl] = field(default_factory=list)
    outline: OutlineBlock | None = None
    hints: list[HintBlock] = field(default_factory=list)
    docstubs: list[DocStub] = field(default_factory=list)
    wire_encoded: bool = False
    # Phase 3: agent/orchestration
    sub_prompts: list[SubPrompt] = field(default_factory=list)
    handoffs: list[HandoffPacket] = field(default_factory=list)
    agent_states: list[AgentStateBlock] = field(default_factory=list)
    motion_blocks: list[MotionBlock] = field(default_factory=list)
    # Phase 4: math/ML/multi-model/privacy
    math_blocks: list[MathBlock] = field(default_factory=list)
    tensor_shapes: list[TensorShape] = field(default_factory=list)
    graphs: list[GraphBlock] = field(default_factory=list)
    trees: list[TreeBlock] = field(default_factory=list)
    structured_confs: list[StructuredConf] = field(default_factory=list)
    dp_budgets: list[DPBudget] = field(default_factory=list)
    grammars: list[GrammarBlock] = field(default_factory=list)
    attn_annotations: list[AttnAnnotation] = field(default_factory=list)
    embeds: list[EmbedBlock] = field(default_factory=list)
    collab_blocks: list[CollabBlock] = field(default_factory=list)
    federated_routes: list[FederatedRoute] = field(default_factory=list)
    polyglot_blocks: list[PolyglotBlock] = field(default_factory=list)
    learned_bpe: LearnedBPEBlock | None = None
    # v5.11: reasoning persistence, interleaved think-act, scheduled wake-up
    prior_reasoning: "PriorReasoningBlock | None" = None   # re-injected by shim
    think_act: "ThinkActBlock | None" = None               # alt body form
    schedules: list["ScheduleBlock"] = field(default_factory=list)
    schedule_updates: list["ScheduleUpdate"] = field(default_factory=list)
    schedule_cancels: list["ScheduleCancel"] = field(default_factory=list)
    wake_context: "WakeContext | None" = None              # set on fire turns
    # Warnings from parsing
    warnings: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def has_protocol(self) -> bool:
        return bool(
            self.ack or self.edits or self.interacts or self.plans
            or self.claims or self.reasoning or self.memory_writes
            or self.test_specs or self.effects or self.dep_edges
            or self.schemas or self.outline or self.hints or self.docstubs
            or self.sub_prompts or self.handoffs or self.agent_states
            or self.motion_blocks or self.math_blocks or self.tensor_shapes
            or self.graphs or self.trees or self.grammars or self.embeds
        )

    # Convenience alias used by the OpenClaw shim
    EditOp = EditOp
    InteractVerb = InteractVerb
