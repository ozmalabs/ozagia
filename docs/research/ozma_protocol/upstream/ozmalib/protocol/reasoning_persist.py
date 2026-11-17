"""
reasoning_persist.py — Cross-turn reasoning cache (v5.11 REASONING-PERSIST).

Caches accepted-branch reasoning by AGENT-STATE goal and re-injects it on
subsequent turns. Rejected branches (`→ reject`) are dropped; a single
`reject-learned` entry may be retained per goal (a branch that corrected
a prior accepted one).

Why accepted-only: raw reasoning is 60–80% dead branches. Storing all of it
(K2.6 `preserve_thinking: {keep: all}`) wastes tokens on paths the model
already decided against. Ozma's REASONING grammar distinguishes
`→ accept` / `∴ EDIT` from `→ reject`, so persistence stores only the
decision trajectory.

Eviction: LRU by emission turn; ∴ EDIT entries are pinned and evict last.
"""

from __future__ import annotations

import hashlib
from collections import OrderedDict
from dataclasses import dataclass, field

from .types import (
    PersistScope, PriorReasoningBlock, ReasoningBlock, ReasoningPersistEntry,
)


def goal_hash(goal: str) -> str:
    """Stable 8-char hash used as cache key and in the re-injection header."""
    return hashlib.sha256(goal.encode("utf-8")).hexdigest()[:8]


def _estimate_tokens(text: str) -> int:
    """Rough WIRE-aware estimate — 3.2 chars/token. Shim replaces with tokenizer."""
    return max(1, len(text) // 3 + 1)


@dataclass
class _GoalBucket:
    """Per-goal cache of accepted/edit branches, ordered by turn_no."""
    goal: str
    goal_hash: str
    entries: OrderedDict[tuple[int, str], ReasoningPersistEntry] = field(default_factory=OrderedDict)
    tokens: int = 0

    def add(self, entry: ReasoningPersistEntry) -> None:
        key = (entry.turn_no, entry.hypothesis_id + (entry.step_id or ""))
        if key in self.entries:
            self.tokens -= self.entries[key].tokens
            del self.entries[key]
        self.entries[key] = entry
        self.tokens += entry.tokens

    def evict_to_budget(self, budget_max: int) -> int:
        """Drop LRU entries (oldest turn first) until ≤ budget. Pin ∴ EDIT entries.

        Returns number of entries dropped."""
        if self.tokens <= budget_max:
            return 0

        pinned: list[tuple[tuple[int, str], ReasoningPersistEntry]] = []
        evictable: list[tuple[tuple[int, str], ReasoningPersistEntry]] = []
        for k, v in self.entries.items():
            (pinned if v.verdict == "edit" else evictable).append((k, v))

        dropped = 0
        for k, v in evictable:
            if self.tokens <= budget_max:
                break
            del self.entries[k]
            self.tokens -= v.tokens
            dropped += 1

        if self.tokens > budget_max and pinned:
            for k, v in pinned[:-1]:
                if self.tokens <= budget_max:
                    break
                del self.entries[k]
                self.tokens -= v.tokens
                dropped += 1

        return dropped


class ReasoningPersistCache:
    """Cross-turn reasoning store keyed by (workspace_id, goal_hash).

    Usage::

        cache = ReasoningPersistCache(scope=PersistScope.WORKUNIT, budget=800)
        cache.ingest_turn(workspace_id="ws_1", goal="add empty-file guard",
                          turn_no=4, reasoning=parsed_reasoning)
        block = cache.prior_reasoning(workspace_id="ws_1", goal="add empty-file guard")
        # block → PriorReasoningBlock ready for system-message injection
    """

    def __init__(
        self,
        *,
        scope: PersistScope = PersistScope.OFF,
        budget: int = 800,
    ) -> None:
        self.scope = scope
        self.budget = budget
        self._buckets: dict[tuple[str, str], _GoalBucket] = {}
        self._archived: dict[tuple[str, str], _GoalBucket] = {}
        self._telemetry: dict[str, int] = {
            "persist_turns_cached": 0,
            "persist_entries": 0,
            "persist_evictions": 0,
            "persist_budget_exceeded": 0,
            "persist_reinjection_hit": 0,
        }

    # -----------------------------------------------------------------
    # Ingestion
    # -----------------------------------------------------------------

    def ingest_turn(
        self,
        *,
        workspace_id: str,
        goal: str,
        turn_no: int,
        reasoning: ReasoningBlock | None,
        step_id: str | None = None,
    ) -> int:
        """Extract accept/edit branches from a REASONING block and add to cache.

        `step_id` is set when the reasoning comes from a THINK-ACT step
        (`t-N.sM h*`); omit for turn-prefix reasoning (`t-N h*`).

        Returns the number of entries added. No-op when scope=OFF.
        """
        if self.scope == PersistScope.OFF or reasoning is None:
            return 0

        bucket = self._get_bucket(workspace_id, goal)
        added = 0

        for step in reasoning.steps:
            verdict = _normalize_verdict(step)
            if verdict not in ("accept", "edit"):
                continue

            entry = ReasoningPersistEntry(
                turn_no=turn_no,
                hypothesis_id=step.hypothesis.split(":", 1)[0].strip() if ":" in step.hypothesis else f"h{added+1}",
                text=step.hypothesis,
                verdict=verdict,
                edit_ref=_extract_edit_ref(step) if verdict == "edit" else None,
                step_id=step_id,
                tokens=_estimate_tokens(step.hypothesis),
            )
            bucket.add(entry)
            added += 1

        if added:
            self._telemetry["persist_turns_cached"] += 1
            self._telemetry["persist_entries"] += added
            dropped = bucket.evict_to_budget(self.budget)
            if dropped:
                self._telemetry["persist_evictions"] += dropped
                self._telemetry["persist_budget_exceeded"] += 1

        return added

    def ingest_reject_learned(
        self,
        *,
        workspace_id: str,
        goal: str,
        turn_no: int,
        hypothesis_id: str,
        text: str,
    ) -> None:
        """Record a single reject-learned entry — a rejected branch that
        corrected a prior accepted branch. Replaces any previous reject-learned
        for this goal."""
        if self.scope == PersistScope.OFF:
            return
        bucket = self._get_bucket(workspace_id, goal)
        for k, v in list(bucket.entries.items()):
            if v.verdict == "reject-learned":
                del bucket.entries[k]
                bucket.tokens -= v.tokens
        entry = ReasoningPersistEntry(
            turn_no=turn_no,
            hypothesis_id=hypothesis_id,
            text=text,
            verdict="reject-learned",
            tokens=_estimate_tokens(text),
        )
        bucket.add(entry)
        bucket.evict_to_budget(self.budget)

    # -----------------------------------------------------------------
    # Re-injection
    # -----------------------------------------------------------------

    def prior_reasoning(
        self,
        *,
        workspace_id: str,
        goal: str,
        budget: int | None = None,
    ) -> PriorReasoningBlock | None:
        """Return the prior-reasoning block ready to prepend to the next turn.

        Returns None if scope=OFF, no cache for this goal, or cache empty."""
        if self.scope == PersistScope.OFF:
            return None
        bucket = self._buckets.get((workspace_id, goal))
        if bucket is None or not bucket.entries:
            return None

        budget_max = budget if budget is not None else self.budget
        bucket.evict_to_budget(budget_max)
        self._telemetry["persist_reinjection_hit"] += 1

        return PriorReasoningBlock(
            goal_hash=bucket.goal_hash,
            turns=len({e.turn_no for e in bucket.entries.values()}),
            budget_used=bucket.tokens,
            budget_max=budget_max,
            entries=list(bucket.entries.values()),
        )

    def render(self, block: PriorReasoningBlock | None) -> str:
        """Wire-form text for a PriorReasoningBlock. Empty string for None."""
        if block is None or not block.entries:
            return ""
        header = (
            f"[prior reasoning ↦ goal:{block.goal_hash} "
            f"turns={block.turns} budget={block.budget_used}/{block.budget_max}]"
        )
        max_turn = max(e.turn_no for e in block.entries)
        lines = [header]
        for e in block.entries:
            rel = max_turn - e.turn_no
            prefix = f"  t-{rel}"
            if e.step_id:
                prefix += f".{e.step_id}"
            verdict_marker = "∴ EDIT" if e.verdict == "edit" else e.verdict
            ref = f" {e.edit_ref}" if e.edit_ref else ""
            lines.append(f"{prefix} {e.hypothesis_id}: {e.text} → {verdict_marker}{ref}")
        lines.append("[/prior reasoning]")
        return "\n".join(lines)

    # -----------------------------------------------------------------
    # Goal lifecycle
    # -----------------------------------------------------------------

    def rebind_goal(self, *, workspace_id: str, old_goal: str, new_goal: str) -> None:
        """AGENT-STATE goal changed — archive the old cache, start a fresh one.

        Archive is retrievable via `archived_for(workspace_id, old_goal)` but
        is never auto-injected."""
        key = (workspace_id, old_goal)
        if key in self._buckets:
            self._archived[key] = self._buckets.pop(key)
        # new_goal cache created lazily on next ingest
        _ = new_goal

    def archived_for(self, workspace_id: str, goal: str) -> _GoalBucket | None:
        return self._archived.get((workspace_id, goal))

    # -----------------------------------------------------------------
    # Observability
    # -----------------------------------------------------------------

    def telemetry(self) -> dict[str, int]:
        return dict(self._telemetry)

    def flush_counters(self, sink, component_id: str | None = None) -> None:
        """Snapshot telemetry counters into a TelemetrySink under component
        name 'reasoning_persist'. Non-resetting — counters continue to
        accumulate after flush. Safe to call periodically."""
        sink.record_counters("reasoning_persist", self.telemetry(), component_id)

    def _get_bucket(self, workspace_id: str, goal: str) -> _GoalBucket:
        key = (workspace_id, goal)
        if key not in self._buckets:
            self._buckets[key] = _GoalBucket(goal=goal, goal_hash=goal_hash(goal))
        return self._buckets[key]


# ---------------------------------------------------------------------------
# Helpers for extracting verdict / edit_ref from a parsed ReasoningStep.
# ---------------------------------------------------------------------------

def _normalize_verdict(step) -> str:
    """Map parsed verdict to canonical accept/edit/reject/defer. Detects ∴ EDIT."""
    raw = (step.verdict or "").strip().lower()
    if raw == "accept":
        hypothesis = getattr(step, "hypothesis", "") or ""
        if "∴ edit" in hypothesis.lower() or "∴edit" in hypothesis.lower():
            return "edit"
        return "accept"
    if raw == "reject":
        return "reject"
    if raw == "defer":
        return "defer"
    return raw


def _extract_edit_ref(step) -> str | None:
    """Pull the §REF out of an `∴ EDIT §N` hypothesis, or None."""
    text = getattr(step, "hypothesis", "") or ""
    idx = text.lower().find("∴ edit")
    if idx < 0:
        idx = text.lower().find("∴edit")
    if idx < 0:
        return None
    tail = text[idx:].split()
    for tok in tail:
        if tok.startswith("§"):
            return tok
    return None
