"""Bounded task specifications, deterministic runner, gates and evidence.

A bounded task is a versioned data file (TOML or JSON) that declares its
objective, inputs, tool allowlist, write scope, worktree expectations,
resource and model budgets, gates, stop conditions, output schema and approval
policy. :class:`TaskRunner` validates that declaration, runs a deterministic
preflight, executes typed steps through an *injected* executor, checks the
gates, accounts the budget and writes an evidence log plus an artifact
manifest under ``.ozagia/tasks/<run-id>/``.

This module never calls a model, never reaches the network and never executes
a tool itself. The executor is supplied by the caller; a run with no executor
is refused. Evidence records metadata (step ids, tools, usage, gate results,
artifact hashes and sizes), never input values, model prompts or file
contents.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ozagia.config import state_dir as default_state_dir
from ozagia.config import state_path
from ozagia.core import resources
from ozagia.core import testing as test_plans
from ozagia.core import worktree as worktree_checks

SCHEMA = 1
TASKS_DIRNAME = "tasks"
EVENTS_FILE = "events.jsonl"
MANIFEST_FILE = "manifest.json"
ARTIFACTS_FILE = "artifacts.json"
OUTPUT_FILE = "output.json"

MAX_SPEC_BYTES = 256 * 1024
MAX_STEPS = 64
MAX_STEP_ARTIFACTS = 100
MAX_EVIDENCE_PER_STEP = 50
MAX_EVENT_TEXT = 240
MAX_EVENT_BYTES = 8 * 1024 * 1024
MAX_SHOW_EVENTS = 5000
MAX_OUTPUT_BYTES = 4 * 1024 * 1024

RUN_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,119}\Z")
SPEC_ID_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,63}\Z")
NAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,63}\Z")
BRANCH_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,254}\Z")
_WINDOWS_ABS = re.compile(r"^[A-Za-z]:/")

#: Neutral tool names. A task may only use these; project-specific tools are a
#: later adapter layer, not an implicit allowlist.
TOOL_CLASSES: dict[str, str] = {
    "read": "read",
    "glob": "read",
    "grep": "read",
    "list": "read",
    "stat": "read",
    "graph_search": "read",
    "graph_impact": "read",
    "model": "model",
    "test": "process",
    "lint": "process",
    "build": "process",
    "write": "mutate",
    "edit": "mutate",
    "patch": "mutate",
    "shell": "mutate",
}
KNOWN_TOOLS = frozenset(TOOL_CLASSES)
GRAPH_TOOLS = frozenset({"graph_search", "graph_impact"})
MUTATING_TOOLS = frozenset(
    name for name, kind in TOOL_CLASSES.items() if kind == "mutate")

BEFORE_GATES = frozenset({
    "worktree_expected", "branch_expected", "drift_clear", "resources_fit",
    "snapshot_fresh", "policy_commit", "policy_merge", "policy_push",
})
AFTER_GATES = frozenset({
    "schema_valid", "evidence_present", "resources_fit", "snapshot_fresh",
    "worktree_expected", "branch_expected", "drift_clear", "policy_commit",
    "policy_merge", "policy_push",
})
KNOWN_GATES = BEFORE_GATES | AFTER_GATES
POLICY_GATES = {
    "policy_commit": "commit",
    "policy_merge": "merge",
    "policy_push": "push",
}
WORKTREE_GATES = frozenset({
    "worktree_expected", "branch_expected", "drift_clear", *POLICY_GATES,
})

STOP_CONDITIONS = frozenset({
    "gate_failed", "budget_exhausted", "resource_limit", "no_progress",
    "repeated_state", "approval_required", "schema_invalid", "error",
    "blocked",
})
MANDATORY_STOPS = (
    "gate_failed", "budget_exhausted", "resource_limit", "no_progress",
    "repeated_state",
)

TOP_KEYS = frozenset({
    "id", "version", "title", "objective", "inputs", "tools", "write_scope",
    "steps", "worktree", "resources", "budget", "gates", "stop", "output",
    "output_schema", "approval", "requires_graph", "test_policy",
})
STEP_KEYS = frozenset({
    "id", "tool", "goal", "paths", "write_paths", "budget", "args",
})
STEP_BUDGET_KEYS = frozenset({"tokens", "model_calls", "cost_usd", "wall_s"})
TEST_POLICY_KEYS = frozenset({"mode", "fallback", "max_tests", "timeout_s"})
WORKTREE_KEYS = frozenset({
    "expected_branch", "base", "require_clean", "require_up_to_date",
})
RESOURCE_KEYS = frozenset({"memory_mb", "disk_mb", "pids", "wall_s"})
BUDGET_KEYS = frozenset({"tokens", "model_calls", "cost_usd", "wall_s"})
GATE_KEYS = frozenset({"before", "after"})
APPROVAL_KEYS = frozenset({"mutate", "network"})
USAGE_KEYS = frozenset({
    "tokens", "model_calls", "cost_usd", "wall_s", "memory_mb", "disk_mb",
    "pids",
})
RESULT_KEYS = frozenset({
    "status", "summary", "usage", "artifacts", "evidence", "output", "state",
})
ARTIFACT_KEYS = frozenset({"path", "kind", "label"})
EVIDENCE_KEYS = frozenset({"label", "kind"})
SCHEMA_TYPES = frozenset({
    "object", "array", "string", "number", "integer", "boolean", "null",
})
SCHEMA_KEYS = frozenset({
    "type", "properties", "required", "additionalProperties", "items", "enum",
    "minItems", "maxItems", "minLength", "maxLength", "minimum", "maximum",
    "description", "title", "default",
})
APPROVAL_CHOICES = frozenset({"ask", "auto", "deny"})

DEFAULT_RESOURCES = {"memory_mb": 2048, "disk_mb": 4096, "pids": 256,
                     "wall_s": 900}
DEFAULT_BUDGET = {"tokens": 40_000, "model_calls": 0, "cost_usd": 0.0,
                  "wall_s": 900}

_UNSET = object()


class TaskError(ValueError):
    """A task specification, run or evidence record is invalid."""


class TaskSpecError(TaskError):
    """A task specification failed validation."""

    def __init__(self, errors: Sequence[str],
                 warnings: Sequence[str] = ()) -> None:
        self.errors = list(errors)
        self.warnings = list(warnings)
        super().__init__("; ".join(self.errors) or "invalid task specification")


class TaskRunError(TaskError):
    """A run could not be started, read or written."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha256_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def _bounded(value: Any, limit: int = MAX_EVENT_TEXT) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit]


# --------------------------------------------------------------------------- #
# specification types
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class StepBudget:
    tokens: int | None = None
    model_calls: int | None = None
    cost_usd: float | None = None
    wall_s: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "tokens": self.tokens, "model_calls": self.model_calls,
            "cost_usd": self.cost_usd, "wall_s": self.wall_s,
        }


@dataclass(frozen=True)
class Budget:
    tokens: int = DEFAULT_BUDGET["tokens"]
    model_calls: int = DEFAULT_BUDGET["model_calls"]
    cost_usd: float = DEFAULT_BUDGET["cost_usd"]
    wall_s: int = DEFAULT_BUDGET["wall_s"]

    def to_dict(self) -> dict[str, Any]:
        return {
            "tokens": self.tokens, "model_calls": self.model_calls,
            "cost_usd": self.cost_usd, "wall_s": self.wall_s,
        }


@dataclass(frozen=True)
class ResourceSpec:
    memory_mb: int = DEFAULT_RESOURCES["memory_mb"]
    disk_mb: int = DEFAULT_RESOURCES["disk_mb"]
    pids: int = DEFAULT_RESOURCES["pids"]
    wall_s: int = DEFAULT_RESOURCES["wall_s"]

    def to_dict(self) -> dict[str, Any]:
        return {
            "memory_mb": self.memory_mb, "disk_mb": self.disk_mb,
            "pids": self.pids, "wall_s": self.wall_s,
        }


@dataclass(frozen=True)
class WorktreeSpec:
    expected_branch: str | None = None
    base: str = "main"
    require_clean: bool = False
    require_up_to_date: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "expected_branch": self.expected_branch, "base": self.base,
            "require_clean": self.require_clean,
            "require_up_to_date": self.require_up_to_date,
        }


@dataclass(frozen=True)
class Gates:
    before: tuple[str, ...] = ()
    after: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"before": list(self.before), "after": list(self.after)}


@dataclass(frozen=True)
class Approval:
    mutate: str = "ask"
    network: str = "ask"

    def to_dict(self) -> dict[str, Any]:
        return {"mutate": self.mutate, "network": self.network}


@dataclass(frozen=True)
class TaskStep:
    id: str
    tool: str
    goal: str
    paths: tuple[str, ...] = ()
    write_paths: tuple[str, ...] = ()
    budget: StepBudget = field(default_factory=StepBudget)
    args: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "tool": self.tool, "goal": self.goal,
            "paths": list(self.paths), "write_paths": list(self.write_paths),
            "budget": self.budget.to_dict(), "args": dict(self.args),
        }


@dataclass(frozen=True)
class TaskSpec:
    path: Path
    id: str
    version: int
    title: str
    objective: str
    inputs: tuple[str, ...]
    tools: tuple[str, ...]
    write_scope: tuple[str, ...]
    steps: tuple[TaskStep, ...]
    worktree: WorktreeSpec
    resources: ResourceSpec
    budget: Budget
    gates: Gates
    stop: tuple[str, ...]
    output: str
    output_schema: dict[str, Any] | None
    approval: Approval
    requires_graph: bool
    test_policy: dict[str, Any] | None = None
    warnings: tuple[str, ...] = ()

    @property
    def digest(self) -> str:
        return _sha256_text(_canonical(self.to_dict()))

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "version": self.version,
            "title": self.title,
            "objective": self.objective,
            "inputs": list(self.inputs),
            "tools": list(self.tools),
            "write_scope": list(self.write_scope),
            "steps": [step.to_dict() for step in self.steps],
            "worktree": self.worktree.to_dict(),
            "resources": self.resources.to_dict(),
            "budget": self.budget.to_dict(),
            "gates": self.gates.to_dict(),
            "stop": list(self.stop),
            "output": self.output,
            "output_schema": self.output_schema,
            "approval": self.approval.to_dict(),
            "requires_graph": self.requires_graph,
            "test_policy": (dict(self.test_policy)
                            if self.test_policy is not None else None),
        }

    def summary(self) -> dict[str, Any]:
        """Metadata safe to copy into an evidence record."""
        return {
            "id": self.id,
            "version": self.version,
            "title": self.title,
            "digest": self.digest,
            "path": str(self.path),
            "output": self.output,
            "tools": list(self.tools),
            "write_scope": list(self.write_scope),
            "requires_graph": self.requires_graph,
            "gates": self.gates.to_dict(),
            "budget": self.budget.to_dict(),
            "resources": self.resources.to_dict(),
            "approval": self.approval.to_dict(),
            "test_policy": (dict(self.test_policy)
                            if self.test_policy is not None else None),
        }


# --------------------------------------------------------------------------- #
# validation helpers
# --------------------------------------------------------------------------- #
def _text(value: Any, *, where: str, max_len: int = 256,
          allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{where}: must be a string")
    text = value.strip()
    if not text and not allow_empty:
        raise ValueError(f"{where}: must not be empty")
    if len(text) > max_len:
        raise ValueError(f"{where}: must be at most {max_len} characters")
    if any(ord(char) < 32 or ord(char) == 127 for char in text):
        raise ValueError(f"{where}: must not contain control characters")
    if any(0xD800 <= ord(char) <= 0xDFFF for char in text):
        raise ValueError(f"{where}: must be valid Unicode")
    return text


def _identifier(value: Any, *, where: str) -> str:
    text = _text(value, where=where, max_len=64)
    if not NAME_RE.fullmatch(text):
        raise ValueError(
            f"{where}: must match [A-Za-z][A-Za-z0-9_.-]{{0,63}}")
    return text


def _branch(value: Any, *, where: str) -> str:
    text = _text(value, where=where, max_len=255)
    if (not BRANCH_RE.fullmatch(text) or ".." in text
            or "//" in text or "@{" in text or text.endswith(("/", ".", ".lock"))):
        raise ValueError(f"{where}: is not a valid branch name")
    return text


def _integer(value: Any, *, where: str, low: int = 0,
             high: int = 1_000_000_000) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{where}: must be an integer")
    if not low <= value <= high:
        raise ValueError(f"{where}: must be between {low} and {high}")
    return value


def _number(value: Any, *, where: str, low: float = 0.0,
            high: float = 1_000_000_000.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{where}: must be a number")
    try:
        number = float(value)
    except OverflowError:
        raise ValueError(f"{where}: must be a finite number") from None
    if not math.isfinite(number):
        raise ValueError(f"{where}: must be a finite number")
    if not low <= number <= high:
        raise ValueError(f"{where}: must be between {low:g} and {high:g}")
    return number


def _boolean(value: Any, *, where: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{where}: must be true or false")
    return value


def _string_list(value: Any, *, where: str, max_items: int = 100) -> list[str]:
    if isinstance(value, (str, bytes, Mapping)) or not isinstance(
            value, (list, tuple)):
        raise ValueError(f"{where}: must be a list of strings")
    if len(value) > max_items:
        raise ValueError(f"{where}: must contain at most {max_items} entries")
    result: list[str] = []
    for index, item in enumerate(value):
        result.append(_text(item, where=f"{where}[{index}]", max_len=200))
    return result


def _relative(value: Any, *, where: str, allow_root: bool = True) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{where}: must be a string path")
    text = value.strip().replace("\\", "/")
    if not text:
        raise ValueError(f"{where}: must not be empty")
    if any(ord(char) < 32 or ord(char) == 127 for char in text):
        raise ValueError(f"{where}: must not contain control characters")
    if text.startswith("/") or _WINDOWS_ABS.match(text):
        raise ValueError(f"{where}: must be relative to the repository root")
    parts = [part for part in text.split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise ValueError(f"{where}: must stay inside the repository root")
    if not parts:
        if allow_root:
            return "."
        raise ValueError(f"{where}: must name a path inside the repository root")
    return "/".join(parts)


def _scope_list(value: Any, *, where: str, max_items: int = 64) -> list[str]:
    if isinstance(value, (str, bytes, Mapping)) or not isinstance(
            value, (list, tuple)):
        raise ValueError(f"{where}: must be a list of repository-relative paths")
    if len(value) > max_items:
        raise ValueError(f"{where}: must contain at most {max_items} entries")
    result = [
        _relative(item, where=f"{where}[{index}]")
        for index, item in enumerate(value)
    ]
    return list(dict.fromkeys(result))


def _table(value: Any, *, where: str,
           allowed: frozenset[str]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{where}: must be a table")
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValueError(
            f"{where}: unknown key(s): " + ", ".join(str(key) for key in unknown))
    return dict(value)


def _safe(bag: list[str], fn: Any, *args: Any,
          default: Any = None, **kwargs: Any) -> Any:
    try:
        return fn(*args, **kwargs)
    except (TypeError, ValueError) as exc:
        message = str(exc)
        if message not in bag:
            bag.append(message)
        return default


def _check_step_budget(value: Any, *, where: str) -> StepBudget:
    table = _table(value, where=where, allowed=STEP_BUDGET_KEYS)
    return StepBudget(
        tokens=(_integer(table["tokens"], where=f"{where}.tokens",
                         low=0) if "tokens" in table else None),
        model_calls=(_integer(table["model_calls"],
                              where=f"{where}.model_calls", low=0)
                     if "model_calls" in table else None),
        cost_usd=(_number(table["cost_usd"], where=f"{where}.cost_usd")
                  if "cost_usd" in table else None),
        wall_s=(_integer(table["wall_s"], where=f"{where}.wall_s", low=1)
                if "wall_s" in table else None),
    )


def _check_steps(value: Any, *, errors: list[str],
                 warnings: list[str], tools: Sequence[str],
                 write_scope: Sequence[str],
                 budget: Budget) -> tuple[TaskStep, ...]:
    if not isinstance(value, list):
        errors.append("steps: must be an array of tables")
        return ()
    if not 1 <= len(value) <= MAX_STEPS:
        errors.append(f"steps: must contain between 1 and {MAX_STEPS} entries")
        return ()
    steps: list[TaskStep] = []
    seen: set[str] = set()
    for index, raw in enumerate(value):
        where = f"steps[{index}]"
        if not isinstance(raw, Mapping):
            errors.append(f"{where}: must be a table")
            continue
        table = _safe(errors, _table, raw, where=where, allowed=STEP_KEYS,
                      default={}) or {}
        step_id = _safe(errors, _identifier, table.get("id"),
                        where=f"{where}.id", default="")
        if step_id and step_id in seen:
            errors.append(f"{where}.id: duplicate step id {step_id!r}")
        if step_id:
            seen.add(step_id)
        tool = _safe(errors, _text, table.get("tool"),
                     where=f"{where}.tool", max_len=64, default="")
        if tool and tool not in KNOWN_TOOLS:
            errors.append(f"{where}.tool: unknown tool {tool!r}")
        elif tool and tool not in tools:
            errors.append(
                f"{where}.tool: {tool!r} is not in the task's tools allowlist")
        goal = _safe(errors, _text, table.get("goal"),
                     where=f"{where}.goal", max_len=500, default="")
        paths = _safe(errors, _scope_list, table.get("paths", []),
                      where=f"{where}.paths", default=[]) or []
        write_paths = _safe(errors, _scope_list,
                            table.get("write_paths", []),
                            where=f"{where}.write_paths", default=[]) or []
        if write_paths:
            if tool and tool not in MUTATING_TOOLS:
                warnings.append(
                    f"{where}: write_paths is ignored for read-only tool "
                    f"{tool!r}")
            for path in write_paths:
                if not _in_scope(path, write_scope):
                    errors.append(
                        f"{where}.write_paths: {path!r} is outside write_scope")
        if tool in MUTATING_TOOLS and not write_scope:
            errors.append(
                f"{where}.tool: mutating tool {tool!r} requires a write_scope")
        step_budget = _safe(errors, _check_step_budget,
                            table.get("budget", {}), where=f"{where}.budget",
                            default=StepBudget()) or StepBudget()
        if step_budget.tokens is not None and step_budget.tokens > budget.tokens:
            errors.append(
                f"{where}.budget.tokens: exceeds the task token budget")
        if (step_budget.model_calls is not None
                and step_budget.model_calls > budget.model_calls):
            errors.append(
                f"{where}.budget.model_calls: exceeds the task model-call "
                "budget")
        if (step_budget.cost_usd is not None
                and step_budget.cost_usd > budget.cost_usd):
            errors.append(
                f"{where}.budget.cost_usd: exceeds the task cost budget")
        if (step_budget.wall_s is not None
                and step_budget.wall_s > budget.wall_s):
            errors.append(
                f"{where}.budget.wall_s: exceeds the task wall budget")
        args = table.get("args", {})
        if not isinstance(args, Mapping):
            errors.append(f"{where}.args: must be a table")
            args = {}
        try:
            _canonical(dict(args))
        except (TypeError, ValueError) as exc:
            errors.append(f"{where}.args: must be JSON-serializable ({exc})")
        steps.append(TaskStep(
            id=step_id, tool=tool, goal=goal, paths=tuple(paths),
            write_paths=tuple(write_paths), budget=step_budget,
            args=dict(args),
        ))
    return tuple(steps)


def _check_schema(schema: Any, *, where: str,
                  errors: list[str]) -> dict[str, Any] | None:
    if not isinstance(schema, Mapping):
        errors.append(f"{where}: must be a table")
        return None
    unknown = sorted(set(schema) - SCHEMA_KEYS)
    if unknown:
        errors.append(
            f"{where}: unsupported schema keyword(s): "
            + ", ".join(str(key) for key in unknown))
    raw_type = schema.get("type")
    types: list[str] = []
    if raw_type is not None:
        if isinstance(raw_type, str):
            types = [raw_type]
        elif isinstance(raw_type, list) and raw_type:
            types = list(raw_type)
        else:
            errors.append(f"{where}.type: must be a string or a non-empty list")
        for item in types:
            if not isinstance(item, str) or item not in SCHEMA_TYPES:
                errors.append(
                    f"{where}.type: unsupported type {item!r}; expected one of "
                    + ", ".join(sorted(SCHEMA_TYPES)))
    properties = schema.get("properties")
    if properties is not None:
        if not isinstance(properties, Mapping):
            errors.append(f"{where}.properties: must be a table")
        else:
            for name, subschema in properties.items():
                if not isinstance(name, str) or not name:
                    errors.append(f"{where}.properties: keys must be names")
                    continue
                _check_schema(subschema, where=f"{where}.properties.{name}",
                              errors=errors)
    required = schema.get("required")
    if required is not None:
        if (not isinstance(required, list)
                or not all(isinstance(item, str) and item for item in required)):
            errors.append(f"{where}.required: must be a list of property names")
        elif len(set(required)) != len(required):
            errors.append(f"{where}.required: must not contain duplicates")
    additional = schema.get("additionalProperties")
    if additional is not None and not isinstance(additional, bool):
        errors.append(f"{where}.additionalProperties: must be true or false")
    items = schema.get("items")
    if items is not None:
        _check_schema(items, where=f"{where}.items", errors=errors)
    enum = schema.get("enum")
    if enum is not None:
        if not isinstance(enum, list) or not enum:
            errors.append(f"{where}.enum: must be a non-empty list")
        else:
            try:
                values = {_canonical(item) for item in enum}
            except (TypeError, ValueError):
                errors.append(f"{where}.enum: values must be JSON-serializable")
                values = set()
            if len(values) != len(enum):
                errors.append(f"{where}.enum: must not contain duplicates")
    for key in ("minItems", "maxItems", "minLength", "maxLength"):
        if key in schema:
            _safe(errors, _integer, schema[key], where=f"{where}.{key}", low=0)
    for key in ("minimum", "maximum"):
        if key in schema:
            _safe(errors, _number, schema[key], where=f"{where}.{key}",
                  low=-1e18, high=1e18)
    minimum = schema.get("minimum")
    maximum = schema.get("maximum")
    if (isinstance(minimum, (int, float)) and not isinstance(minimum, bool)
            and isinstance(maximum, (int, float)) and not isinstance(maximum, bool)
            and minimum > maximum):
        errors.append(f"{where}: minimum must not exceed maximum")
    return dict(schema)


def _check_output_schema(value: Any, *,
                         errors: list[str]) -> dict[str, Any] | None:
    return _check_schema(value, where="output_schema", errors=errors)


def _check_gates(value: Any, *, errors: list[str]) -> Gates:
    table = _table(value, where="gates", allowed=GATE_KEYS)
    before = _string_list(table.get("before", []), where="gates.before") or []
    after = _string_list(table.get("after", []), where="gates.after") or []
    for phase, names, allowed in (
            ("before", before, BEFORE_GATES), ("after", after, AFTER_GATES)):
        if len(set(names)) != len(names):
            errors.append(f"gates.{phase}: must not contain duplicates")
        for name in names:
            if name not in KNOWN_GATES:
                errors.append(f"gates.{phase}: unknown gate {name!r}")
            elif name not in allowed:
                errors.append(
                    f"gates.{phase}: gate {name!r} is only valid in the "
                    f"{'after' if phase == 'before' else 'before'} phase")
    return Gates(before=tuple(before), after=tuple(after))


def _check_worktree(value: Any, *, errors: list[str]) -> WorktreeSpec:
    table = _table(value, where="worktree", allowed=WORKTREE_KEYS)
    expected = None
    if "expected_branch" in table and table["expected_branch"] is not None:
        expected = _safe(errors, _branch, table["expected_branch"],
                         where="worktree.expected_branch")
    base = _safe(errors, _branch, table.get("base", "main"),
                 where="worktree.base", default="main")
    require_clean = _safe(errors, _boolean,
                          table.get("require_clean", False),
                          where="worktree.require_clean", default=False)
    require_up = _safe(errors, _boolean,
                       table.get("require_up_to_date", False),
                       where="worktree.require_up_to_date", default=False)
    return WorktreeSpec(expected_branch=expected, base=base or "main",
                        require_clean=bool(require_clean),
                        require_up_to_date=bool(require_up))


def _check_resources(value: Any, *, errors: list[str]) -> ResourceSpec:
    table = _table(value, where="resources", allowed=RESOURCE_KEYS)
    values: dict[str, int] = {}
    for key, default in DEFAULT_RESOURCES.items():
        values[key] = _safe(errors, _integer, table.get(key, default),
                            where=f"resources.{key}", low=1,
                            default=default) or default
    return ResourceSpec(**values)


def _check_budget(value: Any, *, errors: list[str]) -> Budget:
    table = _table(value, where="budget", allowed=BUDGET_KEYS)
    tokens = _safe(errors, _integer, table.get("tokens", DEFAULT_BUDGET["tokens"]),
                   where="budget.tokens", low=0,
                   default=DEFAULT_BUDGET["tokens"])
    model_calls = _safe(
        errors, _integer, table.get("model_calls", DEFAULT_BUDGET["model_calls"]),
        where="budget.model_calls", low=0,
        default=DEFAULT_BUDGET["model_calls"])
    cost = _safe(errors, _number,
                 table.get("cost_usd", DEFAULT_BUDGET["cost_usd"]),
                 where="budget.cost_usd", default=DEFAULT_BUDGET["cost_usd"])
    wall = _safe(errors, _integer, table.get("wall_s", DEFAULT_BUDGET["wall_s"]),
                 where="budget.wall_s", low=1,
                 default=DEFAULT_BUDGET["wall_s"])
    return Budget(tokens=int(tokens), model_calls=int(model_calls),
                  cost_usd=float(cost), wall_s=int(wall))


def _check_approval(value: Any, *, errors: list[str]) -> Approval:
    table = _table(value, where="approval", allowed=APPROVAL_KEYS)
    choices: dict[str, str] = {}
    for key in ("mutate", "network"):
        raw = table.get(key, "ask")
        if not isinstance(raw, str) or raw not in APPROVAL_CHOICES:
            errors.append(
                f"approval.{key}: must be one of "
                + ", ".join(sorted(APPROVAL_CHOICES)))
            choices[key] = "ask"
        else:
            choices[key] = raw
    return Approval(mutate=choices["mutate"], network=choices["network"])


def _check_test_policy(value: Any, *, errors: list[str]) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, str):
        table: dict[str, Any] = {"mode": value}
    elif isinstance(value, Mapping):
        table = _safe(errors, _table, value, where="test_policy",
                      allowed=TEST_POLICY_KEYS, default={}) or {}
    else:
        errors.append("test_policy: must be a mode string or a table")
        return None
    cleaned: dict[str, Any] = {}
    if "mode" in table:
        mode = _safe(errors, _text, table["mode"], where="test_policy.mode",
                     max_len=32, default="")
        if mode and mode not in test_plans.MODES:
            errors.append(
                "test_policy.mode: must be one of "
                + ", ".join(test_plans.MODES))
        elif mode:
            cleaned["mode"] = mode
    if "fallback" in table:
        fallback = _safe(errors, _text, table["fallback"],
                         where="test_policy.fallback", max_len=32, default="")
        if fallback and fallback not in test_plans.FALLBACKS:
            errors.append(
                "test_policy.fallback: must be one of "
                + ", ".join(test_plans.FALLBACKS))
        elif fallback:
            cleaned["fallback"] = fallback
    if "max_tests" in table:
        max_tests = _safe(errors, _integer, table["max_tests"],
                          where="test_policy.max_tests", low=1,
                          high=100_000)
        if max_tests is not None:
            cleaned["max_tests"] = int(max_tests)
    if "timeout_s" in table:
        timeout_s = _safe(errors, _integer, table["timeout_s"],
                          where="test_policy.timeout_s", low=1,
                          high=86_400)
        if timeout_s is not None:
            cleaned["timeout_s"] = int(timeout_s)
    return cleaned or None


def _check_output_name(value: Any, *, errors: list[str]) -> str:
    return _safe(errors, _text, value, where="output", max_len=120,
                 default="") or ""


def _check_inputs(value: Any, *, errors: list[str]) -> tuple[str, ...]:
    names = _string_list(value, where="inputs") or []
    seen: set[str] = set()
    clean: list[str] = []
    for name in names:
        if not NAME_RE.fullmatch(name):
            errors.append(
                f"inputs: {name!r} must match [A-Za-z][A-Za-z0-9_.-]{{0,63}}")
            continue
        if name in seen:
            errors.append(f"inputs: duplicate input {name!r}")
            continue
        seen.add(name)
        clean.append(name)
    return tuple(clean)


def _check_tools(value: Any, *, errors: list[str]) -> tuple[str, ...]:
    names = _string_list(value, where="tools", max_items=32) or []
    if not names:
        errors.append("tools: must contain at least one tool")
    clean: list[str] = []
    for name in names:
        if name not in KNOWN_TOOLS:
            errors.append(f"tools: unknown tool {name!r}")
            continue
        if name in clean:
            errors.append(f"tools: duplicate tool {name!r}")
            continue
        clean.append(name)
    return tuple(clean)


def _in_scope(path: str, scope: Sequence[str]) -> bool:
    if path == ".":
        return "." in scope
    for entry in scope:
        if entry == ".":
            return True
        if path == entry or path.startswith(entry + "/"):
            return True
    return False


def _check_stop(value: Any, *, errors: list[str],
                warnings: list[str]) -> tuple[str, ...]:
    if value is None:
        return MANDATORY_STOPS
    names = _string_list(value, where="stop") or []
    for name in names:
        if name not in STOP_CONDITIONS:
            errors.append(f"stop: unknown stop condition {name!r}")
    if len(set(names)) != len(names):
        errors.append("stop: must not contain duplicates")
    missing = [name for name in MANDATORY_STOPS if name not in names]
    if missing:
        warnings.append(
            "stop: conditions " + ", ".join(missing)
            + " are always enforced even though they are not listed")
    return tuple(dict.fromkeys(names))


# --------------------------------------------------------------------------- #
# specification loading
# --------------------------------------------------------------------------- #
def _parse_spec(data: Any, path: Path) -> TaskSpec:
    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(data, Mapping):
        raise TaskSpecError(
            ["specification must be a TOML/JSON table"], warnings)
    unknown = sorted(set(data) - TOP_KEYS)
    if unknown:
        errors.append("spec: unknown key(s): " + ", ".join(unknown))

    spec_id = ""
    raw_id = data.get("id")
    if raw_id is None:
        errors.append("id: is required")
    elif not isinstance(raw_id, str) or not SPEC_ID_RE.fullmatch(raw_id.strip()):
        errors.append(
            "id: must match [A-Za-z][A-Za-z0-9_.-]{0,63}")
    else:
        spec_id = raw_id.strip()
    version = _safe(errors, _integer, data.get("version", 1),
                    where="version", low=1, high=1_000_000, default=1)

    objective = _safe(errors, _text, data.get("objective"),
                      where="objective", max_len=4000, default="") or ""
    if "title" in data:
        title = _safe(errors, _text, data["title"], where="title",
                      max_len=200, default="") or ""
    else:
        title = objective[:200]

    inputs = _safe(errors, _check_inputs, data.get("inputs", []),
                   errors=errors, default=()) or ()
    tools = _safe(errors, _check_tools, data.get("tools", []),
                  errors=errors, default=()) or ()
    write_scope = _safe(errors, _scope_list, data.get("write_scope", []),
                        where="write_scope", default=[]) or []
    budget = _safe(errors, _check_budget, data.get("budget", {}),
                   errors=errors, default=Budget()) or Budget()
    resources_spec = _safe(errors, _check_resources, data.get("resources", {}),
                           errors=errors,
                           default=ResourceSpec()) or ResourceSpec()
    steps = _check_steps(data.get("steps"), errors=errors, warnings=warnings,
                         tools=tools, write_scope=write_scope, budget=budget)
    worktree = _safe(errors, _check_worktree, data.get("worktree", {}),
                     errors=errors, default=WorktreeSpec()) or WorktreeSpec()
    gates = _safe(errors, _check_gates, data.get("gates", {}),
                  errors=errors, default=Gates()) or Gates()
    stop = _safe(errors, _check_stop, data.get("stop"), errors=errors,
                 warnings=warnings, default=MANDATORY_STOPS) or MANDATORY_STOPS
    output = _check_output_name(data.get("output"), errors=errors)
    schema = None
    if "output_schema" in data:
        schema = _check_output_schema(data["output_schema"], errors=errors)
    approval = _safe(errors, _check_approval, data.get("approval", {}),
                     errors=errors, default=Approval()) or Approval()
    test_policy = _safe(errors, _check_test_policy, data.get("test_policy"),
                        errors=errors)
    requires_graph = _safe(errors, _boolean,
                           data.get("requires_graph", False),
                           where="requires_graph", default=False)
    if set(tools) & GRAPH_TOOLS:
        if not requires_graph:
            warnings.append(
                "requires_graph: graph tools are declared; the graph snapshot "
                "is required even though requires_graph is false")
        requires_graph = True
    if requires_graph and "snapshot_fresh" not in gates.before:
        warnings.append(
            "gates.before: requires_graph is set but snapshot_fresh is not a "
            "preflight gate; preflight still checks freshness")
    if budget.wall_s > resources_spec.wall_s:
        warnings.append(
            "budget.wall_s: exceeds resources.wall_s; the resource limit is "
            "the shorter wall-clock cap")
    if "model" in tools and budget.model_calls == 0:
        warnings.append(
            "budget.model_calls: a model step is declared but the model-call "
            "budget is zero")
    if any(step.tool in MUTATING_TOOLS for step in steps):
        if approval.mutate != "auto":
            warnings.append(
                "approval.mutate: mutating steps stop for approval unless "
                "the run is explicitly approved")
    used_tools = {step.tool for step in steps}
    unused = [name for name in tools if name not in used_tools]
    if unused:
        warnings.append("tools: unused allowlist entries: " + ", ".join(unused))
    if schema is None:
        warnings.append(
            "output_schema: no output schema is declared; only JSON "
            "serializability is checked")

    if errors:
        raise TaskSpecError(errors, warnings)
    return TaskSpec(
        path=path.resolve(),
        id=spec_id,
        version=int(version),
        title=title,
        objective=objective,
        inputs=inputs,
        tools=tools,
        write_scope=tuple(write_scope),
        steps=steps,
        worktree=worktree,
        resources=resources_spec,
        budget=budget,
        gates=gates,
        stop=stop,
        output=output,
        output_schema=schema,
        approval=approval,
        requires_graph=requires_graph,
        test_policy=test_policy,
        warnings=tuple(warnings),
    )


def load_spec(path: str | Path) -> TaskSpec:
    """Load and validate a TOML or JSON task specification."""
    spec_path = Path(path).expanduser()
    if not spec_path.is_file():
        raise TaskSpecError([f"{spec_path}: no such specification file"])
    suffix = spec_path.suffix.lower()
    if suffix not in {".toml", ".json"}:
        raise TaskSpecError(
            [f"{spec_path}: specification must be .toml or .json"])
    try:
        raw = spec_path.read_bytes()
    except OSError as exc:
        raise TaskSpecError([f"{spec_path}: {exc}"]) from exc
    if len(raw) > MAX_SPEC_BYTES:
        raise TaskSpecError(
            [f"{spec_path}: specification exceeds {MAX_SPEC_BYTES} bytes"])
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise TaskSpecError([f"{spec_path}: not UTF-8: {exc}"]) from exc
    if suffix == ".json":
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise TaskSpecError([f"{spec_path}: invalid JSON: {exc}"]) from exc
    else:
        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            raise TaskSpecError([f"{spec_path}: invalid TOML: {exc}"]) from exc
    return _parse_spec(data, spec_path)


def validate_spec(path: str | Path) -> dict[str, Any]:
    """Validate a specification without running it.

    Returns ``ok``, ``errors``, ``warnings`` and (when valid) a summary. This
    never touches the repository, the state directory or the network.
    """
    spec_path = Path(path).expanduser()
    try:
        spec = load_spec(spec_path)
    except TaskSpecError as exc:
        return {
            "schema": SCHEMA,
            "path": str(spec_path),
            "ok": False,
            "errors": list(exc.errors),
            "warnings": list(exc.warnings),
            "spec": None,
        }
    return {
        "schema": SCHEMA,
        "path": str(spec.path),
        "ok": True,
        "errors": [],
        "warnings": list(spec.warnings),
        "spec": spec.summary(),
    }


# --------------------------------------------------------------------------- #
# output schema checks
# --------------------------------------------------------------------------- #
def _type_matches(value: Any, kind: str) -> bool:
    if kind == "object":
        return isinstance(value, Mapping)
    if kind == "array":
        return isinstance(value, list)
    if kind == "string":
        return isinstance(value, str)
    if kind == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "number":
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return False
        try:
            return math.isfinite(float(value))
        except OverflowError:
            return False
    if kind == "boolean":
        return isinstance(value, bool)
    if kind == "null":
        return value is None
    return False


def _check_value(value: Any, schema: Mapping[str, Any], *, where: str,
                 errors: list[str]) -> None:
    if len(errors) >= 50:
        return
    raw_type = schema.get("type")
    if raw_type is not None:
        kinds = [raw_type] if isinstance(raw_type, str) else list(raw_type)
        if not any(_type_matches(value, kind) for kind in kinds):
            errors.append(
                f"{where}: expected {' or '.join(kinds)}, got "
                f"{type(value).__name__}")
            return
    enum = schema.get("enum")
    if isinstance(enum, list):
        try:
            if _canonical(value) not in {_canonical(item) for item in enum}:
                errors.append(f"{where}: value is not in the declared enum")
        except (TypeError, ValueError):
            errors.append(f"{where}: value is not JSON-serializable")
    if isinstance(value, Mapping):
        properties = schema.get("properties") or {}
        required = schema.get("required") or []
        if isinstance(required, list):
            for name in required:
                if name not in value:
                    errors.append(f"{where}: missing required property {name!r}")
        if isinstance(properties, Mapping):
            for name, subschema in properties.items():
                if name in value and isinstance(subschema, Mapping):
                    _check_value(value[name], subschema,
                                 where=f"{where}.{name}", errors=errors)
        if schema.get("additionalProperties") is False:
            extra = sorted(set(value) - set(properties))
            if extra:
                errors.append(
                    f"{where}: unexpected propert"
                    f"{'y' if len(extra) == 1 else 'ies'}: "
                    + ", ".join(str(name) for name in extra))
    if isinstance(value, list):
        items = schema.get("items")
        if isinstance(items, Mapping):
            for index, item in enumerate(value):
                _check_value(item, items, where=f"{where}[{index}]",
                             errors=errors)
                if len(errors) >= 50:
                    break
        minimum = schema.get("minItems")
        maximum = schema.get("maxItems")
        if isinstance(minimum, int) and len(value) < minimum:
            errors.append(f"{where}: has fewer than {minimum} items")
        if isinstance(maximum, int) and len(value) > maximum:
            errors.append(f"{where}: has more than {maximum} items")
    if isinstance(value, str):
        minimum = schema.get("minLength")
        maximum = schema.get("maxLength")
        if isinstance(minimum, int) and len(value) < minimum:
            errors.append(f"{where}: is shorter than {minimum} characters")
        if isinstance(maximum, int) and len(value) > maximum:
            errors.append(f"{where}: is longer than {maximum} characters")
    if (isinstance(value, (int, float)) and not isinstance(value, bool)):
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if isinstance(minimum, (int, float)) and value < minimum:
            errors.append(f"{where}: is below the minimum {minimum}")
        if isinstance(maximum, (int, float)) and value > maximum:
            errors.append(f"{where}: is above the maximum {maximum}")


def validate_output(value: Any, schema: Mapping[str, Any] | None) -> list[str]:
    """Check an output value against the minimal JSON-schema subset."""
    errors: list[str] = []
    if schema is None:
        return errors
    _check_value(value, schema, where="output", errors=errors)
    return errors


# --------------------------------------------------------------------------- #
# run storage
# --------------------------------------------------------------------------- #
def tasks_dir(root: str | Path, state_dir: str | Path | None = None) -> Path:
    """``<state>/tasks`` for a repository (state defaults to ``.ozagia``)."""
    base = (Path(state_dir).expanduser().resolve()
            if state_dir is not None else default_state_dir(root))
    return base / TASKS_DIRNAME


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        raise TaskRunError(f"cannot read {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise TaskRunError(f"{path}: invalid JSON: {exc}") from exc


def _write_json(path: Path, value: Any) -> None:
    text = json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False,
                      allow_nan=False) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def _read_jsonl(path: Path, *, limit: int = MAX_SHOW_EVENTS) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return []
    records: list[dict[str, Any]] = []
    for line in lines[-limit:]:
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            records.append(item)
    return records


def list_runs(root: str | Path, *, state_dir: str | Path | None = None,
              limit: int = 50) -> list[dict[str, Any]]:
    """Run summaries, newest first. Corrupt runs are reported, not skipped."""
    base = tasks_dir(root, state_dir)
    if not base.is_dir():
        return []
    runs: list[dict[str, Any]] = []
    for child in base.iterdir():
        if not child.is_dir() or not RUN_ID_RE.fullmatch(child.name):
            continue
        manifest_path = child / MANIFEST_FILE
        if not manifest_path.is_file():
            runs.append({
                "run_id": child.name, "state": "incomplete",
                "error": f"{MANIFEST_FILE} is missing",
            })
            continue
        try:
            manifest = _read_json(manifest_path)
        except TaskRunError as exc:
            runs.append({"run_id": child.name, "state": "invalid",
                         "error": str(exc)})
            continue
        if not isinstance(manifest, Mapping):
            runs.append({"run_id": child.name, "state": "invalid",
                         "error": "manifest is not an object"})
            continue
        spec = manifest.get("spec") or {}
        budget = (manifest.get("budget") or {}).get("used") or {}
        output = manifest.get("output") or {}
        runs.append({
            "run_id": manifest.get("run_id") or child.name,
            "state": "complete",
            "spec_id": spec.get("id"),
            "spec_version": spec.get("version"),
            "status": manifest.get("status"),
            "stop": manifest.get("stop"),
            "started_at": manifest.get("started_at"),
            "finished_at": manifest.get("finished_at"),
            "duration_s": manifest.get("duration_s"),
            "steps": len(manifest.get("steps") or []),
            "artifacts": len(manifest.get("artifacts") or []),
            "evidence_count": manifest.get("evidence_count"),
            "output": output.get("name"),
            "output_digest": output.get("digest"),
            "tokens": budget.get("tokens"),
            "wall_s": budget.get("wall_s"),
        })
    runs.sort(key=lambda item: (str(item.get("started_at") or ""),
                                str(item.get("run_id"))), reverse=True)
    return runs[:max(0, min(int(limit), 1000))]


def show_run(root: str | Path, run_id: str, *,
             state_dir: str | Path | None = None) -> dict[str, Any]:
    """One run's manifest, artifacts, events and output value."""
    if not isinstance(run_id, str) or not RUN_ID_RE.fullmatch(run_id):
        raise TaskRunError("run_id must match [A-Za-z0-9][A-Za-z0-9_.-]{0,119}")
    run_dir = tasks_dir(root, state_dir) / run_id
    if not run_dir.is_dir():
        raise TaskRunError(f"no task run {run_id!r} under {tasks_dir(root, state_dir)}")
    result: dict[str, Any] = {
        "schema": SCHEMA,
        "run_id": run_id,
        "run_dir": str(run_dir),
        "state": "incomplete",
        "manifest": None,
        "artifacts": [],
        "events": _read_jsonl(run_dir / EVENTS_FILE),
        "output": None,
        "errors": [],
    }
    manifest_path = run_dir / MANIFEST_FILE
    if manifest_path.is_file():
        try:
            manifest = _read_json(manifest_path)
        except TaskRunError as exc:
            result["state"] = "invalid"
            result["errors"].append(str(exc))
        else:
            if isinstance(manifest, Mapping):
                result["manifest"] = manifest
                result["state"] = "complete"
            else:
                result["state"] = "invalid"
                result["errors"].append("manifest is not an object")
    artifacts_path = run_dir / ARTIFACTS_FILE
    if artifacts_path.is_file():
        try:
            artifacts = _read_json(artifacts_path)
        except TaskRunError as exc:
            result["errors"].append(str(exc))
        else:
            if isinstance(artifacts, Mapping):
                result["artifacts"] = list(artifacts.get("artifacts") or [])
    output_path = run_dir / OUTPUT_FILE
    if output_path.is_file():
        try:
            if output_path.stat().st_size <= MAX_OUTPUT_BYTES:
                result["output"] = _read_json(output_path)
            else:
                result["errors"].append(
                    f"{OUTPUT_FILE} exceeds {MAX_OUTPUT_BYTES} bytes")
        except OSError as exc:
            result["errors"].append(str(exc))
        except TaskRunError as exc:
            result["errors"].append(str(exc))
    return result


# --------------------------------------------------------------------------- #
# evidence writer
# --------------------------------------------------------------------------- #
class _EventLog:
    """Append-only, size-bounded JSONL evidence events."""

    def __init__(self, path: Path, run_id: str) -> None:
        self.path = path
        self.run_id = run_id
        self.seq = 0
        self.bytes = 0
        self.truncated = False
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()

    def write(self, event_type: str, **fields: Any) -> None:
        if self.bytes >= MAX_EVENT_BYTES:
            self.truncated = True
            return
        self.seq += 1
        event: dict[str, Any] = {
            "schema": SCHEMA,
            "seq": self.seq,
            "ts": _now(),
            "run_id": self.run_id,
            "type": event_type,
        }
        event.update(fields)
        line = json.dumps(event, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False) + "\n"
        try:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line)
        except OSError:
            return
        self.bytes += len(line.encode("utf-8"))


# --------------------------------------------------------------------------- #
# budget accounting
# --------------------------------------------------------------------------- #
class _BudgetLedger:
    def __init__(self, budget: Budget, resources_spec: ResourceSpec) -> None:
        self.budget = budget
        self.resources = resources_spec
        self.tokens = 0
        self.model_calls = 0
        self.cost_usd = 0.0
        self.step_wall_s = 0.0
        self.wall_s = 0.0
        self.peaks: dict[str, float | int | None] = {
            "memory_mb": None, "disk_mb": None, "pids": None,
        }

    def record(self, usage: Mapping[str, Any], elapsed_s: float,
               total_elapsed_s: float) -> None:
        self.tokens += int(usage.get("tokens") or 0)
        self.model_calls += int(usage.get("model_calls") or 0)
        self.cost_usd += float(usage.get("cost_usd") or 0.0)
        reported_wall = usage.get("wall_s")
        self.step_wall_s += max(float(elapsed_s),
                                float(reported_wall or 0.0))
        self.wall_s = max(self.step_wall_s, float(total_elapsed_s))
        for key in ("memory_mb", "disk_mb", "pids"):
            value = usage.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                previous = self.peaks[key]
                self.peaks[key] = (value if previous is None
                                   else max(previous, value))

    def set_total_wall(self, seconds: float) -> None:
        self.wall_s = max(self.step_wall_s, float(seconds))

    def used(self) -> dict[str, Any]:
        return {
            "tokens": self.tokens,
            "model_calls": self.model_calls,
            "cost_usd": round(self.cost_usd, 6),
            "wall_s": round(self.wall_s, 3),
            "memory_mb": self.peaks["memory_mb"],
            "disk_mb": self.peaks["disk_mb"],
            "pids": self.peaks["pids"],
        }

    def remaining(self) -> dict[str, Any]:
        wall_limit = min(
            [value for value in (self.budget.wall_s, self.resources.wall_s)
             if value] or [0]
        )
        return {
            "tokens": max(0, self.budget.tokens - self.tokens),
            "model_calls": max(0, self.budget.model_calls - self.model_calls),
            "cost_usd": round(max(0.0, self.budget.cost_usd - self.cost_usd), 6),
            "wall_s": round(max(0.0, wall_limit - self.wall_s), 3),
            "memory_mb": _remaining_limit(self.resources.memory_mb,
                                          self.peaks["memory_mb"]),
            "disk_mb": _remaining_limit(self.resources.disk_mb,
                                        self.peaks["disk_mb"]),
            "pids": _remaining_limit(self.resources.pids, self.peaks["pids"]),
        }

    def exceeded(self) -> dict[str, Any] | None:
        resource_limits: list[str] = []
        budget_limits: list[str] = []
        if self.tokens > self.budget.tokens:
            budget_limits.append(
                f"tokens {self.tokens} > {self.budget.tokens}")
        if self.model_calls > self.budget.model_calls:
            budget_limits.append(
                f"model calls {self.model_calls} > {self.budget.model_calls}")
        if self.cost_usd > self.budget.cost_usd:
            budget_limits.append(
                f"cost ${self.cost_usd:.4f} > ${self.budget.cost_usd:.4f}")
        if self.wall_s > self.budget.wall_s:
            budget_limits.append(
                f"wall {self.wall_s:.1f}s > {self.budget.wall_s}s")
        if self.wall_s > self.resources.wall_s:
            resource_limits.append(
                f"wall {self.wall_s:.1f}s > {self.resources.wall_s}s")
        for key, label in (("memory_mb", "memory"), ("disk_mb", "disk"),
                           ("pids", "pids")):
            peak = self.peaks[key]
            limit = getattr(self.resources, key)
            if isinstance(peak, (int, float)) and peak > limit:
                resource_limits.append(
                    f"{label} {peak:g} > {limit}")
        if resource_limits:
            return {"stop": "resource_limit", "limits": resource_limits,
                    "detail": "resource limit exceeded: "
                              + "; ".join(resource_limits)}
        if budget_limits:
            return {"stop": "budget_exhausted", "limits": budget_limits,
                    "detail": "budget exhausted: " + "; ".join(budget_limits)}
        return None

    def report(self) -> dict[str, Any]:
        return {
            "declared": self.budget.to_dict(),
            "resources": self.resources.to_dict(),
            "used": self.used(),
            "remaining": self.remaining(),
        }


def _remaining_limit(limit: int, peak: float | int | None) -> float | None:
    if not isinstance(peak, (int, float)):
        return None
    return round(max(0.0, float(limit) - float(peak)), 2)


# --------------------------------------------------------------------------- #
# executor result normalisation
# --------------------------------------------------------------------------- #
def _usage(value: Any) -> dict[str, Any]:
    if value is None:
        value = {}
    if not isinstance(value, Mapping):
        raise TaskError("executor usage must be a table")
    unknown = sorted(set(value) - USAGE_KEYS)
    if unknown:
        raise TaskError(
            "executor usage has unsupported key(s): "
            + ", ".join(str(key) for key in unknown))
    result: dict[str, Any] = {
        "tokens": 0, "model_calls": 0, "cost_usd": 0.0,
    }
    for key in ("tokens", "model_calls"):
        if key in value:
            result[key] = _integer(value[key], where=f"usage.{key}", low=0)
    if "cost_usd" in value:
        result["cost_usd"] = _number(value["cost_usd"], where="usage.cost_usd")
    for key in ("wall_s", "memory_mb", "disk_mb"):
        if key in value:
            result[key] = _number(value[key], where=f"usage.{key}")
    if "pids" in value:
        result["pids"] = _integer(value["pids"], where="usage.pids", low=0)
    return result


def _artifact_specs(value: Any) -> list[dict[str, str]]:
    if value is None:
        return []
    if isinstance(value, (str, bytes, Mapping)) or not isinstance(value, list):
        raise TaskError("executor artifacts must be a list")
    if len(value) > MAX_STEP_ARTIFACTS:
        raise TaskError(
            f"executor returned more than {MAX_STEP_ARTIFACTS} artifacts")
    result: list[dict[str, str]] = []
    for index, item in enumerate(value):
        if isinstance(item, str):
            result.append({"path": item, "kind": "", "label": ""})
            continue
        if not isinstance(item, Mapping):
            raise TaskError(
                f"executor artifacts[{index}] must be a path or table")
        unknown = sorted(set(item) - ARTIFACT_KEYS)
        if unknown:
            raise TaskError(
                f"executor artifacts[{index}] has unsupported key(s): "
                + ", ".join(str(key) for key in unknown))
        path = item.get("path")
        if not isinstance(path, str) or not path:
            raise TaskError(
                f"executor artifacts[{index}].path must be a string")
        result.append({
            "path": path,
            "kind": _bounded(item.get("kind", ""), 80),
            "label": _bounded(item.get("label", ""), 120),
        })
    return result


def _evidence(value: Any) -> list[dict[str, str]]:
    if value is None:
        return []
    if isinstance(value, (str, bytes, Mapping)):
        value = [value]
    if not isinstance(value, list):
        raise TaskError("executor evidence must be a list")
    if len(value) > MAX_EVIDENCE_PER_STEP:
        raise TaskError(
            f"executor returned more than {MAX_EVIDENCE_PER_STEP} evidence "
            "labels")
    result: list[dict[str, str]] = []
    for index, item in enumerate(value):
        if isinstance(item, str):
            label = item
            kind = ""
        elif isinstance(item, Mapping):
            unknown = sorted(set(item) - EVIDENCE_KEYS)
            if unknown:
                raise TaskError(
                    f"executor evidence[{index}] has unsupported key(s): "
                    + ", ".join(str(key) for key in unknown))
            label = item.get("label")
            kind = item.get("kind", "")
        else:
            raise TaskError(
                f"executor evidence[{index}] must be a label or table")
        if not isinstance(label, str) or not label.strip():
            raise TaskError(
                f"executor evidence[{index}].label must be a non-empty string")
        if kind is not None and not isinstance(kind, str):
            raise TaskError(
                f"executor evidence[{index}].kind must be a string")
        result.append({"label": _bounded(label, 200),
                       "kind": _bounded(kind or "", 80)})
    return result


def _normalize_result(raw: Any) -> dict[str, Any]:
    if raw is None:
        raise TaskError("executor returned no result")
    if not isinstance(raw, Mapping):
        raise TaskError("executor result must be a table")
    unknown = sorted(set(raw) - RESULT_KEYS)
    if unknown:
        raise TaskError(
            "executor result has unsupported key(s): "
            + ", ".join(str(key) for key in unknown))
    status = raw.get("status", "ok")
    if status not in {"ok", "error", "blocked", "no_progress"}:
        raise TaskError(
            "executor result status must be ok, error, blocked or no_progress")
    has_output = "output" in raw
    output = raw.get("output")
    if has_output:
        try:
            text = _canonical(output)
        except (TypeError, ValueError) as exc:
            raise TaskError(
                f"executor output is not JSON-serializable: {exc}") from exc
        if len(text.encode("utf-8")) > MAX_OUTPUT_BYTES:
            raise TaskError(
                f"executor output exceeds {MAX_OUTPUT_BYTES} bytes")
    state = raw.get("state")
    if state is not None:
        try:
            state = _canonical(state)[:MAX_EVENT_TEXT]
        except (TypeError, ValueError) as exc:
            raise TaskError(
                f"executor state is not JSON-serializable: {exc}") from exc
    return {
        "status": status,
        "summary": _bounded(raw.get("summary", ""), 200),
        "usage": _usage(raw.get("usage")),
        "artifacts": _artifact_specs(raw.get("artifacts")),
        "evidence": _evidence(raw.get("evidence")),
        "has_output": has_output,
        "output": output,
        "state": state,
    }


def _fingerprint(result: Mapping[str, Any],
                 artifacts: Sequence[Mapping[str, Any]]) -> str:
    payload = {
        "status": result.get("status"),
        "summary": result.get("summary"),
        "state": result.get("state"),
        "output": result.get("output") if result.get("has_output") else None,
        "artifacts": [[item.get("path"), item.get("sha256")]
                      for item in artifacts],
        "evidence": [item.get("label") for item in result.get("evidence") or []],
    }
    return _sha256_text(_canonical(payload))


# --------------------------------------------------------------------------- #
# runner
# --------------------------------------------------------------------------- #
class TaskRunner:
    """Validate, preflight and run one bounded task.

    ``executor`` is either a callable ``executor(step, context)`` or an object
    with that ``execute`` method. Its result is a table with ``status``,
    ``summary``, ``usage``, ``artifacts``, ``evidence`` and optional
    ``output`` / ``state``. The runner never calls a model or a tool.
    """

    def __init__(self, spec: TaskSpec, root: str | Path, *,
                 executor: Any = None,
                 state_dir: str | Path | None = None) -> None:
        if not isinstance(spec, TaskSpec):
            raise TypeError("spec must be a TaskSpec")
        self.spec = spec
        self.root = Path(root).expanduser().resolve()
        self.executor = executor
        self.state_dir = (Path(state_dir).expanduser().resolve()
                          if state_dir is not None else None)
        if not self.root.is_dir():
            raise TaskError(f"repository root does not exist: {self.root}")
        self._state_base = (self.state_dir if self.state_dir is not None
                            else default_state_dir(self.root))
        self._preflight_cache: dict[str, Any] | None = None
        self._preflight_item: dict[str, Any] | None = None

    @property
    def tasks_dir(self) -> Path:
        return tasks_dir(self.root, self.state_dir)

    # -- preflight --------------------------------------------------------- #
    def _worktree_item(self) -> dict[str, Any]:
        report = worktree_checks.check(self.root,
                                       base=self.spec.worktree.base)
        item = next(
            (candidate for candidate in report.get("worktrees") or []
             if Path(str(candidate.get("path"))).resolve() == self.root),
            None,
        )
        if item is None:
            raise TaskError(
                f"{self.root} is not a registered worktree of this repository")
        return item

    def _admission(self) -> dict[str, Any]:
        return resources.check_admission(
            self.root, kind="task",
            memory_mb=self.spec.resources.memory_mb,
            disk_mb=self.spec.resources.disk_mb,
            path=self.root,
        )

    def _snapshot_report(self) -> dict[str, Any]:
        dump = state_path(self.root, "graph.json")
        report: dict[str, Any] = {
            "required": self.spec.requires_graph,
            "path": str(dump),
            "state": "not_checked",
            "error": None,
            "details": {},
        }
        if not self.spec.requires_graph:
            return report
        if not dump.is_file():
            report["state"] = "missing"
            return report
        try:
            from ozagia.core.memory import DevMemory
            memory = DevMemory(
                dump=dump, journal=state_path(self.root, "journal.jsonl"),
                root=self.root,
            )
            status = memory.staleness()
        except Exception as exc:  # noqa: BLE001 - any load failure is "invalid"
            report["state"] = "invalid"
            report["error"] = _bounded(exc, 300)
            return report
        report["state"] = str(status.get("freshness") or "unknown")
        report["details"] = {
            key: status.get(key)
            for key in ("snapshot_root", "root", "root_mismatch",
                        "snapshot_head", "head", "tree_changed",
                        "verification_errors", "generated_at")
        }
        return report

    def _forbidden_reason(self, resolved: Path) -> str | None:
        try:
            relative = resolved.relative_to(self.root)
        except ValueError:
            return "outside the repository root"
        if ".git" in relative.parts:
            return "inside .git"
        try:
            resolved.relative_to(self._state_base.resolve())
        except ValueError:
            pass
        else:
            return "inside the state directory"
        return None

    def _path_report(self) -> dict[str, Any]:
        violations: list[str] = []
        for entry in self.spec.write_scope:
            resolved = (self.root / entry).resolve(strict=False)
            reason = self._forbidden_reason(resolved)
            if reason:
                violations.append(f"write_scope {entry!r} is {reason}")
        for step in self.spec.steps:
            for path in (*step.paths, *step.write_paths):
                resolved = (self.root / path).resolve(strict=False)
                reason = self._forbidden_reason(resolved)
                if reason:
                    violations.append(
                        f"step {step.id!r} path {path!r} is {reason}")
        return {
            "ok": not violations,
            "write_scope": list(self.spec.write_scope),
            "violations": violations,
        }

    def _worktree_required(self) -> bool:
        return bool(
            self.spec.worktree.expected_branch
            or self.spec.worktree.require_clean
            or self.spec.worktree.require_up_to_date
            or set(self.spec.gates.before + self.spec.gates.after)
            & WORKTREE_GATES
        )

    def _has_test_steps(self) -> bool:
        return any(step.tool == "test" for step in self.spec.steps)

    def _needs_test_plan(self) -> bool:
        return self.spec.test_policy is not None or self._has_test_steps()

    def _test_scope_paths(self) -> list[str]:
        paths: list[str] = [entry for entry in self.spec.write_scope
                            if entry not in ("", ".")]
        for step in self.spec.steps:
            for path in (*step.paths, *step.write_paths):
                if path not in ("", ".") and path not in paths:
                    paths.append(path)
        return paths

    def _test_plan(self) -> dict[str, Any] | None:
        """Compute the preflight test plan, read-only and without execution."""
        if not self._needs_test_plan():
            return None
        try:
            policy = test_plans.load_policy(
                self.root, task_class=self.spec.id,
                paths=self._test_scope_paths() or None)
            if self.spec.test_policy is not None:
                policy = test_plans.with_overrides(policy,
                                                   **self.spec.test_policy)
            return test_plans.plan(
                self.root, task_class=self.spec.id, policy=policy)
        except Exception as exc:  # noqa: BLE001 - preflight must report, not crash
            return {
                "schema": test_plans.SCHEMA,
                "available": False,
                "executed": False,
                "truth_assessed": False,
                "error": _bounded(f"{type(exc).__name__}: {exc}", 300),
                "not_run_reason": "the test plan could not be computed",
            }

    def preflight(self) -> dict[str, Any]:
        """Read-only precondition report: worktree, admission, snapshot, paths."""
        failures: list[str] = []
        warnings: list[str] = []
        item: dict[str, Any] | None = None
        worktree_error: str | None = None
        try:
            item = self._worktree_item()
        except (TaskError, worktree_checks.WorktreeError, OSError) as exc:
            worktree_error = _bounded(exc, 300)
            if self._worktree_required():
                failures.append(f"worktree check unavailable: {worktree_error}")
            else:
                warnings.append(
                    f"worktree check unavailable and not required: "
                    f"{worktree_error}")
        if item is not None:
            if not item.get("exists"):
                failures.append("worktree path does not exist")
            if item.get("prunable"):
                failures.append("git reports the worktree as prunable")
            if item.get("detached"):
                failures.append(
                    "HEAD is detached; bounded tasks require a branch")
            if item.get("operation"):
                failures.append(
                    f"a {item['operation']} operation is in progress")
            dirty = item.get("dirty") or {}
            changed = sum(int(dirty.get(key, 0)) for key in (
                "staged", "unstaged", "untracked", "conflicted"))
            if dirty.get("conflicted"):
                failures.append(
                    f"{dirty['conflicted']} conflicted path(s)")
            if self.spec.worktree.require_clean and changed:
                failures.append(
                    f"{changed} uncommitted or untracked path(s); the task "
                    "requires a clean worktree")
            elif changed:
                warnings.append(f"{changed} uncommitted or untracked path(s)")
            expected = (self.spec.worktree.expected_branch
                        or (item.get("expected") or {}).get("branch"))
            branch = item.get("branch")
            if expected and branch != expected:
                failures.append(
                    f"expected branch {expected!r}, found {branch!r}")
            behind = (item.get("base") or {}).get("behind")
            if (self.spec.worktree.require_up_to_date
                    and isinstance(behind, int) and behind > 0):
                failures.append(
                    f"branch is {behind} commit(s) behind "
                    f"{self.spec.worktree.base}")
            elif isinstance(behind, int) and behind > 0:
                warnings.append(
                    f"branch is {behind} commit(s) behind "
                    f"{self.spec.worktree.base}")
        paths = self._path_report()
        failures.extend(paths["violations"])
        admission = self._admission()
        if not admission.get("ok"):
            failures.append(
                "resource admission refused: "
                f"{admission.get('reason') or 'unknown reason'}")
        snapshot = self._snapshot_report()
        if self.spec.requires_graph and snapshot["state"] != "fresh":
            failures.append(
                f"graph snapshot is {snapshot['state']}; a fresh snapshot is "
                "required")
        test_plan = self._test_plan()
        if test_plan is not None:
            if test_plan.get("available") is False:
                warnings.append(
                    "test plan could not be computed: "
                    f"{test_plan.get('error') or 'unknown error'}")
            elif test_plan.get("refused"):
                message = (f"test policy refused to select tests: "
                           f"{test_plan.get('fallback_reason') or 'no reason'}")
                if self._has_test_steps():
                    failures.append(message)
                else:
                    warnings.append(message)
            elif test_plan.get("mode") == "none" and self._has_test_steps():
                warnings.append(
                    "test steps will be skipped: "
                    f"{test_plan.get('mode_reason') or 'the test policy is none'}")
            elif test_plan.get("fallback_used"):
                warnings.append(
                    f"test plan used the {test_plan.get('fallback')} fallback: "
                    f"{test_plan.get('fallback_reason') or 'no reason'}")
        result = {
            "schema": SCHEMA,
            "root": str(self.root),
            "checked_at": _now(),
            "ok": not failures,
            "failures": failures,
            "warnings": warnings,
            "worktree": {
                "path": str(self.root),
                "branch": (item or {}).get("branch"),
                "head": (item or {}).get("head"),
                "detached": bool((item or {}).get("detached")),
                "operation": (item or {}).get("operation"),
                "dirty": (item or {}).get("dirty"),
                "expected": (item or {}).get("expected"),
                "error": worktree_error,
            },
            "paths": paths,
            "resources": _admission_summary(admission),
            "snapshot": snapshot,
            "test_plan": test_plan,
        }
        self._preflight_cache = result
        self._preflight_item = item
        return result

    # -- gates ------------------------------------------------------------- #
    def _facts(self, phase: str) -> dict[str, Any]:
        if phase == "before" and self._preflight_cache is not None:
            cache = self._preflight_cache
            return {
                "item": self._preflight_item,
                "worktree_error": cache["worktree"].get("error"),
                "admission": cache["resources"],
                "snapshot": cache["snapshot"],
            }
        try:
            item = self._worktree_item()
            error = None
        except (TaskError, worktree_checks.WorktreeError, OSError) as exc:
            item = None
            error = _bounded(exc, 300)
        return {
            "item": item,
            "worktree_error": error,
            "admission": _admission_summary(self._admission()),
            "snapshot": self._snapshot_report(),
        }

    def _gate(self, name: str, phase: str, facts: Mapping[str, Any],
              *, output_info: Mapping[str, Any] | None,
              evidence_count: int) -> dict[str, Any]:
        try:
            return self._gate_result(name, phase, facts, output_info,
                                     evidence_count)
        except Exception as exc:  # noqa: BLE001 - a gate must report, not crash
            return _gate_result(name, phase, False,
                                f"gate could not run: {_bounded(exc, 300)}")

    def _gate_result(self, name: str, phase: str, facts: Mapping[str, Any],
                     output_info: Mapping[str, Any] | None,
                     evidence_count: int) -> dict[str, Any]:
        spec = self.spec
        item = facts.get("item")
        if name == "worktree_expected":
            if item is None:
                return _gate_result(
                    name, phase, False,
                    "no registered git worktree: "
                    f"{facts.get('worktree_error') or 'unknown error'}")
            if not item.get("exists"):
                return _gate_result(name, phase, False,
                                    "worktree path does not exist")
            if item.get("prunable"):
                return _gate_result(name, phase, False,
                                    "git reports the worktree as prunable")
            expected = item.get("expected") or {}
            if expected.get("source") == "none" and not expected.get("branch"):
                return _gate_result(
                    name, phase, True,
                    "no expected worktree mapping is configured",
                    level="not_checked")
            return _gate_result(
                name, phase, True,
                f"worktree matches its expected mapping "
                f"({expected.get('source')})")
        if name == "branch_expected":
            if item is None:
                return _gate_result(
                    name, phase, False,
                    f"worktree is unavailable: "
                    f"{facts.get('worktree_error') or 'unknown error'}")
            branch = item.get("branch")
            expected = (spec.worktree.expected_branch
                        or (item.get("expected") or {}).get("branch"))
            if item.get("detached") or not branch:
                return _gate_result(name, phase, False,
                                    "HEAD is detached; no branch to check")
            if not expected:
                return _gate_result(name, phase, True,
                                    "no expected branch is configured",
                                    level="not_checked")
            if branch != expected:
                return _gate_result(
                    name, phase, False,
                    f"expected branch {expected!r}, found {branch!r}")
            return _gate_result(name, phase, True,
                                f"on expected branch {branch!r}")
        if name == "drift_clear":
            if item is None:
                return _gate_result(
                    name, phase, False,
                    f"worktree is unavailable: "
                    f"{facts.get('worktree_error') or 'unknown error'}")
            problems: list[str] = []
            warnings: list[str] = []
            if item.get("operation"):
                problems.append(f"{item['operation']} is in progress")
            dirty = item.get("dirty") or {}
            changed = sum(int(dirty.get(key, 0)) for key in (
                "staged", "unstaged", "untracked", "conflicted"))
            if dirty.get("conflicted"):
                problems.append(f"{dirty['conflicted']} conflicted path(s)")
            if spec.worktree.require_clean and changed:
                problems.append(f"{changed} uncommitted or untracked path(s)")
            elif changed:
                warnings.append(f"{changed} uncommitted or untracked path(s)")
            behind = (item.get("base") or {}).get("behind")
            if (spec.worktree.require_up_to_date
                    and isinstance(behind, int) and behind > 0):
                problems.append(
                    f"{behind} commit(s) behind {spec.worktree.base}")
            elif isinstance(behind, int) and behind > 0:
                warnings.append(f"{behind} commit(s) behind "
                                f"{spec.worktree.base}")
            if problems:
                return _gate_result(name, phase, False,
                                    "; ".join(problems))
            if warnings:
                return _gate_result(name, phase, True, "; ".join(warnings),
                                    level="warn")
            return _gate_result(name, phase, True, "no unresolved drift")
        if name == "resources_fit":
            admission = facts.get("admission") or {}
            if admission.get("disabled"):
                return _gate_result(name, phase, True,
                                    "resource checks are disabled",
                                    level="not_checked")
            if admission.get("ok"):
                return _gate_result(
                    name, phase, True,
                    "requested memory and disk fit the host reserve",
                    details={"requested": admission.get("requested"),
                             "available": admission.get("available")})
            return _gate_result(
                name, phase, False,
                f"resource admission refused: "
                f"{admission.get('reason') or 'unknown reason'}",
                details={"requested": admission.get("requested"),
                         "shortfall": admission.get("shortfall"),
                         "suggestions": admission.get("suggestions")})
        if name == "snapshot_fresh":
            snapshot = facts.get("snapshot") or {}
            if not snapshot.get("required"):
                return _gate_result(
                    name, phase, True,
                    "the task does not require the graph; freshness was not "
                    "checked", level="not_checked")
            state = snapshot.get("state")
            if state == "fresh":
                return _gate_result(name, phase, True,
                                    "graph snapshot is fresh")
            return _gate_result(
                name, phase, False,
                f"graph snapshot is {state}"
                + (f" ({snapshot['error']})" if snapshot.get("error") else ""),
                details={"state": state, "path": snapshot.get("path")})
        if name in POLICY_GATES:
            kind = POLICY_GATES[name]
            report = worktree_checks.policy_check(
                self.root, kind, base=spec.worktree.base)
            problems = [check for check in report.get("checks") or []
                        if check.get("level") == "error"]
            details = {
                "branch": report.get("branch"),
                "checks": [
                    {"id": check.get("id"), "level": check.get("level"),
                     "message": _bounded(check.get("message"), 200)}
                    for check in report.get("checks") or []
                    if check.get("level") != "ok"
                ][:20],
            }
            if problems:
                return _gate_result(
                    name, phase, False,
                    f"{kind} policy refused: "
                    + "; ".join(_bounded(check.get("id"), 80)
                                for check in problems[:5]),
                    details=details)
            return _gate_result(name, phase, True,
                                f"{kind} policy passed", details=details)
        if name == "schema_valid":
            info = output_info or {}
            if not info.get("present"):
                return _gate_result(name, phase, False,
                                    "no step supplied an output")
            if not info.get("serializable"):
                return _gate_result(
                    name, phase, False,
                    "; ".join(info.get("errors") or ["output is not "
                                                     "JSON-serializable"]))
            errors = list(info.get("errors") or [])
            if errors:
                return _gate_result(
                    name, phase, False,
                    "output does not match the schema: "
                    + "; ".join(errors[:3]),
                    details={"errors": errors[:20]})
            if info.get("schema_declared"):
                return _gate_result(
                    name, phase, True,
                    f"output matches {spec.output}")
            return _gate_result(
                name, phase, True,
                "no output schema is declared; output is JSON-serializable",
                level="not_checked")
        if name == "evidence_present":
            if evidence_count <= 0:
                return _gate_result(
                    name, phase, False,
                    "no evidence label was recorded by any step")
            return _gate_result(name, phase, True,
                                f"{evidence_count} evidence label(s) recorded")
        return _gate_result(name, phase, False, f"unknown gate {name!r}")

    def _run_gates(self, phase: str, events: _EventLog | None,
                   *, output_info: Mapping[str, Any] | None = None,
                   evidence_count: int = 0) -> list[dict[str, Any]]:
        names = (self.spec.gates.before if phase == "before"
                 else self.spec.gates.after)
        if not names:
            return []
        facts = self._facts(phase)
        results: list[dict[str, Any]] = []
        for name in names:
            result = self._gate(name, phase, facts,
                                output_info=output_info,
                                evidence_count=evidence_count)
            results.append(result)
            if events is not None:
                events.write("gate", **result)
        return results

    # -- inputs ------------------------------------------------------------ #
    def _normalize_inputs(self, inputs: Any,
                          run_id: str | None) -> tuple[dict[str, Any], list[str]]:
        errors: list[str] = []
        values: dict[str, Any] = {}
        if inputs is None:
            inputs = {}
        if not isinstance(inputs, Mapping):
            return {}, ["inputs: must be a mapping of name to value"]
        for key, value in inputs.items():
            if not isinstance(key, str) or not NAME_RE.fullmatch(key):
                errors.append(
                    f"inputs: {key!r} must match "
                    "[A-Za-z][A-Za-z0-9_.-]{0,63}")
                continue
            values[key] = value
        declared = set(self.spec.inputs)
        unknown = sorted(set(values) - declared)
        if unknown:
            errors.append("inputs: unknown input(s): " + ", ".join(unknown))
        if "repo_root" in declared and "repo_root" not in values:
            values["repo_root"] = str(self.root)
        if "run_id" in declared and "run_id" not in values:
            values["run_id"] = run_id or "preview"
        if "state_dir" in declared and "state_dir" not in values:
            values["state_dir"] = str(self._state_base)
        missing = sorted(declared - set(values))
        if missing:
            errors.append("inputs: missing required input(s): "
                          + ", ".join(missing))
        for key, value in values.items():
            try:
                _canonical(value)
            except (TypeError, ValueError) as exc:
                errors.append(
                    f"inputs.{key}: must be JSON-serializable ({exc})")
        return values, errors

    @staticmethod
    def _input_manifest(values: Mapping[str, Any]) -> dict[str, Any]:
        digests: dict[str, str] = {}
        for key in sorted(values):
            try:
                digests[key] = _sha256_text(_canonical(values[key]))
            except (TypeError, ValueError):
                # The input is already an error; the digest must not stop the
                # preview from reporting it.
                digests[key] = _sha256_text("unserializable input")
        return {
            "names": sorted(values),
            "digests": digests,
            "digest": _sha256_text(_canonical(digests)),
        }

    # -- output ------------------------------------------------------------ #
    def _output_info(self, value: Any, present: bool) -> dict[str, Any]:
        info: dict[str, Any] = {
            "name": self.spec.output,
            "present": present,
            "digest": None,
            "schema_declared": self.spec.output_schema is not None,
            "serializable": True,
            "errors": [],
            "checked": False,
            "schema_valid": None,
            "value": value,
        }
        if not present:
            return info
        try:
            text = _canonical(value)
        except (TypeError, ValueError) as exc:
            info["serializable"] = False
            info["errors"] = [f"output is not JSON-serializable: {exc}"]
            return info
        if len(text.encode("utf-8")) > MAX_OUTPUT_BYTES:
            info["serializable"] = False
            info["errors"] = [
                f"output exceeds {MAX_OUTPUT_BYTES} bytes"]
            return info
        info["digest"] = _sha256_text(text)
        if self.spec.output_schema is not None:
            info["checked"] = True
            info["errors"] = validate_output(value, self.spec.output_schema)
            info["schema_valid"] = not info["errors"]
        return info

    # -- artifacts --------------------------------------------------------- #
    def _in_write_scope(self, path: str) -> bool:
        return _in_scope(path, self.spec.write_scope)

    def _collect_artifacts(self, step: TaskStep,
                           specs: Sequence[Mapping[str, str]]
                           ) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        for item in specs:
            raw_path = item["path"]
            candidate = Path(raw_path)
            if not candidate.is_absolute():
                candidate = self.root / candidate
            try:
                resolved = candidate.resolve(strict=True)
            except OSError as exc:
                raise TaskError(
                    f"artifact {raw_path!r} does not exist: {exc}") from exc
            try:
                relative = resolved.relative_to(self.root).as_posix()
            except ValueError:
                raise TaskError(
                    f"artifact {raw_path!r} is outside the repository root"
                ) from None
            reason = self._forbidden_reason(resolved)
            if reason:
                raise TaskError(f"artifact {relative!r} is {reason}")
            if not resolved.is_file():
                raise TaskError(
                    f"artifact {relative!r} must be a regular file")
            if not self._in_write_scope(relative):
                raise TaskError(
                    f"artifact {relative!r} is outside the declared "
                    "write_scope")
            digest, size = _hash_file(resolved)
            entries.append({
                "path": relative,
                "kind": item.get("kind") or "",
                "label": item.get("label") or "",
                "sha256": digest,
                "size": size,
                "step": step.id,
            })
        return entries

    # -- execution --------------------------------------------------------- #
    def _call_executor(self, executor: Any, step: Mapping[str, Any],
                       context: Mapping[str, Any]) -> Any:
        target = executor
        if not callable(target):
            target = getattr(executor, "execute", None)
        if not callable(target):
            raise TaskError(
                "executor must be callable or have an execute() method")
        return target(dict(step), dict(context))

    def _deadline(self, started_mono: float) -> tuple[str, str] | None:
        elapsed = time.monotonic() - started_mono
        if elapsed > self.spec.resources.wall_s:
            return ("resource_limit",
                    f"wall time {elapsed:.1f}s exceeds resources.wall_s="
                    f"{self.spec.resources.wall_s}")
        if elapsed > self.spec.budget.wall_s:
            return ("budget_exhausted",
                    f"wall time {elapsed:.1f}s exceeds budget.wall_s="
                    f"{self.spec.budget.wall_s}")
        return None

    def _approval_stop(self, step: TaskStep,
                       approved: bool) -> tuple[str, str] | None:
        if step.tool not in MUTATING_TOOLS:
            return None
        if self.spec.approval.mutate == "deny":
            return ("approval_required",
                    f"step {step.id!r} uses {step.tool!r}, but mutation is "
                    "denied by the task approval policy")
        if self.spec.approval.mutate == "ask" and not approved:
            return ("approval_required",
                    f"step {step.id!r} uses {step.tool!r} and requires "
                    "explicit approval")
        return None

    @staticmethod
    def _step_exceeded(step: TaskStep, usage: Mapping[str, Any],
                       elapsed_s: float) -> dict[str, Any] | None:
        limits: list[str] = []
        tokens = int(usage.get("tokens") or 0)
        model_calls = int(usage.get("model_calls") or 0)
        cost = float(usage.get("cost_usd") or 0.0)
        wall = max(float(elapsed_s), float(usage.get("wall_s") or 0.0))
        if step.budget.tokens is not None and tokens > step.budget.tokens:
            limits.append(
                f"tokens {tokens} > step budget {step.budget.tokens}")
        if (step.budget.model_calls is not None
                and model_calls > step.budget.model_calls):
            limits.append(
                f"model calls {model_calls} > step budget "
                f"{step.budget.model_calls}")
        if (step.budget.cost_usd is not None
                and cost > step.budget.cost_usd):
            limits.append(
                f"cost ${cost:.4f} > step budget "
                f"${step.budget.cost_usd:.4f}")
        if step.budget.wall_s is not None and wall > step.budget.wall_s:
            limits.append(
                f"wall {wall:.1f}s > step budget {step.budget.wall_s}s")
        if not limits:
            return None
        return {
            "stop": "budget_exhausted",
            "limits": limits,
            "detail": "step budget exceeded: " + "; ".join(limits),
        }

    def preview(self, inputs: Any = None) -> dict[str, Any]:
        """Validate inputs and report the plan and preflight. No writes."""
        values, errors = self._normalize_inputs(inputs, None)
        preflight = self.preflight()
        return {
            "schema": SCHEMA,
            "mode": "preview",
            "executed": False,
            "ok": not errors,
            "root": str(self.root),
            "spec": self.spec.summary(),
            "inputs": self._input_manifest(values),
            "errors": errors,
            "warnings": list(self.spec.warnings),
            "preflight": preflight,
            "test_plan": preflight.get("test_plan"),
            "steps": [step.to_dict() for step in self.spec.steps],
            "gates": self.spec.gates.to_dict(),
            "budget": self.spec.budget.to_dict(),
            "resources": self.spec.resources.to_dict(),
            "output": {
                "name": self.spec.output,
                "schema_declared": self.spec.output_schema is not None,
            },
            "executor_configured": self.executor is not None,
            "would_execute": not errors and preflight["ok"],
            "notes": [
                "preview invokes no executor and writes no evidence; "
                "pass --allow-exec to run",
            ],
        }

    def run(self, inputs: Any = None, *, allow_exec: bool = False,
            approve: bool = False, executor: Any = _UNSET,
            run_id: str | None = None) -> dict[str, Any]:
        """Preview by default; execute only with ``allow_exec``."""
        if not allow_exec:
            return self.preview(inputs)
        if executor is _UNSET:
            executor = self.executor
        if run_id is not None and (not isinstance(run_id, str)
                                   or not RUN_ID_RE.fullmatch(run_id)):
            raise TaskRunError(
                "run_id must match [A-Za-z0-9][A-Za-z0-9_.-]{0,119}")
        values, input_errors = self._normalize_inputs(inputs, run_id)
        input_manifest = self._input_manifest(values)
        chosen = run_id or _new_run_id(self.spec, input_manifest["digest"])
        if not RUN_ID_RE.fullmatch(chosen):
            raise TaskRunError("generated run id is invalid")
        run_dir = self.tasks_dir / chosen
        if run_dir.exists():
            raise TaskRunError(f"run {chosen!r} already exists under "
                               f"{self.tasks_dir}")
        events = _EventLog(run_dir / EVENTS_FILE, chosen)
        started_at = _now()
        started_mono = time.monotonic()
        preflight = self.preflight()
        events.write(
            "run_started", spec_id=self.spec.id,
            spec_version=self.spec.version, spec_digest=self.spec.digest,
            root=str(self.root), input_keys=input_manifest["names"],
            input_digest=input_manifest["digest"],
            allow_exec=True, approve=bool(approve),
        )
        events.write(
            "preflight", ok=preflight["ok"],
            failures=list(preflight["failures"]),
            warnings=list(preflight["warnings"]),
            worktree=preflight["worktree"],
            resources=preflight["resources"],
            snapshot=preflight["snapshot"],
            paths=preflight["paths"],
            test_plan=preflight.get("test_plan"),
        )
        gates: dict[str, Any] = {"before": [], "after": [],
                                 "after_skipped": None}
        if input_errors or not preflight["ok"]:
            stop = "gate_failed" if not preflight["ok"] else "error"
            problems: list[str] = []
            if input_errors:
                problems.append("inputs: " + "; ".join(input_errors))
            if not preflight["ok"]:
                problems.extend(preflight["failures"])
            detail = "; ".join(problems)
            return self._finalize(
                run_id=chosen, run_dir=run_dir, events=events,
                started_at=started_at, started_mono=started_mono,
                preflight=preflight, input_manifest=input_manifest,
                status="refused", stop=stop, detail=detail, gates=gates,
                steps=[], artifacts=[], evidence_count=0,
                ledger=_BudgetLedger(self.spec.budget, self.spec.resources),
                output_info=self._output_info({}, present=False),
                executed=False,
            )
        if executor is None:
            return self._finalize(
                run_id=chosen, run_dir=run_dir, events=events,
                started_at=started_at, started_mono=started_mono,
                preflight=preflight, input_manifest=input_manifest,
                status="refused", stop="error",
                detail="no executor is configured; this build never calls a "
                       "model or a tool itself",
                gates=gates, steps=[], artifacts=[], evidence_count=0,
                ledger=_BudgetLedger(self.spec.budget, self.spec.resources),
                output_info=self._output_info({}, present=False),
                executed=False,
            )

        ledger = _BudgetLedger(self.spec.budget, self.spec.resources)
        gates["before"] = self._run_gates("before", events)
        if any(not gate.get("ok") for gate in gates["before"]):
            failed = [gate for gate in gates["before"] if not gate.get("ok")]
            return self._finalize(
                run_id=chosen, run_dir=run_dir, events=events,
                started_at=started_at, started_mono=started_mono,
                preflight=preflight, input_manifest=input_manifest,
                status="refused", stop="gate_failed",
                detail="; ".join(_bounded(gate.get("message"), 200)
                                  for gate in failed),
                gates=gates, steps=[], artifacts=[], evidence_count=0,
                ledger=ledger,
                output_info=self._output_info({}, present=False),
                executed=False,
            )

        step_reports: list[dict[str, Any]] = []
        artifacts: list[dict[str, Any]] = []
        evidence_count = 0
        last_output: Any = None
        has_output = False
        previous_fingerprint: str | None = None
        stop: str | None = None
        detail = ""
        for index, step in enumerate(self.spec.steps):
            deadline = self._deadline(started_mono)
            if deadline is not None:
                stop, detail = deadline
                break
            approval = self._approval_stop(step, approve)
            if approval is not None:
                stop, detail = approval
                break
            planned = preflight.get("test_plan") or {}
            if step.tool == "test" and planned:
                skip_reason: str | None = None
                if planned.get("available") is False:
                    skip_reason = (
                        "test plan unavailable: "
                        f"{planned.get('error') or 'unknown error'}")
                elif planned.get("refused"):
                    skip_reason = (
                        "test policy refused to select tests: "
                        f"{planned.get('fallback_reason') or 'no reason'}")
                elif planned.get("mode") == "none":
                    skip_reason = (
                        "test policy is none: "
                        f"{planned.get('mode_reason') or 'no test selected'}")
                if skip_reason is not None:
                    events.write(
                        "step_skipped", index=index, step=step.id,
                        tool=step.tool, reason=_bounded(skip_reason, 300))
                    step_reports.append({
                        "id": step.id, "index": index, "tool": step.tool,
                        "status": "skipped", "summary": _bounded(skip_reason, 200),
                        "elapsed_s": 0.0, "usage": {}, "artifacts": 0,
                        "evidence": 0, "skipped": True,
                        "reason": _bounded(skip_reason, 300),
                    })
                    continue
            events.write(
                "step_started", index=index, step=step.id, tool=step.tool,
                paths=list(step.paths), write_paths=list(step.write_paths),
                budget=step.budget.to_dict(),
            )
            context = {
                "run_id": chosen,
                "root": str(self.root),
                "task_id": self.spec.id,
                "inputs": dict(values),
                "write_scope": list(self.spec.write_scope),
                "approval": self.spec.approval.to_dict(),
                "remaining": ledger.remaining(),
                "step_index": index,
                "steps_total": len(self.spec.steps),
            }
            step_table = step.to_dict()
            if step.tool == "test" and planned:
                context["test_plan"] = planned
                step_table["args"] = {
                    **dict(step_table.get("args") or {}),
                    "test_plan": planned,
                }
            step_start = time.monotonic()
            try:
                raw = self._call_executor(executor, step_table, context)
            except Exception as exc:  # noqa: BLE001 - report, never crash
                elapsed = round(time.monotonic() - step_start, 3)
                events.write(
                    "step_error", index=index, step=step.id,
                    error=_bounded(f"{type(exc).__name__}: {exc}", 300),
                    elapsed_s=elapsed,
                )
                step_reports.append({
                    "id": step.id, "index": index, "tool": step.tool,
                    "status": "error", "summary": _bounded(exc, 200),
                    "elapsed_s": elapsed, "artifacts": 0, "evidence": 0,
                })
                stop = "error"
                detail = f"step {step.id!r} raised {type(exc).__name__}"
                break
            elapsed = round(time.monotonic() - step_start, 3)
            try:
                result = _normalize_result(raw)
                entries = self._collect_artifacts(step, result["artifacts"])
            except TaskError as exc:
                events.write(
                    "step_error", index=index, step=step.id,
                    error=_bounded(exc, 300), elapsed_s=elapsed,
                )
                step_reports.append({
                    "id": step.id, "index": index, "tool": step.tool,
                    "status": "error", "summary": _bounded(exc, 200),
                    "elapsed_s": elapsed, "artifacts": 0, "evidence": 0,
                })
                stop = "error"
                detail = f"step {step.id!r}: {_bounded(exc, 200)}"
                break
            ledger.record(result["usage"], elapsed,
                          time.monotonic() - started_mono)
            artifacts.extend(entries)
            evidence_count += len(result["evidence"])
            if result["has_output"]:
                last_output = result["output"]
                has_output = True
            step_reports.append({
                "id": step.id,
                "index": index,
                "tool": step.tool,
                "status": result["status"],
                "summary": result["summary"],
                "elapsed_s": elapsed,
                "usage": dict(result["usage"]),
                "artifacts": len(entries),
                "evidence": len(result["evidence"]),
            })
            events.write(
                "step_finished", index=index, step=step.id,
                status=result["status"], summary=result["summary"],
                usage=dict(result["usage"]), elapsed_s=elapsed,
                artifact_count=len(entries),
                evidence_count=len(result["evidence"]),
                used=ledger.used(),
            )
            for label in result["evidence"]:
                events.write("evidence", step=step.id, label=label["label"],
                             kind=label["kind"])
            for entry in entries:
                events.write(
                    "artifact", step=step.id, path=entry["path"],
                    sha256=entry["sha256"], size=entry["size"],
                    kind=entry["kind"],
                )
            if result["status"] == "no_progress":
                stop = "no_progress"
                detail = f"step {step.id!r} reported no progress"
                break
            if result["status"] == "blocked":
                stop = "blocked"
                detail = f"step {step.id!r} is blocked"
                break
            if result["status"] == "error":
                stop = "error"
                detail = (f"step {step.id!r} failed: "
                          f"{result['summary'] or 'no detail'}")
                break
            fingerprint = _fingerprint(result, entries)
            if (previous_fingerprint is not None
                    and fingerprint == previous_fingerprint):
                events.write("repeated_state", step=step.id)
                stop = "repeated_state"
                detail = (f"step {step.id!r} repeated the previous result "
                          "with no new state")
                break
            previous_fingerprint = fingerprint
            exceeded = self._step_exceeded(step, result["usage"], elapsed)
            if exceeded is None:
                exceeded = ledger.exceeded()
            if exceeded is not None:
                events.write("budget_exceeded", **exceeded)
                stop = exceeded["stop"]
                detail = exceeded["detail"]
                break

        output_value = last_output if has_output else {}
        output_info = self._output_info(output_value, has_output)
        if stop is None:
            deadline = self._deadline(started_mono)
            if deadline is not None:
                stop, detail = deadline
        if stop is None:
            gates["after"] = self._run_gates(
                "after", events, output_info=output_info,
                evidence_count=evidence_count)
            failed = [gate for gate in gates["after"] if not gate.get("ok")]
            if failed:
                stop = "gate_failed"
                detail = "; ".join(_bounded(gate.get("message"), 200)
                                   for gate in failed)
        else:
            gates["after_skipped"] = stop
            events.write("gates_skipped", phase="after", reason=stop)
        if (stop is None and self.spec.output_schema is not None
                and not has_output):
            stop = "schema_invalid"
            detail = "no step supplied an output"
        if (stop is None and self.spec.output_schema is not None
                and output_info["errors"]):
            stop = "schema_invalid"
            detail = "output does not match the schema: " + "; ".join(
                output_info["errors"][:3])
        if stop is None and not output_info["serializable"]:
            stop = "error"
            detail = "; ".join(output_info["errors"])
        if stop is None:
            deadline = self._deadline(started_mono)
            if deadline is not None:
                stop, detail = deadline
        status = _run_status(stop, any(step.get("status") != "error"
                                       for step in step_reports))
        return self._finalize(
            run_id=chosen, run_dir=run_dir, events=events,
            started_at=started_at, started_mono=started_mono,
            preflight=preflight, input_manifest=input_manifest,
            status=status, stop=stop, detail=detail, gates=gates,
            steps=step_reports, artifacts=artifacts,
            evidence_count=evidence_count, ledger=ledger,
            output_info=output_info, executed=True,
        )

    # -- finalisation ------------------------------------------------------ #
    def _finalize(self, *, run_id: str, run_dir: Path, events: _EventLog,
                  started_at: str, started_mono: float,
                  preflight: Mapping[str, Any],
                  input_manifest: Mapping[str, Any], status: str,
                  stop: str | None, detail: str, gates: Mapping[str, Any],
                  steps: Sequence[Mapping[str, Any]],
                  artifacts: Sequence[Mapping[str, Any]], evidence_count: int,
                  ledger: _BudgetLedger, output_info: Mapping[str, Any],
                  executed: bool) -> dict[str, Any]:
        duration = round(time.monotonic() - started_mono, 3)
        ledger.set_total_wall(duration)
        finished_at = _now()
        not_assessed = self._not_assessed(
            executed_steps=len(steps), total_steps=len(self.spec.steps),
            after_skipped=gates.get("after_skipped"),
            output_info=output_info,
        )
        test_plan = (preflight.get("test_plan")
                     if isinstance(preflight, Mapping) else None)
        if isinstance(test_plan, Mapping):
            if test_plan.get("available") is False:
                not_assessed.append(
                    "the test plan was unavailable: "
                    f"{test_plan.get('error') or 'unknown error'}")
            elif test_plan.get("refused"):
                not_assessed.append(
                    "test steps were skipped because the test policy refused "
                    f"to select tests: {test_plan.get('fallback_reason')}")
            elif test_plan.get("mode") == "none":
                not_assessed.append(
                    "test steps were skipped by the test policy (none): "
                    f"{test_plan.get('mode_reason') or 'no test selected'}")
            else:
                not_assessed.append(
                    "the runner did not execute the planned tests; the plan "
                    "is recorded in the manifest and passed to test steps")
        public_output = {key: value for key, value in output_info.items()
                         if key != "value"}
        report = {
            "schema": SCHEMA,
            "mode": "exec",
            "executed": executed,
            "ok": status == "ok",
            "run_id": run_id,
            "root": str(self.root),
            "spec": self.spec.summary(),
            "inputs": dict(input_manifest),
            "started_at": started_at,
            "finished_at": finished_at,
            "duration_s": duration,
            "status": status,
            "stop": stop,
            "stop_detail": _bounded(detail, 400),
            "preflight": dict(preflight),
            "test_plan": preflight.get("test_plan"),
            "gates": {
                "before": list(gates.get("before") or []),
                "after": list(gates.get("after") or []),
                "after_skipped": gates.get("after_skipped"),
            },
            "steps": list(steps),
            "artifacts": list(artifacts),
            "evidence_count": evidence_count,
            "events_truncated": events.truncated,
            "budget": ledger.report(),
            "resources": {
                "declared": self.spec.resources.to_dict(),
                "admission": (preflight.get("resources")
                              if isinstance(preflight, Mapping) else {}),
            },
            "output": {**public_output, "value": output_info.get("value")},
            "what_was_not_assessed": not_assessed,
            "warnings": list(self.spec.warnings),
            "run_dir": str(run_dir),
            "events": str(run_dir / EVENTS_FILE),
            "artifacts_manifest": str(run_dir / ARTIFACTS_FILE),
            "manifest": str(run_dir / MANIFEST_FILE),
        }
        try:
            _write_json(run_dir / ARTIFACTS_FILE, {
                "schema": SCHEMA,
                "run_id": run_id,
                "count": len(artifacts),
                "artifacts": list(artifacts),
            })
            if output_info.get("present") and output_info.get("digest"):
                _write_json(run_dir / OUTPUT_FILE, output_info.get("value"))
            events.write(
                "run_finished", status=status, stop=stop,
                stop_detail=_bounded(detail, 400),
                duration_s=duration, executed=executed,
                steps=len(steps), artifacts=len(artifacts),
                evidence_count=evidence_count, budget=ledger.used(),
                output_digest=public_output.get("digest"),
                schema_valid=public_output.get("schema_valid"),
                what_was_not_assessed=not_assessed,
            )
            report["events_truncated"] = events.truncated
            manifest = {
                key: value for key, value in report.items()
                if key not in {"output", "run_dir", "events",
                               "artifacts_manifest", "manifest"}
            }
            manifest["output"] = public_output
            manifest["run_dir"] = str(run_dir)
            manifest["events"] = str(run_dir / EVENTS_FILE)
            manifest["artifacts_manifest"] = str(run_dir / ARTIFACTS_FILE)
            manifest["manifest"] = str(run_dir / MANIFEST_FILE)
            _write_json(run_dir / MANIFEST_FILE, manifest)
        except OSError as exc:
            raise TaskRunError(
                f"cannot write evidence for run {run_id!r}: {exc}") from exc
        return report

    def _not_assessed(self, *, executed_steps: int, total_steps: int,
                      after_skipped: str | None,
                      output_info: Mapping[str, Any]) -> list[str]:
        items = [
            "the runner did not call a model: tool and model work was "
            "delegated to the injected executor",
            "executor usage (tokens, model calls, cost, memory, disk, pids) "
            "is self-reported and accounted, not enforced by cgroups, rlimits "
            "or a watchdog",
            "network access and the executor's internal tool calls were not "
            "observed",
            f"paths outside {self.root} were not inspected",
        ]
        if not self.spec.requires_graph:
            items.append(
                "graph snapshot freshness was not checked because the task "
                "does not require the graph")
        if not set(self.spec.gates.after) & set(POLICY_GATES):
            items.append("commit, merge and push policy were not checked")
        if self.spec.output_schema is None:
            items.append(
                "output fields were not schema-checked; only JSON "
                "serializability was checked")
        if executed_steps < total_steps:
            items.append(
                f"{executed_steps} of {total_steps} steps ran; the remainder "
                "were not run")
        if after_skipped:
            items.append(
                f"after gates were skipped because the run stopped on "
                f"{after_skipped}")
        if output_info.get("present") and not output_info.get("checked"):
            items.append("the output value was not checked by a schema gate")
        return items


def _admission_summary(report: Mapping[str, Any]) -> dict[str, Any]:
    checks = [
        {"id": check.get("id"), "level": check.get("level"),
         "message": _bounded(check.get("message"), 200)}
        for check in report.get("checks") or []
        if check.get("level") != "ok"
    ][:10]
    return {
        "ok": bool(report.get("ok")),
        "disabled": bool(report.get("disabled")),
        "reason": report.get("reason"),
        "requested": report.get("requested"),
        "available": report.get("available"),
        "shortfall": report.get("shortfall"),
        "retry_after_s": report.get("retry_after_s"),
        "suggestions": list(report.get("suggestions") or [])[:5],
        "checks": checks,
    }


def _gate_result(name: str, phase: str, ok: bool, message: str, *,
                 level: str | None = None,
                 details: Mapping[str, Any] | None = None,
                 command: str | None = None) -> dict[str, Any]:
    return {
        "id": name,
        "phase": phase,
        "level": level or ("ok" if ok else "error"),
        "ok": bool(ok),
        "message": _bounded(message, 300),
        "details": dict(details or {}),
        "command": command,
    }


def _run_status(stop: str | None, ran_steps: bool) -> str:
    if stop is None:
        return "ok"
    if stop == "gate_failed" and not ran_steps:
        return "refused"
    if stop in {"budget_exhausted", "resource_limit", "no_progress",
                "repeated_state", "approval_required", "blocked"}:
        return "stopped"
    return "failed"


def _new_run_id(spec: TaskSpec, inputs_digest: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    token = hashlib.sha256(
        f"{spec.digest}:{inputs_digest}:{time.time_ns()}:{os.getpid()}"
        .encode("utf-8")
    ).hexdigest()[:8]
    name = re.sub(r"[^A-Za-z0-9_.-]+", "-", spec.id)[:40] or "task"
    return f"{name}-{stamp}-{token}"


def _hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
                size += len(chunk)
    except OSError as exc:
        raise TaskError(f"cannot hash {path}: {exc}") from exc
    return "sha256:" + digest.hexdigest(), size


# --------------------------------------------------------------------------- #
# text formatting
# --------------------------------------------------------------------------- #
def format_spec(spec: TaskSpec) -> str:
    return (f"{spec.id} v{spec.version}: {spec.title}\n"
            f"  output={spec.output} steps={len(spec.steps)} "
            f"tools={', '.join(spec.tools)}\n"
            f"  budget tokens={spec.budget.tokens} "
            f"model_calls={spec.budget.model_calls} "
            f"cost=${spec.budget.cost_usd:.2f} "
            f"wall={spec.budget.wall_s}s")


def format_validate(report: Mapping[str, Any]) -> str:
    if report.get("ok"):
        spec = report.get("spec") or {}
        lines = [
            f"ok: {spec.get('id')} v{spec.get('version')} "
            f"({spec.get('path')})",
        ]
        for warning in report.get("warnings") or []:
            lines.append(f"  warning: {warning}")
        return "\n".join(lines)
    lines = [f"invalid: {report.get('path')}"]
    for error in report.get("errors") or []:
        lines.append(f"  error: {error}")
    for warning in report.get("warnings") or []:
        lines.append(f"  warning: {warning}")
    return "\n".join(lines)


def format_preview(report: Mapping[str, Any]) -> str:
    spec = report.get("spec") or {}
    preflight = report.get("preflight") or {}
    lines = [
        f"{spec.get('id')} v{spec.get('version')}: preview "
        f"({'preconditions ok' if report.get('would_execute') else 'preconditions refused'})",
        f"  output: {spec.get('output')}  "
        f"steps: {len(report.get('steps') or [])}",
        f"  executor: "
        f"{'configured' if report.get('executor_configured') else 'not configured (execution would be refused)'}",
        f"  preflight: {'ok' if preflight.get('ok') else 'problems'} "
        f"({preflight.get('root')})",
    ]
    for failure in preflight.get("failures") or []:
        lines.append(f"  error: {failure}")
    for failure in report.get("errors") or []:
        lines.append(f"  error: {failure}")
    for warning in report.get("warnings") or []:
        lines.append(f"  warning: {warning}")
    plan = report.get("test_plan") or {}
    if plan:
        lines.append(
            f"  test plan: mode={plan.get('mode')} "
            f"selected={len(plan.get('selected_files') or [])} "
            f"fallback={plan.get('fallback_used')}"
            + (f" ({plan['fallback_reason']})"
               if plan.get("fallback_reason") else ""))
    return "\n".join(lines)


def format_run(report: Mapping[str, Any]) -> str:
    lines = [
        f"{report.get('status')}: {report.get('run_id')}"
        + (f" (stopped on {report['stop']})" if report.get("stop") else ""),
        f"  run_dir: {report.get('run_dir')}",
    ]
    if report.get("stop_detail"):
        lines.append(f"  stop: {report['stop_detail']}")
    budget = report.get("budget") or {}
    used = budget.get("used") or {}
    lines.append(
        f"  steps: {len(report.get('steps') or [])}  "
        f"artifacts: {len(report.get('artifacts') or [])}  "
        f"evidence: {report.get('evidence_count')}  "
        f"tokens: {used.get('tokens')}  wall: {used.get('wall_s')}s")
    output = report.get("output") or {}
    if output.get("digest"):
        lines.append(f"  output: {output.get('name')} {output.get('digest')} "
                     f"schema_valid={output.get('schema_valid')}")
    plan = report.get("test_plan") or {}
    if plan:
        lines.append(
            f"  test plan: mode={plan.get('mode')} "
            f"selected={len(plan.get('selected_files') or [])} "
            f"fallback={plan.get('fallback_used')}")
    for item in report.get("what_was_not_assessed") or []:
        lines.append(f"  not assessed: {item}")
    return "\n".join(lines)


def format_runs(runs: Sequence[Mapping[str, Any]]) -> str:
    if not runs:
        return "no task runs"
    lines = []
    for item in runs:
        lines.append(
            f"{item.get('status') or item.get('state'):8} "
            f"{item.get('spec_id') or '?':16} {item.get('run_id')}  "
            f"started={item.get('started_at')}  "
            f"stop={item.get('stop') or '-'}")
    return "\n".join(lines)


def format_show(report: Mapping[str, Any]) -> str:
    manifest = report.get("manifest") or {}
    lines = [
        f"{manifest.get('status', report.get('state'))}: "
        f"{report.get('run_id')}",
        f"  run_dir: {report.get('run_dir')}",
        f"  events: {len(report.get('events') or [])}  "
        f"artifacts: {len(report.get('artifacts') or [])}",
    ]
    if manifest.get("stop"):
        lines.append(f"  stop: {manifest.get('stop')} "
                     f"{manifest.get('stop_detail') or ''}")
    output = manifest.get("output") or {}
    if output.get("digest"):
        lines.append(f"  output: {output.get('name')} {output.get('digest')}")
    for error in report.get("errors") or []:
        lines.append(f"  error: {error}")
    return "\n".join(lines)


__all__ = [
    "AFTER_GATES", "BEFORE_GATES", "Budget", "DEFAULT_BUDGET",
    "DEFAULT_RESOURCES", "Gates", "KNOWN_GATES", "KNOWN_TOOLS",
    "MANDATORY_STOPS", "MUTATING_TOOLS", "POLICY_GATES", "ResourceSpec",
    "SCHEMA", "STOP_CONDITIONS", "StepBudget", "TOOL_CLASSES", "TaskError",
    "TaskRunError", "TaskRunner", "TaskSpec", "TaskSpecError", "TaskStep",
    "WorktreeSpec", "format_preview", "format_run", "format_runs",
    "format_show", "format_spec", "format_validate", "list_runs", "load_spec",
    "show_run", "tasks_dir", "validate_output", "validate_spec",
]
