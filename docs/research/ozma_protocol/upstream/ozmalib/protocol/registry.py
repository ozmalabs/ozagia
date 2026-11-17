"""
registry.py — Capability registry: maps model IDs to protocol levels and caps.

The registry is the source of truth. The in-band handshake is advisory only —
it cannot elevate a model beyond its registry-declared level.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any

from .reasoning import (
    MOONSHOT_THINKING,
    OPENROUTER_ANTHROPIC_MAX,
    OPENROUTER_EFFORT,
    ReasoningKnob,
)


class LEVEL(IntEnum):
    L0 = 0   # Native: plain text in, plain prose out. No protocol.
    L1 = 1   # Read: reads compressed context, produces plain prose/edits.
    L2 = 2   # IR: reads + writes IR code (Greek symbols).
    L3 = 3   # Full: reads + writes all protocol data types bidirectionally.
    L4 = 4   # Post-character: token- or activation-native wire. Characters
             # appear only at human-interface boundaries. See docs/l4_plan.md.


# WireFormat values carried by ModelEntry.wire_format and exchanged in the
# handshake probe/ack as `wire:<format>`.
#
#   chars       — character-stream transport. Default; all v5.x behaviour.
#   tokens      — token-ID-stream transport. Skips tokenize/detokenize at the
#                 model boundary. Requires cooperating serving endpoint that
#                 accepts prompt_token_ids (e.g. vLLM /v1/completions).
#   activations — residual-stream / embedding transport. L4b, research-grade.
#
# The shim falls back to chars when either counterparty declares chars, so
# mixed traffic Just Works. See docs/l4_plan.md for PoC scope.
WIRE_FORMATS = ("chars", "tokens", "activations")


# Teaching profiles — how much of the protocol to inject at turn start.
#
# The profile answers "how much does this model need to be told each turn?"
# and sizes the system prompt accordingly. Axes:
#
#   - size     — parameter count drives both how much the model can
#                tolerate and how dense the instruction must be
#   - type     — trained-native models need nothing; prompted models need
#                proportional teaching
#   - caps     — a session using only IR+EDIT doesn't need the full L3
#                cap legend even if the model could handle it
#
# The profile is set per-model in the registry. Unset defaults to "auto",
# which resolves at build time based on level + parse_mode + constrained_decode.
TEACHING_PROFILES = ("auto", "native", "compact", "standard", "full")

# Profile → [avg system-prompt chars at L3] (rough budgets, not enforced)
#
#   native    ~120 — header + version + probe, no legend
#   compact   ~400 — header + active-lang IR lines + cap-name list
#   standard  ~900 — header + core 5 IR langs + cap descriptors + memory
#   full     ~1700 — header + all 15 IR langs + cap descriptors + examples + memory
#
# At L2 the same tiers apply but with no L3 cap block; budgets are roughly
# 60% of the L3 numbers.


# Canonical capability fingerprints (sha256(grammar_section)[:6]).
# These are checked against the ack header on the first response.
CAP_FINGERPRINTS: dict[str, str] = {
    "OUTLINE":          "a4f9c1",
    "EDIT":             "b2c1d3",
    "INTERACT":         "d8f3e2",
    "RATIONALE":        "e1a7f4",
    "CLAIM":            "c3d4b5",
    "HINT":             "f9b2a6",
    "GRAPH":            "3c4d1e",
    "TREE":             "5e6f2a",
    "REASONING":        "71ab3c",
    "PLAN":             "82bc4d",
    "TEST-SPEC":        "93cd5e",
    "HANDOFF":          "04de6f",
    "WIRE":             "7890ab",
    "IR":               "ab12cd",
    "MEMORY":           "cd34ef",
    "PERSONA":          "ef5601",
    "LEARNED-BPE":      "1a2b3c",
    "ATTN":             "9d8e7f",
    "DOCSTUB":          "6c5b4a",
    "EMBED":            "3f2e1d",
    "STRUCTURED-CONF":  "8a7b6c",
    "FORMAL-CLAIM":     "5d4c3b",
    "POLYGLOT":         "2e1f0a",
    "COLLAB":           "f0e1d2",
    "FEDERATED":        "a1b2c3",
    "DP":               "d4e5f6",
    "GRAMMAR":          "789abc",
    "MATH":             "0c1d2e",
    "TENSOR-SHAPE":     "5f6a7b",
    "SCHEMA":           "8c9d0e",
    "DEP":              "1f2a3b",
    "EFFECTS":          "4c5d6e",
    "PROMPT":           "7f8a9b",
    "AGENT-STATE":      "a3b4c5",
    # v5.2 — self-upgrade protocol (quality signal for runtime teaching)
    "UPGRADE":          "0123ab",
    # v5.4 — style/presentation/speech caps
    "STYLE":            "2c3d4e",
    "THEME":            "5f6a7c",
    "PROSODY":          "8b9c0d",
    "VOICE":            "1e2f3a",
    # v5.5 — embodied/avatar caps
    "AVATAR":           "4a5b6c",
    "MOTION":           "7d8e9f",
    # v5.7 — perception caps
    "VISION":           "a0b1c2",
    "LISTEN":           "d3e4f5",
    "SENSOR":           "6a7b8c",
    # v5.8 — orchestration caps
    "ROUTING-CONTEXT":  "8a9b0c",
    "WORK-UNIT":        "1d2e3f",
    "DISPATCH":         "4a5b6d",
    # v5.9 — coordination + session reference
    "COORDINATION":     "7e8f9a",
    "SESSION-REF":      "0b1c2d",
    # v5.10 — specialist dispatch (generic + named forms)
    "SPECIALIST-DISPATCH": "3f4a5b",
    "VISION-DISPATCH":     "6c7d8e",
    "LISTEN-DISPATCH":     "9f0a1b",
    "SENSOR-DISPATCH":     "2c3d4e",
    # v5.11 — interleaved thinking, reasoning persistence, scheduled wake-up
    "REASONING-PERSIST":   "b5c6d7",
    "THINK-ACT":           "c8d9e0",
    "THINKING-MODE":       "f1a2b3",
    "SCHEDULE":            "a4b5c6",
    "TOOL-RESULT-CACHE":   "d7e8f9",
    # v5.14 — authored visual emit
    "DRAW":                "e0f1a2",
    # v5.15 — long-running task visibility
    "TASK":                "b1c2d3",
}

# Fingerprints for the edge cap set (flat declaration, no nonce/HMAC)
EDGE_CAP_FINGERPRINTS: dict[str, str] = {
    "IR":        CAP_FINGERPRINTS["IR"],
    "EDIT-EDGE": "e4f5a6",
}

# L3 full capability set
_L3_CAPS = dict(CAP_FINGERPRINTS)

# L2 IR-only capability set
_L2_CAPS = {"IR": CAP_FINGERPRINTS["IR"]}


@dataclass
class ModelEntry:
    """Registry entry for a single model."""
    model_id: str
    level: LEVEL
    caps: dict[str, str] = field(default_factory=dict)
    # Model secret for HMAC verification (None = skip hmac check)
    secret: str | None = None
    ir_tokenizer: str = "cl100k"
    allow_persona_override: bool = False
    strict_antispoof: bool = False
    dp_user_id: str | None = None
    agent_state_scope: str = "workspace"
    constrained_decode: bool = False
    verifier_registry: dict[str, str] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)
    # "strict"  — trained model; deviations are protocol violations, not parsed
    # "fuzzy"   — prompted model; accept near-misses (bare bodies, inline wire:, etc.)
    parse_mode: str = "strict"
    # "standard" — full handshake with nonce + HMAC + two-section ack
    # "edge"     — flat cap declaration; no nonce/HMAC; [OZMA-EDGE ack: ...]
    handshake: str = "standard"
    # Teaching profile — how much of the protocol to inject per turn.
    # "auto" resolves via resolve_teaching_profile() at build time using
    # level + parse_mode + constrained_decode. See TEACHING_PROFILES docstring.
    teaching_profile: str = "auto"
    # Reasoning-budget knob declaration. None means "no dynamic budget
    # control — send the request as-is". When set, the shim picks a level
    # at intercept time via ReasoningResolver and injects the value at
    # knob.param_path in the request body. See reasoning.py.
    reasoning_knob: ReasoningKnob | None = None
    # Wire format for model-to-shim transport. "chars" is the universal
    # default and the only value implemented today. "tokens" is declared
    # but transport is Week-3 work (see docs/l4_plan.md). Negotiated in
    # the handshake — if either side declares "chars", the session runs
    # in chars.
    wire_format: str = "chars"
    # Fingerprint of the dialect (tokenizer + added-tokens manifest) this
    # model's weights were trained on. Stable across sessions, keyed into
    # the DialectStore so the shim can load the right codec without
    # ambiguity. None when wire_format=="chars" or when the dialect hasn't
    # been packaged yet.
    dialect_fingerprint: str | None = None
    # Filesystem path (or HF repo id) of the dialect's `.ozmap` directory.
    # Used by DialectStore.load_for_entry() to materialize the codec on
    # first request for this model. May be None during development — the
    # shim will fall back to chars transport with a one-time warning.
    dialect_path: str | None = None

    def __post_init__(self) -> None:
        if self.teaching_profile not in TEACHING_PROFILES:
            raise ValueError(
                f"Invalid teaching_profile {self.teaching_profile!r} for "
                f"{self.model_id!r}; must be one of {TEACHING_PROFILES}"
            )
        if self.wire_format not in WIRE_FORMATS:
            raise ValueError(
                f"Invalid wire_format {self.wire_format!r} for "
                f"{self.model_id!r}; must be one of {WIRE_FORMATS}"
            )
        if self.wire_format != "chars" and self.level < LEVEL.L4:
            raise ValueError(
                f"wire_format={self.wire_format!r} requires level >= L4; "
                f"{self.model_id!r} is {self.level.name}"
            )
        # Dialect fingerprint only makes sense alongside a non-default wire
        # — it's the key the DialectStore uses to materialize a codec, and
        # chars has no codec. Reject the inconsistent pair early so we
        # don't ship registry entries that would silently fall back.
        if self.dialect_fingerprint is not None and self.wire_format == "chars":
            raise ValueError(
                f"dialect_fingerprint is set but wire_format=='chars' for "
                f"{self.model_id!r}; dialects only apply to non-default wires"
            )

    def has_cap(self, cap: str) -> bool:
        return cap in self.caps

    def confirmed_caps(self, acked: dict[str, str]) -> dict[str, str]:
        """
        Intersect registry caps with acked caps, checking fingerprints.

        Returns the confirmed capability set. Caps with fingerprint mismatch
        fall through to L1 for that type (logged as cap_fingerprint_mismatch).
        """
        confirmed = {}
        for cap, fp in self.caps.items():
            if cap in acked:
                if acked[cap] == fp:
                    confirmed[cap] = fp
                # else: fingerprint mismatch — omit, caller logs
        return confirmed


def _prompted_l3(
    model_id: str,
    teaching: str = "standard",
    reasoning_knob: ReasoningKnob | None = None,
) -> "ModelEntry":
    """L3 entry for a prompted (non-trained) model: full caps, fuzzy parse.

    `teaching` defaults to "standard". Prompted frontier models are being
    *taught* the protocol at inference time — the core-5 legend + cap
    descriptors are what they need. `compact` is not the right override
    for under-taught frontier models: it's designed for trained compact
    models that already know the protocol and need only light per-turn
    reinforcement. Overriding to `full` is appropriate for models that
    routinely work outside the core-5 languages (C, Haskell, OCaml, Nix).

    `reasoning_knob` attaches a dynamic reasoning-budget control. For
    OpenRouter-prefixed models (anthropic/.../, google/.../, moonshotai/...)
    use the OPENROUTER_* templates — OpenRouter normalizes the schema.
    """
    return ModelEntry(
        model_id=model_id, level=LEVEL.L3, caps=_L3_CAPS,
        parse_mode="fuzzy", teaching_profile=teaching,
        reasoning_knob=reasoning_knob,
    )


def _prompted_l2(model_id: str, teaching: str = "full") -> "ModelEntry":
    """L2 entry for a prompted model that struggles with structured output: compression only.

    `teaching` defaults to "full" because these are the <0.80 compliance
    models — they need the most explicit instruction.
    """
    return ModelEntry(
        model_id=model_id, level=LEVEL.L2, caps=_L2_CAPS,
        parse_mode="fuzzy", teaching_profile=teaching,
    )


def resolve_teaching_profile(entry: "ModelEntry") -> str:
    """Resolve the effective teaching profile for an entry.

    - Explicit profile (not "auto") wins.
    - "auto": derived from level + parse_mode + constrained_decode:
        L0/L1                               → "native" (nothing injected anyway)
        L2 + constrained_decode             → "native" (edge model — has own ack header)
        L2 + parse_mode=strict              → "compact" (trained L2 knows IR)
        L2 + parse_mode=fuzzy               → "full"
        L3 + parse_mode=strict              → "native" (trained L3 knows protocol)
        L3 + parse_mode=fuzzy               → "standard"
    """
    if entry.teaching_profile and entry.teaching_profile != "auto":
        return entry.teaching_profile

    if entry.level <= LEVEL.L1:
        return "native"

    if entry.level == LEVEL.L2:
        if entry.constrained_decode:
            return "native"
        return "compact" if entry.parse_mode == "strict" else "full"

    # L3
    return "native" if entry.parse_mode == "strict" else "standard"


# Built-in registry — known Ozma-trained models.
# Deployments can extend this by calling CapabilityRegistry.register().
MODEL_CAPS: dict[str, ModelEntry] = {
    # --- Ozma-trained (native protocol) ---
    "ozma-coder-1": ModelEntry(
        model_id="ozma-coder-1",
        level=LEVEL.L3,
        caps=_L3_CAPS,
        ir_tokenizer="cl100k",
        allow_persona_override=True,
        verifier_registry={"smt2": "z3", "dafny": "dafny", "lean4": "lean"},
    ),
    "qwen3.6-ozma-l3-v1": ModelEntry(
        model_id="qwen3.6-ozma-l3-v1",
        level=LEVEL.L3,
        caps=_L3_CAPS,
        ir_tokenizer="qwen",
        allow_persona_override=True,
        teaching_profile="native",
        # v5.11 — see protocol.md §REASONING-PERSIST, §THINK-ACT, §SCHEDULE
        extra={
            "reasoning_persist": "workunit",
            "reasoning_persist_budget": 800,
            "thinking_default": "auto",
            "max_output_tokens_per_step": 16384,
            "max_output_tokens_total": 131072,
            "think_act_max_steps": 16,
            "schedule_scope_default": "workspace",
            "wake_modes": ["message-triggered", "scheduled"],
            "cache_policy": {
                "tool_result_retention": "bias-recent",
                "reasoning_retention": "accept-only",
            },
        },
    ),
    "qwen3.6-ozma-l2-v1": ModelEntry(
        model_id="qwen3.6-ozma-l2-v1",
        level=LEVEL.L2,
        caps=_L2_CAPS,
        ir_tokenizer="qwen",
        teaching_profile="compact",  # trained L2 — IR legend redundant but keep a core
    ),
    # Local deployment: Qwen3.6-35B-A3B + ozma-wizard-1 LoRA via llama-server
    "ozma-wizard-1": ModelEntry(
        model_id="ozma-wizard-1",
        level=LEVEL.L3,
        caps=_L3_CAPS,
        ir_tokenizer="qwen",
        allow_persona_override=True,
        parse_mode="strict",
        teaching_profile="native",
    ),
    # Production path: Kimi K2.6 + Ozma L3 LoRA, served via native Moonshot
    # API. The LoRA knows the protocol natively (parse_mode=strict,
    # teaching=native — no per-turn cap teaching needed). Reasoning is
    # controlled via Moonshot's binary `thinking.type` switch, not an effort
    # slider — the resolver picks disabled for mechanical/qa and enabled for
    # everything else.
    "ozma-kimi-k2.6-l3": ModelEntry(
        model_id="ozma-kimi-k2.6-l3",
        level=LEVEL.L3,
        caps=_L3_CAPS,
        ir_tokenizer="cl100k",
        allow_persona_override=True,
        parse_mode="strict",
        teaching_profile="native",
        reasoning_knob=MOONSHOT_THINKING,
    ),

    # --- Ozma edge models (LE profile: IR + EDIT-EDGE, constrained decode) ---
    "ozma-coder-3b": ModelEntry(
        model_id="ozma-coder-3b",
        level=LEVEL.L2,
        caps=EDGE_CAP_FINGERPRINTS,
        ir_tokenizer="cl100k",
        constrained_decode=True,
        handshake="edge",
        parse_mode="strict",
    ),
    "ozma-wizard-0.5b": ModelEntry(
        model_id="ozma-wizard-0.5b",
        level=LEVEL.L2,
        caps=EDGE_CAP_FINGERPRINTS,
        ir_tokenizer="qwen",
        constrained_decode=True,
        handshake="edge",
        parse_mode="strict",
    ),
    "ozma-wizard-1b": ModelEntry(
        model_id="ozma-wizard-1b",
        level=LEVEL.L2,
        caps=EDGE_CAP_FINGERPRINTS,
        ir_tokenizer="qwen",
        constrained_decode=True,
        handshake="edge",
        parse_mode="strict",
    ),

    # --- L4 PoC: Qwen2.5-Coder-3B + Ozma dialect LoRA ------------------------
    # Dialect-pipeline validator. Extended tokenizer (qwen2.5-ozma-v1, +36
    # Ozma glyphs) with LoRA + embed_tokens/lm_head modules_to_save. Caps
    # are minimal (IR + EDIT) because the training mix only exercised those
    # cap grammars; broader declaration awaits post-training compliance
    # measurement. See models/ozma-coder-3b-lora/config/lora_l4.yaml.
    "ozma-coder-3b-l4-lora": ModelEntry(
        model_id="ozma-coder-3b-l4-lora",
        level=LEVEL.L4,
        caps={
            "IR":   CAP_FINGERPRINTS["IR"],
            "EDIT": CAP_FINGERPRINTS["EDIT"],
        },
        ir_tokenizer="qwen",
        parse_mode="strict",
        teaching_profile="native",
        wire_format="tokens",
        dialect_fingerprint=(
            "cd03ff576658f52f6943600701e846f7b0ec9f188e356946dd4964a65a45d78c"
        ),
        dialect_path="models/training/dialects/qwen2.5-ozma-v1",
    ),

    # --- Prompted models: L3 (compliance ≥ 0.80, fuzzy parse) ---
    # Scores from compliance harness run 2026-04-19.
    # These are all frontier/near-frontier models being *taught* the protocol
    # at inference time — they haven't seen it in training. Standard profile
    # (header + core-5 legend + memory ops + cap descriptors) is the right
    # tier for this class; the Kimi K2.6 profile bench (2026-04-21) showed
    # compact under-teaches frontier models on cap-name-only entries.
    #
    # `compact` is reserved for trained *compact* models that already know
    # the protocol and need only a light per-turn reinforcement, not for
    # under-teaching frontier ones.
    # Reasoning-capable models get a knob attached. OpenRouter routes use the
    # OPENROUTER_* templates; the effort→budget translation is brittle for
    # providers without a native effort slider, so the knob's max_tokens
    # backstop pins an authoritative ceiling.
    "anthropic/claude-sonnet-4-5":             _prompted_l3("anthropic/claude-sonnet-4-5", reasoning_knob=OPENROUTER_ANTHROPIC_MAX),
    "anthropic/claude-sonnet-4.6":             _prompted_l3("anthropic/claude-sonnet-4.6", reasoning_knob=OPENROUTER_ANTHROPIC_MAX),
    "anthropic/claude-opus-4-5":               _prompted_l3("anthropic/claude-opus-4-5",   reasoning_knob=OPENROUTER_ANTHROPIC_MAX),
    "meta-llama/llama-4-maverick":             _prompted_l3("meta-llama/llama-4-maverick"),   # 0.95
    "meta-llama/llama-3.3-70b-instruct":       _prompted_l3("meta-llama/llama-3.3-70b-instruct"),  # 0.90
    "openai/gpt-4.1-mini":                     _prompted_l3("openai/gpt-4.1-mini"),   # 0.90 — no reasoning mode
    "openai/gpt-4.1":                          _prompted_l3("openai/gpt-4.1"),        # no reasoning mode
    "openai/gpt-4o-2024-11-20":                _prompted_l3("openai/gpt-4o-2024-11-20"),  # 0.85
    "openai/o3-mini":                          _prompted_l3("openai/o3-mini",         reasoning_knob=OPENROUTER_EFFORT),
    "openai/o4-mini":                          _prompted_l3("openai/o4-mini",         reasoning_knob=OPENROUTER_EFFORT),
    "google/gemini-2.5-flash":                 _prompted_l3("google/gemini-2.5-flash", reasoning_knob=OPENROUTER_EFFORT),   # 0.90
    "google/gemini-2.5-pro":                   _prompted_l3("google/gemini-2.5-pro",   reasoning_knob=OPENROUTER_EFFORT),
    "google/gemma-4-26b-a4b-it":               _prompted_l3("google/gemma-4-26b-a4b-it"),  # 0.85
    "moonshotai/kimi-k2.6":                    _prompted_l3("moonshotai/kimi-k2.6",    reasoning_knob=OPENROUTER_EFFORT),  # 0.83 — see tests/bench_kimi_k26_profiles.py
    # Native Moonshot API (api.moonshot.ai) — base Kimi K2.6 without the
    # Ozma LoRA, for benchmarking and A/B against the prompted OpenRouter
    # route. Uses the binary thinking.type knob directly (no effort shim).
    "kimi-k2.6":                               _prompted_l3("kimi-k2.6",               reasoning_knob=MOONSHOT_THINKING),

    # --- Prompted models: L2 compression only (compliance < 0.80) ---
    # These models get IR + legend + stub instructions but no structured output protocol.
    # Low compliance → full teaching profile.
    "anthropic/claude-3-haiku":                _prompted_l2("anthropic/claude-3-haiku",    "full"),      # 0.75
    "openai/gpt-4o-mini":                      _prompted_l2("openai/gpt-4o-mini",          "full"),      # 0.65
    "google/gemma-3-12b-it":                   _prompted_l2("google/gemma-3-12b-it",       "full"),      # 0.60
    "mistralai/mistral-small-3.2-24b-instruct": _prompted_l2("mistralai/mistral-small-3.2-24b-instruct", "full"),  # 0.20
}


class CapabilityRegistry:
    """
    Mutable registry of model → capability entries.

    Deployments extend the built-in registry with locally-hosted models.
    """

    def __init__(self) -> None:
        # Start with hand-curated trained models, then overlay auto-generated
        # OpenRouter entries (hand-curated wins on conflict).
        try:
            from .openrouter_models import OPENROUTER_ENTRIES
            self._entries: dict[str, ModelEntry] = {**OPENROUTER_ENTRIES, **MODEL_CAPS}
        except ImportError:
            self._entries = dict(MODEL_CAPS)

    def register(self, entry: ModelEntry) -> None:
        """Register or replace an entry."""
        self._entries[entry.model_id] = entry

    def lookup(self, model_id: str) -> ModelEntry:
        """Return the entry for model_id, or a default L1 passthrough entry if unknown."""
        if model_id in self._entries:
            return self._entries[model_id]
        # Unknown model → L1 passthrough. Callers register an explicit entry
        # to enable IR (L2) or full structured protocol (L3).
        return ModelEntry(model_id=model_id, level=LEVEL.L1, caps={}, parse_mode="fuzzy")

    def level(self, model_id: str) -> LEVEL:
        return self.lookup(model_id).level

    def __contains__(self, model_id: str) -> bool:
        return model_id in self._entries

    @classmethod
    def default(cls) -> "CapabilityRegistry":
        """Return a shared default registry (built-in models only)."""
        return _DEFAULT_REGISTRY


_DEFAULT_REGISTRY = CapabilityRegistry()
