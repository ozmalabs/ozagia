"""
system_prompt.py — System prompt construction by protocol level + teaching profile.

The prompt size scales along three axes:

  * **Level** — L0/L1 inject nothing; L2 adds IR; L3 adds the cap legend.
  * **Teaching profile** — how much instruction the specific model needs.
    native    → trained model; near-empty preamble (~60 tok)
    compact   → high-compliance prompted; header + core-5 IR + cap names (~150-400 tok)
    standard  → typical prompted; header + core-5 IR + cap descriptors + memory (~600-900 tok)
    full      → low-compliance prompted; header + all 15 IR + descriptors + examples (~1500+ tok)
  * **Active languages** — per-turn. Languages outside the profile's base
    set get their IR line appended to the *user-role prefix* (not system)
    so the system stays cacheable across turns.

IR maps are loaded from ``ir_maps/`` YAML files and rendered using a
presentation mode. The same map can be presented four ways:

  alphabet  — "X=Y X=Y X=Y" (most compact, leaks on low-compliance models)
  rewrite  — "Write `ƒ` instead of `def`. Write `→` instead of `return`."
  directive — "Apply: def→ƒ, return→→, import→←. Everything else is normal Python."
  example  — "Example: `ƒ foo():` not `def foo():`"

The system block within a profile is constant across turns so upstream
prompt caches hit every turn after the first. Per-turn state (language
extensions, legend aliases, active files) goes into the user prefix.
"""

from __future__ import annotations

from .registry import LEVEL, ModelEntry, resolve_teaching_profile

# ---------------------------------------------------------------------------
# IR map loading — YAML data files
# ---------------------------------------------------------------------------

from pathlib import Path
import re

_IR_MAPS_DIR = Path(__file__).parent / "ir_maps"

# In-memory cache: path → loaded dict (avoids re-parsing YAML every build)
_loaded_maps: dict[str, dict] = {}


def load_ir_map(lang: str) -> dict:
    """Load a language IR map from ``ir_maps/{lang}.yaml``."""
    if lang in _loaded_maps:
        return _loaded_maps[lang]
    path = _IR_MAPS_DIR / f"{lang.lower().replace('/', '_')}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"No IR map for {lang!r} at {path}")
    import yaml  # cheap import; only used at prompt-build time
    with open(path) as f:
        data = yaml.safe_load(f)
    _loaded_maps[lang] = data
    return data


def _make_alphabet(subs: list[list[str]]) -> str:
    """Alphabet style: `def=ƒ return=→ import=←` on one line."""
    return " ".join(f"{f}={t}" for f, t, *_ in subs)


def _make_rewrite(subs: list[list[str]]) -> str:
    r"""Rewrite-rules style: Write `ƒ` instead of `def`. per line."""
    lines = []
    for f, t, *_ in subs:
        lines.append(f"Write `{t}` instead of `{f}`.")
    return " ".join(lines)


def _make_directive(subs: list[list[str]]) -> str:
    """Directive style: `Apply: def→ƒ, return→→, import←. Everything else normal.`"""
    arrows = ", ".join(f"{f}→{t}" for f, t, *_ in subs)
    return (
        f"Apply these substitutions when writing code: {arrows}. "
        "All other keywords are normal Python."
    )


def _make_example(subs: list[list[str]]) -> str:
    """Example style: shows the rewrite on a minimal snippet."""
    # Show just the first (highest-impact) substitution as an example
    if not subs:
        return ""
    f, t = subs[0][0], subs[0][1]
    return f"Example: `{t} foo():` not `{f} foo():`"


# Public presentation-mode dispatcher
IR_PRESENTATIONS = {"alphabet", "rewrite", "directive", "example"}


def render_ir_map(lang: str, presentation: str = "alphabet") -> str:
    """Render the IR map for ``lang`` using ``presentation``.

    Args:
        lang:         canonical language name (e.g. "python", "go", "rust")
        presentation: one of "alphabet", "rewrite", "directive", "example"

    Returns:
        A single string suitable for injection into a system or user prompt.
    """
    data = load_ir_map(lang)
    subs = data.get("substitutions", {})
    # subs is a dict; convert to sorted list for deterministic ordering
    items = sorted(subs.items(), key=lambda x: x[0])
    # Normalise to list-of-lists for _make_* helpers
    flat = [[f, t] for f, t in items]

    if presentation == "alphabet":
        return _make_alphabet(flat)
    elif presentation == "rewrite":
        return _make_rewrite(flat)
    elif presentation == "directive":
        return _make_directive(flat)
    elif presentation == "example":
        return _make_example(flat)
    else:
        raise ValueError(
            f"Unknown presentation {presentation!r}; "
            f"valid: {', '.join(sorted(IR_PRESENTATIONS))}"
        )


# ---------------------------------------------------------------------------
# Atomic building blocks
# ---------------------------------------------------------------------------

_HEADER = (
    "You are a code assistant. Minimise output tokens — your responses are "
    "post-processed and expanded before the client sees them. Two mandatory "
    "compressions:\n"
    "1. IR symbols: replace keywords with symbols below when writing code. "
    "Never write raw keywords in code.\n"
    "2. Diff output: when editing existing code, emit only changed "
    "functions/sections in a code fence. Write `...` for unchanged bodies. "
    "Never repeat unmodified code.\n"
    "No prose unless explicitly asked. Code and brief answers only."
)

_IR_INTRO = (
    "Ozma IR: write `ƒ foo():` not `def foo():`, `→ x` not `return x`, "
    "`←os` not `import os`."
)

# Supported IR languages — keys must match canonical _LANG_ALIASES values
# and filenames in ir_maps/.  Backed by YAML data files.
def _supported_ir_langs() -> frozenset[str]:
    """Return the set of languages that have an ir_maps/{lang}.yaml file."""
    if not hasattr(_supported_ir_langs, "_cache"):
        try:
            files = {p.stem for p in _IR_MAPS_DIR.iterdir() if p.suffix == ".yaml"}
        except FileNotFoundError:
            files = set()
        _supported_ir_langs._cache = frozenset(files)
    return _supported_ir_langs._cache


def _ir_legend(lang: str) -> str:
    """Return the alphabet-style IR legend line for ``lang`` (YAML-backed).

    Returns an empty string if no map file exists for the language.
    """
    try:
        return render_ir_map(lang, "alphabet")
    except FileNotFoundError:
        return ""

# Core five cover ~90% of observed traffic.
# Languages without an ir_maps/ YAML file are skipped in legend builds
# to avoid hard failures — the builder logs a warning but continues.
_CORE_LANGS: tuple[str, ...] = ("Python", "JS/TS", "Go", "Rust", "SQL")

# Maps the many forms a language name arrives in (file extensions, fence
# tags, lowercase variants, common aliases) to the canonical key in
# `_IR_LANGS`. Callers pass any form; `normalize_language` resolves it.
_LANG_ALIASES: dict[str, str] = {
    # Python
    "python": "Python", "py": "Python", "python3": "Python",
    # JS/TS (shared legend)
    "javascript": "JS/TS", "js": "JS/TS", "jsx": "JS/TS", "mjs": "JS/TS",
    "cjs": "JS/TS", "typescript": "JS/TS", "ts": "JS/TS", "tsx": "JS/TS",
    # Go
    "go": "Go", "golang": "Go",
    # Rust
    "rust": "Rust", "rs": "Rust",
    # SQL
    "sql": "SQL", "mysql": "SQL", "postgres": "SQL", "postgresql": "SQL",
    "sqlite": "SQL", "plpgsql": "SQL",
    # C / C++
    "c": "C",
    "cpp": "C++", "c++": "C++", "cxx": "C++", "cc": "C++", "hpp": "C++",
    # Others
    "lua": "Lua",
    "scala": "Scala",
    "elixir": "Elixir", "ex": "Elixir", "exs": "Elixir",
    "dart": "Dart",
    "haskell": "Haskell", "hs": "Haskell",
    "bash": "Bash", "sh": "Bash", "shell": "Bash", "zsh": "Bash",
    "ocaml": "OCaml", "ml": "OCaml",
    "nix": "Nix",
}

def normalize_language(name: str | None) -> str | None:
    """Return the canonical language key for ``name``, or None if unknown.

    Accepts any common form: capitalized canonical (`"Python"`), lowercase
    (`"python"`), fence tags (`"py"`, `"ts"`), extensions (`"rs"`, `"hs"`).
    """
    if not name:
        return None
    if name in _supported_ir_langs():
        return name
    return _LANG_ALIASES.get(name.lower().strip("."))

_MEMORY_OPS = (
    "To store a fact: `remember: <fact>` | `remember:workspace §N: <fact>` "
    "| `remember:user: <fact>` | `remember:session: <fact>`\n"
    "To retrieve: `recall \"<query>\"` (terminal — response arrives next turn). "
    "To check persona: `persona?`"
)

# Precomputed legend preset bodies (alphabet presentation).
# Missing maps produce empty strings and are filtered out.
_CORE_LEGEND = "\n".join(filter(None, (_ir_legend(L) for L in _CORE_LANGS)))
_FULL_LEGEND = "\n".join(filter(None, (_ir_legend(L) for L in _supported_ir_langs())))


# ---------------------------------------------------------------------------
# L3 cap descriptors — grouped
# ---------------------------------------------------------------------------

_CAP_DESCRIPTORS: dict[str, str] = {
    # Core structured types
    "EDIT":      "EDIT §REF (fenced code body)",
    "INTERACT":  "INTERACT: expand §REF | callers §REF | outline §REF | search <query>",
    "PLAN":      "PLAN P1:\\n  p1: task [complexity=trivial|simple|moderate|complex|expert category=K file=F.py] | p2: task [depends_on=p1]",
    "CLAIM":     "claim §REF: assertion [confidence=0.N]",
    "REASONING": "reasoning:\\n  h1: hypothesis → accept|reject\\n  conclusion: …",
    "MEMORY":    "remember: fact | remember:workspace §N: fact | remember:user: fact",
    "HANDOFF":   "handoff [model]:\\n```json\\n{…}\\n```",
    # Code intelligence
    "TEST-SPEC": "test-spec §REF [lang]:\\n```\\n…\\n```",
    "EFFECTS":   "effects §REF: key=val key2=v1,v2",
    "DEP":       "dep §FROM → §TO [qualifier]",
    "SCHEMA":    "schema §REF [lang]:\\n```\\n…\\n```",
    "DOCSTUB":   "docstub §REF:\\n```\\n…\\n```",
    "HINT":      "hint [tag]: content",
    "OUTLINE":   "outline:\\n§REF: description",
    "WIRE":      "wire: (marks following prose as WIRE-encoded)",
    # Agent / orchestration
    "PROMPT":       "prompt [label]:\\n```\\n…\\n```",
    "AGENT-STATE":  "agent-state [scope]:\\n```\\n…\\n```",
    # Control (MOTION covers robots, avatars, vehicles, drones, and digital
    # UI substrates like desktop/browser/mobile via device profiles)
    "MOTION":       'motion [X]:\\n  action: A  params: {…}   (physical — YAML-ish body)\\n'
                    'motion [desktop]:\\n  click §SOM[N] | type §SOM[N] "text" | key "combo"   (UI — verb-line body)\\n'
                    'motion [emergency-stop]:   (reserved — halts all devices)',
    # Math / ML
    "MATH":           "math [label]:\\n```latex\\n…\\n```",
    "TENSOR-SHAPE":   "tensor §REF: [dim=N,dim=N] dtype=T",
    "GRAPH":          "graph [label]:\\n  §A → §B [label]",
    "TREE":           "tree [label]:\\n  §root\\n    §child",
    "EMBED":          "embed §REF: [F, F, …]",
    "ATTN":           "attn §REF: §A=F, §B=F",
    "LEARNED-BPE":    "learned-bpe:\\n```json\\n{…}\\n```",
    # Formal / verification
    "GRAMMAR":        "grammar [name]:\\n```ebnf\\n…\\n```",
    "STRUCTURED-CONF":"structured-conf §REF:\\n  key: F",
    "DP":             "dp budget=ε=F,δ=F mechanism=M",
    "FORMAL-CLAIM":   "formal-claim §REF [smt2|dafny|lean4]:\\n```\\n…\\n```",
    # Multi-model / privacy
    "COLLAB":     "collab [author|attr]:",
    "FEDERATED":  "federated [target=M region=R]:\\n```\\n…\\n```",
    "POLYGLOT":   "polyglot [l1,l2,…]:\\n```l1\\n…\\n```\\n```l2\\n…\\n```",
    "PERSONA":    "persona? (query assigned persona)",
}

_CAP_CATEGORIES: list[tuple[str, list[str]]] = [
    ("Core", ["EDIT", "INTERACT", "PLAN", "CLAIM", "REASONING", "MEMORY", "HANDOFF"]),
    ("Code", ["TEST-SPEC", "EFFECTS", "DEP", "SCHEMA", "DOCSTUB"]),
    ("Explain", ["HINT", "OUTLINE", "WIRE"]),
    ("Agent", ["PROMPT", "AGENT-STATE"]),
    ("Control", ["MOTION"]),
    ("Math/ML", ["MATH", "TENSOR-SHAPE", "GRAPH", "TREE", "EMBED", "ATTN", "LEARNED-BPE"]),
    ("Formal", ["GRAMMAR", "STRUCTURED-CONF", "DP", "FORMAL-CLAIM"]),
    ("Multi-model", ["COLLAB", "FEDERATED", "POLYGLOT", "PERSONA"]),
]

_L3_HEADER = (
    "You speak Ozma Protocol v5.11. Respond using only the structured types listed below. "
    "Prose uses WIRE format. Code uses IR. Positions use §N or §STABLEID."
)

_L3_NATIVE_HEADER = "You speak Ozma Protocol v5.11 natively."


def _build_l3_descriptors(caps: dict[str, str]) -> str:
    """Full cap descriptor block filtered to declared caps."""
    use_all = not caps
    lines = [_L3_HEADER]
    for category, cap_names in _CAP_CATEGORIES:
        items = [
            _CAP_DESCRIPTORS[c]
            for c in cap_names
            if c in _CAP_DESCRIPTORS and (use_all or c in caps)
        ]
        if items:
            lines.append(f"{category}: " + " | ".join(items))
    return "\n".join(lines)


def _build_l3_cap_names(caps: dict[str, str]) -> str:
    """Compact cap-name list grouped by category (names only, no descriptors).

    Uses the same `_CAP_CATEGORIES` grouping as `_build_l3_descriptors` so
    compact and standard profiles present caps in identical order — just
    without the syntax body. Categories with no declared caps are omitted.
    """
    use_all = not caps
    lines = [_L3_HEADER]
    for category, cap_names in _CAP_CATEGORIES:
        names = [
            c for c in cap_names
            if c in _CAP_DESCRIPTORS and (use_all or c in caps)
        ]
        if names:
            lines.append(f"{category}: {' '.join(names)}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Profile → system-block builder
# ---------------------------------------------------------------------------

def _build_by_profile(entry: ModelEntry, profile: str) -> str:
    """Build the profile-appropriate system block for a given model entry.

    Language legends are NOT emitted here — they are injected per-turn by
    ``build_user_prefix`` for the languages actually detected in the
    request (fenced code blocks or explicit ``active_files``). The system
    block remains stable across turns for upstream prompt-cache hits.

    Profiles differ in what surrounds the IR marker: ``compact`` is the
    lightest prompted profile, ``standard`` adds memory ops and full L3
    cap descriptors, ``full`` is identical to ``standard`` today (held
    open for future "verbose teaching" additions for low-compliance
    models, e.g. inline per-turn examples).
    """
    if profile == "native":
        # Trained models — minimum viable header. Enough for tooling to
        # recognise the protocol is in force; nothing is re-taught.
        parts = [_IR_INTRO]  # contains "Ozma IR" marker
        if entry.level >= LEVEL.L3:
            parts.append(_L3_NATIVE_HEADER)
        return "\n".join(parts)

    if profile == "compact":
        parts = [_HEADER, _IR_INTRO]
        if entry.level >= LEVEL.L3:
            parts.append(_build_l3_cap_names(entry.caps))
            # MEMORY is surfaced in cap names but the syntax isn't
            # obvious — teach `remember:` / `recall` when declared.
            if "MEMORY" in entry.caps:
                parts.append(_MEMORY_OPS)
        return "\n".join(parts)

    # "standard" and "full" share the same system block for now. Per-turn
    # legend injection is identical across both (every detected language
    # is taught). Differentiate later if low-compliance models need extra
    # in-prompt examples.
    parts = [_HEADER, _IR_INTRO, _MEMORY_OPS]
    if entry.level >= LEVEL.L3:
        parts.append(_build_l3_descriptors(entry.caps))
    return "\n".join(parts)


def _system_langs_for(profile: str) -> frozenset[str]:
    """Languages the model does not need taught per-turn under `profile`.

    Only ``native`` (a fine-tuned model that saw every legend during
    training) skips per-turn injection. All prompted profiles rely on
    per-turn teaching — whatever is active in the current turn gets its
    legend line prepended to the user message, and nothing else is sent.
    """
    if profile == "native":
        return _supported_ir_langs()   # model knows them all — skip extension
    return frozenset()   # prompted profiles: teach whatever is active per turn


# ---------------------------------------------------------------------------
# SystemPromptBuilder
# ---------------------------------------------------------------------------

class SystemPromptBuilder:
    """Builds the system prompt + per-turn user prefix.

    The system block is stable across turns for cache-efficiency. Per-turn
    state (legend aliases, active files, stale markers, out-of-base-set
    IR language lines) goes into the user-role prefix.
    """

    def build_system(
        self,
        entry: ModelEntry,
        probe_line: str = "",
    ) -> str:
        """Build the system message. Empty string for L0/L1."""
        if entry.level <= LEVEL.L1:
            return ""

        profile = resolve_teaching_profile(entry)
        parts = [_build_by_profile(entry, profile)]
        if probe_line:
            parts.append(probe_line)
        return "\n".join(parts)

    def build_user_prefix(
        self,
        legend: dict[str, str] | None = None,
        active_files: list[tuple[str, str]] | None = None,
        stale_paths: set[str] | None = None,
        entry: ModelEntry | None = None,
        ir_languages: list[str] | None = None,
        ir_presentation: str = "alphabet",
    ) -> str:
        """Build the per-turn user-role prefix block.

        Args:
            legend:           {short_name → original_name} identifier mapping
            active_files:    [(filename, language), ...] files in this turn
            stale_paths:     absolute paths of files with stale cached chunks
            entry:           model entry — used to inject per-turn IR extensions
                             for languages outside the profile's base set
            ir_languages:    shim-provided authoritative language list for IR
                             legend injection. When ``None`` (default) the
                             languages are auto-detected from ``active_files``.
                             When explicitly provided — even as ``[]`` — the
                             shim's list wins.
            ir_presentation: one of ``IR_PRESENTATIONS``; controls how the
                             IR map is rendered in the per-turn legend line.
                             Defaults to ``"alphabet"``.
        """
        parts: list[str] = []

        if legend:
            aliases = ", ".join(f"{k}={v}" for k, v in legend.items())
            parts.append(f"<legend>{aliases}</legend>")

        # Per-turn IR extension.
        #   ir_languages is authoritative when provided (shim decides).
        #   Otherwise fall back to auto-detection from active_files.
        # Language names are normalised so callers can pass "py",
        # "typescript", "rust", etc. and still hit the canonical entry.
        if entry is not None:
            if ir_languages is not None:
                lang_sources: list[str] = list(ir_languages)
            elif active_files:
                lang_sources = [lang for _fname, lang in active_files]
            else:
                lang_sources = []

            if lang_sources:
                profile = resolve_teaching_profile(entry)
                already = _system_langs_for(profile)
                extensions: list[str] = []
                seen: set[str] = set()
                for raw_lang in lang_sources:
                    lang = normalize_language(raw_lang)
                    if lang and lang not in already and lang not in seen:
                        try:
                            rendered = render_ir_map(lang, ir_presentation)
                        except FileNotFoundError:
                            rendered = ""
                        if rendered:
                            extensions.append(
                                f"[ir:{lang.lower()}] {rendered}"
                            )
                        seen.add(lang)
                parts.extend(extensions)

        if active_files:
            for fname, lang in active_files:
                parts.append(f"[file: {fname} lang={lang}]")

        if stale_paths:
            for path in sorted(stale_paths):
                parts.append(f"[stale: {path}]")

        return "\n".join(parts)

    def build_native_header(
        self,
        session_id: str,
        model_id: str,
        workspace: str = "/workspace",
        tools: list[str] | None = None,
        channel: str = "api",
    ) -> str:
        """Minimal native-protocol system header for natively-protocol-
        speaking models. This is the 61-token header used when the shim
        is configured with ``native_header=True``; it is distinct from
        the ``teaching_profile="native"`` build_system path, which still
        emits a short instructional line for tooling detection.
        """
        tools_str = " ".join(tools or [
            "read", "write", "edit", "exec", "web_search", "web_fetch",
            "memory_get", "memory_search",
        ])
        return (
            f"workspace: {workspace}\n"
            f"tools: {tools_str}\n"
            f"channel: {channel}  model: {model_id}\n"
            f"[ozma v5.11 | session: {session_id}]"
        )
