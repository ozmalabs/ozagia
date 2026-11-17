"""
reasoning.py — Dynamic reasoning-budget selection.

Reasoning/thinking tokens are typically 90-98% of a frontier model's output
cost. Every major provider exposes a lever for this (Kimi, Claude, OpenAI o-*,
Gemini) but each spells it differently. Static defaults leave huge wins on the
floor: a mechanical refactor burns the same reasoning budget as an
architectural question, even though one needs ~100 reasoning tokens and the
other needs ~10000.

This module does three things:

  1. ReasoningKnob — capability metadata declaring HOW a given provider
     accepts a reasoning-budget hint (where in the request body, what values
     are valid, what each level approximately costs).

  2. ReasoningStats — a per-(task_class, model) online tracker using
     Welford's algorithm. Builds a distribution over actual reasoning-token
     consumption as we observe responses, so future calls can pick the
     smallest level that's likely to be sufficient.

  3. ReasoningResolver — composes task classification + stats + heuristics to
     pick a level at request time. Falls back to a conservative level when
     stats confidence is low; uses mean + k·σ when confidence is high.

The resolver is intentionally split from the classifier: classification can
be swapped (regex heuristics today, fasttext later) without touching the
stats machinery.
"""

from __future__ import annotations

import math
import os
import re
import sqlite3
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol


# ─────────────────────────────────────────────────────────────────────────
# Capability metadata
# ─────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ReasoningKnob:
    """How a provider accepts a reasoning-budget hint.

    `param_path` is dotted — "reasoning.effort" means the value lives at
    req_body["reasoning"]["effort"]. Missing intermediate dicts are created.

    `levels` is ordered cheapest → most expensive. `approx_reasoning_tokens`
    is the matched rough budget for each level (used by the resolver to pick
    the smallest level whose budget covers the stats prediction).

    `default_idx` is what gets picked when a request has no task-class
    classification available and no per-class stats. It should be the
    middle-of-the-road option for the model in question.

    `max_tokens_backstop`: when True, the shim also injects a `max_tokens`
    cap = target_reasoning + response_headroom_tokens. This is essential
    for OpenRouter-shimmed providers where the effort label is translated
    to max_tokens × ratio — without an explicit cap, "effort=low" combined
    with a large caller-supplied max_tokens can blow past the natural
    budget. For providers where max_tokens means total-output-including-
    thinking (Anthropic), leave this False to avoid truncating the visible
    response.

    `response_headroom_tokens`: budget reserved for the visible response
    on top of the reasoning target. 2000 is a safe default for coding tasks;
    raise for prose-heavy or long-generation use cases.
    """
    param_path: str
    levels: tuple[Any, ...]
    approx_reasoning_tokens: tuple[int, ...]
    default_idx: int = 1
    max_tokens_backstop: bool = True
    response_headroom_tokens: int = 2000

    def __post_init__(self) -> None:
        if len(self.levels) != len(self.approx_reasoning_tokens):
            raise ValueError(
                f"levels ({len(self.levels)}) and approx_reasoning_tokens "
                f"({len(self.approx_reasoning_tokens)}) must align"
            )
        if not 0 <= self.default_idx < len(self.levels):
            raise ValueError(
                f"default_idx {self.default_idx} out of range for "
                f"{len(self.levels)} levels"
            )
        # approx_reasoning_tokens must be monotonic non-decreasing
        for a, b in zip(self.approx_reasoning_tokens,
                        self.approx_reasoning_tokens[1:]):
            if b < a:
                raise ValueError(
                    "approx_reasoning_tokens must be non-decreasing "
                    f"(got {self.approx_reasoning_tokens})"
                )
        if self.response_headroom_tokens < 0:
            raise ValueError("response_headroom_tokens must be >= 0")


# Canonical knobs for the major providers. Add new entries here as we
# characterize more models. These are *provider* templates — a ModelEntry
# that targets Kimi should reference KIMI_EFFORT etc.

KIMI_EFFORT = ReasoningKnob(
    param_path="reasoning.effort",
    levels=("low", "medium", "high"),
    approx_reasoning_tokens=(1000, 5000, 20000),
    default_idx=1,
)

ANTHROPIC_THINKING = ReasoningKnob(
    param_path="thinking.budget_tokens",
    levels=(2048, 8192, 32768),
    approx_reasoning_tokens=(2048, 8192, 32768),
    default_idx=1,
)

OPENAI_REASONING_EFFORT = ReasoningKnob(
    param_path="reasoning_effort",
    levels=("low", "medium", "high"),
    approx_reasoning_tokens=(1500, 8000, 32000),
    default_idx=1,
)

GEMINI_THINKING_BUDGET = ReasoningKnob(
    param_path="thinking_config.thinking_budget",
    levels=(512, 4096, 24576),
    approx_reasoning_tokens=(512, 4096, 24576),
    default_idx=1,
)

# Native Moonshot (api.moonshot.ai) exposes only a binary switch:
#   thinking: {type: "enabled" | "disabled"}
# No effort slider, no budget control — reasoning either happens or it doesn't.
# When disabled, reasoning costs zero (the model replies directly). When
# enabled, Kimi's reasoning spends ~3500–8500 tokens depending on the task
# (observed from the OpenRouter bench — native behaves the same since OR's
# effort slider is a shim on top of this same binary). Production path for
# Ozma-trained K2.6 LoRA uses this knob directly rather than the OR shim.
MOONSHOT_THINKING = ReasoningKnob(
    param_path="thinking.type",
    levels=("disabled", "enabled"),
    approx_reasoning_tokens=(0, 4000),
    default_idx=1,
    max_tokens_backstop=True,
    response_headroom_tokens=2000,
)


# ── OpenRouter-specific knob templates ─────────────────────────────────
#
# OpenRouter unifies reasoning control under `reasoning.effort` and
# `reasoning.max_tokens`. Per their docs, `effort` is translated to
# `budget_tokens ≈ ratio × max_tokens` for the upstream:
#   low=0.2, medium=0.5, high=0.8, xhigh=0.95, minimal=0.1
#
# This translation is brittle for upstreams that don't natively accept an
# effort slider (notably Moonshot/Kimi — its Moonshot API only has
# `thinking: {type: enabled|disabled}`). Enabling max_tokens_backstop
# gives us an authoritative ceiling regardless of how the upstream
# interprets the effort label.
#
# approx_reasoning_tokens values are nominal — the resolver learns the
# real numbers via observed-level stats (see StatsStore.observe_level_cost).

# Pessimistic priors: Kimi-via-OpenRouter `effort=low` empirically costs
# ~3500 reasoning tokens despite OR's 0.2×max_tokens translation — the
# upstream just ignores the shim. Sizing the backstop from 1500 truncated
# the visible response on every cold-start trial. (4000, 10000, 30000)
# oversizes for providers that honor the effort label (OpenAI o-*), but
# that excess disappears once observed level-cost stats converge. See
# tests/bench_kimi_resolver.py output for the +134%/+7%/-100% deltas
# that motivated these numbers.
OPENROUTER_EFFORT = ReasoningKnob(
    param_path="reasoning.effort",
    levels=("low", "medium", "high"),
    approx_reasoning_tokens=(4000, 10000, 30000),
    default_idx=1,
    max_tokens_backstop=True,
    response_headroom_tokens=2000,
)

# For Anthropic via OpenRouter we prefer explicit max_tokens in reasoning
# because Anthropic's thinking.budget_tokens semantics map cleanly and the
# effort shim is looser. Backstop OFF because OR for Anthropic routes
# max_tokens as total-output-including-thinking.
OPENROUTER_ANTHROPIC_MAX = ReasoningKnob(
    param_path="reasoning.max_tokens",
    levels=(2048, 8192, 32768),
    approx_reasoning_tokens=(2048, 8192, 32768),
    default_idx=1,
    max_tokens_backstop=False,
    response_headroom_tokens=2000,
)


def set_dotted(body: dict[str, Any], path: str, value: Any) -> None:
    """Write `value` at the dotted `path` inside `body`, creating dicts as needed."""
    parts = path.split(".")
    cursor = body
    for p in parts[:-1]:
        if p not in cursor or not isinstance(cursor[p], dict):
            cursor[p] = {}
        cursor = cursor[p]
    cursor[parts[-1]] = value


# ─────────────────────────────────────────────────────────────────────────
# Online statistics (Welford)
# ─────────────────────────────────────────────────────────────────────────


@dataclass
class ReasoningStats:
    """Welford online mean + variance over observed reasoning-token counts.

    Kept per (task_class, model_id). The resolver uses mean + k·σ to pick
    the cheapest level whose budget is likely to be sufficient.

    When n < min_samples, `has_confidence()` returns False and the resolver
    falls back to heuristics/defaults rather than committing to what might
    be a noisy estimate.
    """
    n: int = 0
    mean: float = 0.0
    m2: float = 0.0  # sum of squared deviations from the running mean

    def update(self, x: float) -> None:
        self.n += 1
        delta = x - self.mean
        self.mean += delta / self.n
        self.m2 += delta * (x - self.mean)

    @property
    def variance(self) -> float:
        return self.m2 / (self.n - 1) if self.n > 1 else 0.0

    @property
    def stddev(self) -> float:
        return math.sqrt(self.variance)

    def has_confidence(self, min_samples: int = 5) -> bool:
        return self.n >= min_samples

    def predict_budget(self, k_sigma: float = 1.0) -> float:
        """Expected reasoning tokens, inflated by k·σ for safety margin."""
        return self.mean + k_sigma * self.stddev


class StatsStore(Protocol):
    """Abstract stats store for reasoning-budget telemetry.

    Two independent observations per request:

      - `observe` / `get`: per-(task_class, model_id) — distribution over
        how much reasoning a given task *class* wants on a given model.
        Drives "how big a budget do I need".

      - `observe_level_cost` / `get_level_cost`: per-(model_id, level_idx)
        — distribution over what each effort LABEL actually costs on this
        route. Drives "is this knob label honest". Critical for OpenRouter
        shims where `effort=low` may behave nothing like "low".
    """
    def get(self, task_class: str, model_id: str) -> ReasoningStats: ...
    def observe(self, task_class: str, model_id: str, reasoning_tokens: int) -> None: ...
    def get_level_cost(self, model_id: str, level_idx: int) -> ReasoningStats: ...
    def observe_level_cost(self, model_id: str, level_idx: int, reasoning_tokens: int) -> None: ...


@dataclass
class MemoryStatsStore:
    """In-memory StatsStore for tests and single-process deploys."""
    _data: dict[tuple[str, str], ReasoningStats] = field(default_factory=dict)
    _level_data: dict[tuple[str, int], ReasoningStats] = field(default_factory=dict)

    def get(self, task_class: str, model_id: str) -> ReasoningStats:
        key = (task_class, model_id)
        if key not in self._data:
            self._data[key] = ReasoningStats()
        return self._data[key]

    def observe(self, task_class: str, model_id: str, reasoning_tokens: int) -> None:
        self.get(task_class, model_id).update(float(reasoning_tokens))

    def get_level_cost(self, model_id: str, level_idx: int) -> ReasoningStats:
        key = (model_id, level_idx)
        if key not in self._level_data:
            self._level_data[key] = ReasoningStats()
        return self._level_data[key]

    def observe_level_cost(self, model_id: str, level_idx: int, reasoning_tokens: int) -> None:
        self.get_level_cost(model_id, level_idx).update(float(reasoning_tokens))


_DEFAULT_STATS_DB_PATH = Path.home() / ".ozma" / "reasoning_stats.db"

_CREATE_CLASS_TABLE = """
CREATE TABLE IF NOT EXISTS class_stats (
    task_class TEXT NOT NULL,
    model_id   TEXT NOT NULL,
    n          INTEGER NOT NULL DEFAULT 0,
    mean       REAL NOT NULL DEFAULT 0.0,
    m2         REAL NOT NULL DEFAULT 0.0,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (task_class, model_id)
)
"""

_CREATE_LEVEL_TABLE = """
CREATE TABLE IF NOT EXISTS level_stats (
    model_id   TEXT NOT NULL,
    level_idx  INTEGER NOT NULL,
    n          INTEGER NOT NULL DEFAULT 0,
    mean       REAL NOT NULL DEFAULT 0.0,
    m2         REAL NOT NULL DEFAULT 0.0,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (model_id, level_idx)
)
"""


class SQLiteStatsStore:
    """SQLite-backed StatsStore. Persists Welford state across processes.

    The cold-start problem for OpenRouter knobs (level labels whose real
    cost diverges from declared approx) is a once-per-(model, level) tax
    when observations are persistent — the first process to hit a new
    level pays for the calibration, every later process reads the
    converged stats. With MemoryStatsStore the tax repeats every run.

    Thread-safe via a per-instance lock around every connection op.
    Pass ":memory:" for ephemeral use (tests).
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self._path = str(path) if path is not None else str(_DEFAULT_STATS_DB_PATH)
        self._lock = threading.Lock()

        if self._path != ":memory:":
            os.makedirs(os.path.dirname(self._path), exist_ok=True)

        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute(_CREATE_CLASS_TABLE)
            self._conn.execute(_CREATE_LEVEL_TABLE)
            self._conn.commit()

    def get(self, task_class: str, model_id: str) -> ReasoningStats:
        with self._lock:
            row = self._conn.execute(
                "SELECT n, mean, m2 FROM class_stats "
                "WHERE task_class = ? AND model_id = ?",
                (task_class, model_id),
            ).fetchone()
        if row is None:
            return ReasoningStats()
        return ReasoningStats(n=row["n"], mean=row["mean"], m2=row["m2"])

    def observe(self, task_class: str, model_id: str, reasoning_tokens: int) -> None:
        with self._lock:
            row = self._conn.execute(
                "SELECT n, mean, m2 FROM class_stats "
                "WHERE task_class = ? AND model_id = ?",
                (task_class, model_id),
            ).fetchone()
            s = ReasoningStats(
                n=row["n"], mean=row["mean"], m2=row["m2"],
            ) if row else ReasoningStats()
            s.update(float(reasoning_tokens))
            self._conn.execute(
                "INSERT INTO class_stats (task_class, model_id, n, mean, m2) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(task_class, model_id) DO UPDATE SET "
                "n = excluded.n, mean = excluded.mean, m2 = excluded.m2, "
                "updated_at = datetime('now')",
                (task_class, model_id, s.n, s.mean, s.m2),
            )
            self._conn.commit()

    def get_level_cost(self, model_id: str, level_idx: int) -> ReasoningStats:
        with self._lock:
            row = self._conn.execute(
                "SELECT n, mean, m2 FROM level_stats "
                "WHERE model_id = ? AND level_idx = ?",
                (model_id, level_idx),
            ).fetchone()
        if row is None:
            return ReasoningStats()
        return ReasoningStats(n=row["n"], mean=row["mean"], m2=row["m2"])

    def observe_level_cost(
        self, model_id: str, level_idx: int, reasoning_tokens: int,
    ) -> None:
        with self._lock:
            row = self._conn.execute(
                "SELECT n, mean, m2 FROM level_stats "
                "WHERE model_id = ? AND level_idx = ?",
                (model_id, level_idx),
            ).fetchone()
            s = ReasoningStats(
                n=row["n"], mean=row["mean"], m2=row["m2"],
            ) if row else ReasoningStats()
            s.update(float(reasoning_tokens))
            self._conn.execute(
                "INSERT INTO level_stats (model_id, level_idx, n, mean, m2) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(model_id, level_idx) DO UPDATE SET "
                "n = excluded.n, mean = excluded.mean, m2 = excluded.m2, "
                "updated_at = datetime('now')",
                (model_id, level_idx, s.n, s.mean, s.m2),
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()


# ─────────────────────────────────────────────────────────────────────────
# Task classification (heuristic, pluggable)
# ─────────────────────────────────────────────────────────────────────────


# Task classes used as keys in the stats store. Keep the set small so that
# per-class sample counts accumulate fast enough to reach confidence.
TASK_CLASSES = (
    "mechanical",   # refactor, rename, convert, lint-fix — code-in, code-out
    "debug",        # find/fix bug, explain failure, trace execution
    "explain",      # summarize, document, describe existing code
    "design",       # architect, approach, trade-offs, planning
    "generate",     # write new feature from spec
    "qa",           # direct answer to a direct question
    "unknown",      # classifier couldn't decide
)


_FENCE_RE = re.compile(r"^```", re.MULTILINE)

# Keyword sets — conservative, aimed at high precision over recall. Better
# to fall through to "unknown" and use the default level than to mis-route.
_MECHANICAL_KW = re.compile(
    r"\b(refactor|rename|convert|reformat|reorder|port|migrate|translate|"
    r"apply|replace|extract|inline|lint|format|add (types?|type hints)|"
    r"fix the off-by-one|fix the typo)\b",
    re.IGNORECASE,
)
_DEBUG_KW = re.compile(
    r"\b(bug|crash|wrong|unexpected|traceback|exception|fails?|failing|"
    r"broken|regression|subtle|edge case|off.by.one|race condition|"
    r"deadlock|memory leak|segfault)\b",
    re.IGNORECASE,
)
_EXPLAIN_KW = re.compile(
    r"\b(explain|describe|summari[sz]e|what does|how does (this|it) work|"
    r"document|docstring|comment)\b",
    re.IGNORECASE,
)
_DESIGN_KW = re.compile(
    r"\b(design|architect|approach|trade.?offs?|should I|strategy|"
    r"how should (we|i)|what's the best way|recommend|propose)\b",
    re.IGNORECASE,
)
_GENERATE_KW = re.compile(
    r"\b(implement|write|build|create|add (a |an )?(new )?"
    r"(feature|endpoint|function|class|module))\b",
    re.IGNORECASE,
)


def classify_heuristic(messages: list[dict[str, Any]]) -> tuple[str, float]:
    """Regex-only classifier. Returns (task_class, confidence ∈ [0, 1]).

    Confidence is how many disjoint signals fired; used by the resolver to
    decide whether to trust the class or fall back to the default. A
    fasttext model can replace this with the same return type.
    """
    text = "\n".join(
        m.get("content", "") for m in messages
        if isinstance(m.get("content"), str)
    )
    if not text.strip():
        return ("unknown", 0.0)

    has_fence = bool(_FENCE_RE.search(text))
    scores: dict[str, int] = {}

    if _MECHANICAL_KW.search(text):
        # Mechanical is much stronger when there's also a code fence to operate on
        scores["mechanical"] = 2 if has_fence else 1
    if _DEBUG_KW.search(text):
        scores["debug"] = 2
    if _DESIGN_KW.search(text):
        scores["design"] = 2
    if _EXPLAIN_KW.search(text):
        scores["explain"] = 1
    if _GENERATE_KW.search(text) and not has_fence:
        # "implement X" with a code fence attached is usually mechanical-extend,
        # not greenfield generation. Only count generate when there's no code.
        scores["generate"] = 2

    if not scores:
        # Fence with no keyword signal — default to mechanical if it's a code
        # task, otherwise qa.
        return ("mechanical" if has_fence else "qa", 0.3)

    # Winner — tie-break by the TASK_CLASSES order (which puts cheaper-
    # expected classes first).
    best_class = max(
        scores,
        key=lambda c: (scores[c], -TASK_CLASSES.index(c))
    )
    total = sum(scores.values())
    confidence = min(1.0, scores[best_class] / max(total, 1) * 1.2)
    return (best_class, confidence)


# Per-task-class prior expectation over reasoning-token consumption. Used
# when the stats store has no samples yet for a class. These are seeded
# rough estimates that get overwritten by real observations quickly.
_CLASS_PRIORS: dict[str, int] = {
    "mechanical":  500,
    "qa":         1000,
    "explain":    2000,
    "generate":   5000,
    "debug":      8000,
    "design":    15000,
    "unknown":    5000,  # play it safe
}


# ─────────────────────────────────────────────────────────────────────────
# Resolver
# ─────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ReasoningChoice:
    """Outcome of the resolver — what level to send, why, how sure."""
    level_idx: int | None   # None means "don't inject a reasoning param"
    value: Any | None       # The actual param value to send
    task_class: str
    rationale: str
    confidence: str         # "high" | "medium" | "low"
    # What the class *wants* (drives level selection): class_mean + k·σ
    # when confident, otherwise the class prior.
    target_reasoning_tokens: int = 0
    # What we actually expect the model to spend on reasoning at the
    # chosen level: max(target, observed_level_mean + k·σ) when we have
    # per-level stats, otherwise max(target, declared approx for level).
    # The shim sizes max_tokens from this value, NOT target — otherwise
    # a level whose observed cost exceeds target will truncate the
    # visible response.
    expected_reasoning_tokens: int = 0


Classifier = Callable[[list[dict[str, Any]]], tuple[str, float]]


@dataclass
class ReasoningResolver:
    """Picks a reasoning level for a request.

    Strategy:
      - No knob on entry → return None (don't inject).
      - Classify task.
      - If stats.has_confidence() for (class, model): pick smallest level
        whose approx budget covers stats.predict_budget(k_sigma).
      - Otherwise: use _CLASS_PRIORS[class] as the target budget.
      - If the class is "unknown" and class-confidence is also low:
        return the knob's default_idx (conservative mid).
    """
    stats: StatsStore = field(default_factory=MemoryStatsStore)
    classify: Classifier = classify_heuristic
    k_sigma: float = 1.0
    min_samples_for_confidence: int = 5

    def resolve(
        self,
        messages: list[dict[str, Any]],
        knob: ReasoningKnob | None,
        model_id: str,
    ) -> ReasoningChoice:
        if knob is None:
            return ReasoningChoice(
                level_idx=None, value=None, task_class="n/a",
                rationale="model has no reasoning knob", confidence="n/a",
            )

        task_class, class_conf = self.classify(messages)

        # Low class-confidence + unknown → default_idx.
        if task_class == "unknown" and class_conf < 0.3:
            idx = knob.default_idx
            declared = knob.approx_reasoning_tokens[idx]
            expected = self._expected_level_cost(knob, model_id, idx)
            return ReasoningChoice(
                level_idx=idx, value=knob.levels[idx], task_class=task_class,
                rationale=f"unknown class (conf={class_conf:.2f}); using default",
                confidence="low",
                target_reasoning_tokens=declared,
                expected_reasoning_tokens=expected,
            )

        s = self.stats.get(task_class, model_id)
        if s.has_confidence(self.min_samples_for_confidence):
            target = s.predict_budget(self.k_sigma)
            rationale = (
                f"class={task_class} n={s.n} "
                f"mean={s.mean:.0f} σ={s.stddev:.0f} "
                f"target={target:.0f}"
            )
            conf_level = "high"
        else:
            target = float(_CLASS_PRIORS.get(task_class, _CLASS_PRIORS["unknown"]))
            rationale = (
                f"class={task_class} (conf={class_conf:.2f}) "
                f"n={s.n} < {self.min_samples_for_confidence}; "
                f"using prior={target:.0f}"
            )
            conf_level = "medium" if class_conf >= 0.5 else "low"

        # Pick the level whose budget covers `target`. Prefer observed
        # per-level means over declared approx_reasoning_tokens when we
        # have enough samples — this is how we correct for provider
        # shims that misrepresent their knob labels.
        idx = self._pick_level(knob, model_id, target)
        # Size the backstop by what the LEVEL will actually spend, not just
        # what the class wanted. If observed mean for this level exceeds
        # target, the visible response would be truncated otherwise.
        expected = max(int(target), self._expected_level_cost(knob, model_id, idx))
        return ReasoningChoice(
            level_idx=idx, value=knob.levels[idx],
            task_class=task_class, rationale=rationale,
            confidence=conf_level,
            target_reasoning_tokens=int(target),
            expected_reasoning_tokens=expected,
        )

    def observe(
        self,
        choice: ReasoningChoice,
        model_id: str,
        reasoning_tokens: int,
    ) -> None:
        """Feed an observed reasoning-token count back into stats.

        Records two observations:
          - per-(class, model): what the class wanted on this model
          - per-(model, level_idx): what this LABEL actually costs here

        Caller passes the ReasoningChoice they got from resolve(), plus
        the actual reasoning_tokens usage reported by the provider. Only
        called on successful/accepted responses — failed generations
        shouldn't pollute the distribution.
        """
        if choice.task_class not in ("n/a", "unknown"):
            self.stats.observe(choice.task_class, model_id, reasoning_tokens)
        if choice.level_idx is not None:
            self.stats.observe_level_cost(model_id, choice.level_idx, reasoning_tokens)

    def _expected_level_cost(
        self, knob: ReasoningKnob, model_id: str, level_idx: int,
    ) -> int:
        """Expected reasoning-token spend at this level on this model.

        Returns observed mean + k·σ when we have enough samples for this
        (model, level) pair; otherwise declared approx_reasoning_tokens.
        Used by the shim to size the max_tokens backstop — if this is
        under-estimated the visible response truncates.
        """
        s = self.stats.get_level_cost(model_id, level_idx)
        if s.has_confidence(self.min_samples_for_confidence):
            return int(s.predict_budget(self.k_sigma))
        return knob.approx_reasoning_tokens[level_idx]

    def _pick_level(
        self, knob: ReasoningKnob, model_id: str, target_tokens: float,
    ) -> int:
        """Smallest level whose *effective* budget covers `target`.

        Effective budget per level:
          - observed mean if we have enough samples (self.min_samples_for_confidence)
          - otherwise declared knob.approx_reasoning_tokens[i]

        Monotonicity caveat: observed means across levels may not be
        monotonic on providers whose knob is brittle (the Kimi-via-
        OpenRouter case where `low` sometimes costs more than `medium`).
        When that happens we walk every level and pick the cheapest
        observed-or-declared that meets target. If none meet it, pin
        to the level with the largest budget.
        """
        options: list[tuple[int, float]] = []  # (idx, effective_budget)
        for i in range(len(knob.levels)):
            level_stats = self.stats.get_level_cost(model_id, i)
            if level_stats.has_confidence(self.min_samples_for_confidence):
                budget = level_stats.mean
            else:
                budget = float(knob.approx_reasoning_tokens[i])
            options.append((i, budget))

        # Among levels whose budget >= target, pick the one with the
        # SMALLEST effective budget (cheapest). Fall back to highest-
        # budget level if nothing meets target.
        meeting = [(i, b) for i, b in options if b >= target_tokens]
        if meeting:
            return min(meeting, key=lambda ib: (ib[1], ib[0]))[0]
        return max(options, key=lambda ib: ib[1])[0]


