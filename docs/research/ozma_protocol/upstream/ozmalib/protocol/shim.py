"""
shim.py — OzmaShim: the main entry point for the wire protocol.

Wraps any OpenAI-compatible /v1/chat/completions request/response pair.
Stock models (L1) pass through with no modification. Protocol-capable models
(L2/L3) get system prompt injection, probe/ack handling, and structured
output parsing.

The client never sees protocol tokens — they are injected on the way in
and stripped on the way out.

Usage::

    shim = OzmaShim()

    # Inbound
    result = shim.intercept_request(body, session_id="sess_abc")
    raw = client.chat.completions.create(**result.body)

    # Outbound
    turn = shim.intercept_response(raw, result.session_id)
    print(turn.text)      # prose response (protocol tokens stripped)
    print(turn.edits)     # list[EditCmd]
    print(turn.plans)     # list[PlanBlock]
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Any

import time

from .dialect import DialectCodec, DialectStore
from .handshake import HandshakeManager
from .parser import TurnParser
from .reasoning import ReasoningChoice, ReasoningResolver, set_dotted
from .registry import LEVEL, CapabilityRegistry, ModelEntry
from .store import MemorySessionStore, SQLiteSessionStore, SessionStore
from .stream import StreamState, StreamingShim
from .system_prompt import SystemPromptBuilder, normalize_language
from .telemetry import ShimCallRecord, TelemetrySink
from .types import Turn

# Matches ```<lang>\n — only the fence-open line's lang tag is captured.
# The rest of the body is irrelevant for language detection.
_FENCE_RE = re.compile(r"^```([A-Za-z0-9+#/_.\-]+)", re.MULTILINE)


def _dotted_is_set(body: dict[str, Any], path: str) -> bool:
    """True iff every segment of `path` exists in `body` and the leaf is not None."""
    cursor: Any = body
    for part in path.split("."):
        if not isinstance(cursor, dict) or part not in cursor:
            return False
        cursor = cursor[part]
    return cursor is not None


def _extract_reasoning_tokens(response: Any) -> int | None:
    """Best-effort pull of reasoning-token count from a provider response.

    Different providers surface this differently. Tries, in order:
      1. usage.completion_tokens_details.reasoning_tokens   (OpenAI, some OR)
      2. usage.reasoning_tokens                             (some OR routes)
      3. len(choices[0].message.reasoning)                  (Kimi, fallback heuristic — chars/4 token estimate)
    Returns None if none available.
    """
    def _get(obj: Any, key: str) -> Any:
        if isinstance(obj, dict):
            return obj.get(key)
        return getattr(obj, key, None)

    usage = _get(response, "usage") or {}
    details = _get(usage, "completion_tokens_details") or {}
    val = _get(details, "reasoning_tokens")
    if isinstance(val, int):
        return val
    val = _get(usage, "reasoning_tokens")
    if isinstance(val, int):
        return val
    # Kimi / some OpenRouter responses expose a `reasoning` string field
    # on the message; estimate tokens as chars/4 (cl100k heuristic).
    choices = _get(response, "choices") or []
    if choices:
        msg = _get(choices[0], "message") or {}
        reasoning = _get(msg, "reasoning")
        if isinstance(reasoning, str) and reasoning:
            return max(1, len(reasoning) // 4)
    return None


def _detect_languages_from_messages(messages: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """Extract (synthetic-filename, canonical-language) pairs from fenced
    code blocks across all messages. Returns one entry per distinct
    language seen, with a synthetic filename `<fence>.<lang>` since fence
    blocks carry no filename. Unknown fence tags are dropped silently.
    """
    seen: dict[str, str] = {}
    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, str):
            continue
        for raw in _FENCE_RE.findall(content):
            canonical = normalize_language(raw)
            if canonical and canonical not in seen:
                seen[canonical] = f"<fence>.{raw.lower()}"
    return [(fname, lang) for lang, fname in seen.items()]


# Minimal GBNF grammar for llama.cpp constrained decoding.
# Allows any JSON-like response while gating on Ozma protocol keywords.
_LLAMACPP_GBNF = r"""
root   ::= prose | protocol-block
prose  ::= [^\n]+ "\n"
protocol-block ::= keyword " " [^\n]+ "\n" (fenced-body)?
keyword ::= "edit" | "delete" | "add" | "plan" | "claim" | "reasoning:" | "remember" | "test-spec" | "effects" | "dep" | "schema" | "outline:" | "hint" | "docstub" | "wire:" | "prompt" | "handoff" | "agent-state" | "desktop:" | "math" | "tensor" | "graph" | "tree" | "structured-conf" | "dp" | "grammar" | "attn" | "embed" | "collab" | "federated" | "polyglot" | "learned-bpe:"
fenced-body ::= "```" [^\n]* "\n" ([^`] | "`" [^`] | "``" [^`])* "```\n"
""".strip()


@dataclass
class ShimRequest:
    """Result of intercept_request — a modified body ready to send to the model."""
    body: dict[str, Any]        # drop-in replacement for the original body
    session_id: str
    model_entry: ModelEntry
    original_system: str        # the system message before shim injection
    is_first_turn: bool
    reasoning_choice: ReasoningChoice | None = None
    # L4 token-wire transport. When wire_format=="tokens" and a dialect
    # codec is available, the shim pre-tokenizes the rendered prompt and
    # exposes the token ID stream here. Callers serving via vLLM can
    # pass this as `prompt_token_ids` on /v1/completions to skip
    # server-side tokenization and guarantee the dialect tokenizer is
    # the one applied. None when wire is chars, when no codec is loaded,
    # or when chat-template rendering failed — caller falls back to the
    # `body.messages` path in any of those cases.
    prompt_token_ids: list[int] | None = None
    dialect_fingerprint: str | None = None


@dataclass
class ShimTurn:
    """Result of intercept_response — parsed turn plus telemetry."""
    turn: Turn
    session_id: str
    model_id: str
    level: LEVEL
    ack_found: bool
    ack_hmac_valid: bool | None
    cap_mismatches: list[str]
    # Protocol tokens stripped from the response
    stripped_response: str
    telemetry: dict[str, Any] = field(default_factory=dict)


class OzmaShim:
    """
    Drop-in shim over any OpenAI-compatible API.

    Transparent for L0/L1 models. For L2/L3 models:
    - First turn: injects system prompt + probe, records session nonce.
    - Subsequent turns: injects system prompt only (no probe).
    - Response: detects ack (first turn only), strips protocol tokens,
      parses structured output into a Turn.

    Thread-safe: each request is stateless except for per-session nonce
    tracking in HandshakeManager (which is itself thread-safe via dict).
    """

    def __init__(
        self,
        registry: CapabilityRegistry | None = None,
        native_header: bool = False,
        session_store: SessionStore | None = None,
        reasoning_resolver: ReasoningResolver | None = None,
        telemetry_sink: TelemetrySink | None = None,
        dialect_store: DialectStore | None = None,
    ) -> None:
        """
        Args:
            registry:           capability registry; defaults to built-in registry
            native_header:      if True, use the minimal 61-token native header
                                for models that natively speak the protocol
                                (Ozma-Coder-1 mode). Ignored for L1 models.
            session_store:      persistence backend for nonces and confirmed caps.
                                Defaults to SQLiteSessionStore (~/.ozma/sessions.db).
                                Pass MemorySessionStore() or
                                SQLiteSessionStore(":memory:") for ephemeral use.
            reasoning_resolver: picks reasoning-budget level per request when the
                                model's ModelEntry declares a reasoning_knob.
                                Defaults to a ReasoningResolver with an in-memory
                                stats store. Share one instance across requests
                                to let stats accumulate.
            telemetry_sink:     optional. When set, every intercept_response
                                emits a ShimCallRecord with the per-call
                                telemetry (model, level, task_class, reasoning
                                choice + observed tokens, latency, ack outcome,
                                cap mismatches, turn-block counts). Default
                                None = no persistence; callers still get the
                                dict on ShimTurn.telemetry.
        """
        self.registry = registry or CapabilityRegistry.default()
        self.native_header = native_header
        self._store: SessionStore = session_store if session_store is not None \
            else SQLiteSessionStore()
        self._handshake = HandshakeManager(store=self._store)
        self._prompt_builder = SystemPromptBuilder()
        self._parser = TurnParser()
        self._reasoning_resolver = reasoning_resolver or ReasoningResolver()
        self._telemetry_sink = telemetry_sink
        # L4 codec cache. Only consulted when a ModelEntry declares
        # wire_format != "chars" and dialect_fingerprint is set. Default
        # None keeps the shim dependency-free for chars-only deployments.
        self._dialect_store = dialect_store
        # Remember which fingerprints we've already warned about so the
        # "dialect declared but not loaded" log doesn't fire per-request.
        self._dialect_warned: set[str] = set()
        # session_id → requested model_id (for resolving OpenRouter model aliases)
        self._session_models: dict[str, str] = {}
        # session_id → (last ReasoningChoice, model_id) for observe-on-response
        self._session_reasoning: dict[str, tuple[ReasoningChoice, str]] = {}
        # session_id → perf_counter timestamp at intercept_request (for latency)
        self._session_t0: dict[str, float] = {}

    # ── Inbound ──────────────────────────────────────────────────────────

    def intercept_request(
        self,
        body: dict[str, Any],
        session_id: str | None = None,
        legend: dict[str, str] | None = None,
        active_files: list[tuple[str, str]] | None = None,
        stale_paths: set[str] | None = None,
        ir_languages: list[str] | None = None,
    ) -> ShimRequest:
        """
        Intercept and modify a /v1/chat/completions request body.

        Args:
            body:         original request body dict
            session_id:   caller-supplied ID; generated if None
            legend:       {short → original} identifier mapping for this turn
            active_files: [(filename, language), ...] files in context
            stale_paths:  paths of stale cached files (injected as warnings)
            ir_languages: authoritative IR language list when the shim's
                          caller has richer context than fence-tag detection
                          (project inventory, session history, task shape).
                          ``None`` → auto-detect from active_files / fences.
                          ``[]`` → explicitly teach no languages this turn
                          (e.g. raw-binary-IR audit where legends are noise).

        Returns:
            ShimRequest with a modified body and session metadata.
        """
        sid = session_id or f"sess_{uuid.uuid4().hex[:8]}"
        body = dict(body)   # shallow copy — don't mutate caller's dict

        model_id = body.get("model", "")
        entry = self.registry.lookup(model_id)
        is_first = self._handshake.is_first_turn(sid)
        self._session_models[sid] = model_id  # remember for response model alias resolution
        if self._telemetry_sink is not None:
            self._session_t0[sid] = time.perf_counter()

        original_system = self._extract_system(body)

        # Reasoning-budget resolution runs at every level — even L0/L1 models
        # can have a reasoning knob (OpenAI o-*, for instance). Respect any
        # caller-supplied value at the knob's param_path; only inject if the
        # caller didn't already set one.
        reasoning_choice = self._maybe_inject_reasoning(body, entry, sid)

        # Decision-phase telemetry: what we committed to before sending the
        # request. If intercept_response never fires (timeout, upstream error),
        # this row is all the sink has — query_failed() surfaces it.
        self._emit_decision_record(sid, model_id, entry.level, reasoning_choice)

        if entry.level == LEVEL.L0 or entry.level == LEVEL.L1:
            # Pass-through — no protocol injection
            return ShimRequest(
                body=body,
                session_id=sid,
                model_entry=entry,
                original_system=original_system,
                is_first_turn=is_first,
                reasoning_choice=reasoning_choice,
            )

        # Build system message
        if self.native_header and entry.level >= LEVEL.L3:
            system_msg = self._prompt_builder.build_native_header(
                session_id=sid,
                model_id=model_id,
            )
        else:
            probe_line = ""
            if is_first:
                probe_line = self._handshake.generate_probe(sid, entry)
            system_msg = self._prompt_builder.build_system(entry, probe_line=probe_line)
            if original_system:
                system_msg = f"{original_system}\n\n{system_msg}"

        # Build per-turn user prefix — entry is threaded through so
        # system_prompt can append IR lines for languages outside the
        # profile's base set without destabilising the system block.
        #
        # Language-teaching precedence:
        #   1. Caller-supplied ir_languages (authoritative — shim owns this).
        #   2. Caller-supplied active_files (language inferred from file tags).
        #   3. Auto-detection from fenced code blocks in the messages.
        # Auto-detection only fills active_files when it's None AND
        # ir_languages is None, so an explicit empty list from the caller
        # survives all the way down to build_user_prefix.
        resolved_active = active_files
        if resolved_active is None and ir_languages is None:
            resolved_active = _detect_languages_from_messages(
                body.get("messages") or []
            ) or None
        user_prefix = self._prompt_builder.build_user_prefix(
            legend=legend,
            active_files=resolved_active,
            stale_paths=stale_paths,
            entry=entry,
            ir_languages=ir_languages,
        )

        body = self._inject_system(body, system_msg)
        if user_prefix:
            body = self._prepend_user_prefix(body, user_prefix)
        if entry.constrained_decode:
            body = self._apply_constrained_decode(body, entry)

        prompt_token_ids, dialect_fp = self._encode_for_backend(body, entry)

        return ShimRequest(
            body=body,
            session_id=sid,
            model_entry=entry,
            original_system=original_system,
            is_first_turn=is_first,
            reasoning_choice=reasoning_choice,
            prompt_token_ids=prompt_token_ids,
            dialect_fingerprint=dialect_fp,
        )

    # ── Outbound ─────────────────────────────────────────────────────────

    def intercept_response(
        self,
        response: Any,
        session_id: str,
    ) -> ShimTurn:
        """
        Parse and strip protocol tokens from a model response.

        Args:
            response:   raw response from the model API (dict or object
                        with .choices[0].message.content)
            session_id: session ID from the ShimRequest

        Returns:
            ShimTurn with parsed Turn and telemetry.
        """
        # Capture reasoning choice BEFORE observe pops it — we want it in the
        # telemetry record alongside the observed token count.
        reasoning_info = self._session_reasoning.get(session_id)
        # Feed observed reasoning-token usage back into stats before we do
        # anything else — the hook is a no-op if we didn't inject a level.
        reasoning_tokens = self._observe_reasoning(response, session_id)

        text = self._extract_content(response)
        model_id = self._extract_model(response)
        entry = self.registry.lookup(model_id)
        # If response model_id is an alias (e.g. OpenRouter versioned IDs) and
        # the lookup returned a generic L1 fallback, try the request model_id.
        if entry.level == LEVEL.L1 and session_id in self._session_models:
            req_model_id = self._session_models[session_id]
            if req_model_id != model_id:
                alt_entry = self.registry.lookup(req_model_id)
                if alt_entry.level > LEVEL.L1:
                    entry = alt_entry
                    model_id = req_model_id

        if entry.level == LEVEL.L0 or entry.level == LEVEL.L1:
            turn = Turn(raw=text, text=text)
            self._emit_telemetry(
                session_id=session_id, model_id=model_id, level=entry.level,
                reasoning_info=reasoning_info, reasoning_tokens=reasoning_tokens,
                ack_found=False, cap_mismatches=[], turn=turn,
            )
            return ShimTurn(
                turn=turn,
                session_id=session_id,
                model_id=model_id,
                level=entry.level,
                ack_found=False,
                ack_hmac_valid=None,
                cap_mismatches=[],
                stripped_response=text,
            )

        ack_found = False
        ack_hmac_valid: bool | None = None
        cap_mismatches: list[str] = []
        working_text = text

        # Ack detection on first turn only
        if self._handshake.is_first_turn(session_id):
            ack_result = self._handshake.detect_ack(session_id, text, entry)
            ack_found = ack_result.found
            ack_hmac_valid = ack_result.hmac_valid
            cap_mismatches = ack_result.cap_mismatches
            working_text = ack_result.stripped_response

            if ack_result.found and ack_result.header:
                # Confirmed caps already written to store by HandshakeManager.
                # Store the registry-intersected set for session_caps() queries.
                confirmed = entry.confirmed_caps(ack_result.header.caps)
                self._store.mark_acked(
                    session_id, confirmed, model_id, entry.level
                )

        # Parse structured protocol output — strict for trained models, fuzzy for prompted
        strict = (entry.parse_mode == "strict")
        turn = self._parser.parse(working_text, strict=strict)

        # Telemetry
        telemetry: dict[str, Any] = {
            "level": entry.level.name,
            "ack_found": ack_found,
            "edits": len(turn.edits),
            "plans": len(turn.plans),
            "claims": len(turn.claims),
            "memory_writes": len(turn.memory_writes),
        }
        if cap_mismatches:
            telemetry["cap_fingerprint_mismatches"] = cap_mismatches

        self._emit_telemetry(
            session_id=session_id, model_id=model_id, level=entry.level,
            reasoning_info=reasoning_info, reasoning_tokens=reasoning_tokens,
            ack_found=ack_found, cap_mismatches=cap_mismatches, turn=turn,
        )

        return ShimTurn(
            turn=turn,
            session_id=session_id,
            model_id=model_id,
            level=entry.level,
            ack_found=ack_found,
            ack_hmac_valid=ack_hmac_valid,
            cap_mismatches=cap_mismatches,
            stripped_response=working_text,
            telemetry=telemetry,
        )

    # ── Session queries ───────────────────────────────────────────────────

    def session_caps(self, session_id: str) -> dict[str, str]:
        """Confirmed capability set for a session (populated after first ack)."""
        return self._store.get_caps(session_id)

    def stream(self, session_id: str) -> "StreamingShim":
        """
        Return a StreamingShim bound to this shim's parser.

        Usage::
            ss = shim.stream(session_id)
            state = ss.begin(session_id)
            for chunk in raw_stream:
                partial = ss.feed_delta(state, chunk)
            final_turn = ss.end_stream(state)
        """
        return StreamingShim(parser=self._parser, session_id=session_id)

    def session_level(self, session_id: str, model_id: str) -> LEVEL:
        return self.registry.level(model_id)

    # ── Reasoning ─────────────────────────────────────────────────────────

    def _maybe_inject_reasoning(
        self, body: dict[str, Any], entry: ModelEntry, sid: str,
    ) -> ReasoningChoice | None:
        """Resolve a reasoning level and inject it into `body` in-place.

        Two injections happen when enabled:
          1. The reasoning param at knob.param_path (effort/budget_tokens/...)
          2. A max_tokens backstop = target_reasoning + response_headroom,
             when knob.max_tokens_backstop is True. This is the authoritative
             budget cap for providers whose knob label is translated by an
             upstream shim (notably OpenRouter → non-effort-native providers).

        Caller-supplied values at either slot are preserved — they know
        something we don't.

        Returns the ReasoningChoice so callers can inspect the rationale.
        Stashes it under the session id so intercept_response can feed the
        observed reasoning-token count back into stats.
        """
        knob = entry.reasoning_knob
        if knob is None:
            return None
        # Don't override a caller-supplied value at the knob's param_path.
        if _dotted_is_set(body, knob.param_path):
            return None

        choice = self._reasoning_resolver.resolve(
            messages=body.get("messages") or [],
            knob=knob,
            model_id=entry.model_id,
        )
        if choice.value is not None:
            set_dotted(body, knob.param_path, choice.value)

        # Max-tokens backstop: only inject when the knob asks for it AND the
        # caller didn't already pin max_tokens. Size by EXPECTED reasoning
        # spend at the chosen level (max of target and observed level cost),
        # plus headroom for the visible response. Sizing by target alone
        # truncates when the level spends more than target asked for.
        if (knob.max_tokens_backstop
                and choice.expected_reasoning_tokens > 0
                and body.get("max_tokens") is None):
            body["max_tokens"] = (
                choice.expected_reasoning_tokens + knob.response_headroom_tokens
            )

        self._session_reasoning[sid] = (choice, entry.model_id)
        return choice

    def _observe_reasoning(self, response: Any, sid: str) -> int | None:
        """Feed response's reasoning-token usage back into stats if we set
        a level for this session. Returns the observed token count (or None
        if we didn't inject or the provider didn't report it) so the caller
        can stamp it into the telemetry record."""
        entry_info = self._session_reasoning.pop(sid, None)
        if entry_info is None:
            return None
        choice, model_id = entry_info
        tokens = _extract_reasoning_tokens(response)
        if tokens is None:
            return None
        self._reasoning_resolver.observe(choice, model_id, tokens)
        return tokens

    def _emit_decision_record(
        self,
        session_id: str,
        model_id: str,
        level: LEVEL,
        reasoning_choice: ReasoningChoice | None,
    ) -> None:
        """Record a phase='decision' row at intercept_request time. Carries
        everything knowable before the upstream reply; outcome fields stay at
        their defaults. No-op when no sink is wired."""
        if self._telemetry_sink is None:
            return
        record = ShimCallRecord(
            session_id=session_id,
            model_id=model_id,
            level=level.name,
            phase="decision",
            task_class=reasoning_choice.task_class if reasoning_choice else None,
            reasoning_level_idx=(
                reasoning_choice.level_idx if reasoning_choice else None
            ),
            reasoning_value=(
                str(reasoning_choice.value)
                if reasoning_choice and reasoning_choice.value is not None
                else None
            ),
        )
        self._telemetry_sink.record(record)

    def _emit_telemetry(
        self,
        session_id: str,
        model_id: str,
        level: LEVEL,
        reasoning_info: tuple[ReasoningChoice, str] | None,
        reasoning_tokens: int | None,
        ack_found: bool,
        cap_mismatches: list[str],
        turn: Turn,
    ) -> None:
        """Build and record a phase='completed' ShimCallRecord. No-op when no
        sink is wired."""
        if self._telemetry_sink is None:
            # Still clean up the t0 stash if it was set (sink-None path never
            # sets it, but be defensive if the sink was detached mid-session).
            self._session_t0.pop(session_id, None)
            return
        t0 = self._session_t0.pop(session_id, None)
        latency_ms = (time.perf_counter() - t0) * 1000.0 if t0 is not None else None

        choice = reasoning_info[0] if reasoning_info else None
        record = ShimCallRecord(
            session_id=session_id,
            model_id=model_id,
            level=level.name,
            phase="completed",
            task_class=choice.task_class if choice else None,
            reasoning_level_idx=choice.level_idx if choice else None,
            reasoning_value=(
                str(choice.value) if choice and choice.value is not None else None
            ),
            reasoning_tokens=reasoning_tokens,
            latency_ms=latency_ms,
            ack_found=ack_found,
            cap_mismatches=list(cap_mismatches),
            edit_count=len(turn.edits),
            plan_count=len(turn.plans),
            claim_count=len(turn.claims),
            memory_write_count=len(turn.memory_writes),
        )
        self._telemetry_sink.record(record)

    # ── L4 codec ──────────────────────────────────────────────────────────

    def _encode_for_backend(
        self,
        body: dict[str, Any],
        entry: ModelEntry,
    ) -> tuple[list[int] | None, str | None]:
        """Pre-tokenize the prompt using the model's dialect codec when
        wire_format != "chars" and a codec is available.

        Returns `(token_ids, fingerprint)`. Both are None when:
          - wire_format is "chars" (most traffic)
          - entry has no dialect_fingerprint (registry entry is chars-only)
          - the shim has no DialectStore wired, or the codec isn't cached
            and can't be loaded from entry.dialect_path
          - chat-template rendering failed (caller sees body unchanged,
            falls back to the messages[] transport path cleanly)

        The encoded form represents the concatenation of all messages
        rendered through the dialect codec's tokenizer chat template. If
        the tokenizer has no registered chat template, we fall back to a
        minimal role-prefixed rendering that still round-trips through
        the parser on the response side.
        """
        if entry.wire_format == "chars" or entry.dialect_fingerprint is None:
            return None, None
        if self._dialect_store is None:
            self._warn_dialect_once(
                entry.dialect_fingerprint,
                f"model {entry.model_id!r} declares wire_format={entry.wire_format!r} "
                f"with dialect {entry.dialect_fingerprint[:12]}... but shim was "
                f"constructed without a DialectStore; falling back to chars transport",
            )
            return None, None

        try:
            codec = self._dialect_store.ensure_for_entry(entry)
        except Exception as e:  # pragma: no cover — exercised via shim tests
            self._warn_dialect_once(
                entry.dialect_fingerprint,
                f"dialect load failed for {entry.model_id!r}: {e}; falling back to chars",
            )
            return None, None

        if codec is None:
            self._warn_dialect_once(
                entry.dialect_fingerprint,
                f"dialect {entry.dialect_fingerprint[:12]}... declared by "
                f"{entry.model_id!r} is not loaded and no dialect_path is set; "
                f"falling back to chars transport",
            )
            return None, None

        messages = body.get("messages") or []
        rendered = self._render_messages(codec, messages)
        if rendered is None:
            return None, None

        token_ids = codec.encode(rendered)
        return token_ids, codec.fingerprint

    @staticmethod
    def _render_messages(
        codec: DialectCodec,
        messages: list[dict[str, Any]],
    ) -> str | None:
        """Render a messages list to a single prompt string via the
        codec's tokenizer chat template, falling back to role-prefixed
        concatenation when no template is registered.

        Returns None on failure so the caller can fall back to the
        messages[] transport path. Never raises — we'd rather transport
        chars than drop the request.
        """
        tokenizer = codec._tokenizer  # pylint: disable=protected-access
        try:
            if getattr(tokenizer, "chat_template", None):
                return tokenizer.apply_chat_template(
                    messages,
                    tokenize=False,
                    add_generation_prompt=True,
                )
        except Exception:
            # fall through to manual rendering
            pass

        # Minimal fallback: role-prefixed blocks separated by blank lines.
        # Good enough for tokenization; the model's weights don't care
        # about exact framing at this layer (the shim's structural
        # parser operates on the decoded response, not the raw prompt).
        parts: list[str] = []
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if isinstance(content, str) and content:
                parts.append(f"<|{role}|>\n{content}")
        return "\n\n".join(parts) if parts else None

    def _warn_dialect_once(self, fingerprint: str, message: str) -> None:
        """Emit a dialect warning once per fingerprint per shim lifetime.

        Uses stderr rather than logging so we don't force a logging
        config on embedders. Every real production integration wires
        its own handler — this is the minimum signal needed to diagnose
        a misconfigured registry entry.
        """
        if fingerprint in self._dialect_warned:
            return
        self._dialect_warned.add(fingerprint)
        import sys
        print(f"[ozma-shim] {message}", file=sys.stderr)

    # ── Helpers ───────────────────────────────────────────────────────────

    @staticmethod
    def _apply_constrained_decode(body: dict[str, Any], entry: ModelEntry) -> dict[str, Any]:
        """
        Inject constrained-decode parameters based on backend type.

        Backend detection (in priority order):
        1. entry.extra["backend"] == "llamacpp" → inject grammar= (GBNF)
        2. entry.extra["backend"] == "vllm"     → inject guided_json schema
        3. default (OpenAI-compatible)           → inject response_format JSON schema

        The injected schema is a minimal Ozma protocol response schema.
        """
        backend = entry.extra.get("backend", "openai")
        # Minimal schema: object with optional string fields matching protocol caps
        _OZMA_SCHEMA = {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "edits": {"type": "array"},
                "plans": {"type": "array"},
            },
        }
        if backend == "llamacpp":
            # llama.cpp uses a GBNF grammar string
            body = dict(body)
            body.setdefault("grammar", _LLAMACPP_GBNF)
        elif backend == "vllm":
            body = dict(body)
            body.setdefault("guided_json", _OZMA_SCHEMA)
        else:
            # OpenAI-compatible: response_format
            body = dict(body)
            body.setdefault("response_format", {
                "type": "json_schema",
                "json_schema": {"name": "ozma_response", "schema": _OZMA_SCHEMA},
            })
        return body

    @staticmethod
    def _extract_system(body: dict[str, Any]) -> str:
        for msg in body.get("messages", []):
            if msg.get("role") == "system":
                return msg.get("content", "")
        return ""

    @staticmethod
    def _inject_system(body: dict[str, Any], system_msg: str) -> dict[str, Any]:
        """Replace or prepend the system message in the messages list."""
        messages = list(body.get("messages", []))
        if messages and messages[0].get("role") == "system":
            messages[0] = {"role": "system", "content": system_msg}
        else:
            messages.insert(0, {"role": "system", "content": system_msg})
        body["messages"] = messages
        return body

    @staticmethod
    def _prepend_user_prefix(body: dict[str, Any], prefix: str) -> dict[str, Any]:
        """Prepend prefix to the last user message."""
        messages = list(body.get("messages", []))
        for i in reversed(range(len(messages))):
            if messages[i].get("role") == "user":
                existing = messages[i].get("content", "")
                messages[i] = {"role": "user", "content": f"{prefix}\n{existing}"}
                break
        body["messages"] = messages
        return body

    @staticmethod
    def _extract_content(response: Any) -> str:
        if isinstance(response, dict):
            try:
                return response["choices"][0]["message"]["content"] or ""
            except (KeyError, IndexError):
                return ""
        # Object-style (openai SDK)
        try:
            return response.choices[0].message.content or ""
        except AttributeError:
            return str(response)

    @staticmethod
    def _extract_model(response: Any) -> str:
        if isinstance(response, dict):
            return response.get("model", "")
        try:
            return response.model or ""
        except AttributeError:
            return ""
