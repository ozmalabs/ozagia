"""Tests for ozmalib.protocol — wire protocol shim."""

import pytest

from ozmalib.protocol import (
    OzmaShim, CapabilityRegistry, ModelEntry, LEVEL,
    HandshakeManager, SystemPromptBuilder, TurnParser,
    EditOp, InteractVerb, MemoryWrite, MemorySessionStore,
)
from ozmalib.protocol.types import MemScope
from ozmalib.protocol.handshake import _build_probe, _verify_hmac, PROTOCOL_VERSION


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _l1_body(model="gpt-4o"):
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Fix the bug in app.py"},
        ],
    }

def _l3_body():
    return _l1_body(model="ozma-coder-1")


# ---------------------------------------------------------------------------
# CapabilityRegistry
# ---------------------------------------------------------------------------

class TestCapabilityRegistry:
    def test_unknown_model_is_l1(self):
        reg = CapabilityRegistry()
        assert reg.level("gpt-4o") == LEVEL.L1
        assert reg.level("claude-opus-4-6") == LEVEL.L1

    def test_known_l3_model(self):
        reg = CapabilityRegistry()
        assert reg.level("ozma-coder-1") == LEVEL.L3

    def test_known_l2_model(self):
        reg = CapabilityRegistry()
        assert reg.level("qwen3.6-ozma-l2-v1") == LEVEL.L2

    def test_register_custom(self):
        reg = CapabilityRegistry()
        entry = ModelEntry(model_id="my-model", level=LEVEL.L2, caps={"IR": "ab12cd"})
        reg.register(entry)
        assert reg.level("my-model") == LEVEL.L2

    def test_confirmed_caps_full_match(self):
        reg = CapabilityRegistry()
        entry = reg.lookup("ozma-coder-1")
        acked = dict(entry.caps)
        confirmed = entry.confirmed_caps(acked)
        assert "EDIT" in confirmed
        assert "PLAN" in confirmed

    def test_confirmed_caps_fingerprint_mismatch(self):
        reg = CapabilityRegistry()
        entry = reg.lookup("ozma-coder-1")
        acked = dict(entry.caps)
        acked["EDIT"] = "000000"  # wrong fingerprint
        confirmed = entry.confirmed_caps(acked)
        assert "EDIT" not in confirmed
        assert "PLAN" in confirmed


# ---------------------------------------------------------------------------
# HandshakeManager
# ---------------------------------------------------------------------------

class TestHandshakeManager:
    def test_generates_probe(self):
        reg = CapabilityRegistry()
        entry = reg.lookup("ozma-coder-1")
        hm = HandshakeManager()
        probe = hm.generate_probe("sess_1", entry)
        assert "OZMA" in probe
        # ozma-coder-1 is parse_mode=strict → compact probe: [OZMA probe nonce=...]
        assert "nonce=" in probe

    def test_generates_verbose_probe_for_fuzzy(self):
        reg = CapabilityRegistry()
        entry = reg.lookup("anthropic/claude-opus-4-5")
        hm = HandshakeManager()
        probe = hm.generate_probe("sess_1f", entry)
        assert "OZMA" in probe
        # fuzzy-parsed models get the verbose probe with probe: and cap list
        assert "probe:" in probe
        assert "EDIT:" in probe

    def test_no_probe_after_ack(self):
        reg = CapabilityRegistry()
        entry = reg.lookup("ozma-coder-1")
        hm = HandshakeManager()
        hm.generate_probe("sess_1", entry)
        hm.mark_acked("sess_1")
        second_probe = hm.generate_probe("sess_1", entry)
        assert second_probe == ""

    def test_is_first_turn(self):
        hm = HandshakeManager()
        assert hm.is_first_turn("new_sess")
        hm.mark_acked("new_sess")
        assert not hm.is_first_turn("new_sess")

    def test_detect_ack_found(self):
        reg = CapabilityRegistry()
        entry = reg.lookup("ozma-coder-1")
        hm = HandshakeManager()
        hm.generate_probe("sess_x", entry)

        caps_str = " ".join(f"{c}:{f}" for c, f in entry.caps.items())
        ack_line = f"[OZMA v{PROTOCOL_VERSION} ack:abcd1234 {caps_str}]"
        response = f"{ack_line}\n\nHere is the fixed code."

        result = hm.detect_ack("sess_x", response, entry)
        assert result.found
        assert "EDIT" in result.header.caps
        assert "Here is the fixed code." in result.stripped_response
        assert ack_line not in result.stripped_response

    def test_detect_ack_not_found(self):
        reg = CapabilityRegistry()
        entry = reg.lookup("ozma-coder-1")
        hm = HandshakeManager()
        hm.generate_probe("sess_y", entry)

        result = hm.detect_ack("sess_y", "Sure, here's the fix.", entry)
        assert not result.found
        assert result.stripped_response == "Sure, here's the fix."

    def test_detect_ack_skips_preamble(self):
        reg = CapabilityRegistry()
        entry = reg.lookup("ozma-coder-1")
        hm = HandshakeManager()
        hm.generate_probe("sess_z", entry)

        caps_str = " ".join(f"{c}:{f}" for c, f in list(entry.caps.items())[:3])
        ack_line = f"[OZMA v{PROTOCOL_VERSION} ack:deadbeef {caps_str}]"
        response = f"\n\n{ack_line}\n\nContent."
        result = hm.detect_ack("sess_z", response, entry)
        assert result.found

    def test_hmac_verification(self):
        assert _verify_hmac("abcd1234", _compute_hmac("abcd1234", "secret"), "secret")
        assert not _verify_hmac("abcd1234", "00000000", "secret")


def _compute_hmac(nonce: str, secret: str) -> str:
    import hashlib
    return hashlib.sha256(f"{nonce}{secret}".encode()).hexdigest()[:8]


# ---------------------------------------------------------------------------
# SystemPromptBuilder
# ---------------------------------------------------------------------------

class TestSystemPromptBuilder:
    def test_l1_returns_empty(self):
        builder = SystemPromptBuilder()
        entry = ModelEntry(model_id="gpt-4o", level=LEVEL.L1)
        assert builder.build_system(entry) == ""

    def test_l2_contains_ir_marker(self):
        """L2 system block declares the IR marker but does not pre-teach
        language legends — those are injected per-turn for the languages
        actually in use (see TestTeachingProfileTiers)."""
        builder = SystemPromptBuilder()
        entry = ModelEntry(model_id="my-l2", level=LEVEL.L2, caps={"IR": "ab12cd"})
        prompt = builder.build_system(entry)
        assert "Ozma IR" in prompt
        # No language legend baked into the system block.
        assert "Python:" not in prompt
        assert "←=import" not in prompt

    def test_l3_contains_protocol_declaration(self):
        builder = SystemPromptBuilder()
        reg = CapabilityRegistry()
        entry = reg.lookup("ozma-coder-1")
        prompt = builder.build_system(entry)
        assert "Ozma Protocol" in prompt

    def test_probe_appended(self):
        builder = SystemPromptBuilder()
        entry = ModelEntry(model_id="my-l3", level=LEVEL.L3, caps={})
        prompt = builder.build_system(entry, probe_line="[OZMA probe:abc]")
        assert "[OZMA probe:abc]" in prompt

    def test_native_header(self):
        builder = SystemPromptBuilder()
        header = builder.build_native_header("sess_1", "ozma-coder-1")
        assert "workspace:" in header
        assert "tools:" in header
        assert "[ozma v" in header

    def test_user_prefix_legend(self):
        builder = SystemPromptBuilder()
        prefix = builder.build_user_prefix(legend={"a1": "user_service", "a2": "validate"})
        assert "<legend>" in prefix
        assert "a1=user_service" in prefix

    def test_user_prefix_stale(self):
        builder = SystemPromptBuilder()
        prefix = builder.build_user_prefix(stale_paths={"/app/models.py"})
        assert "[stale: /app/models.py]" in prefix


class TestTeachingProfileTiers:
    """Tier-size differentiation and per-turn IR extension."""

    def _build(self, profile, level=LEVEL.L3):
        entry = ModelEntry(
            model_id=f"tier-{profile}",
            level=level,
            caps={"IR": "ab12cd", "EDIT": "b2c1d3", "PLAN": "82bc4d"},
            parse_mode="fuzzy",
            teaching_profile=profile,
        )
        return SystemPromptBuilder().build_system(entry)

    def test_profiles_ordered_by_size(self):
        native = self._build("native")
        compact = self._build("compact")
        standard = self._build("standard")
        full = self._build("full")
        # native is minimum; prompted profiles grow monotonically with teaching
        # verbosity. standard and full share the same system block today
        # (full is held open for future verbose-teaching additions), so they
        # may be equal — the ordering is non-strict between the two.
        assert len(native) < len(compact) < len(standard) <= len(full)

    def test_native_has_markers_only(self):
        prompt = self._build("native")
        assert "Ozma IR" in prompt           # IR marker
        assert "Ozma Protocol" in prompt     # L3 marker
        # Native omits the language legend, memory ops, and cap descriptors.
        assert "←=import" not in prompt
        assert "remember:" not in prompt

    def test_prompted_profiles_omit_language_legend(self):
        """No language legends live in the system block — they are taught
        per-turn by build_user_prefix for the languages actually in use.
        This keeps the system block cacheable and honours the protocol's
        'everything is optional' principle."""
        for profile in ("compact", "standard", "full"):
            prompt = self._build(profile)
            assert "Python:" not in prompt, profile
            assert "JS/TS:" not in prompt, profile
            assert "Lua:" not in prompt, profile
            assert "Haskell:" not in prompt, profile
            assert "Nix:" not in prompt, profile

    def test_per_turn_ir_extension_injects_detected_langs(self):
        """Any detected language — core-5 or outside — gets taught per turn
        under prompted profiles. The system block no longer pre-teaches
        anything, so extension fires for Python too."""
        builder = SystemPromptBuilder()
        entry = ModelEntry(
            model_id="tier-compact",
            level=LEVEL.L3,
            caps={"IR": "ab12cd"},
            parse_mode="fuzzy",
            teaching_profile="compact",
        )
        prefix = builder.build_user_prefix(
            active_files=[("m.py", "Python"), ("x.hs", "Haskell")],
            entry=entry,
        )
        assert "[ir:python]" in prefix
        assert "[ir:haskell]" in prefix

    def test_full_profile_still_extends_per_turn(self):
        """Full profile no longer bakes the full legend into the system —
        per-turn extension fires for whatever is active."""
        builder = SystemPromptBuilder()
        entry = ModelEntry(
            model_id="tier-full",
            level=LEVEL.L3,
            caps={"IR": "ab12cd"},
            parse_mode="fuzzy",
            teaching_profile="full",
        )
        prefix = builder.build_user_prefix(
            active_files=[("x.hs", "Haskell")],
            entry=entry,
        )
        assert "[ir:haskell]" in prefix

    def test_native_profile_no_per_turn_extension(self):
        """Natively-trained models know every IR legend — no extension should fire."""
        builder = SystemPromptBuilder()
        entry = ModelEntry(
            model_id="tier-native",
            level=LEVEL.L3,
            caps={"IR": "ab12cd"},
            parse_mode="strict",
            teaching_profile="native",
        )
        prefix = builder.build_user_prefix(
            active_files=[("m.py", "Python"), ("x.hs", "Haskell"), ("y.nix", "Nix")],
            entry=entry,
        )
        assert "[ir:" not in prefix

    def test_shim_can_override_ir_languages(self):
        """Shim-supplied ir_languages wins over active_files auto-detection.
        An explicit empty list means 'teach no languages this turn' — for
        tasks like raw-binary-IR audits where legends are pure noise."""
        builder = SystemPromptBuilder()
        entry = ModelEntry(
            model_id="tier-compact-override",
            level=LEVEL.L3,
            caps={"IR": "ab12cd"},
            parse_mode="fuzzy",
            teaching_profile="compact",
        )
        # Case A: explicit list overrides active_files.
        prefix = builder.build_user_prefix(
            active_files=[("m.py", "Python")],
            entry=entry,
            ir_languages=["Rust"],
        )
        assert "[ir:rust]" in prefix
        assert "[ir:python]" not in prefix
        # active_files still drives the [file: …] markers.
        assert "[file: m.py lang=Python]" in prefix

        # Case B: empty list explicitly teaches nothing.
        prefix = builder.build_user_prefix(
            active_files=[("audit.ll", "LLVM")],
            entry=entry,
            ir_languages=[],
        )
        assert "[ir:" not in prefix
        assert "[file: audit.ll lang=LLVM]" in prefix

        # Case C: aliases normalise.
        prefix = builder.build_user_prefix(
            entry=entry,
            ir_languages=["py", "ts", "rs"],
        )
        assert "[ir:python]" in prefix
        assert "[ir:js/ts]" in prefix
        assert "[ir:rust]" in prefix

    def test_compact_l3_teaches_memory_syntax_when_cap_declared(self):
        """Compact profile surfaces MEMORY cap name — must also teach the syntax."""
        from ozmalib.protocol.registry import CAP_FINGERPRINTS
        builder = SystemPromptBuilder()
        entry = ModelEntry(
            model_id="compact-with-mem",
            level=LEVEL.L3,
            caps={"IR": CAP_FINGERPRINTS["IR"], "MEMORY": CAP_FINGERPRINTS["MEMORY"]},
            parse_mode="fuzzy",
            teaching_profile="compact",
        )
        prompt = builder.build_system(entry)
        assert "MEMORY" in prompt          # cap name present
        assert "remember:" in prompt       # syntax taught

    def test_compact_l3_skips_memory_syntax_without_cap(self):
        """No MEMORY cap → no memory-ops block (avoid teaching a disabled cap)."""
        builder = SystemPromptBuilder()
        entry = ModelEntry(
            model_id="compact-no-mem",
            level=LEVEL.L3,
            caps={"IR": "ab12cd"},
            parse_mode="fuzzy",
            teaching_profile="compact",
        )
        prompt = builder.build_system(entry)
        assert "remember:" not in prompt

    def test_invalid_profile_rejected_at_registration(self):
        import pytest
        with pytest.raises(ValueError, match="teaching_profile"):
            ModelEntry(
                model_id="typo", level=LEVEL.L3, caps={},
                teaching_profile="compat",  # typo of "compact"
            )

    def test_language_aliases_resolve_to_canonical(self):
        from ozmalib.protocol.system_prompt import normalize_language
        assert normalize_language("Python") == "Python"
        assert normalize_language("python") == "Python"
        assert normalize_language("py") == "Python"
        assert normalize_language("typescript") == "JS/TS"
        assert normalize_language("tsx") == "JS/TS"
        assert normalize_language("rs") == "Rust"
        assert normalize_language("hs") == "Haskell"
        assert normalize_language("postgres") == "SQL"
        assert normalize_language("c++") == "C++"
        assert normalize_language("zsh") == "Bash"
        assert normalize_language("unknown-lang") is None
        assert normalize_language(None) is None
        assert normalize_language("") is None

    def test_per_turn_extension_accepts_lowercase_alias(self):
        builder = SystemPromptBuilder()
        entry = ModelEntry(
            model_id="alias-test", level=LEVEL.L3,
            caps={"IR": "ab12cd"}, parse_mode="fuzzy",
            teaching_profile="compact",
        )
        # Caller passes fence-style lowercase tag; should still inject.
        prefix = builder.build_user_prefix(
            active_files=[("x.hs", "haskell"), ("y.sh", "zsh")],
            entry=entry,
        )
        assert "[ir:haskell]" in prefix
        assert "[ir:bash]" in prefix  # zsh → Bash canonical

    def test_shim_auto_detects_languages_from_fences(self):
        """Per-turn IR extension fires from fenced code blocks when caller
        doesn't pass active_files explicitly. Every detected language gets
        injected — the system block no longer pre-teaches anything, so
        there is no 'core-5 already taught' carve-out."""
        shim = OzmaShim(session_store=MemorySessionStore())
        body = {
            "model": "anthropic/claude-sonnet-4-5",  # compact profile
            "messages": [
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": "Check this code:\n```haskell\nmain = putStrLn \"hi\"\n```\nAnd this:\n```python\nprint('x')\n```"},
            ],
        }
        result = shim.intercept_request(body, session_id="s-fence")
        msgs = result.body["messages"]
        user_msg = next(m for m in msgs if m["role"] == "user")
        assert "[ir:haskell]" in user_msg["content"]
        assert "[ir:python]" in user_msg["content"]

    def test_shim_ir_languages_override_wins(self):
        """Explicit ir_languages passed to intercept_request overrides
        both active_files and fence auto-detection — this is the path
        for shim callers that know more than fence tags reveal."""
        shim = OzmaShim(session_store=MemorySessionStore())
        body = {
            "model": "anthropic/claude-sonnet-4-5",
            "messages": [
                # Python fence would auto-detect; we override to Rust only.
                {"role": "user", "content": "```python\nprint(1)\n```"},
            ],
        }
        result = shim.intercept_request(
            body, session_id="s-override", ir_languages=["Rust"]
        )
        user_msg = next(m for m in result.body["messages"] if m["role"] == "user")
        assert "[ir:rust]" in user_msg["content"]
        assert "[ir:python]" not in user_msg["content"]

    def test_shim_ir_languages_empty_teaches_nothing(self):
        """ir_languages=[] says 'no legends this turn' — the escape hatch
        for tasks like raw-binary IR audits where legends are dead weight."""
        shim = OzmaShim(session_store=MemorySessionStore())
        body = {
            "model": "anthropic/claude-sonnet-4-5",
            "messages": [
                {"role": "user", "content": "```python\nprint(1)\n```"},
            ],
        }
        result = shim.intercept_request(
            body, session_id="s-silent", ir_languages=[]
        )
        user_msg = next(m for m in result.body["messages"] if m["role"] == "user")
        assert "[ir:" not in user_msg["content"]

    def test_shim_caller_active_files_overrides_autodetect(self):
        """If caller passes active_files=[], we treat it as explicit and
        skip autodetection — caller has declared 'no files this turn'."""
        shim = OzmaShim(session_store=MemorySessionStore())
        body = {
            "model": "anthropic/claude-sonnet-4-5",
            "messages": [
                {"role": "user", "content": "```haskell\nfoo\n```"},
            ],
        }
        result = shim.intercept_request(body, session_id="s-explicit", active_files=[])
        msgs = result.body["messages"]
        user_msg = next(m for m in msgs if m["role"] == "user")
        assert "[ir:" not in user_msg["content"]

    def test_compact_cap_names_grouped_by_category(self):
        """Compact L3 cap surface uses the same category grouping as standard
        so compact/standard/full present caps in consistent order."""
        from ozmalib.protocol.registry import CAP_FINGERPRINTS
        builder = SystemPromptBuilder()
        entry = ModelEntry(
            model_id="compact-groups", level=LEVEL.L3,
            caps={
                "IR":   CAP_FINGERPRINTS["IR"],
                "EDIT": CAP_FINGERPRINTS["EDIT"],
                "PLAN": CAP_FINGERPRINTS["PLAN"],
                "MATH": CAP_FINGERPRINTS["MATH"],
            },
            parse_mode="fuzzy",
            teaching_profile="compact",
        )
        prompt = builder.build_system(entry)
        # Category labels are present, alphabetical sort is not.
        assert "Core:" in prompt
        assert "Math/ML:" in prompt
        # EDIT and PLAN are both Core → should appear on the same line
        core_line = next(L for L in prompt.splitlines() if L.startswith("Core:"))
        assert "EDIT" in core_line and "PLAN" in core_line
        math_line = next(L for L in prompt.splitlines() if L.startswith("Math/ML:"))
        assert "MATH" in math_line

    def test_resolve_auto_strict_l3_is_native(self):
        from ozmalib.protocol.registry import resolve_teaching_profile
        entry = ModelEntry(
            model_id="trained-l3", level=LEVEL.L3,
            caps={}, parse_mode="strict", teaching_profile="auto",
        )
        assert resolve_teaching_profile(entry) == "native"

    def test_resolve_auto_fuzzy_l3_is_standard(self):
        from ozmalib.protocol.registry import resolve_teaching_profile
        entry = ModelEntry(
            model_id="prompted-l3", level=LEVEL.L3,
            caps={}, parse_mode="fuzzy", teaching_profile="auto",
        )
        assert resolve_teaching_profile(entry) == "standard"

    def test_resolve_auto_edge_l2_is_native(self):
        from ozmalib.protocol.registry import resolve_teaching_profile
        entry = ModelEntry(
            model_id="edge-l2", level=LEVEL.L2,
            caps={}, parse_mode="strict",
            constrained_decode=True, teaching_profile="auto",
        )
        assert resolve_teaching_profile(entry) == "native"


# ---------------------------------------------------------------------------
# TurnParser
# ---------------------------------------------------------------------------

class TestTurnParser:
    def _parse(self, text: str):
        return TurnParser().parse(text)

    def test_plain_prose(self):
        turn = self._parse("The function looks correct.")
        assert turn.text == "The function looks correct."
        assert not turn.edits

    def test_edit_with_fence(self):
        text = "edit §6\n```python\ndef load(self):\n    return {}\n```"
        turn = self._parse(text)
        assert len(turn.edits) == 1
        e = turn.edits[0]
        assert e.op == EditOp.REPLACE
        assert e.ref == "§6"
        assert "def load" in e.body

    def test_edit_delete(self):
        turn = self._parse("delete §3")
        assert turn.edits[0].op == EditOp.DELETE
        assert turn.edits[0].ref == "§3"

    def test_edit_confidence(self):
        text = "edit §6 confidence=0.7\n```python\npass\n```"
        turn = self._parse(text)
        assert turn.edits[0].confidence == pytest.approx(0.7)

    def test_edit_stable_id(self):
        text = "edit §3f9a1c2d\n```python\npass\n```"
        turn = self._parse(text)
        assert turn.edits[0].ref == "§3f9a1c2d"

    def test_add_before(self):
        text = "add §4:before\n```python\ndef helper(): pass\n```"
        turn = self._parse(text)
        assert turn.edits[0].op == EditOp.ADD_BEFORE

    def test_add_after(self):
        text = "add §4:after\n```python\ndef helper(): pass\n```"
        turn = self._parse(text)
        assert turn.edits[0].op == EditOp.ADD_AFTER

    def test_multiple_edits(self):
        text = (
            "edit §3\n```python\nfoo()\n```\n"
            "edit §7\n```python\nbar()\n```"
        )
        turn = self._parse(text)
        assert len(turn.edits) == 2

    def test_interact_expand(self):
        turn = self._parse("expand §6")
        assert turn.interacts[0].verb == InteractVerb.EXPAND
        assert turn.interacts[0].target == "§6"

    def test_interact_callers(self):
        turn = self._parse("callers §4")
        assert turn.interacts[0].verb == InteractVerb.CALLERS

    def test_interact_recall(self):
        turn = self._parse('recall "database config"')
        assert turn.interacts[0].verb == InteractVerb.RECALL

    def test_plan_block(self):
        text = (
            "plan P1:\n"
            "  p1: fetch arxiv listings\n"
            "  p2: classify papers confidence=0.8\n"
            "  p3: write output\n"
        )
        turn = self._parse(text)
        assert len(turn.plans) == 1
        plan = turn.plans[0]
        assert plan.plan_id == "P1"
        assert len(plan.steps) == 3
        assert plan.steps[1].confidence == pytest.approx(0.8)

    def test_claim_block(self):
        turn = self._parse("claim §6: never raises on empty input confidence=0.9")
        assert len(turn.claims) == 1
        c = turn.claims[0]
        assert c.position == "§6"
        assert "empty input" in c.assertion
        assert c.confidence == pytest.approx(0.9)
        assert not c.is_invariant

    def test_invariant_block(self):
        turn = self._parse("invariant §3: result is always sorted")
        assert turn.claims[0].is_invariant

    def test_reasoning_block(self):
        text = (
            "reasoning:\n"
            "  h1: input could be None → reject confidence=0.9\n"
            "  h2: loop terminates → accept\n"
            "  conclusion: the fix is correct\n"
        )
        turn = self._parse(text)
        assert turn.reasoning is not None
        assert len(turn.reasoning.steps) == 2
        assert turn.reasoning.steps[0].verdict == "reject"
        assert turn.reasoning.conclusion == "the fix is correct"

    def test_memory_write_default(self):
        turn = self._parse("remember: config is loaded lazily")
        assert len(turn.memory_writes) == 1
        mw = turn.memory_writes[0]
        assert mw.scope == MemScope.WORKSPACE
        assert "lazily" in mw.content

    def test_memory_write_user(self):
        turn = self._parse("remember:user: prefers 4-space indent")
        assert turn.memory_writes[0].scope == MemScope.USER

    def test_memory_write_session(self):
        turn = self._parse("remember:session: current task is auth refactor")
        assert turn.memory_writes[0].scope == MemScope.SESSION

    def test_prose_preserved_around_edits(self):
        text = "Here is the fix:\n\nedit §6\n```python\npass\n```\n\nLet me know if you need more."
        turn = self._parse(text)
        assert "Here is the fix" in turn.text
        assert "Let me know" in turn.text
        assert len(turn.edits) == 1

    def test_has_protocol(self):
        turn = self._parse("edit §1\n```python\npass\n```")
        assert turn.has_protocol
        turn2 = self._parse("Just some prose.")
        assert not turn2.has_protocol


# ---------------------------------------------------------------------------
# OzmaShim — full pipeline
# ---------------------------------------------------------------------------

class TestOzmaShim:
    def test_l1_passthrough(self):
        shim = OzmaShim(session_store=MemorySessionStore())
        body = _l1_body("gpt-4o")
        result = shim.intercept_request(body, session_id="s1")
        assert result.model_entry.level == LEVEL.L1
        # L1: no protocol tokens in system message
        msgs = result.body["messages"]
        sys_msg = next((m for m in msgs if m["role"] == "system"), None)
        assert sys_msg is None or "Ozma IR" not in sys_msg.get("content", "")

    def test_l3_system_injection(self):
        shim = OzmaShim(session_store=MemorySessionStore())
        result = shim.intercept_request(_l3_body(), session_id="s2")
        msgs = result.body["messages"]
        sys_msg = next(m for m in msgs if m["role"] == "system")
        assert "Ozma IR" in sys_msg["content"]

    def test_l3_probe_on_first_turn(self):
        shim = OzmaShim(session_store=MemorySessionStore())
        result = shim.intercept_request(_l3_body(), session_id="s3")
        msgs = result.body["messages"]
        sys_msg = next(m for m in msgs if m["role"] == "system")
        # ozma-coder-1 is parse_mode=strict → compact probe with nonce=
        assert "nonce=" in sys_msg["content"]

    def test_l3_no_probe_on_second_turn(self):
        shim = OzmaShim(session_store=MemorySessionStore())
        # First turn
        shim.intercept_request(_l3_body(), session_id="s4")
        shim._handshake.mark_acked("s4")
        # Second turn
        result = shim.intercept_request(_l3_body(), session_id="s4")
        msgs = result.body["messages"]
        sys_msg = next(m for m in msgs if m["role"] == "system")
        assert "nonce=" not in sys_msg["content"]
        assert "probe:" not in sys_msg["content"]

    def test_original_system_preserved(self):
        shim = OzmaShim(session_store=MemorySessionStore())
        body = _l3_body()
        body["messages"][0]["content"] = "You are an expert Python developer."
        result = shim.intercept_request(body, session_id="s5")
        msgs = result.body["messages"]
        sys_msg = next(m for m in msgs if m["role"] == "system")
        assert "expert Python" in sys_msg["content"]
        assert "Ozma IR" in sys_msg["content"]

    def test_legend_prefix_injected(self):
        shim = OzmaShim(session_store=MemorySessionStore())
        result = shim.intercept_request(
            _l3_body(), session_id="s6",
            legend={"a1": "validate_token"},
        )
        user_msg = next(
            m for m in result.body["messages"] if m["role"] == "user"
        )
        assert "<legend>" in user_msg["content"]
        assert "validate_token" in user_msg["content"]

    def test_response_ack_stripped(self):
        shim = OzmaShim(session_store=MemorySessionStore())
        shim.intercept_request(_l3_body(), session_id="s7")
        reg = CapabilityRegistry()
        entry = reg.lookup("ozma-coder-1")
        caps_str = " ".join(f"{c}:{f}" for c, f in list(entry.caps.items())[:3])
        ack = f"[OZMA v{PROTOCOL_VERSION} ack:abcd1234 {caps_str}]"
        raw = {"model": "ozma-coder-1", "choices": [
            {"message": {"content": f"{ack}\n\nHere is the fix.\n\nedit §3\n```python\npass\n```"}}
        ]}
        st = shim.intercept_response(raw, "s7")
        assert st.ack_found
        assert ack not in st.stripped_response
        assert len(st.turn.edits) == 1

    def test_l1_response_plain(self):
        shim = OzmaShim(session_store=MemorySessionStore())
        raw = {"model": "gpt-4o", "choices": [
            {"message": {"content": "The fix is on line 42."}}
        ]}
        st = shim.intercept_response(raw, "plain_sess")
        assert st.turn.text == "The fix is on line 42."
        assert not st.ack_found
        assert st.level == LEVEL.L1

    def test_native_header_mode(self):
        shim = OzmaShim(native_header=True)
        result = shim.intercept_request(_l3_body(), session_id="s8")
        msgs = result.body["messages"]
        sys_msg = next(m for m in msgs if m["role"] == "system")
        assert "[ozma v" in sys_msg["content"]
        assert "workspace:" in sys_msg["content"]

    def test_session_caps_populated_after_ack(self):
        shim = OzmaShim(session_store=MemorySessionStore())
        shim.intercept_request(_l3_body(), session_id="s9")
        reg = CapabilityRegistry()
        entry = reg.lookup("ozma-coder-1")
        caps_str = " ".join(f"{c}:{f}" for c, f in entry.caps.items())
        ack = f"[OZMA v{PROTOCOL_VERSION} ack:abcd1234 {caps_str}]"
        raw = {"model": "ozma-coder-1", "choices": [
            {"message": {"content": f"{ack}\n\nDone."}}
        ]}
        shim.intercept_response(raw, "s9")
        caps = shim.session_caps("s9")
        assert "EDIT" in caps
        assert "PLAN" in caps


# =============================================================================
# New parser types (Phases 2–4)
# =============================================================================

from ozmalib.protocol import (
    TestSpec, EffectAnnotation, DepEdge, SchemaDecl,
    OutlineBlock, HintBlock, DocStub,
    SubPrompt, HandoffPacket, AgentStateBlock, MotionBlock,
    MathBlock, TensorShape, GraphBlock, GraphEdge,
    TreeBlock, StructuredConf, DPBudget, GrammarBlock,
    AttnAnnotation, EmbedBlock, CollabBlock,
    FederatedRoute, PolyglotBlock, LearnedBPEBlock,
    MemorySessionStore, StreamingShim,
)
from ozmalib.protocol.verify import VerifierResult, verify_formal_claims


_parser = TurnParser()


class TestTestSpec:
    def test_basic(self):
        t = _parser.parse("test-spec §3 [python]:\n```python\nassert f(1)==2\n```\n")
        assert len(t.test_specs) == 1
        assert t.test_specs[0].ref == "§3"
        assert t.test_specs[0].lang == "python"
        assert "assert f(1)==2" in t.test_specs[0].body

    def test_default_lang(self):
        t = _parser.parse("test-spec §5:\n```\nbody\n```\n")
        assert t.test_specs[0].lang == "text"


class TestEffectAnnotation:
    def test_single_effect(self):
        t = _parser.parse("effects §2: io=file\n")
        assert len(t.effects) == 1
        assert t.effects[0].ref == "§2"
        assert t.effects[0].effects["io"] == ["file"]

    def test_multiple_effects(self):
        t = _parser.parse("effects §7: io=file,network db=read,write\n")
        e = t.effects[0]
        assert e.effects["io"] == ["file", "network"]
        assert e.effects["db"] == ["read", "write"]


class TestDepEdge:
    def test_arrow_unicode(self):
        t = _parser.parse("dep §3 → §7\n")
        assert len(t.dep_edges) == 1
        assert t.dep_edges[0].from_ref == "§3"
        assert t.dep_edges[0].to_ref == "§7"

    def test_arrow_ascii(self):
        t = _parser.parse("dep §1 -> §2 [transitive]\n")
        assert t.dep_edges[0].qualifier == "transitive"

    def test_no_qualifier(self):
        t = _parser.parse("dep §1 → §4\n")
        assert t.dep_edges[0].qualifier == ""


class TestSchemaDecl:
    def test_json_schema(self):
        t = _parser.parse('schema §4 [json-schema]:\n```json\n{"type":"object"}\n```\n')
        assert len(t.schemas) == 1
        assert t.schemas[0].lang == "json-schema"
        assert '{"type":"object"}' in t.schemas[0].body

    def test_default_lang(self):
        t = _parser.parse("schema §2:\n```\n{}\n```\n")
        assert t.schemas[0].lang == "json-schema"


class TestOutlineBlock:
    def test_basic(self):
        text = "outline:\n  §1: AuthRouter — FastAPI router\n  §3: get_user — retrieves user\n"
        t = _parser.parse(text)
        assert t.outline is not None
        assert len(t.outline.items) == 2
        assert t.outline.items[0].ref == "§1"
        assert "AuthRouter" in t.outline.items[0].description

    def test_empty_outline(self):
        t = _parser.parse("outline:\n")
        assert t.outline is not None
        assert len(t.outline.items) == 0


class TestHintBlock:
    def test_basic(self):
        t = _parser.parse("hint [perf]: Use a compiled regex here.\n")
        assert len(t.hints) == 1
        assert t.hints[0].tag == "perf"
        assert "compiled regex" in t.hints[0].content


class TestDocStub:
    def test_basic(self):
        t = _parser.parse('docstub §5:\n```\n"""Short doc."""\n```\n')
        assert len(t.docstubs) == 1
        assert t.docstubs[0].ref == "§5"
        assert "Short doc" in t.docstubs[0].body


class TestWireFlag:
    def test_wire_sets_flag(self):
        t = _parser.parse("wire:\nsome prose\n")
        assert t.wire_encoded is True

    def test_no_wire(self):
        t = _parser.parse("some prose\n")
        assert t.wire_encoded is False


class TestSubPrompt:
    def test_basic(self):
        t = _parser.parse("prompt [search-refactor]:\n```\nFind all usages.\n```\n")
        assert len(t.sub_prompts) == 1
        assert t.sub_prompts[0].label == "search-refactor"
        assert "Find all usages" in t.sub_prompts[0].body


class TestHandoffPacket:
    def test_basic(self):
        t = _parser.parse('handoff [ozma-coder-1]:\n```json\n{"task":"port"}\n```\n')
        assert len(t.handoffs) == 1
        assert t.handoffs[0].target_model == "ozma-coder-1"
        assert '"task"' in t.handoffs[0].body


class TestAgentStateBlock:
    def test_basic(self):
        t = _parser.parse('agent-state [workspace]:\n```json\n{"step":3}\n```\n')
        assert len(t.agent_states) == 1
        assert t.agent_states[0].scope == "workspace"
        assert '"step"' in t.agent_states[0].body


class TestMotionBlock:
    def test_desktop_device_profile(self):
        t = _parser.parse("motion [desktop]:\n  click §SOM[3]\n  type \"hello\"\n")
        assert len(t.motion_blocks) == 1
        assert t.motion_blocks[0].device == "desktop"
        assert "click §SOM[3]" in t.motion_blocks[0].raw_actions
        assert 'type "hello"' in t.motion_blocks[0].raw_actions

    def test_device_parameter_captured(self):
        t = _parser.parse("motion [ur5-left]:\n  action: home\n")
        assert len(t.motion_blocks) == 1
        assert t.motion_blocks[0].device == "ur5-left"
        assert "action: home" in t.motion_blocks[0].raw_actions

    def test_emergency_stop_reserved_device(self):
        t = _parser.parse("motion [emergency-stop]:\n")
        assert len(t.motion_blocks) == 1
        assert t.motion_blocks[0].device == "emergency-stop"

    def test_browser_device_with_css_ref(self):
        t = _parser.parse(
            "motion [browser]:\n"
            '  navigate "https://example.com"\n'
            '  click §CSS("button.primary")\n'
        )
        assert len(t.motion_blocks) == 1
        assert t.motion_blocks[0].device == "browser"
        assert 'click §CSS("button.primary")' in t.motion_blocks[0].raw_actions

    def test_mobile_device_with_aid_ref(self):
        t = _parser.parse(
            "motion [mobile]:\n"
            '  tap §AID("login_button")\n'
        )
        assert len(t.motion_blocks) == 1
        assert t.motion_blocks[0].device == "mobile"
        assert 'tap §AID("login_button")' in t.motion_blocks[0].raw_actions


class TestMathBlock:
    def test_basic(self):
        t = _parser.parse("math [thm-1]:\n```latex\n$$x^2$$\n```\n")
        assert len(t.math_blocks) == 1
        assert t.math_blocks[0].label == "thm-1"
        assert "$$x^2$$" in t.math_blocks[0].body


class TestTensorShape:
    def test_named_dims(self):
        t = _parser.parse("tensor §3: [B=32, T=512, D=768]\n")
        assert len(t.tensor_shapes) == 1
        ts = t.tensor_shapes[0]
        assert ts.ref == "§3"
        assert ts.dims == {"B": 32, "T": 512, "D": 768}
        assert ts.dtype is None

    def test_with_dtype(self):
        t = _parser.parse("tensor §1: [D=768] dtype=float16\n")
        assert t.tensor_shapes[0].dtype == "float16"


class TestGraphBlock:
    def test_basic(self):
        text = "graph [call-graph]:\n  §1 → §3 [calls]\n  §3 → §5\n"
        t = _parser.parse(text)
        assert len(t.graphs) == 1
        g = t.graphs[0]
        assert g.label == "call-graph"
        assert len(g.edges) == 2
        assert g.edges[0].from_ref == "§1"
        assert g.edges[0].to_ref == "§3"
        assert g.edges[0].label == "calls"
        assert g.edges[1].label == ""


class TestTreeBlock:
    def test_basic(self):
        text = "tree [modules]:\n  §1 [root]\n    §2 [child-a]\n    §3 [child-b]\n"
        t = _parser.parse(text)
        assert len(t.trees) == 1
        tb = t.trees[0]
        assert tb.label == "modules"
        assert tb.root is not None
        assert tb.root.ref == "§1"
        assert len(tb.root.children) == 2


class TestStructuredConf:
    def test_basic(self):
        text = "structured-conf §2:\n  syntax: 0.95\n  semantics: 0.80\n"
        t = _parser.parse(text)
        assert len(t.structured_confs) == 1
        sc = t.structured_confs[0]
        assert sc.breakdown["syntax"] == pytest.approx(0.95)
        assert sc.breakdown["semantics"] == pytest.approx(0.80)


class TestDPBudget:
    def test_basic(self):
        t = _parser.parse("dp budget=ε=0.1,δ=1e-5 mechanism=gaussian\n")
        assert len(t.dp_budgets) == 1
        dp = t.dp_budgets[0]
        assert dp.epsilon == pytest.approx(0.1)
        assert dp.delta == pytest.approx(1e-5)
        assert dp.mechanism == "gaussian"

    def test_default_mechanism(self):
        t = _parser.parse("dp budget=ε=0.5,δ=0.01\n")
        assert t.dp_budgets[0].mechanism == "gaussian"


class TestGrammarBlock:
    def test_basic(self):
        t = _parser.parse("grammar [expr]:\n```ebnf\nexpr = term\n```\n")
        assert len(t.grammars) == 1
        assert t.grammars[0].name == "expr"
        assert "expr = term" in t.grammars[0].body


class TestAttnAnnotation:
    def test_basic(self):
        t = _parser.parse("attn §5: §3=0.45, §7=0.30\n")
        assert len(t.attn_annotations) == 1
        a = t.attn_annotations[0]
        assert a.ref == "§5"
        assert a.weights["§3"] == pytest.approx(0.45)
        assert a.weights["§7"] == pytest.approx(0.30)


class TestEmbedBlock:
    def test_basic(self):
        t = _parser.parse("embed §2: [0.1, -0.5, 0.9]\n")
        assert len(t.embeds) == 1
        e = t.embeds[0]
        assert e.ref == "§2"
        assert e.values == pytest.approx([0.1, -0.5, 0.9])
        assert e.dims == 3


class TestCollabBlock:
    def test_basic(self):
        t = _parser.parse("collab [ozma-coder-1 | P1.p2]:\n")
        assert len(t.collab_blocks) == 1
        c = t.collab_blocks[0]
        assert c.author == "ozma-coder-1"
        assert c.attribution == "P1.p2"

    def test_no_attribution(self):
        t = _parser.parse("collab [alice]:\n")
        assert t.collab_blocks[0].author == "alice"
        assert t.collab_blocks[0].attribution == ""


class TestFederatedRoute:
    def test_basic(self):
        t = _parser.parse('federated [target=ozma-edge-1 region=eu]:\n```json\n{"route":"x"}\n```\n')
        assert len(t.federated_routes) == 1
        f = t.federated_routes[0]
        assert f.target == "ozma-edge-1"
        assert f.region == "eu"
        assert '"route"' in f.body


class TestPolyglotBlock:
    def test_two_langs(self):
        text = (
            "polyglot [python,rust]:\n"
            "```python\ndef foo(): pass\n```\n"
            "```rust\nfn foo() {}\n```\n"
        )
        t = _parser.parse(text)
        assert len(t.polyglot_blocks) == 1
        p = t.polyglot_blocks[0]
        assert p.langs == ["python", "rust"]
        assert "def foo" in p.bodies.get("python", "")
        assert "fn foo" in p.bodies.get("rust", "")


class TestLearnedBPEBlock:
    def test_basic(self):
        t = _parser.parse('learned-bpe:\n```json\n{"vocab":{}}\n```\n')
        assert t.learned_bpe is not None
        assert '{"vocab":{}}' in t.learned_bpe.body


class TestHasProtocol:
    def test_new_types_trigger_has_protocol(self):
        t = _parser.parse("test-spec §1:\n```\nbody\n```\n")
        assert t.has_protocol

    def test_outline_triggers_has_protocol(self):
        t = _parser.parse("outline:\n  §1: foo\n")
        assert t.has_protocol

    def test_empty_does_not_trigger(self):
        t = _parser.parse("just prose\n")
        assert not t.has_protocol


# =============================================================================
# Streaming
# =============================================================================

class TestStreamingShim:
    def _shim(self):
        return OzmaShim(session_store=MemorySessionStore())

    def test_feed_delta_accumulates(self):
        shim = self._shim()
        ss = shim.stream("s_stream")
        state = ss.begin("s_stream")
        ss.feed_delta(state, "edit §3\n")
        ss.feed_delta(state, "```python\n")
        ss.feed_delta(state, "pass\n")
        turn = ss.end_stream(state)
        assert len(turn.edits) == 1
        assert turn.edits[0].ref == "§3"

    def test_partial_line_held(self):
        shim = self._shim()
        ss = shim.stream("s_partial")
        state = ss.begin("s_partial")
        # Send half a line
        t1 = ss.feed_delta(state, "edit")
        # No complete line yet — no edits parsed
        assert len(t1.edits) == 0
        # Complete the line
        ss.feed_delta(state, " §2\n```python\npass\n```\n")
        turn = ss.end_stream(state)
        assert len(turn.edits) == 1

    def test_end_stream_flushes_pending(self):
        shim = self._shim()
        ss = shim.stream("s_flush")
        state = ss.begin("s_flush")
        ss.feed_delta(state, "Some prose without newline")
        turn = ss.end_stream(state)
        assert "Some prose without newline" in turn.text

    def test_same_result_as_batch_parse(self):
        text = "outline:\n  §1: foo\n  §2: bar\nhint [perf]: use caching\n"
        shim = self._shim()
        ss = shim.stream("s_batch")
        state = ss.begin("s_batch")
        # Feed char by char
        for ch in text:
            ss.feed_delta(state, ch)
        stream_turn = ss.end_stream(state)

        batch_turn = TurnParser().parse(text)
        assert len(stream_turn.hints) == len(batch_turn.hints)
        assert len(stream_turn.outline.items) == len(batch_turn.outline.items)


# =============================================================================
# Verifier dispatch
# =============================================================================

class TestVerifyFormalClaims:
    def _entry(self, registry=None):
        return ModelEntry(
            model_id="ozma-coder-1",
            level=LEVEL.L3,
            caps={},
            verifier_registry={"smt2": "z3"},
        )

    def test_no_formal_claims_returns_empty(self):
        turn = TurnParser().parse("claim §1: foo is correct\n")
        results = verify_formal_claims(turn, self._entry())
        assert results == []

    def test_unregistered_lang_skipped(self):
        turn = TurnParser().parse(
            "formal-claim §1 [dafny]:\n```\nmethod M() {}\n```\n"
        )
        entry = ModelEntry(model_id="m", level=LEVEL.L3, caps={}, verifier_registry={})
        results = verify_formal_claims(turn, entry)
        assert len(results) == 1
        assert results[0].status == "skipped"

    def test_z3_unavailable_returns_skipped(self, monkeypatch):
        import importlib.util
        monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
        turn = TurnParser().parse(
            "formal-claim §2 [smt2]:\n```\n(assert (= 1 1))\n```\n"
        )
        results = verify_formal_claims(turn, self._entry())
        assert len(results) == 1
        assert results[0].status == "skipped"

    def test_z3_sat(self):
        pytest.importorskip("z3")
        turn = TurnParser().parse(
            "formal-claim §3 [smt2]:\n```\n(assert (= 1 1))\n(check-sat)\n```\n"
        )
        results = verify_formal_claims(turn, self._entry())
        assert results[0].status == "verified"

    def test_subprocess_binary_missing(self, monkeypatch):
        import shutil
        monkeypatch.setattr(shutil, "which", lambda cmd: None)
        turn = TurnParser().parse(
            "formal-claim §4 [dafny]:\n```\nmethod M() ensures true {}\n```\n"
        )
        entry = ModelEntry(
            model_id="m", level=LEVEL.L3, caps={},
            verifier_registry={"dafny": "dafny"},
        )
        results = verify_formal_claims(turn, entry)
        assert results[0].status == "skipped"


# =============================================================================
# constrained_decode injection
# =============================================================================

class TestConstrainedDecode:
    def _entry_cd(self, backend="openai"):
        return ModelEntry(
            model_id="ozma-coder-1",
            level=LEVEL.L3,
            caps={},
            constrained_decode=True,
            extra={"backend": backend},
        )

    def _body(self):
        return {
            "model": "ozma-coder-1",
            "messages": [{"role": "user", "content": "hi"}],
        }

    def test_openai_backend_injects_response_format(self):
        shim = OzmaShim(
            registry=_make_registry(self._entry_cd("openai")),
            session_store=MemorySessionStore(),
        )
        result = shim.intercept_request(self._body(), session_id="cd1")
        assert "response_format" in result.body

    def test_llamacpp_backend_injects_grammar(self):
        shim = OzmaShim(
            registry=_make_registry(self._entry_cd("llamacpp")),
            session_store=MemorySessionStore(),
        )
        result = shim.intercept_request(self._body(), session_id="cd2")
        assert "grammar" in result.body

    def test_vllm_backend_injects_guided_json(self):
        shim = OzmaShim(
            registry=_make_registry(self._entry_cd("vllm")),
            session_store=MemorySessionStore(),
        )
        result = shim.intercept_request(self._body(), session_id="cd3")
        assert "guided_json" in result.body

    def test_no_constrained_decode_no_injection(self):
        entry = ModelEntry(model_id="ozma-coder-1", level=LEVEL.L3, caps={})
        shim = OzmaShim(
            registry=_make_registry(entry),
            session_store=MemorySessionStore(),
        )
        result = shim.intercept_request(self._body(), session_id="cd4")
        assert "response_format" not in result.body
        assert "grammar" not in result.body


def _make_registry(entry: ModelEntry) -> CapabilityRegistry:
    reg = CapabilityRegistry()
    reg.register(entry)
    return reg


# =============================================================================
# Store wired through OzmaShim
# =============================================================================

class TestShimWithSQLiteStore:
    def test_session_caps_persisted(self, tmp_path):
        from ozmalib.protocol import SQLiteSessionStore
        from ozmalib.protocol.handshake import PROTOCOL_VERSION

        db = str(tmp_path / "shim.db")
        store = SQLiteSessionStore(db)
        shim = OzmaShim(session_store=store)

        body = {"model": "ozma-coder-1", "messages": [{"role": "user", "content": "hi"}]}
        shim.intercept_request(body, session_id="persist1")

        reg = CapabilityRegistry()
        entry = reg.lookup("ozma-coder-1")
        caps_str = " ".join(f"{c}:{f}" for c, f in list(entry.caps.items())[:2])
        ack = f"[OZMA v{PROTOCOL_VERSION} ack:abcd1234 {caps_str}]"
        raw = {"model": "ozma-coder-1", "choices": [
            {"message": {"content": f"{ack}\nSome response"}}
        ]}
        shim.intercept_response(raw, "persist1")

        # New shim with same store — caps should survive
        shim2 = OzmaShim(session_store=SQLiteSessionStore(db))
        caps = shim2.session_caps("persist1")
        assert len(caps) >= 1


# =============================================================================
# Fuzzy parsing (prompted model near-misses)
# =============================================================================

class TestFuzzyParsing:
    """Validates fuzzy mode fixes derived from live qwen3.6 probe runs."""

    _parser = TurnParser()

    def _fuzzy(self, text: str) -> Turn:
        return self._parser.parse(text, strict=False)

    def test_outline_flush_left_items(self):
        """Model writes §N: items at column 0 (no indent)."""
        raw = "outline:\n§1: Model — manages data\n§2: View — renders UI\n§3: Controller — routes\n"
        t = self._fuzzy(raw)
        assert t.outline is not None
        assert len(t.outline.items) == 3

    def test_outline_trailing_slash_header(self):
        """Model writes 'outline: /' with trailing decorative slash."""
        raw = "outline: /\n§1: Model — manages data\n§2: View — renders UI\n"
        t = self._fuzzy(raw)
        assert t.outline is not None
        assert len(t.outline.items) == 2

    def test_outline_space_separator(self):
        """Model writes '§N description' (space, not colon) — fuzzy only."""
        raw = "outline:\n  §1 Model description here\n  §2 View description\n"
        t = self._fuzzy(raw)
        assert t.outline is not None
        assert len(t.outline.items) == 2

    def test_grammar_no_brackets(self):
        """Model writes 'grammar name:' without square brackets — fuzzy only."""
        raw = "grammar arithmetic_expr:\n```ebnf\nexpr ::= term\n```\n"
        t = self._fuzzy(raw)
        assert len(t.grammars) == 1
        assert t.grammars[0].name == "arithmetic_expr"

    def test_effects_all_comma_format(self):
        """Model writes 'key=val,key=val' all-comma (no spaces) — fuzzy."""
        raw = "effects §1: file=write,db=write\n"
        t = self._fuzzy(raw)
        assert len(t.effects) == 1
        eff = t.effects[0].effects
        assert eff.get("file") == ["write"]
        assert eff.get("db") == ["write"]

    def test_reasoning_flush_left_steps(self):
        """Model writes reasoning steps flush-left (no indent) — fuzzy."""
        raw = (
            "reasoning:\n"
            "h1: Python rebinds caller var → reject\n"
            "h2: Python passes by object reference → accept\n"
            "conclusion: pass-by-object-reference\n"
        )
        t = self._fuzzy(raw)
        assert t.reasoning is not None
        assert len(t.reasoning.steps) == 2

    def test_dp_formal_prefix(self):
        """Model writes 'Formal: dp budget=...' with label prefix — fuzzy."""
        raw = "Formal: dp budget=ε=0.1,δ=1e-5 mechanism=gaussian\n"
        t = self._fuzzy(raw)
        assert len(t.dp_budgets) == 1
        assert abs(t.dp_budgets[0].epsilon - 0.1) < 1e-9

    def test_strict_rejects_flush_left_reasoning(self):
        """Strict mode should NOT accept flush-left reasoning steps."""
        raw = (
            "reasoning:\n"
            "h1: premise → accept\n"
            "conclusion: yes\n"
        )
        t = TurnParser().parse(raw, strict=True)
        assert t.reasoning is None or len(t.reasoning.steps) == 0

    def test_strict_rejects_grammar_no_brackets(self):
        """Strict mode should NOT parse 'grammar name:' without brackets."""
        raw = "grammar arithmetic:\n```\nexpr ::= term\n```\n"
        t = TurnParser().parse(raw, strict=True)
        assert len(t.grammars) == 0

    def test_fenced_protocol_keyword_fuzzy(self):
        """Model wraps protocol keyword in ``` fence — fuzzy only."""
        raw = "```structured-conf §3:\n  syntax: 0.95\n  semantics: 0.80\n```\n"
        t = self._fuzzy(raw)
        assert len(t.structured_confs) == 1
        assert t.structured_confs[0].breakdown["syntax"] == 0.95

    def test_fenced_protocol_keyword_strict_rejected(self):
        """Strict mode must NOT parse fenced protocol keywords."""
        raw = "```claim §3: this function is O(n log n)\n```\n"
        t = TurnParser().parse(raw, strict=True)
        assert len(t.claims) == 0
