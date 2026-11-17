"""
think_act.py — Interleaved reasoning + tool-call execution (v5.11 THINK-ACT).

The base TURN grammar has one REASONING prefix before BODY, so multi-step
tool chains must cross multiple turns. THINK-ACT introduces an alternative
BODY form: `THINK_ACT_LIST = { REASONING_STEP? INT_CMD OBSERVATION? }+`
where each tool call carries its own reasoning preamble, the shim injects
the result as `[observation step=N]`, and subsequent steps may reference
prior results via `@obs:N` anaphor.

Executor responsibilities:

1. Enforce `think_act_max_steps` (default 16) and two token budgets
   (per-step, per-turn).
2. Inject `[observation step=N]` between emitted step and next sample.
3. Resolve `@obs:N` anaphors to the injected observation.
4. On turn close, flatten `interact: edit` steps into a consolidated
   EDIT_LIST for the apply pipeline.
5. Telemetry: step count, per-step tool latency, truncated-step count,
   total tokens.

Resume semantics (review-issue #14): when the step cap is reached
mid-body, partial state is captured so a follow-up turn can emit
`THINK_ACT_RESUME = "think-act:\n  resume from=<STEP_NO>\n  ..."` and
continue with prior steps accessible to `@obs:N`.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable

from .types import (
    EditCmd, InteractCmd, ReasoningStep, ThinkActBlock, ThinkActStep,
)

_OBS_ANAPHOR_RE = re.compile(r"@obs:(\d+)")


class ObservationResolveError(Exception):
    """Raised when @obs:N references a step that doesn't exist or is out of scope."""
    def __init__(self, step_ref: int, *, out_of_scope: bool = False):
        self.step_ref = step_ref
        self.out_of_scope = out_of_scope
        reason = "out-of-scope" if out_of_scope else "undefined"
        super().__init__(f"@obs:{step_ref} — {reason}")


@dataclass
class ToolInvocation:
    """Bundle passed to the tool-runner callback."""
    step_no: int
    cmd: InteractCmd | EditCmd
    observations: dict[int, str]    # step_no → observation (for anaphor resolution)


@dataclass
class _ExecutorState:
    steps: list[ThinkActStep] = field(default_factory=list)
    observations: dict[int, str] = field(default_factory=dict)
    tokens_total: int = 0


@dataclass
class ExecutorConfig:
    max_steps: int = 16
    max_tokens_per_step: int = 16384
    max_tokens_total: int = 131072


@dataclass
class ResumeContext:
    """Captured when step cap hit; the AGENT-STATE bridges into the next turn.

    Emitted on the next turn as:

        think-act:
          resume from=<last_completed_step>
          prior-obs:
            @obs:1 <digest of observations[1]>
            @obs:2 <digest of observations[2]>
            ...
    """
    last_completed_step: int
    prior_observations: dict[int, str]   # step → observation (may be digest, not full)


class ThinkActExecutor:
    """Sequential executor for a THINK_ACT_LIST body.

    Usage::

        exec = ThinkActExecutor(tool_runner=my_runner)
        block, resume = exec.run(steps_stream)
        if resume:
            # persist `resume` in AGENT-STATE, continue next turn

    The tool_runner is a callback: given a `ToolInvocation`, it returns the
    observation string. This lets tests substitute a deterministic runner
    without booting the full interact loop.
    """

    def __init__(
        self,
        *,
        tool_runner: Callable[[ToolInvocation], str],
        config: ExecutorConfig | None = None,
        prior_observations: dict[int, str] | None = None,
    ) -> None:
        self.tool_runner = tool_runner
        self.config = config or ExecutorConfig()
        self._state = _ExecutorState(observations=dict(prior_observations or {}))
        self._telemetry: dict[str, int | float] = {
            "step_count": 0,
            "truncated_step_count": 0,
            "total_tokens": 0,
            "tool_latency_ms_sum": 0.0,
        }

    def run(self, steps: Iterable[ThinkActStep]) -> tuple[ThinkActBlock, ResumeContext | None]:
        """Execute steps until exhausted / cap / terminal. Returns (block, resume).

        `resume` is not None iff the step-count cap was reached — caller should
        persist it into AGENT-STATE and emit a THINK_ACT_RESUME header on the
        next turn.
        """
        block = ThinkActBlock()
        resume: ResumeContext | None = None

        for step in steps:
            if len(self._state.steps) >= self.config.max_steps:
                block.step_cap_reached = True
                resume = ResumeContext(
                    last_completed_step=self._state.steps[-1].step_no if self._state.steps else 0,
                    prior_observations=dict(self._state.observations),
                )
                break
            if self._state.tokens_total >= self.config.max_tokens_total:
                block.total_cap_reached = True
                break

            executed = self._execute_step(step)
            block.steps.append(executed)
            self._state.steps.append(executed)
            self._state.tokens_total += executed.tokens_out

            if executed.truncated:
                self._telemetry["truncated_step_count"] = int(self._telemetry["truncated_step_count"]) + 1

            self._telemetry["step_count"] = int(self._telemetry["step_count"]) + 1
            self._telemetry["total_tokens"] = int(self._telemetry["total_tokens"]) + executed.tokens_out

            if _is_terminal(executed):
                break

        return block, resume

    def _execute_step(self, step: ThinkActStep) -> ThinkActStep:
        if step.reasoning is not None:
            self._resolve_anaphors_in_reasoning(step.reasoning)
        if step.interact is not None:
            self._resolve_anaphors_in_cmd(step.interact)

        tokens_out = min(step.tokens_out, self.config.max_tokens_per_step)
        truncated = step.tokens_out > self.config.max_tokens_per_step

        observation: str | None = None
        if step.interact is not None and not truncated:
            inv = ToolInvocation(
                step_no=step.step_no,
                cmd=step.interact,
                observations=dict(self._state.observations),
            )
            t0 = time.perf_counter()
            observation = self.tool_runner(inv)
            self._telemetry["tool_latency_ms_sum"] = (
                float(self._telemetry["tool_latency_ms_sum"]) + (time.perf_counter() - t0) * 1000.0
            )
            self._state.observations[step.step_no] = observation

        return ThinkActStep(
            step_no=step.step_no,
            reasoning=step.reasoning,
            interact=step.interact,
            observation=observation,
            tokens_out=tokens_out,
            truncated=truncated,
        )

    # -----------------------------------------------------------------
    # Anaphor resolution
    # -----------------------------------------------------------------

    def _resolve_anaphors_in_reasoning(self, reasoning: ReasoningStep) -> None:
        self._check_anaphors(reasoning.hypothesis)

    def _resolve_anaphors_in_cmd(self, cmd: InteractCmd | EditCmd) -> None:
        for attr in ("target", "body", "raw"):
            val = getattr(cmd, attr, None)
            if isinstance(val, str):
                self._check_anaphors(val)

    def _check_anaphors(self, text: str) -> None:
        for m in _OBS_ANAPHOR_RE.finditer(text):
            ref = int(m.group(1))
            if ref not in self._state.observations:
                raise ObservationResolveError(ref, out_of_scope=ref > 0)

    # -----------------------------------------------------------------
    # Observability / flatten
    # -----------------------------------------------------------------

    def telemetry(self) -> dict[str, int | float]:
        return dict(self._telemetry)

    def flush_counters(self, sink, component_id: str | None = None) -> None:
        """Snapshot telemetry counters into a TelemetrySink under component
        name 'think_act'. Non-resetting."""
        sink.record_counters("think_act", self.telemetry(), component_id)

    @staticmethod
    def flatten_edits(block: ThinkActBlock) -> list[EditCmd]:
        """Consolidate `interact: edit §N ...` steps into an EDIT_LIST for apply."""
        return [s.interact for s in block.steps if isinstance(s.interact, EditCmd)]


def _is_terminal(step: ThinkActStep) -> bool:
    """A step is terminal if it emits an EDIT or a handoff/recall sentinel."""
    if isinstance(step.interact, EditCmd):
        return True
    if isinstance(step.interact, InteractCmd):
        verb = getattr(step.interact.verb, "value", "")
        return verb in ("recall",)   # handoff is a separate BODY type, not an INTERACT
    return False
