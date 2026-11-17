"""
ozmalib.protocol — Ozma wire protocol implementation.

Implements the Ozma Protocol (current: v5.2) as a shim layer over any
OpenAI-compatible LLM API. Stock models run at Level 1 (plain text).
A model trained on the protocol runs at Level 3 (full bidirectional types).

Quick start::

    from ozmalib.protocol import OzmaShim, MemorySessionStore

    shim = OzmaShim(session_store=MemorySessionStore())

    # Inbound: intercept /v1/chat/completions body
    result = shim.intercept_request(body, session_id="sess_abc")

    # Forward result.body to the model...
    raw_response = client.chat.completions.create(**result.body)

    # Outbound: parse and strip protocol tokens
    turn = shim.intercept_response(raw_response, result.session_id)

    # turn.edits, turn.plans, turn.claims, turn.text, turn.test_specs, etc.
"""

from .registry import CapabilityRegistry, ModelEntry, LEVEL, MODEL_CAPS, WIRE_FORMATS
from .handshake import HandshakeManager, AckResult
from .dialect import (
    DialectCodec, DialectMetadata, DialectStore,
    compute_fingerprint, load_dialect, OZMAP_FILES,
)
from .store import SessionStore, MemorySessionStore, SQLiteSessionStore
from .telemetry import (
    MemoryTelemetrySink, SQLiteTelemetrySink, ShimCallRecord, TelemetrySink,
)
from .system_prompt import SystemPromptBuilder
from .stream import StreamState, StreamingShim
from .verify import VerifierResult, verify_formal_claims
from .types import (
    # Core
    EditOp, EditCmd, InteractVerb, InteractCmd,
    PlanBlock, PlanStep, ClaimBlock, ReasoningBlock, ReasoningStep,
    MemoryWrite, MemScope, AckHeader, Turn,
    # Phase 2: code intelligence
    TestSpec, EffectAnnotation, DepEdge, SchemaDecl,
    OutlineBlock, OutlineItem, HintBlock, DocStub,
    # Phase 3: agent/orchestration
    SubPrompt, HandoffPacket, AgentStateBlock, MotionBlock,
    # Phase 4: math/ML/multi-model/privacy
    MathBlock, TensorShape, GraphBlock, GraphEdge,
    TreeBlock, TreeNode, StructuredConf, DPBudget,
    GrammarBlock, AttnAnnotation, EmbedBlock,
    CollabBlock, FederatedRoute, PolyglotBlock, LearnedBPEBlock,
    # v5.11
    PersistScope, ReasoningPersistEntry, PriorReasoningBlock,
    ThinkActStep, ThinkActBlock,
    TriggerForm, OverlapPolicy, DriftPolicy,
    TriggerSpec, ScheduleBudget, RetrySpec, EscalateSpec, CircuitBreaker,
    ScheduleBlock, ScheduleUpdate, ScheduleCancel,
    WakeContext, FireRecord,
)
from .reasoning_persist import ReasoningPersistCache
from .think_act import ThinkActExecutor, ObservationResolveError
from .schedule import (
    ScheduleRegistry, ScheduleStore, MemoryScheduleStore,
    compute_fire_id, CircuitBreakerTripped, ScheduleValidationError,
)
from .parser import TurnParser
from .shim import OzmaShim, ShimRequest, ShimTurn

__all__ = [
    # Shim
    "OzmaShim", "ShimRequest", "ShimTurn",
    # Registry
    "CapabilityRegistry", "ModelEntry", "LEVEL", "MODEL_CAPS", "WIRE_FORMATS",
    # Handshake
    "HandshakeManager", "AckResult",
    # Dialect (L4 codec)
    "DialectCodec", "DialectMetadata", "DialectStore",
    "compute_fingerprint", "load_dialect", "OZMAP_FILES",
    # Store
    "SessionStore", "MemorySessionStore", "SQLiteSessionStore",
    # Telemetry
    "TelemetrySink", "MemoryTelemetrySink", "SQLiteTelemetrySink",
    "ShimCallRecord",
    # System prompt
    "SystemPromptBuilder",
    # Streaming
    "StreamState", "StreamingShim",
    # Verifier
    "VerifierResult", "verify_formal_claims",
    # Core types
    "EditOp", "EditCmd", "InteractVerb", "InteractCmd",
    "PlanBlock", "PlanStep", "ClaimBlock", "ReasoningBlock", "ReasoningStep",
    "MemoryWrite", "MemScope", "AckHeader", "Turn",
    # Phase 2
    "TestSpec", "EffectAnnotation", "DepEdge", "SchemaDecl",
    "OutlineBlock", "OutlineItem", "HintBlock", "DocStub",
    # Phase 3
    "SubPrompt", "HandoffPacket", "AgentStateBlock", "MotionBlock",
    # Phase 4
    "MathBlock", "TensorShape", "GraphBlock", "GraphEdge",
    "TreeBlock", "TreeNode", "StructuredConf", "DPBudget",
    "GrammarBlock", "AttnAnnotation", "EmbedBlock",
    "CollabBlock", "FederatedRoute", "PolyglotBlock", "LearnedBPEBlock",
    # v5.11 types
    "PersistScope", "ReasoningPersistEntry", "PriorReasoningBlock",
    "ThinkActStep", "ThinkActBlock",
    "TriggerForm", "OverlapPolicy", "DriftPolicy",
    "TriggerSpec", "ScheduleBudget", "RetrySpec", "EscalateSpec", "CircuitBreaker",
    "ScheduleBlock", "ScheduleUpdate", "ScheduleCancel",
    "WakeContext", "FireRecord",
    # v5.11 reference modules
    "ReasoningPersistCache",
    "ThinkActExecutor", "ObservationResolveError",
    "ScheduleRegistry", "ScheduleStore", "MemoryScheduleStore",
    "compute_fire_id", "CircuitBreakerTripped", "ScheduleValidationError",
    # Parser
    "TurnParser",
]
